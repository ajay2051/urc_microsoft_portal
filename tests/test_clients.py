import uuid

import pytest

from audit.models import AuditEvent
from clients.models import Client, ClientStatus

pytestmark = pytest.mark.django_db(transaction=True)


def test_list_filters_search_and_pagination(api_client, staff, client_factory):
    _, h = staff
    client_factory("Fabrikam", "fabrikam.com", ClientStatus.ACTIVE)
    client_factory("Northwind", "northwind.io", ClientStatus.PROSPECT)
    client_factory("Contoso Ltd", "contoso-ltd.com", ClientStatus.ACTIVE)

    body = api_client.get("/api/clients?page_size=2", headers=h).json()
    assert body["total"] == 3 and body["total_pages"] == 2 and len(body["items"]) == 2
    assert [c["name"] for c in api_client.get("/api/clients?status=prospect", headers=h).json()["items"]] == ["Northwind"]
    assert [c["name"] for c in api_client.get("/api/clients?q=NORTH", headers=h).json()["items"]] == ["Northwind"]
    assert api_client.get("/api/clients?q=%25", headers=h).json()["total"] == 0  # LIKE wildcards are escaped


@pytest.mark.parametrize(
    "query",
    ["page=0", "page=-1", "page=abc", "page_size=0", "page_size=101", "status=bogus", "status=ACTIVE", "q=" + "x" * 101, "q="],
)
def test_list_rejects_invalid_query(api_client, staff, query):
    _, h = staff
    assert api_client.get(f"/api/clients?{query}", headers=h).status_code == 422


def test_detail_404_and_422(api_client, staff, client_factory):
    _, h = staff
    assert api_client.get(f"/api/clients/{uuid.uuid4()}", headers=h).status_code == 404
    assert api_client.get("/api/clients/not-a-uuid", headers=h).status_code == 422
    c = client_factory()
    body = api_client.get(f"/api/clients/{c.id}", headers=h).json()
    assert body["name"] == "Fabrikam" and body["integration"] is None and body["users"] == []


def test_status_transition_happy_path_is_audited(api_client, admin, client_factory):
    a, h = admin
    c = client_factory(status=ClientStatus.ACTIVE)
    r = api_client.patch(f"/api/clients/{c.id}/status", json={"status": "suspended", "reason": "  unpaid   invoice "}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    ev = AuditEvent.objects.get(action="client.status_change")
    assert ev.actor_id == a.id and ev.metadata == {"from": "active", "to": "suspended", "reason": "unpaid invoice"}


def test_staff_cannot_suspend_and_attempt_is_audited(api_client, staff, client_factory):
    s, h = staff
    c = client_factory(status=ClientStatus.ACTIVE)
    r = api_client.patch(f"/api/clients/{c.id}/status", json={"status": "suspended", "reason": "because"}, headers=h)
    assert r.status_code == 403
    c.refresh_from_db()
    assert c.status == ClientStatus.ACTIVE
    assert AuditEvent.objects.filter(action="client.status_change", outcome="denied", actor_id=s.id).count() == 1


@pytest.mark.parametrize(
    "start,target",
    [("prospect", "active"), ("offboarded", "active"), ("active", "prospect"), ("active", "active")],
)
def test_invalid_transitions_conflict(api_client, admin, client_factory, start, target):
    _, h = admin
    c = client_factory(status=start)
    r = api_client.patch(f"/api/clients/{c.id}/status", json={"status": target, "reason": "testing"}, headers=h)
    assert r.status_code == 409
    c.refresh_from_db()
    assert c.status == start


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "active"},
        {"reason": "no status"},
        {"status": "bogus", "reason": "valid reason"},
        {"status": "active", "reason": ""},
        {"status": "active", "reason": "  "},
        {"status": "active", "reason": "ab"},
        {"status": "active", "reason": "x" * 501},
        {"status": 5, "reason": "valid reason"},
        {"status": "active", "reason": "valid reason", "is_admin": True},  # unknown fields are rejected
    ],
)
def test_status_change_body_validation(api_client, admin, client_factory, payload):
    _, h = admin
    c = client_factory(status=ClientStatus.SUSPENDED)
    r = api_client.patch(f"/api/clients/{c.id}/status", json=payload, headers=h)
    assert r.status_code == 422, (payload, r.text)


def test_status_change_unknown_client(api_client, admin):
    _, h = admin
    r = api_client.patch(f"/api/clients/{uuid.uuid4()}/status", json={"status": "active", "reason": "whatever"}, headers=h)
    assert r.status_code == 404


def test_malformed_json_body_is_422_or_400(api_client, admin, client_factory):
    _, h = admin
    c = client_factory()
    r = api_client.patch(f"/api/clients/{c.id}/status", content=b"{nope", headers={**h, "Content-Type": "application/json"})
    assert r.status_code in (400, 422)


def test_domain_is_unique_case_insensitively(db):
    from django.db import IntegrityError, transaction

    Client.objects.create(name="A", primary_domain="example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        Client.objects.create(name="B", primary_domain="EXAMPLE.com")
