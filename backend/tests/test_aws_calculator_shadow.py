from __future__ import annotations

import pytest

from app.domain.quote_compiler import (
    QuoteCompilation,
    RequirementFactIR,
    RequirementIR,
    ResourceIR,
)
from app.integrations.aws_calculator_binding import CompiledCalculatorConfiguration
from app.integrations.aws_calculator_contracts import CalculatorServiceContract
from app.integrations.aws_calculator_shadow import (
    AwsCalculatorShadowVerifier,
    CalculatorShadowAudit,
    CalculatorShadowComponentAudit,
    calculator_service_code,
)
from app.services.plugins.base import PluginRegistry
from app.services.quote_service import QuoteService


def _requirement(
    service: str,
    *,
    product_identity: str | None = None,
    fact_path: str = "quantity",
    fact_value: object = 2,
) -> RequirementIR:
    return RequirementIR(
        component_id="0",
        component_key="component-one",
        service_intent=service,
        product_identity=product_identity,
        region="ap-northeast-1",
        facts=(
            RequirementFactIR(
                fact_id="fact-00000001",
                path=fact_path,
                value=fact_value,
                unit=None,
                scope="component_total",
                source_kind="customer_text",
                evidence="must never be sent to the shadow mapper",
            ),
        ),
        evidence_fingerprint="f" * 64,
    )


def _resource(service: str, *, model: str) -> ResourceIR:
    return ResourceIR(
        component_id="0",
        service=service,
        region="ap-northeast-1",
        model=model,
        quantity=2,
        pricing_status="priced",
    )


@pytest.mark.parametrize(
    ("requirement", "resource", "expected"),
    [
        (_requirement("ec2"), _resource("ec2", model="c7g.large"), "ec2Enhancement"),
        (
            _requirement("rds", fact_path="requirements.engine", fact_value="mysql"),
            _resource("rds", model="db.r7g.large"),
            "amazonRDSMySQLDB",
        ),
        (
            _requirement("rds", fact_path="requirements.engine", fact_value="postgresql"),
            _resource("rds", model="db.r7g.large"),
            "amazonRDSPostgreSQLDB",
        ),
        (
            _requirement("redis"),
            _resource("redis", model="cache.r7g.large"),
            "amazonElastiCache",
        ),
        (
            _requirement(
                "s3", fact_path="requirements.storage_class", fact_value="standard"
            ),
            _resource("s3", model="S3 Standard"),
            "amazonS3Standard",
        ),
        (
            _requirement(
                "elb",
                product_identity="application_load_balancer",
                fact_path="requirements.load_balancer_type",
                fact_value="application",
            ),
            _resource("elb", model="Application Load Balancer"),
            "applicationLoadBalancer",
        ),
    ],
)
def test_five_primary_components_resolve_to_official_calculator_service_codes(
    requirement: RequirementIR,
    resource: ResourceIR,
    expected: str,
) -> None:
    assert calculator_service_code(requirement, resource) == expected


def test_non_standard_s3_is_not_silently_forced_into_standard_contract() -> None:
    requirement = _requirement(
        "s3", fact_path="requirements.storage_class", fact_value="standard_ia"
    )

    assert calculator_service_code(requirement, _resource("s3", model="S3")) is None


class _Catalog:
    def get_contract(self, service_code: str) -> CalculatorServiceContract:
        return CalculatorServiceContract(
            service_code=service_code,
            name=service_code,
            definition_url=f"https://official.example/{service_code}.json",
            schema_hash="a" * 64,
            cache_status="fresh",
        )


class _Planner:
    def __init__(self) -> None:
        self.calls: list[tuple[RequirementIR, ResourceIR, CalculatorServiceContract]] = []

    async def plan(
        self,
        *,
        requirement: RequirementIR,
        resource: ResourceIR,
        contract: CalculatorServiceContract,
        required_fact_ids: tuple[str, ...] = (),
        required_resource_paths: tuple[str, ...] = (),
        identity_fact_ids: tuple[str, ...] = (),
    ) -> CompiledCalculatorConfiguration:
        self.calls.append((requirement, resource, contract))
        return CompiledCalculatorConfiguration(
            component_id=requirement.component_id,
            service_code=contract.service_code,
            template_id="template",
            schema_hash=contract.schema_hash,
            configuration={"region": requirement.region},
            bindings=(),
            consumed_fact_ids=(),
            unconsumed_fact_ids=tuple(fact.fact_id for fact in requirement.facts),
        )


@pytest.mark.asyncio
async def test_shadow_verifier_uses_ir_only_and_returns_auditable_result() -> None:
    requirement = _requirement("ec2")
    resource = _resource("ec2", model="c7g.large")
    compilation = QuoteCompilation(
        requirements=(requirement,),
        resources=(resource,),
        usage=(),
        prices=(),
        total_cost=0,
        is_partial=True,
    )
    planner = _Planner()
    verifier = AwsCalculatorShadowVerifier(_Catalog(), planner)

    audit = await verifier.verify(compilation)

    assert audit.status == "validated"
    assert audit.validated_component_count == 1
    assert audit.components[0].service_code == "ec2Enhancement"
    assert audit.components[0].schema_hash == "a" * 64
    assert planner.calls[0][0] is requirement
    assert planner.calls[0][1] is resource


@pytest.mark.asyncio
async def test_shadow_verifier_records_failure_without_fabricating_success() -> None:
    class _FailingPlanner(_Planner):
        async def plan(self, **kwargs: object) -> CompiledCalculatorConfiguration:
            raise ValueError("official field mapping failed")

    compilation = QuoteCompilation(
        requirements=(_requirement("ec2"),),
        resources=(_resource("ec2", model="c7g.large"),),
        usage=(),
        prices=(),
        total_cost=0,
        is_partial=True,
    )

    audit = await AwsCalculatorShadowVerifier(_Catalog(), _FailingPlanner()).verify(
        compilation
    )

    assert audit.status == "failed"
    assert audit.validated_component_count == 0
    assert audit.components[0].status == "failed"
    assert "official field mapping failed" in (audit.components[0].reason or "")


def test_shadow_module_never_reads_customer_prose() -> None:
    source = __import__("inspect").getsource(
        __import__("app.integrations.aws_calculator_shadow", fromlist=["*"])
    )

    assert "source_text" not in source
    assert "original_source_text" not in source


@pytest.mark.asyncio
async def test_quote_service_records_shadow_result_without_replacing_formal_price_ir() -> None:
    class _Verifier:
        async def verify(self, compilation: QuoteCompilation) -> CalculatorShadowAudit:
            assert compilation.total_cost == 12.5
            return CalculatorShadowAudit(
                status="validated",
                attempted_component_count=1,
                validated_component_count=1,
                failed_component_count=0,
                skipped_component_count=0,
                components=(
                    CalculatorShadowComponentAudit(
                        component_id="0",
                        service_intent="ec2",
                        service_code="ec2Enhancement",
                        status="validated",
                    ),
                ),
            )

    compilation = QuoteCompilation(
        requirements=(_requirement("ec2"),),
        resources=(_resource("ec2", model="c7g.large"),),
        usage=(),
        prices=(),
        total_cost=12.5,
        is_partial=True,
    )
    service = QuoteService(
        object(),
        PluginRegistry(),
        object(),
        calculator_shadow_verifier=_Verifier(),
    )
    trace = []

    audit = await service._run_calculator_contract_shadow(compilation, trace, None)

    assert audit["status"] == "validated"
    assert compilation.total_cost == 12.5
    assert "1/1" in trace[-1].message
