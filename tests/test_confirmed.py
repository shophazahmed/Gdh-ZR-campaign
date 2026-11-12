"""'100% confirmed' voters: only the account named 'admin' can see or change them."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import hash_password  # noqa: E402
from app.db import connect, now_iso  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_campaign import set_parties  # noqa: E402
from tests.test_canvass import AGENT_PW, add_agent  # noqa: E402

OWNER_PW = "owner-pass-1234"


def add_owner(app):
    conn = connect(app.config["DATABASE"])
    stamp = now_iso(precise=True)
    conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                 " created_at) VALUES ('admin', ?, 'admin', 0, ?, ?)", (hash_password(OWNER_PW), stamp, stamp))
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]


def cards(client, path):
    return client.get(path).get_data(as_text=True).count('id="v-A2000')


def test_owner_marks_and_sees_metrics(app):
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    token = csrf(o, "/")
    page = o.get("/").get_data(as_text=True)
    assert page.count("Mark confirmed") == 3 and "100% confirmed" in page        # toggle + filter

    r = o.post("/voters/A200001/confirm", data={"csrf_token": token, "confirmed": "1"},
               headers={"X-Requested-With": "fetch"})
    assert r.json == {"ok": True, "confirmed": True, "changed": True,
                      "metrics": {"confirmed": 1, "total": 3, "pct": 33.3}}
    # same request again changes nothing (wanted state, not a toggle)
    r = o.post("/voters/A200001/confirm", data={"csrf_token": token, "confirmed": "1"},
               headers={"X-Requested-With": "fetch"})
    assert r.json["changed"] is False
    o.post("/voters/A200002/confirm", data={"csrf_token": token, "confirmed": "1", "note": "needs transport"})
    assert o.post("/voters/A200002/confirm", data={"csrf_token": token, "confirmed": "x"}).status_code == 400

    assert cards(o, "/?cf=1") == 2 and cards(o, "/?cf=0") == 1
    detail = o.get("/voters/A200002").get_data(as_text=True)
    assert "needs transport" in detail and "Remove confirmation" in detail
    m = o.get("/confirmed/metrics.json").json
    assert (m["confirmed"], m["total"], m["pct"]) == (2, 3, 66.7)
    assert "66.7%" in o.get("/confirmed").get_data(as_text=True)
    assert "100% confirmed" in o.get("/dashboard").get_data(as_text=True)

    lines = o.get("/export.csv?cf=1").get_data(as_text=True).strip().splitlines()
    assert len(lines) == 3 and lines[0].endswith("100% confirmed,Confirmed on") and ",yes," in lines[1]

    # the main number is out of the campaign's voters (e.g. MDP)
    set_parties(o, ["AAA"])
    m = o.get("/confirmed/metrics.json").json
    assert (m["label"], m["confirmed"], m["total"], m["pct"]) == ("AAA", 1, 1, 100.0)
    assert (m["all_confirmed"], m["all_total"]) == (2, 3)

    o.post("/voters/A200001/confirm", data={"csrf_token": token, "confirmed": "0"})
    assert o.get("/confirmed/metrics.json").json["all_confirmed"] == 1
    # deleting a voter drops their confirmation
    o.post("/admin/view-all", data={"csrf_token": token})
    o.post("/voters/A200002/delete", data={"csrf_token": token})
    assert connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM confirmed_voters").fetchone()[0] == 0
    assert "voter_confirm" in o.get("/admin/audit").get_data(as_text=True)


def test_nobody_else_can_see_or_change_it(app):
    owner_id = add_owner(app)
    add_agent(app, "ali")
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    o.post("/voters/A200001/confirm", data={"csrf_token": csrf(o, "/"), "confirmed": "1"})

    b = app.test_client()                      # another admin
    login(b, "boss", ADMIN_PW)
    u = app.test_client()
    login(u, "viewer", USER_PW)
    g = app.test_client()
    login(g, "ali", AGENT_PW)
    for c in (b, u, g):
        token = csrf(c, "/")
        assert c.post("/voters/A200001/confirm", data={"csrf_token": token, "confirmed": "0"}).status_code == 403
        assert c.get("/confirmed").status_code == 403
        assert c.get("/confirmed/metrics.json").status_code == 403
    page = b.get("/").get_data(as_text=True)
    assert "Mark confirmed" not in page and "100% confirmed" not in page
    assert cards(b, "/?cf=1") == 3                                   # the filter is ignored for others
    assert "100% confirmed" not in b.get("/export.csv?cf=1").get_data(as_text=True)
    assert "100% confirmed" not in b.get("/dashboard").get_data(as_text=True)
    assert "confirmed" not in b.get("/voters/A200001").get_data(as_text=True).lower().replace("not confirmed", "")
    # other admins don't see confirm entries in the activity log
    log = b.get("/admin/audit").get_data(as_text=True)
    assert "voter_confirm" not in log and "access_denied" in log
    assert connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM confirmed_voters").fetchone()[0] == 1

    # and can't take over the 'admin' account
    token = csrf(b, "/admin/users")
    assert "Protected: only admin" in b.get("/admin/users").get_data(as_text=True)
    for data in ({"action": "reset_password"}, {"action": "toggle_active"}, {"action": "set_role", "role": "user"}):
        b.post(f"/admin/users/{owner_id}", data={"csrf_token": token, **data})
    row = connect(app.config["DATABASE"]).execute(
        "SELECT role, is_active, must_change_password FROM users WHERE id = ?", (owner_id,)).fetchone()
    assert tuple(row) == ("admin", 1, 0)
    assert o.get("/confirmed").status_code == 200                    # owner's session still works


def test_a_differently_named_admin_is_not_the_owner(app):
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    assert a.get("/confirmed").status_code == 403


def test_owner_confirms_from_a_pasted_list(app):
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    token = csrf(o, "/confirmed")
    o.post("/voters/A200002/confirm", data={"csrf_token": token, "confirmed": "1"})  # already confirmed
    set_parties(o, ["AAA"])                                   # campaign: party AAA (A200001 only)
    token = csrf(o, "/confirmed")
    page = o.post("/confirmed/list", data={"csrf_token": token,
                                           "ids": "a200001\nA200002\tx\nA200003, A999999\nA200001"}).get_data(as_text=True)
    assert "Will be confirmed" in page and "A999999" in page   # not found is listed
    assert '<strong>1</strong>' in page                       # A200001 to confirm (duplicate pasted once)
    o.post("/confirmed/list/apply", data={"csrf_token": token, "ids": "A200001 A200003"})
    conn = connect(app.config["DATABASE"])
    got = {r[0] for r in conn.execute("SELECT national_id FROM confirmed_voters")}
    assert got == {"A200001", "A200002"}                      # A200003 (no party) needs the tick box
    o.post("/confirmed/list/apply", data={"csrf_token": token, "ids": "A200003", "include_other": "1"})
    assert conn.execute("SELECT COUNT(*) FROM confirmed_voters").fetchone()[0] == 3


def test_confirmed_list_is_private_to_owner(app):
    add_owner(app)
    boss = app.test_client()
    login(boss, "boss", ADMIN_PW)
    token = csrf(boss, "/")
    assert boss.post("/confirmed/list", data={"csrf_token": token, "ids": "A200001"}).status_code in (403, 404)
    assert boss.post("/confirmed/list/apply", data={"csrf_token": token, "ids": "A200001"}).status_code in (403, 404)


def test_owner_renamed_keeps_confirmed_list(app):
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    token = csrf(o, "/")
    o.post("/voters/A200001/confirm", data={"csrf_token": token, "confirmed": "1"})
    conn = connect(app.config["DATABASE"])
    owner_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    # another admin can't rename the owner
    boss = app.test_client()
    login(boss, "boss", ADMIN_PW)
    boss.post(f"/admin/users/{owner_id}", data={"csrf_token": csrf(boss, "/admin/users"), "action": "rename",
                                                 "username": "mine"})
    assert conn.execute("SELECT username FROM users WHERE id = ?", (owner_id,)).fetchone()[0] == "admin"
    # the owner renames themself
    o.post(f"/admin/users/{owner_id}", data={"csrf_token": csrf(o, "/admin/users"), "action": "rename",
                                              "username": "administrator"})
    row = conn.execute("SELECT username, is_owner FROM users WHERE id = ?", (owner_id,)).fetchone()
    assert tuple(row) == ("administrator", 1)
    assert o.get("/confirmed").status_code == 200                 # same session, still the owner
    fresh = app.test_client()
    login(fresh, "administrator", OWNER_PW)
    assert fresh.get("/confirmed").status_code == 200
    # someone creating a new 'admin' account doesn't become the owner
    boss.post("/admin/users", data={"csrf_token": csrf(boss, "/admin/users"), "username": "admin", "role": "admin",
                                    "password": "another-pass-123"})
    conn.execute("UPDATE users SET must_change_password = 0 WHERE username = 'admin'")
    conn.commit()
    impostor = app.test_client()
    login(impostor, "admin", "another-pass-123")
    assert impostor.get("/confirmed").status_code == 403
    conn.close()


def test_upgrade_flags_existing_admin_account_as_owner(tmp_path):
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
        VALUES ('admin', 'x', 'admin', 't', 't'), ('boss', 'x', 'admin', 't', 't');""")
    old.commit()
    old.close()
    conn = connect(db)
    init_schema(conn)
    assert dict(conn.execute("SELECT username, is_owner FROM users")) == {"admin": 1, "boss": 0}


def test_owner_shares_voter_cards(app):
    from urllib.parse import unquote

    add_owner(app)
    conn = connect(app.config["DATABASE"])
    stamp = now_iso(precise=True)
    conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                 " created_at, phone, can_login) VALUES ('Ali Agent', 'x', 'agent', 0, ?, ?, '7771234', 0)", (stamp, stamp))
    conn.execute("UPDATE voters SET phone = '9990001', location = 'Gdh. Gadhdhoo', full_name_dv = 'އަމީނަތު'"
                 " WHERE national_id = 'A200001'")
    conn.commit()
    ali = conn.execute("SELECT id FROM users WHERE username = 'Ali Agent'").fetchone()[0]
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    assert "Share on WhatsApp" in o.get("/voters/A200001").get_data(as_text=True)
    hdr = {"X-Requested-With": "fetch"}
    # text, straight to the agent's chat, and hand them the voter
    r = o.post("/voters/share", headers=hdr, data={"csrf_token": csrf(o, "/"), "ids": ["A200001"], "to": str(ali),
                                                   "lang": "en", "mode": "text", "assign": "1"}).get_json()
    assert r["ok"] and r["direct"] and r["wa_url"].startswith("https://wa.me/9607771234?text=")
    text = unquote(r["wa_url"].split("text=")[1])
    assert "Aminath Alpha" in text and "Rose Villa" in text and "Gdh. Gadhdhoo" in text and "9990001" in text
    assert "A200001" not in text                                    # no ID card number
    assert conn.execute("SELECT user_id FROM voter_assignments WHERE national_id = 'A200001'").fetchone()[0] == ali
    # a card image, to a typed number; and in Dhivehi
    r = o.post("/voters/share", headers=hdr, data={"csrf_token": csrf(o, "/"), "ids": ["A200001", "A200002"],
                                                   "to": "number", "number": "960 9123456", "lang": "dv"}).get_json()
    assert r["wa_url"].startswith("https://wa.me/9609123456") and len(r["cards"]) == 2
    png = o.get(r["cards"][0])
    assert png.status_code == 200 and png.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert "އަމީނަތު" in r["text"]
    assert "Shared on WhatsApp" in o.get("/admin/changes").get_data(as_text=True)
    # nobody else may share or fetch cards
    boss = app.test_client()
    login(boss, "boss", ADMIN_PW)
    assert "Share on WhatsApp" not in boss.get("/voters/A200001").get_data(as_text=True)
    assert boss.post("/voters/share", data={"csrf_token": csrf(boss, "/"), "ids": ["A200001"]}).status_code == 403
    assert boss.get("/voters/A200001/card.png").status_code == 403
    conn.close()


def test_owner_gives_view_or_mark_access_without_downloads(app):
    add_owner(app)
    conn = connect(app.config["DATABASE"])
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    boss = conn.execute("SELECT id FROM users WHERE username = 'boss'").fetchone()[0]
    o, v, b = app.test_client(), app.test_client(), app.test_client()
    login(o, "admin", OWNER_PW)
    login(v, "viewer", USER_PW)
    login(b, "boss", ADMIN_PW)
    o.post("/voters/A200001/confirm", data={"csrf_token": csrf(o, "/"), "confirmed": "1"})
    assert v.get("/confirmed").status_code == 403
    # another admin can't hand out access
    assert b.post(f"/admin/users/{viewer}", data={"csrf_token": csrf(b, "/admin/users"),
                                                  "action": "set_confirmed_access", "level": "view"}).status_code == 403
    assert "100% confirmed list" not in b.get("/admin/users").get_data(as_text=True)
    # view: sees the page, the filter and the tag, can't mark, can't download
    o.post(f"/admin/users/{viewer}", data={"csrf_token": csrf(o, "/admin/users"), "action": "set_confirmed_access",
                                           "level": "view"})
    page = v.get("/confirmed").get_data(as_text=True)
    assert v.get("/confirmed").status_code == 200 and "Add from a list" not in page and "export" not in page
    assert v.get("/?cf=1").get_data(as_text=True).count('id="v-A2000') == 1
    assert "Mark confirmed" not in v.get("/").get_data(as_text=True)
    assert v.post("/voters/A200002/confirm", data={"csrf_token": csrf(v, "/"), "confirmed": "1"}).status_code == 403
    # mark, for an admin account: may mark and add from a list, still no downloads
    o.post(f"/admin/users/{boss}", data={"csrf_token": csrf(o, "/admin/users"), "action": "set_confirmed_access",
                                         "level": "mark"})
    assert b.post("/voters/A200002/confirm", data={"csrf_token": csrf(b, "/"), "confirmed": "1"}).status_code == 302
    assert "Add from a list" in b.get("/confirmed").get_data(as_text=True)
    assert b.get("/export.csv?cf=1").status_code == 403 and b.get("/voters.pdf?cf=1").status_code == 403
    assert b.get("/export.csv").status_code == 200 and "100% confirmed" not in b.get("/export.csv").get_data(as_text=True)
    assert o.get("/export.csv?cf=1").status_code == 200                  # the owner still can
    # the owner sees who marked; access off again ends it
    assert "boss" in o.get("/admin/changes?type=voter_confirm").get_data(as_text=True)
    o.post(f"/admin/users/{boss}", data={"csrf_token": csrf(o, "/admin/users"), "action": "set_confirmed_access",
                                         "level": ""})
    assert b.get("/confirmed").status_code == 403
    conn.close()
