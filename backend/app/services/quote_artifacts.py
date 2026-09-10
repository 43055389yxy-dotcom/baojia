"""Resolve opaque public download tokens to private S3 quote artifacts."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOKEN_PATTERN = re.compile(r"^aqdl_[a-f0-9]{48}$")
MANIFEST_SCHEMA = "astraquote-artifact/1"


class QuoteArtifactError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class QuoteArtifactStore:
    def __init__(self, directory: Path | str | None = None) -> None:
        configured = directory or os.environ.get("ASTRAQUOTE_ARTIFACT_DIR")
        if configured:
            self.directory = Path(configured)
        else:
            state_root = Path(os.environ.get("ASTRAQUOTE_V2_STATE_DIR", "/data/v2-quotes"))
            self.directory = state_root / "artifacts"

    def get(self, token: str) -> dict[str, Any]:
        if not TOKEN_PATTERN.fullmatch(token):
            raise QuoteArtifactError(
                "报价文件下载地址无效。", code="quote_artifact_token_invalid"
            )
        target = self.directory / f"{token}.json"
        try:
            artifact = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise QuoteArtifactError(
                "报价文件不存在或已失效。", code="quote_artifact_not_found"
            ) from exc
        required = ("quote_id", "bucket", "region", "key", "filename", "content_type")
        if (
            artifact.get("schema_version") != MANIFEST_SCHEMA
            or artifact.get("token") != token
            or not all(str(artifact.get(field) or "").strip() for field in required)
        ):
            raise QuoteArtifactError(
                "报价文件记录无效。", code="quote_artifact_manifest_invalid"
            )
        try:
            expires_at = datetime.fromisoformat(str(artifact["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise QuoteArtifactError(
                "报价文件记录无效。", code="quote_artifact_manifest_invalid"
            ) from exc
        if expires_at <= datetime.now(UTC):
            raise QuoteArtifactError(
                "报价文件下载地址已过期。", code="quote_artifact_expired"
            )
        return artifact
