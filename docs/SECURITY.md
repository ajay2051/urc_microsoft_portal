# Security model

## Authentication
1. The SPA obtains an Entra **access token for this API** (scope `api://<ENTRA_API_CLIENT_ID>/access_as_user`; set `accessTokenAcceptedVersion: 2` in the app manifest).
2. `EntraAuthMiddleware` runs before every route except `/api/health` and `/api/ready` (deny by default; `test_every_route_except_probes_requires_authentication` enforces it).
3. `apps/core/entra.py` verifies: RS256 only, key by `kid` from the tenant JWKS (cached, refresh rate-limited so junk tokens cannot hammer Microsoft), `iss` = our tenant v2.0, `aud` = this API, `exp/nbf/iat` required, `tid` = our tenant, `ver` = 2.0.
4. `apps/core/auth.py` maps `tid` + `oid` to a pre-provisioned `StaffUser`. Unknown or deactivated users get 403; app-only tokens and tokens lacking the scope get 403.

**Why custom validation:** django-bolt 0.13.0's native JWT check treats `ver` as its own integer claim and rejects Entra's `"ver": "2.0"`, which would reject every real user.

**Why single-tenant:** accepting `common`/`organizations` lets any Microsoft tenant mint a valid token. Settings refuse a non-GUID tenant.

**First sign-in binding:** the immutable `oid` is bound to a pre-provisioned email once, only inside our own tenant. After that only `oid` is trusted, so a later account with the same UPN cannot take over.

## Authorisation
Roles live in the database, never in token claims. Admin: tenant settings, consent verification, audit log, suspend/offboard. Denied privileged attempts are audited.

## Microsoft Graph
Tenant ids are validated GUIDs before use in URLs; `@odata.nextLink` is followed only for `https://graph.microsoft.com`; timeouts, bounded retries honouring `Retry-After`, bounded paging; partial data is never treated as complete; upstream payloads are never returned to clients.

## Data integrity
DB-enforced single running sync and single active onboarding per client; soft removal of users; refusal of syncs that would empty or mass-remove the mirror; append-only, secret-redacting audit log written in the same transaction as the change.

## Operational
Security headers on all responses, no CORS wildcard, 1 MB body cap, OpenAPI docs only in debug, `X-Forwarded-For` trusted only when configured.
