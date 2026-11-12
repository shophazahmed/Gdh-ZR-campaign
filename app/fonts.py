"""An optional Dhivehi (Thaana) font chosen by the admins, e.g. MV Faseyha or Faruma.

Uploaded under Settings and stored next to the campaign picture (never in the code repository, since
fonts are often licensed). PDFs, voter cards and the portal's own pages then show Dhivehi text in it;
English stays in the normal font. Without one, Noto Sans Thaana (built into PyMuPDF) is used.
"""
import os
import re

from flask import Blueprint, abort, current_app, flash, redirect, request, send_file, url_for

from . import audit
from .auth import admin_required, login_required
from .db import get_db

bp = Blueprint("fonts", __name__)

SETTING = "thaana_font"
MAX_BYTES = 8 * 1024 * 1024
MIN_THAANA = 40  # of the 50 Thaana letters: a real Unicode Thaana font, not an old ASCII-mapped one


def _settings():
    from .campaign import get_setting, picture_dir, put_setting
    return get_setting, put_setting, picture_dir


def font_file():
    """Path of the uploaded font, or None."""
    get_setting, _, picture_dir = _settings()
    name = get_setting(SETTING) or ""
    path = os.path.join(picture_dir(), name) if name else ""
    return path if name and os.path.isfile(path) else None


def font_name():
    get_setting, _, _ = _settings()
    return get_setting(SETTING + "_name") or ""


def check(blob):
    """(font family name, None) for a usable Unicode Thaana font, else (None, reason)."""
    import pymupdf

    try:
        font = pymupdf.Font(fontbuffer=blob)
    except Exception:
        return None, "That file isn't a font (use .ttf or .otf)."
    letters = sum(1 for c in range(0x0780, 0x07B2) if font.has_glyph(c))
    if letters < MIN_THAANA:
        return None, ("That font has no Unicode Thaana letters (it may be an old typewriter-style font). "
                      "Use a Unicode font such as MV Faseyha or Faruma.")
    return font.name, None


_THAANA_RUN = re.compile(r"[\u0780-\u07BF](?:[\u0780-\u07BF\u060C\s]*[\u0780-\u07BF])?")


def mark_thaana(html_text):
    """Wrap each run of Thaana text in <span class="dvfont"> so only Dhivehi uses the chosen font."""
    return _THAANA_RUN.sub(lambda m: f'<span class="dvfont">{m.group(0)}</span>', html_text)


def css_for_pdf():
    """(@font-face CSS, file bytes) to add to a PyMuPDF story, or ('', None)."""
    path = font_file()
    if not path:
        return "", None
    with open(path, "rb") as fh:
        blob = fh.read()
    return '@font-face { font-family: "PortalDhivehi"; src: url(dhivehi-font); }\n.dvfont { font-family: "PortalDhivehi"; }\n', blob


@bp.post("/admin/settings/font")
@admin_required
def upload():
    _, put_setting, picture_dir = _settings()
    old = font_file()
    if request.form.get("remove"):
        put_setting(SETTING, "")
        put_setting(SETTING + "_name", "")
        audit.log("thaana_font", detail={"removed": True}, commit=False)
        get_db().commit()
        if old:
            os.remove(old)
        flash("Dhivehi font removed: back to Noto Sans Thaana.", "success")
        return redirect(url_for("campaign.settings") + "#font")
    up = request.files.get("font")
    blob = up.read(MAX_BYTES + 1) if up else b""
    ext = os.path.splitext(up.filename or "")[1].lower() if up else ""
    if not blob or ext not in (".ttf", ".otf"):
        flash("Choose a .ttf or .otf font file.", "error")
        return redirect(url_for("campaign.settings") + "#font")
    if len(blob) > MAX_BYTES:
        flash("The font must be 8 MB or smaller.", "error")
        return redirect(url_for("campaign.settings") + "#font")
    name, problem = check(blob)
    if problem:
        flash(problem, "error")
        return redirect(url_for("campaign.settings") + "#font")
    os.makedirs(picture_dir(), exist_ok=True)
    stored = f"dhivehi-font{ext}"
    with open(os.path.join(picture_dir(), stored), "wb") as fh:
        fh.write(blob)
    if old and os.path.basename(old) != stored:
        os.remove(old)
    put_setting(SETTING, stored)
    put_setting(SETTING + "_name", name)
    audit.log("thaana_font", detail={"font": name, "bytes": len(blob)}, commit=False)
    get_db().commit()
    flash(f"Dhivehi font set to {name}. PDFs, voter cards and the portal now use it for Dhivehi.", "success")
    return redirect(url_for("campaign.settings") + "#font")


@bp.route("/font/dhivehi")
@login_required
def serve():
    path = font_file()
    if not path:
        abort(404)
    mime = "font/otf" if path.endswith(".otf") else "font/ttf"
    return send_file(path, mimetype=mime, max_age=86400)


@bp.route("/font/dhivehi.css")
@login_required
def stylesheet():
    if not font_file():
        abort(404)
    css = ('@font-face { font-family: "Portal Dhivehi"; src: url("%s"); font-display: swap; }\n'
           '.thaana, [lang="dv"] { font-family: "Portal Dhivehi", "Faruma", "MV Faseyha", "Noto Sans Thaana", sans-serif; }\n'
           % url_for("fonts.serve"))
    resp = current_app.response_class(css, mimetype="text/css")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp
