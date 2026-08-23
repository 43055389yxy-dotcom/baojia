from __future__ import annotations

from typing import Any

from app.domain.models import ServiceRequirement

_TEMPLATE_ALIASES = {
    "vm": "azure_vm",
    "virtual_machines": "azure_vm",
    "postgresql": "azure_postgresql",
    "mysql": "azure_mysql",
    "redis": "azure_cache",
    "storage": "blob_storage",
    "cdn": "front_door",
    "data_transfer": "bandwidth",
    "apim": "api_management",
}


def _service_key(service: str) -> str:
    key = service.strip().casefold().replace("-", "_")
    return _TEMPLATE_ALIASES.get(key, key)


AZURE_SERVICE_TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    "azure_vm": (
        "requested_sku",
        "vcpu",
        "memory_gib",
        "operating_system",
        "architecture",
        "system_disk_gib",
        "system_disk_sku",
        "system_disk_type",
        "data_disk_gib",
        "data_disk_count",
        "hours_per_month",
    ),
    "managed_disks": (
        "requested_sku",
        "disk_type",
        "disk_size_gib",
        "storage_gib",
        "iops",
        "throughput_mbps",
        "disk_role",
    ),
    "azure_sql": (
        "requested_sku",
        "deployment_model",
        "service_tier",
        "compute_model",
        "vcore",
        "memory_gib",
        "storage_gib",
        "high_availability",
        "license_model",
    ),
    "azure_postgresql": (
        "requested_sku",
        "service_tier",
        "compute_model",
        "vcore",
        "memory_gib",
        "storage_gib",
        "high_availability",
        "backup_retention_days",
    ),
    "azure_mysql": (
        "requested_sku",
        "service_tier",
        "compute_model",
        "vcore",
        "memory_gib",
        "storage_gib",
        "high_availability",
        "backup_retention_days",
    ),
    "azure_cache": (
        "requested_sku",
        "service_tier",
        "capacity",
        "memory_gib",
        "replicas",
        "shards",
    ),
    "blob_storage": (
        "requested_sku",
        "access_tier",
        "redundancy",
        "storage_gib",
        "write_operations",
        "read_operations",
        "data_retrieval_gib",
    ),
    "load_balancer": (
        "requested_sku",
        "load_balancer_type",
        "rules",
        "data_processed_gib",
    ),
    "application_gateway": (
        "requested_sku",
        "service_tier",
        "capacity_units",
        "data_processed_gib",
    ),
    "front_door": (
        "requested_sku",
        "service_tier",
        "data_transfer_out_gib",
        "requests",
    ),
    "bandwidth": (
        "data_transfer_out_gib",
        "source_region",
        "destination_zone",
    ),
    "aks": (
        "service_tier",
        "cluster_count",
        "worker_requested_sku",
        "worker_node_count",
        "worker_vcpu",
        "worker_memory_gib",
        "worker_system_disk_gib",
    ),
    "monitor": (
        "log_ingestion_gib",
        "retention_days",
        "custom_metrics",
        "alerts",
    ),
    "api_management": (
        "requested_sku",
        "service_tier",
        "units",
        "requests",
        "data_transfer_out_gib",
    ),
}

GENERIC_AZURE_FIELDS = (
    "requested_sku",
    "service_tier",
    "monthly_quantity",
    "usage_unit",
    "storage_gib",
    "requests",
    "data_transfer_out_gib",
)


def azure_requirement_fields(service: str) -> tuple[str, ...]:
    return AZURE_SERVICE_TEMPLATE_FIELDS.get(_service_key(service), GENERIC_AZURE_FIELDS)


def azure_component_template(
    component: ServiceRequirement,
    *,
    extra_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    fields = tuple(dict.fromkeys((*azure_requirement_fields(component.service), *extra_fields)))
    return {
        "service": component.service,
        "calculator_service_name": component.calculator_service_name,
        "product_identity": component.product_identity,
        "region": None,
        "quantity": None,
        "hours_per_month": None,
        "requirements": {field: None for field in fields},
        "source_text": component.source_text,
        "query_action": None,
    }
