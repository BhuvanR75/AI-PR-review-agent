"""Authentication for the console and CLI.

A single shared bearer token, compared in constant time. This is deliberately the
minimum that keeps the approval surface off the open internet, and it is the honest
state of the system: there is no per-user identity, no roles and no attribution beyond
the `actor` string a caller supplies.

Real RBAC is the next step for anything multi-user, and it is called out as such in
the README rather than implied by an `auth/` module that only holds this.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status

from backend.api.deps import get_app_context


def require_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    settings = get_app_context(request).settings
    expected = settings.api_token

    if not expected:
        # An explicitly empty token disables the check. Only sane behind a private
        # network, and logged as a warning at startup.
        return "anonymous"

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    provided = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return provided
