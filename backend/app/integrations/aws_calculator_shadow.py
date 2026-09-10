from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.domain.quote_compiler import QuoteCompilation, RequirementIR, ResourceIR
from app.integrations.aws_calculator_binding import (
    AwsCalculatorConfigurationPlanner,
    CompiledCalculatorConfiguration,
)
from app.integrations.aws_calculator_contracts import (
    AwsCalculatorContractCatalog,
    CalculatorServiceContract,
)


class CalculatorContractReader(Protocol):
    def get_contract(self, service: str) -> CalculatorServiceContract: ...


class CalculatorConfigurationPlanner(Protocol):
    async def plan(
        self,
        *,
        requirement: RequirementIR,
        resource: ResourceIR,
        contract: CalculatorServiceContract,
        required_fact_ids: tuple[str, ...] = (),
        required_resource_paths: tuple[str, ...] = (),
        identity_fact_ids: tuple[str, ...] = (),
    ) -> CompiledCalculatorConfiguration: ...


class CalculatorShadowComponentAudit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    component_id: str
    service_intent: str
    service_code: str | None = None
    status: str
    schema_hash: str | None = None
    configuration_fingerprint: str | None = None
    bound_field_ids: tuple[str, ...] = ()
    consumed_fact_ids: tuple[str, ...] = ()
    identity_fact_ids: tuple[str, ...] = ()
    unconsumed_fact_ids: tuple[str, ...] = ()
    consumed_resource_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    reason: str | None = None


class CalculatorShadowAudit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: str = "official_calculator_contract_shadow"
    published_pricing_authority: str = "aws_bcm_price_ir"
    price_comparison_status: str = "not_run"
    promotion_ready: bool = False
    status: str
    attempted_component_count: int
    validated_component_count: int
    failed_component_count: int
    skipped_component_count: int
    components: tuple[CalculatorShadowComponentAudit, ...]


def _fact_value(requirement: RequirementIR, path: str) -> object | None:
    matches = [fact.value for fact in requirement.facts if fact.path == path]
    return matches[0] if len(matches) == 1 else None


def calculator_service_code(
    requirement: RequirementIR,
    resource: ResourceIR,
) -> str | None:
    """Resolve only official product identity; field mapping stays dynamic.

    The resolver consumes typed IR identities and fields. It deliberately does
    not inspect human wording, display labels, evidence, or compatibility text.
    Unknown variants remain unsupported instead of being coerced into a nearby
    Calculator template.
    """

    service = requirement.service_intent.casefold()
    if resource.service.casefold() != service:
        return None
    if service == "ec2":
        return "ec2Enhancement"
    if service == "rds":
        engine = str(_fact_value(requirement, "requirements.engine") or "").casefold()
        if engine == "mysql":
            return "amazonRDSMySQLDB"
        if engine == "postgresql":
            return "amazonRDSPostgreSQLDB"
        return None
    if service in {"redis", "elasticache"}:
        return "amazonElastiCache"
    if service == "s3":
        storage_class = str(
            _fact_value(requirement, "requirements.storage_class") or ""
        ).casefold()
        if storage_class == "standard":
            return "amazonS3Standard"
        return None
    if service == "elb":
        load_balancer_type = str(
            _fact_value(requirement, "requirements.load_balancer_type") or ""
        ).casefold()
        product_identity = str(requirement.product_identity or "").casefold()
        if load_balancer_type == "application" or product_identity in {
            "alb",
            "application_load_balancer",
        }:
            return "applicationLoadBalancer"
        return None
    return None


def _service_identity_fact_ids(requirement: RequirementIR) -> tuple[str, ...]:
    identity_paths = {
        "rds": {"requirements.engine"},
        "s3": {"requirements.storage_class"},
        "elb": {"requirements.load_balancer_type"},
    }.get(requirement.service_intent.casefold(), set())
    return tuple(
        sorted(fact.fact_id for fact in requirement.facts if fact.path in identity_paths)
    )


def _fingerprint(configuration: dict[str, object]) -> str:
    payload = json.dumps(
        configuration,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _contract_has_resource_quantity(contract: CalculatorServiceContract) -> bool:
    for template in contract.templates:
        for field in template.fields:
            identity = "".join(
                character
                for character in f"{field.field_id} {field.label or ''}".casefold()
                if character.isalnum()
            )
            if "workload" in identity or identity.startswith("numberof"):
                return True
            if field.field_type == "columnFormIPM" and any(
                "node"
                in "".join(
                    character
                    for character in f"{row.selector_id or ''} {row.label or ''}".casefold()
                    if character.isalnum()
                )
                for row in field.row_fields
            ):
                return True
    return False


class AwsCalculatorShadowVerifier:
    """Validate ResourceIR against live Calculator fields without changing price.

    BCM/Price List remain the published price oracle. This second path proves
    that the same immutable requirements and selected resource can populate an
    official Calculator schema. Its result is audit metadata only, so a shadow
    failure can never silently replace, add, or remove a billing line.
    """

    def __init__(
        self,
        catalog: CalculatorContractReader,
        planner: CalculatorConfigurationPlanner,
    ) -> None:
        self._catalog = catalog
        self._planner = planner
        self._cache: dict[str, CalculatorShadowComponentAudit] = {}

    async def verify(self, compilation: QuoteCompilation) -> CalculatorShadowAudit:
        resources = {resource.component_id: resource for resource in compilation.resources}
        components = await asyncio.gather(
            *(
                self._verify_component(requirement, resources.get(requirement.component_id))
                for requirement in compilation.requirements
            )
        )
        attempted = sum(component.status != "skipped" for component in components)
        validated = sum(component.status == "validated" for component in components)
        failed = sum(component.status == "failed" for component in components)
        skipped = sum(component.status == "skipped" for component in components)
        if attempted == 0:
            status = "not_applicable"
        elif failed == 0:
            status = "validated"
        elif validated:
            status = "partial"
        else:
            status = "failed"
        return CalculatorShadowAudit(
            status=status,
            attempted_component_count=attempted,
            validated_component_count=validated,
            failed_component_count=failed,
            skipped_component_count=skipped,
            components=tuple(components),
        )

    async def _verify_component(
        self,
        requirement: RequirementIR,
        resource: ResourceIR | None,
    ) -> CalculatorShadowComponentAudit:
        if resource is None:
            return CalculatorShadowComponentAudit(
                component_id=requirement.component_id,
                service_intent=requirement.service_intent,
                status="failed",
                reason="ResourceIR is missing for this RequirementIR component",
            )
        service_code = calculator_service_code(requirement, resource)
        if service_code is None:
            return CalculatorShadowComponentAudit(
                component_id=requirement.component_id,
                service_intent=requirement.service_intent,
                status="skipped",
                reason="no exact official Calculator service identity for this product variant",
            )
        cache_key = self._cache_key(requirement, resource, service_code)
        if cached := self._cache.get(cache_key):
            return cached
        try:
            contract = await asyncio.to_thread(self._catalog.get_contract, service_code)
            required_quantity_fact_ids = (
                tuple(fact.fact_id for fact in requirement.facts if fact.path == "quantity")
                if _contract_has_resource_quantity(contract)
                else ()
            )
            compiled = await self._planner.plan(
                requirement=requirement,
                resource=resource,
                contract=contract,
                required_fact_ids=required_quantity_fact_ids,
                required_resource_paths=(
                    ("model",)
                    if requirement.service_intent.casefold()
                    in {"ec2", "rds", "redis", "elasticache"}
                    else ()
                ),
                identity_fact_ids=_service_identity_fact_ids(requirement),
            )
            result = CalculatorShadowComponentAudit(
                component_id=requirement.component_id,
                service_intent=requirement.service_intent,
                service_code=service_code,
                status="validated",
                schema_hash=compiled.schema_hash,
                configuration_fingerprint=_fingerprint(compiled.configuration),
                bound_field_ids=tuple(sorted(binding.field_id for binding in compiled.bindings)),
                consumed_fact_ids=compiled.consumed_fact_ids,
                identity_fact_ids=_service_identity_fact_ids(requirement),
                unconsumed_fact_ids=compiled.unconsumed_fact_ids,
                consumed_resource_paths=compiled.consumed_resource_paths,
                warnings=compiled.validation_warnings,
            )
        except Exception as exc:  # noqa: BLE001 - shadow result must be isolated
            result = CalculatorShadowComponentAudit(
                component_id=requirement.component_id,
                service_intent=requirement.service_intent,
                service_code=service_code,
                status="failed",
                reason=str(exc)[:600] or exc.__class__.__name__,
            )
        self._cache[cache_key] = result
        return result

    @staticmethod
    def _cache_key(
        requirement: RequirementIR,
        resource: ResourceIR,
        service_code: str,
    ) -> str:
        return _fingerprint(
            {
                "evidence_fingerprint": requirement.evidence_fingerprint,
                "service_code": service_code,
                "resource": resource.model_dump(mode="json"),
            }
        )


def build_calculator_shadow_verifier(
    catalog: AwsCalculatorContractCatalog,
    planner: AwsCalculatorConfigurationPlanner,
) -> AwsCalculatorShadowVerifier:
    return AwsCalculatorShadowVerifier(catalog, planner)
