from __future__ import annotations

from botocore.loaders import create_loader

# Customer-facing Chinese names for every commercial AWS region bundled with
# the application's botocore directory.  The official directory remains the
# allowlist; this table only localizes regions that AWS says are available.
AWS_COMMERCIAL_REGION_NAMES_ZH: dict[str, str] = {
    "af-south-1": "非洲（开普敦）",
    "ap-east-1": "亚太地区（香港）",
    "ap-east-2": "亚太地区（台北）",
    "ap-northeast-1": "亚太地区（东京）",
    "ap-northeast-2": "亚太地区（首尔）",
    "ap-northeast-3": "亚太地区（大阪）",
    "ap-south-1": "亚太地区（孟买）",
    "ap-south-2": "亚太地区（海得拉巴）",
    "ap-southeast-1": "亚太地区（新加坡）",
    "ap-southeast-2": "亚太地区（悉尼）",
    "ap-southeast-3": "亚太地区（雅加达）",
    "ap-southeast-4": "亚太地区（墨尔本）",
    "ap-southeast-5": "亚太地区（马来西亚）",
    "ap-southeast-6": "亚太地区（新西兰）",
    "ap-southeast-7": "亚太地区（泰国）",
    "ca-central-1": "加拿大（中部）",
    "ca-west-1": "加拿大西部（卡尔加里）",
    "eu-central-1": "欧洲（法兰克福）",
    "eu-central-2": "欧洲（苏黎世）",
    "eu-north-1": "欧洲（斯德哥尔摩）",
    "eu-south-1": "欧洲（米兰）",
    "eu-south-2": "欧洲（西班牙）",
    "eu-west-1": "欧洲（爱尔兰）",
    "eu-west-2": "欧洲（伦敦）",
    "eu-west-3": "欧洲（巴黎）",
    "il-central-1": "以色列（特拉维夫）",
    "me-central-1": "中东（阿联酋）",
    "me-south-1": "中东（巴林）",
    "mx-central-1": "墨西哥（中部）",
    "sa-east-1": "南美洲（圣保罗）",
    "us-east-1": "美国东部（弗吉尼亚北部）",
    "us-east-2": "美国东部（俄亥俄）",
    "us-west-1": "美国西部（加利福尼亚北部）",
    "us-west-2": "美国西部（俄勒冈）",
}


def official_aws_region_labels() -> dict[str, str]:
    """Return the official commercial-region allowlist and English names."""

    endpoints = create_loader().load_data("endpoints")
    labels: dict[str, str] = {}
    for partition in endpoints.get("partitions", []):
        if partition.get("partition") != "aws":
            continue
        for code, metadata in partition.get("regions", {}).items():
            if not isinstance(metadata, dict):
                continue
            normalized_code = str(code).strip()
            description = str(metadata.get("description") or normalized_code).strip()
            labels[normalized_code] = description or normalized_code
    return labels


def bilingual_aws_region_label(
    code: str,
    *,
    official_label: str | None = None,
) -> str:
    """Return one consistent Chinese/English label for a commercial region."""

    normalized_code = code.strip().casefold()
    english = official_label or official_aws_region_labels().get(normalized_code)
    chinese = AWS_COMMERCIAL_REGION_NAMES_ZH.get(normalized_code)
    if chinese and english:
        return f"{chinese} / {english}"
    if chinese:
        return chinese
    if english:
        # New AWS regions remain understandable instead of silently reverting
        # to an English-only card until the next localization update.
        return f"AWS 新地区（{normalized_code}） / {english}"
    return normalized_code or code


def commercial_aws_region_options() -> list[tuple[str, str]]:
    """Return only officially supported commercial regions with UI labels."""

    official = official_aws_region_labels()
    return [
        (code, bilingual_aws_region_label(code, official_label=english))
        for code, english in sorted(official.items())
    ]
