-- Smart Recipe Shopping List — schema.sql
--
-- This file only creates empty tables. All seed data (ingredients, recipes,
-- diet substitutions) is loaded from CSV files in the project folder by
-- app.py on startup -- see ingredients.csv, recipes.csv, and
-- substitutions_<diet>.csv. Edit those to add data; there's nothing to
-- change here.

CREATE TABLE ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL -- e.g., 'Pantry', 'Dairy', 'Produce'
);

CREATE TABLE recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    servings INTEGER NOT NULL DEFAULT 4
);

CREATE TABLE recipe_ingredients (
    recipe_id INTEGER,
    ingredient_id INTEGER,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY (recipe_id, ingredient_id),
    FOREIGN KEY (recipe_id) REFERENCES recipes(id),
    FOREIGN KEY (ingredient_id) REFERENCES ingredients(id)
);

-- Note: conversion_factor adjusts non 1-to-1 ratios (e.g., 0.1 for Stevia replacing Sugar)
CREATE TABLE dietary_substitutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    diet_type TEXT NOT NULL, -- e.g., 'diabetic', 'gluten-free'
    original_ingredient_id INTEGER,
    substitute_ingredient_id INTEGER,
    conversion_factor REAL NOT NULL DEFAULT 1.0,
    substitution_note TEXT NOT NULL,
    FOREIGN KEY (original_ingredient_id) REFERENCES ingredients(id),
    FOREIGN KEY (substitute_ingredient_id) REFERENCES ingredients(id)
);

-- A generated shopping list: one per "generate" action, tied to the diet
-- profile that was active, so a list is reproducible/explainable later.
CREATE TABLE shopping_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    diet_type TEXT NOT NULL DEFAULT 'none'
);

-- Individual line items on a generated list. Quantities are already merged
-- across recipes and substituted for the chosen diet by the time they land
-- here — this table is the *result*, not raw recipe data.
CREATE TABLE shopping_list_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shopping_list_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    checked INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    FOREIGN KEY (shopping_list_id) REFERENCES shopping_lists(id),
    FOREIGN KEY (ingredient_id) REFERENCES ingredients(id)
);

