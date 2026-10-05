from django.contrib import admin

from .models import Client, Connection


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "client_id", "created_at", "created_ip")
    search_fields = ("name", "client_id")
    readonly_fields = ("client_id", "secret_hash", "created_at", "created_ip")


@admin.register(Connection)
class ConnectionAdmin(admin.ModelAdmin):
    list_display = ("client", "user", "created_at", "last_used_at")
    list_select_related = ("client", "user")
    fields = ("client", "user", "created_at", "last_used_at", "access_expires_at", "refresh_expires_at")
    readonly_fields = fields
