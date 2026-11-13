"""Client accounts and their mirrored Microsoft 365 users."""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models.functions import Lower

from core.validators import validate_domain, validate_guid


class ClientStatus(models.TextChoices):
    PROSPECT = "prospect", "Prospect"
    ONBOARDING = "onboarding", "Onboarding"
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    OFFBOARDED = "offboarded", "Offboarded"


# Allowed lifecycle transitions. Anything not listed is rejected (409).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    ClientStatus.PROSPECT: frozenset({ClientStatus.ONBOARDING, ClientStatus.OFFBOARDED}),
    ClientStatus.ONBOARDING: frozenset({ClientStatus.ACTIVE, ClientStatus.PROSPECT, ClientStatus.OFFBOARDED}),
    ClientStatus.ACTIVE: frozenset({ClientStatus.SUSPENDED, ClientStatus.OFFBOARDED}),
    ClientStatus.SUSPENDED: frozenset({ClientStatus.ACTIVE, ClientStatus.OFFBOARDED}),
    ClientStatus.OFFBOARDED: frozenset(),  # terminal
}
# Transitions only an admin may perform.
ADMIN_ONLY_TARGETS = frozenset({ClientStatus.OFFBOARDED, ClientStatus.SUSPENDED})


class Client(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    primary_domain = models.CharField(max_length=253, validators=[validate_domain])
    contact_email = models.EmailField(max_length=254, blank=True, default="")
    status = models.CharField(max_length=16, choices=ClientStatus.choices, default=ClientStatus.PROSPECT, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(Lower("primary_domain"), name="client_domain_ci_unique"),
            models.CheckConstraint(
                condition=models.Q(status__in=[s.value for s in ClientStatus]), name="client_status_valid"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class M365User(models.Model):
    """A user mirrored from a client's Microsoft 365 tenant (source of truth: Entra ID)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="m365_users")
    graph_id = models.CharField(max_length=36, validators=[validate_guid])
    user_principal_name = models.CharField(max_length=320)
    display_name = models.CharField(max_length=256, blank=True, default="")
    mail = models.CharField(max_length=320, blank=True, default="")
    job_title = models.CharField(max_length=256, blank=True, default="")
    department = models.CharField(max_length=256, blank=True, default="")
    account_enabled = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField()
    # Soft removal: users that disappear from the tenant are flagged, never hard-deleted.
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["display_name", "user_principal_name"]
        constraints = [
            models.UniqueConstraint(fields=["client", "graph_id"], name="m365user_client_graphid_unique"),
        ]
        indexes = [models.Index(fields=["client", "removed_at"])]

    def __str__(self) -> str:
        return self.user_principal_name
