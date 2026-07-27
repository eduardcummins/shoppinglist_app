"""
Smart Recipe Shopping List
--------------------------
A CS50-style Flask + SQLite app.

Target shopper: someone managing a dietary condition (e.g. diabetic,
gluten-free) who cooks from recipes and needs a shopping list that
automatically swaps ingredients they should avoid — at the correct
converted quantity, not just a straight 1:1 swap — and that they can
still check off once they're in the store.
"""

import os
import sqlite3
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "dev"  # fine for a local case-study app

DATABASE = os.path.join(os.path.dirname(__file__), "shopping.db")
SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


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


# ---------------------------------------------------------------------------
# Home: pick recipes, set servings, choose a diet profile, generate a list
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    db = get_db()
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
        "index.html", recipes=recipes_with_ingredients, diet_types=diet_types
    )


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
        "INSERT INTO shopping_lists (created_at, diet_type) VALUES (?, ?)",
        (datetime.now().isoformat(timespec="seconds"), diet_type),
    )
    list_id = cursor.lastrowid

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


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
