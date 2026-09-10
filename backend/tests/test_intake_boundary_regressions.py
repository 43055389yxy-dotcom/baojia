"""Regression tests for lossless intake and component ownership boundaries.

These cases intentionally exercise shared parser contracts instead of pricing
plugins.  A presentation heading, an AI display name, normalized source text,
or a product-internal count must never disable or corrupt the component ledger.
"""

from app.core.config import Settings
from app.domain.customer_configuration import preserve_customer_configuration
from app.domain.models import ParsedIntent, ServiceRequirement
from app.domain.models import UnmappedPricingFact
from app.integrations.deepseek import DeepSeekIntentParser


def test_markdown_numbered_plan_title_keeps_lossless_sales_numbering_fast_path() -> None:
    source = """## 2026 年方案 1｜生产架构

1、Amazon EC2：3台，m6a.2xlarge，单台8核32G。
2、Amazon S3：Standard 存储10TB。
3、Amazon Route 53：数量1个 Hosted Zone。
"""

    parsed = DeepSeekIntentParser._intent_from_lossless_sales_numbering(source)

    assert parsed is not None
    assert [component.service for component in parsed.services] == [
        "ec2",
        "s3",
        "route53",
    ]
    assert [component.original_source_text for component in parsed.services] == [
        "Amazon EC2：3台，m6a.2xlarge，单台8核32G。",
        "Amazon S3：Standard 存储10TB。",
        "Amazon Route 53：数量1个 Hosted Zone。",
    ]


def test_global_ai_human_readable_service_name_is_canonicalized_before_schema() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    raw = {
        "customer_summary": "DNS 与对象存储",
        "services": [
            {
                "service": "Amazon Route 53",
                "calculator_service_name": "Amazon Route 53",
                "source_text": "Amazon Route 53：数量1个 Hosted Zone",
                "requirements": {"hosted_zones": 1},
            },
            {
                # A valid sibling proves one display-style label cannot reject
                # the complete global-AI envelope at the Pydantic boundary.
                "service": "s3",
                "calculator_service_name": "Amazon S3",
                "source_text": "Amazon S3：存储10TB",
                "requirements": {"storage_gib": 10240},
            },
        ],
        "ambiguities": [],
    }

    parsed = ParsedIntent.model_validate(
        parser._normalize(raw, fallback_summary="DNS 与对象存储")
    )

    assert [component.service for component in parsed.services] == ["route53", "s3"]


def test_component_order_falls_back_to_immutable_original_source_text() -> None:
    source = """1、Amazon EC2：3台，单台8核32G。
2、Amazon S3：Standard 存储10TB。"""
    parsed = ParsedIntent(
        customer_summary="顺序回归",
        services=[
            ServiceRequirement(
                service="s3",
                source_text="Amazon S3｜总存储：10TB",
                original_source_text="Amazon S3：Standard 存储10TB。",
            ),
            ServiceRequirement(
                service="ec2",
                source_text="Amazon EC2｜实例数量：3台｜每台CPU：8核｜每台内存：32GB",
                original_source_text="Amazon EC2：3台，单台8核32G。",
            ),
        ],
    )

    DeepSeekIntentParser._order_services_by_source(source, parsed)

    assert [component.service for component in parsed.services] == ["ec2", "s3"]


def test_fact_audit_accepts_schema_unit_conversions_without_losing_raw_atoms() -> None:
    api = ServiceRequirement(
        service="apigateway",
        source_text="API：平均请求大小64KB。",
        requirements={"request_size_mb": 0.0625},
        field_evidence={"requirements.request_size_mb": "平均请求大小64KB"},
    )
    lambda_component = ServiceRequirement(
        service="lambda",
        source_text="Lambda：平均执行2秒，内存2GB。",
        requirements={"duration_ms": 2000, "memory_mb": 2048},
        field_evidence={
            "requirements.duration_ms": "平均执行2秒",
            "requirements.memory_mb": "内存2GB",
        },
    )

    assert DeepSeekIntentParser._uncovered_quantitative_claim_issues(
        api.source_text, api
    ) == []
    assert DeepSeekIntentParser._uncovered_quantitative_claim_issues(
        lambda_component.source_text, lambda_component
    ) == []


def test_fact_audit_accepts_literal_generic_counts_owned_by_any_numeric_field() -> None:
    component = ServiceRequirement(
        service="cloudwatch",
        source_text="CloudWatch：自定义指标5000个。",
        requirements={"custom_metrics": 5000},
        field_evidence={"requirements.custom_metrics": "自定义指标5000个"},
    )

    assert DeepSeekIntentParser._uncovered_quantitative_claim_issues(
        component.source_text, component
    ) == []


def test_count_evidence_allows_model_between_number_and_resource_role() -> None:
    examples = (
        ("redshift", "requirements.nodes", 4, "4个ra3.4xlarge计算节点"),
        ("opensearch", "requirements.data_nodes", 9, "9个r7g.2xlarge.search数据节点"),
        ("dms", "requirements.replication_instances", 2, "2个dms.r6i.large复制实例"),
    )
    for service, path, value, evidence in examples:
        field = path.split(".", 1)[1]
        component = ServiceRequirement(
            service=service,
            source_text=evidence,
            requirements={field: value},
            field_evidence={path: evidence},
        )
        DeepSeekIntentParser._validate_numeric_evidence_value(
            component,
            path=path,
            snippet=evidence,
        )


def test_unmapped_capacity_uses_its_declared_unit_during_fact_conservation() -> None:
    component = ServiceRequirement(
        service="backup",
        source_text="AWS Backup：受保护数据40TB。",
        unmapped_pricing_facts=[
            UnmappedPricingFact(
                field_hint="受保护数据总量",
                value=40,
                unit="TB",
                scope="aggregate",
                evidence="受保护数据40TB",
            )
        ],
    )

    assert DeepSeekIntentParser._uncovered_quantitative_claim_issues(
        component.source_text, component
    ) == []


def test_database_topology_preserves_total_members_and_reader_composition() -> None:
    source = "Amazon Aurora MySQL：1主2只读，单节点8核32G。"
    component = ServiceRequirement(
        service="rds",
        calculator_service_name="Amazon Aurora MySQL",
        source_text=source,
        original_source_text=source,
    )
    parsed = ParsedIntent(customer_summary=source, services=[component])

    preserve_customer_configuration(parsed)

    assert component.requirements["cluster_members"] == 3
    assert component.requirements["read_replica_count"] == 2
    assert component.field_sources["requirements.read_replica_count"] == "customer_text"
    assert "2只读" in component.field_evidence["requirements.read_replica_count"]


def test_hosted_zone_count_is_not_owned_by_top_level_component_quantity() -> None:
    component = ServiceRequirement(
        service="route53",
        calculator_service_name="Amazon Route 53",
        source_text="Amazon Route 53：数量：1个 Hosted Zone",
        original_source_text="Amazon Route 53：数量1个 Hosted Zone。",
    )
    parsed = ParsedIntent(customer_summary="Route 53", services=[component])

    preserve_customer_configuration(parsed)

    assert component.quantity == 1
    assert component.requirements["hosted_zones"] == 1
    assert component.field_sources["requirements.hosted_zones"] == "customer_text"
    assert "quantity" not in component.field_sources
    assert "quantity" not in component.locked_fields


def test_monotonic_repair_keeps_hosted_zone_fact_and_rejects_role_count_as_quantity() -> None:
    source = "Amazon Route 53：数量：1个 Hosted Zone。"
    original = ServiceRequirement(
        service="route53",
        calculator_service_name="Amazon Route 53",
        component_key="cmp_route53",
        source_text=source,
        original_source_text=source,
    )
    baseline = original.model_copy(
        update={
            "requirements": {"hosted_zones": 1},
            "field_sources": {"requirements.hosted_zones": "customer_text"},
            "field_evidence": {
                "requirements.hosted_zones": "数量：1个 Hosted Zone"
            },
            "locked_fields": ["requirements.hosted_zones"],
        },
        deep=True,
    )
    candidate = original.model_copy(
        update={
            # Simulate a second AI pass deleting the product dimension and
            # reusing its literal evidence as the component deployment count.
            "quantity": 1,
            "requirements": {},
            "field_sources": {"quantity": "customer_text"},
            "field_evidence": {"quantity": "数量：1个 Hosted Zone"},
            "locked_fields": ["quantity"],
        },
        deep=True,
    )

    merged = DeepSeekIntentParser._merge_monotonic_component_repair(
        original, baseline, candidate
    )

    assert merged.requirements["hosted_zones"] == 1
    assert merged.field_evidence["requirements.hosted_zones"] == (
        "数量：1个 Hosted Zone"
    )
    assert merged.field_sources["requirements.hosted_zones"] == "customer_text"
    assert "quantity" not in merged.field_sources
    assert "quantity" not in merged.field_evidence
    assert "quantity" not in merged.locked_fields


def test_monotonic_repair_adds_retention_without_deleting_verified_baseline_fields() -> None:
    source = "Amazon CloudWatch Logs：每月写入500GB，保留30天。"
    original = ServiceRequirement(
        service="cloudwatch",
        calculator_service_name="Amazon CloudWatch",
        component_key="cmp_cloudwatch",
        source_text=source,
        original_source_text=source,
    )
    baseline = original.model_copy(
        update={
            "requirements": {"log_ingestion_gib": 500},
            "field_sources": {"requirements.log_ingestion_gib": "customer_text"},
            "field_evidence": {"requirements.log_ingestion_gib": "每月写入500GB"},
            "locked_fields": ["requirements.log_ingestion_gib"],
        },
        deep=True,
    )
    candidate = original.model_copy(
        update={
            # The repair found the one missing field but omitted the already
            # validated ingestion field from its fresh JSON response.
            "requirements": {"log_retention_days": 30},
            "field_sources": {"requirements.log_retention_days": "customer_text"},
            "field_evidence": {"requirements.log_retention_days": "保留30天"},
            "locked_fields": ["requirements.log_retention_days"],
        },
        deep=True,
    )

    merged = DeepSeekIntentParser._merge_monotonic_component_repair(
        original, baseline, candidate
    )

    assert merged.requirements == {
        "log_ingestion_gib": 500,
        "log_retention_days": 30,
    }
    assert merged.field_sources["requirements.log_ingestion_gib"] == "customer_text"
    assert merged.field_sources["requirements.log_retention_days"] == "customer_text"
    assert set(merged.locked_fields) >= {
        "requirements.log_ingestion_gib",
        "requirements.log_retention_days",
    }


def test_monotonic_repair_keeps_baseline_on_unproved_same_path_conflict() -> None:
    source = "Amazon S3：Standard 存储10TB。"
    original = ServiceRequirement(
        service="s3",
        calculator_service_name="Amazon S3",
        component_key="cmp_s3_storage",
        source_text=source,
        original_source_text=source,
    )
    baseline = original.model_copy(
        update={
            "requirements": {"storage_gib": 10240},
            "field_sources": {"requirements.storage_gib": "customer_text"},
            "field_evidence": {"requirements.storage_gib": "存储10TB"},
            "locked_fields": ["requirements.storage_gib"],
        },
        deep=True,
    )
    candidate = original.model_copy(
        update={
            "requirements": {"storage_gib": 5120},
            # A conflicting value without a literal customer snippet is not a
            # correction and must never override the verified baseline.
            "field_sources": {},
            "field_evidence": {},
            "locked_fields": [],
        },
        deep=True,
    )

    merged = DeepSeekIntentParser._merge_monotonic_component_repair(
        original, baseline, candidate
    )

    assert merged.requirements["storage_gib"] == 10240
    assert merged.field_sources["requirements.storage_gib"] == "customer_text"
    assert merged.field_evidence["requirements.storage_gib"] == "存储10TB"
    assert "requirements.storage_gib" in merged.locked_fields


def test_numbered_inventory_never_reclassifies_or_duplicates_derived_children() -> None:
    """A second inventory pass may re-key roots, but never reinterpret children."""

    source = (
        "1、Kubernetes 集群（EKS）：1套，Worker 节点5台，"
        "单台8核32G，磁盘500G。\n"
        "2、Amazon S3：Standard 存储10TB。"
    )
    parsed = DeepSeekIntentParser._intent_from_lossless_sales_numbering(source)
    assert parsed is not None

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)
    DeepSeekIntentParser._reconcile_explicit_component_inventory(source, parsed)
    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    roots = [item for item in parsed.services if not item.derived_from_service]
    workers = [item for item in parsed.services if item.derived_from_service]
    assert [item.service for item in roots] == ["eks", "s3"]
    assert len(workers) == 1
    assert workers[0].service == "ec2"
    assert workers[0].parent_component_key == roots[0].component_key
    assert workers[0].quantity == 5
    assert workers[0].requirements["vcpu"] == 8
    assert workers[0].requirements["memory_gib"] == 32
    assert workers[0].requirements["system_disk_gib"] == 500

    snapshot = [
        (
            item.component_key,
            item.parent_component_key,
            item.service,
            item.quantity,
            dict(item.requirements),
        )
        for item in parsed.services
    ]
    DeepSeekIntentParser._reconcile_explicit_component_inventory(source, parsed)
    DeepSeekIntentParser._split_eks_worker_nodes(parsed)
    assert [
        (
            item.component_key,
            item.parent_component_key,
            item.service,
            item.quantity,
            dict(item.requirements),
        )
        for item in parsed.services
    ] == snapshot
