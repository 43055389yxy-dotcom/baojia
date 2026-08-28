import asyncio
import re

import pytest

from app.core.config import Settings
from app.domain.customer_configuration import (
    aurora_cluster_member_count,
    preserve_customer_configuration,
)
from app.domain.models import ParsedIntent, ServiceRequirement, UnmappedPricingFact
from app.integrations.deepseek import (
    DeepSeekIntentParser,
    _component_prompt_cache_model,
)
from app.integrations.service_templates import SERVICE_TEMPLATE_FIELDS


def test_shared_literal_ledger_recovers_write_and_read_volumes() -> None:
    source = "每月写入约5TB数据，每月读取2TB"
    parsed = ParsedIntent(
        customer_summary=source,
        services=[
            ServiceRequirement(
                service="future_ingestion_service",
                source_text=source,
                original_source_text=source,
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(
        source,
        parsed,
        extra_fields=("data_in_gib", "data_out_gib"),
    )

    assert parsed.services[0].requirements["data_in_gib"] == 5120
    assert parsed.services[0].requirements["data_out_gib"] == 2048
    assert "requirements.data_in_gib" in parsed.services[0].locked_fields
    assert "requirements.data_out_gib" in parsed.services[0].locked_fields


def test_dynamic_literal_ledger_preserves_all_customer_pricing_facts() -> None:
    cases = (
        (
            "future_firehose",
            "每月摄入数据约10TB，目标端写入Amazon S3",
            ("data_in_gib",),
            {"data_in_gib": 10 * 1024},
        ),
        (
            "future_firewall",
            "部署2个防火墙Endpoint，每月处理流量约5TB",
            ("endpoint_count", "data_processed_gib"),
            {"endpoint_count": 2, "data_processed_gib": 5 * 1024},
        ),
        (
            "future_migration",
            "复制实例4核16GB，存储200GB，同时运行3个迁移任务",
            ("vcpu", "memory_gib", "storage_gib", "task_count"),
            {"vcpu": 4, "memory_gib": 16, "storage_gib": 200, "task_count": 3},
        ),
        (
            "future_time_series",
            "Timestream for LiveAnalytics每月写入约4亿条时序数据，"
            "内存存储保留24小时，磁性存储保留180天",
            (
                "product_variant", "write_records", "memory_retention_hours",
                "magnetic_retention_days",
            ),
            {
                "product_variant": "live_analytics",
                "write_records": 400_000_000,
                "memory_retention_hours": 24,
                "magnetic_retention_days": 180,
            },
        ),
    )

    for service, source, fields, expected in cases:
        component = ServiceRequirement(service=service, source_text=source)
        DeepSeekIntentParser._overlay_literal_component_facts(
            source,
            component,
            extra_fields=fields,
        )
        for field, value in expected.items():
            assert component.requirements[field] == value
            assert f"requirements.{field}" in component.locked_fields

    dms_component = ServiceRequirement(
        service="dms",
        source_text="复制实例4核16GB，同时运行3个迁移任务",
    )
    DeepSeekIntentParser._overlay_literal_component_facts(
        dms_component.source_text,
        dms_component,
    )
    assert dms_component.requirements["task_count"] == 3
    assert "replication_instances" not in dms_component.requirements


def test_kinesis_literal_ledger_preserves_mode_shards_and_monthly_write_volume() -> None:
    source = (
        "Amazon Kinesis Data Streams：数量1，Provisioned模式，"
        "配置12个Shard，每月写入数据约5TB"
    )
    parsed = ParsedIntent(
        customer_summary=source,
        services=[
            ServiceRequirement(
                service="kinesis",
                source_text=source,
                original_source_text=source,
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    requirements = parsed.services[0].requirements
    assert requirements["capacity_mode"] == "provisioned"
    assert requirements["shards"] == 12
    assert requirements["data_in_gib"] == 5120


@pytest.mark.asyncio
async def test_sales_region_preflight_uses_ai_to_understand_nonstandard_wording() -> None:
    class RegionGateway:
        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            assert "SG 主区" in str(kwargs["user_content"])
            assert "不分析产品" in str(kwargs["system_prompt"])
            return {
                "regions": ["ap-southeast-1"],
                "requires_confirmation": False,
                "reason": "SG 指新加坡",
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = RegionGateway()  # type: ignore[assignment]

    result = await parser.identify_sales_region("应用部署到 SG 主区")

    assert result["regions"] == ["ap-southeast-1"]
    assert result["requires_confirmation"] is False


@pytest.mark.asyncio
async def test_sales_region_preflight_rejects_invented_region_code() -> None:
    class InvalidRegionGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            return {
                "regions": ["moon-east-1"],
                "requires_confirmation": False,
                "reason": "invalid",
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = InvalidRegionGateway()  # type: ignore[assignment]

    result = await parser.identify_sales_region("部署到月球机房")

    assert result["regions"] == []
    assert result["requires_confirmation"] is True


@pytest.mark.asyncio
async def test_sales_region_preflight_accepts_location_first_heading_without_ai() -> None:
    class FailingRegionGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("明确的地区标题不应再调用 AI")

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = FailingRegionGateway()  # type: ignore[assignment]

    result = await parser.identify_sales_region(
        "新加坡地区\n1、4 vCPU｜16 GiB｜c7n.xla...｜Debian 12.0.0 64bit"
    )

    assert result == {
        "regions": ["ap-southeast-1"],
        "requires_confirmation": False,
        "reason": "客户原文已明确给出统一部署地区。",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "heading",
    [
        "俄罗斯地区",
        "地区：俄罗斯",
        "俄罗斯",
        "莫斯科区域",
        "北极地区",
        "巴西地区",
        "应用部署到俄罗斯",
        "地区：俄罗斯靠近法兰克福",
        "地区：eu-future-1",
    ],
)
async def test_sales_region_preflight_never_substitutes_an_unsupported_location(
    heading: str,
) -> None:
    class GuessingRegionGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("明确但不受支持的地点不应交给 AI 猜测附近区域")

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = GuessingRegionGateway()  # type: ignore[assignment]

    result = await parser.identify_sales_region(
        f"{heading}\n1、Amazon EC2：数量1，4核16GB"
    )

    assert result["regions"] == []
    assert result["requires_confirmation"] is True
    assert "必须" in str(result["reason"])


@pytest.mark.asyncio
async def test_every_current_official_region_code_is_accepted_from_the_live_allowlist() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    for code in parser.official_aws_region_labels():
        result = await parser.identify_sales_region(
            f"地区：{code}\n1、Amazon EC2：数量1，4核16GB"
        )
        assert result["regions"] == [code]
        assert result["requires_confirmation"] is False


@pytest.mark.asyncio
async def test_sales_region_preflight_accepts_one_region_per_numbered_component() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    result = await parser.identify_sales_region(
        "1、Amazon DocumentDB：区域ap-southeast-1（新加坡），数量1。\n"
        "2、Amazon Neptune：区域us-east-1（弗吉尼亚北部），数量1。\n"
        "3、Amazon FSx for OpenZFS：区域eu-central-1（法兰克福），数量1。"
    )

    assert result == {
        "regions": ["ap-southeast-1", "us-east-1", "eu-central-1"],
        "requires_confirmation": False,
        "reason": "每个编号组件都已明确填写可用的 AWS 区域。",
    }


@pytest.mark.asyncio
async def test_sales_region_preflight_asks_for_public_region_when_one_row_is_missing_it() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    result = await parser.identify_sales_region(
        "1、Amazon DocumentDB：区域ap-southeast-1（新加坡），数量1。\n"
        "2、Amazon Neptune：数量1。"
    )

    assert result["regions"] == []
    assert result["requires_confirmation"] is True


def test_literal_region_replay_never_overwrites_customer_confirmed_replacement() -> None:
    component = ServiceRequirement(
        service="amazon_timestream_for_liveanalytics",
        calculator_service_name="Amazon Timestream for LiveAnalytics",
        region="ap-south-1",
        source_text=(
            "Amazon Timestream for LiveAnalytics：区域eu-west-2（伦敦），数量1。"
        ),
        field_sources={"region": "customer_confirmation"},
        field_evidence={"region": "客户从该服务实际支持的 AWS 地区中选择"},
        locked_fields=["region"],
    )
    intent = ParsedIntent(customer_summary="Timestream", services=[component])

    DeepSeekIntentParser._reconcile_explicit_regions(component.source_text, intent)

    assert component.region == "ap-south-1"
    assert component.field_sources["region"] == "customer_confirmation"


@pytest.mark.asyncio
async def test_official_catalog_identity_precedes_closed_ai_service_classifier() -> None:
    class OfficialDiscovery:
        @staticmethod
        def resolve_official_product(*labels: str) -> dict[str, object] | None:
            assert labels == ("Amazon Neptune",)
            return {
                "service_code": "AmazonNeptune",
                "service_key": "neptune",
                "display_name": "AmazonNeptune",
                "aliases": ["Amazon Neptune", "Neptune"],
            }

    class FailingGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("官方目录已命中时不应再让固定列表 AI 猜服务")

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=OfficialDiscovery(),  # type: ignore[arg-type]
    )
    parser._gateway = FailingGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2（自建 Amazon Neptune）",
        source_text=(
            "Amazon Neptune：数量1，1个Writer节点+2个Reader节点，"
            "单节点8核32GB，实例规格db.r6g.large"
        ),
        requirements={"vcpu": 8, "memory_gib": 32, "operating_system": "linux"},
        field_sources={
            "_pending_architecture_decision": "system_policy",
            "requirements.operating_system": "system_minimum",
        },
    )

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert component.service == "neptune"
    assert component.calculator_service_name == "Amazon Neptune"
    assert component.product_identity == "AmazonNeptune"
    assert component.field_sources["_official_service_code"] == "AmazonNeptune"
    assert "_pending_architecture_decision" not in component.field_sources
    assert "operating_system" not in component.requirements

    parsed = ParsedIntent(customer_summary="Neptune", services=[component])
    parser._reconcile_explicit_models(component.source_text, parsed)
    parser._reconcile_explicit_service_architecture(component.source_text, parsed)
    parser._append_third_party_managed_decisions(parsed, component.source_text)
    assert parsed.services[0].service == "neptune"
    assert parsed.services[0].requirements["requested_model"] == "db.r6g.large"
    assert parsed.services[0].requirements["writer_nodes"] == 1
    assert parsed.services[0].requirements["reader_nodes"] == 2
    assert parsed.services[0].requirements["instance_count"] == 3
    assert parsed.ambiguities == []


@pytest.mark.asyncio
async def test_comma_delimited_s3_heading_resolves_against_official_directory() -> None:
    source = (
        "S3，容量15T，预估费用4608美元，替换OSS，"
        "用于冷数据存储、Flink快照、业务设备图片"
    )

    class OfficialDiscovery:
        @staticmethod
        def resolve_official_product(*labels: str) -> dict[str, object] | None:
            assert labels == ("S3",)
            return {
                "service_code": "AmazonS3",
                "service_key": "s3",
                "display_name": "AmazonS3",
                "aliases": ["Amazon S3", "S3"],
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=OfficialDiscovery(),  # type: ignore[arg-type]
    )
    component = ServiceRequirement(
        service="s3_capacity15t_4608_usd",
        calculator_service_name=source,
        source_text=source,
    )

    assert parser._component_product_heading(component) == "S3"
    assert parser._self_hosted_product_name(component) is None

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert component.service == "s3"
    assert component.calculator_service_name == "Amazon Simple Storage Service (S3)"
    assert component.field_sources["_official_service_code"] == "AmazonS3"


def test_reference_quote_money_is_not_an_unmapped_pricing_dimension() -> None:
    component = ServiceRequirement(
        service="s3",
        source_text="S3，容量15T，预估费用4608美元",
        unmapped_pricing_facts=[
            UnmappedPricingFact(
                field_hint="客户预估费用",
                value=4608,
                unit="USD",
                evidence="预估费用4608美元",
            )
        ],
    )

    DeepSeekIntentParser._validate_unmapped_pricing_facts(
        component,
        source_text=component.source_text,
    )

    assert component.unmapped_pricing_facts == []


@pytest.mark.asyncio
async def test_official_offer_code_is_routed_to_existing_dms_adapter() -> None:
    class OfficialDiscovery:
        @staticmethod
        def resolve_official_product(*labels: str) -> dict[str, object] | None:
            assert labels == ("AWS Database Migration Svc",)
            return {
                "service_code": "AWSDatabaseMigrationSvc",
                "service_key": "database_migration_svc",
                "display_name": "AWSDatabaseMigrationSvc",
                "aliases": ["AWS Database Migration Service", "AWS DMS"],
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=OfficialDiscovery(),  # type: ignore[arg-type]
    )
    component = ServiceRequirement(
        service="database_migration_svc",
        calculator_service_name="AWS Database Migration Svc",
        source_text=(
            "AWS Database Migration Svc：复制节点4核16GB、200GB存储，"
            "同时运行3个迁移任务。"
        ),
    )

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert component.service == "dms"
    assert component.product_identity == "AWSDatabaseMigrationSvc"


@pytest.mark.asyncio
async def test_known_generic_service_loads_official_field_profile_before_ai_cleanup() -> None:
    class RecordingDiscovery:
        calls: list[tuple[str, str, str | None]] = []

        @classmethod
        def ensure_profile(
            cls, *, service_key: str, display_name: str, region: str | None
        ) -> dict[str, object]:
            cls.calls.append((service_key, display_name, region))
            return {
                "status": "verified",
                "service_code": "AWSLambda",
                "fields": ["requests", "duration_ms", "memory_mb"],
                "field_bindings": [],
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=RecordingDiscovery(),  # type: ignore[arg-type]
    )
    component = ServiceRequirement(
        service="lambda",
        calculator_service_name="AWS Lambda",
        region="ap-southeast-1",
        source_text="Lambda 每月调用 100 万次",
    )

    profile = await parser._auto_discover_component(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert profile is not None and profile["status"] == "verified"
    assert RecordingDiscovery.calls == [
        ("lambda", "AWS Lambda", "ap-southeast-1")
    ]


@pytest.mark.asyncio
async def test_dedicated_adapter_does_not_download_redundant_field_profile() -> None:
    class FailingDiscovery:
        @staticmethod
        def ensure_profile(**_: object) -> dict[str, object]:
            raise AssertionError("专用适配器不得重复建立通用字段档案")

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=FailingDiscovery(),  # type: ignore[arg-type]
    )

    profile = await parser._auto_discover_component(
        ServiceRequirement(service="ec2", source_text="EC2 4核16GB"),
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert profile is None


def test_usage_semantics_select_timestream_liveanalytics_without_marketing_name() -> None:
    source = "时序数据：每月新增约2亿条，历史数据保留180天。"
    parsed = ParsedIntent(
        customer_summary=source,
        services=[
            ServiceRequirement(
                service="timestream",
                calculator_service_name="Amazon Timestream",
                source_text=source,
                requirements={"write_records": 200_000_000, "magnetic_retention_days": 180},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    item = parsed.services[0]
    assert item.requirements["product_variant"] == "live_analytics"
    assert item.field_sources["requirements.product_variant"] == "system_derived"


def test_efs_throughput_and_backup_usage_are_not_dropped_by_literal_recovery() -> None:
    efs_source = "共享文件存储：容量约8TB，期望吞吐量不低于500MB/s。"
    backup_source = "备份数据总量约10TB，保留30天，其中3TB复制到另一个AWS区域。"
    parsed = ParsedIntent(
        customer_summary=f"{efs_source}\n{backup_source}",
        services=[
            ServiceRequirement(service="efs", source_text=efs_source),
            ServiceRequirement(service="backup", source_text=backup_source),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(parsed.customer_summary, parsed)

    assert parsed.services[0].requirements == {
        "storage_gib": 8192,
        "provisioned_throughput_mibps": 500,
        "throughput_mode": "provisioned",
    }
    assert parsed.services[1].requirements["backup_storage_gib"] == 10240
    assert parsed.services[1].requirements["backup_retention_days"] == 30
    assert parsed.services[1].requirements["cross_region_copy_gib"] == 3072


def test_efs_component_template_preserves_class_region_and_read_write_usage() -> None:
    source = (
        "Amazon EFS：新加坡，EFS Standard（Regional），容量6TB，"
        "每月读取12TB、写入5TB。"
    )
    parsed = ParsedIntent(
        customer_summary=source,
        services=[ServiceRequirement(service="efs", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    requirements = parsed.services[0].requirements
    assert requirements["storage_gib"] == 6144
    assert requirements["storage_class"] == "standard"
    assert requirements["deployment_type"] == "regional"
    assert requirements["data_out_gib"] == 12288
    assert requirements["data_in_gib"] == 5120


def test_documentdb_node_count_and_labelled_disk_do_not_get_confused_with_memory() -> None:
    source = (
        "MongoDB集群：现网3节点，单节点4核32GB，单节点数据盘500GB，"
        "迁云后优先考虑托管方案。"
    )
    parsed = ParsedIntent(
        customer_summary=source,
        services=[ServiceRequirement(service="documentdb", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    assert parsed.services[0].requirements == {
        "vcpu": 4,
        "memory_gib": 32,
        "storage_gib": 500,
        "instance_count": 3,
    }


def test_numbered_unknown_heading_owns_row_instead_of_destination_service() -> None:
    source = (
        "5、Amazon Data Firehose：区域ap-southeast-2（悉尼），数量1，"
        "每月摄入数据约10TB，目标端写入Amazon S3。"
    )

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(source)

    assert parsed is not None
    assert len(parsed.services) == 1
    assert parsed.services[0].service == "amazon_data_firehose"
    assert parsed.services[0].calculator_service_name == "Amazon Data Firehose"
    assert "Amazon S3" in parsed.services[0].source_text


def test_numbered_conversational_row_does_not_promote_origin_to_second_component() -> None:
    source = (
        "7、静态资源和下载文件要做CDN，用户主要在东南亚，每月下行8TB，"
        "源站是上面的Amazon S3对象存储"
    )

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(source)

    assert parsed is not None
    assert [item.service for item in parsed.services] == ["cloudfront"]


@pytest.mark.asyncio
async def test_ai_selects_renamed_main_service_from_official_candidates() -> None:
    class CandidateDiscovery:
        @staticmethod
        def resolve_official_product(*labels: str) -> None:
            assert labels == ("Amazon Data Firehose",)
            return None

        @staticmethod
        def candidate_official_products(
            *labels: str,
            limit: int = 12,
        ) -> list[dict[str, object]]:
            assert labels == ("Amazon Data Firehose",)
            assert limit == 12
            return [
                {
                    "service_code": "AmazonKinesisFirehose",
                    "service_key": "kinesis_firehose",
                    "display_name": "AmazonKinesisFirehose",
                    "aliases": ["Amazon Kinesis Firehose", "Kinesis Firehose"],
                },
                {
                    "service_code": "AmazonS3",
                    "service_key": "s3",
                    "display_name": "AmazonS3",
                    "aliases": ["Amazon S3", "S3"],
                },
            ]

    class MainServiceGateway:
        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            prompt = str(kwargs["system_prompt"])
            assert "目标端" in prompt
            assert "AmazonKinesisFirehose" in prompt
            assert "AmazonS3" in prompt
            # Existing business-language markers are context for AI, not a
            # second hard-coded product classifier.
            assert "对象存储" in prompt
            assert "不是子串匹配规则" in prompt
            return {
                "service_code": "AmazonKinesisFirehose",
                "confidence": "high",
            }

    source = (
        "Amazon Data Firehose：区域ap-southeast-2（悉尼），数量1，"
        "每月摄入数据约10TB，目标端写入Amazon S3。"
    )
    component = ServiceRequirement(
        service="amazon_data_firehose",
        calculator_service_name="Amazon Data Firehose",
        source_text=source,
        original_source_text=source,
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=CandidateDiscovery(),  # type: ignore[arg-type]
    )
    parser._gateway = MainServiceGateway()  # type: ignore[assignment]

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert component.service == "kinesis_firehose"
    assert component.calculator_service_name == "Amazon Data Firehose"
    assert component.product_identity == "AmazonKinesisFirehose"
    assert component.field_sources["_official_service_code"] == "AmazonKinesisFirehose"

    parsed = ParsedIntent(customer_summary="Firehose", services=[component])
    parser._reconcile_explicit_component_inventory(source, parsed)
    assert len(parsed.services) == 1
    assert parsed.services[0].service == "kinesis_firehose"


@pytest.mark.asyncio
async def test_ai_can_resolve_unseen_customer_wording_from_full_official_directory() -> None:
    class FullDirectoryDiscovery:
        @staticmethod
        def resolve_official_product(*labels: str) -> None:
            assert labels == ("实时投递管道",)
            return None

        @staticmethod
        def candidate_official_products(
            *labels: str,
            limit: int = 12,
        ) -> list[dict[str, object]]:
            assert labels == ("实时投递管道",)
            assert limit == 12
            return []

        @staticmethod
        def official_products(*, limit: int = 500) -> list[dict[str, object]]:
            assert limit == 500
            return [
                {
                    "service_code": "AmazonKinesisFirehose",
                    "service_key": "kinesis_firehose",
                    "display_name": "AmazonKinesisFirehose",
                    "aliases": ["Amazon Kinesis Firehose", "Kinesis Firehose"],
                },
                {
                    "service_code": "AmazonS3",
                    "service_key": "s3",
                    "display_name": "AmazonS3",
                    "aliases": ["Amazon S3", "S3"],
                },
            ]

    class FullDirectoryGateway:
        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            assert "实时投递管道" in str(kwargs["user_content"])
            assert "不是功能推荐" in str(kwargs["system_prompt"])
            return {
                "service_code": "AmazonKinesisFirehose",
                "confidence": "high",
            }

    source = "实时投递管道：每月摄入10TB，目标端写入Amazon S3"
    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(f"1、{source}")
    assert parsed is not None
    assert len(parsed.services) == 1
    assert parsed.services[0].service.startswith("unknown_component_")

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=FullDirectoryDiscovery(),  # type: ignore[arg-type]
    )
    parser._gateway = FullDirectoryGateway()  # type: ignore[assignment]
    await parser._resolve_unknown_component_service(
        parsed.services[0],
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert parsed.services[0].service == "kinesis_firehose"
    assert parsed.services[0].product_identity == "AmazonKinesisFirehose"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("heading", "source", "returned_identity", "service_code", "service_key"),
    [
        (
            "共享文件存储",
            "共享文件存储：容量约8TB，期望吞吐量不低于500MB/s。",
            "efs",
            "AmazonEFS",
            "efs",
        ),
        (
            "时序数据",
            "时序数据：每月新增约2亿条，历史数据保留180天。",
            "Amazon Timestream",
            "AmazonTimestream",
            "timestream",
        ),
    ],
)
async def test_generic_customer_heading_accepts_unique_official_identity_spelling(
    heading: str,
    source: str,
    returned_identity: str,
    service_code: str,
    service_key: str,
) -> None:
    """A correct AI decision must not be discarded for using key/display spelling."""

    class FullDirectoryDiscovery:
        @staticmethod
        def resolve_official_product(*_labels: str) -> None:
            return None

        @staticmethod
        def candidate_official_products(
            *_labels: str,
            limit: int = 12,
        ) -> list[dict[str, object]]:
            assert limit == 12
            return []

        @staticmethod
        def official_products(*, limit: int = 500) -> list[dict[str, object]]:
            assert limit == 500
            return [
                {
                    "service_code": service_code,
                    "service_key": service_key,
                    "display_name": service_code,
                    "aliases": [
                        service_code,
                        "Amazon EFS" if service_key == "efs" else "Amazon Timestream",
                    ],
                },
                {
                    "service_code": "AmazonS3",
                    "service_key": "s3",
                    "display_name": "AmazonS3",
                    "aliases": ["Amazon S3"],
                },
            ]

    class ServiceKeyGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            # Models sometimes use another unique identity from the closed
            # candidate row even when the requested JSON key says service_code.
            return {"service": returned_identity, "confidence": "high"}

    component = ServiceRequirement(
        service="unknown_component_generic",
        calculator_service_name=heading,
        source_text=source,
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=FullDirectoryDiscovery(),  # type: ignore[arg-type]
    )
    parser._gateway = ServiceKeyGateway()  # type: ignore[assignment]

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert component.service == service_key
    assert component.product_identity == service_code
    assert component.field_sources.get("_identity_resolution_status") is None


@pytest.mark.asyncio
async def test_full_directory_identity_validation_retries_only_failed_component() -> None:
    class FullDirectoryDiscovery:
        @staticmethod
        def resolve_official_product(*_labels: str) -> None:
            return None

        @staticmethod
        def candidate_official_products(
            *_labels: str,
            limit: int = 12,
        ) -> list[dict[str, object]]:
            return []

        @staticmethod
        def official_products(*, limit: int = 500) -> list[dict[str, object]]:
            return [
                {
                    "service_code": "AmazonEFS",
                    "service_key": "efs",
                    "display_name": "AmazonEFS",
                    "aliases": ["Amazon EFS"],
                }
            ]

    class RetryGateway:
        calls = 0

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                return {"service_code": "共享文件存储", "confidence": "low"}
            assert "上一次没有返回可验证的唯一官方产品" in str(kwargs["system_prompt"])
            return {"service_code": "AmazonEFS", "confidence": "high"}

    component = ServiceRequirement(
        service="unknown_component_storage",
        calculator_service_name="共享文件存储",
        source_text="共享文件存储：容量8TB。",
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=FullDirectoryDiscovery(),  # type: ignore[arg-type]
    )
    gateway = RetryGateway()
    parser._gateway = gateway  # type: ignore[assignment]

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert gateway.calls == 2
    assert component.service == "efs"
    assert component.product_identity == "AmazonEFS"


@pytest.mark.asyncio
async def test_wrong_lexical_shortlist_falls_back_to_full_official_directory() -> None:
    remembered: list[tuple[str, str]] = []

    class RenamedDiscovery:
        @staticmethod
        def resolve_official_product(*_labels: str) -> None:
            return None

        @staticmethod
        def candidate_official_products(
            *_labels: str,
            limit: int = 12,
        ) -> list[dict[str, object]]:
            assert limit == 12
            return [
                {
                    "service_code": "AWSManagedServices",
                    "service_key": "managed_services",
                    "display_name": "AWSManagedServices",
                    "aliases": ["AWS Managed Services"],
                }
            ]

        @staticmethod
        def official_products(*, limit: int = 500) -> list[dict[str, object]]:
            assert limit == 500
            return [
                {
                    "service_code": "AWSManagedServices",
                    "service_key": "managed_services",
                    "display_name": "AWSManagedServices",
                    "aliases": ["AWS Managed Services"],
                },
                {
                    "service_code": "AmazonKinesisAnalytics",
                    "service_key": "kinesis_analytics",
                    "display_name": "AmazonKinesisAnalytics",
                    "aliases": ["Amazon Kinesis Analytics"],
                },
            ]

        @staticmethod
        def remember_official_alias(service_code: str, alias: str) -> None:
            remembered.append((service_code, alias))

    class RenamedGateway:
        calls = 0

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            self.calls += 1
            prompt = str(kwargs["system_prompt"])
            if self.calls == 1:
                assert "AmazonKinesisAnalytics" not in prompt
                return {"service_code": "unknown", "confidence": "low"}
            assert "AmazonKinesisAnalytics" in prompt
            return {
                "service_code": "AmazonKinesisAnalytics",
                "confidence": "high",
            }

    source = "Amazon Managed Service for Apache Flink：持续运行，配置4个KPU"
    component = ServiceRequirement(
        service="amazon_managed_service_for_apache_flink",
        calculator_service_name="Amazon Managed Service for Apache Flink",
        source_text=source,
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=RenamedDiscovery(),  # type: ignore[arg-type]
    )
    gateway = RenamedGateway()
    parser._gateway = gateway  # type: ignore[assignment]

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert gateway.calls == 2
    assert component.service == "kinesis_analytics"
    assert component.product_identity == "AmazonKinesisAnalytics"
    assert remembered == [
        (
            "AmazonKinesisAnalytics",
            "Amazon Managed Service for Apache Flink",
        )
    ]


@pytest.mark.asyncio
async def test_heading_is_rechecked_when_ai_mistakes_dependency_for_main_service() -> None:
    class FlinkDiscovery:
        @staticmethod
        def resolve_official_product(*_labels: str) -> None:
            return None

        @staticmethod
        def candidate_official_products(
            *_labels: str,
            limit: int = 12,
        ) -> list[dict[str, object]]:
            assert limit == 12
            return [
                {
                    "service_code": "AmazonKinesisAnalytics",
                    "service_key": "kinesis_analytics",
                    "display_name": "AmazonKinesisAnalytics",
                    "aliases": ["Amazon Managed Service for Apache Flink"],
                },
                {
                    "service_code": "AmazonMSK",
                    "service_key": "msk",
                    "display_name": "AmazonMSK",
                    "aliases": ["Amazon Managed Streaming for Apache Kafka"],
                },
            ]

    class FlinkGateway:
        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            assert "主 AWS 服务" in str(kwargs["system_prompt"])
            assert "目标端" in str(kwargs["system_prompt"])
            assert "Flink实时计算" in str(kwargs["user_content"])
            return {
                "service_code": "AmazonKinesisAnalytics",
                "confidence": "high",
            }

    component = ServiceRequirement(
        # Simulate the first AI pass incorrectly treating the referenced Kafka
        # source as the purchased service.
        service="msk",
        calculator_service_name="Amazon MSK",
        source_text=(
            "Flink实时计算：当前主要用于Kafka流数据处理，预计3个计算节点，"
            "单节点8核32GB，任务需要7×24小时运行。"
        ),
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=FlinkDiscovery(),  # type: ignore[arg-type]
    )
    parser._gateway = FlinkGateway()  # type: ignore[assignment]

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert component.service == "kinesis_analytics"
    assert component.product_identity == "AmazonKinesisAnalytics"


@pytest.mark.asyncio
async def test_official_versioned_product_survives_second_inventory_pass() -> None:
    class OfficialDiscovery:
        @staticmethod
        def resolve_official_product(*labels: str) -> dict[str, object] | None:
            assert labels == ("Amazon AppStream 2.0",)
            return {
                "service_code": "AmazonAppStream",
                "service_key": "app_stream",
                "display_name": "AmazonAppStream",
                "aliases": ["Amazon AppStream", "AppStream"],
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=OfficialDiscovery(),  # type: ignore[arg-type]
    )
    component_text = (
        "5、Amazon AppStream 2.0：数量1，常驻用户约80人，"
        "每人每天使用6小时，实例配置4核16GB"
    )
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2（自建 Amazon AppStream 2.0）",
        source_text=component_text,
        original_source_text=component_text,
        requirements={"vcpu": 4, "memory_gib": 16, "operating_system": "linux"},
        field_sources={"_pending_architecture_decision": "system_policy"},
    )

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )
    parsed = ParsedIntent(customer_summary="AppStream", services=[component])
    parser._reconcile_explicit_component_inventory(component_text, parsed)
    parser._append_third_party_managed_decisions(parsed, component_text)
    parser._reconcile_explicit_capacities(component_text, parsed)

    assert len(parsed.services) == 1
    assert parsed.services[0].service == "app_stream"
    assert parsed.services[0].field_sources["_official_service_code"] == "AmazonAppStream"
    assert "自建" not in (parsed.services[0].calculator_service_name or "")
    assert parsed.services[0].requirements["user_count"] == 80
    assert parsed.services[0].requirements["hours_per_user_per_day"] == 6
    assert parsed.services[0].requirements["vcpu"] == 4
    assert parsed.services[0].requirements["memory_gib"] == 16
    assert parsed.ambiguities == []


@pytest.mark.asyncio
async def test_official_heading_removed_by_inventory_is_restored_before_classification() -> None:
    class OfficialDiscovery:
        @staticmethod
        def resolve_official_product(*labels: str) -> dict[str, object] | None:
            assert labels == ("Amazon Neptune",)
            return {
                "service_code": "AmazonNeptune",
                "service_key": "neptune",
                "display_name": "AmazonNeptune",
                "aliases": ["Amazon Neptune"],
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=OfficialDiscovery(),  # type: ignore[arg-type]
    )
    full_source = (
        "Amazon Neptune：数量1，1个Writer节点+2个Reader节点，"
        "单节点8核32GB，实例规格db.r6g.large"
    )
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2 云服务器",
        source_text="数量1，1个Writer节点+2个Reader节点，单节点8核32GB，实例规格db.r6g.large",
    )
    parsed = ParsedIntent(customer_summary="test", services=[component])

    parser._restore_literal_official_headings(full_source, parsed)
    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert component.source_text == full_source
    assert component.original_source_text == full_source
    assert component.service == "neptune"
    assert component.calculator_service_name == "Amazon Neptune"
    assert component.field_sources["_official_service_code"] == "AmazonNeptune"


@pytest.mark.parametrize(
    "request_text",
    [
        "Amazon Neptune：数量1，1个Writer节点+2个Reader节点，单节点8核32GB",
        "Amazon DocumentDB：数量1，3个数据库节点，单节点4核16GB",
        "Amazon MQ for RabbitMQ：数量1，3个Broker节点，单节点4核16GB",
    ],
)
def test_unnumbered_official_service_quantity_is_not_mistaken_for_row_number(
    request_text: str,
) -> None:
    """The guard is provider-wide; ``数量1`` can never erase a service name."""

    assert DeepSeekIntentParser._numbered_requirement_blocks(request_text) == []


def test_embedded_note_before_real_numbered_service_is_still_supported() -> None:
    assert DeepSeekIntentParser._numbered_requirement_blocks(
        "补充说明1、Amazon EC2：数量2，单台4核16GB"
    ) == ["Amazon EC2：数量2，单台4核16GB"]


def test_generic_official_component_recovers_separate_system_and_user_volumes() -> None:
    source = (
        "Amazon WorkSpaces：数量50，单用户配置2核8GB/80GB系统盘/"
        "50GB用户盘，按月计费"
    )
    component = ServiceRequirement(
        service="work_spaces",
        calculator_service_name="Amazon WorkSpaces",
        source_text=source,
        original_source_text=source,
        requirements={},
    )
    parsed = ParsedIntent(customer_summary=source, services=[component])

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    assert component.quantity == 50
    assert component.requirements["vcpu"] == 2
    assert component.requirements["memory_gib"] == 8
    assert component.requirements["system_disk_gib"] == 80
    assert component.requirements["user_volume_gib"] == 50
    assert component.field_sources["requirements.system_disk_gib"] == "customer_text"
    assert component.field_sources["requirements.user_volume_gib"] == "customer_text"


def test_discovered_official_service_preserves_labelled_instance_model() -> None:
    from app.domain.customer_facts import explicit_requested_model

    result = explicit_requested_model(
        "neptune",
        "Amazon Neptune：实例规格db.r6g.large，1个Writer+2个Reader",
    )

    assert result is not None
    assert result[0] == "db.r6g.large"


def test_component_cache_key_is_isolated_by_active_service_prompt() -> None:
    ec2_key = _component_prompt_cache_model("deepseek-chat", "ec2", "EC2 m6i.xlarge 4C16G")
    rds_key = _component_prompt_cache_model("deepseek-chat", "rds", "RDS MySQL db.t3.large")

    assert ec2_key is not None
    assert rds_key is not None
    assert ec2_key != rds_key
    assert ec2_key == _component_prompt_cache_model("deepseek-chat", "ec2", "EC2 m6i.xlarge 4C16G")


class RepairingGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, **_: object) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            return {
                "customer_summary": "Redis 报价",
                "services": [
                    {
                        "service": "elasticache",
                        "calculator_service_name": "Amazon ElastiCache",
                        "quantity": "两台",
                        "requirements": {"engine": "redis", "memory_gib": 8},
                    }
                ],
                "ambiguities": [],
            }
        return {
            "customer_summary": "Redis 报价",
            "services": [
                {
                    "service": "elasticache",
                    "calculator_service_name": "Amazon ElastiCache",
                    "quantity": 2,
                    "requirements": {"engine": "redis", "memory_gib": 8},
                    "source_text": "Redis 一主一从，每节点 8 GiB",
                    "query_action": None,
                }
            ],
            "ambiguities": [],
        }


class RepairStillMissingServiceGateway(RepairingGateway):
    """The repair succeeds structurally but still omits an explicit service."""


class MissingSummaryGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, **_: object) -> dict[str, object]:
        self.calls += 1
        return {
            "services": [
                {
                    "service": "elasticache",
                    "calculator_service_name": "Amazon ElastiCache",
                    "quantity": 2,
                    "requirements": {"engine": "redis", "memory_gib": 8},
                    "source_text": "Redis 一主一从，每节点 8 GiB",
                }
            ],
            "ambiguities": [],
        }


class CapturingWorkloadGateway(MissingSummaryGateway):
    def __init__(self) -> None:
        super().__init__()
        self.system_prompts: list[str] = []
        self.user_contents: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.system_prompts.append(str(kwargs.get("system_prompt", "")))
        self.user_contents.append(str(kwargs.get("user_content", "")))
        return await super().complete_json(**kwargs)


class NumberedCleaningGateway:
    def __init__(self) -> None:
        self.calls = 0
        self.system_prompts: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        self.system_prompts.append(str(kwargs.get("system_prompt", "")))
        return {
            "customer_summary": "应用服务器报价配置",
            "services": [
                {
                    "service": "ec2",
                    "calculator_service_name": "Amazon EC2",
                    "component_key": "cmp_source_0001",
                    "region": None,
                    "quantity": 3,
                    "hours_per_month": 730,
                    "requirements": {
                        "vcpu": 8,
                        "memory_gib": 32,
                        "system_disk_gib": 200,
                        "additional_ebs_volumes": [
                            {"size_gib": 500, "volume_type": "gp3", "count_per_instance": 1}
                        ],
                    },
                    "source_text": (
                        "应用服务器（Amazon EC2）｜数量：3台｜每台CPU：8核｜"
                        "每台内存：32GB｜每台系统盘：200GB｜每台数据盘：500GB"
                    ),
                    "query_action": None,
                }
            ],
            "ambiguities": [],
        }


class ComponentCorrectionGateway:
    def __init__(self) -> None:
        self.user_contents: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.user_contents.append(str(kwargs.get("user_content", "")))
        return {
            "component": {
                "service": "s3",
                "calculator_service_name": "Amazon S3",
                "region": "ap-southeast-1",
                "quantity": 1,
                "hours_per_month": 730,
                "requirements": {
                    "storage_gib": 30720,
                    "storage_class": "standard",
                },
                "field_evidence": {
                    "requirements.storage_gib": "S3 容量改为 30TB",
                },
                "source_text": "S3 30TB",
                "query_action": None,
            }
        }


class ComponentFieldRepairGateway:
    def __init__(self) -> None:
        self.calls = 0
        self.user_contents: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        self.user_contents.append(str(kwargs.get("user_content", "")))
        field = "monthly_accelerated_traffic_gb" if self.calls == 1 else "data_transfer_out_gib"
        return {
            "component": {
                "service": "global_accelerator",
                "calculator_service_name": "AWS Global Accelerator",
                "region": "global",
                "quantity": 1,
                "hours_per_month": 730,
                "requirements": {"accelerators": 1, field: 1000},
                "field_evidence": {
                    "requirements.accelerators": "1个加速器",
                    f"requirements.{field}": "每月加速流量1000GB",
                },
                "source_text": "配置1个加速器，每月加速流量1000GB",
                "query_action": None,
            }
        }


class UnchangedEc2CorrectionGateway:
    async def complete_json(self, **_: object) -> dict[str, object]:
        # Simulate a model that overlooks the purchase-plan sentence. The
        # deterministic closed-vocabulary reconciliation must still apply it.
        return {
            "component": {
                "service": "ec2",
                "calculator_service_name": "Amazon EC2",
                "region": "ap-southeast-1",
                "quantity": 2,
                "hours_per_month": 730,
                "requirements": {
                    "requested_model": "c6g.xlarge",
                    "vcpu": 4,
                    "memory_gib": 8,
                    "operating_system": "linux",
                    "purchase_option": "on_demand",
                },
                "field_evidence": {},
                "source_text": "2台 EC2 c6g.xlarge，按需付费",
                "query_action": None,
            }
        }


class UnchangedComponentCorrectionGateway:
    """Simulate a valid response that ignores the customer's latest edit."""

    def __init__(self, component: ServiceRequirement) -> None:
        self.component = component
        self.calls = 0

    async def complete_json(self, **_: object) -> dict[str, object]:
        self.calls += 1
        return {
            "component": {
                "service": self.component.service,
                "calculator_service_name": self.component.calculator_service_name,
                "region": self.component.region,
                "quantity": self.component.quantity,
                "hours_per_month": self.component.hours_per_month,
                "requirements": dict(self.component.requirements),
                "field_evidence": {},
                "source_text": self.component.source_text,
                "query_action": None,
            }
        }


@pytest.mark.asyncio
async def test_component_feedback_sends_only_the_changed_component_to_ai() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = ComponentCorrectionGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    component = ServiceRequirement(
        service="s3",
        calculator_service_name="Amazon S3",
        region="ap-southeast-1",
        quantity=1,
        requirements={"storage_gib": 20480, "storage_class": "standard"},
        source_text="S3 20TB",
    )

    revised = await parser.revise_component_from_feedback(
        "EC2 4 台；RDS MySQL 1 套；S3 20TB",
        component,
        "S3 容量改为 30TB",
    )

    assert revised.requirements["storage_gib"] == 30720
    assert revised.service == "s3"
    # Simple single-field edits need one isolated template pass; a second
    # network audit is reserved for related fields such as per-node vs total.
    assert len(gateway.user_contents) == 1
    assert "RDS MySQL" not in gateway.user_contents[0]
    assert "EC2 4 台" not in gateway.user_contents[0]
    assert "客户最新修改（最高优先级）" in gateway.user_contents[0]
    assert "该组件当前完整旧配置（只用于补全客户没有修改的字段）" in gateway.user_contents[0]
    assert "该组件客户历史原话（只用于核对来源）" in gateway.user_contents[0]
    assert "当前旧配置" not in gateway.user_contents[0]


def test_component_feedback_uses_only_configured_stable_ai() -> None:
    parser = DeepSeekIntentParser(
        Settings(
            ai_provider="bedrock",
            bedrock_api_key="test",
            bedrock_model="zai.glm-4.7-flash",
            component_revision_model="deepseek.v3.2",
        )
    )
    gateways = parser._component_ai_gateways()

    assert len(gateways) == 1
    assert gateways[0]._settings.ai_model == "deepseek.v3.2"


def test_service_identity_uses_independent_configured_ai_routes() -> None:
    parser = DeepSeekIntentParser(
        Settings(
            ai_provider="bedrock",
            bedrock_api_key="bedrock-test",
            bedrock_model="deepseek.v3.2",
            deepseek_api_key="deepseek-test",
            deepseek_model="deepseek-chat",
        )
    )

    gateways = parser._service_identity_gateways()

    assert [gateway._settings.ai_model for gateway in gateways] == [
        "deepseek.v3.2",
        "deepseek-chat",
        "zai.glm-4.7-flash",
    ]


def test_legacy_official_amazon_es_identity_routes_to_opensearch() -> None:
    assert DeepSeekIntentParser._service_key("AmazonES") == "opensearch"
    assert DeepSeekIntentParser._service_key("ElasticsearchService") == "opensearch"


def test_failed_product_identity_is_not_rewritten_as_self_hosted_ec2() -> None:
    component = ServiceRequirement(
        service="unknown_component_search",
        calculator_service_name="日志检索",
        source_text=(
            "日志检索：计划部署3个数据节点，单节点8核32GB，"
            "单节点存储500GB，日志保留约30天。"
        ),
        requirements={"vcpu": 8, "memory_gib": 32, "storage_gib": 500},
        field_sources={
            "_identity_resolution_status": "failed",
            "_identity_resolution_reason": "服务名称识别线路暂时无法连接",
        },
    )
    parsed = ParsedIntent(customer_summary="日志检索", services=[component])

    DeepSeekIntentParser._append_third_party_managed_decisions(
        parsed,
        component.source_text,
    )

    assert component.service == "unknown_component_search"
    assert component.calculator_service_name == "日志检索"
    assert "自建" not in component.calculator_service_name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product", "source", "expected_quantity"),
    [
        ("Doris", "Doris 预计3台，单台16核128G，磁盘4T。", 3),
        (
            "DolphinScheduler",
            "DolphinScheduler 预计2个节点，单台16核64G，磁盘1T。",
            2,
        ),
    ],
)
async def test_named_third_party_workload_survives_official_catalog_miss(
    product: str, source: str, expected_quantity: int
) -> None:
    class EmptyOfficialDiscovery:
        @staticmethod
        def resolve_official_product(*_labels: str) -> None:
            return None

        @staticmethod
        def candidate_official_products(
            *_labels: str, limit: int = 12
        ) -> list[dict[str, object]]:
            return []

        @staticmethod
        def official_products(*, limit: int = 500) -> list[dict[str, object]]:
            return []

    class UnknownGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            return {"service": "unknown", "confidence": "low"}

    component = ServiceRequirement(
        service=f"unknown_component_{product.casefold()}",
        calculator_service_name=product,
        source_text=source,
        requirements={"vcpu": 16, "memory_gib": 128 if product == "Doris" else 64},
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=EmptyOfficialDiscovery(),  # type: ignore[arg-type]
    )
    parser._gateway = UnknownGateway()  # type: ignore[assignment]

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )
    parsed = ParsedIntent(customer_summary=product, services=[component])
    parser._append_third_party_managed_decisions(parsed, source)

    assert component.service == "ec2"
    assert component.calculator_service_name == f"Amazon EC2（自建 {product}）"
    assert component.quantity == expected_quantity
    assert component.field_sources["_identity_resolution_status"] == "third_party"
    assert component.field_sources["_third_party_product"] == product
    assert component.field_sources["_pending_architecture_decision"] == "system_policy"
    assert len(parsed.ambiguities) == 1
    assert product in parsed.ambiguities[0]


def test_generic_capability_heading_is_not_treated_as_named_third_party_product() -> None:
    component = ServiceRequirement(
        service="unknown_component_search",
        calculator_service_name="日志检索",
        source_text="日志检索：计划部署3个数据节点，单节点8核32GB，单节点存储500GB。",
        requirements={"vcpu": 8, "memory_gib": 32, "storage_gib": 500},
    )

    assert DeepSeekIntentParser._route_named_third_party_workload(component) is False
    assert component.service == "unknown_component_search"


@pytest.mark.asyncio
async def test_learned_ec2_alias_cannot_overwrite_named_software_identity() -> None:
    class ContaminatedLocalRegistry:
        @staticmethod
        def resolve_official_product(*labels: str) -> dict[str, object] | None:
            assert labels == ("Doris",)
            return {
                "service_code": "AmazonEC2",
                "service_key": "ec2",
                "display_name": "AmazonEC2",
                "aliases": ["Amazon EC2", "Doris"],
                "identity_match_source": "learned_alias",
            }

    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Doris",
        source_text="Doris 预计3台，单台16核128G，磁盘4T。",
        requirements={"vcpu": 16, "memory_gib": 128},
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=ContaminatedLocalRegistry(),  # type: ignore[arg-type]
    )

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )

    assert component.service == "ec2"
    assert component.calculator_service_name == "Amazon EC2（自建 Doris）"
    assert component.field_sources["_identity_resolution_status"] == "third_party"
    assert component.field_sources["_third_party_product"] == "Doris"
    assert component.field_sources.get("_official_service_code") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product", "source", "expected_quantity"),
    [
        ("Doris", "Doris｜3台｜单台16核128G｜磁盘4T", 3),
        ("TBMQ/EMQX", "TBMQ/EMQX｜2个节点｜单台16核64G｜磁盘500G", 2),
        (
            "DolphinScheduler",
            "DolphinScheduler｜2个节点｜单台16核64G｜磁盘1T",
            2,
        ),
    ],
)
async def test_pipe_cleaned_third_party_name_cannot_reuse_learned_ec2_alias(
    product: str,
    source: str,
    expected_quantity: int,
) -> None:
    class ContaminatedLocalRegistry:
        @staticmethod
        def resolve_official_product(*labels: str) -> dict[str, object] | None:
            assert labels == (product,)
            return {
                "service_code": "AmazonEC2",
                "service_key": "ec2",
                "display_name": "AmazonEC2",
                "aliases": ["Amazon EC2", product],
                "identity_match_source": "learned_alias",
            }

    component = ServiceRequirement(
        service="unknown_" + re.sub(r"[^a-z0-9]+", "_", product.casefold()).strip("_"),
        calculator_service_name=product,
        source_text=source,
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        auto_discovery=ContaminatedLocalRegistry(),  # type: ignore[arg-type]
    )

    await parser._resolve_unknown_component_service(
        component,
        semaphore=asyncio.Semaphore(1),
        reporter=None,
        component_number=1,
    )
    parsed = ParsedIntent(customer_summary=product, services=[component])
    parser._append_third_party_managed_decisions(parsed, f"1、{source}")

    assert component.service == "ec2"
    assert component.calculator_service_name == f"Amazon EC2（自建 {product}）"
    assert component.quantity == expected_quantity
    assert component.requirements["vcpu"] == 16
    assert component.requirements["memory_gib"] in {64, 128}
    assert component.field_sources["_pending_architecture_decision"] == "system_policy"
    assert component.field_sources.get("_official_service_code") is None
    assert len(parsed.ambiguities) == 1
    assert product in parsed.ambiguities[0]


def test_pipe_cleaned_flink_fixed_nodes_require_managed_or_self_hosted_choice() -> None:
    source = "Flink｜3个节点｜单台24核64G｜磁盘500G"
    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(f"1、{source}")

    assert parsed is not None
    component = parsed.services[0]
    assert component.service.startswith("flink") or component.service.startswith("unknown")
    assert DeepSeekIntentParser._route_named_third_party_workload(component) is True

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, f"1、{source}")

    assert component.service == "ec2"
    assert component.calculator_service_name == "Amazon EC2（自建 Flink）"
    assert component.quantity == 3
    assert component.requirements["vcpu"] == 24
    assert component.requirements["memory_gib"] == 64
    assert component.requirements["system_disk_gib"] == 500
    assert len(parsed.ambiguities) == 1
    assert "EC2 上自建 Flink" in parsed.ambiguities[0]


@pytest.mark.parametrize(
    ("source", "expected_service"),
    [
        ("Kafka｜3个Broker节点｜单台8核16G｜磁盘2T", "msk"),
        ("Redis｜3个节点｜单台16核64G｜存储500G", "elasticache"),
        ("MySQL｜2个节点｜单台16核64G｜磁盘2T｜主从", "rds"),
    ],
)
def test_pipe_cleaned_managed_products_with_matching_node_contract_stay_managed(
    source: str,
    expected_service: str,
) -> None:
    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(f"1、{source}")

    assert parsed is not None
    assert parsed.services[0].service == expected_service


def test_component_template_cannot_erase_third_party_workload_identity() -> None:
    original = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2（自建 Doris）",
        source_text="Doris 预计3台，单台16核128G，磁盘4T。",
        field_sources={
            "_identity_resolution_status": "third_party",
            "_third_party_product": "Doris",
            "_pending_architecture_decision": "system_policy",
        },
    )
    filled = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        source_text=original.source_text,
        field_sources={"_official_service_code": "AmazonEC2"},
    )

    DeepSeekIntentParser._restore_authoritative_component_fields(original, filled)

    assert filled.calculator_service_name == "Amazon EC2（自建 Doris）"
    assert filled.field_sources["_third_party_product"] == "Doris"
    assert filled.field_sources["_pending_architecture_decision"] == "system_policy"
    assert filled.field_sources.get("_official_service_code") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feedback", "expected"),
    [
        ("硬盘10个T", {"system_disk_gib": 10240}),
        ("改成4核8G", {"vcpu": 4, "memory_gib": 8}),
    ],
)
async def test_ec2_literal_revision_is_never_lost(
    feedback: str, expected: dict[str, float]
) -> None:
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-northeast-1",
        quantity=3,
        requirements={
            "requested_model": "t4g.small",
            "vcpu": 2,
            "memory_gib": 2,
            "operating_system": "linux",
        },
        source_text="EC2 3台，t4g.small",
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = UnchangedComponentCorrectionGateway(component)  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text, component, feedback
    )

    for field, value in expected.items():
        assert revised.requirements[field] == value
        assert revised.field_sources[f"requirements.{field}"] == "customer_confirmation"


def test_component_template_derives_missing_ec2_total_disk() -> None:
    payload: dict[str, object] = {
        "service": "ec2",
        "quantity": 8,
        "requirements": {"system_disk_gib": 10240},
        "field_evidence": {"requirements.system_disk_gib": "硬盘10个T"},
    }

    DeepSeekIntentParser._complete_repeated_storage_template(payload)

    assert payload["requirements"] == {
        "system_disk_gib": 10240,
        "total_system_disk_gib": 81920,
    }
    assert payload["field_evidence"] == {
        "requirements.system_disk_gib": "硬盘10个T",
        "requirements.total_system_disk_gib": "system_derived",
    }


@pytest.mark.asyncio
async def test_s3_revision_rebuilds_capacity_and_drops_old_reference_default() -> None:
    class S3RevisionGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            return {
                "component": {
                    "service": "s3",
                    "calculator_service_name": "Amazon S3",
                    "region": "ap-northeast-1",
                    "quantity": 1,
                    "hours_per_month": 730,
                    "requirements": {"storage_class": "standard"},
                    "field_evidence": {"requirements.storage_class": "Standard"},
                    "source_text": "S3 Standard",
                    "query_action": None,
                }
            }

    component = ServiceRequirement(
        service="s3",
        calculator_service_name="Amazon S3",
        region="ap-northeast-1",
        requirements={
            "storage_class": "standard",
            "reference_unit_only": True,
            "system_default_assumption": "客户未提供 S3 容量",
        },
        source_text="Amazon S3，存储类型 Standard",
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = S3RevisionGateway()  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text, component, "存储改为20个T"
    )

    assert revised.requirements["storage_gib"] == 20 * 1024
    assert "reference_unit_only" not in revised.requirements
    assert "system_default_assumption" not in revised.requirements
    assert revised.field_sources["requirements.storage_gib"] == "customer_confirmation"


@pytest.mark.asyncio
async def test_new_configuration_parses_only_added_text_and_preserves_existing_rows() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    received: list[str] = []

    async def parse_addition(
        text: str, reporter: object | None = None
    ) -> ParsedIntent:
        received.append(text)
        return ParsedIntent(
            customer_summary="新增 S3",
            services=[
                ServiceRequirement(
                    service="s3",
                    calculator_service_name="Amazon S3",
                    region="ap-southeast-1",
                    requirements={"storage_gib": 1024},
                    source_text="Amazon S3：存储 1TB",
                )
            ],
        )

    parser.parse = parse_addition  # type: ignore[method-assign]
    current = ParsedIntent(
        customer_summary="现有 EC2",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                region="ap-southeast-1",
                requirements={"requested_model": "m7i.large"},
                source_text="Amazon EC2：m7i.large",
            )
        ],
    )

    revised = await parser.revise_configuration_from_feedback(
        "一整张很长的原始报价单",
        current,
        "请新增以下配置：\nAmazon S3：存储 1TB",
    )

    assert received == ["1、Amazon S3：存储 1TB"]
    assert [item.service for item in revised.services] == ["ec2", "s3"]
    assert revised.services[0].requirements["requested_model"] == "m7i.large"
    assert revised.services[1].requirements["storage_gib"] == 1024
    assert len({item.component_key for item in revised.services}) == 2


@pytest.mark.asyncio
async def test_unknown_component_field_is_returned_for_targeted_repair() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = ComponentFieldRepairGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    component = ServiceRequirement(
        service="global_accelerator",
        calculator_service_name="AWS Global Accelerator",
        region="global",
        quantity=1,
        source_text="配置1个加速器，每月加速流量1000GB",
    )

    cleaned = await parser._cleanup_components(
        component.source_text,
        ParsedIntent(customer_summary="GA 报价", services=[component]),
    )

    assert gateway.calls == 2
    assert "monthly_accelerated_traffic_gb" in gateway.user_contents[1]
    assert "data_transfer_out_gib" in gateway.user_contents[1]
    assert cleaned.services[0].requirements["data_transfer_out_gib"] == 1000


def test_legacy_component_field_is_normalized_without_retry() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="global_accelerator",
        calculator_service_name="AWS Global Accelerator",
        source_text="配置1个加速器，每月加速流量1000GB",
    )
    raw = {
        "component": {
            "service": "global_accelerator",
            "requirements": {"accelerators": 1, "data_transfer_gib": 1000},
            "field_evidence": {
                "requirements.accelerators": "1个加速器",
                "requirements.data_transfer_gib": "每月加速流量1000GB",
            },
        }
    }

    cleaned = parser._component_from_template_output(raw, component)

    assert cleaned.requirements == {
        "accelerators": 1,
        "data_transfer_out_gib": 1000,
    }
    assert "requirements.data_transfer_out_gib" in cleaned.field_evidence


@pytest.mark.asyncio
async def test_component_feedback_deterministically_applies_reserved_purchase_plan() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = UnchangedEc2CorrectionGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-southeast-1",
        quantity=2,
        requirements={
            "requested_model": "c6g.xlarge",
            "vcpu": 4,
            "memory_gib": 8,
            "operating_system": "linux",
            "purchase_option": "on_demand",
        },
        source_text="2台 EC2 c6g.xlarge，按需付费",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "改成一年全预付",
    )

    assert revised.requirements["purchase_option"] == "standard_reserved"
    assert revised.requirements["reserved_term_years"] == 1
    assert revised.requirements["payment_option"] == "all_upfront"
    assert revised.field_sources["requirements.purchase_option"] == "customer_confirmation"
    assert revised.field_sources["requirements.reserved_term_years"] == "customer_confirmation"
    assert revised.field_sources["requirements.payment_option"] == "customer_confirmation"


@pytest.mark.asyncio
async def test_latest_rds_capacity_correction_cannot_be_overwritten_by_old_source() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="rds",
        calculator_service_name="Amazon RDS MySQL",
        region="ap-southeast-1",
        requirements={"engine": "mysql", "storage_gib": 500},
        source_text="RDS MySQL，存储容量500GB",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "存储容量改成2000G吧",
    )

    assert gateway.calls == 2
    assert revised.requirements["storage_gib"] == 2000
    assert revised.field_sources["requirements.storage_gib"] == "customer_confirmation"
    assert revised.source_text.startswith("客户最新修改：存储容量改成2000G吧")


@pytest.mark.asyncio
async def test_latest_redis_capacity_correction_wins_even_when_model_returns_old_value() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="elasticache",
        calculator_service_name="Amazon ElastiCache for Redis",
        region="ap-east-1",
        requirements={
            "engine": "redis",
            "memory_gib": 52.82,
            "shards": 1,
            "replicas_per_shard": 2,
        },
        source_text="Redis，内存52.82GiB，一主两从",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "改成2000G",
    )

    assert gateway.calls == 2
    assert revised.requirements["memory_gib"] == 2000
    assert revised.requirements["replicas_per_shard"] == 2


@pytest.mark.asyncio
async def test_latest_redshift_node_and_storage_correction_are_authoritative() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="redshift",
        calculator_service_name="Amazon Redshift",
        region="ap-southeast-1",
        quantity=1,
        requirements={
            "requested_model": "ra3.large",
            "nodes": 2,
            "storage_gib": 10240,
            "managed_storage_gib": 10240,
        },
        source_text="Redshift，2个计算节点，存储容量10TB",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "计算节点改成6个，存储容量改成20T",
    )

    assert gateway.calls == 2
    assert revised.requirements["nodes"] == 6
    assert revised.requirements["storage_gib"] == 20480
    assert revised.requirements["managed_storage_gib"] == 20480
    assert revised.field_sources["requirements.nodes"] == "customer_confirmation"
    assert revised.field_sources["requirements.storage_gib"] == "customer_confirmation"
    assert "requirements.nodes" in revised.locked_fields
    assert "requirements.storage_gib" in revised.locked_fields


@pytest.mark.asyncio
async def test_latest_emr_core_node_quantity_correction_is_authoritative() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="emr",
        calculator_service_name="Amazon EMR",
        region="ap-southeast-1",
        quantity=1,
        requirements={
            "requested_model": "c6g.xlarge",
            "applications": "spark",
            "master_nodes": 1,
            "core_nodes": 5,
        },
        source_text="Amazon EMR，Spark，主节点1个，核心节点5个",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "核心节点数改成7个",
    )

    assert revised.requirements["core_nodes"] == 7
    assert "nodes" not in revised.requirements
    assert revised.field_sources["requirements.core_nodes"] == "customer_confirmation"
    assert "requirements.core_nodes" in revised.locked_fields


@pytest.mark.asyncio
async def test_role_specific_broker_count_does_not_overwrite_generic_nodes() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="msk",
        calculator_service_name="Amazon MSK",
        region="ap-southeast-1",
        quantity=1,
        requirements={"broker_count": 3, "requested_model": "m7g.xlarge"},
        source_text="Amazon MSK，3个 Broker 节点",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "消息代理节点数量改为4个",
    )

    assert revised.requirements["broker_count"] == 4
    assert "nodes" not in revised.requirements


@pytest.mark.asyncio
async def test_rabbitmq_high_availability_correction_forces_three_brokers() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="mq",
        calculator_service_name="Amazon MQ for RabbitMQ",
        region="ap-northeast-1",
        requirements={
            "engine_type": "rabbitmq",
            "requested_model": "mq.t3.micro",
            "broker_count": 1,
        },
        source_text="RabbitMQ，需要消息队列服务",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "需要高可用和故障切换",
    )

    assert gateway.calls == 2
    assert revised.requirements["broker_count"] == 3
    assert revised.requirements["deployment_mode"] == "cluster_multi_az"


def test_rabbitmq_high_availability_is_reconciled_during_initial_extraction() -> None:
    component = ServiceRequirement(
        service="mq",
        requirements={"engine_type": "rabbitmq", "broker_count": 1},
        source_text="RabbitMQ，需要消息队列服务，并且要求高可用。",
    )
    intent = ParsedIntent(customer_summary="RabbitMQ", services=[component])

    DeepSeekIntentParser._reconcile_explicit_service_architecture(component.source_text, intent)

    assert component.requirements["broker_count"] == 3
    assert component.requirements["deployment_mode"] == "cluster_multi_az"


@pytest.mark.asyncio
async def test_exact_model_confirmation_does_not_wait_for_ai() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class FailingGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("closed model choice must not call the model")

    parser._gateway = FailingGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="elasticache",
        calculator_service_name="Amazon ElastiCache",
        region="ap-east-1",
        quantity=1,
        requirements={
            "engine": "redis",
            "memory_gib": 16,
            "shards": 1,
            "replicas_per_shard": 1,
            "_review_selected_model": "cache.r6g.xlarge",
            "_review_selected_specifications": {"memoryGiB": 26.32},
        },
        source_text="Redis 16GB，1主1从",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "问题：AWS 相邻规格请选择。\n客户回答：选择 cache.m7g.xlarge",
    )

    assert revised.requirements["requested_model"] == "cache.m7g.xlarge"
    assert revised.field_sources["requirements.requested_model"] == "customer_confirmation"
    assert "requirements.requested_model" in revised.locked_fields
    assert "memory_gib" not in revised.requirements
    assert "_review_selected_model" not in revised.requirements
    assert "_review_selected_specifications" not in revised.requirements


@pytest.mark.asyncio
async def test_exact_model_answer_always_replaces_old_cpu_and_memory() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class FailingGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("closed model choice must not call the model")

    parser._gateway = FailingGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-northeast-1",
        requirements={"vcpu": 6, "memory_gib": 24},
        source_text="EC2 6核24GB",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        ("问题：AWS 没有完全相同的型号，请在下方重新选择您需要的型号。\n客户回答：选择 t2.micro"),
    )

    assert revised.requirements["requested_model"] == "t2.micro"
    assert "vcpu" not in revised.requirements
    assert "memory_gib" not in revised.requirements


@pytest.mark.asyncio
async def test_unrelated_component_edit_preserves_review_model_and_overwrites_old_shape() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class QuantityGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            return {
                "component": {
                    "service": "ec2",
                    "calculator_service_name": "Amazon EC2",
                    "region": "ap-northeast-1",
                    "quantity": 8,
                    "hours_per_month": 730,
                    "requirements": {
                        "requested_model": "t2.micro",
                        "vcpu": 6,
                        "memory_gib": 24,
                        "operating_system": "linux",
                    },
                    "field_evidence": {"quantity": "8台"},
                    "source_text": "EC2 6核24GB，选择 t2.micro",
                    "query_action": None,
                }
            }

    parser._gateway = QuantityGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-northeast-1",
        quantity=3,
        requirements={
            "requested_model": "t2.micro",
            "vcpu": 6,
            "memory_gib": 24,
            "operating_system": "linux",
            "_review_selected_model": "t2.micro",
            "_review_selected_specifications": {
                "vCPU": 1,
                "memoryGiB": 1,
            },
        },
        source_text="EC2 6核24GB，客户已选择 t2.micro",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "改成8台机器吧",
    )

    assert revised.quantity == 8
    assert revised.requirements["requested_model"] == "t2.micro"
    assert revised.requirements["vcpu"] == 1
    assert revised.requirements["memory_gib"] == 1
    assert "_review_selected_model" not in revised.requirements
    assert "_review_selected_specifications" not in revised.requirements


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service", "model", "base_requirements", "official_vcpu", "official_memory"),
    [
        ("rds", "db.m5.large", {"engine": "mysql"}, 2, 8),
        ("elasticache", "cache.m7g.large", {"engine": "redis"}, 2, 6.38),
        ("opensearch", "r6g.large.search", {}, 2, 16),
    ],
)
async def test_quantity_edit_rebuilds_any_component_from_latest_confirmed_model(
    service: str,
    model: str,
    base_requirements: dict[str, object],
    official_vcpu: float,
    official_memory: float,
) -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    old_requirements = {
        **base_requirements,
        "requested_model": model,
        "memory_gib": 999,
    }
    old_requirements["vcpu"] = 99

    class QuantityGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            return {
                "component": {
                    "service": service,
                    "region": "ap-southeast-1",
                    "quantity": 2,
                    "hours_per_month": 730,
                    "requirements": old_requirements,
                    "field_evidence": {"quantity": "数量改成2台"},
                    "source_text": f"{service} 数量1，已选择 {model}",
                    "query_action": None,
                }
            }

    parser._gateway = QuantityGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service=service,
        region="ap-southeast-1",
        quantity=1,
        requirements={
            **old_requirements,
            "_review_selected_model": model,
            "_review_selected_specifications": {
                "vCPU": official_vcpu,
                "memoryGiB": official_memory,
            },
        },
        source_text=f"{service} 数量1，已选择 {model}",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "数量改成2台",
    )

    assert revised.quantity == 2
    assert revised.requirements["requested_model"] == model
    assert revised.requirements["vcpu"] == official_vcpu
    assert revised.requirements["memory_gib"] == official_memory


@pytest.mark.asyncio
async def test_repeated_component_edits_rebuild_from_the_latest_result() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class SequentialStorageGateway:
        def __init__(self) -> None:
            self.values = iter((30720, 40960))

        async def complete_json(self, **_: object) -> dict[str, object]:
            storage = next(self.values)
            return {
                "component": {
                    "service": "s3",
                    "region": "ap-southeast-1",
                    "quantity": 1,
                    "hours_per_month": 730,
                    "requirements": {
                        "storage_gib": storage,
                        "storage_class": "standard",
                    },
                    "field_evidence": {"requirements.storage_gib": f"{storage / 1024:g}TB"},
                    "source_text": f"S3 {storage / 1024:g}TB",
                    "query_action": None,
                }
            }

    parser._gateway = SequentialStorageGateway()  # type: ignore[assignment]
    original = ServiceRequirement(
        service="s3",
        region="ap-southeast-1",
        requirements={"storage_gib": 20480, "storage_class": "standard"},
        source_text="S3 20TB",
    )

    first = await parser.revise_component_from_feedback(
        original.source_text, original, "容量改成30TB"
    )
    second = await parser.revise_component_from_feedback(first.source_text, first, "容量再改成40TB")

    assert first.requirements["storage_gib"] == 30720
    assert second.requirements["storage_gib"] == 40960
    assert second.source_text.startswith("客户最新修改：容量再改成40TB")


@pytest.mark.asyncio
async def test_generic_official_model_confirmation_is_authoritative() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class FailingGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("closed official model choice must not call the model")

    parser._gateway = FailingGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="mq",
        calculator_service_name="Amazon MQ for RabbitMQ",
        region="ap-southeast-1",
        requirements={
            "engine_type": "rabbitmq",
            "requested_model": "mq.t3.micro",
            "broker_count": 3,
        },
        source_text="RabbitMQ，高可用，3个 Broker",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "问题：原型号不可用，请选择官方型号。\n客户回答：选择 mq.m5.large",
    )

    assert revised.requirements["requested_model"] == "mq.m5.large"
    assert revised.field_sources["requirements.requested_model"] == "customer_confirmation"
    assert "requirements.requested_model" in revised.locked_fields


@pytest.mark.asyncio
async def test_component_model_choice_reads_answer_instead_of_question_candidates() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class FailingGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("closed customer choice must not call the model")

    parser._gateway = FailingGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="elasticache",
        calculator_service_name="Amazon ElastiCache for Redis",
        region="ap-northeast-1",
        requirements={
            "engine": "redis",
            "memory_gib": 16,
            "_review_selected_model": "cache.r6g.xlarge",
            "_review_selected_specifications": {"memoryGiB": 26.32},
        },
        source_text="Redis 每节点约16GB",
    )
    feedback = (
        "问题：客户需要 Redis 每节点约16G；AWS 相邻规格为"
        "cache.m4.xlarge（14.28G，偏低）、cache.r6g.xlarge（26.32G，不低配），请选择。\n"
        "客户回答：选择 cache.m4.xlarge"
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text, component, feedback
    )

    assert revised.requirements["requested_model"] == "cache.m4.xlarge"
    assert "memory_gib" not in revised.requirements
    assert "_review_selected_model" not in revised.requirements


def test_explicit_purchase_plan_is_reconciled_from_each_component_source() -> None:
    component = ServiceRequirement(
        service="ec2",
        source_text="应用服务器 4 台，8核16G，购买方式三年全预付",
        requirements={"vcpu": 8, "memory_gib": 16},
    )
    intent = ParsedIntent(customer_summary="EC2 报价", services=[component])

    DeepSeekIntentParser._reconcile_explicit_capacities(component.source_text, intent)

    assert component.requirements["purchase_option"] == "standard_reserved"
    assert component.requirements["reserved_term_years"] == 3
    assert component.requirements["payment_option"] == "all_upfront"


class MutatingComponentGateway:
    async def complete_json(self, **_: object) -> dict[str, object]:
        return {
            "customer_summary": "AI 改写结果",
            "services": [
                {
                    "service": "ec2",
                    "calculator_service_name": "错误名称",
                    "region": "us-east-1",
                    "quantity": 99,
                    "hours_per_month": 100,
                    "requirements": {
                        "vcpu": 32,
                        "memory_gib": 128,
                        "operating_system": "windows",
                        "tenancy": "shared",
                    },
                    "source_text": "AI 改写的原文",
                }
            ],
            "ambiguities": [],
        }


@pytest.mark.asyncio
async def test_component_template_cannot_overwrite_customer_locked_fields() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = MutatingComponentGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-southeast-1",
        quantity=2,
        hours_per_month=730,
        requirements={"vcpu": 4, "memory_gib": 16, "operating_system": "linux"},
        source_text="新加坡 2 台 Linux EC2，每台 4 核 16G",
    )

    cleaned = await parser._cleanup_components(
        component.source_text,
        ParsedIntent(customer_summary="原始摘要", services=[component]),
    )

    result = cleaned.services[0]
    assert result.calculator_service_name == "Amazon EC2"
    assert result.region == "ap-southeast-1"
    assert result.quantity == 2
    assert result.hours_per_month == 730
    assert result.source_text == component.source_text
    assert result.requirements["vcpu"] == 4
    assert result.requirements["memory_gib"] == 16
    assert result.requirements["operating_system"] == "linux"
    assert "tenancy" not in result.requirements


def test_customer_correction_is_restored_after_a_stale_result() -> None:
    original = ServiceRequirement(
        service="opensearch",
        calculator_service_name="Amazon OpenSearch Service",
        region="ap-southeast-1",
        quantity=1,
        requirements={"data_nodes": 3, "total_storage_gib": 1024},
        field_sources={
            "requirements.total_storage_gib": "customer_correction",
            "requirements.data_nodes": "customer_confirmation",
        },
        locked_fields=[
            "requirements.total_storage_gib",
            "requirements.data_nodes",
        ],
    )
    stale = ServiceRequirement(
        service="opensearch",
        calculator_service_name="Amazon OpenSearch Service",
        region="ap-southeast-1",
        quantity=1,
        requirements={"data_nodes": 500, "total_storage_gib": 500 * 1024},
    )

    DeepSeekIntentParser._restore_authoritative_component_fields(original, stale)

    assert stale.requirements["data_nodes"] == 3
    assert stale.requirements["total_storage_gib"] == 1024
    assert stale.field_sources["requirements.total_storage_gib"] == "customer_correction"


@pytest.mark.asyncio
async def test_initial_intake_then_each_component_gets_its_own_prompt() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = CapturingWorkloadGateway()
    parser._gateway = gateway  # type: ignore[assignment]

    await parser.parse("Redis 一主一从，每节点 8 GiB；S3 对象存储 500GB")

    assert gateway.calls >= 5
    assert "第一步数据清洗员" in gateway.system_prompts[0]
    assert "拆分、去除干扰、统一格式" in gateway.system_prompts[0]
    assert "requirements 必须填写" in gateway.system_prompts[0]
    assert "replicas_per_shard" not in gateway.system_prompts[0]
    assert any("replicas_per_shard" in prompt for prompt in gateway.system_prompts[1:])
    assert any("storage_class" in content for content in gateway.user_contents[1:])
    assert not any("结构化结果审核员" in prompt for prompt in gateway.system_prompts[1:])


@pytest.mark.asyncio
async def test_numbered_request_skips_workload_ai_and_keeps_lossless_component_source() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = NumberedCleaningGateway()
    parser._gateway = gateway  # type: ignore[assignment]

    async def keep_first_pass(
        _original_text: str,
        intent: ParsedIntent,
        *,
        reporter: object | None = None,
    ) -> ParsedIntent:
        return intent

    parser._cleanup_components = keep_first_pass  # type: ignore[method-assign]
    raw = "1、应用服务器：预计部署3台Linux服务器，单台8核32GB，系统盘200GB，数据盘500GB。"

    parsed = await parser.parse(raw)

    assert gateway.calls == 0
    component = parsed.services[0]
    assert component.source_text == raw.removeprefix("1、")
    assert component.original_source_text == raw.removeprefix("1、")
    assert component.quantity == 3
    assert component.requirements["system_disk_gib"] == 200
    assert component.requirements["additional_ebs_volumes"][0]["size_gib"] == 500


def test_fast_numbered_path_requires_a_real_sequence_starting_at_one() -> None:
    assert (
        DeepSeekIntentParser._intent_from_lossless_sales_numbering(
            "3、Broker节点\n4核16G"
        )
        is None
    )
    parsed = DeepSeekIntentParser._intent_from_lossless_sales_numbering(
        "新加坡地区\n1、Amazon EC2：4核16GB\n2、Amazon S3：10TB"
    )
    assert parsed is not None
    assert len(parsed.services) == 2


@pytest.mark.asyncio
async def test_unusable_numbered_intake_falls_back_without_aborting_quote() -> None:
    class EmptyInventoryGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            return {"customer_summary": "", "services": [], "ambiguities": []}

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = EmptyInventoryGateway()  # type: ignore[assignment]

    async def keep_inventory(
        _original_text: str,
        intent: ParsedIntent,
        *,
        reporter: object | None = None,
    ) -> ParsedIntent:
        return intent

    parser._cleanup_components = keep_inventory  # type: ignore[method-assign]
    parsed = await parser.parse(
        "1、Doris：每节点16核128GB，4TB磁盘，共3节点。\n"
        "2、DolphinScheduler：每节点16核64GB，1TB磁盘，共2节点。"
    )

    assert len(parsed.services) == 2
    assert "Doris" in parsed.services[0].source_text
    assert "DolphinScheduler" in parsed.services[1].source_text


def test_legacy_product_label_is_not_mistaken_for_product_name() -> None:
    component = ServiceRequirement(
        service="unknown_component_doris",
        calculator_service_name="Doris",
        quantity=3,
        source_text="产品：Doris；数量：3台；每台CPU：16核；每台内存：128GB",
        requirements={"vcpu": 16, "memory_gib": 128},
    )
    parsed = ParsedIntent(customer_summary="Doris", services=[component])

    DeepSeekIntentParser._normalize_cleaned_source_prefixes(parsed)

    assert component.source_text.startswith("Doris｜")
    assert DeepSeekIntentParser._self_hosted_product_name(component) == "Doris"
    assert DeepSeekIntentParser._route_named_third_party_workload(component) is True
    assert component.calculator_service_name == "Amazon EC2（自建 Doris）"


@pytest.mark.parametrize(
    ("product", "source", "expected_quantity"),
    [
        ("Doris", "Doris，3台，单台16核128G，磁盘4T", 3),
        ("TBMQ/EMQX", "TBMQ/EMQX，2个节点，单台16核64G，磁盘500G", 2),
        ("DolphinScheduler", "DolphinScheduler，2个节点，单台16核64G，磁盘1T", 2),
    ],
)
def test_comma_cleaned_named_workload_keeps_self_hosted_identity(
    product: str, source: str, expected_quantity: int
) -> None:
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        source_text=source,
        requirements={"vcpu": 16, "memory_gib": 64},
    )
    parsed = ParsedIntent(customer_summary=product, services=[component])

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, f"1、{source}")

    assert component.calculator_service_name == f"Amazon EC2（自建 {product}）"
    assert component.field_sources["_third_party_product"] == product
    assert component.quantity == expected_quantity
    assert any(product in notice and "自建" in notice for notice in parsed.ambiguities)


@pytest.mark.parametrize("product", ["Doris", "Flink", "TBMQ/EMQX", "DolphinScheduler"])
def test_explicit_ec2_self_hosted_row_keeps_the_software_identity(product: str) -> None:
    source = (
        f"{product}，Amazon EC2 自建，m6i.4xlarge，16 vCPU，64 GiB，Linux，数量3"
    )
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2 云服务器",
        quantity=3,
        source_text=source,
        requirements={"vcpu": 16, "memory_gib": 64, "operating_system": "linux"},
    )
    parsed = ParsedIntent(customer_summary=product, services=[component])

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, f"1、{source}")

    assert component.service == "ec2"
    assert component.calculator_service_name == f"Amazon EC2（自建 {product}）"
    assert component.field_sources["_third_party_product"] == product
    assert component.field_sources["_pending_architecture_decision"] == "system_policy"
    assert "_architecture_decision" not in component.field_sources
    assert len(parsed.ambiguities) == 1
    assert product in parsed.ambiguities[0]
    assert "托管" in parsed.ambiguities[0]
    assert "自建" in parsed.ambiguities[0]


def test_sales_self_hosted_plan_rows_all_become_customer_architecture_questions() -> None:
    source = """1、Doris，Amazon EC2 自建，r6i.4xlarge，16 vCPU，128 GiB，Linux，数量3
2、Flink，Amazon EC2 自建，c6a.8xlarge，32 vCPU，64 GiB，Linux，数量3
3、TBMQ/EMQX，Amazon EC2 自建，m6i.4xlarge，16 vCPU，64 GiB，Linux，数量2
4、应用服务器，Amazon EC2，r6i.4xlarge，16 vCPU，128 GiB，Linux，数量3
5、DolphinScheduler，Amazon EC2 自建，m6i.4xlarge，16 vCPU，64 GiB，Linux，数量2"""
    parsed = DeepSeekIntentParser._intent_from_lossless_sales_numbering(source)

    assert parsed is not None
    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, source)

    assert [item.calculator_service_name for item in parsed.services] == [
        "Amazon EC2（自建 Doris）",
        "Amazon EC2（自建 Flink）",
        "Amazon EC2（自建 TBMQ/EMQX）",
        "Amazon EC2 云服务器",
        "Amazon EC2（自建 DolphinScheduler）",
    ]
    assert len(parsed.ambiguities) == 4
    for product in ("Doris", "Flink", "TBMQ/EMQX", "DolphinScheduler"):
        assert any(
            product in question and "托管" in question and "自建" in question
            for question in parsed.ambiguities
        )


def test_component_cleanup_keeps_numbered_owner_and_cannot_duplicate_row() -> None:
    raw = "Doris，3台，单台16核128G，磁盘4T"
    original = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2（自建 Doris）",
        component_key="cmp_source_0002",
        quantity=3,
        source_text="Doris｜数量：3台｜每台CPU：16核｜每台内存：128GB｜每台磁盘：4TB",
        original_source_text=raw,
        requirements={"vcpu": 16, "memory_gib": 128},
        field_sources={"_intake_ai_identity": "ai_cleaning"},
    )
    filled = original.model_copy(update={"original_source_text": None})

    DeepSeekIntentParser._restore_authoritative_component_fields(original, filled)
    parsed = ParsedIntent(customer_summary="Doris", services=[filled])
    DeepSeekIntentParser._reconcile_explicit_component_inventory(f"2、{raw}", parsed)

    assert filled.original_source_text == raw
    assert len(parsed.services) == 1
    assert parsed.services[0].component_key.startswith("cmp_sales_")


@pytest.mark.asyncio
async def test_invalid_ai_structure_is_repaired_once() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = RepairingGateway()
    parser._gateway = gateway  # type: ignore[assignment]

    parsed = await parser.parse("Redis 一主一从，每节点 8 GiB")

    # Invalid intake gets one repair, then one validated component extraction.
    assert gateway.calls == 3
    assert parsed.services[0].quantity == 1


@pytest.mark.asyncio
async def test_missing_ai_summary_uses_customer_text_after_component_cleanup() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = MissingSummaryGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    text = "Redis 一主一从，每节点 8 GiB"

    parsed = await parser.parse(text)

    assert gateway.calls == 2
    assert (
        parsed.customer_summary
        == "已识别 1 项 AWS 配置；区域：待确认；Amazon ElastiCache for Redis × 1。"
    )


@pytest.mark.asyncio
async def test_schema_repair_does_not_add_services_after_ai_cleanup() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = RepairStillMissingServiceGateway()
    parser._gateway = gateway  # type: ignore[assignment]

    parsed = await parser.parse("Redis 一主一从，每节点 8 GiB；对象存储使用 S3 1TB")

    # Intake repair plus extraction and audit for each explicit component.
    assert gateway.calls >= 6
    # AI owns interpretation, while the lossless completeness guard preserves
    # an explicitly named service if a cleanup pass accidentally drops it.
    assert {item.service for item in parsed.services} == {"elasticache", "s3"}


def test_compact_mixed_service_capacities_and_annual_transfer_are_lossless() -> None:
    text = (
        "Amazon EC2：4 vCPU、8 GiB 内存，40GB 系统盘、60GB 数据盘，数量 35。\n"
        "Amazon ElastiCache for Redis：Redis 主从，2 GB，数量 1。\n"
        "Amazon S3：对象存储，容量约 3 TB。\n"
        "公网流量：4 TB/年。"
    )
    parsed = ParsedIntent(
        customer_summary="mixed",
        services=[
            ServiceRequirement(service="ec2", quantity=35, source_text=text.splitlines()[0]),
            ServiceRequirement(service="elasticache", source_text=text.splitlines()[1]),
            ServiceRequirement(service="s3", source_text=text.splitlines()[2]),
            ServiceRequirement(service="data_transfer", source_text=text.splitlines()[3]),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert parsed.services[0].requirements["system_disk_gib"] == 40
    assert parsed.services[0].requirements["additional_ebs_volumes"] == [
        {"size_gib": 60, "volume_type": "gp3", "count_per_instance": 1}
    ]
    assert parsed.services[1].requirements["memory_gib"] == 2
    assert parsed.services[2].requirements["storage_gib"] == 3072
    assert parsed.services[3].requirements["data_transfer_out_gib"] == pytest.approx(4096 / 12)


def test_ec2_label_first_data_disk_is_preserved_losslessly() -> None:
    text = (
        "应用服务器：预计部署3台Linux服务器，单台8核32GB，"
        "系统盘200GB，数据盘500GB。"
    )
    parsed = ParsedIntent(
        customer_summary="ec2 disks",
        services=[ServiceRequirement(service="ec2", quantity=3, source_text=text)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    component = parsed.services[0]
    assert component.requirements["system_disk_gib"] == 200
    assert component.requirements["additional_ebs_volumes"] == [
        {"size_gib": 500, "volume_type": "gp3", "count_per_instance": 1}
    ]
    assert component.field_sources["requirements.additional_ebs_volumes"] == (
        "customer_text"
    )


def test_modern_service_audit_preserves_identity_units_and_eks_workers() -> None:
    text = """区域：亚太地区（东京）
Amazon Lambda｜请求量500万/月｜内存512MB｜运行时间3秒
Amazon DynamoDB｜存储500GB｜读写容量按需模式
Amazon EKS｜3个Worker节点｜m7g.large
Amazon Fargate｜CPU 4 vCPU｜内存16GB｜运行任务
Amazon Kinesis Data Streams｜2个Shard｜数据流处理
Amazon Athena｜每月查询数据量5TB｜数据分析
Amazon Glue｜10个ETL任务｜数据处理
Amazon SageMaker｜ml.m5.xlarge｜机器学习环境
Amazon Cognito｜10万用户｜用户认证服务
Amazon Secrets Manager｜100个Secret｜密钥管理
Amazon MQ｜RabbitMQ｜mq.m5.large｜消息队列"""
    parsed = ParsedIntent(
        customer_summary="audit",
        services=[
            ServiceRequirement(
                service="lambda",
                source_text=text.splitlines()[1],
                requirements={"request_count": 5_000_000, "memory_mb": 512},
            ),
            ServiceRequirement(
                service="dynamodb",
                source_text=text.splitlines()[2],
                requirements={"storage_gib": 512_000},
            ),
            ServiceRequirement(service="eks", source_text=text.splitlines()[3]),
            ServiceRequirement(
                service="fargate",
                source_text=text.splitlines()[4],
                requirements={"vcpu": 4, "memory_gib": 16_384},
            ),
            ServiceRequirement(service="kinesis", source_text=text.splitlines()[5]),
            ServiceRequirement(
                service="athena",
                source_text=text.splitlines()[6],
                requirements={"data_scanned_gib": 5_120_000},
            ),
            ServiceRequirement(service="glue", source_text=text.splitlines()[7]),
            ServiceRequirement(service="sagemaker", source_text=text.splitlines()[8]),
            ServiceRequirement(service="cognito", source_text=text.splitlines()[9]),
            ServiceRequirement(service="secrets_manager", source_text=text.splitlines()[10]),
            ServiceRequirement(
                service="mq",
                calculator_service_name="Amazon MQ",
                source_text=text.splitlines()[11],
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_models(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    by_service = {item.service: item for item in parsed.services}
    assert "sqs" not in by_service
    assert by_service["lambda"].requirements == {
        "memory_mb": 512.0,
        "requests": 5_000_000.0,
        "duration_ms": 3000.0,
    }
    assert by_service["dynamodb"].requirements["storage_gib"] == 500
    assert by_service["dynamodb"].requirements["capacity_mode"] == "on_demand"
    assert by_service["fargate"].requirements["task_vcpu"] == 4
    assert by_service["fargate"].requirements["task_memory_gib"] == 16
    assert by_service["athena"].requirements["data_scanned_gib"] == 5120
    assert by_service["glue"].requirements["job_count"] == 10
    assert by_service["cognito"].requirements["user_count"] == 100_000
    assert by_service["secrets_manager"].requirements["secret_count"] == 100
    assert by_service["mq"].requirements["requested_model"] == "mq.m5.large"
    assert by_service["mq"].requirements["engine_type"] == "rabbitmq"
    worker = next(
        item for item in parsed.services if item.service == "ec2" and "Worker" in item.source_text
    )
    assert worker.quantity == 3
    assert worker.requirements["requested_model"] == "m7g.large"


def test_explicit_models_and_redis_node_count_are_recovered_from_customer_text() -> None:
    text = (
        "EC2：c7i.xlarge × 2，4核 8G。\n"
        "RDS MySQL：db.m7g.large，2核 8G。\n"
        "Redis：cache.t4g.small × 2，1主1从。"
    )
    parsed = ParsedIntent(
        customer_summary="explicit",
        services=[
            ServiceRequirement(service="ec2", quantity=2, source_text=text.splitlines()[0]),
            ServiceRequirement(service="rds", source_text=text.splitlines()[1]),
            ServiceRequirement(
                service="elasticache",
                quantity=2,
                source_text=text.splitlines()[2],
                requirements={"shards": 1, "replicas_per_shard": 1},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_models(text, parsed)
    DeepSeekIntentParser._normalize_redis_group_quantity(parsed)

    assert parsed.services[0].requirements["requested_model"] == "c7i.xlarge"
    assert parsed.services[1].requirements["requested_model"] == "db.m7g.large"
    assert parsed.services[2].requirements["requested_model"] == "cache.t4g.small"
    assert parsed.services[2].quantity == 1


def test_explicit_rds_and_cache_engines_survive_component_cleanup() -> None:
    text = (
        "Amazon RDS for PostgreSQL：db.t4g.large，2核8G，Multi-AZ。\n"
        "Amazon ElastiCache for Redis：Redis OSS，1主1从。"
    )
    parsed = ParsedIntent(
        customer_summary="explicit engines",
        services=[
            ServiceRequirement(
                service="rds",
                source_text=text.splitlines()[0],
                requirements={"requested_model": "db.t4g.large"},
            ),
            ServiceRequirement(
                service="elasticache",
                source_text=text.splitlines()[1],
                requirements={"shards": 1, "replicas_per_shard": 1},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_engines(text, parsed)

    assert parsed.services[0].requirements["engine"] == "postgresql"
    assert parsed.services[1].requirements["engine"] == "redis"


def test_explicit_rds_storage_deployment_and_cache_topology_survive_cleanup() -> None:
    text = (
        "RDS PostgreSQL：db.t4g.large，2核8G，Multi-AZ，gp3 100GB，1套。\n"
        "Redis：1主1从，共2个节点，1套。"
    )
    parsed = ParsedIntent(
        customer_summary="lossless",
        services=[
            ServiceRequirement(
                service="rds",
                source_text=text.splitlines()[0],
                requirements={"system_disk_gib": 100},
            ),
            ServiceRequirement(
                service="elasticache",
                quantity=2,
                source_text=text.splitlines()[1],
                requirements={},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_service_architecture(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert parsed.services[0].requirements["deployment"] == "multi_az"
    assert parsed.services[0].requirements["storage_gib"] == 100
    assert "system_disk_gib" not in parsed.services[0].requirements
    assert parsed.services[1].requirements["shards"] == 1
    assert parsed.services[1].requirements["replicas_per_shard"] == 1


def test_explicit_load_balancer_omission_is_detected() -> None:
    parsed = ParsedIntent(
        customer_summary="EC2 和静态文件",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
            )
        ],
    )

    assert DeepSeekIntentParser._missing_explicit_services("前面需要一个负载均衡", parsed) == [
        "elastic-load-balancing"
    ]


def test_explicit_ec2_and_rds_omission_is_detected() -> None:
    parsed = ParsedIntent(
        customer_summary="Redis",
        services=[
            ServiceRequirement(service="elasticache", calculator_service_name="Amazon ElastiCache")
        ],
    )

    assert DeepSeekIntentParser._missing_explicit_services(
        "应用服务器：新加坡区域，3 台 Linux；数据库：MySQL 8.0", parsed
    ) == ["ec2", "rds"]


def test_explicit_capacities_override_wrong_model_values() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="elasticache",
                calculator_service_name="Amazon ElastiCache",
                source_text="Redis：单节点内存约 8G",
                requirements={"memory_gib": 16},
            ),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                source_text="对象存储：存储约 3TB 图片",
                requirements={"storage_gib": 5120},
            ),
            ServiceRequirement(
                service="cloudfront",
                calculator_service_name="Amazon CloudFront",
                source_text="CDN：每月预计向公网下行约 5TB",
                requirements={"data_transfer_out_gib": 8192},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert parsed.services[0].requirements["memory_gib"] == 8
    assert parsed.services[1].requirements["storage_gib"] == 3072
    assert parsed.services[2].requirements["data_transfer_out_gib"] == 5120


def test_named_models_do_not_create_unstated_cpu_or_memory_constraints() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text="应用服务器：型号 m7i.xlarge，每台 150GB gp3 系统盘",
                requirements={
                    "requested_model": "m7i.xlarge",
                    "vcpu": 8,
                    "memory_gib": 32,
                },
            ),
            ServiceRequirement(
                service="rds",
                source_text="数据库：型号 db.m7i.2xlarge，Multi-AZ",
                requirements={
                    "requested_model": "db.m7i.2xlarge",
                    "vcpu": 8,
                    "memory_gib": 64,
                },
            ),
            ServiceRequirement(
                service="elasticache",
                source_text="Redis：型号 cache.r7g.large",
                requirements={
                    "requested_model": "cache.r7g.large",
                    "vcpu": 2,
                    "memory_gib": 8,
                },
            ),
        ],
    )

    DeepSeekIntentParser._drop_specs_inferred_from_models("", parsed)

    for service in parsed.services:
        assert "vcpu" not in service.requirements
        assert "memory_gib" not in service.requirements


def test_named_model_keeps_explicit_customer_shape() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text="应用服务器：型号 m7i.xlarge，客户明确要求 4 核 16G 内存",
                requirements={
                    "requested_model": "m7i.xlarge",
                    "vcpu": 4,
                    "memory_gib": 16,
                },
            )
        ],
    )

    DeepSeekIntentParser._drop_specs_inferred_from_models("", parsed)

    assert parsed.services[0].requirements["vcpu"] == 4
    assert parsed.services[0].requirements["memory_gib"] == 16


def test_single_workload_region_is_inherited_by_regional_services() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                region="ap-southeast-1",
            ),
            ServiceRequirement(service="rds", calculator_service_name="Amazon RDS"),
            ServiceRequirement(service="elasticache", calculator_service_name="Amazon ElastiCache"),
            ServiceRequirement(service="cloudfront", calculator_service_name="Amazon CloudFront"),
        ],
    )

    DeepSeekIntentParser._inherit_single_workload_region(parsed)

    assert parsed.services[1].region == "ap-southeast-1"
    assert parsed.services[2].region == "ap-southeast-1"
    assert parsed.services[3].region is None


def test_standalone_region_heading_is_inherited_when_parser_omits_all_regions() -> None:
    parsed = ParsedIntent(
        customer_summary="新加坡工作负载",
        services=[
            ServiceRequirement(service="ec2"),
            ServiceRequirement(service="rds"),
            ServiceRequirement(service="elasticache"),
            ServiceRequirement(service="cloudfront"),
        ],
        ambiguities=["请确认这些区域型服务部署在哪个 AWS 区域。"],
    )

    DeepSeekIntentParser._inherit_single_workload_region(
        parsed,
        source_text=(
            "新加坡（ap-southeast-1）\n\n"
            "1、Amazon EC2 云服务器：数量4台\n"
            "2、Amazon RDS MySQL：Multi-AZ"
        ),
    )

    assert parsed.services[0].region == "ap-southeast-1"
    assert parsed.services[1].region == "ap-southeast-1"
    assert parsed.services[2].region == "ap-southeast-1"
    assert parsed.services[3].region is None
    assert parsed.ambiguities == []


@pytest.mark.parametrize(
    "source_text",
    [
        (
            "1、Amazon EC2 云服务器：数量4台\n"
            "统一部署在新加坡（ap-southeast-1）\n"
            "2、Amazon RDS MySQL：Multi-AZ"
        ),
        (
            "1、Amazon EC2 云服务器：数量4台\n"
            "2、Amazon RDS MySQL：Multi-AZ\n"
            "以上服务统一部署在新加坡（ap-southeast-1）"
        ),
    ],
)
def test_single_region_anywhere_in_request_becomes_quote_default(
    source_text: str,
) -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(service="ec2"),
            ServiceRequirement(service="rds"),
            ServiceRequirement(service="cloudfront"),
        ],
    )

    DeepSeekIntentParser._inherit_single_workload_region(parsed, source_text)

    assert parsed.services[0].region == "ap-southeast-1"
    assert parsed.services[1].region == "ap-southeast-1"
    assert parsed.services[2].region is None


def test_first_written_region_is_default_when_no_component_region_survives_parse() -> None:
    parsed = ParsedIntent(
        customer_summary="多区域工作负载",
        services=[
            ServiceRequirement(service="ec2"),
            ServiceRequirement(service="rds"),
        ],
    )

    DeepSeekIntentParser._inherit_single_workload_region(
        parsed,
        source_text="新加坡（ap-southeast-1）\n备用区域：东京（ap-northeast-1）",
    )

    assert [item.region for item in parsed.services] == [
        "ap-southeast-1",
        "ap-southeast-1",
    ]


def test_single_workload_region_ignores_global_and_removes_stale_question() -> None:
    parsed = ParsedIntent(
        customer_summary="新加坡工作负载",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
            ServiceRequirement(service="rds"),
            ServiceRequirement(service="cloudfront", region="global"),
        ],
        ambiguities=["请确认这些区域型服务部署在哪个 AWS 区域。"],
    )

    DeepSeekIntentParser._inherit_single_workload_region(parsed)

    assert parsed.services[1].region == "ap-southeast-1"
    assert parsed.services[2].region == "global"
    assert parsed.ambiguities == []


def test_regional_s3_global_label_inherits_the_only_concrete_region() -> None:
    parsed = ParsedIntent(
        customer_summary="新加坡工作负载",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                region="全球",
                requirements={"storage_gib": 30720, "storage_class": "standard"},
            ),
        ],
    )

    DeepSeekIntentParser._normalize_invalid_global_regions(parsed)
    DeepSeekIntentParser._inherit_single_workload_region(parsed)

    assert parsed.services[1].region == "ap-southeast-1"
    assert parsed.ambiguities == []


def test_regional_s3_without_region_inherits_first_region_in_multi_region_quote() -> None:
    parsed = ParsedIntent(
        customer_summary="多区域工作负载",
        services=[
            ServiceRequirement(service="ec2", region="ap-northeast-1"),
            ServiceRequirement(service="rds", region="us-west-2"),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                region="global",
                requirements={"storage_gib": 30720, "storage_class": "standard"},
            ),
            ServiceRequirement(service="cloudfront", region="global"),
        ],
    )

    DeepSeekIntentParser._normalize_invalid_global_regions(parsed)
    DeepSeekIntentParser._inherit_single_workload_region(
        parsed,
        source_text=("区域：东京（ap-northeast-1）\n另一个组件：俄勒冈（us-west-2）\nS3：30TB"),
    )

    assert parsed.services[2].region == "ap-northeast-1"
    assert parsed.services[3].region == "global"
    assert parsed.ambiguities == []


def test_component_region_conflict_is_not_silently_inherited() -> None:
    parsed = ParsedIntent(
        customer_summary="区域冲突",
        services=[
            ServiceRequirement(service="ec2", region="ap-northeast-1"),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                source_text="S3 部署在东京或悉尼",
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions("区域：东京\nS3 部署在东京或悉尼", parsed)
    DeepSeekIntentParser._inherit_single_workload_region(
        parsed, source_text="区域：东京\nS3 部署在东京或悉尼"
    )

    assert parsed.services[1].region is None
    assert parsed.services[1].field_sources["region"] == "customer_region_conflict"
    assert len(parsed.ambiguities) == 1


def test_opensearch_optional_node_role_question_is_not_blocking() -> None:
    question = (
        "OpenSearch 3节点架构：未明确是3个独立节点还是包含Master、Data、"
        "Coordinating角色的集群，请确认。"
    )

    assert DeepSeekIntentParser._is_optional_opensearch_role_question(question)


def test_generic_kafka_is_normalized_to_managed_msk_without_question() -> None:
    parsed = ParsedIntent(
        customer_summary="Kafka",
        services=[ServiceRequirement(service="s3")],
        ambiguities=[],
    )

    DeepSeekIntentParser._append_explicit_minimum_services(
        "Kafka消息队列，3节点，每台4核16G。", parsed
    )

    assert any(item.service == "msk" for item in parsed.services)
    assert parsed.ambiguities == []


def test_explicit_self_hosted_kafka_still_uses_managed_msk_policy() -> None:
    keys = DeepSeekIntentParser._inventory_keys_for_line("EC2 自建 Kafka，3 个节点。")

    assert [key for key, _ in keys] == ["msk"]


def test_kafka_architecture_regression_is_repaired_to_managed_msk() -> None:
    source = (
        "4、Apache Kafka：区域：新加坡（ap-southeast-1），"
        "用途：业务消息队列和实时数据流处理，部署数量：3个Broker节点"
    )
    parsed = ParsedIntent(
        customer_summary="Kafka",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建 Apache Kafka）",
                region="ap-southeast-1",
                quantity=1,
                source_text="Apache Kafka：",
                field_sources={"_pending_architecture_decision": "system_policy"},
            )
        ],
        ambiguities=["AWS 没有与 Apache Kafka 完全等价的托管服务，采用托管还是自建？"],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, source)
    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    item = parsed.services[0]
    assert item.service == "msk"
    assert item.calculator_service_name == "Amazon MSK"
    assert item.quantity == 1
    assert item.requirements["broker_count"] == 3
    assert "_pending_architecture_decision" not in item.field_sources
    assert parsed.ambiguities == []


@pytest.mark.parametrize(
    ("product", "expected_service"),
    [
        ("Redis", "elasticache"),
        ("Valkey", "elasticache"),
        ("Memcached", "elasticache"),
        ("MySQL", "rds"),
        ("PostgreSQL", "rds"),
        ("MariaDB", "rds"),
        ("Prometheus", "amp"),
        ("RabbitMQ", "mq"),
        ("ActiveMQ", "mq"),
        ("MongoDB", "documentdb"),
        ("Elasticsearch", "opensearch"),
        ("Kubernetes", "eks"),
    ],
)
def test_known_full_managed_equivalents_never_become_self_hosted_questions(
    product: str, expected_service: str
) -> None:
    source = f"1、{product}：部署数量：3个节点"
    parsed = ParsedIntent(
        customer_summary=product,
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name=f"Amazon EC2（自建 {product}）",
                source_text=f"{product}：",
            )
        ],
        ambiguities=[f"{product} 采用 AWS 托管还是自建？"],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, source)

    assert parsed.services[0].service == expected_service
    assert "_pending_architecture_decision" not in parsed.services[0].field_sources
    assert parsed.ambiguities == []


@pytest.mark.parametrize(
    ("product", "expected_service", "expected_engine"),
    [
        ("Redis缓存", "elasticache", "redis"),
        ("MySQL数据库", "rds", "mysql"),
        ("PostgreSQL数据库", "rds", "postgresql"),
    ],
)
def test_native_managed_database_and_cache_names_never_ask_for_self_hosting(
    product: str, expected_service: str, expected_engine: str
) -> None:
    source = f"1、{product}：部署数量：1个节点"
    parsed = ParsedIntent(
        customer_summary=product,
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name=f"Amazon EC2（自建 {product}）",
                source_text=f"{product}：",
                field_sources={"_pending_architecture_decision": "system_policy"},
            )
        ],
        ambiguities=[f"AWS 没有与 {product} 完全等价的托管服务，采用托管还是自建？"],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, source)

    item = parsed.services[0]
    assert item.service == expected_service
    assert item.requirements["engine"] == expected_engine
    assert "_pending_architecture_decision" not in item.field_sources
    assert parsed.ambiguities == []


def test_generic_application_server_is_plain_ec2_not_a_managed_architecture_question() -> None:
    source = "1、应用服务器预计3台，单台16核128G，磁盘1T。"
    parsed = ParsedIntent(
        customer_summary="应用服务器",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建 应用服务器）",
                source_text="应用服务器预计3台，单台16核128G，磁盘1T。",
                field_sources={
                    "_pending_architecture_decision": "system_policy",
                    "_third_party_product": "应用服务器",
                },
            )
        ],
        ambiguities=[
            "AWS 没有与 应用服务器 完全等价的托管服务。采用托管还是在 EC2 自建？"
        ],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, source)

    item = parsed.services[0]
    assert item.service == "ec2"
    assert item.calculator_service_name == "Amazon EC2 云服务器"
    assert "_pending_architecture_decision" not in item.field_sources
    assert "_third_party_product" not in item.field_sources
    assert parsed.ambiguities == []


@pytest.mark.parametrize(
    ("expected_service", "product"),
    [
        (service, display_name)
        for service, display_name, _markers in DeepSeekIntentParser._INVENTORY_DEFINITIONS
        if service != "ec2" and service in SERVICE_TEMPLATE_FIELDS
    ],
)
def test_every_native_aws_inventory_product_bypasses_self_hosting_question(
    expected_service: str, product: str
) -> None:
    parsed = ParsedIntent(
        customer_summary=product,
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name=f"Amazon EC2（自建 {product}）",
                source_text=f"{product}：数量 1",
                field_sources={"_pending_architecture_decision": "system_policy"},
            )
        ],
        ambiguities=[f"{product} 采用 AWS 托管还是自建？"],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed)

    item = parsed.services[0]
    assert item.service == expected_service
    assert "_pending_architecture_decision" not in item.field_sources
    assert parsed.ambiguities == []


@pytest.mark.parametrize(
    "service",
    [service for service, fields in SERVICE_TEMPLATE_FIELDS.items() if "requests" in fields],
)
def test_every_request_metered_service_restores_chinese_monthly_request_total(
    service: str,
) -> None:
    parsed = ParsedIntent(
        customer_summary="请求量恢复",
        services=[
            ServiceRequirement(
                service=service,
                source_text="用于业务处理，每月大约5000万次请求",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    item = parsed.services[0]
    assert item.requirements["requests"] == 50_000_000
    assert item.field_sources["requirements.requests"] == "customer_text"
    assert "5000万次请求" in item.field_evidence["requirements.requests"]
    assert "requirements.requests" in item.locked_fields


def test_request_recovery_is_component_scoped_and_does_not_copy_waf_usage_to_sqs() -> None:
    parsed = ParsedIntent(
        customer_summary="组件隔离",
        services=[
            ServiceRequirement(
                service="sqs",
                source_text="消息队列：用于异步任务",
            ),
            ServiceRequirement(
                service="waf",
                source_text="Web 防护：每月请求量5000万次",
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert "requests" not in parsed.services[0].requirements
    assert parsed.services[1].requirements["requests"] == 50_000_000


def test_per_second_request_rate_is_not_treated_as_monthly_request_total() -> None:
    parsed = ParsedIntent(
        customer_summary="速率不是月用量",
        services=[
            ServiceRequirement(
                service="sqs",
                source_text="消息队列峰值每秒 1000 次请求",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert "requests" not in parsed.services[0].requirements


def test_monitoring_targets_do_not_become_duplicate_workload_components() -> None:
    text = """1、数据库，8核32G左右，磁盘先给600G。
2、监控和日志也一起算进去，服务器、数据库和容器都需要监控，日志量暂时没统计。"""

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    assert [item.service for item in parsed.services] == ["rds", "cloudwatch"]
    assert parsed.services[1].source_text.startswith("监控和日志")


def test_eks_colloquial_worker_word_order_restores_worker_fleet() -> None:
    source = "我们还有一套 K8s，1个集群，工作节点先放 4 台，每台大概 4核16G。"
    parsed = ParsedIntent(
        customer_summary="EKS",
        services=[
            ServiceRequirement(
                service="eks",
                calculator_service_name="Amazon Elastic Kubernetes Service (EKS)",
                quantity=1,
                source_text=source,
                requirements={"cluster_count": 1},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    eks = next(item for item in parsed.services if item.service == "eks")
    worker = next(
        item
        for item in parsed.services
        if item.calculator_service_name == "Amazon EC2 (EKS Worker Nodes)"
    )
    assert eks.quantity == 1
    assert worker.quantity == 4
    assert worker.requirements["vcpu"] == 4
    assert worker.requirements["memory_gib"] == 16
    assert worker.requirements["operating_system"] == "Linux"


def test_missing_required_product_family_choice_becomes_customer_question() -> None:
    parsed = ParsedIntent(
        customer_summary="数据库",
        services=[
            ServiceRequirement(
                service="rds",
                calculator_service_name="Amazon RDS",
                source_text="数据库，8核32G左右，磁盘先给600G。",
                requirements={"vcpu": 8, "memory_gib": 32, "storage_gib": 600},
            )
        ],
    )

    DeepSeekIntentParser._append_missing_required_choice_questions(parsed)

    assert len(parsed.ambiguities) == 1
    assert "数据库类型" in parsed.ambiguities[0]
    assert "MySQL" in parsed.ambiguities[0]
    assert "PostgreSQL" in parsed.ambiguities[0]


def test_explicit_database_engine_does_not_ask_redundant_choice() -> None:
    parsed = ParsedIntent(
        customer_summary="MySQL",
        services=[
            ServiceRequirement(
                service="rds",
                source_text="MySQL数据库，8核32G。",
                requirements={"engine": "mysql", "vcpu": 8, "memory_gib": 32},
            )
        ],
    )

    DeepSeekIntentParser._append_missing_required_choice_questions(parsed)

    assert parsed.ambiguities == []


def test_full_colloquial_workload_preserves_each_component_without_cross_talk() -> None:
    text = """1、服务器先要 5 台，win，差不多 4C16G 就行，主要跑 Java 服务。
2、数据库，8核32G左右，磁盘先给 600G。
3、Redis 做一主一从，缓存大概需要 50G 左右。
4、我们还有一套 K8s，1个集群，工作节点先放 4 台，每台大概 4核16G。
5、负载均衡放两个，主要做 HTTPS。
6、Kafka 先按 3 个 Broker 来，单个 Broker 的配置不确定。
7、对象存储先按 25TB 算。
8、CloudFront 每月流量 4TB 左右。
9、WAF 每月请求量大概 3000 万。
10、监控和日志也一起算进去，服务器、数据库和容器都需要监控，日志量暂时没统计。
区域统一放新加坡。"""

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)
    assert parsed is not None
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._append_missing_required_choice_questions(parsed)
    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    assert [item.service for item in parsed.services].count("rds") == 1
    assert [item.service for item in parsed.services].count("cloudwatch") == 1
    ec2 = parsed.services[0]
    rds = next(item for item in parsed.services if item.service == "rds")
    elb = next(item for item in parsed.services if item.service == "elb")
    waf = next(item for item in parsed.services if item.service == "waf")
    worker = next(
        item
        for item in parsed.services
        if item.calculator_service_name == "Amazon EC2 (EKS Worker Nodes)"
    )
    assert (ec2.quantity, ec2.requirements["vcpu"], ec2.requirements["memory_gib"]) == (5, 4, 16)
    assert rds.requirements["storage_gib"] == 600
    assert elb.quantity == 2
    assert waf.requirements["requests"] == 30_000_000
    assert (worker.quantity, worker.requirements["vcpu"], worker.requirements["memory_gib"]) == (
        4,
        4,
        16,
    )
    assert any("数据库类型" in question for question in parsed.ambiguities)


def test_explicit_jakarta_region_overrides_ai_sydney_guess() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-2"),
            ServiceRequirement(service="rds", region="ap-southeast-2"),
            ServiceRequirement(service="cloudfront", region=None),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions("部署区域：亚太地区（雅加达）。", parsed)

    assert parsed.services[0].region == "ap-southeast-3"
    assert parsed.services[1].region == "ap-southeast-3"
    assert parsed.services[2].region is None


def test_component_region_overrides_global_default_without_affecting_siblings() -> None:
    parsed = ParsedIntent(
        customer_summary="多区域测试",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                source_text="应用服务器：4台，8核16G。",
            ),
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                source_text=(
                    "Amazon EC2：需要1台服务器，区域为悉尼（ap-southeast-2），Linux系统，8核16G。"
                ),
            ),
            ServiceRequirement(
                service="data_transfer",
                region="ap-southeast-1",
                source_text="公网出站流量：新加坡区域每月5TB。",
            ),
            ServiceRequirement(service="cloudfront", region=None),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions(
        "区域：新加坡（ap-southeast-1）\n"
        "1、应用服务器：4台，8核16G。\n"
        "2、Amazon EC2：需要1台服务器，区域为悉尼（ap-southeast-2）。\n"
        "3、公网出站流量：新加坡区域每月5TB。",
        parsed,
    )

    assert parsed.services[0].region == "ap-southeast-1"
    assert parsed.services[0].field_sources["region"] == "customer_global_default"
    assert "region" not in parsed.services[0].locked_fields
    assert parsed.services[1].region == "ap-southeast-2"
    assert parsed.services[1].field_sources["region"] == "customer_text"
    assert "region" in parsed.services[1].locked_fields
    assert parsed.services[2].region == "ap-southeast-1"
    assert parsed.services[3].region is None


def test_service_line_regions_are_not_mistaken_for_a_global_default() -> None:
    parsed = ParsedIntent(
        customer_summary="多区域测试",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                source_text="EC2：区域为悉尼，1台。",
            ),
            ServiceRequirement(
                service="rds",
                region="ap-southeast-1",
                source_text="RDS：区域为新加坡，1套。",
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions(
        "1、EC2：区域为悉尼，1台。\n2、RDS：区域为新加坡，1套。",
        parsed,
    )

    assert parsed.services[0].region == "ap-southeast-2"
    assert parsed.services[1].region == "ap-southeast-1"


def test_conflicting_regions_inside_one_component_require_confirmation() -> None:
    parsed = ParsedIntent(
        customer_summary="冲突测试",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                source_text="EC2 区域写了新加坡，同时又写悉尼，请确认。",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions("", parsed)

    assert parsed.services[0].region is None
    assert len(parsed.ambiguities) == 1
    assert "多个区域" in parsed.ambiguities[0]


def test_nonnumeric_usage_and_redis_set_count_cannot_become_capacity() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text="Amazon ElastiCache for Redis：Redis，1主1从，共2个节点，1套。",
                requirements={"memory_gib": 1024},
            ),
            ServiceRequirement(
                service="s3",
                source_text="Amazon S3：标准对象存储，按实际存储量计费。",
                requirements={"storage_gib": 1},
            ),
            ServiceRequirement(
                service="data_transfer",
                source_text="公网出网流量：按实际使用流量计费。",
                requirements={"data_transfer_out_gib": 1},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert "memory_gib" not in parsed.services[0].requirements
    assert "storage_gib" not in parsed.services[1].requirements
    assert "data_transfer_out_gib" not in parsed.services[2].requirements


def test_explicit_redis_node_memory_is_preserved() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text="Redis 一主一从，每个节点内存不低于 8 GiB。",
                requirements={"memory_gib": 1024},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert parsed.services[0].requirements["memory_gib"] == 8


def test_compact_compute_rows_keep_disk_memory_and_bandwidth_separate() -> None:
    parsed = ParsedIntent(
        customer_summary="compute and database",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text="Amazon EC2｜4核16G｜500GB系统盘｜20Mbps公网带宽",
                requirements={
                    "vcpu": 4,
                    "memory_gib": 16,
                    "system_disk_gib": 16,
                    "data_transfer_out_gib": 20,
                },
            ),
            ServiceRequirement(
                service="rds",
                source_text="Amazon RDS MySQL｜8核32G｜500GB存储｜主备高可用",
                requirements={"vcpu": 8, "memory_gib": 32768, "storage_gib": 500},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert parsed.services[0].requirements["system_disk_gib"] == 500
    assert parsed.services[0].requirements["memory_gib"] == 16
    assert "data_transfer_out_gib" not in parsed.services[0].requirements
    assert parsed.services[1].requirements["vcpu"] == 8
    assert parsed.services[1].requirements["memory_gib"] == 32
    assert parsed.services[1].requirements["storage_gib"] == 500


def test_redis_service_alias_is_normalized_to_elasticache() -> None:
    normalized = DeepSeekIntentParser._normalize(
        {
            "customer_summary": "Redis",
            "services": [
                {
                    "service": "redis",
                    "quantity": 2,
                    "requirements": {"engine": "redis", "memory_gib": 8},
                }
            ],
            "ambiguities": [],
        }
    )

    assert normalized["services"][0]["service"] == "elasticache"  # type: ignore[index]


def test_single_service_returned_at_root_is_wrapped_as_services_list() -> None:
    normalized = DeepSeekIntentParser._normalize(
        {
            "service": "ec2",
            "calculator_service_name": "Amazon EC2",
            "quantity": 3,
            "requirements": {"vcpu": 1, "memory_gib": 8},
            "ambiguities": [],
        },
        fallback_summary="东京 EC2",
    )

    parsed = ParsedIntent.model_validate(normalized)

    assert parsed.customer_summary == "东京 EC2"
    assert len(parsed.services) == 1
    assert parsed.services[0].service == "ec2"


def test_alb_backend_reference_does_not_create_extra_ec2_workload() -> None:
    parsed = ParsedIntent(
        customer_summary="负载均衡",
        services=[
            ServiceRequirement(
                service="elastic-load-balancing",
                calculator_service_name="Elastic Load Balancing",
            ),
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                source_text="后端挂 3 台应用服务器",
            ),
        ],
    )

    DeepSeekIntentParser._drop_referenced_only_ec2(
        "负载均衡：1 个 ALB，后端挂 3 台应用服务器，HTTPS 访问。",
        parsed,
    )

    assert [item.service for item in parsed.services] == ["elastic-load-balancing"]


def test_explicit_architecture_conflicts_survive_small_model_output() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(service="rds", requirements={"deployment": "single_az"}),
            ServiceRequirement(service="elasticache", requirements={"memory_gib": 8}),
            ServiceRequirement(service="elb", requirements={"load_balancer_type": "application"}),
        ],
    )
    text = (
        "数据库用 Single-AZ，但要求主备自动故障切换。"
        "Redis 整套缓存只需要 1G，但每个节点至少 8G。"
        "使用 Application Load Balancer，固定一个公网 IP，IP 永远不变。"
    )

    DeepSeekIntentParser._append_explicit_design_conflicts(text, parsed)

    assert parsed.ambiguities == [
        "RDS Single-AZ 与主备自动故障切换冲突",
        "ALB 不支持固定公网 IP",
        "Redis 整套 1G 与每节点 8G 的要求冲突",
    ]


def test_cross_service_design_conflicts_are_detected_before_pricing() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[ServiceRequirement(service="ec2", requirements={"vcpu": 4})],
    )
    text = (
        "服务器全部放在一个可用区，同时要求可用区故障时切到另一个可用区。"
        "RDS 使用 Multi-AZ，并让备用库跑只读查询。"
        "Redis 两节点部署在同一个可用区，但要求可用区故障时自动切换。"
        "NLB 按 URL 路径把 /api 和 /static 转发到不同目标。"
        "S3 Standard 七天后自动转成 S3 Express One Zone。"
        "CloudFront 要求固定不变的公网 IP。"
    )

    DeepSeekIntentParser._append_explicit_design_conflicts(text, parsed)

    assert len(parsed.ambiguities) == 6
    assert "NLB 不支持按 URL 路径转发" in parsed.ambiguities
    assert any("Anycast Static IP" in item for item in parsed.ambiguities)


def test_ec2_availability_zone_conflict_is_not_assigned_to_redis() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(service="ec2", requirements={"vcpu": 4}),
            ServiceRequirement(service="elasticache", requirements={"memory_gib": 8}),
        ],
    )
    text = (
        "Redis 一主一从，共 2 个节点，单节点 8G 内存。\n"
        "3 台 EC2 全部放在同一个可用区，但希望单个可用区故障时应用自动保持高可用。"
    )

    DeepSeekIntentParser._append_explicit_design_conflicts(text, parsed)

    assert parsed.ambiguities == ["EC2 单可用区部署与跨可用区自动切换要求冲突"]


def test_numbered_customer_acceptance_resolves_the_matching_question() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[ServiceRequirement(service="ec2", requirements={"vcpu": 4})],
    )
    text = (
        "3 台 EC2 全部放在同一个可用区，但希望单个可用区故障时应用自动保持高可用。\n\n"
        "【客户确认回复】\n1 同意"
    )

    DeepSeekIntentParser._append_explicit_design_conflicts(text, parsed)

    assert parsed.ambiguities == []


def test_plain_customer_acceptance_resolves_all_questions_on_current_page() -> None:
    notices = [
        "EC2 单可用区部署与跨可用区自动切换要求冲突",
        "ALB 不支持固定公网 IP",
    ]
    text = "原始需求\n\n【客户确认回复】\n同意"

    remaining = DeepSeekIntentParser._apply_confirmation_replies(notices, text)

    assert remaining == []


def test_plain_acceptance_is_not_sent_back_as_a_new_workload() -> None:
    text = "东京 2 台 EC2。\n\n【客户确认回复】\n同意"

    assert DeepSeekIntentParser._text_for_ai(text) == "东京 2 台 EC2。"


def test_customer_reply_with_new_configuration_is_kept_as_supplement() -> None:
    text = "Redis 一主一从。\n\n【客户确认回复】\nRedis 每节点 8G"

    assert DeepSeekIntentParser._text_for_ai(text) == (
        "Redis 一主一从。\n\n客户补充确认：\nRedis 每节点 8G"
    )


def test_model_added_services_are_removed_when_customer_only_asks_for_ec2() -> None:
    parsed = ParsedIntent(
        customer_summary="错误地带入旧需求",
        services=[
            ServiceRequirement(
                service="ec2",
                quantity=3,
                requirements={"vcpu": 1, "memory_gib": 8, "system_disk_gib": 100},
                source_text="旧的 EC2 需求",
            ),
            ServiceRequirement(service="rds", requirements={"engine": "mysql"}),
            ServiceRequirement(service="elasticache", requirements={"engine": "redis"}),
            ServiceRequirement(service="elastic-load-balancing"),
            ServiceRequirement(service="s3", requirements={"storage_gib": 2048}),
            ServiceRequirement(service="cloudfront", requirements={"data_transfer_out_gib": 5120}),
        ],
    )
    text = "东京区域需要 2 台 Linux EC2，4核16G，每台 200GB gp3 系统盘，按需运行整月。"

    DeepSeekIntentParser._drop_unrequested_services(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert [item.service for item in parsed.services] == ["ec2"]
    assert parsed.services[0].quantity == 2
    assert parsed.services[0].requirements["vcpu"] == 4
    assert parsed.services[0].requirements["memory_gib"] == 16
    assert parsed.services[0].requirements["system_disk_gib"] == 200


def test_bare_ec2_instance_models_count_as_explicit_ec2_request() -> None:
    text = (
        "开发环境：m6g.large，2核8G，100G 存储，1 台\n生产环境：c6g.xlarge，4核8G，100G 存储，2 台"
    )
    parsed = ParsedIntent(
        customer_summary=text,
        services=[
            ServiceRequirement(service="amazon_ec2", source_text=text.splitlines()[0]),
            ServiceRequirement(service="ec2", source_text=text.splitlines()[1]),
        ],
    )

    DeepSeekIntentParser._drop_unrequested_services(text, parsed)
    DeepSeekIntentParser._drop_referenced_only_ec2(text, parsed)

    assert len(parsed.services) == 2
    assert DeepSeekIntentParser._service_key("amazon_ec2") == "ec2"


def test_compact_architecture_list_preserves_each_ec2_disk_and_kinesis_component() -> None:
    text = """1、应用服务器1：EC2 c6i.xlarge (4C8G) + gp3 200GB，数量1
2、应用服务器2：EC2 m6i.xlarge (4C16G) + gp3 200GB，数量1
5、数据同步管道：AWS DMS + Kinesis Data Streams (2 shards)，数量1"""
    lines = text.splitlines()
    # Simulate a valid AI response that omitted both EC2 disks and the second
    # product named in the DMS + Kinesis line.
    parsed = ParsedIntent(
        customer_summary=text,
        services=[
            ServiceRequirement(service="ec2", source_text=lines[0]),
            ServiceRequirement(service="ec2", source_text=lines[1]),
            ServiceRequirement(service="dms", source_text=lines[2]),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._isolate_shared_component_sources(parsed)
    DeepSeekIntentParser._reconcile_explicit_models(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    ec2_items = [item for item in parsed.services if item.service == "ec2"]
    assert [item.requirements["system_disk_gib"] for item in ec2_items] == [200, 200]
    assert [item.requirements["volume_type"] for item in ec2_items] == ["gp3", "gp3"]
    kinesis = next(item for item in parsed.services if item.service == "kinesis")
    assert kinesis.requirements["shards"] == 2
    assert "Kinesis Data Streams" in kinesis.source_text


def test_compact_ec2_shape_plus_capacity_is_preserved_as_system_disk() -> None:
    text = """应用服务器1（4C8G + 200G）
应用服务器2（4C16G + 200G）"""
    parsed = ParsedIntent(
        customer_summary=text,
        services=[
            ServiceRequirement(service="ec2", source_text=line) for line in text.splitlines()
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert [item.requirements["vcpu"] for item in parsed.services] == [4, 4]
    assert [item.requirements["memory_gib"] for item in parsed.services] == [8, 16]
    assert [item.requirements["system_disk_gib"] for item in parsed.services] == [
        200,
        200,
    ]
    assert all(
        item.field_sources["requirements.system_disk_gib"] == "customer_text"
        for item in parsed.services
    )


@pytest.mark.asyncio
async def test_ai_valid_cannot_waive_literal_component_fact() -> None:
    class AlwaysValidAuditGateway:
        supports_component_audit = True

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            return {"valid": True, "issues": []}

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = AlwaysValidAuditGateway()  # type: ignore[assignment]
    original = ServiceRequirement(
        service="ec2",
        source_text="应用服务器1（4C8G + 200G）",
    )
    filled = ServiceRequirement(
        service="ec2",
        requirements={"vcpu": 4, "memory_gib": 8},
        source_text=original.source_text,
    )

    issues = await parser._component_audit_issues(
        index=0,
        original_component=original,
        filled=filled,
        runtime_defaults={},
        semaphore=asyncio.Semaphore(1),
        reporter=None,
    )

    assert any("system_disk_gib=200" in issue for issue in issues)


def test_environment_lines_with_bare_models_survive_full_service_filter() -> None:
    text = (
        "开发环境：m6g.large，2核8G，100G 存储，1 台\n"
        "测试环境：m6g.large，2核8G，100G 存储，1 台\n"
        "生产环境：c6g.xlarge，4核8G，100G 存储，2 台"
    )
    parsed = ParsedIntent(
        customer_summary=text,
        services=[
            ServiceRequirement(
                service="compute",
                source_text=line,
                requirements={"requested_model": line.split("：", 1)[1].split("，", 1)[0]},
            )
            for line in text.splitlines()
        ],
    )

    DeepSeekIntentParser._drop_unrequested_services(text, parsed)
    DeepSeekIntentParser._drop_referenced_only_ec2(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_models(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert [item.service for item in parsed.services] == ["ec2", "ec2", "ec2"]
    assert [item.quantity for item in parsed.services] == [1, 1, 2]
    assert [item.requirements["requested_model"] for item in parsed.services] == [
        "m6g.large",
        "m6g.large",
        "c6g.xlarge",
    ]


def test_ai_ec2_disk_alias_is_canonicalized_before_validation() -> None:
    normalized = DeepSeekIntentParser._normalize(
        {
            "customer_summary": "开发环境 100G 存储",
            "services": [
                {
                    "service": "ec2",
                    "region": "eu-west-2",
                    "quantity": 1,
                    "requirements": {
                        "requested_model": "m6g.large",
                        "system_disk_size_gib": 100,
                    },
                    "source_text": "开发环境：m6g.large，100G 存储",
                }
            ],
        }
    )

    requirements = normalized["services"][0]["requirements"]
    assert requirements["system_disk_gib"] == 100
    assert "system_disk_size_gib" not in requirements


def test_explicit_auxiliary_services_are_recovered_with_customer_capacities() -> None:
    text = (
        "云硬盘\t全球\tgp3 云盘，每台 500GB，共 1000GB\n"
        "公网出网流量\t新加坡、悉尼、香港流量到国内用户\t"
        "按 1000GB/月 公网出网流量估算\n"
        "全球访问加速 GA\t全球\tAWS Global Accelerator，1 个加速器，"
        "按 1000GB/月 加速流量估算"
    )
    parsed = ParsedIntent.model_construct(customer_summary="辅助服务", services=[], ambiguities=[])

    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    services = {item.service: item for item in parsed.services}
    assert services["ebs"].quantity == 2
    assert services["ebs"].requirements["storage_gib"] == 500
    assert services["ebs"].requirements["total_storage_gib"] == 1000
    assert services["ebs"].requirements["volume_type"] == "gp3"
    assert services["data_transfer"].requirements["data_transfer_out_gib"] == 1000
    assert services["global_accelerator"].requirements["accelerators"] == 1
    assert services["global_accelerator"].requirements["data_transfer_out_gib"] == 1000


def test_cloudfront_traffic_cannot_bleed_into_global_accelerator() -> None:
    text = (
        "1、Amazon CloudFront：静态资源加速，每月公网流量8TB。\n"
        "2、AWS Global Accelerator：配置1个加速器。"
    )
    parsed = ParsedIntent(
        customer_summary="内容分发与全球加速",
        services=[
            ServiceRequirement(
                service="cloudfront",
                calculator_service_name="Amazon CloudFront",
                source_text="Amazon CloudFront：静态资源加速，每月公网流量8TB。",
            ),
            ServiceRequirement(
                service="global_accelerator",
                calculator_service_name="AWS Global Accelerator",
                source_text="AWS Global Accelerator：配置1个加速器。",
                # Simulate a model copying the neighbouring CloudFront field.
                requirements={"data_transfer_out_gib": 8192},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    services = {item.service: item for item in parsed.services}
    assert services["cloudfront"].requirements["data_transfer_out_gib"] == 8192
    assert "data_transfer_out_gib" not in services["global_accelerator"].requirements
    assert services["global_accelerator"].requirements["accelerators"] == 1


def test_repeated_storage_derives_missing_count_from_per_unit_and_total() -> None:
    parsed = ParsedIntent(
        customer_summary="重复资源",
        services=[
            ServiceRequirement(
                service="ebs",
                calculator_service_name="Amazon EBS",
                source_text="云硬盘：gp3，每块500GB，共1000GB",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    disk = parsed.services[0]
    assert disk.quantity == 2
    assert disk.requirements == {
        "storage_gib": 500,
        "total_storage_gib": 1000,
        "volume_type": "gp3",
    }
    assert disk.field_sources["quantity"] == "customer_text"
    assert not parsed.ambiguities


@pytest.mark.parametrize(
    ("service", "source", "count_field", "per_field"),
    [
        ("msk", "Kafka每个Broker存储500GB，共1500GB", "broker_count", "storage_gib_per_broker"),
        (
            "opensearch",
            "OpenSearch每个数据节点存储500GB，总容量1500GB",
            "data_nodes",
            "storage_gib_per_node",
        ),
        ("mq", "RabbitMQ每个Broker存储100GB，合计300GB", "broker_count", "storage_gib_per_broker"),
    ],
)
def test_repeated_node_services_share_capacity_consistency_guard(
    service: str,
    source: str,
    count_field: str,
    per_field: str,
) -> None:
    parsed = ParsedIntent(
        customer_summary="重复节点",
        services=[ServiceRequirement(service=service, source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    requirements = parsed.services[0].requirements
    assert requirements[count_field] == 3
    assert requirements[per_field] in {100, 500}
    assert requirements["total_storage_gib"] in {300, 1500}
    assert not parsed.ambiguities


def test_conflicting_repeated_storage_becomes_one_customer_question() -> None:
    parsed = ParsedIntent(
        customer_summary="冲突容量",
        services=[
            ServiceRequirement(
                service="ebs",
                calculator_service_name="Amazon EBS",
                source_text="云硬盘数量3块，每块500GB，共1000GB",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    assert parsed.services[0].quantity == 3
    assert len(parsed.ambiguities) == 1
    assert "不一致" in parsed.ambiguities[0]
    assert "500" in parsed.ambiguities[0]
    assert "1000" in parsed.ambiguities[0]


@pytest.mark.parametrize(
    ("source", "expected_count", "expected_per", "expected_total"),
    [
        ("云硬盘数量2块，每块500GB", 2, 500, 1000),
        ("2块云硬盘，总容量1000GB", 2, 500, 1000),
    ],
)
def test_ebs_derives_any_missing_member_of_capacity_equation(
    source: str,
    expected_count: int,
    expected_per: int,
    expected_total: int,
) -> None:
    parsed = ParsedIntent(
        customer_summary="云硬盘",
        services=[ServiceRequirement(service="ebs", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    disk = parsed.services[0]
    assert disk.quantity == expected_count
    assert disk.requirements["storage_gib"] == expected_per
    assert disk.requirements["total_storage_gib"] == expected_total


def test_node_memory_is_not_mistaken_for_per_node_storage() -> None:
    parsed = ParsedIntent(
        customer_summary="节点内存",
        services=[
            ServiceRequirement(
                service="msk",
                source_text="Kafka 3个节点，每个节点16GB内存",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    assert "storage_gib_per_broker" not in parsed.services[0].requirements
    assert "total_storage_gib" not in parsed.services[0].requirements


def test_opensearch_per_node_capacity_cannot_become_node_count() -> None:
    source = "Amazon OpenSearch Service，3个节点，每节点500GB存储，数量1套"
    parsed = ParsedIntent(
        customer_summary="OpenSearch",
        services=[ServiceRequirement(service="opensearch", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    requirements = parsed.services[0].requirements
    assert requirements["data_nodes"] == 3
    assert requirements["storage_gib_per_node"] == 500
    assert requirements["total_storage_gib"] == 1500
    assert not parsed.ambiguities


def test_inventory_short_alias_does_not_match_inside_another_service_name() -> None:
    keys = {
        key
        for key, _ in DeepSeekIntentParser._inventory_keys_for_line(
            "Amazon EKS，Kubernetes集群，数量1套"
        )
    }

    assert keys == {"eks"}


def test_post_component_inventory_removes_cross_service_duplicate() -> None:
    text = """1、Amazon OpenSearch Service：3个节点，每节点500GB存储。
2、Amazon EKS：Kubernetes集群，数量1套。"""
    parsed = ParsedIntent(
        customer_summary="搜索与容器",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text="Amazon OpenSearch Service：3个节点，每节点500GB存储。",
            ),
            ServiceRequirement(
                service="eks",
                source_text="Amazon EKS：Kubernetes集群，数量1套。",
            ),
            ServiceRequirement(
                service="opensearch",
                source_text="Amazon EKS：Kubernetes集群，数量1套。",
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    assert [item.service for item in parsed.services] == ["opensearch", "eks"]


def test_component_evidence_rejects_capacity_used_as_node_count() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="opensearch",
        source_text="OpenSearch 3个节点，每节点500GB存储",
    )
    raw = {
        "component": {
            "service": "opensearch",
            "region": None,
            "quantity": 1,
            "requirements": {"data_nodes": 500},
            "field_evidence": {"requirements.data_nodes": "节点500"},
            "source_text": component.source_text,
            "query_action": None,
        }
    }

    with pytest.raises(ValueError, match="不是明确的数量表达"):
        parser._component_from_template_output(raw, component)


@pytest.mark.parametrize(
    ("field", "value", "snippet"),
    [
        ("master_nodes", 1, "1个主节点"),
        ("core_nodes", 3, "3个核心节点"),
        ("task_nodes", 2, "2个任务节点"),
    ],
)
def test_component_evidence_accepts_generic_role_node_counts(
    field: str,
    value: int,
    snippet: str,
) -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(service="emr", source_text=snippet)
    raw = {
        "component": {
            "service": "emr",
            "region": None,
            "quantity": 1,
            "requirements": {field: value},
            "field_evidence": {f"requirements.{field}": snippet},
            "source_text": component.source_text,
            "query_action": None,
        }
    }

    parsed = parser._component_from_template_output(raw, component)

    assert parsed.requirements[field] == value


def test_selective_audit_only_flags_suspicious_incomplete_repeated_component() -> None:
    source = "OpenSearch 3个节点，每节点500GB存储"
    original = ServiceRequirement(service="opensearch", source_text=source)
    incomplete = ServiceRequirement(
        service="opensearch",
        source_text=source,
        requirements={"data_nodes": 3},
        field_evidence={"requirements.data_nodes": "3个节点"},
    )
    complete = ServiceRequirement(
        service="opensearch",
        source_text=source,
        requirements={
            "data_nodes": 3,
            "storage_gib_per_node": 500,
            "total_storage_gib": 1500,
        },
        field_evidence={
            "requirements.data_nodes": "3个节点",
            "requirements.storage_gib_per_node": "每节点500GB存储",
            "requirements.total_storage_gib": "system_derived",
        },
    )

    assert DeepSeekIntentParser._needs_selective_component_audit(original, incomplete)
    assert not DeepSeekIntentParser._needs_selective_component_audit(original, complete)


@pytest.mark.asyncio
async def test_every_component_is_audited_and_repaired_against_its_own_source() -> None:
    class AuditRepairGateway:
        supports_component_audit = True

        def __init__(self) -> None:
            self.extractions = 0
            self.audits = 0

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            prompt = str(kwargs.get("system_prompt") or "")
            if "结构化结果审核员" in prompt:
                self.audits += 1
                if self.audits == 1:
                    return {"valid": False, "issues": ["漏填 gp3 200GB 系统盘"]}
                return {"valid": True, "issues": []}
            self.extractions += 1
            requirements: dict[str, object] = {
                "vcpu": 4,
                "memory_gib": 8,
                "operating_system": "linux",
            }
            evidence: dict[str, str] = {
                "requirements.vcpu": "4C",
                "requirements.memory_gib": "8G",
                "requirements.operating_system": "system_minimum",
            }
            if self.extractions > 1:
                requirements.update({"system_disk_gib": 200, "volume_type": "gp3"})
                evidence.update(
                    {
                        "requirements.system_disk_gib": "gp3 200GB",
                        "requirements.volume_type": "gp3",
                    }
                )
            return {
                "component": {
                    "service": "ec2",
                    "calculator_service_name": "Amazon EC2",
                    "region": "ap-southeast-1",
                    "quantity": 1,
                    "hours_per_month": 730,
                    "requirements": requirements,
                    "field_evidence": evidence,
                    "source_text": "EC2 4C8G + gp3 200GB",
                    "query_action": None,
                }
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = AuditRepairGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-southeast-1",
        source_text="EC2 4C8G + gp3 200GB",
    )

    cleaned = await parser._cleanup_components(
        component.source_text,
        ParsedIntent(customer_summary="EC2", services=[component]),
    )

    # One additional call may be used to obtain the minimum-runtime default;
    # the important contract is extraction -> audit -> repair -> re-audit.
    assert gateway.extractions >= 2
    assert gateway.audits == 2
    assert cleaned.services[0].requirements["system_disk_gib"] == 200
    assert cleaned.services[0].requirements["volume_type"] == "gp3"


@pytest.mark.asyncio
async def test_free_form_component_edit_is_repaired_then_reaudited() -> None:
    class EditAuditGateway:
        supports_component_audit = True

        def __init__(self) -> None:
            self.extractions = 0
            self.audits = 0

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            prompt = str(kwargs.get("system_prompt") or "")
            if "结构化结果审核员" in prompt:
                self.audits += 1
                if self.audits == 1:
                    return {"valid": False, "issues": ["漏填每月出站 1TB"]}
                return {"valid": True, "issues": []}
            self.extractions += 1
            requirements: dict[str, object] = {
                "storage_gib": 30720,
                "storage_class": "standard",
            }
            evidence: dict[str, str] = {
                "requirements.storage_gib": "容量改成30TB",
            }
            if self.extractions > 1:
                requirements["data_transfer_out_gib"] = 1024
                evidence["requirements.data_transfer_out_gib"] = "每月出站1TB"
            return {
                "component": {
                    "service": "s3",
                    "calculator_service_name": "Amazon S3",
                    "region": "ap-southeast-1",
                    "quantity": 1,
                    "hours_per_month": 730,
                    "requirements": requirements,
                    "field_evidence": evidence,
                    "source_text": "S3 30TB，每月出站1TB",
                    "query_action": None,
                }
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = EditAuditGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    component = ServiceRequirement(
        service="s3",
        calculator_service_name="Amazon S3",
        region="ap-southeast-1",
        requirements={"storage_gib": 20480, "storage_class": "standard"},
        source_text="S3 20TB",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "容量改成30TB，每月出站1TB",
    )

    assert gateway.extractions == 2
    assert gateway.audits == 2
    assert revised.requirements["storage_gib"] == 30720
    assert revised.requirements["data_transfer_out_gib"] == 1024


def test_component_template_derives_missing_ai_disk_count() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="ebs",
        calculator_service_name="Amazon EBS",
        source_text="云硬盘：gp3，每块500GB，共1000GB",
    )
    raw = {
        "component": {
            "service": "ebs",
            "calculator_service_name": "Amazon EBS",
            "region": None,
            "quantity": None,
            "hours_per_month": None,
            "requirements": {
                "storage_gib": 500,
                "total_storage_gib": 1000,
                "volume_type": "gp3",
            },
            "field_evidence": {
                "requirements.storage_gib": "每块500GB",
                "requirements.total_storage_gib": "共1000GB",
                "requirements.volume_type": "gp3",
            },
            "source_text": component.source_text,
            "query_action": None,
        }
    }

    result = parser._component_from_template_output(raw, component)

    assert result.quantity == 2
    assert result.field_evidence["quantity"] == "system_derived"


def test_component_template_accepts_ai_derived_disk_count_after_self_correction() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="ebs",
        calculator_service_name="Amazon EBS",
        source_text="云硬盘：gp3，每块500GB，共1000GB",
    )
    raw = {
        "component": {
            "service": "ebs",
            "calculator_service_name": "Amazon EBS",
            "region": None,
            "quantity": 2,
            "hours_per_month": None,
            "requirements": {
                "storage_gib": 500,
                "total_storage_gib": 1000,
                "volume_type": "gp3",
            },
            "field_evidence": {
                "quantity": "system_derived",
                "requirements.storage_gib": "每块500GB",
                "requirements.total_storage_gib": "共1000GB",
                "requirements.volume_type": "gp3",
            },
            "source_text": component.source_text,
            "query_action": None,
        }
    }

    result = parser._component_from_template_output(raw, component)

    assert result.quantity == 2
    assert result.requirements["storage_gib"] == 500
    assert result.requirements["total_storage_gib"] == 1000


def test_ec2_ebs_disk_wording_does_not_create_standalone_ebs_service() -> None:
    text = "EC2 两台，每台 100GB EBS gp3 系统盘"
    parsed = ParsedIntent(
        customer_summary="EC2",
        services=[ServiceRequirement(service="ec2", source_text=text)],
    )

    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)

    assert [item.service for item in parsed.services] == ["ec2"]


def test_numbered_component_dependency_does_not_create_an_extra_service() -> None:
    text = "3、Amazon Macie：数量1，检查500个S3存储桶，每月扫描20TB敏感数据"
    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)
    assert parsed is not None

    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)

    assert len(parsed.services) == 1
    assert "macie" in parsed.services[0].service


def test_discovered_pricing_fields_are_recovered_from_customer_text() -> None:
    component = ServiceRequirement(
        service="amazon_appflow",
        source_text="Amazon AppFlow：数量1，每月运行300次流程，每月处理2048GB数据",
    )

    DeepSeekIntentParser._overlay_literal_component_facts(
        component.source_text,
        component,
        extra_fields=("flow_runs", "data_processed_gib"),
    )

    assert component.requirements["flow_runs"] == 300
    assert component.requirements["data_processed_gib"] == 2048


def test_processed_traffic_is_not_relabelled_as_outbound_transfer() -> None:
    component = ServiceRequirement(
        service="network_firewall",
        source_text="AWS Network Firewall：每月共处理15TB流量",
    )

    DeepSeekIntentParser._overlay_literal_component_facts(
        component.source_text,
        component,
        extra_fields=("data_processed_gib", "data_transfer_out_gib"),
    )

    assert component.requirements["data_processed_gib"] == 15 * 1024
    assert "data_transfer_out_gib" not in component.requirements


def test_discovered_scan_and_bucket_fields_are_recovered_together() -> None:
    component = ServiceRequirement(
        service="macie",
        source_text="Amazon Macie：检查500个S3存储桶，每月扫描20TB敏感数据",
    )

    DeepSeekIntentParser._overlay_literal_component_facts(
        component.source_text,
        component,
        extra_fields=("bucket_count", "data_scanned_gib"),
    )

    assert component.requirements["bucket_count"] == 500
    assert component.requirements["data_scanned_gib"] == 20 * 1024


def test_neptune_topology_and_backup_storage_survive_literal_recovery() -> None:
    source = (
        "Amazon Neptune：数量1，1个Writer节点和2个Reader节点，"
        "实例型号db.r6g.xlarge，单节点4核32GB，数据库存储500GB，备份存储100GB"
    )
    component = ServiceRequirement(
        service="amazon_neptune",
        calculator_service_name="Amazon Neptune",
        source_text=source,
    )
    parsed = ParsedIntent(customer_summary="Neptune", services=[component])

    DeepSeekIntentParser._reconcile_explicit_service_architecture(source, parsed)
    DeepSeekIntentParser._overlay_literal_component_facts(
        source,
        component,
        extra_fields=("backup_storage_gib",),
    )

    assert component.requirements["writer_nodes"] == 1
    assert component.requirements["reader_nodes"] == 2
    assert component.requirements["instance_count"] == 3
    assert component.requirements["backup_storage_gib"] == 100


def test_quicksight_roles_sessions_and_spice_do_not_collapse_into_generic_fields() -> None:
    source = (
        "Amazon QuickSight：企业版，10名作者、120名读者，"
        "每月2万次读者会话，SPICE容量200GB"
    )
    component = ServiceRequirement(
        service="quick_sight",
        calculator_service_name="Amazon QuickSight",
        source_text=source,
        requirements={"users": 130, "storage_gib": 200, "requested_model": "企业版"},
    )

    DeepSeekIntentParser._overlay_literal_component_facts(source, component)

    assert component.requirements["edition"] == "enterprise"
    assert component.requirements["author_users"] == 10
    assert component.requirements["reader_users"] == 120
    assert component.requirements["session_capacity"] == 20_000
    assert component.requirements["spice_gib"] == 200
    assert "users" not in component.requirements
    assert "user_count" not in component.requirements
    assert "storage_gib" not in component.requirements
    assert "requested_model" not in component.requirements


def test_codedeploy_on_premise_updates_are_recovered_as_usage() -> None:
    source = "AWS CodeDeploy：数量1，每月更新80台本地服务器"
    component = ServiceRequirement(
        service="code_deploy",
        calculator_service_name="AWS CodeDeploy",
        source_text=source,
    )

    DeepSeekIntentParser._overlay_literal_component_facts(
        source,
        component,
        extra_fields=("deployment_updates",),
    )

    assert component.requirements["deployment_updates"] == 80


def test_appsync_operations_updates_and_connection_minutes_are_all_recovered() -> None:
    source = (
        "AWS AppSync：数量1，每月3000万次GraphQL查询和数据修改，"
        "每月500万次实时更新，每月200万连接分钟"
    )
    component = ServiceRequirement(
        service="app_sync",
        calculator_service_name="AWS AppSync",
        source_text=source,
    )

    DeepSeekIntentParser._overlay_literal_component_facts(
        source,
        component,
        extra_fields=("requests", "messages", "connection_minutes"),
    )

    assert component.requirements["requests"] == 30_000_000
    assert component.requirements["messages"] == 5_000_000
    assert component.requirements["connection_minutes"] == 2_000_000


def test_explicit_platform_services_are_never_lost_when_ai_omits_them() -> None:
    text = (
        "Amazon EKS：1 个集群。\n"
        "Amazon ECR 私有仓库：1 个。\n"
        "Amazon MSK：kafka.t3.small，3 个 Broker，每 Broker 100GB。\n"
        "Amazon OpenSearch：t3.small.search，2 个数据节点，每节点 50GB。\n"
        "AWS Secrets Manager：5 个 Secret。"
    )
    parsed = ParsedIntent.model_construct(customer_summary="平台组件", services=[], ambiguities=[])

    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_models(text, parsed)

    services = {item.service: item for item in parsed.services}
    assert set(services) == {"eks", "ecr", "msk", "opensearch", "secrets_manager"}
    assert services["eks"].requirements["cluster_count"] == 1
    assert services["ecr"].requirements["repositories"] == 1
    assert services["msk"].requirements["requested_model"] == "kafka.t3.small"
    assert services["msk"].requirements["broker_count"] == 3
    assert services["opensearch"].requirements["requested_model"] == "t3.small.search"
    assert services["opensearch"].requirements["data_nodes"] == 2
    assert services["secrets_manager"].requirements["secret_count"] == 5


def test_worker_node_root_disk_is_not_duplicated_as_standalone_ebs() -> None:
    source = "EKS Worker Node：t3.xlarge，2 个节点，每节点 gp3 100GB，Managed Node Group。"
    parsed = ParsedIntent(
        customer_summary="EKS",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text=source,
                requirements={"requested_model": "t3.xlarge", "system_disk_gib": 100},
            ),
            ServiceRequirement(
                service="ebs",
                source_text=source,
                requirements={"storage_gib": 100, "volume_type": "gp3"},
            ),
        ],
    )

    DeepSeekIntentParser._drop_embedded_ebs_duplicates(parsed)

    assert [item.service for item in parsed.services] == ["ec2"]


def test_single_aggregate_auxiliary_line_is_not_multiplied_by_regions() -> None:
    text = (
        "云硬盘：全球，gp3，共 1000GB\n"
        "公网出网流量：新加坡、悉尼、香港合计 1000GB/月\n"
        "WAF：1 个 Web ACL，1000 万次请求/月"
    )
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(service="ebs", region="ap-southeast-1"),
            ServiceRequirement(service="ebs", region="ap-southeast-2"),
            ServiceRequirement(service="data_transfer", region="ap-southeast-1"),
            ServiceRequirement(service="data_transfer", region="ap-southeast-2"),
            ServiceRequirement(service="waf", region="global"),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._collapse_explicit_auxiliary_duplicates(text, parsed)

    assert [item.service for item in parsed.services] == ["ebs", "data_transfer", "waf"]
    assert parsed.services[0].region == "global"
    assert parsed.services[0].requirements["storage_gib"] == 1000
    assert parsed.services[1].requirements["data_transfer_out_gib"] == 1000
    assert parsed.services[2].requirements["requests"] == 10_000_000


def test_compact_redis_msk_and_s3_rows_preserve_literal_customer_fields() -> None:
    text = (
        "Amazon ElastiCache for Redis｜8GB × 2分片\n"
        "Amazon MSK｜3 Broker节点 m7g.large｜存储510GB\n"
        "Amazon S3｜500GB"
    )
    parsed = ParsedIntent(
        customer_summary="compact rows",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text=text.splitlines()[0],
                requirements={"requested_model": "8gb × 2分片", "engine": "redis"},
            ),
            ServiceRequirement(
                service="msk",
                source_text=text.splitlines()[1],
                requirements={"storage_gib": 510},
            ),
            ServiceRequirement(service="s3", source_text=text.splitlines()[2]),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._drop_specs_inferred_from_models(text, parsed)

    redis = parsed.services[0].requirements
    assert redis["memory_gib"] == 8
    assert redis["shards"] == 2
    assert "requested_model" not in redis

    msk = parsed.services[1].requirements
    assert msk["requested_model"] == "m7g.large"
    assert msk["broker_count"] == 3
    assert msk["storage_gib_per_broker"] == 510
    assert "storage_gib" not in msk

    assert parsed.services[2].requirements["storage_gib"] == 500


def test_inventory_preserves_mongodb_and_keeps_elk_separate_from_es() -> None:
    text = "\n".join(
        [
            "ES集群 5节点 16G内存",
            "MongoDB 2T",
            "日志系统 ELK 1套",
        ]
    )
    parsed = ParsedIntent(
        customer_summary="bad ai result",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text="ES集群 5节点 16G内存",
                requirements={"nodes": 5, "memory_gib": 16},
            ),
            ServiceRequirement(
                service="ec2",
                source_text="日志系统 ELK 1套",
                requirements={"vcpu": 2, "memory_gib": 4},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert [item.service for item in parsed.services] == [
        "opensearch",
        "documentdb",
        "opensearch",
    ]
    documentdb = parsed.services[1]
    assert documentdb.requirements["storage_gib"] == 2048
    assert "MongoDB" in documentdb.source_text
    assert "ELK" in parsed.services[2].source_text


def test_explicit_inventory_preserves_every_named_component_and_removes_msk_as_ec2() -> None:
    lines = [
        "Amazon ECS / Fargate",
        "Amazon EC2",
        "Amazon Aurora MySQL",
        "Amazon ElastiCache for Redis（8GB × 2分片）",
        "Amazon OpenSearch Service",
        "Amazon MSK（3 Broker节点 m7g.large，存储510GB）",
        "Amazon EMR",
        "AWS Glue",
        "Amazon Redshift",
        "Amazon S3（500GB）",
        "Amazon EFS",
        "Amazon API Gateway（5120MB最大入口请求）",
        "Amazon EventBridge Scheduler（1套）",
    ]
    parsed = ParsedIntent(
        customer_summary="bad ai result",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                source_text="Amazon MSK (3 Broker节点 m7g.large, 存储 510GB)",
                requirements={"requested_model": "m7g.large", "system_disk_gib": 510},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory("\n".join(lines), parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities("\n".join(lines), parsed)
    DeepSeekIntentParser._ensure_missing_region_ambiguity(parsed)

    assert [item.service for item in parsed.services] == [
        "ecs",
        "ec2",
        "rds",
        "elasticache",
        "opensearch",
        "msk",
        "emr",
        "glue",
        "redshift",
        "s3",
        "efs",
        "apigateway",
        "scheduler",
    ]
    assert len(parsed.services) == len(lines)
    msk = next(item for item in parsed.services if item.service == "msk")
    assert msk.requirements == {
        "requested_model": "m7g.large",
        "broker_count": 3,
        "storage_gib_per_broker": 510,
    }
    redis = next(item for item in parsed.services if item.service == "elasticache")
    assert redis.requirements["memory_gib"] == 8
    assert redis.requirements["shards"] == 2
    s3 = next(item for item in parsed.services if item.service == "s3")
    assert s3.requirements["storage_gib"] == 500
    assert parsed.ambiguities == ["请确认部署区域。"]


def test_analytics_services_keep_their_own_official_fields() -> None:
    parsed = ParsedIntent(
        customer_summary="大数据分析报价",
        services=[
            ServiceRequirement(
                service="emr",
                calculator_service_name="Amazon EMR",
                source_text="Spark大数据计算集群，主节点1个，核心节点5个",
                requirements={"requested_model": "t1.micro"},
            ),
            ServiceRequirement(
                service="redshift",
                calculator_service_name="Amazon Redshift",
                source_text="数据仓库集群，存储容量：20TB",
                requirements={},
            ),
            ServiceRequirement(
                service="athena",
                calculator_service_name="Amazon Athena",
                source_text="用于查询S3数据湖中的分析数据",
                requirements={
                    "requested_model": "t1.micro",
                    "cluster_count": 1,
                    "storage_gib": 1,
                },
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    emr, redshift, athena = parsed.services
    assert emr.requirements["applications"] == ["spark"]
    assert emr.requirements["master_nodes"] == 1
    assert emr.requirements["core_nodes"] == 5
    assert "requested_model" not in emr.requirements
    assert redshift.requirements["storage_gib"] == 20 * 1024
    assert athena.requirements == {}


def test_service_identity_guard_replaces_wrong_rabbitmq_ec2_and_recovers_api_gateway() -> None:
    text = """区域：ap-southeast-1（新加坡）
1、消息队列：目前使用RabbitMQ，准备迁移到AWS，预计3个节点。
2、接口服务：需要提供API给外部系统调用。"""
    parsed = ParsedIntent(
        customer_summary="bad classification",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                quantity=3,
                source_text="消息队列：目前使用RabbitMQ，准备迁移到AWS，预计3个节点。",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert [item.service for item in parsed.services] == ["mq", "apigateway"]
    rabbitmq = parsed.services[0]
    assert rabbitmq.quantity == 1
    assert rabbitmq.requirements == {
        "engine_type": "rabbitmq",
        "broker_count": 3,
    }
    assert "RabbitMQ" in rabbitmq.source_text
    assert "提供API给外部系统调用" in parsed.services[1].source_text


def test_service_identity_guard_enforces_managed_first_and_api_direction() -> None:
    self_hosted = DeepSeekIntentParser._inventory_keys_for_line(
        "明确要求在 EC2 自建 RabbitMQ 三节点集群"
    )
    outbound_only = DeepSeekIntentParser._inventory_keys_for_line(
        "应用服务器需要调用外部系统的 API"
    )

    assert [key for key, _ in self_hosted] == ["mq"]
    assert "apigateway" not in {key for key, _ in outbound_only}


def test_memorydb_identity_and_explicit_capacity_survive_redis_normalization() -> None:
    assert DeepSeekIntentParser._inventory_keys_for_line(
        "Amazon MemoryDB，db.r7g.xlarge / 26.32 GiB，Redis"
    ) == [("memorydb", "Amazon MemoryDB")]
    parsed = ParsedIntent(
        customer_summary="MemoryDB",
        services=[
            ServiceRequirement(
                service="elasticache",
                calculator_service_name="Amazon ElastiCache for Redis",
                quantity=1,
                source_text="Amazon MemoryDB，db.r7g.xlarge / 26.32 GiB，Redis",
                requirements={"engine": "redis", "memory_gib": 7},
            )
        ],
    )

    preserve_customer_configuration(parsed)

    item = parsed.services[0]
    assert item.service == "memorydb"
    assert item.calculator_service_name == "Amazon MemoryDB"
    assert item.product_identity == "amazon_memorydb_redis"
    assert item.requirements["requested_model"] == "db.r7g.xlarge"
    assert item.requirements["memory_gib"] == 26.32
    assert item.requirements["engine"] == "redis"


@pytest.mark.parametrize(
    ("service", "source", "expected"),
    [
        (
            "fsx",
            "Amazon FSx for Lustre：数量1，文件系统容量6TB，持久型部署，吞吐量250MB/s/TiB",
            {"storage_gib": 6144.0, "throughput_mbps_per_tib": 250.0},
        ),
        (
            "apigateway",
            "Amazon API Gateway WebSocket API：数量1，每月消息约6000万条，每月连接时长约1500万分钟",
            {
                "api_type": "websocket",
                "messages": 60_000_000.0,
                "connection_minutes": 15_000_000.0,
            },
        ),
            (
                "global_accelerator",
                "AWS Global Accelerator：数量1，配置2个Listener、4个Endpoint，每月通过加速器传输约3TB数据",
                {
                    "listener_count": 2,
                    "endpoint_count": 4,
                    "data_transfer_out_gib": 3072.0,
                },
            ),
    ],
)
def test_universal_pricing_fact_ledger_preserves_official_dimensions(
    service: str,
    source: str,
    expected: dict[str, object],
) -> None:
    parsed = ParsedIntent(
        customer_summary="literal pricing facts",
        services=[
            ServiceRequirement(
                service=service,
                source_text=source,
                requirements={},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    item = parsed.services[0]
    assert item.requirements == expected
    assert DeepSeekIntentParser._uncovered_quantitative_claim_issues(source, item) == []
    for field in expected:
        if field == "api_type":
            continue
        assert item.field_sources[f"requirements.{field}"] == "customer_text"


def test_explicit_pinpoint_is_never_rewritten_to_ses() -> None:
    parsed = ParsedIntent(
        customer_summary="Pinpoint 邮件推送",
        services=[
            ServiceRequirement(
                service="ses",
                calculator_service_name="Amazon SES",
                source_text="Amazon Pinpoint：邮件推送 100 万封",
                requirements={},
            )
        ],
    )

    preserve_customer_configuration(parsed)

    item = parsed.services[0]
    assert item.service == "pinpoint"
    assert item.product_identity == "amazon_pinpoint"
    assert item.calculator_service_name == "Amazon Pinpoint"
    assert item.requirements["outbound_messages"] == 1_000_000
    assert item.field_sources["requirements.outbound_messages"] == "customer_text"


def test_old_pinpoint_draft_drops_cross_product_ses_review_state() -> None:
    parsed = ParsedIntent(
        customer_summary="old Pinpoint draft",
        services=[
            ServiceRequirement(
                service="pinpoint",
                calculator_service_name="Amazon Pinpoint",
                source_text="Amazon Pinpoint：邮件推送 100 万封",
                requirements={
                    "_review_selected_model": "SES Outbound Email",
                    "_review_selected_specifications": {"edition": "ses"},
                    "_review_available_shapes": [{"vcpu": 1, "memory_gib": 1}],
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)

    item = parsed.services[0]
    assert item.service == "pinpoint"
    assert item.requirements["outbound_messages"] == 1_000_000
    assert "_review_selected_model" not in item.requirements
    assert "_review_selected_specifications" not in item.requirements
    assert "_review_available_shapes" not in item.requirements


def test_customer_edits_outrank_original_rds_quantity_version_model_and_storage() -> None:
    parsed = ParsedIntent(
        customer_summary="RDS edit",
        services=[
            ServiceRequirement(
                service="rds",
                quantity=3,
                source_text=(
                    "RDS MySQL：数量1，MySQL 5.7.44，单实例4核16GB/40GB存储，"
                    "实例规格db.m4.xlarge"
                ),
                requirements={
                    "engine": "mysql",
                    "engine_version": "8.4.11",
                    "requested_model": "db.m6g.xlarge",
                    "storage_gib": 1000,
                },
                field_sources={
                    "quantity": "customer_confirmation",
                    "requirements.engine_version": "customer_confirmation",
                    "requirements.requested_model": "customer_confirmation",
                    "requirements.storage_gib": "customer_confirmation",
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)

    database = parsed.services[0]
    assert database.quantity == 3
    assert database.requirements["engine_version"] == "8.4.11"
    assert database.requirements["requested_model"] == "db.m6g.xlarge"
    assert database.requirements["storage_gib"] == 1000
    assert database.field_sources["requirements.engine_version"] == "customer_confirmation"


def test_rds_compact_per_instance_storage_is_preserved_from_customer_text() -> None:
    parsed = ParsedIntent(
        customer_summary="RDS storage",
        services=[
            ServiceRequirement(
                service="rds",
                source_text="RDS MySQL：MySQL 5.7.44，单实例4核16GB/40GB存储",
                requirements={"engine": "mysql", "vcpu": 4, "memory_gib": 16},
            )
        ],
    )

    preserve_customer_configuration(parsed)

    assert parsed.services[0].requirements["storage_gib"] == 40
    assert parsed.services[0].field_sources["requirements.storage_gib"] == "customer_text"


def test_rds_high_availability_wording_keeps_multi_az_deployment() -> None:
    parsed = ParsedIntent(
        customer_summary="RDS HA",
        services=[
            ServiceRequirement(
                service="rds",
                source_text=(
                    "RDS MySQL：数量1，MySQL 5.7.44，高可用主备架构，"
                    "2个数据库实例，单实例4核16GB/40GB存储"
                ),
                requirements={"engine": "mysql", "deployment": "single_az"},
            )
        ],
    )

    preserve_customer_configuration(parsed)

    database = parsed.services[0]
    assert database.quantity == 1
    assert database.requirements["deployment"] == "multi_az"
    assert database.requirements["storage_gib"] == 40


def test_official_lowest_cost_replacement_model_is_not_restored_to_unavailable_original() -> None:
    parsed = ParsedIntent(
        customer_summary="RDS replacement",
        services=[
            ServiceRequirement(
                service="rds",
                source_text="RDS MySQL：实例规格 db.m4.xlarge，4核16GB",
                requirements={
                    "engine": "mysql",
                    "requested_model": "db.m6g.xlarge",
                    "vcpu": 4,
                    "memory_gib": 16,
                },
                field_sources={
                    "requirements.requested_model": "system_cheapest_official_match",
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)

    assert parsed.services[0].requirements["requested_model"] == "db.m6g.xlarge"
    assert parsed.services[0].field_sources["requirements.requested_model"] == (
        "system_cheapest_official_match"
    )


@pytest.mark.parametrize("name", ["Public-VPC", "Private-VPC", "Public VPC", "Private VPC"])
def test_public_and_private_vpc_are_native_network_components(name: str) -> None:
    keys = DeepSeekIntentParser._inventory_keys_for_line(f"{name}：北美区域网络")

    assert keys == [("vpc", "Amazon Virtual Private Cloud (VPC)")]


def test_vpc_workload_descriptions_do_not_create_api_gateway_or_ec2() -> None:
    public_keys = DeepSeekIntentParser._inventory_keys_for_line(
        "13、Amazon VPC（Public）：数量2，承载 API 服务公网入口，每套独立公网 IP"
    )
    private_keys = DeepSeekIntentParser._inventory_keys_for_line(
        "14、Amazon VPC（Private）：数量1，承载内部 EC2 服务器、EKS 工作负载"
    )

    assert public_keys == [("vpc", "Amazon Virtual Private Cloud (VPC)")]
    assert private_keys == [("vpc", "Amazon Virtual Private Cloud (VPC)")]


def test_waf_and_alb_heading_does_not_become_vpc_from_relationship_text() -> None:
    keys = DeepSeekIntentParser._inventory_keys_for_line(
        "15、WAF + ALB：数量2，基于 Public-VPC 提供安全防护、DDoS 防护和负载均衡"
    )

    assert {key for key, _ in keys} == {"waf", "elb"}


def test_old_composite_vpc_draft_is_repaired_into_independent_waf_and_alb() -> None:
    source = "15、WAF + ALB：数量2，基于 Public-VPC 提供安全防护、流控、负载均衡"
    parsed = ParsedIntent(
        customer_summary="old draft",
        services=[
            ServiceRequirement(
                service="vpc",
                calculator_service_name="Amazon VPC (Public)",
                quantity=2,
                source_text=source,
                requirements={
                    "requested_model": "t4g.nano",
                    "_review_selected_model": "t4g.nano",
                    "vcpu": 2,
                    "memory_gib": 0.5,
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)

    assert [item.service for item in parsed.services] == ["waf", "elb"]
    waf, alb = parsed.services
    assert waf.quantity == 2
    assert waf.requirements["web_acls"] == 2
    assert "requested_model" not in waf.requirements
    assert "_review_selected_model" not in waf.requirements
    assert alb.quantity == 2
    assert alb.requirements == {"load_balancer_type": "application"}


def test_old_private_vpc_draft_drops_stale_ec2_review_model() -> None:
    parsed = ParsedIntent(
        customer_summary="old private vpc",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                source_text="14、Private-VPC：数量1，用于 EC2 集群 / Pod 实例",
                requirements={
                    "requested_model": "t4g.nano",
                    "_review_selected_model": "t4g.nano",
                    "_review_selected_specifications": {"vCPU": 2, "memoryGiB": 0.5},
                    "vcpu": 2,
                    "memory_gib": 0.5,
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)

    vpc = parsed.services[0]
    assert vpc.service == "vpc"
    assert vpc.calculator_service_name == "Amazon VPC (Private)"
    assert not any(key.startswith("_review_") for key in vpc.requirements)
    assert "requested_model" not in vpc.requirements
    assert "vcpu" not in vpc.requirements
    assert "memory_gib" not in vpc.requirements


def test_customer_configuration_removes_duplicate_and_vpc_generated_components() -> None:
    public_source = "Amazon VPC（Public）：数量2，用于公网业务访问环境，承载API服务公网入口"
    parsed = ParsedIntent(
        customer_summary="quote",
        services=[
            ServiceRequirement(
                service="ses",
                calculator_service_name="Amazon SES",
                source_text="9、Amazon Pinpoint：数量1，邮件发送量约100万封",
                requirements={"outbound_messages": 1_000_000},
            ),
            ServiceRequirement(
                service="ses",
                calculator_service_name="Amazon SES",
                source_text="Amazon Pinpoint：数量1，邮件发送量约100万封",
                requirements={"outbound_messages": 1_000_000},
            ),
            ServiceRequirement(service="vpc", source_text=public_source),
            ServiceRequirement(service="apigateway", source_text=public_source),
            ServiceRequirement(
                service="vpc",
                source_text="Amazon VPC（Private）：数量1，用于私有网络环境，",
            ),
            ServiceRequirement(
                service="ec2",
                source_text="承载内部EC2服务器、EKS工作负载及其他内部业务资源",
                requirements={"requested_model": "m6g.medium"},
                field_sources={"requirements.requested_model": "customer_confirmation"},
            ),
        ],
    )

    preserve_customer_configuration(parsed)

    assert [item.service for item in parsed.services] == ["pinpoint", "vpc", "vpc"]
    assert [item.quantity for item in parsed.services] == [1, 2, 1]


def test_customer_quantity_and_rhel_are_locked_from_original_text() -> None:
    parsed = ParsedIntent(
        customer_summary="quote",
        services=[
            ServiceRequirement(
                service="route53",
                quantity=1,
                source_text="Amazon Route 53：数量2，用于域名解析",
            ),
            ServiceRequirement(
                service="waf",
                quantity=1,
                source_text="AWS WAF：数量2，用于Web攻击防护",
            ),
            ServiceRequirement(
                service="ec2",
                source_text=(
                    "Amazon EC2跳板服务器：数量1，2C2G/40G，"
                    "Red Hat 9，实例类型t3.small"
                ),
                requirements={"operating_system": "linux"},
            ),
        ],
    )

    preserve_customer_configuration(parsed)

    route53, waf, ec2 = parsed.services
    assert route53.quantity == 2
    assert route53.requirements["hosted_zones"] == 2
    assert waf.quantity == 2
    assert waf.requirements["web_acls"] == 2
    assert "rules" not in waf.requirements
    assert ec2.requirements["operating_system"] == "RHEL"
    assert ec2.requirements["system_disk_gib"] == 40
    assert ec2.field_sources["requirements.operating_system"] == "customer_text"


def test_chinese_labels_cannot_hide_customer_ec2_model_or_compact_disk() -> None:
    source = (
        "Amazon EC2：数量5，单台8核32GB/250GB存储，"
        "实例类型m6i.2xlarge，Ubuntu 22.04"
    )
    parsed = ParsedIntent(
        customer_summary="EC2",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text=source,
                requirements={
                    "_review_selected_model": "t4g.2xlarge",
                    "_review_selected_specifications": {"vCPU": 8, "memoryGiB": 32},
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)
    DeepSeekIntentParser._reconcile_explicit_models(source, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    component = parsed.services[0]
    assert component.quantity == 5
    assert component.requirements["requested_model"] == "m6i.2xlarge"
    assert component.requirements["vcpu"] == 8
    assert component.requirements["memory_gib"] == 32
    assert component.requirements["system_disk_gib"] == 250
    assert "_review_selected_model" not in component.requirements
    assert "_review_selected_specifications" not in component.requirements
    assert component.field_sources["requirements.requested_model"] == "customer_text"
    assert "requirements.requested_model" in component.locked_fields


def test_waf_keeps_per_acl_rules_and_request_scope_independent_from_quantity() -> None:
    source = (
        "AWS WAF：数量2，每个Web ACL配置12条规则，"
        "每个Web ACL每月处理约6000万次请求"
    )
    parsed = ParsedIntent(
        customer_summary="WAF",
        services=[
            ServiceRequirement(
                service="waf",
                source_text=source,
                # Simulate the incorrect AI result seen in production. The
                # literal customer ledger must repair it deterministically.
                requirements={
                    "rules": 2,
                    "requests": 60_000_000,
                    "_review_selected_model": "WAF Basic Protection",
                    "_review_selected_specifications": {
                        "webACLs": 2,
                        "rules": 2,
                        "requests": 60_000_000,
                    },
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    component = parsed.services[0]
    assert component.quantity == 2
    assert component.requirements == {
        "web_acls": 2,
        "rules": 12,
        "requests": 60_000_000,
    }
    assert component.field_scopes["web_acls"] == "component_total"
    assert component.field_scopes["rules"] == "per_resource"
    assert component.field_scopes["requests"] == "per_resource"
    assert "_review_selected_model" not in component.requirements
    assert "_review_selected_specifications" not in component.requirements


def test_rds_model_and_version_survive_chinese_text_without_ascii_separator() -> None:
    parsed = ParsedIntent(
        customer_summary="RDS",
        services=[
            ServiceRequirement(
                service="rds",
                source_text=(
                    "Amazon RDS for MySQL：数量1，MySQL 5.7.44，"
                    "实例规格db.m4.xlarge，单实例4核16GB"
                ),
                requirements={"engine": "mysql", "vcpu": 4, "memory_gib": 16},
            )
        ],
    )

    preserve_customer_configuration(parsed)

    rds = parsed.services[0]
    assert rds.requirements["requested_model"] == "db.m4.xlarge"
    assert rds.requirements["engine_version"] == "5.7.44"
    assert rds.field_sources["requirements.requested_model"] == "customer_text"


@pytest.mark.parametrize(
    ("source", "identity", "display"),
    [
        ("Public-VPC：北美区域", "amazon_vpc_public", "Amazon VPC (Public)"),
        ("Private-VPC：北美区域", "amazon_vpc_private", "Amazon VPC (Private)"),
        (
            "Private-VPC：数量1，用于 EC2 集群 / Pod 实例",
            "amazon_vpc_private",
            "Amazon VPC (Private)",
        ),
    ],
)
def test_vpc_source_repairs_erroneous_ec2_self_hosted_fallback(
    source: str, identity: str, display: str
) -> None:
    parsed = ParsedIntent(
        customer_summary="VPC",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建 VPC）",
                source_text=source,
                requirements={
                    "requested_model": "t3.micro",
                    "vcpu": 2,
                    "memory_gib": 1,
                    "operating_system": "linux",
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)

    item = parsed.services[0]
    assert item.service == "vpc"
    assert item.product_identity == identity
    assert item.calculator_service_name == display
    compute_fields = {"requested_model", "vcpu", "memory_gib", "operating_system"}
    assert not compute_fields & item.requirements.keys()


def test_nacos_product_identity_beats_partial_capability_match() -> None:
    keys = DeepSeekIntentParser._inventory_keys_for_line(
        "Nacos：服务注册发现和配置中心，部署数量：3个节点"
    )

    assert keys == [("ec2", "Amazon EC2")]


def test_nacos_requires_clear_managed_or_self_hosted_decision_and_keeps_nodes() -> None:
    parsed = ParsedIntent(
        customer_summary="Nacos",
        services=[
            ServiceRequirement(
                service="cloud_map",
                calculator_service_name="AWS Cloud Map",
                quantity=1,
                region="ap-southeast-1",
                source_text="Nacos：服务注册发现和配置中心，部署数量：3个节点",
            )
        ],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed)

    assert parsed.services[0].service == "ec2"
    assert parsed.services[0].quantity == 3
    assert parsed.services[0].requirements["operating_system"] == "linux"
    assert parsed.services[0].field_sources["_pending_architecture_decision"] == "system_policy"
    assert len(parsed.ambiguities) == 1
    assert "Cloud Map + AppConfig" in parsed.ambiguities[0]
    assert "3 个节点" in parsed.ambiguities[0]


def test_all_named_self_hosted_partial_replacements_enter_staged_workflow() -> None:
    parsed = ParsedIntent(
        customer_summary="XXL-JOB",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建 XXL-JOB）",
                quantity=2,
                source_text="XXL-JOB 调度中心，部署 2 个节点",
            )
        ],
        ambiguities=[
            "XXL-JOB 没有完全等价的 AWS 托管服务，请选择 AWS 托管方案还是保留原产品自建。"
        ],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed)

    assert parsed.services[0].field_sources["_pending_architecture_decision"] == "system_policy"


def test_named_clickhouse_ec2_cannot_skip_self_hosted_architecture_decision() -> None:
    parsed = ParsedIntent(
        customer_summary="ClickHouse",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                quantity=3,
                source_text=(
                    "ClickHouse：用途：实时数据分析和报表查询，部署数量：3个节点，"
                    "每节点配置：8核32GB，存储容量：1TB/节点"
                ),
                requirements={
                    "vcpu": 8,
                    "memory_gib": 32,
                    "system_disk_gib": 1024,
                },
            )
        ],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed)

    component = parsed.services[0]
    assert component.calculator_service_name == "Amazon EC2（自建 ClickHouse）"
    assert component.field_sources["_pending_architecture_decision"] == "system_policy"
    assert len(parsed.ambiguities) == 1
    assert "3 个节点" in parsed.ambiguities[0]
    assert "每节点 8 核 32 GiB" in parsed.ambiguities[0]
    assert "每节点 1024 GiB 存储" in parsed.ambiguities[0]


def test_explicit_clickhouse_ec2_is_already_a_self_hosted_decision() -> None:
    source = "ClickHouse：EC2 m6i.xlarge (4C16G) + gp3 500GB，数量1"
    parsed = ParsedIntent(
        customer_summary="ClickHouse",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                source_text=source,
                requirements={},
            )
        ],
        ambiguities=["AWS 没有与 ClickHouse 完全等价的托管服务，请选择托管还是自建。"],
    )

    DeepSeekIntentParser._reconcile_explicit_models(source, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, source)

    component = parsed.services[0]
    assert component.calculator_service_name == "Amazon EC2（自建 ClickHouse）"
    assert component.field_sources["_architecture_decision"] == "customer_text"
    assert "_pending_architecture_decision" not in component.field_sources
    assert parsed.ambiguities == []
    assert component.requirements["requested_model"] == "m6i.xlarge"
    assert component.requirements["vcpu"] == 4
    assert component.requirements["memory_gib"] == 16
    assert component.requirements["system_disk_gib"] == 500
    assert component.requirements["volume_type"] == "gp3"
    assert "requirements.system_disk_gib" in component.locked_fields


def test_explicit_quicksight_is_routed_to_its_native_component_template() -> None:
    source = "BI可视化：QuickSight Enterprise (10用户)，数量1"
    parsed = ParsedIntent(
        customer_summary="BI",
        services=[
            ServiceRequirement(
                service="business_intelligence",
                calculator_service_name="BI可视化",
                source_text=source,
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(source, parsed)
    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, source)

    component = parsed.services[0]
    assert component.service == "quicksight"
    assert component.calculator_service_name == "Amazon QuickSight"
    # Edition and user count are intentionally extracted by the isolated
    # QuickSight AI template, not by a shared hard-coded field parser.
    assert component.requirements == {}
    assert parsed.ambiguities == []


def test_literal_clickhouse_service_key_still_requires_architecture_decision() -> None:
    parsed = ParsedIntent(
        customer_summary="ClickHouse",
        services=[
            ServiceRequirement(
                service="clickhouse",
                calculator_service_name="ClickHouse",
                quantity=3,
                source_text=(
                    "ClickHouse：用途：实时数据分析和报表查询，部署数量：3个节点，"
                    "每节点配置：8核32GB，存储容量：1TB/节点"
                ),
                requirements={
                    "vcpu": 8,
                    "memory_gib": 32,
                    "system_disk_gib": 1024,
                },
            )
        ],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed)

    component = parsed.services[0]
    assert component.service == "ec2"
    assert component.calculator_service_name == "Amazon EC2（自建 ClickHouse）"
    assert component.field_sources["_pending_architecture_decision"] == "system_policy"
    assert len(parsed.ambiguities) == 1
    assert "托管方案" in parsed.ambiguities[0]
    assert "EC2 上自建 ClickHouse" in parsed.ambiguities[0]


def test_clickhouse_header_only_recovers_full_numbered_block_before_decision() -> None:
    parsed = ParsedIntent(
        customer_summary="ClickHouse",
        services=[
            ServiceRequirement(
                service="clickhouse",
                calculator_service_name="ClickHouse",
                source_text="ClickHouse：",
            )
        ],
    )
    original = (
        "1、Amazon EC2：数量 2 台\n"
        "2、ClickHouse：用途：实时分析，部署数量：3个节点，"
        "每节点配置：8核32GB，存储容量：1TB/节点\n"
        "3、Amazon S3：标准存储"
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, original)

    component = parsed.services[0]
    assert component.service == "ec2"
    assert component.quantity == 3
    assert "每节点配置：8核32GB" in (component.source_text or "")
    assert component.field_sources["_pending_architecture_decision"] == "system_policy"
    assert "3 个节点" in parsed.ambiguities[0]


def test_multiline_blocks_repair_units_and_split_eks_worker_nodes() -> None:
    text = """Amazon EC2
区域：ap-southeast-1
规格：r6g.2xlarge
数量：2
系统：Linux

EC2云服务器
区域：新加坡
配置：4核16G
数量：3
系统：CentOS

Amazon RDS MySQL
规格：db.r6g.large
CPU：2核
内存：16GB
存储：500GB
部署：Multi-AZ

Redis缓存服务
配置：8GB
架构：主从
数量：1

Amazon S3
容量：50TB
存储类型：Standard

Kafka消息队列
配置：3节点
每台：4核16G
磁盘：500GB

Amazon EKS
Kubernetes集群
节点规格：8核32G
节点数量：3"""
    blocks = [part.strip() for part in text.split("\n\n")]
    parsed = ParsedIntent(
        customer_summary="bad scaled AI output",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text=blocks[0],
                quantity=2,
                requirements={"requested_model": "r6g.2xlarge", "memory_gib": 65536},
            ),
            ServiceRequirement(
                service="ec2",
                source_text=blocks[1],
                quantity=3,
                requirements={"vcpu": 4, "memory_gib": 16384},
            ),
            ServiceRequirement(
                service="rds",
                source_text=blocks[2],
                requirements={"vcpu": 2, "memory_gib": 16384, "storage_gib": 512000},
            ),
            ServiceRequirement(
                service="elasticache",
                source_text=blocks[3],
                requirements={"memory_gib": 8192},
            ),
            ServiceRequirement(
                service="s3",
                source_text=blocks[4],
                requirements={"storage_gib": 52428800},
            ),
            ServiceRequirement(
                service="msk",
                source_text=blocks[5],
                requirements={"memory_gib": 16384, "system_disk_gib": 512000},
            ),
            ServiceRequirement(
                service="eks",
                source_text=blocks[6],
                requirements={"vcpu": 8, "memory_gib": 32768},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_engines(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    shaped_ec2 = next(
        item
        for item in parsed.services
        if item.service == "ec2" and item.requirements.get("vcpu") == 4
    )
    assert shaped_ec2.quantity == 3
    assert shaped_ec2.requirements["memory_gib"] == 16

    rds = next(item for item in parsed.services if item.service == "rds")
    assert rds.requirements["vcpu"] == 2
    assert rds.requirements["memory_gib"] == 16
    assert rds.requirements["storage_gib"] == 500

    redis = next(item for item in parsed.services if item.service == "elasticache")
    assert redis.requirements["memory_gib"] == 8

    s3 = next(item for item in parsed.services if item.service == "s3")
    assert s3.requirements["storage_gib"] == 50 * 1024

    msk = next(item for item in parsed.services if item.service == "msk")
    assert msk.requirements["broker_count"] == 3
    assert msk.requirements["vcpu"] == 4
    assert msk.requirements["memory_gib"] == 16
    assert msk.requirements["storage_gib_per_broker"] == 500
    assert "system_disk_gib" not in msk.requirements

    eks = next(item for item in parsed.services if item.service == "eks")
    assert "vcpu" not in eks.requirements
    assert "memory_gib" not in eks.requirements
    worker = next(
        item
        for item in parsed.services
        if item.service == "ec2" and item.calculator_service_name == "Amazon EC2 (EKS Worker Nodes)"
    )
    assert worker.quantity == 3
    assert worker.requirements == {
        "vcpu": 8,
        "memory_gib": 32,
        "operating_system": "Linux",
    }


def test_inventory_binds_following_form_lines_to_the_service_heading() -> None:
    text = """EC2云服务器
区域：新加坡
配置：4核16G
数量：3

Amazon ElastiCache Redis
配置：8GB
架构：主从
数量：1

Amazon MSK
Broker数量：3
每台：4核16G
磁盘：500GB"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="ec2", source_text="EC2云服务器"),
            ServiceRequirement(service="elasticache", source_text="Amazon ElastiCache Redis"),
            ServiceRequirement(service="msk", source_text="Amazon MSK"),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    ec2, redis, msk = parsed.services
    assert ec2.quantity == 3
    assert ec2.requirements == {"vcpu": 4, "memory_gib": 16}
    assert redis.requirements["memory_gib"] == 8
    assert msk.requirements == {
        "broker_count": 3,
        "vcpu": 4,
        "memory_gib": 16,
        "storage_gib_per_broker": 500,
    }


def test_vague_customer_values_are_questions_not_silent_guesses() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                quantity=3,
                source_text="后台服务4核16G，两三台",
                requirements={"vcpu": 4, "memory_gib": 16},
            ),
            ServiceRequirement(
                service="elasticache",
                calculator_service_name="Amazon ElastiCache Redis",
                source_text="Redis大概十几个G，一主一从",
            ),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                source_text="图片预计几十T存储",
            ),
            ServiceRequirement(
                service="msk",
                calculator_service_name="Amazon MSK",
                source_text="Kafka大概几个节点",
            ),
            ServiceRequirement(
                service="eks",
                calculator_service_name="Amazon EKS",
                source_text="K8S环境需要跑几个服务",
            ),
        ],
    )

    DeepSeekIntentParser._append_vague_value_questions(parsed)

    combined = "\n".join(parsed.ambiguities)
    assert "两三台" in combined
    assert "Redis" in combined and "每个节点" in combined
    assert "S3" in combined and "存储容量" in combined
    assert "MSK" in combined and "Broker" in combined
    assert "EKS" not in combined


def test_labeled_broker_count_cannot_be_taken_from_region_suffix() -> None:
    source = """Amazon MSK
区域：ap-southeast-1
Broker数量：3
规格：kafka.m5.large
磁盘：500GB/节点"""
    parsed = ParsedIntent(
        customer_summary="AI hallucinated another region",
        services=[
            ServiceRequirement(
                service="msk",
                calculator_service_name="Amazon MSK",
                region="ap-southeast-1",
                requirements={
                    "broker_count": 3,
                    "requested_model": "kafka.m5.large",
                    "storage_gib_per_broker": 500,
                },
                source_text=source,
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    DeepSeekIntentParser._replace_untrusted_customer_summary(parsed)

    assert parsed.services[0].requirements["broker_count"] == 3
    assert parsed.services[0].requirements["requested_model"] == "m5.large"
    assert parsed.services[0].requirements["storage_gib_per_broker"] == 500
    assert "ap-southeast-1" in parsed.customer_summary
    assert "雅加达" not in parsed.customer_summary


def test_ai_invented_model_is_removed_when_customer_never_wrote_it() -> None:
    source = "Amazon MSK：三个 Broker，每节点存储按客户容量要求"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                source_text=source,
                requirements={"requested_model": "m7g.large", "broker_count": 3},
            )
        ],
    )

    DeepSeekIntentParser._drop_unwritten_requested_models(source, parsed)

    assert "requested_model" not in parsed.services[0].requirements


def test_explicit_model_is_retained_only_for_its_source_component() -> None:
    msk_source = "Amazon MSK：型号 kafka.m5.large，三个 Broker"
    redis_source = "Amazon ElastiCache Redis：主从部署"
    text = f"{msk_source}\n{redis_source}"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                source_text=msk_source,
                requirements={"requested_model": "kafka.m5.large"},
            ),
            ServiceRequirement(
                service="elasticache",
                source_text=redis_source,
                requirements={"requested_model": "kafka.m5.large"},
            ),
        ],
    )

    DeepSeekIntentParser._drop_unwritten_requested_models(text, parsed)

    assert parsed.services[0].requirements["requested_model"] == "kafka.m5.large"
    assert "requested_model" not in parsed.services[1].requirements


def test_customer_selected_model_outweighs_older_model_in_original_text() -> None:
    source = "Amazon Neptune：实例规格 db.r6g.large，单节点8核32GB"
    component = ServiceRequirement(
        service="neptune",
        source_text=source,
        requirements={"requested_model": "db.r7g.xlarge"},
        field_sources={
            "requirements.requested_model": "customer_confirmation",
        },
    )
    parsed = ParsedIntent(customer_summary="Neptune", services=[component])

    DeepSeekIntentParser._reconcile_explicit_models(source, parsed)
    DeepSeekIntentParser._drop_unwritten_requested_models(source, parsed)

    assert component.requirements["requested_model"] == "db.r7g.xlarge"
    assert (
        component.field_sources["requirements.requested_model"]
        == "customer_confirmation"
    )


def test_colon_labeled_memory_survives_when_customer_also_gives_model() -> None:
    rds_source = """Amazon RDS PostgreSQL
规格：db.m6g.large
CPU：2 vCPU
内存：8 GiB
存储：300GB"""
    redis_source = """Amazon ElastiCache Redis
规格：cache.r7g.large
内存：13GB"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="rds",
                source_text=rds_source,
                requirements={"requested_model": "db.m6g.large", "vcpu": 2, "memory_gib": 8},
            ),
            ServiceRequirement(
                service="elasticache",
                source_text=redis_source,
                requirements={"requested_model": "cache.r7g.large", "memory_gib": 13},
            ),
        ],
    )

    DeepSeekIntentParser._drop_specs_inferred_from_models(f"{rds_source}\n\n{redis_source}", parsed)

    assert parsed.services[0].requirements["memory_gib"] == 8
    assert parsed.services[1].requirements["memory_gib"] == 13


def test_aurora_cluster_members_and_opensearch_nodes_are_lossless() -> None:
    aurora_source = """Amazon Aurora MySQL
区域：ap-southeast-1
实例规格：db.r7g.large
节点数量：2
存储：500GB
部署方式：高可用"""
    search_source = """Amazon OpenSearch Service
区域：ap-southeast-1
节点数量：3
节点规格：r6g.large.search
磁盘：500GB/节点"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="rds",
                quantity=1,
                source_text=aurora_source,
                requirements={
                    "requested_model": "db.r7g.large",
                    "storage_gib": 500,
                    "multi_az": True,
                },
            ),
            ServiceRequirement(
                service="opensearch",
                quantity=1,
                source_text=search_source,
                requirements={
                    "requested_model": "r6g.large.search",
                    "storage_gib": 500,
                },
            ),
        ],
    )

    text = f"{aurora_source}\n\n{search_source}"
    DeepSeekIntentParser._reconcile_explicit_engines(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_service_architecture(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    preserve_customer_configuration(parsed)

    aurora, search = parsed.services
    assert aurora.quantity == 1
    assert aurora.requirements["engine"] == "aurora_mysql"
    assert aurora.requirements["aurora_cluster"] is True
    assert aurora.requirements["deployment"] == "multi_az"
    assert aurora.requirements["cluster_members"] == 2
    assert "multi_az" not in aurora.requirements
    assert aurora.calculator_service_name == "Amazon Aurora MySQL"
    assert search.requirements["data_nodes"] == 3
    assert search.requirements["storage_gib_per_node"] == 500
    assert "storage_gib" not in search.requirements


def test_aurora_topology_never_reads_per_node_cpu_as_member_count() -> None:
    source = (
        "Amazon Aurora MySQL：数量1，MySQL兼容版本8.0，"
        "1个主节点+2个只读节点，单节点8核32GB，存储800GB"
    )
    parsed = ParsedIntent(
        customer_summary="x",
        services=[ServiceRequirement(service="rds", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    preserve_customer_configuration(parsed)

    requirement = parsed.services[0]
    assert aurora_cluster_member_count(source) == (3, "1个主节点+2个只读节点")
    assert aurora_cluster_member_count("Aurora 单节点8核32GB") is None
    assert requirement.requirements["cluster_members"] == 3
    assert requirement.requirements["engine_version"] == "8.0"
    assert requirement.requirements["vcpu"] == 8
    assert requirement.requirements["memory_gib"] == 32
    assert requirement.field_sources["requirements.cluster_members"] == "customer_text"
    assert requirement.field_match_policies["vcpu"] == "exact"
    assert requirement.field_match_policies["memory_gib"] == "exact"


def test_global_quantitative_audit_covers_lambda_memory_duration_and_invocations() -> None:
    source = "AWS Lambda：数量5，每个函数内存1024MB，平均执行时长800ms，每月总调用量约2000万次"
    component = ServiceRequirement(
        service="lambda",
        quantity=5,
        source_text=source,
        requirements={
            "memory_mb": 1024,
            "duration_ms": 800,
            "requests": 20_000_000,
        },
        field_evidence={
            "quantity": "数量5",
            "requirements.memory_mb": "内存1024MB",
            "requirements.duration_ms": "执行时长800ms",
            "requirements.requests": "调用量约2000万次",
        },
    )

    assert DeepSeekIntentParser._uncovered_quantitative_claim_issues(source, component) == []

    missing_duration = component.model_copy(deep=True)
    missing_duration.requirements.pop("duration_ms")
    assert any(
        "800ms" in issue
        for issue in DeepSeekIntentParser._uncovered_quantitative_claim_issues(
            source, missing_duration
        )
    )


def test_lambda_total_invocations_keep_aggregate_scope_during_reconciliation() -> None:
    source = "AWS Lambda：数量5，每个函数内存1024MB，平均执行时长800ms，每月总调用量约2000万次"
    parsed = ParsedIntent(
        customer_summary="lambda",
        services=[ServiceRequirement(service="lambda", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    requirement = parsed.services[0]
    assert requirement.quantity == 5
    assert requirement.requirements["memory_mb"] == 1024
    assert requirement.requirements["duration_ms"] == 800
    assert requirement.requirements["requests"] == 20_000_000
    assert requirement.field_scopes["requests"] == "aggregate"
    assert requirement.field_match_policies["requests"] == "approximate"


def test_aurora_high_availability_uses_minimum_members_without_rewriting_product() -> None:
    source = "Amazon Aurora MySQL，MySQL兼容数据库，高可用部署，存储容量2TB，数量1套"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="rds",
                calculator_service_name="Amazon RDS / Amazon Aurora",
                quantity=1,
                source_text=source,
                requirements={"engine": "aurora_mysql", "storage_gib": 2048},
            )
        ],
    )

    preserve_customer_configuration(parsed)

    aurora = parsed.services[0]
    assert aurora.service == "rds"
    assert aurora.calculator_service_name == "Amazon Aurora MySQL"
    assert aurora.quantity == 1
    assert aurora.requirements["deployment"] == "multi_az"
    assert aurora.requirements["cluster_members"] == 2
    assert aurora.field_sources["requirements.cluster_members"] == "system_minimum"


def test_plain_rds_mysql_never_uses_the_combined_rds_aurora_display_name() -> None:
    parsed = ParsedIntent(
        customer_summary="RDS MySQL",
        services=[
            ServiceRequirement(
                service="rds",
                calculator_service_name="Amazon RDS / Amazon Aurora",
                source_text=(
                    "Amazon RDS MySQL：区域：新加坡（ap-southeast-1），"
                    "配置4核16GB，存储500GB，Multi-AZ高可用"
                ),
                requirements={
                    "engine": "mysql",
                    "vcpu": 4,
                    "memory_gib": 16,
                    "storage_gib": 500,
                    "deployment": "multi_az",
                    "aurora_cluster": True,
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)

    database = parsed.services[0]
    assert database.calculator_service_name == "Amazon RDS MySQL"
    assert database.product_identity == "rds_mysql"
    assert database.requirements["engine"] == "mysql"
    assert "aurora_cluster" not in database.requirements


@pytest.mark.parametrize(
    ("service", "source", "requirements", "identity", "display", "locked_field", "locked_value"),
    [
        (
            "rds",
            "Amazon Aurora PostgreSQL，高可用部署",
            {"engine": "postgresql"},
            "aurora_postgresql",
            "Amazon Aurora PostgreSQL",
            "engine",
            "aurora_postgresql",
        ),
        (
            "elasticache",
            "Amazon ElastiCache for Valkey，2个节点",
            {"engine": "redis"},
            "elasticache_valkey",
            "Amazon ElastiCache for Valkey",
            "engine",
            "valkey",
        ),
        (
            "elb",
            "使用 Network Load Balancer（NLB）",
            {"load_balancer_type": "application"},
            "network_load_balancer",
            "Network Load Balancer",
            "load_balancer_type",
            "network",
        ),
        (
            "mq",
            "Amazon MQ for RabbitMQ，3个 Broker",
            {"engine_type": "activemq"},
            "amazon_mq_rabbitmq",
            "Amazon MQ for RabbitMQ",
            "engine_type",
            "rabbitmq",
        ),
        (
            "apigateway",
            "Amazon API Gateway REST API",
            {"api_type": "http"},
            "api_gateway_rest",
            "Amazon API Gateway REST API",
            "api_type",
            "rest",
        ),
        (
            "msk",
            "Amazon MSK Serverless",
            {"cluster_type": "provisioned"},
            "amazon_msk_serverless",
            "Amazon MSK Serverless",
            "cluster_type",
            "serverless",
        ),
        (
            "fsx",
            "Amazon FSx for Lustre，10TB",
            {"file_system_type": "windows"},
            "amazon_fsx_lustre",
            "Amazon FSx for Lustre",
            "file_system_type",
            "lustre",
        ),
    ],
)
def test_shared_pricing_families_keep_independent_customer_product_identity(
    service: str,
    source: str,
    requirements: dict[str, object],
    identity: str,
    display: str,
    locked_field: str,
    locked_value: str,
) -> None:
    parsed = ParsedIntent(
        customer_summary="product identity",
        services=[
            ServiceRequirement(
                service=service,
                source_text=source,
                requirements=requirements,
            )
        ],
    )

    preserve_customer_configuration(parsed)

    item = parsed.services[0]
    assert item.product_identity == identity
    assert item.calculator_service_name == display
    assert item.requirements[locked_field] == locked_value
    assert f"requirements.{locked_field}" in item.locked_fields


def test_customer_confirmed_product_variant_has_priority_over_original_text() -> None:
    parsed = ParsedIntent(
        customer_summary="corrected cache engine",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text="客户原来写 Redis",
                requirements={"engine": "valkey"},
                field_sources={"requirements.engine": "customer_confirmation"},
                locked_fields=["requirements.engine"],
            )
        ],
    )

    preserve_customer_configuration(parsed)

    cache = parsed.services[0]
    assert cache.product_identity == "elasticache_valkey"
    assert cache.calculator_service_name == "Amazon ElastiCache for Valkey"
    assert cache.requirements["engine"] == "valkey"
    assert cache.field_sources["requirements.engine"] == "customer_confirmation"


def test_opensearch_per_node_cpu_is_not_mistaken_for_node_count() -> None:
    source = "搜索这块现在是ES，想换OpenSearch，3个节点，每个节点4核16G，500G盘。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text=source,
                requirements={
                    "data_nodes": 3,
                    "data_node_vcpu": 4,
                    "data_node_memory_gib": 16,
                    "data_node_storage_gib": 500,
                },
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_service_architecture(source, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    requirements = parsed.services[0].requirements

    assert requirements["data_nodes"] == 3
    assert "nodes" not in requirements
    assert requirements["vcpu"] == 4
    assert requirements["memory_gib"] == 16


def test_eks_exact_worker_model_and_count_are_split_into_ec2() -> None:
    source = """Amazon EKS
区域：ap-southeast-1
Kubernetes 集群：1 套
Worker 节点型号：m7g.large
Worker 节点数量：5 台"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="eks",
                region="ap-southeast-1",
                quantity=1,
                source_text=source,
                requirements={"requested_model": "m7g.large", "node_count": 5},
            )
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    eks = next(item for item in parsed.services if item.service == "eks")
    worker = next(item for item in parsed.services if item.service == "ec2")
    assert eks.quantity == 1
    assert "requested_model" not in eks.requirements
    assert "node_count" not in eks.requirements
    assert worker.quantity == 5
    assert worker.requirements == {
        "requested_model": "m7g.large",
        "operating_system": "Linux",
    }


def test_eks_workers_per_cluster_are_multiplied_without_losing_shape() -> None:
    source = """部分业务使用Kubernetes。
先部署2套集群，
每套worker节点4台，
配置8核32G。"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="eks",
                region="ap-east-1",
                quantity=2,
                source_text=source,
                requirements={"cluster_count": 2},
            )
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    worker = next(item for item in parsed.services if item.service == "ec2")
    assert worker.quantity == 8
    assert worker.requirements["vcpu"] == 8
    assert worker.requirements["memory_gib"] == 32
    assert worker.source_text == source


def test_eks_worker_quantity_word_is_multiplied_by_cluster_count() -> None:
    source = (
        "Amazon EKS：区域：东京（ap-northeast-1），用途：微服务容器平台，"
        "集群数量：2个，每个集群Worker节点数量：3台"
    )
    parsed = ParsedIntent(
        customer_summary="EKS",
        services=[
            ServiceRequirement(
                service="eks",
                region="ap-northeast-1",
                quantity=2,
                source_text=source,
                requirements={"cluster_count": 2},
            )
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    worker = next(item for item in parsed.services if item.service == "ec2")
    assert worker.quantity == 6


def test_customer_confirmed_eks_worker_count_is_not_overwritten_by_parent_formula() -> None:
    source = "Amazon EKS：数量2，每个集群Worker节点数量3台"
    parsed = ParsedIntent(
        customer_summary="EKS",
        services=[
            ServiceRequirement(service="eks", quantity=2, source_text=source),
            ServiceRequirement(
                service="ec2",
                derived_from_service="eks",
                calculator_service_name="Amazon EC2 (EKS Worker Nodes)",
                quantity=5,
                source_text=source,
                field_sources={"quantity": "customer_confirmation"},
            ),
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    worker = next(item for item in parsed.services if item.service == "ec2")
    assert worker.quantity == 5
    assert worker.field_sources["quantity"] == "customer_confirmation"


def test_prometheus_identity_overrides_cloudwatch_mapping() -> None:
    parsed = ParsedIntent(
        customer_summary="Prometheus",
        services=[
            ServiceRequirement(
                service="cloudwatch",
                calculator_service_name="Amazon CloudWatch",
                source_text="Prometheus：用于 Kubernetes 指标监控",
            )
        ],
    )

    DeepSeekIntentParser._normalize_prometheus_managed_service(parsed)

    component = parsed.services[0]
    assert component.service == "amp"
    assert component.calculator_service_name == ("Amazon Managed Service for Prometheus (AMP)")
    assert component.product_identity == "prometheus"


def test_numbered_shorthand_blocks_restore_opensearch_storage_and_unsized_eks_workers() -> None:
    """A partial first-pass result cannot discard later facts in its numbered block."""

    text = """7、搜索服务：目前使用ES做搜索和日志分析，预计3个节点，每个节点4核16G，存储500GB。

8、容器：部分应用准备放到Kubernetes里面，先部署1套EKS集群，worker节点3台。"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text="搜索服务：目前使用ES做搜索和日志分析，预计3个节点，每个节点4核16G。",
                requirements={"data_nodes": 3, "vcpu": 4, "memory_gib": 16},
            ),
            ServiceRequirement(
                service="eks",
                quantity=1,
                source_text="容器：先部署1套EKS集群。",
                requirements={"cluster_count": 1},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._isolate_shared_component_sources(parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    search = next(item for item in parsed.services if item.service == "opensearch")
    worker = next(
        item
        for item in parsed.services
        if item.service == "ec2" and item.calculator_service_name == "Amazon EC2 (EKS Worker Nodes)"
    )
    assert search.requirements["data_nodes"] == 3
    assert search.requirements["storage_gib_per_node"] == 500
    assert worker.quantity == 3
    assert worker.requirements == {"operating_system": "Linux"}
    assert worker.derived_from_service == "eks"
    assert worker.field_sources["_customer_select_configuration"] == "system_policy"
    assert "worker节点3台" in worker.source_text


def test_existing_narrow_worker_fragment_is_updated_not_duplicated() -> None:
    source = """部分业务使用Kubernetes。
先部署2套集群，
每套worker节点4台，
配置8核32G。"""
    worker_fragment = "每套worker节点4台，配置8核32G。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="eks",
                region="ap-east-1",
                quantity=2,
                source_text=source,
                requirements={"cluster_count": 2},
            ),
            ServiceRequirement(
                service="ec2",
                region="ap-east-1",
                quantity=4,
                source_text=worker_fragment,
                requirements={"vcpu": 8, "memory_gib": 32},
            ),
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    workers = [item for item in parsed.services if item.service == "ec2"]
    assert len(workers) == 1
    assert workers[0].quantity == 8
    assert workers[0].requirements["memory_gib"] == 32
    assert workers[0].source_text == source


def test_edited_eks_worker_is_reused_by_stable_parent_and_duplicate_is_removed() -> None:
    source = (
        "Amazon EKS：数量2，用于生产和测试 Kubernetes 集群，每个集群配置3个Worker节点，"
        "Worker节点单台4核8GB/100GB存储，Linux系统"
    )
    parsed = ParsedIntent(
        customer_summary="EKS",
        services=[
            ServiceRequirement(
                service="eks",
                component_key="cmp_parent_eks",
                region="ap-east-1",
                quantity=2,
                source_text=source,
                requirements={
                    "cluster_count": 2,
                    "worker_nodes_per_cluster": 3,
                    "total_worker_system_disk_gib": 300,
                },
            ),
            ServiceRequirement(
                service="ec2",
                component_key="cmp_edited_worker",
                derived_from_service="eks",
                parent_component_key="cmp_parent_eks",
                calculator_service_name="Amazon EC2 (EKS Worker Nodes)",
                quantity=2,
                source_text=f"客户通过配置表直接修改：system_disk_gib\n{source}",
                requirements={
                    "vcpu": 4,
                    "memory_gib": 8,
                    "system_disk_gib": 6776,
                    "operating_system": "Linux",
                },
                field_sources={
                    "requirements.system_disk_gib": "customer_confirmation",
                },
            ),
            ServiceRequirement(
                service="ec2",
                component_key="cmp_duplicate_worker",
                derived_from_service="eks",
                parent_component_key="cmp_parent_eks",
                calculator_service_name="Amazon EC2 (EKS Worker Nodes)",
                quantity=6,
                source_text=source,
                requirements={"vcpu": 4, "memory_gib": 8, "operating_system": "Linux"},
            ),
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    workers = [item for item in parsed.services if item.service == "ec2"]
    assert len(workers) == 1
    assert workers[0].component_key == "cmp_edited_worker"
    assert workers[0].parent_component_key == "cmp_parent_eks"
    assert workers[0].quantity == 6
    assert workers[0].requirements["system_disk_gib"] == 6776
    assert workers[0].field_sources["requirements.system_disk_gib"] == (
        "customer_confirmation"
    )
    assert "total_worker_system_disk_gib" not in parsed.services[0].requirements


def test_eks_worker_disk_and_total_are_derived_from_per_worker_customer_value() -> None:
    source = (
        "Amazon EKS：数量2，每个集群配置3个Worker节点，"
        "Worker节点单台4核8GB/100GB存储，Linux系统"
    )
    parsed = ParsedIntent(
        customer_summary="EKS",
        services=[
            ServiceRequirement(
                service="eks",
                quantity=2,
                source_text=source,
                requirements={"cluster_count": 2, "worker_nodes_per_cluster": 3},
            )
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    parent = next(item for item in parsed.services if item.service == "eks")
    worker = next(item for item in parsed.services if item.service == "ec2")
    assert worker.quantity == 6
    assert worker.requirements["system_disk_gib"] == 100
    assert worker.parent_component_key == parent.component_key
    assert not any(field.startswith("requirements.worker_") for field in parent.locked_fields)


def test_all_products_reject_an_unbound_numeric_customer_fact() -> None:
    source = "Future AWS Product：容量500GB，每月200万次请求"
    filled = ServiceRequirement(
        service="future_product",
        source_text=source,
        requirements={"storage_gib": 500},
        field_evidence={"requirements.storage_gib": "容量500GB"},
    )

    issues = DeepSeekIntentParser._uncovered_quantitative_claim_issues(source, filled)

    assert any("200万次请求" in issue for issue in issues)


def test_shared_numbered_block_is_split_by_service_before_extraction() -> None:
    shared = """网络：
需要公网负载均衡，
另外需要CDN加速，
每月流量大概10TB。"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="elb", source_text=shared),
            ServiceRequirement(service="cloudfront", source_text=shared),
        ],
    )

    DeepSeekIntentParser._isolate_shared_component_sources(parsed)

    elb = next(item for item in parsed.services if item.service == "elb")
    cloudfront = next(item for item in parsed.services if item.service == "cloudfront")
    assert "10TB" not in elb.source_text
    assert "负载均衡" in elb.source_text
    assert "10TB" in cloudfront.source_text
    assert "CDN" in cloudfront.source_text


def test_one_line_shared_block_is_also_split_by_service() -> None:
    shared = "网络：需要公网负载均衡，另外需要CDN加速，每月流量大概10TB。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="elb", source_text=shared),
            ServiceRequirement(service="cloudfront", source_text=shared),
        ],
    )

    DeepSeekIntentParser._isolate_shared_component_sources(parsed)

    elb = next(item for item in parsed.services if item.service == "elb")
    cloudfront = next(item for item in parsed.services if item.service == "cloudfront")
    assert "10TB" not in elb.source_text
    assert "10TB" in cloudfront.source_text


def test_inventory_keeps_related_services_without_duplicating_attached_alb() -> None:
    lines = [
        "AWS VPC + 子网 数量 1 套",
        "AWS ALB 数量 1 套",
        "AWS WAF 数量 1 套",
        "规格/说明：挂载 ALB",
        "AWS DMS 数量 1 套 dms.t3.large",
        "Secrets Manager / KMS 1套",
        "CloudWatch + X-Ray 1套",
    ]
    keys = [key for line in lines for key, _ in DeepSeekIntentParser._inventory_keys_for_line(line)]

    assert keys.count("elb") == 1
    assert {"vpc", "waf", "dms", "secrets_manager", "kms", "cloudwatch", "xray"}.issubset(keys)


def test_redis_memory_times_total_nodes_is_not_interpreted_as_shards() -> None:
    source = "ElastiCache Redis 数量1集群 8GB × 3节点"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text=source,
                requirements={"memory_gib": 8, "shards": 3},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_service_architecture(source, parsed)

    assert parsed.services[0].requirements["memory_gib"] == 8
    assert parsed.services[0].requirements["shards"] == 1
    assert parsed.services[0].requirements["replicas_per_shard"] == 2


@pytest.mark.parametrize(
    ("source", "expected_replicas"),
    [
        ("Redis 架构：一主两从", 2),
        ("Redis 架构：一主二从", 2),
        ("Redis 架构：1主2从", 2),
        ("Redis 架构：一主三从", 3),
        ("Redis 主备模式", 1),
    ],
)
def test_redis_chinese_primary_replica_topology_is_exact(
    source: str, expected_replicas: int
) -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text=source,
                requirements={"shards": 1, "replicas_per_shard": 1},
            )
        ],
    )

    DeepSeekIntentParser._normalize_redis_topology(parsed)

    assert parsed.services[0].requirements["shards"] == 1
    assert parsed.services[0].requirements["replicas_per_shard"] == expected_replicas


def test_sales_numbering_is_a_hard_component_boundary() -> None:
    text = """区域：新加坡
1、Amazon EC2
数量：4台
配置：8核32G
2、Amazon RDS MySQL
数量：1
存储：500GB
3、Amazon MSK
Broker数量：3
每节点：4核16G
磁盘：500GB"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="ec2", source_text="Amazon EC2"),
            ServiceRequirement(service="rds", source_text="Amazon RDS MySQL"),
            ServiceRequirement(service="msk", source_text="Amazon MSK"),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    ec2, rds, msk = parsed.services
    assert "数量：4台" in ec2.source_text
    assert "Amazon RDS" not in ec2.source_text
    assert "存储：500GB" in rds.source_text
    assert "Broker数量" not in rds.source_text
    assert "每节点：4核16G" in msk.source_text


def test_cluster_nodes_do_not_multiply_cluster_quantity() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                quantity=3,
                source_text="Kafka预计3个节点",
                requirements={"broker_count": 3},
            ),
            ServiceRequirement(
                service="opensearch",
                quantity=3,
                source_text="ES集群预计3个节点",
                requirements={"data_nodes": 3},
            ),
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert [item.quantity for item in parsed.services] == [1, 1]


def test_msk_literal_node_count_overrides_generic_quantity_and_minimum_default() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                quantity=3,
                source_text=("消息队列：目前有Kafka需求，预计3个节点，每个节点4核16G，磁盘500GB。"),
                requirements={"broker_count": 2, "vcpu": 4, "memory_gib": 16},
            ),
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert parsed.services[0].quantity == 1
    assert parsed.services[0].requirements["broker_count"] == 3
    assert "requirements.broker_count" in parsed.services[0].locked_fields


def test_msk_deployment_quantity_label_with_broker_suffix_is_not_cluster_count() -> None:
    parsed = ParsedIntent(
        customer_summary="Kafka",
        services=[
            ServiceRequirement(
                service="msk",
                quantity=3,
                source_text=(
                    "Apache Kafka：区域：新加坡，用途：业务消息队列和实时数据流处理，"
                    "部署数量：3个Broker节点"
                ),
                requirements={"broker_count": 3},
            )
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert parsed.services[0].quantity == 1
    assert parsed.services[0].requirements["broker_count"] == 3


@pytest.mark.parametrize(
    ("service", "field", "source"),
    [
        ("mq", "broker_count", "RabbitMQ 部署数量：3个 Broker 节点"),
        ("opensearch", "data_nodes", "OpenSearch 部署数量：4个数据节点"),
        ("eks", "worker_node_count", "EKS 包含 6 个 Worker 节点"),
        ("documentdb", "instance_count", "DocumentDB 集群包含 3 个数据库实例"),
        ("redshift", "nodes", "Redshift 集群包含 2 个计算节点"),
        ("ecs", "tasks", "ECS 服务运行 8 个任务"),
    ],
)
def test_internal_topology_never_becomes_complete_deployment_quantity(
    service: str, field: str, source: str
) -> None:
    parsed = ParsedIntent(
        customer_summary=service,
        services=[
            ServiceRequirement(
                service=service,
                quantity=3,
                source_text=source,
                requirements={field: 3},
            )
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert parsed.services[0].quantity == 1


def test_explicit_independent_cluster_count_is_preserved() -> None:
    parsed = ParsedIntent(
        customer_summary="Kafka",
        services=[
            ServiceRequirement(
                service="msk",
                quantity=3,
                source_text="Kafka 集群数量：2，每个集群 3 个 Broker 节点",
                requirements={"broker_count": 3},
            )
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert parsed.services[0].quantity == 2
    assert parsed.services[0].requirements["broker_count"] == 3


def test_rabbitmq_nodes_shape_and_deployment_quantity_are_reconciled_together() -> None:
    source = "目前使用RabbitMQ，预计3个节点，每个节点4核16G。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="mq",
                calculator_service_name="Amazon MQ",
                quantity=3,
                source_text=source,
                requirements={"broker_count": 4, "requested_model": "mq.t3.micro"},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    item = parsed.services[0]
    assert item.quantity == 1
    assert item.requirements["broker_count"] == 3
    assert item.requirements["vcpu"] == 4
    assert item.requirements["memory_gib"] == 16
    assert "requirements.broker_count" in item.locked_fields


def test_numeric_field_evidence_must_support_the_filled_value() -> None:
    component = ServiceRequirement(
        service="mq",
        source_text="RabbitMQ预计3个节点，每个节点4核16G。",
        requirements={"broker_count": 4},
        field_evidence={"requirements.broker_count": "3个节点"},
    )

    with pytest.raises(ValueError, match="与原文证据中的数值"):
        DeepSeekIntentParser._validate_component_evidence(
            component,
            provided_payload={"requirements": {"broker_count": 4}},
            source_text=component.source_text,
            original=ServiceRequirement(service="mq", source_text=component.source_text),
        )


def test_unsupported_managed_mq_topology_becomes_customer_question_before_quote() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="mq",
                calculator_service_name="Amazon MQ",
                source_text="RabbitMQ需要2个节点",
                requirements={"engine_type": "rabbitmq", "broker_count": 2},
            )
        ],
    )

    DeepSeekIntentParser._append_vague_value_questions(parsed)

    assert len(parsed.ambiguities) == 1
    assert "1个还是3个" in parsed.ambiguities[0]


def test_cloudfront_summary_and_usage_fragments_merge_and_false_vpc_drops() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="vpc", source_text="网络："),
            ServiceRequirement(service="cloudfront", source_text="需要CDN加速"),
            ServiceRequirement(
                service="cloudfront",
                source_text="CDN流量预计每月5TB",
                requirements={"data_transfer_out_gib": 5120},
            ),
        ],
    )

    DeepSeekIntentParser._drop_unrequested_section_services(
        "网络：\n需要CDN加速\nCDN流量预计每月5TB", parsed
    )
    DeepSeekIntentParser._merge_duplicate_service_fragments(parsed)

    assert [item.service for item in parsed.services] == ["cloudfront"]
    assert parsed.services[0].requirements["data_transfer_out_gib"] == 5120


def test_s3_capacity_is_recovered_from_natural_numbered_wording() -> None:
    source = "文件存储预计30TB左右，主要存图片和业务文件。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[ServiceRequirement(service="s3", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    assert parsed.services[0].requirements["storage_gib"] == 30 * 1024


def test_pricing_fact_conservation_recovers_s3_cloudfront_and_ec2_disk() -> None:
    """Every literal pricing dimension survives isolated component cleanup."""

    s3_source = (
        "Amazon S3：S3 Standard，存储容量20TB，"
        "每月PUT请求约500万次，GET请求约8000万次"
    )
    cloudfront_source = (
        "Amazon CloudFront：数量1，每月公网下行流量10TB，"
        "每月HTTPS请求约2亿次，访问区域以亚太地区为主"
    )
    jira_source = "一台jira，硬盘400G"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="s3", source_text=s3_source),
            ServiceRequirement(service="cloudfront", source_text=cloudfront_source),
            ServiceRequirement(service="ec2", source_text=jira_source),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    s3, cloudfront, jira = parsed.services
    assert s3.requirements == {
        "storage_gib": 20 * 1024,
        "storage_class": "Standard",
        "put_copy_post_list_requests": 5_000_000,
        "get_select_requests": 80_000_000,
    }
    assert cloudfront.requirements == {
        "data_transfer_out_gib": 10 * 1024,
        "https_requests": 200_000_000,
        "traffic_geography": "Asia Pacific",
    }
    assert jira.requirements["system_disk_gib"] == 400
    for component in parsed.services:
        for field in component.requirements:
            path = f"requirements.{field}"
            assert component.field_sources[path] == "customer_text"
            assert component.field_evidence[path]


def test_literal_recovery_never_overwrites_a_later_customer_edit() -> None:
    source = (
        "Amazon CloudFront：每月公网下行流量10TB，"
        "每月HTTPS请求约2亿次，访问区域以亚太地区为主"
    )
    component = ServiceRequirement(
        service="cloudfront",
        source_text=source,
        requirements={"traffic_geography": "Europe"},
        field_sources={"requirements.traffic_geography": "customer_correction"},
        field_evidence={"requirements.traffic_geography": "客户改为 Europe"},
        locked_fields=["requirements.traffic_geography"],
    )
    parsed = ParsedIntent(customer_summary=source, services=[component])

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    assert parsed.services[0].requirements["traffic_geography"] == "Europe"
    assert (
        parsed.services[0].field_sources["requirements.traffic_geography"]
        == "customer_correction"
    )


def test_saved_draft_pricing_ledger_is_upgraded_and_stale_review_is_removed() -> None:
    source = (
        "Amazon CloudFront：每月公网下行流量10TB，"
        "每月HTTPS请求约2亿次，访问区域以亚太地区为主"
    )
    component = ServiceRequirement(
        service="cloudfront",
        source_text=source,
        original_source_text=source,
        requirements={
            "data_transfer_out_gib": 10 * 1024,
            "traffic_geography": "United States",
            "_review_selected_model": "CloudFront Pay-as-you-go",
            "_review_selected_specifications": {
                "dataTransferOutGiB": 10 * 1024,
                "httpsRequests": None,
                "priceClassGeography": "United States",
            },
        },
        field_sources={
            "requirements.traffic_geography": "customer_confirmation",
        },
        field_evidence={
            "requirements.traffic_geography": "客户从 CloudFront 官方流量地区中选择",
        },
        locked_fields=["requirements.traffic_geography"],
    )
    parsed = ParsedIntent(customer_summary=source, services=[component])

    DeepSeekIntentParser.reconcile_customer_pricing_facts(parsed)

    requirements = parsed.services[0].requirements
    assert requirements["https_requests"] == 200_000_000
    assert requirements["traffic_geography"] == "Asia Pacific"
    assert not any(field.startswith("_review_") for field in requirements)
    assert (
        parsed.services[0].field_sources["requirements.traffic_geography"]
        == "customer_text"
    )


def test_request_claims_are_detected_when_label_precedes_the_number() -> None:
    source = "Amazon S3：存储20TB，每月PUT请求约500万次"
    filled = ServiceRequirement(
        service="s3",
        source_text=source,
        requirements={"storage_gib": 20 * 1024},
        field_evidence={"requirements.storage_gib": "存储20TB"},
    )

    issues = DeepSeekIntentParser._uncovered_quantitative_claim_issues(source, filled)

    assert any("PUT请求约500万次" in issue for issue in issues)


def test_capacity_recovery_uses_canonical_service_identity_for_all_aliases() -> None:
    """Display-name spellings must not bypass the component's own source guard."""

    cases = [
        (
            ServiceRequirement(
                service="amazon_msk",
                source_text="Kafka：预计3个节点，每个节点8核32G，磁盘2TB。",
                requirements={"broker_count": 3, "memory_gib": 16},
            ),
            {"broker_count": 3, "vcpu": 8, "memory_gib": 32, "storage_gib_per_broker": 2048},
        ),
        (
            ServiceRequirement(
                service="amazon_opensearch_service",
                source_text="ES：预计5个节点，每节点8核32G，磁盘1TB。",
                requirements={"data_nodes": 3, "storage_gib_per_node": 2048},
            ),
            {"data_nodes": 5, "vcpu": 8, "memory_gib": 32, "storage_gib_per_node": 1024},
        ),
        (
            ServiceRequirement(
                service="amazon_ec2",
                source_text="每套worker节点4台，配置8核32G。",
                requirements={"vcpu": 8, "memory_gib": 16},
            ),
            {"vcpu": 8, "memory_gib": 32},
        ),
    ]

    parsed = ParsedIntent(customer_summary="x", services=[item for item, _ in cases])
    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    for item, (_, expected) in zip(parsed.services, cases, strict=True):
        for field, value in expected.items():
            assert item.requirements[field] == value


def test_broker_cpu_cannot_overwrite_literal_broker_count() -> None:
    source = "Kafka：每个Broker 8核CPU、16GB内存、2TB磁盘，共3个Broker。"
    parsed = ParsedIntent(
        customer_summary="Kafka",
        services=[
            ServiceRequirement(
                service="msk",
                quantity=3,
                source_text=source,
                requirements={"broker_count": 8, "vcpu": 8, "memory_gib": 16},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)
    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    component = parsed.services[0]
    assert component.quantity == 1
    assert component.requirements["broker_count"] == 3
    assert component.requirements["storage_gib_per_broker"] == 2048
    assert component.requirements["total_storage_gib"] == 6144


def test_mysql_primary_replica_keeps_member_count_without_double_pricing_quantity() -> None:
    source = (
        "MySQL：每个数据库节点16核CPU、64GB内存、2TB磁盘，"
        "共2个节点，采用1主1从。"
    )
    parsed = ParsedIntent(
        customer_summary="MySQL",
        services=[
            ServiceRequirement(
                service="rds",
                quantity=2,
                source_text=source,
                requirements={"engine": "mysql", "vcpu": 16, "memory_gib": 64},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    DeepSeekIntentParser._normalize_database_group_quantity(parsed)
    DeepSeekIntentParser._sanitize_parsed_requirements(parsed)

    component = parsed.services[0]
    assert component.quantity == 1
    assert component.requirements["deployment"] == "multi_az"
    assert component.requirements["instance_count"] == 2
    DeepSeekIntentParser._validate_numeric_evidence_value(
        component,
        path="quantity",
        snippet="共2个节点，采用1主1从",
    )
    assert DeepSeekIntentParser._deterministic_component_audit_issues(
        ServiceRequirement(service="rds", source_text=source), component
    ) == []


def test_self_hosted_service_switch_preserves_per_node_disk_and_count() -> None:
    source = "Flink：每个节点24核CPU、64GB内存、500GB磁盘，共3个节点。"
    parsed = ParsedIntent(
        customer_summary="Flink",
        services=[
            ServiceRequirement(
                service="flink",
                calculator_service_name="Flink",
                source_text=source,
                requirements={"storage_gib": 500},
            )
        ],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, source)
    DeepSeekIntentParser._sanitize_parsed_requirements(parsed)

    component = parsed.services[0]
    assert component.service == "ec2"
    assert component.quantity == 3
    assert component.requirements["vcpu"] == 24
    assert component.requirements["memory_gib"] == 64
    assert component.requirements["system_disk_gib"] == 500
    assert "storage_gib" not in component.requirements


def test_redis_keeps_cpu_node_count_and_source_storage_separate_from_memory() -> None:
    source = "Redis：每个节点16核CPU、64GB内存、500GB存储，共3个节点。"
    parsed = ParsedIntent(
        customer_summary="Redis",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text=source,
                requirements={"engine": "redis", "memory_gib": 500},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    DeepSeekIntentParser._reconcile_explicit_service_architecture(source, parsed)
    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)
    DeepSeekIntentParser._sanitize_parsed_requirements(parsed)

    component = parsed.services[0]
    assert component.quantity == 1
    assert component.requirements["vcpu"] == 16
    assert component.requirements["memory_gib"] == 64
    assert component.requirements["node_count"] == 3
    assert component.requirements["shards"] == 1
    assert component.requirements["replicas_per_shard"] == 2
    assert component.requirements["source_storage_gib_per_node"] == 500
    assert "requirements.node_count" in component.locked_fields


def test_cleaned_component_source_never_falls_back_to_full_quote() -> None:
    """Line folding must not let one component read a neighbour's numbers."""

    full_text = """业务服务器：8核16G
Kafka：3个节点
每个节点8核32G
磁盘2TB
ES：5个节点
每节点8核32G
磁盘1TB
Kubernetes：每套worker节点4台
配置8核32G"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                source_text="Kafka：3个节点，每个节点8核32G，磁盘2TB",
                requirements={"memory_gib": 16},
            ),
            ServiceRequirement(
                service="opensearch",
                source_text="ES：5个节点，每节点8核32G，磁盘1TB",
                requirements={"data_nodes": 3, "storage_gib_per_node": 2048},
            ),
            ServiceRequirement(
                service="ec2",
                source_text="Kubernetes：每套worker节点4台，配置8核32G",
                requirements={"memory_gib": 16},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(full_text, parsed)

    msk, search, worker = parsed.services
    assert msk.requirements["memory_gib"] == 32
    assert msk.requirements["storage_gib_per_broker"] == 2048
    assert search.requirements["data_nodes"] == 5
    assert search.requirements["storage_gib_per_node"] == 1024
    assert worker.requirements["memory_gib"] == 32


def test_duplicate_merge_cannot_overwrite_customer_locked_component_fields() -> None:
    source = "ES：预计5个节点，每节点8核32G，磁盘1TB。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text=source,
                requirements={
                    "data_nodes": 5,
                    "vcpu": 8,
                    "memory_gib": 32,
                    "storage_gib_per_node": 1024,
                },
                field_evidence={
                    "requirements.data_nodes": "5个节点",
                    "requirements.memory_gib": "32G",
                },
                field_sources={
                    "requirements.data_nodes": "customer_text",
                    "requirements.memory_gib": "customer_text",
                },
                locked_fields=[
                    "requirements.data_nodes",
                    "requirements.memory_gib",
                ],
            ),
            ServiceRequirement(
                service="amazon_opensearch_service",
                source_text=source,
                requirements={
                    "data_nodes": 3,
                    "memory_gib": 16,
                    "storage_gib_per_node": 2048,
                },
            ),
        ],
    )

    DeepSeekIntentParser._merge_duplicate_service_fragments(parsed)

    assert len(parsed.services) == 1
    requirements = parsed.services[0].requirements
    assert requirements["data_nodes"] == 5
    assert requirements["memory_gib"] == 32


def test_rds_primary_standby_is_one_deployment() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="rds",
                quantity=2,
                source_text="数据库现在用MySQL，500G，需要主备高可用。",
                requirements={"engine": "mysql", "deployment": "multi_az"},
            ),
            ServiceRequirement(
                service="rds",
                quantity=1,
                source_text="PostgreSQL 数据库，存储300GB。",
                requirements={"engine": "postgresql", "storage_gib": 300},
            ),
        ],
    )

    DeepSeekIntentParser._normalize_database_group_quantity(parsed)
    assert parsed.services[0].quantity == 1
    assert "deployment" not in parsed.services[1].requirements


@pytest.mark.asyncio
async def test_component_template_error_is_sent_back_to_ai_for_self_repair() -> None:
    class SelfRepairGateway:
        def __init__(self) -> None:
            self.calls = 0
            self.inputs: list[str] = []

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            self.calls += 1
            self.inputs.append(str(kwargs.get("user_content") or ""))
            if self.calls == 1:
                return {"component": "not-an-object"}
            if self.calls == 2:
                return {
                    "component": {
                        "service": "s3",
                        "calculator_service_name": "Amazon S3",
                        "quantity": 1,
                        "hours_per_month": 730,
                        "requirements": {"storage_gib": 1024},
                        "field_evidence": {
                            "requirements.storage_gib": "S3 1TB",
                        },
                        "source_text": "S3 1TB",
                        "query_action": None,
                    }
                }
            return {"corrections": {}, "customer_questions": []}

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = SelfRepairGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    intent = ParsedIntent(
        customer_summary="S3",
        services=[
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                source_text="S3 1TB",
            )
        ],
    )

    repaired = await parser._cleanup_components("S3 1TB", intent)

    assert gateway.calls == 2
    assert "程序校验错误" in gateway.inputs[1]
    assert repaired.services[0].requirements["storage_gib"] == 1024


def test_numbered_block_rebinds_unknown_model_alias_and_merges_fragments() -> None:
    text = """5、公网出站流量：
新加坡区域，每月公网出网流量1000GB。"""
    parsed = ParsedIntent(
        customer_summary="公网流量",
        services=[
            ServiceRequirement(
                service="data_transfer",
                calculator_service_name="AWS Data Transfer",
                source_text="5、公网出站流量：",
            ),
            ServiceRequirement(
                # Deliberately use an unseen generated alias.  The parser must
                # not need an alias-table entry to recover the component.
                service="monthly_public_egress",
                calculator_service_name="Data Transfer",
                source_text="新加坡区域，每月公网出网流量1000GB。",
                requirements={"data_transfer_out_gib": 1000},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    assert len(parsed.services) == 1
    component = parsed.services[0]
    assert component.service == "data_transfer"
    assert component.calculator_service_name == "AWS Data Transfer"
    assert component.source_text == "公网出站流量：\n新加坡区域，每月公网出网流量1000GB。"
    assert component.requirements["data_transfer_out_gib"] == 1000


def test_same_service_numbered_blocks_remain_independent_by_source() -> None:
    text = """1、AWS Data Transfer：新加坡每月公网出网1000GB。
2、AWS Data Transfer：悉尼每月公网出网500GB。"""
    parsed = ParsedIntent(
        customer_summary="两地公网流量",
        services=[
            ServiceRequirement(
                service="egress_sydney_generated_name",
                source_text="悉尼每月公网出网500GB。",
                requirements={"data_transfer_out_gib": 500},
            ),
            ServiceRequirement(
                service="egress_singapore_generated_name",
                source_text="新加坡每月公网出网1000GB。",
                requirements={"data_transfer_out_gib": 1000},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    assert len(parsed.services) == 2
    assert [item.requirements["data_transfer_out_gib"] for item in parsed.services] == [
        1000,
        500,
    ]
    assert all(item.service == "data_transfer" for item in parsed.services)


def test_space_numbered_components_do_not_split_numbered_service_fields() -> None:
    text = """1 Amazon EC2
区域：新加坡
数量：1
2 Amazon MSK
区域：新加坡
3 Broker节点
Kafka集群
3 Amazon CloudFront
每月流量5TB"""

    blocks = DeepSeekIntentParser._numbered_requirement_blocks(text)

    assert len(blocks) == 3
    assert blocks[1] == "Amazon MSK\n区域：新加坡\n3 Broker节点\nKafka集群"
    assert DeepSeekIntentParser._numbered_requirement_match("3 Broker节点") is None
    assert DeepSeekIntentParser._numbered_requirement_match("4核16G") is None


def test_unknown_official_component_keeps_its_own_numbered_block() -> None:
    text = """1 Amazon EC2
数量1
2 Amazon Managed Grafana
用于数据可视化"""

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    assert len(parsed.services) == 2
    assert parsed.services[0].source_text == "Amazon EC2\n数量1"
    assert parsed.services[1].service == "amazon_managed_grafana"
    assert parsed.services[1].calculator_service_name == "Amazon Managed Grafana"
    assert parsed.services[1].source_text == "Amazon Managed Grafana\n用于数据可视化"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("1、Amazon EC2", "Amazon EC2"),
        ("1，Amazon EC2", "Amazon EC2"),
        ("1, Amazon EC2", "Amazon EC2"),
        ("1。Amazon EC2", "Amazon EC2"),
        ("1；Amazon EC2", "Amazon EC2"),
        ("1：Amazon EC2", "Amazon EC2"),
        ("1 Amazon EC2", "Amazon EC2"),
        ("（1）Amazon EC2", "Amazon EC2"),
        ("(1) Amazon EC2", "Amazon EC2"),
    ],
)
def test_sales_number_prefix_accepts_common_punctuation(line: str, expected: str) -> None:
    match = DeepSeekIntentParser._numbered_requirement_match(line)

    assert match is not None
    assert match.group(2).strip() == expected


def test_numbered_inventory_can_skip_workload_wide_classification() -> None:
    text = """区域：新加坡
1，应用服务器：4台，8核16G，Linux
2 数据库：MySQL，500GB，主备高可用
（3）缓存：Redis，16GB，主从
4、对象存储：20TB"""

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    assert [item.service for item in parsed.services] == [
        "ec2",
        "rds",
        "elasticache",
        "s3",
    ]
    assert parsed.services[1].source_text.startswith("数据库：MySQL")


def test_identical_numbered_vm_rows_are_never_deduplicated() -> None:
    text = """新加坡地区
1、4 vCPU｜16 GiB｜c7n.xla...｜Debian 12.0.0 64bit
2、4 vCPU｜16 GiB｜c7n.xla...｜Ubuntu 24.04 server 64bit
3、4 vCPU｜16 GiB｜c7n.xla...｜Ubuntu 24.04 server 64bit
4、2 vCPU｜8 GiB｜c7n.larg...｜Debian 12.0.0 64bit
5、8 vCPU｜32 GiB｜c7n.2xl...｜Debian 12.0.0 64bit
6、8 vCPU｜32 GiB｜c7n.2xl...｜Debian 12.0.0 64bit
7、8 vCPU｜32 GiB｜c7n.2xl...｜Debian 12.0.0 64bit
8、4 vCPU｜8 GiB｜c7n.xlar...｜Debian 12.0.0 64bit
9、4 vCPU｜8 GiB｜c7n.xlar...｜Debian 12.0.0 64bit"""

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    assert len(parsed.services) == 9
    assert all(item.service == "ec2" for item in parsed.services)
    assert len({item.component_key for item in parsed.services}) == 9

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_regions(text, parsed)
    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, text)
    DeepSeekIntentParser._merge_duplicate_service_fragments(parsed)

    assert len(parsed.services) == 9
    assert parsed.ambiguities == []
    assert all(item.region == "ap-southeast-1" for item in parsed.services)


def test_plain_vm_shape_does_not_enter_third_party_architecture_flow() -> None:
    source = "8 vCPU｜32 GiB｜c7n.2xl...｜Debian 12.0.0 64bit"
    parsed = ParsedIntent(
        customer_summary="服务器",
        services=[
            ServiceRequirement(
                service="8_vcpu_32_gib_c7n_2xl",
                calculator_service_name="8 vCPU | 32 GiB | c7n.2xl",
                source_text=source,
                requirements={"vcpu": 8, "memory_gib": 32},
            )
        ],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed, source)

    assert parsed.services[0].service == "ec2"
    assert parsed.services[0].calculator_service_name == "Amazon EC2 云服务器"
    assert parsed.ambiguities == []


def test_legacy_deduplicated_vm_draft_restores_every_numbered_owner() -> None:
    text = """新加坡地区
1、4 vCPU｜16 GiB｜c7n.xla...｜Debian 12.0.0 64bit
2、4 vCPU｜16 GiB｜c7n.xla...｜Ubuntu 24.04 server 64bit
3、4 vCPU｜16 GiB｜c7n.xla...｜Ubuntu 24.04 server 64bit
4、2 vCPU｜8 GiB｜c7n.larg...｜Debian 12.0.0 64bit
5、8 vCPU｜32 GiB｜c7n.2xl...｜Debian 12.0.0 64bit
6、8 vCPU｜32 GiB｜c7n.2xl...｜Debian 12.0.0 64bit
7、8 vCPU｜32 GiB｜c7n.2xl...｜Debian 12.0.0 64bit
8、4 vCPU｜8 GiB｜c7n.xlar...｜Debian 12.0.0 64bit
9、4 vCPU｜8 GiB｜c7n.xlar...｜Debian 12.0.0 64bit"""
    unique_sources = list(dict.fromkeys(DeepSeekIntentParser._numbered_requirement_blocks(text)))
    legacy = ParsedIntent(
        customer_summary="旧草稿",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2 云服务器",
                source_text=source,
            )
            for source in unique_sources
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, legacy)
    preserve_customer_configuration(legacy)

    assert len(legacy.services) == 9
    assert [item.source_text for item in legacy.services] == (
        DeepSeekIntentParser._numbered_requirement_blocks(text)
    )
    assert len({item.component_key for item in legacy.services}) == 9


def test_compact_chinese_vm_wording_preserves_count_cpu_and_memory() -> None:
    text = "1、两台4核16的机器"

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    assert len(parsed.services) == 1
    component = parsed.services[0]
    assert component.service == "ec2"

    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert component.quantity == 2
    assert component.requirements["vcpu"] == 4
    assert component.requirements["memory_gib"] == 16
    assert component.field_sources["quantity"] == "customer_text"
    assert component.field_sources["requirements.vcpu"] == "customer_text"
    assert component.field_sources["requirements.memory_gib"] == "customer_text"


def test_colloquial_liang_vm_wording_preserves_count_when_ai_is_unavailable() -> None:
    text = "1、俩台ec2"

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    assert parsed.services[0].service == "ec2"
    assert parsed.services[0].quantity == 2
    assert parsed.services[0].field_sources["quantity"] == "customer_text"


def test_customer_replacement_model_prevents_old_shape_from_being_restored() -> None:
    parsed = ParsedIntent(
        customer_summary="Aurora PostgreSQL",
        services=[
            ServiceRequirement(
                service="rds",
                source_text="单节点8核32GB，客户已从官方相邻规格中另选型号",
                requirements={"requested_model": "db.r6g.xlarge"},
                field_sources={
                    "requirements.requested_model": "customer_confirmation",
                    "_customer_shape_replaced_by_model": "customer_confirmation",
                },
            )
        ],
    )

    DeepSeekIntentParser.reconcile_customer_pricing_facts(parsed)

    assert parsed.services[0].requirements == {
        "requested_model": "db.r6g.xlarge"
    }


def test_text_glued_before_first_number_cannot_hide_first_component() -> None:
    text = """nacos1、Amazon EC2 云服务器
m6i.xlarge（4C16G）×2，Linux，系统盘 gp3 200GB
2、Amazon RDS 数据库
MySQL，db.m6g.large（2C8G），Multi-AZ，gp3 100GB
3、nacos"""

    blocks = DeepSeekIntentParser._numbered_requirement_blocks(text)
    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert blocks == [
        "Amazon EC2 云服务器\nm6i.xlarge（4C16G）×2，Linux，系统盘 gp3 200GB",
        "Amazon RDS 数据库\nMySQL，db.m6g.large（2C8G），Multi-AZ，gp3 100GB",
        "nacos",
    ]
    assert parsed is not None
    assert len(parsed.services) == 3
    assert parsed.services[0].service == "ec2"
    assert "m6i.xlarge" in parsed.services[0].source_text
    DeepSeekIntentParser._overlay_literal_component_facts(
        parsed.services[0].source_text,
        parsed.services[0],
    )
    assert parsed.services[0].quantity == 2
    assert parsed.services[0].requirements["requested_model"] == "m6i.xlarge"
    assert parsed.services[0].requirements["vcpu"] == 4
    assert parsed.services[0].requirements["memory_gib"] == 16
    assert parsed.services[0].requirements["operating_system"] == "linux"
    assert parsed.services[0].requirements["system_disk_gib"] == 200
    assert parsed.services[0].requirements["volume_type"] == "gp3"
    assert parsed.services[1].service == "rds"
    assert parsed.services[2].source_text == "nacos"


def test_managed_server_wording_does_not_create_an_extra_ec2_component() -> None:
    text = """1、Redis服务器：16GB，主从
2、数据库服务器：MySQL，500GB，主备高可用"""

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    assert [item.service for item in parsed.services] == ["elasticache", "rds"]


def test_non_sequential_punctuated_field_does_not_create_component() -> None:
    text = """1、Amazon MSK
3，Broker节点
每节点4核16G
2、Amazon S3
存储20TB"""

    blocks = DeepSeekIntentParser._numbered_requirement_blocks(text)

    assert blocks == [
        "Amazon MSK\n3，Broker节点\n每节点4核16G",
        "Amazon S3\n存储20TB",
    ]


def test_opensearch_total_storage_shorthand_overrides_stale_per_node_value() -> None:
    parsed = ParsedIntent(
        customer_summary="OpenSearch 修改",
        services=[
            ServiceRequirement(
                service="opensearch",
                calculator_service_name="Amazon OpenSearch Service",
                source_text=(
                    "Amazon OpenSearch Service\n3节点\n每节点4核16GB\n"
                    "总存储1TB\n客户最新修改：总容量为1TB"
                ),
                requirements={
                    "data_nodes": 3,
                    # Simulate the stale, incorrect value returned before the
                    # customer correction is reconciled.
                    "storage_gib_per_node": 1024,
                },
            )
        ],
    )

    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    requirements = parsed.services[0].requirements
    assert requirements["data_nodes"] == 3
    assert requirements["total_storage_gib"] == 1024
    assert requirements["storage_gib_per_node"] == pytest.approx(1024 / 3)
    assert not parsed.ambiguities


@pytest.mark.asyncio
async def test_unknown_generated_name_is_classified_before_template_extraction() -> None:
    class UnknownNameGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_json(self, **_: object) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                return {"service": "s3", "confidence": "high"}
            return {
                "component": {
                    "service": "s3",
                    "calculator_service_name": "Amazon S3",
                    "region": None,
                    "quantity": 1,
                    "requirements": {"storage_gib": 51200},
                    "field_evidence": {"requirements.storage_gib": "对象文件预计50TB"},
                    "source_text": "对象文件预计50TB",
                    "query_action": None,
                }
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = UnknownNameGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    intent = ParsedIntent(
        customer_summary="对象存储",
        services=[
            ServiceRequirement(
                service="object_file_service_generated_name",
                calculator_service_name="对象文件服务",
                source_text="对象文件预计50TB",
            )
        ],
    )

    cleaned = await parser._cleanup_components("对象文件预计50TB", intent)

    assert gateway.calls == 2
    assert cleaned.services[0].service == "s3"
    assert cleaned.services[0].calculator_service_name == "Amazon Simple Storage Service (S3)"
    assert cleaned.services[0].requirements["storage_gib"] == 51200


def test_memorydb_shards_and_replicas_are_preserved_as_billable_node_count() -> None:
    source = (
        "Amazon MemoryDB for Redis：数量1，2个Shard，"
        "每个Shard配置1个主节点+1个副本，单节点内存约13GB，Redis 7.x"
    )
    parsed = ParsedIntent(
        customer_summary="memorydb",
        services=[
            ServiceRequirement(
                service="memorydb",
                calculator_service_name="Amazon MemoryDB",
                source_text=source,
                requirements={"engine": "redis", "memory_gib": 13},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_service_architecture(source, parsed)

    assert parsed.services[0].requirements["shards"] == 2
    assert parsed.services[0].requirements["replicas_per_shard"] == 1
    assert parsed.services[0].requirements["node_count"] == 4


def test_memorydb_non_pricing_engine_version_does_not_trigger_template_retry() -> None:
    source = (
        "Amazon MemoryDB for Redis：数量1，2个Shard，每个Shard配置1个主节点+1个副本，"
        "单节点内存约13GB，Redis 7.x"
    )
    original = ServiceRequirement(
        service="memorydb",
        calculator_service_name="Amazon MemoryDB",
        source_text=source,
        requirements={
            "engine": "redis",
            "memory_gib": 13,
            "shards": 2,
            "replicas_per_shard": 1,
            "node_count": 4,
        },
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    cleaned = parser._component_from_template_output(
        {
            "component": {
                "requirements": {
                    **original.requirements,
                    "engine_version": "7.x",
                }
            }
        },
        original,
    )

    assert cleaned.requirements == original.requirements
    assert "engine_version" not in cleaned.requirements


def test_mq_broker_count_never_uses_per_node_cpu_as_node_quantity() -> None:
    source = "Amazon MQ for RabbitMQ：数量1，3节点Broker集群，单节点4核16GB"
    parsed = ParsedIntent(
        customer_summary="mq",
        services=[
            ServiceRequirement(
                service="mq",
                calculator_service_name="Amazon MQ for RabbitMQ",
                source_text=source,
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    assert parsed.services[0].requirements["broker_count"] == 3
    assert parsed.services[0].requirements["vcpu"] == 4
    assert parsed.services[0].requirements["memory_gib"] == 16
