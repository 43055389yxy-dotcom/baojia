from app.domain.billing_rules import coalesce, constant, number, product, reconcile, sum_of
from app.integrations.aws_component_templates.base import (
    BillingOutput,
    ComponentTemplateSpec,
    OfficialSource,
    TemplateField,
)

TOPOLOGY_NODES = product(
    number("quantity", minimum=1, integer=True),
    number("shards", default=1, minimum=1, integer=True),
    sum_of(constant(1), number("replicas_per_shard", default=0, integer=True)),
)
TOTAL_NODES = coalesce(
    reconcile(
        number("node_count", minimum=1, integer=True),
        TOPOLOGY_NODES.when_complete("shards", "replicas_per_shard"),
    ),
    TOPOLOGY_NODES,
)

TEMPLATE = ComponentTemplateSpec(
    service_key="elasticache",
    display_name="Amazon ElastiCache（Redis / Valkey）",
    aliases=("redis", "valkey", "amazon_elasticache", "elasticache_redis"),
    primary_variants=("redis", "valkey"),
    official_sources=(
        OfficialSource(
            "Amazon ElastiCache pricing",
            "https://aws.amazon.com/elasticache/pricing/",
            "节点小时、Serverless、备份与数据传输计费",
        ),
        OfficialSource(
            "ElastiCache supported node types",
            "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/CacheNodes.SupportedTypes.html",
            "cache.* 节点类型与规格",
        ),
        OfficialSource(
            "Replication groups for Redis OSS",
            "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Replication.html",
            "分片、主节点与副本拓扑",
        ),
    ),
    fields=(
        TemplateField("requested_model", "string", "客户指定的 cache.* 节点类型"),
        TemplateField(
            "engine", "string", "缓存引擎", allowed_values=("redis", "valkey", "memcached")
        ),
        TemplateField("vcpu", "number", "客户要求的最低节点 vCPU", unit="vCPU/node"),
        TemplateField("memory_gib", "number", "客户要求的最低节点内存", unit="GiB/node"),
        TemplateField(
            "shards", "integer", "每套复制组的分片数", role="usage", unit="shards/deployment"
        ),
        TemplateField(
            "replicas_per_shard",
            "integer",
            "每个分片的只读副本数，不含主节点",
            role="usage",
            unit="replicas/shard",
        ),
        TemplateField(
            "node_count", "integer", "本组件全部缓存节点总数", role="usage", unit="nodes/component"
        ),
        TemplateField("cluster_mode", "string", "集群模式", allowed_values=("enabled", "disabled")),
        TemplateField("data_tiering", "boolean", "是否使用支持数据分层的节点"),
        TemplateField(
            "source_storage_gib_per_node",
            "number",
            "迁移或调整大小时每节点源数据容量",
            unit="GiB/node",
        ),
        TemplateField(
            "backup_retention_days", "integer", "自动备份保留天数", role="usage", unit="days"
        ),
        TemplateField(
            "purchase_option", "string", "购买方式", allowed_values=("on_demand", "reserved")
        ),
        TemplateField("reserved_term_years", "integer", "预留节点期限", allowed_values=("1", "3")),
        TemplateField(
            "payment_option",
            "string",
            "预留付款方式",
            allowed_values=("no_upfront", "partial_upfront", "all_upfront"),
        ),
        TemplateField("utilization_percent", "number", "运行利用率", role="usage", unit="percent"),
    ),
    billing_outputs=(
        BillingOutput(
            "cache_node_hours",
            "ElastiCache Node Hours",
            (
                "quantity",
                "hours_per_month",
                "requested_model",
                "engine",
                "vcpu",
                "memory_gib",
                "shards",
                "replicas_per_shard",
                "node_count",
                "purchase_option",
                "utilization_percent",
            ),
            "node_count × hours_per_month；未给 node_count 时 quantity × shards × "
            "(1 + replicas_per_shard) × hours_per_month",
            calculation=product(TOTAL_NODES, number("hours_per_month")),
            unit="Hours",
        ),
        BillingOutput(
            "backup_storage",
            "ElastiCache Backup GB-Month",
            ("quantity", "node_count", "source_storage_gib_per_node", "backup_retention_days"),
            "仅在客户事实足够且超出免费备份配额时生成",
        ),
    ),
    safe_defaults={"backup_retention_days": 0},
    # Engine patch versions do not select a different node-hour SKU. Keep the
    # compatibility discard rule beside Redis rather than in a shared file.
    non_pricing_fields=frozenset({"engine_version"}),
    critical_rule=(
        "cache.* 写 requested_model；每节点内存、分片数、副本数与总节点数分别填写。"
        "8GB×3节点不是3个分片；一主一从是 shards=1、replicas_per_shard=1。"
    ),
    guidance="""
Redis/Valkey 是引擎身份，Amazon ElastiCache 是产品身份。“一主 N 从”写 shards=1、
replicas_per_shard=N；没有出现分片语义时，不得把节点总数写成 shards。node_count 是整个当前组件的
节点总数，不能再乘 quantity。客户只给内存而没给 cache.* 型号时保留 memory_gib，由官方目录选型。
""",
    example_customer_text="东京 ElastiCache for Redis 1 套，一主两从，每节点 8 GiB，全天运行。",
)
