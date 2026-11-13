"""Client onboarding workflow runs."""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from clients.models import Client


class OnboardingStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    IN_PROGRESS = "in_progress", "In progress"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


ACTIVE_ONBOARDING_STATUSES = (OnboardingStatus.QUEUED, OnboardingStatus.IN_PROGRESS)

# Ordered workflow definition. Execution of the steps belongs to a background worker (see docs/ARCHITECTURE.md).
ONBOARDING_STEPS = (
    "verify_tenant_consent",
    "sync_directory",
    "provision_workspace",
    "notify_client_contact",
)


class OnboardingRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="onboarding_runs")
    status = models.CharField(max_length=16, choices=OnboardingStatus.choices, default=OnboardingStatus.QUEUED)
    steps = models.JSONField(default=list)
    started_by_id = models.UUIDField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client"],
                condition=models.Q(status__in=["queued", "in_progress"]),
                name="onboardingrun_one_active_per_client",
            ),
            models.UniqueConstraint(
                fields=["client", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="onboardingrun_idempotency_unique",
            ),
        ]
