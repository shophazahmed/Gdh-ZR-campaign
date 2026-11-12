"""Login, sessions, CSRF and the RBAC decorators."""
import functools
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import (
    Blueprint,
    abort,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import audit
from .db import get_db, now_iso

bp = Blueprint("auth", __name__)

ROLES = ("admin", "agent", "user")
ROLE_LABELS = {
    "admin": "Admin: everything, including users, imports and exports",
    "user": "User: read-only, all voters, can record canvassing",
    "agent": "Agent: canvasser listed by name, no sign-in",
}
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 10

_hasher = PasswordHasher()  # Argon2id, library defaults
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def hash_password(password):
    return _hasher.hash(password)


def verify_password(stored_hash, password):
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password, username=""):
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if username and password.casefold() == username.casefold():
        return "The password can't be the same as the username."
    return None


def generate_password():
    return secrets.token_urlsafe(12)


# --- request hooks -----------------------------------------------------------

def load_user():
    """Attach the signed-in user to g, rejecting stale or disabled sessions."""
    g.user = None
    user_id = session.get("uid")
    if user_id is None:
        return
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    # A password change or deactivation ends every existing session.
    if (
        user is None
        or not user["is_active"]
        or not user["can_login"]
        or user["deleted_at"]
        or session.get("pwd_at") != user["password_changed_at"]
    ):
        session.clear()
        return
    g.user = user


def csrf_token():
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_urlsafe(32)
    return token


def _same_origin():
    """True when the browser says the request came from one of our own pages."""
    source = request.headers.get("Origin") or request.headers.get("Referer") or ""
    own = request.host_url.rstrip("/")
    return source == own or source.startswith(own + "/")


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if sent and hmac.compare_digest(sent, session.get("csrf", "")):
        return
    if request.endpoint == "auth.login":
        # Some phone browsers (Brave, private tabs, a page restored from the back cache) post the
        # sign-in form without the cookie the token lives in. Sign-in only needs proof that the form
        # is ours, which the Origin/Referer header gives; everything after sign-in still needs the token.
        if _same_origin():
            return
        flash("That sign-in page had expired. Please sign in again.", "error")
        return redirect(url_for("auth.login", next=request.form.get("next", "")))
    abort(400, description="Your form expired. Reload the page and try again.")


# --- decorators --------------------------------------------------------------

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if g.user["must_change_password"] and request.endpoint != "auth.change_password":
            flash("Choose a new password to continue.", "info")
            return redirect(url_for("auth.change_password"))
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles):
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                audit.log("access_denied", target=request.path)
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


admin_required = role_required("admin")

# The candidate's own account. Only it may see or change the "100% confirmed" voters.
OWNER_USERNAME = "admin"
CONFIRMED_ACTIONS = ("voter_confirm", "voter_unconfirm", "confirmed_export")


def _owner_flagged():
    if "owner_flagged" not in g:
        g.owner_flagged = get_db().execute("SELECT 1 FROM users WHERE is_owner = 1 LIMIT 1").fetchone() is not None
    return g.owner_flagged


def is_owner(user=None):
    """The candidate's account: flagged as owner, so it keeps the confirmed list when renamed. A fresh
    install has no flag yet; then the account called 'admin' is the owner (and gets the flag on rename)."""
    user = user if user is not None else g.get("user")
    if user is None or user["role"] != "admin":
        return False
    if user["is_owner"]:
        return True
    return user["username"].casefold() == OWNER_USERNAME and not _owner_flagged()


CONFIRMED_ACCESS = {"": "No access", "view": "View", "mark": "View and mark"}


def confirmed_access(user=None):
    """'owner', 'mark', 'view' or '' for the 100% confirmed list. Only the owner gives access; downloads stay
    with the owner."""
    user = user if user is not None else g.get("user")
    if user is None:
        return ""
    if is_owner(user):
        return "owner"
    if not user["can_login"] or user["deleted_at"] or not user["is_active"]:
        return ""
    return user["confirmed_access"] if user["confirmed_access"] in ("view", "mark") else ""


def can_view_confirmed(user=None):
    return confirmed_access(user) in ("owner", "view", "mark")


def can_mark_confirmed(user=None):
    return confirmed_access(user) in ("owner", "mark")


def _confirmed_gate(test):
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not test():
                audit.log("access_denied", target=request.path)
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


confirmed_viewer_required = _confirmed_gate(can_view_confirmed)
confirmed_marker_required = _confirmed_gate(can_mark_confirmed)


def can_manage_confirmed_voters(view):
    """Strict rule for every confirmed-voter route: only the owner account, nobody else."""
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_owner():
            audit.log("access_denied", target=request.path)
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def is_admin():
    return g.get("user") is not None and g.user["role"] == "admin"


# --- views -------------------------------------------------------------------

def _safe_next(target, user=None):
    if target in (None, "", "/"):
        return home_url(user)
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return home_url()


def home_url(user=None):
    user = user or g.get("user")
    if user is not None and user["role"] == "agent":
        return url_for("canvass.my_houses")
    return url_for("voters.search")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        target = request.args.get("next")
        return redirect(_safe_next(target) if target else home_url())
    if request.method != "POST":
        if request.args.get("check"):
            # We just signed them in, but the browser didn't keep the session cookie.
            flash("Your browser is blocking cookies for this site, so you can't stay signed in. "
                  "In Brave, tap the lion icon and turn Shields off for this site, or use Safari or Chrome. "
                  "Private tabs that block cookies won't work either.", "error")
        resp = make_response(render_template("login.html", next=request.args.get("next", "")))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    db = get_db()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    now = datetime.now(timezone.utc)
    error = "Wrong username or password."

    if user is None:
        verify_password(_DUMMY_HASH, password)  # same timing as a real check
        audit.log("login_failed", target=username[:64], detail="unknown user")
    elif user["locked_until"] and datetime.fromisoformat(user["locked_until"]) > now:
        error = "Too many failed attempts. Try again in 15 minutes."
        audit.log("login_blocked", target=user["username"], detail="account locked")
    elif not verify_password(user["password_hash"], password):
        failed = user["failed_logins"] + 1
        locked = (now + LOCKOUT).replace(microsecond=0).isoformat() if failed >= MAX_FAILED_LOGINS else None
        db.execute(
            "UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
            (0 if locked else failed, locked, user["id"]),
        )
        audit.log("login_failed", target=user["username"], detail="locked" if locked else "bad password")
    elif not user["can_login"] or user["deleted_at"]:
        error = "This account doesn't sign in. Ask an admin."
        audit.log("login_failed", target=user["username"], detail="no sign-in")
    elif not user["is_active"]:
        error = "This account is deactivated. Ask an admin."
        audit.log("login_failed", target=user["username"], detail="inactive")
    else:
        if _hasher.check_needs_rehash(user["password_hash"]):
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
        db.execute(
            "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
            (now_iso(), user["id"]),
        )
        session.clear()
        session.permanent = True
        session["uid"] = user["id"]
        session["pwd_at"] = user["password_changed_at"]
        audit.log("login", user=user)
        # Go through /login once more: a signed-in browser is sent straight on to the page,
        # one that dropped the cookie sees why instead of a silent loop back to sign-in.
        return redirect(url_for("auth.login", check=1, next=_safe_next(request.form.get("next"), user)))

    flash(error, "error")
    return render_template("login.html", next=request.form.get("next", ""), username=username), 401


@bp.post("/logout")
def logout():
    if g.user is not None:
        audit.log("logout")
    session.clear()
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        problem = None
        if not verify_password(g.user["password_hash"], current):
            problem = "Your current password is wrong."
        elif new != confirm:
            problem = "The new passwords don't match."
        elif new == current:
            problem = "Pick a password different from the current one."
        else:
            problem = password_problem(new, g.user["username"])
        if problem:
            flash(problem, "error")
        else:
            stamp = now_iso(precise=True)
            db = get_db()
            db.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0, password_changed_at = ?"
                " WHERE id = ?",
                (hash_password(new), stamp, g.user["id"]),
            )
            audit.log("password_changed", target=g.user["username"])
            session["pwd_at"] = stamp
            flash("Password changed.", "success")
            return redirect(home_url())
    return render_template("account/password.html")
