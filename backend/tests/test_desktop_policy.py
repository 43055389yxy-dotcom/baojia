from __future__ import annotations

from pathlib import Path


def test_desktop_policy_disables_idle_lock_without_removing_vnc_authentication() -> None:
    root = Path(__file__).resolve().parents[2]
    policy = (root / "deploy/desktop/astraquote-session-policy.sh").read_text(
        encoding="utf-8"
    )
    installer = (root / "deploy/install-desktop-policy.sh").read_text(
        encoding="utf-8"
    )
    unlock_guard = (root / "deploy/desktop/astraquote-unlock-guard.sh").read_text(
        encoding="utf-8"
    )
    unlock_service = (
        root / "deploy/desktop/astraquote-unlock-guard.service"
    ).read_text(encoding="utf-8")
    vnc = (root / "deploy/desktop/tigervnc-config").read_text(encoding="utf-8")

    assert "idle-delay 'uint32 0'" in policy
    assert "lock-enabled false" in policy
    assert "idle-activation-enabled false" in policy
    assert "xset -dpms" in policy
    assert "DBUS_SESSION_BUS_ADDRESS" in installer
    assert "systemctl --user enable --now astraquote-unlock-guard.service" in installer
    assert 'Service --value' in unlock_guard
    assert '"tigervnc"' in unlock_guard
    assert 'LockedHint --value' in unlock_guard
    assert 'loginctl unlock-session "$session_id"' in unlock_guard
    assert "org.gnome.ScreenSaver.SetActive false" in unlock_guard
    assert "Restart=always" in unlock_service
    assert "securitytypes=vncauth" in vnc
