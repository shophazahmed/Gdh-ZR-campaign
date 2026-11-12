import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401


def test_announcement_whatsapp_banner_and_seen(app):
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/announcements")
    r = a.post("/announcements", data={"csrf_token": token, "text": "Meeting tonight 9pm", "urgent": "1"})
    assert r.status_code == 302 and "sent=1" in r.headers["Location"]
    page = a.get("/announcements?sent=1").get_data(as_text=True)
    assert "https://wa.me/?text=" in page and "Seen by 0 of 1" in page
    assert "Meeting tonight 9pm" in unquote(page.split("https://wa.me/?text=")[1].split('"')[0])
    assert "urgent-banner" not in a.get("/").get_data(as_text=True)  # the writer doesn't get their own banner

    u = app.test_client()
    login(u, "viewer", USER_PW)
    home = u.get("/").get_data(as_text=True)
    assert "urgent-banner" in home and "Meeting tonight 9pm" in home
    assert "wa.me" not in u.get("/announcements").get_data(as_text=True)  # only admins send
    u.post("/announcements/1/seen", data={"csrf_token": csrf(u, "/"), "back": "/"})
    assert "urgent-banner" not in u.get("/").get_data(as_text=True)
    assert "Seen by 1 of 1" in a.get("/announcements").get_data(as_text=True)


def test_only_admins_post_and_take_down(app):
    u = app.test_client()
    login(u, "viewer", USER_PW)
    token = csrf(u, "/announcements")
    assert u.post("/announcements", data={"csrf_token": token, "text": "x"}).status_code == 403
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    t = csrf(a, "/announcements")
    a.post("/announcements", data={"csrf_token": t, "text": "Old news", "urgent": "1"})
    assert u.post("/announcements/1/end", data={"csrf_token": token}).status_code == 403
    a.post("/announcements/1/end", data={"csrf_token": t})
    assert "Old news" not in u.get("/").get_data(as_text=True)
    assert "Old news" not in u.get("/announcements").get_data(as_text=True)


def _owner(app):
    from app.db import connect
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE users SET is_owner = 1 WHERE username = 'boss'")
    conn.commit()


def test_whatsapp_list_to_agent(app):
    from tests.test_canvass import add_agent
    _owner(app)
    agent = add_agent(app, "sharu")
    from app.db import connect
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE users SET phone = '7948664' WHERE id = ?", (agent,))
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES ('Rose Villa', ?, 'x')", (agent,))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    assert "WhatsApp list" in a.get("/admin/agents").get_data(as_text=True)
    r = a.get(f"/voters/whatsapp-list?ag={agent}")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://wa.me/9607948664?text=")
    text = unquote(r.headers["Location"].split("text=")[1])
    assert "sharu: 1 house · 2 voters" in text and "*Rose Villa* (2)" in text and "2. Aminath Alpha · —" in text
    assert "Moosa Beta" not in text  # Blue Ge isn't theirs
    assert a.get("/voters/whatsapp-list?cf=1").status_code == 403


def test_whatsapp_list_owner_only(app):
    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert u.get("/voters/whatsapp-list").status_code == 403


def test_agent_dashboard_link(app):
    import re as _re
    from tests.test_canvass import add_agent
    from app.db import connect
    _owner(app)
    agent = add_agent(app, "sharu")
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE users SET phone = '7948664' WHERE id = ?", (agent,))
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES ('Rose Villa', ?, 'x')", (agent,))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/admin/agents")
    a.post(f"/admin/agents/{agent}/link", data={"csrf_token": token, "days": "7"})
    page = a.get("/admin/agents").get_data(as_text=True)
    url = _re.search(r'data-copy="([^"]+/a/[^"]+)"', page).group(1)
    assert "wa.me/9607948664?text=" in page and "opened 0 times" in page
    path = url.split("localhost", 1)[1]

    anon = app.test_client()  # no sign-in
    r = anon.get(path)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Aminath Alpha" in body and "Moosa Beta" not in body
    assert "For sharu · Do not forward" in body and r.headers["Cache-Control"] == "private, no-store"
    assert "csrf_token" not in body or "/admin" not in body
    assert "opened 1 time," in a.get("/admin/agents").get_data(as_text=True)
    assert anon.get(path + "x").status_code == 404
    assert anon.get(path.replace("/a/", "/a/") + "/p/A200002").status_code == 404  # not their voter

    a.post(f"/admin/agents/{agent}/link/stop", data={"csrf_token": token})
    assert anon.get(path).status_code == 410


def test_dashboard_link_owner_only(app):
    from tests.test_canvass import add_agent
    agent = add_agent(app, "sharu")
    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert u.post(f"/admin/agents/{agent}/link", data={"csrf_token": csrf(u, "/"), "days": "7"}).status_code == 403
