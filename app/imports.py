"""Admin page to upload the voter files and run scripts/ingest.py in the background."""
import io
import json
import os
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import audit
from .auth import admin_required
from .db import connect, get_db, now_iso

bp = Blueprint("imports", __name__, url_prefix="/admin/import")

TABLE_EXTS = {".xlsx", ".xlsm", ".xls", ".csv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
ALLOWED_EXTS = TABLE_EXTS | IMAGE_EXTS | {".zip", ".pdf"}
MAX_UNZIPPED = 1024 * 1024 * 1024  # 1 GB: guards against zip bombs
MAX_ZIP_MEMBERS = 20000
STALE_AFTER = timedelta(minutes=30)   # a "running" job older than this died with its worker
KEEP_FILES_FOR = timedelta(hours=24)  # uploaded files of previews are deleted after this

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_jobs (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    user_id     INTEGER,
    username    TEXT,
    status      TEXT NOT NULL,              -- running, done, failed
    dry_run     INTEGER NOT NULL,
    mode        TEXT NOT NULL,              -- upsert, replace
    files       TEXT NOT NULL,              -- JSON list of uploaded file names
    output      TEXT,                       -- what the importer printed
    applied_by  TEXT                        -- id of the real import made from this preview
);
"""


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def imports_dir():
    return Path(current_app.config["IMPORTS_DIR"])


def job_dir(job_id):
    return imports_dir() / job_id


def get_job(job_id):
    try:
        uuid.UUID(job_id)
    except ValueError:
        abort(404)
    job = get_db().execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        abort(404)
    return job


# --- housekeeping -------------------------------------------------------------------

def _housekeeping(db):
    now = datetime.now(timezone.utc)
    db.execute(
        "UPDATE import_jobs SET status = 'failed', finished_at = ?,"
        " output = COALESCE(output, '') || '\nStopped: the server restarted while this import was running.'"
        " WHERE status = 'running' AND created_at < ?",
        (now_iso(), (now - STALE_AFTER).isoformat(timespec="seconds")),
    )
    db.commit()
    base = imports_dir()
    if not base.is_dir():
        return
    for d in base.iterdir():
        files = d / "files"
        if files.is_dir() and now - datetime.fromtimestamp(d.stat().st_mtime, timezone.utc) > KEEP_FILES_FOR:
            shutil.rmtree(files, ignore_errors=True)


def _running_job(db):
    return db.execute("SELECT id FROM import_jobs WHERE status = 'running' LIMIT 1").fetchone()


# --- saving uploads -----------------------------------------------------------------

def _clean_name(name):
    name = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "").strip().lstrip(".")
    return name[:180]


def _safe_extract(fileobj, dest):
    """Extract a zip without letting it write outside `dest` or explode in size."""
    with zipfile.ZipFile(fileobj) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > MAX_ZIP_MEMBERS:
            raise ValueError("The zip holds too many files.")
        if sum(m.file_size for m in members) > MAX_UNZIPPED:
            raise ValueError("The zip is larger than 1 GB when unpacked.")
        root = dest.resolve()
        for m in members:
            parts = [p for p in m.filename.replace("\\", "/").split("/") if p not in ("", ".", "..")]
            if not parts or parts[0] == "__MACOSX" or parts[-1].startswith("._"):
                continue
            if Path(parts[-1]).suffix.lower() not in TABLE_EXTS | IMAGE_EXTS | {".pdf"}:
                continue
            target = root.joinpath(*parts).resolve()
            if root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _data_folder(files_dir):
    """The folder holding the spreadsheets: the upload root, or a folder inside an uploaded zip."""
    best, best_count = files_dir, -1
    for d in [files_dir, *[p for p in files_dir.rglob("*") if p.is_dir()]]:
        count = sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in TABLE_EXTS | {".pdf"})
        if count > best_count:
            best, best_count = d, count
    return best


# --- running ------------------------------------------------------------------------

def _run(app_config, job_id, dry_run, mode, data_dir):
    """Background thread: run the importer and store its output on the job row."""
    from scripts import ingest

    out = io.StringIO()
    d = Path(app_config["IMPORTS_DIR"]) / job_id
    status = "done"
    try:
        argv = ["--data-dir", str(data_dir), "--db", app_config["DATABASE"],
                "--photos-dir", app_config["PHOTOS_DIR"], "--log", str(d / "merge_conflicts.log"),
                "--mode", mode, "--no-seed-users"]
        if dry_run:
            argv += ["--dry-run", "--csv-out", str(d / "merged_preview.csv")]
        _convert_pdfs(Path(data_dir), out)
        ingest.main(argv, out=out)
    except ingest.IngestError as exc:
        status = "failed"
        out.write(f"\n{exc}\n")
    except Exception:
        status = "failed"
        out.write("\nThe import stopped with an error:\n" + traceback.format_exc(limit=3))
    conn = connect(app_config["DATABASE"])
    try:
        text = (out.getvalue().replace(str(d / "merge_conflicts.log"), "merge_conflicts.log")
                .replace(app_config["DATABASE"], "portal database"))
        text = "\n".join(line for line in text.splitlines() if not line.startswith("Data folder: "))
        conn.execute("UPDATE import_jobs SET status = ?, finished_at = ?, output = ? WHERE id = ?",
                     (status, now_iso(), text, job_id))
        conn.execute(
            "INSERT INTO audit_log (at, user_id, username, action, target, detail)"
            " SELECT ?, user_id, username, ?, id, ? FROM import_jobs WHERE id = ?",
            (now_iso(), "import_preview" if dry_run else "import_run",
             json.dumps({"status": status, "mode": mode}), job_id),
        )
        conn.commit()
    finally:
        conn.close()
    if not dry_run:
        shutil.rmtree(d / "files", ignore_errors=True)  # don't keep raw voter files after a real import


def _convert_pdfs(data_dir, out):
    """Turn each uploaded PDF into <name>.csv + photos/ (skipped when a same-named table exists)."""
    from scripts.extract_from_pdf import extract_pdf

    for pdf in sorted(data_dir.glob("*.pdf")) + sorted(data_dir.glob("*.PDF")):
        if any((data_dir / f"{pdf.stem}{ext}").exists() for ext in TABLE_EXTS):
            out.write(f"{pdf.name}: a spreadsheet with the same name was uploaded too; using that instead.\n")
            continue
        extract_pdf(pdf, reverse_rtl=False, say=lambda *a: out.write(" ".join(str(x) for x in a) + "\n"))
    out.write("\n")


def _start(job_id, dry_run, mode, data_dir):
    db = get_db()
    files = sorted(str(p.relative_to(job_dir(job_id) / "files")) for p in (job_dir(job_id) / "files").rglob("*")
                   if p.is_file())
    db.execute(
        "INSERT INTO import_jobs (id, created_at, user_id, username, status, dry_run, mode, files)"
        " VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
        (job_id, now_iso(), g.user["id"], g.user["username"], int(dry_run), mode, json.dumps(files)),
    )
    db.commit()
    config = {k: current_app.config[k] for k in ("DATABASE", "PHOTOS_DIR", "IMPORTS_DIR")}
    thread = threading.Thread(target=_run, args=(config, job_id, dry_run, mode, data_dir),
                              name=f"import-{job_id[:8]}", daemon=True)
    thread.start()
    if current_app.config.get("TESTING"):
        thread.join()


# --- views --------------------------------------------------------------------------

@bp.route("", methods=["GET", "POST"])
@admin_required
def index():
    db = get_db()
    _housekeeping(db)
    if request.method == "POST":
        if _running_job(db):
            flash("Another import is still running. Wait for it to finish.", "error")
            return redirect(url_for("imports.index"))
        mode = request.form.get("mode", "upsert")
        if mode not in ("upsert", "replace"):
            abort(400)
        uploads = [f for f in request.files.getlist("files") if f and f.filename]
        if not uploads:
            flash("Choose the voter files (or one .zip) to upload.", "error")
            return redirect(url_for("imports.index"))
        job_id = str(uuid.uuid4())
        files_dir = job_dir(job_id) / "files"
        files_dir.mkdir(parents=True, mode=0o700)
        try:
            for up in uploads:
                name = _clean_name(up.filename)
                ext = Path(name).suffix.lower()
                if not name or ext not in ALLOWED_EXTS:
                    raise ValueError(f"'{up.filename}' isn't a spreadsheet, image or .zip.")
                if ext == ".zip":
                    try:
                        _safe_extract(up.stream, files_dir)
                    except zipfile.BadZipFile:
                        raise ValueError(f"'{name}' isn't a readable zip file.")
                else:
                    up.save(files_dir / name)
        except ValueError as exc:
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
            flash(str(exc), "error")
            return redirect(url_for("imports.index"))
        audit.log("import_upload", target=job_id, detail={"files": [_clean_name(u.filename) for u in uploads]})
        _start(job_id, dry_run=True, mode=mode, data_dir=_data_folder(files_dir))
        return redirect(url_for("imports.job", job_id=job_id))

    jobs = db.execute("SELECT * FROM import_jobs ORDER BY created_at DESC LIMIT 20").fetchall()
    return render_template("admin/import.html", jobs=jobs, running=_running_job(db))


@bp.route("/<job_id>")
@admin_required
def job(job_id):
    _housekeeping(get_db())
    j = get_job(job_id)
    d = job_dir(job_id)
    return render_template(
        "admin/import_job.html", j=j, files=json.loads(j["files"]),
        has_log=(d / "merge_conflicts.log").is_file(),
        has_csv=(d / "merged_preview.csv").is_file(),
        can_apply=bool(j["dry_run"] and j["status"] == "done" and not j["applied_by"] and (d / "files").is_dir()),
        log_head=_log_head(d / "merge_conflicts.log"),
    )


def _log_head(path, max_lines=400):
    if not path.is_file():
        return ""
    with open(path, encoding="utf-8") as fh:
        lines = [next(fh, None) for _ in range(max_lines)]
    text = "".join(line for line in lines if line is not None)
    return text


@bp.post("/<job_id>/apply")
@admin_required
def apply(job_id):
    db = get_db()
    j = get_job(job_id)
    files_dir = job_dir(job_id) / "files"
    if not (j["dry_run"] and j["status"] == "done" and not j["applied_by"] and files_dir.is_dir()):
        flash("This preview can't be applied any more. Upload the files again.", "error")
        return redirect(url_for("imports.job", job_id=job_id))
    if _running_job(db):
        flash("Another import is still running. Wait for it to finish.", "error")
        return redirect(url_for("imports.job", job_id=job_id))
    mode = j["mode"]
    if mode == "replace" and request.form.get("confirm") != "REPLACE":
        flash("Type REPLACE to confirm wiping the current voter list.", "error")
        return redirect(url_for("imports.job", job_id=job_id))
    new_id = str(uuid.uuid4())
    new_dir = job_dir(new_id)
    new_dir.mkdir(parents=True, mode=0o700)
    shutil.move(str(files_dir), str(new_dir / "files"))
    db.execute("UPDATE import_jobs SET applied_by = ? WHERE id = ?", (new_id, job_id))
    db.commit()
    _start(new_id, dry_run=False, mode=mode, data_dir=_data_folder(new_dir / "files"))
    return redirect(url_for("imports.job", job_id=new_id))


@bp.route("/<job_id>/<kind>")
@admin_required
def download(job_id, kind):
    get_job(job_id)
    names = {"log": ("merge_conflicts.log", "text/plain"), "csv": ("merged_preview.csv", "text/csv")}
    if kind not in names:
        abort(404)
    name, mime = names[kind]
    path = job_dir(job_id) / name
    if not path.is_file():
        abort(404)
    audit.log("import_download", target=job_id, detail=name)
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(path.stat().st_mtime))
    return send_file(path, mimetype=mime, as_attachment=True,
                     download_name=f"{Path(name).stem}-{stamp}{Path(name).suffix}", max_age=0)
