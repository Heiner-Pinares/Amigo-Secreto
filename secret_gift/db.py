from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .drawing import DrawError, validate_draw


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  instructions TEXT NOT NULL DEFAULT '',
  event_date TEXT NOT NULL DEFAULT '',
  event_time TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  budget TEXT NOT NULL DEFAULT '',
  avoid_same_area INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','preview','confirmed')),
  preview_json TEXT,
  preview_hash TEXT,
  draw_id TEXT,
  confirmed_at TEXT,
  email_test_succeeded_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS participants (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  gifts TEXT NOT NULL,
  area TEXT NOT NULL DEFAULT '',
  confirmed INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(event_id, email)
);
CREATE TABLE IF NOT EXISTS restrictions (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  giver_id TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
  recipient_id TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  UNIQUE(event_id, giver_id, recipient_id)
);
CREATE TABLE IF NOT EXISTS assignments (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  draw_id TEXT NOT NULL,
  giver_id TEXT NOT NULL REFERENCES participants(id),
  recipient_id TEXT NOT NULL REFERENCES participants(id),
  idempotency_key TEXT NOT NULL UNIQUE,
  mail_status TEXT NOT NULL DEFAULT 'pending' CHECK(mail_status IN ('pending','sending','accepted','failed','unknown')),
  attempts INTEGER NOT NULL DEFAULT 0,
  provider_reference TEXT NOT NULL DEFAULT '',
  technical_error TEXT NOT NULL DEFAULT '',
  last_attempt_at TEXT,
  UNIQUE(event_id, giver_id),
  UNIQUE(event_id, recipient_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT,
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  technical_detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def clean_text(value: str) -> str:
    return " ".join(value.strip().split())


@contextmanager
def connect(path: str | Path):
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
    finally:
        conn.close()


def initialize(path: str | Path, event_name: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "email_test_succeeded_at" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN email_test_succeeded_at TEXT")
        row = conn.execute("SELECT id FROM events ORDER BY created_at LIMIT 1").fetchone()
        if row:
            return row["id"]
        event_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO events(id,name,created_at) VALUES(?,?,?)",
            (event_id, event_name, now()),
        )
        return event_id


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def add_participant(conn, event_id: str, *, name: str, email: str, gifts: str, area: str = "") -> str:
    name = clean_text(name)
    email = normalize_email(email)
    gifts = gifts.strip()
    area = clean_text(area)
    if len(name) < 2:
        raise ValueError("Ingresa el nombre completo.")
    if "@" not in email or email.startswith("@") or email.endswith("@") or len(email) > 254:
        raise ValueError("Ingresa un correo electrónico válido.")
    if len(gifts) < 2:
        raise ValueError("Describe al menos una preferencia de regalo.")
    participant_id = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO participants(id,event_id,name,email,gifts,area,created_at) VALUES(?,?,?,?,?,?,?)",
            (participant_id, event_id, name, email, gifts, area, now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("Este correo ya está registrado.") from exc
    return participant_id


def save_preview(conn, event_id: str, assignments: dict[str, str], digest: str, actor: str) -> None:
    event = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
    if not event or event["status"] == "confirmed":
        raise DrawError("El sorteo ya está confirmado.")
    participant_ids = {
        row["id"] for row in conn.execute(
            "SELECT id FROM participants WHERE event_id=? AND active=1 AND confirmed=1", (event_id,)
        )
    }
    validate_draw(assignments, participant_ids)
    conn.execute(
        "UPDATE events SET status='preview',preview_json=?,preview_hash=? WHERE id=?",
        (json.dumps(assignments, sort_keys=True), digest, event_id),
    )
    conn.execute(
        "INSERT INTO audit_log(event_id,action,actor,created_at) VALUES(?,?,?,?)",
        (event_id, "draw_preview_created", actor, now()),
    )


def confirm_preview(conn, event_id: str, actor: str, fail_after: int | None = None) -> str:
    with transaction(conn):
        event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["status"] != "preview" or not event["preview_json"]:
            raise DrawError("No hay una vista previa vigente para confirmar.")
        assignments = json.loads(event["preview_json"])
        participant_ids = {
            row["id"] for row in conn.execute(
                "SELECT id FROM participants WHERE event_id=? AND active=1 AND confirmed=1", (event_id,)
            )
        }
        validate_draw(assignments, participant_ids)
        draw_id = str(uuid.uuid4())
        for index, (giver_id, recipient_id) in enumerate(assignments.items()):
            if fail_after is not None and index >= fail_after:
                raise RuntimeError("Fallo de prueba durante la confirmación")
            conn.execute(
                "INSERT INTO assignments(id,event_id,draw_id,giver_id,recipient_id,idempotency_key) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), event_id, draw_id, giver_id, recipient_id, f"{event_id}:{draw_id}:{giver_id}"),
            )
        conn.execute(
            "UPDATE events SET status='confirmed',draw_id=?,confirmed_at=?,preview_json=NULL,preview_hash=NULL WHERE id=?",
            (draw_id, now(), event_id),
        )
        conn.execute(
            "INSERT INTO audit_log(event_id,action,actor,created_at) VALUES(?,?,?,?)",
            (event_id, "draw_confirmed", actor, now()),
        )
    return draw_id
