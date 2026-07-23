"""Password hashing, cookie sessions, and the ASGI guard protecting every route."""
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import bcrypt
from fastapi import Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import db
from .config import Settings
from .models import Session, User

SESSION_COOKIE = "ao3_session"
# One write per hour per session: SSE keepalives would otherwise turn every
# page view into a database write.
TOUCH_INTERVAL = timedelta(hours=1)

PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",  # the Docker healthcheck carries no cookie
        "/login",
        "/logout",
        "/auth/oidc/login",
        "/auth/oidc/callback",
        "/favicon.ico",
    }
)
# /static holds only app.js, which already ships publicly in the image and on
# GitHub. Revisit this if a private asset is ever added there.
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)

# A real bcrypt hash of a random string, compared against on unknown usernames so
# the response time does not reveal whether an account exists.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(16), bcrypt.gensalt())

MAX_PASSWORD_BYTES = 72  # bcrypt truncates past this; reject rather than silently cut


# ---------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        # OIDC accounts have no local password and must never be reachable by one.
        bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def password_problem(password: str) -> str | None:
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return "Password must be at most 72 bytes."
    return None


# ---------------------------------------------------------------- tokens & sessions


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_session(db_path: Path, raw_token: str | None) -> tuple[User, Session] | None:
    if not raw_token:
        return None
    session = db.get_session(db_path, token_hash(raw_token))
    if session is None:
        return None

    now = _now()
    if session.expires_at <= now.isoformat():
        db.delete_session(db_path, session.token_hash)
        return None

    user = db.get_user(db_path, session.user_id)
    if user is None:  # account deleted mid-session
        db.delete_session(db_path, session.token_hash)
        return None

    if now - datetime.fromisoformat(session.last_used_at) > TOUCH_INTERVAL:
        db.touch_session(db_path, session.token_hash, now.isoformat())
    return user, session


def cookie_kwargs(settings: Settings, request: Request) -> dict:
    mode = settings.session_cookie_secure
    if mode == "true":
        secure = True
    elif mode == "false":
        secure = False
    else:
        secure = request.url.scheme == "https"
    return {
        "httponly": True,
        # Lax, not Strict: Strict withholds the cookie when arriving from an
        # external link (an IdP application tile), bouncing the user to /login.
        "samesite": "lax",
        "secure": secure,
        "path": "/",
    }


def issue_session(
    db_path: Path, user: User, response: Response, settings: Settings, request: Request
) -> str:
    token = new_token()
    csrf_token = new_token()
    now = _now()
    db.create_session(
        db_path,
        token_hash=token_hash(token),
        user_id=user.id,
        csrf_token=csrf_token,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(days=settings.session_ttl_days)).isoformat(),
        user_agent=request.headers.get("user-agent", "")[:200] or None,
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_days * 86400,
        **cookie_kwargs(settings, request),
    )
    return csrf_token


def revoke_session(db_path: Path, raw_token: str | None) -> None:
    if raw_token:
        db.delete_session(db_path, token_hash(raw_token))


def clear_session_cookie(response: Response, settings: Settings, request: Request) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def check_csrf(session: Session, submitted: str | None) -> bool:
    return bool(submitted) and hmac.compare_digest(session.csrf_token, submitted)


def safe_next(raw: str | None) -> str:
    """Only allow same-site absolute paths, so ?next= cannot become an open redirect."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


# ---------------------------------------------------------------- login throttle

# In-memory is sufficient: the app is deliberately a single process.
_failures: dict[str, list[float]] = {}


def _throttle_key(username: str, client_ip: str) -> str:
    return f"{client_ip}|{username.lower()}"


def login_allowed(settings: Settings, username: str, client_ip: str) -> bool:
    attempts = _failures.get(_throttle_key(username, client_ip), [])
    cutoff = time.monotonic() - settings.login_lockout_seconds
    return len([t for t in attempts if t > cutoff]) < settings.login_max_attempts


def record_login_failure(settings: Settings, username: str, client_ip: str) -> None:
    key = _throttle_key(username, client_ip)
    cutoff = time.monotonic() - settings.login_lockout_seconds
    attempts = [t for t in _failures.get(key, []) if t > cutoff]
    attempts.append(time.monotonic())
    _failures[key] = attempts


def clear_login_failures(username: str, client_ip: str) -> None:
    _failures.pop(_throttle_key(username, client_ip), None)


# ---------------------------------------------------------------- the guard


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _is_admin_area(path: str) -> bool:
    return path == "/system" or path.startswith("/system/") or path.startswith("/api/system/")


class AuthMiddleware:
    """Default-deny guard applied to every request.

    Pure ASGI rather than BaseHTTPMiddleware: the latter wraps responses in
    anyio streams, which is long-standing trouble for held-open StreamingResponse
    bodies — and /api/events is exactly that.
    """

    def __init__(self, app, db_path: Path) -> None:
        self.app = app
        self.db_path = db_path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or is_public(scope["path"]):
            return await self.app(scope, receive, send)

        request = Request(scope, receive)
        resolved = resolve_session(self.db_path, request.cookies.get(SESSION_COOKIE))
        if resolved is None:
            return await self._deny(scope, receive, send, 401)

        user, session = resolved
        if _is_admin_area(scope["path"]) and not user.is_admin:
            return await self._deny(scope, receive, send, 403)

        scope.setdefault("state", {}).update(user=user, session=session)
        await self.app(scope, receive, send)

    async def _deny(self, scope, receive, send, status: int) -> None:
        path = scope["path"]
        if path.startswith("/api/"):
            detail = "Not authenticated." if status == 401 else "Administrator access required."
            response: Response = JSONResponse({"detail": detail}, status_code=status)
        elif status == 403:
            # Already signed in — redirecting to /login would look like a broken loop.
            response = HTMLResponse(_FORBIDDEN_PAGE, status_code=403)
        else:
            query = scope.get("query_string", b"").decode()
            target = path + (f"?{query}" if query else "")
            response = RedirectResponse(
                f"/login?next={quote(target, safe='')}",
                status_code=303 if scope["method"] != "GET" else 302,
            )
        await response(scope, receive, send)


_FORBIDDEN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Forbidden</title>
<script src="https://cdn.tailwindcss.com"></script></head>
<body class="min-h-screen bg-slate-950 text-slate-200 flex items-center justify-center">
  <div class="bg-slate-900 border border-slate-800 rounded-xl p-8 max-w-sm text-center">
    <h1 class="text-xl font-bold text-white mb-2">Administrator access required</h1>
    <p class="text-sm text-slate-400 mb-5">Your account does not have permission to view this page.</p>
    <a href="/" class="inline-block bg-indigo-600 hover:bg-indigo-500 text-white text-sm rounded-lg px-5 py-2.5">Back to the app</a>
  </div>
</body></html>"""
