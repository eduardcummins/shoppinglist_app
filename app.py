import csv
import glob
import os
import sqlite3
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
# Flask needs a secret key to sign session cookies (e.g. for flash messages).
# A fixed string is fine for a local project like this one; a real deployed
# app would use a long random value kept out of the source code.
app.secret_key = "dev"

INGREDIENT_CATEGORIES = ["Pantry", "Dairy", "Produce", "Other"]
UNIT_CHOICES = ["ml", "grams", "units"]

# __file__ is the path to this script itself, so BASE_DIR is the folder this
# file lives in. Building paths from it (instead of just "shopping.db") means
# the app finds its database/schema/CSVs no matter what folder you run it from.
BASE_DIR = os.path.dirname(__file__)
DATABASE = os.path.join(BASE_DIR, "shopping.db")
SCHEMA = os.path.join(BASE_DIR, "schema.sql")
INGREDIENTS_CSV = os.path.join(BASE_DIR, "ingredients.csv")
RECIPES_CSV = os.path.join(BASE_DIR, "recipes.csv")
SUBSTITUTIONS_GLOB = os.path.join(BASE_DIR, "substitutions_*.csv")


def get_db():
    """Open a new database connection with row access by column name."""
    db = sqlite3.connect(DATABASE)
    # By default a row from sqlite3 behaves like a tuple (row[0], row[1], ...).
    # Setting row_factory to sqlite3.Row lets us also use column names, like
    # row["name"] -- similar to what CS50's own SQL library does for you.
    db.row_factory = sqlite3.Row
    # SQLite ignores FOREIGN KEY constraints unless you turn this on for
    # every connection. With it on, SQLite will refuse to delete a row that
    # other rows still point to via a foreign key -- e.g. a recipe that
    # still has recipe_ingredients rows -- which is why every delete route
    # in this app deletes the child rows first, then the parent row.
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


def get_or_create_recipe(db, title, servings):
    """Case-insensitive recipe lookup; inserts a new row if none exists."""
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
    """Add any ingredient from ingredients.csv that isn't already in the
    database (matched by name, case-insensitive). This only ever adds rows,
    so it's safe to run on every startup and won't erase an ingredient
    that was added through the app itself."""
    db = get_db()
    with open(INGREDIENTS_CSV, newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            category = (row.get("category") or "").strip()
            if name:
                get_or_create_ingredient(db, name, category)
    db.commit()
    db.close()


def load_recipes_from_csv():
    """Add any recipe/ingredient row from recipes.csv that isn't already in
    the database. One row = one ingredient used in one recipe, so the same
    recipe title appears on several rows, once per ingredient. Like
    load_ingredients_from_csv(), this only adds rows -- it never deletes or
    changes a recipe already there, so recipes added through the New Recipe
    form aren't wiped out on the next restart."""
    db = get_db()
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

            recipe_id = get_or_create_recipe(db, title, servings)
            ingredient_id = get_or_create_ingredient(db, ingredient_name, "")

            already_have_it = db.execute(
                "SELECT 1 FROM recipe_ingredients WHERE recipe_id = ? AND ingredient_id = ?",
                (recipe_id, ingredient_id),
            ).fetchone()
            if already_have_it is None:
                db.execute(
                    """
                    INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit)
                    VALUES (?, ?, ?, ?)
                    """,
                    (recipe_id, ingredient_id, quantity, unit),
                )
    db.commit()
    db.close()


def load_dietary_substitutions():
    """Sync the dietary_substitutions table from substitutions_<diet>.csv
    files in the project root, so editing a CSV and restarting the app is
    enough to change substitution rules — no SQL required. Each file fully
    replaces that diet's rows, so deleting a row from the CSV removes it
    from the DB too."""
    db = get_db()

    # glob.glob finds every file matching a filename pattern -- here, every
    # file in the project folder named "substitutions_<something>.csv".
    for path in sorted(glob.glob(SUBSTITUTIONS_GLOB)):
        filename = os.path.basename(path)
        diet_type = filename[len("substitutions_"):-len(".csv")].replace("_", "-")

        db.execute("DELETE FROM dietary_substitutions WHERE diet_type = ?", (diet_type,))

        with open(path, newline="") as f:
            # enumerate(..., start=2) numbers rows starting at 2, not 0 or 1,
            # since csv.DictReader already consumed row 1 (the header) --
            # so row_num here matches the actual line number in the file,
            # which makes the warning messages below easier to act on.
            for row_num, row in enumerate(csv.DictReader(f), start=2):
                # row.get("ingredient") is None if that column is missing
                # from this row; "or ''" swaps None for an empty string
                # first, since None has no .strip() method.
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
        # The template turns this list into JSON for the ingredient-search
        # box's JavaScript, which needs plain dicts (not sqlite3.Row objects)
        # to convert cleanly -- so we copy each row into a dict by hand.
        ingredients = []
        for row in db.execute("SELECT id, name, category FROM ingredients ORDER BY name"):
            ingredients.append(dict(row))
        db.close()
        return render_template(
            "new_recipe.html",
            categories=INGREDIENT_CATEGORIES,
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
    ingredient_ids = request.form.getlist("ingredient_id")

    # Collect valid rows, merging repeated ingredients (same as how
    # generate() merges ingredients across recipes) so re-adding the same
    # ingredient twice doesn't violate the recipe_ingredients primary key.
    # A row picked from the autocomplete dropdown carries its ingredient_id
    # directly, so it's merged/looked up by id rather than by name — that's
    # what actually fixes "flour" vs "All-Purpose Flour" ending up as two
    # separate ingredients.
    merged = {}
    # quantities/units/names/categories/ingredient_ids are five separate
    # lists, one entry per ingredient row on the form, all in the same
    # order -- so quantities[i], units[i], names[i], etc. all describe the
    # same row. Looping over the index lets us read all five together.
    for i in range(len(quantities)):
        quantity = quantities[i]
        unit = units[i].strip()
        name = names[i].strip()
        category = categories[i]
        ingredient_id = ingredient_ids[i].strip()

        if not name or not unit or not quantity:
            continue
        try:
            quantity = float(quantity)
        except ValueError:
            continue

        # The unit field is a fixed dropdown; if a request didn't come from
        # our form and sent something else, just fall back to "units"
        # rather than trying to guess what was meant.
        if unit not in UNIT_CHOICES:
            unit = "units"

        # Rows picked from the autocomplete dropdown carry their
        # ingredient_id directly, so key on that instead of the name.
        # New-ingredient rows (no id yet) are keyed by name instead, so
        # re-adding the same brand new name twice on one form still merges.
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
                "category": category if category in INGREDIENT_CATEGORIES else "Other",
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
        # row["ingredient_id"] is set if this row was picked from the
        # autocomplete dropdown (an existing ingredient); otherwise it's
        # None, meaning this is a brand new ingredient that needs creating.
        if row["ingredient_id"] is not None:
            ingredient_id = row["ingredient_id"]
        else:
            ingredient_id = get_or_create_ingredient(db, row["name"], row["category"])

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
# Delete a recipe (and its recipe_ingredients rows). Already-generated
# shopping lists store their own copied-out ingredient rows and don't
# reference recipes at all, so this can't affect them.
# ---------------------------------------------------------------------------
@app.route("/recipes/<int:recipe_id>/delete", methods=["POST"])
def delete_recipe(recipe_id):
    db = get_db()
    db.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    db.commit()
    db.close()
    flash("Recipe deleted.")
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
    # The key is a 2-item tuple (an ingredient ID together with its unit),
    # so quantities only ever add up when both match -- e.g. sugar measured
    # in grams stays separate from sugar measured in units. "notes" is a
    # set, not a list, so the same substitution note doesn't show up twice
    # just because two different recipes both triggered it.
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

    # .items() gives us (key, value) pairs -- and since each key here is
    # itself a (ingredient_id, unit) tuple, Python lets us unpack both
    # levels at once instead of writing "for key, data in ..." and then
    # key[0]/key[1] every time we need the ingredient id or unit.
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
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    load_ingredients_from_csv()
    load_recipes_from_csv()
    load_dietary_substitutions()
    app.run(debug=True, port=5001)
