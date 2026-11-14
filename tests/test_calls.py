"""Call centre: island agents, the call queue, outcomes, the reachability dashboard and comments."""
import csv
import io
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect, now_iso  # noqa: E402
from app.normalize import normalize_phones  # noqa: E402
from scripts import ingest  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, csrf, login  # noqa: E402
from tests.test_app import app as base_app  # noqa: E402,F401
from tests.test_canvass import AGENT_PW, add_agent  # noqa: E402

ISLAND_VOTERS = (
    ("A300001", "Ahmed One", "Thinadhoo", "Asurumaage", "7771111"),
    ("A300002", "Aishath Two", "Thinadhoo", "Beach House", "7772222, 9993333"),
    ("A300003", "Hassan Three", "Fares-Maathoda", "Coral", "7774444"),
    ("A300004", "Fathimath Four", "Fares-Maathoda", "Coral", ""),
)


@pytest.fixture()
def app(base_app):  # noqa: F811
    conn = connect(base_app.config["DATABASE"])
    stamp = now_iso()
    for nid, name, island, house, phone in ISLAND_VOTERS:
        conn.execute("INSERT INTO voters (national_id, full_name, house_name, island, lives_in, phone, created_at,"
                     " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (nid, name, house, island, "Male'" if nid == "A300002" else island, phone, stamp, stamp))
    conn.commit()
    conn.close()
    return base_app


def admin(app):
    c = app.test_client()
    login(c, "boss", ADMIN_PW)
    return c


def turn_on(c, **rules):
    token = csrf(c, "/admin/settings")
    data = {"csrf_token": token, "call_center": "1", "show_photos": "1", "call_max_attempts": 3,
            "call_retry_minutes": 60, "call_hold_minutes": 10}
    data.update(rules)
    return c.post("/admin/features", data=data, follow_redirects=True)


def give_island(c, island, *agent_ids):
    token = csrf(c, "/calls/islands")
    return c.post("/calls/islands", data={"csrf_token": token, "island": island,
                                          "agents": [str(a) for a in agent_ids]}, follow_redirects=True)


def agent_client(app, name):
    c = app.test_client()
    login(c, name, AGENT_PW)
    return c


def held(client, path="/calls/"):
    """National ID of the voter the queue shows this agent, or None."""
    import re
    m = re.search(r'action="/calls/(A\d{6})"', client.get(path).get_data(as_text=True))
    return m.group(1) if m else None


def log(client, nid, outcome, comment="", **extra):
    token = csrf(client, f"/calls/voter/{nid}")
    data = {"csrf_token": token, "outcome": outcome, "comment": comment}
    data.update(extra)
    return client.post(f"/calls/{nid}", data=data)


def db(app):
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    return conn


def test_off_by_default_and_portal_unchanged(app):
    c = admin(app)
    assert c.get("/calls/").status_code == 404
    assert c.get("/calls/dashboard").status_code == 404
    page = c.get("/").get_data(as_text=True)
    assert ">Calls<" not in page and ">Dashboard<" in page and ">Agents<" in page


def test_setup_islands_and_agent_home(app):
    ali = add_agent(app, "ali")
    c = admin(app)
    r = turn_on(c)
    assert "Call centre and photo settings saved." in r.get_data(as_text=True)
    page = c.get("/calls/islands").get_data(as_text=True)
    assert "Thinadhoo" in page and "Fares-Maathoda" in page and "No island recorded" in page
    give_island(c, "Thinadhoo", ali)
    nav = c.get("/").get_data(as_text=True)
    assert ">Reachability<" in nav and ">Islands<" in nav and ">Agents<" not in nav
    a = app.test_client()
    r = login(a, "ali", AGENT_PW)
    assert r.headers["Location"].endswith("/calls/")    # agents land on their call list


def test_queue_holds_voters_and_keeps_islands_apart(app):
    ali, bob, sara = add_agent(app, "ali"), add_agent(app, "bob"), add_agent(app, "sara")
    c = admin(app)
    turn_on(c)
    give_island(c, "Thinadhoo", ali, bob)
    give_island(c, "Fares-Maathoda", sara)
    a, b, s = agent_client(app, "ali"), agent_client(app, "bob"), agent_client(app, "sara")
    first = held(a)
    assert first in ("A300001", "A300002")
    assert held(a) == first                        # same voter again until a call is logged
    second = held(b)
    assert second in ("A300001", "A300002") and second != first   # never the voter ali holds
    assert held(s) == "A300003"                     # only Fares-Maathoda, never the voter without a phone
    assert log(s, "A300001", "reached").status_code == 403        # not sara's island
    assert s.get("/calls/voter/A300001").status_code == 404
    # The voter page offers each number separately
    page = b.get("/calls/voter/A300002").get_data(as_text=True)
    assert 'href="tel:7772222"' in page and 'href="tel:9993333"' in page


def test_outcomes_retry_callback_and_mirror(app):
    ali = add_agent(app, "ali")
    c = admin(app)
    turn_on(c)
    give_island(c, "Thinadhoo", ali)
    a = agent_client(app, "ali")
    nid = held(a)
    r = log(a, nid, "noanswer", "rang twice")
    assert r.status_code == 302 and r.headers["Location"].endswith("/calls/")
    other = held(a)
    assert other and other != nid                  # tried again only after the retry gap
    conn = db(app)
    s = conn.execute("SELECT * FROM call_state WHERE national_id = ?", (nid,)).fetchone()
    assert s["attempts"] == 1 and s["done"] == 0 and s["held_by"] is None
    v = conn.execute("SELECT status, notes FROM voters WHERE national_id = ?", (nid,)).fetchone()
    assert v["status"] == "noanswer" and "rang twice" in v["notes"]
    assert conn.execute("SELECT status FROM canvass WHERE national_id = ? AND user_id = ?", (nid, ali)).fetchone()[0] == "noanswer"

    # Call back later: out of the queue until the time comes, then first in line
    log(a, other, "callback", "at work", callback_at="2099-01-01T10:00")
    assert held(a) is None
    assert conn.execute("SELECT callback_at FROM call_state WHERE national_id = ?", (other,)).fetchone()[0] \
        == "2099-01-01T05:00:00+00:00"            # typed in Maldives time, stored in UTC
    conn.execute("UPDATE call_state SET callback_at = '2000-01-01T00:00:00+00:00' WHERE national_id = ?", (other,))
    conn.execute("UPDATE call_state SET last_at = '2000-01-01T00:00:00+00:00' WHERE national_id = ?", (nid,))
    conn.commit()
    assert held(a) == other                        # due call-back beats a retry

    # Reached: finished, never offered again
    log(a, other, "reached", "Supports us, needs a ride")
    assert held(a) == nid
    assert conn.execute("SELECT done FROM call_state WHERE national_id = ?", (other,)).fetchone()[0] == 1
    assert conn.execute("SELECT status FROM voters WHERE national_id = ?", (other,)).fetchone()[0] == "contacted"
    assert conn.execute("SELECT COUNT(*) FROM calls WHERE national_id = ?", (other,)).fetchone()[0] == 2


def test_hard_to_reach_dashboard_comments_export(app):
    ali = add_agent(app, "ali")
    c = admin(app)
    turn_on(c, call_max_attempts=2, call_retry_minutes=0)
    give_island(c, "Thinadhoo", ali)
    a = agent_client(app, "ali")
    log(a, "A300001", "noanswer")
    log(a, "A300001", "switchedoff", "phone off all day")
    log(a, "A300002", "reached", "=SUM(1) will vote for us")
    assert held(a) is None                         # one hard to reach, one done: nothing left
    page = c.get("/calls/dashboard").get_data(as_text=True)
    assert "Thinadhoo" in page and "50%" in page   # 1 of 2 Thinadhoo voters reached
    assert "will vote for us" in page and "phone off all day" in page
    hard = c.get("/calls/voters?state=hard&island=Thinadhoo").get_data(as_text=True)
    assert "Ahmed One" in hard and "Aishath Two" not in hard
    feed = c.get("/calls/comments?island=Thinadhoo").get_data(as_text=True)
    assert "phone off all day" in feed and "will vote for us" in feed
    assert "Ahmed One" not in c.get("/calls/comments?outcome=reached").get_data(as_text=True)
    out = c.get("/calls/export.csv").get_data(as_text=True)
    rows = list(csv.reader(io.StringIO(out.lstrip("﻿"))))
    assert rows[0][:3] == ["Time (Maldives)", "ID", "Name"]
    assert any(r[8] == "'=SUM(1) will vote for us" for r in rows[1:])   # formula kept as text
    # Admin can put a finished voter back
    token = csrf(c, "/calls/voter/A300002")
    c.post("/calls/voter/A300002/reopen", data={"csrf_token": token})
    assert held(a) == "A300002"


def test_agents_cant_see_admin_pages(app):
    ali = add_agent(app, "ali")
    c = admin(app)
    turn_on(c)
    give_island(c, "Thinadhoo", ali)
    a = agent_client(app, "ali")
    for path in ("/calls/dashboard", "/calls/comments", "/calls/islands", "/calls/export.csv",
                 "/calls/voters?state=never"):
        assert a.get(path).status_code == 403, path
    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert "No island has been given to you yet" in u.get("/calls/").get_data(as_text=True)


def test_photos_off_and_island_filter(app):
    c = admin(app)
    turn_on(c, show_photos="")
    form = c.get("/voters/A300001/edit").get_data(as_text=True)
    assert "Photo (JPEG" not in form and 'name="island"' in form
    page = c.get("/?isl=Thinadhoo").get_data(as_text=True)
    assert "Ahmed One" in page and "Hassan Three" not in page
    assert 'name="isl"' in page
    detail = c.get("/voters/A300002").get_data(as_text=True)
    assert "Lives in" in detail and 'href="tel:9993333"' in detail


def test_phone_cells_with_several_numbers():
    assert normalize_phones("7771234 / 9991234")[0] == "7771234, 9991234"
    assert normalize_phones("+960 7771234, 7771234")[0] == "7771234"
    assert normalize_phones("7771234 9991234")[0] == "7771234, 9991234"
    assert normalize_phones(7771234.0)[0] == "7771234"
    assert normalize_phones("")[0] == ""


def test_zr_file_columns(tmp_path):
    data = tmp_path / "files"
    data.mkdir()
    (data / "zr.csv").write_text(
        "nationalID\tDisplayName\tHouse\tAddress\tPhone\tLivesIn\tAtoll\tIsland\tConstituency\tBoxName\tBoxCode\n"
        "A111111\tAli Test\tRose\tRose, Majeedhee Magu\t7771234 / 9991234\tMale'\tGDh\tThinadhoo\tThinadhoo North"
        "\tThinadhoo School\tG1.1\n", encoding="utf-8")
    dbfile = tmp_path / "vp.db"
    assert ingest.main(["--data-dir", str(data), "--db", str(dbfile), "--photos-dir", str(tmp_path / "p"),
                        "--log", str(tmp_path / "log"), "--no-seed-users"]) == 0
    v = sqlite3.connect(dbfile)
    v.row_factory = sqlite3.Row
    r = dict(v.execute("SELECT * FROM voters").fetchone())
    assert (r["full_name"], r["house_name"], r["address"], r["phone"], r["lives_in"], r["atoll"], r["island"],
            r["constituency"], r["location"], r["box_code"]) == (
        "Ali Test", "Rose", "Rose, Majeedhee Magu", "7771234, 9991234", "Male'", "GDh", "Thinadhoo",
        "Thinadhoo North", "Thinadhoo School", "G1.1")


def test_users_and_agents_see_only_their_islands(app):
    ali = add_agent(app, "ali")
    c = admin(app)
    turn_on(c)
    viewer = db(app).execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    give_island(c, "Thinadhoo", ali)
    give_island(c, "Fares-Maathoda", viewer)
    a = agent_client(app, "ali")
    page = a.get("/?n=200").get_data(as_text=True)
    assert "Ahmed One" in page and "Aishath Two" in page
    assert "Hassan Three" not in page and "Aminath Alpha" not in page     # other island / no island
    assert a.get("/voters/A300003").status_code == 404
    assert a.get("/voters/A300001").status_code == 200
    u = app.test_client()
    login(u, "viewer", USER_PW)
    page = u.get("/?n=200").get_data(as_text=True)
    assert "Hassan Three" in page and "Fathimath Four" in page and "Ahmed One" not in page
    assert u.get("/voters/A300001").status_code == 404
    assert u.get("/?q=Ahmed").get_data(as_text=True).count('id="v-A3') == 0
    # The admin still sees everyone
    assert "Aminath Alpha" in c.get("/?n=200").get_data(as_text=True)


def test_my_dashboard(app):
    ali, bob = add_agent(app, "ali"), add_agent(app, "bob")
    c = admin(app)
    turn_on(c)
    give_island(c, "Thinadhoo", ali)
    give_island(c, "Fares-Maathoda", bob)
    a, b = agent_client(app, "ali"), agent_client(app, "bob")
    log(a, "A300001", "reached", "will come to vote")
    log(b, "A300003", "noanswer", "bob's note")
    page = a.get("/calls/mine").get_data(as_text=True)
    assert "My dashboard" in page and "Thinadhoo" in page and "50%" in page
    assert "will come to vote" in page and "bob's note" not in page
    assert "Fares-Maathoda" not in page.split("My islands")[1].split("My latest calls")[0]
    assert c.get("/calls/mine").status_code == 302          # the admin has the full dashboard


def test_will_vote_and_will_not_vote(app):
    ali = add_agent(app, "ali")
    c = admin(app)
    turn_on(c)
    give_island(c, "Thinadhoo", ali)
    a = agent_client(app, "ali")
    page = a.get("/calls/").get_data(as_text=True)
    assert 'value="willvote"' in page and 'value="willnotvote"' in page
    log(a, "A300001", "willvote", "with family")
    log(a, "A300002", "willnotvote")
    assert held(a) is None                          # both finished
    conn = db(app)
    assert conn.execute("SELECT status FROM voters WHERE national_id = 'A300001'").fetchone()[0] == "contacted"
    dash = c.get("/calls/dashboard").get_data(as_text=True)
    assert "100%" in dash                           # both Thinadhoo voters count as reached
    assert "1 will vote" in dash and "1 will not vote" in dash
    lst = c.get("/calls/voters?state=willvote&island=Thinadhoo").get_data(as_text=True)
    assert "Ahmed One" in lst and "Aishath Two" not in lst
    reached = c.get("/calls/voters?state=reached&island=Thinadhoo").get_data(as_text=True)
    assert "Ahmed One" in reached and "Aishath Two" in reached
    assert "with family" in c.get("/calls/comments?outcome=willvote").get_data(as_text=True)
    mine = a.get("/calls/mine").get_data(as_text=True)
    assert "1 will vote" in mine
