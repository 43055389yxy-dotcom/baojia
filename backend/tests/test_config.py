from app.core.config import Settings


def test_estimate_pool_ids_accept_comma_separated_environment(monkeypatch) -> None:
    monkeypatch.setenv("BCM_WORKLOAD_ESTIMATE_IDS", "first-id, second-id")
    settings = Settings(_env_file=None)
    assert settings.bcm_workload_estimate_ids == ["first-id", "second-id"]


def test_ai_uses_desktop_network_proxy_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.ai_trust_env_proxy is True
