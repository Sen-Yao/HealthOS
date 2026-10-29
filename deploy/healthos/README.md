# HealthOS MVP

HealthOS runs as one standard-library Python service with SQLite and a mounted
filesystem. The MVP includes canonical health tables, Owner Console login,
manual records, the shared query layer, `HealthContext` projections, ephemeral
read-only share snapshots, and a native strength-training domain.

The training console supports synthetic/manual session entry, history and detail
views, editing sessions with their exercise records, confirmed single-session
deletion, per-record drop-set entry, an elapsed-time timer, and browser-local
draft recovery for new sessions. Exercise aliases can be managed in the Owner
Console and resolve to their canonical exercise using case-insensitive,
whitespace-insensitive matching; conflicting alias assignments are rejected.

The production `/training` route serves the Ironbook-aligned training console.
It reads and writes the local HealthOS SQLite training tables through Owner-only
JSON endpoints and the existing transactional save, edit, delete, alias, and
validation paths. History search and pagination, action suggestions, previous
session references, analytics, and e1RM charts use stored HealthOS data. The
former server-rendered form remains available at `/training-legacy` for
compatibility; `/training-demo` redirects to the production console.
Its analysis view summarizes 12
weeks of session frequency, volume, and effective sets, 90-day body-part counts,
and per-session exercise weight and volume trends. HealthOS currently counts an
effective set when its record is marked as a drop-set group or any recorded RPE
is at least 7; this is a derived HealthOS metric, not an imported Ironbook value.
Training is excluded from HealthContext and share snapshots by default. The
Owner must explicitly opt in before a ChatGPT copy or temporary share includes
training dates, body part, duration, volume, exercise names, weight, reps, RPE,
set count, and drop-set marker. Exercise and session notes are not exported.
Training data has its own tables and an explicit Apple Health workout match
table. The Ironbook transition is complete: its training history is imported into
HealthOS, and the production Owner training page now reads and writes the local
HealthOS training tables. Ironbook remains unchanged as a historical rollback
source; ongoing synchronization is not enabled.

The authenticated `GET /api/v1/training/migration-preflight` endpoint performs a
read-only mapping and duplicate check against the sessions returned by Ironbook.
It does not import or modify training data. HealthOS follows the optional
`format=page` response until its reported total is reached and confirms coverage
only when page totals stay stable and all session IDs are valid and unique. With
Ironbook's current legacy API, the report can confirm coverage only when fewer
than 100 sessions are returned; at 100 entries or when the payload is malformed,
it leaves coverage unconfirmed and requires an approved full-history export or a
source API that supports pagination.
For each exercise record, preflight prefers Ironbook's `standard_name` over
`raw_exercise_name`. Source-standardized names absent from the HealthOS catalog
are listed as catalog entries to create, with their source exercise body-part
counts. Missing or conflicting source classifications are explicitly marked for
manual review; HealthOS does not infer a replacement classification. Only
records without a usable source standard name and without a local match are
listed for manual name mapping. The report remains read-only and does not create
catalog entries or import training sessions. Each report also includes a
SHA-256 `source_fingerprint` over the expanded Ironbook session payload. A
future migration must fetch the source again and stop if its fingerprint differs
from the approved preflight report.

The migration engine is currently an explicit local function,
`migrate_ironbook_training_sessions`; it requires that approved fingerprint and
confirmed full-history metadata, validates all source IDs and standardized action
categories before writing, commits the full snapshot in one SQLite transaction,
and uses Ironbook source IDs for idempotent retries. The guarded Owner migration
route requires its feature flag and explicit confirmation; read-only mode
overrides it.
Successful migration transactions also write a `migration_runs` audit row with
the source fingerprint, backup path, completion time, and reconciled counts.
Ironbook exercise catalog IDs are retained on the corresponding HealthOS
exercise entries. Migration aborts atomically when a source ID, standardized
name, or source body-part classification conflicts with an existing catalog
entry. Local HealthOS training entry keeps its existing behavior when no source
exercise ID is supplied.

On 2026-10-27, a non-production drill fetched the complete paginated Ironbook
history (67 sessions, 1,086 records, 50 catalog exercises) into a disposable
SQLite database. Session, record, and catalog source-ID reconciliation reported
zero missing IDs, and a repeat import remained idempotent. The approved snapshot
fingerprint was `a61dfa795f089d3a49fe88f8d2cb46ad718d17fe0eae60030d4959bfb9bd3ca5`.
This was a temporary local drill only; it did not modify the preview database,
Ironbook, or the production HealthOS database.

The same read-only check can be repeated with the bundled audit command:

```powershell
python deploy/healthos/scripts/ironbook_audit.py --base-url http://127.0.0.1:18787
```

Pass `--drill-db <temporary-path>` only when a disposable local migration drill
is desired. The command never writes to Ironbook; do not point the drill path at
the production HealthOS database.

The production image includes `scripts/training.html` and
`scripts/training-ui.js` alongside the HealthOS service. Production uses a
single Docker container named `healthos`; Compose is not part of the release
path. Build and publish a release from the project root with:

```bash
deploy/healthos/scripts/publish_dockerhub.sh 0.9.5
```

The default repository is the public `docker.io/ziyaolin/healthos`; set
`HEALTHOS_IMAGE_REPOSITORY` only when publishing to a different repository.
The command pushes both the version tag and `latest`. Log in to Docker Hub
before publishing. On Yggdrasil, `scripts/deploy_yggdrasil.sh` pulls
`docker.io/ziyaolin/healthos:latest` by default. The script saves the active
container environment, stops and removes the old container, and starts exactly
one new `healthos` container. It checks
`/healthz`; if startup fails, it recreates the same container from the previous
image. Historical `healthos-backup-*` containers are removed automatically.

For a controlled rollback, set `HEALTHOS_IMAGE` to a version tag such as
`docker.io/ziyaolin/healthos:0.9.4`. The registry retains version tags while
the host removes the previous HealthOS image after a successful rollout. It
does not run a host-wide Docker prune. The rollout script does not enable
formal Ironbook migration.

The 2026-10-27 production migration imported the then-current complete Ironbook
history: 68 sessions, 1,103 records, and 50 exercises. The approved SHA-256
fingerprint was `dfb25ae9e49160f6f16280654e87149d167d127baa4daaf2e9dad3ff3da33a80`.
The transaction created migration audit row `1`; source ID sets matched for
sessions, records, and exercises, and `PRAGMA foreign_key_check` returned zero
errors. A full pre-migration SQLite backup passed `PRAGMA integrity_check` and is
retained at `/mnt/user/appdata/healthos/migration-backups/healthos-pre-migration-20261027T042739Z-06649f9b.sqlite3`.
Ironbook was read only during migration; no source records were modified or
deleted.

After reconciliation, the production container was restarted without
`HEALTHOS_IRONBOOK_BASE_URL`, so the Owner training UI now uses the local
HealthOS data. Release images are now tracked as `latest` plus ordinary
version tags; database migration backups remain under the mounted data
directory and are independent of image cleanup.

An Owner confirmation form is available only when
`HEALTHOS_IRONBOOK_MIGRATION_ENABLED=1` and Ironbook is not in read-only mode.
Submitting requires typing `确认迁移`; HealthOS refetches the entire source and
compares it with the approved fingerprint before opening the target transaction.
The migration switch remains off after the completed migration. Read-only mode
overrides the switch.

When `HEALTHOS_IRONBOOK_BASE_URL` is configured for a temporary read-only
preview, the Owner training page can show Ironbook history, individual details,
create/edit, confirmed deletion, and read-only training analytics from Ironbook's
`/api/stats` endpoint. HealthOS requests pages of 50 and accepts an optional
`format=page` response with total count and offset metadata. Against the current
legacy list response, HealthOS paginates locally through the latest 100 returned
sessions and warns when that API cap prevents access to older history. This UI
pagination does not remove the source API's 100-session limit, and migration
preflight still requires a full-history export or paginated source API when the
source contains 100 or more sessions. Analytics are fetched on the server and
rendered in HealthOS. During that preview mode, remote writes go to Ironbook and
no local copy is created. The current Ironbook API requires
weight, reps, and RPE for each ordinary exercise group, so HealthOS rejects
incomplete records and marks them required in the form. Action name suggestions
come from Ironbook's read-only API through the authenticated Owner session;
credentials stay server-side. Existing session date and body part are locked
while editing because Ironbook upserts on those fields plus `source_file`; editing
those identity fields could otherwise create a duplicate. New Ironbook-backed
training forms show the last recorded sets for a selected exercise as read-only
reference data; the proxy returns only date, weight, reps, RPE, and drop-set
segments, and never pre-fills the current session. Drop-set groups use Ironbook's
`extra_data.segments` format and require a weight and repetition count for every
segment; they do not require RPE. Use
`?source=local` to reach the separate HealthOS-local training tools.

The write-through adapter was verified against a local mock Ironbook API.
Production integration used Ironbook read-only for preflight and final migration;
training writes after the migration now target HealthOS local storage.

## Configuration

Generate an Owner password hash once:

```powershell
python deploy/healthos/scripts/healthos.py hash-password
```

Set the resulting value as `HEALTHOS_OWNER_PASSWORD_HASH`. The API token remains
required for machine ingestion and query endpoints. Set `HEALTHOS_COOKIE_SECURE=1`
when serving through HTTPS (the Compose default is `1`). Set
`HEALTHOS_IRONBOOK_READ_ONLY=1` for a local preview that can read Ironbook
history and analytics but hides and rejects remote training writes.

## Key routes

- `/login`, `/`, `/data`, `/add`, `/training`, `/training-demo`, `/training/migration-preflight`, `/shares`: Owner Console
- `/api/v1/health/context`: authenticated `HealthContext` projection
- `/api/v1/training/migration-preflight`: authenticated, read-only Ironbook mapping report
- `/share/<opaque-token>`: anonymous immutable snapshot access
- `/healthz`: liveness probe

Share tokens are generated with `secrets`, stored only as SHA-256 hashes, expire
after 5, 15, or 60 minutes, and default to five reads. Expired, revoked, and
exhausted snapshots return `410 Gone`.
