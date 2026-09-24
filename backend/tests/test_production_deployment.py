from __future__ import annotations

from pathlib import Path


def test_jenkins_deploys_the_single_mcp_and_retires_remote_relays() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "deploy/jenkins-shell.sh").read_text(encoding="utf-8")

    assert "AstraQuote single-MCP deployment succeeded" in script
    assert "http://127.0.0.1:8200/readyz" in script
    assert "http://127.0.0.1:8001/readyz" in script
    assert "http://127.0.0.1:8200/v2/mcp" in script
    assert "Authorization: Bearer $ASTRAQUOTE_INTERNAL_TOKEN" in script
    assert "systemctl disable --now astraquote-gpt-relay.service" in script
    assert "systemctl disable --now astraquote-gemini-relay.service" in script
    assert "docker stop astraquote-chatgpt-desktop" in script
    assert "stage_host_browser_relay" not in script
    assert "wait_for_chatgpt_quote_engine" not in script
    assert "gpt_quote_relay_worker.py" not in script


def test_jenkins_preserves_oauth_and_validates_the_public_route() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "deploy/jenkins-shell.sh").read_text(encoding="utf-8")

    assert "snapshot_oauth_database" in script
    assert "verify_oauth_database_continuity" in script
    assert "OAuth client registry lost entries during deployment" in script
    assert "update_caddy_route" in script
    assert '"$APP_DIR/deploy/caddy-astraquote.caddy"' in script
    assert "/home/ec2-user/caddy-gateway/managed:/host/caddy-managed" in script
    assert "caddy validate --config /etc/caddy/Caddyfile" in script
    assert "caddy reload --config /etc/caddy/Caddyfile" in script
    assert "The AstraQuote Caddy route was invalid and has been rolled back" in script


def test_production_runs_local_pricing_inside_the_mcp() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "deploy/compose.production.yml").read_text(encoding="utf-8")
    dockerfile = (root / "deploy/Dockerfile").read_text(encoding="utf-8")
    start = (root / "deploy/start-production.sh").read_text(encoding="utf-8")

    assert 'ASTRAQUOTE_LOCAL_MODE: "1"' in compose
    assert "ASTRAQUOTE_BACKEND_ROOT: /app/backend" in compose
    assert "ASTRAQUOTE_LOCAL_BRIDGE: /app/backend/scripts/local_mcp_bridge.py" in compose
    assert "ASTRAQUOTE_DELIVERY_MODE: local" in compose
    assert "ASTRAQUOTE_DOWNLOAD_DIR: /data/downloads" in compose
    assert "ASTRAQUOTE_BACKEND_URL" not in compose
    assert "BACKEND_API_URL" not in compose
    assert "local_mcp_bridge.py" in dockerfile
    assert "frontend-builder" not in dockerfile
    assert "MCP_PID" in start
    assert "OAUTH_PID" in start
    assert "BACKEND_PID" not in start
    assert "FRONTEND_PID" not in start


def test_internal_mcp_is_private_token_authenticated() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "deploy/compose.production.yml").read_text(encoding="utf-8")
    server = (root / "deploy/astraquote-mcp/server.js").read_text(encoding="utf-8")
    oauth = (root / "deploy/astraquote-mcp-oauth/app.py").read_text(encoding="utf-8")

    assert "ASTRAQUOTE_MCP_HOST: 0.0.0.0" in compose
    assert "ASTRAQUOTE_INTERNAL_TOKEN" in server
    assert "validMcpBearer" in server
    assert 'headers["authorization"] = f"Bearer {UPSTREAM_BEARER_TOKEN}"' in oauth


def test_runtime_image_contains_only_needed_host_retirement_helper() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "deploy/Dockerfile").read_text(encoding="utf-8")

    assert "util-linux" in dockerfile
    assert "chromium" not in dockerfile.casefold()
    assert "playwright" not in dockerfile.casefold()


def test_public_proxy_routes_only_mcp_oauth_and_tokenized_downloads() -> None:
    root = Path(__file__).resolve().parents[2]
    caddyfile = (root / "deploy/caddy-astraquote.caddy").read_text(encoding="utf-8")

    assert "path /mcp /oauth/* /.well-known/oauth-authorization-server" in caddyfile
    assert "reverse_proxy astraquote:8001" in caddyfile
    assert "path /downloads/*" in caddyfile
    assert "reverse_proxy astraquote:8200" in caddyfile
    assert "reverse_proxy astraquote:3000" not in caddyfile
