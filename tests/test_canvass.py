import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import hash_password  # noqa: E402
from app.canvass import plan_assignment  # noqa: E402
from app.db import connect, init_schema, now_iso  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401

AGENT_PW = "agent-pass-1234"


def add_agent(app, name="ali"):
    conn = connect(app.config["DATABASE"])
    stamp = now_iso(precise=True)
    conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                 " created_at) VALUES (?, ?, 'agent', 0, ?, ?)", (name, hash_password(AGENT_PW), stamp, stamp))
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]


def test_agent_sees_only_assigned_houses(app):
    agent_id = add_agent(app)
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/admin/agents")
    houses = re.findall(r'name="house" value="([^"]+)"', a.get("/admin/agents").get_data(as_text=True))
    assert houses == ["Blue Ge", "Rose Villa"]
    a.post("/admin/agents/assign", data={"csrf_token": token, "house": houses,
                                         "agent": ["", str(agent_id)]})

    g = app.test_client()
    r = login(g, "ali", AGENT_PW)
    assert r.headers["Location"].endswith("/my")  # agents land on their houses
    my = g.get("/my").get_data(as_text=True)
    assert "Rose Villa" in my and "Blue Ge" not in my and "Suggested next" in my
    page = g.get("/").get_data(as_text=True)
    assert page.count('id="v-A2000') == 2 and "Moosa Beta" not in page
    assert g.get("/?q=Moosa").get_data(as_text=True).count('id="v-A2000') == 0
    assert g.get("/voters/A200002").status_code == 404             # another agent's house
    token = csrf(g, "/")
    assert g.post("/voters/A200002/canvass", data={"csrf_token": token, "status": "contacted"}).status_code == 404
    assert g.post("/voters/A200001/canvass", data={"csrf_token": token, "status": "contacted"}).status_code == 302
    assert g.get("/dashboard").status_code == 403
    assert g.get("/admin/agents").status_code == 403
    assert g.get("/export.csv").status_code == 403
    page = g.get("/?h=Rose+Villa").get_data(as_text=True)          # exact-house link from My houses
    assert page.count('id="v-A2000') == 2

    # the dashboard (admin) shows the progress and who owns the house
    dash = a.get("/dashboard").get_data(as_text=True)
    assert "1/2" in dash and "ali" in dash and "Updates today" in dash
    assert "Blue Ge" in a.get("/dashboard?agent=none").get_data(as_text=True)
    assert "Blue Ge" not in a.get(f"/dashboard?agent={agent_id}").get_data(as_text=True)
    assert "Rose Villa" in a.get(f"/my?agent={agent_id}").get_data(as_text=True)


def test_own_dashboards(app):
    agent_id = add_agent(app)
    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert u.get("/dashboard").status_code == 403                   # the full dashboard is admin-only
    assert u.get("/admin/agents").status_code == 403
    page = u.get("/my").get_data(as_text=True)
    assert "My dashboard" in page and "Nothing yet" in page and "No houses assigned" not in page
    token = csrf(u, "/")
    u.post("/voters/A200001/canvass", data={"csrf_token": token, "status": "followup"})
    u.post("/voters/A200002/canvass", data={"csrf_token": token, "status": "contacted"})
    page = u.get("/my").get_data(as_text=True)
    assert "Aminath Alpha" in page and "Moosa Beta" in page and "1 contacted · 1 follow up" in page
    assert u.get("/my?agent=1").status_code == 200 and "viewer" in u.get("/my?agent=1").get_data(as_text=True)

    # another person's work doesn't count on someone's own dashboard
    g = app.test_client()
    login(g, "ali", AGENT_PW)
    page = g.get("/my").get_data(as_text=True)
    assert "Aminath Alpha" not in page and "No houses assigned" in page
    assert g.get("/dashboard").status_code == 403

    # admins have the full dashboard and can open anyone's own
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    assert a.get("/dashboard").status_code == 200
    viewer_id = connect(app.config["DATABASE"]).execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    page = a.get(f"/my?agent={viewer_id}").get_data(as_text=True)
    assert "viewer's dashboard" in page and "Moosa Beta" in page
    assert f"/my?agent={agent_id}" in a.get("/admin/users").get_data(as_text=True)


def test_auto_assign_balances_work(app):
    a_id, b_id = add_agent(app, "aa"), add_agent(app, "bb")
    conn = connect(app.config["DATABASE"])
    stamp = now_iso()
    for i in range(6):  # house sizes 1..6
        for j in range(i + 1):
            conn.execute("INSERT INTO voters (national_id, full_name, house_name, created_at, updated_at)"
                         " VALUES (?, ?, ?, ?, ?)", (f"A9{i}{j:04d}", "X", f"H{i}", stamp, stamp))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/admin/agents")
    r = a.post("/admin/agents/auto", data={"csrf_token": token, "agents": [str(a_id), str(b_id)],
                                           "scope": "unassigned"}, follow_redirects=True)
    assert "Assigned 8 houses" in r.get_data(as_text=True)
    load = dict(conn.execute("SELECT a.user_id, COUNT(*) FROM house_assignments a JOIN voters v"
                             " ON v.house_name = a.house_name GROUP BY a.user_id").fetchall())
    assert sum(load.values()) == 24 and abs(load[a_id] - load[b_id]) <= 1


def test_plan_assignment_largest_first():
    houses = [{"house": h, "work": w} for h, w in [("a", 8), ("b", 7), ("c", 6), ("d", 5), ("e", 4)]]
    plan, load = plan_assignment(houses, [1, 2], {2: 3})
    assert load == {1: 18, 2: 15}  # a→1, b→2, c→1, d→2, e→1
    assert plan["a"] == 1 and plan["b"] == 2


def test_old_database_gains_agent_role(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
        is_active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 1,
        failed_logins INTEGER NOT NULL DEFAULT 0, locked_until TEXT, password_changed_at TEXT NOT NULL,
        last_login_at TEXT, created_at TEXT NOT NULL);
      INSERT INTO users (username, password_hash, role, password_changed_at, created_at)
        VALUES ('admin', 'x', 'admin', 't', 't');""")
    conn.commit()
    conn.close()
    conn = connect(db)
    init_schema(conn)
    init_schema(conn)  # idempotent
    conn.execute("INSERT INTO users (username, password_hash, role, password_changed_at, created_at)"
                 " VALUES ('ali', 'x', 'agent', 't', 't')")
    assert [r[0] for r in conn.execute("SELECT username FROM users ORDER BY id")] == ["admin", "ali"]


def test_assign_individual_voters(app):
    ali = add_agent(app, "ali")
    sara = add_agent(app, "sara")
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/admin/agents")
    houses = re.findall(r'name="house" value="([^"]+)"', a.get("/admin/agents").get_data(as_text=True))
    a.post("/admin/agents/assign", data={"csrf_token": token, "house": houses, "agent": ["", str(ali)]})
    rose = re.findall(r'id="v-(A\d+)"', a.get("/?h=Rose+Villa").get_data(as_text=True))
    assert len(rose) == 2

    # one voter from ali's house and Moosa (no agent) go to sara, one by one
    detail = a.get(f"/voters/{rose[0]}").get_data(as_text=True)
    assert "Assigned agent" in detail and "House agent (ali)" in detail
    assert a.post(f"/voters/{rose[0]}/assign", data={"csrf_token": token, "agent": str(sara)}).status_code == 302
    r = a.post("/voters/assign", data={"csrf_token": token, "agent": str(sara), "scope": "selected",
                                       "ids": ["A200002", "A999999"], "back": "/"})
    assert r.status_code == 302
    assert a.post("/voters/assign", data={"csrf_token": token, "agent": "9999", "ids": ["A200002"]}).status_code == 400

    s = app.test_client()
    login(s, "sara", AGENT_PW)
    page = s.get("/").get_data(as_text=True)
    assert f'id="v-{rose[0]}"' in page and 'id="v-A200002"' in page and f'id="v-{rose[1]}"' not in page
    assert "one by one" in s.get("/my").get_data(as_text=True)

    g = app.test_client()
    login(g, "ali", AGENT_PW)
    page = g.get("/").get_data(as_text=True)
    assert f'id="v-{rose[0]}"' not in page and f'id="v-{rose[1]}"' in page   # handed to sara
    assert g.get(f"/voters/{rose[0]}").status_code == 404
    assert "1/1" in g.get("/my").get_data(as_text=True) or "0/1" in g.get("/my").get_data(as_text=True)
    assert g.post("/voters/assign", data={"csrf_token": csrf(g, "/"), "agent": str(ali),
                                          "ids": [rose[0]]}).status_code == 403

    # admin sees who has them, filters by agent, and sends one back to the house agent by filter
    assert "sara" in a.get("/?h=Rose+Villa").get_data(as_text=True)
    assert a.get(f"/?va={sara}").get_data(as_text=True).count('id="v-A2000') == 2
    import csv as _csv
    rows = {r["ID"]: r for r in _csv.DictReader(a.get("/export.csv").get_data(as_text=True).lstrip("\ufeff").splitlines())}
    assert (rows[rose[0]]["Agent"], rows[rose[0]]["Agent via"]) == ("sara", "One by one")
    assert (rows[rose[1]]["Agent"], rows[rose[1]]["Agent via"]) == ("ali", "House")
    assert rows["A200002"]["Agent"] == "sara"
    from openpyxl import load_workbook
    import io as _io
    ws = load_workbook(_io.BytesIO(a.get("/export.xlsx").data)).active
    assert [c.value for c in ws[1]][9:11] == ["Agent", "Agent via"]
    # agent filter (responsible agent, house or one by one), and the export follows it
    assert a.get(f"/?ag={sara}").get_data(as_text=True).count('id="v-A2000') == 2
    page = a.get(f"/?ag={ali}").get_data(as_text=True)
    assert page.count('id="v-A2000') == 1 and f'id="v-{rose[1]}"' in page and "Agent: ali" in page
    lines = a.get(f"/export.csv?ag={ali}").get_data(as_text=True).strip().splitlines()
    assert len(lines) == 2 and rose[1] in lines[1]
    assert a.get("/?ag=none").get_data(as_text=True).count('id="v-A2000') == 0
    a.post("/voters/assign", data={"csrf_token": token, "agent": "", "scope": "all", "h": "Rose Villa"})
    assert a.get(f"/?va={sara}").get_data(as_text=True).count('id="v-A2000') == 1
    assert f'id="v-{rose[0]}"' in g.get("/").get_data(as_text=True)
    conn = connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'voters_assigned'").fetchone()[0] == 3


def test_card_shows_who_changed_last(app):
    add_agent(app, "ali")
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/")
    r = a.post("/voters/A200002/canvass", data={"csrf_token": token, "status": "followup"},
               headers={"X-Requested-With": "fetch"})
    assert r.json["by"] == "boss" and r.json["at"]
    a.post("/voters/A200002/edit", data={"csrf_token": token, "full_name": "Moosa Beta", "sex": "M", "age": "61",
                                          "house_name": "Blue Ge", "party": "BBB", "phone": ""})
    card = a.get("/?q=Moosa").get_data(as_text=True)
    assert "Follow up by <strong>boss</strong>" in card and "Edited by <strong>boss</strong>" in card
    header = a.get("/export.csv").get_data(as_text=True).splitlines()[0]
    assert "Status by" in header and "Last edited by" in header


def test_filter_by_who_contacted(app):
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    u = app.test_client()
    login(u, "viewer", USER_PW)
    u.post("/voters/A200001/canvass", data={"csrf_token": csrf(u, "/"), "status": "contacted"})
    a.post("/voters/A200002/canvass", data={"csrf_token": csrf(a, "/"), "status": "noanswer"})
    ids = {r[1]: r[0] for r in connect(app.config["DATABASE"]).execute("SELECT id, username FROM users")}
    page = a.get("/").get_data(as_text=True)
    assert "Contacted by" in page and f'<option value="{ids["viewer"]}"' in page
    page = a.get(f"/?by={ids['viewer']}").get_data(as_text=True)
    assert page.count('id="v-A2000') == 1 and 'id="v-A200001"' in page and "Contacted by viewer" in page
    lines = a.get(f"/export.csv?by={ids['boss']}").get_data(as_text=True).strip().splitlines()
    assert len(lines) == 2 and "A200002" in lines[1]
    # set back to "not contacted": no longer counts as contacted by anyone
    u.post("/voters/A200001/canvass", data={"csrf_token": csrf(u, "/"), "status": "new"})
    assert a.get(f"/?by={ids['viewer']}").get_data(as_text=True).count('id="v-A2000') == 0


def test_dashboard_date_range_and_user_filter(app):
    from datetime import datetime, timedelta, timezone
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    u = app.test_client()
    login(u, "viewer", USER_PW)
    u.post("/voters/A200001/canvass", data={"csrf_token": csrf(u, "/"), "status": "contacted"})
    a.post("/voters/A200002/canvass", data={"csrf_token": csrf(a, "/"), "status": "followup"})
    ids = {r[1]: r[0] for r in connect(app.config["DATABASE"]).execute("SELECT id, username FROM users")}

    dash = a.get("/dashboard").get_data(as_text=True)
    assert "Last 7 days" in dash and "Reached by" in dash and "2 of 3" in dash

    # one person: only the houses where they reached someone
    dash = a.get(f"/dashboard?user={ids['viewer']}").get_data(as_text=True)
    assert "reached by <strong>viewer</strong>" in dash and "Rose Villa" in dash and "Blue Ge" not in dash
    assert "1 of 2" in dash

    maldives_today = (datetime.now(timezone.utc) + timedelta(hours=5)).date()  # the portal counts Maldives days
    today = maldives_today.isoformat()
    long_ago = (maldives_today - timedelta(days=400)).isoformat()
    dash = a.get(f"/dashboard?from={long_ago}&to={long_ago}").get_data(as_text=True)
    assert "0 of 3" in dash
    dash = a.get(f"/dashboard?user={ids['viewer']}&from={long_ago}&to={long_ago}").get_data(as_text=True)
    assert "Nobody was reached by viewer in this period" in dash
    dash = a.get(f"/dashboard?from={long_ago}&to={today}").get_data(as_text=True)
    assert "2 of 3" in dash
    # bad dates are ignored, reversed dates are swapped
    assert a.get("/dashboard?from=nope&to=2026-13-40").status_code == 200
    assert "2 of 3" in a.get(f"/dashboard?from={today}&to={long_ago}").get_data(as_text=True)

    # own dashboard follows the range too
    page = u.get(f"/my?from={long_ago}&to={long_ago}").get_data(as_text=True)
    assert "Aminath Alpha" not in page
    assert "Aminath Alpha" in u.get(f"/my?from={long_ago}&to={today}").get_data(as_text=True)
