# Customer-facing components do not always share the name of their owning AWS
# Price List offer.  This table is the reviewed contract between every curated
# extraction template and AWS's official Bulk Price List index.  Runtime code
# still verifies that each value exists in the current official registry before
# using it, so a stale entry fails the system audit instead of becoming a fake
# "unsupported region" customer problem.
CURATED_SERVICE_OFFER_CODES: dict[str, str] = {
    "amp": "AmazonPrometheus",
    "apigateway": "AmazonApiGateway",
    "appconfig": "AWSSystemsManager",
    "athena": "AmazonAthena",
    "backup": "AWSBackup",
    "bedrock": "AmazonBedrock",
    "cloud_map": "AWSCloudMap",
    "cloudfront": "AmazonCloudFront",
    "cloudwatch": "AmazonCloudWatch",
    "cognito": "AmazonCognito",
    "data_transfer": "AWSDataTransfer",
    "dms": "AWSDatabaseMigrationSvc",
    "documentdb": "AmazonDocDB",
    "dynamodb": "AmazonDynamoDB",
    "ebs": "AmazonEC2",
    "ec2": "AmazonEC2",
    "ecr": "AmazonECR",
    "ecs": "AmazonECS",
    "efs": "AmazonEFS",
    "eks": "AmazonEKS",
    "elasticache": "AmazonElastiCache",
    "elb": "AWSELB",
    "emr": "ElasticMapReduce",
    "eventbridge": "AWSEvents",
    "fargate": "AmazonECS",
    "fsx": "AmazonFSx",
    "global_accelerator": "AWSGlobalAccelerator",
    "glue": "AWSGlue",
    "kinesis": "AmazonKinesis",
    "kms": "awskms",
    "lambda": "AWSLambda",
    "memorydb": "AmazonMemoryDB",
    "mq": "AmazonMQ",
    "msk": "AmazonMSK",
    "nat_gateway": "AmazonEC2",
    "opensearch": "AmazonES",
    "pinpoint": "AmazonPinpoint",
    "quicksight": "AmazonQuickSight",
    "rds": "AmazonRDS",
    "redshift": "AmazonRedshift",
    "route53": "AmazonRoute53",
    "s3": "AmazonS3",
    "sagemaker": "AmazonSageMaker",
    "scheduler": "AWSEvents",
    "secrets_manager": "AWSSecretsManager",
    "ses": "AmazonSES",
    "sns": "AmazonSNS",
    "sqs": "AWSQueueService",
    "step_functions": "AmazonStates",
    "vpc": "AmazonVPC",
    "waf": "awswaf",
    "xray": "AWSXRay",
}


# Botocore's signed endpoint catalogue is the local official source for region
# availability.  Marketing names and endpoint service ids are often different,
# so keep that identity mapping separate from Price List offer codes.  New
# products can be added here without adding quote-flow branches.
CURATED_ENDPOINT_SERVICE_IDS: dict[str, tuple[str, ...]] = {
    "timestream": ("timestream-query", "timestream-write"),
    "timestreamforliveanalytics": ("timestream-query", "timestream-write"),
    "keyspaces": ("keyspaces",),
    "keyspacesforapachecassandra": ("keyspaces",),
    "appstream": ("appstream",),
    "appstream20": ("appstream",),
    "workspaces": ("workspaces",),
    "managedgrafana": ("grafana",),
    "grafana": ("grafana",),
}


# Retirement is not a retryable catalogue outage.  AWS Price List does not
# publish lifecycle/replacement decisions, so this small reviewed registry
# turns a retired product into a finite customer choice instead of an internal
# error or an invented EC2 substitute.
RETIRED_AWS_SERVICE_PROFILES: dict[str, dict[str, object]] = {
    "qldb": {
        "display_name": "Amazon QLDB",
        "retired_on": "2025-07-31",
        "replacements": (
            {
                "label": "改用 Amazon Aurora PostgreSQL",
                "decision": "replace_service:rds:aurora_postgresql",
            },
            {
                "label": "暂不纳入本次报价",
                "decision": "exclude_component",
            },
        ),
    },
    "quantumledgerdatabase": {
        "display_name": "Amazon QLDB",
        "retired_on": "2025-07-31",
        "replacements": (
            {
                "label": "改用 Amazon Aurora PostgreSQL",
                "decision": "replace_service:rds:aurora_postgresql",
            },
            {
                "label": "暂不纳入本次报价",
                "decision": "exclude_component",
            },
        ),
    },
}
