# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""JWT issuance and verification for Nimbus's single-user API.

Nimbus is a single-user, self-hosted deployment (PRD section 1, AD-8) --
there is no user table to authenticate against, just one shared login
secret (`settings.auth_password`). `login`'s router is exempted from
`src.api.middleware.auth.JWTAuthMiddleware` (it has to be: you cannot
require a token to obtain a token) and is the only place the password is
checked; every other route trusts a previously issued JWT.
"""

import hmac
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config.settings import settings

SUBJECT = "owner"  # the one user Nimbus has


class AuthenticationError(Exception):
    """Raised when the login password does not match."""


class TokenError(Exception):
    """Raised when a bearer token is missing, malformed, expired, or has a bad signature."""


class TokenPayload(BaseModel):
    sub: str
    exp: datetime


def verify_login_password(password: str) -> bool:
    """Constant-time comparison -- a naive `==` here would let a timing
    attack narrow down the password one byte at a time (CWE-208).
    """
    return hmac.compare_digest(password.encode("utf-8"), settings.auth_password.encode("utf-8"))


def create_access_token(expires_delta: timedelta | None = None) -> str:
    expire = datetime.now(UTC) + (expires_delta or timedelta(minutes=settings.jwt_expire_minutes))
    return jwt.encode(
        {"sub": SUBJECT, "exp": expire}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )


def decode_access_token(token: str) -> TokenPayload:
    try:
        raw = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        # Sanitised: never echoes the token itself back, only the failure kind.
        raise TokenError(type(exc).__name__) from None
    return TokenPayload.model_validate(raw)


class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


router = APIRouter()


@router.post("/token", response_model=TokenResponse)
async def login(payload: LoginRequest) -> TokenResponse:
    if not verify_login_password(payload.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return TokenResponse(access_token=create_access_token())
