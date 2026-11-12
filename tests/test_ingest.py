import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.normalize import normalize_age, normalize_id, normalize_sex  # noqa: E402
from scripts import ingest  # noqa: E402
from tests.fixtures import build  # noqa: E402


@pytest.mark.parametrize("raw, expected", [
    ("A100001", "A100001"), (" a 100001 ", "A100001"), ("A-100001", "A100001"),
    (100001, "A100001"), ("100001.0", "A100001"), ("A12345", "A012345"), ("", None), ("hello", None),
])
def test_normalize_id(raw, expected):
    assert normalize_id(raw)[0] == expected


def test_normalize_sex_and_age():
    assert normalize_sex("female")[0] == "F"
    assert normalize_sex("ފިރިހެން")[0] == "M"
    assert normalize_sex("x") == ("", "unrecognised sex value 'x'")
    assert normalize_age("45 yrs")[0] == 45
    assert normalize_age(200)[0] is None


@pytest.fixture()
def run(tmp_path):
    data = build(tmp_path / "data")
    db, log = tmp_path / "vp.db", tmp_path / "merge_conflicts.log"
    out = tmp_path / "merged.csv"
    assert ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "photos"),
                        "--log", str(log), "--csv-out", str(out)]) == 0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    voters = {r["national_id"]: dict(r) for r in conn.execute("SELECT * FROM voters")}
    return voters, log.read_text(encoding="utf-8"), conn, tmp_path


def test_dedup_and_priority(run):
    voters, log, conn, _ = run
    assert sorted(voters) == [f"A10000{i}" for i in range(1, 10)]
    # 2026 list wins over new voters (age 34 vs 33), new voters' blank party doesn't erase AAA
    assert voters["A100001"]["age"] == 34 and voters["A100001"]["party"] == "AAA"
    # for the party, the updated party list beats even the 2026 list
    assert voters["A100002"]["party"] == "CCC"
    # "Updated Party" column is preferred over "Party", and the party list beats the old list
    assert voters["A100005"]["house_name"] == "Old House" and voters["A100005"]["party"] == "AAA"
    # cleaned values
    assert voters["A100004"]["full_name"] == "Hassan Demo"
    assert voters["A100004"]["party"] == "CCC" and voters["A100004"]["sex"] == "M"
    assert voters["A100003"]["sex"] == "F"


def test_photos(run):
    voters, _, _, tmp = run
    assert voters["A100001"]["photo_path"] == "A100001.jpg"   # embedded picture
    assert voters["A100002"]["photo_path"] == "A100002.jpg"   # embedded picture
    assert voters["A100004"]["photo_path"] == "A100004.jpg"   # Windows path in a column
    assert voters["A100009"]["photo_path"] == "A100009.jpg"   # file named after the ID
    assert voters["A100003"]["photo_path"] is None
    assert (tmp / "photos" / "A100001.jpg").is_file()


def test_conflict_log(run):
    _, log, _, _ = run
    for code in ("AMBIGUOUS_NAME_MISMATCH", "PARTY_CHANGED", "HOUSE_CHANGED", "DUPLICATE_IN_FILE",
                 "POSSIBLE_DUPLICATE_PERSON", "UNDER_18", "NO_VALID_ID", "ID_NORMALISED"):
        assert code in log, code
    assert "AGE_CONFLICT               A100006" in log   # 60 vs 30
    assert "AGE_CONFLICT               A100001" not in log  # 34 vs 33 is within a year


def test_seeded_users_and_rerun(run):
    _, _, conn, tmp = run
    users = {r["username"]: r["role"] for r in conn.execute("SELECT username, role FROM users")}
    assert users == {"admin": "admin", "user": "user"}
    # a voter edited in the portal survives a re-import
    conn.execute("UPDATE voters SET party = 'EDITED', updated_by = 1 WHERE national_id = 'A100002'")
    conn.commit()
    ingest.main(["--data-dir", str(tmp / "data"), "--db", str(tmp / "vp.db"),
                 "--photos-dir", str(tmp / "photos"), "--log", str(tmp / "log2")])
    assert conn.execute("SELECT party FROM voters WHERE national_id = 'A100002'").fetchone()[0] == "EDITED"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_mdp_list_membership(tmp_path):
    """Every voter on the MDP list is MDP; MDP voters missing from it have left MDP."""
    import csv
    from openpyxl import Workbook

    data = tmp_path / "data"
    data.mkdir()
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Name", "Sex", "Age", "House", "Party"])
    ws.append(["A400001", "Stays Mdp", "M", 40, "One", "MDP"])
    ws.append(["A400002", "Left Mdp", "F", 41, "Two", "M.D.P"])
    ws.append(["A400003", "Was Pnc", "M", 42, "Three", "PNC"])
    ws.append(["A400004", "Joins Mdp", "F", 43, "Four", "PNC"])
    ws.append(["A400005", "No Party", "M", 44, "Five", ""])
    wb.save(data / "final_cleaned_gadhdhoo_updated_party.xlsx")
    with open(data / "Gadhdhoo_updated_Party_list_MPD_.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["#", "National ID", "Name", "Phone", "House", "Atoll", "Island"])
        w.writerow([1, "A400001", "Stays Mdp", "", "One", "GDh", "Gadhdhoo"])
        w.writerow([2, "A400004", "Joins Mdp", "", "Four", "GDh", "Gadhdhoo"])
        w.writerow([3, "A400006", "Only On List", "", "Six", "GDh", "Gadhdhoo"])
    db, log = tmp_path / "vp.db", tmp_path / "log"
    ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "p"),
                 "--log", str(log), "--no-seed-users"])
    conn = sqlite3.connect(db)
    party = dict(conn.execute("SELECT national_id, party FROM voters"))
    assert party == {"A400001": "MDP", "A400002": "OTHER", "A400003": "PNC",
                     "A400004": "MDP", "A400005": "", "A400006": "MDP"}
    text = log.read_text()
    assert "PARTY_LEFT_MDP             A400002" in text and "A400003" not in text.split("PARTY_LEFT_MDP")[-1]
    assert "3 MDP (on the updated MDP list), 1 moved from MDP to OTHER" in text


def test_no_mdp_list_changes_nobody(tmp_path):
    from openpyxl import Workbook

    data = tmp_path / "data"
    data.mkdir()
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Name", "Party"])
    ws.append(["A400001", "Stays Mdp", "MDP"])
    wb.save(data / "final_cleaned_gadhdhoo_updated_party.xlsx")
    db = tmp_path / "vp.db"
    ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "p"),
                 "--log", str(tmp_path / "log"), "--no-seed-users"])
    assert sqlite3.connect(db).execute("SELECT party FROM voters").fetchone()[0] == "MDP"
    assert "MDP_LIST_NOT_LOADED" in (tmp_path / "log").read_text()


def test_phone_normalisation():
    from app.normalize import normalize_phone

    assert normalize_phone("7771234") == ("7771234", None)
    assert normalize_phone("9607902314")[0] == "7902314"      # +960 country code dropped
    assert normalize_phone("+960 777-1234")[0] == "7771234"
    assert normalize_phone(7771234.0)[0] == "7771234"
    assert normalize_phone("")[0] == "" and normalize_phone("n/a")[0] == ""
    assert normalize_phone("12")[0] == ""


def test_phone_import_and_old_database(tmp_path):
    import csv

    from app.db import connect

    # a database created before the phone column existed
    db = tmp_path / "vp.db"
    old = sqlite3.connect(db)
    old.executescript("""CREATE TABLE voters (national_id TEXT PRIMARY KEY, full_name TEXT NOT NULL,
        sex TEXT NOT NULL DEFAULT '', age INTEGER, house_name TEXT NOT NULL DEFAULT '',
        party TEXT NOT NULL DEFAULT '', photo_path TEXT, source TEXT NOT NULL DEFAULT 'import',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by INTEGER);
        INSERT INTO voters VALUES ('A500002','Keeps Phone','M',30,'X','',NULL,'import','t','t',NULL);""")
    old.commit()
    old.close()
    data = tmp_path / "data"
    data.mkdir()
    with open(data / "gadhoo-new voters.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["National ID", "Name", "Phone", "House"])
        w.writerow(["A500001", "Has Phone", "9607902314", "One"])
        w.writerow(["A500002", "Keeps Phone", "", "X"])
    conn = connect(db)
    from app.db import init_schema
    init_schema(conn)
    conn.execute("UPDATE voters SET phone = '7000001' WHERE national_id = 'A500002'")
    conn.commit()
    ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "p"),
                 "--log", str(tmp_path / "log"), "--no-seed-users"])
    phones = dict(conn.execute("SELECT national_id, phone FROM voters"))
    assert phones == {"A500001": "7902314", "A500002": "7000001"}  # a blank in the file keeps the old number


def test_mdp_list_alone_updates_voters_already_in_portal(tmp_path):
    import csv

    from openpyxl import Workbook

    db = tmp_path / "vp.db"
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Name", "Party"])
    ws.append(["A600001", "Stays", "MDP"])
    ws.append(["A600002", "Leaves", "MDP"])
    ws.append(["A600003", "Edited", "MDP"])
    ws.append(["A600004", "Pnc", "PNC"])
    wb.save(first / "Gahdhdhoo.2026.xlsx")
    args = ["--db", str(db), "--photos-dir", str(tmp_path / "p"), "--no-seed-users"]
    ingest.main(["--data-dir", str(first), "--log", str(tmp_path / "l1")] + args)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE voters SET updated_by = 1 WHERE national_id = 'A600003'")  # edited in the portal
    conn.commit()
    with open(second / "Gadhdhoo_updated_Party_list_MPD_.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["#", "National ID", "Name", "Phone", "House"])
        w.writerow([1, "A600001", "Stays", "7771234", "One"])
    ingest.main(["--data-dir", str(second), "--log", str(tmp_path / "dry"), "--dry-run"] + args)
    assert "Also 1 voters already in the portal would move" in (tmp_path / "dry").read_text()
    ingest.main(["--data-dir", str(second), "--log", str(tmp_path / "l2")] + args)
    rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT national_id, party, phone FROM voters")}
    assert rows == {"A600001": ("MDP", "7771234"), "A600002": ("OTHER", ""),
                    "A600003": ("MDP", ""), "A600004": ("PNC", "")}


def test_imports_keep_canvassing(tmp_path):
    from openpyxl import Workbook

    data = tmp_path / "data"
    data.mkdir()

    def write(rows):
        wb = Workbook()
        ws = wb.active
        ws.append(["ID", "Name", "Party"])
        for r in rows:
            ws.append(r)
        wb.save(data / "Gahdhdhoo.2026.xlsx")

    db = tmp_path / "vp.db"
    args = ["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "p"),
            "--log", str(tmp_path / "log"), "--no-seed-users"]
    write([["A700001", "One", "MDP"], ["A700002", "Two", "PNC"], ["A700003", "Gone", "PNC"]])
    ingest.main(args)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE voters SET status = 'contacted', notes = 'n1' WHERE national_id = 'A700001'")
    conn.execute("UPDATE voters SET status = 'followup', notes = 'n2', updated_by = 1 WHERE national_id = 'A700002'")
    conn.commit()
    write([["A700001", "One Renamed", "MDP"], ["A700002", "Two Renamed", "MDP"]])
    ingest.main(args + ["--mode", "replace"])
    rows = {r[0]: r[1:] for r in conn.execute("SELECT national_id, full_name, party, status, notes FROM voters")}
    assert rows == {"A700001": ("One Renamed", "MDP", "contacted", "n1"),
                    "A700002": ("Two Renamed", "MDP", "followup", "n2")}  # replace overwrites hand edits too


def test_party_only_file_keeps_other_details(tmp_path):
    import csv

    db = tmp_path / "vp.db"
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with open(first / "Gahdhdhoo.2026.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID Card No.", "Name", "Sex", "Age", "House", "Party"])
        w.writerow(["A800001", "Joins", "M", 40, "One", "PPM"])
        w.writerow(["A800002", "Leaves", "F", 35, "Two", "MDP"])
    args = ["--db", str(db), "--photos-dir", str(tmp_path / "p"), "--no-seed-users"]
    ingest.main(["--data-dir", str(first), "--log", str(tmp_path / "l1")] + args)
    with open(second / "gadhoo-new voters.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID Card No.", "Name", "Sex", "Age", "House", "Party"])
        w.writerow(["A800001", "", "", "", "", "MDP"])
        w.writerow(["A800002", "", "", "", "", "OTHER"])
    ingest.main(["--data-dir", str(second), "--log", str(tmp_path / "l2")] + args)
    conn = sqlite3.connect(db)
    rows = {r[0]: r[1:] for r in conn.execute("SELECT national_id, full_name, sex, age, house_name, party FROM voters")}
    assert rows == {"A800001": ("Joins", "M", 40, "One", "MDP"), "A800002": ("Leaves", "F", 35, "Two", "OTHER")}


def test_location_column_imported_and_kept(tmp_path):
    import csv

    db = tmp_path / "vp.db"
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with open(first / "Gahdhdhoo.2026.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID Card No.", "Name", "Sex", "Age", "House", "Location"])
        w.writerow(["A900001", "Boxed", "M", 40, "One", "Gdh. Gadhdhoo"])
    args = ["--db", str(db), "--photos-dir", str(tmp_path / "p"), "--no-seed-users"]
    ingest.main(["--data-dir", str(first), "--log", str(tmp_path / "l1")] + args)
    with open(second / "gadhoo-new voters.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID Card No.", "Name", "Party"])
        w.writerow(["A900001", "Boxed", "MDP"])
    ingest.main(["--data-dir", str(second), "--log", str(tmp_path / "l2")] + args)
    row = sqlite3.connect(db).execute("SELECT location, party FROM voters").fetchone()
    assert row == ("Gdh. Gadhdhoo", "MDP")  # a file without a location keeps the stored one


def test_source_files_with_extra_words_in_the_name(tmp_path):
    import csv

    db = tmp_path / "vp.db"
    for name in ("gadhoo-new voters (1).csv", "gadhoo-new voters - sex.csv"):
        data = tmp_path / name.replace(" ", "_")
        data.mkdir()
        with open(data / name, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ID Card No.", "Name", "Sex"])
            w.writerow(["A910001", "Renamed File", "F"])
        ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "p"),
                     "--log", str(tmp_path / "log"), "--no-seed-users"])
    row = sqlite3.connect(db).execute("SELECT full_name, sex FROM voters").fetchone()
    assert row == ("Renamed File", "F")
