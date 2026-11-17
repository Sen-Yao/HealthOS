# Local HealthOS Run

## Training entry regression checks (2026-11-17)

Run the complete training interaction check from the project root:

```powershell
.venv\Scripts\python.exe deploy/healthos/tests/training_browser_smoke.py
```

It reuses the isolated HTTPS/Chrome harness below with a synthetic exercise
catalog and alias. Desktop and 390px mobile scenarios run as separate batches;
the mobile scenarios exercise touch taps as well as viewport layout. Tests cover
group menus/deletion/undo, last-group removal, weight inheritance, canonical and
alias suggestion filtering (including late responses), local validation, matching
footer/dialog feedback, HTTP/authentication/network failures, invalid save
acknowledgements, idempotent retries, retained edits after uncertain submissions,
and warnings after acknowledged saves. No production data or service is used.

Results, sanitized server logs, and desktop/mobile screenshots are written to
`output/playwright/training-update/`. Browser profiles, certificates, databases
and the ephemeral listener are disposed automatically. Run the authentication
check and full Python suite below to verify their existing behavior as well.

## Authentication regression checks (2026-11-17)

These checks use synthetic accounts and disposable databases. They do not connect
to production or Ironbook, publish images, or modify existing local services.
Run from the project root in PowerShell:

```powershell
.venv\Scripts\python.exe -m unittest discover -s deploy/healthos/tests -p "test_*.py" -v
.venv\Scripts\python.exe deploy/healthos/tests/auth_browser_smoke.py
.venv\Scripts\python.exe deploy/healthos/tests/auth_docker_smoke.py
```

The browser check requires Node/npx, installed Google Chrome, and OpenSSL. On
Windows it also discovers the OpenSSL executable included with Git for Windows.
It resolves Playwright CLI through npx, then invokes Node directly to avoid the
Windows cmd.exe command-length limit. No project npm dependency is installed.
The check serves HTTPS on an ephemeral 127.0.0.1 port with a temporary certificate,
probes `/healthz` from a separate process, and opens an isolated Chrome profile.
The localhost source represents another site and permits a cross-site test form;
this fixture policy is never included in production. Browser and listener close
automatically, including on assertion failures.

Browser results and structured server logs are saved under
`output/playwright/auth-update/`. Expected: every reported scenario passes,
including Lax navigation, old Strict-cookie recovery, response-order races,
cross-tab sign-in, draft retention, CSRF preservation and rejected cross-site POST.

The Docker check requires the existing Docker Desktop engine. It discovers the
CLI under PATH or the local Docker Desktop installation, supplies a synthetic API
token required by the normal serve entrypoint, builds a uniquely tagged image,
and runs a uniquely named container bound only to an ephemeral localhost port.
It verifies sign-in, v2 attributes, CSRF rejection, logout, and session persistence
after container recreation using the same disposable mounted database. It removes
its container and image tag on completion; it does not prune other images.
Results are saved under `output/auth-update-20261117/`.

The server retains seven-day idle and thirty-day absolute expiry. New cookies
use `healthos_session_v2`, SameSite=Lax, and a fixed browser deadline; only legacy
cookies receive response-based renewal. Existing sessions need not reauthenticate
on upgrade. Older release images do not understand v2 cookies, so rollback may
require a new sign-in. See the deployment README for diagnostic log fields.

Last verified: 2026-11-17, Windows, isolated Chrome and Docker Desktop.

## Release regression checks

Run from the project root with Python 3.12, Node/npx, Chrome, OpenSSL and
Docker Desktop available. On Windows, the release rollback test also requires
Git for Windows Bash. The browser harness locates Git's bundled OpenSSL and
creates disposable TLS certificates and synthetic databases automatically.

```powershell
python -m unittest discover -s deploy/healthos/tests -p 'test_*.py' -v
python deploy/healthos/tests/auth_browser_smoke.py
python deploy/healthos/tests/training_browser_smoke.py
python deploy/healthos/tests/auth_docker_smoke.py
```

Version 0.10.0 passed 53 Python tests, 13 authentication browser scenarios,
18 desktop/mobile training scenarios and all five Docker smoke groups.
These checks do not use production credentials or production databases.
Logs, JSON results and screenshots are written below ignored `output/`.

The separate `deploy/healthos/scripts/release_acceptance.py` checks an already
deployed HTTPS origin with an authorized credential supplied through protected
stdin. It prints sanitized results and creates/revokes only its own authentication
session; it does not save health records. Its `expected_js_sha256` must identify
the UTF-8 JavaScript served over HTTP. Compare the committed Git blob, since
Python's text response normalizes Windows CRLF working-tree files to LF:

```powershell
python -c "import hashlib,subprocess; print(hashlib.sha256(subprocess.check_output(['git','show','v0.10.0:deploy/healthos/scripts/training-ui.js'])).hexdigest())"
```

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

Last verified: 2026-11-12, Windows local development environment.

## Isolated Browser Use workaround (2026-11-09)

For frontend-only checks without real credentials or business data, run
`.venv\Scripts\python.exe output/healthos-ui-workaround/server.py` from the project
root. This local test artifact serves `http://127.0.0.1:18793/training` using
repository assets and 53 synthetic records, with all business writes disabled.
Verify `/healthz` from a separate shell. The optional `/training-confirm-fixture`
route explicitly stubs only the discard confirmation for a disposable draft;
it does not verify the native browser confirmation flow. No production service
or authentication configuration is changed. Stop with Ctrl+C.

Results and limitations: `output/healthos-ui-workaround/RESULTS.md`.
Verified listener and HTTP response on Windows, 2026-11-09. Test artifacts under
`output/` are local and must be retained separately if sharing this runbook.

## Docker local smoke run

Prerequisites: Docker Desktop with the WSL 2 engine running. The Dockerfile is
built from the project root so its `COPY` paths match the release build.

From the project root in PowerShell:

```powershell
$dockerBin = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin"
$env:Path = "$dockerBin;$env:Path"
$docker = "$dockerBin\docker.exe"
New-Item -ItemType Directory -Force .local-dev\docker\raw | Out-Null
$ownerHash = (& .venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'deploy/healthos/scripts'); import healthos; print(healthos.hash_password('local-healthos-owner'))").Trim()
& $docker rm -f healthos-local 2>$null | Out-Null
& $docker build --file deploy\healthos\Dockerfile --tag healthos:local .
& $docker run --detach --name healthos-local --publish 8791:8791 `
  --env "HEALTHOS_API_TOKEN=local-container-$([guid]::NewGuid().ToString('N'))" `
  --env "HEALTHOS_OWNER_PASSWORD_HASH=$ownerHash" `
  --volume "${PWD}\.local-dev\docker:/data" `
  healthos:local serve --db /data/healthos.sqlite3 --raw-dir /data/raw --host 0.0.0.0 --port 8791
```

Verify from another PowerShell window:

```powershell
(Invoke-WebRequest http://127.0.0.1:8791/healthz).StatusCode
(Invoke-WebRequest http://127.0.0.1:8791/login).StatusCode
& $docker logs healthos-local
```

Expected status: `200` for both requests. Stop the smoke container with
`& $docker rm -f healthos-local`. The database and raw files remain under
`.local-dev\docker\`, which is ignored by Git and excluded from Docker build
contexts.

Troubleshooting: if Docker CLI is not on `PATH`, use
`$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin` for both `docker.exe`
and `docker-credential-desktop.exe` before building. Docker Desktop must be
running with the WSL 2 engine enabled.

Last verified: 2026-11-12. The local Python service and Docker image both passed `/healthz` and `/login` checks; the Docker smoke container was removed after verification.

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
