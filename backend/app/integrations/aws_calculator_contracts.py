from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, computed_field

AWS_CALCULATOR_CDN_BASE = "https://d1qsjq9pzbk1k6.cloudfront.net"
AWS_CALCULATOR_MANIFEST_PATH = "/manifest/en_US.json"
CALCULATOR_CONTRACT_CACHE_VERSION = 1

_INPUT_TYPES = {
    "input",
    "numericInput",
    "frequency",
    "fileSize",
    "durationInput",
    "percentInput",
}
_INPUT_SUBTYPES = {
    "dropdown",
    "numericInput",
    "frequency",
    "fileSize",
    "durationInput",
    "columnFormIPM",
    "dataTransferV2",
}
_DECORATIVE_SUBTYPES = {"alert", "bodyText", "headerText"}
_CONFIG_META_FIELDS = {"region", "description"}


class CalculatorContractError(RuntimeError):
    """Base error for the AWS Calculator field-contract boundary."""


class CalculatorContractUnavailable(CalculatorContractError):
    """Raised when neither AWS nor a cached official snapshot is available."""


class CalculatorContractNotFound(CalculatorContractError):
    """Raised when no exact official service identity can be established."""


class CalculatorFieldOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    option_id: str
    label: str | None = None
    value: Any = None


class CalculatorRowField(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    selector_id: str | None = None
    label: str | None = None
    field_type: str | None = None
    required: bool = False
    default_value: Any = None
    min_value: float | None = None
    max_value: float | None = None
    allow_decimals: bool | None = None
    export_value_as: str | None = None
    is_instance_type: bool = False


class CalculatorFieldContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field_id: str
    field_type: str
    label: str | None = None
    required: bool = False
    default_value: Any = None
    placeholder: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    allow_decimals: bool | None = None
    unit: Any = None
    options: tuple[CalculatorFieldOption, ...] = ()
    valid_size_units: tuple[str, ...] = ()
    valid_frequency_units: tuple[str, ...] = ()
    default_unit: str | None = None
    display_if: dict[str, Any] | None = None
    mapping_definition_name: str | None = None
    row_fields: tuple[CalculatorRowField, ...] = ()
    # One AWS Calculator template can contain several independent pricing
    # cards. Keep that official boundary so required inputs from an unrelated
    # card are not mistaken for missing fields in the selected workload.
    section_id: str | None = None
    section_title: str | None = None


class CalculatorTemplateContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    title: str | None = None
    fields: tuple[CalculatorFieldContract, ...] = ()


class CalculatorServiceSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service_code: str
    name: str
    sub_type: str | None = None
    definition_url: str


class CalculatorServiceContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service_code: str
    name: str
    service_type: str | None = None
    sub_type: str | None = None
    definition_version: str | None = None
    definition_url: str
    schema_hash: str
    schema_changed: bool = False
    previous_schema_hash: str | None = None
    source: Literal["aws_calculator_runtime"] = "aws_calculator_runtime"
    cache_status: Literal["live", "fresh", "stale"]
    templates: tuple[CalculatorTemplateContract, ...] = ()
    subservice_codes: tuple[str, ...] = ()

    @computed_field
    @property
    def field_count(self) -> int:
        return sum(len(template.fields) for template in self.templates)


class CalculatorConfigurationValidation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    service_code: str
    template_id: str | None = None
    schema_hash: str
    validation_scope: Literal["calculator_manifest_schema"] = "calculator_manifest_schema"
    normalized_configuration: dict[str, Any] = Field(default_factory=dict)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() == "true"
    return value is True


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _inner_value(value: Any) -> Any:
    if isinstance(value, Mapping) and "value" in value:
        return value["value"]
    return value


def _is_present(value: Any) -> bool:
    inner = _inner_value(value)
    return inner is not None and inner != ""


class AwsCalculatorContractCatalog:
    """Read and validate AWS Calculator's own runtime form definitions.

    The CloudFront documents are the data consumed by calculator.aws. They are
    treated as a versioned external contract rather than as a pricing result:
    callers still need a pricing oracle and the quote compiler before release.
    """

    def __init__(
        self,
        *,
        cache_path: Path,
        base_url: str = AWS_CALCULATOR_CDN_BASE,
        manifest_path: str = AWS_CALCULATOR_MANIFEST_PATH,
        cache_ttl_seconds: float = 6 * 60 * 60,
        timeout_seconds: float = 20.0,
        trust_env_proxy: bool = True,
        fetch_json: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._cache_path = cache_path
        self._base_url = base_url.rstrip("/")
        self._manifest_path = manifest_path
        self._cache_ttl_seconds = max(float(cache_ttl_seconds), 0.0)
        self._timeout_seconds = timeout_seconds
        self._trust_env_proxy = trust_env_proxy
        self._custom_fetch_json = fetch_json
        self._lock = threading.RLock()
        self._state = self._read_cache()

    def search(
        self,
        query: str,
        *,
        limit: int = 30,
        force_refresh: bool = False,
    ) -> list[CalculatorServiceSummary]:
        term = query.strip().casefold()
        if not term:
            return []
        manifest, _ = self._manifest(force_refresh=force_refresh)
        matches: list[tuple[int, CalculatorServiceSummary]] = []
        for service in self._manifest_services(manifest):
            if str(service.get("isActive", "true")).casefold() == "false":
                continue
            code = str(service.get("key") or service.get("serviceCode") or "").strip()
            name = str(service.get("name") or code).strip()
            if not code:
                continue
            keywords = [
                str(value).casefold()
                for value in service.get("searchKeywords", [])
                if isinstance(value, str)
            ]
            code_folded = code.casefold()
            name_folded = name.casefold()
            if term not in code_folded and term not in name_folded and not any(
                term in keyword for keyword in keywords
            ):
                continue
            if term in {code_folded, name_folded}:
                rank = 0
            elif code_folded.startswith(term) or name_folded.startswith(term):
                rank = 1
            else:
                rank = 2
            matches.append((rank, self._summary(service)))
        matches.sort(key=lambda item: (item[0], item[1].name.casefold(), item[1].service_code))
        return [item[1] for item in matches[: max(1, min(limit, 100))]]

    def get_contract(
        self,
        service: str,
        *,
        force_refresh: bool = False,
    ) -> CalculatorServiceContract:
        manifest, manifest_status = self._manifest(force_refresh=force_refresh)
        entry = self._find_exact_service(manifest, service)
        definition, definition_status, previous_schema_hash = self._definition(
            entry, force_refresh=force_refresh
        )
        status: Literal["live", "fresh", "stale"]
        if "stale" in {manifest_status, definition_status}:
            status = "stale"
        elif "live" in {manifest_status, definition_status}:
            status = "live"
        else:
            status = "fresh"
        return self._build_contract(
            entry,
            definition,
            status,
            previous_schema_hash=previous_schema_hash,
        )

    @classmethod
    def validate_configuration(
        cls,
        contract: CalculatorServiceContract,
        *,
        configuration: Mapping[str, Any],
        template_id: str | None = None,
    ) -> CalculatorConfigurationValidation:
        errors: list[str] = []
        warnings: list[str] = []
        normalized = dict(configuration)
        template = cls._select_template(contract, template_id, errors)
        if template is None:
            return CalculatorConfigurationValidation(
                valid=False,
                service_code=contract.service_code,
                template_id=template_id,
                schema_hash=contract.schema_hash,
                normalized_configuration=normalized,
                errors=tuple(errors),
            )

        fields = {field.field_id: field for field in template.fields}
        section_ids = {
            field.section_id for field in template.fields if field.section_id
        }
        explicitly_active_sections = {
            field.section_id
            for field in template.fields
            if field.section_id
            and field.field_id in normalized
            and _is_present(normalized[field.field_id])
        }
        active_sections = (
            explicitly_active_sections
            if len(section_ids) > 1 and explicitly_active_sections
            else section_ids
        )
        unknown = sorted(set(configuration) - set(fields) - _CONFIG_META_FIELDS)
        if unknown:
            errors.append(f"unknown calculator fields: {', '.join(unknown)}")

        for field in template.fields:
            if field.section_id and field.section_id not in active_sections:
                continue
            present = field.field_id in normalized and _is_present(
                normalized[field.field_id]
            )
            condition = cls._evaluate_condition(field.display_if, normalized, fields)
            has_calculator_default = field.default_value is not None
            if (
                field.required
                and condition is not False
                and not present
                and not has_calculator_default
            ):
                if condition is None and field.display_if is not None:
                    warnings.append(
                        f'{field.field_id}: required condition could not be evaluated locally'
                    )
                else:
                    errors.append(f"{field.field_id}: required field is missing")
            if not present:
                continue
            value, field_errors = cls._validate_field(field, normalized[field.field_id])
            normalized[field.field_id] = value
            errors.extend(field_errors)

        return CalculatorConfigurationValidation(
            valid=not errors,
            service_code=contract.service_code,
            template_id=template.template_id,
            schema_hash=contract.schema_hash,
            normalized_configuration=normalized,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    def _fetch_json(self, url: str) -> dict[str, Any]:
        # AWS publishes immutable-ish JSON over CloudFront. A single transient
        # read timeout must not invalidate an otherwise valid customer quote.
        # When a desktop proxy is configured, the second attempt uses a direct
        # connection so a local proxy hiccup cannot become a provider outage.
        proxy = self._proxy_url()
        attempts = (proxy, None) if proxy else (None, None)
        for attempt_index, attempt_proxy in enumerate(attempts):
            try:
                if self._custom_fetch_json is not None:
                    payload = self._custom_fetch_json(url)
                else:
                    timeout = httpx.Timeout(
                        self._timeout_seconds,
                        connect=min(self._timeout_seconds, 10),
                    )
                    with httpx.Client(
                        timeout=timeout,
                        trust_env=False,
                        # Desktop NO_PROXY commonly contains a raw IPv6 literal
                        # that httpx rejects while parsing environment config.
                        proxy=attempt_proxy,
                    ) as client:
                        response = client.get(
                            url, headers={"Accept": "application/json"}
                        )
                        response.raise_for_status()
                        payload = response.json()
            except (TimeoutError, ConnectionError, httpx.TransportError):
                if attempt_index + 1 >= len(attempts):
                    raise
                continue
            if not isinstance(payload, dict):
                raise CalculatorContractUnavailable(
                    "AWS Calculator returned a non-object document"
                )
            return payload
        raise AssertionError("official Calculator fetch exhausted without a result")

    def _proxy_url(self) -> str | None:
        if not self._trust_env_proxy:
            return None
        for name in (
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
            "HTTP_PROXY",
            "http_proxy",
        ):
            value = os.environ.get(name)
            if value:
                return value
        return None

    def _read_cache(self) -> dict[str, Any]:
        empty = {
            "cache_version": CALCULATOR_CONTRACT_CACHE_VERSION,
            "manifest": None,
            "manifest_fetched_at": 0.0,
            "definitions": {},
        }
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return empty
        if not isinstance(payload, dict) or payload.get("cache_version") != (
            CALCULATOR_CONTRACT_CACHE_VERSION
        ):
            return empty
        if not isinstance(payload.get("definitions"), dict):
            return empty
        return {**empty, **payload}

    def _write_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_name(
            f".{self._cache_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self._cache_path)

    def _is_fresh(self, fetched_at: Any) -> bool:
        timestamp = _number(fetched_at)
        if timestamp is None:
            return False
        return time.time() - timestamp < self._cache_ttl_seconds

    def _manifest(
        self,
        *,
        force_refresh: bool,
    ) -> tuple[dict[str, Any], Literal["live", "fresh", "stale"]]:
        with self._lock:
            cached = self._state.get("manifest")
            if (
                isinstance(cached, dict)
                and not force_refresh
                and self._is_fresh(self._state.get("manifest_fetched_at"))
            ):
                return cached, "fresh"
            try:
                payload = self._fetch_json(self._absolute_url(self._manifest_path))
                self._manifest_services(payload)
            except Exception as exc:
                if isinstance(cached, dict):
                    return cached, "stale"
                raise CalculatorContractUnavailable(
                    f"AWS Calculator manifest is unavailable: {type(exc).__name__}"
                ) from exc
            self._state["manifest"] = payload
            self._state["manifest_fetched_at"] = time.time()
            self._write_cache()
            return payload, "live"

    def _definition(
        self,
        entry: Mapping[str, Any],
        *,
        force_refresh: bool,
    ) -> tuple[
        dict[str, Any],
        Literal["live", "fresh", "stale"],
        str | None,
    ]:
        with self._lock:
            code = str(entry.get("key") or entry.get("serviceCode") or "")
            definitions = self._state.setdefault("definitions", {})
            cached_entry = definitions.get(code)
            cached_payload = (
                cached_entry.get("payload") if isinstance(cached_entry, dict) else None
            )
            if (
                isinstance(cached_payload, dict)
                and not force_refresh
                and self._is_fresh(cached_entry.get("fetched_at"))
            ):
                return cached_payload, "fresh", None
            url = self._definition_url(entry)
            try:
                payload = self._fetch_json(url)
            except Exception as exc:
                if isinstance(cached_payload, dict):
                    return cached_payload, "stale", None
                raise CalculatorContractUnavailable(
                    f"AWS Calculator definition for {code} is unavailable: {type(exc).__name__}"
                ) from exc
            previous_schema_hash = None
            if isinstance(cached_payload, dict):
                old_hash = _canonical_hash(cached_payload)
                new_hash = _canonical_hash(payload)
                if old_hash != new_hash:
                    previous_schema_hash = old_hash
            definitions[code] = {
                "fetched_at": time.time(),
                "schema_hash": _canonical_hash(payload),
                "payload": payload,
            }
            self._write_cache()
            return payload, "live", previous_schema_hash

    @staticmethod
    def _manifest_services(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
        services = manifest.get("awsServices")
        if not isinstance(services, list):
            raise CalculatorContractUnavailable("AWS Calculator manifest has no awsServices list")
        return [item for item in services if isinstance(item, dict)]

    def _find_exact_service(
        self,
        manifest: Mapping[str, Any],
        requested: str,
    ) -> dict[str, Any]:
        wanted = requested.strip().casefold()
        matches = []
        for service in self._manifest_services(manifest):
            if str(service.get("isActive", "true")).casefold() == "false":
                continue
            code = str(service.get("key") or service.get("serviceCode") or "").strip()
            name = str(service.get("name") or "").strip()
            if wanted in {code.casefold(), name.casefold()}:
                matches.append(service)
        if len(matches) != 1:
            raise CalculatorContractNotFound(
                f'exact AWS Calculator service identity not found for "{requested}"'
            )
        return matches[0]

    def _summary(self, entry: Mapping[str, Any]) -> CalculatorServiceSummary:
        code = str(entry.get("key") or entry.get("serviceCode") or "")
        return CalculatorServiceSummary(
            service_code=code,
            name=str(entry.get("name") or code),
            sub_type=str(entry["subType"]) if entry.get("subType") else None,
            definition_url=self._definition_url(entry),
        )

    def _definition_url(self, entry: Mapping[str, Any]) -> str:
        code = str(entry.get("key") or entry.get("serviceCode") or "")
        path = entry.get("serviceDefinitionUrlPath") or f"/data/{code}/en_US.json"
        if not isinstance(path, str):
            raise CalculatorContractUnavailable(f"invalid definition path for {code}")
        return self._absolute_url(path)

    def _absolute_url(self, path: str) -> str:
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc or not path.startswith("/") or path.startswith("//"):
            raise CalculatorContractUnavailable("AWS Calculator published an unsafe document path")
        return f"{self._base_url}{path}"

    def _build_contract(
        self,
        entry: Mapping[str, Any],
        definition: Mapping[str, Any],
        cache_status: Literal["live", "fresh", "stale"],
        *,
        previous_schema_hash: str | None = None,
    ) -> CalculatorServiceContract:
        code = str(entry.get("key") or entry.get("serviceCode") or "")
        raw_templates = definition.get("templates")
        templates: list[CalculatorTemplateContract] = []
        subservices: list[str] = []
        if isinstance(raw_templates, list):
            for raw_template in raw_templates:
                if isinstance(raw_template, str):
                    subservices.append(raw_template)
                elif isinstance(raw_template, dict):
                    template_id = str(raw_template.get("id") or "").strip()
                    if not template_id:
                        continue
                    templates.append(
                        CalculatorTemplateContract(
                            template_id=template_id,
                            title=(
                                str(raw_template["title"])
                                if raw_template.get("title") is not None
                                else None
                            ),
                            fields=tuple(self._extract_fields(raw_template)),
                        )
                    )
        return CalculatorServiceContract(
            service_code=str(definition.get("serviceCode") or code),
            name=str(definition.get("serviceName") or entry.get("name") or code),
            service_type=(
                str(definition["type"]) if definition.get("type") is not None else None
            ),
            sub_type=(
                str(definition.get("subType") or entry.get("subType"))
                if definition.get("subType") or entry.get("subType")
                else None
            ),
            definition_version=(
                str(definition["version"]) if definition.get("version") is not None else None
            ),
            definition_url=self._definition_url(entry),
            schema_hash=_canonical_hash(definition),
            schema_changed=previous_schema_hash is not None,
            previous_schema_hash=previous_schema_hash,
            cache_status=cache_status,
            templates=tuple(templates),
            subservice_codes=tuple(subservices),
        )

    def _extract_fields(self, template: Mapping[str, Any]) -> list[CalculatorFieldContract]:
        fields: list[CalculatorFieldContract] = []
        seen: set[tuple[str, str]] = set()

        def visit(
            value: Any,
            *,
            section_id: str | None = None,
            section_title: str | None = None,
        ) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(
                        item,
                        section_id=section_id,
                        section_title=section_title,
                    )
                return
            if not isinstance(value, dict):
                return
            field_id = value.get("id")
            field_type = value.get("subType") or value.get("type")
            is_input = value.get("type") in _INPUT_TYPES or value.get("subType") in (
                _INPUT_SUBTYPES
            )
            excluded = (
                _as_bool(value.get("isDisabled"))
                or field_type in _DECORATIVE_SUBTYPES
                or (isinstance(field_id, str) and "WithoutFreeTier" in field_id)
                or (isinstance(field_id, str) and "_withoutFree" in field_id)
                or (isinstance(field_id, str) and field_id.endswith("_MVP"))
            )
            if isinstance(field_id, str) and field_id and is_input and not excluded:
                dedup = (field_id, str(field_type))
                if dedup not in seen:
                    seen.add(dedup)
                    fields.append(
                        self._field_contract(
                            value,
                            str(field_type),
                            section_id=section_id,
                            section_title=section_title,
                        )
                    )
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(
                        child,
                        section_id=section_id,
                        section_title=section_title,
                    )

        raw_cards = template.get("cards")
        if isinstance(raw_cards, list):
            for index, card in enumerate(raw_cards):
                if not isinstance(card, dict):
                    continue
                visit(
                    card,
                    section_id=f"card:{index}",
                    section_title=(
                        str(card["title"])
                        if card.get("title") is not None
                        else None
                    ),
                )
            # A few official definitions also place shared controls directly
            # on the template. Preserve those without assigning a card.
            for key, value in template.items():
                if key != "cards" and isinstance(value, (dict, list)):
                    visit(value)
        else:
            visit(template)
        return fields

    @staticmethod
    def _field_contract(
        raw: Mapping[str, Any],
        field_type: str,
        *,
        section_id: str | None = None,
        section_title: str | None = None,
    ) -> CalculatorFieldContract:
        validations = raw.get("validations") if isinstance(raw.get("validations"), dict) else {}
        default = raw.get("defaultValue")
        if default is None and raw.get("defaultDropDownItem") is not None:
            default = raw.get("defaultDropDownItem")
        options: list[CalculatorFieldOption] = []
        for option in raw.get("options", []):
            if not isinstance(option, dict):
                continue
            option_id = option.get("id")
            if option_id is None:
                option_id = option.get("value")
            if option_id is None:
                continue
            options.append(
                CalculatorFieldOption(
                    option_id=str(option_id),
                    label=str(option["label"]) if option.get("label") is not None else None,
                    value=option.get("value"),
                )
            )
        valid_sizes = AwsCalculatorContractCatalog._unit_values(raw.get("dropDownSize"))
        valid_frequencies = AwsCalculatorContractCatalog._unit_values(
            raw.get("dropDownFrequency") or raw.get("dropDownDuration")
        )
        default_unit = None
        if field_type == "fileSize":
            size = raw.get("outputSize") or "gb"
            frequency = raw.get("outputFrequency") or "NA"
            default_unit = f"{size}|{frequency}"
        rows: list[CalculatorRowField] = []
        for row in raw.get("row", []):
            if not isinstance(row, dict):
                continue
            row_validations = (
                row.get("validations") if isinstance(row.get("validations"), dict) else {}
            )
            rows.append(
                CalculatorRowField(
                    selector_id=(
                        str(row["selectorId"]) if row.get("selectorId") is not None else None
                    ),
                    label=str(row["label"]) if row.get("label") is not None else None,
                    field_type=str(row["type"]) if row.get("type") is not None else None,
                    required=_as_bool(row_validations.get("required")),
                    default_value=row.get("defaultValue"),
                    min_value=_number(row_validations.get("minValue")),
                    max_value=_number(row_validations.get("maxValue")),
                    allow_decimals=AwsCalculatorContractCatalog._allow_decimals(row_validations),
                    export_value_as=(
                        str(row["exportValueAs"])
                        if row.get("exportValueAs") is not None
                        else None
                    ),
                    is_instance_type=_as_bool(row.get("isInstanceType")),
                )
            )
        return CalculatorFieldContract(
            field_id=str(raw["id"]),
            field_type=field_type,
            label=str(raw["label"]) if raw.get("label") is not None else None,
            required=_as_bool(validations.get("required")),
            default_value=default,
            placeholder=(
                str(raw["placeholder"])
                if raw.get("placeholder") not in {None, "Enter amount", "Enter the amount"}
                else None
            ),
            min_value=_number(validations.get("minValue")),
            max_value=_number(validations.get("maxValue")),
            allow_decimals=AwsCalculatorContractCatalog._allow_decimals(validations),
            unit=raw.get("unit"),
            options=tuple(options),
            valid_size_units=valid_sizes,
            valid_frequency_units=valid_frequencies,
            default_unit=default_unit,
            display_if=(dict(raw["displayIf"]) if isinstance(raw.get("displayIf"), dict) else None),
            mapping_definition_name=(
                str(raw["mappingDefinitionName"])
                if raw.get("mappingDefinitionName") is not None
                else None
            ),
            row_fields=tuple(rows),
            section_id=section_id,
            section_title=section_title,
        )

    @staticmethod
    def _unit_values(raw_options: Any) -> tuple[str, ...]:
        if not isinstance(raw_options, list):
            return ()
        values: list[str] = []
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            value = option.get("value") if option.get("value") is not None else option.get("id")
            if value is not None:
                values.append(str(value))
        return tuple(values)

    @staticmethod
    def _allow_decimals(validations: Mapping[str, Any]) -> bool | None:
        value = validations.get("allowDecimals")
        if value is None:
            value = validations.get("allowDecimal")
        if value is None:
            return None
        return _as_bool(value)

    @staticmethod
    def _select_template(
        contract: CalculatorServiceContract,
        template_id: str | None,
        errors: list[str],
    ) -> CalculatorTemplateContract | None:
        if not contract.templates:
            errors.append(
                "service has no directly configurable template; choose one of its subservices"
            )
            return None
        if template_id is not None:
            for template in contract.templates:
                if template.template_id == template_id:
                    return template
            errors.append(f'unknown calculator template "{template_id}"')
            return None
        if len(contract.templates) == 1:
            return contract.templates[0]
        errors.append("template_id is required because the service has multiple templates")
        return None

    @classmethod
    def _validate_field(
        cls,
        field: CalculatorFieldContract,
        value: Any,
    ) -> tuple[Any, list[str]]:
        errors: list[str] = []
        if field.field_type == "dropdown":
            raw_value = _inner_value(value)
            allowed = [option.option_id for option in field.options]
            if not isinstance(raw_value, str) or (allowed and raw_value not in allowed):
                errors.append(
                    f'{field.field_id}: "{raw_value}" is not an official option; allowed: {allowed}'
                )
            return {"value": raw_value}, errors

        if field.field_type in {"numericInput", "percentInput"}:
            raw_value = _inner_value(value)
            number = _number(raw_value)
            if number is not None:
                cls._validate_number(field, number, errors)
                return {"value": str(raw_value)}, errors
            if field.field_type == "percentInput" or any(
                constraint is not None
                for constraint in (field.min_value, field.max_value, field.allow_decimals)
            ):
                errors.append(f"{field.field_id}: expected a finite number")
            return {"value": raw_value}, errors

        if field.field_type in {"frequency", "durationInput"}:
            if not isinstance(value, Mapping) or "value" not in value or "unit" not in value:
                return value, [f"{field.field_id}: expected {{value, unit}}"]
            normalized = dict(value)
            numeric = _number(value.get("value"))
            if numeric is None:
                errors.append(f"{field.field_id}: expected a finite numeric value")
            else:
                cls._validate_number(field, numeric, errors)
                normalized["value"] = str(value["value"])
            allowed = [option.option_id for option in field.options]
            if field.field_type == "durationInput" and field.valid_frequency_units:
                allowed = list(field.valid_frequency_units)
            unit = str(value.get("unit"))
            if allowed and unit not in allowed:
                errors.append(
                    f'{field.field_id}: unit "{unit}" is not official; allowed: {allowed}'
                )
            return normalized, errors

        if field.field_type == "fileSize":
            if not isinstance(value, Mapping) or "value" not in value or "unit" not in value:
                return value, [f"{field.field_id}: expected {{value, unit}}"]
            normalized = dict(value)
            numeric = _number(value.get("value"))
            if numeric is None:
                errors.append(f"{field.field_id}: expected a finite numeric value")
            else:
                cls._validate_number(field, numeric, errors)
                normalized["value"] = str(value["value"])
            unit = str(value.get("unit"))
            if (
                "|" not in unit
                and unit in field.valid_size_units
                and field.default_unit
                and "|" in field.default_unit
            ):
                # The runtime contract already fixes the output frequency. A
                # size-only shorthand therefore has one exact canonical form;
                # completing it is schema normalization, not an AI default.
                _, default_frequency = field.default_unit.split("|", 1)
                unit = f"{unit}|{default_frequency}"
                normalized["unit"] = unit
            parts = unit.split("|")
            if len(parts) != 2:
                errors.append(f'{field.field_id}: unit must use "size|frequency"')
            else:
                size, frequency = parts
                if field.valid_size_units and size not in field.valid_size_units:
                    errors.append(
                        f'{field.field_id}: size unit "{size}" is not official; '
                        f"allowed: {list(field.valid_size_units)}"
                    )
                if field.valid_frequency_units and frequency not in field.valid_frequency_units:
                    errors.append(
                        f'{field.field_id}: frequency "{frequency}" is not official; '
                        f"allowed: {list(field.valid_frequency_units)}"
                    )
            return normalized, errors

        if field.field_type == "columnFormIPM":
            return cls._validate_column_form(field, value)

        if isinstance(value, Mapping):
            return dict(value), errors
        return {"value": value}, errors

    @staticmethod
    def _validate_number(
        field: CalculatorFieldContract,
        number: float,
        errors: list[str],
    ) -> None:
        if field.min_value is not None and number < field.min_value:
            errors.append(f"{field.field_id}: value is below minimum {field.min_value:g}")
        if field.max_value is not None and number > field.max_value:
            errors.append(f"{field.field_id}: value exceeds maximum {field.max_value:g}")
        if field.allow_decimals is False and not number.is_integer():
            errors.append(f"{field.field_id}: value must be an integer")

    @staticmethod
    def _validate_column_form(
        field: CalculatorFieldContract,
        value: Any,
    ) -> tuple[Any, list[str]]:
        if not isinstance(value, Mapping) or not isinstance(value.get("value"), list):
            return value, [f"{field.field_id}: expected {{value: [row, ...]}}"]
        normalized = {**value, "value": []}
        errors: list[str] = []
        for index, raw_row in enumerate(value["value"]):
            if not isinstance(raw_row, Mapping):
                errors.append(f"{field.field_id}[{index}]: row must be an object")
                continue
            row = dict(raw_row)
            for row_field in field.row_fields:
                key = row_field.selector_id or row_field.label
                if not key:
                    continue
                present = key in row and _is_present(row[key])
                if row_field.required and not present and row_field.default_value is None:
                    errors.append(f"{field.field_id}[{index}].{key}: required field is missing")
                if not present:
                    continue
                cell = row[key]
                if not isinstance(cell, Mapping) or "value" not in cell:
                    errors.append(f"{field.field_id}[{index}].{key}: expected {{value: ...}}")
                    continue
                numeric = _number(cell.get("value"))
                if numeric is not None:
                    if row_field.min_value is not None and numeric < row_field.min_value:
                        errors.append(
                            f"{field.field_id}[{index}].{key}: value is below minimum "
                            f"{row_field.min_value:g}"
                        )
                    if row_field.max_value is not None and numeric > row_field.max_value:
                        errors.append(
                            f"{field.field_id}[{index}].{key}: value exceeds maximum "
                            f"{row_field.max_value:g}"
                        )
                    if row_field.allow_decimals is False and not numeric.is_integer():
                        errors.append(f"{field.field_id}[{index}].{key}: value must be an integer")
            normalized["value"].append(row)
        return normalized, errors

    @classmethod
    def _evaluate_condition(
        cls,
        condition: Mapping[str, Any] | None,
        configuration: Mapping[str, Any],
        fields: Mapping[str, CalculatorFieldContract],
    ) -> bool | None:
        if condition is None:
            return True
        if len(condition) != 1:
            return None
        operator, operands = next(iter(condition.items()))
        if operator in {"and", "or"} and isinstance(operands, list):
            results = [cls._evaluate_condition(item, configuration, fields) for item in operands]
            if operator == "and":
                if False in results:
                    return False
                return True if all(result is True for result in results) else None
            if True in results:
                return True
            return False if all(result is False for result in results) else None
        if operator == "not":
            nested = cls._evaluate_condition(operands, configuration, fields)
            return None if nested is None else not nested
        if operator not in {"==", "!="} or not isinstance(operands, list) or len(operands) != 2:
            return None
        left = cls._condition_operand(operands[0], configuration, fields)
        right = cls._condition_operand(operands[1], configuration, fields)
        if left is _UNKNOWN or right is _UNKNOWN:
            return None
        result = left == right
        return result if operator == "==" else not result

    @staticmethod
    def _condition_operand(
        operand: Any,
        configuration: Mapping[str, Any],
        fields: Mapping[str, CalculatorFieldContract],
    ) -> Any:
        if not isinstance(operand, Mapping):
            return operand
        if operand.get("type") != "component" or not isinstance(operand.get("id"), str):
            return _UNKNOWN
        field_id = operand["id"]
        if field_id in configuration:
            return _inner_value(configuration[field_id])
        field = fields.get(field_id)
        if field is not None and field.default_value is not None:
            return field.default_value
        return _UNKNOWN


_UNKNOWN = object()
