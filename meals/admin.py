from django.contrib import admin

from .models import (
    AIUsage,
    Dish,
    ExtraItem,
    FamilyMember,
    Feedback,
    Household,
    Ingredient,
    MenuRequest,
    PlannedMeal,
    RecipeImport,
    RecipePhoto,
    Rule,
)


class RecipePhotoInline(admin.TabularInline):
    model = RecipePhoto
    fk_name = "dish"
    extra = 0
    fields = ("image", "uploaded_by", "created_at")
    readonly_fields = ("uploaded_by", "created_at")


class IngredientInline(admin.TabularInline):
    model = Ingredient
    extra = 0


@admin.register(Dish)
class DishAdmin(admin.ModelAdmin):
    list_display = ("name", "household", "kind", "status", "minutes", "recipe_url")
    list_filter = ("household", "kind", "status")
    search_fields = ("name",)
    inlines = [IngredientInline, RecipePhotoInline]


@admin.register(ExtraItem)
class ExtraItemAdmin(admin.ModelAdmin):
    list_display = ("week", "name", "quantity", "category", "household", "created_by")
    list_filter = ("household",)
    date_hierarchy = "week"


@admin.register(PlannedMeal)
class PlannedMealAdmin(admin.ModelAdmin):
    list_display = ("date", "slot", "dish", "leftovers", "note", "household", "updated_by")
    list_filter = ("household", "slot", "leftovers")
    date_hierarchy = "date"
    autocomplete_fields = ("dish",)


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("meal", "kids", "parents", "reaction", "updated_by")
    list_filter = ("kids", "parents")


@admin.register(FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "birth_year", "household")
    list_filter = ("household",)


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("text", "active", "household", "created_at")
    list_filter = ("household", "active")


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = ("__str__", "created_at", "ai_approved", "ai_daily_limit", "is_active")
    list_filter = ("ai_approved", "is_active")
    search_fields = ("name", "members__email")


@admin.register(MenuRequest)
class MenuRequestAdmin(admin.ModelAdmin):
    list_display = ("week", "kind", "status", "model", "cost", "web_searches", "household", "created_by", "created_at")
    list_filter = ("status", "kind", "model", "household")
    readonly_fields = [f.name for f in MenuRequest._meta.fields]


@admin.register(RecipeImport)
class RecipeImportAdmin(admin.ModelAdmin):
    list_display = ("created_at", "status", "dish", "model", "cost", "household", "created_by")
    list_filter = ("status", "household")
    readonly_fields = [f.name for f in RecipeImport._meta.fields]


@admin.register(AIUsage)
class AIUsageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "household", "user", "kind", "model", "cost", "web_searches")
    list_filter = ("kind", "model", "household")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AIUsage._meta.fields]
