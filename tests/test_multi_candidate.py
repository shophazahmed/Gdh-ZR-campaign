"""Settings and imports that let one code base serve many candidates (one instance each)."""
import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import ingest  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401


def test_campaign_name_from_env_then_settings(app):
    a = app.test_client()
    assert "Campaign voter portal" in a.get("/login").get_data(as_text=True)   # no name configured
    app.config["CAMPAIGN_NAME"] = "Madaveli"
    assert "Madaveli voter portal" in a.get("/login").get_data(as_text=True)
    assert "Gadhdhoo" not in a.get("/login").get_data(as_text=True)

    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/admin/settings")
    r = a.post("/admin/name", data={"csrf_token": token, "campaign_name": "  GDh  Thinadhoo "},
               follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Campaign name changed to GDh Thinadhoo." in page
    assert "GDh Thinadhoo <span class=\"brand-sub\">" in page
    assert "GDh Thinadhoo voter portal" in app.test_client().get("/login").get_data(as_text=True)
    assert a.post("/admin/name", data={"csrf_token": token, "campaign_name": " "},
                  follow_redirects=True).status_code == 200

    u = app.test_client()
    login(u, "viewer", USER_PW)
    token = csrf(u, "/")
    assert u.post("/admin/name", data={"csrf_token": token, "campaign_name": "Hacked"}).status_code == 403


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def test_generic_import_priority_by_file_name(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    header = ["ID Card", "Name", "Sex", "Age", "House", "Party"]
    _write(data / "2 old list.csv", [header, ["A300001", "Old Name", "M", "40", "Old House", "PNC"],
                                     ["A300002", "Only Old", "F", "51", "Sea View", "MDP"]])
    _write(data / "1 new list.csv", [header, ["A300001", "New Name", "M", "41", "", "MDP"]])
    db = tmp_path / "vp.db"
    assert ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "p"),
                        "--log", str(tmp_path / "log.txt"), "--no-seed-users"]) == 0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    v = {r["national_id"]: dict(r) for r in conn.execute("SELECT * FROM voters")}
    assert sorted(v) == ["A300001", "A300002"]
    assert v["A300001"]["full_name"] == "New Name" and v["A300001"]["party"] == "MDP"   # file 1 wins
    assert v["A300001"]["house_name"] == "Old House"                                  # blanks don't erase
    assert v["A300002"]["full_name"] == "Only Old"


def test_natural_order():
    names = [Path(n) for n in ("10 z.csv", "2 b.csv", "1 a.csv")]
    assert [p.name for p in sorted(names, key=ingest._natural_key)] == ["1 a.csv", "2 b.csv", "10 z.csv"]
