from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

_ALGORITHM = "HS256"
_TOKEN_EXPIRE_DAYS = 30

# auto_error=False so a missing header is our 401, not HTTPBearer's default 403
# (403 is reserved for ForbiddenError).
_security = HTTPBearer(auto_error=False)


def create_jwt(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(days=_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, settings.APP_SESSION_SECRET, algorithm=_ALGORITHM)


def get_current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
) -> int:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.APP_SESSION_SECRET,
            algorithms=[_ALGORITHM],
        )
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
