"""Base class for request bodies."""

from __future__ import annotations

from django_bolt.serializers import Serializer


class StrictRequest(Serializer, forbid_unknown_fields=True):
    """Request body that rejects unknown fields (422) instead of silently ignoring them.

    Silently dropping unexpected keys hides client bugs and is the usual root of
    mass-assignment vulnerabilities, so every request model inherits from this.
    """
