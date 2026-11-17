"""Read-only production acceptance; credentials remain in process memory.

The CLI accepts a JSON object on protected stdin with base_url, username,
password and expected_js_sha256. It prints only sanitized check results.
expected_js_sha256 is the canonical committed UTF-8/LF blob digest, matching
the server's text response rather than a CRLF checkout's raw file digest.
Only its own sign-in/session rows are created/revoked; health records are not written.
"""
from http.cookies import SimpleCookie
import hashlib
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


class AcceptanceError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class AcceptanceClient:
    def __init__(self, base_url, username, password):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise AcceptanceError("HTTPS_ORIGIN_REQUIRED")
        self.base = base_url.rstrip("/")
        self.username, self.password = username, password
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies), NoRedirect())
        self.csrf = None

    def request(self, path, fields=None):
        body = urllib.parse.urlencode(fields).encode() if fields is not None else None
        request = urllib.request.Request(self.base + path, data=body, headers={
            "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "HealthOS-release-acceptance/0.10.0",
        })
        try:
            response = self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError):
            raise AcceptanceError("HTTPS_REQUEST_FAILED") from None
        with response:
            return response.status, response.headers, response.read()

    def session(self):
        status, headers, body = self.request("/owner/api/session")
        if status != 200:
            raise AcceptanceError("SESSION_NOT_AUTHENTICATED")
        data = json.loads(body)
        if not isinstance(data.get("csrf"), str) or not data["csrf"]:
            raise AcceptanceError("SESSION_CSRF_MISSING")
        self.csrf = data["csrf"]
        return headers

    def login(self):
        status, headers, body = self.request("/login", {"username": self.username, "password": self.password})
        if status != 200:
            raise AcceptanceError("LOGIN_REJECTED")
        self.csrf = json.loads(body).get("csrf")
        if not isinstance(self.csrf, str) or not self.csrf:
            raise AcceptanceError("LOGIN_CSRF_MISSING")
        return headers

    def logout(self):
        if not self.csrf:
            return
        status, _, _ = self.request("/logout", {"csrf": self.csrf})
        if status != 303:
            raise AcceptanceError("LOGOUT_FAILED")
        self.csrf = None

    def verify(self, expected_js_sha256, expect_legacy=False):
        checks = []
        if expect_legacy:
            headers = self.session()
            legacy = SimpleCookie()
            for value in headers.get_all("Set-Cookie", []):
                legacy.load(value)
            if "healthos_session" not in legacy or legacy["healthos_session"]["samesite"].lower() != "lax":
                raise AcceptanceError("LEGACY_SESSION_DID_NOT_SURVIVE_OR_RENEW")
            checks.append("pre-deployment legacy session survived container replacement and renewed as Lax")
        headers = self.login()
        values = SimpleCookie()
        for value in headers.get_all("Set-Cookie", []):
            values.load(value)
        current = values.get("healthos_session_v2")
        if not current or not current["secure"] or not current["httponly"] or current["samesite"].lower() != "lax":
            raise AcceptanceError("V2_COOKIE_ATTRIBUTES_INVALID")
        if not 30 * 86400 - 60 <= int(current["max-age"]) <= 30 * 86400:
            raise AcceptanceError("V2_COOKIE_LIFETIME_INVALID")
        checks.append("real credential login issued Secure HttpOnly Lax v2 cookie with fixed thirty-day deadline")
        if self.session().get_all("Set-Cookie"):
            raise AcceptanceError("ORDINARY_RESPONSE_REWRITES_V2_COOKIE")
        checks.append("ordinary authenticated session response does not rewrite cookies")
        status, _, body = self.request("/training")
        if status != 200 or any(marker not in body for marker in (b'id="setMenu"', b'id="saveFeedback"', b'id="decisionSaveFeedback"')):
            raise AcceptanceError("TRAINING_PAGE_RELEASE_MARKERS_MISSING")
        checks.append("authenticated production training page serves group menu and unified save feedback")
        status, _, body = self.request("/training-ui.js")
        if status != 200 or hashlib.sha256(body).hexdigest() != expected_js_sha256:
            raise AcceptanceError("TRAINING_ASSET_HASH_MISMATCH")
        checks.append("public authenticated JavaScript exactly matches committed release source")
        status, _, _ = self.request("/training", {"csrf": "release-acceptance-invalid-csrf"})
        if status != 403:
            raise AcceptanceError("INVALID_CSRF_NOT_REJECTED")
        checks.append("invalid CSRF is rejected before any training write")
        status, _, body = self.request("/owner/api/training/sessions?limit=1")
        data = json.loads(body) if status == 200 else {}
        if status != 200 or not isinstance(data.get("items"), list) or not isinstance(data.get("total"), int):
            raise AcceptanceError("TRAINING_READ_FAILED")
        checks.append("existing production training history is readable; contents are not exported")
        self.logout()
        if self.request("/owner/api/session")[0] != 401:
            raise AcceptanceError("LOGGED_OUT_SESSION_STILL_VALID")
        checks.append("acceptance session explicitly logged out and now returns 401")
        return {"checks": checks, "health_record_writes": 0, "credentials_exported": False}


if __name__ == "__main__":
    client = None
    try:
        config = json.load(sys.stdin)
        client = AcceptanceClient(config["base_url"], config["username"], config["password"])
        result = client.verify(config["expected_js_sha256"])
        print(json.dumps(result, indent=2))
    except AcceptanceError as error:
        print(json.dumps({"error": str(error)}))
        raise SystemExit(1)
    except Exception:
        print(json.dumps({"error": "ACCEPTANCE_FAILED"}))
        raise SystemExit(1)
    finally:
        if client and client.csrf:
            try:
                client.logout()
            except Exception:
                pass
