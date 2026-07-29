import csv
import glob
import os
import sqlite3
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "dev" # Local testing

UNIT_CHOICES = ["ml", "grams", "units"]

BASE_DIR = os.path.dirname(__file__)
DATABASE = os.path.join(BASE_DIR, "shopping.db")
SCHEMA = os.path.join(BASE_DIR, "schema.sql")
INGREDIENTS_CSV = os.path.join(BASE_DIR, "ingredients.csv")
RECIPES_CSV = os.path.join(BASE_DIR, "recipes.csv")
SUBSTITUTIONS_GLOB = os.path.join(BASE_DIR, "substitutions_*.csv")


def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON") # refuses to delete a recipe with recipe_ingredients rows still attached
    return db


def init_db():
    db = get_db()
    with open(SCHEMA) as f:
        db.executescript(f.read())
    db.commit()
    db.close()


def resolve_ingredient_category(category):
    return category.strip() if category and category.strip() else "Other"


def get_or_create_ingredient(db, name, category):
    ingredient = db.execute(
        "SELECT * FROM ingredients WHERE LOWER(name) = LOWER(?)", (name,)
    ).fetchone()
    if ingredient is not None:
        return ingredient["id"]

    cursor = db.execute(
        "INSERT INTO ingredients (name, category) VALUES (?, ?)",
        (name, resolve_ingredient_category(category)),
    )
    return cursor.lastrowid


def get_or_create_recipe(db, title, servings):
    recipe = db.execute(
        "SELECT * FROM recipes WHERE LOWER(title) = LOWER(?)", (title,)
    ).fetchone()
    if recipe is not None:
        return recipe["id"]

    cursor = db.execute(
        "INSERT INTO recipes (title, servings) VALUES (?, ?)", (title, servings)
    )
    return cursor.lastrowid


def load_ingredients_from_csv():
    db = get_db()
    with open(INGREDIENTS_CSV, newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            category = (row.get("category") or "").strip()
            if not name:
                continue

            existing = db.execute(
                "SELECT * FROM ingredients WHERE LOWER(name) = LOWER(?)", (name,)
            ).fetchone()

            if existing is None:
                get_or_create_ingredient(db, name, category)
            elif category and category != existing["category"]:
                db.execute(
                    "UPDATE ingredients SET category = ? WHERE id = ?",
                    (category, existing["id"]),
                )
    db.commit()
    db.close()


def load_recipes_from_csv():
    db = get_db()
    # Grouped by title first so each recipe's full ingredient list is ready before any db writes.
    recipes_in_csv = {}  # title.lower() -> {"title", "servings", "ingredients"}
    with open(RECIPES_CSV, newline="") as f:
        for row in csv.DictReader(f):
            title = (row.get("title") or "").strip()
            ingredient_name = (row.get("ingredient_name") or "").strip()
            unit = (row.get("unit") or "").strip()
            if not title or not ingredient_name or not unit:
                continue

            try:
                servings = int(row.get("servings") or 1)
                quantity = float(row.get("quantity") or 0)
            except ValueError:
                continue

            if unit not in UNIT_CHOICES:
                unit = "units"

            title_key = title.lower()
            if title_key not in recipes_in_csv:
                recipes_in_csv[title_key] = {
                    "title": title,
                    "servings": servings,
                    "ingredients": {},
                }

            ingredients = recipes_in_csv[title_key]["ingredients"]
            ingredient_key = ingredient_name.lower()
            if ingredient_key not in ingredients:
                ingredients[ingredient_key] = {"name": ingredient_name, "quantity": 0.0, "unit": unit}
            ingredients[ingredient_key]["quantity"] += quantity

    for entry in recipes_in_csv.values():
        recipe = db.execute(
            "SELECT * FROM recipes WHERE LOWER(title) = LOWER(?)", (entry["title"],)
        ).fetchone()

        if recipe is None:
            recipe_id = get_or_create_recipe(db, entry["title"], entry["servings"])
        else:
            recipe_id = recipe["id"]
            db.execute(
                "UPDATE recipes SET servings = ? WHERE id = ?",
                (entry["servings"], recipe_id),
            )
            db.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))

        for line in entry["ingredients"].values():
            ingredient_id = get_or_create_ingredient(db, line["name"], "")
            db.execute(
                """
                INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit)
                VALUES (?, ?, ?, ?)
                """,
                (recipe_id, ingredient_id, line["quantity"], line["unit"]),
            )

    db.commit()
    db.close()


def load_dietary_substitutions():
    db = get_db()

    for path in sorted(glob.glob(SUBSTITUTIONS_GLOB)):
        filename = os.path.basename(path)
        diet_type = filename[len("substitutions_"):-len(".csv")].replace("_", "-")

        db.execute("DELETE FROM dietary_substitutions WHERE diet_type = ?", (diet_type,))

        with open(path, newline="") as f:
            for row_num, row in enumerate(csv.DictReader(f), start=2):
                ingredient_name = (row.get("ingredient") or "").strip()
                substitute_name = (row.get("substitute") or "").strip()
                note = (row.get("note") or "").strip()

                if not ingredient_name or not substitute_name:
                    print(f"{filename}:{row_num}: skipping row, missing ingredient/substitute")
                    continue

                try:
                    conversion_factor = float(row.get("conversion_factor") or 1.0)
                except ValueError:
                    print(f"{filename}:{row_num}: skipping row, bad conversion_factor")
                    continue

                original_id = get_or_create_ingredient(
                    db, ingredient_name, (row.get("ingredient_category") or "").strip()
                )
                substitute_id = get_or_create_ingredient(
                    db, substitute_name, (row.get("substitute_category") or "").strip()
                )

                db.execute(
                    """
                    INSERT INTO dietary_substitutions
                        (diet_type, original_ingredient_id, substitute_ingredient_id,
                         conversion_factor, substitution_note)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (diet_type, original_id, substitute_id, conversion_factor, note),
                )

    db.commit()
    db.close()


# Home: pick recipes, set servings, choose a diet profile, generate a list
DEFAULT_RECIPE_LIMIT = 6


@app.route("/")
def index():
    db = get_db()
    search = request.args.get("q", "").strip()

    selected_ids = []
    for raw_id in request.args.getlist("selected"):
        if raw_id.isdigit():
            selected_ids.append(int(raw_id))

    if search:
        base_rows = db.execute(
            "SELECT id FROM recipes WHERE LOWER(title) LIKE LOWER(?) ORDER BY title",
            (f"%{search}%",),
        ).fetchall()
    else:
        base_rows = db.execute(
            "SELECT id FROM recipes ORDER BY title LIMIT ?", (DEFAULT_RECIPE_LIMIT,)
        ).fetchall()

    recipe_ids = []
    for row in base_rows:
        recipe_ids.append(row["id"])
    for sid in selected_ids:
        if sid not in recipe_ids:
            recipe_ids.append(sid)

    recipes = []
    if recipe_ids:
        # Only values can be parameterized, not the number of placeholders, so build one "?" per id.
        placeholders = ",".join("?" for _ in recipe_ids)
        recipes = db.execute(
            f"SELECT * FROM recipes WHERE id IN ({placeholders}) ORDER BY title",
            recipe_ids,
        ).fetchall()

    total_recipe_count = db.execute("SELECT COUNT(*) c FROM recipes").fetchone()["c"]

    # Carries over an edited servings value across a search reload instead of resetting it.
    servings_overrides = {}
    for sid in selected_ids:
        raw = request.args.get(f"servings_{sid}")
        if raw:
            servings_overrides[sid] = raw

    recipes_with_ingredients = []
    for recipe in recipes:
        ingredients = db.execute(
            """
            SELECT i.name, ri.quantity, ri.unit
            FROM recipe_ingredients ri
            JOIN ingredients i ON i.id = ri.ingredient_id
            WHERE ri.recipe_id = ?
            ORDER BY i.name
            """,
            (recipe["id"],),
        ).fetchall()
        recipes_with_ingredients.append({"recipe": recipe, "ingredients": ingredients})

    diet_types = db.execute(
        "SELECT DISTINCT diet_type FROM dietary_substitutions ORDER BY diet_type"
    ).fetchall()

    db.close()
    return render_template(
        "index.html",
        recipes=recipes_with_ingredients,
        diet_types=diet_types,
        search=search,
        selected_ids=selected_ids,
        servings_overrides=servings_overrides,
        show_limit_hint=(not search) and (total_recipe_count > DEFAULT_RECIPE_LIMIT),
        total_recipe_count=total_recipe_count,
        default_limit=DEFAULT_RECIPE_LIMIT,
    )


# Add a new recipe, with a repeatable ingredient row per line
@app.route("/recipes/new", methods=["GET", "POST"])
def new_recipe():
    if request.method == "GET":
        db = get_db()
        ingredients = []
        for row in db.execute("SELECT id, name, category FROM ingredients ORDER BY name"):
            ingredients.append(dict(row))  # dict, not sqlite3.Row, so the template's tojson can serialize it
        categories = []
        for row in db.execute("SELECT DISTINCT category FROM ingredients ORDER BY category"):
            categories.append(row["category"])
        db.close()
        return render_template(
            "new_recipe.html",
            categories=categories,
            units=UNIT_CHOICES,
            ingredients=ingredients,
        )

    db = get_db()

    title = request.form.get("title", "").strip()
    servings = request.form.get("servings", "").strip()

    if not title:
        flash("Title is required.")
        db.close()
        return redirect(url_for("new_recipe"))

    try:
        servings = int(servings)
        if servings < 1:
            raise ValueError
    except ValueError:
        flash("Servings must be a whole number of at least 1.")
        db.close()
        return redirect(url_for("new_recipe"))

    quantities = request.form.getlist("quantity")
    units = request.form.getlist("unit")
    names = request.form.getlist("ingredient_name")
    categories = request.form.getlist("category")
    new_categories = request.form.getlist("new_category")  # typed name when "+ New category" was picked
    ingredient_ids = request.form.getlist("ingredient_id")

    # Merge repeated ingredients so re-adding one doesn't violate the recipe_ingredients primary key;
    # keying by id (when picked from autocomplete) rather than name avoids "flour" vs "All-Purpose Flour" duplicates.
    merged = {}
    for i in range(len(quantities)):
        quantity = quantities[i]
        unit = units[i].strip()
        name = names[i].strip()
        category = categories[i].strip()
        new_category = new_categories[i].strip()
        ingredient_id = ingredient_ids[i].strip()

        if not name or not unit or not quantity:
            continue
        try:
            quantity = float(quantity)
        except ValueError:
            continue

        if unit not in UNIT_CHOICES:
            unit = "units"

        if ingredient_id.isdigit():
            key = ("id", int(ingredient_id))
        else:
            key = ("name", name.lower())

        if key not in merged:
            merged[key] = {
                "ingredient_id": int(ingredient_id) if ingredient_id.isdigit() else None,
                "name": name,
                "unit": unit,
                "quantity": 0.0,
                "category": category or new_category or "Other",
            }
        merged[key]["quantity"] += quantity

    if not merged:
        flash("Add at least one ingredient.")
        db.close()
        return redirect(url_for("new_recipe"))

    cursor = db.execute(
        "INSERT INTO recipes (title, servings) VALUES (?, ?)",
        (title, servings),
    )
    recipe_id = cursor.lastrowid

    for row in merged.values():
        if row["ingredient_id"] is not None:
            ingredient_id = row["ingredient_id"]
        else:
            ingredient_id = get_or_create_ingredient(db, row["name"], row["category"])

        quantity = round(row["quantity"], 2)
        db.execute(
            """
            INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit)
            VALUES (?, ?, ?, ?)
            """,
            (recipe_id, ingredient_id, quantity, row["unit"]),
        )

    db.commit()
    db.close()

    flash(f'Recipe "{title}" added.')
    return redirect(url_for("index"))


# Deleting a recipe can't affect already-generated lists: those store their own copied-out ingredient data.
@app.route("/recipes/<int:recipe_id>/delete", methods=["POST"])
def delete_recipe(recipe_id):
    db = get_db()
    db.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    db.commit()
    db.close()

    flash("Recipe deleted.")
    return redirect(url_for("index"))


# Generate a shopping list from the selected recipes + servings + diet
@app.route("/generate", methods=["POST"])
def generate():
    db = get_db()

    selected_recipe_ids = request.form.getlist("use_recipe")
    if not selected_recipe_ids:
        flash("Select at least one recipe first.")
        db.close()
        return redirect(url_for("index"))

    diet_type = request.form.get("diet_type", "none")
    list_name = request.form.get("name", "").strip()

    aggregated = {}  # (ingredient_id, unit) -> {"quantity": float, "notes": set()}

    for recipe_id in selected_recipe_ids:
        recipe = db.execute(
            "SELECT * FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        if recipe is None:
            continue

        requested_servings = request.form.get(
            f"servings_{recipe_id}", recipe["servings"]
        )
        try:
            requested_servings = float(requested_servings)
        except ValueError:
            requested_servings = recipe["servings"]
        multiplier = requested_servings / recipe["servings"]

        ingredients = db.execute(
            "SELECT * FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,)
        ).fetchall()

        for ri in ingredients:
            quantity = ri["quantity"] * multiplier
            final_ingredient_id = ri["ingredient_id"]
            unit = ri["unit"]
            note = None

            if diet_type != "none":
                sub = db.execute(
                    """
                    SELECT * FROM dietary_substitutions
                    WHERE diet_type = ? AND original_ingredient_id = ?
                    """,
                    (diet_type, ri["ingredient_id"]),
                ).fetchone()
                if sub is not None:
                    final_ingredient_id = sub["substitute_ingredient_id"]
                    quantity = quantity * sub["conversion_factor"]
                    note = sub["substitution_note"]

            key = (final_ingredient_id, unit)
            if key not in aggregated:
                aggregated[key] = {"quantity": 0.0, "notes": set()}
            aggregated[key]["quantity"] += quantity
            if note:
                aggregated[key]["notes"].add(note)

    cursor = db.execute(
        "INSERT INTO shopping_lists (name, created_at, diet_type) VALUES (?, ?, ?)",
        (list_name or None, datetime.now().isoformat(timespec="seconds"), diet_type),
    )
    list_id = cursor.lastrowid

    if not list_name:
        db.execute(
            "UPDATE shopping_lists SET name = ? WHERE id = ?",
            (f"Shopping List #{list_id}", list_id),
        )

    for (ingredient_id, unit), data in aggregated.items():
        db.execute(
            """
            INSERT INTO shopping_list_items
                (shopping_list_id, ingredient_id, quantity, unit, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                list_id,
                ingredient_id,
                round(data["quantity"], 2),
                unit,
                " / ".join(data["notes"]) if data["notes"] else None,
            ),
        )

    db.commit()
    db.close()
    return redirect(url_for("view_list", list_id=list_id))


# History of previously generated lists
@app.route("/lists")
def all_lists():
    db = get_db()
    lists = db.execute(
        """
        SELECT sl.*,
               COUNT(sli.id) AS item_count,
               SUM(sli.checked) AS checked_count
        FROM shopping_lists sl
        LEFT JOIN shopping_list_items sli ON sli.shopping_list_id = sl.id
        GROUP BY sl.id
        ORDER BY sl.created_at DESC
        """
    ).fetchall()
    db.close()
    return render_template("history.html", lists=lists)


# View a single shopping list, grouped by aisle/category
@app.route("/list/<int:list_id>")
def view_list(list_id):
    db = get_db()
    shopping_list = db.execute(
        "SELECT * FROM shopping_lists WHERE id = ?", (list_id,)
    ).fetchone()
    if shopping_list is None:
        db.close()
        flash("That shopping list doesn't exist.")
        return redirect(url_for("index"))

    items = db.execute(
        """
        SELECT sli.*, i.name AS ingredient_name, i.category AS category
        FROM shopping_list_items sli
        JOIN ingredients i ON i.id = sli.ingredient_id
        WHERE sli.shopping_list_id = ?
        ORDER BY i.category, i.name
        """,
        (list_id,),
    ).fetchall()

    ingredients = []
    for row in db.execute("SELECT id, name, category FROM ingredients ORDER BY name"):
        ingredients.append(dict(row))
    categories = []
    for row in db.execute("SELECT DISTINCT category FROM ingredients ORDER BY category"):
        categories.append(row["category"])

    db.close()

    grouped = {}
    for item in items:
        category = item["category"]
        if category not in grouped:
            grouped[category] = []
        grouped[category].append(item)

    total = len(items)
    checked = 0
    for item in items:
        if item["checked"]:
            checked += 1

    return render_template(
        "list.html",
        shopping_list=shopping_list,
        grouped=grouped,
        total=total,
        checked=checked,
        ingredients=ingredients,
        categories=categories,
        units=UNIT_CHOICES,
    )


# Add a manual, ad-hoc item to an existing shopping list (not tied to any recipe)
@app.route("/list/<int:list_id>/add-item", methods=["POST"])
def add_list_item(list_id):
    db = get_db()
    shopping_list = db.execute(
        "SELECT id FROM shopping_lists WHERE id = ?", (list_id,)
    ).fetchone()
    if shopping_list is None:
        db.close()
        flash("That shopping list doesn't exist.")
        return redirect(url_for("index"))

    name = request.form.get("ingredient_name", "").strip()
    ingredient_id = request.form.get("ingredient_id", "").strip()
    category = request.form.get("category", "").strip()
    new_category = request.form.get("new_category", "").strip()
    quantity = request.form.get("quantity", "").strip()
    unit = request.form.get("unit", "").strip()

    if not name or not quantity or not unit:
        db.close()
        flash("Fill in an ingredient, quantity, and unit.")
        return redirect(url_for("view_list", list_id=list_id))

    try:
        quantity = float(quantity)
    except ValueError:
        db.close()
        flash("Quantity must be a number.")
        return redirect(url_for("view_list", list_id=list_id))

    if unit not in UNIT_CHOICES:
        unit = "units"

    if ingredient_id.isdigit():
        resolved_ingredient_id = int(ingredient_id)
    else:
        resolved_ingredient_id = get_or_create_ingredient(
            db, name, category or new_category or "Other"
        )

    quantity = round(quantity, 2)

    # Same merge rule as generate(): bump an existing (ingredient, unit) row instead of duplicating it.
    existing_item = db.execute(
        """
        SELECT id, quantity FROM shopping_list_items
        WHERE shopping_list_id = ? AND ingredient_id = ? AND unit = ?
        """,
        (list_id, resolved_ingredient_id, unit),
    ).fetchone()

    if existing_item is not None:
        db.execute(
            "UPDATE shopping_list_items SET quantity = ? WHERE id = ?",
            (round(existing_item["quantity"] + quantity, 2), existing_item["id"]),
        )
    else:
        db.execute(
            """
            INSERT INTO shopping_list_items (shopping_list_id, ingredient_id, quantity, unit)
            VALUES (?, ?, ?, ?)
            """,
            (list_id, resolved_ingredient_id, quantity, unit),
        )
    db.commit()
    db.close()

    flash(f'Added "{name}" to the list.')
    return redirect(url_for("view_list", list_id=list_id))


# Toggle a single item's checked state (used at the store)
@app.route("/list/<int:list_id>/toggle/<int:item_id>", methods=["POST"])
def toggle_item(list_id, item_id):
    db = get_db()
    db.execute(
        "UPDATE shopping_list_items SET checked = 1 - checked WHERE id = ? AND shopping_list_id = ?",
        (item_id, list_id),
    )
    db.commit()
    db.close()
    return redirect(url_for("view_list", list_id=list_id))


# Delete a single shopping list (and its items)
@app.route("/list/<int:list_id>/delete", methods=["POST"])
def delete_list(list_id):
    db = get_db()
    db.execute("DELETE FROM shopping_list_items WHERE shopping_list_id = ?", (list_id,))
    db.execute("DELETE FROM shopping_lists WHERE id = ?", (list_id,))
    db.commit()
    db.close()
    flash("List deleted.")
    return redirect(url_for("all_lists"))


def seed_from_csv_if_empty():
    # Only seeds empty tables, so restarts (incl. the debug reloader) don't undo an in-app delete.
    db = get_db()
    ingredients_empty = db.execute("SELECT COUNT(*) c FROM ingredients").fetchone()["c"] == 0
    recipes_empty = db.execute("SELECT COUNT(*) c FROM recipes").fetchone()["c"] == 0
    db.close()

    if ingredients_empty:
        load_ingredients_from_csv()
    if recipes_empty:
        load_recipes_from_csv()


if __name__ == "__main__":
    init_db()
    seed_from_csv_if_empty()
    load_dietary_substitutions()
    app.run(debug=True, port=5001)
