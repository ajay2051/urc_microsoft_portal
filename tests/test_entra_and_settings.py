import json
from pathlib import Path

import httpx
import pytest

from core import entra
from core.entra import EntraTokenVerifier, JWKSCache, KeysUnavailable, TokenInvalid
from tests.conftest import ISSUER, make_token
from tests.keys import API_CLIENT_ID, JWKS_JSON, TENANT_ID

ROOT = Path(__file__).resolve().parent.parent


def run(coro):
    import asyncio

    return asyncio.run(coro)


def verifier(jwks):
    return EntraTokenVerifier(issuer=ISSUER, audience=API_CLIENT_ID, tenant_id=TENANT_ID, jwks=jwks)


def test_verifier_accepts_good_and_rejects_garbage():
    v = verifier(JWKSCache(static_jwks=JWKS_JSON))
    token = make_token(oid="a" * 8 + "-aaaa-aaaa-aaaa-" + "a" * 12, username="a@b.com")
    assert run(v.verify(token))["tid"] == TENANT_ID
    for bad in ("", "x", "a.b.c", "a" * (entra.MAX_TOKEN_BYTES + 1)):
        with pytest.raises(TokenInvalid):
            run(v.verify(bad))


def test_token_without_required_claims_rejected():
    import jwt

    from tests.keys import KID, PRIVATE_KEY

    v = verifier(JWKSCache(static_jwks=JWKS_JSON))
    for missing in ("exp", "nbf", "iat", "aud", "iss"):
        claims = jwt.decode(make_token(oid="x", username="a@b.com"), options={"verify_signature": False})
        claims.pop(missing)
        token = jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})
        with pytest.raises(TokenInvalid):
            run(v.verify(token))


def test_jwks_cache_requires_exactly_one_source_and_https():
    with pytest.raises(ValueError):
        JWKSCache()
    with pytest.raises(ValueError):
        JWKSCache(url="https://x/keys", static_jwks=JWKS_JSON)
    with pytest.raises(ValueError):
        JWKSCache(url="http://insecure.example.com/keys")


def test_remote_jwks_refresh_is_rate_limited(monkeypatch):
    calls = {"n": 0}

    def route(request):
        calls["n"] += 1
        return httpx.Response(200, json=json.loads(JWKS_JSON))

    real = httpx.AsyncClient
    monkeypatch.setattr(entra.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(route), **kw))
    cache = JWKSCache(url="https://login.microsoftonline.com/t/discovery/v2.0/keys", min_refresh_interval=60)
    assert run(cache.get_key("test-key-1")) is not None and calls["n"] == 1
    assert run(cache.get_key("test-key-1")) is not None and calls["n"] == 1  # cached
    for _ in range(5):  # a flood of unknown key ids must not hammer Microsoft
        with pytest.raises(TokenInvalid):
            run(cache.get_key("attacker-kid"))
    assert calls["n"] == 1


def test_keys_unavailable_when_idp_down_and_no_cache(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        entra.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(500)), **kw)
    )
    cache = JWKSCache(url="https://login.microsoftonline.com/t/discovery/v2.0/keys")
    with pytest.raises(KeysUnavailable):
        run(cache.get_key("any"))


def test_keeps_last_good_keys_when_refresh_fails(monkeypatch):
    state = {"ok": True}

    def route(request):
        return httpx.Response(200, json=json.loads(JWKS_JSON)) if state["ok"] else httpx.Response(500)

    real = httpx.AsyncClient
    monkeypatch.setattr(entra.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(route), **kw))
    cache = JWKSCache(url="https://login.microsoftonline.com/t/discovery/v2.0/keys", ttl_seconds=0, min_refresh_interval=0)
    assert run(cache.get_key("test-key-1")) is not None
    state["ok"] = False
    assert run(cache.get_key("test-key-1")) is not None  # stale-but-valid beats an outage


# # ------------------------- production configuration fail-fast ------------------------
# def boot(env: dict[str, str]) -> subprocess.CompletedProcess:
#     base = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "ENTRA_", "GRAPH_", "DATABASE_"))}
#     base.update({"DJANGO_SETTINGS_MODULE": "config.settings", "PYTHONPATH": str(ROOT)})
#     base.update(env)
#     return subprocess.run(
#         [sys.executable, "-c", "import django; django.setup(); from django.conf import settings; settings.SECRET_KEY"],
#         env=base,
#         capture_output=True,
#         text=True,
#         cwd=ROOT,
#     )
#
#
# GOOD = {
#     "DJANGO_SECRET_KEY": "k" * 60,
#     "DJANGO_ALLOWED_HOSTS": "api.example.com",
#     "ENTRA_TENANT_ID": TENANT_ID,
#     "ENTRA_API_CLIENT_ID": API_CLIENT_ID,
#     "GRAPH_CLIENT_ID": "33333333-3333-3333-3333-333333333333",
#     "GRAPH_CLIENT_SECRET": "s",
#     "DATABASE_URL": "sqlite:///:memory:",
# }
#
#
# def test_production_settings_boot_when_complete():
#     assert boot(GOOD).returncode == 0, boot(GOOD).stderr
#
#
# @pytest.mark.parametrize(
#     "drop,override",
#     [
#         ("DJANGO_SECRET_KEY", {}),
#         ("DJANGO_ALLOWED_HOSTS", {}),
#         ("ENTRA_TENANT_ID", {}),
#         ("ENTRA_API_CLIENT_ID", {}),
#         ("GRAPH_CLIENT_SECRET", {}),
#         (None, {"DJANGO_SECRET_KEY": "short"}),
#         (None, {"ENTRA_TENANT_ID": "common"}),  # multi-tenant authority is refused
#         (None, {"GRAPH_BACKEND": "mock"}),  # mock Graph must never run in production
#         (None, {"CORS_ALLOWED_ORIGINS": "*"}),
#         (None, {"CORS_ALLOWED_ORIGINS": "http://insecure.example.com"}),
#     ],
# )
# def test_production_refuses_unsafe_configuration(drop, override):
#     env = {k: v for k, v in GOOD.items() if k != drop}
#     env.update(override)
#     result = boot(env)
#     assert result.returncode != 0 and "ImproperlyConfigured" in result.stderr
