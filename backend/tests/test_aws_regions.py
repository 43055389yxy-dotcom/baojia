from __future__ import annotations

import re

from app.integrations.aws_regions import (
    AWS_COMMERCIAL_REGION_NAMES_ZH,
    bilingual_aws_region_label,
    commercial_aws_region_options,
    official_aws_region_labels,
)
from app.services.quote_service import QuoteService


def test_every_official_commercial_region_has_a_chinese_name() -> None:
    official = official_aws_region_labels()

    assert official
    assert set(official) <= set(AWS_COMMERCIAL_REGION_NAMES_ZH)
    for code, english in official.items():
        label = bilingual_aws_region_label(code, official_label=english)
        assert re.search(r"[\u4e00-\u9fff]", label)
        assert english in label


def test_sales_and_confirmation_region_options_share_the_official_catalog() -> None:
    official = official_aws_region_labels()
    sales_options = commercial_aws_region_options()
    confirmation_options = QuoteService._region_confirmation_options()

    assert {code for code, _ in sales_options} == set(official)
    assert {option.value for option in confirmation_options} == set(official)
    assert all(" / " in label for _, label in sales_options)


def test_recent_regions_are_not_rendered_in_english_only() -> None:
    labels = dict(commercial_aws_region_options())

    assert labels["ap-east-2"].startswith("亚太地区（台北） / ")
    assert labels["ap-southeast-7"].startswith("亚太地区（泰国） / ")
    assert labels["ca-west-1"].startswith("加拿大西部（卡尔加里） / ")
    assert labels["mx-central-1"].startswith("墨西哥（中部） / ")
