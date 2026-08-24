from app.domain.models import ServiceRequirement
from app.domain.structured_component_updates import apply_component_update


def test_structured_shape_edit_reselects_model_and_locks_customer_fields() -> None:
    original = ServiceRequirement(
        service="ec2",
        region="ap-southeast-1",
        quantity=2,
        requirements={
            "requested_model": "m5.xlarge",
            "_review_selected_model": "m5.xlarge",
            "vcpu": 4,
            "memory_gib": 16,
        },
    )

    revised = apply_component_update(
        original,
        {"quantity": 3, "requirements": {"vcpu": 8, "memory_gib": 32}},
    )

    assert revised.quantity == 3
    assert revised.requirements["vcpu"] == 8
    assert revised.requirements["memory_gib"] == 32
    assert "requested_model" not in revised.requirements
    assert "_review_selected_model" not in revised.requirements
    assert revised.field_sources["quantity"] == "customer_confirmation"
    assert revised.field_sources["requirements.vcpu"] == "customer_confirmation"
    assert "requirements.memory_gib" in revised.locked_fields
    assert original.requirements["vcpu"] == 4


def test_structured_storage_edit_clears_old_reference_default() -> None:
    original = ServiceRequirement(
        service="s3",
        requirements={
            "storage_class": "standard",
            "reference_unit_only": True,
            "system_default_assumption": "容量未提供",
        },
    )

    revised = apply_component_update(
        original, {"requirements": {"storage_gib": 20480}}
    )

    assert revised.requirements["storage_gib"] == 20480
    assert "reference_unit_only" not in revised.requirements
    assert "system_default_assumption" not in revised.requirements
    assert revised.field_sources["requirements.storage_gib"] == "customer_confirmation"


def test_structured_edit_only_changes_targeted_fields() -> None:
    original = ServiceRequirement(
        service="rds",
        region="ap-southeast-1",
        quantity=1,
        requirements={
            "engine": "mysql",
            "deployment": "multi_az",
            "storage_gib": 300,
        },
    )

    revised = apply_component_update(
        original, {"requirements": {"storage_gib": 10240}}
    )

    assert revised.region == original.region
    assert revised.quantity == original.quantity
    assert revised.requirements["engine"] == "mysql"
    assert revised.requirements["deployment"] == "multi_az"
    assert revised.requirements["storage_gib"] == 10240


def test_catalog_compatibility_edit_clears_stale_model_for_every_service() -> None:
    for service, field, value in (
        ("ec2", "operating_system", "windows"),
        ("rds", "engine", "postgresql"),
        ("elasticache", "engine", "valkey"),
        ("msk", "cluster_type", "serverless"),
    ):
        original = ServiceRequirement(
            service=service,
            region="ap-southeast-1",
            requirements={
                "requested_model": "old.model",
                "_review_selected_model": "old.model",
                "_review_selected_specifications": {"vCPU": 4, "memoryGiB": 16},
            },
        )

        revised = apply_component_update(
            original, {"requirements": {field: value}}
        )

        assert "requested_model" not in revised.requirements
        assert "_review_selected_model" not in revised.requirements
        assert "_review_selected_specifications" not in revised.requirements
        assert revised.requirements[field] == value


def test_region_edit_clears_model_selected_in_another_region() -> None:
    original = ServiceRequirement(
        service="opensearch",
        region="ap-southeast-1",
        requirements={
            "requested_model": "m6g.large.search",
            "_review_selected_model": "m6g.large.search",
        },
    )

    revised = apply_component_update(original, {"region": "ap-northeast-1"})

    assert revised.region == "ap-northeast-1"
    assert "requested_model" not in revised.requirements
    assert "_review_selected_model" not in revised.requirements
