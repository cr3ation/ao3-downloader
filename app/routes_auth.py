"""Login, logout and the OIDC entry points."""
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import auth, db
from .config import Settings
from .oidc import load_oidc_config

router = APIRouter()

NOTICES = {
    "logged_out": "You have been signed out.",
    "session_expired": "Your session expired. Please sign in again.",
}


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _render_login(request: Request, *, status: int = 200, error: str | None = None) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    templates = request.app.state.templates
    next_path = auth.safe_next(request.query_params.get("next"))
    notice = NOTICES.get(request.query_params.get("ok", ""))
    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": next_path,
            "error": error,
            "notice": notice,
            # Read per request, so toggling SSO in the GUI takes effect with no restart.
            "oidc_enabled": load_oidc_config(settings.db_path).usable,
        },
        status_code=status,
    )
    return _no_store(response)


@router.get("/login")
async def login_page(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    if auth.resolve_session(settings.db_path, request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse(auth.safe_next(request.query_params.get("next")), status_code=302)
    return _render_login(request)


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
) -> Response:
    settings: Settings = request.app.state.settings
    client_ip = request.client.host if request.client else "unknown"
    username = username.strip()

    if not auth.login_allowed(settings, username, client_ip):
        return _render_login(request, status=429, error="Too many attempts. Try again later.")

    user = db.get_user_by_username(settings.db_path, username)
    if user is None or not auth.verify_password(password, user.password_hash):
        auth.record_login_failure(settings, username, client_ip)
        return _render_login(request, status=401, error="Incorrect username or password.")

    auth.clear_login_failures(username, client_ip)
    db.touch_login(settings.db_path, user.id, datetime.now(timezone.utc).isoformat())

    response = RedirectResponse(auth.safe_next(next), status_code=303)
    auth.issue_session(settings.db_path, user, response, settings, request)
    return _no_store(response)


@router.post("/logout")
async def logout(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    auth.revoke_session(settings.db_path, request.cookies.get(auth.SESSION_COOKIE))
    response = RedirectResponse("/login?ok=logged_out", status_code=303)
    auth.clear_session_cookie(response, settings, request)
    return _no_store(response)
