from django import forms

from .models import Dish, ExtraItem, FamilyMember, Feedback, Household, Ingredient, PlannedMeal, Rule


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class PlannedMealForm(forms.ModelForm):
    """Edits a planned meal together with its dish, which is picked or created by name."""

    dish_name = forms.CharField(
        label="Dish", max_length=200, widget=forms.TextInput(attrs={"list": "dishes", "autocomplete": "off"})
    )
    kind = forms.ChoiceField(label="Type", choices=Dish.Kind.choices, initial=Dish.Kind.OTHER)
    recipe_url = forms.URLField(label="Recipe link", max_length=500, required=False, assume_scheme="https")
    minutes = forms.IntegerField(label="Cooking time (min)", min_value=0, max_value=1000, required=False)

    class Meta:
        model = PlannedMeal
        fields = ["date", "slot", "leftovers", "note", "eaters", "servings"]
        widgets = {"date": DateInput(), "eaters": forms.CheckboxSelectMultiple}

    field_order = ["date", "slot", "dish_name", "kind", "recipe_url", "minutes", "leftovers", "note", "eaters", "servings"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            dish = self.instance.dish
            self.initial.update(dish_name=dish.name, kind=dish.kind, recipe_url=dish.recipe_url, minutes=dish.minutes)

    def clean_dish_name(self):
        return " ".join(self.cleaned_data["dish_name"].split())

    def save(self, commit=True):
        data = self.cleaned_data
        dish = Dish.objects.filter(name__iexact=data["dish_name"]).first() or Dish(name=data["dish_name"])
        dish.kind = data["kind"]
        dish.recipe_url = data["recipe_url"]
        dish.minutes = data["minutes"]
        dish.save()
        self.instance.dish = dish
        return super().save(commit)


def small_textareas(fields, rows=2):
    return {name: forms.Textarea(attrs={"rows": rows}) for name in fields}


class FamilyMemberForm(forms.ModelForm):
    class Meta:
        model = FamilyMember
        fields = ["name", "kind", "birth_year", "likes", "dislikes", "avoid", "allergies", "notes"]
        widgets = small_textareas(["likes", "dislikes", "avoid", "allergies", "notes"])
        labels = {"kind": "Adult or child"}


class RuleForm(forms.ModelForm):
    class Meta:
        model = Rule
        fields = ["text", "active"]
        labels = {"text": "Rule", "active": "Active"}
        widgets = {"text": forms.TextInput(attrs={"placeholder": "E.g. Fish twice a week"})}


class HouseholdForm(forms.ModelForm):
    class Meta:
        model = Household
        exclude = ["usual_week"]  # edited on its own page
        widgets = small_textareas(["cuisines", "equipment", "shops", "recipe_sites"])
        widgets["pantry"] = forms.Textarea(attrs={"rows": 4})

    def clean_adventurousness(self):
        value = self.cleaned_data["adventurousness"]
        if value is not None and not 1 <= value <= 10:
            raise forms.ValidationError("Use a number from 1 to 10.")
        return value


class FeedbackForm(forms.ModelForm):
    RATINGS = [("", "Not rated"), *Feedback.Rating.choices]

    kids = forms.ChoiceField(label="Kids", choices=RATINGS, required=False, widget=forms.RadioSelect)
    parents = forms.ChoiceField(label="Parents", choices=RATINGS, required=False, widget=forms.RadioSelect)

    class Meta:
        model = Feedback
        fields = ["kids", "parents", "reaction", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


UNITS = ["g", "kg", "ml", "l", "pcs", "tbsp", "tsp", "can", "pack", "bunch", "head", "clove", "bag", "jar"]


class IngredientForm(forms.ModelForm):
    class Meta:
        model = Ingredient
        fields = ["name", "quantity", "unit", "category", "note"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Ingredient", "list": "ingredient-names"}),
            "quantity": forms.NumberInput(
                attrs={"placeholder": "Qty", "step": "any", "min": "0", "inputmode": "decimal"}
            ),
            "unit": forms.TextInput(attrs={"placeholder": "Unit", "list": "units"}),
            "note": forms.TextInput(attrs={"placeholder": "Note (optional)"}),
        }


IngredientFormSet = forms.inlineformset_factory(Dish, Ingredient, form=IngredientForm, extra=3, can_delete=True)


class ExtraItemForm(forms.ModelForm):
    class Meta:
        model = ExtraItem
        fields = ["name", "quantity", "category", "note"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "E.g. milk"}),
            "quantity": forms.TextInput(attrs={"placeholder": "E.g. 2 l"}),
            "note": forms.TextInput(attrs={"placeholder": "Note (optional)"}),
        }


class DishServingsForm(forms.ModelForm):
    class Meta:
        model = Dish
        fields = ["servings"]
        labels = {"servings": "Quantities are for (portions)"}
        help_texts = {"servings": "The shopping list scales them to the portions planned for each meal."}


class RecipeForm(forms.ModelForm):
    recipe_url = forms.URLField(label="Recipe link", max_length=500, required=False, assume_scheme="https")

    class Meta:
        model = Dish
        fields = ["name", "recipe_url", "kind", "minutes", "servings", "notes", "status"]
        labels = {"servings": "Portions", "status": "In the binder as"}
        help_texts = {"servings": "How many portions the ingredients are for."}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3, "placeholder": "E.g. where we found it, what to change"})}

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        clash = Dish.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).first()
        if clash:
            raise forms.ValidationError(f"There's already a recipe called “{clash.name}”.")
        return name


class AddToMenuForm(forms.Form):
    date = forms.DateField(widget=DateInput())
    slot = forms.ChoiceField(label="Meal", choices=PlannedMeal.Slot.choices, initial=PlannedMeal.Slot.DINNER)
