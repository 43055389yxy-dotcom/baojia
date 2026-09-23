from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_jenkins_deploy_updates_and_restarts_the_headless_codex_relay() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "deploy/jenkins-shell.sh").read_text(encoding="utf-8")

    assert 'RELAY_HOST_ROOT="/home/ec2-user/astraquote"' in script
    assert "-cf - backend tools policies" in script
    assert '-v "$RELAY_HOST_ROOT:/host/astraquote"' in script
    assert "--pid=host" in script
    assert '"$RELAY_WORKER_COMMAND "*' in script
    assert "--entrypoint /usr/bin/nsenter" in script
    assert "/usr/bin/systemctl enable astraquote-gpt-relay.service" in script
    assert "/usr/bin/rm -f /home/ec2-user/astraquote/data/gpt-relay/worker-heartbeat.json" in script
    assert "/usr/bin/systemctl restart astraquote-gpt-relay.service" in script
    assert "/usr/bin/systemctl disable --now astraquote-gemini-relay.service" in script
    assert "/usr/bin/systemctl enable astraquote-gemini-relay.service" not in script
    assert "/usr/bin/systemctl restart astraquote-gemini-relay.service" not in script
    assert "astraquote-chatgpt-desktop" in script
    assert "docker stop astraquote-chatgpt-desktop" in script
    assert "--entrypoint /usr/lib/chatgpt/resources/codex" in script
    assert '"$CODEX_IMAGE" login status' in script
    assert "com.astraquote.codex-relay=true" in script
    assert "http://astraquote:8200/v2/mcp" in script
    assert 'Authorization: Bearer $ASTRAQUOTE_INTERNAL_TOKEN' in script
    assert "host_codex_cdp_ready" not in script
    assert "worker-heartbeat.json" in script
    assert "heartbeat.get" in script
    assert "logged_in" in script
    assert "The headless Codex quote engine did not become ready" in script
    assert "cd / && exec /home/ec2-user/astraquote/gpt-relay-venv/bin/python -m pip" in script
    assert "/home/ec2-user/astraquote/gpt-relay-venv/bin/pip install" not in script
    assert "firefox" not in script.casefold()
    assert "geckodriver" not in script.casefold()
    assert "rsync" not in script
    assert "sudo" not in script


def test_jenkins_health_checks_explain_the_failure_stage() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "deploy/jenkins-shell.sh").read_text(encoding="utf-8")

    assert "AstraQuote container endpoints are ready" in script
    assert "AstraQuote container endpoints did not become ready" in script
    assert "Staging the desktop relay source" in script
    assert "Activating the staged desktop relay source" in script
    assert "Installing the versioned quote relay systemd unit" in script
    assert "Restarting the quote relay through the Docker host systemd" in script
    assert "systemctl --no-pager --full status astraquote-gpt-relay.service" in script
    assert "journalctl --no-pager -u astraquote-gpt-relay.service -n 120" in script
    assert "snapshot_oauth_database" in script
    assert "verify_oauth_database_continuity" in script
    assert "OAuth client registry lost entries during deployment" in script

    unit = (root / "deploy/desktop/astraquote-gpt-relay.service").read_text(
        encoding="utf-8"
    )
    assert "ASTRAQUOTE_GPT_RELAY_MAX_TABS" not in unit
    assert (
        "Environment=ASTRAQUOTE_V2_STATE_DIR=/home/ec2-user/astraquote/data/v2-quotes"
        in unit
    )
    assert "ASTRAQUOTE_CODEX_IMAGE=astraquote/chatgpt-desktop" in unit
    assert "ASTRAQUOTE_DOCKER_NETWORK=caddy-net" in unit
    assert "ExecStartPre=/usr/bin/docker image inspect" in unit
    assert "ASTRAQUOTE_CODEX_CDP" not in unit
    assert "vncserver@:1.service" not in unit
    assert "StartLimitIntervalSec=0" in unit
    assert "Restart=always" in unit
    assert "ASTRAQUOTE_FIREFOX_PROFILE" not in unit
    assert "ASTRAQUOTE_CHATGPT_PROJECT" not in unit
    assert "chatgpt.com/projects" not in unit
    assert "docker.service" in unit


def test_internal_mcp_is_private_token_authenticated() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "deploy/compose.production.yml").read_text(encoding="utf-8")
    server = (root / "deploy/astraquote-mcp/server.js").read_text(encoding="utf-8")
    oauth = (root / "deploy/astraquote-mcp-oauth/app.py").read_text(
        encoding="utf-8"
    )

    assert "ASTRAQUOTE_MCP_HOST: 0.0.0.0" in compose
    assert "ASTRAQUOTE_INTERNAL_TOKEN" in server
    assert "validMcpBearer" in server
    assert 'headers["authorization"] = f"Bearer {UPSTREAM_BEARER_TOKEN}"' in oauth


def test_gemini_relay_is_a_separate_visible_persistent_worker() -> None:
    root = Path(__file__).resolve().parents[2]
    unit = (root / "deploy/desktop/astraquote-gemini-relay.service").read_text(
        encoding="utf-8"
    )
    requirements = (root / "deploy/desktop/relay-requirements.txt").read_text(
        encoding="utf-8"
    )

    assert "ASTRAQUOTE_RELAY_ENGINE=gemini" in unit
    assert "DISPLAY=:1" in unit
    assert "ASTRAQUOTE_GEMINI_PROFILE=" in unit
    assert "Restart=always" in unit
    assert "pkill -u ec2-user -f astraquote-gemini" not in unit
    assert "firefox.*astraquote-gemini" in unit
    assert "selenium==" in requirements


def test_runtime_image_contains_the_host_namespace_helper() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "deploy/Dockerfile").read_text(encoding="utf-8")

    assert "util-linux" in dockerfile


def test_public_proxy_keeps_oauth_and_mcp_outside_the_sales_site() -> None:
    root = Path(__file__).resolve().parents[2]
    caddyfile = (root / "deploy/caddy-astraquote.caddy").read_text(encoding="utf-8")

    assert "path /mcp /oauth/* /.well-known/oauth-authorization-server" in caddyfile
    assert "reverse_proxy astraquote:8001" in caddyfile
    assert "reverse_proxy astraquote:3000" in caddyfile


def test_jenkins_updates_and_validates_the_versioned_caddy_route() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "deploy/jenkins-shell.sh").read_text(encoding="utf-8")

    assert "update_caddy_route" in script
    assert '"$APP_DIR/deploy/caddy-astraquote.caddy"' in script
    assert "/home/ec2-user/caddy-gateway/managed:/host/caddy-managed" in script
    assert "caddy validate --config /etc/caddy/Caddyfile" in script
    assert "caddy reload --config /etc/caddy/Caddyfile" in script
    assert "The AstraQuote Caddy route was invalid and has been rolled back" in script


def test_desktop_relay_prompt_import_does_not_require_botocore() -> None:
    root = Path(__file__).resolve().parents[2]
    script = """
import builtins

original_import = builtins.__import__

def without_botocore(name, *args, **kwargs):
    if name == "botocore" or name.startswith("botocore."):
        raise ModuleNotFoundError("botocore intentionally unavailable")
    return original_import(name, *args, **kwargs)

builtins.__import__ = without_botocore
from app.services.gpt_quote_prompt import build_quote_prompt

prompt = build_quote_prompt(
    relay_job_id="gpt-0123456789abcdef0123456789abcdef",
    submission_code="1",
    customer_request="云服务器 1 台",
    options={"cloud_provider": "alibaba", "preferred_region": "cn-shanghai"},
)
assert "阿里云中国站" in prompt
"""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=root / "backend",
        check=True,
    )
