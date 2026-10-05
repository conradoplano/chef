"""Views, by area. URLs refer to them as views.<name>."""
from .family import family, household_edit, member_edit, rule_edit, rule_toggle, usual_week
from .family import weekly_item_add, weekly_item_delete
from .manage import manage, manage_household
from .menu import feedback, feedback_quick, home, ingredients, meal_edit, menu, menu_copy
from .people import household_rename, person_invite, person_remove
from .planning import meal_replace, menu_create, menu_request, menu_request_status
from .recipes import (
    recipe,
    recipe_add,
    recipe_edit,
    recipe_import_discard,
    recipe_import_retry,
    recipe_import_status,
    recipe_import_view,
    meal_pick,
    recipe_link_new,
    recipe_photo_add,
    recipe_photo_delete,
    recipe_photo_file,
    recipe_photo_new,
    recipe_status,
    recipes,
)
from .shopping import (
    extra_add,
    extra_delete,
    extra_restore,
    shopping,
    shopping_state,
    shopping_toggle,
    staple_add,
    staple_remove,
)

__all__ = [
    "extra_add", "extra_delete", "extra_restore", "family", "feedback", "feedback_quick", "home",
    "household_edit", "ingredients", "meal_edit", "meal_replace", "member_edit", "menu", "menu_create",
    "menu_copy", "menu_request", "menu_request_status", "rule_edit", "rule_toggle", "shopping", "shopping_state",
    "shopping_toggle", "usual_week", "recipes", "recipe", "recipe_edit", "recipe_status",
    "recipe_photo_new", "recipe_import_view", "recipe_import_retry", "recipe_import_status", "recipe_photo_file",
    "recipe_photo_add", "recipe_photo_delete", "recipe_import_discard", "recipe_add", "recipe_link_new", "staple_add", "staple_remove", "weekly_item_add", "weekly_item_delete", "meal_pick",
    "manage", "manage_household", "household_rename", "person_invite", "person_remove",
]
