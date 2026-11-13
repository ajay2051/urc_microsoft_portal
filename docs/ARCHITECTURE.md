# Architecture

```
config/            settings (env-driven, fail-fast), root BoltAPI
apps/core          auth middleware + Entra verifier, errors, pagination, http helpers
apps/accounts      StaffUser (authorisation record, no passwords)
apps/clients       Client lifecycle (state machine), M365User mirror
apps/integrations  TenantIntegration, SyncRun, Graph client (live + mock), sync service
apps/onboarding    OnboardingRun
apps/audit         append-only AuditEvent
```

Layering: `api.py` (HTTP, validation) -> `services.py` (business rules, transactions, audit) -> models. Routers never touch business rules; services never touch HTTP.

Async: reads use Django's async ORM; multi-statement atomic units are sync functions run via `run_atomic_unit` (Bolt ORM executor). Network calls to Graph happen outside transactions.

Sync flow: take DB lock (SyncRun) -> fetch from Graph -> diff and apply in one transaction -> audit. Failures mark the run failed and release the lock; stale locks expire.

Extending: add an app with `schemas.py`, `services.py`, `api.py`, register its router in `config/api.py`, and add tests; the route-coverage test will confirm it is authenticated.
