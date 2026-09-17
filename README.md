# SEEDING Plan Backend

Standalone implementation of the safe, read-only slice of the V1.1 SEEDING
Design RFC (Feishu revision 38). This repository contains only the SEEDING
planning backend, its SQLite policy, migrations, tests, and local API entry.

## Included

- versioned ProjectConfig, static DMP, dynamic behavior, keyword, note, pair,
  plan, ExecutionGrant, and ReleaseGrant contracts;
- strict `0 <= I+TI <= AIPS <= population` validation;
- Type-7 quantiles, Winsorize, population z-score, directional overlap,
  keyword normalization, heat threshold, IQR/MAD bid evidence, and K=0 budget
  behavior;
- stable `logical_plan_key` without `config_sha`, plus separate plan revision;
- SQLite migrations for projects, revisions, artifacts, plans, grants,
  attempts, platform objects, state transitions, durable jobs, and outbox;
- lease-backed durable prepare jobs with HTTP 202, status, cancel, and resume;
- authenticated/RBAC project, prepare, preview, and confirmation APIs;
- immutable matrix artifacts and compare-and-set confirmation;
- explicit 423 responses for test-write authorization, execution, and release.

## Explicitly not included

- no Juguang/Lingxi browser or API platform adapter;
- no production or test-account advertising writes;
- no ReleaseGrant issuance/activation/revocation implementation;
- no automatic Search release; the preview reports
  `SEARCH_RELEASE_GATE_NOT_EVALUATED` when Search is enabled;
- no content Brief generation.

These exclusions are fail-closed because the RFC still has unresolved platform
enums, ReleaseGrant lifecycle details, Search release thresholds, and the
original ROI integration baseline has not passed its golden-test gate.

## Reproducible setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-seeding.lock
.venv/bin/pip install . --no-deps
.venv/bin/python -m pytest -q \
  tests/test_seeding_algorithms.py \
  tests/test_seeding_contracts.py \
  tests/test_seeding_store_service.py \
  tests/test_seeding_api.py
```

## Local API

The standalone entry is deny-all unless credentials and roles are injected by
a secret manager. Never commit this environment variable.

```bash
export SEEDING_DATABASE_PATH="$PWD/runtime/seeding.sqlite3"
export SEEDING_OPERATOR_AUTH_JSON='{"operator-id":{"token":"runtime-secret","roles":["admin"]}}'
.venv/bin/uvicorn seeding_entry:app --host 127.0.0.1 --port 8031
```

Required headers are `X-CID-Operator-Id`, `X-CID-Operator-Token`, and
`X-CID-Session-Id`. Tokens, cookies, and full sensitive headers are neither
logged nor persisted by the SEEDING package.

`GET /health` must always report:

```json
{
  "platform_adapter_mounted": false,
  "formal_execution_locked": true,
  "platform_business_write_count": 0
}
```
