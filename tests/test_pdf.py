import csv
import io
import sys
from pathlib import Path

import pymupdf
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import ingest  # noqa: E402
from scripts.extract_from_pdf import extract_pdf  # noqa: E402

HEADER = ["#", "Photo", "ID Card", "Name", "Sex", "Age", "House", "Party"]
COL_X = [40, 70, 130, 210, 340, 380, 420, 520]
PEOPLE = [(f"A3000{i:02d}", f"Person Number{i}", "MF"[i % 2], 20 + i, f"House {i}", "AAA" if i % 3 else "BBB")
          for i in range(1, 15)]


def png(color):
    buf = io.BytesIO()
    Image.new("RGB", (60, 75), color).save(buf, "PNG")
    return buf.getvalue()


def make_pdf(path, ruled):
    doc = pymupdf.open()
    logo = png("black")
    per_page = 8
    for start in range(0, len(PEOPLE), per_page):
        page = doc.new_page(width=600, height=842)
        page.insert_text((40, 40), "GADHDHOO VOTER LIST 2026", fontsize=14)
        page.insert_image(pymupdf.Rect(540, 20, 580, 50), stream=logo)       # logo on every page
        y = 80
        if ruled or start == 0:  # plain layout prints the column titles on page 1 only
            for x, h in zip(COL_X, HEADER):
                page.insert_text((x, y), h, fontsize=9)
            if ruled:
                page.draw_rect(pymupdf.Rect(35, y - 12, 580, y + 6))
            y += 20
        for n, (nid, name, sex, age, house, party) in enumerate(PEOPLE[start:start + per_page]):
            row_h = 60
            if ruled:
                page.draw_rect(pymupdf.Rect(35, y - 12, 580, y - 12 + row_h))
                for x in COL_X[1:]:
                    page.draw_line((x - 5, y - 12), (x - 5, y - 12 + row_h))
            idx = start + n
            if idx % 4 != 3:  # some voters have no photo
                page.insert_image(pymupdf.Rect(70, y - 8, 110, y + 42), stream=png((idx * 15, 90, 160)))
            for x, val in zip(COL_X, [str(idx + 1), "", nid if idx != 5 else nid[0] + " " + nid[1:],
                                      name, sex, str(age), house, party]):
                page.insert_text((x, y + 10), val, fontsize=9)
            y += row_h
        page.insert_text((40, 820), f"Page {start // per_page + 1}", fontsize=8)
    doc.save(path)


def check(tmp_path, ruled):
    data = tmp_path / "data"
    data.mkdir()
    make_pdf(data / "Gahdhdhoo.2026.pdf", ruled)
    rows, photos = extract_pdf(data / "Gahdhdhoo.2026.pdf")
    assert rows == len(PEOPLE)
    assert photos == sum(1 for i in range(len(PEOPLE)) if i % 4 != 3)
    with open(data / "Gahdhdhoo.2026.csv", encoding="utf-8-sig") as fh:
        table = list(csv.DictReader(fh))
    by_id = {ingest.normalize_id(r.get("ID Card", ""))[0]: r for r in table}
    assert sorted(by_id) == sorted(p[0] for p in PEOPLE)
    assert by_id["A300006"]["Name"] == "Person Number6"   # ID printed with a space still matched
    assert by_id["A300001"]["House"] == "House 1" and by_id["A300003"]["Party"] == "BBB"
    assert by_id["A300004"]["Photo file"] == ""                 # no photo printed on that row
    # the CSV + photos go straight into the merge
    db = tmp_path / "vp.db"
    ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "ph"),
                 "--log", str(tmp_path / "log"), "--no-seed-users"])
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0] == len(PEOPLE)
    assert conn.execute("SELECT full_name, sex, age FROM voters WHERE national_id='A300002'").fetchone() == (
        "Person Number2", "M", 22)
    assert conn.execute("SELECT COUNT(*) FROM voters WHERE photo_path IS NOT NULL").fetchone()[0] == photos


def test_ruled_table(tmp_path):
    check(tmp_path, ruled=True)


def test_plain_text_columns(tmp_path):
    check(tmp_path, ruled=False)
