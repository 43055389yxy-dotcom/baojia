import base64
import hashlib
import hmac
import html
import json
import math
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from contextlib import asynccontextmanager, contextmanager
from threading import Lock
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)


def positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def boolean_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


PUBLIC_ORIGIN = os.environ["PUBLIC_ORIGIN"].rstrip("/")
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
if not MCP_PATH.startswith("/") or MCP_PATH.endswith("/"):
    raise RuntimeError("MCP_PATH must start with / and must not end with /")
RESOURCE = f"{PUBLIC_ORIGIN}{MCP_PATH}"
UPSTREAM = os.environ.get("UPSTREAM_MCP", "http://127.0.0.1:8200/v2/mcp")
_upstream_parts = urlparse(UPSTREAM)
UPSTREAM_READY_URL = os.environ.get(
    "UPSTREAM_READY_URL",
    _upstream_parts._replace(path="/readyz", params="", query="", fragment="").geturl(),
)
PASSWORDLESS_AUTH = boolean_env("PASSWORDLESS_AUTH", False)
if PASSWORDLESS_AUTH:
    LOGIN_USERNAME = ""
    PASSWORD_SALT = b""
    PASSWORD_DIGEST = b""
else:
    LOGIN_USERNAME = os.environ["LOGIN_USERNAME"]
    PASSWORD_SALT = bytes.fromhex(os.environ["PASSWORD_SALT"])
    PASSWORD_DIGEST = bytes.fromhex(os.environ["PASSWORD_DIGEST"])
DB_PATH = os.environ.get("DB_PATH", "/data/oauth.db")
ACCESS_TOKEN_TTL = positive_int_env("ACCESS_TOKEN_TTL_SECONDS", 3600)
REFRESH_TOKEN_TTL = positive_int_env("REFRESH_TOKEN_TTL_SECONDS", 30 * 24 * 3600)
CODE_TTL = positive_int_env("AUTHORIZATION_CODE_TTL_SECONDS", 300)
READY_TIMEOUT_SECONDS = positive_int_env("READY_TIMEOUT_SECONDS", 3)
DEFAULT_SCOPE = "pricing:read pricing:write offline_access"
ALLOWED_SCOPES = set(DEFAULT_SCOPE.split())
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024

RATE_WINDOW_SECONDS = positive_int_env("OAUTH_RATE_WINDOW_SECONDS", 600)
RATE_LIMITS = {
    "register": (
        positive_int_env("OAUTH_REGISTER_GLOBAL_LIMIT", 120),
        positive_int_env("OAUTH_REGISTER_IP_LIMIT", 20),
    ),
    "authorize": (
        positive_int_env("OAUTH_AUTHORIZE_GLOBAL_LIMIT", 300),
        positive_int_env("OAUTH_AUTHORIZE_IP_LIMIT", 60),
    ),
    "login": (
        positive_int_env("OAUTH_LOGIN_GLOBAL_LIMIT", 120),
        positive_int_env("OAUTH_LOGIN_IP_LIMIT", 12),
    ),
    "token": (
        positive_int_env("OAUTH_TOKEN_GLOBAL_LIMIT", 600),
        positive_int_env("OAUTH_TOKEN_IP_LIMIT", 120),
    ),
}

READ_ONLY_TOOLS = {
    "describe_service",
    "get_attribute_values",
    "get_prices",
}
WRITE_TOOLS = {
    "build_estimate",
}


def validate_runtime_urls() -> None:
    public = urlparse(PUBLIC_ORIGIN)
    if public.scheme != "https" or not public.hostname or public.path not in {"", "/"}:
        raise RuntimeError("PUBLIC_ORIGIN must be an HTTPS origin without a path")
    upstream = urlparse(UPSTREAM)
    if upstream.scheme not in {"http", "https"} or not upstream.hostname:
        raise RuntimeError("UPSTREAM_MCP must be an HTTP(S) URL")
    upstream_ready = urlparse(UPSTREAM_READY_URL)
    if upstream_ready.scheme not in {"http", "https"} or not upstream_ready.hostname:
        raise RuntimeError("UPSTREAM_READY_URL must be an HTTP(S) URL")


validate_runtime_urls()


def request_body_is_limited(scope: dict) -> bool:
    """Limit every body consumed by the public MCP and OAuth endpoints."""

    path = scope.get("path", "")
    if path == MCP_PATH:
        return True
    return path.startswith("/oauth/") and scope.get("method") not in {
        "GET",
        "HEAD",
        "OPTIONS",
    }


class RequestBodyLimitMiddleware:
    """Reject oversized bodies before FastAPI or the upstream proxy buffers them."""

    def __init__(self, app, max_body_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def reject(self, scope, receive, send) -> None:
        response = JSONResponse(
            {
                "error": "request_too_large",
                "error_description": "Request body exceeds the 2 MiB limit",
            },
            status_code=413,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not request_body_is_limited(scope):
            await self.app(scope, receive, send)
            return

        for name, raw_value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared_length = int(raw_value)
            except ValueError:
                continue
            if declared_length > self.max_body_bytes:
                await self.reject(scope, receive, send)
                return

        buffered_messages = deque()
        received_bytes = 0
        while True:
            message = await receive()
            buffered_messages.append(message)
            if message["type"] != "http.request":
                break
            received_bytes += len(message.get("body", b""))
            if received_bytes > self.max_body_bytes:
                await self.reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay_receive():
            if buffered_messages:
                return buffered_messages.popleft()
            return await receive()

        await self.app(scope, replay_receive, send)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(
    RequestBodyLimitMiddleware, max_body_bytes=MAX_REQUEST_BODY_BYTES
)


class SlidingWindowLimiter:
    """Small, process-wide limiter for the single-worker OAuth gateway."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def consume(self, key: str, limit: int, window_seconds: int) -> int | None:
        current = time.monotonic()
        cutoff = current - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return max(1, math.ceil(events[0] + window_seconds - current))
            events.append(current)

            # The global bucket bounds accepted traffic, so periodic pruning also
            # prevents a stream of one-off source addresses from growing this map.
            if len(self._events) > 2_000:
                stale = [
                    bucket
                    for bucket, values in self._events.items()
                    if not values or values[-1] <= cutoff
                ]
                for bucket in stale:
                    self._events.pop(bucket, None)
        return None


RATE_LIMITER = SlidingWindowLimiter()


def now() -> int:
    return int(time.time())


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_matches(value: str) -> bool:
    candidate = hashlib.scrypt(
        value.encode(), salt=PASSWORD_SALT, n=16384, r=8, p=1, dklen=32
    )
    return hmac.compare_digest(candidate, PASSWORD_DIGEST)


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def cleanup_expired(connection: sqlite3.Connection) -> None:
    current = now()
    connection.execute(
        "DELETE FROM auth_requests WHERE created_at < ?", (current - 86400,)
    )
    connection.execute("DELETE FROM auth_codes WHERE expires_at < ?", (current,))
    connection.execute("DELETE FROM auth_responses WHERE expires_at < ?", (current,))
    connection.execute("DELETE FROM tokens WHERE refresh_expires_at < ?", (current,))


def init_db() -> None:
    database_directory = os.path.dirname(DB_PATH)
    if database_directory:
        os.makedirs(database_directory, exist_ok=True)
    with db() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                redirect_uris TEXT NOT NULL,
                token_method TEXT NOT NULL,
                client_secret_hash TEXT,
                client_name TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_requests (
                request_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                state TEXT NOT NULL,
                scope TEXT NOT NULL,
                resource TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS auth_codes (
                code_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                scope TEXT NOT NULL,
                resource TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_responses (
                request_id TEXT PRIMARY KEY,
                redirect_uri TEXT NOT NULL,
                state TEXT NOT NULL,
                code TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                access_hash TEXT PRIMARY KEY,
                refresh_hash TEXT UNIQUE NOT NULL,
                client_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                resource TEXT NOT NULL,
                access_expires_at INTEGER NOT NULL,
                refresh_expires_at INTEGER NOT NULL
            );
            """
        )
        cleanup_expired(connection)


@app.middleware("http")
async def secure_response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/oauth/"):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
    return response


def json_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, operation: str) -> JSONResponse | None:
    global_limit, address_limit = RATE_LIMITS[operation]
    buckets = (
        (f"{operation}:global", global_limit),
        (f"{operation}:address:{client_address(request)}", address_limit),
    )
    for bucket, limit in buckets:
        retry_after = RATE_LIMITER.consume(bucket, limit, RATE_WINDOW_SECONDS)
        if retry_after is not None:
            return JSONResponse(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "Too many requests",
                },
                status_code=429,
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "Retry-After": str(retry_after),
                },
            )
    return None


def scope_string(raw: str | None) -> str | None:
    requested = set((raw or DEFAULT_SCOPE).split())
    if not requested or not requested.issubset(ALLOWED_SCOPES):
        return None
    return " ".join(scope for scope in DEFAULT_SCOPE.split() if scope in requested)


def valid_redirect_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return False
    if parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"}:
        return False
    return (
        parsed.path == "/connector_platform_oauth_redirect"
        or parsed.path.startswith("/connector/oauth/")
    )


def oauth_metadata() -> dict:
    return {
        "issuer": PUBLIC_ORIGIN,
        "authorization_endpoint": f"{PUBLIC_ORIGIN}/oauth/authorize",
        "token_endpoint": f"{PUBLIC_ORIGIN}/oauth/token",
        "registration_endpoint": f"{PUBLIC_ORIGIN}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_basic",
            "client_secret_post",
        ],
        "scopes_supported": list(DEFAULT_SCOPE.split()),
        "service_documentation": f"{PUBLIC_ORIGIN}/",
    }


def protected_resource_metadata() -> dict:
    return {
        "resource": RESOURCE,
        "authorization_servers": [PUBLIC_ORIGIN],
        "scopes_supported": list(DEFAULT_SCOPE.split()),
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{PUBLIC_ORIGIN}/",
    }


@app.get("/")
def home() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
        <meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>AstraQuote MCP</title><style>
        body{font-family:system-ui,sans-serif;max-width:720px;margin:10vh auto;padding:24px;color:#17343a}
        main{border:1px solid #d9e7e4;border-radius:18px;padding:32px;box-shadow:0 12px 40px #17343a12}
        h1{margin-top:0}code{background:#eef7f5;padding:3px 7px;border-radius:6px}</style>
        <main><h1>AstraQuote MCP</h1><p>服务运行正常。</p>
        <p>请从 ChatGPT 开发者模式连接 <code>__MCP_PATH__</code>，首次连接会进入授权确认页面。</p></main></html>""".replace(
            "__MCP_PATH__", html.escape(MCP_PATH)
        ),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})


def database_ready() -> bool:
    try:
        connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", timeout=2, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tokens'"
            ).fetchone()
            return row is not None
        finally:
            connection.close()
    except sqlite3.Error:
        return False


async def upstream_ready() -> bool:
    try:
        async with httpx.AsyncClient(timeout=READY_TIMEOUT_SECONDS) as client:
            response = await client.get(UPSTREAM_READY_URL)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


@app.get("/readyz")
async def readyz() -> JSONResponse:
    if not database_ready() or not await upstream_ready():
        return JSONResponse(
            {"status": "not_ready"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse({"status": "ready"}, headers={"Cache-Control": "no-store"})


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
@app.get("/.well-known/oauth-protected-resource/v2/mcp")
def resource_metadata() -> JSONResponse:
    return JSONResponse(protected_resource_metadata())


@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/openid-configuration")
def authorization_metadata() -> JSONResponse:
    return JSONResponse(oauth_metadata())


def registered_client(client_id: str):
    with db() as connection:
        return connection.execute(
            "SELECT * FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()


@app.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    limited = rate_limit(request, "register")
    if limited:
        return limited
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return json_error("invalid_client_metadata", "JSON body is required")
    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return json_error("invalid_redirect_uri", "redirect_uris is required")
    if not all(
        isinstance(uri, str) and valid_redirect_uri(uri) for uri in redirect_uris
    ):
        return json_error(
            "invalid_redirect_uri", "Only ChatGPT HTTPS callbacks are allowed"
        )
    method = body.get("token_endpoint_auth_method", "none")
    if method not in {"none", "client_secret_basic", "client_secret_post"}:
        return json_error(
            "invalid_client_metadata", "Unsupported token authentication method"
        )
    client_id = f"chatgpt_{secrets.token_urlsafe(24)}"
    client_secret = secrets.token_urlsafe(48) if method != "none" else None
    client_name = str(body.get("client_name", "ChatGPT"))[:200]
    with db() as connection:
        cleanup_expired(connection)
        connection.execute(
            "INSERT INTO clients VALUES (?, ?, ?, ?, ?, ?)",
            (
                client_id,
                json.dumps(redirect_uris),
                method,
                token_hash(client_secret) if client_secret else None,
                client_name,
                now(),
            ),
        )
    response = {
        "client_id": client_id,
        "client_id_issued_at": now(),
        "redirect_uris": redirect_uris,
        "client_name": client_name,
        "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if client_secret:
        response["client_secret"] = client_secret
        response["client_secret_expires_at"] = 0
    return JSONResponse(
        response, status_code=201, headers={"Cache-Control": "no-store"}
    )


def authorize_error(redirect_uri: str, state: str, error: str, description: str):
    query = urlencode(
        {"error": error, "error_description": description, "state": state}
    )
    return RedirectResponse(f"{redirect_uri}?{query}", status_code=302)


def login_page(request_id: str, error: str = "") -> HTMLResponse:
    error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    if PASSWORDLESS_AUTH:
        introduction = "确认后，ChatGPT 可以调用 AstraQuote 报价工具。"
        credential_fields = ""
        button_label = "确认授权"
        footer = "无需用户名和密码，仅授权 AWS 报价工具。"
    else:
        introduction = "登录后，ChatGPT 可以调用 AstraQuote 报价工具。"
        credential_fields = """
      <label for='username'>用户名</label><input id='username' name='username' required autocomplete='username'>
      <label for='password'>密码</label><input id='password' name='password' type='password' required autocomplete='current-password'>"""
        button_label = "登录并授权"
        footer = "仅授权 AWS 报价工具，不会登录其他网站。"
    page = f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>授权 AstraQuote MCP</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f3f8f6;color:#17343a;font-family:system-ui,sans-serif}}
    main{{width:min(440px,calc(100% - 32px));margin:10vh auto;background:white;border:1px solid #d7e5e2;
    border-radius:20px;padding:32px;box-shadow:0 18px 60px #17343a1a}}h1{{margin:0 0 10px;font-size:26px}}
    p{{color:#587176;line-height:1.6}}label{{display:block;font-weight:650;margin:18px 0 7px}}
    input{{width:100%;padding:13px 14px;border:1px solid #bdd3cf;border-radius:10px;font-size:16px}}
    button{{width:100%;margin-top:24px;padding:14px;border:0;border-radius:10px;background:#087f73;color:white;
    font-size:16px;font-weight:700;cursor:pointer}}.error{{background:#fff0ec;color:#a83d26;padding:10px 12px;border-radius:8px}}
    small{{display:block;margin-top:16px;color:#789095;text-align:center}}</style><main>
    <h1>授权 AstraQuote MCP</h1><p>{introduction}</p>{error_html}
    <form method='post' action='/oauth/authorize' autocomplete='on'>
      <input type='hidden' name='request_id' value='{html.escape(request_id)}'>
      {credential_fields}
      <button type='submit'>{button_label}</button>
    </form><small>{footer}</small></main></html>"""
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com https://www.chatgpt.com; base-uri 'none'; frame-ancestors 'none'",
        },
    )


@app.get("/oauth/authorize")
def authorize_get(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    resource: str,
    scope: str | None = None,
):
    limited = rate_limit(request, "authorize")
    if limited:
        return limited
    client = registered_client(client_id)
    if not client:
        return HTMLResponse("Unknown OAuth client", status_code=400)
    registered_uris = json.loads(client["redirect_uris"])
    if redirect_uri not in registered_uris or not valid_redirect_uri(redirect_uri):
        return HTMLResponse("Invalid redirect URI", status_code=400)
    if response_type != "code":
        return authorize_error(
            redirect_uri, state, "unsupported_response_type", "Use code"
        )
    if code_challenge_method != "S256" or len(code_challenge) < 43:
        return authorize_error(
            redirect_uri, state, "invalid_request", "PKCE S256 is required"
        )
    if resource != RESOURCE:
        return authorize_error(
            redirect_uri, state, "invalid_target", "Invalid resource"
        )
    normalized_scope = scope_string(scope)
    if not normalized_scope:
        return authorize_error(
            redirect_uri, state, "invalid_scope", "Unsupported scope"
        )
    request_id = secrets.token_urlsafe(32)
    with db() as connection:
        cleanup_expired(connection)
        connection.execute(
            "INSERT INTO auth_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                request_id,
                client_id,
                redirect_uri,
                state,
                normalized_scope,
                resource,
                code_challenge,
                now(),
            ),
        )
    return login_page(request_id)


@app.post("/oauth/authorize")
def authorize_post(
    request: Request,
    request_id: str = Form(...),
    username: str | None = Form(None),
    password: str | None = Form(None),
):
    limited = rate_limit(request, "login")
    if limited:
        return limited
    with db() as connection:
        cleanup_expired(connection)
        completed = connection.execute(
            "SELECT * FROM auth_responses WHERE request_id = ?", (request_id,)
        ).fetchone()
        if completed:
            if completed["expires_at"] < now():
                connection.execute(
                    "DELETE FROM auth_responses WHERE request_id = ?", (request_id,)
                )
                return HTMLResponse(
                    "Authorization request expired. Start again in ChatGPT.",
                    status_code=400,
                )
            query = urlencode({"code": completed["code"], "state": completed["state"]})
            return RedirectResponse(
                f"{completed['redirect_uri']}?{query}", status_code=303
            )
        auth_request = connection.execute(
            "SELECT * FROM auth_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if not auth_request or auth_request["created_at"] + CODE_TTL < now():
            connection.execute(
                "DELETE FROM auth_requests WHERE request_id = ?", (request_id,)
            )
            return HTMLResponse(
                "Authorization request expired. Start again in ChatGPT.",
                status_code=400,
            )
        if auth_request["failures"] >= 5:
            return HTMLResponse(
                "Too many attempts. Start again in ChatGPT.", status_code=429
            )
        credentials_valid = PASSWORDLESS_AUTH or (
            isinstance(username, str)
            and isinstance(password, str)
            and hmac.compare_digest(username, LOGIN_USERNAME)
            and password_matches(password)
        )
        if not credentials_valid:
            connection.execute(
                "UPDATE auth_requests SET failures = failures + 1 WHERE request_id = ?",
                (request_id,),
            )
            return login_page(request_id, "用户名或密码不正确")
        code = secrets.token_urlsafe(48)
        connection.execute(
            "INSERT INTO auth_codes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                token_hash(code),
                auth_request["client_id"],
                auth_request["redirect_uri"],
                auth_request["scope"],
                auth_request["resource"],
                auth_request["code_challenge"],
                now() + CODE_TTL,
            ),
        )
        connection.execute(
            "INSERT INTO auth_responses VALUES (?, ?, ?, ?, ?)",
            (
                request_id,
                auth_request["redirect_uri"],
                auth_request["state"],
                code,
                now() + CODE_TTL,
            ),
        )
        connection.execute(
            "DELETE FROM auth_requests WHERE request_id = ?", (request_id,)
        )
    query = urlencode({"code": code, "state": auth_request["state"]})
    return RedirectResponse(f"{auth_request['redirect_uri']}?{query}", status_code=303)


def basic_credentials(request: Request) -> tuple[str | None, str | None]:
    value = request.headers.get("authorization", "")
    if not value.lower().startswith("basic "):
        return None, None
    try:
        decoded = base64.b64decode(value.split(None, 1)[1]).decode()
        parts = decoded.split(":", 1)
        return (parts[0], parts[1]) if len(parts) == 2 else (None, None)
    except (ValueError, UnicodeDecodeError):
        return None, None


def authenticate_client(request: Request, form: dict, client_id: str):
    client = registered_client(client_id)
    if not client:
        return None
    method = client["token_method"]
    if method == "none":
        return client if not client["client_secret_hash"] else None
    supplied = None
    if method == "client_secret_basic":
        basic_id, supplied = basic_credentials(request)
        if basic_id != client_id:
            return None
    elif method == "client_secret_post":
        supplied = form.get("client_secret")
    if not supplied or not client["client_secret_hash"]:
        return None
    return (
        client
        if hmac.compare_digest(token_hash(supplied), client["client_secret_hash"])
        else None
    )


def issue_tokens(connection, client_id: str, scope: str, resource: str) -> dict:
    access_token = secrets.token_urlsafe(48)
    refresh_token = secrets.token_urlsafe(64)
    connection.execute(
        "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            token_hash(access_token),
            token_hash(refresh_token),
            client_id,
            scope,
            resource,
            now() + ACCESS_TOKEN_TTL,
            now() + REFRESH_TOKEN_TTL,
        ),
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "refresh_token": refresh_token,
        "scope": scope,
    }


@app.post("/oauth/token")
async def oauth_token(request: Request):
    limited = rate_limit(request, "token")
    if limited:
        return limited
    form_data = await request.form()
    form = dict(form_data)
    grant_type = form.get("grant_type")
    client_id = form.get("client_id")
    if not client_id:
        basic_id, _ = basic_credentials(request)
        client_id = basic_id
    if not client_id or not authenticate_client(request, form, client_id):
        return json_error("invalid_client", "Client authentication failed", 401)
    if grant_type == "authorization_code":
        code = form.get("code", "")
        verifier = form.get("code_verifier", "")
        redirect_uri = form.get("redirect_uri", "")
        resource = form.get("resource", "")
        if not code or not verifier or resource != RESOURCE:
            return json_error(
                "invalid_grant", "Code, PKCE verifier, and resource are required"
            )
        with db() as connection:
            cleanup_expired(connection)
            row = connection.execute(
                "SELECT * FROM auth_codes WHERE code_hash = ?", (token_hash(code),)
            ).fetchone()
            if not row:
                return json_error("invalid_grant", "Authorization code is invalid")
            connection.execute(
                "DELETE FROM auth_codes WHERE code_hash = ?", (token_hash(code),)
            )
            connection.execute("DELETE FROM auth_responses WHERE code = ?", (code,))
            if (
                row["expires_at"] < now()
                or row["client_id"] != client_id
                or row["redirect_uri"] != redirect_uri
                or row["resource"] != resource
            ):
                return json_error(
                    "invalid_grant", "Authorization code does not match request"
                )
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if not hmac.compare_digest(challenge, row["code_challenge"]):
                return json_error("invalid_grant", "PKCE verification failed")
            response = issue_tokens(connection, client_id, row["scope"], resource)
        return JSONResponse(
            response, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )
    if grant_type == "refresh_token":
        refresh_token = form.get("refresh_token", "")
        resource = form.get("resource", RESOURCE)
        with db() as connection:
            cleanup_expired(connection)
            row = connection.execute(
                "SELECT * FROM tokens WHERE refresh_hash = ?",
                (token_hash(refresh_token),),
            ).fetchone()
            if (
                not row
                or row["refresh_expires_at"] < now()
                or row["client_id"] != client_id
            ):
                return json_error("invalid_grant", "Refresh token is invalid")
            if resource != row["resource"]:
                return json_error("invalid_target", "Invalid resource")
            connection.execute(
                "DELETE FROM tokens WHERE refresh_hash = ?",
                (token_hash(refresh_token),),
            )
            response = issue_tokens(
                connection, client_id, row["scope"], row["resource"]
            )
        return JSONResponse(
            response, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )
    return json_error(
        "unsupported_grant_type", "Use authorization_code or refresh_token"
    )


def bearer_token(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value.split(None, 1)[1]
    return None


def challenge() -> JSONResponse:
    metadata = f"{PUBLIC_ORIGIN}/.well-known/oauth-protected-resource"
    value = f'Bearer resource_metadata="{metadata}", scope="pricing:read pricing:write"'
    return JSONResponse(
        {"error": "unauthorized", "error_description": "OAuth authorization required"},
        status_code=401,
        headers={"WWW-Authenticate": value, "Cache-Control": "no-store"},
    )


def insufficient_scope(required: set[str]) -> JSONResponse:
    scope = " ".join(sorted(required))
    value = f'Bearer error="insufficient_scope", scope="{scope}"'
    return JSONResponse(
        {
            "error": "insufficient_scope",
            "error_description": "Token scope is insufficient",
        },
        status_code=403,
        headers={"WWW-Authenticate": value, "Cache-Control": "no-store"},
    )


def access_scopes(value: str | None) -> set[str] | None:
    if not value:
        return None
    with db() as connection:
        row = connection.execute(
            "SELECT access_expires_at, resource, scope FROM tokens WHERE access_hash = ?",
            (token_hash(value),),
        ).fetchone()
    if not row or row["access_expires_at"] < now() or row["resource"] != RESOURCE:
        return None
    scopes = set(row["scope"].split())
    return scopes if scopes.issubset(ALLOWED_SCOPES) else None


def required_scope_for_message(message: object) -> set[str] | None:
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return None
    method = message["method"]
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return None
        tool_name = params["name"]
        if tool_name in READ_ONLY_TOOLS:
            return {"pricing:read"}
        if tool_name in WRITE_TOOLS:
            return {"pricing:write"}
        # Future tools are write-capable until explicitly classified.
        return {"pricing:write"}
    if method in {
        "initialize",
        "ping",
        "prompts/get",
        "prompts/list",
        "resources/list",
        "resources/read",
        "tools/list",
    } or method.startswith("notifications/"):
        return {"pricing:read"}
    # Unknown protocol extensions fail closed behind the write scope.
    return {"pricing:write"}


def required_scopes_for_payload(body: bytes) -> set[str] | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    messages = payload if isinstance(payload, list) else [payload]
    if not messages:
        return None
    required: set[str] = set()
    for message in messages:
        message_scopes = required_scope_for_message(message)
        if message_scopes is None:
            return None
        required.update(message_scopes)
    return required


def forwarded_headers(request: Request) -> dict[str, str]:
    blocked = {
        "authorization",
        "connection",
        "content-length",
        "cookie",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in blocked
    }


def response_headers(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    allowed = {
        "cache-control",
        "content-encoding",
        "content-type",
        "mcp-session-id",
        "retry-after",
        "vary",
        "www-authenticate",
    }
    return {key: value for key, value in items if key.lower() in allowed}


@app.api_route(MCP_PATH, methods=["GET", "POST", "DELETE"])
async def mcp_proxy(request: Request):
    granted = access_scopes(bearer_token(request))
    if granted is None:
        return challenge()

    body = await request.body()
    if request.method == "POST":
        required = required_scopes_for_payload(body)
        if required is None:
            return json_error("invalid_request", "A valid JSON-RPC request is required")
    else:
        required = {"pricing:read"}
    if not required.issubset(granted):
        return insufficient_scope(required)

    client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
    try:
        upstream_request = client.build_request(
            request.method,
            UPSTREAM,
            headers=forwarded_headers(request),
            content=body,
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return JSONResponse({"error": "upstream_unavailable"}, status_code=502)

    async def stream_body():
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream_response.status_code,
        headers=response_headers(upstream_response.headers.multi_items()),
    )
