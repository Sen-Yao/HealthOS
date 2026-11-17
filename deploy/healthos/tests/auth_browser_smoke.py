"""Run isolated HTTPS/Chrome authentication regressions with Playwright CLI.

Requires Node/npx, Chrome and OpenSSL (Git for Windows includes OpenSSL).
No production credentials, databases, or connections are used.
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import urllib.parse
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "output/playwright/auth-update"
SCRIPT = ROOT / "deploy/healthos/scripts/healthos.py"
spec = importlib.util.spec_from_file_location("healthos_browser", SCRIPT)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
ready, release = threading.Event(), threading.Event()


class Handler(h.HealthOSHandler):
    def end_headers(self):
        if self.path == "/owner/api/session?hold=1":
            ready.set()
            if not release.wait(20):
                raise RuntimeError("Synthetic response gate timed out")
        super().end_headers()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/__auth_probe/source":
            target = f"https://127.0.0.1:{self.server.server_port}/training"
            body = (f"<a href='{target}'>Open HealthOS</a>"
                    f"<form method='post' action='{target}'><button>Try cross-site write</button></form>").encode()
            # This represents another site, whose CSP is not controlled by HealthOS.
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/__auth_probe/legacy":
            token, _ = h.create_session(self.server.db_path)
            cookie = self.session_cookie(token, name=h.LEGACY_SESSION_COOKIE, max_age=h.SESSION_TTL_SECONDS)
            self.send_html(200, "Synthetic legacy cookie installed", headers=[("Set-Cookie", cookie.replace("SameSite=Lax", "SameSite=Strict"))])
        elif path == "/__auth_probe/status":
            self.send_json(200, {"ready": ready.is_set()})
        elif path == "/__auth_probe/reset":
            ready.clear()
            release.clear()
            self.send_json(200, {"reset": True})
        elif path == "/__auth_probe/release":
            release.set()
            self.send_json(200, {"released": True})
        else:
            super().do_GET()


def main(scenario_file=None, output_dir=None):
    out = Path(output_dir) if output_dir is not None else OUT
    npx = shutil.which("npx.cmd" if os.name == "nt" else "npx")
    openssl = shutil.which("openssl")
    if not openssl and os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe"
        if candidate.is_file():
            openssl = str(candidate)
    if not npx or not openssl:
        raise RuntimeError("Browser smoke requires npx, Chrome and OpenSSL; see LOCAL_RUN.md")
    discovery = subprocess.run([npx, "--yes", "--package", "@playwright/cli", "--call",
                                "where.exe playwright-cli" if os.name == "nt" else "command -v playwright-cli"],
                               capture_output=True, text=True, check=True, timeout=90)
    executable = Path(discovery.stdout.strip().splitlines()[0])
    cli_script = executable.parent.parent / "@playwright/cli/playwright-cli.js" if os.name == "nt" else executable.resolve()
    # Invoke Node directly: cmd.exe's 8191-character limit cannot carry the browser scenarios.
    cli_prefix = [shutil.which("node"), str(cli_script)]
    out.mkdir(parents=True, exist_ok=True)
    session_name = "healthos-auth-smoke-" + str(os.getpid())
    environment = {"HEALTHOS_OWNER_PASSWORD_HASH": h.hash_password("synthetic-probe-password"),
                   "HEALTHOS_COOKIE_SECURE": "1", "HEALTHOS_IRONBOOK_BASE_URL": "",
                   "HEALTHOS_API_TOKEN": "", "HEALTHOS_API_TOKEN_FILE": ""}
    with tempfile.TemporaryDirectory(prefix="healthos-browser-") as directory, mock.patch.dict(os.environ, environment):
        temp = Path(directory)
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(temp / "key.pem"),
                        "-out", str(temp / "cert.pem"), "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
        db = temp / "synthetic.sqlite3"
        conn = h.init_db(db)
        h.create_training_session(conn, {"request_id": "synthetic-browser-seed", "session_date": "2026-11-01",
            "body_part": "胸", "records": [{"name": "Synthetic press", "weight_kg": 40, "reps": [8], "rpe": [7]},
                                        {"name": "Synthetic fly", "weight_kg": 15, "reps": [12], "rpe": [7]}]})
        h.add_training_exercise_alias(conn, 1, "Bench alias")
        conn.close()
        server = h.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.db_path = str(db)
        server.raw_dir = temp / "raw"
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(temp / "cert.pem", temp / "key.pem")
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        config = temp / "chrome.json"
        config.write_text(json.dumps({"browser": {"browserName": "chromium", "isolated": True,
            "launchOptions": {"channel": "chrome"}, "contextOptions": {"ignoreHTTPSErrors": True, "hasTouch": scenario_file is not None}},
            "outputDir": str(out), "outputMode": "file"}), encoding="utf-8")
        base = f"https://127.0.0.1:{server.server_port}"

        def cli(*args):
            result = subprocess.run([*cli_prefix, "-s=" + session_name, *args], cwd=ROOT, capture_output=True,
                                    encoding="utf-8", errors="replace", timeout=90)
            with (out / "cli.log").open("a", encoding="utf-8") as log:
                log.write(result.stdout + result.stderr)
            if result.returncode or "### Error" in result.stdout:
                raise RuntimeError(f"Playwright CLI failed; inspect {out / 'cli.log'}\n" + (result.stdout + result.stderr)[-1800:])
            return result.stdout

        with (out / "server.log").open("w", encoding="utf-8") as log, contextlib.redirect_stderr(log):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # Separate process verifies the listener before driving the browser.
                probe = subprocess.run([shutil.which("curl.exe") or shutil.which("curl"), "-k", "-fsS", base + "/healthz"],
                                       capture_output=True, text=True, check=True)
                if not json.loads(probe.stdout)["ok"]:
                    raise RuntimeError("Local HTTPS health probe failed")
                cli("open", base + "/login", "--browser", "chrome", "--config", str(config))
                cli("snapshot")
                code = Path(scenario_file or Path(__file__).with_name("auth_browser_smoke.js")).read_text(encoding="utf-8")
                code = code.replace("__BASE_URL__", base).replace("__SOURCE_URL__", f"https://localhost:{server.server_port}/__auth_probe/source")
                code = code.replace("__OUTPUT_DIR__", out.as_posix())
                batches = (1280, 390) if "__VIEWPORTS__" in code else (None,)
                result = None
                for viewport in batches:
                    scenario = code.replace("__VIEWPORTS__", json.dumps([viewport]))
                    output = cli("run-code", scenario)
                    match = re.search(r"### Result\s*\n([^\n]+)", output)
                    if not match:
                        raise RuntimeError("Browser assertions did not produce a result")
                    batch = json.loads(match.group(1))
                    if result is None:
                        result = batch
                    else:
                        result["passed"] += batch["passed"]
                        result["scenarios"].extend(batch["scenarios"])
                (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                release.set()
                try:
                    cli("close")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(5)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
