from __future__ import annotations

from typing import Any

from app.domain.models import ServiceRequirement


# Runtime extraction contracts.  These are deliberately separate from the
# editable explanatory prompt library: the model sees a complete, stable set
# of fields and can only fill values that are explicitly present in the
# customer's component text.  Unknown optional values remain ``null``.
SERVICE_TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    "ec2": (
        "requested_model", "vcpu", "memory_gib", "operating_system",
        "architecture", "tenancy", "business_type", "system_disk_gib",
        "total_system_disk_gib",
        "volume_type", "ebs_iops", "ebs_throughput_mbps",
        "additional_ebs_volumes", "purchase_option", "reserved_term_years",
        "payment_option", "utilization_percent", "detailed_monitoring",
        "snapshot_frequency", "snapshot_changed_gib", "snapshot_retention_days",
        "data_transfer_in_gib", "data_transfer_regional_gib",
        "data_transfer_out_gib", "data_transfer_in_gib_per_instance",
        "data_transfer_regional_gib_per_instance",
        "data_transfer_out_gib_per_instance", "purpose",
    ),
    "eks": (
        "cluster_count", "kubernetes_version", "support_tier",
        "control_plane_hours", "worker_management",
        "worker_nodes_per_cluster", "worker_node_count",
        "worker_requested_model", "worker_vcpu", "worker_memory_gib",
        "worker_system_disk_gib", "total_worker_system_disk_gib",
    ),
    "ecr": ("repositories", "storage_gib", "image_scans", "data_transfer_out_gib"),
    "rds": (
        "requested_model", "engine", "engine_version", "vcpu", "memory_gib",
        "deployment", "storage_gib", "storage_type", "storage_iops",
        "storage_throughput_mbps", "purchase_option", "reserved_term_years",
        "payment_option", "utilization_percent", "license_model",
        "backup_retention_days", "read_replica_count", "aurora_cluster",
        "cluster_members",
    ),
    "elasticache": (
        "requested_model", "engine", "engine_version", "memory_gib", "shards",
        "replicas_per_shard", "node_count", "cluster_mode", "data_tiering",
        "backup_retention_days", "purchase_option", "reserved_term_years",
        "payment_option", "utilization_percent",
    ),
    "elb": (
        "load_balancer_type", "scheme", "processed_bytes_gib",
        "processed_bytes_ec2_ip_gib_per_hour", "new_connections_per_second",
        "average_connection_duration_seconds", "active_connections_per_minute",
        "requests_per_second", "rule_evaluations_per_request",
        "rule_evaluations_per_second", "lcu_count", "listeners",
    ),
    "s3": (
        "storage_gib", "storage_class", "put_copy_post_list_requests",
        "get_select_requests", "data_retrieval_gib", "data_transfer_out_gib",
    ),
    "cloudfront": ("data_transfer_out_gib", "https_requests", "price_class"),
    "route53": ("hosted_zones", "dns_queries", "health_checks"),
    "waf": ("web_acls", "rules", "requests", "protected_resource"),
    "sqs": ("requests", "queue_type", "payload_size_kib"),
    "ses": ("outbound_messages", "inbound_messages", "attachments_gib"),
    "cloudwatch": (
        "log_ingestion_gib", "log_storage_gib", "custom_metrics", "alarms",
        "include_logs", "include_metrics",
    ),
    "amp": (
        "active_series", "samples_ingested", "query_samples_processed",
        "collector_hours", "storage_gib",
    ),
    "backup": (
        "backup_storage_gib", "warm_storage_gib", "cold_storage_gib",
        "restore_gib", "backup_frequency", "backup_retention_days", "protected_service",
    ),
    "ebs": (
        "storage_gib", "total_storage_gib", "volume_type", "iops",
        "throughput_mbps",
    ),
    "data_transfer": ("data_transfer_out_gib", "source_regions", "destination"),
    "global_accelerator": (
        "accelerators", "data_transfer_out_gib", "source_regions",
        "destination_geography",
    ),
    "msk": (
        "requested_model", "broker_count", "cluster_type",
        "storage_gib_per_broker", "storage_type", "broker_hours", "vcpu",
        "memory_gib", "total_storage_gib", "data_transfer_in_gib",
        "data_transfer_out_gib",
    ),
    "apigateway": ("api_type", "requests", "request_size_mb", "data_transfer_out_gib"),
    "scheduler": ("scheduled_invocations", "schedules"),
    "opensearch": (
        "requested_model", "data_nodes", "vcpu", "memory_gib",
        "storage_gib_per_node", "volume_type", "master_nodes",
        "dedicated_master", "multi_az", "warm_node_count", "engine_version",
        "total_storage_gib", "data_transfer_out_gib",
    ),
    "documentdb": (
        "requested_model", "instance_count", "vcpu", "memory_gib",
        "storage_gib", "io_requests", "backup_storage_gib", "engine_version",
    ),
    "nat_gateway": ("gateway_count", "hours_per_month", "data_processed_gib"),
    "secrets_manager": ("secret_count", "api_calls", "rotation_enabled"),
    "vpc": ("vpc_count", "public_subnets", "private_subnets", "availability_zones"),
    "dms": (
        "requested_model", "replication_instances", "hours_per_month", "multi_az",
        "storage_gib", "data_processed_gib",
    ),
    "kms": ("key_count", "requests", "key_type"),
    "xray": ("traces_recorded", "traces_retrieved", "traces_stored"),
    "lambda": (
        "architecture", "memory_mb", "ephemeral_storage_mb", "requests",
        "duration_ms", "provisioned_concurrency",
    ),
    "ecs": (
        "cluster_count", "launch_type", "tasks", "task_vcpu",
        "task_memory_gib", "task_hours",
    ),
    "fargate": (
        "tasks", "task_vcpu", "task_memory_gib", "task_hours",
        "operating_system", "architecture", "ephemeral_storage_gib",
    ),
    "dynamodb": (
        "capacity_mode", "read_request_units", "write_request_units", "storage_gib",
        "streams_read_requests", "backup_storage_gib", "restore_gib",
    ),
    "efs": (
        "storage_gib", "storage_class", "throughput_mode",
        "provisioned_throughput_mibps", "lifecycle_policy",
    ),
    "fsx": (
        "file_system_type", "storage_gib", "throughput_mbps", "iops",
        "backup_storage_gib",
    ),
    "sns": ("requests", "deliveries", "delivery_type", "data_transfer_out_gib"),
    "kinesis": (
        "capacity_mode", "shards", "shard_hours", "put_payload_units",
        "data_in_gib", "data_out_gib", "extended_retention_hours",
    ),
    "emr": (
        "deployment_type", "applications", "cluster_count",
        "master_nodes", "master_requested_model", "master_vcpu",
        "master_memory_gib", "master_storage_gib_per_node",
        "core_nodes", "core_requested_model", "core_vcpu",
        "core_memory_gib", "core_storage_gib_per_node",
        "task_nodes", "task_requested_model", "task_vcpu",
        "task_memory_gib", "task_storage_gib_per_node",
        "requested_model", "hours_per_month",
    ),
    "redshift": (
        "deployment_type", "requested_model", "nodes", "vcpu", "memory_gib",
        "storage_gib", "managed_storage_gib", "rpu", "hours_per_month",
        "snapshot_storage_gib",
    ),
    "athena": ("data_scanned_gib", "queries", "provisioned_dpu_hours"),
    "glue": (
        "job_type", "job_count", "dpu_hours", "crawler_dpu_hours",
        "data_catalog_objects", "interactive_session_dpu_hours",
    ),
    "sagemaker": (
        "requested_model", "instance_count", "instance_hours", "endpoint_type",
        "storage_gib",
    ),
    "cognito": (
        "user_count", "monthly_active_users", "machine_to_machine_tokens",
        "advanced_security",
    ),
    "mq": (
        "engine_type", "requested_model", "broker_count", "deployment_mode",
        "vcpu", "memory_gib", "storage_gib", "storage_gib_per_broker",
        "total_storage_gib", "hours_per_month",
    ),
    "step_functions": (
        "workflow_type", "state_transitions", "requests", "duration_gb_seconds",
    ),
    "bedrock": (
        "requested_model", "input_tokens", "output_tokens", "images",
        "provisioned_throughput_units",
    ),
    "cloud_map": ("namespaces", "service_instances", "api_calls", "dns_queries"),
    "appconfig": (
        "configuration_requests", "configuration_retrievals",
        "targets_receiving_configuration",
    ),
    "eventbridge": ("events", "event_buses", "schema_discovery_events", "pipes_requests"),
}


SERVICE_ALIASES = {
    "redis": "elasticache",
    "valkey": "elasticache",
    "elbv2": "elb",
    "elasticloadbalancingv2": "elb",
    "wafv2": "waf",
    "awswafv2": "waf",
    "aurora": "rds",
}


GENERIC_TEMPLATE_FIELDS = (
    "requested_model", "vcpu", "memory_gib", "storage_gib", "quantity_detail",
    "hours_per_month", "requests", "data_transfer_out_gib", "purpose",
)

COMMON_TEMPLATE_FIELDS = ("system_default_assumption",)


def normalized_service_key(service: str) -> str:
    key = service.strip().casefold()
    return SERVICE_ALIASES.get(key, key)


def requirement_fields(service: str) -> tuple[str, ...]:
    fields = SERVICE_TEMPLATE_FIELDS.get(
        normalized_service_key(service), GENERIC_TEMPLATE_FIELDS
    )
    return tuple(dict.fromkeys((*fields, *COMMON_TEMPLATE_FIELDS)))


def component_template(
    component: ServiceRequirement,
    *,
    extra_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the complete blank template shown to the extraction model."""

    fields = tuple(
        dict.fromkeys((*requirement_fields(component.service), *extra_fields))
    )
    return {
        "service": component.service,
        "calculator_service_name": component.calculator_service_name,
        "region": None,
        "quantity": None,
        "hours_per_month": None,
        "requirements": {field: None for field in fields},
        "field_evidence": {
            "region": None,
            "quantity": None,
            "hours_per_month": None,
            **{f"requirements.{field}": None for field in fields},
        },
        "source_text": component.source_text,
        "query_action": None,
    }


def allowed_requirement_fields(
    service: str,
    *,
    extra_fields: tuple[str, ...] = (),
) -> set[str]:
    return {*requirement_fields(service), *extra_fields}


def compact_template_values(value: object) -> object:
    """Remove empty template placeholders before Pydantic/adapters see them."""

    if isinstance(value, dict):
        return {
            str(key): compact_template_values(item)
            for key, item in value.items()
            if item is not None and item != ""
        }
    if isinstance(value, list):
        return [compact_template_values(item) for item in value if item is not None]
    return value
