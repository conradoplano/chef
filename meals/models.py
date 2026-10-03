from urllib.parse import urlsplit

from django.conf import settings
from django.db import models


class Dish(models.Model):
    """A meal we cook, reused across weeks. Feedback and ingredients attach here."""

    class Kind(models.TextChoices):
        FISH = "fish", "Fish"
        MEAT = "meat", "Meat"
        VEGETARIAN = "vegetarian", "Vegetarian"
        OTHER = "other", "Other"

    ICONS = {"fish": "🐟", "meat": "🥩", "vegetarian": "🌱", "other": "🍽"}

    # Friendly names for recipe sites; anything else shows its domain.
    SOURCES = {
        "bbcgoodfood.com": "BBC Good Food",
        "bbc.co.uk": "BBC Food",
        "jamieoliver.com": "Jamie Oliver",
        "ottolenghi.co.uk": "Ottolenghi",
        "allrecipes.com": "Allrecipes",
        "seriouseats.com": "Serious Eats",
        "cooking.nytimes.com": "NYT Cooking",
        "bonappetit.com": "Bon Appétit",
        "recipetineats.com": "RecipeTin Eats",
        "chefkoch.de": "Chefkoch",
        "lecker.de": "Lecker",
        "eatsmarter.de": "EatSmarter",
        "hellofresh.com": "HelloFresh",
        "youtube.com": "YouTube",
    }

    name = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OTHER, verbose_name="type")
    recipe_url = models.URLField("recipe link", max_length=500, blank=True)
    minutes = models.PositiveSmallIntegerField("cooking time (min)", null=True, blank=True)
    servings = models.PositiveSmallIntegerField(
        "portions", default=4, help_text="How many portions the ingredient quantities are for."
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "dishes"

    def __str__(self):
        return self.name

    @property
    def icon(self):
        return self.ICONS.get(self.kind, "🍽")

    @property
    def recipe_source(self):
        host = (urlsplit(self.recipe_url).hostname or "").lower().removeprefix("www.")
        if not host:
            return ""
        for domain, name in self.SOURCES.items():
            if host == domain or host.endswith("." + domain):
                return name
        return host


class PlannedMeal(models.Model):
    """A dish on the menu for a given day and meal."""

    class Slot(models.TextChoices):
        LUNCH = "lunch", "Lunch"
        DINNER = "dinner", "Dinner"

    date = models.DateField()
    slot = models.CharField(max_length=10, choices=Slot.choices, default=Slot.DINNER, verbose_name="meal")
    dish = models.ForeignKey(Dish, on_delete=models.PROTECT, related_name="planned")
    leftovers = models.BooleanField(default=False, help_text="Eaten from leftovers; nothing to buy.")
    note = models.CharField(max_length=300, blank=True)
    eaters = models.ManyToManyField("FamilyMember", blank=True, related_name="meals", verbose_name="who eats")
    servings = models.PositiveSmallIntegerField(
        "portions to cook", null=True, blank=True,
        help_text="Including any to keep as leftovers. Empty: as many as the recipe makes.",
    )
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Reverse alphabetical puts lunch before dinner.
        ordering = ["date", "-slot", "pk"]

    def __str__(self):
        return f"{self.date} {self.get_slot_display()}: {self.dish}"


class Feedback(models.Model):
    """How a cooked meal went. Used to plan future menus."""

    class Rating(models.TextChoices):
        LOVED = "loved", "❤️ Loved it"
        LIKED = "liked", "👍 Liked it"
        OKAY = "okay", "😐 Okay"
        DISLIKED = "disliked", "👎 Didn't like it"

    EMOJI = {"loved": "❤️", "liked": "👍", "okay": "😐", "disliked": "👎"}

    meal = models.OneToOneField(PlannedMeal, on_delete=models.CASCADE, related_name="feedback")
    kids = models.CharField(max_length=10, choices=Rating.choices, blank=True)
    parents = models.CharField(max_length=10, choices=Rating.choices, blank=True)
    reaction = models.CharField(
        "bad reaction", max_length=300, blank=True, help_text="Who reacted badly to it, and how."
    )
    notes = models.TextField(blank=True, help_text="E.g. too spicy for the kids, would make it again with less garlic.")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Feedback on {self.meal}"

    @property
    def shared_rating(self):
        """The rating when kids and parents agree, else ""."""
        return self.kids if self.kids == self.parents else ""

    @property
    def kids_emoji(self):
        return self.EMOJI.get(self.kids, "")

    @property
    def parents_emoji(self):
        return self.EMOJI.get(self.parents, "")


class FamilyMember(models.Model):
    class Kind(models.TextChoices):
        ADULT = "adult", "Adult"
        CHILD = "child", "Child"

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.ADULT)
    birth_year = models.PositiveSmallIntegerField(null=True, blank=True)
    likes = models.TextField("favourite foods", blank=True)
    dislikes = models.TextField(blank=True)
    avoid = models.TextField("won't eat", blank=True)
    allergies = models.TextField("allergies & intolerances", blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["kind", "birth_year", "name"]

    def __str__(self):
        return self.name

    def age(self, today):
        return today.year - self.birth_year if self.birth_year else None


class Rule(models.Model):
    """A planning rule, e.g. "Fish twice a week"."""

    text = models.CharField(max_length=300)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-active", "created_at"]

    def __str__(self):
        return self.text


class Household(models.Model):
    """How we cook and shop. A single row, edited on the family page."""

    class Priority(models.TextChoices):
        BALANCED = "balanced", "Balanced"
        PRICE = "price", "Price"
        CONVENIENCE = "convenience", "Convenience"
        QUALITY = "quality", "Quality"
        WASTE = "waste", "Less waste"

    meals_to_plan = models.TextField(
        blank=True, help_text="E.g. dinners Mon–Fri, lunch and dinner at the weekend."
    )
    weekday_minutes = models.PositiveSmallIntegerField("max. cooking time on weekdays (min)", null=True, blank=True)
    weekend_minutes = models.PositiveSmallIntegerField("max. cooking time at the weekend (min)", null=True, blank=True)
    adventurousness = models.PositiveSmallIntegerField(
        "how adventurous (1–10)", null=True, blank=True, help_text="1 = only familiar meals, 10 = try anything."
    )
    leftovers = models.TextField("leftovers", blank=True, help_text="E.g. welcome for weekend lunches.")
    cuisines = models.TextField("favourite cuisines", blank=True)
    equipment = models.TextField("cooking equipment", blank=True)
    shops = models.TextField("where we shop", blank=True)
    priority = models.CharField("optimise for", max_length=12, choices=Priority.choices, default=Priority.BALANCED)
    pantry = models.TextField("pantry staples", blank=True, help_text="Things we always have; left off the shopping list.")
    notes = models.TextField("anything else", blank=True)
    # Which meals we usually need and who eats them, by weekday ("0" = Monday):
    # {"0": {"lunch": {"on": false, "eaters": [ids]}, "dinner": {...}}, ...}. See meals.schedule.
    usual_week = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name_plural = "household"

    def __str__(self):
        return "Household"

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]


class Category(models.TextChoices):
    """Shopping list sections, in the order they are shown."""

    VEGETABLES = "vegetables", "🥕 Fruit & vegetables"
    FISH = "fish", "🐟 Fish"
    MEAT = "meat", "🥩 Meat"
    VEGETARIAN = "vegetarian", "🌱 Vegetarian protein"
    DAIRY = "dairy", "🧀 Dairy & eggs"
    BAKERY = "bakery", "🍞 Bread & bakery"
    BREAKFAST = "breakfast", "🥣 Breakfast & snacks"
    PANTRY = "pantry", "🫙 Pantry"
    FROZEN = "frozen", "❄️ Frozen"
    SPECIALITY = "speciality", "🥢 Asian / speciality shop"
    OTHER = "other", "🛒 Other"


class Ingredient(models.Model):
    """What a dish needs, for the whole family."""

    dish = models.ForeignKey(Dish, on_delete=models.CASCADE, related_name="ingredients")
    name = models.CharField(max_length=100)
    quantity = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True, help_text="E.g. g, kg, ml, pcs, can, bunch.")
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.OTHER)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.name


class ExtraItem(models.Model):
    """Something on a week's shopping list that isn't for a planned meal."""

    week = models.DateField(help_text="Monday of the week.")
    name = models.CharField(max_length=100)
    quantity = models.CharField(max_length=50, blank=True)
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.OTHER)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    # Set when removed from the list; kept for a while so the removal can be undone.
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.name


class ShoppingCheck(models.Model):
    """An item marked as bought on a week's list. Items are identified by key, so checks
    survive menu changes for ingredients that are still on the list."""

    week = models.DateField()
    key = models.CharField(max_length=120)
    checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    checked_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["week", "key"], name="unique_check_per_week")]

    def __str__(self):
        return f"{self.week}: {self.key}"


class MenuRequest(models.Model):
    """A request to create a week's menu with AI, run in the background."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        RUNNING = "running", "Creating"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    week = models.DateField(help_text="Monday of the week.")
    # [{"date": "2026-10-05", "slot": "dinner", "eaters": [member ids]}, ...]
    slots = models.JSONField(default=list)
    keep_existing = models.BooleanField(default=True, help_text="Keep meals that are already planned.")
    details = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    summary = models.TextField(blank=True)
    error = models.TextField(blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Menu for week of {self.week} ({self.get_status_display()})"

    @property
    def finished(self):
        return self.status in (self.Status.DONE, self.Status.FAILED)
