from django.contrib import admin

from .models import Dish, ExtraItem, FamilyMember, Feedback, Household, Ingredient, MenuRequest, PlannedMeal, Rule


class IngredientInline(admin.TabularInline):
    model = Ingredient
    extra = 0


@admin.register(Dish)
class DishAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "minutes", "recipe_url")
    list_filter = ("kind",)
    search_fields = ("name",)
    inlines = [IngredientInline]


@admin.register(ExtraItem)
class ExtraItemAdmin(admin.ModelAdmin):
    list_display = ("week", "name", "quantity", "category", "created_by")
    date_hierarchy = "week"


@admin.register(PlannedMeal)
class PlannedMealAdmin(admin.ModelAdmin):
    list_display = ("date", "slot", "dish", "leftovers", "note", "updated_by")
    list_filter = ("slot", "leftovers")
    date_hierarchy = "date"
    autocomplete_fields = ("dish",)


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("meal", "kids", "parents", "reaction", "updated_by")
    list_filter = ("kids", "parents")


@admin.register(FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "birth_year")


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("text", "active", "created_at")
    list_filter = ("active",)


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    pass


@admin.register(MenuRequest)
class MenuRequestAdmin(admin.ModelAdmin):
    list_display = ("week", "kind", "status", "model", "cost", "web_searches", "created_by", "created_at")
    list_filter = ("status", "kind", "model")
    readonly_fields = [f.name for f in MenuRequest._meta.fields]
