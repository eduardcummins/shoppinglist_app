-- Smart Recipe Shopping List — schema.sql

CREATE TABLE ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL -- e.g., 'Pantry', 'Dairy', 'Produce'
);

CREATE TABLE recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
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

-- Seed Data
INSERT INTO ingredients (id, name, category) VALUES
(1, 'Granulated Sugar', 'Pantry'),
(2, 'Stevia', 'Pantry'),
(3, 'All-Purpose Flour', 'Pantry'),
(4, 'Almond Flour', 'Pantry'),
(5, 'Unsalted Butter', 'Dairy'),
(6, 'Eggs', 'Dairy'),
(7, 'Whole Milk', 'Dairy'),
(8, 'Baking Powder', 'Pantry');

INSERT INTO recipes (id, title, instructions, servings) VALUES
(1, 'Chocolate Chip Cookies', 'Mix sugar, butter, and eggs. Add flour. Bake at 180°C for 12 minutes.', 4),
(2, 'Simple Pancakes', 'Whisk flour, milk, and eggs. Fry on griddle until golden on both sides.', 2);

INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit) VALUES
(1, 1, 100.0, 'grams'), -- 100g Sugar
(1, 3, 200.0, 'grams'), -- 200g Flour
(1, 5, 100.0, 'grams'), -- 100g Butter
(1, 6, 2.0, 'units'),   -- 2 Eggs
(2, 3, 150.0, 'grams'), -- 150g Flour
(2, 6, 1.0, 'units'),   -- 1 Egg
(2, 1, 20.0, 'grams'),  -- 20g Sugar
(2, 7, 200.0, 'ml'),    -- 200ml Milk
(2, 8, 5.0, 'grams');   -- 5g Baking Powder

-- Diabetic Profile: Swap Sugar (1) for Stevia (2) with a 0.1 multiplier (10g sugar = 1g stevia)
INSERT INTO dietary_substitutions (diet_type, original_ingredient_id, substitute_ingredient_id, conversion_factor, substitution_note) VALUES
('diabetic', 1, 2, 0.1, 'Swapped for Stevia (1:10 ratio) — sugar is not diabetic-friendly.');

-- Gluten-Free Profile: Swap All-Purpose Flour (3) for Almond Flour (4) at a 1:1 ratio
INSERT INTO dietary_substitutions (diet_type, original_ingredient_id, substitute_ingredient_id, conversion_factor, substitution_note) VALUES
('gluten-free', 3, 4, 1.0, 'Swapped for Almond Flour (1:1 ratio) — standard flour contains gluten.');
