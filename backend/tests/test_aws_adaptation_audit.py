from pathlib import Path

from app.integrations.aws_adaptation_audit import AwsAdaptationAudit
from app.integrations.aws_product_registry import AwsProductRegistry
from app.integrations.aws_public_catalog import PublicAwsPriceCatalog
from app.integrations.aws_supported_services import CURATED_SERVICE_OFFER_CODES
from app.integrations.service_templates import SERVICE_TEMPLATE_FIELDS
from tests.test_aws_public_catalog import fixture_fetch


def test_adaptation_audit_reports_all_twelve_layers(tmp_path: Path) -> None:
    registry = AwsProductRegistry(
        PublicAwsPriceCatalog(fixture_fetch),
        tmp_path / "aws-products.sqlite3",
    )
    registry.sync()
    registry.sync_region_availability(workers=2)

    report = AwsAdaptationAudit(registry).report()

    assert report["summary"]["official_product_count"] == 2
    assert report["summary"]["region_ready"] == 2
    assert report["summary"]["strictly_isolated"] == 2
    assert report["summary"]["policy_ready"] == 2
    assert [stage["number"] for stage in report["stages"]] == list(range(1, 13))
    assert all(stage["status"] != "blocked" for stage in report["stages"])


def test_every_curated_component_declares_one_official_offer_binding() -> None:
    assert set(CURATED_SERVICE_OFFER_CODES) == set(SERVICE_TEMPLATE_FIELDS)
    assert all(CURATED_SERVICE_OFFER_CODES.values())
