"""Guarded one-shot production Ironbook to HealthOS migration runner."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import healthos


def migrate(base_url: str, db_path: Path, expected_fingerprint: str, confirmation: str) -> dict[str, object]:
    if confirmation != "执行最终迁移":
        raise ValueError("explicit final migration confirmation is required")

    history = healthos.fetch_ironbook_session_history(base_url, timeout_seconds=15.0)
    summaries = history.get("sessions")
    if not isinstance(summaries, list):
        raise RuntimeError("Ironbook history response is malformed")
    sessions = healthos.fetch_ironbook_session_details(base_url, summaries, timeout_seconds=15.0)
    fingerprint = healthos.sha256_text(healthos.canonical_json(sessions))
    if fingerprint != expected_fingerprint:
        raise RuntimeError("Ironbook changed after final preflight; stop and review a new fingerprint")
    if not history.get("full_history_confirmed") or history.get("source_total") != len(sessions):
        raise RuntimeError("Ironbook full history is not confirmed")

    conn = healthos.init_db(db_path)
    try:
        preflight = healthos.analyze_ironbook_training_migration(
            conn,
            sessions,
            source_full_history_confirmed=True,
            source_total=len(sessions),
            source_limit=history.get("source_limit"),
            paginated_source=bool(history.get("paginated_source")),
        )
        if (
            preflight["malformed_sessions"]
            or preflight["sessions"]["missing_required_fields"]
            or preflight["records"]["malformed_records"]
            or preflight["records"]["missing_required_fields"]
            or preflight["records"]["unmapped_record_count"]
            or preflight["limitations"]
        ):
            raise RuntimeError("Ironbook preflight contains malformed, unmapped, or incomplete data")

        target_counts = {
            "sessions": int(conn.execute("SELECT COUNT(1) FROM training_sessions").fetchone()[0]),
            "records": int(conn.execute("SELECT COUNT(1) FROM training_exercise_records").fetchone()[0]),
            "runs": int(conn.execute("SELECT COUNT(1) FROM migration_runs").fetchone()[0]),
        }
        if any(target_counts.values()):
            raise RuntimeError(f"HealthOS target is not empty: {target_counts}")

        backup = healthos.backup_sqlite_database(db_path)
        result = healthos.migrate_ironbook_training_sessions(
            conn,
            sessions,
            expected_fingerprint=fingerprint,
            source_full_history_confirmed=True,
            source_total=len(sessions),
            backup_path=backup,
        )

        source_session_ids = {int(session["id"]) for session in sessions}
        source_record_ids = {
            int(record["id"])
            for session in sessions
            for record in session.get("records", [])
        }
        source_exercise_ids = {
            int(record["exercise_id"])
            for session in sessions
            for record in session.get("records", [])
            if record.get("exercise_id") is not None
        }
        target_session_ids = {
            int(row[0])
            for row in conn.execute(
                "SELECT source_ironbook_id FROM training_sessions WHERE source_ironbook_id IS NOT NULL"
            )
        }
        target_record_ids = {
            int(row[0])
            for row in conn.execute(
                "SELECT source_ironbook_id FROM training_exercise_records WHERE source_ironbook_id IS NOT NULL"
            )
        }
        target_exercise_ids = {
            int(row[0])
            for row in conn.execute(
                "SELECT source_ironbook_id FROM training_exercises WHERE source_ironbook_id IS NOT NULL"
            )
        }
        run = conn.execute(
            """SELECT source_fingerprint,backup_path,sessions_seen,records_seen,
                      exercises_seen,status
               FROM migration_runs WHERE id=?""",
            (result["migration_run_id"],),
        ).fetchone()
        fk_errors = list(conn.execute("PRAGMA foreign_key_check"))
        checks = {
            "sessions_match": target_session_ids == source_session_ids,
            "records_match": target_record_ids == source_record_ids,
            "exercises_match": target_exercise_ids == source_exercise_ids,
            "audit_row_matches": bool(
                run
                and run["source_fingerprint"] == fingerprint
                and run["backup_path"] == str(backup)
                and run["sessions_seen"] == len(source_session_ids)
                and run["records_seen"] == len(source_record_ids)
                and run["exercises_seen"] == len(source_exercise_ids)
                and run["status"] == "completed"
            ),
            "foreign_key_errors": len(fk_errors),
        }
        if not all(value is True for key, value in checks.items() if key != "foreign_key_errors") or fk_errors:
            raise RuntimeError(f"Post-migration reconciliation failed: {checks}")
        return {
            "status": "completed",
            "fingerprint": fingerprint,
            "source_total": history["source_total"],
            "sessions": len(source_session_ids),
            "records": len(source_record_ids),
            "exercises": len(source_exercise_ids),
            "backup_path": str(backup),
            "migration_run_id": result["migration_run_id"],
            "checks": checks,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--expected-fingerprint", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    result = migrate(args.base_url, args.db, args.expected_fingerprint, args.confirm)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
