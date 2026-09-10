from __future__ import annotations

import pytest

from app.domain.cleaned_input import (
    CLEANED_INPUT_POLICY_VERSION,
    intent_is_cleaned_only,
)
from app.domain.structured_intake import (
    StructuredFieldContract,
    StructuredIntakeCompiler,
    StructuredIntakeViolation,
    StructuredMinimumUnitPolicy,
    StructuredServiceContract,
)

SCHEMA_HASH = "a" * 64


def _field(
    value_type: str,
    *,
    unit: str | None = None,
    allowed_values: tuple[str, ...] = (),
    minimum: float | None = 0,
) -> StructuredFieldContract:
    return StructuredFieldContract(
        value_type=value_type,
        unit=unit,
        allowed_values=allowed_values,
        minimum=minimum,
    )


def _contract(
    service: str,
    fields: dict[str, StructuredFieldContract],
    *,
    contract_id: str | None = None,
    minimum_unit_policy: StructuredMinimumUnitPolicy | None = None,
) -> StructuredServiceContract:
    return StructuredServiceContract(
        service=service,
        display_name=service,
        contract_id=contract_id or f"{service}-official-v1",
        schema_hash=SCHEMA_HASH,
        fields=fields,
        minimum_unit_policy=minimum_unit_policy,
    )


def _component(
    *,
    service: str,
    component_key: str,
    cleaned_source: str,
    source_block_key: str,
    facts: list[dict[str, object]],
    parent_component_key: str | None = None,
    contract_id: str | None = None,
) -> dict[str, object]:
    return {
        "component_key": component_key,
        "parent_component_key": parent_component_key,
        "service": service,
        "contract_id": contract_id or f"{service}-official-v1",
        "contract_schema_hash": SCHEMA_HASH,
        "cleaned_source": cleaned_source,
        "source_block_key": source_block_key,
        "facts": facts,
    }


def _fact(
    path: str,
    value: object,
    evidence: str,
    *,
    unit: str | None = None,
    scope: str = "component_total",
) -> dict[str, object]:
    return {
        "path": path,
        "value": value,
        "unit": unit,
        "scope": scope,
        "match_policy": "exact",
        "evidence": evidence,
    }


def test_alb_lcu_is_preserved_as_a_stable_server_owned_fact() -> None:
    contracts = {
        "elb": _contract(
            "elb",
            {
                "load_balancer_type": _field(
                    "string", allowed_values=("application", "network", "gateway")
                ),
                "lcu_count": _field("number", unit="LCU"),
            },
        )
    }
    payload = {
        "components": [
            _component(
                service="elb",
                component_key="cmp_alb_0001",
                cleaned_source=(
                    "Application Load Balancer｜类型：application｜"
                    "数量 1 个｜每个 ALB 平均 1 LCU"
                ),
                source_block_key="request-line-7",
                facts=[
                    _fact(
                        "requirements.load_balancer_type",
                        "application",
                        "类型：application",
                    ),
                    _fact("quantity", 1, "数量 1 个", unit="count"),
                    _fact(
                        "requirements.lcu_count",
                        1,
                        "每个 ALB 平均 1 LCU",
                        unit="LCU",
                        scope="per_resource",
                    ),
                ],
            )
        ]
    }

    first = StructuredIntakeCompiler(contracts).compile(payload)
    second = StructuredIntakeCompiler(contracts).compile(payload)
    requirement = first.intent.services[0]

    assert requirement.requirements["lcu_count"] == 1
    assert requirement.field_scopes["lcu_count"] == "per_resource"
    assert intent_is_cleaned_only(first.intent)
    assert requirement.original_source_text is None
    assert requirement.intake_source_fragments == []
    facts = {fact.path: fact for fact in requirement.customer_pricing_facts}
    assert facts["requirements.lcu_count"].unit == "LCU"
    assert facts["requirements.lcu_count"].fact_id == (
        {
            fact.path: fact
            for fact in second.intent.services[0].customer_pricing_facts
        }["requirements.lcu_count"].fact_id
    )
    assert {
        fact.path for fact in first.requirement_ir[0].facts
    } == {"quantity", "requirements.lcu_count"}


def test_ecr_storage_capacity_cannot_disappear() -> None:
    contracts = {"ecr": _contract("ecr", {"storage_gib": _field("number", unit="GiB")})}
    result = StructuredIntakeCompiler(contracts).compile(
        {
            "components": [
                _component(
                    service="ecr",
                    component_key="cmp_ecr_0001",
                    cleaned_source="Amazon ECR｜镜像存储：20 GB/月",
                    source_block_key="request-line-12",
                    facts=[
                        _fact(
                            "requirements.storage_gib",
                            20,
                            "镜像存储：20 GB",
                            unit="GiB",
                        )
                    ],
                )
            ]
        }
    )

    requirement = result.intent.services[0]
    assert requirement.requirements["storage_gib"] == 20
    assert requirement.customer_pricing_facts[0].path == "requirements.storage_gib"
    assert requirement.customer_pricing_facts[0].value == 20


def test_outbound_transfer_accepts_normalized_tib_value_and_keeps_lineage() -> None:
    contracts = {
        "data_transfer": _contract(
            "data_transfer",
            {"data_transfer_out_gib": _field("number", unit="GiB")},
        )
    }
    result = StructuredIntakeCompiler(contracts).compile(
        {
            "components": [
                _component(
                    service="data_transfer",
                    component_key="cmp_transfer_0001",
                    cleaned_source="AWS Data Transfer｜公网出站：2 TiB/月",
                    source_block_key="request-line-transfer",
                    facts=[
                        _fact(
                            "requirements.data_transfer_out_gib",
                            2048,
                            "公网出站：2 TiB",
                            unit="GiB",
                        )
                    ],
                )
            ]
        }
    )

    requirement = result.intent.services[0]
    assert requirement.requirements["data_transfer_out_gib"] == 2048
    assert result.requirement_ir[0].facts[0].unit == "GiB"


def test_cloudwatch_missing_volume_uses_server_minimum_reference_semantics() -> None:
    contracts = {
        "cloudwatch": _contract(
            "cloudwatch",
            {
                "include_logs": _field("boolean", minimum=None),
                "log_ingestion_gib": _field("number", unit="GiB"),
            },
            minimum_unit_policy=StructuredMinimumUnitPolicy(
                watched_fields=("log_ingestion_gib",),
                flag_field="reference_unit_only",
                message=(
                    "客户未提供 CloudWatch 日志写入量；"
                    "仅展示 AWS 官方最小单位参考价，不计入月费合计"
                ),
            ),
        )
    }
    result = StructuredIntakeCompiler(contracts).compile(
        {
            "components": [
                _component(
                    service="cloudwatch",
                    component_key="cmp_cloudwatch_0001",
                    cleaned_source="Amazon CloudWatch Logs｜采集容器及平台日志｜未提供月写入量",
                    source_block_key="request-line-13",
                    facts=[
                        _fact(
                            "requirements.include_logs",
                            True,
                            "CloudWatch Logs",
                        )
                    ],
                )
            ]
        }
    )

    requirement = result.intent.services[0]
    assert "log_ingestion_gib" not in requirement.requirements
    assert requirement.requirements["reference_unit_only"] is True
    assert requirement.field_sources["requirements.reference_unit_only"] == "system_minimum"
    assert requirement.customer_pricing_facts == []
    assert result.requirement_ir[0].facts == ()


def test_nat_fact_cannot_be_reused_as_a_second_vpc_charge() -> None:
    contracts = {
        "nat_gateway": _contract(
            "nat_gateway",
            {"data_processed_gib": _field("number", unit="GiB")},
        ),
        "vpc": _contract("vpc", {"vpc_count": _field("integer", unit="count")}),
    }
    shared_evidence = "NAT Gateway 数量 2 个"
    payload = {
        "components": [
            _component(
                service="nat_gateway",
                component_key="cmp_nat_0001",
                cleaned_source=f"{shared_evidence}｜月处理 200 GB",
                source_block_key="request-line-9",
                facts=[
                    _fact("quantity", 2, shared_evidence, unit="count"),
                    _fact(
                        "requirements.data_processed_gib",
                        200,
                        "月处理 200 GB",
                        unit="GiB",
                    ),
                ],
            ),
            _component(
                service="vpc",
                component_key="cmp_vpc_0001",
                cleaned_source=shared_evidence,
                source_block_key="request-line-9",
                facts=[
                    _fact(
                        "requirements.vpc_count",
                        2,
                        shared_evidence,
                        unit="count",
                    )
                ],
            ),
        ]
    }

    with pytest.raises(StructuredIntakeViolation, match="重复归属"):
        StructuredIntakeCompiler(contracts).compile(payload)


def test_parent_and_child_can_only_cite_their_own_cleaned_source() -> None:
    contracts = {
        "eks": _contract("eks", {}),
        "ec2": _contract("ec2", {"vcpu": _field("integer", unit="vCPU")}),
    }
    payload = {
        "components": [
            _component(
                service="eks",
                component_key="cmp_parent_0001",
                cleaned_source="Amazon EKS 控制面｜数量 1 个",
                source_block_key="request-line-1",
                facts=[_fact("quantity", 1, "数量 1 个", unit="count")],
            ),
            _component(
                service="ec2",
                component_key="cmp_child_0001",
                parent_component_key="cmp_parent_0001",
                cleaned_source="EKS 工作节点｜数量 4 台｜每节点 8 vCPU",
                source_block_key="request-line-1",
                facts=[
                    _fact("quantity", 4, "数量 4 台", unit="count"),
                    _fact(
                        "requirements.vcpu",
                        8,
                        "控制面｜数量 1 个",
                        unit="vCPU",
                        scope="per_node",
                    ),
                ],
            ),
        ]
    }

    with pytest.raises(StructuredIntakeViolation, match="不属于该组件"):
        StructuredIntakeCompiler(contracts).compile(payload)


def test_one_numeric_claim_cannot_own_two_fields() -> None:
    contracts = {
        "ecr": _contract(
            "ecr",
            {
                "storage_gib": _field("number", unit="GiB"),
                "backup_storage_gib": _field("number", unit="GiB"),
            },
        )
    }
    payload = {
        "components": [
            _component(
                service="ecr",
                component_key="cmp_ecr_0001",
                cleaned_source="Amazon ECR｜镜像存储 20 GB",
                source_block_key="request-line-12",
                facts=[
                    _fact("requirements.storage_gib", 20, "镜像存储 20 GB", unit="GiB"),
                    _fact(
                        "requirements.backup_storage_gib",
                        20,
                        "镜像存储 20 GB",
                        unit="GiB",
                    ),
                ],
            )
        ]
    }

    with pytest.raises(StructuredIntakeViolation, match="多个字段"):
        StructuredIntakeCompiler(contracts).compile(payload)


@pytest.mark.parametrize(
    ("fact", "message"),
    [
        (_fact("requirements.unknown", 20, "镜像存储 20 GB", unit="GiB"), "未在服务端字段合同"),
        (_fact("requirements.storage_gib", "20", "镜像存储 20 GB", unit="GiB"), "必须是 number"),
        (_fact("requirements.storage_gib", 20, "镜像存储 20 GB", unit="LCU"), "单位"),
        (_fact("requirements.storage_gib", 30, "镜像存储 20 GB", unit="GiB"), "数字证据"),
    ],
)
def test_field_contract_rejects_unknown_type_unit_and_numeric_mismatch(
    fact: dict[str, object], message: str
) -> None:
    contracts = {"ecr": _contract("ecr", {"storage_gib": _field("number", unit="GiB")})}
    payload = {
        "components": [
            _component(
                service="ecr",
                component_key="cmp_ecr_0001",
                cleaned_source="Amazon ECR｜镜像存储 20 GB",
                source_block_key="request-line-12",
                facts=[fact],
            )
        ]
    }

    with pytest.raises(StructuredIntakeViolation, match=message):
        StructuredIntakeCompiler(contracts).compile(payload)


def test_every_cleaned_numeric_atom_must_be_declared() -> None:
    contracts = {
        "elb": _contract(
            "elb",
            {"lcu_count": _field("number", unit="LCU")},
        )
    }
    payload = {
        "components": [
            _component(
                service="elb",
                component_key="cmp_alb_0001",
                cleaned_source="Application Load Balancer｜数量 1 个｜平均 1 LCU",
                source_block_key="request-line-7",
                facts=[_fact("quantity", 1, "数量 1 个", unit="count")],
            )
        ]
    }

    with pytest.raises(StructuredIntakeViolation, match="1 LCU"):
        StructuredIntakeCompiler(contracts).compile(payload)


@pytest.mark.parametrize(
    ("target", "forbidden_field"),
    [
        ("root", "customer_request"),
        ("component", "original_source_text"),
        ("component", "intake_source_fragments"),
        ("fact", "raw_customer_text"),
        ("fact", "fact_id"),
    ],
)
def test_raw_or_client_owned_identity_fields_are_rejected(
    target: str, forbidden_field: str
) -> None:
    contracts = {"ecr": _contract("ecr", {"storage_gib": _field("number", unit="GiB")})}
    component = _component(
        service="ecr",
        component_key="cmp_ecr_0001",
        cleaned_source="Amazon ECR｜镜像存储 20 GB",
        source_block_key="request-line-12",
        facts=[_fact("requirements.storage_gib", 20, "镜像存储 20 GB", unit="GiB")],
    )
    payload: dict[str, object] = {"components": [component]}
    if target == "root":
        payload[forbidden_field] = "raw"
    elif target == "component":
        component[forbidden_field] = "raw" if forbidden_field != "intake_source_fragments" else []
    else:
        component["facts"][0][forbidden_field] = "raw"  # type: ignore[index]

    with pytest.raises(StructuredIntakeViolation, match=forbidden_field):
        StructuredIntakeCompiler(contracts).compile(payload)


def test_cross_component_duplicate_fact_is_rejected_even_when_paths_differ() -> None:
    contracts = {
        "ec2": _contract("ec2", {"data_transfer_out_gib": _field("number", unit="GiB")}),
        "data_transfer": _contract(
            "data_transfer", {"data_transfer_out_gib": _field("number", unit="GiB")}
        ),
    }
    evidence = "公网出站 2 TiB/月"
    payload = {
        "components": [
            _component(
                service="ec2",
                component_key="cmp_ec2_0001",
                cleaned_source=f"Amazon EC2｜{evidence}",
                source_block_key="request-line-transfer",
                facts=[
                    _fact(
                        "requirements.data_transfer_out_gib",
                        2048,
                        evidence,
                        unit="GiB",
                    )
                ],
            ),
            _component(
                service="data_transfer",
                component_key="cmp_transfer_0001",
                cleaned_source=f"AWS Data Transfer｜{evidence}",
                source_block_key="request-line-transfer",
                facts=[
                    _fact(
                        "requirements.data_transfer_out_gib",
                        2048,
                        evidence,
                        unit="GiB",
                    )
                ],
            ),
        ]
    }

    with pytest.raises(StructuredIntakeViolation, match="跨组件重复归属"):
        StructuredIntakeCompiler(contracts).compile(payload)


def test_parent_graph_is_preserved_in_requirement_ir_without_raw_text() -> None:
    contracts = {
        "eks": _contract("eks", {}),
        "ec2": _contract("ec2", {"vcpu": _field("integer", unit="vCPU")}),
    }
    payload = {
        "components": [
            _component(
                service="eks",
                component_key="cmp_parent_0001",
                cleaned_source="Amazon EKS 控制面｜数量 1 个",
                source_block_key="request-line-1",
                facts=[_fact("quantity", 1, "数量 1 个", unit="count")],
            ),
            _component(
                service="ec2",
                component_key="cmp_child_0001",
                parent_component_key="cmp_parent_0001",
                cleaned_source="EKS 工作节点｜数量 4 台｜每节点 8 vCPU",
                source_block_key="request-line-1",
                facts=[
                    _fact("quantity", 4, "数量 4 台", unit="count"),
                    _fact(
                        "requirements.vcpu",
                        8,
                        "每节点 8 vCPU",
                        unit="vCPU",
                        scope="per_node",
                    ),
                ],
            ),
        ]
    }

    result = StructuredIntakeCompiler(contracts).compile(payload)

    assert result.requirement_ir[1].parent_component_key == "cmp_parent_0001"
    assert all(
        component.original_source_text is None
        and component.intake_source_fragments == []
        and component.field_sources["_source_retention_policy"]
        == CLEANED_INPUT_POLICY_VERSION
        for component in result.intent.services
    )
    assert all(
        component.original_source_text is None
        and component.intake_source_fragments == []
        for component in result.intent.services
    )


def test_same_internal_service_can_use_two_official_child_contracts() -> None:
    mysql = _contract(
        "rds",
        {"storage_gib": _field("number", unit="GiB")},
        contract_id="rds-mysql-official-v1",
    )
    aurora = _contract(
        "rds",
        {"cluster_members": _field("integer", unit="count")},
        contract_id="rds-aurora-official-v1",
    )
    compiler = StructuredIntakeCompiler(
        {
            mysql.contract_id: mysql,
            aurora.contract_id: aurora,
        }
    )

    result = compiler.compile(
        {
            "components": [
                _component(
                    service="rds",
                    component_key="cmp_rds_mysql",
                    cleaned_source="Amazon RDS MySQL｜存储 100 GB",
                    source_block_key="request-rds-mysql",
                    contract_id=mysql.contract_id,
                    facts=[
                        _fact(
                            "requirements.storage_gib",
                            100,
                            "存储 100 GB",
                            unit="GiB",
                        )
                    ],
                ),
                _component(
                    service="rds",
                    component_key="cmp_rds_aurora",
                    cleaned_source="Amazon Aurora｜集群成员 3 个",
                    source_block_key="request-rds-aurora",
                    contract_id=aurora.contract_id,
                    facts=[
                        _fact(
                            "requirements.cluster_members",
                            3,
                            "集群成员 3 个",
                            unit="count",
                        )
                    ],
                ),
            ]
        }
    )

    assert [item.service for item in result.intent.services] == ["rds", "rds"]
    assert [
        item.field_sources["_structured_contract_id"]
        for item in result.intent.services
    ] == [mysql.contract_id, aurora.contract_id]
