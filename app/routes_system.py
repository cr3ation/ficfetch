"""Admin pages: account management and OIDC settings.

Every mutation is Post/Redirect/Get and carries the session's CSRF token.
Flash state travels as short codes in the query string, never as free text
echoed back into the page.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

import ipaddress
from urllib.parse import urlparse

from . import auth, db, grimmory, oidc
from .config import Settings

router = APIRouter(prefix="/system")

MESSAGES = {
    "created": "Account created.",
    "password_set": "Password updated. That account was signed out everywhere.",
    "role_set": "Role updated.",
    "deleted": "Account deleted.",
    "settings_saved": "Settings saved.",
    "library_saved": "Library integration saved. It applies to the next download.",
}
ERRORS = {
    "csrf": "That form expired. Please try again.",
    "exists": "An account with that name already exists.",
    "last_admin": "This is the last administrator — it cannot be deleted or demoted.",
    "not_found": "That account no longer exists.",
    "bad_username": "Usernames must be 1–64 characters and cannot be blank.",
    "bad_password": "Password must be at least 8 characters and at most 72 bytes.",
    "bad_role": "Unknown role.",
    "oidc_incomplete": "Enabling SSO needs both a client ID and an issuer URL.",
    "bad_genres": "Unknown genre option.",
}


def _back(page: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    query = f"?ok={ok}" if ok else (f"?err={err}" if err else "")
    return RedirectResponse(f"/system/{page}{query}", status_code=303)


def _guard_csrf(request: Request, csrf_token: str) -> bool:
    return auth.check_csrf(request.state.session, csrf_token)


def render(request: Request, template: str, active: str, context: dict) -> Response:
    response = request.app.state.templates.TemplateResponse(
        request,
        template,
        {
            "active": active,
            "current_user": request.state.user,
            "csrf_token": request.state.session.csrf_token,
            "ok": MESSAGES.get(request.query_params.get("ok", "")),
            "err": ERRORS.get(request.query_params.get("err", "")),
            **context,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("")
async def system_root() -> Response:
    return RedirectResponse("/system/accounts", status_code=302)


@router.get("/accounts")
async def accounts_page(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    users = db.list_users(settings.db_path)
    return render(
        request,
        "system_accounts.html",
        "accounts",
        {
            "users": users,
            "only_one_admin": sum(1 for u in users if u.is_admin) <= 1,
        },
    )


@router.post("/accounts")
async def create_account(
    request: Request,
    csrf_token: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form("user"),
) -> Response:
    settings: Settings = request.app.state.settings
    if not _guard_csrf(request, csrf_token):
        return _back("accounts", err="csrf")

    username = username.strip()
    if not username or len(username) > 64:
        return _back("accounts", err="bad_username")
    if role not in ("user", "admin"):
        return _back("accounts", err="bad_role")
    if auth.password_problem(password):
        return _back("accounts", err="bad_password")
    if db.get_user_by_username(settings.db_path, username):
        return _back("accounts", err="exists")

    db.create_user(
        settings.db_path,
        username,
        now=datetime.now(timezone.utc).isoformat(),
        password_hash=auth.hash_password(password),
        role=role,
    )
    return _back("accounts", ok="created")


@router.post("/accounts/{user_id}/password")
async def set_account_password(
    request: Request, user_id: int, csrf_token: str = Form(""), password: str = Form("")
) -> Response:
    settings: Settings = request.app.state.settings
    if not _guard_csrf(request, csrf_token):
        return _back("accounts", err="csrf")
    if auth.password_problem(password):
        return _back("accounts", err="bad_password")

    user = db.get_user(settings.db_path, user_id)
    if user is None:
        return _back("accounts", err="not_found")

    db.set_password_hash(settings.db_path, user.id, auth.hash_password(password))
    # A password change should end any session that predates it.
    db.delete_sessions_for_user(settings.db_path, user.id)
    return _back("accounts", ok="password_set")


@router.post("/accounts/{user_id}/role")
async def set_account_role(
    request: Request, user_id: int, csrf_token: str = Form(""), role: str = Form("user")
) -> Response:
    settings: Settings = request.app.state.settings
    if not _guard_csrf(request, csrf_token):
        return _back("accounts", err="csrf")
    if role not in ("user", "admin"):
        return _back("accounts", err="bad_role")

    user = db.get_user(settings.db_path, user_id)
    if user is None:
        return _back("accounts", err="not_found")
    if role == "user" and db.count_admins(settings.db_path, excluding_id=user.id) == 0:
        return _back("accounts", err="last_admin")

    db.set_role(settings.db_path, user.id, role)
    # Revoke, so a demoted admin loses access now rather than when the cookie expires.
    db.delete_sessions_for_user(settings.db_path, user.id)
    return _back("accounts", ok="role_set")


@router.post("/accounts/{user_id}/delete")
async def delete_account(request: Request, user_id: int, csrf_token: str = Form("")) -> Response:
    settings: Settings = request.app.state.settings
    if not _guard_csrf(request, csrf_token):
        return _back("accounts", err="csrf")

    user = db.get_user(settings.db_path, user_id)
    if user is None:
        return _back("accounts", err="not_found")
    if user.is_admin and db.count_admins(settings.db_path, excluding_id=user.id) == 0:
        return _back("accounts", err="last_admin")

    db.delete_user(settings.db_path, user.id)  # sessions cascade
    if user.id == request.state.user.id:
        return RedirectResponse("/login?ok=logged_out", status_code=303)
    return _back("accounts", ok="deleted")


def _scheme_looks_wrong(uri: str) -> bool:
    """Warn only when http:// is used to reach a host that isn't clearly local."""
    parsed = urlparse(uri)
    if parsed.scheme != "http":
        return False
    host = parsed.hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return False
    try:
        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return True  # a real hostname over http is worth flagging


@router.get("/settings")
async def settings_page(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    cfg = oidc.load_oidc_config(settings.db_path)
    redirect_uri = oidc.redirect_uri(request, settings.public_base_url)
    return render(
        request,
        "system_settings.html",
        "settings",
        {
            "cfg": cfg,
            "has_secret": bool(cfg.client_secret),  # the value itself never reaches the template
            "redirect_uri": redirect_uri,
            "scheme_warning": _scheme_looks_wrong(redirect_uri),
        },
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    csrf_token: str = Form(""),
    enabled: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    clear_secret: str = Form(""),
    issuer: str = Form(""),
    scopes: str = Form(""),
) -> Response:
    settings: Settings = request.app.state.settings
    if not _guard_csrf(request, csrf_token):
        return _back("settings", err="csrf")

    enabled_bool = enabled == "true"
    issuer = issuer.strip().rstrip("/")
    client_id = client_id.strip()
    if enabled_bool and (not client_id or not issuer):
        return _back("settings", err="oidc_incomplete")

    values = {
        "oidc.enabled": "true" if enabled_bool else "false",
        "oidc.client_id": client_id,
        "oidc.issuer": issuer,
        "oidc.scopes": scopes.strip() or oidc.DEFAULT_SCOPES,
    }
    # Secret: blank submission leaves the stored value untouched; the checkbox
    # is the only way to erase it. So the secret is never round-tripped through
    # the browser.
    if clear_secret == "true":
        values["oidc.client_secret"] = ""
    elif client_secret:
        values["oidc.client_secret"] = client_secret

    db.set_settings(settings.db_path, values, datetime.now(timezone.utc).isoformat())
    oidc.invalidate_discovery_cache()
    return _back("settings", ok="settings_saved")


@router.get("/library")
async def library_page(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    return render(
        request,
        "system_library.html",
        "library",
        {
            "cfg": grimmory.load_grimmory_config(settings.db_path),
            "epub_tag": settings.epub_tag,
        },
    )


@router.post("/library")
async def save_library(
    request: Request,
    csrf_token: str = Form(""),
    enabled: str = Form(""),
    genres: str = Form(grimmory.DEFAULT_GENRE_MODE),
    trim_subjects: str = Form(""),
    map_rating: str = Form(""),
) -> Response:
    settings: Settings = request.app.state.settings
    if not _guard_csrf(request, csrf_token):
        return _back("library", err="csrf")

    now = datetime.now(timezone.utc).isoformat()
    # Turning the integration off keeps the sub-options as they were, so they
    # come back intact when it is turned on again — the form disables them, and
    # a disabled field submits nothing.
    if enabled != "true":
        db.set_settings(settings.db_path, {"grimmory.enabled": "false"}, now)
        return _back("library", ok="library_saved")

    if genres not in grimmory.GENRE_MODES:
        return _back("library", err="bad_genres")

    db.set_settings(
        settings.db_path,
        {
            "grimmory.enabled": "true",
            "grimmory.genres": genres,
            "grimmory.trim_subjects": "true" if trim_subjects == "true" else "false",
            "grimmory.map_rating": "true" if map_rating == "true" else "false",
        },
        now,
    )
    return _back("library", ok="library_saved")
