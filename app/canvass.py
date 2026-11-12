"""Canvassing progress: the dashboard, an agent's own houses, and assigning houses to agents."""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import audit, campaign
from . import personal as own_view  # (canvass.personal() is a function here)
from .auth import role_required
from .db import get_db, now_iso

bp = Blueprint("canvass", __name__)

LOCAL_OFFSET = timedelta(hours=5)  # Maldives time, no daylight saving
REACHED = ("contacted", "followup", "noanswer")
STATUS_LABELS = {"new": "Not contacted", "contacted": "Contacted", "followup": "Follow up", "noanswer": "No answer"}
SORTS = {
    "progress": "reached * 1.0 / total, total DESC, house",
    "remaining": "(total - reached) DESC, house",
    "name": "house COLLATE NOCASE",
    "voters": "total DESC, house",
    "recent": "last_at IS NULL, last_at DESC",
}

# {c} limits what counts as reached (a date range, one person); "1" counts every status
HOUSE_STATS = """
SELECT v.house_name AS house,
       COUNT(*) AS total,
       SUM(v.status = 'contacted' AND {c}) AS contacted,
       SUM(v.status = 'followup' AND {c}) AS followup,
       SUM(v.status = 'noanswer' AND {c}) AS noanswer,
       SUM(v.status != 'new' AND {c}) AS reached,
       MAX(CASE WHEN {c} THEN v.status_at END) AS last_at,
       a.user_id AS agent_id,
       u.username AS agent,
       u.is_active AS agent_active
FROM {src}
LEFT JOIN house_assignments a ON a.house_name = v.house_name
LEFT JOIN users u ON u.id = a.user_id
"""


def local_day_start_utc():
    local_now = datetime.now(timezone.utc) + LOCAL_OFFSET
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start_local - LOCAL_OFFSET).isoformat(timespec="seconds")


def house_rows(where="", params=(), sort="progress", count_if=("1", []), having="", src="voters v"):
    """src: 'voters v' (everyone's latest status) or own_view.own_source(id, 'v') (one person's own)."""
    clause, extra = campaign.party_scope("v.party")
    if clause:
        where, params = f"{where} AND {clause}", list(params) + extra
    cond, cond_params = count_if
    sql = (f"{HOUSE_STATS.format(c=f'({cond})', src=src)} WHERE v.house_name != '' {where} GROUP BY v.house_name"
           f" {having} ORDER BY {SORTS[sort]}")
    return get_db().execute(sql, list(cond_params) * 5 + list(params)).fetchall()


def _parse_day(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def read_range(args):
    """Date range from ?from=YYYY-MM-DD&to=YYYY-MM-DD (Maldives days, both ends included)."""
    start, end = _parse_day(args.get("from")), _parse_day(args.get("to"))
    if start and end and start > end:
        start, end = end, start
    to_utc = lambda d: (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - LOCAL_OFFSET).isoformat(  # noqa: E731
        timespec="seconds")
    today = (datetime.now(timezone.utc) + LOCAL_OFFSET).date()
    presets = [("Today", today, today), ("Last 7 days", today - timedelta(days=6), today),
               ("Last 30 days", today - timedelta(days=29), today)]
    label = ""
    if start or end:
        fmt = lambda d: d.strftime("%-d %b")  # noqa: E731
        label = (fmt(start) if start == end else
                 f"{fmt(start) if start else 'start'} – {fmt(end) if end else 'today'}")
        label = next((name for name, a, b in presets if (a, b) == (start, end)), label)
    return {
        "from": start.isoformat() if start else "", "to": end.isoformat() if end else "",
        "start_utc": to_utc(start) if start else None,
        "end_utc": to_utc(end + timedelta(days=1)) if end else None,
        "label": label,
        "presets": [(name, a.isoformat(), b.isoformat()) for name, a, b in presets],
    }


def range_condition(rng, column="v.status_at"):
    parts, params = [], []
    if rng["start_utc"]:
        parts.append(f"{column} >= ?")
        params.append(rng["start_utc"])
    if rng["end_utc"]:
        parts.append(f"{column} < ?")
        params.append(rng["end_utc"])
    return parts, params


def totals(rows):
    t = {"houses": len(rows), "total": 0, "contacted": 0, "followup": 0, "noanswer": 0, "reached": 0,
         "done_houses": 0}
    for r in rows:
        for k in ("total", "contacted", "followup", "noanswer", "reached"):
            t[k] += r[k] or 0
        t["done_houses"] += 1 if r["total"] and r["reached"] == r["total"] else 0
    t["new"] = t["total"] - t["reached"]
    t["pct"] = round(100 * t["reached"] / t["total"]) if t["total"] else 0
    return t


def activity(user_id=None, rng=None):
    db = get_db()
    if rng and (rng["start_utc"] or rng["end_utc"]):
        parts, params = range_condition(rng, "at")
        if user_id:
            parts.append("user_id = ?")
            params.append(user_id)
        cond = "".join(f" AND {p}" for p in parts)
        n = db.execute(f"SELECT COUNT(*) FROM audit_log WHERE action = 'voter_canvass'{cond}", params).fetchone()[0]
        leaders = db.execute(
            "SELECT username, COUNT(*) AS n, MAX(at) AS last FROM audit_log WHERE action = 'voter_canvass'"
            f"{''.join(f' AND {p}' for p in range_condition(rng, 'at')[0])} GROUP BY username ORDER BY n DESC LIMIT 10",
            range_condition(rng, "at")[1]).fetchall()
        return {"in_range": n, "leaders": leaders}
    since_today = local_day_start_utc()
    since_week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    cond, params = ("AND user_id = ?", [user_id]) if user_id else ("", [])
    count = lambda since: db.execute(  # noqa: E731
        f"SELECT COUNT(*) FROM audit_log WHERE action = 'voter_canvass' AND at >= ? {cond}", [since] + params
    ).fetchone()[0]
    leaders = db.execute(
        "SELECT username, COUNT(*) AS n, MAX(at) AS last FROM audit_log"
        " WHERE action = 'voter_canvass' AND at >= ? GROUP BY username ORDER BY n DESC LIMIT 10",
        (since_week,),
    ).fetchall()
    return {"today": count(since_today), "week": count(since_week), "leaders": leaders}


@bp.route("/dashboard")
@role_required("admin")
def dashboard():
    db = get_db()
    party = request.args.get("party", "")
    agent = request.args.get("agent", "")
    who = request.args.get("user", "")
    who = who if who.isdigit() else ""
    rng = read_range(request.args)
    sort = request.args.get("sort", "progress")
    if sort not in SORTS:
        sort = "progress"
    # What counts as reached: changed in the date range, and by that person
    parts, cond_params = range_condition(rng)
    if who:
        parts.append("v.status_by = ?")
        cond_params.append(int(who))
    count_if = (" AND ".join(parts) or "1", cond_params)
    where, params = "", []
    if party:
        where += " AND v.party = ?"
        params.append(party)
    if agent == "none":
        where += " AND a.user_id IS NULL"
    elif agent.isdigit():
        where += " AND a.user_id = ?"
        params.append(int(agent))
    # Looking at one person: only the houses where they reached someone
    rows = house_rows(where, params, sort, count_if, having="HAVING reached > 0" if who else "")
    clause, extra = campaign.party_scope()
    scope = f" AND {clause}" if clause else ""
    parties = [r[0] for r in db.execute(
        f"SELECT DISTINCT party FROM voters WHERE party != ''{scope} ORDER BY party", extra)]
    agents = db.execute("SELECT id, username FROM users WHERE role IN ('agent', 'user', 'admin') AND deleted_at IS NULL ORDER BY username").fetchall()
    people = db.execute("SELECT id, username, role FROM users ORDER BY username COLLATE NOCASE").fetchall()
    who_name = next((u["username"] for u in people if str(u["id"]) == who), "")
    no_house = db.execute(f"SELECT COUNT(*) FROM voters WHERE house_name = ''{scope}", extra).fetchone()[0]
    return render_template(
        "canvass/dashboard.html", rows=rows, t=totals(rows), act=activity(int(who) if who else None, rng),
        parties=parties, agents=agents, people=people, who=who, who_name=who_name, rng=rng,
        party=party, agent=agent, sort=sort, sorts=SORTS, no_house=no_house,
        leaders=activity(None, rng)["leaders"],
    )


def personal(user_id, rng=None):
    """One person's own canvassing: voters they last marked as reached, and their latest ones."""
    from .voters import scoped  # here to avoid a circular import at load time

    db = get_db()
    parts, extra = range_condition(rng, "status_at") if rng else ([], [])
    w, p = scoped(" WHERE status != 'new'" + "".join(f" AND {x}" for x in parts), extra)
    src = own_view.own_source(user_id)  # their own status for every voter they reached
    mine = db.execute(
        "SELECT COUNT(*) AS total, SUM(status = 'contacted') AS contacted, SUM(status = 'followup') AS followup,"
        f" SUM(status = 'noanswer') AS noanswer FROM {src}{w}", p).fetchone()
    recent = db.execute(
        f"SELECT national_id, full_name, house_name, status, status_at FROM {src}{w}"
        " ORDER BY status_at DESC LIMIT 8", p).fetchall()
    return mine, recent


@bp.route("/my")
@role_required("agent", "admin", "user")
def my_houses():
    """Everyone's own dashboard (admins have the full one): their canvassing, and for agents their houses.
    Admins can open anyone's with ?agent=<id>."""
    user = g.user
    if g.user["role"] == "admin":
        agent_id = request.args.get("agent", type=int)
        if agent_id:
            user = get_db().execute("SELECT * FROM users WHERE id = ?", (agent_id,)).fetchone() or abort(404)
    sort = request.args.get("sort", "progress")
    if sort not in SORTS:
        sort = "progress"
    # Voters in their houses that were handed to another agent one by one aren't theirs
    rows = house_rows(" AND a.user_id = ? AND v.national_id NOT IN"
                      " (SELECT national_id FROM voter_assignments WHERE user_id != ?)",
                      [user["id"], user["id"]], sort, src=own_view.own_source(user["id"], "v"))
    t = totals(rows)
    clause, extra = campaign.party_scope("v.party")
    singles = get_db().execute(
        "SELECT COUNT(*) AS total, SUM(v.status != 'new') AS reached, SUM(v.status = 'contacted') AS contacted,"
        " SUM(v.status = 'followup') AS followup, SUM(v.status = 'noanswer') AS noanswer"
        f" FROM voter_assignments va JOIN {own_view.own_source(user['id'], 'v')} ON v.national_id = va.national_id"
        f" WHERE va.user_id = ?{' AND ' + clause if clause else ''}", [user["id"]] + extra).fetchone()
    # Suggested next stop: follow-ups first (someone asked us to come back), then the house
    # with the most voters nobody has reached yet.
    open_rows = [r for r in rows if r["reached"] < r["total"] or r["followup"]]
    suggestion = None
    if open_rows:
        suggestion = max(open_rows, key=lambda r: (r["followup"] or 0, r["total"] - r["reached"], -len(r["house"])))
    rng = read_range(request.args)
    mine, recent = personal(user["id"], rng)
    return render_template("canvass/my.html", rows=rows, t=t, act=activity(user["id"], rng), agent=user, singles=singles,
                           rng=rng,
                           mine=mine, recent=recent, statuses=STATUS_LABELS,
                           suggestion=suggestion, sort=sort, viewing_other=user["id"] != g.user["id"])


# --- admin: agents and house assignments ---------------------------------------------

def _agents(active_only=False):
    cond = " AND u.is_active = 1" if active_only else ""
    clause, extra = campaign.party_scope("v.party")
    join_scope = f" AND {clause}" if clause else ""
    return get_db().execute(
        f"""SELECT u.id, u.username, u.is_active, u.last_login_at, u.role, u.phone, u.can_login,
                   (SELECT COUNT(*) FROM voter_assignments va WHERE va.user_id = u.id) AS singles,
                   COUNT(DISTINCT a.house_name) AS houses,
                   COUNT(v.national_id) AS voters,
                   COUNT(v.national_id) AS total,
                   SUM(v.status != 'new') AS reached,
                   SUM(v.status = 'contacted') AS contacted,
                   SUM(v.status = 'followup') AS followup,
                   SUM(v.status = 'noanswer') AS noanswer
            FROM users u
            LEFT JOIN house_assignments a ON a.user_id = u.id
            LEFT JOIN voters v ON v.house_name = a.house_name{join_scope}
            WHERE u.role IN ('agent', 'user', 'admin') AND u.deleted_at IS NULL{cond}
            GROUP BY u.id ORDER BY u.role = 'admin', u.role, u.username COLLATE NOCASE""", extra
    ).fetchall()


@bp.route("/admin/agents")
@role_required("admin")
def agents():
    db = get_db()
    houses = house_rows(sort="name")
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    weekly = dict(db.execute(
        "SELECT user_id, COUNT(*) FROM audit_log WHERE action = 'voter_canvass' AND at >= ? GROUP BY user_id",
        (week,),
    ).fetchall())
    from . import agentlink
    from .auth import is_owner

    links = agentlink.active_links() if is_owner() else {}
    return render_template("canvass/agents.html", agents=_agents(), houses=houses, weekly=weekly, links=links,
                           link_url=agentlink.link_url, link_wa=agentlink.whatsapp_url, link_days=agentlink.DAYS,
                           unassigned=sum(1 for h in houses if h["agent_id"] is None))


@bp.post("/admin/agents/assign")
@role_required("admin")
def assign():
    """Save the agent chosen for each house in the table."""
    db = get_db()
    valid = {r["id"] for r in _agents()}
    houses = request.form.getlist("house")
    chosen = request.form.getlist("agent")
    if len(houses) != len(chosen):
        abort(400)
    current = dict(db.execute("SELECT house_name, user_id FROM house_assignments").fetchall())
    changed = 0
    for house, agent in zip(houses, chosen):
        agent_id = int(agent) if agent.isdigit() and int(agent) in valid else None
        if current.get(house) == agent_id:
            continue
        if agent_id is None:
            db.execute("DELETE FROM house_assignments WHERE house_name = ?", (house,))
        else:
            db.execute(
                "INSERT INTO house_assignments (house_name, user_id, assigned_at, assigned_by) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(house_name) DO UPDATE SET user_id = excluded.user_id,"
                " assigned_at = excluded.assigned_at, assigned_by = excluded.assigned_by",
                (house, agent_id, now_iso(), g.user["id"]),
            )
        changed += 1
    if changed:
        audit.log("houses_assigned", detail={"changed": changed}, commit=False)
    db.commit()
    flash(f"Saved {changed} house assignment{'s' if changed != 1 else ''}." if changed else "Nothing changed.",
          "success" if changed else "info")
    return redirect(url_for("canvass.agents"))


def plan_assignment(houses, agent_ids, current_load):
    """Spread houses over agents so each gets a similar amount of work left.

    Work for a house = voters not yet reached + follow-ups (at least 1). Biggest houses go first,
    each to the agent with the least work so far (longest-processing-time first).
    """
    load = {a: current_load.get(a, 0) for a in agent_ids}
    plan = {}
    for h in sorted(houses, key=lambda h: (-h["work"], h["house"])):
        agent = min(load, key=lambda a: (load[a], a))
        plan[h["house"]] = agent
        load[agent] += h["work"]
    return plan, load


@bp.post("/admin/agents/unassign-all")
@role_required("admin")
def unassign_all():
    """Start over: no house and no voter is assigned to anyone."""
    if request.form.get("confirm", "").strip().upper() != "UNASSIGN":
        flash("Type UNASSIGN to confirm.", "error")
        return redirect(url_for("canvass.agents"))
    db = get_db()
    houses = db.execute("DELETE FROM house_assignments").rowcount
    singles = db.execute("DELETE FROM voter_assignments").rowcount
    audit.log("unassign_all", detail={"houses": houses, "voters": singles}, commit=False)
    db.commit()
    flash(f"Everyone is unassigned: {houses} house{'s' if houses != 1 else ''} and {singles} one-by-one voter"
          f"{'s' if singles != 1 else ''} taken back.", "success")
    return redirect(url_for("canvass.agents"))


@bp.post("/admin/agents/auto")
@role_required("admin")
def auto_assign():
    db = get_db()
    active = {r["id"]: r["username"] for r in _agents(active_only=True)}
    agent_ids = [int(a) for a in request.form.getlist("agents") if a.isdigit() and int(a) in active]
    if not agent_ids:
        flash("Pick at least one active agent.", "error")
        return redirect(url_for("canvass.agents"))
    rebalance = request.form.get("scope") == "all"
    rows = house_rows(sort="name")
    work = lambda r: max(1, (r["total"] - r["reached"]) + (r["followup"] or 0))  # noqa: E731
    current_load = {}
    houses = []
    for r in rows:
        if rebalance or r["agent_id"] is None:
            houses.append({"house": r["house"], "work": work(r)})
        elif r["agent_id"] in agent_ids:
            current_load[r["agent_id"]] = current_load.get(r["agent_id"], 0) + work(r)
    if not houses:
        flash("Every house already has an agent. Choose 'Re-balance all houses' to redistribute.", "info")
        return redirect(url_for("canvass.agents"))
    plan, load = plan_assignment(houses, agent_ids, current_load)
    if rebalance:
        db.execute("DELETE FROM house_assignments")
    stamp = now_iso()
    db.executemany(
        "INSERT INTO house_assignments (house_name, user_id, assigned_at, assigned_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(house_name) DO UPDATE SET user_id = excluded.user_id, assigned_at = excluded.assigned_at,"
        " assigned_by = excluded.assigned_by",
        [(house, agent, stamp, g.user["id"]) for house, agent in plan.items()],
    )
    summary = ", ".join(f"{active[a]} {load[a]}" for a in agent_ids)
    audit.log("houses_auto_assigned", detail={"houses": len(plan), "rebalance": rebalance, "load": summary},
              commit=False)
    db.commit()
    flash(f"Assigned {len(plan)} houses. Work per agent (voters left to reach + follow-ups): {summary}.", "success")
    return redirect(url_for("canvass.agents"))
