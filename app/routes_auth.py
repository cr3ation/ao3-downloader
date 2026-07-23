"""Login, logout and the OIDC entry points."""
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from authlib.common.security import generate_token
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import auth, db, oidc
from .config import Settings
from .oidc import OidcError, load_oidc_config

router = APIRouter()


def _pkce_pair() -> tuple[str, str]:
    verifier = generate_token(64)
    challenge = oidc.create_s256_challenge(verifier)
    return verifier, challenge

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


def _oidc_error(request: Request, message: str) -> Response:
    response = request.app.state.templates.TemplateResponse(
        request, "oidc_error.html", {"message": message}, status_code=400
    )
    return _no_store(response)


@router.get("/auth/oidc/login")
async def oidc_login(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    cfg = load_oidc_config(settings.db_path)
    if not cfg.usable:
        return _oidc_error(request, "SSO is not enabled. Ask an administrator to configure it.")

    now = datetime.now(timezone.utc)
    db.purge_oidc_states(settings.db_path, (now - timedelta(seconds=settings.oidc_state_ttl)).isoformat())

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            meta = await oidc.discover(cfg.issuer, client)
    except OidcError as exc:
        return _oidc_error(request, str(exc))

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    redirect = oidc.redirect_uri(request, settings.public_base_url)

    db.create_oidc_state(
        settings.db_path, state=state, nonce=nonce, code_verifier=verifier,
        redirect_uri=redirect, next_path=auth.safe_next(request.query_params.get("next")),
        created_at=now.isoformat(),
    )

    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": redirect,
        "scope": cfg.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{meta['authorization_endpoint']}?{httpx.QueryParams(params)}"
    return RedirectResponse(authorize_url, status_code=302)


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    params = request.query_params

    if params.get("error"):
        return _oidc_error(request, params.get("error_description") or params["error"])
    if not params.get("code") or not params.get("state"):
        return _oidc_error(request, "The provider's response was missing required parameters.")

    row = db.consume_oidc_state(settings.db_path, params["state"])  # single-use
    if row is None:
        return _oidc_error(request, "This login attempt expired or was already used. Please try again.")
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row.created_at)
    if age.total_seconds() > settings.oidc_state_ttl:
        return _oidc_error(request, "This login attempt expired. Please try again.")

    cfg = load_oidc_config(settings.db_path)  # re-read: settings may have changed mid-flow
    if not cfg.usable:
        return _oidc_error(request, "SSO was disabled during login.")

    try:
        # A dedicated client, not the AO3 one — that carries AO3 cookies and headers.
        async with httpx.AsyncClient(timeout=15) as client:
            meta = await oidc.discover(cfg.issuer, client)
            # redirect_uri is replayed from the stored row, not recomputed — the IdP
            # requires an exact match with the authorize request.
            tokens = await oidc.exchange_code(cfg, meta, params["code"], row.redirect_uri, row.code_verifier, client)
            claims = await oidc.validate_id_token(tokens["id_token"], cfg, meta, row.nonce, client)
    except OidcError as exc:
        return _oidc_error(request, str(exc))

    user = _provision_user(settings.db_path, claims)
    db.touch_login(settings.db_path, user.id, datetime.now(timezone.utc).isoformat())
    response = RedirectResponse(auth.safe_next(row.next_path), status_code=303)
    auth.issue_session(settings.db_path, user, response, settings, request)
    return _no_store(response)


def _provision_user(db_path, claims: dict):
    """Resolve the local account for an OIDC login, creating it just-in-time."""
    subject = str(claims["sub"])
    user = db.get_user_by_subject(db_path, subject)
    if user:
        return user

    now = datetime.now(timezone.utc).isoformat()
    username = oidc.claim_username(claims)
    existing = db.get_user_by_username(db_path, username)
    if existing:
        # Adopt a pre-existing local account of the same name; role and password
        # are left untouched. This is the one real trust assumption (documented).
        db.link_subject(db_path, existing.id, subject)
        return db.get_user(db_path, existing.id)

    # New SSO users are always plain users; admin is granted manually.
    user_id = db.create_user(
        db_path, username, now=now, password_hash=None, role="user",
        provider="oidc", subject=subject,
    )
    return db.get_user(db_path, user_id)
