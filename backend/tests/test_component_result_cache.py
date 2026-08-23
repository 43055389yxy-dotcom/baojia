from pathlib import Path

from app.domain.models import ServiceRequirement
from app.integrations.component_result_cache import ValidatedComponentResultCache


def test_validated_component_result_cache_persists_exact_result(tmp_path: Path) -> None:
    database_path = tmp_path / "components.sqlite3"
    original = ServiceRequirement(
        service="msk",
        calculator_service_name="Amazon MSK",
        source_text="Kafka 3个节点，每节点8核32G，磁盘500GB",
    )
    validated = original.model_copy(
        update={
            "requirements": {
                "broker_count": 3,
                "vcpu": 8,
                "memory_gib": 32,
                "storage_gib_per_broker": 500,
            }
        }
    )

    ValidatedComponentResultCache(database_path).put(
        original, "deepseek.v3.2", validated
    )
    restored = ValidatedComponentResultCache(database_path).get(
        original, "deepseek.v3.2"
    )

    assert restored is not None
    assert restored.requirements == validated.requirements


def test_component_result_cache_never_reuses_changed_customer_values(
    tmp_path: Path,
) -> None:
    cache = ValidatedComponentResultCache(tmp_path / "components.sqlite3")
    original = ServiceRequirement(
        service="s3",
        calculator_service_name="Amazon S3",
        source_text="对象存储20TB",
    )
    validated = original.model_copy(
        update={"requirements": {"storage_gib": 20480}}
    )
    cache.put(original, "deepseek.v3.2", validated)

    changed = original.model_copy(update={"source_text": "对象存储50TB"})

    assert cache.get(changed, "deepseek.v3.2") is None
