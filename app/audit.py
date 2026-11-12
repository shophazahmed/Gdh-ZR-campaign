import json

from flask import g, has_request_context, request

from .db import get_db, now_iso


def client_ip():
    if not has_request_context():
        return None
    return request.remote_addr


def log(action, target=None, detail=None, user=None, db=None, commit=True):
    """Append one row to audit_log. `detail` may be a dict (stored as JSON)."""
    db = db or get_db()
    if user is None and has_request_context():
        user = g.get("user")
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    db.execute(
        "INSERT INTO audit_log (at, user_id, username, action, target, detail, ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            user["id"] if user else None,
            user["username"] if user else None,
            action,
            target,
            detail,
            client_ip(),
        ),
    )
    if commit:
        db.commit()
