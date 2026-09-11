import hashlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

APP_PATH = Path(__file__).with_name("app.py")


def load_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    passwordless_auth: bool = False,
):
    try:
        import python_multipart  # noqa: F401
    except ImportError:
        # The production image installs the pinned runtime dependency. The main
        # application development environment does not need it, and these unit
        # tests do not parse form bodies.
        multipart_stub = types.ModuleType("python_multipart")
        multipart_stub.__version__ = "0.0.20"
        monkeypatch.setitem(sys.modules, "python_multipart", multipart_stub)

    salt = bytes.fromhex("11" * 16)
    digest = hashlib.scrypt(b"test-password", salt=salt, n=16384, r=8, p=1, dklen=32)
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://pricing-mcp.tontiancloud.com")
    monkeypatch.setenv("MCP_PATH", "/mcp")
    monkeypatch.setenv("UPSTREAM_MCP", "http://127.0.0.1:8200/v2/mcp")
    monkeypatch.setenv(
        "PASSWORDLESS_AUTH", "true" if passwordless_auth else "false"
    )
    if passwordless_auth:
        monkeypatch.delenv("LOGIN_USERNAME", raising=False)
        monkeypatch.delenv("PASSWORD_SALT", raising=False)
        monkeypatch.delenv("PASSWORD_DIGEST", raising=False)
    else:
        monkeypatch.setenv("LOGIN_USERNAME", "test-user")
        monkeypatch.setenv("PASSWORD_SALT", salt.hex())
        monkeypatch.setenv("PASSWORD_DIGEST", digest.hex())
    monkeypatch.setenv("DB_PATH", str(tmp_path / "oauth.db"))

    module_name = f"astraquote_oauth_test_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def stage_authorization_request(gateway, request_id: str = "request-id") -> None:
    gateway.init_db()
    with gateway.db() as connection:
        connection.execute(
            "INSERT INTO clients VALUES (?, ?, ?, ?, ?, ?)",
            (
                "chatgpt-client",
                json.dumps(["https://chatgpt.com/connector_platform_oauth_redirect"]),
                "none",
                None,
                "ChatGPT",
                gateway.now(),
            ),
        )
        connection.execute(
            "INSERT INTO auth_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                request_id,
                "chatgpt-client",
                "https://chatgpt.com/connector_platform_oauth_redirect",
                "state-value",
                gateway.DEFAULT_SCOPE,
                gateway.RESOURCE,
                "challenge-value",
                gateway.now(),
            ),
        )


def test_passwordless_authorization_page_has_only_a_confirmation_button(
    tmp_path, monkeypatch
):
    gateway = load_gateway(tmp_path, monkeypatch, passwordless_auth=True)

    body = gateway.login_page("request-id").body.decode()

    assert "确认授权" in body
    assert "name='username'" not in body
    assert "name='password'" not in body


def test_passwordless_authorization_issues_code_without_credentials(
    tmp_path, monkeypatch
):
    gateway = load_gateway(tmp_path, monkeypatch, passwordless_auth=True)
    stage_authorization_request(gateway)

    response = gateway.authorize_post(
        request_from("203.0.113.12"),
        request_id="request-id",
        username=None,
        password=None,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "https://chatgpt.com/connector_platform_oauth_redirect?"
    )
    with sqlite3.connect(gateway.DB_PATH) as connection:
        assert connection.execute("SELECT count(*) FROM auth_codes").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM auth_requests").fetchone()[0]
            == 0
        )


def request_from(address: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/oauth/register",
            "raw_path": b"/oauth/register",
            "query_string": b"",
            "headers": [],
            "client": (address, 12345),
            "server": ("pricing-mcp.tontiancloud.com", 443),
        }
    )


def test_existing_database_rows_survive_initialization(tmp_path, monkeypatch):
    gateway = load_gateway(tmp_path, monkeypatch)
    gateway.init_db()
    with gateway.db() as connection:
        connection.execute(
            "INSERT INTO clients VALUES (?, ?, ?, ?, ?, ?)",
            (
                "existing-client",
                json.dumps(["https://chatgpt.com/connector_platform_oauth_redirect"]),
                "none",
                None,
                "ChatGPT",
                gateway.now(),
            ),
        )
    gateway.init_db()
    with sqlite3.connect(gateway.DB_PATH) as connection:
        count = connection.execute(
            "SELECT count(*) FROM clients WHERE client_id = 'existing-client'"
        ).fetchone()[0]
    assert count == 1


def test_readiness_does_not_create_a_missing_database(tmp_path, monkeypatch):
    gateway = load_gateway(tmp_path, monkeypatch)
    database_path = Path(gateway.DB_PATH)
    assert not database_path.exists()
    assert gateway.database_ready() is False
    assert not database_path.exists()


def test_readiness_requires_the_complete_oauth_schema(tmp_path, monkeypatch):
    gateway = load_gateway(tmp_path, monkeypatch)
    gateway.init_db()
    with sqlite3.connect(gateway.DB_PATH) as connection:
        connection.execute('DROP TABLE clients')

    assert gateway.database_ready() is False


def test_readiness_rejects_a_corrupt_oauth_database(tmp_path, monkeypatch):
    gateway = load_gateway(tmp_path, monkeypatch)
    Path(gateway.DB_PATH).write_bytes(b'not a sqlite database')

    assert gateway.database_ready() is False


def test_scope_classification_is_fail_closed(tmp_path, monkeypatch):
    gateway = load_gateway(tmp_path, monkeypatch)

    initialize = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    ).encode()
    prices = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_prices", "arguments": {}},
        }
    ).encode()
    resume = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "resume_quote_job", "arguments": {}},
        }
    ).encode()
    build = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "build_estimate", "arguments": {}},
        }
    ).encode()
    unknown = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "future_tool", "arguments": {}},
        }
    ).encode()
    removed_tool = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "import_estimate", "arguments": {}},
        }
    ).encode()

    assert gateway.required_scopes_for_payload(initialize) == {"pricing:read"}
    assert gateway.required_scopes_for_payload(prices) == {"pricing:read"}
    assert gateway.required_scopes_for_payload(resume) == {"pricing:read"}
    assert gateway.required_scopes_for_payload(build) == {"pricing:write"}
    assert gateway.required_scopes_for_payload(removed_tool) == {"pricing:write"}
    assert gateway.required_scopes_for_payload(unknown) == {"pricing:write"}
    assert gateway.required_scopes_for_payload(b"not-json") is None


def test_access_token_returns_only_its_persisted_scopes(tmp_path, monkeypatch):
    gateway = load_gateway(tmp_path, monkeypatch)
    gateway.init_db()
    access_token = "access-token"
    with gateway.db() as connection:
        connection.execute(
            "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                gateway.token_hash(access_token),
                gateway.token_hash("refresh-token"),
                "client-id",
                "pricing:read offline_access",
                gateway.RESOURCE,
                gateway.now() + 60,
                gateway.now() + 600,
            ),
        )

    assert gateway.access_scopes(access_token) == {"pricing:read", "offline_access"}
    assert gateway.access_scopes("wrong-token") is None


def test_global_and_per_address_rate_limits(tmp_path, monkeypatch):
    gateway = load_gateway(tmp_path, monkeypatch)
    gateway.RATE_LIMITS["register"] = (2, 1)
    first = request_from("203.0.113.10")
    second = request_from("203.0.113.10")
    other = request_from("203.0.113.11")

    assert gateway.rate_limit(first, "register") is None
    limited = gateway.rate_limit(second, "register")
    assert limited is not None
    assert limited.status_code == 429
    globally_limited = gateway.rate_limit(other, "register")
    assert globally_limited is not None
    assert globally_limited.status_code == 429


@pytest.mark.asyncio
async def test_health_endpoints_do_not_disclose_dependency_details(
    tmp_path, monkeypatch
):
    gateway = load_gateway(tmp_path, monkeypatch)
    gateway.init_db()
    assert gateway.healthz().status_code == 200

    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(gateway, "upstream_ready", unavailable)
    response = await gateway.readyz()
    assert response.status_code == 503
    assert response.body == b'{"status":"not_ready"}'


def echo_body_app(calls: list[int]):
    async def app(scope, receive, send):
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        calls.append(len(body))
        await JSONResponse({"size": len(body)})(scope, receive, send)

    return app


@pytest.mark.asyncio
async def test_body_limit_accepts_boundary_and_ignores_unrelated_paths(
    tmp_path, monkeypatch
):
    gateway = load_gateway(tmp_path, monkeypatch)
    calls: list[int] = []
    limited_app = gateway.RequestBodyLimitMiddleware(
        echo_body_app(calls), max_body_bytes=8
    )
    transport = httpx.ASGITransport(app=limited_app)

    async with httpx.AsyncClient(
        transport=transport, base_url="https://pricing-mcp.tontiancloud.com"
    ) as client:
        boundary = await client.post("/mcp", content=b"12345678")
        unrelated = await client.post("/unrelated", content=b"123456789")

    assert boundary.status_code == 200
    assert boundary.json() == {"size": 8}
    assert unrelated.status_code == 200
    assert unrelated.json() == {"size": 9}
    assert calls == [8, 9]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/mcp", "/oauth/register", "/oauth/authorize", "/oauth/token"]
)
async def test_body_limit_rejects_declared_oversize_before_downstream(
    tmp_path, monkeypatch, path
):
    gateway = load_gateway(tmp_path, monkeypatch)
    calls: list[int] = []
    limited_app = gateway.RequestBodyLimitMiddleware(
        echo_body_app(calls), max_body_bytes=8
    )
    transport = httpx.ASGITransport(app=limited_app)

    async with httpx.AsyncClient(
        transport=transport, base_url="https://pricing-mcp.tontiancloud.com"
    ) as client:
        response = await client.post(path, content=b"123456789")

    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert response.headers["cache-control"] == "no-store"
    assert calls == []


@pytest.mark.asyncio
async def test_body_limit_rejects_chunked_oversize_without_content_length(
    tmp_path, monkeypatch
):
    gateway = load_gateway(tmp_path, monkeypatch)
    calls: list[int] = []
    limited_app = gateway.RequestBodyLimitMiddleware(
        echo_body_app(calls), max_body_bytes=8
    )
    transport = httpx.ASGITransport(app=limited_app)

    async def chunks():
        yield b"12345"
        yield b"6789"

    async with httpx.AsyncClient(
        transport=transport, base_url="https://pricing-mcp.tontiancloud.com"
    ) as client:
        response = await client.post("/mcp", content=chunks())

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp", "/oauth/register"])
async def test_gateway_installs_explicit_two_mib_limit(tmp_path, monkeypatch, path):
    gateway = load_gateway(tmp_path, monkeypatch)
    assert gateway.MAX_REQUEST_BODY_BYTES == 2 * 1024 * 1024
    transport = httpx.ASGITransport(app=gateway.app)
    oversized = b"x" * (gateway.MAX_REQUEST_BODY_BYTES + 1)

    async with httpx.AsyncClient(
        transport=transport, base_url="https://pricing-mcp.tontiancloud.com"
    ) as client:
        response = await client.post(
            path,
            content=oversized,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
