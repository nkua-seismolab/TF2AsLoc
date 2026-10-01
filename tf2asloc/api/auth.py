"""Shared API key authentication dependency for write endpoints."""

import hmac
import os

from fastapi import Header, HTTPException, status


def verify_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """Require a valid X-API-Key header (TF2ASLOC_API_KEY in .env)."""
    expected = os.environ.get("TF2ASLOC_API_KEY")
    if not expected:
        # Fail closed: refuse writes if no key is configured on the server.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key authentication is not configured on this server.",
        )
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
        )
