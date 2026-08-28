from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.core.data_paths import AWS_DATA_ROOT
from app.integrations.aws_public_catalog import PublicAwsPriceCatalog
from app.integrations.service_templates import DYNAMIC_SEMANTIC_TEMPLATE_FIELDS

PRODUCT_REGISTRY_SCHEMA_VERSION = 3


def _service_key(service_code: str) -> str:
    # Split a CamelCase service code without splitting every letter in an
    # acronym.  The old expression turned AmazonEC2 into ``e_c2`` and
    # AmazonMQ into ``m_q``; that made the complete official catalog unusable
    # as an identity source for customer-facing names.
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", service_code)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value).casefold()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    for prefix in ("amazon_", "aws_"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value or service_code.casefold()


def _aliases(service_code: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", service_code)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced).strip()
    stripped = re.sub(r"^(Amazon|AWS)\s*", "", spaced, flags=re.IGNORECASE).strip()
    values = {
        service_code,
        spaced,
        stripped,
        _service_key(service_code),
        _service_key(service_code).replace("_", " "),
    }
    return sorted(value for value in values if value)


def _canonical(value: str) -> str:
    # Learned aliases come from real customer wording and are not limited to
    # ASCII.  The previous normalizer deleted every Chinese character, so an
    # alias such as ``共享文件存储`` could be successfully persisted yet could
    # never be read back.  Keep every Unicode letter/number after compatibility
    # normalization; English offer codes retain their existing canonical form.
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


_IDENTITY_STOP_WORDS = frozenset(
    {
        "amazon",
        "aws",
        "service",
        "services",
        "managed",
        "for",
        "the",
        "data",
    }
)


def _identity_words(value: str) -> set[str]:
    """Return useful product-name words without provider boilerplate.

    This is used only to retrieve a short list of official candidates.  It is
    deliberately not an identity decision: a candidate still has to be chosen
    by the isolated service classifier and validated against the registry.
    """

    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", split)
    return {
        word
        for word in re.findall(r"[a-z0-9]+", split.casefold())
        if len(word) >= 3 and word not in _IDENTITY_STOP_WORDS
    }


def _identity_targets(value: str) -> set[str]:
    """Return exact, identity-safe variants of one customer product label.

    AWS sometimes keeps a marketing version in the public product name while
    the Price List service code omits it (``Amazon AppStream 2.0`` versus
    ``AmazonAppStream``).  Removing only a terminal numeric version preserves
    exact product matching without introducing fuzzy or substring guesses.
    """

    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if not compact:
        return set()
    variants = {_canonical(compact)}
    without_terminal_version = re.sub(
        r"(?:\s+|[-_])v?\d+(?:\.\d+)*(?:\s*)$",
        "",
        compact,
        flags=re.IGNORECASE,
    ).strip()
    if without_terminal_version and without_terminal_version != compact:
        variants.add(_canonical(without_terminal_version))
    return {variant for variant in variants if variant}


class AwsProductRegistry:
    """Persistent identity registry for every official AWS price-list offer.

    One row represents one AWS product boundary. Later field templates and
    billing dimensions are attached to that same boundary, so new products do
    not share a mutable, universal component template.
    """

    def __init__(
        self,
        public_catalog: PublicAwsPriceCatalog | None = None,
        database_path: Path | None = None,
    ) -> None:
        self.public_catalog = public_catalog or PublicAwsPriceCatalog()
        self._database_path = database_path or AWS_DATA_ROOT / "aws_product_registry.sqlite3"
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS aws_product_registry (
                    service_code TEXT PRIMARY KEY,
                    service_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    offer_json TEXT NOT NULL,
                    field_template_json TEXT NOT NULL DEFAULT '{}',
                    policy_json TEXT NOT NULL DEFAULT '{}',
                    identity_status TEXT NOT NULL,
                    profile_status TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_aws_product_service_key "
                "ON aws_product_registry(service_key)"
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(aws_product_registry)").fetchall()
            }
            if "field_template_json" not in columns:
                connection.execute(
                    "ALTER TABLE aws_product_registry ADD COLUMN "
                    "field_template_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "policy_json" not in columns:
                connection.execute(
                    "ALTER TABLE aws_product_registry ADD COLUMN "
                    "policy_json TEXT NOT NULL DEFAULT '{}'"
                )
            # Repair identities created by older releases in place.  This is
            # metadata-only and does not touch offers, prices or customer
            # sessions.  It lets the local catalog immediately recognize all
            # existing official services without waiting for a network sync.
            rows = connection.execute(
                "SELECT service_code, aliases_json, field_template_json "
                "FROM aws_product_registry"
            ).fetchall()
            for row in rows:
                service_code = str(row["service_code"])
                try:
                    aliases = {
                        str(value)
                        for value in json.loads(str(row["aliases_json"]))
                        if str(value).strip()
                    }
                except (TypeError, json.JSONDecodeError):
                    aliases = set()
                aliases.update(_aliases(service_code))
                try:
                    field_template = json.loads(str(row["field_template_json"]))
                except (TypeError, json.JSONDecodeError):
                    field_template = {}
                if not isinstance(field_template, dict):
                    field_template = {}
                field_template["service_code"] = service_code
                field_template["fields"] = sorted(
                    set(DYNAMIC_SEMANTIC_TEMPLATE_FIELDS)
                    | {
                        str(field)
                        for field in field_template.get("fields", [])
                        if isinstance(field, str) and field
                    }
                )
                field_template.setdefault("source", "official_dimensions_on_first_use")
                field_template["isolation"] = "strict_component_boundary"
                connection.execute(
                    "UPDATE aws_product_registry SET service_key = ?, aliases_json = ?, "
                    "field_template_json = ?, schema_version = ? "
                    "WHERE service_code = ?",
                    (
                        _service_key(service_code),
                        json.dumps(sorted(aliases), ensure_ascii=False),
                        json.dumps(field_template, ensure_ascii=False, separators=(",", ":")),
                        PRODUCT_REGISTRY_SCHEMA_VERSION,
                        service_code,
                    ),
                )

    def sync(self, *, refresh: bool = False) -> dict[str, Any]:
        offers = self.public_catalog.offers(refresh=refresh)
        now = time.time()
        current_codes = {offer.service_code for offer in offers}
        inserted = 0
        updated = 0
        with self._lock, self._connect() as connection:
            existing = {
                str(row["service_code"]): {
                    "profile_status": str(row["profile_status"]),
                    "aliases": json.loads(str(row["aliases_json"])),
                    "offer": json.loads(str(row["offer_json"])),
                    "field_template": json.loads(str(row["field_template_json"])),
                    "policy": json.loads(str(row["policy_json"])),
                }
                for row in connection.execute(
                    "SELECT service_code, profile_status, aliases_json, offer_json, "
                    "field_template_json, policy_json "
                    "FROM aws_product_registry"
                ).fetchall()
            }
            for offer in offers:
                payload = asdict(offer)
                previous = existing.get(offer.service_code)
                if previous and isinstance(previous.get("offer"), dict):
                    for key in (
                        "available_regions",
                        "region_count",
                        "region_publication_date",
                    ):
                        if previous["offer"].get(key) is not None:
                            payload[key] = previous["offer"][key]
                profile_status = str(previous["profile_status"]) if previous else "identity_ready"
                field_template = (
                    previous.get("field_template")
                    if previous and previous.get("field_template")
                    else {
                        "service_code": offer.service_code,
                        "fields": list(DYNAMIC_SEMANTIC_TEMPLATE_FIELDS),
                        "source": "official_dimensions_on_first_use",
                        "isolation": "strict_component_boundary",
                    }
                )
                field_template["fields"] = sorted(
                    set(DYNAMIC_SEMANTIC_TEMPLATE_FIELDS)
                    | {
                        str(field)
                        for field in field_template.get("fields", [])
                        if isinstance(field, str) and field
                    }
                )
                policy = self._base_policy(offer.service_code)
                if previous and isinstance(previous.get("policy"), dict):
                    policy.update(previous["policy"])
                aliases = set(_aliases(offer.service_code))
                if previous and isinstance(previous.get("aliases"), list):
                    aliases.update(
                        str(alias).strip()
                        for alias in previous["aliases"]
                        if str(alias).strip()
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO aws_product_registry (
                        service_code, service_key, display_name, aliases_json,
                        offer_json, field_template_json, policy_json,
                        identity_status, profile_status,
                        schema_version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        offer.service_code,
                        _service_key(offer.service_code),
                        offer.service_code,
                        json.dumps(sorted(aliases), ensure_ascii=False),
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(field_template, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(policy, ensure_ascii=False, separators=(",", ":")),
                        "official",
                        profile_status,
                        PRODUCT_REGISTRY_SCHEMA_VERSION,
                        now,
                    ),
                )
                if offer.service_code in existing:
                    updated += 1
                else:
                    inserted += 1
            if current_codes:
                placeholders = ",".join("?" for _ in current_codes)
                connection.execute(
                    f"UPDATE aws_product_registry SET identity_status = 'retired' "
                    f"WHERE service_code NOT IN ({placeholders})",
                    tuple(sorted(current_codes)),
                )
        return {
            "official_offer_count": len(offers),
            "inserted": inserted,
            "updated": updated,
            "schema_version": PRODUCT_REGISTRY_SCHEMA_VERSION,
            "publication_date": (offers[0].publication_date if offers else None),
        }

    def add_alias(self, service_code: str, alias: str) -> None:
        """Remember one AI-confirmed marketing name for an official product.

        The alias never creates or selects a product. It is accepted only for
        an exact service code already present in the AWS directory, after the
        closed-choice classifier has selected that code.
        """

        clean_alias = re.sub(r"\s+", " ", str(alias or "")).strip()
        if not clean_alias:
            return
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT aliases_json, identity_status FROM aws_product_registry "
                "WHERE service_code = ?",
                (service_code,),
            ).fetchone()
            if row is None or str(row["identity_status"]) != "official":
                return
            aliases = {
                str(value).strip()
                for value in json.loads(str(row["aliases_json"]))
                if str(value).strip()
            }
            aliases.add(clean_alias)
            connection.execute(
                "UPDATE aws_product_registry SET aliases_json = ?, updated_at = ? "
                "WHERE service_code = ?",
                (
                    json.dumps(sorted(aliases), ensure_ascii=False),
                    time.time(),
                    service_code,
                ),
            )

    @staticmethod
    def _base_policy(service_code: str) -> dict[str, Any]:
        """Return the invariant contract shared by every AWS product row.

        This is deliberately structural rather than a universal product
        template: products keep separate fields and prices, while only the
        customer's quote-wide region is allowed to cross a component boundary.
        """

        return {
            "service_code": service_code,
            "identity_source": "aws_bulk_offer_index",
            "specification_source": "aws_price_list",
            "final_price_source": "aws_bcm_or_official_price_dimension",
            "customer_explicit_value_priority": "highest",
            "cross_component_inheritance": "region_only",
            "missing_value": "service_specific_default_or_confirmation",
            "price_failure": "retain_component_and_retry_official_sources",
            "zero_price": "allowed_only_for_explicit_zero_base_resources",
            "edit_recalculation": "affected_component_only_from_intake",
        }

    def update_profile(
        self,
        service_code: str,
        profile: dict[str, Any],
        *,
        status: str,
    ) -> None:
        """Attach one verified official field contract to its own product row."""

        fields = sorted(
            set(DYNAMIC_SEMANTIC_TEMPLATE_FIELDS)
            | {
                str(field)
                for field in profile.get("fields", [])
                if isinstance(field, str) and field
            }
        )
        bindings = [
            dict(binding)
            for binding in profile.get("field_bindings", [])
            if isinstance(binding, dict)
        ]
        template = {
            "service_code": service_code,
            "fields": fields,
            "field_bindings": bindings,
            "attribute_names": list(profile.get("attribute_names") or []),
            "official_dimension_count": len(profile.get("dimensions") or []),
            "source": "aws_price_list_dimensions",
            "region": profile.get("region"),
            "isolation": "strict_component_boundary",
            "profile_schema_version": profile.get("profile_schema_version"),
        }
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT policy_json FROM aws_product_registry WHERE service_code = ?",
                (service_code,),
            ).fetchone()
            if row is None:
                return
            policy = json.loads(str(row["policy_json"]))
            policy["field_discovery"] = "verified"
            policy["billing_dimension_count"] = len(profile.get("dimensions") or [])
            connection.execute(
                "UPDATE aws_product_registry SET field_template_json = ?, "
                "policy_json = ?, profile_status = ?, schema_version = ?, "
                "updated_at = ? WHERE service_code = ?",
                (
                    json.dumps(template, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(policy, ensure_ascii=False, separators=(",", ":")),
                    status,
                    PRODUCT_REGISTRY_SCHEMA_VERSION,
                    time.time(),
                    service_code,
                ),
            )

    def sync_region_availability(
        self,
        *,
        workers: int = 12,
        only_missing: bool = True,
    ) -> dict[str, int]:
        products = self.list_products()
        pending = [
            product
            for product in products
            if product["identity_status"] == "official"
            and (
                not only_missing
                or not isinstance(product.get("offer"), dict)
                or not product["offer"].get("available_regions")
            )
        ]
        result = {"checked": len(pending), "updated": 0, "failed": 0}

        def discover(service_code: str) -> tuple[str, list[str], str | None]:
            regions, publication_date = self.public_catalog.available_regions(service_code)
            return service_code, regions, publication_date

        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(discover, str(product["service_code"])): product
                for product in pending
            }
            for future in as_completed(futures):
                try:
                    service_code, regions, publication_date = future.result()
                except Exception:
                    result["failed"] += 1
                    continue
                with self._lock, self._connect() as connection:
                    row = connection.execute(
                        "SELECT offer_json FROM aws_product_registry WHERE service_code = ?",
                        (service_code,),
                    ).fetchone()
                    if row is None:
                        result["failed"] += 1
                        continue
                    offer = json.loads(str(row["offer_json"]))
                    offer["available_regions"] = regions
                    offer["region_count"] = len(regions)
                    offer["region_publication_date"] = publication_date
                    connection.execute(
                        "UPDATE aws_product_registry SET offer_json = ?, updated_at = ? "
                        "WHERE service_code = ?",
                        (
                            json.dumps(offer, ensure_ascii=False, separators=(",", ":")),
                            time.time(),
                            service_code,
                        ),
                    )
                result["updated"] += 1
        return result

    def mark_profile_status(self, service_code: str, status: str) -> None:
        allowed = {
            "identity_ready",
            "profile_ready",
            "pricing_ready",
            "needs_review",
            "zero_base_fee",
            "composite",
        }
        if status not in allowed:
            raise ValueError(f"unsupported AWS product profile status: {status}")
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE aws_product_registry SET profile_status = ?, updated_at = ? "
                "WHERE service_code = ?",
                (status, time.time(), service_code),
            )

    def list_products(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM aws_product_registry ORDER BY service_code"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "service_code": str(row["service_code"]),
                    "service_key": str(row["service_key"]),
                    "display_name": str(row["display_name"]),
                    "aliases": json.loads(str(row["aliases_json"])),
                    "offer": json.loads(str(row["offer_json"])),
                    "field_template": json.loads(str(row["field_template_json"])),
                    "policy": json.loads(str(row["policy_json"])),
                    "identity_status": str(row["identity_status"]),
                    "profile_status": str(row["profile_status"]),
                    "schema_version": int(row["schema_version"]),
                    "updated_at": float(row["updated_at"]),
                }
            )
        return result

    def resolve_product(self, *labels: str) -> dict[str, Any] | None:
        """Resolve one exact official product identity from customer labels.

        This deliberately performs identity matching only.  It never guesses
        a product by price, capacity or a fuzzy nearest name, so a third-party
        workload cannot be silently converted into an unrelated AWS service.

        Provider-owned names and aliases learned from earlier AI classifications
        are deliberately distinguished in the result.  A learned alias remains
        useful for candidate retrieval, but callers must validate it again; one
        historic bad classification must never become an authoritative local
        catalog fact for every future quote.
        """

        targets = {target for label in labels for target in _identity_targets(label)}
        if not targets:
            return None
        provider_matches: list[dict[str, Any]] = []
        learned_matches: list[dict[str, Any]] = []
        for product in self.list_products():
            if str(product.get("identity_status") or "") != "official":
                continue
            provider_aliases = set(_aliases(str(product["service_code"])))
            provider_identities = {
                _canonical(str(product["service_code"])),
                _canonical(str(product["service_key"])),
                _canonical(str(product["display_name"])),
                *(_canonical(str(alias)) for alias in provider_aliases),
            }
            if provider_identities & targets:
                provider_matches.append(product)
                continue
            learned_identities = {
                _canonical(str(alias))
                for alias in product["aliases"]
                if str(alias).strip() and str(alias) not in provider_aliases
            }
            if learned_identities & targets:
                learned_matches.append(product)
        if len(provider_matches) == 1:
            return {**provider_matches[0], "identity_match_source": "provider"}
        if provider_matches:
            return None
        if len(learned_matches) == 1:
            return {**learned_matches[0], "identity_match_source": "learned_alias"}
        return None

    def candidate_products(
        self,
        *labels: str,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Retrieve likely official identities without silently choosing one.

        AWS marketing names can change while the Price List offer code keeps
        its older name (for example Data Firehose/Kinesis Firehose). Exact
        matching remains the automatic path. This broader lookup merely gives
        the AI classifier a small provider-owned candidate set; its selected
        service code is validated again before it can change a component.
        """

        query_values = [str(label).strip() for label in labels if str(label).strip()]
        if not query_values:
            return []
        query_targets = {
            target for label in query_values for target in _identity_targets(label)
        }
        query_word_sets = [_identity_words(label) for label in query_values]
        query_words = set().union(*query_word_sets) if query_word_sets else set()
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for product in self.list_products():
            if str(product.get("identity_status") or "") != "official":
                continue
            identities = [
                str(product.get("service_code") or ""),
                str(product.get("service_key") or ""),
                str(product.get("display_name") or ""),
                *(str(alias) for alias in product.get("aliases", [])),
            ]
            identity_targets = {_canonical(value) for value in identities if value}
            if query_targets & identity_targets:
                score = 100.0
            else:
                identity_word_sets = [_identity_words(value) for value in identities]
                identity_words = (
                    set().union(*identity_word_sets) if identity_word_sets else set()
                )
                sequence = max(
                    (
                        SequenceMatcher(
                            None,
                            _canonical(label),
                            _canonical(identity),
                        ).ratio()
                        for label in query_values
                        for identity in identities
                        if identity
                    ),
                    default=0.0,
                )
                overlap = query_words & identity_words
                if not overlap:
                    # Marketing names can contain a qualifier absent from the
                    # long-lived Price List code (AWS IoT Core -> AWSIoT).
                    # Keep a sufficiently similar official identity in the AI
                    # candidate set; this is retrieval only, never an automatic
                    # product decision, and the selected code is validated
                    # against this registry before it can be applied.
                    if sequence < 0.62:
                        continue
                    score = sequence
                else:
                    coverage = len(overlap) / max(len(query_words), 1)
                    specificity = len(overlap) / max(len(identity_words), 1)
                    score = (coverage * 5.0) + (specificity * 3.0) + sequence
            ranked.append((score, str(product.get("service_code") or ""), product))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [product for _score, _code, product in ranked[: max(1, limit)]]

    def official_products(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return the current provider directory for AI-only identity fallback."""

        return [
            product
            for product in self.list_products()
            if str(product.get("identity_status") or "") == "official"
        ][: max(1, limit)]

    def resolve_service_code(self, *labels: str) -> str | None:
        product = self.resolve_product(*labels)
        return str(product["service_code"]) if product is not None else None

    def coverage(self) -> dict[str, Any]:
        products = self.list_products()
        status_counts: dict[str, int] = {}
        for product in products:
            status = str(product["profile_status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "total": len(products),
            "official": sum(1 for item in products if item["identity_status"] == "official"),
            "retired": sum(1 for item in products if item["identity_status"] == "retired"),
            "profile_status": status_counts,
        }
