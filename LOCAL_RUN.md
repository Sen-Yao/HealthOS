# Local HealthOS Run

Purpose: isolated Owner Console development instance. This run uses an empty local SQLite database and does not connect to Ironbook.

Prerequisites: Windows PowerShell and the project `.venv` Python environment.

Start from the project root:

```powershell
$env:HEALTHOS_API_TOKEN = 'local-' + [guid]::NewGuid().ToString('N')
$ownerHash = & .venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'deploy/healthos/scripts'); import healthos; print(healthos.hash_password('local-healthos-owner'))"
$env:HEALTHOS_OWNER_PASSWORD_HASH = $ownerHash
Remove-Item Env:HEALTHOS_IRONBOOK_BASE_URL -ErrorAction SilentlyContinue
.venv\Scripts\python.exe deploy\healthos\scripts\healthos.py serve --db .local-dev\healthos.sqlite3 --raw-dir .local-dev\raw --host 127.0.0.1 --port 8791
```

Open `http://127.0.0.1:8791`. Owner password: `local-healthos-owner` (local development only).

Verify from another PowerShell window:

```powershell
(Invoke-WebRequest http://127.0.0.1:8791/healthz).StatusCode
(Invoke-WebRequest http://127.0.0.1:8791/login).StatusCode
```

Expected status: `200` for both requests. The local database and raw files live under `.local-dev/`. No production database, Ironbook connection, or Apple Health import is enabled by this run. Stop the server with `Ctrl+C`.

If port `8791` is already occupied, run a separate source-current instance on
`8792` with its own empty database:

```powershell
$env:HEALTHOS_API_TOKEN = 'local-' + [guid]::NewGuid().ToString('N')
$ownerHash = & .venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'deploy/healthos/scripts'); import healthos; print(healthos.hash_password('local-healthos-owner'))"
$env:HEALTHOS_OWNER_PASSWORD_HASH = $ownerHash
Remove-Item Env:HEALTHOS_IRONBOOK_BASE_URL -ErrorAction SilentlyContinue
.venv\Scripts\python.exe deploy\healthos\scripts\healthos.py serve --db .local-dev\healthos-dev.sqlite3 --raw-dir .local-dev\healthos-dev-raw --host 127.0.0.1 --port 8792
```

Open `http://127.0.0.1:8792`; verify `/healthz` and `/login` each return `200`.

Last verified: 2026-10-26, Windows local development environment.

## Ironbook Read-Only Integration Preview

This separate instance exercises the HealthOS Ironbook history, analytics,
migration preflight, and training form integration. It uses a dedicated empty
HealthOS database and reads Ironbook through the existing SSH tunnel at
`127.0.0.1:18787`. Remote training writes are disabled by the application.

Start from the project root in PowerShell:

```powershell
$env:HEALTHOS_API_TOKEN = 'local-' + [guid]::NewGuid().ToString('N')
$ownerHash = & .venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'deploy/healthos/scripts'); import healthos; print(healthos.hash_password('local-healthos-owner'))"
$env:HEALTHOS_OWNER_PASSWORD_HASH = $ownerHash
$env:HEALTHOS_IRONBOOK_BASE_URL = 'http://127.0.0.1:18787'
$env:HEALTHOS_IRONBOOK_READ_ONLY = '1'
# Formal migration stays disabled in the read-only preview.
.venv\Scripts\python.exe deploy/healthos/scripts/healthos.py serve --db .local-dev/healthos-ironbook-paging-preview.sqlite3 --raw-dir .local-dev/healthos-ironbook-paging-preview-raw --host 127.0.0.1 --port 8794
```

Open `http://127.0.0.1:8794`; local preview owner password is
`local-healthos-owner`. Verify `/healthz`, then sign in and open
`/training?source=ironbook` and `/training/migration-preflight`. The SSH tunnel
must be active. Stop this preview with `Ctrl+C` in its terminal; do not stop the
tunnel or other HealthOS instances.

Last verified: 2026-10-26, Windows local development environment.
