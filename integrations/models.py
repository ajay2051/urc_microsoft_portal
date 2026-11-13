"""Microsoft tenant integration settings and sync bookkeeping."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from clients.models import Client
from core.validators import validate_guid


class ConsentStatus(models.TextChoices):
    NOT_REQUESTED = "not_requested", "Not requested"
    PENDING = "pending", "Pending"
    GRANTED = "granted", "Granted"
    REVOKED = "revoked", "Revoked"


class TenantIntegration(models.Model):
    """One Microsoft 365 tenant per client. Holds *no secrets*: app credentials are global config."""

    client = models.OneToOneField(Client, on_delete=models.CASCADE, primary_key=True, related_name="integration")
    tenant_id = models.CharField(max_length=36, validators=[validate_guid], unique=True)
    consent_status = models.CharField(
        max_length=16, choices=ConsentStatus.choices, default=ConsentStatus.NOT_REQUESTED
    )
    consent_granted_at = models.DateTimeField(null=True, blank=True)
    sync_enabled = models.BooleanField(default=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(consent_status__in=[c.value for c in ConsentStatus]),
                name="tenantintegration_consent_valid",
            ),
        ]


class SyncStatus(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class SyncRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="sync_runs")
    status = models.CharField(max_length=16, choices=SyncStatus.choices, default=SyncStatus.RUNNING)
    started_by_id = models.UUIDField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    removed_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            # Database-enforced mutual exclusion: at most one running sync per client.
            models.UniqueConstraint(
                fields=["client"],
                condition=models.Q(status="running"),
                name="syncrun_one_running_per_client",
            ),
        ]
