"""File-backed queue shared with the host-side ChatGPT browser worker.

The backend container only accepts sales requests and exposes progress.  The
worker on the desktop host owns the logged-in browser.  Raw customer text is
kept only while queued and is removed as soon as the worker has submitted the
first-pass cleaning prompt.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc  # noqa: UP017 - the host-side worker still supports Python 3.9

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
DELIVERY_RECEIPT_STATUSES = {
    "delivered",
    "page_result_ready",
}
PUBLIC_FIELDS = {
    "job_id",
    "status",
    "created_at",
    "updated_at",
    "submission_code",
    "cloud_provider",
    "preferred_region",
    "failure_code",
    "display_result_on_page",
    "quick_quote_result",
    "quote_download_url",
    "quote_download_filename",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class GptRelayError(RuntimeError):
    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class GptQuoteRelayStore:
    def __init__(self, directory: Path | str | None = None) -> None:
        owner_uid = os.environ.get("ASTRAQUOTE_GPT_RELAY_UID")
        owner_gid = os.environ.get("ASTRAQUOTE_GPT_RELAY_GID")
        self.owner_uid = int(owner_uid) if owner_uid and owner_uid.isdigit() else None
        self.owner_gid = int(owner_gid) if owner_gid and owner_gid.isdigit() else None
        configured_directory = directory or os.environ.get("ASTRAQUOTE_GPT_RELAY_DIR")
        if configured_directory:
            self.directory = Path(configured_directory)
        else:
            data_root = Path("/data")
            self.directory = (
                data_root / "gpt-relay"
                if data_root.is_dir() and os.access(data_root, os.W_OK)
                else Path(__file__).resolve().parents[3] / ".data" / "gpt-relay"
            )
        self.jobs_directory = self.directory / "jobs"
        self.completions_directory = self.directory / "completions"
        self.requests_directory = self.directory / "requests"
        self.jobs_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.completions_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.requests_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._set_owner(self.directory)
        self._set_owner(self.jobs_directory)
        self._set_owner(self.completions_directory)
        self._set_owner(self.requests_directory)
        self.lock_path = self.directory / ".queue.lock"
        self.heartbeat_path = self.directory / "worker-heartbeat.json"

    def _set_owner(self, path: Path | str) -> None:
        if self.owner_uid is None and self.owner_gid is None:
            return
        os.chown(
            path,
            self.owner_uid if self.owner_uid is not None else -1,
            self.owner_gid if self.owner_gid is not None else -1,
        )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_path.touch(mode=0o600, exist_ok=True)
        self._set_owner(self.lock_path)
        with self.lock_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _path(self, job_id: str) -> Path:
        if not job_id.startswith("gpt-") or not job_id[4:].isalnum():
            raise GptRelayError("无效的 GPT 报价任务编号。", code="gpt_relay_job_id_invalid")
        return self.jobs_directory / f"{job_id}.json"

    def _completion_path(self, job_id: str) -> Path:
        self._path(job_id)
        return self.completions_directory / f"{job_id}.json"

    def _request_path(self, client_request_id: str) -> Path:
        try:
            normalized = str(uuid.UUID(client_request_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise GptRelayError(
                "无效的客户端提交编号。", code="gpt_relay_client_request_id_invalid"
            ) from exc
        return self.requests_directory / f"{normalized}.json"

    def _write_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            self._set_owner(temporary)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _event(stage: str, message: str) -> dict[str, str]:
        return {"stage": stage, "message": message, "time": utc_now()}

    def create(
        self,
        customer_request: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        text = customer_request.strip()
        if len(text) < 3 or len(text) > 12000:
            raise GptRelayError(
                "客户需求长度必须在 3 到 12000 个字符之间。",
                code="gpt_relay_request_invalid",
            )
        job_id = f"gpt-{uuid.uuid4().hex}"
        now = utc_now()
        with self._lock():
            client_request_id = str(options.get("client_request_id") or uuid.uuid4())
            request_path = self._request_path(client_request_id)
            if request_path.exists():
                try:
                    request_index = self._read(request_path)
                    existing_path = self._path(str(request_index.get("job_id") or ""))
                    if existing_path.exists():
                        return self.public(self._read(existing_path))
                except (OSError, ValueError, TypeError, GptRelayError):
                    pass
            active_codes: set[str] = set()
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    existing = self._read(path)
                except (OSError, ValueError):
                    continue
                if existing.get("status") not in TERMINAL_STATUSES:
                    code = str(existing.get("submission_code") or "")
                    if code in {str(value) for value in range(1, 10)}:
                        active_codes.add(code)
            available = [str(value) for value in range(1, 10) if str(value) not in active_codes]
            submission_code = secrets.choice(available or [str(value) for value in range(1, 10)])
            record = {
                "schema_version": "astraquote-gpt-relay/2",
                "job_id": job_id,
                "submission_code": submission_code,
                "cloud_provider": str(options.get("cloud_provider") or "aws"),
                "preferred_region": str(options.get("preferred_region") or ""),
                "display_result_on_page": True,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "customer_request": text,
                "quote_options": options,
                "source_purged_at": None,
                "events": [self._event("queue", "报价申请已进入队列")],
                "chat_url": None,
                "project_name": None,
                "result_summary": None,
                "error": None,
                "continuation_attempts": 0,
            }
            self._write_atomic(self._path(job_id), record)
            self._write_atomic(
                request_path,
                {"client_request_id": client_request_id, "job_id": job_id, "created_at": now},
            )
        return self.public(record)

    def get(self, job_id: str) -> dict[str, Any]:
        path = self._path(job_id)
        if not path.exists():
            raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
        return self._read(path)

    def public(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = {key: record.get(key) for key in PUBLIC_FIELDS}
        payload["failure_code"] = (
            "AQ-QUOTE-FAILED" if record.get("status") == "failed" else None
        )
        return payload

    def public_get(self, job_id: str) -> dict[str, Any]:
        # A browser-worker heartbeat is transport health, not quote outcome.
        # Keeping the job non-terminal lets a restarted worker reattach to the
        # saved ChatGPT conversation and continue from its persisted checkpoint.
        record = self.reconcile_delivery_receipt(job_id)
        return self.public(record)

    def reconcile_delivery_receipt(self, job_id: str) -> dict[str, Any]:
        """Prefer a verified MCP delivery receipt over brittle chat prose.

        The receipt is written only after the private workbook and stable sales
        page link exist. It is bound to the relay job id and submission code,
        and it repairs an earlier false browser failure. Cancellation wins.
        """

        with self._lock():
            job_path = self._path(job_id)
            if not job_path.exists():
                raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
            record = self._read(job_path)
            if record.get("status") == "cancelled":
                return record
            if (
                record.get("status") == "completed"
                and record.get("quick_quote_result")
                and record.get("quote_download_url")
            ):
                return record
            receipt_path = self._completion_path(job_id)
            if not receipt_path.exists() or not record.get("source_purged_at"):
                return record
            try:
                receipt = self._read(receipt_path)
            except (OSError, ValueError, TypeError):
                return record
            if (
                receipt.get("schema_version") != "astraquote-relay-completion/1"
                or receipt.get("job_id") != job_id
                or str(receipt.get("submission_code") or "")
                != str(record.get("submission_code") or "")
                or receipt.get("status") not in DELIVERY_RECEIPT_STATUSES
                or not str(receipt.get("quote_id") or "").startswith("aqv2_")
                or not str(receipt.get("delivered_at") or "").strip()
            ):
                return record
            page_result = self._page_result_public(receipt.get("page_result"))
            download_url = self._download_url_public(receipt.get("spreadsheet_url"))
            if page_result is None or download_url is None:
                return record
            record.update(
                {
                    "status": "completed",
                    "result_status": receipt["status"],
                    "result_summary": "报价结果和 Excel 已生成。",
                    "quick_quote_result": page_result,
                    "quote_download_url": download_url,
                    "quote_download_filename": str(
                        receipt.get("spreadsheet_filename") or "报价单.xlsx"
                    )[:220],
                    "error": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                    "events": [
                        *(record.get("events") or []),
                        self._event(
                            "completed",
                            "报价结果和 Excel 已生成",
                        ),
                    ][-100:],
                }
            )
            self._write_atomic(job_path, record)
            return record

    @staticmethod
    def _page_result_public(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or value.get("schema_version") != "astraquote-page-result/1":
            return None
        currency = str(value.get("currency") or "")
        if len(currency) != 3 or not currency.isalpha() or currency.upper() != currency:
            return None
        components = value.get("components")
        scenarios = value.get("scenarios")
        if not isinstance(components, list) or not 1 <= len(components) <= 50:
            return None
        if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
            return None
        allowed_scenarios = {
            "on_demand",
            "one_year_commitment",
            "three_year_commitment",
            "one_year_all_upfront",
            "three_year_all_upfront",
        }
        public_components = []
        for component in components:
            if not isinstance(component, dict):
                return None
            service_name = str(component.get("service_name") or "").strip()
            if not service_name:
                return None
            costs = component.get("scenario_costs")
            if not isinstance(costs, list) or len(costs) > 3:
                return None
            public_costs = []
            for cost in costs:
                if not isinstance(cost, dict) or cost.get("scenario_key") not in allowed_scenarios:
                    return None
                try:
                    monthly = float(cost["monthly_cost"])
                    upfront = float(cost.get("upfront_cost", 0))
                except (KeyError, TypeError, ValueError):
                    return None
                if monthly < 0 or upfront < 0:
                    return None
                public_costs.append(
                    {
                        "scenario_key": cost["scenario_key"],
                        "label": str(cost.get("label") or "")[:40],
                        "monthly_cost": str(cost["monthly_cost"]),
                        "upfront_cost": str(cost.get("upfront_cost", "0")),
                    }
                )
            public_components.append(
                {
                    "service_name": service_name[:120],
                    "model_or_plan": str(component.get("model_or_plan") or "")[:160],
                    "quantity": str(component.get("quantity") or "")[:80],
                    "configuration_summary": str(
                        component.get("configuration_summary") or ""
                    )[:1200],
                    "scenario_costs": public_costs,
                }
            )
        public_scenarios = []
        for scenario in scenarios:
            if (
                not isinstance(scenario, dict)
                or scenario.get("scenario_key") not in allowed_scenarios
            ):
                return None
            try:
                monthly = float(scenario["monthly_total"])
                upfront = float(scenario["upfront_total"])
            except (KeyError, TypeError, ValueError):
                return None
            if monthly < 0 or upfront < 0:
                return None
            public_scenarios.append(
                {
                    "scenario_key": scenario["scenario_key"],
                    "label": str(scenario.get("label") or "")[:40],
                    "monthly_total": str(scenario["monthly_total"]),
                    "upfront_total": str(scenario["upfront_total"]),
                }
            )
        return {
            "schema_version": "astraquote-page-result/1",
            "currency": currency,
            "region": str(value.get("region") or "")[:32],
            "preferred_region": str(value.get("preferred_region") or "")[:80],
            "region_adjustment_reason": str(
                value.get("region_adjustment_reason") or ""
            )[:500],
            "components": public_components,
            "scenarios": public_scenarios,
        }

    @staticmethod
    def _download_url_public(value: Any) -> str | None:
        from urllib.parse import urlsplit

        text = str(value or "").strip()
        try:
            parsed = urlsplit(text)
        except ValueError:
            return None
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            return None
        if not re.fullmatch(
            r"/api/backend/api/quote-artifacts/aqdl_[a-f0-9]{48}", parsed.path
        ):
            return None
        return text

    def update(
        self,
        job_id: str,
        changes: dict[str, Any],
        *,
        stage: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        with self._lock():
            path = self._path(job_id)
            if not path.exists():
                raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
            record = self._read(path)
            record.update(changes)
            record["updated_at"] = utc_now()
            if stage and message:
                record["events"] = [
                    *(record.get("events") or []),
                    self._event(stage, message),
                ][-100:]
            self._write_atomic(path, record)
        return record

    def update_if_not_cancelled(
        self,
        job_id: str,
        changes: dict[str, Any],
        *,
        stage: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Apply a worker transition without ever reviving a cancelled job."""

        with self._lock():
            path = self._path(job_id)
            if not path.exists():
                raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
            record = self._read(path)
            if record.get("status") == "cancelled":
                return record
            record.update(changes)
            record["updated_at"] = utc_now()
            if stage and message:
                record["events"] = [
                    *(record.get("events") or []),
                    self._event(stage, message),
                ][-100:]
            self._write_atomic(path, record)
            return record

    def claim_next(self, worker_id: str, lease_minutes: int = 30) -> dict[str, Any] | None:
        with self._lock():
            now = datetime.now(UTC)
            candidates: list[tuple[str, Path, dict[str, Any]]] = []
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    record = self._read(path)
                except (OSError, ValueError):
                    continue
                status = record.get("status")
                lease_text = record.get("lease_expires_at")
                expired = False
                if status == "processing" and lease_text:
                    try:
                        expired = datetime.fromisoformat(lease_text) <= now
                    except ValueError:
                        expired = True
                if status == "queued" or expired:
                    candidates.append((str(record.get("created_at") or ""), path, record))
            if not candidates:
                return None
            _, path, record = min(candidates, key=lambda item: item[0])
            record.update(
                {
                    "status": "processing",
                    "worker_id": worker_id,
                    "lease_expires_at": (now + timedelta(minutes=lease_minutes)).isoformat(),
                    "updated_at": utc_now(),
                    "events": [
                        *(record.get("events") or []),
                        self._event("browser", "报价任务已开始处理"),
                    ][-100:],
                }
            )
            self._write_atomic(path, record)
            return record

    def renew_lease(
        self, job_id: str, worker_id: str, *, lease_minutes: int = 35
    ) -> dict[str, Any]:
        """Keep a genuinely active browser tab from being reclaimed as abandoned."""

        with self._lock():
            path = self._path(job_id)
            record = self._read(path)
            if record.get("status") != "processing" or record.get("worker_id") != worker_id:
                return record
            now = datetime.now(UTC)
            try:
                expires = datetime.fromisoformat(str(record.get("lease_expires_at") or ""))
            except ValueError:
                expires = now
            if expires - now > timedelta(minutes=max(1, lease_minutes // 2)):
                return record
            record["lease_expires_at"] = (now + timedelta(minutes=lease_minutes)).isoformat()
            record["updated_at"] = utc_now()
            self._write_atomic(path, record)
            return record

    def claim_submitted_for_monitoring(
        self,
        worker_id: str,
        *,
        limit: int | None,
        lease_minutes: int = 30,
    ) -> list[dict[str, Any]]:
        """Reattach submitted conversations after the browser worker restarts.

        Their source text has already been purged, so they must be monitored by
        the saved conversation URL instead of being submitted a second time.
        """

        if limit is not None and limit < 1:
            return []
        claimed: list[dict[str, Any]] = []
        with self._lock():
            candidates: list[tuple[str, Path, dict[str, Any]]] = []
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    record = self._read(path)
                except (OSError, ValueError):
                    continue
                if (
                    record.get("status") == "processing"
                    and record.get("source_purged_at")
                    and str(record.get("chat_url") or "").strip()
                ):
                    candidates.append((str(record.get("created_at") or ""), path, record))
            now = datetime.now(UTC)
            ordered = sorted(candidates, key=lambda item: item[0])
            selected = ordered if limit is None else ordered[:limit]
            for _, path, record in selected:
                record.update(
                    {
                        "worker_id": worker_id,
                        "lease_expires_at": (now + timedelta(minutes=lease_minutes)).isoformat(),
                        "updated_at": utc_now(),
                        "events": [
                            *(record.get("events") or []),
                            self._event("browser", "报价监控已自动恢复"),
                        ][-100:],
                    }
                )
                self._write_atomic(path, record)
                claimed.append(record)
        return claimed

    def purge_source(self, job_id: str) -> dict[str, Any]:
        return self.update(
            job_id,
            {"customer_request": "", "source_purged_at": utc_now()},
            stage="intake",
            message="报价需求已进入安全处理阶段",
        )

    def cancel(self, job_id: str) -> dict[str, Any]:
        record = self.get(job_id)
        if record.get("status") in TERMINAL_STATUSES:
            return self.public(record)
        updated = self.update(
            job_id,
            {"status": "cancelled", "customer_request": "", "source_purged_at": utc_now()},
            stage="cancelled",
            message="报价任务已取消",
        )
        return self.public(updated)

    def resume_login_waiting(self) -> int:
        """Return login-blocked jobs to the queue after the browser signs in."""
        resumed = 0
        with self._lock():
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    record = self._read(path)
                except (OSError, ValueError):
                    continue
                if record.get("status") != "needs_login":
                    continue
                record.update(
                    {
                        "status": "queued",
                        "worker_id": None,
                        "lease_expires_at": None,
                        "updated_at": utc_now(),
                        "events": [
                            *(record.get("events") or []),
                            self._event("login", "报价服务已恢复，任务自动继续"),
                        ][-100:],
                    }
                )
                self._write_atomic(path, record)
                resumed += 1
        return resumed

    def mark_queued_needs_login(self) -> int:
        """Expose an expired browser login without dropping queued source text."""
        marked = 0
        with self._lock():
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    record = self._read(path)
                except (OSError, ValueError):
                    continue
                if record.get("status") != "queued":
                    continue
                record.update(
                    {
                        "status": "needs_login",
                        "worker_id": None,
                        "lease_expires_at": None,
                        "updated_at": utc_now(),
                        "events": [
                            *(record.get("events") or []),
                            self._event("login", "报价服务暂时不可用，等待管理员处理"),
                        ][-100:],
                    }
                )
                self._write_atomic(path, record)
                marked += 1
        return marked

    def health(self) -> dict[str, Any]:
        if not self.heartbeat_path.exists():
            return {"status": "offline", "message": "报价服务暂时不可用"}
        try:
            heartbeat = self._read(self.heartbeat_path)
            updated = datetime.fromisoformat(str(heartbeat["updated_at"]))
            fresh = datetime.now(UTC) - updated < timedelta(seconds=45)
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "offline", "message": "报价服务暂时不可用"}
        logged_in = bool(heartbeat.get("logged_in"))
        ready = fresh and logged_in
        return {
            "status": "ready" if ready else "offline",
            "message": "服务正常" if ready else "报价服务暂时不可用",
            "updated_at": heartbeat.get("updated_at"),
        }
