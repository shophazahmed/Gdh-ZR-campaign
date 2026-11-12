"""'100% confirmed' voters: the candidate's own supporters.

Private to the 'admin' account (auth.can_manage_confirmed_voters on every route). The list lives
in its own table, so no other page, export or role can show it unless it is joined on purpose.
"""
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from . import audit, campaign
from .auth import confirmed_marker_required, confirmed_viewer_required, is_owner
from .db import get_db, now_iso

bp = Blueprint("confirmed", __name__)

MAX_NOTE = 300
MAX_LIST = 5000
LIST_ID_RE = re.compile(r"\b(A\d{6}|[A-Z]{1,2}\d{4,8})\b")
# Search rows: whether the voter is confirmed (only added to the query for the 'admin' account)
CONFIRMED_COLUMN = ("EXISTS (SELECT 1 FROM confirmed_voters cv WHERE cv.national_id = voters.national_id)"
                    " AS confirmed")


def metrics():
    """Confirmed out of the campaign's voters (e.g. MDP), plus the same out of all voters."""
    db = get_db()
    clause, params = campaign.campaign_clause("v.party")
    scope = f" AND {clause}" if clause else ""
    join = "FROM voters v LEFT JOIN confirmed_voters c ON c.national_id = v.national_id"
    row = db.execute(
        f"SELECT COUNT(*) AS total, COUNT(c.national_id) AS confirmed {join} WHERE 1{scope}", params).fetchone()
    every = db.execute(f"SELECT COUNT(*) AS total, COUNT(c.national_id) AS confirmed {join}").fetchone()
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    this_week = db.execute(
        f"SELECT COUNT(*) {join} WHERE c.confirmed_at >= ?{scope}", [week] + params).fetchone()[0]
    pct = lambda n, d: round(100 * n / d, 1) if d else 0.0  # noqa: E731
    parties = campaign.visible_parties()
    return {
        "label": ", ".join("No party" if p == campaign.NO_PARTY else p for p in parties) if parties else "All voters",
        "total": row["total"], "confirmed": row["confirmed"], "pct": pct(row["confirmed"], row["total"]),
        "all_total": every["total"], "all_confirmed": every["confirmed"],
        "all_pct": pct(every["confirmed"], every["total"]), "this_week": this_week,
    }


def by_house():
    clause, params = campaign.campaign_clause("v.party")
    scope = f" AND {clause}" if clause else ""
    return get_db().execute(
        "SELECT v.house_name AS house, COUNT(*) AS total, COUNT(c.national_id) AS confirmed"
        " FROM voters v LEFT JOIN confirmed_voters c ON c.national_id = v.national_id"
        f" WHERE v.house_name != ''{scope} GROUP BY v.house_name"
        " ORDER BY COUNT(c.national_id) DESC, COUNT(*) DESC, v.house_name COLLATE NOCASE", params).fetchall()


@bp.post("/voters/<national_id>/confirm")
@confirmed_marker_required
def set_confirmed(national_id):
    """Mark (confirmed=1) or unmark (confirmed=0). Takes the wanted state, not a toggle, so a
    double tap on a slow phone can't undo itself."""
    from .voters import get_voter_or_404, _safe_back

    get_voter_or_404(national_id)
    wanted = request.form.get("confirmed")
    if wanted not in ("0", "1"):
        abort(400)
    db = get_db()
    current = db.execute("SELECT note FROM confirmed_voters WHERE national_id = ?", (national_id,)).fetchone()
    note = request.form.get("note")
    note = note.replace("\r\n", "\n").strip()[:MAX_NOTE] if note is not None else (current["note"] if current else "")
    changed = False
    if wanted == "1":
        if current is None:
            db.execute("INSERT INTO confirmed_voters (national_id, confirmed_at, note) VALUES (?, ?, ?)",
                       (national_id, now_iso(), note))
            audit.log("voter_confirm", target=national_id, commit=False)
            changed = True
        elif note != current["note"]:
            db.execute("UPDATE confirmed_voters SET note = ? WHERE national_id = ?", (note, national_id))
            changed = True
    elif current is not None:
        db.execute("DELETE FROM confirmed_voters WHERE national_id = ?", (national_id,))
        audit.log("voter_unconfirm", target=national_id, commit=False)
        changed = True
    db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        m = metrics()
        return {"ok": True, "confirmed": wanted == "1", "changed": changed,
                "metrics": {k: m[k] for k in ("confirmed", "total", "pct")}}
    flash(("Marked as 100% confirmed." if wanted == "1" else "No longer confirmed.") if changed else "Nothing changed.",
          "success" if changed else "info")
    return redirect(_safe_back(request.form.get("back")) if request.form.get("back") else
                    url_for("voters.detail", national_id=national_id))


@bp.route("/confirmed")
@confirmed_viewer_required
def overview():
    return render_template("confirmed/overview.html", m=metrics(), houses=by_house(), owner=is_owner())


@bp.route("/confirmed/metrics.json")
@confirmed_viewer_required
def metrics_json():
    resp = metrics()
    return resp


def _ids_from_text(text):
    """ID cards found anywhere in pasted text (one per line, or a copied spreadsheet column)."""
    seen, ids = set(), []
    for m in LIST_ID_RE.finditer((text or "").upper()):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            ids.append(m.group(1))
    return ids[:MAX_LIST]


def _sort_list(ids):
    """Split pasted ID cards into: to confirm, already confirmed, not a campaign voter, not found."""
    db = get_db()
    clause, params = campaign.campaign_clause("v.party")
    found = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in db.execute(
                "SELECT v.national_id, v.full_name, v.house_name, v.location, v.party,"
                " (c.national_id IS NOT NULL) AS confirmed"
                + (f", ({clause}) AS in_campaign" if clause else ", 1 AS in_campaign")
                + " FROM voters v LEFT JOIN confirmed_voters c ON c.national_id = v.national_id"
                f" WHERE v.national_id IN ({marks})", (params if clause else []) + chunk):
            found[r["national_id"]] = r
    groups = {"new": [], "already": [], "other": [], "missing": []}
    for nid in ids:
        r = found.get(nid)
        if r is None:
            groups["missing"].append(nid)
        elif r["confirmed"]:
            groups["already"].append(r)
        elif not r["in_campaign"]:
            groups["other"].append(r)
        else:
            groups["new"].append(r)
    return groups


@bp.post("/confirmed/list")
@confirmed_marker_required
def list_preview():
    ids = _ids_from_text(request.form.get("ids", ""))
    if not ids:
        flash("No ID card numbers found. Paste them one per line, like A123456.", "error")
        return redirect(url_for("confirmed.overview"))
    return render_template("confirmed/list_preview.html", g_=_sort_list(ids), count=len(ids))


@bp.post("/confirmed/list/apply")
@confirmed_marker_required
def list_apply():
    groups = _sort_list(_ids_from_text(request.form.get("ids", "")))
    take = groups["new"] + (groups["other"] if request.form.get("include_other") else [])
    db = get_db()
    stamp = now_iso()
    for r in take:
        db.execute("INSERT OR IGNORE INTO confirmed_voters (national_id, confirmed_at, note) VALUES (?, ?, '')",
                   (r["national_id"], stamp))
        audit.log("voter_confirm", target=r["national_id"], detail="from a list", commit=False)
    db.commit()
    flash(f"{len(take)} voter{'s' if len(take) != 1 else ''} marked as 100% confirmed." if take else "Nothing changed.",
          "success" if take else "info")
    return redirect(url_for("confirmed.overview"))
