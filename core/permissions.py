"""Permissions applied globally.

Lives outside views.py by necessity: DRF resolves DEFAULT_PERMISSION_CLASSES
while views.py is still being imported, so a permission referenced there cannot
also be defined there — the module would import itself half-built. Everything
role-specific stays in views.py with the views that use it.
"""

from rest_framework import permissions


class PasswordChangeRequired(permissions.BasePermission):
    """Blocks a user who still has a manager-set starting password.

    Applied to every endpoint. Without it the flag would be advice rather than
    a rule, and a password the manager knows would keep working indefinitely.
    Views that must stay reachable so the user can fix it opt out with
    `allows_pending_password = True`.
    """

    message = 'You must change your password before continuing.'

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return True
        if not getattr(user, 'must_change_password', False):
            return True
        return getattr(view, 'allows_pending_password', False)
