import io
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.fixtures import build  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401


def zip_folder(folder, prefix="Data files/"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in Path(folder).rglob("*"):
            if p.is_file():
                zf.write(p, prefix + str(p.relative_to(folder)))
        zf.writestr("../../evil.csv", "id\nA999999\n")  # path traversal attempt
    buf.seek(0)
    return buf


def test_upload_preview_apply(app, tmp_path):
    data = build(tmp_path / "src")
    c = app.test_client()
    login(c, "boss", ADMIN_PW)
    token = csrf(c, "/admin/import")
    r = c.post("/admin/import", data={"csrf_token": token, "mode": "upsert",
                                      "files": (zip_folder(data), "Data files.zip")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    job_url = r.headers["Location"]
    page = c.get(job_url).get_data(as_text=True)
    assert "Import preview" in page and "unique voters: 9" in page and "Apply import" in page
    assert "AMBIGUOUS_NAME_MISMATCH" in page
    conn = connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0] == 3  # preview changed nothing
    assert c.get(job_url + "/csv").data.startswith(b"\xef\xbb\xbfID,")
    assert not (Path(app.config["IMPORTS_DIR"]).parent / "evil.csv").exists()

    r = c.post(job_url + "/apply", data={"csrf_token": token})
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "done" in page and "&#39;inserted&#39;: 9" in page
    assert conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0] == 12
    assert conn.execute("SELECT photo_path FROM voters WHERE national_id = 'A100001'").fetchone()[0] == "A100001.jpg"
    # raw files are gone after a real import, and the preview can't be applied twice
    job_id = r.headers["Location"].rsplit("/", 1)[1]
    assert not (Path(app.config["IMPORTS_DIR"]) / job_id / "files").exists()
    assert c.post(job_url + "/apply", data={"csrf_token": token}).status_code == 302
    assert conn.execute("SELECT COUNT(*) FROM import_jobs WHERE dry_run = 0").fetchone()[0] == 1
    audit = c.get("/admin/audit").get_data(as_text=True)
    for action in ("import_upload", "import_preview", "import_run", "import_download"):
        assert action in audit


def test_replace_needs_confirmation(app, tmp_path):
    data = build(tmp_path / "src")
    c = app.test_client()
    login(c, "boss", ADMIN_PW)
    token = csrf(c, "/admin/import")
    files = [(open(p, "rb"), p.name) for p in Path(data).iterdir() if p.is_file()]
    r = c.post("/admin/import", data={"csrf_token": token, "mode": "replace", "files": files},
               content_type="multipart/form-data")
    job_url = r.headers["Location"]
    assert "Type REPLACE" in c.get(job_url).get_data(as_text=True)
    c.post(job_url + "/apply", data={"csrf_token": token, "confirm": "nope"})
    conn = connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0] == 3
    c.post(job_url + "/apply", data={"csrf_token": token, "confirm": "REPLACE"})
    ids = {r[0] for r in conn.execute("SELECT national_id FROM voters")}
    assert len(ids) == 9 and "A200001" not in ids


def test_rejects_bad_files_and_non_admins(app):
    c = app.test_client()
    login(c, "boss", ADMIN_PW)
    token = csrf(c, "/admin/import")
    r = c.post("/admin/import", data={"csrf_token": token, "mode": "upsert",
                                      "files": (io.BytesIO(b"MZ"), "virus.exe")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "a spreadsheet, image or .zip" in r.get_data(as_text=True)
    r = c.post("/admin/import", data={"csrf_token": token, "mode": "upsert",
                                      "files": (io.BytesIO(b"name\nx\n"), "random.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert re.search(r"None of the four voter files were found", r.get_data(as_text=True))

    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert u.get("/admin/import").status_code == 403
    assert "Import</a>" not in u.get("/").get_data(as_text=True)


def test_pdf_upload(app, tmp_path):
    from tests.test_pdf import PEOPLE, make_pdf

    make_pdf(tmp_path / "Gahdhdhoo.2026.pdf", ruled=True)
    c = app.test_client()
    login(c, "boss", ADMIN_PW)
    token = csrf(c, "/admin/import")
    with open(tmp_path / "Gahdhdhoo.2026.pdf", "rb") as fh:
        r = c.post("/admin/import", data={"csrf_token": token, "mode": "upsert",
                                          "files": (fh, "Gahdhdhoo.2026.pdf")},
                   content_type="multipart/form-data")
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert f"{len(PEOPLE)} voter rows on 2 pages" in page and f"unique voters: {len(PEOPLE)}" in page
    c.post(r.headers["Location"] + "/apply", data={"csrf_token": token})
    conn = connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM voters WHERE national_id LIKE 'A3000%' AND photo_path IS NOT NULL"
                        ).fetchone()[0] == 11
