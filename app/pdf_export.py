"""Printable voter list (PDF) with photos, for agents. English or Dhivehi (Thaana) names.

Built with PyMuPDF's HTML engine, which ships Noto Sans Thaana and lays Thaana out right to left
with the vowel marks in place, so no extra fonts or packages are needed on the server.
"""
import html
import io
import os
from datetime import datetime, timedelta, timezone

from PIL import Image

THUMB_PX = 160          # photo size in the PDF: small enough for WhatsApp, clear enough to recognise
MAX_ROWS = 2000

LABELS = {
    "en": {"name": "Name", "house": "Address", "location": "Location", "phone": "Phone", "notes": "Met / notes",
           "voters": "voters", "no_house": "No address"},
    "dv": {"name": "ނަން", "house": "އެޑްރެސް", "location": "ވޯޓުލާ ތަން", "phone": "ފޯނު", "notes": "ބައްދަލުވި / ނޯޓު",
           "voters": "ވޯޓަރުން", "no_house": "އެޑްރެސް ނެތް"},
}

CSS = """
body { font-family: sans-serif; font-size: 9pt; color: #111; }
h1 { font-size: 13pt; margin: 0 0 2pt 0; }
p.sub { font-size: 8.5pt; color: #555; margin: 0 0 8pt 0; }
table { width: 100%; border-collapse: collapse; }
th { font-size: 8pt; color: #555; text-align: left; border-bottom: 1pt solid #999; padding: 2pt 4pt; }
td { border-bottom: 0.5pt solid #ccc; padding: 3pt 4pt; vertical-align: middle; }
tr.house td { background: #eef3f2; font-weight: bold; font-size: 9.5pt; border-bottom: 1pt solid #999; }
td.photo { width: 46pt; }
td.num, th.num { width: 24pt; color: #555; font-size: 8.5pt; text-align: left; padding-right: 6pt; }
td.name b { font-size: 10pt; }
.alt { color: #666; font-size: 7.5pt; }
td.phone { font-family: monospace; white-space: nowrap; }
td.notes { width: 90pt; }
span.box { font-family: monospace; }
table.dv td.name, table.dv th.name, table.dv tr.house td { text-align: right; }
table.dv th { text-align: right; }
table.dv td.photo { text-align: right; }
"""


def _esc(value):
    return html.escape(value or "")


def _thumb(path):
    try:
        img = Image.open(path)
        img = img.convert("RGB")
        img.thumbnail((THUMB_PX, THUMB_PX))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=70)
        return out.getvalue()
    except Exception:  # an unreadable photo just leaves the cell empty
        return None


def _local_now():
    return datetime.now(timezone.utc) + timedelta(hours=5)  # Maldives time


def build_pdf(rows, *, lang, title, subtitle, recipient, photos_dir):
    """rows: voters sorted by house. Returns the PDF as bytes."""
    import pymupdf

    t = LABELS["dv" if lang == "dv" else "en"]
    dv = lang == "dv"
    archive = pymupdf.Archive()

    def row(cells):
        # Dhivehi lists read right to left (photo on the right, notes on the left);
        # the # column stays first, on the left, in both languages
        num, rest = cells[0], cells[1:]
        return "<tr>" + num + "".join(reversed(rest) if dv else rest) + "</tr>"

    right = ' style="text-align:right"' if dv else ""
    head = ['<th class="num">#</th>', f'<th class="photo"></th>', f'<th class="name"{right}>{t["name"]}</th>', f'<th>{t["location"]}</th>',
            f'<th>{t["phone"]}</th>', f'<th class="notes">{t["notes"]}</th>']
    parts = [f'<h1>{_esc(title)}</h1><p class="sub">{_esc(subtitle)}</p>',
             f'<table class="{"dv" if dv else "en"}">', row(head)]
    current = None
    counts = {}
    for r in rows:
        counts[r["house_name"]] = counts.get(r["house_name"], 0) + 1
    for n, r in enumerate(rows, 1):
        house = r["house_name"]
        if house != current:
            current = house
            label = (r["house_name_dv"] or house) if dv else house
            text = f'{_esc(label) or t["no_house"]} · {counts[house]} {t["voters"]}'
            rtl = ' style="text-align:right"' if dv else ""  # (dir=rtl would flip it back to the left)
            parts.append(f'<tr class="house"><td colspan="6"{rtl}>{text}</td></tr>')
        img = ""
        if r["photo_path"]:
            blob = _thumb(os.path.join(photos_dir, r["photo_path"]))
            if blob:
                name = f"p{n}.jpg"
                archive.add(blob, name)
                img = f'<img src="{name}" width="40"/>'
        if dv:
            main = _esc(r["full_name_dv"] or r["full_name"])
            alt = _esc(r["full_name"]) if r["full_name_dv"] else ""
            address = _esc(r["house_name_dv"] or house)
            dead = " · ނިޔާވެފައި" if r["deceased"] else ""  # no brackets: they flip in right-to-left text
            name_cell = (f'<td class="name" style="text-align:right"><b>{main}</b>{dead}<br/>{address}'
                         + (f'<br/><span class="alt" dir="ltr">{alt}</span>' if alt else "") + "</td>")
        else:
            dead = " (deceased)" if r["deceased"] else ""
            name_cell = (f'<td class="name"><b>{_esc(r["full_name"])}</b>{dead}<br/>{_esc(house)}'
                         + (f'<br/><span class="alt" dir="rtl">{_esc(r["full_name_dv"])}</span>'
                            if r["full_name_dv"] else "") + "</td>")
        parts.append(row([
            f'<td class="num">{n}</td>',
            f'<td class="photo"{right}>{img}</td>', name_cell,
            f'<td class="loc" dir="ltr">{_esc(r["location"])}</td>',
            f'<td class="phone" dir="ltr">{_esc(r["phone"])}</td>',
            '<td class="notes"><span class="box">[ ]</span></td>',
        ]))
    parts.append("</table>")

    from .fonts import css_for_pdf, mark_thaana

    font_css, font_blob = css_for_pdf()
    if font_blob:
        archive.add(font_blob, "dhivehi-font")
    story = pymupdf.Story(html=mark_thaana("".join(parts)) if font_blob else "".join(parts),
                          user_css=CSS + font_css, archive=archive)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page_rect = pymupdf.paper_rect("a4")
    body = page_rect + (28, 28, -28, -40)
    more = True
    while more:
        device = writer.begin_page(page_rect)
        more, _ = story.place(body)
        story.draw(device)
        writer.end_page()
    writer.close()

    # Footer on every page: who it is for, when, page number (traceable if it is forwarded)
    doc = pymupdf.open("pdf", buf.getvalue())
    stamp = _local_now().strftime("%d %b %Y %H:%M")
    note = f"For {recipient} · {stamp} · Do not forward" if recipient else f"{stamp} · Do not forward"
    for i, page in enumerate(doc, 1):
        page.insert_text((28, page.rect.height - 20), note, fontsize=7.5, color=(0.4, 0.4, 0.4))
        page.insert_text((page.rect.width - 70, page.rect.height - 20), f"Page {i} / {doc.page_count}",
                         fontsize=7.5, color=(0.4, 0.4, 0.4))
    doc.set_metadata({"title": title, "creator": "Gadhdhoo voter portal"})
    return doc.tobytes(garbage=3, deflate=True)
