"""User preferences — reachable by every signed-in account (not just admins).

Deliberately outside the /system prefix: the AuthMiddleware gates that prefix to
admins, whereas each user must set their own cover style. Same Post/Redirect/Get
+ CSRF + flash-code shape as routes_system.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from . import auth, covers, db
from .config import Settings

router = APIRouter()

MESSAGES = {"cover_saved": "Cover style saved."}
ERRORS = {
    "csrf": "That form expired. Please try again.",
    "bad_style": "Unknown cover style.",
}


def _back(*, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    query = f"?ok={ok}" if ok else (f"?err={err}" if err else "")
    return RedirectResponse(f"/preferences{query}", status_code=303)


@router.get("/preferences")
async def preferences_page(request: Request) -> Response:
    user = request.state.user
    response = request.app.state.templates.TemplateResponse(
        request,
        "preferences.html",
        {
            "current_user": user,
            "csrf_token": request.state.session.csrf_token,
            "cover_style": user.cover_style,
            "styles": covers.ALLOWED_STYLES,
            "ok": MESSAGES.get(request.query_params.get("ok", "")),
            "err": ERRORS.get(request.query_params.get("err", "")),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/preferences")
async def save_preferences(
    request: Request, csrf_token: str = Form(""), cover_style: str = Form("")
) -> Response:
    settings: Settings = request.app.state.settings
    if not auth.check_csrf(request.state.session, csrf_token):
        return _back(err="csrf")
    if cover_style not in covers.STYLE_CHOICES:
        return _back(err="bad_style")
    db.set_cover_style(settings.db_path, request.state.user.id, cover_style)
    return _back(ok="cover_saved")
