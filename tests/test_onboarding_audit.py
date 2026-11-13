import uuid

import pytest

from audit.models import AuditEvent, AuditImmutableError
from audit.services import record_event
from clients.models import ClientStatus
from integrations.models import TenantIntegration
from onboarding.models import OnboardingRun, OnboardingStatus

pytestmark = pytest.mark.django_db(transaction=True)


def start(api_client, headers, client_id, key=None):
    h = dict(headers)
    if key:
        h["Idempotency-Key"] = key
    return api_client.post("/api/onboarding/start", json={"client_id": str(client_id)}, headers=h)


def test_onboarding_happy_path(api_client, staff, linked_client):
    c = linked_client(status=ClientStatus.PROSPECT)
    r = start(api_client, staff[1], c.id)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued" and body["replayed"] is False
    assert [s["name"] for s in body["steps"]][0] == "verify_tenant_consent"
    c.refresh_from_db()
    assert c.status == ClientStatus.ONBOARDING
    assert AuditEvent.objects.filter(action="onboarding.start", entity_id=str(c.id)).count() == 1


def test_onboarding_idempotency_and_double_start(api_client, staff, linked_client):
    c = linked_client(status=ClientStatus.PROSPECT)
    first = start(api_client, staff[1], c.id, key="abcdefgh-1234").json()
    again = start(api_client, staff[1], c.id, key="abcdefgh-1234")
    assert again.status_code == 202 and again.json()["id"] == first["id"] and again.json()["replayed"] is True
    other = start(api_client, staff[1], c.id, key="different-key-1")
    assert other.status_code == 409 and other.json()["extra"]["code"] == "onboarding_in_progress"
    assert OnboardingRun.objects.filter(client=c).count() == 1


@pytest.mark.parametrize("key", ["short", "has spaces in it", "x" * 65, "bad/chars/here"])
def test_invalid_idempotency_key(api_client, staff, linked_client, key):
    c = linked_client(status=ClientStatus.PROSPECT)
    assert start(api_client, staff[1], c.id, key=key).status_code == 422


def test_onboarding_preconditions(api_client, staff, client_factory, linked_client):
    h = staff[1]
    assert start(api_client, h, uuid.uuid4()).status_code == 404
    assert api_client.post("/api/onboarding/start", json={}, headers=h).status_code == 422
    assert api_client.post("/api/onboarding/start", json={"client_id": "nope"}, headers=h).status_code == 422
    no_tenant = client_factory("NoTenant", "nt.com", ClientStatus.PROSPECT)
    assert start(api_client, h, no_tenant.id).json()["extra"]["code"] == "integration_not_configured"
    unverified = client_factory("Unv", "unv.com", ClientStatus.PROSPECT)
    TenantIntegration.objects.create(client=unverified, tenant_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    assert start(api_client, h, unverified.id).json()["extra"]["code"] == "tenant_consent_required"
    active = linked_client(status=ClientStatus.ACTIVE, domain="act.com", tenant="ffffffff-ffff-ffff-ffff-ffffffffffff")
    assert start(api_client, h, active.id).json()["extra"]["code"] == "invalid_client_status"


def test_completed_run_does_not_block_new_attempt(api_client, staff, linked_client):
    c = linked_client(status=ClientStatus.PROSPECT)
    OnboardingRun.objects.create(client=c, status=OnboardingStatus.FAILED, steps=[])
    assert start(api_client, staff[1], c.id).status_code == 202


# ------------------------------------ audit ----------------------------------
def test_audit_requires_admin_and_filters(api_client, staff, admin, client_factory):
    a, ah = admin
    c = client_factory(status=ClientStatus.ACTIVE)
    api_client.patch(f"/api/clients/{c.id}/status", json={"status": "suspended", "reason": "audit test"}, headers=ah)
    assert api_client.get("/api/audit", headers=staff[1]).status_code == 403

    body = api_client.get("/api/audit?action=client.status_change&outcome=success", headers=ah).json()
    assert body["total"] == 1 and body["items"][0]["actor_label"] == "admin@contoso.com"
    assert api_client.get(f"/api/audit?actor_id={a.id}", headers=ah).json()["total"] == 1
    assert api_client.get(f"/api/audit?entity_id={c.id}&entity_type=client", headers=ah).json()["total"] == 1
    assert api_client.get("/api/audit?occurred_after=2999-01-01T00:00:00Z", headers=ah).json()["total"] == 0


@pytest.mark.parametrize(
    "query",
    [
        "outcome=bogus",
        "action=DROP TABLE",
        "action=" + "a" * 65,
        "actor_id=nope",
        "entity_id=../etc",
        "occurred_after=yesterday",
        "occurred_after=2026-01-01T00:00:00",  # naive datetime
        "occurred_after=2026-02-01T00:00:00Z&occurred_before=2026-01-01T00:00:00Z",
        "page_size=1000",
    ],
)
def test_audit_query_validation(api_client, admin, query):
    assert api_client.get(f"/api/audit?{query}", headers=admin[1]).status_code == 422


def test_audit_is_append_only(db):
    ev = record_event(action="x.test", entity_type="thing", metadata={"password": "hunter2", "ok": "yes"})
    assert ev.metadata == {"password": "[redacted]", "ok": "yes"}  # secrets never persisted
    ev.actor_label = "tampered"
    with pytest.raises(AuditImmutableError):
        ev.save()
    with pytest.raises(AuditImmutableError):
        ev.delete()
    with pytest.raises(AuditImmutableError):
        AuditEvent.objects.all().update(actor_label="x")
    with pytest.raises(AuditImmutableError):
        AuditEvent.objects.all().delete()


def test_audit_survives_actor_removal(admin, api_client, client_factory):
    a, h = admin
    c = client_factory(status=ClientStatus.ACTIVE)
    api_client.patch(f"/api/clients/{c.id}/status", json={"status": "suspended", "reason": "audit test"}, headers=h)
    a.delete()
    ev = AuditEvent.objects.get(action="client.status_change")
    assert ev.actor_label == "admin@contoso.com"
