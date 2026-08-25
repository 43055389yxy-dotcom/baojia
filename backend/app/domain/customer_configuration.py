from __future__ import annotations

import re

from app.domain.component_integrity import (
    canonical_component_source,
    enforce_component_integrity,
    ensure_component_keys,
)
from app.domain.models import ParsedIntent, ServiceRequirement
from app.integrations.service_templates import requirement_fields

CUSTOMER_AUTHORITATIVE_SOURCES = {
    "customer_confirmation",
    "customer_confirmation_removed",
    "customer_correction",
    "sales_confirmation",
}

_AURORA_ENGINE_NAMES = {
    "auroramysql": "Amazon Aurora MySQL",
    "aurorapostgres": "Amazon Aurora PostgreSQL",
    "aurorapostgresql": "Amazon Aurora PostgreSQL",
}

_RDS_ENGINE_NAMES = {
    "mysql": "Amazon RDS MySQL",
    "postgresql": "Amazon RDS PostgreSQL",
    "postgres": "Amazon RDS PostgreSQL",
    "mariadb": "Amazon RDS MariaDB",
    "oracle": "Amazon RDS Oracle",
    "db2": "Amazon RDS Db2",
    "sqlserverenterprise": "Amazon RDS SQL Server Enterprise",
    "sqlserverstandard": "Amazon RDS SQL Server Standard",
    "sqlserverweb": "Amazon RDS SQL Server Web",
}

_CACHE_ENGINE_NAMES = {
    "redis": ("elasticache_redis", "Amazon ElastiCache for Redis"),
    "valkey": ("elasticache_valkey", "Amazon ElastiCache for Valkey"),
    "memcached": ("elasticache_memcached", "Amazon ElastiCache for Memcached"),
}

_LOAD_BALANCER_NAMES = {
    "application": ("application_load_balancer", "Application Load Balancer"),
    "network": ("network_load_balancer", "Network Load Balancer"),
    "gateway": ("gateway_load_balancer", "Gateway Load Balancer"),
}

_MQ_ENGINE_NAMES = {
    "rabbitmq": ("amazon_mq_rabbitmq", "Amazon MQ for RabbitMQ"),
    "activemq": ("amazon_mq_activemq", "Amazon MQ for ActiveMQ"),
}

_API_GATEWAY_NAMES = {
    "http": ("api_gateway_http", "Amazon API Gateway HTTP API"),
    "httpapi": ("api_gateway_http", "Amazon API Gateway HTTP API"),
    "rest": ("api_gateway_rest", "Amazon API Gateway REST API"),
    "restapi": ("api_gateway_rest", "Amazon API Gateway REST API"),
    "websocket": ("api_gateway_websocket", "Amazon API Gateway WebSocket API"),
    "websocketapi": ("api_gateway_websocket", "Amazon API Gateway WebSocket API"),
}

_MSK_CLUSTER_NAMES = {
    "serverless": ("amazon_msk_serverless", "Amazon MSK Serverless"),
    "provisioned": ("amazon_msk_provisioned", "Amazon MSK Provisioned"),
}

_FSX_TYPE_NAMES = {
    "windows": ("amazon_fsx_windows", "Amazon FSx for Windows File Server"),
    "lustre": ("amazon_fsx_lustre", "Amazon FSx for Lustre"),
    "ontap": ("amazon_fsx_ontap", "Amazon FSx for NetApp ONTAP"),
    "openzfs": ("amazon_fsx_openzfs", "Amazon FSx for OpenZFS"),
}


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _confirmed_value(requirement: ServiceRequirement, field: str) -> object | None:
    path = f"requirements.{field}"
    if requirement.field_sources.get(path) == "customer_confirmation":
        return requirement.requirements.get(field)
    return None


def _set_customer_product_field(
    requirement: ServiceRequirement,
    field: str,
    value: object,
    evidence: str,
) -> None:
    """Write and lock a product-defining fact found in customer text."""

    path = f"requirements.{field}"
    # A value the customer selected on the confirmation page or edited in the
    # configuration table is newer than the original sales text.  Replaying
    # literal-preservation on a saved draft must never turn MySQL 8.4 back into
    # 5.7.44, restore an old model, or undo a capacity edit.
    if requirement.field_sources.get(path) in {
        *CUSTOMER_AUTHORITATIVE_SOURCES,
        "system_cheapest_official_match",
    }:
        return
    requirement.requirements[field] = value
    requirement.field_sources[path] = "customer_text"
    requirement.field_evidence[path] = evidence
    requirement.locked_fields = sorted(set(requirement.locked_fields) | {path})


def _set_customer_scalar_field(
    requirement: ServiceRequirement,
    field: str,
    value: object,
    evidence: str,
) -> None:
    """Write and lock a top-level component fact found in customer text."""

    # Structured edits are authoritative for the same reason as product-field
    # edits above.  In particular, a saved ``数量 1`` in the original text must
    # not overwrite a later table edit to 3 during revalidation.
    if requirement.field_sources.get(field) in CUSTOMER_AUTHORITATIVE_SOURCES:
        return
    setattr(requirement, field, value)
    requirement.field_sources[field] = "customer_text"
    requirement.field_evidence[field] = evidence
    requirement.locked_fields = sorted(set(requirement.locked_fields) | {field})


def _without_sales_number(value: str) -> str:
    return re.sub(
        r"^\s*(?:需求\s*)?(?:[（(]\s*)?\d{1,3}\s*(?:[)）]\s*)?[、,，.．。:：;；\-—]?\s*",
        "",
        value,
        count=1,
        flags=re.I,
    ).strip()


def _set_product_identity(
    requirement: ServiceRequirement,
    identity: str,
    display_name: str,
) -> None:
    requirement.product_identity = identity
    requirement.calculator_service_name = display_name


def customer_product_identity(requirement: ServiceRequirement) -> str:
    """Return the stable product identity used by pre-quote invariants."""

    if requirement.product_identity:
        return requirement.product_identity
    return _normalized(requirement.calculator_service_name or requirement.service)


def aurora_display_name(requirement: ServiceRequirement) -> str | None:
    """Return the customer-facing Aurora product identity, when applicable."""

    confirmed_engine = _normalized(_confirmed_value(requirement, "engine"))
    if confirmed_engine in _AURORA_ENGINE_NAMES:
        return _AURORA_ENGINE_NAMES[confirmed_engine]
    source = (requirement.source_text or "").casefold()
    if "aurora" in source:
        if "postgres" in source:
            return "Amazon Aurora PostgreSQL"
        return "Amazon Aurora MySQL"
    # Literal ordinary-RDS wording is stronger than a generated Aurora engine.
    # This is the exact failure mode that previously relabelled RDS MySQL as
    # Aurora after component cleanup.
    if "rds" in source and any(
        marker in source
        for marker in ("mysql", "postgres", "mariadb", "sql server", "oracle", "db2")
    ):
        return None
    engine = _normalized(requirement.requirements.get("engine"))
    if engine in _AURORA_ENGINE_NAMES:
        return _AURORA_ENGINE_NAMES[engine]
    if "aurora" not in source:
        return None
    return None


def preserve_customer_configuration(intent: ParsedIntent) -> None:
    """Preserve product identity and architecture independently of billing fields.

    AWS publishes Aurora prices in the Amazon RDS catalog, but that catalog
    detail must never rewrite the product and topology confirmed by a customer.
    This function only restores literal Aurora facts; pricing adapters perform
    their own private catalog-field conversion on a copied requirement.
    """

    original_boundaries = {
        id(item): (_normalized(item.service), _normalized(item.product_identity))
        for item in intent.services
    }
    for item in intent.services:
        service_key = _normalized(item.service)
        source = item.source_text or ""
        folded = source.casefold()
        source_heading = re.split(r"[：:]", _without_sales_number(source), maxsplit=1)[0]

        # A composite heading is a declaration of two independent products.
        # ``Public-VPC`` in the explanation is only the network they protect;
        # it must not turn the whole WAF + ALB row into a VPC.  This also
        # repairs persisted drafts produced before the inventory boundary was
        # tightened below.
        explicit_waf_alb = bool(
            re.search(r"(?=.*\bwaf\b)(?=.*\balb\b)", source_heading, re.I)
            or re.search(
                r"waf\s*[+＋/&和与]\s*(?:application\s+load\s+balancer|负载均衡)",
                source_heading,
                re.I,
            )
        )
        if explicit_waf_alb and service_key == "vpc":
            item.service = "waf"
            item.product_identity = "aws_waf"
            item.calculator_service_name = "AWS WAF"
            service_key = "waf"
            for field in tuple(item.requirements):
                if field.startswith("_review_") or field in {
                    "requested_model", "vcpu", "memory_gib", "operating_system",
                    "architecture", "system_disk_gib", "total_system_disk_gib",
                    "volume_type", "vpc_count", "public_subnets", "private_subnets",
                    "_quote_skip_reason", "_quote_skip_code", "_quote_skip_category",
                }:
                    item.requirements.pop(field, None)
            _set_customer_product_field(item, "web_acls", item.quantity, "WAF 数量")
            _set_customer_product_field(item, "rules", item.quantity, "WAF 基础规则")

        # ``数量`` is the customer's component count. It is independent from
        # product-specific dimensions such as RDS cluster members and must not
        # fall back to one merely because a parser/plugin uses its own count
        # field internally.
        quantity_match = re.search(r"(?:^|[：:，,；;\s])数量\s*[:：]?\s*(\d+)", source, re.I)
        if quantity_match:
            explicit_quantity = max(int(quantity_match.group(1)), 1)
            _set_customer_scalar_field(
                item,
                "quantity",
                explicit_quantity,
                quantity_match.group(0).strip(),
            )
            if service_key in {"route53", "amazonroute53"}:
                _set_customer_product_field(
                    item, "hosted_zones", explicit_quantity, quantity_match.group(0).strip()
                )
            elif service_key in {"waf", "awswaf"}:
                _set_customer_product_field(
                    item, "web_acls", explicit_quantity, quantity_match.group(0).strip()
                )
                if item.requirements.get("rules") in {None, 1}:
                    _set_customer_product_field(
                        item, "rules", explicit_quantity, quantity_match.group(0).strip()
                    )

        # Preserve licensed Linux distributions. Pricing Red Hat Enterprise
        # Linux as generic Linux omits the RHEL software charge and can also
        # select a product with the wrong official operation.
        if service_key == "ec2":
            rhel_match = re.search(
                r"red\s*hat(?:\s+enterprise)?(?:\s+linux)?(?:\s+\d+(?:\.\d+)?)?|\brhel\b(?:\s*\d+(?:\.\d+)?)?",
                source,
                re.I,
            )
            if rhel_match:
                _set_customer_product_field(
                    item, "operating_system", "RHEL", rhel_match.group(0)
                )
            disk_match = re.search(
                r"\d+(?:\.\d+)?\s*(?:核|c|vcpu)\s*"
                r"\d+(?:\.\d+)?\s*(?:gib|gb|g)\s*[/＋+]\s*"
                r"(\d+(?:\.\d+)?)\s*(gib|gb|g|tib|tb|t)\b",
                source,
                re.I,
            )
            if disk_match:
                disk_gib = float(disk_match.group(1))
                if disk_match.group(2).casefold() in {"tib", "tb", "t"}:
                    disk_gib *= 1024
                _set_customer_product_field(
                    item,
                    "system_disk_gib",
                    int(disk_gib) if disk_gib.is_integer() else disk_gib,
                    disk_match.group(0),
                )

        # Pinpoint and SES can both deliver email, but they are distinct
        # customer products with different official billing dimensions.
        if "pinpoint" in folded or "pinpoint" in service_key:
            item.service = "pinpoint"
            _set_product_identity(item, "amazon_pinpoint", "Amazon Pinpoint")
            # Earlier drafts occasionally reused the SES review model for a
            # Pinpoint request.  It is not merely a display label: retaining
            # it can route the later quote through the wrong AWS product.
            # Remove only that known cross-product residue; a genuine
            # Pinpoint option remains untouched.
            stale_review_model = str(
                item.requirements.get("_review_selected_model") or ""
            ).casefold()
            if "ses" in stale_review_model:
                for internal_field in (
                    "_review_selected_model",
                    "_review_selected_specifications",
                    "_review_available_shapes",
                    "_review_field_options",
                ):
                    item.requirements.pop(internal_field, None)
            message_match = re.search(
                r"(\d+(?:\.\d+)?)\s*(万|亿)?\s*封(?:\s*(?:/|每)\s*月)?",
                source,
                re.I,
            )
            if message_match:
                multiplier = {"万": 10_000, "亿": 100_000_000}.get(
                    message_match.group(2), 1
                )
                messages = float(message_match.group(1)) * multiplier
                _set_customer_product_field(
                    item,
                    "outbound_messages",
                    int(messages) if messages.is_integer() else messages,
                    message_match.group(0),
                )
            continue

        # MemoryDB is a distinct AWS product even though Redis is its engine.
        # Explicit product wording outranks a generic Redis/ElastiCache label
        # produced by an earlier model pass.  In particular, never interpret
        # the family token ``r7g`` inside a node type as "7 GB".
        if "memorydb" in folded or "memorydb" in service_key:
            item.service = "memorydb"
            _set_product_identity(item, "amazon_memorydb_redis", "Amazon MemoryDB")
            _set_customer_product_field(item, "engine", "redis", "MemoryDB Redis")
            model_match = re.search(
                r"(?<![a-z0-9.])(db\.[a-z0-9][a-z0-9.-]*)(?![a-z0-9.])",
                source,
                re.I,
            )
            if model_match:
                _set_customer_product_field(
                    item, "requested_model", model_match.group(1), model_match.group(1)
                )
            memory_match = re.search(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*(?:gib|gb)\b", source, re.I)
            if memory_match:
                value = float(memory_match.group(1))
                _set_customer_product_field(
                    item,
                    "memory_gib",
                    int(value) if value.is_integer() else value,
                    memory_match.group(0),
                )
            continue

        # Public-VPC and Private-VPC describe networking boundaries, not
        # software workloads. Repair an erroneous "unknown product -> EC2"
        # fallback without touching a component that explicitly names EC2.
        is_named_vpc = re.search(
            r"\b(?:public|private)[-_ ]?vpc\b|公有\s*vpc|私有\s*vpc",
            source,
            re.I,
        )
        source_heading = re.split(r"[：:]", source, maxsplit=1)[0]
        if (
            is_named_vpc
            and not explicit_waf_alb
            and not re.search(r"\bec2\b", source_heading, re.I)
        ):
            item.service = "vpc"
            label = (
                "Private" if re.search(r"private[-_ ]?vpc|私有\s*vpc", source, re.I) else "Public"
            )
            _set_product_identity(
                item,
                f"amazon_vpc_{label.casefold()}",
                f"Amazon VPC ({label})",
            )
            for field in (
                "requested_model",
                "vcpu",
                "memory_gib",
                "operating_system",
                "architecture",
                "system_disk_gib",
                "total_system_disk_gib",
                "volume_type",
                "purchase_option",
                "reserved_term_years",
                "payment_option",
                "utilization_percent",
                "detailed_monitoring",
                "system_default_assumption",
            ):
                item.requirements.pop(field, None)
                path = f"requirements.{field}"
                item.field_sources.pop(path, None)
                item.field_evidence.pop(path, None)
                if path in item.locked_fields:
                    item.locked_fields.remove(path)
            for internal_field in (
                "_review_selected_model",
                "_review_selected_specifications",
                "_review_available_shapes",
                "_review_field_options",
                "_quote_skip_reason",
                "_quote_skip_code",
                "_quote_skip_category",
            ):
                item.requirements.pop(internal_field, None)
            continue

        # Products below share a catalog/adapter family, but not a customer
        # identity. Explicit customer wording wins over generated subtype
        # fields; a later customer-confirmed subtype wins over the original
        # wording so the correction workflow remains authoritative.
        if any(marker in service_key for marker in ("elasticache", "redis", "valkey", "memcached")):
            confirmed = _normalized(_confirmed_value(item, "engine"))
            engine = confirmed
            if not engine:
                if "valkey" in folded:
                    engine = "valkey"
                elif "memcached" in folded:
                    engine = "memcached"
                elif "redis" in folded:
                    engine = "redis"
                else:
                    engine = _normalized(item.requirements.get("engine")) or "redis"
            identity, display = _CACHE_ENGINE_NAMES.get(engine, _CACHE_ENGINE_NAMES["redis"])
            if not confirmed and any(
                marker in folded for marker in ("redis", "valkey", "memcached")
            ):
                _set_customer_product_field(item, "engine", engine, engine)
            _set_product_identity(item, identity, display)
            continue

        if (
            service_key in {"elb", "elbv2", "alb", "nlb", "gwlb", "elasticloadbalancing"}
            or "loadbalanc" in service_key
        ):
            confirmed = _normalized(_confirmed_value(item, "load_balancer_type"))
            lb_type = confirmed
            evidence = ""
            if not lb_type:
                if re.search(
                    r"\b(?:gwlb|gateway\s+load\s+balancer)\b|网关型负载均衡", source, re.I
                ):
                    lb_type, evidence = "gateway", "GWLB"
                elif re.search(
                    r"\b(?:nlb|network\s+load\s+balancer)\b|网络型负载均衡", source, re.I
                ):
                    lb_type, evidence = "network", "NLB"
                elif re.search(
                    r"\b(?:alb|application\s+load\s+balancer)\b|应用型负载均衡|公网负载均衡",
                    source,
                    re.I,
                ):
                    lb_type, evidence = "application", "ALB"
                else:
                    lb_type = _normalized(item.requirements.get("load_balancer_type"))
            identity, display = _LOAD_BALANCER_NAMES.get(
                lb_type, ("elastic_load_balancing", "Elastic Load Balancing")
            )
            if evidence and not confirmed:
                _set_customer_product_field(item, "load_balancer_type", lb_type, evidence)
            _set_product_identity(item, identity, display)
            continue

        if service_key in {"mq", "amazonmq"}:
            confirmed = _normalized(_confirmed_value(item, "engine_type"))
            engine = confirmed
            evidence = ""
            if not engine:
                if "rabbitmq" in folded:
                    engine, evidence = "rabbitmq", "RabbitMQ"
                elif "activemq" in folded or "active mq" in folded:
                    engine, evidence = "activemq", "ActiveMQ"
                else:
                    engine = _normalized(item.requirements.get("engine_type"))
            identity, display = _MQ_ENGINE_NAMES.get(engine, ("amazon_mq", "Amazon MQ"))
            if evidence and not confirmed:
                _set_customer_product_field(item, "engine_type", engine, evidence)
            _set_product_identity(item, identity, display)
            continue

        if service_key in {"apigateway", "amazonapigateway"}:
            confirmed = _normalized(_confirmed_value(item, "api_type"))
            api_type = confirmed
            evidence = ""
            if not api_type:
                if re.search(r"websocket\s*(?:api)?", source, re.I):
                    api_type, evidence = "websocket", "WebSocket API"
                elif re.search(r"rest\s*api", source, re.I):
                    api_type, evidence = "rest", "REST API"
                elif re.search(r"http\s*api", source, re.I):
                    api_type, evidence = "http", "HTTP API"
                else:
                    api_type = _normalized(item.requirements.get("api_type"))
            identity, display = _API_GATEWAY_NAMES.get(
                api_type, ("amazon_api_gateway", "Amazon API Gateway")
            )
            if evidence and not confirmed:
                _set_customer_product_field(item, "api_type", api_type, evidence)
            _set_product_identity(item, identity, display)
            continue

        if service_key in {"msk", "amazonmsk"}:
            confirmed = _normalized(_confirmed_value(item, "cluster_type"))
            cluster_type = confirmed
            evidence = ""
            if not cluster_type:
                if "serverless" in folded:
                    cluster_type, evidence = "serverless", "Serverless"
                elif re.search(r"provisioned|预置容量", source, re.I):
                    cluster_type, evidence = "provisioned", "Provisioned"
                else:
                    cluster_type = _normalized(item.requirements.get("cluster_type"))
            identity, display = _MSK_CLUSTER_NAMES.get(cluster_type, ("amazon_msk", "Amazon MSK"))
            if evidence and not confirmed:
                _set_customer_product_field(item, "cluster_type", cluster_type, evidence)
            _set_product_identity(item, identity, display)
            continue

        if service_key in {"fsx", "amazonfsx"}:
            confirmed = _normalized(_confirmed_value(item, "file_system_type"))
            fsx_type = confirmed
            evidence = ""
            if not fsx_type:
                for candidate in ("openzfs", "ontap", "lustre", "windows"):
                    if candidate in folded:
                        fsx_type, evidence = candidate, candidate
                        break
                else:
                    fsx_type = _normalized(item.requirements.get("file_system_type"))
            identity, display = _FSX_TYPE_NAMES.get(fsx_type, ("amazon_fsx", "Amazon FSx"))
            if evidence and not confirmed:
                _set_customer_product_field(item, "file_system_type", fsx_type, evidence)
            _set_product_identity(item, identity, display)
            continue

        display_name = aurora_display_name(item)
        if display_name is None:
            # The component inventory intentionally groups RDS and Aurora under
            # one pricing family.  That internal grouping must never leak into
            # the customer-facing product name.  Ordinary engines remain RDS;
            # only an explicit Aurora engine/source is labelled Aurora.
            if "rds" in service_key or service_key in {"aurora", "amazonaurora"}:
                confirmed_engine = _normalized(_confirmed_value(item, "engine"))
                if confirmed_engine:
                    item.requirements["engine"] = confirmed_engine
                elif "aurora" not in folded:
                    explicit_engines = (
                        ("postgresql", r"postgres(?:ql)?"),
                        ("mariadb", r"mariadb"),
                        ("mysql", r"mysql"),
                    )
                    for engine_name, pattern in explicit_engines:
                        if re.search(pattern, source, re.I):
                            _set_customer_product_field(item, "engine", engine_name, engine_name)
                            break
                engine = _normalized(item.requirements.get("engine"))
                display = _RDS_ENGINE_NAMES.get(engine, "Amazon RDS")
                identity = f"rds_{engine}" if engine else "amazon_rds"
                _set_product_identity(item, identity, display)
                item.requirements.pop("aurora_cluster", None)
                model_match = re.search(
                    r"(?<![a-z0-9.])(db\.[a-z0-9][a-z0-9.-]*)(?![a-z0-9.])",
                    source,
                    re.I,
                )
                if model_match:
                    _set_customer_product_field(
                        item,
                        "requested_model",
                        model_match.group(1),
                        model_match.group(1),
                    )
                version_match = re.search(
                    r"(?:mysql|postgres(?:ql)?|mariadb)\s*(\d+(?:\.\d+){1,3}(?:[-.][a-z0-9.]+)?)",
                    source,
                    re.I,
                )
                if version_match:
                    _set_customer_product_field(
                        item,
                        "engine_version",
                        version_match.group(1),
                        version_match.group(0),
                    )
                deployment_match = re.search(
                    r"高可用(?:主备|多可用区)?(?:架构)?|主备(?:架构|部署)?|"
                    r"multi[-_ ]?az|多可用区(?:主备|部署)?",
                    source,
                    re.I,
                )
                if deployment_match:
                    _set_customer_product_field(
                        item,
                        "deployment",
                        "multi_az",
                        deployment_match.group(0),
                    )
                storage_patterns = (
                    r"(\d+(?:\.\d+)?)\s*(gib|gb|g|tib|tb|t)\s*(?:存储|硬盘|磁盘|盘)",
                    r"(?:存储|硬盘|磁盘|容量)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*"
                    r"(gib|gb|g|tib|tb|t)",
                    r"\d+(?:\.\d+)?\s*(?:核|c|vcpu)\s*"
                    r"\d+(?:\.\d+)?\s*(?:gib|gb|g)\s*[/＋+]\s*"
                    r"(\d+(?:\.\d+)?)\s*(gib|gb|g|tib|tb|t)",
                )
                storage_match = next(
                    (
                        match
                        for pattern in storage_patterns
                        if (match := re.search(pattern, source, re.I)) is not None
                    ),
                    None,
                )
                if storage_match:
                    storage = float(storage_match.group(1))
                    if storage_match.group(2).casefold() in {"tib", "tb", "t"}:
                        storage *= 1024
                    _set_customer_product_field(
                        item,
                        "storage_gib",
                        int(storage) if storage.is_integer() else storage,
                        storage_match.group(0),
                    )
            continue
        requirements = item.requirements
        aurora_engine = (
            "aurora_postgresql" if "postgres" in display_name.casefold() else "aurora_mysql"
        )
        if "aurora" in folded:
            _set_customer_product_field(item, "engine", aurora_engine, "Aurora")
        _set_product_identity(item, aurora_engine, display_name)
        requirements["aurora_cluster"] = True
        source = item.source_text or ""
        high_availability = bool(
            re.search(r"主备|高可用|multi[ -]?az|一主(?:一|两|二|三|\d+)读", source, re.I)
        )
        explicitly_single = bool(re.search(r"single[ -]?az|单可用区", source, re.I))
        if high_availability:
            requirements["deployment"] = "multi_az"
            requirements.pop("multi_az", None)
        elif explicitly_single:
            requirements["deployment"] = "single_az"

        member_match = re.search(
            r"(?:节点(?:数量)?|数据库实例(?:数量)?|实例(?:数量)?)\s*[:：]?\s*(\d+)",
            source,
            re.I,
        )
        if member_match:
            members = max(int(member_match.group(1)), 1)
            requirements["cluster_members"] = members
            item.field_evidence.setdefault("requirements.cluster_members", member_match.group(0))
            item.field_sources["requirements.cluster_members"] = "customer_text"
            item.locked_fields = sorted(set(item.locked_fields) | {"requirements.cluster_members"})
        elif high_availability and not requirements.get("cluster_members"):
            # Two members are the smallest topology that can satisfy an
            # explicit Aurora high-availability requirement.
            requirements["cluster_members"] = 2
            item.field_evidence["requirements.cluster_members"] = "system_minimum"
            item.field_sources["requirements.cluster_members"] = "system_minimum"

    # Product identity is the hard cache boundary. If deterministic recovery
    # changes it, every old review/catalog result belongs to another product
    # and must be discarded before the component can be displayed or priced.
    # Fields outside the new product contract are removed only when they were
    # system-generated; literal customer facts and later customer edits remain
    # authoritative and auditable.
    for item in intent.services:
        previous_service, previous_explicit_identity = original_boundaries[id(item)]
        current_service = _normalized(item.service)
        current_identity = customer_product_identity(item)
        review_service = _normalized(item.requirements.get("_review_service"))
        review_identity = _normalized(item.requirements.get("_review_product_identity"))
        boundary_changed = (
            previous_service != current_service
            or bool(
                previous_explicit_identity
                and previous_explicit_identity != _normalized(current_identity)
            )
        )
        review_boundary_mismatch = bool(
            (review_service and review_service != current_service)
            or (review_identity and review_identity != _normalized(current_identity))
        )
        if not boundary_changed and not review_boundary_mismatch:
            continue
        allowed_fields = set(requirement_fields(item.service))
        for field in tuple(item.requirements):
            path = f"requirements.{field}"
            source = item.field_sources.get(path, "")
            is_internal_result = field.startswith("_review_") or field.startswith("_quote_skip_")
            is_foreign_system_field = (
                not field.startswith("_")
                and field not in allowed_fields
                and source not in {*CUSTOMER_AUTHORITATIVE_SOURCES, "customer_text"}
            )
            if not (is_internal_result or is_foreign_system_field):
                continue
            item.requirements.pop(field, None)
            item.field_sources.pop(path, None)
            item.field_evidence.pop(path, None)
            item.locked_fields = [entry for entry in item.locked_fields if entry != path]

    # A named VPC block may mention EC2/API workloads as resources it carries.
    # Those relationship words do not declare new products. Remove only the
    # generated fragment immediately following that VPC and only when the
    # fragment contains no customer sizing/model/quantity evidence.
    filtered: list[ServiceRequirement] = []
    for item in intent.services:
        service_key = _normalized(item.service)
        source = (item.source_text or "").strip()
        previous_is_vpc = bool(filtered and _normalized(filtered[-1].service) == "vpc")
        relationship_fragment = bool(
            re.match(r"^(?:承载|用于|基于|关联|依赖)", _without_sales_number(source), re.I)
        )
        has_compute_request = bool(
            re.search(
                r"\b[a-z]+\d+[a-z]*\.[a-z0-9]+\b|\d+\s*(?:核|c)(?![a-z])|\d+(?:\.\d+)?\s*(?:gib|gb)\b|数量\s*[:：]?\s*\d+",
                source,
                re.I,
            )
        )
        if (
            service_key == "ec2"
            and previous_is_vpc
            and relationship_fragment
            and not has_compute_request
        ):
            continue

        # Public-VPC wording such as "API 服务公网入口" describes traffic
        # carried by that VPC. It is not an API Gateway declaration. This
        # guard is deliberately limited to an identical VPC-owned source so
        # genuinely requested API Gateway components remain untouched.
        if service_key in {"apigateway", "amazonapigateway"}:
            same_source_vpc = any(
                _normalized(existing.service) == "vpc"
                and _without_sales_number(existing.source_text).casefold()
                == _without_sales_number(source).casefold()
                for existing in intent.services
            )
            explicitly_named = bool(
                re.search(r"(?:amazon\s+)?api\s*gateway|api\s*网关|接口网关", source, re.I)
            )
            if same_source_vpc and not explicitly_named:
                continue
        filtered.append(item)

        # Old drafts may contain only the repaired WAF half. Add the ALB half
        # once, adjacent to its sibling, with an independent requirement map.
        source_heading = re.split(r"[：:]", _without_sales_number(source), maxsplit=1)[0]
        explicit_waf_alb = bool(
            re.search(r"(?=.*\bwaf\b)(?=.*\balb\b)", source_heading, re.I)
            or re.search(
                r"waf\s*[+＋/&和与]\s*(?:application\s+load\s+balancer|负载均衡)",
                source_heading,
                re.I,
            )
        )
        if service_key == "waf" and explicit_waf_alb:
            same_source_alb = any(
                _normalized(candidate.service)
                in {"elb", "elbv2", "alb", "elasticloadbalancing"}
                and _without_sales_number(candidate.source_text).casefold()
                == _without_sales_number(source).casefold()
                for candidate in intent.services
            )
            if not same_source_alb:
                alb = item.model_copy(deep=True)
                alb.service = "elb"
                alb.product_identity = "application_load_balancer"
                alb.calculator_service_name = "Application Load Balancer"
                alb.requirements = {
                    "load_balancer_type": "application",
                }
                alb.field_sources = {
                    "requirements.load_balancer_type": "customer_text",
                }
                alb.field_evidence = {
                    "requirements.load_balancer_type": "ALB",
                }
                alb.locked_fields = ["requirements.load_balancer_type"]
                filtered.append(alb)

    # Recovery can temporarily produce both the model's shortened row and a
    # deterministic copy rebound to the complete numbered source. Keep the
    # numbered/original copy, not two billable versions of the same product.
    deduplicated: list[ServiceRequirement] = []
    for item in filtered:
        canonical_source = _without_sales_number(item.source_text).casefold()
        identity = customer_product_identity(item)
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(deduplicated)
                if customer_product_identity(existing) == identity
                and _without_sales_number(existing.source_text).casefold() == canonical_source
                and (
                    existing.source_text.strip() == item.source_text.strip()
                    or bool(re.match(r"^\s*\d{1,3}\s*[、,，.．。:：;；\-—]", existing.source_text))
                    != bool(re.match(r"^\s*\d{1,3}\s*[、,，.．。:：;；\-—]", item.source_text))
                )
            ),
            None,
        )
        if duplicate_index is None:
            deduplicated.append(item)
            continue
        existing = deduplicated[duplicate_index]
        item_is_numbered = bool(
            re.match(r"^\s*\d{1,3}\s*[、,，.．。:：;；\-—]", item.source_text)
        )
        existing_is_numbered = bool(
            re.match(r"^\s*\d{1,3}\s*[、,，.．。:：;；\-—]", existing.source_text)
        )
        if item_is_numbered and not existing_is_numbered:
            deduplicated[duplicate_index] = item
    intent.services = deduplicated
    ensure_component_keys(intent)

    # A storage line that exists solely because it is the explicitly named
    # CloudFront origin is a separately priced child, not an unrelated S3
    # workload. Bind that relationship once here so review, quote and later
    # billing all show the same parent/child structure.
    cloudfront_items = [
        item
        for item in intent.services
        if _normalized(item.service) in {"cloudfront", "amazoncloudfront"}
    ]
    for storage in intent.services:
        if _normalized(storage.service) not in {"s3", "amazons3"}:
            continue
        source = storage.source_text or ""
        if not re.search(r"源站|origin", source, re.I) or not re.search(
            r"cloudfront|cdn|加速", source, re.I
        ):
            continue
        storage_source = canonical_component_source(source)
        parent = next(
            (
                candidate
                for candidate in cloudfront_items
                if canonical_component_source(candidate.source_text) == storage_source
            ),
            None,
        )
        if parent is None:
            continue
        storage.derived_from_service = "cloudfront"
        storage.parent_component_key = parent.component_key
    enforce_component_integrity(intent)


def preserve_service_configuration(requirement: ServiceRequirement) -> None:
    """Apply the same product-identity guard to one repaired component."""

    preserve_customer_configuration(
        ParsedIntent(customer_summary="", services=[requirement], ambiguities=[])
    )


def restore_customer_authority(
    original: ServiceRequirement,
    revised: ServiceRequirement,
) -> ServiceRequirement:
    """Keep every earlier customer edit across every automated revision.

    AI cleanup and official-catalog repair are allowed to enrich a component,
    but they are never allowed to erase or replace a value entered in the customer form.
    An explicit empty value is represented by ``customer_confirmation_removed``
    so replaying the original sales text cannot resurrect the deleted field.
    """

    restored = revised.model_copy(deep=True)
    # Identity and lineage are not AI-editable fields.  Without this guard an
    # otherwise valid component rewrite can lose its parent binding and later
    # be regenerated as a second billable row.
    restored.component_key = original.component_key
    restored.parent_component_key = original.parent_component_key
    restored.derived_from_service = original.derived_from_service
    locked = set(restored.locked_fields)

    for field in ("region", "quantity", "hours_per_month"):
        source = original.field_sources.get(field)
        if source not in CUSTOMER_AUTHORITATIVE_SOURCES:
            continue
        setattr(restored, field, getattr(original, field))
        restored.field_sources[field] = source
        if field in original.field_evidence:
            restored.field_evidence[field] = original.field_evidence[field]
        locked.add(field)

    for path, source in original.field_sources.items():
        if not path.startswith("requirements."):
            continue
        if source not in CUSTOMER_AUTHORITATIVE_SOURCES:
            continue
        field = path.split(".", 1)[1]
        if source == "customer_confirmation_removed":
            restored.requirements.pop(field, None)
        elif field in original.requirements:
            restored.requirements[field] = original.requirements[field]
        restored.field_sources[path] = source
        if path in original.field_evidence:
            restored.field_evidence[path] = original.field_evidence[path]
        locked.add(path)

    restored.locked_fields = sorted(locked)
    return restored
