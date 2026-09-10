"""Production entrypoint for the official multi-cloud catalog API.

The separate historical provider programs remain available only for their
legacy boundaries; production ``app.main:app`` exposes the unified MCP
backend selected by the sales portal.
"""

from app.aws_main import app

__all__ = ["app"]
