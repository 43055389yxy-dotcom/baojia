from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.integrations.aws import AwsClients, PricingCatalog
from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor

logger = logging.getLogger(__name__)


DEFAULT_WARM_REGIONS = (
    "ap-southeast-1",  # Singapore
    "ap-northeast-1",  # Tokyo
    "ap-northeast-2",  # Seoul
    "ap-southeast-2",  # Sydney
    "eu-central-1",  # Frankfurt
    "us-east-1",  # N. Virginia
)


@dataclass(slots=True)
class CatalogWarmupStatus:
    state: str = "idle"
    completed: int = 0
    failed: int = 0
    current: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "completed": self.completed,
            "failed": self.failed,
            "current": self.current,
            "errors": self.errors[-10:],
        }


class CommonCatalogWarmer:
    """Populate the persistent read-only catalog cache without blocking startup.

    These reads never create or modify AWS resources. BCM remains the only final
    price source; the warmed data is only used for specification validation and
    billing-dimension lookup.
    """

    def __init__(
        self,
        clients: AwsClients,
        catalog: PricingCatalog,
        regions: tuple[str, ...] = DEFAULT_WARM_REGIONS,
    ) -> None:
        self._clients = clients
        self._catalog = catalog
        self._regions = regions
        self.status = CatalogWarmupStatus()

    def warm(self) -> None:
        self.status = CatalogWarmupStatus(state="running")
        for region in self._regions:
            self._warm_region(region)
        self._warm_cloudfront()
        self._warm_global_services()
        self.status.current = None
        self.status.state = "ready" if self.status.failed == 0 else "ready_with_warnings"
        logger.info(
            "AWS catalog warmup finished completed=%d failed=%d",
            self.status.completed,
            self.status.failed,
        )

    def _run(self, label: str, action: Callable[[], object]) -> None:
        self.status.current = label
        try:
            action()
            self.status.completed += 1
        except Exception as exc:
            self.status.failed += 1
            self.status.errors.append(f"{label}: {type(exc).__name__}")
            logger.warning("Catalog warmup skipped %s: %s", label, type(exc).__name__)

    def _warm_region(self, region: str) -> None:
        location_holder: dict[str, str] = {}

        def resolve_location() -> None:
            location_holder["value"] = self._catalog.location(region)

        self._run(f"{region}/region", resolve_location)
        location = location_holder.get("value")
        if not location:
            return

        executor = ReadOnlyAwsQueryExecutor(self._clients)
        for vcpu, memory_gib in (
            (2, 4),
            (2, 8),
            (4, 8),
            (4, 16),
            (8, 16),
            (8, 32),
            (16, 64),
        ):
            self._run(
                f"{region}/ec2-{vcpu}c-{memory_gib}g",
                lambda vcpu=vcpu, memory_gib=memory_gib: executor.execute(
                    service="ec2",
                    operation="describe_instance_types",
                    region=region,
                    parameters={
                        "Filters": [
                            {
                                "Name": "vcpu-info.default-vcpus",
                                "Values": [str(vcpu)],
                            },
                            {
                                "Name": "memory-info.size-in-mib",
                                "Values": [str(memory_gib * 1024)],
                            },
                        ]
                    },
                    max_items=100,
                ),
            )
        self._run(
            f"{region}/ec2-gp3",
            lambda: self._catalog.products(
                "AmazonEC2",
                {
                    "location": location,
                    "productFamily": "Storage",
                    "volumeApiName": "gp3",
                },
                max_pages=5,
            ),
        )

        for api_engine, pricing_engine, pricing_edition in (
            ("mysql", "MySQL", None),
            ("postgres", "PostgreSQL", None),
            ("sqlserver-se", "SQL Server", "Standard"),
        ):
            self._run(
                f"{region}/rds-{api_engine}-orderable",
                lambda api_engine=api_engine: executor.execute(
                    service="rds",
                    operation="describe_orderable_db_instance_options",
                    region=region,
                    parameters={"Engine": api_engine},
                    max_items=1000,
                ),
            )
            for deployment in ("Single-AZ", "Multi-AZ"):
                for vcpu, memory_gib in ((4, 16), (8, 32), (16, 64)):
                    self._run(
                        f"{region}/rds-{api_engine}-{deployment}-{vcpu}c-{memory_gib}g",
                        lambda pricing_engine=pricing_engine, pricing_edition=pricing_edition,
                        deployment=deployment, vcpu=vcpu,
                        memory_gib=memory_gib: self._catalog.products(
                            "AmazonRDS",
                            {
                                "location": location,
                                "productFamily": "Database Instance",
                                "databaseEngine": pricing_engine,
                                "deploymentOption": deployment,
                                "vcpu": str(vcpu),
                                "memory": f"{memory_gib} GiB",
                                **(
                                    {"databaseEdition": pricing_edition}
                                    if pricing_edition
                                    else {}
                                ),
                            },
                            max_pages=3,
                        ),
                    )
        self._run(
            f"{region}/rds-storage-types",
            lambda: self._catalog.attribute_values("AmazonRDS", "volumeType"),
        )

        self._run(
            f"{region}/redis-engine",
            lambda: executor.execute(
                service="elasticache",
                operation="describe_cache_engine_versions",
                region=region,
                parameters={"Engine": "redis", "MaxRecords": 20},
                paginate=False,
            ),
        )
        self._run(
            f"{region}/redis-nodes",
            lambda: self._catalog.products(
                "AmazonElastiCache",
                {
                    "location": location,
                    "productFamily": "Cache Instance",
                    "cacheEngine": "Redis",
                },
                max_pages=20,
            ),
        )
        self._run(
            f"{region}/alb",
            lambda: self._catalog.products(
                "AWSELB",
                {"location": location, "operation": "LoadBalancing:Application"},
                max_pages=2,
            ),
        )
        self._run(
            f"{region}/s3-standard",
            lambda: self._catalog.products(
                "AmazonS3",
                {
                    "location": location,
                    "productFamily": "Storage",
                    "storageClass": "General Purpose",
                    "volumeType": "Standard",
                },
                max_pages=3,
            ),
        )
        self._run(
            f"{region}/data-transfer-out",
            lambda: self._catalog.products(
                "AWSDataTransfer",
                {
                    "fromLocation": location,
                    "toLocation": "External",
                    "transferType": "AWS Outbound",
                },
                max_pages=3,
            ),
        )
        self._run(
            f"{region}/waf",
            lambda: self._catalog.products(
                "awswaf", {"location": location}, max_pages=4
            ),
        )
        self._run(
            f"{region}/sqs",
            lambda: self._catalog.products(
                "AWSQueueService",
                {"location": location, "group": "SQS-APIRequest-Tier1"},
                max_pages=4,
            ),
        )
        self._run(
            f"{region}/ses",
            lambda: self._catalog.products(
                "AmazonSES", {"location": location, "operation": "Send"}, max_pages=4
            ),
        )
        self._run(
            f"{region}/cloudwatch-logs",
            lambda: self._catalog.products(
                "AmazonCloudWatch",
                {"location": location, "group": "Ingested Logs"},
                max_pages=4,
            ),
        )
        self._run(
            f"{region}/cloudwatch-metrics",
            lambda: self._catalog.products(
                "AmazonCloudWatch",
                {"location": location, "group": "Metric"},
                max_pages=2,
            ),
        )
        self._run(
            f"{region}/cloudwatch-log-storage",
            lambda: self._catalog.products(
                "AmazonCloudWatch",
                {
                    "location": location,
                    "group": "Amazon CloudWatch Standard Storage pricing current",
                },
                max_pages=2,
            ),
        )
        self._run(
            f"{region}/cloudwatch-alarms",
            lambda: self._catalog.products(
                "AmazonCloudWatch",
                {"location": location, "group": "Alarm"},
                max_pages=2,
            ),
        )

    def _warm_cloudfront(self) -> None:
        for geography, prefix in (
            ("Asia Pacific", "AP"),
            ("Japan", "JP"),
            ("Australia", "AU"),
            ("Europe", "EU"),
            ("United States", "US"),
        ):
            self._run(
                f"cloudfront/{geography}/transfer",
                lambda geography=geography: self._catalog.products(
                    "AmazonCloudFront",
                    {
                        "transferType": "CloudFront Outbound",
                        "fromLocation": geography,
                        "toLocation": "External",
                    },
                    max_pages=3,
                ),
            )
            self._run(
                f"cloudfront/{geography}/requests",
                lambda prefix=prefix: self._catalog.products(
                    "AmazonCloudFront",
                    {
                        "productFamily": "Request",
                        "requestType": "CloudFront-Request-HTTPS-Proxy",
                        "usagetype": f"{prefix}-Requests-HTTPS-Proxy",
                    },
                    max_pages=3,
                ),
            )

    def _warm_global_services(self) -> None:
        self._run(
            "route53/hosted-zone",
            lambda: self._catalog.products(
                "AmazonRoute53", {"usagetype": "HostedZone"}, max_pages=1
            ),
        )
        self._run(
            "waf/global",
            lambda: self._catalog.products(
                "awswaf", {"location": "Any"}, max_pages=4
            ),
        )
        self._run(
            "global-accelerator/fixed",
            lambda: self._catalog.products(
                "AWSGlobalAccelerator",
                {"usagetype": "Global-Accelerator-fixed-fee"},
                max_pages=1,
            ),
        )
        self._run(
            "global-accelerator/transfer",
            lambda: self._catalog.products(
                "AWSGlobalAccelerator", {}, max_pages=20
            ),
        )
