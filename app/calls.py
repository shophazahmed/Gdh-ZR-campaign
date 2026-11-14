"""Call centre: agents phone the voters of their islands and record every call; the candidate (admin)
follows reachability per island, per agent and per day, and reads every comment.

Switched on per portal under Settings (campaign.call_center_on). A one-island portal without it is
unchanged.

Tables
  island_agents  which agents (sign-in accounts) work which islands; '' is "no island recorded"
  calls          one row per call: who, when, outcome, comment, call-back time
  call_state     one row per voter: attempts, last outcome, call-back time, done, and a short hold
                 so two agents never ring the same voter at once

Each call is mirrored into the canvassing status (voters.status, the agent's own canvass row) and
its comment into the voter's notes, so search chips, exports and the voter page stay in step.
"""
import csv
import io
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from . import audit, campaign
from .auth import admin_required, login_required
from .canvass import LOCAL_OFFSET, range_condition, read_range
from .db import get_db, now_iso
from .normalize import clean_text, split_phones

bp = Blueprint("calls", __name__, url_prefix="/calls")

OUTCOMES = {
    "willvote": "Will vote",
    "willnotvote": "Will not vote",
    "reached": "Reached",
    "noanswer": "No answer",
    "switchedoff": "Switched off",
    "wrongnumber": "Wrong number",
    "callback": "Call back later",
    "refused": "Refused",
}
# Spoke to the voter: all three count as reached; the first two also record how they'll vote
REACHED = ("reached", "willvote", "willnotvote")
REACHED_SQL = "('reached', 'willvote', 'willnotvote')"
FINAL = set(REACHED) | {"refused", "wrongnumber"}  # no more calls after these
# Canvassing status each outcome sets (the existing chips: contacted / follow up / no answer)
CANVASS_STATUS = {"reached": "contacted", "willvote": "contacted", "willnotvote": "contacted", "refused": "contacted", "callback": "followup",
                  "noanswer": "noanswer", "switchedoff": "noanswer", "wrongnumber": "noanswer"}
STATES = {
    "never": "Never tried",
    "reached": "Reached (all)",
    "willvote": "Will vote",
    "willnotvote": "Will not vote",
    "refused": "Refused",
    "wrongnumber": "Wrong number",
    "callback": "Call back booked",
    "due": "Call back due now",
    "hard": "Hard to reach",
    "trying": "Still trying",
    "nophone": "No phone number",
}
MAX_COMMENT = 1000
NO_ISLAND = "(none)"  # how '' is passed in URLs and forms

SCHEMA = """
CREATE TABLE IF NOT EXISTS island_agents (
    island      TEXT NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (island, user_id)
);
CREATE INDEX IF NOT EXISTS ix_island_agents_user ON island_agents (user_id);
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    national_id TEXT NOT NULL,
    user_id     INTEGER,
    at          TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    comment     TEXT NOT NULL DEFAULT '',
    callback_at TEXT,
    number      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_calls_voter ON calls (national_id, at);
CREATE INDEX IF NOT EXISTS ix_calls_user ON calls (user_id, at);
CREATE INDEX IF NOT EXISTS ix_calls_at ON calls (at);
CREATE TABLE IF NOT EXISTS call_state (
    national_id  TEXT PRIMARY KEY,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_outcome TEXT,
    last_at      TEXT,
    last_by      INTEGER,
    callback_at  TEXT,
    done         INTEGER NOT NULL DEFAULT 0,
    held_by      INTEGER,
    held_until   TEXT
);
CREATE INDEX IF NOT EXISTS ix_call_state_hold ON call_state (held_by, held_until);
"""


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# --- helpers -------------------------------------------------------------------------

def _iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _now():
    return datetime.now(timezone.utc)


def island_value(text):
    """URL/form value -> stored island ('(none)' is the blank island)."""
    return "" if text == NO_ISLAND else clean_text(text)[:200]


def island_label(island):
    return island or "No island recorded"


def my_islands(user=None):
    user = user or g.user
    return [r[0] for r in get_db().execute(
        "SELECT island FROM island_agents WHERE user_id = ? ORDER BY island COLLATE NOCASE", (user["id"],))]


def all_islands():
    w, p = _scope(" WHERE v.deceased = 0")
    return [r[0] for r in get_db().execute(
        f"SELECT DISTINCT v.island FROM voters v{w} ORDER BY v.island = '', v.island COLLATE NOCASE", p)]


def _scope(where, params=()):
    """Add the campaign's party scope (e.g. MDP only) to a WHERE clause on voters aliased v."""
    clause, extra = campaign.party_scope("v.party")
    if clause:
        where = f"{where} AND {clause}" if where else f" WHERE {clause}"
    return where, list(params) + list(extra)


def _require_on():
    if not campaign.call_center_on():
        abort(404)


def _can_call(voter, user=None):
    user = user or g.user
    return user["role"] == "admin" or voter["island"] in my_islands(user)


def _voter(national_id):
    w, p = _scope(" WHERE v.national_id = ?", [national_id])
    row = get_db().execute(
        f"SELECT v.*, s.attempts, s.last_outcome, s.last_at, s.callback_at, s.done, s.held_by, s.held_until"
        f" FROM voters v LEFT JOIN call_state s ON s.national_id = v.national_id{w}", p).fetchone()
    if row is None:
        abort(404)
    return row


def history(national_id):
    return get_db().execute(
        "SELECT c.*, u.username FROM calls c LEFT JOIN users u ON u.id = c.user_id"
        " WHERE c.national_id = ? ORDER BY c.at DESC, c.id DESC", (national_id,)).fetchall()


def _state_sql(max_attempts, now):
    """SQL CASE giving each voter's call state (keys of STATES); voters v LEFT JOIN call_state s."""
    return (
        "CASE WHEN v.phone = '' THEN 'nophone'"
        " WHEN s.done = 1 THEN s.last_outcome"
        f" WHEN s.callback_at IS NOT NULL AND s.callback_at <= '{now}' THEN 'due'"
        " WHEN s.callback_at IS NOT NULL THEN 'callback'"
        " WHEN COALESCE(s.attempts, 0) = 0 THEN 'never'"
        f" WHEN s.attempts >= {int(max_attempts)} THEN 'hard'"
        " ELSE 'trying' END"
    )


# --- the queue ------------------------------------------------------------------------

def next_voter(user, island=None):
    """Claim the next voter for this agent (or return the one they already hold).

    Order: their own held voter, call-backs that are due (oldest first), voters never tried, then
    voters to try again (fewest tries, longest ago). Runs in one write transaction so two agents
    can't claim the same voter.
    """
    db = get_db()
    now = _now()
    now_s = _iso(now)
    retry_before = _iso(now - timedelta(minutes=campaign.call_rule("call_retry_minutes")))
    max_attempts = campaign.call_rule("call_max_attempts")
    hold_until = _iso(now + timedelta(minutes=campaign.call_rule("call_hold_minutes")))
    if island is not None:
        islands = [island]
    elif user["role"] == "admin":
        islands = None  # every island
    else:
        islands = my_islands(user)
        if not islands:
            return None
    where = (" WHERE v.deceased = 0 AND v.phone != '' AND COALESCE(s.done, 0) = 0"
             " AND (s.held_until IS NULL OR s.held_until < ? OR s.held_by = ?)"
             " AND ((s.callback_at IS NOT NULL AND s.callback_at <= ?)"
             "   OR (s.callback_at IS NULL AND COALESCE(s.attempts, 0) = 0)"
             "   OR (s.callback_at IS NULL AND s.attempts < ? AND s.last_at <= ?))")
    params = [now_s, user["id"], now_s, max_attempts, retry_before]
    if islands is not None:
        where += f" AND v.island IN ({', '.join('?' * len(islands))})"
        params += islands
    where, params = _scope(where, params)
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT v.national_id FROM voters v LEFT JOIN call_state s ON s.national_id = v.national_id"
            f"{where} ORDER BY (COALESCE(s.held_by, 0) = ? AND COALESCE(s.held_until, '') >= ?) DESC,"
            " (s.callback_at IS NOT NULL) DESC, s.callback_at, COALESCE(s.attempts, 0), COALESCE(s.last_at, ''),"
            " v.island COLLATE NOCASE, v.house_name COLLATE NOCASE, v.full_name COLLATE NOCASE LIMIT 1",
            params + [user["id"], now_s]).fetchone()
        if row is not None:
            db.execute(
                "INSERT INTO call_state (national_id, held_by, held_until) VALUES (?, ?, ?)"
                " ON CONFLICT(national_id) DO UPDATE SET held_by = excluded.held_by, held_until = excluded.held_until",
                (row[0], user["id"], hold_until))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return row[0] if row else None


def queue_counts(user):
    """What is left for this agent: due call-backs, never tried, to retry, booked call-backs."""
    islands = None if user["role"] == "admin" else my_islands(user)
    if islands == []:
        return None
    now = _now()
    state = _state_sql(campaign.call_rule("call_max_attempts"), _iso(now))
    retry_before = _iso(now - timedelta(minutes=campaign.call_rule("call_retry_minutes")))
    where, params = " WHERE v.deceased = 0", []
    if islands is not None:
        where += f" AND v.island IN ({', '.join('?' * len(islands))})"
        params += islands
    where, params = _scope(where, params)
    row = get_db().execute(
        f"SELECT SUM(st = 'due') AS due, SUM(st = 'never') AS never,"
        f" SUM(st = 'trying' AND last_at <= ?) AS retry_now, SUM(st = 'trying' AND last_at > ?) AS retry_later,"
        f" SUM(st = 'callback') AS booked, COUNT(*) AS total"
        f" FROM (SELECT {state} AS st, s.last_at AS last_at FROM voters v"
        f" LEFT JOIN call_state s ON s.national_id = v.national_id{where})",
        [retry_before, retry_before] + params).fetchone()
    return {k: row[k] or 0 for k in row.keys()}


def my_today(user):
    row = get_db().execute(
        "SELECT COUNT(*) AS calls, SUM(outcome IN ('reached', 'willvote', 'willnotvote')) AS reached FROM calls WHERE user_id = ? AND at >= ?",
        (user["id"], _local_midnight_utc(0))).fetchone()
    return {"calls": row["calls"] or 0, "reached": row["reached"] or 0}


@bp.route("/")
@login_required
def queue():
    _require_on()
    user = g.user
    islands = all_islands() if user["role"] == "admin" else my_islands()
    pick = request.args.get("island")
    island = None
    if pick is not None and pick != "":
        island = island_value(pick)
        if island not in islands:
            abort(404)
    counts = queue_counts(user)
    if user["role"] != "admin" and not islands:
        return render_template("calls/queue.html", voter=None, no_islands=True, counts=None, today=my_today(user),
                               islands=[], island=None)
    paused = request.args.get("pause") == "1"
    nid = None if paused else next_voter(user, island)
    voter = _voter(nid) if nid else None
    return render_template(
        "calls/queue.html", voter=voter, calls=history(nid) if nid else [], outcomes=OUTCOMES,
        counts=counts, today=my_today(user), islands=islands, island=island, paused=paused,
        no_islands=False, hold_minutes=campaign.call_rule("call_hold_minutes"),
        island_label=island_label, no_island=NO_ISLAND)


def _parse_callback(text):
    """'YYYY-MM-DDTHH:MM' typed in Maldives time -> UTC ISO, or None."""
    try:
        local = datetime.strptime((text or "").strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return _iso(local.replace(tzinfo=timezone.utc) - LOCAL_OFFSET)


@bp.post("/<national_id>")
@login_required
def log_call(national_id):
    _require_on()
    voter = _voter(national_id)
    if not _can_call(voter):
        audit.log("access_denied", target=request.path)
        abort(403)
    outcome = request.form.get("outcome", "")
    if outcome not in OUTCOMES:
        abort(400)
    comment = (request.form.get("comment") or "").replace("\r\n", "\n").strip()[:MAX_COMMENT]
    callback_at = None
    if outcome == "callback":
        callback_at = _parse_callback(request.form.get("callback_at"))
        if callback_at is None:
            # No time picked: try again after the usual retry gap
            callback_at = _iso(_now() + timedelta(minutes=max(30, campaign.call_rule("call_retry_minutes"))))
    number = request.form.get("number", "")
    number = number if number in split_phones(voter["phone"]) else ""
    record(voter, g.user, outcome, comment, callback_at, number)
    flash(f"Saved: {voter['full_name']} · {OUTCOMES[outcome]}.", "success")
    back = request.form.get("back", "")
    if back.startswith("/") and not back.startswith("//"):
        return redirect(back)
    args = {"island": request.form["island"]} if request.form.get("island") else {}
    if request.form.get("then") == "pause":
        args["pause"] = "1"
    return redirect(url_for("calls.queue", **args))


def record(voter, user, outcome, comment="", callback_at=None, number=""):
    db = get_db()
    nid = voter["national_id"]
    stamp = now_iso()
    db.execute("INSERT INTO calls (national_id, user_id, at, outcome, comment, callback_at, number)"
               " VALUES (?, ?, ?, ?, ?, ?, ?)", (nid, user["id"], stamp, outcome, comment, callback_at, number))
    db.execute(
        "INSERT INTO call_state (national_id, attempts, last_outcome, last_at, last_by, callback_at, done)"
        " VALUES (?, 1, ?, ?, ?, ?, ?) ON CONFLICT(national_id) DO UPDATE SET attempts = attempts + 1,"
        " last_outcome = excluded.last_outcome, last_at = excluded.last_at, last_by = excluded.last_by,"
        " callback_at = excluded.callback_at, done = excluded.done, held_by = NULL, held_until = NULL",
        (nid, outcome, stamp, user["id"], callback_at, 1 if outcome in FINAL else 0))
    # Mirror into the canvassing status and notes
    status = CANVASS_STATUS[outcome]
    db.execute("INSERT INTO canvass (national_id, user_id, status, status_at) VALUES (?, ?, ?, ?)"
               " ON CONFLICT(national_id, user_id) DO UPDATE SET status = excluded.status,"
               " status_at = excluded.status_at", (nid, user["id"], status, stamp))
    db.execute("UPDATE voters SET status = ?, status_at = ?, status_by = ? WHERE national_id = ?",
               (status, stamp, user["id"], nid))
    if comment:
        text = f"📞 {OUTCOMES[outcome]}: {comment}"
        db.execute("INSERT INTO voter_notes (national_id, user_id, at, text) VALUES (?, ?, ?, ?)",
                   (nid, user["id"], stamp, text))
        db.execute("UPDATE voters SET notes = ? WHERE national_id = ?", (text, nid))
    audit.log("call", target=nid, detail={"outcome": outcome, "comment": comment, "callback_at": callback_at},
              commit=False)
    db.commit()


@bp.post("/<national_id>/release")
@login_required
def release(national_id):
    """Give the voter back to the queue (the agent stops for now)."""
    _require_on()
    db = get_db()
    db.execute("UPDATE call_state SET held_by = NULL, held_until = NULL WHERE national_id = ? AND held_by = ?",
               (national_id, g.user["id"]))
    db.commit()
    return redirect(url_for("calls.queue", pause="1"))


@bp.route("/voter/<national_id>")
@login_required
def voter_page(national_id):
    """One voter's calls, and a form to log another (e.g. the voter rang back)."""
    _require_on()
    voter = _voter(national_id)
    if not _can_call(voter):
        abort(404)
    return render_template("calls/voter.html", voter=voter, calls=history(national_id), outcomes=OUTCOMES,
                           island_label=island_label, back=request.args.get("back", ""))


@bp.post("/voter/<national_id>/reopen")
@admin_required
def reopen(national_id):
    """Put a finished voter (reached, refused, wrong number) back in the queue."""
    _require_on()
    _voter(national_id)
    db = get_db()
    db.execute("UPDATE call_state SET done = 0, attempts = 0, callback_at = NULL WHERE national_id = ?",
               (national_id,))
    audit.log("call_reopen", target=national_id, commit=False)
    db.commit()
    flash("Back in the call queue.", "success")
    return redirect(url_for("calls.voter_page", national_id=national_id))


# --- islands and agents (admin) --------------------------------------------------------

def _call_agents():
    """Sign-in accounts that can take calls: agents and users (admins can always call)."""
    return get_db().execute(
        "SELECT id, username, role, is_active FROM users WHERE role IN ('agent', 'user') AND can_login = 1"
        " AND deleted_at IS NULL ORDER BY is_active DESC, username COLLATE NOCASE").fetchall()


@bp.route("/islands", methods=["GET", "POST"])
@admin_required
def islands():
    _require_on()
    db = get_db()
    agents = _call_agents()
    if request.method == "POST":
        island = island_value(request.form.get("island", ""))
        valid = {a["id"] for a in agents}
        chosen = {int(x) for x in request.form.getlist("agents") if x.isdigit() and int(x) in valid}
        before = {r[0] for r in db.execute("SELECT user_id FROM island_agents WHERE island = ?", (island,))}
        db.execute("DELETE FROM island_agents WHERE island = ?", (island,))
        db.executemany("INSERT INTO island_agents (island, user_id, assigned_at) VALUES (?, ?, ?)",
                       [(island, uid, now_iso()) for uid in sorted(chosen)])
        names = {a["id"]: a["username"] for a in agents}
        audit.log("island_agents", target=island_label(island),
                  detail={"added": [names[u] for u in chosen - before], "removed": [names.get(u, u) for u in before - chosen]},
                  commit=False)
        db.commit()
        flash(f"{island_label(island)}: {len(chosen)} agent{'s' if len(chosen) != 1 else ''}.", "success")
        return redirect(url_for("calls.islands") + f"#isl-{_anchor(island)}")
    w, p = _scope(" WHERE v.deceased = 0")
    rows = db.execute(
        f"SELECT v.island AS island, COUNT(*) AS voters, SUM(v.phone != '') AS with_phone FROM voters v{w}"
        " GROUP BY v.island ORDER BY v.island = '', v.island COLLATE NOCASE", p).fetchall()
    assigned = {}
    for island, uid in db.execute("SELECT island, user_id FROM island_agents"):
        assigned.setdefault(island, set()).add(uid)
    return render_template("calls/islands.html", rows=rows, agents=agents, assigned=assigned,
                           island_label=island_label, no_island=NO_ISLAND, anchor=_anchor)


def _anchor(island):
    return "".join(ch if ch.isalnum() else "-" for ch in (island or "none")).lower()


# --- dashboard (admin) -----------------------------------------------------------------

def island_stats(islands=None):
    """Reachability per island; `islands` limits it (an agent's own islands)."""
    now = _iso(_now())
    state = _state_sql(campaign.call_rule("call_max_attempts"), now)
    where, params = " WHERE v.deceased = 0", []
    if islands is not None:
        marks = ", ".join("?" * len(islands)) or "NULL"
        where += f" AND v.island IN ({marks})"
        params += list(islands)
    w, p = _scope(where, params)
    rows = get_db().execute(
        f"SELECT island, COUNT(*) AS total, SUM(st != 'nophone') AS with_phone, SUM(st = 'nophone') AS nophone,"
        f" SUM(tried) AS tried, SUM(st IN {REACHED_SQL}) AS reached, SUM(st = 'willvote') AS willvote,"
        " SUM(st = 'willnotvote') AS willnotvote, SUM(st = 'refused') AS refused,"
        " SUM(st = 'wrongnumber') AS wrongnumber, SUM(st = 'hard') AS hard, SUM(st IN ('callback', 'due')) AS callback,"
        " SUM(st = 'due') AS due, SUM(st = 'never') AS never, SUM(st = 'trying') AS trying"
        f" FROM (SELECT v.island AS island, {state} AS st, COALESCE(s.attempts, 0) > 0 AS tried"
        f" FROM voters v LEFT JOIN call_state s ON s.national_id = v.national_id{w})"
        " GROUP BY island ORDER BY island = '', island COLLATE NOCASE", p).fetchall()
    out = []
    for r in rows:
        d = {k: (r[k] or 0) if k != "island" else r[k] for k in r.keys()}
        d["pct"] = round(100 * d["reached"] / d["total"]) if d["total"] else 0
        d["spoke"] = d["reached"] - d["willvote"] - d["willnotvote"]   # reached, intent not recorded
        d["tried_pct"] = round(100 * d["tried"] / d["total"]) if d["total"] else 0
        out.append(d)
    total = {k: sum(r[k] for r in out) for k in out[0] if k not in ("island", "pct", "tried_pct")} if out else {}
    for d in out + ([total] if total else []):
        d["vote_pct"] = round(100 * d["willvote"] / d["reached"]) if d["reached"] else 0
    if total:
        total["pct"] = round(100 * total["reached"] / total["total"]) if total["total"] else 0
        total["tried_pct"] = round(100 * total["tried"] / total["total"]) if total["total"] else 0
    return out, total


def _local_midnight_utc(days_ago=0):
    local = datetime.now(timezone.utc) + LOCAL_OFFSET - timedelta(days=days_ago)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return _iso(start.replace(tzinfo=timezone.utc) - LOCAL_OFFSET)


def agent_stats():
    today, week = _local_midnight_utc(0), _local_midnight_utc(6)
    return get_db().execute(
        "SELECT u.id, u.username, u.is_active,"
        " (SELECT GROUP_CONCAT(CASE WHEN island = '' THEN 'No island' ELSE island END, ', ')"
        "    FROM island_agents ia WHERE ia.user_id = u.id) AS islands,"
        " SUM(c.at >= ?) AS calls_today, SUM(c.at >= ? AND c.outcome IN ('reached', 'willvote', 'willnotvote')) AS reached_today,"
        " SUM(c.at >= ?) AS calls_week, SUM(c.at >= ? AND c.outcome IN ('reached', 'willvote', 'willnotvote')) AS reached_week,"
        " COUNT(c.id) AS calls_all, MAX(c.at) AS last_at"
        " FROM users u LEFT JOIN calls c ON c.user_id = u.id"
        " WHERE u.deleted_at IS NULL AND (u.id IN (SELECT user_id FROM island_agents) OR u.id IN (SELECT user_id FROM calls))"
        " GROUP BY u.id ORDER BY calls_today DESC, calls_week DESC, u.username COLLATE NOCASE",
        (today, today, week, week)).fetchall()


def daily(days=14):
    start = _local_midnight_utc(days - 1)
    rows = dict((r[0], (r[1], r[2])) for r in get_db().execute(
        "SELECT date(at, '+5 hours') AS day, COUNT(*), SUM(outcome IN ('reached', 'willvote', 'willnotvote')) FROM calls WHERE at >= ?"
        " GROUP BY day", (start,)))
    today = (datetime.now(timezone.utc) + LOCAL_OFFSET).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        calls, reached = rows.get(d.isoformat(), (0, 0))
        out.append({"day": d, "calls": calls or 0, "reached": reached or 0})
    peak = max([r["calls"] for r in out] + [1])
    for r in out:
        r["h_calls"], r["h_reached"] = round(100 * r["calls"] / peak), round(100 * r["reached"] / peak)
    return out


@bp.route("/dashboard")
@admin_required
def dashboard():
    _require_on()
    rows, total = island_stats()
    return render_template("calls/dashboard.html", rows=rows, total=total, agents=agent_stats(), days=daily(),
                           recent=comment_rows({"only": "1"}, limit=8)[0], outcomes=OUTCOMES,
                           max_attempts=campaign.call_rule("call_max_attempts"), island_label=island_label,
                           no_island=NO_ISLAND)


@bp.route("/mine")
@login_required
def mine():
    """An agent's or user's own dashboard: their islands' progress, their calls, their latest comments."""
    _require_on()
    user = g.user
    if user["role"] == "admin":
        return redirect(url_for("calls.dashboard"))
    islands = my_islands()
    rows, total = island_stats(islands) if islands else ([], {})
    db = get_db()
    today, week = _local_midnight_utc(0), _local_midnight_utc(6)
    me = db.execute(
        "SELECT SUM(at >= ?) AS calls_today, SUM(at >= ? AND outcome IN ('reached', 'willvote', 'willnotvote')) AS reached_today,"
        " SUM(at >= ?) AS calls_week, SUM(at >= ? AND outcome IN ('reached', 'willvote', 'willnotvote')) AS reached_week,"
        " COUNT(*) AS calls_all, SUM(outcome IN ('reached', 'willvote', 'willnotvote')) AS reached_all,"
        " SUM(outcome = 'willvote') AS willvote_all, SUM(outcome = 'willnotvote') AS willnotvote_all FROM calls WHERE user_id = ?",
        (today, today, week, week, user["id"])).fetchone()
    recent = db.execute(
        "SELECT c.*, v.full_name, v.island FROM calls c JOIN voters v ON v.national_id = c.national_id"
        " WHERE c.user_id = ? ORDER BY c.at DESC, c.id DESC LIMIT 20", (user["id"],)).fetchall()
    return render_template("calls/mine.html", rows=rows, total=total, me={k: me[k] or 0 for k in me.keys()},
                           recent=recent, outcomes=OUTCOMES, islands=islands, counts=queue_counts(user),
                           max_attempts=campaign.call_rule("call_max_attempts"), island_label=island_label)


# --- comments feed and call log (admin) --------------------------------------------------

def read_comment_filters(args):
    return {
        "island": args.get("island", ""),
        "agent": args.get("agent", "") if args.get("agent", "").isdigit() else "",
        "outcome": args.get("outcome", "") if args.get("outcome", "") in OUTCOMES else "",
        "only": "0" if args.get("only") == "0" else "1",   # only calls with a comment
        "q": clean_text(args.get("q", ""))[:100],
        "from": args.get("from", ""), "to": args.get("to", ""),
    }


def comment_rows(f, limit=100, offset=0):
    rng = read_range(f)
    parts, params = range_condition(rng, "c.at")
    if f.get("island"):
        parts.append("v.island = ?")
        params.append(island_value(f["island"]))
    if f.get("agent"):
        parts.append("c.user_id = ?")
        params.append(int(f["agent"]))
    if f.get("outcome"):
        parts.append("c.outcome = ?")
        params.append(f["outcome"])
    if f.get("only", "1") == "1":
        parts.append("c.comment != ''")
    if f.get("q"):
        parts.append("(c.comment LIKE ? OR v.full_name LIKE ? OR c.national_id LIKE ?)")
        params += [f"%{f['q']}%"] * 3
    where, params = _scope((" WHERE " + " AND ".join(parts)) if parts else "", params)
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM calls c JOIN voters v ON v.national_id = c.national_id{where}",
                       params).fetchone()[0]
    rows = db.execute(
        "SELECT c.*, u.username, v.full_name, v.island, v.house_name, v.lives_in, v.phone AS voter_phone"
        f" FROM calls c JOIN voters v ON v.national_id = c.national_id LEFT JOIN users u ON u.id = c.user_id{where}"
        " ORDER BY c.at DESC, c.id DESC" + (f" LIMIT {int(limit)} OFFSET {int(offset)}" if limit else ""),
        params).fetchall()
    return rows, total, rng


@bp.route("/comments")
@admin_required
def comments():
    _require_on()
    f = read_comment_filters(request.args)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    rows, total, rng = comment_rows(f, limit=100, offset=(page - 1) * 100)
    agents = get_db().execute("SELECT DISTINCT u.id, u.username FROM users u JOIN calls c ON c.user_id = u.id"
                              " ORDER BY u.username COLLATE NOCASE").fetchall()
    args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("calls/comments.html", rows=rows, total=total, f=f, rng=rng, page=page,
                           pages=max(1, -(-total // 100)), islands=all_islands(), agents=agents, outcomes=OUTCOMES,
                           args=args, island_label=island_label, no_island=NO_ISLAND)


def _cell(value):
    """Keep spreadsheet formulas out of exported text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _local_text(iso):
    if not iso:
        return ""
    try:
        return (datetime.fromisoformat(iso) + LOCAL_OFFSET).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


@bp.route("/export.csv")
@admin_required
def export():
    _require_on()
    f = read_comment_filters(request.args)
    rows, total, _ = comment_rows(f, limit=0)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Time (Maldives)", "ID", "Name", "Island", "Lives in", "House", "Agent", "Outcome", "Comment",
                "Call back at", "Number called"])
    for r in rows:
        w.writerow([_local_text(r["at"]), r["national_id"], _cell(r["full_name"]), _cell(r["island"]),
                    _cell(r["lives_in"]), _cell(r["house_name"]), _cell(r["username"] or ""),
                    OUTCOMES.get(r["outcome"], r["outcome"]), _cell(r["comment"]), _local_text(r["callback_at"]),
                    r["number"]])
    audit.log("export", target="calls", detail={"filters": {k: v for k, v in f.items() if v}, "rows": total})
    name = f"calls-{datetime.now().strftime('%Y%m%d')}.csv"
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --- voters by call state (admin drill-down from the dashboard) -----------------------------

@bp.route("/voters")
@admin_required
def by_state():
    _require_on()
    state = request.args.get("state", "")
    if state not in STATES:
        abort(404)
    island = request.args.get("island")
    now = _iso(_now())
    st = _state_sql(campaign.call_rule("call_max_attempts"), now)
    where, params = " WHERE v.deceased = 0", []
    if island is not None:
        where += " AND v.island = ?"
        params.append(island_value(island))
    where, params = _scope(where, params)
    rows = get_db().execute(
        f"SELECT * FROM (SELECT v.national_id, v.full_name, v.island, v.house_name, v.lives_in, v.phone,"
        f" s.attempts, s.last_outcome, s.last_at, s.callback_at, {st} AS st,"
        " (SELECT comment FROM calls c WHERE c.national_id = v.national_id AND c.comment != ''"
        "  ORDER BY c.at DESC LIMIT 1) AS last_comment"
        f" FROM voters v LEFT JOIN call_state s ON s.national_id = v.national_id{where})"
        f" WHERE {'st IN ' + REACHED_SQL if state == 'reached' else 'st = ?'}"
        " ORDER BY island COLLATE NOCASE, house_name COLLATE NOCASE, full_name COLLATE NOCASE",
        params + ([] if state == "reached" else [state])).fetchall()
    if request.args.get("format") == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["ID", "Name", "Island", "Lives in", "House", "Phone", "Tries", "Last outcome", "Last call",
                    "Call back at", "Last comment"])
        for r in rows:
            w.writerow([r["national_id"], _cell(r["full_name"]), _cell(r["island"]), _cell(r["lives_in"]),
                        _cell(r["house_name"]), r["phone"], r["attempts"] or 0,
                        OUTCOMES.get(r["last_outcome"] or "", ""), _local_text(r["last_at"]),
                        _local_text(r["callback_at"]), _cell(r["last_comment"] or "")])
        audit.log("export", target=f"calls:{state}", detail={"island": island, "rows": len(rows)})
        return Response("﻿" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="calls-{state}.csv"'})
    return render_template("calls/by_state.html", rows=rows, state=state, states=STATES, island=island,
                           outcomes=OUTCOMES, island_label=island_label, no_island=NO_ISLAND)
