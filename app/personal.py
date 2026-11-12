"""Each person's own canvassing: their status and their notes for a voter.

Several people may approach the same voter on the candidate's behalf, so users and agents see only
their own status and notes. Admins see the overall picture: voters.status / status_at / status_by
hold the latest status anyone set, voters.notes the latest note, and the per-person tables below
hold everything.
"""
from flask import g

from .db import get_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS canvass (
    national_id TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    status_at   TEXT NOT NULL,
    PRIMARY KEY (national_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_canvass_user ON canvass (user_id, status);
CREATE TABLE IF NOT EXISTS voter_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    national_id TEXT NOT NULL,
    user_id     INTEGER,            -- NULL: an earlier note whose writer isn't known (admins only)
    at          TEXT NOT NULL,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notes_voter ON voter_notes (national_id, at);
CREATE INDEX IF NOT EXISTS ix_notes_user ON voter_notes (user_id, at);
"""
PERSONAL = ("status", "status_at", "status_by", "notes")


def init_schema(conn):
    """Create the tables; the first time, move the shared status and notes into them."""
    import json

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")  # workers start together: only one does the move
    try:
        fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'canvass'").fetchone() is None
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        if fresh:
            conn.execute("INSERT OR IGNORE INTO canvass (national_id, user_id, status, status_at)"
                         " SELECT national_id, status_by, status, COALESCE(status_at, updated_at) FROM voters"
                         " WHERE status != 'new' AND status_by IS NOT NULL")
            # Each existing note goes to whoever last wrote it (from the activity log)
            writer = {}
            for target, uid, at, detail in conn.execute(
                    "SELECT target, user_id, at, detail FROM audit_log WHERE action = 'voter_canvass' ORDER BY id"):
                try:
                    if "notes" in json.loads(detail or "{}"):
                        writer[target] = (uid, at)
                except ValueError:
                    pass
            for nid, notes, by, at in conn.execute(
                    "SELECT national_id, notes, status_by, COALESCE(status_at, updated_at) FROM voters"
                    " WHERE notes != ''").fetchall():
                uid, when = writer.get(nid, (by, at))
                conn.execute("INSERT INTO voter_notes (national_id, user_id, at, text) VALUES (?, ?, ?, ?)",
                             (nid, uid, when or at, notes))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _voter_columns():
    if "voter_cols" not in g:
        g.voter_cols = [r[1] for r in get_db().execute("PRAGMA table_info(voters)") if r[1] not in PERSONAL]
    return g.voter_cols


def own_source(user_id, alias="voters"):
    """The voters table as one person sees it: their own status and latest note (SQL, aliased 'voters')."""
    uid = int(user_id)
    cols = ", ".join(f"v.{c}" for c in _voter_columns())
    return (f"(SELECT {cols}, COALESCE(c.status, 'new') AS status, c.status_at AS status_at,"
            f" CASE WHEN c.status IS NOT NULL THEN {uid} END AS status_by,"
            f" COALESCE((SELECT n.text FROM voter_notes n WHERE n.national_id = v.national_id AND n.user_id = {uid}"
            "  ORDER BY n.at DESC, n.id DESC LIMIT 1), '') AS notes"
            f" FROM voters v LEFT JOIN canvass c ON c.national_id = v.national_id AND c.user_id = {uid}) AS {alias}")


def sees_everyone(user=None):
    user = user if user is not None else g.get("user")
    return user is not None and user["role"] == "admin"


def source(user=None):
    """'voters' for admins (everyone's latest), the person's own view for users and agents."""
    user = user if user is not None else g.get("user")
    return "voters" if user is None or sees_everyone(user) else own_source(user["id"])
