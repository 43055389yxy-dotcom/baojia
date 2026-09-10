from __future__ import annotations

from pathlib import Path


def test_jenkins_deploy_updates_and_restarts_the_host_browser_relay() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "deploy/jenkins-shell.sh").read_text(encoding="utf-8")

    assert "/home/ec2-user/astraquote/source" in script
    assert '"$APP_DIR/backend/"' in script
    assert '"$APP_DIR/tools/"' in script
    assert '"$APP_DIR/policies/"' in script
    assert "rsync" in script
    assert "astraquote-gpt-relay.service" in script
    assert "systemctl restart" in script
    assert "systemctl is-active" in script
