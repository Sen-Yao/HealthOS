"""HTTP and persistence regressions for training authentication."""

import http.client
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse


SCRIPT = Path(__file__).parents[1] / "scripts" / "healthos.py"
spec = importlib.util.spec_from_file_location("healthos_sessions", SCRIPT)
healthos = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthos)


class QuietHandler(healthos.HealthOSHandler):
    def log_message(self, *_args):
        pass


class OwnerSessionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "healthos.sqlite3"
        self.environment = mock.patch.dict(os.environ, {
            "HEALTHOS_OWNER_PASSWORD_HASH": healthos.hash_password("synthetic test password"),
            "HEALTHOS_COOKIE_SECURE": "1",
        })
        self.environment.start()
        healthos.init_db(self.db).close()
        self.server = healthos.http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.server.db_path = str(self.db)
        self.server.raw_dir = Path(self.directory.name) / "raw"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.environment.stop()
        self.directory.cleanup()

    def http(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result_headers = dict(response.getheaders())
            result_headers["_cookies"] = response.headers.get_all("Set-Cookie", [])
            return response.status, result_headers, response.read().decode("utf-8")
        finally:
            connection.close()

    def test_expired_owner_requests_return_json_without_redirect(self):
        for path in ("/training", "/training/delete", "/training/aliases", "/logout"):
            status, headers, body = self.http("POST", path, "csrf=old", {
                "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": "healthos_session=expired",
            })
            self.assertEqual(status, 401, path)
            self.assertNotIn("Location", headers)
            self.assertEqual(json.loads(body), {"error": "owner_auth_required"})
        self.assertEqual(self.http("GET", "/training")[0], 303)
        self.assertEqual(self.http("GET", "/owner/api/session")[0], 401)

    def test_login_autofill_and_json_reauthentication(self):
        _, _, page = self.http("GET", "/login")
        self.assertEqual(page.count("<form"), 1)
        self.assertIn("autocomplete='username'", page)
        self.assertIn("autocomplete='current-password'", page)
        self.assertIn("value='Senyao'", page)
        json_headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
        for username in ("wrong", ""):
            status, _, body = self.http("POST", "/login", urllib.parse.urlencode({
                "username": username, "password": "synthetic test password",
            }), json_headers)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["error"], "invalid_credentials")
        status, headers, body = self.http("POST", "/login", urllib.parse.urlencode({
            "username": "Senyao", "password": "synthetic test password",
        }), json_headers)
        self.assertEqual(status, 200)
        session = json.loads(body)
        self.assertTrue(session["csrf"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertTrue(cookie.startswith("healthos_session_v2="))
        self.assertIn("HttpOnly; SameSite=Lax", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        status, headers, body = self.http("GET", "/owner/api/session", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf"], session["csrf"])
        self.assertNotIn("Set-Cookie", headers)
        status, new_headers, body = self.http("POST", "/login", urllib.parse.urlencode({
            "username": "Senyao", "password": "synthetic test password",
        }), {**json_headers, "Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertNotEqual(json.loads(body)["csrf"], session["csrf"])
        self.assertIsNone(healthos.session_data(cookie.split("=", 1)[1], self.db))
        new_cookie = new_headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.http("POST", "/training", "csrf=" + session["csrf"], {
            **json_headers, "Cookie": new_cookie,
        })[0], 403)
        self.assertEqual(self.http("POST", "/logout", "csrf=" + json.loads(body)["csrf"], {
            **json_headers, "Cookie": new_cookie,
        })[0], 303)
        self.assertEqual(self.http("GET", "/owner/api/session", headers={"Cookie": new_cookie})[0], 401)
        self.assertEqual(self.http("POST", "/login", urllib.parse.urlencode({
            "password": "synthetic test password",
        }), {"Content-Type": "application/x-www-form-urlencoded"})[0], 303)

    def test_session_sliding_expiration_absolute_limit_and_cookie_agree(self):
        start = 1000000
        with mock.patch.object(healthos.time, "time", return_value=start):
            token, _ = healthos.create_session(self.db)
        with mock.patch.object(healthos.time, "time", return_value=start + 6 * 86400):
            session = healthos.session_data(token, self.db)
            self.assertEqual(session["expires_at"], start + 13 * 86400)
        for day in (12, 18, 24, 29):
            with mock.patch.object(healthos.time, "time", return_value=start + day * 86400):
                session = healthos.session_data(token, self.db)
                self.assertEqual(session["expires_at"], min(start + (day + 7) * 86400, start + 30 * 86400))
        with mock.patch.object(healthos.time, "time", return_value=start + 29 * 86400):
            status, headers, _ = self.http("GET", "/owner/api/session", headers={"Cookie": f"healthos_session={token}"})
            self.assertEqual(status, 200)
            self.assertIn("Max-Age=86400", headers["Set-Cookie"])
        with mock.patch.object(healthos.time, "time", return_value=start + 30 * 86400):
            self.assertIsNone(healthos.session_data(token, self.db))
        with mock.patch.object(healthos.time, "time", return_value=start):
            idle_token, _ = healthos.create_session(self.db)
        with mock.patch.object(healthos.time, "time", return_value=start + 7 * 86400):
            self.assertIsNone(healthos.session_data(idle_token, self.db))

    def test_session_survives_new_process_and_stores_only_token_hash(self):
        token, csrf = healthos.create_session(self.db)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT token_hash,csrf FROM owner_sessions").fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (healthos.sha256_text(token), csrf))
        code = (
            "import importlib.util,sys,json; "
            "s=importlib.util.spec_from_file_location('h',sys.argv[1]); "
            "h=importlib.util.module_from_spec(s);s.loader.exec_module(h); "
            "print(json.dumps(h.session_data(sys.argv[3],sys.argv[2])))"
        )
        output = subprocess.check_output([sys.executable, "-c", code, str(SCRIPT), str(self.db), token], text=True)
        self.assertEqual(json.loads(output)["csrf"], csrf)
        with mock.patch.dict(os.environ, {"HEALTHOS_OWNER_PASSWORD_HASH": healthos.hash_password("changed")}):
            self.assertIsNone(healthos.session_data(token, self.db))
        self.assertIsNone(healthos.session_data(token, self.db))

    def test_migration_is_additive_and_repeatable(self):
        healthos.init_db(self.db).close()
        healthos.init_db(self.db).close()
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version='0.6.0-owner-sessions'").fetchone()[0], 1)
        finally:
            conn.close()

    def login(self, cookie=None):
        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        status, response, body = self.http("POST", "/login", urllib.parse.urlencode({
            "username": "Senyao", "password": "synthetic test password",
        }), headers)
        self.assertEqual(status, 200)
        return response["Set-Cookie"].split(";", 1)[0], json.loads(body), response

    def test_v2_cookie_absolute_lifetime_and_no_response_renewal(self):
        cookie, session, headers = self.login()
        max_age = int(headers["Set-Cookie"].split("Max-Age=", 1)[1].split(";", 1)[0])
        self.assertGreaterEqual(max_age, 30 * 86400 - 2)
        self.assertLessEqual(max_age, 30 * 86400)
        self.assertIn("healthos_session=;", headers["_cookies"][0])
        start = float(session["created_at"])
        for day in (6, 12, 18, 24, 29):
            with mock.patch.object(healthos.time, "time", return_value=start + day * 86400):
                status, headers, body = self.http("GET", "/owner/api/session", headers={"Cookie": cookie})
            self.assertEqual(status, 200)
            self.assertNotIn("Set-Cookie", headers)
            self.assertEqual(json.loads(body)["absolute_expires_at"], session["absolute_expires_at"])
        with mock.patch.object(healthos.time, "time", return_value=start + 30 * 86400):
            status, headers, _ = self.http("GET", "/owner/api/session", headers={"Cookie": cookie})
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)

    def test_v2_invalid_cookie_never_falls_back_to_valid_legacy(self):
        token, _ = healthos.create_session(self.db)
        for invalid in ("missing", "", "malformed!"):
            status, headers, _ = self.http("GET", "/owner/api/session", headers={
                "Cookie": f"healthos_session={token}; healthos_session_v2={invalid}",
            })
            self.assertEqual(status, 401)
            self.assertNotIn("Set-Cookie", headers)
        self.assertIsNotNone(healthos.session_data(token, self.db))

    def test_legacy_renewal_uses_lax_without_promoting_cookie(self):
        token, _ = healthos.create_session(self.db)
        status, headers, _ = self.http("GET", "/owner/api/session", headers={"Cookie": f"healthos_session={token}"})
        self.assertEqual(status, 200)
        self.assertEqual(len(headers["_cookies"]), 1)
        self.assertTrue(headers["Set-Cookie"].startswith("healthos_session="))
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])
        self.assertNotIn("healthos_session_v2", headers["Set-Cookie"])

    def test_login_rotation_is_atomic_when_revocation_fails(self):
        token, _ = healthos.create_session(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TRIGGER fail_revoke BEFORE DELETE ON owner_sessions BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            healthos.create_session(self.db, replace_tokens=(token,))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM owner_sessions").fetchone()[0], 1)
        conn.close()
        self.assertIsNotNone(healthos.session_data(token, self.db))

    def test_logout_revokes_and_clears_both_cookie_versions(self):
        legacy, _ = healthos.create_session(self.db)
        current, session, _ = self.login()
        status, headers, _ = self.http("POST", "/logout", "csrf=" + session["csrf"], {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": f"healthos_session={legacy}; {current}",
        })
        self.assertEqual(status, 303)
        self.assertEqual(len(headers["_cookies"]), 2)
        self.assertTrue(all("Max-Age=0" in value for value in headers["_cookies"]))
        for token in (legacy, current.split("=", 1)[1]):
            self.assertIsNone(healthos.session_data(token, self.db))

    def test_parallel_requests_and_duplicate_logins_remain_valid(self):
        cookie, _, _ = self.login()
        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(lambda _: self.http("GET", "/owner/api/session", headers={"Cookie": cookie})[0], range(36)))
        self.assertEqual(statuses, [200] * 36)
        first, _, _ = self.login()
        second, _, _ = self.login()
        for value in (first, second):
            self.assertEqual(self.http("GET", "/owner/api/session", headers={"Cookie": value})[0], 200)

    def test_delayed_response_cannot_replace_login_or_undo_logout(self):
        for legacy in (False, True):
            for logout in (False, True):
                with self.subTest(legacy=legacy, logout=logout):
                    ready, release = threading.Event(), threading.Event()

                    class Delayed(QuietHandler):
                        def end_headers(self):
                            if self.path == "/owner/api/session?hold=1":
                                ready.set()
                                release.wait(5)
                            super().end_headers()

                    self.server.RequestHandlerClass = Delayed
                    if legacy:
                        token, _ = healthos.create_session(self.db)
                        original = "healthos_session=" + token
                        csrf = healthos.session_data(token, self.db)["csrf"]
                    else:
                        original, session, _ = self.login()
                        csrf = session["csrf"]
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(self.http, "GET", "/owner/api/session?hold=1", None, {"Cookie": original})
                        try:
                            self.assertTrue(ready.wait(2))
                            if logout:
                                status, _, _ = self.http("POST", "/logout", "csrf=" + csrf, {
                                    "Cookie": original, "Content-Type": "application/x-www-form-urlencoded",
                                })
                                self.assertEqual(status, 303)
                            else:
                                new_cookie, _, _ = self.login(original)
                        finally:
                            release.set()
                        status, headers, _ = pending.result(timeout=5)
                    self.assertEqual(status, 200)
                    self.assertFalse(any(value.startswith("healthos_session_v2=") for value in headers["_cookies"]))
                    target = original if logout else new_cookie
                    self.assertEqual(self.http("GET", "/owner/api/session", headers={"Cookie": target})[0], 401 if logout else 200)

    def test_login_page_has_bounded_same_site_recovery_and_existing_server_redirect(self):
        _, _, page = self.http("GET", "/login")
        self.assertIn("/owner/api/session", page)
        self.assertIn("3000", page)
        cookie, _, _ = self.login()
        status, headers, _ = self.http("GET", "/login", headers={"Cookie": cookie})
        self.assertEqual((status, headers["Location"]), (303, "/"))

    def test_diagnostic_reasons_and_secret_redaction(self):
        class Logged(QuietHandler):
            log_message = healthos.HealthOSHandler.log_message

        self.server.RequestHandlerClass = Logged
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            self.http("GET", "/owner/api/session")
            self.http("GET", "/owner/api/session", headers={"Cookie": "healthos_session_v2=secret-token"})
            self.http("GET", "/owner/api/session", headers={"Cookie": "healthos_session_v2=bad!token"})
            cookie, session, _ = self.login()
            self.http("POST", "/training", "csrf=secret-csrf&records_json=secret-body", {
                "Cookie": cookie, "Accept": "application/json", "Authorization": "Bearer secret-auth",
            })
            self.http("GET", "/share/secret-share?token=secret-query")
            self.http("POST", "/login", "password=secret-password", {"Accept": "application/json"})
        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        reasons = {line["auth_reason"] for line in lines}
        self.assertTrue({"missing_cookie", "unknown_session", "malformed_cookie", "login_success", "csrf_invalid", "invalid_credentials"} <= reasons)
        self.assertEqual(len({line["request_id"] for line in lines}), len(lines))
        for secret in ("secret-token", "bad!token", "secret-csrf", "secret-body", "secret-auth", "secret-share", "secret-query", "secret-password", cookie.split("=", 1)[1], session["csrf"]):
            self.assertNotIn(secret, stream.getvalue())
        self.assertIn("/share/<REDACTED>", stream.getvalue())

    def test_lookup_reports_expiry_before_removal(self):
        start = 1000000
        for after, expected in ((7 * 86400, "idle_expired"), (30 * 86400, "absolute_expired")):
            with mock.patch.object(healthos.time, "time", return_value=start):
                token, _ = healthos.create_session(self.db)
            with mock.patch.object(healthos.time, "time", return_value=start + after):
                self.assertEqual(healthos._session_lookup(token, self.db), (None, expected))
                self.assertEqual(healthos._session_lookup(token, self.db), (None, "unknown_session"))
        token, _ = healthos.create_session(self.db)
        with mock.patch.dict(os.environ, {"HEALTHOS_OWNER_PASSWORD_HASH": healthos.hash_password("changed")}):
            self.assertEqual(healthos._session_lookup(token, self.db), (None, "credential_changed"))


if __name__ == "__main__":
    unittest.main()
