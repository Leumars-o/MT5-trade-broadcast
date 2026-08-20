"""Container HEALTHCHECK: exit 0 if the heartbeat in the SQLite DB is fresh,
non-zero otherwise. Usage: healthcheck.py <db_path> <max_age_seconds>

The HealthMonitor writes ``health_heartbeat.last_beat`` every ~30s, so a beat
older than ~120s means the pipeline task is wedged or dead. Stdlib only — no app
imports — so it works even if the package fails to import.
"""

import sqlite3
import sys
from datetime import datetime, timezone


def main() -> int:
    if len(sys.argv) < 3:
        return 1
    db_path, max_age = sys.argv[1], float(sys.argv[2])
    try:
        con = sqlite3.connect(db_path, timeout=5)
        row = con.execute(
            "SELECT last_beat FROM health_heartbeat WHERE id = 1"
        ).fetchone()
        con.close()
    except Exception:
        return 1
    if not row or not row[0]:
        return 1
    last = datetime.fromisoformat(row[0])
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return 0 if age <= max_age else 1


if __name__ == "__main__":
    sys.exit(main())
