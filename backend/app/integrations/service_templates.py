from __future__ import annotations

from typing import Any

from app.domain.models import ServiceKind, ServiceRequirement
from app.integrations.aws_component_templates.registry import (
    component_template_aliases,
    component_template_field_sets,
    component_template_spec,
    primary_component_templates,
)

# Runtime extraction contracts.  These are deliberately separate from the
# editable explanatory prompt library: the model sees a complete, stable set
# of fields and can only fill values that are explicitly present in the
# customer's component text.  Unknown optional values remain ``null``.
SERVICE_TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    "eks": (
        "cluster_count",
        "kubernetes_version",
        "support_tier",
        "control_plane_hours",
        "worker_management",
        "worker_nodes_per_cluster",
        "worker_node_count",
        "worker_requested_model",
        "worker_vcpu",
        "worker_memory_gib",
        "worker_system_disk_gib",
        "total_worker_system_disk_gib",
    ),
    "ecr": (
        "repositories",
        "storage_gib",
        "image_scans",
        "data_transfer_out_gib",
        "transfer_scope",
    ),
    "memorydb": (
        "requested_model",
        "engine",
        "vcpu",
        "memory_gib",
        "node_count",
        "shards",
        "replicas_per_shard",
        "snapshot_retention_days",
        "data_transfer_in_gib",
        "data_transfer_out_gib",
    ),
    "s3_glacier_deep_archive": (
        "storage_gib",
        "data_retrieval_gib",
        "retrieval_tier",
    ),
    "storage_gateway": (
        "gateway_type",
        "cache_storage_gib",
        "data_processed_gib",
    ),
    "data_sync": ("task_mode", "data_processed_gib"),
    "transfer": (
        "protocol",
        "storage_backend",
        "transfer_direction",
        "endpoint_count",
        "data_processed_gib",
    ),
    "app_stream": (
        "requested_model",
        "user_count",
        "hours_per_user_per_day",
        "hours_per_user_per_month",
    ),
    "work_mail": ("user_count",),
    "cloudfront": (
        "data_transfer_out_gib",
        "https_requests",
        "traffic_geography",
        "price_class",
    ),
    "route53": (
        "route53_type",
        "hosted_zones",
        "dns_queries",
        "health_checks",
        "resolver_endpoints",
        "resolver_ip_addresses_per_endpoint",
    ),
    "waf": ("web_acls", "rules", "requests", "protected_resource"),
    "sqs": ("requests", "queue_type", "payload_size_kib"),
    "ses": ("outbound_messages", "inbound_messages", "attachments_gib"),
    "pinpoint": ("outbound_messages",),
    "cloudwatch": (
        "log_ingestion_gib",
        "log_delivery_to_s3_gib",
        "log_destination",
        "log_storage_gib",
        "custom_metrics",
        "alarms",
        "log_retention_days",
        "include_logs",
        "include_metrics",
    ),
    "amp": (
        "active_series",
        "samples_ingested",
        "query_samples_processed",
        "collector_hours",
        "storage_gib",
    ),
    "backup": (
        "backup_storage_gib",
        "warm_storage_gib",
        "cold_storage_gib",
        "restore_gib",
        "cross_region_copy_gib",
        "backup_frequency",
        "backup_retention_days",
        "protected_service",
    ),
    "ebs": (
        # AmazonEC2 is one Price List offer, but EBS volumes and EBS
        # snapshots are different customer products with different billing
        # identities.  Keep the variant and both closed field sets in the
        # extraction contract so the component AI can select one schema once;
        # pricing code must never reopen the customer's prose to decide which
        # product was requested.
        "product_variant",
        "storage_gib",
        "total_storage_gib",
        "volume_type",
        "iops",
        "throughput_mbps",
        "backup_storage_gib",
        "snapshot_changed_gib",
        "snapshot_frequency",
        "snapshot_retention_days",
    ),
    "data_transfer": ("data_transfer_out_gib", "source_regions", "destination"),
    "global_accelerator": (
        "accelerators",
        "listener_count",
        "endpoint_count",
        "data_transfer_out_gib",
        "source_regions",
        "destination_geography",
    ),
    "transit_gateway": (
        "attachments",
        "attachment_type",
        "data_processed_gib",
    ),
    "direct_connect": (
        "connection_count",
        "port_speed_gbps",
        "data_transfer_out_gib",
    ),
    "site_to_site_vpn": (
        "connection_count",
        "vpn_tier",
        "data_processed_gib",
    ),
    "vpc_endpoint": (
        "endpoint_count",
        "endpoint_type",
        "data_processed_gib",
    ),
    "msk": (
        "requested_model",
        "broker_count",
        "cluster_type",
        "storage_gib_per_broker",
        "storage_type",
        "broker_hours",
        "vcpu",
        "memory_gib",
        "total_storage_gib",
        "storage_gib",
        "partition_count",
        "data_in_gib",
        "data_out_gib",
        "data_transfer_in_gib",
        "data_transfer_out_gib",
    ),
    "apigateway": (
        "api_type",
        "requests",
        "messages",
        "connection_minutes",
        "request_size_mb",
        "data_transfer_out_gib",
    ),
    "scheduler": ("scheduled_invocations", "schedules"),
    "opensearch": (
        "requested_model",
        "data_nodes",
        "vcpu",
        "memory_gib",
        "storage_gib_per_node",
        "volume_type",
        "master_nodes",
        "dedicated_master",
        "multi_az",
        "warm_node_count",
        "total_storage_gib",
        "data_transfer_out_gib",
    ),
    "documentdb": (
        "requested_model",
        "instance_count",
        "vcpu",
        "memory_gib",
        "storage_gib",
        "io_requests",
        "backup_storage_gib",
    ),
    "nat_gateway": ("gateway_count", "hours_per_month", "data_processed_gib"),
    "secrets_manager": ("secret_count", "api_calls", "rotation_enabled"),
    "vpc": ("vpc_count", "public_subnets", "private_subnets", "availability_zones"),
    "dms": (
        "requested_model",
        "vcpu",
        "memory_gib",
        "replication_instances",
        "task_count",
        "hours_per_month",
        "multi_az",
        "storage_gib",
        "data_processed_gib",
    ),
    "kms": ("key_count", "requests", "key_type"),
    "xray": ("traces_recorded", "traces_retrieved", "traces_stored"),
    "lambda": (
        "architecture",
        "memory_mb",
        "ephemeral_storage_mb",
        "requests",
        "duration_ms",
        "provisioned_concurrency",
    ),
    "ecs": (
        "cluster_count",
        "launch_type",
        "tasks",
        "task_vcpu",
        "task_memory_gib",
        "task_hours",
        "operating_system",
        "architecture",
        "ephemeral_storage_gib",
    ),
    "fargate": (
        "tasks",
        "task_vcpu",
        "task_memory_gib",
        "task_hours",
        "operating_system",
        "architecture",
        "ephemeral_storage_gib",
    ),
    "dynamodb": (
        "capacity_mode",
        "read_request_units",
        "write_request_units",
        "storage_gib",
        "streams_read_requests",
        "backup_storage_gib",
        "restore_gib",
    ),
    "efs": (
        "storage_gib",
        "storage_class",
        "deployment_type",
        "throughput_mode",
        "provisioned_throughput_mibps",
        "data_in_gib",
        "data_out_gib",
        "lifecycle_policy",
    ),
    "fsx": (
        "file_system_type",
        "deployment_type",
        "storage_type",
        "storage_gib",
        "throughput_mbps",
        "throughput_mbps_per_tib",
        "iops",
        "backup_storage_gib",
    ),
    "sns": (
        "topic_type",
        "requests",
        "deliveries",
        "delivery_type",
        "data_transfer_out_gib",
    ),
    "kinesis": (
        "capacity_mode",
        "shards",
        "shard_hours",
        "put_payload_units",
        "data_in_gib",
        "data_out_gib",
        "extended_retention_hours",
    ),
    "kinesis_firehose": (
        "data_in_gib",
        "data_out_gib",
        "records",
        "format_conversion_gib",
        "vpc_delivery_hours",
    ),
    "emr": (
        "deployment_type",
        "applications",
        "cluster_count",
        "master_nodes",
        "master_requested_model",
        "master_vcpu",
        "master_memory_gib",
        "master_storage_gib_per_node",
        "core_nodes",
        "core_requested_model",
        "core_vcpu",
        "core_memory_gib",
        "core_storage_gib_per_node",
        "task_nodes",
        "task_requested_model",
        "task_vcpu",
        "task_memory_gib",
        "task_storage_gib_per_node",
        "requested_model",
        "hours_per_month",
    ),
    "redshift": (
        "deployment_type",
        "requested_model",
        "nodes",
        "vcpu",
        "memory_gib",
        "storage_gib",
        "managed_storage_gib",
        "rpu",
        "hours_per_month",
        "snapshot_storage_gib",
    ),
    "athena": ("data_scanned_gib", "queries", "provisioned_dpu_hours"),
    "glue": (
        "job_type",
        "job_count",
        "dpu_hours",
        "crawler_dpu_hours",
        "data_catalog_objects",
        "interactive_session_dpu_hours",
    ),
    "sagemaker": (
        "requested_model",
        "instance_count",
        "instance_hours",
        "endpoint_type",
        "storage_gib",
    ),
    "textract": ("document_pages", "analysis_type", "processing_mode"),
    "comprehend": ("characters", "analysis_type"),
    "rekognition": ("images", "analysis_type"),
    "transcribe": ("audio_minutes", "transcription_type", "processing_mode"),
    "translate": ("characters", "translation_type"),
    "polly": ("characters", "voice_engine"),
    "cognito": (
        "user_count",
        "monthly_active_users",
        "machine_to_machine_tokens",
        "advanced_security",
    ),
    "mq": (
        "engine_type",
        "requested_model",
        "broker_count",
        "deployment_mode",
        "vcpu",
        "memory_gib",
        "storage_gib",
        "storage_gib_per_broker",
        "total_storage_gib",
        "hours_per_month",
    ),
    "step_functions": (
        "workflow_type",
        "state_transitions",
        "requests",
        "duration_gb_seconds",
    ),
    "bedrock": (
        "requested_model",
        "input_tokens",
        "output_tokens",
        "images",
        "provisioned_throughput_units",
    ),
    "cloud_map": ("namespaces", "service_instances", "api_calls", "dns_queries"),
    "appconfig": (
        "configuration_requests",
        "configuration_retrievals",
        "targets_receiving_configuration",
        "experiment_hours",
    ),
    "eventbridge": ("events", "event_buses", "schema_discovery_events", "pipes_requests"),
    "config": ("configuration_items_recorded", "rule_evaluations"),
    "code_build": (
        "build_minutes",
        "compute_type",
        "operating_system",
        "architecture",
    ),
    "code_pipeline": (
        "pipeline_type",
        "action_execution_minutes",
        "active_pipelines",
    ),
    "code_artifact": (
        "storage_gib",
        "requests",
        "data_transfer_out_gib",
        "transfer_scope",
    ),
    "code_deploy": ("deployment_updates", "deployment_target"),
    "cloud_formation": (
        "resource_handler_operations",
        "resource_handler_duration_seconds",
        "hook_invocations",
        "hook_duration_seconds",
    ),
    "inspector_v2": ("ec2_instances", "ecr_images", "lambda_functions"),
    "macie": ("bucket_count", "data_scanned_gib"),
    "security_hub": ("security_checks", "resource_count"),
    "auditmanager": ("resource_assessments", "evidence_items"),
    "io_t": (
        "device_count",
        "connection_minutes",
        "messages",
        "message_size_kib",
    ),
    "io_t_device_management": ("things_registered", "remote_actions"),
    "io_t_device_defender": ("device_count", "metric_datapoints"),
    "kinesis_video": ("data_in_gib", "data_out_gib", "storage_gib"),
    "ivs": (
        "input_channel_hours",
        "viewer_hours",
        "channel_type",
        "output_resolution",
        "viewer_geography",
    ),
    "elemental_media_convert": (
        "transcode_minutes",
        "resolution",
        "transcoding_tier",
    ),
    "elemental_media_live": (
        "channel_count",
        "channel_class",
        "channel_hours",
        "input_codec",
        "input_resolution",
        "input_bitrate_mbps",
        "output_codec",
        "output_resolution",
        "output_bitrate_mbps",
        "output_fps",
    ),
    "elemental_media_package": ("data_in_gib", "data_out_gib"),
    "media_connect": (
        "output_count",
        "output_hours",
        "output_bandwidth_mbps",
        "data_transfer_out_gib",
        "transfer_destination",
    ),
    "quicksight": (
        "edition",
        "users",
        "author_users",
        "reader_users",
        "session_capacity",
        "spice_gib",
    ),
}

# The five high-frequency products are defined in separate modules. This
# registry merge preserves the legacy public mapping without returning their
# ownership to this shared file.
SERVICE_TEMPLATE_FIELDS.update(component_template_field_sets())


SERVICE_ALIASES = {
    "wafv2": "waf",
    "awswafv2": "waf",
    "codebuild": "code_build",
    "awscodebuild": "code_build",
    "codepipeline": "code_pipeline",
    "awscodepipeline": "code_pipeline",
    "codeartifact": "code_artifact",
    "awscodeartifact": "code_artifact",
    "codedeploy": "code_deploy",
    "awscodedeploy": "code_deploy",
    "cloudformation": "cloud_formation",
    "awscloudformation": "cloud_formation",
    "amazoninspectorv2": "inspector_v2",
    "securityhub": "security_hub",
    "awssecurityhub": "security_hub",
    "awsiot": "io_t",
    "iottcore": "io_t",
    "iotcore": "io_t",
    "iotdevicemanagement": "io_t_device_management",
    "iotdevicedefender": "io_t_device_defender",
    "amazonkinesisvideo": "kinesis_video",
    "kinesisvideostreams": "kinesis_video",
    "amazonivs": "ivs",
    "awselementalmediaconvert": "elemental_media_convert",
    "mediaconvert": "elemental_media_convert",
    "awselementalmedialive": "elemental_media_live",
    "medialive": "elemental_media_live",
    "awselementalmediapackage": "elemental_media_package",
    "mediapackage": "elemental_media_package",
    "awsmediaconnect": "media_connect",
    "awselementalmediaconnect": "media_connect",
    "mediaconnect": "media_connect",
    "aurora": "rds",
    # Official product discovery and models use both spellings.  The runtime
    # contract must not fall back to the generic ``requests`` template merely
    # because an underscore was inserted into the same AWS product name.
    "dynamo_db": "dynamodb",
    "amazon_dynamodb": "dynamodb",
    "cloud_front": "cloudfront",
    "quick_sight": "quicksight",
    "events": "eventbridge",
    "aws_events": "eventbridge",
}
SERVICE_ALIASES.update(component_template_aliases())


GENERIC_TEMPLATE_FIELDS = (
    "requested_model",
    "product_variant",
    "vcpu",
    "memory_gib",
    "storage_gib",
    "storage_gib_per_node",
    "total_storage_gib",
    "backup_storage_gib",
    "snapshot_storage_gib",
    "quantity_detail",
    "cluster_count",
    "instance_count",
    "node_count",
    "broker_count",
    "listener_count",
    "endpoint_count",
    "task_count",
    "shards",
    "replica_count",
    "writer_nodes",
    "reader_nodes",
    "user_count",
    "hours_per_user_per_day",
    "hours_per_user_per_month",
    "hours_per_month",
    "system_disk_gib",
    "user_volume_gib",
    "requests",
    "messages",
    "write_records",
    "document_pages",
    "characters",
    "audio_minutes",
    "images",
    "data_in_gib",
    "data_out_gib",
    "data_processed_gib",
    "data_scanned_gib",
    "data_transfer_out_gib",
    "throughput_mbps",
    "memory_retention_hours",
    "magnetic_retention_days",
    "kpu_count",
    "kpu_hours",
)

# These services have purpose-built pricing adapters.  The distinction only
# decides which code turns normalized facts into price queries; it must never
# decide whether extraction receives the official field profile.  Every
# component, including a dedicated one, merges its fixed business template
# with the current AWS Price List profile.  Derive the set from the routing
# enum so adding an adapter cannot silently leave a second list stale.
# ``redis`` is the adapter route while the customer-facing extraction contract
# is ``elasticache``.
DEDICATED_TEMPLATE_SERVICES = frozenset(
    "elasticache" if kind.value == "redis" else kind.value for kind in ServiceKind
)


def requires_official_field_profile(service: str) -> bool:
    """Return whether extraction must merge the official Price List profile.

    Kept as a named policy boundary for callers and audits.  Official profile
    enrichment is deliberately universal; a dedicated pricing adapter is not
    permission to hide official billing fields from extraction.
    """

    return True


COMMON_TEMPLATE_FIELDS = ("system_default_assumption",)

# Customer prose is preserved verbatim in ``source_text``.  These normalized
# fields are useful deployment context, but they do not change the AWS product
# price selected by this application.  Keep them out of the pricing contract
# so an operational compatibility check can never block or mutate a quote.
# Database versions deliberately are not listed: RDS Extended Support can add
# a real charge, so RDS engine_version remains a pricing field.
NON_PRICING_CONTEXT_FIELDS: dict[str, frozenset[str]] = {
    "memorydb": frozenset({"engine_version"}),
    "opensearch": frozenset({"engine_version"}),
    "documentdb": frozenset({"engine_version"}),
    "waf": frozenset({"protected_resource"}),
    # AWS Backup's official Calculator entry is only a parent selector.  The
    # protected service chooses the exact official child form and therefore is
    # product identity, not discardable display context.
    "backup": frozenset(),
}
NON_PRICING_CONTEXT_FIELDS.update(
    {
        service: template.non_pricing_fields
        for service, template in primary_component_templates().items()
        if template.non_pricing_fields
    }
)

# Safe defaults are values that do not change which product the customer is
# buying.  Every known template participates in this registry, even when its
# default mapping is empty.  Product-family decisions (for example the RDS
# engine) deliberately do not belong here and must remain customer choices.
SERVICE_SAFE_DEFAULTS: dict[str, dict[str, Any]] = {
    service: {} for service in SERVICE_TEMPLATE_FIELDS
}
SERVICE_SAFE_DEFAULTS.update(
    {
        "msk": {"cluster_type": "provisioned", "storage_type": "ebs"},
        "opensearch": {"volume_type": "gp3"},
        "apigateway": {"api_type": "http"},
        "textract": {
            "analysis_type": "document_text",
            "processing_mode": "async",
        },
        "comprehend": {"analysis_type": "sentiment"},
        "rekognition": {"analysis_type": "image"},
        "transcribe": {
            "transcription_type": "standard",
            "processing_mode": "batch",
        },
        "translate": {"translation_type": "text"},
        "polly": {"voice_engine": "standard"},
        "transit_gateway": {"attachment_type": "vpc"},
        "direct_connect": {},
        "site_to_site_vpn": {"vpn_tier": "standard"},
        "vpc_endpoint": {"endpoint_type": "interface"},
        "s3_glacier_deep_archive": {"retrieval_tier": "standard"},
        "storage_gateway": {"gateway_type": "file_gateway"},
        "data_sync": {"task_mode": "basic"},
        "transfer": {
            "protocol": "sftp",
            "storage_backend": "s3",
            "transfer_direction": "upload",
        },
        "route53": {
            "route53_type": "hosted_zone",
            "resolver_ip_addresses_per_endpoint": 2,
        },
    }
)
SERVICE_SAFE_DEFAULTS.update(
    {
        service: dict(template.safe_defaults)
        for service, template in primary_component_templates().items()
    }
)

# Fields that represent metered quantities customers may add to a component.
# Keeping this beside the extraction contracts gives the confirmation UI one
# provider-owned source of truth instead of a universal frontend menu.
BILLING_DIMENSION_FIELDS = {
    "active_series",
    "alarms",
    "api_calls",
    "attachments",
    "attachments_gib",
    "backup_storage_gib",
    "broker_hours",
    "collector_hours",
    "configuration_requests",
    "configuration_retrievals",
    "characters",
    "cache_storage_gib",
    "configuration_items_recorded",
    "connection_count",
    "rule_evaluations",
    "control_plane_hours",
    "custom_metrics",
    "data_in_gib",
    "data_out_gib",
    "data_processed_gib",
    "data_retrieval_gib",
    "data_scanned_gib",
    "data_transfer_in_gib",
    "data_transfer_out_gib",
    "data_transfer_regional_gib",
    "dns_queries",
    "duration_gb_seconds",
    "event_buses",
    "events",
    "get_select_requests",
    "health_checks",
    "hosted_zones",
    "hours_per_month",
    "https_requests",
    "image_scans",
    "images",
    "inbound_messages",
    "input_tokens",
    "io_requests",
    "key_count",
    "log_ingestion_gib",
    "log_storage_gib",
    "managed_storage_gib",
    "namespaces",
    "outbound_messages",
    "output_tokens",
    "pipes_requests",
    "provisioned_dpu_hours",
    "put_copy_post_list_requests",
    "queries",
    "query_samples_processed",
    "read_request_units",
    "repositories",
    "requests",
    "resolver_endpoints",
    "resolver_ip_addresses_per_endpoint",
    "restore_gib",
    "samples_ingested",
    "scheduled_invocations",
    "schedules",
    "schema_discovery_events",
    "secret_count",
    "service_instances",
    "snapshot_changed_gib",
    "snapshot_storage_gib",
    "state_transitions",
    "storage_gib",
    "storage_gib_per_broker",
    "storage_iops",
    "storage_throughput_mbps",
    "experiment_hours",
    "total_storage_gib",
    "user_count",
    "hours_per_user_per_day",
    "hours_per_user_per_month",
    "messages",
    "connection_minutes",
    "throughput_mbps_per_tib",
    "deployment_updates",
    "document_pages",
    "audio_minutes",
    "endpoint_count",
    "task_count",
    "write_records",
    "memory_retention_hours",
    "magnetic_retention_days",
    "kpu_count",
    "kpu_hours",
    "user_volume_gib",
    "write_request_units",
    # Less common official units discovered from AWS offer files.  These used
    # to exist only inside a generated profile, so an AI extraction could fill
    # them but the review UI and generic pricing contract would subsequently
    # treat them as unknown.
    "author_users",
    "bucket_count",
    "deployment_updates",
    "dpu_hours",
    "endpoint_hours",
    "flow_runs",
    "magnetic_store_gib_months",
    "memory_store_gib_hours",
    "object_count",
    "reader_users",
    "resource_count",
    "session_capacity",
    "provisioned_throughput_mibps",
    "throughput_mbps",
    "warm_storage_gib",
    "cold_storage_gib",
    "cross_region_copy_gib",
    "traces_recorded",
    "traces_retrieved",
    "traces_stored",
    "monthly_active_users",
    "machine_to_machine_tokens",
    "provisioned_throughput_units",
    "crawler_dpu_hours",
    "interactive_session_dpu_hours",
    "data_catalog_objects",
    "spice_gib",
    "task_hours",
    "instance_hours",
    "processing_hours",
    "shard_hours",
    "put_payload_units",
    "extended_retention_hours",
    "deliveries",
    "build_minutes",
    "action_execution_minutes",
    "active_pipelines",
    "resource_handler_operations",
    "resource_handler_duration_seconds",
    "hook_invocations",
    "hook_duration_seconds",
    "ec2_instances",
    "ecr_images",
    "lambda_functions",
    "security_checks",
    "resource_assessments",
    "evidence_items",
    "device_count",
    "message_size_kib",
    "things_registered",
    "remote_actions",
    "metric_datapoints",
    "input_channel_hours",
    "viewer_hours",
    "transcode_minutes",
    "channel_count",
    "channel_hours",
    "output_count",
    "output_hours",
    "output_bandwidth_mbps",
}

# Official Price List rows expose billed units, but many customer inputs are
# configuration facts that must first be converted into those units.  Every
# dynamically profiled AWS product receives the complete stable semantic
# vocabulary plus its exact generated ``official_usage_*`` fields.  Keeping
# this as a union prevents a newly learned field from being accepted during
# discovery and then removed by the generic extraction allow-list.
DYNAMIC_SEMANTIC_TEMPLATE_FIELDS = tuple(
    dict.fromkeys((*GENERIC_TEMPLATE_FIELDS, *sorted(BILLING_DIMENSION_FIELDS)))
)


def normalized_service_key(service: str) -> str:
    key = service.strip().casefold()
    aliased = SERVICE_ALIASES.get(key)
    if aliased is not None:
        return aliased
    primary_template = component_template_spec(service)
    return primary_template.service_key if primary_template is not None else key


def requirement_fields(service: str) -> tuple[str, ...]:
    fields = SERVICE_TEMPLATE_FIELDS.get(
        normalized_service_key(service), DYNAMIC_SEMANTIC_TEMPLATE_FIELDS
    )
    return tuple(dict.fromkeys((*fields, *COMMON_TEMPLATE_FIELDS)))


def safe_requirement_defaults(
    service: str,
    requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return system-owned defaults that may never override customer input."""

    service_key = normalized_service_key(service)
    defaults = dict(SERVICE_SAFE_DEFAULTS.get(service_key, {}))
    engine = str((requirements or {}).get("engine") or "").casefold()
    if service_key == "rds" and engine.startswith("aurora"):
        # Aurora uses Aurora Standard or Aurora I/O-Optimized shared cluster
        # storage.  A normal RDS gp3 default is invalid configuration metadata
        # even though the Aurora price adapter can recover later.
        defaults.pop("storage_type", None)
    return defaults


def strip_non_pricing_context_fields(service: str, requirements: dict[str, Any]) -> dict[str, Any]:
    """Project a component onto the fields allowed to influence its price."""

    ignored = NON_PRICING_CONTEXT_FIELDS.get(normalized_service_key(service), frozenset())
    return {field: value for field, value in requirements.items() if field not in ignored}


def billing_dimension_fields(service: str) -> tuple[str, ...]:
    """Return only real metered fields supported by this AWS service."""

    service_key = normalized_service_key(service)
    primary_template = component_template_spec(service_key)
    if primary_template is not None:
        return primary_template.metered_fields
    if service_key not in SERVICE_TEMPLATE_FIELDS:
        return ()
    return tuple(
        field for field in requirement_fields(service) if field in BILLING_DIMENSION_FIELDS
    )


def component_template(
    component: ServiceRequirement,
    *,
    extra_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the complete blank template shown to the extraction model."""

    fields = tuple(dict.fromkeys((*requirement_fields(component.service), *extra_fields)))
    return {
        "service": component.service,
        "calculator_service_name": component.calculator_service_name,
        "region": None,
        "quantity": None,
        "hours_per_month": None,
        "requirements": {field: None for field in fields},
        # This is a lossless safety valve, not a second pricing schema.  The
        # model uses it only when customer-written price data has no honest
        # destination in either the curated fields or the official profile.
        # A later component-scoped repair must map it before final pricing.
        "unmapped_pricing_facts": [],
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
