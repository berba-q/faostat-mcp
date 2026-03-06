"""
FAOSTAT HTTP client for the MCP server.
Rate-limited to 2 req/s, with automatic retries on transport and 5xx errors,
and transparent JWT token refresh via AWS Cognito InitiateAuth.
"""

import asyncio
import base64
import json as _json
import logging
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

logger = logging.getLogger("faostat_mcp")

BASE_URL = os.getenv("FAOSTAT_BASE_URL", "https://faostatservices.fao.org/api/v1")
DEFAULT_LANG = os.getenv("FAOSTAT_DEFAULT_LANG", "en")

# Cognito user pool for token refresh (prod)
_COGNITO_URL = "https://cognito-idp.eu-west-1.amazonaws.com/"
_COGNITO_CLIENT_ID = os.getenv("FAOSTAT_COGNITO_CLIENT_ID", "2csltsigao85ivhp6ojp1aic7o")

_DEFAULT_PARAMS = {"caching": "true"}

_RATE_LIMIT = 2
_MIN_INTERVAL = 1.0 / _RATE_LIMIT
_last_request_time: float = 0.0
_rate_lock = asyncio.Lock()


class FAOSTATAuthError(Exception):
    """Raised when the API token is missing, expired, or invalid."""


class FAOSTATRateLimitError(Exception):
    """Raised when the API rate limit is exceeded."""


class FAOSTATServerError(Exception):
    """Raised when the API returns a 5xx server error."""


# ---------------------------------------------------------------------------
# Token management with automatic refresh
# ---------------------------------------------------------------------------

def _is_token_expired(token: str, buffer_seconds: int = 60) -> bool:
    """Return True if the JWT token is expired or will expire within buffer."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = claims.get("exp")
        if exp is None:
            return False
        return (exp - time.time()) <= buffer_seconds
    except (IndexError, ValueError, KeyError):
        return False  # can't decode — assume valid


class TokenManager:
    """Manages JWT tokens with automatic refresh via AWS Cognito InitiateAuth."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        username: str = "",
        password: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._username = username
        self._password = password
        self._lock = asyncio.Lock()

    @property
    def has_credentials(self) -> bool:
        return bool(self._username and self._password)

    async def _login(self) -> str:
        """Call AWS Cognito InitiateAuth to obtain a fresh access token."""
        logger.info("Token expired or missing — authenticating via Cognito InitiateAuth …")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _COGNITO_URL,
                headers={
                    "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
                    "Content-Type": "application/x-amz-json-1.1",
                },
                json={
                    "AuthFlow": "USER_PASSWORD_AUTH",
                    "ClientId": _COGNITO_CLIENT_ID,
                    "AuthParameters": {
                        "USERNAME": self._username,
                        "PASSWORD": self._password,
                    },
                },
            )
            if resp.status_code == 400:
                body = resp.json()
                err_type = body.get("__type", "")
                if "NotAuthorized" in err_type or "UserNotFound" in err_type:
                    raise FAOSTATAuthError(
                        "Login failed — invalid username or password. "
                        "Check FAOSTAT_USERNAME and FAOSTAT_PASSWORD in your .env file."
                    )
            resp.raise_for_status()
            data = resp.json()
            access_token = data["AuthenticationResult"]["AccessToken"]
            expires_in = data["AuthenticationResult"].get("ExpiresIn", "?")
            logger.info("Login successful — new token expires in %s seconds.", expires_in)
            return access_token

    async def get_token(self) -> str:
        """Return a valid token, refreshing automatically if needed."""
        # Fast path: token is still valid
        if self._token and not _is_token_expired(self._token):
            return self._token

        # No credentials — can't auto-refresh
        if not self.has_credentials:
            if not self._token:
                raise FAOSTATAuthError(
                    "FAOSTAT_API_TOKEN is not set and no credentials configured for auto-login. "
                    "Set FAOSTAT_USERNAME + FAOSTAT_PASSWORD in .env, or provide a token."
                )
            raise FAOSTATAuthError(
                "Your FAOSTAT_API_TOKEN has expired. "
                "Set FAOSTAT_USERNAME + FAOSTAT_PASSWORD in .env for automatic refresh, "
                "or log in at the developer portal and update your token."
            )

        # Acquire lock to avoid concurrent refresh attempts
        async with self._lock:
            # Double-check after acquiring lock
            if self._token and not _is_token_expired(self._token):
                return self._token
            self._token = await self._login()
            return self._token

    async def force_refresh(self) -> str:
        """Force a token refresh (used after 401 responses)."""
        if not self.has_credentials:
            raise FAOSTATAuthError(
                "Received 401 and no credentials configured for auto-refresh. "
                "Set FAOSTAT_USERNAME + FAOSTAT_PASSWORD in .env."
            )
        async with self._lock:
            self._token = await self._login()
            return self._token


# Lazily-initialised singleton
_token_manager: TokenManager | None = None


def _get_token_manager() -> TokenManager:
    """Return the module-level TokenManager singleton."""
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager(
            base_url=BASE_URL,
            token=os.getenv("FAOSTAT_API_TOKEN", ""),
            username=os.getenv("FAOSTAT_USERNAME", ""),
            password=os.getenv("FAOSTAT_PASSWORD", ""),
        )
    return _token_manager


async def get_token() -> str:
    return await _get_token_manager().get_token()


async def get_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {await get_token()}",
        "Accept": "application/json",
    }


async def _throttle() -> None:
    global _last_request_time
    async with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)
        _last_request_time = time.monotonic()


def _raise_for_status(response: httpx.Response) -> None:
    """Raise meaningful errors for common HTTP status codes."""
    try:
        body = response.text.strip()[:500]
    except Exception:
        body = ""
    detail = f" Server response: {body}" if body else ""

    if response.status_code == 401:
        raise FAOSTATAuthError(f"401 Unauthorized — invalid or expired API token.{detail}")
    if response.status_code == 403:
        raise FAOSTATAuthError(
            f"403 Forbidden — authentication failed.{detail} "
            "If your token expired, log in again at the developer portal and update .env."
        )
    if response.status_code == 429:
        raise FAOSTATRateLimitError(f"429 Rate limit exceeded.{detail}")
    response.raise_for_status()


def _retry_on_transient(retry_state) -> bool:
    """Retry on transport errors and 5xx server errors."""
    exc = retry_state.outcome.exception()
    if exc is None:
        return False
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return True
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=_retry_on_transient,
    reraise=True,
)
async def faostat_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Rate-limited GET request to the FAOSTAT API."""
    await _throttle()
    tm = _get_token_manager()
    async with httpx.AsyncClient(
        base_url=BASE_URL.rstrip("/"),
        headers=await get_headers(),
        params=_DEFAULT_PARAMS,
        timeout=60.0,
    ) as client:
        response = await client.get(path, params=params)
        # Auto-refresh on 401 and retry once
        if response.status_code == 401 and tm.has_credentials:
            logger.info("Got 401 — refreshing token and retrying …")
            await tm.force_refresh()
            response = await client.get(
                path, params=params, headers=await get_headers(),
            )
        _raise_for_status(response)
        if not response.content:
            return {"status": response.status_code}
        try:
            return response.json()
        except ValueError:
            logger.warning("Non-JSON response from GET %s: %.500s", path, response.text)
            return {"status": response.status_code, "text": response.text}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=_retry_on_transient,
    reraise=True,
)
async def faostat_post(path: str, json: Any = None, params: dict[str, Any] | None = None) -> Any:
    """Rate-limited POST request to the FAOSTAT API."""
    await _throttle()
    tm = _get_token_manager()
    async with httpx.AsyncClient(
        base_url=BASE_URL.rstrip("/"),
        headers=await get_headers(),
        params=_DEFAULT_PARAMS,
        timeout=60.0,
    ) as client:
        response = await client.post(path, json=json, params=params)
        # Auto-refresh on 401 and retry once
        if response.status_code == 401 and tm.has_credentials:
            logger.info("Got 401 — refreshing token and retrying …")
            await tm.force_refresh()
            response = await client.post(
                path, json=json, params=params, headers=await get_headers(),
            )
        _raise_for_status(response)
        if not response.content:
            return {"status": response.status_code}
        try:
            return response.json()
        except ValueError:
            logger.warning("Non-JSON response from POST %s: %.500s", path, response.text)
            return {"status": response.status_code, "text": response.text}
