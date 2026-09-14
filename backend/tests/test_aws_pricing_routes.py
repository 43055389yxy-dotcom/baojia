from __future__ import annotations

import json

import pytest

from app.services.aws_pricing_routes import (
    AwsPricingRouteCatalog,
    AwsPricingRouteError,
)


def _route_document() -> dict[str, object]:
    return {
        "schema_version": "astraquote-aws-pricing-route/1",
        "provider": "aws",
        "account_site": "aws-commercial",
        "partition": "aws",
        "component_id": "ec2",
        "component_name": "Amazon EC2",
        "service_code": "AmazonEC2",
        "pricing_endpoint_region": "us-east-1",
        "aliases": ["EC2"],
        "routes": [
            {
                "route_id": "ec2-shared-on-demand",
                "billing_dimension": "shared_instance_on_demand",
                "status": "ready",
                "applicability": {
                    "regions": "all_supported_commercial_regions",
                    "required_partition": "aws",
                },
                "supported_pricing_models": ["on_demand"],
                "runtime_inputs": [
                    {"key": "instance_type", "type": "string", "required": True},
                    {"key": "operation", "type": "string", "required": True},
                ],
                "api_1": {
                    "type": "aws_price_list_query",
                    "endpoint": "https://api.pricing.us-east-1.amazonaws.com",
                    "service": "pricing",
                    "version": "2017-10-15",
                    "action": "GetProducts",
                    "method": "POST",
                    "service_code": "AmazonEC2",
                    "filters": [
                        {
                            "field": "regionCode",
                            "source": "region",
                            "send_to_api": True,
                        },
                        {
                            "field": "tenancy",
                            "source": "constant",
                            "value": "Shared",
                            "send_to_api": True,
                        },
                        {
                            "field": "instanceType",
                            "source": "runtime_input",
                            "input_key": "instance_type",
                            "send_to_api": True,
                        },
                        {
                            "field": "operation",
                            "source": "runtime_input",
                            "input_key": "operation",
                            "send_to_api": True,
                        },
                    ],
                    "post_filters": [
                        {
                            "field": "servicecode",
                            "source": "constant",
                            "value": "AmazonEC2",
                            "operator": "equals",
                            "scope": "product",
                            "send_to_api": False,
                        },
                        {
                            "field": "usagetype",
                            "source": "constant",
                            "value": "(?:^|-)BoxUsage:[^:]+$",
                            "operator": "regex",
                            "scope": "product",
                            "send_to_api": False,
                        },
                    ],
                    "pagination": {"consume_all_pages": True},
                    "response_paths": {"products": "PriceList"},
                },
                "api_2": None,
                "official_price_page": "https://aws.amazon.com/ec2/pricing/",
                "official_sources": [
                    "https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetProducts.html"
                ],
                "validation": {"verified": True},
            }
        ],
    }


def _write_route(tmp_path, payload: dict[str, object]) -> None:
    (tmp_path / "aws-commercial-ec2-route.json").write_text(json.dumps(payload), encoding="utf-8")


def test_catalog_materializes_only_route_declared_filters(tmp_path) -> None:
    _write_route(tmp_path, _route_document())
    catalog = AwsPricingRouteCatalog(tmp_path)

    prepared = catalog.prepare_query(
        route_id="ec2-shared-on-demand",
        service_code="AmazonEC2",
        region="ap-southeast-1",
        pricing_model="on_demand",
        route_inputs={"instance_type": "m6i.large", "operation": "RunInstances"},
        caller_filters={},
    )

    assert prepared.filters == {
        "regionCode": "ap-southeast-1",
        "tenancy": "Shared",
        "instanceType": "m6i.large",
        "operation": "RunInstances",
    }
    assert prepared.route["route_id"] == "ec2-shared-on-demand"


def test_catalog_rejects_conflicting_caller_filter(tmp_path) -> None:
    _write_route(tmp_path, _route_document())
    catalog = AwsPricingRouteCatalog(tmp_path)

    with pytest.raises(AwsPricingRouteError) as error:
        catalog.prepare_query(
            route_id="ec2-shared-on-demand",
            service_code="AmazonEC2",
            region="ap-southeast-1",
            pricing_model="on_demand",
            route_inputs={"instance_type": "m6i.large", "operation": "RunInstances"},
            caller_filters={"tenancy": "Dedicated"},
        )

    assert error.value.code == "aws_local_route_filter_conflict"


def test_catalog_filters_official_products_without_using_descriptions(tmp_path) -> None:
    _write_route(tmp_path, _route_document())
    catalog = AwsPricingRouteCatalog(tmp_path)
    prepared = catalog.prepare_query(
        route_id="ec2-shared-on-demand",
        service_code="AmazonEC2",
        region="ap-southeast-1",
        pricing_model="on_demand",
        route_inputs={"instance_type": "m6i.large", "operation": "RunInstances"},
        caller_filters={},
    )
    matching = {
        "serviceCode": "AmazonEC2",
        "product": {
            "attributes": {
                "servicecode": "AmazonEC2",
                "usagetype": "APS1-BoxUsage:m6i.large",
            }
        },
        "terms": {
            "OnDemand": {
                "term": {"priceDimensions": {"dimension": {"pricePerUnit": {"USD": "0.10"}}}}
            }
        },
    }
    unrelated = {
        "serviceCode": "AmazonEC2",
        "product": {
            "attributes": {
                "servicecode": "AmazonEC2",
                "usagetype": "APS1-DataTransfer-Out-Bytes",
            }
        },
        "terms": {
            "OnDemand": {
                "term": {"priceDimensions": {"dimension": {"pricePerUnit": {"USD": "0.10"}}}}
            }
        },
    }

    assert catalog.filter_products([matching, unrelated], prepared) == [matching]


def test_catalog_rejects_mutating_or_non_pricing_routes(tmp_path) -> None:
    payload = _route_document()
    payload["routes"][0]["api_1"]["action"] = "RunInstances"  # type: ignore[index]
    _write_route(tmp_path, payload)

    catalog = AwsPricingRouteCatalog(tmp_path)

    assert catalog.route_count == 0
    assert catalog.rejected_file_count == 1


def test_catalog_lists_compact_route_summaries_without_network_access(tmp_path) -> None:
    _write_route(tmp_path, _route_document())
    catalog = AwsPricingRouteCatalog(tmp_path)

    result = catalog.describe(
        service_code="AmazonEC2",
        component_id="ec2",
        pricing_model="on_demand",
        route_search="shared",
        offset=0,
        limit=20,
    )

    assert result["status"] == "found"
    assert result["matched_count"] == 1
    assert result["routes"][0]["route_id"] == "ec2-shared-on-demand"
    assert result["routes"][0]["required_inputs"] == ["instance_type", "operation"]


def test_production_route_directory_contains_the_twelve_approved_components() -> None:
    catalog = AwsPricingRouteCatalog()

    assert catalog.component_ids == {
        "cloudfront",
        "dynamodb",
        "ebs-volumes",
        "ec2",
        "elasticache",
        "elb",
        "lambda",
        "msk",
        "nat-gateway",
        "opensearch",
        "rds",
        "s3",
    }
    assert catalog.route_count == 395
    assert catalog.rejected_file_count == 0
