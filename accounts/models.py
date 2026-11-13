"""Staff identities.

A :class:`StaffUser` is an *authorisation record* for a person who signs in with
Microsoft Entra ID. It never stores a password. Authentication is proven by the
Entra access token; **authorisation (role, active flag) always comes from this
table, never from the token or the client**.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower

from core.validators import validate_guid


class StaffRole(models.TextChoices):
    ADMIN = "admin", "Admin"
    STAFF = "staff", "Staff"


class StaffUserManager(BaseUserManager["StaffUser"]):
    use_in_migrations = True

    def create_staff(self, email: str, display_name: str, role: str = StaffRole.STAFF, **extra) -> StaffUser:
        if not email:
            raise ValueError("email is required")
        user = self.model(email=self.normalize_email(email).lower(), display_name=display_name, role=role, **extra)
        user.set_unusable_password()
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user


class StaffUser(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=254, unique=True)
    display_name = models.CharField(max_length=150)
    role = models.CharField(max_length=16, choices=StaffRole.choices, default=StaffRole.STAFF)
    is_active = models.BooleanField(default=True)

    # Immutable Entra identifiers, bound on first successful sign-in.
    entra_tenant_id = models.CharField(max_length=36, blank=True, default="", validators=[validate_guid])
    entra_object_id = models.CharField(max_length=36, blank=True, default="", validators=[validate_guid])

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["display_name"]

    objects = StaffUserManager()

    class Meta:
        ordering = ["email"]
        constraints = [
            models.UniqueConstraint(Lower("email"), name="staffuser_email_ci_unique"),
            models.UniqueConstraint(
                fields=["entra_tenant_id", "entra_object_id"],
                condition=~models.Q(entra_object_id=""),
                name="staffuser_entra_identity_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=["admin", "staff"]),
                name="staffuser_role_valid",
            ),
        ]

    def save(self, *args, **kwargs):
        # Emails are stored lower-case so uniqueness is effectively case-insensitive.
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.email

    @property
    def is_admin(self) -> bool:
        return self.role == StaffRole.ADMIN
