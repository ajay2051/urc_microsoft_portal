# Microsoft Client Operations Portal: API (Django + django-bolt)

Internal staff tool for managing client Microsoft 365 accounts: client list and lifecycle, Microsoft 365 user sync from Entra ID via Microsoft Graph, onboarding trigger, tenant settings, and an audit log. This repository is the **backend**; the React frontend lives in `portal-ui`.

## Why Django and django-bolt

**Django** gives a mature ORM, migrations, validation, a large ecosystem and a security track record, so we do not hand-roll persistence or auth plumbing.

**django-bolt** keeps Django's models and ORM but serves HTTP through a Rust (Actix) core:

- **Fast:** the project advertises 60k+ requests/second on its hot path, which is in the range of Node.js-class frameworks. That is the project's own figure; *this service has not been benchmarked*, so measure on your hardware before quoting it.
- **Async-first:** handlers are `async def`, reads use Django's async ORM, and blocking transactional work runs on Bolt's ORM executor so the event loop is never blocked.
- **Typed validation:** msgspec-based request/response models validate input before handler code runs.

Trade-off: django-bolt is young, so it is pinned to `0.13.0`. See "Known limitations".

## Project layout

```
manage.py
project/            settings, env helpers, root BoltAPI (project/api.py)
core/               auth middleware, Entra token verifier, errors, pagination
accounts/           StaffUser (authorisation record, no passwords)
clients/            Client lifecycle, mirrored Microsoft 365 users
integrations/       tenant settings, sync runs, Graph client (live + mock)
onboarding/         onboarding runs
audit/              append-only audit log
scripts/dev_token.py   mints local test tokens (development only)
docs/               SECURITY.md, ARCHITECTURE.md
```

## Run the project

### 1. Install

Requires Python 3.12+ and PostgreSQL (or SQLite for a quick try). With uv:

```bash
uv sync                    # install dependencies
# first time only, if you start from requirements.txt:
# uv init --bare && uv add -r requirements.txt
```

Without uv: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt`.

Keep `django-bolt==0.13.0` pinned.

### 2. Configure `.env`

Create `.env` in the project root (never commit it):

```
MODE=development
DJANGO_SECRET_KEY=<any long random string>

DATABASE_NAME=urc
DATABASE_USERNAME=postgres
DATABASE_PASSWORD=<password>
DATABASE_HOST=localhost
DATABASE_PORT=5432

# Entra ID: single tenant, never "common"
ENTRA_TENANT_ID=11111111-1111-1111-1111-111111111111
ENTRA_API_CLIENT_ID=22222222-2222-2222-2222-222222222222

# Microsoft Graph (mock in development)
GRAPH_BACKEND=mock
GRAPH_CLIENT_ID=
GRAPH_CLIENT_SECRET=
GRAPH_TIMEOUT_SECONDS=15
GRAPH_MAX_RETRIES=4
GRAPH_MAX_PAGES=200
GRAPH_PAGE_SIZE=200

SYNC_STALE_AFTER_SECONDS=900
SYNC_MAX_REMOVAL_RATIO=0.5
TRUST_X_FORWARDED_FOR=false
```

Every setting above must also be read in `project/settings.py` (`ENTRA_*`, `ENTRA_ISSUER`, `ENTRA_JWKS_URL`, `ENTRA_JWKS_JSON`, `GRAPH_*`, `SYNC_*`, `TRUST_X_FORWARDED_FOR`, `AUTH_USER_MODEL = "accounts.StaffUser"`, `BOLT_API = ["project.api:api"]`). A missing one fails at request time with `AttributeError: 'Settings' object has no attribute ...`.

### 3. Set up local authentication (development only)

There is no auth bypass. To test without a Microsoft tenant, create a throw-away signing key and let the server trust it:

```bash
python scripts/dev_token.py init
echo "ENTRA_JWKS_JSON='$(cat .dev/jwks.json)'" >> .env
```

The server refuses `ENTRA_JWKS_JSON` unless debug mode is on, so this key can never work in production. Remove that line from `.env` before using the file anywhere else. If you run `init` again, replace the line in `.env`, restart the server and mint new tokens.

### 4. Database and demo data

```bash
uv run python manage.py migrate
uv run python manage.py seed_demo            # demo staff and clients with fixed ids
uv run python manage.py seed_demo --reset    # back to the starting state after testing
```

Demo staff: `admin@contoso.com` (admin), `james.wright@contoso.com` and `priya.sharma@contoso.com` (staff).

Demo clients (fixed ids):

| Client | Status | Id |
|---|---|---|
| Fabrikam Inc | active, consent granted | `a0000000-0000-4000-8000-000000000001` |
| Northwind Traders | prospect, consent granted | `a0000000-0000-4000-8000-000000000002` |
| Adventure Works | onboarding, no tenant | `a0000000-0000-4000-8000-000000000003` |

Provision real staff with `python manage.py create_staff email@yourdomain.com "Name" --role admin`.

### 5. Start the server

```bash
uv run python manage.py runbolt --port 8000
# development reload: uv run python manage.py runbolt --dev
```

Check it: `curl http://127.0.0.1:8000/api/health` returns `{"status":"ok"}`. The server reads `.env` at startup, so restart it after editing.

### 6. Run the tests

```bash
uv run pytest
```

The automated tests mint their own tokens, so they need no server and no Microsoft account.

## Test the API manually (curl or Postman)

Get a token (reads `ENTRA_TENANT_ID` and `ENTRA_API_CLIENT_ID` from `.env`):

```bash
python scripts/dev_token.py token admin@contoso.com
python scripts/dev_token.py token james.wright@contoso.com
```

Tokens last 1 hour. Send the output as `Authorization: Bearer <token>`.

In Postman, set collection variables `base_url = http://127.0.0.1:8000/api`, `token`, `staff_token`, and the three client ids from the table above. Set the collection Authorization to Bearer Token `{{token}}`, and override with `{{staff_token}}` for staff requests. Use Body, raw, JSON for PATCH/POST/PUT.

Suggested order, with the expected result:

| # | Request | Expected |
|---|---|---|
| 1 | `GET /health` (no token) | 200 |
| 2 | `GET /me` (no token) | 401 `missing_token` |
| 3 | `GET /me` (admin / staff) | 200, role `admin` / `staff` |
| 4 | `GET /clients?status=active&q=fab` | 200, Fabrikam |
| 5 | `GET /clients?page=0` | 422 |
| 6 | `POST /sync/microsoft` `{"client_id": "<fab>"}` | 200, `created: 8` |
| 7 | `GET /graph/users?client_id=<fab>&page_size=2` | 200 |
| 8 | `PUT /integrations/<fab>` as staff | 403 |
| 9 | `POST /integrations/<fab>/verify-consent` as admin | 200 |
| 10 | `PATCH /clients/<fab>/status` `{"status":"suspended","reason":"unpaid invoice"}` as staff | 403 |
| 11 | same request as admin | 200 |
| 12 | `PATCH` to `prospect` as admin | 409 `invalid_transition` |
| 13 | `POST /onboarding/start` `{"client_id": "<nw>"}` with header `Idempotency-Key: demo-key-0001`, twice | 202; second has `replayed: true` |
| 14 | `GET /audit` as admin / staff | 200 / 403 |

## Docker Compose

`docker-compose.yml` runs three services: `db` (PostgreSQL 16), `migrate` (a one-off job that applies migrations, then exits) and `api` (the server). Migrations run in their own service so two API replicas never race on them.

```bash
docker compose up --build -d                    # start everything
docker compose ps                               # api should become "healthy"
curl http://127.0.0.1:8000/api/health           # {"status":"ok"}
docker compose --profile dev run --rm seed      # demo data (needs debug mode in .env)
docker compose logs -f api                      # follow logs
docker compose down                             # stop; add -v to also delete the database volume
```

Things to know:

- The container listens on **8080** (`Dockerfile` `CMD ... --port 8080`) and Compose publishes it on **8000** of your machine, so the API is at `http://127.0.0.1:8000/api`. Use `127.0.0.1`, not `0.0.0.0`, in a browser. The Dockerfile, the `ports:` mapping and the healthcheck must all agree on the container port.
- Inside Compose the database host is `db`, not `localhost`; the compose file overrides `DATABASE_HOST` and `DATABASE_PORT` for you.
- `.env` is read by Compose. A `$` in a value is treated as a variable and silently replaced (the symptom is `The "xxxx" variable is not set`). Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(64))"` so they contain only safe characters.
- The container starts in production mode unless `.env` enables debug. In production mode the mock Graph and `dev_token.py` tokens do not work; to try the dev auth flow in Docker, enable debug and add `ENTRA_JWKS_JSON` to `.env`.
- A `.dockerignore` keeps `.env`, `.dev/`, `.venv/` and `.git` out of the image. Do not remove it.
- Regenerate `requirements.txt` whenever dependencies change: `uv export --no-dev --no-hashes -o requirements.txt`.

## CI/CD (GitHub Actions)

`.github/workflows/ci-cd.yml` has three jobs.

| Job | Runs on | What it does |
|---|---|---|
| `test` | every push and pull request | `uv sync`, `ruff check`, `makemigrations --check` (fails if a migration is missing), `pytest`, and `pip-audit` for known vulnerable dependencies |
| `image` | after `test` passes | builds the Docker image; on pushes only, publishes it to GitHub Container Registry (`ghcr.io/<owner>/<repo>`, tagged by commit SHA and `latest`) and scans it with Trivy for critical/high vulnerabilities |
| `deploy` | pushes to `main`, after `image` | SSHes to the server, runs `docker compose pull && docker compose up -d`. The `production` environment can require a manual approval |

Setup:

1. Make sure `uv add --dev ruff pytest pytest-django` has been run so CI has its tools.
2. In GitHub, add the repository secrets `DEPLOY_HOST`, `DEPLOY_USER` and `DEPLOY_SSH_KEY`, and create an environment named `production` (add required reviewers for manual approval).
3. On the server, put a production `docker-compose.yml` in `/opt/client-ops` that uses `image: ghcr.io/<owner>/<repo>:latest` for `api` and `migrate` (no `build:` and no `seed`), plus the real `.env`. Run `docker login ghcr.io` once if the repository is private.
4. The production `.env` needs: debug off, `GRAPH_BACKEND=live` with real `GRAPH_CLIENT_ID` and `GRAPH_CLIENT_SECRET`, your real `ENTRA_TENANT_ID` and `ENTRA_API_CLIENT_ID`, a strong `DJANGO_SECRET_KEY`, and **no `ENTRA_JWKS_JSON`**.

Limits of this pipeline: CI tests run on SQLite (an in-memory test database), not PostgreSQL; the deploy job is a plain SSH template and must be rewritten for Kubernetes, Azure App Service or similar; and no step runs against a real Microsoft tenant.

## Before exposing it to the internet

- Confirm unauthenticated requests to `/docs`, `/admin/` and `/openapi.json` return 401 or 404, not a page. (The project does not define an admin site, and API docs are meant to exist in debug mode only.)
- Put TLS in front of the container (a reverse proxy or load balancer) and set `TRUST_X_FORWARDED_FOR=true` only if that proxy overwrites the header.
- Add startup checks in `settings.py` that refuse weak configuration when debug is off: short secret key, `GRAPH_BACKEND=mock`, a set `ENTRA_JWKS_JSON`, or a non-GUID `ENTRA_TENANT_ID`.

## API

All routes are under `/api` and require `Authorization: Bearer <Entra access token>` except `/health` and `/ready`.

| Method | Route | Notes |
|---|---|---|
| GET | `/me` | current staff profile |
| GET | `/clients` | filters `status`, `q`; `page`, `page_size` (max 100) |
| GET | `/clients/{id}` | client, integration summary, first 100 users |
| PATCH | `/clients/{id}/status` | `{status, reason}`; transition rules; suspend/offboard are admin-only |
| GET | `/graph/users?client_id=` | mirrored users, paginated |
| GET / PUT | `/integrations/{client_id}` | tenant settings (PUT admin-only) |
| POST | `/integrations/{client_id}/verify-consent` | admin; proves consent with a Graph call |
| POST | `/sync/microsoft` | `{client_id}` |
| POST | `/onboarding/start` | `{client_id}`, optional `Idempotency-Key` header, returns 202 |
| GET | `/audit` | admin-only; filters by actor, action, entity, outcome, time range |

Errors: `{"detail": "...", "extra": {"code": "machine_code"}}`; validation failures are 422.

## Real Microsoft sign-in

1. In Entra ID, register an **API app** that exposes the scope `access_as_user` and set `accessTokenAcceptedVersion` to `2` in its manifest. Its client id is `ENTRA_API_CLIENT_ID`.
2. Set `ENTRA_TENANT_ID` to your own directory's id.
3. Remove `ENTRA_JWKS_JSON` from `.env` so the server downloads Microsoft's signing keys.
4. Add staff with `create_staff`, then sign in through the frontend or request a token for `api://<api-client-id>/access_as_user`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Postman returns 401 `unauthorized` / "Authentication required." although a token is sent | `EntraAuthMiddleware` is not enabled. In `project/api.py`, use `middleware=[SecurityHeadersMiddleware, EntraAuthMiddleware]` (import it from `core.auth`) and keep prefix `/api` |
| 401 `invalid_token` | the token and the server disagree: different `ENTRA_TENANT_ID` / `ENTRA_API_CLIENT_ID`, expired token, or the key was recreated. Re-run `init`, update `.env`, restart, mint a new token |
| 503 `idp_unavailable` | the server has no `ENTRA_JWKS_JSON`, so it tried to download real Microsoft keys. Check `python manage.py shell -c "from django.conf import settings; print(bool(settings.ENTRA_JWKS_JSON))"` prints `True` |
| 403 `not_provisioned` | the email isn't a staff user. Run `seed_demo` or `create_staff` |
| `AttributeError: 'Settings' object has no attribute ...` | a setting from step 2 is missing in `project/settings.py` |
| Token script says "Set ENTRA_TENANT_ID first" | the value is not in `.env` and not exported |
| Wrong data after testing | `python manage.py seed_demo --reset` |

## Security notes

See `docs/SECURITY.md`. In short: tokens are verified for signature, issuer, audience, tenant and expiry on every route except the two probes; roles come from the database, never from token claims; request bodies reject unknown fields; the audit log is append-only; Microsoft Graph calls are bounded, retried and never follow non-Graph links.

## Known limitations

- Tests run on SQLite; run CI against PostgreSQL (row locks and partial unique indexes).
- Sync runs inside the request. Large tenants need a queue and Graph delta queries.
- Onboarding creates a queued run; a worker to execute the steps is not built.
- Graph app credentials are a client secret from the environment; prefer a certificate or Key Vault.
- django-bolt 0.13.0 rejects Entra tokens in its native JWT check, so token validation is our own code (`core/entra.py`).
- There is no endpoint to create or edit clients yet.