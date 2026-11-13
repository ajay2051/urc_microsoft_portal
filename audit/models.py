"""Append-only audit trail.

Rows are immutable: ``save()`` on an existing row, ``delete()`` and bulk
``update()/delete()`` all raise. Actor details are *snapshotted* (id + label) so
history survives staff deactivation or removal.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class AuditImmutableError(RuntimeError):
    """Raised on any attempt to modify or delete an audit record."""


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):  # noqa: D401
        raise AuditImmutableError("Audit events are append-only.")

    def delete(self):
        raise AuditImmutableError("Audit events are append-only.")

    def bulk_update(self, *args, **kwargs):
        raise AuditImmutableError("Audit events are append-only.")


class AuditOutcome(models.TextChoices):
    SUCCESS = "success", "Success"
    FAILURE = "failure", "Failure"
    DENIED = "denied", "Denied"


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor_id = models.UUIDField(null=True, blank=True, db_index=True)
    actor_label = models.CharField(max_length=254, help_text="Email or system principal at the time of the event.")
    action = models.CharField(max_length=64, db_index=True)
    entity_type = models.CharField(max_length=64)
    entity_id = models.CharField(max_length=64, blank=True, default="")
    outcome = models.CharField(max_length=16, choices=AuditOutcome.choices, default=AuditOutcome.SUCCESS)
    metadata = models.JSONField(default=dict, blank=True)
    request_id = models.CharField(max_length=64, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    objects = AuditQuerySet.as_manager()

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [models.Index(fields=["entity_type", "entity_id"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditImmutableError("Audit events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditImmutableError("Audit events are append-only.")
