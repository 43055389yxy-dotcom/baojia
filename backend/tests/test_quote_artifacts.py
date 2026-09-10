from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app import aws_main
from app.services.quote_artifacts import QuoteArtifactError, QuoteArtifactStore


def test_private_artifact_manifest_resolves_a_stable_download_token(tmp_path) -> None:
    token = "aqdl_" + "a" * 48
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    (tmp_path / f"{token}.json").write_text(
        json.dumps(
            {
                "schema_version": "astraquote-artifact/1",
                "token": token,
                "quote_id": "aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "bucket": "private-bucket",
                "region": "ap-east-1",
                "key": "quotes/private.xlsx",
                "filename": "正式报价.xlsx",
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "expires_at": expires_at,
            }
        ),
        encoding="utf-8",
    )

    artifact = QuoteArtifactStore(tmp_path).get(token)

    assert artifact["bucket"] == "private-bucket"
    assert artifact["filename"] == "正式报价.xlsx"


def test_artifact_token_rejects_expired_or_path_traversal_requests(tmp_path) -> None:
    token = "aqdl_" + "b" * 48
    (tmp_path / f"{token}.json").write_text(
        json.dumps(
            {
                "schema_version": "astraquote-artifact/1",
                "token": token,
                "quote_id": "aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "bucket": "private-bucket",
                "region": "ap-east-1",
                "key": "quotes/private.xlsx",
                "filename": "正式报价.xlsx",
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(QuoteArtifactError) as expired:
        QuoteArtifactStore(tmp_path).get(token)
    assert expired.value.code == "quote_artifact_expired"

    with pytest.raises(QuoteArtifactError) as invalid:
        QuoteArtifactStore(tmp_path).get("../../etc/passwd")
    assert invalid.value.code == "quote_artifact_token_invalid"


def test_download_route_ignores_chat_tracking_query_parameters(monkeypatch) -> None:
    token = "aqdl_" + "c" * 48

    class Artifacts:
        @staticmethod
        def get(received: str):
            assert received == token
            return {
                "bucket": "private-bucket",
                "region": "ap-east-1",
                "key": "quotes/private.xlsx",
                "filename": "正式报价.xlsx",
                "content_type": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
            }

    class S3:
        @staticmethod
        def get_object(**kwargs):
            assert kwargs == {"Bucket": "private-bucket", "Key": "quotes/private.xlsx"}
            return {"Body": BytesIO(b"xlsx-bytes")}

    class Clients:
        @staticmethod
        def regional(service: str, region: str):
            assert (service, region) == ("s3", "ap-east-1")
            return S3()

    monkeypatch.setattr(aws_main, "quote_artifacts", Artifacts())
    monkeypatch.setattr(aws_main, "clients", Clients())

    response = TestClient(aws_main.app).get(
        f"/api/quote-artifacts/{token}?utm_source=chatgpt.com"
    )

    assert response.status_code == 200
    assert response.content == b"xlsx-bytes"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
