"""SQLite access: one connection per request, schema creation."""
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL CHECK (role IN ('admin', 'agent', 'user')),
    is_active            INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    failed_logins        INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT,
    password_changed_at  TEXT NOT NULL,
    last_login_at        TEXT,
    created_at           TEXT NOT NULL,
    phone                TEXT NOT NULL DEFAULT '',
    note                 TEXT NOT NULL DEFAULT '',
    can_login            INTEGER NOT NULL DEFAULT 1,  -- 0: a canvasser listed by name, never signs in
    deleted_at           TEXT,                        -- set when deleted; kept so history keeps the name
    is_owner             INTEGER NOT NULL DEFAULT 0,  -- the candidate's account (100% confirmed list); survives renames
    confirmed_access     TEXT NOT NULL DEFAULT ''     -- given by the owner: '' none, 'view', 'mark' (never downloads)
);

CREATE TABLE IF NOT EXISTS voters (
    national_id TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    sex         TEXT NOT NULL DEFAULT '' CHECK (sex IN ('M', 'F', '')),
    age         INTEGER CHECK (age IS NULL OR (age >= 0 AND age <= 130)),
    house_name  TEXT NOT NULL DEFAULT '',
    party       TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',  -- where the voter is registered to vote (ballot box)
    deceased    INTEGER NOT NULL DEFAULT 0,
    full_name_dv  TEXT NOT NULL DEFAULT '',  -- name in Dhivehi (Thaana)
    house_name_dv TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'new',
    status_at   TEXT,
    status_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    notes       TEXT NOT NULL DEFAULT '',
    photo_path  TEXT,
    source      TEXT NOT NULL DEFAULT 'import',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_voters_name  ON voters (full_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_voters_house ON voters (house_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_voters_party ON voters (party);

CREATE TABLE IF NOT EXISTS house_assignments (
    house_name  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_at TEXT NOT NULL,
    assigned_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_assign_user ON house_assignments (user_id);

-- One voter handed to one agent, overriding the house's agent. No foreign key to voters on purpose:
-- imports rewrite voter rows, and deletes clean these up themselves.
CREATE TABLE IF NOT EXISTS voter_assignments (
    national_id TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_at TEXT NOT NULL,
    assigned_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_vassign_user ON voter_assignments (user_id);

-- "100% confirmed": the candidate's own supporters. Only the 'admin' account reads or writes this
-- (auth.can_manage_confirmed_voters). Kept out of the voters table so no existing page or export
-- can show it by accident, and so imports never touch it. No foreign key, like voter_assignments.
CREATE TABLE IF NOT EXISTS confirmed_voters (
    national_id  TEXT PRIMARY KEY,
    confirmed_at TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_confirmed_at ON confirmed_voters (confirmed_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    user_id  INTEGER,
    username TEXT,
    action   TEXT NOT NULL,
    target   TEXT,
    detail   TEXT,
    ip       TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log (at);
"""


def now_iso(precise=False):
    now = datetime.now(timezone.utc)
    return now.isoformat() if precise else now.replace(microsecond=0).isoformat()


def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _allow_agent_role(conn):
    """Databases from before the agent role have CHECK (role IN ('admin', 'user')): rebuild the table."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()
    if not sql or "'agent'" in sql[0]:
        return
    new_sql = sql[0].replace("CREATE TABLE users", "CREATE TABLE users_new", 1).replace(
        "CHECK (role IN ('admin', 'user'))", "CHECK (role IN ('admin', 'agent', 'user'))")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # other tables reference users; keep their rows as they are
    try:
        conn.executescript(
            "BEGIN;"
            f"{new_sql};"
            "INSERT INTO users_new SELECT * FROM users;"
            "DROP TABLE users;"
            "ALTER TABLE users_new RENAME TO users;"
            "COMMIT;"
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_schema(conn):
    _allow_agent_role(conn)
    conn.executescript(SCHEMA)
    # Columns added after the first release
    columns = {r[1] for r in conn.execute("PRAGMA table_info(voters)")}
    for name, ddl in (
        ("phone", "phone TEXT NOT NULL DEFAULT ''"),
        ("status", "status TEXT NOT NULL DEFAULT 'new'"),
        ("status_at", "status_at TEXT"),
        ("status_by", "status_by INTEGER REFERENCES users(id) ON DELETE SET NULL"),
        ("notes", "notes TEXT NOT NULL DEFAULT ''"),
        ("location", "location TEXT NOT NULL DEFAULT ''"),
        ("deceased", "deceased INTEGER NOT NULL DEFAULT 0"),
        ("full_name_dv", "full_name_dv TEXT NOT NULL DEFAULT ''"),
        ("house_name_dv", "house_name_dv TEXT NOT NULL DEFAULT ''"),
        # Several islands per campaign (call centre); all blank for a one-island portal
        ("island", "island TEXT NOT NULL DEFAULT ''"),
        ("atoll", "atoll TEXT NOT NULL DEFAULT ''"),
        ("lives_in", "lives_in TEXT NOT NULL DEFAULT ''"),       # where the voter lives now (e.g. Male')
        ("address", "address TEXT NOT NULL DEFAULT ''"),
        ("constituency", "constituency TEXT NOT NULL DEFAULT ''"),
        ("box_code", "box_code TEXT NOT NULL DEFAULT ''"),       # ballot box code; the box name is 'location'
    ):
        if name not in columns:
            try:
                conn.execute(f"ALTER TABLE voters ADD COLUMN {ddl}")
            except sqlite3.OperationalError as exc:
                # Gunicorn workers start together: another one may have just added it
                if "duplicate column" not in str(exc):
                    raise
    conn.execute("CREATE INDEX IF NOT EXISTS ix_voters_status ON voters (status)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_voters_island ON voters (island)")
    _add_user_columns(conn)
    conn.commit()


def _add_user_columns(conn):
    """Columns added to users later. When 'can_login' first appears, existing agents become
    canvassers without sign-in (the admin updates the portal for them)."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for name, ddl in (
        ("phone", "phone TEXT NOT NULL DEFAULT ''"),
        ("note", "note TEXT NOT NULL DEFAULT ''"),
        ("can_login", "can_login INTEGER NOT NULL DEFAULT 1"),
        ("deleted_at", "deleted_at TEXT"),
        ("is_owner", "is_owner INTEGER NOT NULL DEFAULT 0"),
        ("confirmed_access", "confirmed_access TEXT NOT NULL DEFAULT ''"),
    ):
        if name in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise
            continue  # another worker added it (and switched the agents) first
        if name == "can_login":
            conn.execute("UPDATE users SET can_login = 0 WHERE role = 'agent'")
        if name == "is_owner":  # until now the owner was whoever was called 'admin'
            conn.execute("UPDATE users SET is_owner = 1 WHERE role = 'admin' AND username = 'admin' COLLATE NOCASE")
    conn.commit()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()
