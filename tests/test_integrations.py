import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from audit.models import AuditEvent
from clients.models import ClientStatus, M365User
from integrations.graph import GraphError
from integrations.models import ConsentStatus, SyncRun, SyncStatus, TenantIntegration
from tests.conftest import GUEST_TENANT, TENANT_ID, graph_user

pytestmark = pytest.mark.django_db(transaction=True)

OTHER_TENANT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


# ------------------------------ tenant settings ------------------------------
def test_put_integration_admin_only(api_client, staff, admin, client_factory):
    c = client_factory()
    body = {"tenant_id": GUEST_TENANT.upper(), "sync_enabled": True}
    assert api_client.put(f"/api/integrations/{c.id}", json=body, headers=staff[1]).status_code == 403
    r = api_client.put(f"/api/integrations/{c.id}", json=body, headers=admin[1])
    assert r.status_code == 200
    out = r.json()
    assert out["tenant_id"] == GUEST_TENANT and out["consent_status"] == "not_requested"
    assert AuditEvent.objects.filter(action="integration.created").exists()
    assert api_client.get(f"/api/integrations/{c.id}", headers=staff[1]).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tenant_id": "not-a-guid"},
        {"tenant_id": ""},
        {"tenant_id": GUEST_TENANT, "sync_enabled": "maybe"},
        {"tenant_id": GUEST_TENANT, "consent_status": "granted"},  # consent cannot be self-asserted
        {"tenant_id": 5},
    ],
)
def test_put_integration_validation(api_client, admin, client_factory, body):
    c = client_factory()
    assert api_client.put(f"/api/integrations/{c.id}", json=body, headers=admin[1]).status_code == 422


def test_own_tenant_and_duplicate_tenant_rejected(api_client, admin, client_factory):
    a, b = client_factory("A", "a.com"), client_factory("B", "b.com")
    assert api_client.put(f"/api/integrations/{a.id}", json={"tenant_id": TENANT_ID}, headers=admin[1]).status_code == 400
    assert api_client.put(f"/api/integrations/{a.id}", json={"tenant_id": GUEST_TENANT}, headers=admin[1]).status_code == 200
    r = api_client.put(f"/api/integrations/{b.id}", json={"tenant_id": GUEST_TENANT}, headers=admin[1])
    assert r.status_code == 409 and r.json()["extra"]["code"] == "tenant_already_linked"


def test_changing_tenant_resets_consent_and_mirror(api_client, admin, linked_client):
    c = linked_client()
    M365User.objects.create(client=c, graph_id=str(uuid.uuid4()), user_principal_name="x@y.com", last_synced_at=timezone.now())
    r = api_client.put(f"/api/integrations/{c.id}", json={"tenant_id": OTHER_TENANT}, headers=admin[1])
    assert r.status_code == 200 and r.json()["consent_status"] == "not_requested"
    assert not M365User.objects.filter(client=c, removed_at__isnull=True).exists()


def test_get_integration_404s(api_client, staff, client_factory):
    c = client_factory()
    r = api_client.get(f"/api/integrations/{c.id}", headers=staff[1])
    assert r.status_code == 404 and r.json()["extra"]["code"] == "integration_not_found"
    r = api_client.get(f"/api/integrations/{uuid.uuid4()}", headers=staff[1])
    assert r.json()["extra"]["code"] == "client_not_found"


def test_verify_consent_success_and_revocation(api_client, admin, staff, client_factory, graph):
    c = client_factory()
    TenantIntegration.objects.create(client=c, tenant_id=GUEST_TENANT)
    assert api_client.post(f"/api/integrations/{c.id}/verify-consent", headers=staff[1]).status_code == 403
    r = api_client.post(f"/api/integrations/{c.id}/verify-consent", headers=admin[1])
    assert r.status_code == 200 and r.json()["consent_status"] == "granted"
    graph.no_consent = frozenset({GUEST_TENANT})
    r = api_client.post(f"/api/integrations/{c.id}/verify-consent", headers=admin[1])
    assert r.status_code == 200 and r.json()["consent_status"] == "revoked"


def test_verify_consent_transient_failure_keeps_status(api_client, admin, linked_client, graph, monkeypatch):
    c = linked_client()

    async def boom(tenant_id):
        raise GraphError("throttled", "slow down", retry_after=3)

    monkeypatch.setattr(graph, "probe", boom)
    r = api_client.post(f"/api/integrations/{c.id}/verify-consent", headers=admin[1])
    assert r.status_code == 503
    assert TenantIntegration.objects.get(pk=c.pk).consent_status == ConsentStatus.GRANTED


# ------------------------------------ sync -----------------------------------
def sync(api_client, headers, client_id):
    return api_client.post("/api/sync/microsoft", json={"client_id": str(client_id)}, headers=headers)


def test_sync_creates_updates_and_soft_removes(api_client, staff, linked_client, graph):
    c = linked_client()
    r = sync(api_client, staff[1], c.id)
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["updated"], r.json()["removed"], r.json()["total_active"]) == (3, 0, 0, 3)
    assert TenantIntegration.objects.get(pk=c.pk).last_sync_at is not None

    # idempotent: nothing changed
    body = sync(api_client, staff[1], c.id).json()
    assert (body["created"], body["updated"], body["removed"]) == (0, 0, 0)

    # one user edited, one removed, one new
    graph.users_by_tenant[GUEST_TENANT] = [graph_user(1, display_name="Renamed"), graph_user(2), graph_user(9)]
    body = sync(api_client, staff[1], c.id).json()
    assert (body["created"], body["updated"], body["removed"], body["total_active"]) == (1, 1, 1, 3)
    gone = M365User.objects.get(client=c, graph_id=graph_user(3).id)
    assert gone.removed_at is not None  # soft-removed, never deleted

    # the removed user returns -> restored
    graph.users_by_tenant[GUEST_TENANT].append(graph_user(3))
    body = sync(api_client, staff[1], c.id).json()
    assert body["updated"] == 1 and body["total_active"] == 4
    assert AuditEvent.objects.filter(action="sync.microsoft", outcome="success").count() == 4


def test_sync_validation_and_preconditions(api_client, staff, client_factory, linked_client, graph):
    h = staff[1]
    assert api_client.post("/api/sync/microsoft", json={}, headers=h).status_code == 422
    assert api_client.post("/api/sync/microsoft", json={"client_id": "x"}, headers=h).status_code == 422
    assert api_client.post("/api/sync/microsoft", json={"client_id": str(uuid.uuid4()), "tenant_id": "x"}, headers=h).status_code == 422
    assert sync(api_client, h, uuid.uuid4()).status_code == 404
    bare = client_factory("Bare", "bare.com")
    assert sync(api_client, h, bare.id).json()["extra"]["code"] == "integration_not_configured"
    unverified = client_factory("Unv", "unv.com")
    TenantIntegration.objects.create(client=unverified, tenant_id=OTHER_TENANT)
    r = sync(api_client, h, unverified.id)
    assert r.status_code == 409 and r.json()["extra"]["code"] == "tenant_consent_required"
    disabled = linked_client(domain="dis.com", tenant="cccccccc-cccc-cccc-cccc-cccccccccccc", sync_enabled=False)
    assert sync(api_client, h, disabled.id).json()["extra"]["code"] == "sync_disabled"
    off = linked_client(status=ClientStatus.OFFBOARDED, domain="off.com", tenant="dddddddd-dddd-dddd-dddd-dddddddddddd")
    assert sync(api_client, h, off.id).json()["extra"]["code"] == "client_offboarded"
    assert graph.calls == 0  # none of the rejected requests reached Microsoft


def test_sync_consent_missing_marks_revoked(api_client, staff, linked_client, graph):
    c = linked_client()
    graph.no_consent = frozenset({GUEST_TENANT})
    r = sync(api_client, staff[1], c.id)
    assert r.status_code == 409 and r.json()["extra"]["code"] == "tenant_consent_missing"
    assert TenantIntegration.objects.get(pk=c.pk).consent_status == ConsentStatus.REVOKED
    run = SyncRun.objects.get(client=c)
    assert run.status == SyncStatus.FAILED and run.error_code == "consent_missing"
    assert AuditEvent.objects.filter(action="sync.microsoft", outcome="failure").count() == 1


def test_sync_throttled_is_503_with_retry_after_and_releases_lock(api_client, staff, linked_client, graph, monkeypatch):
    c = linked_client()

    async def throttled(tenant_id):
        raise GraphError("throttled", "429", retry_after=7)

    monkeypatch.setattr(graph, "list_users", throttled)
    r = sync(api_client, staff[1], c.id)
    assert r.status_code == 503 and r.headers.get("retry-after") == "8"
    assert not SyncRun.objects.filter(status=SyncStatus.RUNNING).exists()  # lock released: retry possible


def test_sync_unexpected_error_never_leaks_internals(api_client, staff, linked_client, graph, monkeypatch):
    c = linked_client()

    async def explode(tenant_id):
        raise RuntimeError("secret-connection-string=xyz")

    monkeypatch.setattr(graph, "list_users", explode)
    r = sync(api_client, staff[1], c.id)
    assert r.status_code == 502 and "secret" not in r.text
    assert not SyncRun.objects.filter(status=SyncStatus.RUNNING).exists()


def test_concurrent_sync_is_rejected_by_db_lock(api_client, staff, linked_client, graph):
    c = linked_client()
    SyncRun.objects.create(client=c)  # a run that is genuinely in flight
    r = sync(api_client, staff[1], c.id)
    assert r.status_code == 409 and r.json()["extra"]["code"] == "sync_in_progress"


def test_stale_running_sync_is_superseded(api_client, staff, linked_client, graph):
    c = linked_client()
    SyncRun.objects.create(client=c, started_at=timezone.now() - timedelta(hours=2))  # crashed worker
    assert sync(api_client, staff[1], c.id).status_code == 200
    assert SyncRun.objects.filter(client=c, error_code="stale").count() == 1


def test_empty_or_mass_removal_result_is_refused(api_client, staff, linked_client, graph):
    c = linked_client()
    graph.users_by_tenant[GUEST_TENANT] = [graph_user(i) for i in range(1, 21)]
    assert sync(api_client, staff[1], c.id).status_code == 200
    graph.users_by_tenant[GUEST_TENANT] = []
    r = sync(api_client, staff[1], c.id)
    assert r.status_code == 409 and r.json()["extra"]["code"] == "suspicious_sync_result"
    graph.users_by_tenant[GUEST_TENANT] = [graph_user(1)]  # 95% removal
    assert sync(api_client, staff[1], c.id).status_code == 409
    assert M365User.objects.filter(client=c, removed_at__isnull=True).count() == 20  # mirror untouched


def test_graph_users_endpoint(api_client, staff, linked_client, graph):
    c = linked_client()
    sync(api_client, staff[1], c.id)
    h = staff[1]
    body = api_client.get(f"/api/graph/users?client_id={c.id}&page_size=2", headers=h).json()
    assert body["total"] == 3 and len(body["items"]) == 2
    assert api_client.get(f"/api/graph/users?client_id={c.id}&q=user2", headers=h).json()["total"] == 1
    assert api_client.get("/api/graph/users", headers=h).status_code == 422  # tenant scoping is mandatory
    assert api_client.get(f"/api/graph/users?client_id={uuid.uuid4()}", headers=h).status_code == 404
    detail = api_client.get(f"/api/clients/{c.id}", headers=h).json()
    assert detail["m365_user_count"] == 3 and detail["integration"]["consent_status"] == "granted"
