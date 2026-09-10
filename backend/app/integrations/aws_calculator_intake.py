from __future__ import annotations

import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.domain.models import ServiceRequirement
from app.integrations.aws_calculator_contracts import (
    CalculatorContractNotFound,
    CalculatorServiceContract,
    CalculatorServiceSummary,
)


class CalculatorContractReader(Protocol):
    def get_contract(self, service: str) -> CalculatorServiceContract: ...

    def search(self, query: str, *, limit: int = 30) -> list[CalculatorServiceSummary]: ...


class OfficialCalculatorTemplateSelectionError(ValueError):
    """Raised when a submitted child does not belong to its official parent."""


class OfficialCalculatorFormAbsent(CalculatorContractNotFound):
    """No corresponding form was found in the official product manifest."""


class OfficialCalculatorTemplateOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service_code: str
    name: str


class OfficialCalculatorIntakeResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    status: Literal["ready", "selection_required"]
    parent_service_code: str | None = None
    contract: CalculatorServiceContract | None = None
    options: tuple[OfficialCalculatorTemplateOption, ...] = ()


class OfficialCalculatorIntakeResolver:
    """Resolve the AWS Calculator form before any component field extraction.

    A selector such as ``AWS Backup`` is not a usable form: it contains no
    fields and only points at official child forms.  This boundary therefore
    refuses to expose a local fallback schema and requires one of the exact
    child service codes published by AWS.
    """

    def __init__(self, catalog: CalculatorContractReader) -> None:
        self._catalog = catalog

    def resolve(
        self,
        component: ServiceRequirement,
        *,
        selected_service_code: str | None = None,
    ) -> OfficialCalculatorIntakeResolution:
        identity = (
            component.field_sources.get("_official_calculator_parent_service_code")
            or official_calculator_entrypoint(component)
            or component.calculator_service_name
            or component.service
        )
        try:
            parent = self._catalog.get_contract(identity)
        except CalculatorContractNotFound:
            return self._resolve_official_namespace(
                component,
                selected_service_code=selected_service_code,
            )
        is_selector = (
            parent.sub_type == "subServiceSelector" or bool(parent.subservice_codes)
        )
        if not is_selector:
            if selected_service_code and selected_service_code.casefold() != (
                parent.service_code.casefold()
            ):
                raise OfficialCalculatorTemplateSelectionError(
                    "selected service does not match the official Calculator contract"
                )
            return OfficialCalculatorIntakeResolution(
                status="ready",
                contract=parent,
            )

        options = tuple(
            OfficialCalculatorTemplateOption(
                service_code=child.service_code,
                name=child.name,
            )
            for child in (
                self._catalog.get_contract(service_code)
                for service_code in parent.subservice_codes
            )
        )
        if not selected_service_code:
            return OfficialCalculatorIntakeResolution(
                status="selection_required",
                parent_service_code=parent.service_code,
                options=options,
            )

        allowed = {
            option.service_code.casefold(): option.service_code for option in options
        }
        selected = allowed.get(selected_service_code.strip().casefold())
        if selected is None:
            raise OfficialCalculatorTemplateSelectionError(
                "selected service is not published by the official parent contract"
            )
        return OfficialCalculatorIntakeResolution(
            status="ready",
            parent_service_code=parent.service_code,
            contract=self._catalog.get_contract(selected),
            options=options,
        )

    def _resolve_official_namespace(
        self,
        component: ServiceRequirement,
        *,
        selected_service_code: str | None,
    ) -> OfficialCalculatorIntakeResolution:
        """Expand a generic AWS product name into exact official services.

        Some Calculator families, including Amazon FSx and Amazon RDS, publish
        only leaf services and no selector contract.  The manifest is still
        authoritative: a branded namespace such as ``Amazon FSx`` can safely
        expose the official services whose names begin with that exact product
        namespace.  Local code never invents the child list.
        """

        search = getattr(self._catalog, "search", None)
        namespace = _official_product_namespace(component.calculator_service_name)
        if search is None or namespace is None:
            raise OfficialCalculatorFormAbsent(
                component.calculator_service_name or component.service
            )
        summaries = list(search(namespace, limit=100))
        required_tokens = _official_product_tokens(namespace)
        if not any(
            _official_name_matches_product_tokens(summary.name, required_tokens)
            for summary in summaries
        ):
            # AWS commonly publishes an expanded official name with the short
            # product name only in parentheses, for example ``AWS WAF`` versus
            # ``AWS Web Application Firewall (WAF)``. Search the distinctive
            # product tokens as a second official-manifest query, then require
            # every token to occur in the returned official name.
            seen_codes = {summary.service_code.casefold() for summary in summaries}
            for token in sorted(required_tokens, key=lambda item: (-len(item), item)):
                for summary in search(token, limit=100):
                    folded_code = summary.service_code.casefold()
                    if folded_code in seen_codes:
                        continue
                    seen_codes.add(folded_code)
                    summaries.append(summary)
        normalized_namespace = _normalized_official_name(namespace)
        matches = tuple(
            OfficialCalculatorTemplateOption(
                service_code=summary.service_code,
                name=summary.name,
            )
            for summary in summaries
            if _official_name_belongs_to_namespace(summary.name, normalized_namespace)
            or _official_name_matches_product_tokens(summary.name, required_tokens)
        )
        if not matches:
            raise OfficialCalculatorFormAbsent(namespace)

        allowed = {
            option.service_code.casefold(): option.service_code for option in matches
        }
        if selected_service_code:
            selected = allowed.get(selected_service_code.strip().casefold())
            if selected is None:
                raise OfficialCalculatorTemplateSelectionError(
                    "selected service is not published in the official product namespace"
                )
            return OfficialCalculatorIntakeResolution(
                status="ready",
                contract=self._catalog.get_contract(selected),
                options=matches,
            )
        if len(matches) == 1:
            return OfficialCalculatorIntakeResolution(
                status="ready",
                contract=self._catalog.get_contract(matches[0].service_code),
                options=matches,
            )
        return OfficialCalculatorIntakeResolution(
            status="selection_required",
            options=matches,
        )

    @staticmethod
    def prompt_payload(contract: CalculatorServiceContract) -> dict[str, Any]:
        """Return only fields read from the current AWS Calculator contract."""

        return {
            "source": contract.source,
            "service_code": contract.service_code,
            "service_name": contract.name,
            "schema_hash": contract.schema_hash,
            "templates": [
                {
                    "template_id": template.template_id,
                    "title": template.title,
                    "fields": [
                        {
                            "field_id": field.field_id,
                            "label": field.label,
                            "field_type": field.field_type,
                            "required": field.required,
                            "default_value": field.default_value,
                            "options": [
                                {"id": option.option_id, "label": option.label}
                                for option in field.options
                            ],
                            "valid_size_units": list(field.valid_size_units),
                            "valid_frequency_units": list(
                                field.valid_frequency_units
                            ),
                            "section_id": field.section_id,
                            "section_title": field.section_title,
                        }
                        for field in template.fields
                    ],
                }
                for template in contract.templates
            ],
        }


_OFFICIAL_CALCULATOR_ENTRYPOINTS: dict[str, str] = {
    "ec2": "ec2Enhancement",
    "redis": "amazonElastiCache",
    "elasticache": "amazonElastiCache",
    "s3": "amazonSimpleStorageServiceGroup",
    "elb": "elasticLoadBalancing",
    "alb": "elasticLoadBalancing",
    "backup": "awsBackup",
}

_OFFICIAL_RDS_ENGINE_ENTRYPOINTS: dict[str, str] = {
    "mysql": "amazonRDSMySQLDB",
    "postgres": "amazonRDSPostgreSQLDB",
    "postgresql": "amazonRDSPostgreSQLDB",
    "mariadb": "amazonRDSMariaDB",
    "oracle": "amazonRdsForOracle",
    "sqlserver": "amazonRDSForSQLServer",
    "sql_server": "amazonRDSForSQLServer",
    "db2": "amazonRdsForDb2",
}


def _normalized_official_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold()).rstrip(" :-–—")


def _official_product_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in {"amazon", "aws"}
    )


def _official_name_matches_product_tokens(
    candidate_name: str,
    required_tokens: frozenset[str],
) -> bool:
    if not required_tokens:
        return False
    candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate_name.casefold()))
    return required_tokens <= candidate_tokens


def _official_product_namespace(value: str | None) -> str | None:
    """Return a branded product stem suitable for official manifest search."""

    if not value:
        return None
    compact = re.sub(r"\s+", " ", value.strip())
    compact = re.split(r"[（(/]", compact, maxsplit=1)[0].strip().rstrip(" :-–—")
    if not re.match(r"^(?:Amazon|AWS)\s+\S+", compact, re.I):
        return None
    return compact


def _official_name_belongs_to_namespace(
    candidate_name: str,
    normalized_namespace: str,
) -> bool:
    candidate = _normalized_official_name(candidate_name)
    return candidate == normalized_namespace or candidate.startswith(
        normalized_namespace + " "
    )


def official_calculator_entrypoint(
    component: ServiceRequirement,
) -> str | None:
    """Resolve an exact Calculator identity from typed product identity only."""

    service = component.service.strip().casefold()
    if service == "rds":
        engine = str(component.requirements.get("engine") or "").strip().casefold()
        return _OFFICIAL_RDS_ENGINE_ENTRYPOINTS.get(engine)
    return _OFFICIAL_CALCULATOR_ENTRYPOINTS.get(service)


# Product-identity translations are declarative and are applied only after an
# exact official child code has been selected.  They do not inspect customer
# prose and cannot manufacture usage values.
OFFICIAL_CHILD_IDENTITY_FIELDS: dict[
    tuple[str, str], tuple[str, str]
] = {
    ("s3", "amazons3standard"): ("storage_class", "standard"),
    ("s3", "amazons3intelligenttiering"): (
        "storage_class",
        "intelligent_tiering",
    ),
    ("s3", "amazons3standardinfrequentaccess"): (
        "storage_class",
        "standard_ia",
    ),
    ("s3", "s3onezoneinfrequentaccess"): ("storage_class", "one_zone_ia"),
    ("s3", "s3glacierinstantretrieval"): (
        "storage_class",
        "glacier_instant_retrieval",
    ),
    ("elb", "applicationloadbalancer"): (
        "load_balancer_type",
        "application",
    ),
    ("elb", "networkloadbalancer"): ("load_balancer_type", "network"),
    ("elb", "gatewayloadbalancer"): ("load_balancer_type", "gateway"),
    ("elb", "classicloadbalancer"): ("load_balancer_type", "classic"),
    ("fsx", "amazonfsx"): ("file_system_type", "windows"),
    ("fsx", "amazonfsxforlustre"): ("file_system_type", "lustre"),
    ("fsx", "amazonfsxfornetappontap"): ("file_system_type", "ontap"),
    ("fsx", "amazonfsxforopenzfs"): ("file_system_type", "openzfs"),
}


def official_child_identity_field(
    service: str,
    child_service_code: str,
    child_name: str,
) -> tuple[str, str] | None:
    normalized_service = service.strip().casefold()
    normalized_code = child_service_code.strip().casefold()
    declared = OFFICIAL_CHILD_IDENTITY_FIELDS.get(
        (normalized_service, normalized_code)
    )
    if declared is not None:
        return declared
    if normalized_service == "backup" and normalized_code.endswith("backup"):
        protected_service = child_name.removesuffix(" Backup").strip()
        if protected_service:
            return "protected_service", protected_service
    return None
