from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from app.core.errors import ManualConfirmationRequired
from app.domain.models import (
    CandidateOption,
    PreviewSelection,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
)
from app.integrations.aws import AwsClients, PricingCatalog


class ServicePlugin(ABC):
    kind: ServiceKind
    display_name: str

    def __init__(self, clients: AwsClients, catalog: PricingCatalog):
        self.clients = clients
        self.catalog = catalog

    @abstractmethod
    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        """Validate the request against official APIs and return exactly one selection."""

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        """Return candidates for services that have not implemented richer ranking yet."""

        selection = self.select(requirement, default_region)
        option = CandidateOption(
            model=selection.model,
            family=str(selection.service),
            specifications=selection.specifications,
            rationale=selection.rationale,
            official_product=selection.official_product,
            is_default=True,
        )
        return PreviewSelection(
            component_id="component",
            service=self.kind,
            display_name=selection.display_name,
            region=selection.region,
            requested_model=requirement.requirements.get("requested_model"),
            selected_model=selection.model,
            selection_reason=selection.rationale,
            candidates=[option],
            requires_confirmation=bool(selection.substitution_notice),
            confirmation_reason=selection.substitution_notice,
        )


class PluginRegistry:
    def __init__(self, plugins: Iterable[ServicePlugin] = ()):
        self._plugins: dict[ServiceKind, ServicePlugin] = {}
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: ServicePlugin) -> None:
        if plugin.kind in self._plugins:
            raise ValueError(f"Plugin already registered: {plugin.kind}")
        self._plugins[plugin.kind] = plugin

    def get(self, kind: ServiceKind) -> ServicePlugin:
        try:
            return self._plugins[kind]
        except KeyError as exc:
            raise ManualConfirmationRequired(
                f"服务 {kind} 尚未安装报价插件",
                code="unsupported_service",
                service=kind,
            ) from exc

    def list(self) -> list[dict[str, str]]:
        return [
            {"id": kind.value, "name": plugin.display_name}
            for kind, plugin in sorted(self._plugins.items(), key=lambda item: item[0].value)
        ]


def required_float(requirements: dict[str, object], key: str) -> float | None:
    value = requirements.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ManualConfirmationRequired(
            f"需求字段 {key} 必须是数值", code="invalid_requirement", field=key
        )
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ManualConfirmationRequired(
            f"需求字段 {key} 必须是数值", code="invalid_requirement", field=key
        ) from exc
    if number <= 0:
        raise ManualConfirmationRequired(
            f"需求字段 {key} 必须大于 0", code="invalid_requirement", field=key
        )
    return number


def required_int(requirements: dict[str, object], key: str, default: int) -> int:
    value = requirements.get(key, default)
    if isinstance(value, bool):
        raise ManualConfirmationRequired(
            f"需求字段 {key} 必须是整数", code="invalid_requirement", field=key
        )
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ManualConfirmationRequired(
            f"需求字段 {key} 必须是整数", code="invalid_requirement", field=key
        ) from exc
    if number < 0:
        raise ManualConfirmationRequired(
            f"需求字段 {key} 不能小于 0", code="invalid_requirement", field=key
        )
    return number
