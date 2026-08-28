from app.domain.models import ParsedIntent, ServiceKind, ServiceRequirement
from app.domain.requirement_fields import canonicalize_requirement_fields
from app.services.quote_service import QuoteService


def test_common_customer_value_variants_are_canonicalized() -> None:
    normalized = canonicalize_requirement_fields(
        {
            "instance_type": " M7I.XLARGE ",
            "os": "Ubuntu Server 24.04",
            "purchase_type": "按需付费",
            "root_disk_gib": "100 GiB",
            "disk_type": "General Purpose SSD (GP3)",
            "data_transfer_out_gib": "1 TB",
        },
        service="ec2",
    )

    assert normalized["requested_model"] == "m7i.xlarge"
    assert normalized["operating_system"] == "linux"
    assert normalized["purchase_option"] == "on_demand"
    assert normalized["system_disk_gib"] == 100
    assert normalized["volume_type"] == "gp3"
    assert normalized["data_transfer_out_gib"] == 1024


def test_rds_aliases_units_and_deployment_are_canonicalized() -> None:
    normalized = canonicalize_requirement_fields(
        {
            "database_engine": "Postgres",
            "deployment_option": "Multi-AZ",
            "storage_size_gib": "1 TiB",
            "disk_type": "通用型 SSD gp3",
            "iops": "3,000 IOPS",
            "throughput_mbps": "125 MiB/s",
        },
        service="rds",
    )

    assert normalized["engine"] == "postgresql"
    assert normalized["deployment"] == "multi_az"
    assert normalized["storage_gib"] == 1024
    assert normalized["storage_type"] == "gp3"
    assert normalized["storage_iops"] == 3000
    assert normalized["storage_throughput_mbps"] == 125


def test_ebs_additional_volumes_keep_size_type_and_count() -> None:
    normalized = canonicalize_requirement_fields(
        {
            "additional_ebs_volumes": [
                {
                    "size_gib": "1 TB",
                    "volume_type": "General Purpose SSD GP3",
                    "count_per_instance": "2",
                }
            ]
        },
        service="ec2",
    )

    assert normalized["additional_ebs_volumes"] == [
        {"size_gib": 1024.0, "volume_type": "gp3", "count_per_instance": 2}
    ]


def test_official_aws_service_names_map_to_basic_adapters() -> None:
    assert QuoteService._service_kind("Amazon EC2") == ServiceKind.EC2
    assert QuoteService._service_kind("Amazon RDS for PostgreSQL") == ServiceKind.RDS
    assert QuoteService._service_kind("Amazon ElastiCache for Redis") == ServiceKind.REDIS
    assert QuoteService._service_kind("Amazon Simple Storage Service (S3)") == ServiceKind.S3
    assert QuoteService._service_kind("Amazon CloudFront CDN") == ServiceKind.CLOUDFRONT


def test_numeric_values_with_units_survive_placeholder_cleanup() -> None:
    intent = ParsedIntent(
        customer_summary="compatibility",
        services=[
            ServiceRequirement(
                service="ec2",
                requirements={
                    "cpu": "4 vCPU",
                    "memory": "16 GiB",
                    "root_disk_gib": "100 GB",
                },
            ),
            ServiceRequirement(
                service="s3",
                requirements={"storage_size_gib": "2 TB"},
            ),
        ],
    )

    QuoteService._strip_non_numeric_placeholders(intent)

    assert intent.services[0].requirements["vcpu"] == 4
    assert intent.services[0].requirements["memory_gib"] == 16
    assert intent.services[0].requirements["system_disk_gib"] == 100
    assert intent.services[1].requirements["storage_gib"] == 2048


def test_prose_cannot_masquerade_as_model_or_numeric_field() -> None:
    normalized = canonicalize_requirement_fields(
        {
            "requested_model": "4核16G",
            "vcpu": 4,
            "memory_gib": 16,
            "broker_count": "客户询问了节点数量，请向客户确认",
        },
        service="ec2",
    )

    assert "requested_model" not in normalized
    assert "broker_count" not in normalized
    assert normalized["vcpu"] == 4
    assert normalized["memory_gib"] == 16


def test_catalog_model_survives_service_prefix_difference_for_adapter_validation() -> None:
    normalized = canonicalize_requirement_fields(
        {
            "requested_model": "r5.xlarge",
            "storage_gib": 200,
        },
        service="dms",
    )

    assert normalized["requested_model"] == "r5.xlarge"
    assert normalized["storage_gib"] == 200


def test_valid_models_and_numeric_strings_still_survive_contract_validation() -> None:
    ec2 = canonicalize_requirement_fields(
        {"requested_model": "M6G.2XLARGE", "vcpu": "8 vCPU", "memory_gib": "32 GiB"},
        service="ec2",
    )
    msk = canonicalize_requirement_fields(
        {"requested_model": "kafka.m7g.large", "broker_count": "3"},
        service="msk",
    )

    assert ec2 == {"requested_model": "m6g.2xlarge", "vcpu": 8, "memory_gib": 32}
    assert msk == {"requested_model": "m7g.large", "broker_count": 3}


def test_dynamic_official_numeric_fields_are_normalized_before_pricing() -> None:
    normalized = canonicalize_requirement_fields(
        {
            "write_records": "2亿条",
            "memory_retention_hours": "24小时",
            "magnetic_retention_days": "180天",
            "endpoint_count": "2个",
            "task_count": "3个",
            "kpu_count": "4KPU",
        },
        service="future_official_service",
    )

    assert normalized == {
        "write_records": 200_000_000,
        "memory_retention_hours": 24,
        "magnetic_retention_days": 180,
        "endpoint_count": 2,
        "task_count": 3,
        "kpu_count": 4,
    }
