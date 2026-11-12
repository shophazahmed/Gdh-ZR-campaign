"""Gadhdhoo voter portal: Flask application factory."""
import os
import secrets
from datetime import datetime, timedelta, timezone

import click
from flask import Flask, g, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

from . import admin, agentlink, announce, auth, campaign, canvass, confirmed, fonts, imports, personal, share, voters
from .db import close_db, connect, init_schema, now_iso

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)


def env_flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    base = os.environ.get("VOTER_PORTAL_DATA_DIR", app.instance_path)
    secret = os.environ.get("VOTER_PORTAL_SECRET_KEY")
    dev = env_flag("VOTER_PORTAL_DEV", "0")
    app.config.update(
        SECRET_KEY=secret or secrets.token_hex(32),
        DATABASE=os.environ.get("VOTER_PORTAL_DB", os.path.join(base, "voter_portal.db")),
        PHOTOS_DIR=os.environ.get("VOTER_PORTAL_PHOTOS", os.path.join(base, "photos")),
        IMPORTS_DIR=os.path.join(base, "imports"),
        CAMPAIGN_DIR=os.path.join(base, "campaign"),
        SESSION_COOKIE_NAME="vp_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=not dev,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        # Large for the import page (workbooks with embedded photos); voter photo uploads check 5 MB themselves
        MAX_CONTENT_LENGTH=300 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if not secret and not (dev or app.config.get("TESTING")):
        raise RuntimeError("Set VOTER_PORTAL_SECRET_KEY (or VOTER_PORTAL_DEV=1 for local testing).")

    os.makedirs(os.path.dirname(os.path.abspath(app.config["DATABASE"])), exist_ok=True)
    os.makedirs(app.config["PHOTOS_DIR"], exist_ok=True)
    os.makedirs(app.config["IMPORTS_DIR"], mode=0o700, exist_ok=True)
    os.makedirs(app.config["CAMPAIGN_DIR"], exist_ok=True)

    # Nginx sits in front: trust one hop of X-Forwarded-For / -Proto.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.teardown_appcontext(close_db)
    app.before_request(auth.load_user)
    app.before_request(auth.check_csrf)
    app.register_blueprint(auth.bp)
    app.register_blueprint(voters.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(imports.bp)
    app.register_blueprint(canvass.bp)
    app.register_blueprint(campaign.bp)
    app.register_blueprint(campaign.themes_bp)
    app.register_blueprint(campaign.picture_bp)
    app.register_blueprint(confirmed.bp)
    app.register_blueprint(share.bp)
    app.register_blueprint(fonts.bp)
    app.register_blueprint(announce.bp)
    app.register_blueprint(agentlink.bp)
    app.jinja_env.globals["dhivehi_font"] = fonts.font_file
    app.jinja_env.globals["dhivehi_font_name"] = fonts.font_name
    app.jinja_env.globals["share_people"] = share.people

    @app.template_filter("local")
    def local_time(value):
        """Stored UTC timestamp -> Maldives time, e.g. '11 Oct 17:20'."""
        if not value:
            return ""
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            return value[:16].replace("T", " ")
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (stamp + timedelta(hours=5)).strftime("%-d %b %H:%M")

    @app.context_processor
    def inject():
        return {"csrf_token": auth.csrf_token, "current_user": g.get("user"), "is_admin": auth.is_admin(), "is_owner": auth.is_owner(),
                "can_view_cf": auth.can_view_confirmed(), "can_mark_cf": auth.can_mark_confirmed(),
                "confirmed_metrics": confirmed.metrics,
                "campaign_banner": campaign.banner, "whatsapp_url": campaign.whatsapp_url,
                "sms_url": campaign.sms_url, "viber_url": campaign.viber_url, "campaign_message": campaign.message,
                "picture_token": campaign.picture_token, "theme_name": campaign.theme_name, "themes": campaign.THEMES,
                "appearance": campaign.appearance, "urgent_notice": announce.banner}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if g.get("user") is not None:
            resp.headers.setdefault("Cache-Control", "private, no-store")
        return resp

    for code, title in (
        (400, "Bad request"),
        (403, "Not allowed"),
        (404, "Not found"),
        (413, "File too large"),
        (500, "Something went wrong"),
    ):
        app.register_error_handler(
            code,
            lambda e, code=code, title=title: (
                render_template("error.html", code=code, title=title, message=getattr(e, "description", "")),
                code,
            ),
        )

    register_cli(app)
    with app.app_context():
        conn = connect(app.config["DATABASE"])
        init_schema(conn)
        imports.init_schema(conn)
        personal.init_schema(conn)
        campaign.init_schema(conn)
        announce.init_schema(conn)
        agentlink.init_schema(conn)
        bootstrap_admin(conn)
        conn.close()
    return app


def bootstrap_admin(conn):
    """First start only: create 'admin' from VOTER_PORTAL_ADMIN_PASSWORD when there are no users."""
    password = os.environ.get("VOTER_PORTAL_ADMIN_PASSWORD", "")
    if not password or conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return
    stamp = now_iso(precise=True)
    # OR IGNORE: several Gunicorn workers may get here at the same time
    conn.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role, must_change_password,"
        " password_changed_at, created_at) VALUES ('admin', ?, 'admin', 1, ?, ?)",
        (auth.hash_password(password), stamp, stamp),
    )
    conn.commit()


def register_cli(app):
    @app.cli.command("create-user")
    @click.argument("username")
    @click.option("--role", type=click.Choice(auth.ROLES), default="user")
    @click.password_option()
    def create_user(username, role, password):
        """Create a user (they must change the password at first sign-in)."""
        problem = auth.password_problem(password, username)
        if problem:
            raise click.ClickException(problem)
        conn = connect(app.config["DATABASE"])
        stamp = now_iso(precise=True)
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, password_changed_at, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (username, auth.hash_password(password), role, stamp, stamp),
            )
        except Exception as exc:  # sqlite3.IntegrityError on duplicate
            raise click.ClickException(f"Could not create user: {exc}")
        conn.commit()
        click.echo(f"Created {role} '{username}'.")

    @app.cli.command("set-password")
    @click.argument("username")
    @click.password_option()
    def set_password(username, password):
        """Reset a password and unlock / reactivate the account."""
        problem = auth.password_problem(password, username)
        if problem:
            raise click.ClickException(problem)
        conn = connect(app.config["DATABASE"])
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, password_changed_at = ?, failed_logins = 0,"
            " locked_until = NULL, is_active = 1, must_change_password = 1 WHERE username = ?",
            (auth.hash_password(password), now_iso(precise=True), username),
        )
        conn.commit()
        if not cur.rowcount:
            raise click.ClickException(f"No user '{username}'.")
        click.echo(f"Password set for '{username}'. They must change it at next sign-in.")
