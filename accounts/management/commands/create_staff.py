"""Provision (or update) a staff authorisation record.

Usage::

    python manage.py create_staff admin@contoso.com "Alex Admin" --role admin
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.models import StaffRole, StaffUser
from audit.services import record_event


class Command(BaseCommand):
    help = "Create a staff user who may sign in with Microsoft Entra ID."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("display_name")
        parser.add_argument("--role", choices=[r.value for r in StaffRole], default=StaffRole.STAFF.value)

    def handle(self, *args, email, display_name, role, **options):
        email = email.strip().lower()
        if StaffUser.objects.filter(email__iexact=email).exists():
            raise CommandError(f"A staff user with email {email} already exists.")
        try:
            user = StaffUser.objects.create_staff(email, display_name.strip(), role)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        record_event(
            action="staff.created",
            entity_type="staff_user",
            entity_id=str(user.id),
            actor=None,
            actor_label="management-command",
            metadata={"email": user.email, "role": user.role},
        )
        self.stdout.write(self.style.SUCCESS(f"Created {user.role} {user.email} ({user.id})."))
