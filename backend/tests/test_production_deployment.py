from __future__ import annotations

from pathlib import Path


def test_jenkins_deploy_updates_and_restarts_the_host_browser_relay() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "deploy/jenkins-shell.sh").read_text(encoding="utf-8")

    assert 'RELAY_HOST_ROOT="/home/ec2-user/astraquote"' in script
    assert "-cf - backend tools policies" in script
    assert '-v "$RELAY_HOST_ROOT:/host/astraquote"' in script
    assert "--pid=host" in script
    assert '"$RELAY_WORKER_COMMAND "*' in script
    assert "--entrypoint /usr/bin/nsenter" in script
    assert "/usr/bin/systemctl restart astraquote-gpt-relay.service" in script
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

    unit = (root / "deploy/desktop/astraquote-gpt-relay.service").read_text(
        encoding="utf-8"
    )
    assert "ASTRAQUOTE_GPT_RELAY_MAX_TABS" not in unit


def test_runtime_image_contains_the_host_namespace_helper() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "deploy/Dockerfile").read_text(encoding="utf-8")

    assert "util-linux" in dockerfile
