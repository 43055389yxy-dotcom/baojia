from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_jenkins_deploy_updates_and_restarts_the_host_codex_chat_relay() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "deploy/jenkins-shell.sh").read_text(encoding="utf-8")

    assert 'RELAY_HOST_ROOT="/home/ec2-user/astraquote"' in script
    assert "-cf - backend tools policies" in script
    assert '-v "$RELAY_HOST_ROOT:/host/astraquote"' in script
    assert "--pid=host" in script
    assert '"$RELAY_WORKER_COMMAND "*' in script
    assert "--entrypoint /usr/bin/nsenter" in script
    assert "/usr/bin/systemctl enable --now astraquote-gpt-relay.service" in script
    assert "astraquote-chatgpt-desktop" in script
    assert "codex://threads/new?mode=chat" in script
    assert "--shm-size 1g" in script
    assert "host_codex_cdp_ready" in script
    assert "--network host" in script
    assert "--entrypoint /usr/bin/curl" in script
    assert "if curl -fsS http://127.0.0.1:9222/json/list" not in script
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
    assert "Installing the versioned desktop relay systemd unit" in script
    assert "Restarting the desktop relay through the Docker host systemd" in script
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
    assert "ASTRAQUOTE_CODEX_CONTAINER=astraquote-chatgpt-desktop" in unit
    assert "ASTRAQUOTE_CODEX_CDP=http://127.0.0.1:9222" in unit
    assert "ASTRAQUOTE_FIREFOX_PROFILE" not in unit
    assert "ASTRAQUOTE_CHATGPT_PROJECT" not in unit
    assert "chatgpt.com/projects" not in unit
    assert "docker.service" in unit


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
