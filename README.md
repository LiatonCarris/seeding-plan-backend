# SEEDING Plan Backend

Independent, fail-closed backend for the SEEDING planning and Xiaohongshu
Juguang execution workflow. The implementation follows the SEEDING Design RFC
revision 71 and keeps planning, platform writes, and production activation as
separate authorization domains.

## Implemented modules

- **Source layer** — typed Juguang MAPI reads for deliverable audiences,
  audience estimates, keyword recommendations, word bags, unit/campaign/
  creativity readback, and group reports; audited Lingxi AIPS/I+TI import.
- **Decision layer** — static DMP validation/scoring, dynamic audience
  separation, directional containment/Jaccard evidence, keyword normalization,
  exact/semantic dedupe, heat gates, IQR/MAD bid evidence, Search release gates,
  and K=0 budget handling.
- **Compiler layer** — deterministic Feed and Search plan matrices and strict
  `cascade/create` campaign/unit/creative payloads pinned to a verified platform
  profile.
- **Execution layer** — HMAC-signed, one-time grants; create → immediate pause →
  campaign/unit/creative readback → account relock; unknown results enter an
  explicit reconcile flow and are never recreated automatically.
- **Release layer** — a separate production paused-create grant, followed by an
  independent ReleaseGrant bound to exact campaign/unit/creative IDs, an armed
  spend cap, enable readback, periodic spend evaluation, and emergency
  pause/relock.
- **Control plane** — FastAPI authentication/RBAC, per-project membership,
  SQLite authority, migrations, durable jobs/leases, outbox, audit trail,
  worker heartbeats, operational metrics, preview, confirmation,
  revoke/cancel/resume/reconcile, and a browser operations console.

Content Brief generation is intentionally outside this RFC scope.

## Safety defaults

The normal entry point mounts only planning and optional Juguang **read**
capabilities. Platform writes remain unavailable unless deployment code injects:

1. a Juguang client with the exact TEST or PRODUCTION advertiser allowlist;
2. an account relock adapter for every create attempt, including timeouts;
3. a production spend-monitor/emergency-relock adapter;
4. a runtime HMAC signing key.

Tokens and signing keys are read from files. They are not accepted in request
bodies, persisted in SQLite, or included in platform error details.

## Reproducible setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-seeding.lock
.venv/bin/pip install . --no-deps
.venv/bin/python -m pytest -q
```

The repository also includes `Dockerfile` and `compose.example.yml`. The compose
example uses mounted secret files, a named SQLite volume, a readiness
healthcheck, and binds the API to localhost.

## Runtime configuration

Copy `.env.example` and create the referenced files outside version control.

| Variable | Purpose |
|---|---|
| `SEEDING_DATABASE_PATH` | SQLite authority path |
| `SEEDING_OPERATOR_AUTH_FILE` | operator/role JSON file |
| `SEEDING_GRANT_SIGNING_KEY_FILE` | at least 32-byte HMAC key |
| `SEEDING_ALERT_WEBHOOK_FILE` | optional file containing the alert webhook URL |
| `JUGUANG_ACCESS_TOKEN_FILE` | Juguang MAPI Access-Token file |

Operator file example:

```json
{
  "operator-id": {
    "token": "replace-in-secret-store",
    "roles": ["viewer", "operator", "approver", "executor", "admin"]
  }
}
```

Start locally:

```bash
set -a
. ./.env
set +a
.venv/bin/uvicorn seeding_entry:app --host 127.0.0.1 --port 8031
```

Requests require `X-CID-Operator-Id`, `X-CID-Operator-Token`, and
`X-CID-Session-Id`. Use `GET /ready` to see which capabilities are configured;
it returns HTTP 503 until planning authentication is mounted. `GET /health`
does not expose secret values. `/console` provides authenticated GET/POST/PUT
operations without persisting credentials in browser storage.

## Main workflow endpoints

- `POST /api/seeding/v1/projects`
- `POST /projects/{id}/sources/juguang/sync`
- `POST /projects/{id}/sources/lingxi/import`
- `POST /projects/{id}/prepare` → `GET /operations/{job_id}`
- `GET /projects/{id}/preview` → `POST /projects/{id}/confirm`
- `POST /projects/{id}/execution-grants` → `POST /projects/{id}/execute`
- `POST /projects/{id}/production-create-grants` →
  `POST /projects/{id}/production-create`
- `POST /projects/{id}/release-grants` →
  `POST /projects/{id}/release-grants/{grant_id}/execute`
- `POST /projects/{id}/release-grants/{grant_id}/evaluate`
- `POST /projects/{id}/jobs/{job_id}/reconcile`
- `GET /projects/{id}/audit` and `GET /api/seeding/v1/ops/metrics`

All paths above are prefixed with `/api/seeding/v1` unless already shown.

## External inputs still required for live validation

- a Juguang test advertiser ID, MAPI Access-Token, and required account
  permissions/whitelist;
- an implementation of the account-level relock interface (not exposed in the
  public Juguang MAPI documents reviewed for this project);
- a spend monitor/emergency relock implementation for production release;
- frozen business ParameterSet decisions and verified Juguang enum values;
- Lingxi exports/private API data for AIPS, I+TI, and audience intersections.

Without these inputs the system remains useful for deterministic planning and
payload inspection, but live write/release capabilities stay unavailable.
