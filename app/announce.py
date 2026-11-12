"""Admin announcements: write once, send to the Agents WhatsApp group in one tap, show on the portal.

WhatsApp: the portal never sends anything itself. It opens WhatsApp on the admin's phone with the
message already typed (wa.me/?text=...), the admin picks the group and presses Send. No bot, no
ban risk, and the message comes from the admin's own number.

Portal: the newest urgent announcement shows as a banner for everyone who signs in until they
press "Seen"; admins can see who has seen each one.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import audit
from .auth import admin_required, login_required
from .db import get_db, now_iso

bp = Blueprint("announce", __name__)

MAX_LEN = 1500

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    urgent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by INTEGER,
    created_by_name TEXT,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS announcement_seen (
    announcement_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (announcement_id, user_id)
)
"""


def init_schema(conn):
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()


def _local(stamp):
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when + timedelta(hours=5)).strftime("%-d %b %H:%M")


def whatsapp_text(row):
    head = "*URGENT* · MDP Gadhdhoo" if row["urgent"] else "*Announcement* · MDP Gadhdhoo"
    return f"{head}\n\n{row['text']}\n\n— {row['created_by_name'] or 'Admin'}, {_local(row['created_at'])}"


def whatsapp_url(row):
    """WhatsApp with the message typed in; the admin chooses the Agents group and presses Send."""
    return "https://wa.me/?text=" + quote(whatsapp_text(row))


def banner():
    """The newest active urgent announcement the signed-in person hasn't marked seen (or None)."""
    user = g.get("user")
    if user is None:
        return None
    return get_db().execute(
        "SELECT a.* FROM announcements a WHERE a.urgent = 1 AND a.ended_at IS NULL"
        " AND COALESCE(a.created_by, -1) != ?"
        " AND NOT EXISTS (SELECT 1 FROM announcement_seen s WHERE s.announcement_id = a.id AND s.user_id = ?)"
        " ORDER BY a.id DESC LIMIT 1", (user["id"], user["id"])).fetchone()


def _audience():
    """People who can sign in and should see announcements (everyone but the writer is counted)."""
    return get_db().execute(
        "SELECT id, username, role FROM users WHERE deleted_at IS NULL AND is_active = 1"
        " AND COALESCE(can_login, 1) = 1 ORDER BY username COLLATE NOCASE").fetchall()


@bp.route("/announcements")
@login_required
def index():
    db = get_db()
    rows = db.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT 50").fetchall()
    seen = {}
    if g.user["role"] == "admin":
        for r in db.execute("SELECT s.announcement_id, u.username, s.seen_at FROM announcement_seen s"
                            " JOIN users u ON u.id = s.user_id ORDER BY s.seen_at"):
            seen.setdefault(r["announcement_id"], []).append(r)
    else:
        rows = [r for r in rows if r["ended_at"] is None]
    audience = _audience()
    just = request.args.get("sent", type=int)
    return render_template("announce/index.html", rows=rows, seen=seen, audience=audience, just=just,
                           wa=whatsapp_url, max_len=MAX_LEN)


@bp.post("/announcements")
@admin_required
def create():
    text = (request.form.get("text") or "").strip().replace("\r\n", "\n")
    if not text:
        flash("Write the announcement first.", "error")
        return redirect(url_for("announce.index"))
    if len(text) > MAX_LEN:
        flash(f"Keep it under {MAX_LEN} characters.", "error")
        return redirect(url_for("announce.index"))
    urgent = 1 if request.form.get("urgent") else 0
    db = get_db()
    cur = db.execute("INSERT INTO announcements (text, urgent, created_at, created_by, created_by_name)"
                     " VALUES (?, ?, ?, ?, ?)", (text, urgent, now_iso(), g.user["id"], g.user["username"]))
    audit.log("announcement", target=str(cur.lastrowid), detail={"urgent": bool(urgent), "text": text[:200]},
              commit=False)
    db.commit()
    flash("Saved. Now tap “Send to WhatsApp group” and choose the Agents group.", "success")
    return redirect(url_for("announce.index", sent=cur.lastrowid) + f"#a-{cur.lastrowid}")


@bp.post("/announcements/<int:aid>/seen")
@login_required
def mark_seen(aid):
    db = get_db()
    if not db.execute("SELECT 1 FROM announcements WHERE id = ?", (aid,)).fetchone():
        abort(404)
    db.execute("INSERT OR IGNORE INTO announcement_seen (announcement_id, user_id, seen_at) VALUES (?, ?, ?)",
               (aid, g.user["id"], now_iso()))
    db.commit()
    back = request.form.get("back") or ""
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("announce.index"))


@bp.post("/announcements/<int:aid>/end")
@admin_required
def end(aid):
    db = get_db()
    cur = db.execute("UPDATE announcements SET ended_at = ? WHERE id = ? AND ended_at IS NULL", (now_iso(), aid))
    if cur.rowcount:
        audit.log("announcement_end", target=str(aid), commit=False)
    db.commit()
    flash("Taken down: it no longer shows on the portal.", "success")
    return redirect(url_for("announce.index"))
