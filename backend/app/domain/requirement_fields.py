from __future__ import annotations

import re
from typing import Any


# AI output is untrusted structured data.  Models sometimes express the same
# customer value with a plausible synonym.  Keep this mapping centralized so
# every service adapter receives the canonical contract instead of silently
# losing a field.
REQUIREMENT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "requested_model": (
        "instance_type",
        "instance_model",
        "node_type",
        "model_name",
        "aws_model",
    ),
    "vcpu": (
        "vcpus",
        "vcpu_count",
        "cpu",
        "cpu_count",
        "cpu_cores",
        "cores",
        "data_node_vcpu",
    ),
    "memory_gib": (
        "memory",
        "memory_gb",
        "memory_size_gib",
        "ram_gib",
        "ram_gb",
        "data_node_memory_gib",
    ),
    "system_disk_gib": (
        "system_disk_size_gib",
        "system_disk_gb",
        "root_disk_gib",
        "root_volume_gib",
        "disk_size_gib",
    ),
    "user_volume_gib": (
        "user_disk_gib",
        "user_disk_size_gib",
        "user_volume_size_gib",
    ),
    "hours_per_user_per_day": (
        "daily_hours_per_user",
        "user_hours_per_day",
        "hours_per_user_day",
    ),
    "storage_gib": (
        "storage_size_gib",
        "storage_gb",
        "capacity_gib",
        "capacity_gb",
    ),
    "storage_gib_per_node": (
        "data_node_storage_gib",
        "node_storage_gib",
        "storage_per_node_gib",
    ),
    "storage_iops": ("iops", "provisioned_iops", "disk_iops"),
    "storage_throughput_mbps": (
        "throughput_mbps",
        "disk_throughput_mbps",
        "storage_throughput",
    ),
    "data_transfer_out_gib": (
        "outbound_transfer_gib",
        "egress_gib",
        "internet_egress_gib",
        "monthly_egress_gib",
        "data_transfer_gib",
        "transfer_gib",
        "cdn_egress_gib",
        "monthly_accelerated_traffic_gb",
    ),
    "data_transfer_in_gib": ("inbound_transfer_gib", "ingress_gib"),
    "processed_bytes_gib": ("data_processed_gib",),
    "backup_retention_days": ("backup_days", "retention_days"),
    "replicas_per_shard": ("replicas", "replica_count", "read_replicas"),
    "shards": ("shard_count",),
    "operating_system": ("os", "platform"),
    "volume_type": ("disk_type", "ebs_volume_type"),
    "storage_type": ("disk_type", "rds_storage_type"),
    "deployment": ("deployment_option", "availability", "availability_mode"),
    "purchase_option": ("pricing_model", "purchase_type", "billing_option"),
    "engine": ("database_engine", "cache_engine"),
}


# Some model-facing count words are intentionally generic (``node_count``),
# while the pricing contract must name the role that AWS actually bills.  The
# destination is only unambiguous after product identity is known, so keep the
# routing here instead of teaching every parser/prompt the same aliases.  This
# map is deliberately limited to one-to-one meanings; ambiguous words stay in
# ``unmapped_pricing_facts`` for customer/AI clarification.
SERVICE_REQUIREMENT_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "rds": {
        "node_count": "instance_count",
        "nodes": "instance_count",
        "db_instance_count": "instance_count",
        "database_instance_count": "instance_count",
    },
    "documentdb": {
        "node_count": "instance_count",
        "nodes": "instance_count",
    },
    "opensearch": {
        "node_count": "data_nodes",
        "nodes": "data_nodes",
    },
    "msk": {
        "node_count": "broker_count",
        "nodes": "broker_count",
    },
    "mq": {
        "node_count": "broker_count",
        "nodes": "broker_count",
    },
    "eks": {
        "node_count": "worker_node_count",
        "nodes": "worker_node_count",
    },
    "redshift": {
        "node_count": "nodes",
    },
    "sagemaker": {
        "node_count": "instance_count",
        "nodes": "instance_count",
    },
}


def _normalized_service_key(service: str | None) -> str:
    key = (service or "").strip().casefold().replace("-", "_")
    return {
        "amazon_rds": "rds",
        "aurora": "rds",
        "amazon_opensearch": "opensearch",
        "amazon_msk": "msk",
        "amazon_mq": "mq",
        "amazon_eks": "eks",
        "amazon_documentdb": "documentdb",
        "amazon_redshift": "redshift",
        "amazon_sagemaker": "sagemaker",
    }.get(key, key)


def pricing_directive_from_text(
    text: str, *, service: str | None = None
) -> dict[str, Any]:
    """Extract an explicit purchase-plan correction from short customer text.

    Purchase plans are a small closed vocabulary and should not depend solely
    on probabilistic extraction.  This function is intentionally service
    neutral: EC2, RDS and ElastiCache share the same term/payment wording while
    their adapters use slightly different canonical purchase-option values.
    An empty mapping means the text did not ask to change the purchase plan.
    """

    compact = re.sub(r"[\s，,。.!！、;；:：]", "", text).casefold()
    if not compact:
        return {}

    service_key = (service or "").strip().casefold().replace("-", "_")
    service_key = {
        "amazon_ec2": "ec2",
        "amazon_rds": "rds",
        "redis": "elasticache",
        "valkey": "elasticache",
    }.get(service_key, service_key)
    if service_key not in {"ec2", "rds", "elasticache"}:
        return {}

    on_demand = any(
        marker in compact
        for marker in ("按需付费", "按需实例", "按需", "ondemand", "payasyougo")
    )
    reserved = any(
        marker in compact
        for marker in (
            "预留实例", "预留", "reserved", "全预付", "部分预付", "无预付",
            "allupfront", "partialupfront", "noupfront",
        )
    ) or bool(re.search(r"(?:1|一|3|三)年", compact))
    reject_reserved = any(
        marker in compact for marker in ("不要预留", "取消预留", "不用预留", "改回按需")
    )
    reject_on_demand = any(
        marker in compact for marker in ("不要按需", "取消按需", "不用按需", "改成预留")
    )
    if on_demand and reserved:
        if reject_reserved:
            reserved = False
        elif reject_on_demand:
            on_demand = False
        else:
            # A comparison request can legitimately contain both. It is not a
            # single component override, so leave it to scenario selection.
            return {}
    if not on_demand and not reserved:
        return {}
    if on_demand and not reserved:
        return {
            "purchase_option": "on_demand",
            "reserved_term_years": None,
            "payment_option": None,
        }

    years_match = re.search(r"(1|一|3|三)年", compact)
    years = 3 if years_match and years_match.group(1) in {"3", "三"} else 1
    if "全预付" in compact or "allupfront" in compact:
        payment = "all_upfront"
    elif "部分预付" in compact or "partialupfront" in compact:
        payment = "partial_upfront"
    else:
        payment = "no_upfront"

    if service_key == "ec2":
        purchase = (
            "convertible_reserved"
            if "可转换" in compact or "convertiblereserved" in compact
            else "standard_reserved"
        )
    else:
        purchase = "reserved"
    return {
        "purchase_option": purchase,
        "reserved_term_years": years,
        "payment_option": payment,
    }


def _alias_applies(canonical: str, service_key: str) -> bool:
    if canonical == "processed_bytes_gib" and service_key not in {
        "elb",
        "elbv2",
        "alb",
        "elastic_load_balancing",
    }:
        return False
    if canonical == "system_disk_gib" and service_key not in {
            "ec2",
            "amazon_ec2",
            "compute",
    }:
        return False
    if canonical == "volume_type" and service_key not in {
            "ec2",
            "amazon_ec2",
            "compute",
            "ebs",
    }:
        return False
    storage_fields = {"storage_type", "storage_iops", "storage_throughput_mbps"}
    if canonical in storage_fields and service_key not in {"rds", "aurora"}:
        return False
    return True


def canonical_requirement_field_name(field: str, *, service: str | None = None) -> str:
    """Return the pricing-contract name for one model-facing field."""

    service_key = _normalized_service_key(service)
    routed = SERVICE_REQUIREMENT_FIELD_ALIASES.get(service_key, {}).get(field)
    if routed is not None:
        return routed
    for canonical, aliases in REQUIREMENT_FIELD_ALIASES.items():
        if _alias_applies(canonical, service_key) and field in aliases:
            return canonical
    return field


def canonicalize_requirement_fields(
    requirements: dict[str, Any], *, service: str | None = None
) -> dict[str, Any]:
    """Return the stable pricing contract for common AI-authored aliases.

    Canonical values always win when both forms are present.  Ambiguous
    service-specific aliases such as ``disk_type`` are only applied to the
    matching service family.
    """

    normalized = dict(requirements)
    service_key = _normalized_service_key(service)
    for alias, canonical in SERVICE_REQUIREMENT_FIELD_ALIASES.get(
        service_key, {}
    ).items():
        if canonical not in normalized and alias in normalized:
            normalized[canonical] = normalized[alias]
        normalized.pop(alias, None)
    for canonical, aliases in REQUIREMENT_FIELD_ALIASES.items():
        if not _alias_applies(canonical, service_key):
            continue
        for alias in aliases:
            if canonical not in normalized and alias in normalized:
                normalized[canonical] = normalized[alias]
            # Remove only aliases handled by this service.  This prevents a
            # second code path from interpreting the same value differently.
            normalized.pop(alias, None)
    return sanitize_requirement_values(
        _canonicalize_values(normalized, service_key), service=service_key
    )


_MODEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "ec2": re.compile(
        r"^[a-z][a-z0-9-]*\.(?:nano|micro|small|medium|large|xlarge|\d+xlarge|metal(?:-\d+)?)$",
        re.I,
    ),
    "rds": re.compile(
        r"^db\.[a-z][a-z0-9-]*\.(?:micro|small|medium|large|xlarge|\d+xlarge)$",
        re.I,
    ),
    "elasticache": re.compile(
        r"^cache\.[a-z][a-z0-9-]*\.(?:micro|small|medium|large|xlarge|\d+xlarge)$",
        re.I,
    ),
    "msk": re.compile(
        r"^(?:kafka\.)?[a-z][a-z0-9-]*\.(?:small|medium|large|xlarge|\d+xlarge)$",
        re.I,
    ),
    "opensearch": re.compile(
        r"^[a-z][a-z0-9-]*\.(?:micro|small|medium|large|xlarge|\d+xlarge)\.search$",
        re.I,
    ),
    "dms": re.compile(
        r"^dms\.[a-z][a-z0-9-]*\.(?:micro|small|medium|large|xlarge|\d+xlarge)$",
        re.I,
    ),
    "sagemaker": re.compile(r"^ml\.[a-z][a-z0-9.-]+$", re.I),
    "mq": re.compile(r"^mq\.[a-z][a-z0-9.-]+$", re.I),
}

# Product APIs and the AWS Price List do not always spell the same official
# model with identical service prefixes. Preserve any conservative
# dot-delimited catalog token and let the service adapter verify whether it is
# actually offered in the selected region. This prevents a valid choice from
# being silently deleted while still rejecting prose such as ``4核16G``.
_GENERIC_CATALOG_MODEL_TOKEN = re.compile(
    r"^[a-z][a-z0-9-]*(?:\.[a-z0-9-]+){1,4}$",
    re.I,
)


_NUMERIC_REQUIREMENT_FIELDS = {
    "vcpu", "memory_gib", "memory_mb", "ephemeral_storage_mb",
    "system_disk_gib", "user_volume_gib", "total_system_disk_gib",
    "total_worker_system_disk_gib", "hours_per_user_per_day",
    "storage_gib", "total_storage_gib", "storage_gib_per_node",
    "storage_gib_per_broker", "source_storage_gib_per_node",
    "storage_iops", "storage_throughput_mbps",
    "ebs_iops", "ebs_throughput_mbps", "hours_per_month", "broker_hours",
    "instance_hours", "task_hours", "processing_hours", "control_plane_hours", "shard_hours",
    "data_transfer_in_gib", "data_transfer_regional_gib",
    "data_transfer_out_gib", "data_transfer_gib", "data_processed_gib",
    "processed_bytes_gib", "processed_bytes_ec2_ip_gib_per_hour",
    "requests", "request_count", "https_requests", "dns_queries",
    "read_request_units", "write_request_units",
    "api_calls", "io_requests", "put_payload_units", "data_in_gib",
    "data_out_gib", "data_scanned_gib", "duration_ms", "input_tokens",
    "output_tokens", "images", "custom_metrics", "alarms",
    "new_connections_per_second", "average_connection_duration_seconds",
    "active_connections_per_minute", "requests_per_second",
    "rule_evaluations_per_request", "rule_evaluations_per_second", "lcu_count",
    "utilization_percent", "snapshot_changed_gib", "backup_storage_gib",
    "warm_storage_gib", "cold_storage_gib", "restore_gib", "cross_region_copy_gib",
    "provisioned_throughput_mibps", "throughput_mbps", "rpu", "dpu_hours",
    "throughput_mbps_per_tib", "connection_minutes",
    "crawler_dpu_hours", "interactive_session_dpu_hours",
    "master_vcpu", "master_memory_gib", "master_storage_gib_per_node",
    "core_vcpu", "core_memory_gib", "core_storage_gib_per_node",
    "task_vcpu", "task_memory_gib", "task_storage_gib_per_node",
    "managed_storage_gib", "snapshot_storage_gib", "provisioned_dpu_hours",
    "resource_count", "flow_runs", "bucket_count", "object_count",
    "deployment_updates", "author_users", "reader_users", "session_capacity",
    "spice_gib", "write_records", "memory_retention_hours",
    "magnetic_retention_days", "kpu_hours",
}


_INTEGER_REQUIREMENT_FIELDS = {
    "broker_count", "node_count", "data_nodes", "master_nodes",
    "warm_node_count", "shards", "replicas_per_shard", "instance_count",
    "replication_instances", "cluster_count", "tasks", "repositories",
    "hosted_zones", "health_checks", "web_acls", "rules", "listeners",
    "secret_count", "key_count", "vpc_count", "public_subnets",
    "private_subnets", "availability_zones", "gateway_count", "accelerators",
    "schedules", "scheduled_invocations", "read_replica_count",
    "backup_retention_days", "snapshot_retention_days", "retention_days",
    "log_retention_days",
    "outbound_messages", "inbound_messages", "image_scans", "queue_count",
    "event_buses", "namespaces", "service_instances", "nodes",
    "master_nodes", "core_nodes", "task_nodes", "provisioned_throughput_units",
    "user_count",
    "messages", "flow_runs", "bucket_count", "object_count",
    "deployment_updates", "author_users", "reader_users", "session_capacity",
    "listener_count", "endpoint_count", "task_count", "replica_count",
    "writer_nodes", "reader_nodes", "kpu_count", "write_records",
}


_BOOLEAN_REQUIREMENT_FIELDS = {
    "detailed_monitoring", "performance_insights", "enhanced_monitoring",
    "rotation_enabled", "cluster_mode", "data_tiering", "dedicated_master",
    "multi_az", "include_logs", "include_metrics", "advanced_security",
    "reference_unit_only", "reference_lcu_unit_only", "data_transfer_monitoring",
}


def sanitize_requirement_values(
    requirements: dict[str, Any], *, service: str | None = None
) -> dict[str, Any]:
    """Reject prose and field-shape errors before an AWS adapter sees them.

    AI output can be valid JSON while still putting ``4核16G`` in
    ``requested_model`` or a sentence such as ``请客户确认节点数`` in a numeric
    field.  Those values are missing data, not models or quantities.  This is
    intentionally deterministic and never invents a replacement value.
    """

    cleaned = dict(requirements)
    service_key = (service or "").strip().casefold().replace("-", "_")
    aliases = {
        "redis": "elasticache",
        "valkey": "elasticache",
        "amazon_msk": "msk",
        "amazon_ec2": "ec2",
        "aurora": "rds",
    }
    service_key = aliases.get(service_key, service_key)

    model = cleaned.get("requested_model")
    if isinstance(model, str):
        model = model.strip().casefold()
        pattern = _MODEL_PATTERNS.get(service_key)
        if not model or (
            pattern is not None
            and not pattern.fullmatch(model)
            and not _GENERIC_CATALOG_MODEL_TOKEN.fullmatch(model)
        ):
            cleaned.pop("requested_model", None)
        else:
            cleaned["requested_model"] = model.removeprefix("kafka.") if service_key == "msk" else model
    elif model is not None:
        cleaned.pop("requested_model", None)

    for field in _NUMERIC_REQUIREMENT_FIELDS | _INTEGER_REQUIREMENT_FIELDS:
        if field not in cleaned:
            continue
        value = cleaned[field]
        parsed = _parse_numeric_value(value, gib=field.endswith("_gib"))
        if parsed is None or parsed < 0:
            cleaned.pop(field, None)
            continue
        if field in _INTEGER_REQUIREMENT_FIELDS:
            if not float(parsed).is_integer():
                cleaned.pop(field, None)
                continue
            cleaned[field] = int(parsed)
        else:
            cleaned[field] = parsed

    # Automatically discovered services use this guarded namespace for AWS
    # billing dimensions whose unit has no existing cross-service canonical
    # field.  Values are still numeric-only and are bound to an exact official
    # UsageType/Operation before they can enter pricing.
    for field in list(cleaned):
        if not field.startswith("official_usage_"):
            continue
        parsed = _parse_numeric_value(cleaned[field], gib=False)
        if parsed is None or parsed < 0:
            cleaned.pop(field, None)
        else:
            cleaned[field] = parsed

    boolean_values = {
        "true": True, "yes": True, "on": True, "开启": True, "启用": True, "是": True,
        "false": False, "no": False, "off": False, "关闭": False, "禁用": False, "否": False,
    }
    for field in _BOOLEAN_REQUIREMENT_FIELDS:
        if field not in cleaned:
            continue
        value = cleaned[field]
        if isinstance(value, bool):
            continue
        token = str(value).strip().casefold()
        if token in boolean_values:
            cleaned[field] = boolean_values[token]
        else:
            cleaned.pop(field, None)
    return cleaned


def _token(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(r"[\s\-/]+", "_", value.strip().casefold()).strip("_")


def _canonicalize_values(requirements: dict[str, Any], service: str) -> dict[str, Any]:
    """Normalize common AI/customer value spellings into the adapter contract.

    This deliberately normalizes only equivalent labels.  It never changes a
    storage tier, instance family, database edition, or purchase commitment.
    """

    normalized = dict(requirements)
    requested_model = normalized.get("requested_model")
    if isinstance(requested_model, str) and requested_model.strip():
        normalized["requested_model"] = requested_model.strip().casefold()

    aliases: dict[str, dict[str, str]] = {
        "operating_system": {
            "linux": "linux",
            "ubuntu": "linux",
            "ubuntu_server": "linux",
            "amazon_linux": "linux",
            "al2023": "linux",
            "windows": "windows",
            "windows_server": "windows",
            "windows_server_2019": "windows",
            "windows_server_2022": "windows",
            "rhel": "rhel",
            "red_hat": "rhel",
            "red_hat_enterprise_linux": "rhel",
            "suse": "suse",
        },
        "purchase_option": {
            "on_demand": "on_demand",
            "ondemand": "on_demand",
            "pay_as_you_go": "on_demand",
            "按需": "on_demand",
            "按需付费": "on_demand",
            "spot": "spot",
            "竞价": "spot",
            "reserved": "reserved",
            "standard_reserved": "standard_reserved",
            "标准预留实例": "standard_reserved",
            "convertible_reserved": "convertible_reserved",
            "可转换预留实例": "convertible_reserved",
        },
        "deployment": {
            "single_az": "single_az",
            "singleaz": "single_az",
            "单可用区": "single_az",
            "multi_az": "multi_az",
            "multiaz": "multi_az",
            "主备": "multi_az",
            "主备高可用": "multi_az",
            "multi_az_db_instance": "multi_az",
            "multi_az_cluster": "multi_az_cluster",
            "multi_az_db_cluster": "multi_az_cluster",
        },
        "engine": {
            "postgres": "postgresql",
            "postgresql": "postgresql",
            "mysql": "mysql",
            "mariadb": "mariadb",
            "aurora_mysql": "aurora_mysql",
            "aurora_postgresql": "aurora_postgresql",
            "aurora_postgres": "aurora_postgresql",
            "redis": "redis",
            "redis_oss": "redis",
            "valkey": "valkey",
            "memcached": "memcached",
            "sql_server_standard": "sql_server_standard",
            "sqlserver_standard": "sql_server_standard",
            "sql_server_web": "sql_server_web",
            "sql_server_enterprise": "sql_server_enterprise",
            "sql_server_express": "sql_server_express",
        },
    }
    for field, mapping in aliases.items():
        token = _token(normalized.get(field))
        if token in mapping:
            normalized[field] = mapping[token]

    os_token = _token(normalized.get("operating_system")) or ""
    if os_token.startswith(("ubuntu", "amazon_linux", "al20")):
        normalized["operating_system"] = "linux"
    elif os_token.startswith("windows_server"):
        normalized["operating_system"] = "windows"

    for field in ("volume_type", "storage_type"):
        token = _token(normalized.get(field))
        if not token:
            continue
        compact = token.replace("_", "")
        if "gp3" in compact:
            normalized[field] = "gp3"
        elif "gp2" in compact:
            normalized[field] = "gp2"
        elif "io2" in compact:
            normalized[field] = "io2"
        elif "io1" in compact:
            normalized[field] = "io1"
        elif "st1" in compact:
            normalized[field] = "st1"
        elif "sc1" in compact:
            normalized[field] = "sc1"
        elif compact in {"ssd", "solidstatedrive", "generalpurposessd", "通用型ssd", "固态硬盘"}:
            normalized[field] = "ssd"

    numeric_fields = {
        "vcpu",
        "memory_gib",
        "system_disk_gib",
        "storage_gib",
        "storage_iops",
        "storage_throughput_mbps",
        "ebs_iops",
        "ebs_throughput_mbps",
        "data_transfer_in_gib",
        "data_transfer_regional_gib",
        "data_transfer_out_gib",
        "processed_bytes_gib",
        "backup_retention_days",
        "utilization_percent",
        "https_requests",
    }
    gib_fields = {
        "memory_gib",
        "system_disk_gib",
        "storage_gib",
        "data_transfer_in_gib",
        "data_transfer_regional_gib",
        "data_transfer_out_gib",
        "processed_bytes_gib",
    }
    for field in numeric_fields.intersection(normalized):
        parsed = _parse_numeric_value(normalized[field], gib=field in gib_fields)
        if parsed is not None:
            normalized[field] = parsed

    volumes = normalized.get("additional_ebs_volumes")
    if isinstance(volumes, list):
        cleaned_volumes: list[object] = []
        for volume in volumes:
            if not isinstance(volume, dict):
                cleaned_volumes.append(volume)
                continue
            cleaned = dict(volume)
            cleaned.setdefault(
                "volume_type",
                normalized.get("volume_type") or "gp3",
            )
            token = _token(cleaned.get("volume_type"))
            compact = (token or "").replace("_", "")
            for volume_type in ("gp3", "gp2", "io2", "io1", "st1", "sc1"):
                if volume_type in compact:
                    cleaned["volume_type"] = volume_type
                    break
            size = _parse_numeric_value(cleaned.get("size_gib"), gib=True)
            if size is not None:
                cleaned["size_gib"] = size
            count = _parse_numeric_value(cleaned.get("count_per_instance"), gib=False)
            if count is not None:
                cleaned["count_per_instance"] = int(count)
            cleaned_volumes.append(cleaned)
        normalized["additional_ebs_volumes"] = cleaned_volumes

    return normalized


def _parse_numeric_value(value: object, *, gib: bool) -> float | None:
    """Parse a model/customer number while preserving the requested unit.

    The AI contract asks for bare numbers, but real integrations and cached
    drafts can still contain labels such as ``1 TB`` or ``3,000 IOPS``.  Only
    an explicit leading number is accepted; prose placeholders remain missing.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "")
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*(万|亿)?\s*"
        r"(tib|tb|ti?b?|gib|gb|gi?b?|mib|mb|vcpu|核|kpu|iops|io/s|mb/s|mib/s|%|天|days?)?\s*"
        r"(?:条|次|个|台|节点|人|小时|分钟)?\s*",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    number = float(match.group(1))
    scale = match.group(2) or ""
    if scale == "万":
        number *= 10_000
    elif scale == "亿":
        number *= 100_000_000
    unit = (match.group(3) or "").casefold()
    if gib and unit in {"tb", "tib", "t", "ti"}:
        return number * 1024
    if gib and unit in {"mb", "mib"}:
        return number / 1024
    return number
