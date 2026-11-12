import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_canvass import AGENT_PW, add_agent  # noqa: E402

# Fixture voters: A200001 Aminath Alpha (AAA, Rose Villa), A200002 Moosa Beta (BBB, Blue Ge),
# A200003 (no party, Rose Villa)


def set_parties(client, parties):
    token = csrf(client, "/admin/settings")
    return client.post("/admin/settings", data={"csrf_token": token, "parties": parties, "mode": "selected"},
                       follow_redirects=True)


def cards(client, path="/"):
    return client.get(path).get_data(as_text=True).count('id="v-A2000')


def test_campaign_hides_other_parties(app):
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    page = a.get("/admin/settings").get_data(as_text=True)
    assert "No party recorded" in page and "AAA" in page and "BBB" in page
    r = set_parties(a, ["AAA"])
    assert "AAA voters only" in r.get_data(as_text=True) and "2 other voters hidden" in r.get_data(as_text=True)

    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert cards(u, "/?q=A2000") == 1
    assert cards(u, "/?q=Moosa") == 0
    assert u.get("/voters/A200002").status_code == 404              # direct link to a hidden voter
    token = csrf(u, "/")
    assert u.post("/voters/A200002/canvass", data={"csrf_token": token, "status": "contacted"}).status_code == 404
    assert "Show all parties" not in u.get("/").get_data(as_text=True)   # users can't switch it off
    assert u.post("/admin/view-all", data={"csrf_token": token}).status_code == 403
    assert u.get("/dashboard").status_code == 403                    # full dashboard is admin-only
    dash = a.get("/dashboard").get_data(as_text=True)
    assert "Blue Ge" not in dash and "Rose Villa" in dash and "0 of 1" in dash

    # admin: campaign view by default, can switch to all parties for their own session only
    assert cards(a) == 1 and "Campaign view: <strong>AAA</strong>" in a.get("/").get_data(as_text=True)
    token = csrf(a, "/")
    a.post("/admin/view-all", data={"csrf_token": token, "back": "/"})
    assert cards(a) == 3 and a.get("/voters/A200002").status_code == 200
    assert "Moosa" in a.get("/export.csv").get_data(as_text=True)
    assert cards(u, "/?q=A2000") == 1                                             # other users unaffected
    a.post("/admin/view-all", data={"csrf_token": token})
    assert cards(a) == 1 and "Moosa" not in a.get("/export.csv").get_data(as_text=True)

    # no party can be part of the campaign too; clearing shows everyone again
    set_parties(a, ["AAA", "(none)"])
    assert cards(u, "/?q=A2000") == 2
    token = csrf(a, "/admin/settings")
    a.post("/admin/settings", data={"csrf_token": token, "mode": "all"})
    assert cards(u, "/?q=A2000") == 3
    assert "campaign_parties" in a.get("/admin/audit").get_data(as_text=True)


def test_campaign_and_agent_scopes_combine(app):
    agent_id = add_agent(app)
    conn = connect(app.config["DATABASE"])
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES ('Rose Villa', ?, 't')",
                 (agent_id,))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    set_parties(a, ["AAA"])
    g = app.test_client()
    login(g, "ali", AGENT_PW)
    assert cards(g) == 1                       # Rose Villa has 2 voters, only 1 is AAA
    assert g.get("/voters/A200003").status_code == 404
    assert "0/1" in g.get("/my").get_data(as_text=True)
    assert "1 voters" in a.get("/admin/agents").get_data(as_text=True)


def test_whatsapp_button(app):
    from urllib.parse import unquote

    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET phone = '7771234' WHERE national_id = 'A200001'")   # mobile
    conn.execute("UPDATE voters SET phone = '3001234' WHERE national_id = 'A200002'")   # landline
    conn.commit()
    u = app.test_client()
    login(u, "viewer", USER_PW)
    page = u.get("/?q=A2000").get_data(as_text=True)
    assert page.count('class="wa"') == 1
    assert 'href="https://wa.me/9607771234?text=Assalaamu%20alaikum%20Aminath%2C%20"' in page
    assert "wa.me" in u.get("/voters/A200001").get_data(as_text=True)
    assert "wa.me" not in u.get("/voters/A200002").get_data(as_text=True)   # landlines have no WhatsApp

    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/admin/settings")
    a.post("/admin/settings", data={"csrf_token": token, "form": "whatsapp",
                                    "template": "Hi {first_name} of {house}! {name.__class__} {unknown}"})
    page = u.get("/voters/A200001").get_data(as_text=True)
    link = page.split('href="https://wa.me/9607771234?text=')[1].split('"')[0]
    assert unquote(link) == "Hi Aminath of Rose Villa! {name.__class__} {unknown} "
    assert u.post("/admin/settings", data={"csrf_token": csrf(u, "/"), "form": "whatsapp",
                                           "template": "x"}).status_code == 403


def test_theme_appearance_and_sms(app):
    from urllib.parse import unquote

    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET phone = '9771234' WHERE national_id = 'A200001'")
    conn.execute("UPDATE voters SET phone = '3001234' WHERE national_id = 'A200002'")
    conn.commit()
    u = app.test_client()
    login(u, "viewer", USER_PW)
    page = u.get("/?q=A2000").get_data(as_text=True)
    assert page.count('class="sms"') == 1
    sms = page.split('class="sms" href="')[1].split('"')[0]
    assert sms.startswith("sms:+9609771234?&amp;body=") and "Aminath" in unquote(sms)
    assert "SMS" in u.get("/voters/A200001").get_data(as_text=True)

    # colour: teal by default, admins change it for everyone, users can't
    assert "/theme.css?v=teal" in page
    assert "--accent: #0f766e" in u.get("/theme.css?v=teal").get_data(as_text=True)
    token = csrf(u, "/")
    assert u.post("/admin/theme", data={"csrf_token": token, "theme": "mdp"}).status_code == 403
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    a.post("/admin/theme", data={"csrf_token": csrf(a, "/admin/settings"), "theme": "mdp"})
    page = u.get("/").get_data(as_text=True)
    assert "/theme.css?v=mdp" in page and 'content="#f5c400"' in page
    css = u.get("/theme.css?v=mdp").get_data(as_text=True)
    assert "--accent: #f5c400" in css and ':root[data-theme="dark"]' in css
    a.post("/admin/theme", data={"csrf_token": csrf(a, "/admin/settings"), "theme": "nope"})
    assert "/theme.css?v=mdp" in u.get("/").get_data(as_text=True)

    # appearance: per person, remembered in a cookie
    assert "data-theme" not in u.get("/").get_data(as_text=True).split("<head>")[0]
    u.post("/account/appearance", data={"appearance": "dark", "back": "/", "csrf_token": csrf(u, "/")})
    assert '<html lang="en" data-theme="dark">' in u.get("/").get_data(as_text=True)
    assert 'data-theme="dark"' not in a.get("/").get_data(as_text=True).split("<head>")[0]
    u.post("/account/appearance", data={"appearance": "system", "back": "/", "csrf_token": csrf(u, "/")})
    assert "data-theme" not in u.get("/").get_data(as_text=True).split("<head>")[0]


def test_separate_messages_picture_and_viber(app):
    import io
    from urllib.parse import unquote

    from PIL import Image

    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET phone = '7771234' WHERE national_id = 'A200001'")
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/admin/settings")
    page = a.get("/admin/settings").get_data(as_text=True)
    assert 'id="msg-whatsapp"' in page and 'id="msg-sms"' in page and 'id="msg-viber"' in page
    a.post("/admin/settings", data={"csrf_token": token, "form": "whatsapp", "template": "WA hi {first_name}",
                                    "picture_link": "1"})
    a.post("/admin/settings", data={"csrf_token": token, "form": "sms", "template": "SMS hi {first_name}"})
    detail = a.get("/voters/A200001").get_data(as_text=True)
    wa = unquote(detail.split('href="https://wa.me/')[1].split('"')[0])
    sms = unquote(detail.split('href="sms:')[1].split('"')[0])
    assert "WA hi Aminath" in wa and "SMS hi Aminath" in sms
    # Viber: opens the chat; the (here: WhatsApp's, until it has its own) text is copied by the page
    assert 'href="viber://chat?number=%2B9607771234"' in detail and 'data-copy="WA hi Aminath' in detail

    # picture: upload, public link in the WhatsApp text, served without sign-in
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "PNG")
    r = a.post("/admin/picture", data={"csrf_token": token, "picture": (io.BytesIO(buf.getvalue()), "p.png")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    detail = a.get("/voters/A200001").get_data(as_text=True)
    wa = unquote(detail.split('href="https://wa.me/')[1].split('"')[0])
    link = wa.split("http://localhost")[1].strip()
    assert link.startswith("/c/") and "data-share-picture" in detail
    anon = app.test_client()
    page = anon.get(link)
    assert page.status_code == 200 and 'property="og:image"' in page.get_data(as_text=True)
    img = anon.get(link + ".jpg")
    assert img.status_code == 200 and img.mimetype == "image/jpeg"
    assert anon.get("/c/wrong-token").status_code == 404 and anon.get("/c/wrong-token.jpg").status_code == 404
    # a bad file is refused; removing makes the old link stop working
    r = a.post("/admin/picture", data={"csrf_token": token, "picture": (io.BytesIO(b"nope"), "x.png")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "a JPEG, PNG or WebP picture" in r.get_data(as_text=True)
    a.post("/admin/picture", data={"csrf_token": token, "remove": "1"})
    assert anon.get(link + ".jpg").status_code == 404
    assert "/c/" not in unquote(a.get("/voters/A200001").get_data(as_text=True))

    # only admins can change any of it
    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert u.post("/admin/picture", data={"csrf_token": csrf(u, "/")}).status_code == 403
