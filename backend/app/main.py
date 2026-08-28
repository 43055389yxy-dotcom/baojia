"""Backward-compatible AWS-only entrypoint.

Use ``app.azure_main:app`` for Microsoft Azure.  The default historical
``app.main:app`` command can no longer start a mixed-cloud process.
"""

from app.aws_main import app

__all__ = ["app"]
