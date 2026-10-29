"""Read-only Ironbook integration audit and optional disposable migration drill."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import healthos


def run_audit(base_url: str, timeout: float, drill_db: str | None = None) -> dict[str, object]:
    history = healthos.fetch_ironbook_session_history(base_url, timeout)
    summaries = history.get("sessions")
    if not isinstance(summaries, list):
        raise RuntimeError("Ironbook history response is malformed")
    details = healthos.fetch_ironbook_session_details(base_url, summaries, timeout)
    fingerprint = healthos.sha256_text(healthos.canonical_json(details))

    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if drill_db:
        db_path = Path(drill_db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        temporary_directory = tempfile.TemporaryDirectory(prefix="healthos-ironbook-audit-")
        db_path = Path(temporary_directory.name) / "audit.sqlite3"

    try:
        conn = healthos.init_db(db_path)
        try:
            preflight = healthos.analyze_ironbook_training_migration(
                conn,
                details,
                source_full_history_confirmed=bool(history.get("full_history_confirmed")),
                source_total=history.get("source_total"),
                source_limit=history.get("source_limit"),
                paginated_source=bool(history.get("paginated_source")),
            )
            result: dict[str, object] | None = None
            if drill_db:
                result = healthos.migrate_ironbook_training_sessions(
                    conn,
                    details,
                    expected_fingerprint=fingerprint,
                    source_full_history_confirmed=bool(history.get("full_history_confirmed")),
                    source_total=history.get("source_total"),
                )
            return {
                "source": {
                    "base_url": base_url,
                    "full_history_confirmed": bool(history.get("full_history_confirmed")),
                    "paginated_source": bool(history.get("paginated_source")),
                    "source_total": history.get("source_total"),
                    "session_count": len(details),
                    "fingerprint": fingerprint,
                },
                "preflight": preflight,
                "drill": result,
                "drill_db": str(db_path) if drill_db else None,
            }
        finally:
            conn.close()
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Ironbook API base URL")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--drill-db",
        help="Optional disposable SQLite path for a local migration drill; never use the production DB",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    result = run_audit(args.base_url, args.timeout, args.drill_db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
