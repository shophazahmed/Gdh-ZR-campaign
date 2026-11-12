"""Share voter cards on WhatsApp. Only the owner account (the candidate's administrator) may share.

A card is a small picture (photo, name, house, location, phone) or the same as plain text. The admin
always presses Send: the portal opens WhatsApp (a chat with the number, or the phone's share sheet)
with the card or text ready. Sharing with an agent or user can also hand them the voters.
"""
import html
import io
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Blueprint, abort, current_app, flash, g, render_template, request, send_file, url_for

from . import audit, campaign
from .auth import can_manage_confirmed_voters
from .db import get_db
from .normalize import normalize_phone

bp = Blueprint("share", __name__)

MAX_SHARE = 10  # more than this: use the PDF list
LABELS = {
    "en": {"house": "House", "location": "Votes at", "phone": "Phone"},
    "dv": {"house": "ގެ", "location": "ވޯޓުލާ ތަން", "phone": "ފޯނު"},
}

CARD_CSS = """
body { font-family: sans-serif; color: #102a28; margin: 0; }
table { width: 100%; border-collapse: collapse; }
td { vertical-align: top; padding: 0; }
td.photo { width: 120pt; }
.name { font-size: 19pt; font-weight: bold; margin: 0 0 1pt 0; }
.alt { font-size: 12pt; color: #44615e; margin: 0 0 8pt 0; }
.row { font-size: 11.5pt; margin: 3pt 0; }
.label { color: #5d7a77; font-size: 9pt; }
.phone { font-family: monospace; font-size: 13pt; }
"""
CARD_W, CARD_H = 520, 190  # points; rendered at 2x


def _esc(value):
    return html.escape(value or "")


def _local_now():
    return datetime.now(timezone.utc) + timedelta(hours=5)


def _photo_blob(voter):
    if not voter["photo_path"]:
        return None
    from PIL import Image

    try:
        img = Image.open(os.path.join(current_app.config["PHOTOS_DIR"], voter["photo_path"])).convert("RGB")
        img.thumbnail((360, 360))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=82)
        return out.getvalue()
    except Exception:
        return None


def card_png(voter, lang, recipient):
    """The voter card as a PNG: photo on one side, details on the other (mirrored for Dhivehi)."""
    import pymupdf

    t = LABELS[lang]
    dv = lang == "dv"
    archive = pymupdf.Archive()
    blob = _photo_blob(voter)
    photo = ""
    if blob:
        archive.add(blob, "photo.jpg")
        photo = '<img src="photo.jpg" width="108"/>'
    align = ' style="text-align:right"' if dv else ""
    main = (voter["full_name_dv"] or voter["full_name"]) if dv else voter["full_name"]
    alt = (voter["full_name"] if voter["full_name_dv"] else "") if dv else voter["full_name_dv"]
    house = (voter["house_name_dv"] or voter["house_name"]) if dv else voter["house_name"]
    details = (f'<td{align}><p class="name">{_esc(main)}</p>'
               + (f'<p class="alt">{_esc(alt)}</p>' if alt else '<p class="alt"> </p>')
               + f'<p class="row"><span class="label">{t["house"]}</span><br/>{_esc(house) or "—"}</p>'
               f'<p class="row"><span class="label">{t["location"]}</span><br/>'
               f'<span dir="ltr">{_esc(voter["location"]) or "—"}</span></p>'
               f'<p class="row"><span class="label">{t["phone"]}</span><br/>'
               f'<span class="phone" dir="ltr">{_esc(voter["phone"]) or "—"}</span></p></td>')
    photo_cell = f'<td class="photo"{align}>{photo}</td>'
    cells = details + photo_cell if dv else photo_cell + details
    from .fonts import css_for_pdf, mark_thaana

    font_css, font_blob = css_for_pdf()
    html_text = f"<table><tr>{cells}</tr></table>"
    if font_blob:
        archive.add(font_blob, "dhivehi-font")
        html_text = mark_thaana(html_text)
    story = pymupdf.Story(html=html_text, user_css=CARD_CSS + font_css, archive=archive)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    rect = pymupdf.Rect(0, 0, CARD_W, CARD_H)
    device = writer.begin_page(rect)
    story.place(rect + (18, 16, -18, -26))
    story.draw(device)
    writer.end_page()
    writer.close()
    doc = pymupdf.open("pdf", buf.getvalue())
    page = doc[0]
    page.draw_rect(pymupdf.Rect(0, CARD_H - 4, CARD_W, CARD_H), color=None, fill=(0.06, 0.47, 0.42))
    note = (f"For {recipient} · " if recipient else "") + _local_now().strftime("%d %b %Y") + " · Do not forward"
    page.insert_text((18, CARD_H - 11), note, fontsize=7.5, color=(0.45, 0.5, 0.5))
    return page.get_pixmap(dpi=144, alpha=False).tobytes("png")


def card_text(voters, lang):
    t = LABELS[lang]
    blocks = []
    for v in voters:
        if lang == "dv":
            name = v["full_name_dv"] or v["full_name"]
            house = v["house_name_dv"] or v["house_name"]
        else:
            name, house = v["full_name"], v["house_name"]
        lines = [f"*{name}*"]
        if lang == "en" and v["full_name_dv"]:
            lines.append(v["full_name_dv"])
        lines += [f"{t['house']}: {house or '—'}", f"{t['location']}: {v['location'] or '—'}",
                  f"{t['phone']}: {v['phone'] or '—'}"]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _voters(ids):
    from .voters import scoped

    ids = [i for i in dict.fromkeys(ids) if i][:MAX_SHARE + 1]
    if not ids:
        return []
    w, p = scoped(f" WHERE national_id IN ({','.join('?' * len(ids))})", ids)
    found = {r["national_id"]: r for r in get_db().execute(f"SELECT * FROM voters{w}", p)}
    return [found[i] for i in ids if i in found]


def _recipient(value):
    """An agent, user or admin who can be given voters (their phone is used for WhatsApp)."""
    if not value.isdigit():
        return None
    return get_db().execute(
        "SELECT id, username, phone, role FROM users WHERE id = ? AND role IN ('agent', 'user', 'admin')"
        " AND deleted_at IS NULL AND is_active = 1", (int(value),)).fetchone()


def people():
    """Who a card can be sent to by name (agents, users, admins), for the Share panel."""
    return get_db().execute(
        "SELECT id, username, phone, role FROM users WHERE role IN ('agent', 'user', 'admin') AND deleted_at IS NULL"
        " AND is_active = 1 ORDER BY role, username COLLATE NOCASE").fetchall()


def _wa_number(phone):
    number, _ = normalize_phone(phone or "")
    return campaign._mobile_number({"phone": number})


@bp.route("/voters/<national_id>/card.png")
@can_manage_confirmed_voters
def card(national_id):
    voters = _voters([national_id])
    if not voters:
        abort(404)
    lang = "dv" if request.args.get("lang") == "dv" else "en"
    person = _recipient(request.args.get("for", ""))
    png = card_png(voters[0], lang, person["username"] if person else request.args.get("to", "")[:20])
    return send_file(io.BytesIO(png), mimetype="image/png", max_age=0,
                     download_name=f"voter-{voters[0]['full_name'].replace(' ', '-')}.png")


@bp.post("/voters/share")
@can_manage_confirmed_voters
def share():
    """Prepare a share: log it, optionally hand the voters to the person, and give the browser
    what it needs (card image links, text, WhatsApp link). Without JavaScript a page shows the same."""
    from .voters import assign_voters

    voters = _voters(request.form.getlist("ids"))
    if not voters:
        abort(400)
    if len(voters) > MAX_SHARE:
        msg = f"Share up to {MAX_SHARE} voters at a time; for more, use the PDF list."
        if request.headers.get("X-Requested-With") == "fetch":
            return {"ok": False, "error": msg}, 400
        flash(msg, "error")
        return render_template("share/result.html", voters=[], cards=[], wa_url=None, text="", to_name=""), 400
    lang = "dv" if request.form.get("lang") == "dv" else "en"
    mode = "text" if request.form.get("mode") == "text" else "card"
    to = request.form.get("to", "")
    person = _recipient(to)
    number = None
    to_name = ""
    if person:
        number, to_name = _wa_number(person["phone"]), person["username"]
    elif to == "number":
        raw = request.form.get("number", "")
        number = _wa_number(raw)
        if not number:
            msg = "That doesn't look like a WhatsApp number (7 digits starting with 7 or 9)."
            if request.headers.get("X-Requested-With") == "fetch":
                return {"ok": False, "error": msg}, 400
            abort(400)
        to_name = normalize_phone(raw)[0]
    assigned = 0
    if person and request.form.get("assign"):
        assigned = assign_voters([v["national_id"] for v in voters], person["id"])
    text = card_text(voters, lang)
    wa_url = f"https://wa.me/{number}?text={quote(text)}" if number else f"https://wa.me/?text={quote(text)}"
    cards = [url_for("share.card", national_id=v["national_id"], lang=lang,
                     **({"for": person["id"]} if person else {"to": to_name} if to_name else {})) for v in voters]
    audit.log("voter_share", target=voters[0]["national_id"] if len(voters) == 1 else f"{len(voters)} voters",
              detail={"to": to_name or "picked in WhatsApp", "mode": mode, "lang": lang,
                      "voters": [v["national_id"] for v in voters], "assigned": assigned})
    get_db().commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "mode": mode, "text": text, "wa_url": wa_url, "direct": bool(number),
                "cards": cards, "to": to_name, "assigned": assigned,
                "names": [f"voter-{v['full_name'].replace(' ', '-')}.png" for v in voters]}
    return render_template("share/result.html", voters=voters, cards=cards, wa_url=wa_url, text=text,
                           to_name=to_name, mode=mode, assigned=assigned)


# --- an agent's list as a WhatsApp message --------------------------------------------------

LIST_URL_MAX = 12000  # longer links are refused by some phones; the PDF has no limit
LIST_LABELS = {
    "en": {"head": "MDP Gadhdhoo · Your voters", "list": "MDP Gadhdhoo · Voter list", "house": "house", "houses": "houses",
           "voters": "voters", "voter": "voter", "foot": "Please update the portal after each visit.", "none": "No address"},
    "dv": {"head": "އެމްޑީޕީ ގައްދޫ · ތިބާގެ ވޯޓަރުން", "list": "އެމްޑީޕީ ގައްދޫ · ވޯޓަރުންގެ ލިސްޓު", "house": "ގެ",
           "houses": "ގެ", "voters": "ވޯޓަރުން", "voter": "ވޯޓަރު",
           "foot": "ކޮންމެ ޒިޔާރަތަކަށްފަހު ޕޯޓަލް އަޕްޑޭޓް ކުރައްވާ.", "none": "އެޑްރެސް ނެތް"},
}


def list_text(rows, lang, name, sender):
    """Voters grouped by house, numbered, with phones: one WhatsApp message."""
    t = LIST_LABELS[lang]
    dv = lang == "dv"
    houses = {}
    for r in rows:
        houses.setdefault(r["house_name"] or "", []).append(r)
    count = (f"{len(houses)} {t['house'] if len(houses) == 1 else t['houses']} · "
             f"{len(rows)} {t['voter'] if len(rows) == 1 else t['voters']}")
    lines = [f"*{t['head'] if name else t['list']}*", f"{name}: {count}" if name else count]
    n = 0
    for house, people in houses.items():
        label = ((people[0]["house_name_dv"] or house) if dv else house) or t["none"]
        lines += ["", f"*{label}* ({len(people)})"]
        for r in people:
            n += 1
            who = (r["full_name_dv"] or r["full_name"]) if dv else r["full_name"]
            lines.append(f"{n}. {who} · {r['phone'] or '—'}")
    lines += ["", t["foot"], f"— {sender}, {_local_now().strftime('%-d %b')}"]
    return "\n".join(lines)


@bp.route("/voters/whatsapp-list")
@can_manage_confirmed_voters
def whatsapp_list():
    """Open WhatsApp with the list typed in: to the agent's own number when filtering by an agent
    who has a mobile number, otherwise WhatsApp asks who to send it to."""
    from flask import redirect
    from .pdf_export import MAX_ROWS
    from .voters import build_where, read_filters

    f = read_filters(request.args)
    if f.get("cf"):
        abort(403)  # the confirmed list never leaves the portal
    lang = "dv" if request.args.get("lang") == "dv" else "en"
    where, params = build_where(f)
    rows = get_db().execute(
        f"SELECT * FROM voters{where} ORDER BY house_name = '', house_name COLLATE NOCASE,"
        " full_name COLLATE NOCASE LIMIT ?", params + [MAX_ROWS + 1]).fetchall()
    back = request.referrer if (request.referrer or "").startswith(request.host_url) else url_for("canvass.agents")
    if not rows:
        flash("No voters to send.", "info")
        return redirect(back)
    person = _recipient(f["ag"]) if f.get("ag", "").isdigit() else None
    number = _wa_number(person["phone"]) if person else None
    text = list_text(rows, lang, person["username"] if person else "", g.user["username"])
    url = f"https://wa.me/{number or ''}?text={quote(text)}"
    if len(url) > LIST_URL_MAX:
        flash(f"{len(rows)} voters is too long for one WhatsApp message. Use the PDF button instead "
              "(on a phone it opens WhatsApp with the PDF).", "error")
        return redirect(back)
    audit.log("voter_list_whatsapp", target=person["username"] if person else f"{len(rows)} voters",
              detail={"to": person["username"] if person else "picked in WhatsApp", "voters": len(rows),
                      "lang": lang, "direct": bool(number)})
    return redirect(url)
