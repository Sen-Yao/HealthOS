"""Build and verify a disposable local container; never publish or touch production."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "output/auth-update-20261117"
spec = importlib.util.spec_from_file_location("healthos_docker", ROOT / "deploy/healthos/scripts/healthos.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def main():
    docker = shutil.which("docker")
    if not docker:
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DockerDesktop/resources/bin/docker.exe"
        if candidate.is_file():
            docker = str(candidate)
    if not docker:
        raise RuntimeError("Docker CLI is required; see LOCAL_RUN.md")
    env = dict(os.environ)
    env["PATH"] = str(Path(docker).parent) + os.pathsep + env.get("PATH", "")
    name = "healthos-auth-smoke-" + str(os.getpid())
    image = "healthos:auth-smoke-" + str(os.getpid())
    OUT.mkdir(parents=True, exist_ok=True)
    owner_hash = h.hash_password("synthetic-docker-password")
    report = {"production_connections": False, "image_published": False, "checks": []}

    def run(*args, check=True):
        result = subprocess.run([docker, *args], cwd=ROOT, env=env, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=180)
        with (OUT / "docker-build.log").open("a", encoding="utf-8") as log:
            log.write(result.stdout + result.stderr)
        if check and result.returncode:
            raise RuntimeError("Docker smoke failed; inspect output/auth-update-20261117/docker-build.log")
        return result.stdout.strip()

    def request(port, method, path, body=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, response.headers, response.read().decode()
        finally:
            connection.close()

    with tempfile.TemporaryDirectory(prefix="healthos-docker-") as directory:
        data = Path(directory)
        (data / "raw").mkdir()

        def start():
            run("run", "--detach", "--name", name, "--publish", "127.0.0.1::8791",
                "--env", "HEALTHOS_OWNER_PASSWORD_HASH=" + owner_hash, "--env", "HEALTHOS_COOKIE_SECURE=1",
                "--env", "HEALTHOS_API_TOKEN=synthetic-local-smoke-token",
                "--volume", str(data) + ":/data", image, "serve", "--db", "/data/healthos.sqlite3",
                "--raw-dir", "/data/raw", "--host", "0.0.0.0", "--port", "8791")
            ports = json.loads(run("inspect", name, "--format", "{{json .NetworkSettings.Ports}}"))["8791/tcp"]
            assert ports[0]["HostIp"] == "127.0.0.1"
            port = int(ports[0]["HostPort"])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if request(port, "GET", "/healthz")[0] == 200:
                        return port
                except OSError:
                    pass
                if run("inspect", name, "--format", "{{.State.Running}}") != "true":
                    raise RuntimeError("Disposable Docker service exited; inspect docker-build.log")
                time.sleep(.2)
            raise RuntimeError("Disposable Docker service did not become healthy")

        try:
            run("build", "--file", "deploy/healthos/Dockerfile", "--tag", image, ".")
            report["checks"].append("local image build")
            port = start()
            assert request(port, "GET", "/login")[0] == 200
            status, headers, body = request(port, "POST", "/login", urllib.parse.urlencode({"password": "synthetic-docker-password"}))
            assert status == 200
            cookie_header = next(value for value in headers.get_all("Set-Cookie") if value.startswith("healthos_session_v2="))
            assert all(value in cookie_header for value in ("SameSite=Lax", "Secure", "HttpOnly"))
            cookie = cookie_header.split(";", 1)[0]
            session = json.loads(body)
            status, headers, _ = request(port, "GET", "/owner/api/session", cookie=cookie)
            assert status == 200 and not headers.get_all("Set-Cookie")
            report["checks"].append("health, login, v2 attributes and ordinary response without Set-Cookie")
            assert request(port, "POST", "/training", "csrf=wrong", cookie=cookie)[0] == 403
            report["checks"].append("CSRF rejection")
            run("rm", "--force", name)
            port = start()
            assert request(port, "GET", "/owner/api/session", cookie=cookie)[0] == 200
            report["checks"].append("session survives container recreation with mounted synthetic database")
            status, headers, _ = request(port, "POST", "/logout", urllib.parse.urlencode({"csrf": session["csrf"]}), cookie=cookie)
            assert status == 303 and len(headers.get_all("Set-Cookie")) == 2
            assert request(port, "GET", "/owner/api/session", cookie=cookie)[0] == 401
            report["checks"].append("logout revokes session and clears both cookies")
            run("logs", name)
        except Exception:
            run("logs", name, check=False)
            raise
        finally:
            run("rm", "--force", name, check=False)
            run("image", "rm", image, check=False)
    report["cleaned_up"] = True
    (OUT / "docker-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
