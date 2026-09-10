from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.domain.quote_compiler import RequirementIR, ResourceIR
from app.integrations.aws_calculator_contracts import (
    AwsCalculatorContractCatalog,
    CalculatorFieldContract,
    CalculatorServiceContract,
    CalculatorTemplateContract,
)


class StructuredJsonCompleter(Protocol):
    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_content: str,
        timeout_seconds: float,
        expected_keys: tuple[str, ...],
        max_attempts: int,
    ) -> dict[str, Any]: ...


class CalculatorConfigurationMappingError(ValueError):
    """Raised when an AI candidate cannot cross the ResourceIR boundary."""


class CalculatorBindingSource(BaseModel):
    """One typed source bound to an exact leaf in a composite widget value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_pointer: str = Field(min_length=1, max_length=500, pattern=r"^/")
    fact_id: str | None = None
    resource_path: str | None = None

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> CalculatorBindingSource:
        if bool(self.fact_id) == bool(self.resource_path):
            raise ValueError("structured source requires exactly one typed source")
        return self


class CalculatorFieldBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field_id: str = Field(min_length=1, max_length=240)
    value: Any
    source_fact_ids: tuple[str, ...] = ()
    source_resource_paths: tuple[str, ...] = ()
    sources: tuple[CalculatorBindingSource, ...] = ()

    @model_validator(mode="after")
    def require_a_typed_source(self) -> CalculatorFieldBinding:
        if not self.source_fact_ids and not self.source_resource_paths and not self.sources:
            raise ValueError("each binding requires a customer fact or ResourceIR source")
        return self


class CalculatorConfigurationCandidate(BaseModel):
    """Untrusted structured output proposed by AI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_id: str = Field(min_length=1, max_length=160)
    service_code: str = Field(min_length=1, max_length=240)
    template_id: str = Field(min_length=1, max_length=240)
    schema_hash: str = Field(min_length=64, max_length=64)
    bindings: tuple[CalculatorFieldBinding, ...]


class CompiledCalculatorConfiguration(BaseModel):
    """Schema-checked provider configuration suitable for ResourceIR metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "manifest_schema_validated"
    component_id: str
    service_code: str
    template_id: str
    schema_hash: str
    configuration: dict[str, Any]
    bindings: tuple[CalculatorFieldBinding, ...]
    consumed_fact_ids: tuple[str, ...]
    unconsumed_fact_ids: tuple[str, ...]
    consumed_resource_paths: tuple[str, ...] = ()
    validation_warnings: tuple[str, ...] = ()

    def resource_metadata(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AwsCalculatorConfigurationCompiler:
    """Verify identity, fact ownership and official form shape without guessing."""

    @staticmethod
    def compile(
        *,
        requirement: RequirementIR,
        resource: ResourceIR | None = None,
        contract: CalculatorServiceContract,
        candidate: CalculatorConfigurationCandidate,
    ) -> CompiledCalculatorConfiguration:
        errors: list[str] = []
        if candidate.component_id != requirement.component_id:
            errors.append("component identity does not match RequirementIR")
        if candidate.service_code != contract.service_code:
            errors.append("service identity does not match the official contract")
        if candidate.schema_hash != contract.schema_hash:
            errors.append("schema hash is stale or belongs to another official definition")

        field_ids = [binding.field_id for binding in candidate.bindings]
        duplicate_fields = sorted(
            field_id for field_id, count in Counter(field_ids).items() if count > 1
        )
        if duplicate_fields:
            errors.append(f"duplicate official field bindings: {duplicate_fields}")

        known_fact_ids = {fact.fact_id for fact in requirement.facts}
        facts_by_id = {fact.fact_id: fact for fact in requirement.facts}
        used_by_field: dict[str, set[str]] = {}
        used_resource_paths: set[str] = set()
        for binding in candidate.bindings:
            structured_fact_ids = tuple(
                source.fact_id for source in binding.sources if source.fact_id is not None
            )
            all_binding_fact_ids = (*binding.source_fact_ids, *structured_fact_ids)
            unknown = sorted(set(all_binding_fact_ids) - known_fact_ids)
            if unknown:
                errors.append(f"{binding.field_id} references unknown facts: {unknown}")
            if len(binding.source_fact_ids) > 1:
                errors.append(
                    f"{binding.field_id} derives one value from multiple customer facts "
                    "without an executable calculation"
                )
            if len(binding.source_fact_ids) == 1:
                fact = facts_by_id.get(binding.source_fact_ids[0])
                if fact is not None and not _equivalent_calculator_fact_value(
                    binding.value,
                    fact.value,
                    service_code=contract.service_code,
                    fact_path=fact.path,
                    field_id=binding.field_id,
                ):
                    errors.append(
                        f"{binding.field_id} does not equal customer fact "
                        f"{binding.source_fact_ids[0]}"
                    )
            for fact_id in binding.source_fact_ids:
                used_by_field.setdefault(fact_id, set()).add(binding.field_id)
            for path in binding.source_resource_paths:
                if resource is None:
                    errors.append(f"{binding.field_id} references ResourceIR without a resource")
                    continue
                try:
                    expected = _resource_value(resource, path)
                except KeyError:
                    errors.append(f"{binding.field_id} references unknown ResourceIR path {path}")
                    continue
                if not _equivalent_value(binding.value, expected):
                    errors.append(f"{binding.field_id} does not equal ResourceIR {path}")
                used_resource_paths.add(path)
            for source in binding.sources:
                try:
                    actual = _json_pointer_value(binding.value, source.target_pointer)
                except (KeyError, IndexError, TypeError, ValueError):
                    errors.append(
                        f"{binding.field_id} has invalid source pointer "
                        f"{source.target_pointer}"
                    )
                    continue
                if source.fact_id is not None:
                    fact = facts_by_id.get(source.fact_id)
                    if fact is not None and not _equivalent_calculator_fact_value(
                        actual,
                        fact.value,
                        service_code=contract.service_code,
                        fact_path=fact.path,
                        field_id=binding.field_id,
                    ):
                        errors.append(
                            f"{binding.field_id}{source.target_pointer} does not equal "
                            f"customer fact {source.fact_id}"
                        )
                    used_by_field.setdefault(source.fact_id, set()).add(binding.field_id)
                    continue
                assert source.resource_path is not None
                if resource is None:
                    errors.append(
                        f"{binding.field_id} references ResourceIR without a resource"
                    )
                    continue
                try:
                    expected = _resource_value(resource, source.resource_path)
                except KeyError:
                    errors.append(
                        f"{binding.field_id} references unknown ResourceIR path "
                        f"{source.resource_path}"
                    )
                    continue
                if not _equivalent_value(actual, expected):
                    errors.append(
                        f"{binding.field_id}{source.target_pointer} does not equal "
                        f"ResourceIR {source.resource_path}"
                    )
                used_resource_paths.add(source.resource_path)
        multiply_owned = {
            fact_id: sorted(owners)
            for fact_id, owners in used_by_field.items()
            if len(owners) > 1
        }
        if multiply_owned:
            errors.append(
                "customer facts assigned to multiple official fields: "
                + json.dumps(multiply_owned, ensure_ascii=False, sort_keys=True)
            )
        configuration: dict[str, Any] = {}
        if requirement.region:
            configuration["region"] = requirement.region
        configuration.update(
            {binding.field_id: binding.value for binding in candidate.bindings}
        )
        validation = AwsCalculatorContractCatalog.validate_configuration(
            contract,
            template_id=candidate.template_id,
            configuration=configuration,
        )
        if not validation.valid:
            errors.append(
                "official calculator configuration is invalid: "
                + "; ".join(validation.errors)
            )
        if errors:
            raise CalculatorConfigurationMappingError("; ".join(errors))

        consumed = tuple(sorted(used_by_field))
        unconsumed = tuple(sorted(known_fact_ids - set(consumed)))
        return CompiledCalculatorConfiguration(
            component_id=requirement.component_id,
            service_code=contract.service_code,
            template_id=validation.template_id or candidate.template_id,
            schema_hash=contract.schema_hash,
            configuration=validation.normalized_configuration,
            bindings=candidate.bindings,
            consumed_fact_ids=consumed,
            unconsumed_fact_ids=unconsumed,
            consumed_resource_paths=tuple(sorted(used_resource_paths)),
            validation_warnings=validation.warnings,
        )


def _inner_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) >= {"value"}:
        return value["value"]
    return value


def _json_pointer_value(value: Any, pointer: str) -> Any:
    current = value
    for raw_part in pointer.removeprefix("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not part.isdigit():
                raise ValueError(pointer)
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise TypeError(pointer)
    return current


def _equivalent_value(actual: Any, expected: Any) -> bool:
    """Compare a Calculator input with its typed source without loose guessing."""

    actual = _inner_value(actual)
    expected = _inner_value(expected)
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    try:
        return Decimal(str(actual)) == Decimal(str(expected))
    except (InvalidOperation, TypeError, ValueError):
        pass
    if isinstance(actual, str) and isinstance(expected, str):
        actual_text = actual.strip().casefold()
        expected_text = expected.strip().casefold()
        if actual_text == expected_text:
            return True
        return (
            "".join(character for character in actual_text if character.isalnum())
            == "".join(character for character in expected_text if character.isalnum())
        )
    return actual == expected


_OFFICIAL_ENUM_EQUIVALENCE: dict[tuple[str, str, str], dict[str, str]] = {
    (
        "amazonRDSMySQLDB",
        "requirements.storage_type",
        "storageType",
    ): {
        "gp2": "General Purpose",
        "gp3": "General Purpose-GP3",
        "io1": "Provisioned IOPS",
        "io2": "Provisioned IOPS-IO2",
        "magnetic": "Magnetic",
    },
    (
        "amazonRDSPostgreSQLDB",
        "requirements.storage_type",
        "storageVolume",
    ): {
        "gp2": "General Purpose",
        "gp3": "General Purpose-GP3",
        "io1": "Provisioned IOPS",
        "io2": "Provisioned IOPS-IO2",
        "magnetic": "Magnetic",
    },
    (
        "ec2Enhancement",
        "requirements.volume_type",
        "storageType",
    ): {
        "gp3": "Storage General Purpose gp3 GB Mo",
        "gp2": "Storage General Purpose GB Mo",
        "io1": "Storage Provisioned IOPS GB Mo",
        "io2": "Storage Provisioned IOPS io2 GB month",
        "st1": "Storage Throughput Optimized HDD GB Mo",
        "sc1": "Storage Cold HDD GB Mo",
        "standard": "Storage Magnetic GB Mo",
    },
}


def _equivalent_calculator_fact_value(
    actual: Any,
    expected: Any,
    *,
    service_code: str,
    fact_path: str,
    field_id: str,
) -> bool:
    if fact_path.casefold().endswith("_gib") and isinstance(actual, dict):
        try:
            raw_unit = str(actual.get("unit") or "").split("|", 1)[0].casefold()
            factor = {
                "mib": Decimal(1) / Decimal(1024),
                "mb": Decimal(1) / Decimal(1024),
                "gib": Decimal(1),
                "gb": Decimal(1),
                "tib": Decimal(1024),
                "tb": Decimal(1024),
            }.get(raw_unit)
            numeric = Decimal(str(actual.get("value")))
            expected_gib = Decimal(str(expected))
        except (InvalidOperation, TypeError, ValueError):
            factor = None
        if factor is not None:
            return numeric * factor == expected_gib
    if _equivalent_value(actual, expected):
        return True
    aliases = _OFFICIAL_ENUM_EQUIVALENCE.get((service_code, fact_path, field_id), {})
    mapped = aliases.get(str(_inner_value(expected)).strip().casefold())
    return mapped is not None and _equivalent_value(actual, mapped)


def _pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _field_identity(field: CalculatorFieldContract) -> str:
    return "".join(
        character
        for character in f"{field.field_id} {field.label or ''}".casefold()
        if character.isalnum()
    )


def _candidate_template(
    contract: CalculatorServiceContract,
    template_id: object,
) -> CalculatorTemplateContract | None:
    return next(
        (
            template
            for template in contract.templates
            if template.template_id == str(template_id or "")
        ),
        contract.templates[0] if len(contract.templates) == 1 else None,
    )


def _binding_source_ids(binding: dict[str, Any]) -> tuple[set[str], set[str]]:
    facts = {
        str(value)
        for value in binding.get("source_fact_ids", [])
        if isinstance(value, str)
    }
    resources = {
        str(value)
        for value in binding.get("source_resource_paths", [])
        if isinstance(value, str)
    }
    for source in binding.get("sources", []):
        if not isinstance(source, dict):
            continue
        if isinstance(source.get("fact_id"), str):
            facts.add(source["fact_id"])
        if isinstance(source.get("resource_path"), str):
            resources.add(source["resource_path"])
    return facts, resources


def _matching_leaf_pointers(value: Any, expected: Any, pointer: str = "") -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            matches.extend(
                _matching_leaf_pointers(
                    child,
                    expected,
                    f"{pointer}/{_pointer_part(str(key))}",
                )
            )
        return matches
    if isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(
                _matching_leaf_pointers(child, expected, f"{pointer}/{index}")
            )
        return matches
    if pointer and _equivalent_value(value, expected):
        matches.append(pointer)
    return matches


def _promote_composite_sources(
    binding: dict[str, Any],
    *,
    field: CalculatorFieldContract | None,
    facts_by_id: dict[str, Any],
    resource: ResourceIR | None,
) -> None:
    """Turn whole-widget source claims into exact, verified leaf pointers."""

    if field is None or field.field_type != "columnFormIPM":
        return
    sources = binding.setdefault("sources", [])
    if not isinstance(sources, list):
        return
    remaining_facts: list[str] = []
    for fact_id in binding.get("source_fact_ids", []):
        fact = facts_by_id.get(str(fact_id))
        pointers = _matching_leaf_pointers(binding.get("value"), fact.value) if fact else []
        if len(pointers) == 1:
            sources.append({"target_pointer": pointers[0], "fact_id": str(fact_id)})
        else:
            remaining_facts.append(str(fact_id))
    binding["source_fact_ids"] = remaining_facts
    remaining_resources: list[str] = []
    for path in binding.get("source_resource_paths", []):
        try:
            expected = _resource_value(resource, str(path)) if resource is not None else None
        except KeyError:
            remaining_resources.append(str(path))
            continue
        pointers = _matching_leaf_pointers(binding.get("value"), expected)
        if len(pointers) == 1:
            sources.append({"target_pointer": pointers[0], "resource_path": str(path)})
        else:
            remaining_resources.append(str(path))
    binding["source_resource_paths"] = remaining_resources


def _repair_required_source_coverage(
    payload: dict[str, Any],
    *,
    requirement: RequirementIR,
    resource: ResourceIR | None,
    contract: CalculatorServiceContract,
    required_fact_ids: tuple[str, ...],
    required_resource_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Fill only universal count/model bindings from typed IR and live fields."""

    template = _candidate_template(contract, payload.get("template_id"))
    bindings = payload.get("bindings")
    if template is None or not isinstance(bindings, list):
        return payload
    normalized_bindings = [dict(item) for item in bindings if isinstance(item, dict)]
    facts_by_id = {fact.fact_id: fact for fact in requirement.facts}
    fields_by_id = {field.field_id: field for field in template.fields}
    for binding in normalized_bindings:
        _promote_composite_sources(
            binding,
            field=fields_by_id.get(str(binding.get("field_id") or "")),
            facts_by_id=facts_by_id,
            resource=resource,
        )
    bound_facts: set[str] = set()
    bound_resources: set[str] = set()
    for binding in normalized_bindings:
        facts, resources = _binding_source_ids(binding)
        bound_facts.update(facts)
        bound_resources.update(resources)
    missing_quantity_facts = [
        fact_id
        for fact_id in required_fact_ids
        if fact_id not in bound_facts
        and facts_by_id.get(fact_id) is not None
        and facts_by_id[fact_id].path == "quantity"
    ]
    model_required = (
        resource is not None
        and "model" in required_resource_paths
        and "model" not in bound_resources
    )

    column_candidates: list[tuple[CalculatorFieldContract, str, str]] = []
    for field in template.fields:
        if field.field_type != "columnFormIPM":
            continue
        node_key = next(
            (
                row.selector_id or row.label
                for row in field.row_fields
                if "node" in "".join(
                    character
                    for character in f"{row.selector_id or ''} {row.label or ''}".casefold()
                    if character.isalnum()
                )
            ),
            None,
        )
        model_key = next(
            (
                row.selector_id or row.label
                for row in field.row_fields
                if row.is_instance_type
                or "instancetype"
                in "".join(
                    character
                    for character in f"{row.selector_id or ''} {row.label or ''}".casefold()
                    if character.isalnum()
                )
            ),
            None,
        )
        if node_key and model_key:
            column_candidates.append((field, node_key, model_key))
    if (missing_quantity_facts or model_required) and column_candidates:
        field, node_key, model_key = next(
            (
                candidate
                for candidate in column_candidates
                if candidate[0].field_id == "columnFormIPM"
            ),
            column_candidates[0],
        )
        existing = next(
            (
                binding
                for binding in normalized_bindings
                if binding.get("field_id") == field.field_id
            ),
            None,
        )
        if existing is None:
            existing = {
                "field_id": field.field_id,
                "value": {"value": [{}]},
                "source_fact_ids": [],
                "source_resource_paths": [],
                "sources": [],
            }
            normalized_bindings.append(existing)
        value = existing.setdefault("value", {"value": [{}]})
        if isinstance(value, dict):
            rows = value.setdefault("value", [{}])
            if isinstance(rows, list) and rows:
                if not isinstance(rows[0], dict):
                    rows[0] = {}
                row = rows[0]
                sources = existing.setdefault("sources", [])
                if isinstance(sources, list):
                    for fact_id in missing_quantity_facts:
                        fact = facts_by_id[fact_id]
                        row[node_key] = {"value": str(fact.value)}
                        sources.append(
                            {
                                "target_pointer": (
                                    f"/value/0/{_pointer_part(node_key)}/value"
                                ),
                                "fact_id": fact_id,
                            }
                        )
                    if model_required and resource is not None:
                        row[model_key] = {"value": resource.model}
                        sources.append(
                            {
                                "target_pointer": (
                                    f"/value/0/{_pointer_part(model_key)}/value"
                                ),
                                "resource_path": "model",
                            }
                        )
        missing_quantity_facts = []
        model_required = False

    if model_required and resource is not None:
        model_fields = [
            field
            for field in template.fields
            if field.field_type != "columnFormIPM"
            and "instancetype" in _field_identity(field)
        ]
        if len(model_fields) == 1:
            normalized_bindings.append(
                {
                    "field_id": model_fields[0].field_id,
                    "value": resource.model,
                    "source_fact_ids": [],
                    "source_resource_paths": ["model"],
                    "sources": [],
                }
            )

    for fact_id in missing_quantity_facts:
        fact = facts_by_id[fact_id]
        quantity_fields = [
            field
            for field in template.fields
            if field.field_type != "columnFormIPM"
            and (
                "workload" in _field_identity(field)
                and field.field_id.casefold() == "workload"
                or _field_identity(field).startswith("numberof")
            )
        ]
        if len(quantity_fields) == 1:
            normalized_bindings.append(
                {
                    "field_id": quantity_fields[0].field_id,
                    "value": fact.value,
                    "source_fact_ids": [fact_id],
                    "source_resource_paths": [],
                    "sources": [],
                }
            )
    return {**payload, "bindings": normalized_bindings}


def _resource_value(resource: ResourceIR, path: str) -> Any:
    """Read only explicitly allow-listed ResourceIR fields for AI bindings."""

    if path == "model":
        return resource.model
    if path.startswith("official_specifications."):
        key = path.removeprefix("official_specifications.")
        if key and key in resource.official_specifications:
            return resource.official_specifications[key]
    raise KeyError(path)


def calculator_configuration_prompt(
    requirement: RequirementIR,
    contract: CalculatorServiceContract,
    *,
    resource: ResourceIR | None = None,
) -> str:
    """Build an evidence-free prompt from immutable typed facts and official fields."""

    facts = [
        {
            "fact_id": fact.fact_id,
            "path": fact.path,
            "value": fact.value,
            "unit": fact.unit,
            "scope": fact.scope,
            "match_policy": fact.match_policy,
        }
        for fact in requirement.facts
    ]
    templates = [
        {
            "template_id": template.template_id,
            "title": template.title,
            "fields": [field.model_dump(mode="json") for field in template.fields],
        }
        for template in contract.templates
    ]
    payload = {
        "component_id": requirement.component_id,
        "service_intent": requirement.service_intent,
        "product_identity": requirement.product_identity,
        "region": requirement.region,
        "facts": facts,
        "official_calculator_contract": {
            "service_code": contract.service_code,
            "schema_hash": contract.schema_hash,
            "templates": templates,
        },
    }
    if resource is not None:
        payload["selected_resource"] = {
            "service": resource.service,
            "region": resource.region,
            "model": resource.model,
            "quantity": resource.quantity,
            "official_specifications": resource.official_specifications,
            "allowed_source_paths": [
                "model",
                *[
                    f"official_specifications.{key}"
                    for key in sorted(resource.official_specifications)
                ],
            ],
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AwsCalculatorConfigurationPlanner:
    """Ask AI for bindings, then accept only compiler-verified structured output."""

    _SYSTEM_PROMPT = """You map typed customer facts to one AWS Calculator template.
Return one JSON object containing component_id, service_code, template_id, schema_hash and bindings.
Each binding must contain field_id and value. For scalar fields, also return
source_fact_ids and source_resource_paths. For composite fields, return sources;
each source has an RFC 6901 target_pointer plus exactly one fact_id or resource_path.
Use only supplied fact IDs, allowed ResourceIR source paths and official field IDs.
The value must equal its one typed source. Do not calculate, invent, infer, or materialize defaults.
For fileSize, frequency and durationInput fields, value must use the official
{value, unit} object shape and the contract's allowed/default unit.
For columnFormIPM fields, value must use {value: [row, ...]} and every row cell
must use {value: ...}, exactly as described by row_fields. Bind every customer or
ResourceIR value inside that object with its exact JSON pointer in sources.
Omit facts that are non-billing context.
Omit every Calculator default or field that has no supplied fact/resource source;
never return an empty source_fact_ids + source_resource_paths binding.
required_source_coverage is mandatory: every listed fact_id and resource path
must appear in exactly one binding, even when the official field is optional or
has a default. Check this coverage yourself before returning JSON.
Map selected_resource.model to the matching official instance/model field.
Map a required quantity fact to the official count, workload, or Nodes field.
service_identity_fact_ids selected the service variant itself; never bind them
to a configuration field.
Do not return prose, prices, or unofficial specifications."""

    @staticmethod
    def _remove_unbound_default_proposals(payload: dict[str, Any]) -> dict[str, Any]:
        """Discard source-free AI defaults; the Calculator owns its own defaults."""

        bindings = payload.get("bindings")
        if not isinstance(bindings, list):
            return payload
        filtered = [
            item
            for item in bindings
            if isinstance(item, dict)
            and (
                item.get("source_fact_ids")
                or item.get("source_resource_paths")
                or item.get("sources")
            )
        ]
        return {**payload, "bindings": filtered}

    @staticmethod
    def _remove_identity_fact_proposals(
        payload: dict[str, Any],
        identity_fact_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        """Keep product-routing facts out of Calculator configuration fields."""

        if not identity_fact_ids or not isinstance(payload.get("bindings"), list):
            return payload
        excluded = set(identity_fact_ids)
        filtered = []
        for item in payload["bindings"]:
            if not isinstance(item, dict):
                continue
            facts, _ = _binding_source_ids(item)
            if facts & excluded:
                continue
            filtered.append(item)
        return {**payload, "bindings": filtered}

    def __init__(
        self,
        completer: StructuredJsonCompleter,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._completer = completer
        self._timeout_seconds = timeout_seconds

    async def plan(
        self,
        *,
        requirement: RequirementIR,
        resource: ResourceIR | None = None,
        contract: CalculatorServiceContract,
        required_fact_ids: tuple[str, ...] = (),
        required_resource_paths: tuple[str, ...] = (),
        identity_fact_ids: tuple[str, ...] = (),
    ) -> CompiledCalculatorConfiguration:
        user_content = calculator_configuration_prompt(
            requirement, contract, resource=resource
        )
        if required_fact_ids or required_resource_paths or identity_fact_ids:
            user_content = json.dumps(
                {
                    "mapping_input": json.loads(user_content),
                    "required_source_coverage": {
                        "fact_ids": list(required_fact_ids),
                        "resource_paths": list(required_resource_paths),
                    },
                    "service_identity_fact_ids": list(identity_fact_ids),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        last_error: Exception | None = None
        for attempt in range(2):
            system_prompt = self._SYSTEM_PROMPT
            if attempt and last_error is not None:
                system_prompt += (
                    "\nThe previous candidate was rejected by the deterministic compiler. "
                    "Correct only these errors and return the complete object again:\n- "
                    + str(last_error)[:1200]
                )
            payload = await self._completer.complete_json(
                system_prompt=system_prompt,
                user_content=user_content,
                timeout_seconds=self._timeout_seconds,
                expected_keys=(
                    "component_id",
                    "service_code",
                    "template_id",
                    "schema_hash",
                    "bindings",
                ),
                max_attempts=1,
            )
            try:
                payload = self._remove_unbound_default_proposals(payload)
                payload = self._remove_identity_fact_proposals(
                    payload, identity_fact_ids
                )
                payload = _repair_required_source_coverage(
                    payload,
                    requirement=requirement,
                    resource=resource,
                    contract=contract,
                    required_fact_ids=required_fact_ids,
                    required_resource_paths=required_resource_paths,
                )
                candidate = CalculatorConfigurationCandidate.model_validate(payload)
                compiled = AwsCalculatorConfigurationCompiler.compile(
                    requirement=requirement,
                    resource=resource,
                    contract=contract,
                    candidate=candidate,
                )
                missing_facts = sorted(
                    set(required_fact_ids) - set(compiled.consumed_fact_ids)
                )
                missing_resource_paths = sorted(
                    set(required_resource_paths)
                    - set(compiled.consumed_resource_paths)
                )
                if missing_facts or missing_resource_paths:
                    raise CalculatorConfigurationMappingError(
                        "required typed sources were omitted: "
                        + json.dumps(
                            {
                                "fact_ids": missing_facts,
                                "resource_paths": missing_resource_paths,
                            },
                            sort_keys=True,
                        )
                    )
                return compiled
            except (ValidationError, CalculatorConfigurationMappingError) as exc:
                last_error = exc
        raise CalculatorConfigurationMappingError(
            "AI calculator mapping was rejected after deterministic repair"
            + (f": {last_error}" if last_error is not None else "")
        ) from last_error
