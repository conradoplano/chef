from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """Users are identified by email and never have a usable password."""

    def create_user(self, email, **extra_fields):
        """Without a household, the user starts a new one of their own."""
        if not email:
            raise ValueError("An email address is required.")
        if not extra_fields.get("household"):
            from meals.models import Household

            extra_fields["household"] = Household.objects.create()
        user = self.model(email=self.normalize_email(email).lower(), **extra_fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    # The family the user plans meals with. Everyone in a household sees and changes the same data.
    household = models.ForeignKey(
        "meals.Household", null=True, blank=True, on_delete=models.SET_NULL, related_name="members"
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.name or self.email

    def get_full_name(self):
        return self.name or self.email

    def get_short_name(self):
        return self.name.split(" ")[0] if self.name else self.email.split("@")[0]


class LoginCodeRequest(models.Model):
    """One login code requested for an email address (known or not), to limit how many are sent."""

    email = models.EmailField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} at {self.created_at:%Y-%m-%d %H:%M}"
