"""Views, by area. URLs refer to them as views.<name>."""
from .family import family, household_edit, member_edit, rule_edit, rule_toggle, usual_week
from .menu import feedback, feedback_quick, home, ingredients, meal_edit, menu, menu_copy
from .planning import meal_replace, menu_create, menu_request, menu_request_status
from .recipes import (
    recipe,
    recipe_edit,
    recipe_import_discard,
    recipe_import_retry,
    recipe_import_status,
    recipe_import_view,
    recipe_photo_add,
    recipe_photo_delete,
    recipe_photo_file,
    recipe_photo_new,
    recipe_status,
    recipes,
)
from .shopping import extra_add, extra_delete, extra_restore, shopping, shopping_state, shopping_toggle

__all__ = [
    "extra_add", "extra_delete", "extra_restore", "family", "feedback", "feedback_quick", "home",
    "household_edit", "ingredients", "meal_edit", "meal_replace", "member_edit", "menu", "menu_create",
    "menu_copy", "menu_request", "menu_request_status", "rule_edit", "rule_toggle", "shopping", "shopping_state",
    "shopping_toggle", "usual_week", "recipes", "recipe", "recipe_edit", "recipe_status",
    "recipe_photo_new", "recipe_import_view", "recipe_import_retry", "recipe_import_status", "recipe_photo_file",
    "recipe_photo_add", "recipe_photo_delete", "recipe_import_discard",
]
