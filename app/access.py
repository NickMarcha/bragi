"""Who may reach what: the access middleware, the login page, and the
Security dialog's endpoints. The rules themselves live in auth.py.

Every path falls into one of four groups (see AccessMiddleware._group):

- public: static files, the login page, and the API explainer.
- peer: the endpoints the Android app and the desktop tray app use to
  register and hold their presence sockets. Neither app has a way to log
  in, so these stay open, as they always were. Tailscale is the boundary
  for them.
- api: /api/v1/*. Always needs an API key, password or not, so that a
  connection set up in the Security dialog is the only way in.
- web: everything else, including the dashboard's /ws. Open while no
  password is set; otherwise needs a session cookie. /ws also takes an
  API key, so an app can use the same live socket the dashboard does.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.datastructures import Headers, QueryParams
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import auth

_PUBLIC_PREFIXES = ("/static/",)
_PUBLIC_PATHS = {"/login", "/auth/login", "/api/v1/help", "/favicon.ico"}
_PEER_PATTERNS = (
    re.compile(r"^/ws/peer/[^/]+$"),
    re.compile(r"^/api/peers/register$"),
    re.compile(r"^/api/peers/register-desktop$"),
    re.compile(r"^/api/peers/[^/]+/streams$"),
)


def _group(path: str) -> str:
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        return "public"
    if any(p.match(path) for p in _PEER_PATTERNS):
        return "peer"
    if path == "/api/v1" or path.startswith("/api/v1/"):
        return "api"
    return "web"


def api_key_from(headers: Headers, query: QueryParams) -> str | None:
    """Authorization: Bearer <key>, X-API-Key: <key>, or ?token=<key>.
    The query form exists because browsers cannot set headers on a
    WebSocket handshake."""
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return headers.get("x-api-key") or query.get("token")


def _cookies(headers: Headers) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            cookies[name] = value
    return cookies


def _is_https(scope: Scope, headers: Headers) -> bool:
    return scope.get("scheme") in ("https", "wss") or headers.get("x-forwarded-proto") == "https"


def session_cookies(access: str, refresh: str | None, secure: bool) -> list[str]:
    flags = "; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
    cookies = [f"{auth.ACCESS_COOKIE}={access}; Max-Age={auth.ACCESS_TTL}{flags}"]
    if refresh is not None:
        cookies.append(f"{auth.REFRESH_COOKIE}={refresh}; Max-Age={auth.REFRESH_IDLE_TTL}{flags}")
    return cookies


def cleared_cookies(secure: bool) -> list[str]:
    flags = "; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
    return [f"{name}=; Max-Age=0{flags}" for name in (auth.ACCESS_COOKIE, auth.REFRESH_COOKIE)]


def _same_origin(headers: Headers) -> bool:
    """A cookie-authenticated request that changes something must come
    from this site. SameSite=Lax already keeps cookies off cross-site
    POSTs in current browsers; this covers the rest."""
    origin = headers.get("origin")
    if not origin:
        return True
    return urlsplit(origin).netloc == headers.get("host")


class AccessMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        group = _group(scope["path"])
        if group in ("public", "peer"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        query = QueryParams(scope.get("query_string", b""))
        websocket = scope["type"] == "websocket"
        state = scope.setdefault("state", {})

        if group == "api" or (websocket and scope["path"] == "/ws"):
            token = api_key_from(headers, query)
            if token is not None or group == "api":
                key = auth.check_api_key(token)
                if key is None:
                    await self._deny_api(scope, receive, send, websocket)
                    return
                state["api_key"] = key
                await self.app(scope, receive, send)
                return

        if not auth.password_set():
            await self.app(scope, receive, send)
            return

        cookies = _cookies(headers)
        session_id = auth.check_access(cookies.get(auth.ACCESS_COOKIE))
        set_cookies: list[str] = []
        if session_id is None:
            refresh = cookies.get(auth.REFRESH_COOKIE)
            if websocket:
                session_id = auth.peek_refresh(refresh)
            elif (result := auth.refresh_session(refresh)) is not None:
                session_id, access, new_refresh = result
                set_cookies = session_cookies(access, new_refresh, _is_https(scope, headers))
        if session_id is None:
            await self._deny_web(scope, receive, send, websocket, headers)
            return
        if not websocket and scope["method"] not in ("GET", "HEAD", "OPTIONS") and not _same_origin(headers):
            await JSONResponse({"detail": "Cross-site request refused"}, 403)(scope, receive, send)
            return
        state["session"] = session_id

        if not set_cookies:
            await self.app(scope, receive, send)
            return

        async def send_with_cookies(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [
                    (b"set-cookie", c.encode()) for c in set_cookies
                ]
            await send(message)

        await self.app(scope, receive, send_with_cookies)

    @staticmethod
    async def _deny_api(scope: Scope, receive: Receive, send: Send, websocket: bool) -> None:
        if websocket:
            await send({"type": "websocket.close", "code": 1008, "reason": "Invalid API key"})
            return
        await JSONResponse(
            {"detail": "Missing or invalid API key. See /api/v1/help."}, 401,
            headers={"WWW-Authenticate": "Bearer"},
        )(scope, receive, send)

    @staticmethod
    async def _deny_web(scope: Scope, receive: Receive, send: Send, websocket: bool, headers: Headers) -> None:
        if websocket:
            await send({"type": "websocket.close", "code": 1008, "reason": "Log in first"})
            return
        login = "/login"
        target = scope["path"] + (("?" + scope["query_string"].decode()) if scope.get("query_string") else "")
        if target != "/":
            login += "?next=" + quote(target, safe="")
        if headers.get("hx-request"):
            response: Response = Response(status_code=401, headers={"HX-Redirect": login})
        elif scope["method"] == "GET" and "text/html" in headers.get("accept", ""):
            response = RedirectResponse(login, 303)
        else:
            response = JSONResponse({"detail": "Log in first"}, 401)
        await response(scope, receive, send)


# --- routes ---

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _safe_next(target: str | None) -> str:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def _secure(request: Request) -> bool:
    return _is_https(request.scope, request.headers)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if not auth.password_set():
        return RedirectResponse(_safe_next(next), 303)
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": None})


@router.post("/auth/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    target = _safe_next(next)
    if not auth.password_set():
        return RedirectResponse(target, 303)
    if not auth.check_password(password):
        return templates.TemplateResponse(
            request, "login.html", {"next": target, "error": "Wrong password"}, status_code=401
        )
    access, refresh = auth.start_session(request.headers.get("user-agent", ""))
    response = RedirectResponse(target, 303)
    for cookie in session_cookies(access, refresh, _secure(request)):
        response.headers.append("set-cookie", cookie)
    return response


@router.post("/auth/logout")
def logout(request: Request):
    auth.end_session(getattr(request.state, "session", None))
    response = RedirectResponse("/login" if auth.password_set() else "/", 303)
    for cookie in cleared_cookies(_secure(request)):
        response.headers.append("set-cookie", cookie)
    return response


@router.get("/auth/session")
def session_status(request: Request):
    """ws.js calls this before every (re)connect: reaching it at all
    refreshes an expired access cookie, which the WebSocket handshake
    itself cannot do."""
    return {"password_set": auth.password_set(), "authenticated": True}


@router.get("/auth/settings")
def security_settings(request: Request):
    return {
        "password_set": auth.password_set(),
        "sessions": auth.session_count(),
        "api_keys": auth.list_api_keys(),
    }


class PasswordChange(BaseModel):
    new_password: str = Field(max_length=200)
    current_password: str | None = Field(default=None, max_length=200)


class PasswordRemoval(BaseModel):
    current_password: str = Field(default="", max_length=200)


class NewApiKey(BaseModel):
    name: str = Field(max_length=60)


@router.post("/auth/password")
def change_password(request: Request, body: PasswordChange):
    try:
        auth.set_password(body.new_password, body.current_password)
    except auth.PasswordError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Every session just ended, this browser's included; give it a new one.
    access, refresh = auth.start_session(request.headers.get("user-agent", ""))
    response = JSONResponse({"password_set": True})
    for cookie in session_cookies(access, refresh, _secure(request)):
        response.headers.append("set-cookie", cookie)
    return response


@router.delete("/auth/password")
def delete_password(request: Request, body: PasswordRemoval):
    try:
        auth.remove_password(body.current_password)
    except auth.PasswordError as exc:
        raise HTTPException(400, str(exc)) from exc
    response = JSONResponse({"password_set": False})
    for cookie in cleared_cookies(_secure(request)):
        response.headers.append("set-cookie", cookie)
    return response


@router.post("/auth/sessions/revoke-others")
def revoke_other_sessions(request: Request):
    ended = auth.end_other_sessions(getattr(request.state, "session", None))
    return {"ended": ended}


@router.post("/auth/api-keys")
def create_api_key(body: NewApiKey):
    try:
        record, token = auth.create_api_key(body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**record, "key": token}


@router.delete("/auth/api-keys/{key_id}")
def delete_api_key(key_id: str):
    if not auth.revoke_api_key(key_id):
        raise HTTPException(404, "No such API key")
    return {"revoked": key_id}

