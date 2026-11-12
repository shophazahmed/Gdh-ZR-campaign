import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import connect, now_iso  # noqa: E402

ADMIN_PW, USER_PW = "admin-pass-123", "user-pass-1234"


@pytest.fixture()
def app(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "test", "DATABASE": str(tmp_path / "t.db"),
                      "PHOTOS_DIR": str(tmp_path / "photos"), "IMPORTS_DIR": str(tmp_path / "imports"),
                      "CAMPAIGN_DIR": str(tmp_path / "campaign"), "SESSION_COOKIE_SECURE": False})
    conn = connect(app.config["DATABASE"])
    stamp = now_iso(precise=True)
    for name, role, pw in (("boss", "admin", ADMIN_PW), ("viewer", "user", USER_PW)):
        conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                     " created_at) VALUES (?, ?, ?, 0, ?, ?)", (name, hash_password(pw), role, stamp, stamp))
    for nid, name, sex, age, house, party in (
        ("A200001", "Aminath Alpha", "F", 30, "Rose Villa", "AAA"),
        ("A200002", "Moosa Beta", "M", 55, "Blue Ge", "BBB"),
        ("A200003", "=HYPERLINK(1)", "M", 70, "Rose Villa", ""),
    ):
        conn.execute("INSERT INTO voters (national_id, full_name, sex, age, house_name, party, created_at, updated_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (nid, name, sex, age, house, party, stamp, stamp))
    conn.commit()
    conn.close()
    return app


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password,
                                       "csrf_token": csrf(client)})


def test_requires_login(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.get("/voters/A200001").status_code == 302
    assert c.get("/export.csv").status_code == 302


def test_csrf_enforced(app):
    c = app.test_client()
    c.get("/login")
    creds = {"username": "boss", "password": ADMIN_PW}
    # No token and no proof the form is ours: back to sign-in, not signed in
    r = c.post("/login", data=creds)
    assert r.status_code == 302 and "check=1" not in r.headers["Location"]
    assert c.get("/").status_code == 302
    r = c.post("/login", data=creds, headers={"Origin": "https://evil.example"})
    assert "check=1" not in r.headers["Location"]
    assert c.get("/").status_code == 302
    # Other forms still need the token
    assert c.post("/logout").status_code == 400


def test_login_without_cookie_from_own_page(app):
    """Brave / private tabs can post the sign-in form without the session cookie."""
    c = app.test_client()
    r = c.post("/login", data={"username": "boss", "password": ADMIN_PW},
               headers={"Origin": "http://localhost"})
    assert r.status_code == 302 and "check=1" in r.headers["Location"]
    r = c.get(r.headers["Location"])  # cookie kept: sent on to the portal
    assert r.status_code == 302 and "/login" not in r.headers["Location"]
    # A browser that dropped the cookie is told why instead of looping
    fresh = app.test_client()
    page = fresh.get("/login?check=1&next=/").get_data(as_text=True)
    assert "blocking cookies" in page


def test_user_is_read_only(app):
    c = app.test_client()
    assert login(c, "viewer", USER_PW).status_code == 302
    html = c.get("/?party=AAA").get_data(as_text=True)
    assert "Aminath Alpha" in html and "Moosa Beta" not in html
    assert "Export" not in html
    assert c.get("/?sex=M&age_min=50&age_max=60").get_data(as_text=True).count('id="v-A2000') == 1
    assert c.get("/?house=rose").get_data(as_text=True).count('id="v-A2000') == 2
    assert c.get("/?national_id=a200002").get_data(as_text=True).count('id="v-A2000') == 1
    assert c.get("/").get_data(as_text=True).count('id="v-A2000') == 3          # can browse the list
    assert c.get("/voters/new").status_code == 403
    assert c.get("/voters/A200001").status_code == 200
    token = csrf(c, "/")
    for method, path in (("get", "/export.csv"), ("get", "/export.xlsx"), ("get", "/voters/new"),
                         ("get", "/voters/A200001/edit"), ("post", "/voters/A200001/delete"),
                         ("get", "/admin/users"), ("get", "/admin/audit")):
        kwargs = {"data": {"csrf_token": token}} if method == "post" else {}
        assert getattr(c, method)(path, **kwargs).status_code == 403, path


def test_admin_crud_export_and_audit(app):
    c = app.test_client()
    login(c, "boss", ADMIN_PW)
    token = csrf(c, "/")
    r = c.post("/voters/new", data={"csrf_token": token, "national_id": "a 200009", "full_name": " New  Person ",
                                    "sex": "F", "age": "22", "house_name": "X", "party": "ccc"})
    assert r.status_code == 302
    page = c.get("/voters/A200009").get_data(as_text=True)
    assert "New Person" in page and "CCC" in page
    assert c.post("/voters/new", data={"csrf_token": token, "national_id": "A200009", "full_name": "Dup"}).status_code == 400
    r = c.post("/voters/A200009/edit", data={"csrf_token": token, "full_name": "New Person", "sex": "F",
                                             "age": "23", "house_name": "X", "party": "CCC"})
    assert r.status_code == 302
    csv_text = c.get("/export.csv?house=rose").get_data(as_text=True)
    assert csv_text.startswith("﻿#,ID,") and "'=HYPERLINK(1)" in csv_text and "Moosa" not in csv_text
    xlsx = c.get("/export.xlsx")
    assert xlsx.status_code == 200 and xlsx.data[:2] == b"PK"
    assert c.post("/voters/A200009/delete", data={"csrf_token": token}).status_code == 302
    assert c.get("/voters/A200009").status_code == 404
    audit = c.get("/admin/audit").get_data(as_text=True)
    for action in ("voter_create", "voter_update", "voter_delete", "export", "login"):
        assert action in audit


def test_user_management(app):
    c = app.test_client()
    login(c, "boss", ADMIN_PW)
    token = csrf(c, "/admin/users")
    r = c.post("/admin/users", data={"csrf_token": token, "username": "newbie", "role": "user", "password": ""},
               follow_redirects=True)
    temp = re.search(r"shown once\): (\S+)<", r.get_data(as_text=True)).group(1)
    # first sign-in forces a password change
    n = app.test_client()
    login(n, "newbie", temp)
    assert n.get("/").headers["Location"].endswith("/account/password")
    # deactivation ends the session
    conn = connect(app.config["DATABASE"])
    uid = conn.execute("SELECT id FROM users WHERE username = 'newbie'").fetchone()[0]
    c.post(f"/admin/users/{uid}", data={"csrf_token": token, "action": "toggle_active"})
    assert n.get("/account/password").status_code == 302
    # the only admin can't demote or deactivate themselves
    boss = conn.execute("SELECT id FROM users WHERE username = 'boss'").fetchone()[0]
    c.post(f"/admin/users/{boss}", data={"csrf_token": token, "action": "set_role", "role": "user"})
    c.post(f"/admin/users/{boss}", data={"csrf_token": token, "action": "toggle_active"})
    assert tuple(conn.execute("SELECT role, is_active FROM users WHERE id = ?", (boss,)).fetchone()) == ("admin", 1)


def test_lockout(app):
    c = app.test_client()
    for _ in range(5):
        assert login(c, "viewer", "wrong-password").status_code == 401
    r = login(c, "viewer", USER_PW)
    assert r.status_code == 401 and "Too many failed attempts" in r.get_data(as_text=True)


def test_security_headers(app):
    r = app.test_client().get("/login")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_bootstrap_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("VOTER_PORTAL_ADMIN_PASSWORD", "first-admin-pass-1")
    cfg = {"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / "b.db"), "PHOTOS_DIR": str(tmp_path / "p"),
           "IMPORTS_DIR": str(tmp_path / "i"), "CAMPAIGN_DIR": str(tmp_path / "c")}
    create_app(cfg)
    create_app(cfg)  # second start: no duplicate, no error
    conn = connect(cfg["DATABASE"])
    rows = conn.execute("SELECT username, role, must_change_password FROM users").fetchall()
    assert [tuple(r) for r in rows] == [("admin", "admin", 1)]


def test_phone_visible_searchable_editable(app):
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET phone = '7771234' WHERE national_id = 'A200001'")
    conn.commit()
    u = app.test_client()
    login(u, "viewer", USER_PW)
    page = u.get("/?q=777123").get_data(as_text=True)
    assert 'href="tel:7771234"' in page and page.count('id="v-A2000') == 1
    assert "7771234" in u.get("/voters/A200001").get_data(as_text=True)

    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/")
    a.post("/voters/A200002/edit", data={"csrf_token": token, "full_name": "Moosa Beta", "sex": "M", "age": "55",
                                         "house_name": "Blue Ge", "party": "BBB", "phone": "+960 912-3456"})
    assert conn.execute("SELECT phone FROM voters WHERE national_id = 'A200002'").fetchone()[0] == "9123456"
    assert "voter_update" in a.get("/admin/audit?target=A200002").get_data(as_text=True)
    assert "Phone" in a.get("/export.csv").get_data(as_text=True).splitlines()[0]


def test_canvassing_status_and_notes(app):
    u = app.test_client()
    login(u, "viewer", USER_PW)  # read-only users canvass too
    token = csrf(u, "/")
    page = u.get("/?q=A2000").get_data(as_text=True)
    assert "Not contacted <b data-count=\"new\">3</b>" in page

    # background save from a card
    r = u.post("/voters/A200001/canvass", data={"csrf_token": token, "status": "contacted"},
               headers={"X-Requested-With": "fetch"})
    reply = r.get_json()
    assert reply.pop("at")
    assert reply == {"ok": True, "changed": True, "status": "contacted", "label": "Contacted", "by": "viewer"}
    # status + notes from the voter page (normal form post)
    r = u.post("/voters/A200002/canvass", data={"csrf_token": token, "status": "followup",
                                                "note": " Wants a call after 5pm \r\n", "back": "/voters/A200002"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/voters/A200002")
    detail = u.get("/voters/A200002").get_data(as_text=True)
    assert "Wants a call after 5pm" in detail and "Your notes" in detail

    assert u.get("/?q=A2000&status=contacted").get_data(as_text=True).count('id="v-A2000') == 1
    page = u.get("/?q=A2000&status=followup").get_data(as_text=True)
    assert page.count('id="v-A2000') == 1 and "Wants a call after 5pm" in page
    assert u.get("/?q=after+5pm").get_data(as_text=True).count('id="v-A2000') == 1  # notes are searchable
    assert u.post("/voters/A200001/canvass", data={"csrf_token": token, "status": "bogus"}).status_code == 400
    # still read-only for everything else
    assert u.post("/voters/A200001/edit", data={"csrf_token": token, "full_name": "X"}).status_code == 403

    conn = connect(app.config["DATABASE"])
    row = conn.execute("SELECT status, notes, updated_by FROM voters WHERE national_id = 'A200002'").fetchone()
    assert tuple(row) == ("followup", "Wants a call after 5pm", None)  # canvassing doesn't count as an edit

    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    assert "voter_canvass" in a.get("/admin/audit").get_data(as_text=True)
    header = a.get("/export.csv").get_data(as_text=True).splitlines()[0]
    assert "Status,Status changed,Status by,Notes" in header


def test_pages_and_housemates(app):
    conn = connect(app.config["DATABASE"])
    stamp = now_iso()
    for i in range(60):
        conn.execute("INSERT INTO voters (national_id, full_name, house_name, created_at, updated_at)"
                     " VALUES (?, ?, 'Big House', ?, ?)", (f"A3{i:05d}", f"Person {i:02d}", stamp, stamp))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    page = a.get("/").get_data(as_text=True)
    assert page.count('class="vcard ') == 50 and "Voters per page" in page and 'name="page"' in page
    assert "showing 1–50" in page
    page = a.get("/?n=25&page=3").get_data(as_text=True)
    assert page.count('class="vcard ') == 13 and "showing 51–63" in page
    assert a.get("/?n=100").get_data(as_text=True).count('class="vcard ') == 63
    assert a.get("/?n=7").get_data(as_text=True).count('class="vcard ') == 50       # only the offered sizes
    assert a.get("/?page=999").get_data(as_text=True).count('class="vcard ') == 13    # clamped to the last page

    detail = a.get("/voters/A300000").get_data(as_text=True)
    assert "Also at Big House" in detail and detail.count('class="mate ') == 59 and "Person 59" in detail
    detail = a.get("/voters/A200001").get_data(as_text=True)   # Rose Villa has two voters
    assert detail.count('class="mate ') == 1


def test_location_filter_detail_and_export(app):
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Male hub' WHERE national_id = 'A200002'")
    conn.commit()
    conn.close()
    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    html = client.get("/?loc=Male+hub").get_data(as_text=True)
    assert "Moosa Beta" in html and "Aminath Alpha" not in html
    assert "<option selected>Male hub</option>" in html
    html = client.get("/?loc=(none)").get_data(as_text=True)
    assert "Aminath Alpha" in html and "Moosa Beta" not in html
    assert "Male hub" in client.get("/voters/A200002").get_data(as_text=True)
    csv_text = client.get("/export.csv?loc=Male+hub").get_data(as_text=True)
    assert "Location" in csv_text.splitlines()[0] and "Male hub" in csv_text


def test_deceased_marker(app):
    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET phone = '7771234' WHERE national_id = 'A200002'")
    conn.commit()
    form = {"csrf_token": csrf(client, "/voters/A200002/edit"), "full_name": "Moosa Beta", "sex": "M", "age": "55",
            "house_name": "Blue Ge", "party": "BBB", "phone": "7771234", "location": "", "deceased": "1"}
    assert client.post("/voters/A200002/edit", data=form).status_code == 302
    assert conn.execute("SELECT deceased FROM voters WHERE national_id = 'A200002'").fetchone()[0] == 1
    page = client.get("/voters/A200002").get_data(as_text=True)
    assert "Deceased" in page and "contact-wide" not in page  # no call / message buttons
    assert "Moosa Beta" not in client.get("/?dc=0").get_data(as_text=True)
    assert "Moosa Beta" in client.get("/?dc=1").get_data(as_text=True)
    csv_text = client.get("/export.csv?dc=1").get_data(as_text=True)
    import csv as _csv
    rows = list(_csv.DictReader(csv_text.lstrip("\ufeff").splitlines()))
    assert rows[0]["Deceased"] == "yes"
    conn.close()


def test_schema_upgrade_tolerates_concurrent_workers(tmp_path):
    from app.db import init_schema

    db = str(tmp_path / "race.db")
    init_schema(connect(db))  # the first worker added every new column

    class Stale:
        """The second worker read the column list before the first one added the new columns."""
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            if sql.startswith("PRAGMA table_info(voters)"):
                return [r for r in self.conn.execute(sql) if r[1] not in ("location", "deceased")]
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    init_schema(Stale(connect(db)))  # must not raise "duplicate column name"


def _post_user(client, uid, **data):
    return client.post(f"/admin/users/{uid}", data={"csrf_token": csrf(client, "/admin/users"), **data})


def test_agents_without_sign_in_assign_move_delete(app):
    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    token = csrf(client, "/admin/users")
    client.post("/admin/agents", data={"csrf_token": token, "name": "Ali Ahmed", "phone": "9607771234", "note": ""})
    conn = connect(app.config["DATABASE"])
    ali = conn.execute("SELECT * FROM users WHERE username = 'Ali Ahmed'").fetchone()
    assert ali["role"] == "agent" and ali["can_login"] == 0 and ali["phone"] == "7771234"
    # no sign-in, even with a guessed password
    other = app.test_client()
    r = other.post("/login", data={"username": "Ali Ahmed", "password": "x" * 12, "csrf_token": csrf(other)})
    assert "/login" in r.headers.get("Location", "/login") and other.get("/").status_code in (302, 401)
    # voters can go to an agent or to a read-only user
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    assert client.post("/voters/assign", data={"csrf_token": csrf(client, "/"), "agent": str(ali["id"]),
                                               "ids": ["A200001", "A200002"]}).status_code == 302
    assert client.post("/voters/assign", data={"csrf_token": csrf(client, "/"), "agent": str(viewer),
                                               "ids": ["A200003"]}).status_code == 302
    conn.execute("INSERT INTO house_assignments VALUES ('Rose Villa', ?, 't', NULL)", (ali["id"],))
    conn.execute("UPDATE voters SET status = 'contacted', status_by = ? WHERE national_id = 'A200001'", (ali["id"],))
    conn.commit()
    # move all of Ali's work to the user
    _post_user(client, ali["id"], action="move_work", to=str(viewer))
    assert conn.execute("SELECT COUNT(*) FROM voter_assignments WHERE user_id = ?", (viewer,)).fetchone()[0] == 3
    assert conn.execute("SELECT user_id FROM house_assignments").fetchone()[0] == viewer
    # delete needs the typed name; history keeps the name, the name is free again
    _post_user(client, ali["id"], action="delete", confirm="wrong")
    assert conn.execute("SELECT deleted_at FROM users WHERE id = ?", (ali["id"],)).fetchone()[0] is None
    _post_user(client, ali["id"], action="delete", confirm="ali ahmed")
    row = conn.execute("SELECT username, deleted_at, is_active FROM users WHERE id = ?", (ali["id"],)).fetchone()
    assert row["deleted_at"] and not row["is_active"] and row["username"] == "Ali Ahmed (deleted)"
    assert conn.execute("SELECT status_by FROM voters WHERE national_id = 'A200001'").fetchone()[0] == ali["id"]
    page = client.get("/admin/users").get_data(as_text=True)
    assert "Ali Ahmed (deleted)" not in page
    client.post("/admin/agents", data={"csrf_token": csrf(client, "/admin/users"), "name": "Ali Ahmed"})
    assert conn.execute("SELECT COUNT(*) FROM users WHERE username = 'Ali Ahmed'").fetchone()[0] == 1
    conn.close()


def test_cannot_delete_self_or_owner(app):
    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    conn = connect(app.config["DATABASE"])
    boss = conn.execute("SELECT id FROM users WHERE username = 'boss'").fetchone()[0]
    _post_user(client, boss, action="delete", confirm="boss")
    assert conn.execute("SELECT deleted_at FROM users WHERE id = ?", (boss,)).fetchone()[0] is None
    conn.close()


def test_upgrade_turns_existing_agents_into_no_sign_in(tmp_path):
    import sqlite3

    from app.db import init_schema

    db = str(tmp_path / "old.db")
    old = sqlite3.connect(db)
    old.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin', 'agent', 'user')),
        is_active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 1,
        failed_logins INTEGER NOT NULL DEFAULT 0, locked_until TEXT, password_changed_at TEXT NOT NULL,
        last_login_at TEXT, created_at TEXT NOT NULL);
        INSERT INTO users (username, password_hash, role, password_changed_at, created_at)
        VALUES ('sara', 'x', 'agent', 't', 't'), ('viewer', 'x', 'user', 't', 't');""")
    old.commit()
    old.close()
    conn = connect(db)
    init_schema(conn)
    init_schema(conn)  # running again changes nothing
    assert dict(conn.execute("SELECT username, can_login FROM users")) == {"sara": 0, "viewer": 1}


def test_search_cards_show_who_is_responsible(app):
    conn = connect(app.config["DATABASE"])
    stamp = now_iso()
    conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                 " created_at, can_login) VALUES ('Sara', 'x', 'agent', 0, ?, ?, 0)", (stamp, stamp))
    sara = conn.execute("SELECT id FROM users WHERE username = 'Sara'").fetchone()[0]
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    conn.execute("INSERT INTO house_assignments VALUES ('Rose Villa', ?, ?, NULL)", (sara, stamp))
    conn.execute("INSERT INTO voter_assignments VALUES ('A200003', ?, ?, NULL)", (viewer, stamp))
    conn.commit()
    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    html = client.get("/?sort=id").get_data(as_text=True)
    cards = html.split('id="v-')[1:]
    assert "Sara · house" in cards[0]                       # A200001: through Rose Villa
    assert "Unassigned" in cards[1]                         # A200002: Blue Ge has nobody
    assert "viewer" in cards[2] and "· house" not in cards[2]  # A200003: handed to a user one by one
    assert '<optgroup label="Users">' in html and "Tick all on this page" in html
    conn.close()


def test_dhivehi_names_import_edit_search_export(app, tmp_path):
    import csv as _csv

    from scripts import ingest

    data = tmp_path / "dv"
    data.mkdir()
    with open(data / "gadhoo-new voters.csv", "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["ID Card No.", "Dhivehi name", "Dhivehi house"])
        w.writerow(["A200001", "އަމީނަތު", "ރޯޒްވިލާ"])
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET phone = '7771234' WHERE national_id = 'A200001'")
    conn.commit()
    ingest.main(["--data-dir", str(data), "--db", app.config["DATABASE"], "--photos-dir", str(tmp_path / "p"),
                 "--log", str(tmp_path / "log"), "--no-seed-users"])
    row = conn.execute("SELECT full_name, house_name, phone, full_name_dv, house_name_dv FROM voters"
                       " WHERE national_id = 'A200001'").fetchone()
    assert tuple(row) == ("Aminath Alpha", "Rose Villa", "7771234", "އަމީނަތު", "ރޯޒްވިލާ")  # nothing else changed
    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    assert "Aminath Alpha" in client.get("/?q=އަމީނަތު").get_data(as_text=True)
    assert "ރޯޒްވިލާ" in client.get("/voters/A200001").get_data(as_text=True)
    out = list(_csv.DictReader(client.get("/export.csv?q=Aminath").get_data(as_text=True).lstrip("\ufeff").splitlines()))
    assert out[0]["Name (Dhivehi)"] == "އަމީނަތު"
    conn.close()


def test_admins_can_be_given_voters_and_unassign_everyone(app):
    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    conn = connect(app.config["DATABASE"])
    boss = conn.execute("SELECT id FROM users WHERE username = 'boss'").fetchone()[0]
    page = client.get("/").get_data(as_text=True)
    assert '<optgroup label="Admins">' in page and "boss (you)" in page
    assert client.post("/voters/assign", data={"csrf_token": csrf(client, "/"), "agent": str(boss),
                                               "ids": ["A200001"]}).status_code == 302
    conn.execute("INSERT INTO house_assignments VALUES ('Blue Ge', ?, 't', NULL)", (boss,))
    conn.commit()
    assert "boss" in client.get("/admin/agents").get_data(as_text=True)
    token = csrf(client, "/admin/agents")
    client.post("/admin/agents/unassign-all", data={"csrf_token": token, "confirm": "no"})
    assert conn.execute("SELECT COUNT(*) FROM voter_assignments").fetchone()[0] == 1
    client.post("/admin/agents/unassign-all", data={"csrf_token": token, "confirm": "unassign"})
    assert conn.execute("SELECT (SELECT COUNT(*) FROM voter_assignments) + (SELECT COUNT(*) FROM house_assignments)").fetchone()[0] == 0
    assert "Unassigned" in client.get("/").get_data(as_text=True)
    conn.close()


def test_pdf_export_english_and_dhivehi(app):
    import io as _io

    import pymupdf
    from PIL import Image

    conn = connect(app.config["DATABASE"])
    stamp = now_iso()
    conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                 " created_at, can_login) VALUES ('Sara Agent', 'x', 'agent', 0, ?, ?, 0)", (stamp, stamp))
    sara = conn.execute("SELECT id FROM users WHERE username = 'Sara Agent'").fetchone()[0]
    conn.execute("INSERT INTO house_assignments VALUES ('Rose Villa', ?, ?, NULL)", (sara, stamp))
    conn.execute("UPDATE voters SET phone = '7771234', location = 'Gdh. Gadhdhoo', full_name_dv = 'އަމީނަތު',"
                 " house_name_dv = 'ރޯޒްވިލާ', photo_path = 'A200001.jpg' WHERE national_id = 'A200001'")
    conn.commit()
    import os
    os.makedirs(app.config["PHOTOS_DIR"], exist_ok=True)
    Image.new("RGB", (300, 400), "teal").save(os.path.join(app.config["PHOTOS_DIR"], "A200001.jpg"))
    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    r = client.get(f"/voters.pdf?ag={sara}")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert "Sara-Agent" in r.headers["Content-Disposition"]
    doc = pymupdf.open("pdf", r.data)
    text = doc[0].get_text()
    assert "Voters for Sara Agent" in text and "Aminath Alpha" in text and "7771234" in text
    assert "Moosa Beta" not in text                          # Blue Ge isn't Sara's
    assert "For Sara Agent" in text and "Do not forward" in text
    assert doc[0].get_images()                               # the photo is in
    dv = pymupdf.open("pdf", client.get(f"/voters.pdf?ag={sara}&lang=dv").data)[0].get_text()
    assert "އަމީނަތު" in dv and "ރޯޒްވިލާ" in dv
    viewer = app.test_client()
    login(viewer, "viewer", USER_PW)
    assert viewer.get("/voters.pdf").status_code in (302, 403)  # admins only
    conn.close()


def test_status_and_notes_are_personal(app):
    conn = connect(app.config["DATABASE"])
    stamp = now_iso(precise=True)
    conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                 " created_at) VALUES ('second', ?, 'user', 0, ?, ?)", (hash_password(USER_PW), stamp, stamp))
    conn.commit()
    one, two, boss = app.test_client(), app.test_client(), app.test_client()
    login(one, "viewer", USER_PW)
    login(two, "second", USER_PW)
    login(boss, "boss", ADMIN_PW)
    one.post("/voters/A200001/canvass", data={"csrf_token": csrf(one, "/"), "status": "followup",
                                              "note": "Call after 5pm"})
    two.post("/voters/A200001/canvass", data={"csrf_token": csrf(two, "/"), "status": "noanswer",
                                              "note": "Nobody home"})
    # each sees only their own status and notes
    p1, p2 = one.get("/voters/A200001").get_data(as_text=True), two.get("/voters/A200001").get_data(as_text=True)
    assert "Call after 5pm" in p1 and "Nobody home" not in p1
    assert "Nobody home" in p2 and "Call after 5pm" not in p2
    assert one.get("/?status=followup").get_data(as_text=True).count('id="v-A2000') == 1
    assert one.get("/?status=noanswer").get_data(as_text=True).count('id="v-A2000') == 0
    assert two.get("/?status=new").get_data(as_text=True).count('id="v-A2000') == 2
    # a third person starts from 'Not contacted'
    login(app.test_client(), "viewer", USER_PW)
    # admins see everyone's status and every note, with names
    pb = boss.get("/voters/A200001").get_data(as_text=True)
    assert "Everyone's status" in pb and "Call after 5pm" in pb and "Nobody home" in pb
    assert "viewer" in pb and "second" in pb
    # only the writer (or an admin) deletes a note
    note_id = conn.execute("SELECT id FROM voter_notes WHERE text = 'Call after 5pm'").fetchone()[0]
    assert two.post(f"/voters/A200001/notes/{note_id}/delete", data={"csrf_token": csrf(two, "/")}).status_code == 403
    one.post(f"/voters/A200001/notes/{note_id}/delete", data={"csrf_token": csrf(one, "/")})
    assert conn.execute("SELECT COUNT(*) FROM voter_notes").fetchone()[0] == 1
    conn.close()


def test_upgrade_moves_shared_status_and_notes(tmp_path):
    import sqlite3

    from app import personal
    from app.db import init_schema

    db = str(tmp_path / "old.db")
    conn = connect(db)
    init_schema(conn)
    conn.execute("INSERT INTO users (id, username, password_hash, role, password_changed_at, created_at)"
                 " VALUES (7, 'ali', 'x', 'user', 't', 't'), (8, 'sara', 'x', 'user', 't', 't')")
    conn.execute("INSERT INTO voters (national_id, full_name, status, status_at, status_by, notes, created_at,"
                 " updated_at) VALUES ('A1', 'One', 'contacted', '2026-10-01T10:00:00+00:00', 8, 'Wants a lift', 't', 't')")
    conn.execute("INSERT INTO audit_log (at, user_id, username, action, target, detail) VALUES"
                 " ('2026-09-30T10:00:00+00:00', 7, 'ali', 'voter_canvass', 'A1', '{\"notes\": {\"from\": \"\", \"to\": \"x\"}}')")
    conn.commit()
    personal.init_schema(conn)
    personal.init_schema(conn)  # second start: nothing moves twice
    assert [tuple(r) for r in conn.execute("SELECT national_id, user_id, status FROM canvass")] == [("A1", 8, "contacted")]
    notes = conn.execute("SELECT user_id, text FROM voter_notes").fetchall()
    assert [tuple(n) for n in notes] == [(7, "Wants a lift")]  # credited to the last writer of the notes


def test_changes_page_for_admins(app):
    u, boss = app.test_client(), app.test_client()
    login(u, "viewer", USER_PW)
    login(boss, "boss", ADMIN_PW)
    u.post("/voters/A200001/canvass", data={"csrf_token": csrf(u, "/"), "status": "contacted", "note": "Will vote"})
    page = boss.get("/admin/changes").get_data(as_text=True)
    assert "viewer" in page and "Aminath Alpha" in page and "Not contacted → Contacted" in page and "Will vote" in page
    viewer_id = connect(app.config["DATABASE"]).execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    assert "Will vote" in boss.get(f"/admin/changes?person={viewer_id}&q=Aminath").get_data(as_text=True)
    assert "Will vote" not in boss.get("/admin/changes?type=voter_update").get_data(as_text=True)
    csv_text = boss.get("/admin/changes?format=csv").get_data(as_text=True)
    assert "What changed" in csv_text and "Will vote" in csv_text
    assert u.get("/admin/changes").status_code in (302, 403)


def test_dhivehi_font_upload_used_in_pdf(app):
    import io as _io

    import pymupdf

    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    token = csrf(client, "/admin/settings")
    post = lambda blob, name: client.post("/admin/settings/font", follow_redirects=True,  # noqa: E731
                                          data={"csrf_token": token, "font": (_io.BytesIO(blob), name)}).get_data(as_text=True)
    assert "a font (use .ttf or .otf)" in post(b"not a font at all", "x.ttf")
    assert "no Unicode Thaana letters" in post(pymupdf.Font("helv").buffer, "helv.ttf")
    assert client.get("/font/dhivehi.css").status_code == 404           # nothing uploaded yet
    thaana = pymupdf.Font(script=8)                                     # Noto Sans Thaana, a real Thaana font
    assert "Dhivehi font set to" in post(thaana.buffer, "dhivehi.ttf")
    assert client.get("/font/dhivehi").status_code == 200
    assert "Portal Dhivehi" in client.get("/font/dhivehi.css").get_data(as_text=True)
    assert "/font/dhivehi.css" in client.get("/").get_data(as_text=True)  # the portal's pages use it
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET full_name_dv = 'އަމީނަތު' WHERE national_id = 'A200001'")
    conn.commit()
    doc = pymupdf.open("pdf", client.get("/voters.pdf?lang=dv").data)
    fonts = {f[3] for f in doc[0].get_fonts()}
    assert any("Thaana" in f for f in fonts) and "އަމީނަތު" in doc[0].get_text()
    assert "Use the built-in font again" in post(thaana.buffer, "again.ttf")
    client.post("/admin/settings/font", data={"csrf_token": token, "remove": "1"})
    assert client.get("/font/dhivehi").status_code == 404
    conn.close()


def test_lists_are_numbered(app):
    import csv as _csv

    import pymupdf

    client = app.test_client()
    login(client, "boss", ADMIN_PW)
    rows = list(_csv.reader(client.get("/export.csv?sort=name").get_data(as_text=True).lstrip("﻿").splitlines()))
    assert rows[0][:2] == ["#", "ID"] and [r[0] for r in rows[1:]] == ["1", "2", "3"]
    text = pymupdf.open("pdf", client.get("/voters.pdf").data)[0].get_text()
    assert "#" in text and all(f"\n{n}\n" in f"\n{text}" for n in ("1", "2", "3"))
