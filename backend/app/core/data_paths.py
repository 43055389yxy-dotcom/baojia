from __future__ import annotations

import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _provider_data_root(provider: str) -> Path:
    environment_key = f"ASTRAQUOTE_{provider.upper()}_DATA_DIR"
    configured = os.getenv(environment_key, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return BACKEND_ROOT / ".cache" / provider


AWS_DATA_ROOT = _provider_data_root("aws")
AZURE_DATA_ROOT = _provider_data_root("azure")
