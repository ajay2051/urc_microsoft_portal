"""Load demo staff and clients for local development. Refuses to run unless DEBUG is on."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import StaffRole, StaffUser
from clients.models import Client, ClientStatus
from integrations.models import ConsentStatus, TenantIntegration

STAFF = [
    ("admin@contoso.com", "Alex Admin", StaffRole.ADMIN),
    ("james.wright@contoso.com", "James Wright", StaffRole.STAFF),
    ("priya.sharma@contoso.com", "Priya Sharma", StaffRole.STAFF),
]
CLIENTS = [
    ("Fabrikam Inc", "fabrikam.com", ClientStatus.ACTIVE, "11111111-aaaa-4aaa-8aaa-000000000001"),
    ("Northwind Traders", "northwind.com", ClientStatus.PROSPECT, "11111111-aaaa-4aaa-8aaa-000000000002"),
    ("Adventure Works", "adventure-works.com", ClientStatus.ONBOARDING, None),
]


class Command(BaseCommand):
    help = "Seed demo data (development only; idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DJANGO_DEBUG=true.")
        for email, name, role in STAFF:
            if not StaffUser.objects.filter(email__iexact=email).exists():
                StaffUser.objects.create_staff(email, name, role)
        for name, domain, status, tenant in CLIENTS:
            client, _ = Client.objects.get_or_create(
                primary_domain=domain, defaults={"name": name, "status": status, "contact_email": f"it@{domain}"}
            )
            if tenant and not TenantIntegration.objects.filter(pk=client.pk).exists():
                TenantIntegration.objects.create(client=client, tenant_id=tenant, consent_status=ConsentStatus.GRANTED)
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
