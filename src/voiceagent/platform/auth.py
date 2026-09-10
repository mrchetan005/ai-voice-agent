"""Bearer-token auth for the platform API."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


async def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> None:
    expected: str = request.app.state.settings.platform.api_token
    if not expected:
        raise HTTPException(
            status_code=401,
            detail="platform API disabled: set PLATFORM_API_TOKEN",
        )
    provided = credentials.credentials if credentials else ""
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid bearer token")
