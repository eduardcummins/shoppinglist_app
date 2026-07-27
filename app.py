import csv
import glob
import os
import sqlite3
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "dev"

INGREDIENT_CATEGORIES = ["Pantry", "Dairy", "Produce", "Other"]
UNIT_CHOICES = ["ml", "grams", "units"]

# Historic free-text unit values (from before the unit field became a fixed
# dropdown) get collapsed onto one of UNIT_CHOICES. Anything not listed here
# falls back to "units" as the closest generic match.
UNIT_SYNONYMS = {
    "ml": "ml", "milliliter": "ml", "milliliters": "ml", "millilitre": "ml",
    "millilitres": "ml", "l": "ml", "liter": "ml", "liters": "ml", "litre": "ml",
    "litres": "ml", "tsp": "ml", "teaspoon": "ml", "teaspoons": "ml",
    "tbsp": "ml", "tablespoon": "ml", "tablespoons": "ml", "cup": "ml", "cups": "ml",

    "g": "grams", "gram": "grams", "grams": "grams", "gs": "grams",
    "kg": "grams", "kilogram": "grams", "kilograms": "grams",
    "oz": "grams", "ounce": "grams", "ounces": "grams",
    "lb": "grams", "lbs": "grams", "pound": "grams", "pounds": "grams",

    "unit": "units", "units": "units", "piece": "units", "pieces": "units",
    "pc": "units", "pcs": "units", "whole": "units", "clove": "units",
    "cloves": "units", "slice": "units", "slices": "units", "can": "units",
    "cans": "units", "jar": "units", "jars": "units", "pack": "units",
    "packet": "units", "packets": "units", "each": "units",
}


def normalize_unit(raw):
    return UNIT_SYNONYMS.get(raw.strip().lower(), "units")

BASE_DIR = os.path.dirname(__file__)
DATABASE = os.path.join(BASE_DIR, "shopping.db")
SCHEMA = os.path.join(BASE_DIR, "schema.sql")
SUBSTITUTIONS_GLOB = os.path.join(BASE_DIR, "substitutions_*.csv")


def get_db():
    """Open a new database connection with row access by column name."""
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    """(Re)create the database from schema.sql. Only runs if the db file
    doesn't already exist, so restarting the app doesn't wipe checked-off
    lists."""
    if os.path.exists(DATABASE):
        return
    db = get_db()
    with open(SCHEMA) as f:
        db.executescript(f.read())
    db.commit()
    db.close()


def migrate_db():
    """Add columns introduced after the db file was first created, so an
    existing shopping.db picks up schema changes without losing data."""
    db = get_db()
    columns = {row["name"] for row in db.execute("PRAGMA table_info(shopping_lists)")}
    if "name" not in columns:
        db.execute("ALTER TABLE shopping_lists ADD COLUMN name TEXT")
    db.commit()
    db.close()


def normalize_units_db():
    """One-time cleanup: collapse any historic free-text unit values in
    recipe_ingredients/shopping_list_items onto UNIT_CHOICES. Safe to run on
    every startup — once a row is normalized, it's a no-op from then on."""
    db = get_db()
    for table in ("recipe_ingredients", "shopping_list_items"):
        rows = db.execute(f"SELECT DISTINCT unit FROM {table}").fetchall()
        for row in rows:
            raw = row["unit"]
            canonical = normalize_unit(raw)
            if canonical != raw:
                db.execute(f"UPDATE {table} SET unit = ? WHERE unit = ?", (canonical, raw))
    db.commit()
    db.close()


def get_or_create_ingredient(db, name, category):
    """Case-insensitive ingredient lookup; inserts a new row if none exists."""
    ingredient = db.execute(
        "SELECT * FROM ingredients WHERE LOWER(name) = LOWER(?)", (name,)
    ).fetchone()
    if ingredient is not None:
        return ingredient["id"]

    category = category if category in INGREDIENT_CATEGORIES else "Other"
    cursor = db.execute(
        "INSERT INTO ingredients (name, category) VALUES (?, ?)", (name, category)
    )
    return cursor.lastrowid


def load_dietary_substitutions():
    """Sync the dietary_substitutions table from substitutions_<diet>.csv
    files in the project root, so editing a CSV and restarting the app is
    enough to change substitution rules — no SQL required. Each file fully
    replaces that diet's rows, so deleting a row from the CSV removes it
    from the DB too."""
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


# ---------------------------------------------------------------------------
# Home: pick recipes, set servings, choose a diet profile, generate a list
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    db = get_db()
    search = request.args.get("q", "").strip()
    if search:
        recipes = db.execute(
            "SELECT * FROM recipes WHERE LOWER(title) LIKE LOWER(?) ORDER BY title",
            (f"%{search}%",),
        ).fetchall()
    else:
        recipes = db.execute("SELECT * FROM recipes ORDER BY title").fetchall()

    # Attach each recipe's ingredient list so the page can preview them
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
    )


# ---------------------------------------------------------------------------
# Add a new recipe, with a repeatable ingredient row per line
# ---------------------------------------------------------------------------
@app.route("/recipes/new", methods=["GET", "POST"])
def new_recipe():
    if request.method == "GET":
        db = get_db()
        ingredients = [
            dict(row)
            for row in db.execute("SELECT id, name, category FROM ingredients ORDER BY name")
        ]
        db.close()
        return render_template(
            "new_recipe.html",
            categories=INGREDIENT_CATEGORIES,
            units=UNIT_CHOICES,
            ingredients=ingredients,
        )

    db = get_db()

    title = request.form.get("title", "").strip()
    instructions = request.form.get("instructions", "").strip()
    servings = request.form.get("servings", "").strip()

    if not title or not instructions:
        flash("Title and instructions are required.")
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
    ingredient_ids = request.form.getlist("ingredient_id")

    # Collect valid rows, merging repeated ingredients (same as how
    # generate() merges ingredients across recipes) so re-adding the same
    # ingredient twice doesn't violate the recipe_ingredients primary key.
    # A row picked from the autocomplete dropdown carries its ingredient_id
    # directly, so it's merged/looked up by id rather than by name — that's
    # what actually fixes "flour" vs "All-Purpose Flour" ending up as two
    # separate ingredients.
    merged = {}
    for quantity, unit, name, category, ingredient_id in zip(
        quantities, units, names, categories, ingredient_ids
    ):
        name = name.strip()
        ingredient_id = ingredient_id.strip()
        if not name or not unit or not quantity:
            continue
        try:
            quantity = float(quantity)
        except ValueError:
            continue

        # The unit field is a fixed dropdown, but normalize server-side too
        # in case the request didn't come from our form.
        unit = unit.strip() if unit.strip() in UNIT_CHOICES else normalize_unit(unit)

        key = ("id", int(ingredient_id)) if ingredient_id.isdigit() else ("name", name.lower())
        if key not in merged:
            merged[key] = {
                "ingredient_id": int(ingredient_id) if ingredient_id.isdigit() else None,
                "name": name,
                "unit": unit,
                "quantity": 0.0,
                "category": category if category in INGREDIENT_CATEGORIES else "Other",
            }
        merged[key]["quantity"] += quantity

    if not merged:
        flash("Add at least one ingredient.")
        db.close()
        return redirect(url_for("new_recipe"))

    cursor = db.execute(
        "INSERT INTO recipes (title, instructions, servings) VALUES (?, ?, ?)",
        (title, instructions, servings),
    )
    recipe_id = cursor.lastrowid

    for row in merged.values():
        ingredient_id = row["ingredient_id"] or get_or_create_ingredient(
            db, row["name"], row["category"]
        )

        db.execute(
            """
            INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit)
            VALUES (?, ?, ?, ?)
            """,
            (recipe_id, ingredient_id, round(row["quantity"], 2), row["unit"]),
        )

    db.commit()
    db.close()
    flash(f'Recipe "{title}" added.')
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Generate a shopping list from the selected recipes + servings + diet
# ---------------------------------------------------------------------------
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

    # aggregated[(final_ingredient_id, unit)] = {"quantity": float, "notes": set()}
    aggregated = {}

    for recipe_id in selected_recipe_ids:
        recipe = db.execute(
            "SELECT * FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        if recipe is None:
            continue

        # How many servings did the user ask for this recipe?
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


# ---------------------------------------------------------------------------
# View a single shopping list, grouped by aisle/category
# ---------------------------------------------------------------------------
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
    db.close()

    # Group items by category for aisle-by-aisle display
    grouped = {}
    for item in items:
        grouped.setdefault(item["category"], []).append(item)

    total = len(items)
    checked = sum(1 for item in items if item["checked"])

    return render_template(
        "list.html",
        shopping_list=shopping_list,
        grouped=grouped,
        total=total,
        checked=checked,
    )


# ---------------------------------------------------------------------------
# Toggle a single item's checked state (used at the store)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# History of previously generated lists
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Delete a single shopping list (and its items)
# ---------------------------------------------------------------------------
@app.route("/list/<int:list_id>/delete", methods=["POST"])
def delete_list(list_id):
    db = get_db()
    db.execute("DELETE FROM shopping_list_items WHERE shopping_list_id = ?", (list_id,))
    db.execute("DELETE FROM shopping_lists WHERE id = ?", (list_id,))
    db.commit()
    db.close()
    flash("List deleted.")
    return redirect(url_for("all_lists"))


# ---------------------------------------------------------------------------
# Delete every shopping list (and their items)
# ---------------------------------------------------------------------------
@app.route("/lists/delete-all", methods=["POST"])
def delete_all_lists():
    db = get_db()
    db.execute("DELETE FROM shopping_list_items")
    db.execute("DELETE FROM shopping_lists")
    db.commit()
    db.close()
    flash("All lists deleted.")
    return redirect(url_for("all_lists"))


if __name__ == "__main__":
    init_db()
    migrate_db()
    normalize_units_db()
    load_dietary_substitutions()
    app.run(debug=True, port=5001)
