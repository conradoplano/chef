from django.contrib import admin

from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "is_active", "is_staff", "last_login")
    list_filter = ("is_active", "is_staff")
    search_fields = ("email", "name")
    readonly_fields = ("last_login", "date_joined")
    fieldsets = (
        (None, {"fields": ("email", "name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    filter_horizontal = ("groups",)

    def save_model(self, request, obj, form, change):
        obj.email = obj.email.lower()
        if not change:
            obj.set_unusable_password()
        super().save_model(request, obj, form, change)
