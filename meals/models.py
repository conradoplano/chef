from urllib.parse import urlsplit

from django.conf import settings
from django.db import models
from django.utils import timezone


class Dish(models.Model):
    """A recipe: cooked on menus, kept in the recipe binder. Feedback and ingredients attach here."""

    class Status(models.TextChoices):
        NONE = "", "Not in the binder"
        TRY = "try", "Want to try"
        FAVOURITE = "favourite", "Favourite"

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

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OTHER, verbose_name="type")
    recipe_url = models.URLField("recipe link", max_length=500, blank=True)
    minutes = models.PositiveSmallIntegerField("cooking time (min)", null=True, blank=True)
    servings = models.PositiveSmallIntegerField(
        "portions", default=4, help_text="How many portions the ingredient quantities are for."
    )
    notes = models.TextField(blank=True)
    # For recipes without a web page (magazines, books, family recipes) the app keeps the recipe itself.
    source = models.CharField(
        max_length=200, blank=True, help_text="Where it's from, e.g. BBC Good Food magazine, Oct 2026, p. 42."
    )
    instructions = models.TextField("method", blank=True, help_text="The steps, one per line.")
    # The recipe binder: recipes we like, and ones we found and want to try.
    status = models.CharField("binder", max_length=10, choices=Status.choices, default=Status.NONE, blank=True)
    saved_at = models.DateTimeField(null=True, blank=True, help_text="When it was put in the binder.")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "dishes"
        constraints = [models.UniqueConstraint(fields=["household", "name"], name="unique_dish_per_household")]

    def __str__(self):
        return self.name

    @property
    def icon(self):
        return self.ICONS.get(self.kind, "🍽")

    @property
    def has_own_recipe(self):
        """True when the app holds the recipe itself (method or photos), e.g. from a magazine."""
        return bool(self.instructions.strip()) or bool(self.photos.all())

    @property
    def steps(self):
        return [line.strip() for line in self.instructions.splitlines() if line.strip()]

    def set_status(self, status):
        """Moves the recipe in the binder; remembers when it was first saved."""
        if status and not self.status:
            self.saved_at = timezone.now()
        self.status = status
        self.save(update_fields=["status", "saved_at"])

    def learn_from(self, review):
        """A recipe we wanted to try that everyone liked becomes a favourite. Returns True if it did."""
        if self.status == self.Status.TRY and review.shared_rating in ("loved", "liked"):
            self.set_status(self.Status.FAVOURITE)
            return True
        return False

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

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
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

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
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

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
    text = models.CharField(max_length=300)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-active", "created_at"]

    def __str__(self):
        return self.text


class Household(models.Model):
    """A family using the app, and how they cook and shop. Everything else (dishes, menus, lists)
    belongs to one household; users belong to one household. Free-text preferences are rules;
    which meals and who eats is the usual week."""

    class Priority(models.TextChoices):
        BALANCED = "balanced", "Balanced"
        PRICE = "price", "Price"
        CONVENIENCE = "convenience", "Convenience"
        QUALITY = "quality", "Quality"
        WASTE = "waste", "Less waste"

    name = models.CharField(max_length=100, blank=True, help_text="E.g. The Smiths.")
    created_at = models.DateTimeField(default=timezone.now)
    # AI costs money: new households can use everything else straight away, AI once an admin approves.
    ai_approved = models.BooleanField("AI approved", default=False)
    ai_daily_limit = models.DecimalField(
        "AI limit per day (USD)", max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Empty: the app's default. 0: no AI.",
    )
    is_active = models.BooleanField("active", default=True, help_text="Suspended households can't log in.")

    weekday_minutes = models.PositiveSmallIntegerField("max. cooking time on weekdays (min)", null=True, blank=True)
    weekend_minutes = models.PositiveSmallIntegerField("max. cooking time at the weekend (min)", null=True, blank=True)
    adventurousness = models.PositiveSmallIntegerField(
        "how adventurous (1–10)", null=True, blank=True, help_text="1 = only familiar meals, 10 = try anything."
    )
    cuisines = models.TextField("favourite cuisines", blank=True)
    equipment = models.TextField("cooking equipment", blank=True)
    shops = models.TextField("where we shop", blank=True)
    priority = models.CharField("optimise for", max_length=12, choices=Priority.choices, default=Priority.BALANCED)
    pantry = models.TextField("pantry staples", blank=True, help_text="Things we always have; left off the shopping list.")
    freezer = models.TextField("freezer staples", blank=True, help_text="Things always in the freezer.")

    class OtherSites(models.TextChoices):
        NEVER = "never", "Never – only the sources above"
        RARELY = "rarely", "Rarely – about one recipe a week"
        SOMETIMES = "sometimes", "Sometimes – about a third of the recipes"
        OFTEN = "often", "Often – whenever another site has a better recipe"

    recipe_sites = models.TextField(
        "recipe sources", blank=True,
        help_text="One per line: websites (bbcgoodfood.com) or names (Jamie Oliver). Searched first when creating a menu.",
    )
    other_sites = models.CharField(
        "recipes from other sources", max_length=10, choices=OtherSites.choices, default=OtherSites.SOMETIMES
    )
    # Which meals we usually need and who eats them, by weekday ("0" = Monday):
    # {"0": {"lunch": {"on": false, "eaters": [ids]}, "dinner": {...}}, ...}. See meals.schedule.
    usual_week = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name or f"Household {self.pk}"


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

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
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

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
    week = models.DateField()
    key = models.CharField(max_length=120)
    checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    checked_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["household", "week", "key"], name="unique_check_per_week")]

    def __str__(self):
        return f"{self.week}: {self.key}"


class MenuRequest(models.Model):
    """A request to create a week's menu with AI, run in the background."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        RUNNING = "running", "Creating"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    class Kind(models.TextChoices):
        CREATE = "create", "Create menu"
        CHANGE = "change", "Change menu"
        REPLACE = "replace", "Replace a dish"

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
    week = models.DateField(help_text="Monday of the week.")
    # "create" and "change" requests write the week's "About this menu"; "replace" doesn't.
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.CREATE)
    replacing = models.CharField(max_length=200, blank=True, help_text="Dish being replaced, for 'replace' requests.")
    # [{"date": "2026-10-05", "slot": "dinner", "eaters": [member ids]}, ...]
    slots = models.JSONField(default=list)
    keep_existing = models.BooleanField(default=True, help_text="Keep meals that are already planned.")
    details = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    summary = models.TextField(blank=True)
    error = models.TextField(blank=True)
    # What was sent and received, to understand and tune the results.
    model = models.CharField(max_length=50, blank=True)
    prompt = models.TextField(blank=True)
    response = models.TextField(blank=True, help_text="The AI's answer (save_menu arguments) as JSON.")
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    web_searches = models.PositiveIntegerField(default=0)
    cost = models.DecimalField("cost (USD)", max_digits=8, decimal_places=4, null=True, blank=True)
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


class RecipeImport(models.Model):
    """A recipe from photos (e.g. a magazine page) or a web page, read by AI, then checked by the family."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        RUNNING = "running", "Reading"
        DONE = "done", "Ready to check"
        FAILED = "failed", "Failed"
        SAVED = "saved", "Saved"

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # Started from a day on the menu: once saved, the recipe is planned there.
    plan_date = models.DateField(null=True, blank=True)
    plan_slot = models.CharField(max_length=10, blank=True)
    plan_next = models.CharField(max_length=300, blank=True)
    # A recipe is read either from photos (RecipePhoto rows) or from a web page at this address.
    url = models.URLField("recipe link", max_length=500, blank=True)
    result = models.JSONField(default=dict, blank=True, help_text="What the AI read, before checking.")
    error = models.TextField(blank=True)
    dish = models.ForeignKey(Dish, null=True, blank=True, on_delete=models.SET_NULL, related_name="imports")
    model = models.CharField(max_length=50, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    web_searches = models.PositiveIntegerField(default=0)
    cost = models.DecimalField("cost (USD)", max_digits=8, decimal_places=4, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Recipe photos {self.created_at:%Y-%m-%d %H:%M} ({self.get_status_display()})"

    @property
    def finished(self):
        return self.status not in (self.Status.PENDING, self.Status.RUNNING)


class RecipePhoto(models.Model):
    """A photo of a recipe (magazine page, book, handwritten card) or of the finished dish."""

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
    image = models.ImageField(upload_to="recipes/%Y/%m")
    dish = models.ForeignKey(Dish, null=True, blank=True, on_delete=models.CASCADE, related_name="photos")
    recipe_import = models.ForeignKey(RecipeImport, null=True, blank=True, on_delete=models.SET_NULL, related_name="photos")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return self.image.name

    def delete(self, *args, **kwargs):
        storage, name = self.image.storage, self.image.name
        super().delete(*args, **kwargs)
        if name:
            storage.delete(name)


class WeeklyItem(models.Model):
    """Something on every week's shopping list, e.g. fruit, bread or snacks for the kids."""

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=100)
    quantity = models.CharField(max_length=50, blank=True)
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.OTHER)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AIUsage(models.Model):
    """What one call to the AI cost, for the daily limits and the admin page.
    Kept when the menu request or recipe import it was for is deleted."""

    class Kind(models.TextChoices):
        MENU = "menu", "Menu"
        RECIPE = "recipe", "Recipe"

    household = models.ForeignKey(Household, null=True, on_delete=models.SET_NULL, related_name="ai_usage")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    model = models.CharField(max_length=50, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    web_searches = models.PositiveIntegerField(default=0)
    cost = models.DecimalField("cost (USD)", max_digits=8, decimal_places=4, default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI usage"
        verbose_name_plural = "AI usage"

    def __str__(self):
        return f"{self.get_kind_display()} for {self.household}: ${self.cost}"
