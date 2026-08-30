from app.services.plugins.rds import RdsPlugin


def test_customer_confirmed_rds_version_is_sent_exactly_to_official_api(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeExecutor:
        def __init__(self, clients: object) -> None:
            pass

        def execute(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {
                "pages": [
                    {
                        "OrderableDBInstanceOptions": [
                            {"DBInstanceClass": "db.m6g.xlarge"}
                        ]
                    }
                ]
            }

    monkeypatch.setattr(
        "app.services.plugins.rds.ReadOnlyAwsQueryExecutor",
        FakeExecutor,
    )
    plugin = RdsPlugin(None, None)  # type: ignore[arg-type]

    classes = plugin._orderable_classes(
        "us-east-1",
        "mysql",
        "8.4.11",
        requested_model="db.m6g.xlarge",
        exact_engine_version=True,
    )

    assert classes == {"db.m6g.xlarge"}
    assert calls[0]["parameters"] == {
        "Engine": "mysql",
        "DBInstanceClass": "db.m6g.xlarge",
        "EngineVersion": "8.4.11",
    }


def test_unconfirmed_community_patch_is_not_sent_as_fake_rds_build(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeExecutor:
        def __init__(self, clients: object) -> None:
            pass

        def execute(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {
                "pages": [
                    {
                        "OrderableDBInstanceOptions": [
                            {"DBInstanceClass": "db.m6g.xlarge"}
                        ]
                    }
                ]
            }

    monkeypatch.setattr(
        "app.services.plugins.rds.ReadOnlyAwsQueryExecutor",
        FakeExecutor,
    )
    plugin = RdsPlugin(None, None)  # type: ignore[arg-type]

    plugin._orderable_classes(
        "us-east-1",
        "mysql",
        "5.7.44",
        requested_model="db.m6g.xlarge",
        exact_engine_version=False,
    )

    assert "EngineVersion" not in calls[0]["parameters"]


def test_broad_rds_model_discovery_is_scoped_to_latest_engine_build(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeExecutor:
        def __init__(self, clients: object) -> None:
            pass

        def execute(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {
                "pages": [
                    {
                        "OrderableDBInstanceOptions": [
                            {"DBInstanceClass": "db.m7g.2xlarge"}
                        ]
                    }
                ]
            }

    monkeypatch.setattr(
        "app.services.plugins.rds.ReadOnlyAwsQueryExecutor",
        FakeExecutor,
    )
    plugin = RdsPlugin(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "_supported_engine_versions",
        lambda region, engine, **kwargs: ["18.6", "17.11"],
    )

    classes = plugin._orderable_classes(
        "ap-southeast-2",
        "postgres",
        None,
    )

    assert classes == {"db.m7g.2xlarge"}
    assert calls[0]["parameters"] == {
        "Engine": "postgres",
        "EngineVersion": "18.6",
    }
