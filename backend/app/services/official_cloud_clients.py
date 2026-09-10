from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx


class OfficialCloudClientError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _json_body(value: dict[str, Any]) -> bytes:
    if not value:
        return b""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _parameter_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return str(value)


def _encoded_query(parameters: dict[str, Any]) -> str:
    pairs = [
        (quote(str(key), safe="-_.~"), quote(_parameter_text(value), safe="-_.~"))
        for key, value in parameters.items()
    ]
    return "&".join(f"{key}={value}" for key, value in sorted(pairs))


class OfficialCloudApiClient:
    """Sign and send one caller-specified read-only official API request.

    Endpoint/action/path safety is validated by the Pydantic query boundary.
    This class only authenticates and transports the exact request supplied by
    GPT; it does not select products, fill defaults, convert usage or calculate
    prices.
    """

    def __init__(
        self,
        credentials: dict[str, dict[str, str]],
        *,
        request: Callable[..., Any] = httpx.request,
    ) -> None:
        self._credentials = credentials
        self._request = request

    def execute(self, query: Any) -> dict[str, Any]:
        credentials = self._credentials.get(query.provider) or {}
        access_key = str(credentials.get("access_key_id") or "")
        secret_key = str(credentials.get("secret_access_key") or "")
        if not access_key or not secret_key:
            raise OfficialCloudClientError(
                f"{query.provider} official API credentials are not configured",
                code=f"{query.provider}_credentials_not_configured",
            )
        method, url, params, content, headers = getattr(
            self, f"_prepare_{query.provider}"
        )(query, access_key, secret_key)
        response = self._request(
            method,
            url,
            params=params or None,
            content=content or None,
            headers=headers,
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise OfficialCloudClientError(
                "Official cloud API returned a non-object JSON payload",
                code="official_catalog_invalid_response",
            )
        return payload

    @staticmethod
    def _prepare_tencent(
        query: Any, access_key: str, secret_key: str
    ) -> tuple[str, str, dict[str, Any], bytes, dict[str, str]]:
        method = query.method.upper()
        body = _json_body(query.body) if method == "POST" else b""
        params = dict(query.query_parameters) if method == "GET" else {}
        timestamp = int(datetime.now(UTC).timestamp())
        date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
        content_type = "application/json; charset=utf-8"
        canonical_headers = f"content-type:{content_type}\nhost:{query.endpoint}\n"
        canonical_request = "\n".join(
            [
                method,
                query.path,
                _encoded_query(params),
                canonical_headers,
                "content-type;host",
                _sha256(body),
            ]
        )
        scope = f"{date}/{query.service}/tc3_request"
        string_to_sign = "\n".join(
            ["TC3-HMAC-SHA256", str(timestamp), scope, _sha256(canonical_request.encode())]
        )
        date_key = _hmac(("TC3" + secret_key).encode(), date)
        service_key = _hmac(date_key, query.service)
        signing_key = _hmac(service_key, "tc3_request")
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers = {
            "Authorization": (
                "TC3-HMAC-SHA256 "
                f"Credential={access_key}/{scope}, SignedHeaders=content-type;host, "
                f"Signature={signature}"
            ),
            "Content-Type": content_type,
            "Host": query.endpoint,
            "X-TC-Action": query.action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": str(query.version),
            "X-TC-Region": query.region,
        }
        return method, f"https://{query.endpoint}{query.path}", params, body, headers

    @staticmethod
    def _prepare_alibaba(
        query: Any, access_key: str, secret_key: str
    ) -> tuple[str, str, dict[str, Any], bytes, dict[str, str]]:
        method = query.method.upper()
        parameters = {
            "AccessKeyId": access_key,
            "Action": query.action,
            "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": str(query.version),
            **query.query_parameters,
        }
        if query.region and not any(
            key.casefold() == "regionid" for key in parameters
        ):
            parameters["RegionId"] = query.region
        parameters.update(query.body)
        canonical = _encoded_query(parameters)
        string_to_sign = (
            f"{method}&%2F&{quote(canonical, safe='-_.~')}"
        )
        digest = hmac.new(
            f"{secret_key}&".encode(), string_to_sign.encode(), hashlib.sha1
        ).digest()
        parameters["Signature"] = base64.b64encode(digest).decode()
        return (
            method,
            f"https://{query.endpoint}{query.path}",
            parameters,
            b"",
            {"Accept": "application/json", "Host": query.endpoint},
        )

    @staticmethod
    def _prepare_huawei(
        query: Any, access_key: str, secret_key: str
    ) -> tuple[str, str, dict[str, Any], bytes, dict[str, str]]:
        method = query.method.upper()
        params = dict(query.query_parameters)
        body = _json_body(query.body)
        sdk_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "Content-Type": "application/json",
            "Host": query.endpoint,
            "X-Sdk-Date": sdk_date,
        }
        signed_names = sorted(key.lower() for key in headers)
        lower_headers = {key.lower(): value.strip() for key, value in headers.items()}
        canonical_headers = "".join(
            f"{key}:{lower_headers[key]}\n" for key in signed_names
        )
        canonical_uri = "/".join(
            quote(part, safe="-_.~") for part in query.path.split("/")
        )
        if not canonical_uri.endswith("/"):
            canonical_uri += "/"
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                _encoded_query(params),
                canonical_headers,
                ";".join(signed_names),
                _sha256(body),
            ]
        )
        string_to_sign = "\n".join(
            ["SDK-HMAC-SHA256", sdk_date, _sha256(canonical_request.encode())]
        )
        signature = hmac.new(
            secret_key.encode(), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        headers["Authorization"] = (
            f"SDK-HMAC-SHA256 Access={access_key}, "
            f"SignedHeaders={';'.join(signed_names)}, Signature={signature}"
        )
        return method, f"https://{query.endpoint}{query.path}", params, body, headers

    @staticmethod
    def _prepare_baidu(
        query: Any, access_key: str, secret_key: str
    ) -> tuple[str, str, dict[str, Any], bytes, dict[str, str]]:
        method = query.method.upper()
        params = dict(query.query_parameters)
        body = _json_body(query.body)
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        auth_prefix = f"bce-auth-v1/{access_key}/{timestamp}/1800"
        signing_key = hmac.new(
            secret_key.encode(), auth_prefix.encode(), hashlib.sha256
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "Host": query.endpoint,
            "x-bce-date": timestamp,
        }
        signed_names = sorted(key.lower() for key in headers)
        lower_headers = {key.lower(): value for key, value in headers.items()}
        canonical_headers = "\n".join(
            f"{quote(key, safe='-_.~')}:{quote(lower_headers[key].strip(), safe='-_.~')}"
            for key in signed_names
        )
        canonical_uri = quote(query.path, safe="/-_.~")
        canonical_request = "\n".join(
            [method, canonical_uri, _encoded_query(params), canonical_headers]
        )
        signature = hmac.new(
            signing_key.encode(), canonical_request.encode(), hashlib.sha256
        ).hexdigest()
        headers["Authorization"] = (
            f"{auth_prefix}/{';'.join(signed_names)}/{signature}"
        )
        return method, f"https://{query.endpoint}{query.path}", params, body, headers

    @staticmethod
    def _prepare_volcengine(
        query: Any, access_key: str, secret_key: str
    ) -> tuple[str, str, dict[str, Any], bytes, dict[str, str]]:
        method = query.method.upper()
        params = {
            "Action": query.action,
            "Version": str(query.version),
            **query.query_parameters,
        }
        body = _json_body(query.body)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "Content-Type": "application/json",
            "Host": query.endpoint,
            "X-Date": timestamp,
            "X-Content-Sha256": _sha256(body),
        }
        signed_names = sorted(key.lower() for key in headers)
        lower_headers = {key.lower(): value for key, value in headers.items()}
        canonical_headers = "".join(
            f"{key}:{lower_headers[key]}\n" for key in signed_names
        )
        canonical_request = "\n".join(
            [
                method,
                query.path,
                _encoded_query(params),
                canonical_headers,
                ";".join(signed_names),
                _sha256(body),
            ]
        )
        date = timestamp[:8]
        scope = f"{date}/{query.region}/{query.service}/request"
        string_to_sign = "\n".join(
            ["HMAC-SHA256", timestamp, scope, _sha256(canonical_request.encode())]
        )
        date_key = _hmac(secret_key.encode(), date)
        region_key = _hmac(date_key, query.region)
        service_key = _hmac(region_key, query.service)
        signing_key = _hmac(service_key, "request")
        signature = hmac.new(
            signing_key, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        headers["Authorization"] = (
            f"HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={';'.join(signed_names)}, Signature={signature}"
        )
        return method, f"https://{query.endpoint}{query.path}", params, body, headers

    @staticmethod
    def _prepare_ctyun(
        query: Any, access_key: str, secret_key: str
    ) -> tuple[str, str, dict[str, Any], bytes, dict[str, str]]:
        method = query.method.upper()
        params = dict(query.query_parameters)
        body = _json_body(query.body)
        beijing = timezone(timedelta(hours=8))
        eop_date = datetime.now(beijing).strftime("%Y%m%dT%H%M%SZ")
        request_id = str(uuid.uuid4())
        signed_header_names = "ctyun-eop-request-id;eop-date"
        header_text = (
            f"ctyun-eop-request-id:{request_id}\n"
            f"eop-date:{eop_date}\n"
        )
        signature_text = f"{header_text}\n{_encoded_query(params)}\n{_sha256(body)}"
        time_key = _hmac(secret_key.encode(), eop_date)
        access_key_key = _hmac(time_key, access_key)
        date_key = _hmac(access_key_key, eop_date[:8])
        signature = base64.b64encode(_hmac(date_key, signature_text)).decode()
        headers = {
            "Content-Type": "application/json",
            "Host": query.endpoint,
            "ctyun-eop-request-id": request_id,
            "Eop-date": eop_date,
            "Eop-Authorization": (
                f"{access_key} Headers={signed_header_names} Signature={signature}"
            ),
        }
        return method, f"https://{query.endpoint}{query.path}", params, body, headers
