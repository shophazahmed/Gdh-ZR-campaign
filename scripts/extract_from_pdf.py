#!/usr/bin/env python3
"""Turn a voter-list PDF into a CSV plus one photo per voter, ready for ingest.py.

    pip install pymupdf
    python scripts/extract_from_pdf.py                       # every PDF in ./Data files
    python scripts/extract_from_pdf.py --data-dir "C:\\...\\Data files"
    python scripts/extract_from_pdf.py --inspect             # show the page layout, text masked

For each <name>.pdf it writes, next to the PDF:
    <name>.csv                 one row per voter (ID, name, sex, age, house, party, ... as in the PDF)
    photos/<ID>.jpg            the picture printed on that voter's row
so `ingest.py --data-dir <same folder>` (or the portal's Import page) picks both up.
A PDF named like a source (e.g. Gahdhdhoo.2026.pdf) gets that source's priority.

How rows are found
    1. PyMuPDF's table detection (ruled tables).
    2. If that finds no ID cards, the words are grouped into lines and cells by position.
Every row must contain an ID card number (A123456); lines without one are ignored, so
page headers, footers and repeated column titles drop out by themselves.

Photos: each picture on the page goes to the ID row it overlaps vertically (logos repeated
on every page and tiny icons are skipped).

Thaana text: some PDFs store right-to-left text in visual order, so names come out
reversed. If they look backwards in the CSV, run again with --reverse-rtl.
The output contains personal data: keep it out of git and off shared drives.
"""
import argparse
import csv
import io
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ID_IN_TEXT = re.compile(r"\b[Aa]\s?-?\s?\d{6}\b")
THAANA = re.compile(r"[\u0780-\u07BF]")
MIN_PHOTO_PT = 18          # pictures smaller than this (points) are icons
REPEATED_IMAGE_SHARE = 0.5  # an image on more than half the pages is a logo


def role_for_header(text):
    from scripts.ingest import role_for_header as rfh
    return rfh(text)


def fix_rtl(text, reverse):
    if reverse and THAANA.search(text or ""):
        return " ".join(word[::-1] for word in reversed(text.split()))
    return text


def norm_id(text):
    m = ID_IN_TEXT.search(text or "")
    return re.sub(r"[\s\-]", "", m.group()).upper() if m else None


# --- strategy 1: PyMuPDF table detection ----------------------------------------------

def rows_from_tables(page):
    """Return (header or None, [(cells, y0, y1)]) from page.find_tables(); cells are (text, x0, x1)."""
    header, rows = None, []
    try:
        tables = page.find_tables().tables
    except Exception:
        return None, []
    for t in tables:
        data = t.extract()
        for i, texts in enumerate(data):
            if i >= len(t.rows):
                break
            row = t.rows[i]
            cells = []
            for text, box in zip(texts, row.cells):
                if box is None:
                    continue
                cells.append(((text or "").replace("\n", " ").strip(), box[0], box[2]))
            if any(norm_id(c[0]) for c in cells):
                rows.append((cells, row.bbox[1], row.bbox[3]))
            elif header is None and sum(1 for c in cells if role_for_header(c[0])) >= 2:
                header = cells
    return header, rows


# --- strategy 2: words -> lines -> cells ------------------------------------------------

def _lines(page):
    words = page.get_text("words")  # x0, y0, x1, y1, text, block, line, word
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines, current, cy = [], [], None
    for w in words:
        mid = (w[1] + w[3]) / 2
        h = max(w[3] - w[1], 1)
        if current and abs(mid - cy) > h * 0.5:
            lines.append(current)
            current = []
        current.append(w)
        cy = mid if len(current) == 1 else (cy * (len(current) - 1) + mid) / len(current)
    if current:
        lines.append(current)
    return [sorted(line, key=lambda w: w[0]) for line in lines]


def _cells(line):
    """Split a line into cells where the gap between words is wider than ~1.5 spaces."""
    cells, cur = [], [line[0]]
    for prev, w in zip(line, line[1:]):
        size = max(prev[3] - prev[1], 4)
        if w[0] - prev[2] > size * 0.9:
            cells.append(cur)
            cur = []
        cur.append(w)
    cells.append(cur)
    return [(" ".join(w[4] for w in c), c[0][0], c[-1][2]) for c in cells]


def rows_from_words(page):
    """Return (header [(text, x0, x1)] or None, [(cells [(text,x0,x1)], y0, y1)])."""
    header, rows = None, []
    for line in _lines(page):
        cells = _cells(line)
        y0, y1 = min(w[1] for w in line), max(w[3] for w in line)
        if any(norm_id(c[0]) for c in cells):
            rows.append((cells, y0, y1))
        elif header is None and not rows and sum(1 for c in cells if role_for_header(c[0])) >= 2:
            header = cells
    return header, rows


def align_to_header(header, cells):
    """Put positioned cells under the header column they overlap most."""
    out = [""] * len(header)
    for text, x0, x1 in cells:
        best, best_overlap = None, -1e9
        for i, (_, hx0, hx1) in enumerate(header):
            overlap = min(x1, hx1) - max(x0, hx0)
            if overlap <= 0:
                overlap = -min(abs(x0 - hx1), abs(hx0 - x1))  # distance, as a negative score
            if overlap > best_overlap:
                best, best_overlap = i, overlap
        out[best] = (out[best] + " " + text).strip()
    return out


# --- photos -----------------------------------------------------------------------------

def page_images(doc, page):
    out = []
    for info in page.get_image_info(xrefs=True):
        x0, y0, x1, y1 = info["bbox"]
        if min(x1 - x0, y1 - y0) < MIN_PHOTO_PT or not info.get("xref"):
            continue
        out.append((info["xref"], x0, y0, x1, y1))
    return out


def assign_photos(images, rows):
    """Map row index -> xref for the picture that overlaps the row band the most."""
    if not rows:
        return {}
    bands = []
    ordered = sorted(range(len(rows)), key=lambda i: rows[i][1])
    for k, i in enumerate(ordered):
        top = rows[i][1]
        bottom = rows[ordered[k + 1]][1] if k + 1 < len(ordered) else max(rows[i][2], top) + 200
        prev_bottom = rows[ordered[k - 1]][2] if k else top - 200
        bands.append((i, min(top, max(prev_bottom, top - 4)), bottom))
    chosen = {}
    for xref, x0, y0, x1, y1 in images:
        cy = (y0 + y1) / 2
        best = None
        for i, top, bottom in bands:
            if top <= cy < bottom:
                best = i
                break
        if best is None:
            overlaps = [(min(y1, b) - max(y0, t), i) for i, t, b in bands]
            ov, i = max(overlaps)
            best = i if ov > 0 else None
        if best is not None and best not in chosen:
            chosen[best] = xref
    return chosen


def save_photo(doc, xref, path):
    from PIL import Image

    pix_info = doc.extract_image(xref)
    img = Image.open(io.BytesIO(pix_info["image"]))
    img.load()
    img = img.convert("RGB")
    img.thumbnail((800, 800))
    img.save(path, "JPEG", quality=85)


# --- per PDF ------------------------------------------------------------------------

def extract_pdf(pdf_path, out_dir=None, reverse_rtl=False, say=print):
    """Write <stem>.csv and photos/<ID>.jpg next to the PDF (or in out_dir). Returns (rows, photos)."""
    import pymupdf

    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir or pdf_path.parent)
    photos_dir = out_dir / "photos"
    doc = pymupdf.open(pdf_path)

    image_pages = Counter()
    for page in doc:
        for xref in {x[0] for x in page_images(doc, page)}:
            image_pages[xref] += 1
    logo_like = {x for x, n in image_pages.items() if doc.page_count > 2 and n > doc.page_count * REPEATED_IMAGE_SHARE}

    header_names, last_header, records, strategy = None, None, [], Counter()
    seen_ids = set()
    photo_count = 0
    for pno, page in enumerate(doc, start=1):
        header, rows = rows_from_tables(page)
        if rows:
            strategy["tables"] += 1
        else:
            header, rows = rows_from_words(page)
            strategy["words"] += 1
        if header is None:
            header = rows_from_words(page)[0]  # titles printed outside the ruled table
        if header:
            header_names = header_names or [h[0] for h in header]
            last_header = header
        page_header = header or last_header  # later pages rarely repeat the column titles
        if page_header:
            texts = [align_to_header(page_header, cells) for cells, _, _ in rows]
        else:
            texts = [[c[0] for c in cells] for cells, _, _ in rows]

        images = [im for im in page_images(doc, page) if im[0] not in logo_like]
        photos = assign_photos(images, rows)
        for i, cells in enumerate(texts):
            cells = [fix_rtl(c, reverse_rtl) for c in cells]
            nid = next((norm_id(c) for c in cells if norm_id(c)), None)
            if not nid:
                continue
            photo = ""
            if i in photos and nid not in seen_ids:
                photos_dir.mkdir(parents=True, exist_ok=True)
                try:
                    save_photo(doc, photos[i], photos_dir / f"{nid}.jpg")
                    photo = f"photos/{nid}.jpg"
                    photo_count += 1
                except Exception as exc:  # unreadable image data
                    say(f"  page {pno}: picture for {nid} could not be read ({exc})")
            seen_ids.add(nid)
            records.append((pno, cells, photo))

    width = max((len(c) for _, c, _ in records), default=0)
    if header_names and len(header_names) >= width:
        columns = [h or f"col{i + 1}" for i, h in enumerate(header_names)]
    else:
        columns = (list(header_names or []) + [f"col{i + 1}" for i in range(width)])[:width]
    out_csv = out_dir / f"{pdf_path.stem}.csv"
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(columns + ["Photo file", "PDF page"])
        for pno, cells, photo in records:
            w.writerow(list(cells) + [""] * (len(columns) - len(cells)) + [photo, pno])

    roles = sorted({r for r in (role_for_header(c) for c in columns) if r})
    say(f"{pdf_path.name}: {len(records)} voter rows on {doc.page_count} pages, {photo_count} photos "
        f"(read with {', '.join(f'{k} on {v} page(s)' for k, v in strategy.items())})")
    say(f"  columns: {columns}")
    if "full_name" not in roles:
        say("  ! no column header was recognised as the name; check the CSV and rename the header to 'Name'")
    if records and THAANA.search(" ".join(" ".join(c) for _, c, _ in records[:20])) and not reverse_rtl:
        say("  Thaana text found: if names look reversed in the CSV, run again with --reverse-rtl")
    say(f"  wrote {out_csv.name}" + (f" and {photo_count} files in photos/" if photo_count else ""))
    return len(records), photo_count


def inspect(pdf_path, pages=2):
    """Print the layout of the first pages with letters masked, safe to share for debugging."""
    import pymupdf

    def mask(s):
        return re.sub(r"[0-9]", "9", re.sub(r"[^\W\d_]", "x", s))

    doc = pymupdf.open(pdf_path)
    for pno, page in enumerate(doc, start=1):
        if pno > pages:
            break
        print(f"--- page {pno} ({page.rect.width:.0f}x{page.rect.height:.0f}pt), "
              f"{len(page_images(doc, page))} pictures, tables found: {len(page.find_tables().tables)}")
        for line in _lines(page)[:40]:
            cells = _cells(line)
            # Only a column-title line (no ID, several header words) is shown as is
            titles = not any(norm_id(c[0]) for c in cells) and sum(1 for c in cells if role_for_header(c[0])) >= 2
            print(f"y={line[0][1]:6.1f} | " + " | ".join(
                f"{c[1]:.0f}:{c[0] if titles else mask(c[0])}" for c in cells))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="*", help="PDF files (default: every PDF in --data-dir)")
    ap.add_argument("--data-dir", help="folder with the PDFs (default: ./Data files or ../Data files)")
    ap.add_argument("--reverse-rtl", action="store_true", help="reverse Thaana text that comes out backwards")
    ap.add_argument("--inspect", action="store_true", help="print the layout of the first 2 pages (text masked)")
    args = ap.parse_args(argv)

    pdfs = [Path(p) for p in args.pdfs]
    if not pdfs:
        candidates = [Path(args.data_dir)] if args.data_dir else [Path("Data files"), Path("../Data files")]
        folder = next((c for c in candidates if c.is_dir()), None)
        if folder is None:
            ap.error("no PDFs given and no 'Data files' folder here; use --data-dir")
        pdfs = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".pdf")
        if not pdfs:
            ap.error(f"no .pdf files in {folder}")
    for pdf in pdfs:
        if args.inspect:
            inspect(pdf)
        else:
            extract_pdf(pdf, reverse_rtl=args.reverse_rtl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
