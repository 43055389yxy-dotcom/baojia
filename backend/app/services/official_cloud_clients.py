from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import ssl
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx


class OfficialCloudClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        category: str = "official_api_error",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.details = details or {}


_BAIDU_CHAIN_ONLY_TLS_HOSTS = frozenset({"bcc.sin.baidubce.com"})
_SENSITIVE_TEXT = re.compile(
    r"(?i)(accesskeyid|access[_-]?key|secret|signature|authorization)"
    r"(?:\s*[:=]\s*|%3[dD])[^&\s,}\]]+"
)


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


def _flatten_rpc_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Flatten RPC repeated parameters before both signing and transport.

    Alibaba RPC APIs sign the exact flattened key/value pairs that are sent on
    the wire.  Empty collections and nulls are omitted from both views so a
    value cannot be signed and then silently dropped by the HTTP client.
    """

    flattened: dict[str, Any] = {}

    def visit(prefix: str, value: Any) -> None:
        if value is None or value == [] or value == {}:
            return
        if isinstance(value, list):
            for index, item in enumerate(value, start=1):
                visit(f"{prefix}.{index}", item)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                visit(f"{prefix}.{key}", item)
            return
        flattened[prefix] = value

    for key, value in parameters.items():
        visit(str(key), value)
    return flattened


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
                category="credentials",
            )
        method, url, params, content, headers = getattr(
            self, f"_prepare_{query.provider}"
        )(query, access_key, secret_key)
        request_options: dict[str, Any] = {
            "params": params or None,
            "content": content or None,
            "headers": headers,
            "timeout": 30.0,
            "follow_redirects": False,
        }
        if (
            query.provider == "baidu"
            and query.endpoint in _BAIDU_CHAIN_ONLY_TLS_HOSTS
        ):
            tls_context = ssl.create_default_context()
            tls_context.check_hostname = False
            tls_context.verify_mode = ssl.CERT_REQUIRED
            request_options["verify"] = tls_context
        try:
            response = self._request(method, url, **request_options)
        except Exception as exc:
            raise OfficialCloudClientError(
                "The official cloud API could not be reached.",
                code=f"{query.provider}_transport_error",
                category="transport",
                retryable=True,
                details={"exception_type": type(exc).__name__},
            ) from exc

        try:
            payload = response.json()
        except Exception as exc:
            payload = None
            response_status = int(getattr(response, "status_code", 200) or 200)
            response_url = str(getattr(response, "url", "") or "").casefold()
            redirect_statuses = [
                int(getattr(item, "status_code", 0) or 0)
                for item in (getattr(response, "history", None) or [])
            ]
            maintenance_redirect = bool(redirect_statuses) and (
                any(status in {301, 302, 303, 307, 308} for status in redirect_statuses)
                or any(token in response_url for token in ("maintenance", "/splash/"))
            )
            if response_status < 400:
                raise OfficialCloudClientError(
                    (
                        "Official cloud pricing service is temporarily unavailable."
                        if maintenance_redirect
                        else "Official cloud API returned an invalid JSON response."
                    ),
                    code=(
                        f"{query.provider}_official_catalog_unavailable"
                        if maintenance_redirect
                        else "official_catalog_invalid_response"
                    ),
                    category=(
                        "provider_unavailable"
                        if maintenance_redirect
                        else "response_schema"
                    ),
                    retryable=True,
                    details={
                        "http_status": response_status,
                        **(
                            {"redirect_statuses": redirect_statuses}
                            if redirect_statuses
                            else {}
                        ),
                    },
                ) from exc

        status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code >= 400:
            raise _official_api_error(query.provider, payload, status_code)
        if not isinstance(payload, dict):
            raise OfficialCloudClientError(
                "Official cloud API returned a non-object JSON payload",
                code="official_catalog_invalid_response",
                category="response_schema",
                retryable=True,
                details={"http_status": status_code},
            )
        if _has_official_error_envelope(payload):
            raise _official_api_error(query.provider, payload, status_code)
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
        parameters = _flatten_rpc_parameters({
            "AccessKeyId": access_key,
            "Action": query.action,
            "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": str(query.version),
            **query.query_parameters,
            **query.body,
        })
        region_parameter = getattr(query, "region_parameter", None)
        has_region = any(
            key.casefold() in {"region", "regionid"} for key in parameters
        )
        if (
            query.region
            and region_parameter
            and region_parameter != "none"
            and not has_region
        ):
            parameters[region_parameter] = query.region
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


def _first_text(payload: Any, paths: tuple[tuple[str, ...], ...]) -> str:
    for path in paths:
        current = payload
        for part in path:
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current is not None and str(current).strip():
            return str(current).strip()
    return ""


def _has_official_error_envelope(payload: dict[str, Any]) -> bool:
    """Detect provider business errors returned inside an HTTP 2xx response."""

    candidates = (
        payload.get("Error"),
        payload.get("error"),
        (payload.get("Response") or {}).get("Error")
        if isinstance(payload.get("Response"), dict)
        else None,
        (payload.get("ResponseMetadata") or {}).get("Error")
        if isinstance(payload.get("ResponseMetadata"), dict)
        else None,
    )
    if any(
        isinstance(candidate, dict)
        and any(candidate.get(key) for key in ("Code", "code", "Message", "message"))
        for candidate in candidates
    ):
        return True
    success = payload.get("Success", payload.get("success"))
    return success is False and bool(
        _first_text(
            payload,
            (
                ("Code",),
                ("code",),
                ("Message",),
                ("message",),
                ("error_code",),
                ("error_msg",),
            ),
        )
    )


def _redact_error_text(value: str) -> str:
    redacted = _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return redacted[:800]


def _official_api_error(
    provider: str, payload: Any, status_code: int
) -> OfficialCloudClientError:
    code = _first_text(
        payload,
        (
            ("Code",),
            ("code",),
            ("error_code",),
            ("Error", "Code"),
            ("error", "code"),
            ("Response", "Error", "Code"),
            ("ResponseMetadata", "Error", "Code"),
            ("statusCode",),
        ),
    ) or f"http_{status_code}"
    message = _first_text(
        payload,
        (
            ("Message",),
            ("message",),
            ("error_msg",),
            ("Error", "Message"),
            ("error", "message"),
            ("Response", "Error", "Message"),
            ("ResponseMetadata", "Error", "Message"),
        ),
    ) or f"Official cloud API returned HTTP {status_code}."
    request_id = _first_text(
        payload,
        (
            ("RequestId",),
            ("requestId",),
            ("request_id",),
            ("Response", "RequestId"),
            ("ResponseMetadata", "RequestId"),
        ),
    )
    folded = f"{code} {message}".casefold()
    if any(
        token in folded
        for token in (
            "signaturedoesnotmatch",
            "invalidsignature",
            "signature mismatch",
            "signature not match",
        )
    ):
        category, retryable = "request_signing", True
    elif status_code == 429 or any(
        token in folded for token in ("throttl", "rate limit", "too many")
    ):
        category, retryable = "rate_limit", True
    elif status_code in {401, 403} or any(
        token in folded
        for token in ("unauthor", "forbidden", "permission")
    ):
        category, retryable = "authorization", False
    elif any(
        token in folded
        for token in (
            "internalerror",
            "internal error",
            "serviceunavailable",
            "service unavailable",
            "temporarily unavailable",
            "maintenance",
        )
    ):
        category, retryable = "provider_unavailable", True
    elif status_code == 404 or any(
        token in folded
        for token in ("notfound", "not found", "productnotfind", "unknown action")
    ):
        category, retryable = "route_not_found", True
    elif status_code == 400 or any(
        token in folded
        for token in (
            "missingparameter",
            "invalidparameter",
            "invalid parameter",
            "invalidmodulecode",
            "not spu object",
        )
    ):
        category, retryable = "invalid_request", True
    elif status_code >= 500:
        category, retryable = "provider_unavailable", True
    else:
        category, retryable = "official_api_error", False
    details: dict[str, Any] = {
        "http_status": status_code,
        "provider_code": code,
    }
    if request_id:
        details["request_id"] = request_id
    snake_code = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", code)
    normalized_code = re.sub(r"[^a-z0-9]+", "_", snake_code.casefold()).strip("_")
    return OfficialCloudClientError(
        _redact_error_text(message),
        code=f"{provider}_{normalized_code or f'http_{status_code}'}",
        category=category,
        retryable=retryable,
        details=details,
    )
