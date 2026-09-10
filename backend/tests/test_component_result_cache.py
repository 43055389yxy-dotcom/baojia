from pathlib import Path

import pytest

from app.domain.models import ServiceRequirement
from app.integrations.component_result_cache import ValidatedComponentResultCache


@pytest.mark.parametrize("change", [
    {"parent_component_key": "another-parent"},
    {"official_calculator_schema_hash": "b" * 64},
    {"official_calculator_template_id": "another-template"},
    {"field_evidence": {"_owned_source_slice_text": "child 8 nodes"}},
    {"field_sources": {"quantity": "customer_confirmation"}},
])
def test_cache_never_crosses_ownership_schema_or_authority(tmp_path, change):
    cache = ValidatedComponentResultCache(tmp_path / "cache.sqlite3")
    original = ServiceRequirement(
        service="ec2", source_text="parent 1 cluster, child 4 nodes",
        parent_component_key="cmp_parent", official_calculator_schema_hash="a" * 64,
        field_sources={"_owned_source_slice": "system_policy"},
        field_evidence={"_owned_source_slice_text": "child 4 nodes"})
    cache.put(original, "model", original)
    assert cache.get(original.model_copy(update=change), "model") is None


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


def test_component_result_cache_never_crosses_product_identity_variant(
    tmp_path: Path,
) -> None:
    cache = ValidatedComponentResultCache(tmp_path / "components.sqlite3")
    source = "Amazon EBS Snapshot：每日快照，保留7天"
    volume_identity = ServiceRequirement(
        service="ebs",
        calculator_service_name="Amazon EBS",
        product_identity="ebs_volume",
        source_text=source,
    )
    cached_volume = volume_identity.model_copy(
        update={"requirements": {"product_variant": "volume"}}
    )
    cache.put(volume_identity, "test-model", cached_volume)

    snapshot_identity = volume_identity.model_copy(
        update={
            "calculator_service_name": "Amazon EBS Snapshot",
            "product_identity": "ebs_snapshot",
        }
    )

    assert cache.get(snapshot_identity, "test-model") is None
