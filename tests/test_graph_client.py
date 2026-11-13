"""LiveGraphClient against a fake Microsoft (httpx MockTransport)."""

import httpx
import pytest

from integrations import graph as graph_module
from integrations.graph import GraphError, LiveGraphClient
from tests.conftest import GUEST_TENANT


def user(i):
    return {
        "id": f"00000000-0000-0000-0000-{i:012d}",
        "userPrincipalName": f"U{i}@Client.com",
        "displayName": f"U{i}",
        "mail": None,
        "jobTitle": None,
        "department": None,
        "accountEnabled": True,
    }


@pytest.fixture
def fake_ms(monkeypatch, settings):
    settings.GRAPH_BACKEND = "live"
    settings.GRAPH_CLIENT_ID = "33333333-3333-3333-3333-333333333333"
    settings.GRAPH_CLIENT_SECRET = "s3cret"
    settings.GRAPH_MAX_RETRIES = 2
    monkeypatch.setattr(graph_module, "MAX_RETRY_AFTER_SECONDS", 0.01)
    handlers = {"calls": [], "graph": None, "token": None}

    def route(request: httpx.Request) -> httpx.Response:
        handlers["calls"].append(request)
        if request.url.host == "login.microsoftonline.com":
            return handlers["token"](request) if handlers["token"] else httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600}
            )
        return handlers["graph"](request)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(route)
        return real(*args, **kwargs)

    monkeypatch.setattr(graph_module.httpx, "AsyncClient", factory)
    return handlers


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_follows_pagination_and_normalises(fake_ms):
    def g(request):
        if "page2" in str(request.url):
            return httpx.Response(200, json={"value": [user(2), {"id": "bad"}, {"userPrincipalName": "x"}]})
        return httpx.Response(200, json={"value": [user(1)], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?page2"})

    fake_ms["graph"] = g
    users = run(LiveGraphClient().list_users(GUEST_TENANT))
    assert [u.user_principal_name for u in users] == ["u1@client.com", "u2@client.com"]  # junk skipped, lowercased
    assert all(c.headers.get("authorization") == "Bearer tok" for c in fake_ms["calls"] if c.url.host == "graph.microsoft.com")


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example.com/steal",
        "http://graph.microsoft.com/v1.0/users?x",
        "https://graph.microsoft.com.evil.com/v1.0/users",
        "https://graph.microsoft.com:8443/v1.0/users",
        "//evil.com",
    ],
)
def test_refuses_foreign_next_link_so_token_cannot_leak(fake_ms, link):
    fake_ms["graph"] = lambda r: httpx.Response(200, json={"value": [user(1)], "@odata.nextLink": link})
    with pytest.raises(GraphError) as e:
        run(LiveGraphClient().list_users(GUEST_TENANT))
    assert e.value.code == "graph_bad_response"
    assert all(c.url.host in {"graph.microsoft.com", "login.microsoftonline.com"} for c in fake_ms["calls"])


def test_retries_throttling_then_succeeds(fake_ms):
    state = {"n": 0}

    def g(request):
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"value": [user(1)]})

    fake_ms["graph"] = g
    assert len(run(LiveGraphClient().list_users(GUEST_TENANT))) == 1 and state["n"] == 3


def test_gives_up_after_max_retries(fake_ms):
    fake_ms["graph"] = lambda r: httpx.Response(503, headers={"Retry-After": "5"})
    with pytest.raises(GraphError) as e:
        run(LiveGraphClient().list_users(GUEST_TENANT))
    assert e.value.code == "throttled" and e.value.retry_after is not None


def test_403_means_consent_missing_and_is_not_retried(fake_ms):
    fake_ms["graph"] = lambda r: httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}})
    with pytest.raises(GraphError) as e:
        run(LiveGraphClient().list_users(GUEST_TENANT))
    assert e.value.code == "consent_missing"
    assert len([c for c in fake_ms["calls"] if c.url.host == "graph.microsoft.com"]) == 1


def test_401_refreshes_token_once(fake_ms):
    state = {"n": 0}

    def g(request):
        state["n"] += 1
        return httpx.Response(401) if state["n"] == 1 else httpx.Response(200, json={"value": []})

    fake_ms["graph"] = g
    run(LiveGraphClient().list_users(GUEST_TENANT))
    assert len([c for c in fake_ms["calls"] if c.url.host == "login.microsoftonline.com"]) == 2


@pytest.mark.parametrize(
    "description,code",
    [("AADSTS65001: consent", "consent_missing"), ("AADSTS90002: tenant not found", "tenant_not_found"), ("whatever", "token_rejected")],
)
def test_token_endpoint_errors_are_classified(fake_ms, description, code):
    fake_ms["token"] = lambda r: httpx.Response(400, json={"error": "x", "error_description": description})
    fake_ms["graph"] = lambda r: httpx.Response(200, json={"value": []})
    with pytest.raises(GraphError) as e:
        run(LiveGraphClient().list_users(GUEST_TENANT))
    assert e.value.code == code


def test_page_limit_refuses_partial_directory(fake_ms, settings):
    settings.GRAPH_MAX_PAGES = 2
    fake_ms["graph"] = lambda r: httpx.Response(
        200, json={"value": [user(1)], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?more"}
    )
    with pytest.raises(GraphError) as e:
        run(LiveGraphClient().list_users(GUEST_TENANT))
    assert e.value.code == "graph_too_large"


def test_invalid_tenant_id_never_reaches_a_url(fake_ms):
    with pytest.raises(ValueError):
        run(LiveGraphClient().list_users("../../evil"))
    assert fake_ms["calls"] == []
