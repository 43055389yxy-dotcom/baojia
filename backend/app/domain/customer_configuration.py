from __future__ import annotations

import re

from app.domain.models import ParsedIntent, ServiceRequirement

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
    requirement.requirements[field] = value
    requirement.field_sources[path] = "customer_text"
    requirement.field_evidence[path] = evidence
    requirement.locked_fields = sorted(set(requirement.locked_fields) | {path})


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

    for item in intent.services:
        service_key = _normalized(item.service)
        source = item.source_text or ""
        folded = source.casefold()

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
            identity, display = _CACHE_ENGINE_NAMES.get(
                engine, _CACHE_ENGINE_NAMES["redis"]
            )
            if not confirmed and any(marker in folded for marker in ("redis", "valkey", "memcached")):
                _set_customer_product_field(item, "engine", engine, engine)
            _set_product_identity(item, identity, display)
            continue

        if service_key in {"elb", "elbv2", "alb", "nlb", "gwlb", "elasticloadbalancing"} or "loadbalanc" in service_key:
            confirmed = _normalized(_confirmed_value(item, "load_balancer_type"))
            lb_type = confirmed
            evidence = ""
            if not lb_type:
                if re.search(r"\b(?:gwlb|gateway\s+load\s+balancer)\b|网关型负载均衡", source, re.I):
                    lb_type, evidence = "gateway", "GWLB"
                elif re.search(r"\b(?:nlb|network\s+load\s+balancer)\b|网络型负载均衡", source, re.I):
                    lb_type, evidence = "network", "NLB"
                elif re.search(r"\b(?:alb|application\s+load\s+balancer)\b|应用型负载均衡|公网负载均衡", source, re.I):
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
            identity, display = _MQ_ENGINE_NAMES.get(
                engine, ("amazon_mq", "Amazon MQ")
            )
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
            identity, display = _MSK_CLUSTER_NAMES.get(
                cluster_type, ("amazon_msk", "Amazon MSK")
            )
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
            identity, display = _FSX_TYPE_NAMES.get(
                fsx_type, ("amazon_fsx", "Amazon FSx")
            )
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
                            _set_customer_product_field(
                                item, "engine", engine_name, engine_name
                            )
                            break
                engine = _normalized(item.requirements.get("engine"))
                display = _RDS_ENGINE_NAMES.get(engine, "Amazon RDS")
                identity = f"rds_{engine}" if engine else "amazon_rds"
                _set_product_identity(item, identity, display)
                item.requirements.pop("aurora_cluster", None)
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
            item.field_evidence.setdefault(
                "requirements.cluster_members", member_match.group(0)
            )
            item.field_sources["requirements.cluster_members"] = "customer_text"
            item.locked_fields = sorted(
                set(item.locked_fields) | {"requirements.cluster_members"}
            )
        elif high_availability and not requirements.get("cluster_members"):
            # Two members are the smallest topology that can satisfy an
            # explicit Aurora high-availability requirement.
            requirements["cluster_members"] = 2
            item.field_evidence[
                "requirements.cluster_members"
            ] = "system_minimum"
            item.field_sources[
                "requirements.cluster_members"
            ] = "system_minimum"


def preserve_service_configuration(requirement: ServiceRequirement) -> None:
    """Apply the same product-identity guard to one repaired component."""

    preserve_customer_configuration(
        ParsedIntent(customer_summary="", services=[requirement], ambiguities=[])
    )
