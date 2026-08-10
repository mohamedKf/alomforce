"""Django admin — used by managers for staff administration and data fixes."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from core.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ['last_name', 'first_name']
    list_display = ['id_number', 'full_name', 'role', 'phone', 'is_active']
    list_filter = ['role', 'is_active', 'language']
    search_fields = ['id_number', 'first_name', 'last_name', 'phone', 'email']

    fieldsets = [
        (None, {'fields': ['id_number', 'password']}),
        (_('Personal'), {
            'fields': ['first_name', 'last_name', 'email', 'phone',
                       'date_of_birth', 'address', 'language'],
        }),
        (_('Employment'), {
            'fields': ['role', 'hired_on', 'emergency_contact', 'emergency_phone'],
        }),
        (_('Client access'), {'fields': ['client']}),
        (_('Permissions'), {
            'fields': ['is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'],
        }),
    ]

    add_fieldsets = [
        (None, {
            'classes': ['wide'],
            'fields': ['id_number', 'first_name', 'last_name', 'phone',
                       'role', 'password1', 'password2'],
        }),
    ]
