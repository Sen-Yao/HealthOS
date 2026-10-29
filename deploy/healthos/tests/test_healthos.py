import importlib.util
import http.client
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "healthos.py"
spec = importlib.util.spec_from_file_location("healthos", SCRIPT)
healthos = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(healthos)


class CanonicalStoreTests(unittest.TestCase):
    def test_owner_dashboard_observation_count_is_migrated_and_maintained(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            conn = healthos.init_db(db)
            sample = healthos.record_payload({
                "type": "HKQuantityTypeIdentifierHeartRate",
                "value": "61", "unit": "count/min",
                "startDate": "2026-10-09T08:00:00+00:00",
                "endDate": "2026-10-09T08:01:00+00:00",
            })
            healthos.insert_sample(conn, sample, healthos.utc_now())
            conn.commit()

            conn.execute("DROP TRIGGER observations_count_after_insert")
            conn.execute("DROP TRIGGER observations_count_after_delete")
            conn.execute("DROP TABLE owner_dashboard_counts")
            conn.execute(
                "DELETE FROM schema_migrations WHERE version='0.5.0-observation-count'"
            )
            conn.commit()
            conn.close()

            conn = healthos.init_db(db)
            self.assertEqual(
                conn.execute("SELECT observations FROM owner_dashboard_counts WHERE id=1").fetchone()[0],
                1,
            )
            self.assertFalse(healthos.insert_sample(conn, sample, healthos.utc_now()))
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT observations FROM owner_dashboard_counts WHERE id=1").fetchone()[0],
                1,
            )
            conn.execute("DELETE FROM observations")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT observations FROM owner_dashboard_counts WHERE id=1").fetchone()[0],
                0,
            )
            conn.close()

    def test_bulk_import_refreshes_dashboard_count_and_restores_triggers(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "export.xml"
            db = Path(directory) / "healthos.sqlite3"
            source.write_text(
                '<HealthData><Record type="HKQuantityTypeIdentifierHeartRate" '
                'value="61" unit="count/min" startDate="2026-10-09T08:00:00Z" '
                'endDate="2026-10-09T08:01:00Z" /></HealthData>',
                encoding="utf-8",
            )

            healthos.import_xml(source, db, progress_every=0)

            conn = healthos.init_db(db)
            self.assertEqual(
                conn.execute("SELECT observations FROM owner_dashboard_counts WHERE id=1").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                    "AND name='observations_count_after_insert'"
                ).fetchone()[0],
                1,
            )
            conn.close()

    def test_sqlite_backup_is_integrity_checked_and_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            backup_dir = Path(directory) / "backups"
            conn = healthos.init_db(db)
            healthos.insert_lab_result(conn, {
                "test_name": "HRV", "value": 42, "unit": "ms",
                "collection_time": "2026-10-27T00:00:00Z",
            })
            conn.commit()
            conn.close()
            backup = healthos.backup_sqlite_database(db, backup_dir)
            self.assertTrue(backup.is_file())
            copied = healthos.sqlite3.connect(backup)
            self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copied.execute("SELECT COUNT(1) FROM lab_results").fetchone()[0], 1)
            copied.close()
            conn = healthos.init_db(db)
            conn.execute("DELETE FROM lab_results")
            conn.commit()
            conn.close()
            copied = healthos.sqlite3.connect(backup)
            self.assertEqual(copied.execute("SELECT COUNT(1) FROM lab_results").fetchone()[0], 1)
            copied.close()

    def test_canonical_schema_and_manual_records(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            conn = healthos.init_db(db)
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "schema_migrations",
                    "observations",
                    "lab_results",
                    "medications",
                    "health_events",
                    "medical_documents",
                    "share_snapshots",
                    "training_sessions",
                    "training_exercises",
                    "training_exercise_records",
                    "training_exercise_aliases",
                    "training_alias_review_queue",
                    "training_apple_workout_matches",
                }.issubset(tables)
            )

            sample = healthos.record_payload(
                {
                    "type": "HKQuantityTypeIdentifierHeartRate",
                    "value": "61",
                    "unit": "count/min",
                    "startDate": "2026-10-09T08:00:00+08:00",
                    "endDate": "2026-10-09T08:01:00+08:00",
                }
            )
            self.assertTrue(healthos.insert_sample(conn, sample, healthos.utc_now()))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)

            lab = healthos.insert_lab_result(
                conn,
                {
                    "collection_time": "2026-10-09T08:00:00+08:00",
                    "test_name": "TSH",
                    "canonical_name": "tsh",
                    "value_num": 2.1,
                    "unit": "mIU/L",
                },
            )
            medication = healthos.insert_medication(
                conn,
                {
                    "drug_name": "Example Drug",
                    "dose": 10,
                    "dose_unit": "mg",
                    "start_time": "2026-10-09",
                },
            )
            event = healthos.insert_health_event(
                conn,
                {
                    "event_type": "symptom",
                    "start_time": "2026-10-09T09:00:00+08:00",
                    "title": "Headache",
                    "structured_payload": {"severity": 2},
                },
            )
            conn.commit()
            self.assertIsInstance(lab["id"], int)
            self.assertIsInstance(medication["id"], int)
            self.assertIsInstance(event["id"], int)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM health_events").fetchone()[0], 1)
            conn.close()

    def test_training_session_create_retry_and_cascade_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            payload = {
                "request_id": "record-form-attempt-1",
                "session_date": "2026-10-26",
                "body_part": "胸",
                "duration_minutes": 55,
                "feeling": "状态良好",
                "extra_data": {"entry_model": "exercise-set-v1"},
                "records": [
                    {"name": "卧推", "weight_kg": 60, "reps": [8], "rpe": [8], "sets_count": 1},
                    {"name": "卧推", "weight_kg": 65, "reps": [6], "rpe": [9], "sets_count": 1},
                    {"name": "夹胸", "weight_kg": None, "reps": [12, 10], "rpe": [], "extra_data": {"structure": "drop_set_group"}},
                ],
            }
            first = healthos.create_training_session(conn, payload)
            replay = healthos.create_training_session(conn, payload)
            self.assertEqual(first["id"], replay["id"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercise_records").fetchone()[0], 3)
            self.assertAlmostEqual(first["total_volume_kg"], 870)
            self.assertEqual([record["reps"] for record in replay["records"]], [[8], [6], [12, 10]])
            self.assertEqual(healthos.query_training_sessions(conn)[0]["record_count"], 3)
            self.assertTrue(healthos.delete_training_session(conn, int(first["id"])))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercise_records").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercises").fetchone()[0], 2)
            conn.close()

    def test_training_body_part_cycle_wraps_and_defaults_from_local_history(self):
        self.assertEqual(healthos.next_training_body_part("腿"), "胸")
        self.assertEqual(healthos.next_training_body_part("其他"), "胸")
        with tempfile.TemporaryDirectory() as directory:
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(Path(directory) / "healthos.sqlite3")
            server.raw_dir = Path(directory) / "raw"
            session_id, _ = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cookie = {"Cookie": f"healthos_session={session_id}"}
            try:
                conn = healthos.init_db(server.db_path)
                healthos.create_training_session(conn, {
                    "request_id": "cycle-seed-session",
                    "session_date": "2026-10-25",
                    "body_part": "背",
                    "records": [],
                })
                conn.close()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/training-legacy?source=local&new=1",
                    headers=cookie,
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    page = response.read().decode("utf-8")
                self.assertIn("<option selected>肩</option>", page)
            finally:
                healthos.destroy_session(session_id)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_training_session_rejects_invalid_payload_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            with self.assertRaises(ValueError):
                healthos.create_training_session(conn, {
                    "request_id": "bad-session",
                    "session_date": "not-a-date",
                    "body_part": "胸",
                    "records": [{"name": "卧推", "weight_kg": 60, "reps": [8]}],
                })
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 0)
            with self.assertRaises(ValueError):
                healthos.create_training_session(conn, {
                    "request_id": "bad-session",
                    "session_date": "2026-10-26",
                    "body_part": "胸",
                    "records": [{"name": "卧推", "weight_kg": -1, "reps": [8]}],
                })
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercises").fetchone()[0], 0)
            conn.close()

    def test_training_session_edit_preserves_identity_and_extended_record_data(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            original = healthos.create_training_session(conn, {
                "request_id": "original-entry",
                "session_date": "2026-10-25",
                "body_part": "腿",
                "records": [{
                    "name": "深蹲", "raw_name": "杠铃深蹲", "weight_kg": 80,
                    "reps": [8, 6], "rpe": [8, 9], "sets_count": 2,
                    "source_ironbook_id": 1234,
                    "extra_data": {"structure": "drop_set_group", "rest_seconds": 90},
                }],
            })
            updated = healthos.create_training_session(conn, {
                "update_session_id": original["id"],
                "request_id": "new-edit-request",
                "session_date": "2026-10-26",
                "body_part": "腿",
                "records": [{
                    "name": "深蹲", "raw_name": "杠铃深蹲", "weight_kg": 82.5,
                    "reps": [8, 6], "rpe": [8, 9], "sets_count": 2,
                    "source_ironbook_id": 1234,
                    "extra_data": {"structure": "drop_set_group", "rest_seconds": 90},
                }],
            })
            self.assertEqual(updated["id"], original["id"])
            self.assertEqual(updated["request_id"], "original-entry")
            self.assertEqual(updated["records"][0]["source_ironbook_id"], 1234)
            self.assertEqual(updated["records"][0]["raw_exercise_name"], "杠铃深蹲")
            self.assertEqual(updated["records"][0]["extra_data"], {"structure": "drop_set_group", "rest_seconds": 90})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 1)
            conn.close()

    def test_training_alias_resolves_to_canonical_exercise_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            canonical = healthos.create_training_session(conn, {
                "request_id": "alias-canonical-session",
                "session_date": "2026-10-20",
                "body_part": "胸",
                "records": [{"name": "杠铃卧推", "weight_kg": 60, "reps": [8]}],
            })
            exercise_id = conn.execute(
                "SELECT id FROM training_exercises WHERE standard_name='杠铃卧推'"
            ).fetchone()[0]
            alias = healthos.add_training_exercise_alias(conn, exercise_id, "Barbell Bench Press")
            repeated = healthos.add_training_exercise_alias(conn, exercise_id, "barbellbench press")
            self.assertEqual(alias["id"], repeated["id"])

            healthos.create_training_session(conn, {
                "request_id": "alias-resolved-session",
                "session_date": "2026-10-21",
                "body_part": "胸",
                "records": [{"name": " BARBELL BENCH PRESS ", "weight_kg": 62.5, "reps": [8]}],
            })
            resolved = healthos.get_training_session(conn, 2)
            self.assertEqual(resolved["records"][0]["standard_name"], "杠铃卧推")
            self.assertEqual(resolved["records"][0]["raw_exercise_name"], "BARBELL BENCH PRESS")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercises").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercise_aliases").fetchone()[0], 2)

            other = healthos.create_training_session(conn, {
                "request_id": "alias-other-exercise",
                "session_date": "2026-10-22",
                "body_part": "胸",
                "records": [{"name": "哑铃飞鸟", "reps": [12]}],
            })
            other_id = conn.execute(
                "SELECT id FROM training_exercises WHERE standard_name='哑铃飞鸟'"
            ).fetchone()[0]
            with self.assertRaises(ValueError):
                healthos.add_training_exercise_alias(conn, other_id, "barbell bench press")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercise_aliases").fetchone()[0], 3)
            conn.close()

    def test_ironbook_migration_preflight_reports_mapping_and_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            healthos.create_training_session(conn, {
                "request_id": "preflight-existing",
                "source_ironbook_id": 11,
                "session_date": "2026-10-25",
                "body_part": "背",
                "records": [{
                    "name": "高位下拉", "source_ironbook_id": 21,
                    "weight_kg": 40, "reps": [12], "sets_count": 1,
                }],
            })
            before = tuple(conn.execute(
                "SELECT (SELECT COUNT(*) FROM training_sessions),"
                "(SELECT COUNT(*) FROM training_exercises),"
                "(SELECT COUNT(*) FROM training_exercise_records)"
            ).fetchone())
            payload = [{
                "id": 11,
                "session_date": "2026-10-25",
                "body_part": "背",
                "total_volume_kg": 480,
                "records": [
                    {"id": 21, "raw_exercise_name": "下拉", "standard_name": "高位下拉", "reps": [12], "sets_count": 1},
                    {"id": 22, "raw_exercise_name": "陌生动作", "reps": [8], "sets_count": 1},
                    {"id": 23, "raw_exercise_name": "肩推", "standard_name": "器械肩推", "body_part": "肩", "reps": [10], "sets_count": 1},
                    {"id": 24, "raw_exercise_name": "器械推举", "standard_name": "器械肩推", "body_part": "胸", "reps": [8], "sets_count": 1},
                    {"id": 25, "raw_exercise_name": "飞鸟", "standard_name": "器械飞鸟", "reps": [10], "sets_count": 1},
                ],
            }, {
                "id": 11,
                "session_date": "2026-10-24",
                "body_part": "胸",
                "records": [],
            }]

            report = healthos.analyze_ironbook_training_migration(conn, payload)
            after = tuple(conn.execute(
                "SELECT (SELECT COUNT(*) FROM training_sessions),"
                "(SELECT COUNT(*) FROM training_exercises),"
                "(SELECT COUNT(*) FROM training_exercise_records)"
            ).fetchone())
            self.assertTrue(report["read_only"])
            self.assertEqual(report["scope"]["sessions_in_payload"], 2)
            expected_fingerprint = healthos.sha256_text(healthos.canonical_json(payload))
            self.assertEqual(report["scope"]["source_fingerprint"], expected_fingerprint)
            self.assertFalse(report["scope"]["full_history_confirmed"])
            self.assertEqual(report["scope"]["coverage"], "up_to_100_unconfirmed")
            self.assertEqual(report["sessions"]["duplicate_source_ids"], 1)
            self.assertEqual(report["sessions"]["existing_in_healthos"], 1)
            self.assertEqual(report["sessions"]["total_volume_kg"], 480)
            self.assertEqual(report["records"]["count"], 5)
            self.assertEqual(report["records"]["existing_in_healthos"], 1)
            self.assertEqual(report["records"]["unmapped_record_count"], 1)
            self.assertEqual(report["records"]["unmapped_exercise_names"][0]["name"], "陌生动作")
            self.assertEqual(report["records"]["source_standardized_records"], 4)
            self.assertEqual(report["records"]["catalog_creation_record_count"], 3)
            self.assertEqual(
                report["records"]["catalog_creation_exercise_names"],
                [
                    {"name": "器械肩推", "record_count": 2, "body_part_counts": {"胸": 1, "肩": 1}, "missing_body_part_record_count": 0},
                    {"name": "器械飞鸟", "record_count": 1, "body_part_counts": {}, "missing_body_part_record_count": 1},
                ],
            )
            preflight_html = healthos.render_ironbook_migration_preflight(report)
            self.assertIn("需人工确认", preflight_html)
            self.assertIn("缺分类 1 组", preflight_html)
            self.assertIn(expected_fingerprint, preflight_html)
            self.assertEqual(before, after)
            conn.close()

    def test_ironbook_migration_preflight_keeps_cap_sized_payload_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            payload = [{
                "id": index,
                "session_date": "2026-10-25",
                "body_part": "胸",
                "records": [],
            } for index in range(1, 101)]

            report = healthos.analyze_ironbook_training_migration(conn, payload)

            self.assertFalse(report["scope"]["full_history_confirmed"])
            self.assertEqual(report["scope"]["coverage"], "up_to_100_unconfirmed")
            self.assertEqual(len(report["limitations"]), 1)
            conn.close()

    def test_ironbook_migration_is_fingerprint_guarded_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            conn = healthos.init_db(db)
            snapshot = [{
                "id": 41,
                "session_date": "2026-10-20",
                "body_part": "背",
                "duration_minutes": 50,
                "records": [{
                    "id": 401,
                    "exercise_id": 4001,
                    "raw_exercise_name": "下拉",
                    "standard_name": "高位下拉",
                    "body_part": "背",
                    "weight_kg": 45,
                    "reps": [10, 9],
                    "rpe": [8, 9],
                    "sets_count": 2,
                    "total_reps": 19,
                    "volume_kg": 855,
                    "exercise_order": 1,
                    "extra_data": {"source": "ironbook"},
                }],
            }]
            fingerprint = healthos.sha256_text(healthos.canonical_json(snapshot))
            result = healthos.migrate_ironbook_training_sessions(
                conn, snapshot, expected_fingerprint=fingerprint,
                source_full_history_confirmed=True, source_total=1,
            )
            self.assertEqual(result["sessions_after"], 1)
            self.assertEqual(result["records_seen"], 1)
            self.assertEqual(result["sessions_reconciled"], 1)
            self.assertEqual(result["records_reconciled"], 1)
            self.assertEqual(result["missing_sessions"], 0)
            self.assertEqual(result["missing_records"], 0)
            self.assertIsInstance(result["migration_run_id"], int)
            self.assertEqual(conn.execute("SELECT COUNT(1) FROM migration_runs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercises").fetchone()[0], 1)
            catalog_source_id = conn.execute(
                "SELECT source_ironbook_id FROM training_exercises WHERE standard_name='高位下拉'"
            ).fetchone()[0]
            self.assertEqual(catalog_source_id, 4001)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercise_records").fetchone()[0], 1)

            repeated = healthos.migrate_ironbook_training_sessions(
                conn, snapshot, expected_fingerprint=fingerprint,
                source_full_history_confirmed=True, source_total=1,
            )
            self.assertEqual(repeated["sessions_before"], 1)
            self.assertEqual(repeated["sessions_after"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(1) FROM migration_runs").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercise_records").fetchone()[0], 1)

            with self.assertRaisesRegex(ValueError, "fingerprint changed"):
                healthos.migrate_ironbook_training_sessions(
                    conn, [{**snapshot[0], "body_part": "胸"}],
                    expected_fingerprint=fingerprint,
                    source_full_history_confirmed=True, source_total=1,
                )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 1)

            invalid = [{**snapshot[0], "id": 42, "records": [{
                **snapshot[0]["records"][0], "id": 402, "standard_name": "", "body_part": "背",
            }]}]
            invalid_fingerprint = healthos.sha256_text(healthos.canonical_json(invalid))
            with self.assertRaisesRegex(ValueError, "no standard name"):
                healthos.migrate_ironbook_training_sessions(
                    conn, invalid, expected_fingerprint=invalid_fingerprint,
                    source_full_history_confirmed=True, source_total=1,
                )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 1)

            healthos.create_training_session(conn, {
                "request_id": "preexisting-local-action",
                "session_date": "2026-10-19",
                "body_part": "胸",
                "records": [{"name": "冲突动作", "body_part": "胸", "weight_kg": 20, "reps": [10], "rpe": [6]}],
            })
            conflict = [{
                "id": 42,
                "session_date": "2026-10-21",
                "body_part": "背",
                "records": [{
                    "id": 402, "raw_exercise_name": "冲突动作", "standard_name": "冲突动作",
                    "body_part": "背", "weight_kg": 20, "reps": [10], "rpe": [6],
                }],
            }]
            conflict_fingerprint = healthos.sha256_text(healthos.canonical_json(conflict))
            with self.assertRaisesRegex(ValueError, "conflicting body part"):
                healthos.migrate_ironbook_training_sessions(
                    conn, conflict, expected_fingerprint=conflict_fingerprint,
                    source_full_history_confirmed=True, source_total=1,
                )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 2)

            healthos.create_training_session(conn, {
                "request_id": "preexisting-source-id-owner",
                "session_date": "2026-10-18",
                "body_part": "腿",
                "records": [{
                    "name": "已有源 ID 动作", "body_part": "腿", "source_exercise_id": 4999,
                    "weight_kg": 40, "reps": [8], "rpe": [7],
                }],
            })
            source_id_conflict = [{
                "id": 43,
                "session_date": "2026-10-22",
                "body_part": "胸",
                "records": [{
                    "id": 403, "exercise_id": 4999, "raw_exercise_name": "卧推",
                    "standard_name": "卧推", "body_part": "胸", "weight_kg": 60,
                    "reps": [8], "rpe": [8],
                }],
            }]
            source_id_conflict_fingerprint = healthos.sha256_text(
                healthos.canonical_json(source_id_conflict)
            )
            counts_before_conflict = (
                conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM training_exercise_records").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM training_exercises").fetchone()[0],
            )
            with self.assertRaisesRegex(ValueError, "conflicts with its catalog entry"):
                healthos.migrate_ironbook_training_sessions(
                    conn, source_id_conflict,
                    expected_fingerprint=source_id_conflict_fingerprint,
                    source_full_history_confirmed=True, source_total=1,
                )
            self.assertEqual(
                (
                    conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM training_exercise_records").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM training_exercises").fetchone()[0],
                ),
                counts_before_conflict,
            )
            conn.close()

    def test_ironbook_write_through_retries_edit_guards_and_delete(self):
        state = {
            "sessions": {
                9: {
                    "id": 9,
                    "session_date": "2026-10-25",
                    "body_part": "背",
                    "source_file": "ironbook",
                    "weekday": "星期日",
                    "status_note": "保留状态",
                    "raw_markdown": "保留原文",
                    "extra_data": {},
                    "records": [],
                }
            },
            "posts": [],
            "deletes": [],
        }

        class IronbookStub(healthos.http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def send_payload(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                session_id = int(self.path.rsplit("/", 1)[-1])
                self.send_payload(200, state["sessions"][session_id])

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                state["posts"].append(payload)
                existing = next((
                    session for session in state["sessions"].values()
                    if session["session_date"] == payload["date"]
                    and session["body_part"] == payload["body_part"]
                    and session["source_file"] == payload["source_file"]
                ), None)
                if existing is None:
                    result = {"id": 10, "session_date": payload["date"], "body_part": payload["body_part"]}
                else:
                    existing.update({
                        "session_date": payload["date"],
                        "body_part": payload["body_part"],
                        "records": payload["records"],
                    })
                    result = existing
                self.send_payload(201, result)

            def do_DELETE(self):
                session_id = int(self.path.rsplit("/", 1)[-1])
                state["deletes"].append(session_id)
                self.send_payload(200, {"ok": True, "deleted": session_id})

        server = healthos.http.server.ThreadingHTTPServer(("127.0.0.1", 0), IronbookStub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        payload = {
            "session_date": "2026-10-26",
            "body_part": "胸",
            "duration_minutes": 45,
            "sleep_hours": 7.5,
            "stress_level": 3,
            "hrv_score": 62,
            "feeling": "状态良好",
            "extra_data": {"training_timing": {
                "started_at": "2026-10-26T08:00:00+00:00",
                "ended_at": "2026-10-26T08:45:00+00:00",
            }},
            "records": [{"name": "卧推", "weight_kg": 60, "reps": [8], "rpe": [8]}],
        }
        try:
            first = healthos.save_ironbook_training_session(
                base_url, 2, payload, request_id="request-token-0001"
            )
            replay = healthos.save_ironbook_training_session(
                base_url, 2, payload, request_id="request-token-0001"
            )
            self.assertEqual((first["id"], replay["id"]), (10, 10))
            self.assertEqual(len(state["posts"]), 2)
            self.assertEqual(len({item["source_file"] for item in state["posts"]}), 1)
            self.assertEqual(
                {key: state["posts"][-1][key] for key in (
                    "duration_minutes", "sleep_hours", "stress_level", "hrv_score", "feeling",
                )},
                {"duration_minutes": 45, "sleep_hours": 7.5, "stress_level": 3, "hrv_score": 62, "feeling": "状态良好"},
            )
            self.assertEqual(
                state["posts"][-1]["extra_data"]["training_timing"],
                payload["extra_data"]["training_timing"],
            )

            edit = {**payload, "session_date": "2026-10-25", "body_part": "背"}
            updated = healthos.save_ironbook_training_session(
                base_url, 2, edit, request_id="request-token-0002", session_id=9
            )
            self.assertEqual(updated["id"], 9)
            self.assertEqual(state["posts"][-1]["source_file"], "ironbook")
            self.assertEqual(state["posts"][-1]["weekday"], "星期日")
            self.assertEqual(state["posts"][-1]["status_note"], "保留状态")
            self.assertEqual(state["posts"][-1]["raw_markdown"], "保留原文")

            changed_natural_key = {**edit, "session_date": "2026-10-24"}
            with self.assertRaisesRegex(ValueError, "暂不支持"):
                healthos.save_ironbook_training_session(
                    base_url, 2, changed_natural_key,
                    request_id="request-token-0003", session_id=9,
                )
            self.assertEqual(len(state["posts"]), 3)
            with self.assertRaisesRegex(ValueError, "RPE"):
                healthos.save_ironbook_training_session(
                    base_url, 2,
                    {**payload, "records": [{"name": "卧推", "weight_kg": 60, "reps": [8], "rpe": []}]},
                    request_id="request-token-0004",
                )
            with self.assertRaisesRegex(ValueError, "重量"):
                healthos.save_ironbook_training_session(
                    base_url, 2,
                    {**payload, "records": [{"name": "卧推", "weight_kg": float("nan"), "reps": [8], "rpe": [8]}]},
                    request_id="request-token-0005",
                )
            invalid_drop_set = {**payload, "records": [{
                "name": "卧推", "extra_data": {"structure": "drop_set_group", "segments": []},
            }]}
            with self.assertRaisesRegex(ValueError, "至少需要一个分段"):
                healthos.save_ironbook_training_session(
                    base_url, 2,
                    invalid_drop_set,
                    request_id="request-token-0006",
                )
            for request_id, segment, expected_error in (
                ("request-token-0009", {"reps": 8}, "缺少重量"),
                ("request-token-0010", {"weight_kg": 40}, "次数必须大于 0"),
            ):
                invalid_segment = {**payload, "records": [{
                    "name": "卧推", "extra_data": {
                        "structure": "drop_set_group", "segments": [segment],
                    },
                }]}
                with self.assertRaisesRegex(ValueError, expected_error):
                    healthos.save_ironbook_training_session(
                        base_url, 2, invalid_segment, request_id=request_id,
                    )
            drop_set = {**payload, "records": [{
                "name": "夹胸", "extra_data": {"structure": "drop_set_group", "segments": [
                    {"weight_kg": "40", "reps": "12"},
                    {"weight_kg": "30", "reps": "10"},
                    {"weight_kg": "20", "reps": "8"},
                ]},
            }]}
            drop_created = healthos.save_ironbook_training_session(
                base_url, 2, drop_set, request_id="request-token-0007"
            )
            drop_replayed = healthos.save_ironbook_training_session(
                base_url, 2, drop_set, request_id="request-token-0007"
            )
            self.assertEqual((drop_created["id"], drop_replayed["id"]), (10, 10))
            drop_record = state["posts"][-1]["records"][0]
            self.assertIsNone(drop_record["weight_kg"])
            self.assertEqual(drop_record["reps"], [12, 10, 8])
            self.assertEqual(drop_record["rpe"], [])
            self.assertEqual(drop_record["sets_count"], 1)
            self.assertEqual(drop_record["total_reps"], 30)
            self.assertEqual(drop_record["volume_kg"], 940)
            self.assertEqual(drop_record["extra_data"]["drop_set_group"], "第 1 大组")
            self.assertEqual(drop_record["extra_data"]["segments"], [
                {"weight_kg": 40, "reps": 12}, {"weight_kg": 30, "reps": 10}, {"weight_kg": 20, "reps": 8},
            ])
            self.assertEqual(state["posts"][-1]["source_file"], state["posts"][-2]["source_file"])
            edited_drop = {**edit, "records": drop_set["records"]}
            healthos.save_ironbook_training_session(
                base_url, 2, edited_drop, request_id="request-token-0008", session_id=9
            )
            self.assertEqual(state["posts"][-1]["records"][0]["extra_data"]["segments"], drop_record["extra_data"]["segments"])
            healthos.delete_ironbook_training_session(base_url, 2, 9)
            self.assertEqual(state["deletes"], [9])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_ironbook_session_pages_support_new_api_and_legacy_cap_fallback(self):
        class JsonResponse:
            status = 200

            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        source = [{"id": index} for index in range(100)]
        with mock.patch.object(healthos.urllib.request, "urlopen", return_value=JsonResponse(source)) as open_url:
            first_page = healthos.fetch_ironbook_session_page("http://ironbook.test", 1)
            second_page = healthos.fetch_ironbook_session_page(
                "http://ironbook.test", 1, offset=50
            )
            capped_page = healthos.fetch_ironbook_session_page(
                "http://ironbook.test", 1, offset=100
            )
        self.assertEqual([item["id"] for item in first_page["items"]], list(range(50)))
        self.assertTrue(first_page["has_next"])
        self.assertEqual([item["id"] for item in second_page["items"]], list(range(50, 100)))
        self.assertTrue(second_page["api_cap_reached"])
        self.assertIsNone(second_page["total"])
        self.assertEqual(capped_page["items"], [])
        self.assertTrue(capped_page["api_cap_reached"])
        self.assertFalse(capped_page["has_next"])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(open_url.call_args.args[0].full_url).query)
        self.assertEqual(query, {"limit": ["100"], "offset": ["100"], "format": ["page"]})

        envelope = {
            "items": [{"id": index} for index in range(50, 150)],
            "offset": 50,
            "limit": 100,
            "total": 167,
        }
        with mock.patch.object(healthos.urllib.request, "urlopen", return_value=JsonResponse(envelope)):
            page = healthos.fetch_ironbook_session_page("http://ironbook.test", 1, offset=50)
        self.assertEqual([item["id"] for item in page["items"]], list(range(50, 100)))
        self.assertEqual(page["total"], 167)
        self.assertTrue(page["has_next"])
        self.assertFalse(page["api_cap_reached"])
        with self.assertRaises(ValueError):
            healthos.fetch_ironbook_session_page("http://ironbook.test", 1, page_size=0)
        with mock.patch.object(healthos.urllib.request, "urlopen", return_value=JsonResponse([{"id": 1}, None])):
            with self.assertRaisesRegex(RuntimeError, "malformed items"):
                healthos.fetch_ironbook_session_page("http://ironbook.test", 1)
        with mock.patch.object(healthos.urllib.request, "urlopen", return_value=JsonResponse({"id": 2})):
            with self.assertRaisesRegex(RuntimeError, "mismatched session ID"):
                healthos.fetch_ironbook_session_details(
                    "http://ironbook.test", [{"id": 1}], 1
                )

    def test_ironbook_session_history_confirms_full_paged_source_and_guards_legacy_cap(self):
        source = [{"id": index} for index in range(1, 151)]
        offsets = []

        class JsonResponse:
            status = 200

            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        def paged_source(request, timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            offset = int(query["offset"][0])
            limit = int(query["limit"][0])
            offsets.append(offset)
            return JsonResponse({
                "items": source[offset:offset + limit],
                "offset": offset,
                "limit": limit,
                "total": len(source),
            })

        with mock.patch.object(healthos.urllib.request, "urlopen", side_effect=paged_source):
            history = healthos.fetch_ironbook_session_history("http://ironbook.test", 1)
        self.assertEqual(len(history["sessions"]), 150)
        self.assertTrue(history["full_history_confirmed"])
        self.assertEqual(history["source_total"], 150)
        self.assertIsNone(history["source_limit"])
        self.assertEqual(offsets, [0, 50, 100])

        def short_legacy_source(_base_url, _timeout, *, offset, page_size):
            items = source[:67][offset:offset + page_size]
            return {
                "items": items,
                "offset": offset,
                "total": None,
                "has_next": offset + len(items) < 67,
                "api_cap_reached": False,
            }

        with mock.patch.object(healthos, "fetch_ironbook_session_page", side_effect=short_legacy_source):
            short_history = healthos.fetch_ironbook_session_history("http://ironbook.test", 1)
        self.assertEqual(len(short_history["sessions"]), 67)
        self.assertTrue(short_history["full_history_confirmed"])
        self.assertEqual(short_history["source_limit"], 100)

        def legacy_source(_base_url, _timeout, *, offset, page_size):
            items = source[:100][offset:offset + page_size]
            return {
                "items": items,
                "offset": offset,
                "total": None,
                "has_next": offset + len(items) < 100,
                "api_cap_reached": offset + page_size >= 100,
            }

        with mock.patch.object(healthos, "fetch_ironbook_session_page", side_effect=legacy_source):
            capped_history = healthos.fetch_ironbook_session_history("http://ironbook.test", 1)
        self.assertEqual(len(capped_history["sessions"]), 100)
        self.assertFalse(capped_history["full_history_confirmed"])
        self.assertTrue(capped_history["api_cap_reached"])
        self.assertEqual(capped_history["source_limit"], 100)

        def changing_total(_base_url, _timeout, *, offset, page_size):
            return {
                "items": source[offset:offset + page_size],
                "offset": offset,
                "total": 151 if offset else 150,
                "has_next": True,
                "api_cap_reached": False,
            }

        with mock.patch.object(healthos, "fetch_ironbook_session_page", side_effect=changing_total):
            with self.assertRaisesRegex(RuntimeError, "total changed"):
                healthos.fetch_ironbook_session_history("http://ironbook.test", 1)

    def test_ironbook_exercise_suggestions_are_validated_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            server = healthos.http.server.ThreadingHTTPServer(("127.0.0.1", 0), healthos.HealthOSHandler)
            server.db_path = str(Path(directory) / "healthos.sqlite3")
            server.raw_dir = Path(directory) / "raw"
            server.ironbook_base_url = "http://ironbook.test"
            server.ironbook_timeout_seconds = 1
            server.ironbook_read_only = True
            session_id, _ = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}/owner/api/training/exercises?q=bench&body_part=%E8%83%B8"
                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    urllib.request.urlopen(base, timeout=2)
                self.assertEqual(unauthorized.exception.code, 401)
                request = urllib.request.Request(base, headers={"Cookie": f"healthos_session={session_id}"})
                with mock.patch.object(healthos, "fetch_ironbook_exercise_suggestions", return_value=[{
                    "standard_name": "杠铃卧推", "body_part": "胸",
                }]) as fetch:
                    with urllib.request.urlopen(request, timeout=2) as response:
                        result = json.loads(response.read())
                self.assertEqual(result, [{"standard_name": "杠铃卧推", "body_part": "胸"}])
                self.assertEqual(fetch.call_args.args[2:], ("bench", "胸"))
                with self.assertRaises(ValueError):
                    healthos.fetch_ironbook_exercise_suggestions("http://ironbook.test", 1, "x", "invalid")
            finally:
                healthos.destroy_session(session_id)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_ironbook_last_exercise_reference_is_minimal_and_owner_only(self):
        raw_payload = {
            "session_date": "2026-10-11",
            "sets": [{
                "weight_kg": 40,
                "reps": [12],
                "rpe": [8],
                "note": "private note",
                "extra_data": {"source": "ironbook", "structure": "drop_set_group", "segments": [
                    {"weight_kg": 30, "reps": 12, "private": "omit"},
                ], "private": "omit"},
            }],
        }
        response_context = mock.MagicMock()
        response_context.__enter__.return_value.status = 200
        response_context.__enter__.return_value.read.return_value = json.dumps(raw_payload).encode()
        with mock.patch.object(healthos.urllib.request, "urlopen", return_value=response_context) as request:
            result = healthos.fetch_ironbook_last_exercise("http://ironbook.test", 1, "高位 下拉")
        self.assertEqual(
            request.call_args.args[0].full_url,
            "http://ironbook.test/api/exercises/last?name=%E9%AB%98%E4%BD%8D+%E4%B8%8B%E6%8B%89",
        )
        self.assertEqual(result, {
            "session_date": "2026-10-11",
            "sets": [{
                "weight_kg": 40,
                "reps": [12],
                "rpe": [8],
                "extra_data": {"structure": "drop_set_group", "segments": [{"weight_kg": 30, "reps": 12}]},
            }],
        })
        with self.assertRaises(ValueError):
            healthos.fetch_ironbook_last_exercise("http://ironbook.test", 1, "  ")
        empty_response = mock.MagicMock()
        empty_response.__enter__.return_value.status = 200
        empty_response.__enter__.return_value.read.return_value = b"null"
        with mock.patch.object(healthos.urllib.request, "urlopen", return_value=empty_response):
            self.assertEqual(
                healthos.fetch_ironbook_last_exercise("http://ironbook.test", 1, "新动作"),
                {"session_date": None, "sets": []},
            )

        with tempfile.TemporaryDirectory() as directory:
            server = healthos.http.server.ThreadingHTTPServer(("127.0.0.1", 0), healthos.HealthOSHandler)
            server.db_path = str(Path(directory) / "healthos.sqlite3")
            server.raw_dir = Path(directory) / "raw"
            server.ironbook_base_url = "http://ironbook.test"
            server.ironbook_timeout_seconds = 1
            server.ironbook_read_only = True
            session_id, _ = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/owner/api/training/exercises/last?name=bench"
            try:
                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    urllib.request.urlopen(url, timeout=2)
                self.assertEqual(unauthorized.exception.code, 401)
                request = urllib.request.Request(url, headers={"Cookie": f"healthos_session={session_id}"})
                with mock.patch.object(healthos, "fetch_ironbook_last_exercise", return_value=result) as fetch:
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(json.loads(response.read()), {"exercise": "bench", **result})
                self.assertEqual(fetch.call_args.args[2], "bench")
            finally:
                healthos.destroy_session(session_id)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_owner_training_form_writes_and_deletes_remotely_without_local_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(Path(directory) / "healthos.sqlite3")
            server.raw_dir = Path(directory) / "raw"
            server.ironbook_base_url = "http://ironbook.test"
            server.ironbook_timeout_seconds = 1
            session_cookie, csrf = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            records = [{"name": "卧推", "weight_kg": 60, "reps": [8], "rpe": [8]}]
            timing = {
                "started_at": "2026-10-26T08:00:00+00:00",
                "ended_at": "2026-10-26T08:45:00+00:00",
            }
            form = urllib.parse.urlencode({
                "csrf": csrf,
                "training_source": "ironbook",
                "request_id": "request-token-0001",
                "session_date": "2026-10-26",
                "body_part": "胸",
                "duration_minutes": "45",
                "sleep_hours": "7.5",
                "stress_level": "3",
                "hrv_score": "62",
                "feeling": "状态良好",
                "records_json": json.dumps(records),
                "training_timing_json": json.dumps(timing),
                "ironbook_extra_data": json.dumps({"source_marker": "keep"}),
            })
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                with mock.patch.object(healthos, "save_ironbook_training_session", return_value={"id": 88}) as save:
                    connection.request(
                        "POST", "/training", form,
                        {"Cookie": f"healthos_session={session_cookie}", "Content-Type": "application/x-www-form-urlencoded"},
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.status, 303)
                    self.assertIn("ironbook_session=88", response.getheader("Location"))
                    response.read()
                    self.assertEqual(save.call_args.kwargs["session_id"], None)
                    self.assertEqual(save.call_args.args[2]["records"], records)
                    self.assertEqual(
                        {key: save.call_args.args[2][key] for key in (
                            "duration_minutes", "sleep_hours", "stress_level", "hrv_score", "feeling",
                        )},
                        {"duration_minutes": "45", "sleep_hours": "7.5", "stress_level": "3", "hrv_score": "62", "feeling": "状态良好"},
                    )
                    self.assertEqual(
                        save.call_args.args[2]["extra_data"],
                        {"source_marker": "keep", "training_timing": timing},
                    )
                with mock.patch.object(healthos, "delete_ironbook_training_session") as delete:
                    delete_form = urllib.parse.urlencode({
                        "csrf": csrf, "training_source": "ironbook", "session_id": "88",
                    })
                    connection.request(
                        "POST", "/training/delete", delete_form,
                        {"Cookie": f"healthos_session={session_cookie}", "Content-Type": "application/x-www-form-urlencoded"},
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.status, 303)
                    self.assertEqual(response.getheader("Location"), "/training?source=ironbook")
                    response.read()
                    self.assertEqual(delete.call_args.args[2], 88)
                conn = healthos.init_db(server.db_path)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 0)
                conn.close()
            finally:
                connection.close()
                healthos.destroy_session(session_cookie)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_training_analytics_weekly_body_part_and_exercise_trends(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            first = healthos.create_training_session(conn, {
                "request_id": "analytics-session-1",
                "session_date": "2026-10-20",
                "body_part": "胸",
                "records": [
                    {"name": "卧推", "weight_kg": 60, "reps": [8, 8], "rpe": [8, 8], "sets_count": 2},
                    {"name": "夹胸", "weight_kg": 20, "reps": [12], "rpe": [6], "sets_count": 1,
                     "extra_data": {"structure": "drop_set_group"}},
                ],
            })
            healthos.create_training_session(conn, {
                "request_id": "analytics-session-2",
                "session_date": "2026-10-26",
                "body_part": "背",
                "records": [{"name": "卧推", "weight_kg": 65, "reps": [6], "rpe": [9], "sets_count": 1}],
            })
            analytics = healthos.training_analytics(conn, as_of=healthos.dt.date(2026, 10, 26))
            conn.close()
            this_week = analytics["weeks"][-1]
            previous_week = analytics["weeks"][-2]
            self.assertEqual((this_week["week_start"], this_week["sessions"], this_week["volume_kg"]), ("2026-10-26", 1, 390))
            self.assertEqual((previous_week["week_start"], previous_week["sessions"], previous_week["effective_sets"]), ("2026-10-19", 1, 2))
            self.assertEqual(analytics["body_parts_90d"], [("背", 1), ("胸", 1)])
            bench = next(item for item in analytics["exercise_trends"] if item["name"] == "卧推")
            self.assertEqual([item["session_date"] for item in bench["sessions"]], ["2026-10-26", "2026-10-20"])
            self.assertEqual([item["max_weight_kg"] for item in bench["sessions"]], [65.0, 60.0])

    def test_training_health_context_requires_explicit_opt_in_and_redacts_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            healthos.create_training_session(conn, {
                "request_id": "context-training-session",
                "session_date": "2026-10-26",
                "body_part": "胸",
                "duration_minutes": 48,
                "feeling": "private session feeling",
                "records": [{
                    "name": "卧推", "weight_kg": 60, "reps": [8, 7], "rpe": [8, 9],
                    "sets_count": 2, "note": "private exercise note",
                    "extra_data": {"structure": "drop_set_group"},
                }],
            })
            default_context = healthos.build_health_context(conn, {"preset": "recovery_30d"})
            self.assertEqual(default_context["training"], [])
            self.assertNotIn("training", default_context["scope"]["domains"])

            context = healthos.build_health_context(conn, {
                "preset": "recovery_30d", "include_training": True,
            })
            self.assertIn("training", context["scope"]["domains"])
            self.assertEqual(context["training"][0]["exercises"][0]["reps"], [8, 7])
            markdown = healthos.format_health_context_markdown(context)
            self.assertIn("## Strength Training", markdown)
            self.assertIn("卧推", markdown)
            self.assertIn("drop set", markdown)
            self.assertNotIn("private session feeling", markdown)
            self.assertNotIn("private exercise note", markdown)

            token, metadata = healthos.create_share_snapshot(
                conn, {"preset": "recovery_30d", "include_training": True},
            )
            self.assertEqual(metadata["scope_type"], "recovery_30d+training")
            _, snapshot = healthos.consume_share_snapshot(conn, token)
            self.assertEqual(len(snapshot["training"]), 1)
            self.assertNotIn("private exercise note", healthos.canonical_json(snapshot))
            conn.close()

    def test_manual_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            with self.assertRaises(ValueError):
                healthos.insert_lab_result(conn, {"test_name": "TSH"})
            with self.assertRaises(ValueError):
                healthos.insert_medication(conn, {"drug_name": "Example Drug"})
            with self.assertRaises(ValueError):
                healthos.insert_health_event(conn, {"event_type": "note", "title": "x"})
            conn.close()

    def test_legacy_samples_are_migrated_to_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "legacy.sqlite3"
            conn = healthos.sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE ingest_batches (
                    batch_id TEXT PRIMARY KEY, received_at TEXT NOT NULL,
                    device TEXT, window_start TEXT, window_end TEXT,
                    sample_count INTEGER NOT NULL DEFAULT 0, schema_version TEXT NOT NULL,
                    raw_path TEXT, source_kind TEXT NOT NULL DEFAULT 'shortcut',
                    status TEXT NOT NULL DEFAULT 'accepted'
                );
                CREATE TABLE health_samples (
                    id INTEGER PRIMARY KEY, metric TEXT NOT NULL, value_num REAL,
                    value_text TEXT, unit TEXT, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
                    creation_time TEXT, source TEXT, source_name TEXT, device_name TEXT,
                    identity_hash TEXT NOT NULL, content_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    raw_type TEXT NOT NULL, metadata_json TEXT,
                    UNIQUE(identity_hash, content_hash)
                );
                """
            )
            conn.execute(
                """INSERT INTO health_samples
                (id,metric,value_num,value_text,unit,start_time,end_time,source,
                 device_name,identity_hash,content_hash,first_seen_at,last_seen_at,raw_type)
                VALUES (1,'hrv',51,'51','ms','2026-10-01','2026-10-01',
                        'apple_health','watch','identity','content','2026-10-01','2026-10-01','hrv')"""
            )
            conn.commit()
            conn.close()

            upgraded = healthos.init_db(db)
            row = upgraded.execute(
                "SELECT metric, value_num, source_device FROM observations"
            ).fetchone()
            self.assertEqual(tuple(row), ("hrv", 51.0, "watch"))
            self.assertIsNotNone(upgraded.execute(
                "SELECT version FROM schema_migrations WHERE version='0.3.0-canonical'"
            ).fetchone())
            upgraded.close()

    def test_manual_api_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(db)
            server.raw_dir = Path(directory) / "raw"
            old_token = os.environ.get("HEALTHOS_API_TOKEN")
            os.environ["HEALTHOS_API_TOKEN"] = "test-token"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *_args, **_kwargs):
                        return None

                base = f"http://127.0.0.1:{server.server_port}"
                payload = json.dumps(
                    {
                        "collection_time": "2026-10-09",
                        "test_name": "TSH",
                        "value_num": 2.1,
                        "unit": "mIU/L",
                    }
                ).encode()
                request = urllib.request.Request(
                    base + "/api/v1/health/labs",
                    data=payload,
                    method="POST",
                    headers={
                        "Authorization": "Bearer test-token",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                request = urllib.request.Request(
                    base + "/api/v1/health/labs?limit=10",
                    headers={"Authorization": "Bearer test-token"},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    result = json.loads(response.read())
                self.assertEqual(len(result["items"]), 1)
                self.assertEqual(result["items"][0]["test_name"], "TSH")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                if old_token is None:
                    os.environ.pop("HEALTHOS_API_TOKEN", None)
                else:
                    os.environ["HEALTHOS_API_TOKEN"] = old_token

    def test_ironbook_migration_preflight_route_is_authenticated_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(db)
            server.raw_dir = Path(directory) / "raw"
            server.ironbook_base_url = "http://ironbook.test"
            server.ironbook_timeout_seconds = 1
            old_token = os.environ.get("HEALTHOS_API_TOKEN")
            os.environ["HEALTHOS_API_TOKEN"] = "test-token"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            source = [{"id": 31, "session_date": "2026-10-25", "body_part": "胸", "records": []}]
            try:
                base = f"http://127.0.0.1:{server.server_port}/api/v1/training/migration-preflight"
                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    urllib.request.urlopen(base, timeout=3)
                self.assertEqual(unauthorized.exception.code, 401)
                request = urllib.request.Request(base, headers={"Authorization": "Bearer test-token"})
                history = {
                    "sessions": source,
                    "full_history_confirmed": True,
                    "source_total": 1,
                    "source_limit": None,
                    "paginated_source": True,
                }
                with mock.patch.object(healthos, "fetch_ironbook_session_history", return_value=history), \
                     mock.patch.object(healthos, "fetch_ironbook_session_details", return_value=source):
                    with urllib.request.urlopen(request, timeout=3) as response:
                        report = json.loads(response.read())
                self.assertEqual(report["scope"]["sessions_in_payload"], 1)
                self.assertTrue(report["scope"]["full_history_confirmed"])
                self.assertEqual(report["scope"]["source_total"], 1)
                self.assertTrue(report["scope"]["paginated_source"])
                conn = healthos.init_db(db)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 0)
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                if old_token is None:
                    os.environ.pop("HEALTHOS_API_TOKEN", None)
                else:
                    os.environ["HEALTHOS_API_TOKEN"] = old_token

    def test_owner_can_browse_ironbook_sessions_without_local_entry_form(self):
        with tempfile.TemporaryDirectory() as directory:
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(Path(directory) / "healthos.sqlite3")
            server.raw_dir = Path(directory) / "raw"
            server.ironbook_base_url = "http://ironbook.test"
            server.ironbook_timeout_seconds = 1
            session_id, _ = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            summary = [{
                "id": 71, "session_date": "2026-10-25", "body_part": "背",
                "duration_minutes": 45, "total_volume_kg": 1234, "effective_sets": 9,
            }]
            detail = [{**summary[0], "records": [{
                "raw_exercise_name": "坐姿划船", "weight_kg": 40,
                "reps": [12, 10], "rpe": [7, 8], "sets_count": 2,
            }, {
                "raw_exercise_name": "夹胸", "weight_kg": None,
                "reps": [12, 10], "rpe": [], "sets_count": 1,
                "extra_data": {"structure": "drop_set_group", "segments": [
                    {"weight_kg": 40, "reps": 12}, {"weight_kg": 30, "reps": 10},
                ]},
            }]}]
            stats = {
                "summary": {
                    "total_sessions": 67, "sessions_last_30d": 8,
                    "avg_sessions_per_week": 2.1, "total_effective_sets": 420,
                    "avg_duration_minutes": 55, "date_range": "2026-03-07 → 2026-10-25",
                },
                "performance_summary": {
                    "latest_session_score": 82.5, "avg_last_6_sessions": 80.2,
                },
                "weekly_frequency": [{"week_start": "2026-10-19", "sessions": 3, "volume_kg": 12000}],
                "exercise_progression": [{
                    "standard_name": "<脚举机>", "body_part": "腿", "sessions": 4,
                    "max_weight": 180, "last_weight": 170, "max_e1rm": 220, "last_date": "2026-10-25",
                }],
                "performance_exercises": [{
                    "standard_name": "坐姿划船", "body_part": "背", "status": "进步",
                    "latest_score": 60, "trend_delta": 2, "sessions": 4, "is_pr": True,
                }],
            }
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/training-legacy?source=ironbook&ironbook_session=71",
                    headers={"Cookie": f"healthos_session={session_id}"},
                )
                with mock.patch.object(healthos, "fetch_ironbook_session_page", return_value={
                    "items": summary, "offset": 0, "total": 1, "has_next": False, "api_cap_reached": False,
                }), \
                     mock.patch.object(healthos, "fetch_ironbook_session_details", return_value=detail), \
                     mock.patch.object(healthos, "request_ironbook_json", return_value=stats) as fetch_stats:
                    with urllib.request.urlopen(request, timeout=3) as response:
                        page = response.read().decode("utf-8")
                    default_request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/training-legacy",
                        headers={"Cookie": f"healthos_session={session_id}"},
                    )
                    with urllib.request.urlopen(default_request, timeout=3) as response:
                        default_page = response.read().decode("utf-8")
                    new_request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/training-legacy?source=ironbook&new=1",
                        headers={"Cookie": f"healthos_session={session_id}"},
                    )
                    with urllib.request.urlopen(new_request, timeout=3) as response:
                        new_page = response.read().decode("utf-8")
                    edit_request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/training-legacy?source=ironbook&ironbook_session=71&edit=1",
                        headers={"Cookie": f"healthos_session={session_id}"},
                    )
                    with urllib.request.urlopen(edit_request, timeout=3) as response:
                        edit_page = response.read().decode("utf-8")
                self.assertIn("Ironbook", page)
                self.assertIn("/training/migration-preflight", page)
                self.assertIn("坐姿划船", page)
                self.assertIn("40 → 30", page)
                self.assertIn("12 → 10", page)
                self.assertIn("显示第 1–1 条，共 1 条", page)
                self.assertIn("训练分析", page)
                self.assertIn("每周训练趋势", page)
                self.assertIn("近 6 次平均 e1RM", page)
                self.assertIn("&lt;脚举机&gt;", page)
                self.assertNotIn("<脚举机>", page)
                fetch_stats.assert_any_call("http://ironbook.test", "/api/stats", 1.0)
                self.assertIn("<div hidden class='panel'>", page)
                self.assertIn("Ironbook", default_page)
                self.assertIn("新建训练", default_page)
                self.assertIn("<div hidden class='panel'>", default_page)
                self.assertIn("Ironbook 写回", new_page)
                self.assertIn("name='training_source' value='ironbook'", new_page)
                self.assertIn("const remoteRequired = remoteWrite ? ' required' : ''", edit_page)
                self.assertIn('class="exercise-weight" type="number" min="0" step="0.1"${remoteRequired}', edit_page)
                self.assertIn("name='session_date' value='2026-10-25'", edit_page)
                self.assertIn("name='body_part' value='背'", edit_page)
                self.assertIn("readonly aria-readonly='true'", edit_page)
                conn = healthos.init_db(server.db_path)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 0)
                conn.close()
            finally:
                healthos.destroy_session(session_id)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_ironbook_history_renders_all_sessions_from_current_api_response(self):
        with tempfile.TemporaryDirectory() as directory:
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(Path(directory) / "healthos.sqlite3")
            server.raw_dir = Path(directory) / "raw"
            server.ironbook_base_url = "http://ironbook.test"
            server.ironbook_timeout_seconds = 1
            session_id, _ = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            sessions = [{
                "id": 71 + index, "session_date": f"2026-10-{25 - (index % 25):02d}",
                "body_part": "背", "duration_minutes": 45, "total_volume_kg": 1234,
                "effective_sets": 9,
            } for index in range(67)]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/training-legacy?source=ironbook",
                    headers={"Cookie": f"healthos_session={session_id}"},
                )
                def session_page(_base, _timeout, *, offset, page_size):
                    items = sessions[offset:offset + page_size]
                    return {
                        "items": items, "offset": offset, "total": len(sessions),
                        "has_next": offset + len(items) < len(sessions), "api_cap_reached": False,
                    }

                with mock.patch.object(healthos, "fetch_ironbook_session_page", side_effect=session_page), \
                     mock.patch.object(healthos, "request_ironbook_json", return_value={}):
                    with urllib.request.urlopen(request, timeout=5) as response:
                        page = response.read().decode("utf-8")
                    next_request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/training-legacy?source=ironbook&offset=50",
                        headers={"Cookie": f"healthos_session={session_id}"},
                    )
                    with urllib.request.urlopen(next_request, timeout=5) as response:
                        second_page = response.read().decode("utf-8")
                self.assertIn("显示第 1–50 条，共 67 条", page)
                self.assertIn("offset=50", page)
                visible_session_links = re.findall(
                    r"<a href='/training\?source=ironbook&amp;ironbook_session=\d+'>查看</a>",
                    page,
                )
                self.assertEqual(len(visible_session_links), 50)
                self.assertIn("显示第 51–67 条，共 67 条", second_page)
                self.assertIn("offset=0", second_page)
            finally:
                healthos.destroy_session(session_id)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_owner_migration_preflight_is_authenticated_read_only_and_escapes_names(self):
        with tempfile.TemporaryDirectory() as directory:
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(Path(directory) / "healthos.sqlite3")
            server.raw_dir = Path(directory) / "raw"
            server.ironbook_base_url = "http://ironbook.test"
            server.ironbook_timeout_seconds = 1
            session_id, _ = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            summaries = [{"id": 71, "session_date": "2026-10-25", "body_part": "背"}]
            details = [{**summaries[0], "records": [{
                "id": 91, "raw_exercise_name": "<script>alert(1)</script>",
                "reps": [10], "sets_count": 1,
            }]}]
            base = f"http://127.0.0.1:{server.server_port}/training/migration-preflight"
            request = urllib.request.Request(
                base, headers={"Cookie": f"healthos_session={session_id}"}
            )
            history = {
                "sessions": summaries,
                "full_history_confirmed": True,
                "source_total": 1,
                "source_limit": None,
                "paginated_source": True,
            }
            try:
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *_args, **_kwargs):
                        return None

                opener = urllib.request.build_opener(NoRedirect)
                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    opener.open(base, timeout=3)
                self.assertEqual(unauthorized.exception.code, 303)
                self.assertEqual(unauthorized.exception.headers["Location"], "/login")
                with mock.patch.object(healthos, "fetch_ironbook_session_history", return_value=history), \
                     mock.patch.object(healthos, "fetch_ironbook_session_details", return_value=details):
                    with urllib.request.urlopen(request, timeout=3) as response:
                        page = response.read().decode("utf-8")
                self.assertIn("已确认覆盖当前全部训练历史", page)
                self.assertIn("需要人工映射动作组", page)
                self.assertIn("待加入动作名称", page)
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
                self.assertNotIn("<script>alert(1)</script>", page)
                self.assertIn("本检查不会导入或修改数据", page)
                conn = healthos.init_db(server.db_path)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 0)
                conn.close()
            finally:
                healthos.destroy_session(session_id)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_owner_can_confirm_fingerprint_guarded_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(Path(directory) / "healthos.sqlite3")
            server.raw_dir = Path(directory) / "raw"
            server.ironbook_base_url = "http://ironbook.test"
            server.ironbook_timeout_seconds = 1
            server.ironbook_read_only = False
            server.ironbook_migration_enabled = True
            session_id, csrf = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            snapshot = [{
                "id": 81, "session_date": "2026-10-25", "body_part": "背",
                "records": [{
                    "id": 811, "raw_exercise_name": "下拉", "standard_name": "高位下拉",
                    "body_part": "背", "weight_kg": 40, "reps": [10], "rpe": [8],
                    "sets_count": 1, "total_reps": 10, "volume_kg": 400,
                }],
            }]
            fingerprint = healthos.sha256_text(healthos.canonical_json(snapshot))
            history = {
                "sessions": [{"id": 81, "session_date": "2026-10-25", "body_part": "背"}],
                "full_history_confirmed": True, "source_total": 1,
                "source_limit": None, "paginated_source": True,
            }
            try:
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *_args, **_kwargs):
                        return None

                base = f"http://127.0.0.1:{server.server_port}"
                get_request = urllib.request.Request(
                    base + "/training/migration-preflight",
                    headers={"Cookie": f"healthos_session={session_id}"},
                )
                with mock.patch.object(healthos, "fetch_ironbook_session_history", return_value=history), \
                     mock.patch.object(healthos, "fetch_ironbook_session_details", return_value=snapshot):
                    with urllib.request.urlopen(get_request, timeout=3) as response:
                        page = response.read().decode("utf-8")
                self.assertIn("/training/migration", page)
                self.assertIn(fingerprint, page)

                form = urllib.parse.urlencode({
                    "csrf": csrf, "source_fingerprint": fingerprint, "confirmation": "确认迁移",
                })
                post_request = urllib.request.Request(
                    base + "/training/migration", data=form.encode(), method="POST",
                    headers={
                        "Cookie": f"healthos_session={session_id}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                opener = urllib.request.build_opener(NoRedirect)
                with mock.patch.object(healthos, "fetch_ironbook_session_history", return_value=history), \
                     mock.patch.object(healthos, "fetch_ironbook_session_details", return_value=snapshot):
                    with self.assertRaises(urllib.error.HTTPError) as redirect:
                        opener.open(post_request, timeout=3)
                self.assertEqual(redirect.exception.code, 303)
                self.assertIn("migrated=1", redirect.exception.headers["Location"])
                conn = healthos.init_db(server.db_path)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercise_records").fetchone()[0], 1)
                conn.close()
            finally:
                healthos.destroy_session(session_id)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_ironbook_read_only_preview_hides_and_rejects_remote_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(Path(directory) / "healthos.sqlite3")
            server.raw_dir = Path(directory) / "raw"
            server.ironbook_base_url = "http://ironbook.test"
            server.ironbook_timeout_seconds = 1
            server.ironbook_read_only = True
            server.ironbook_migration_enabled = True
            session_id, csrf = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            source = [{
                "id": 71, "session_date": "2026-10-25", "body_part": "背",
                "duration_minutes": 45, "total_volume_kg": 1234,
            }]
            detail = [{**source[0], "records": []}]
            base = f"http://127.0.0.1:{server.server_port}"
            cookie = {"Cookie": f"healthos_session={session_id}"}
            try:
                page_request = urllib.request.Request(
                    base + "/training-legacy?source=ironbook&ironbook_session=71&edit=1",
                    headers=cookie,
                )
                with mock.patch.object(healthos, "fetch_ironbook_session_page", return_value={
                    "items": source, "offset": 0, "total": 1, "has_next": False, "api_cap_reached": False,
                }), \
                     mock.patch.object(healthos, "fetch_ironbook_session_details", return_value=detail), \
                     mock.patch.object(healthos, "request_ironbook_json", return_value={}):
                    with urllib.request.urlopen(page_request, timeout=3) as response:
                        page = response.read().decode("utf-8")
                self.assertIn("Ironbook 只读预览", page)
                self.assertNotIn("action='/training/migration'", page)
                self.assertNotIn("href='/training?source=ironbook&amp;new=1'", page)
                self.assertNotIn("ironbook_session=71&amp;edit=1", page)
                self.assertNotIn("action='/training/delete'", page)

                for path, fields in (
                    ("/training", {"training_source": "ironbook", "request_id": "readonly-test-1"}),
                    ("/training/delete", {"training_source": "ironbook", "session_id": "71"}),
                    ("/training/migration", {"source_fingerprint": "0" * 64, "confirmation": "确认迁移"}),
                ):
                    body = urllib.parse.urlencode({"csrf": csrf, **fields}).encode()
                    request = urllib.request.Request(
                        base + path, data=body, headers={**cookie, "Content-Type": "application/x-www-form-urlencoded"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as rejected:
                        urllib.request.urlopen(request, timeout=3)
                    self.assertEqual(rejected.exception.code, 403)

                conn = healthos.init_db(server.db_path)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 0)
                conn.close()
            finally:
                healthos.destroy_session(session_id)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_owner_login_and_overview(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            server = healthos.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), healthos.HealthOSHandler
            )
            server.db_path = str(db)
            server.raw_dir = Path(directory) / "raw"
            server.training_demo_path = Path(__file__).resolve().parents[3] / ".local-dev" / "ironbook-aligned-training-demo.html"
            old_hash = os.environ.get("HEALTHOS_OWNER_PASSWORD_HASH")
            os.environ["HEALTHOS_OWNER_PASSWORD_HASH"] = healthos.hash_password("correct horse")
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection.request("GET", "/login")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
                response.read()

                connection.request("GET", "/training-legacy")
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader("Location"), "/login")
                response.read()

                body = urllib.parse.urlencode({"password": "correct horse"})
                connection.request("POST", "/login", body, {"Content-Type": "application/x-www-form-urlencoded"})
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                response.read()

                connection.request("GET", "/", headers={"Cookie": cookie})
                response = connection.getresponse()
                page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("健康总览", page)
                self.assertIn("健康数据", page)

                connection.request("GET", "/training-legacy", headers={"Cookie": cookie})
                response = connection.getresponse()
                training_page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("力量训练", training_page)
                self.assertIn("记录一次训练", training_page)
                self.assertIn("训练分析", training_page)
                self.assertIn("近 12 周趋势", training_page)
                self.assertIn("training-timing-json", training_page)
                self.assertIn("training-start", training_page)
                self.assertIn("training-finish", training_page)
                self.assertIn("动作名称与别名", training_page)
                csrf = re.search(r"name='csrf' value='([^']+)'", training_page).group(1)

                connection.request("GET", "/training-demo", headers={"Cookie": cookie})
                response = connection.getresponse()
                demo_page = response.read().decode("utf-8")
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader("Location"), "/training")

                body = urllib.parse.urlencode({
                    "csrf": csrf,
                    "request_id": "http-training-attempt-1",
                    "session_date": "2026-10-26",
                    "body_part": "背",
                    "duration_minutes": "45",
                    "training_timing_json": json.dumps({"started_at": "2026-10-26T08:00:00+00:00", "ended_at": "2026-10-26T08:45:00+00:00"}),
                    "records_json": json.dumps([
                        {"name": "高位下拉", "weight_kg": 50, "reps": [10], "rpe": [8]},
                        {"name": "夹胸", "weight_kg": None, "reps": [12, 10], "rpe": [], "extra_data": {"structure": "drop_set_group", "rest_seconds": 75, "segments": [{"weight_kg": 20, "reps": 12}, {"weight_kg": 15, "reps": 10}]}},
                    ], ensure_ascii=False),
                })
                connection.request("POST", "/training", body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                location = response.getheader("Location")
                response.read()
                legacy_location = location.replace("/training?", "/training-legacy?", 1)
                connection.request("GET", legacy_location, headers={"Cookie": cookie})
                response = connection.getresponse()
                detail_page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("高位下拉", detail_page)
                self.assertIn("递减组", detail_page)
                self.assertIn("20 → 15", detail_page)
                self.assertIn("12 → 10", detail_page)
                stored = healthos.init_db(db)
                stored_session = healthos.get_training_session(stored, 1)
                self.assertEqual(stored_session["extra_data"]["training_timing"]["started_at"], "2026-10-26T08:00:00+00:00")
                self.assertEqual(stored_session["records"][1]["extra_data"], {"structure": "drop_set_group", "rest_seconds": 75, "segments": [{"weight_kg": 20, "reps": 12}, {"weight_kg": 15, "reps": 10}]})
                exercise_id = stored.execute("SELECT id FROM training_exercises WHERE standard_name='高位下拉'").fetchone()[0]
                started, ended = healthos.session_timing(stored_session)
                self.assertEqual((ended - started).total_seconds(), 2700)
                stored.close()
                alias_body = urllib.parse.urlencode({
                    "csrf": csrf,
                    "exercise_id": str(exercise_id),
                    "alias_name": "宽握下拉",
                })
                connection.request("POST", "/training/aliases", alias_body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader("Location"), "/training?aliases=saved")
                response.read()
                connection.request("GET", "/training-legacy?aliases=saved", headers={"Cookie": cookie})
                response = connection.getresponse()
                alias_page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("宽握下拉", alias_page)
                response.read()
                csrf = re.search(r"name='csrf' value='([^']+)'", detail_page).group(1)
                connection.request("GET", legacy_location + "&edit=1", headers={"Cookie": cookie})
                response = connection.getresponse()
                edit_page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("编辑训练", edit_page)
                self.assertIn("name='session_id' value='1'", edit_page)
                self.assertIn("class=\"drop-set\"", edit_page)
                self.assertIn("drop_set_group", edit_page)
                edit_body = urllib.parse.urlencode({
                    "csrf": csrf,
                    "session_id": re.search(r"session=(\d+)", location).group(1),
                    "request_id": "http-training-edit-attempt-1",
                    "session_date": "2026-10-25",
                    "body_part": "背",
                    "duration_minutes": "50",
                    "records_json": json.dumps([{"name": "坐姿划船", "weight_kg": 45, "reps": [12], "rpe": [7], "extra_data": {"structure": "drop_set_group"}}], ensure_ascii=False),
                })
                connection.request("POST", "/training", edit_body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                edit_location = response.getheader("Location")
                response.read()
                legacy_edit_location = edit_location.replace("/training?", "/training-legacy?", 1)
                connection.request("GET", legacy_edit_location, headers={"Cookie": cookie})
                response = connection.getresponse()
                edited_page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("坐姿划船", edited_page)
                self.assertIn("递减组", edited_page)
                self.assertIn("删除此训练", edited_page)
                conn = healthos.init_db(db)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT standard_name FROM training_exercises e JOIN training_exercise_records r ON r.exercise_id=e.id").fetchone()[0], "坐姿划船")
                conn.close()

                connection.request("POST", "/training/delete", urllib.parse.urlencode({"csrf": "invalid", "session_id": "1"}), {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                delete_body = urllib.parse.urlencode({"csrf": csrf, "session_id": "1"})
                connection.request("POST", "/training/delete", delete_body, {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader("Location"), "/training")
                response.read()
                conn = healthos.init_db(db)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM training_exercise_records").fetchone()[0], 0)
                conn.close()

                connection.request("POST", "/training/delete", urllib.parse.urlencode({"csrf": csrf, "session_id": "1"}), {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                if old_hash is None:
                    os.environ.pop("HEALTHOS_OWNER_PASSWORD_HASH", None)
                else:
                    os.environ["HEALTHOS_OWNER_PASSWORD_HASH"] = old_hash

    def test_owner_training_production_ui_and_json_crud_are_data_backed(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "healthos.sqlite3"
            server = healthos.http.server.ThreadingHTTPServer(("127.0.0.1", 0), healthos.HealthOSHandler)
            server.db_path = str(db_path)
            server.raw_dir = Path(directory) / "raw"
            session_id, csrf = healthos.create_session()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            cookie = {"Cookie": f"healthos_session={session_id}"}
            try:
                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    urllib.request.urlopen(base + "/owner/api/training/sessions", timeout=2)
                self.assertEqual(unauthorized.exception.code, 401)

                request = urllib.request.Request(base + "/training", headers=cookie)
                with urllib.request.urlopen(request, timeout=2) as response:
                    page = response.read().decode("utf-8")
                    self.assertEqual(response.headers.get_content_type(), "text/html")
                self.assertIn('name="healthos-csrf"', page)
                self.assertIn('src="/training-ui.js"', page)
                self.assertIn('id="exerciseHints"', page)
                self.assertNotIn("sessions.unshift", page)
                script_request = urllib.request.Request(base + "/training-ui.js", headers=cookie)
                with urllib.request.urlopen(script_request, timeout=2) as response:
                    self.assertIn("text/javascript", response.headers.get("Content-Type", ""))
                    self.assertIn("owner/api/training/sessions", response.read().decode("utf-8"))

                def api(path, fields=None, method=None):
                    headers = {**cookie, "Accept": "application/json"}
                    data = None
                    if fields is not None:
                        headers["Content-Type"] = "application/x-www-form-urlencoded"
                        data = urllib.parse.urlencode(fields).encode()
                    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
                    with urllib.request.urlopen(req, timeout=3) as response:
                        return response.status, json.loads(response.read())

                create_fields = {
                    "csrf": csrf, "request_id": "ui-crud-create-01", "session_date": "2026-10-26",
                    "body_part": "胸", "duration_minutes": "42", "feeling": "测试记录",
                    "records_json": json.dumps([{
                        "name": "测试卧推", "body_part": "胸", "weight_kg": None, "reps": [], "rpe": [],
                        "sets_count": 1, "total_reps": 18, "volume_kg": 930, "exercise_order": 1,
                        "extra_data": {"structure": "drop_set_group", "rest_seconds": 60, "segments": [
                            {"weight_kg": 60, "reps": 8}, {"weight_kg": 45, "reps": 10},
                        ]},
                    }], ensure_ascii=False),
                    "training_timing_json": json.dumps({"started_at": "2026-10-26T08:00:00+00:00", "ended_at": "2026-10-26T08:42:00+00:00"}),
                }
                status, created = api("/training", create_fields, "POST")
                self.assertEqual(status, 200)
                session_key = str(created["item"]["id"])
                status, sessions = api("/owner/api/training/sessions?limit=10&offset=0")
                self.assertEqual(status, 200)
                self.assertEqual(sessions["total"], 1)
                self.assertEqual(sessions["items"][0]["total_volume_kg"], 930)
                _, filtered = api("/owner/api/training/sessions?" + urllib.parse.urlencode({"q": "测试卧推"}))
                self.assertEqual(filtered["total"], 1)
                _, detail = api(f"/owner/api/training/sessions/{session_key}")
                self.assertEqual(detail["records"][0]["extra_data"]["structure"], "drop_set_group")
                _, analytics = api("/owner/api/training/analytics")
                self.assertEqual(analytics["summary"]["total_sessions"], 1)

                exercise_id = detail["records"][0]["exercise_id"]
                _, alias = api("/training/aliases", {"csrf": csrf, "exercise_id": str(exercise_id), "alias_name": "测试推举"}, "POST")
                self.assertTrue(alias["saved"])
                _, alias_results = api("/owner/api/training/exercises?" + urllib.parse.urlencode({"q": "测试推举"}))
                self.assertEqual(alias_results["items"][0]["name"], "测试卧推")

                update_fields = {**create_fields, "request_id": "ui-crud-update-01", "session_id": session_key,
                                 "session_date": "2026-10-25", "body_part": "肩", "records_json": json.dumps([
                                     {"name": "哑铃推举", "weight_kg": 20, "reps": [10], "rpe": [7], "sets_count": 1,
                                      "exercise_order": 1, "volume_kg": 200, "total_reps": 10, "extra_data": {}}
                                 ], ensure_ascii=False)}
                _, updated = api("/training", update_fields, "POST")
                self.assertEqual(updated["item"]["id"], int(session_key))
                _, detail = api(f"/owner/api/training/sessions/{session_key}")
                self.assertEqual(detail["body_part"], "肩")
                self.assertEqual(detail["records"][0]["standard_name"], "哑铃推举")

                _, deleted = api("/training/delete", {"csrf": csrf, "session_id": session_key}, "POST")
                self.assertEqual(deleted["deleted"], int(session_key))
                _, sessions = api("/owner/api/training/sessions")
                self.assertEqual(sessions["total"], 0)
                with self.assertRaises(urllib.error.HTTPError) as invalid_csrf:
                    api("/training", {**create_fields, "csrf": "invalid"}, "POST")
                self.assertEqual(invalid_csrf.exception.code, 403)
            finally:
                healthos.destroy_session(session_id)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_query_layer_and_health_context_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            conn = healthos.init_db(db)
            seen = healthos.utc_now()
            for day, value in (("2026-10-08", 50), ("2026-10-09", 54)):
                healthos.insert_sample(
                    conn,
                    healthos.record_payload({
                        "type": "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
                        "value": str(value), "unit": "ms",
                        "startDate": f"{day}T08:00:00+00:00", "endDate": f"{day}T08:01:00+00:00",
                    }),
                    seen,
                )
            healthos.insert_lab_result(conn, {"collection_time": "2026-10-09", "test_name": "TSH", "value_num": 2.1, "unit": "mIU/L"})
            conn.commit()
            rows = healthos.query_observations(conn, metric="HKQuantityTypeIdentifierHeartRateVariabilitySDNN", from_date="2026-10-09", limit=10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["value_num"], 54.0)
            context = healthos.build_health_context(conn, {
                "from": "2026-10-08", "to": "2026-10-09",
                "domains": ["metrics"], "metrics": ["hrv"],
            })
            self.assertEqual(context["scope"]["domains"], ["metrics"])
            self.assertEqual(context["metrics"]["hrv"][0]["value"], 50.0)
            self.assertEqual(context["metrics"]["hrv"][1]["value"], 54.0)
            self.assertEqual(context["labs"], [])
            conn.close()

    def test_health_context_markdown_is_compact_and_redacted(self):
        markdown = healthos.format_health_context_markdown({
            "generated_at": "2026-10-21T00:00:00+00:00",
            "scope": {"preset": "clinical_90d", "from": "2026-07-24", "to": "2026-10-21"},
            "metrics": {"hrv": [{"date": "2026-10-20", "value": 51, "unit": "ms", "id": 99}]},
            "labs": [{"id": 7, "collection_time": "2026-10-20", "test_name": "Ferritin", "value_num": 42, "unit": "ng/mL", "flag": "normal", "reference_text": "20-200"}],
            "medications": [{"id": 3, "drug_name": "Example", "dose": 1, "dose_unit": "tablet", "frequency": "daily", "start_time": "2026-10-20"}],
            "events": [{"id": 4, "event_type": "note", "start_time": "2026-10-20", "title": "Follow-up", "notes": "Doing well"}],
        })
        self.assertIn("## Labs", markdown)
        self.assertIn("Ferritin", markdown)
        self.assertIn("## Medications", markdown)
        self.assertNotIn("99", markdown)
        self.assertNotIn("source_device", markdown)
        self.assertNotIn("generated_at", markdown)
        self.assertIn("Do not diagnose", markdown)

    def test_ephemeral_share_snapshot_is_scoped_immutable_and_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            conn = healthos.init_db(db)
            healthos.insert_lab_result(conn, {"collection_time": "2026-10-09", "test_name": "TSH", "value_num": 2.1, "unit": "mIU/L"})
            conn.commit()
            token, metadata = healthos.create_share_snapshot(conn, {"domains": ["labs"], "from": "2026-10-01", "to": "2026-10-09"}, max_reads=2)
            self.assertNotIn(token, conn.execute("SELECT token_hash FROM share_snapshots").fetchone()[0])
            _, payload = healthos.consume_share_snapshot(conn, token)
            self.assertEqual(len(payload["labs"]), 1)
            self.assertEqual(payload["metrics"], {})
            healthos.insert_lab_result(conn, {"collection_time": "2026-10-10", "test_name": "NEW", "value_num": 1})
            conn.commit()
            _, old_payload = healthos.consume_share_snapshot(conn, token)
            self.assertEqual(len(old_payload["labs"]), 1)
            with self.assertRaises(LookupError):
                healthos.consume_share_snapshot(conn, token)
            token2, _ = healthos.create_share_snapshot(conn, {"domains": ["labs"]})
            snapshot_id = conn.execute("SELECT id FROM share_snapshots WHERE token_hash=?", (healthos.sha256_text(token2),)).fetchone()[0]
            self.assertTrue(healthos.revoke_share_snapshot_id(conn, snapshot_id))
            with self.assertRaises(LookupError):
                healthos.consume_share_snapshot(conn, token2)
            conn.close()

    def test_unlimited_html_share_can_be_consumed_repeatedly(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            healthos.insert_lab_result(conn, {"collection_time": "2026-10-09", "test_name": "TSH", "value_num": 2.1, "unit": "mIU/L"})
            token, metadata = healthos.create_share_snapshot(conn, {"domains": ["labs"]}, ttl_minutes=60, max_reads=0, snapshot_format="html")
            self.assertEqual(metadata["snapshot_format"], "html")
            for _ in range(3):
                _, payload = healthos.consume_share_snapshot(conn, token)
                self.assertEqual(payload["labs"][0]["test_name"], "TSH")
            row = conn.execute("SELECT read_count,max_reads,snapshot_format FROM share_snapshots").fetchone()
            self.assertEqual(tuple(row), (3, 0, "html"))
            conn.close()

    def test_expired_share_payload_is_cleaned_but_metadata_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = healthos.init_db(Path(directory) / "healthos.sqlite3")
            token, _ = healthos.create_share_snapshot(conn, {"domains": ["labs"]}, ttl_minutes=5)
            conn.execute("UPDATE share_snapshots SET expires_at=?", ("2000-01-01T00:00:00+00:00",))
            conn.commit()
            self.assertEqual(healthos.cleanup_expired_share_payloads(conn, grace_minutes=0, batch_size=1), 1)
            row = conn.execute("SELECT token_hash,snapshot_payload FROM share_snapshots WHERE token_hash=?", (healthos.sha256_text(token),)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[1], "{}")
            conn.close()

    def test_context_and_anonymous_share_http_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "healthos.sqlite3"
            conn = healthos.init_db(db)
            healthos.insert_sample(conn, healthos.record_payload({
                "type": "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "value": "51", "unit": "ms",
                "startDate": "2026-10-09T08:00:00+00:00", "endDate": "2026-10-09T08:01:00+00:00",
            }), healthos.utc_now())
            healthos.create_training_session(conn, {
                "request_id": "api-training-context",
                "session_date": "2026-10-09",
                "body_part": "背",
                "records": [{"name": "划船", "weight_kg": 40, "reps": [10], "rpe": [8]}],
            })
            token, _ = healthos.create_share_snapshot(conn, {"domains": ["metrics"], "metrics": ["hrv"], "from": "2026-10-09", "to": "2026-10-09"}, max_reads=1)
            conn.close()
            server = healthos.http.server.ThreadingHTTPServer(("127.0.0.1", 0), healthos.HealthOSHandler)
            server.db_path = str(db)
            server.raw_dir = Path(directory) / "raw"
            old_token = os.environ.get("HEALTHOS_API_TOKEN")
            os.environ["HEALTHOS_API_TOKEN"] = "test-token"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                request = urllib.request.Request(base + "/api/v1/health/context?preset=custom&domains=metrics&metrics=hrv&from=2026-10-09&to=2026-10-09", headers={"Authorization": "Bearer test-token"})
                with urllib.request.urlopen(request, timeout=3) as response:
                    context = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                self.assertEqual(context["metrics"]["hrv"][0]["value"], 51.0)
                default_request = urllib.request.Request(
                    base + "/api/v1/health/context?preset=custom&domains=metrics&from=2026-10-09&to=2026-10-09",
                    headers={"Authorization": "Bearer test-token"},
                )
                with urllib.request.urlopen(default_request, timeout=3) as response:
                    default_context = json.loads(response.read())
                self.assertEqual(default_context["training"], [])
                training_request = urllib.request.Request(
                    base + "/api/v1/health/context?preset=custom&domains=training&from=2026-10-09&to=2026-10-09",
                    headers={"Authorization": "Bearer test-token"},
                )
                with urllib.request.urlopen(training_request, timeout=3) as response:
                    training_context = json.loads(response.read())
                self.assertEqual(training_context["training"][0]["exercises"][0]["name"], "划船")
                with urllib.request.urlopen(base + "/share/" + urllib.parse.quote(token, safe=""), timeout=3) as response:
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store, private")
                self.assertEqual(payload["scope"]["domains"], ["metrics"])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    with mock.patch.object(healthos, "cleanup_expired_share_payloads", side_effect=AssertionError("anonymous share access must not run cleanup")):
                        urllib.request.urlopen(base + "/share/" + urllib.parse.quote(token, safe=""), timeout=3)
                self.assertEqual(error.exception.code, 410)
                with self.assertRaises(urllib.error.HTTPError) as invalid_share:
                    with mock.patch.object(healthos, "cleanup_expired_share_payloads", side_effect=AssertionError("anonymous share access must not run cleanup")):
                        urllib.request.urlopen(base + "/share/not-a-valid-token", timeout=3)
                self.assertEqual(invalid_share.exception.code, 410)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                if old_token is None:
                    os.environ.pop("HEALTHOS_API_TOKEN", None)
                else:
                    os.environ["HEALTHOS_API_TOKEN"] = old_token


if __name__ == "__main__":
    unittest.main()
