-- SQLite schema for copier-bot. All timestamps are ISO8601 UTC strings.
-- See ARCHITECTURE.md §6.

CREATE TABLE IF NOT EXISTS positions (
  position_id   TEXT PRIMARY KEY,
  symbol        TEXT NOT NULL,
  direction     TEXT NOT NULL CHECK (direction IN ('buy','sell')),
  volume        REAL NOT NULL,
  open_price    REAL NOT NULL,
  open_time     TEXT NOT NULL,        -- ISO8601 UTC
  sl            REAL,
  tp            REAL,
  close_price   REAL,
  close_time    TEXT,
  profit        REAL,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  state         TEXT NOT NULL         -- open | closed | pre_existing
);

CREATE TABLE IF NOT EXISTS alerts (
  id                INTEGER PRIMARY KEY,
  position_id       TEXT NOT NULL,
  event_type        TEXT NOT NULL,    -- opened | modified | closed | pre_existing | anomaly | health
  broker_event_time TEXT,
  detected_at       TEXT NOT NULL,
  sent_at           TEXT,
  signal_age_ms     INTEGER,
  payload_json      TEXT NOT NULL,
  send_status       TEXT NOT NULL,    -- pending | sent | failed
  UNIQUE (position_id, event_type)
);

CREATE TABLE IF NOT EXISTS sizing_decisions (
  alert_id         INTEGER REFERENCES alerts(id),
  destination_lots REAL,
  protective_stop  REAL,
  risk_usd         REAL,
  utilisation_pct  REAL,
  stop_basis_points REAL,
  buffer_multiplier REAL,
  size_multiplier  REAL,
  binding_constraint TEXT,
  governor_verdict TEXT
);

CREATE TABLE IF NOT EXISTS executions (   -- manually filled in later
  alert_id     INTEGER REFERENCES alerts(id),
  acted        INTEGER,                    -- 0/1
  fill_price   REAL,
  fill_time    TEXT,
  actual_lots  REAL,
  notes        TEXT
);

CREATE TABLE IF NOT EXISTS health_heartbeat (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  last_beat     TEXT NOT NULL,
  last_snapshot TEXT
);
