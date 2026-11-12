"""Campaign scope: which parties' voters the portal shows (e.g. MDP members only).

Voters of other parties stay in the database and keep being updated by imports; they are only
hidden. Users and agents never see them. Admins see the campaign view by default and can switch
to "all parties" for their own session.
"""
import io
import json
import os
import re
import secrets
from urllib.parse import quote

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)

from . import audit
from .auth import admin_required
from .db import get_db, now_iso

bp = Blueprint("campaign", __name__, url_prefix="/admin")

NO_PARTY = "(none)"  # how a blank party is stored in the setting
DEFAULT_WHATSAPP = "Assalaamu alaikum {first_name}, "
MAX_TEMPLATE = 600
# One message per app; SMS and Viber fall back to the WhatsApp text until they get their own
CHANNELS = {"whatsapp": "WhatsApp", "sms": "SMS", "viber": "Viber"}
MAX_PICTURE_BYTES = 8 * 1024 * 1024
PLACEHOLDER = re.compile(r"\{(first_name|name|house|id)\}")


# Colour presets: (light, dark) values for the accent tokens in style.css. "ink" is the accent
# used for text, so it stays readable on light backgrounds even for yellow.
THEMES = {
    "teal": ("Teal", {"accent": "#0f766e", "accent-strong": "#0b5e58", "accent-soft": "#e3f4f2",
                      "accent-text": "#ffffff", "accent-ink": "#0f766e"},
                     {"accent": "#2dd4bf", "accent-strong": "#5eead4", "accent-soft": "#123a36",
                      "accent-text": "#06201d", "accent-ink": "#5eead4"}),
    "mdp": ("MDP yellow", {"accent": "#f5c400", "accent-strong": "#e0b200", "accent-soft": "#fff5c7",
                           "accent-text": "#1c1700", "accent-ink": "#7a5a00"},
                          {"accent": "#facc15", "accent-strong": "#fde047", "accent-soft": "#3a3108",
                           "accent-text": "#1c1700", "accent-ink": "#fde047"}),
    "blue": ("Blue", {"accent": "#1d4ed8", "accent-strong": "#1e40af", "accent-soft": "#e7eefc",
                      "accent-text": "#ffffff", "accent-ink": "#1d4ed8"},
                     {"accent": "#60a5fa", "accent-strong": "#93c5fd", "accent-soft": "#16264a",
                      "accent-text": "#0b1220", "accent-ink": "#93c5fd"}),
    "green": ("Green", {"accent": "#15803d", "accent-strong": "#166534", "accent-soft": "#e6f4ea",
                        "accent-text": "#ffffff", "accent-ink": "#15803d"},
                       {"accent": "#4ade80", "accent-strong": "#86efac", "accent-soft": "#10301c",
                        "accent-text": "#052e16", "accent-ink": "#86efac"}),
    "purple": ("Purple", {"accent": "#6d28d9", "accent-strong": "#5b21b6", "accent-soft": "#efe9fc",
                          "accent-text": "#ffffff", "accent-ink": "#6d28d9"},
                         {"accent": "#a78bfa", "accent-strong": "#c4b5fd", "accent-soft": "#271b47",
                          "accent-text": "#140b2e", "accent-ink": "#c4b5fd"}),
    "red": ("Red", {"accent": "#b91c1c", "accent-strong": "#991b1b", "accent-soft": "#fbe9e9",
                    "accent-text": "#ffffff", "accent-ink": "#b91c1c"},
                   {"accent": "#f87171", "accent-strong": "#fca5a5", "accent-soft": "#3d1515",
                    "accent-text": "#2b0606", "accent-ink": "#fca5a5"}),
}
DEFAULT_THEME = "teal"
APPEARANCES = ("system", "light", "dark")


def theme_name():
    name = get_setting("theme", DEFAULT_THEME)
    return name if name in THEMES else DEFAULT_THEME


def appearance():
    value = request.cookies.get("appearance", "system")
    return value if value in APPEARANCES else "system"


def get_setting(key, default=None):
    cache = g.setdefault("settings_cache", {})
    if key not in cache:
        row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        cache[key] = row[0] if row else default
    return cache[key]


def put_setting(key, value):
    get_db().execute(
        "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,"
        " updated_by = excluded.updated_by",
        (key, value, now_iso(), g.user["username"]),
    )
    g.get("settings_cache", {}).pop(key, None)


def _mobile_number(voter):
    """International number for a voter's mobile phone, or None for landlines and junk."""
    digits = re.sub(r"\D", "", voter["phone"] or "")
    if re.fullmatch(r"[79]\d{6}", digits):
        return "960" + digits
    if 10 <= len(digits) <= 15 and not digits.startswith("960"):
        return digits
    return None


def template_for(channel):
    text = get_setting(f"{channel}_template")
    if text is None and channel != "whatsapp":
        text = get_setting("whatsapp_template")
    return text if text is not None else DEFAULT_WHATSAPP


def message(voter, channel="whatsapp"):
    name = (voter["full_name"] or "").strip()
    values = {"name": name, "first_name": name.split(" ")[0] if name else "",
              "house": voter["house_name"] or "", "id": voter["national_id"]}
    # Plain substitution, not str.format: the template is typed by an admin.
    text = PLACEHOLDER.sub(lambda m: values[m.group(1)], template_for(channel)).strip()
    if channel == "whatsapp" and picture_token() and get_setting("picture_link", "1") == "1":
        # WhatsApp shows the picture as a preview of this link
        text = f"{text}\n\n{url_for('picture.page', token=picture_token(), _external=True)}".strip()
    return text + " "


def sms_url(voter):
    """sms: link with the campaign message; '?&body=' works on both iPhone and Android."""
    number = _mobile_number(voter)
    return f"sms:+{number}?&body={quote(message(voter, 'sms'))}" if number else None


def viber_url(voter):
    """Opens the voter's Viber chat. Viber can't pre-fill text for a number, so the page copies
    the message to the clipboard first (see app.js)."""
    number = _mobile_number(voter)
    return f"viber://chat?number=%2B{number}" if number else None


def picture_token():
    return get_setting("picture_token") or None


def picture_dir():
    return current_app.config["CAMPAIGN_DIR"]


def whatsapp_url(voter):
    """wa.me link with the campaign's message filled in, or None if the number can't use WhatsApp.

    Maldives mobile numbers have 7 digits starting with 7 or 9; landlines (3xxxxxx, 6xxxxxx) have
    no WhatsApp. Longer numbers are taken to include their own country code.
    """
    number = _mobile_number(voter)
    return f"https://wa.me/{number}?text={quote(message(voter, 'whatsapp'))}" if number else None


def init_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL,"
                 " updated_at TEXT, updated_by TEXT)")
    conn.commit()


def visible_parties():
    """List of parties the campaign covers, or None when every party is shown."""
    if "visible_parties" not in g:
        row = get_db().execute("SELECT value FROM settings WHERE key = 'visible_parties'").fetchone()
        parties = json.loads(row[0]) if row else []
        g.visible_parties = parties or None
    return g.visible_parties


def showing_all():
    """True when an admin has switched the campaign filter off for their session."""
    user = g.get("user")
    return bool(user is not None and user["role"] == "admin" and session.get("show_all_parties"))


def party_scope(column="party"):
    """SQL limiting voters to the campaign parties, or (None, []) when nothing is hidden."""
    if showing_all():
        return None, []
    return campaign_clause(column)


def campaign_clause(column="party"):
    """The campaign's parties as SQL, even while an admin is viewing all parties (for fixed metrics)."""
    parties = visible_parties()
    if not parties:
        return None, []
    named = [p for p in parties if p != NO_PARTY]
    parts, params = [], []
    if named:
        parts.append(f"{column} IN ({', '.join('?' * len(named))})")
        params += named
    if NO_PARTY in parties:
        parts.append(f"{column} = ''")
    return "(" + " OR ".join(parts) + ")", params


def banner():
    """What the page header says about hidden voters (admins only)."""
    user = g.get("user")
    parties = visible_parties() if user is not None else None
    if not parties or user["role"] != "admin":
        return None
    label = ", ".join("No party" if p == NO_PARTY else p for p in parties)
    return {"label": label, "all": showing_all()}


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    db = get_db()
    counts = db.execute("SELECT party, COUNT(*) FROM voters GROUP BY party ORDER BY COUNT(*) DESC").fetchall()
    options = [(p or NO_PARTY, n) for p, n in counts]
    if request.method == "POST" and request.form.get("form") in CHANNELS:
        channel = request.form["form"]
        template = request.form.get("template", "").replace("\r\n", "\n").strip()[:MAX_TEMPLATE]
        put_setting(f"{channel}_template", template)
        if channel == "whatsapp":
            put_setting("picture_link", "1" if request.form.get("picture_link") else "0")
        audit.log(f"{channel}_template", detail={"template": template}, commit=False)
        db.commit()
        flash(f"{CHANNELS[channel]} message saved.", "success")
        return redirect(url_for("campaign.settings") + f"#msg-{channel}")
    if request.method == "POST":
        known = {p for p, _ in options} | set(visible_parties() or [])
        chosen = [p for p in request.form.getlist("parties") if p in known]
        if request.form.get("mode") == "all":
            chosen = []
        db.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('visible_parties', ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,"
            " updated_by = excluded.updated_by",
            (json.dumps(chosen), now_iso(), g.user["username"]),
        )
        audit.log("campaign_parties", detail={"visible": chosen or "all"}, commit=False)
        db.commit()
        g.pop("visible_parties", None)
        session.pop("show_all_parties", None)
        flash("Campaign set to " + (", ".join(chosen) + " voters only." if chosen else "all parties."), "success")
        return redirect(url_for("campaign.settings"))
    current = visible_parties() or []
    hidden = sum(n for p, n in options if current and p not in current)
    return render_template("admin/settings.html", options=options, current=current, hidden=hidden,
                           templates={c: template_for(c) for c in CHANNELS}, channels=CHANNELS,
                           own_template={c: get_setting(f"{c}_template") is not None for c in CHANNELS},
                           picture=picture_token(), picture_link=get_setting("picture_link", "1") == "1",
                           themes=THEMES, theme=theme_name())


@bp.post("/view-all")
@admin_required
def toggle_view_all():
    session["show_all_parties"] = not session.get("show_all_parties")
    audit.log("view_all_parties", detail={"on": session["show_all_parties"]})
    back = request.form.get("back", "")
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("voters.search"))


@bp.post("/theme")
@admin_required
def set_theme():
    name = request.form.get("theme", "")
    if name in THEMES:
        put_setting("theme", name)
        audit.log("theme", detail={"theme": name}, commit=False)
        get_db().commit()
        flash(f"Colour changed to {THEMES[name][0]}.", "success")
    return redirect(url_for("campaign.settings"))


@bp.post("/picture")
@admin_required
def upload_picture():
    """The campaign picture for WhatsApp: stored re-encoded (no EXIF), under a new random name each time."""
    from PIL import Image, UnidentifiedImageError

    os.makedirs(picture_dir(), exist_ok=True)
    old = picture_token()
    if request.form.get("remove"):
        put_setting("picture_token", "")
        audit.log("campaign_picture", detail={"removed": True}, commit=False)
        get_db().commit()
        _delete_picture(old)
        flash("Picture removed.", "success")
        return redirect(url_for("campaign.settings") + "#msg-whatsapp")
    upload = request.files.get("picture")
    blob = upload.read(MAX_PICTURE_BYTES + 1) if upload else b""
    error = None
    if not blob:
        error = "Choose a picture first."
    elif len(blob) > MAX_PICTURE_BYTES:
        error = "The picture must be 8 MB or smaller."
    else:
        try:
            img = Image.open(io.BytesIO(blob))
            if img.format not in ("JPEG", "PNG", "WEBP"):
                raise UnidentifiedImageError
            img.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            error = "That file isn't a JPEG, PNG or WebP picture."
    if error:
        flash(error, "error")
        return redirect(url_for("campaign.settings") + "#msg-whatsapp")
    img = img.convert("RGB")
    img.thumbnail((1600, 1600))
    token = secrets.token_urlsafe(12)
    img.save(os.path.join(picture_dir(), f"{token}.jpg"), "JPEG", quality=85)
    put_setting("picture_token", token)
    audit.log("campaign_picture", detail={"size": list(img.size)}, commit=False)
    get_db().commit()
    _delete_picture(old)
    flash("Picture saved. WhatsApp messages now carry a link that shows it.", "success")
    return redirect(url_for("campaign.settings") + "#msg-whatsapp")


def _delete_picture(token):
    if token:
        try:
            os.remove(os.path.join(picture_dir(), f"{token}.jpg"))
        except OSError:
            pass


# Public on purpose: WhatsApp (and the voter) must be able to load the campaign picture without
# signing in. It holds no voter data, and the address is random and changes with every upload.
picture_bp = Blueprint("picture", __name__)


def _current_token_or_404(token):
    if not token or token != picture_token():
        abort(404)


@picture_bp.route("/c/<token>")
def page(token):
    _current_token_or_404(token)
    resp = Response(render_template("picture.html", token=token))
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@picture_bp.route("/c/<token>.jpg")
def image(token):
    _current_token_or_404(token)
    resp = send_from_directory(os.path.abspath(picture_dir()), f"{token}.jpg", max_age=86400)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


themes_bp = Blueprint("theme", __name__)


@themes_bp.route("/theme.css")
def theme_css():
    """The chosen colour preset as CSS variables (a file, since the CSP forbids inline styles)."""
    _, light, dark = THEMES[theme_name()]
    decl = lambda values: " ".join(f"--{k}: {v};" for k, v in values.items())  # noqa: E731
    css = (f":root {{ {decl(light)} }}\n"
           f"@media (prefers-color-scheme: dark) {{ :root:not([data-theme=\"light\"]) {{ {decl(dark)} }} }}\n"
           f":root[data-theme=\"dark\"] {{ {decl(dark)} }}\n")
    resp = Response(css, mimetype="text/css")
    resp.headers["Cache-Control"] = "public, max-age=86400"  # the URL carries ?v=<theme>
    return resp


@themes_bp.post("/account/appearance")
def set_appearance():
    value = request.form.get("appearance", "system")
    back = request.form.get("back", "")
    resp = redirect(back if back.startswith("/") and not back.startswith("//") else "/")
    if value in APPEARANCES:
        resp.set_cookie("appearance", value, max_age=365 * 24 * 3600, samesite="Lax",
                        secure=request.is_secure, httponly=True)
    return resp
