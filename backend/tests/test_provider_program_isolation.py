from __future__ import annotations

from fastapi.testclient import TestClient

from app.aws_main import app as aws_app
from app.azure_main import app as azure_app
from app.core.data_paths import AWS_DATA_ROOT, AZURE_DATA_ROOT
from app.main import app as default_app


def test_provider_programs_use_different_physical_data_roots() -> None:
    assert AWS_DATA_ROOT != AZURE_DATA_ROOT
    assert AWS_DATA_ROOT.name == "aws"
    assert AZURE_DATA_ROOT.name == "azure"


def test_historical_default_entrypoint_is_aws_only() -> None:
    assert default_app is aws_app
    assert default_app.title == "AWS 智能报价 API"


def test_aws_program_rejects_azure_tasks_and_links() -> None:
    with TestClient(aws_app) as client:
        quote_response = client.post(
            "/api/quotes/preview",
            json={
                "cloud_provider": "azure",
                "customer_request": "1、Azure Virtual Machines",
            },
        )
        link_response = client.get("/api/confirmation-sessions/azure_example")

    assert quote_response.status_code == 403
    assert quote_response.json()["code"] == "provider_boundary_violation"
    assert link_response.status_code == 403


def test_azure_program_rejects_aws_tasks_and_links() -> None:
    with TestClient(azure_app) as client:
        quote_response = client.post(
            "/api/quotes/preview",
            json={
                "cloud_provider": "aws",
                "customer_request": "1、Amazon EC2",
            },
        )
        link_response = client.get("/api/confirmation-sessions/aws_example")

    assert quote_response.status_code == 403
    assert quote_response.json()["code"] == "provider_boundary_violation"
    assert link_response.status_code == 403
