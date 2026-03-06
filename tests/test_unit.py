"""
Offline unit tests — no network required.

Tests cover:
  - JWT token expiry detection (_is_token_expired)
  - HTTP client behaviour (mocked via respx): success, 401, 429
  - Tool-level error handling: auth/rate-limit errors return structured dicts
  - faostat_get_data truncation logic
"""

import base64
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

import faostat_mcp.client as client_module
from faostat_mcp.client import (
    FAOSTATAuthError,
    FAOSTATRateLimitError,
    _is_token_expired,
    faostat_get,
)
from faostat_mcp.server import (
    faostat_get_data,
    faostat_list_groups,
    faostat_ping,
)

# ---------------------------------------------------------------------------
# Helpers — crafted JWTs (signature is never verified by _is_token_expired)
# ---------------------------------------------------------------------------

def _make_jwt(exp: int) -> str:
    """Return a minimal JWT string with the given exp claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload_bytes = json.dumps({"exp": exp, "sub": "test"}).encode()
    payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


# Non-expiring token valid until year 2286
_VALID_TOKEN = _make_jwt(9_999_999_999)
# Token that expired one hour ago
_EXPIRED_TOKEN = _make_jwt(int(time.time()) - 3600)
# Token that expires in 30 seconds (within the 60s buffer)
_NEAR_EXPIRY_TOKEN = _make_jwt(int(time.time()) + 30)


# ---------------------------------------------------------------------------
# Autouse fixture — resets module-level singletons between every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch):
    """Prevent state leaking between tests via module-level globals."""
    monkeypatch.setattr(client_module, "_token_manager", None)
    monkeypatch.setattr(client_module, "_last_request_time", 0.0)
    monkeypatch.setenv("FAOSTAT_API_TOKEN", _VALID_TOKEN)
    monkeypatch.delenv("FAOSTAT_USERNAME", raising=False)
    monkeypatch.delenv("FAOSTAT_PASSWORD", raising=False)
    yield
    monkeypatch.setattr(client_module, "_token_manager", None)
    monkeypatch.setattr(client_module, "_last_request_time", 0.0)


# ---------------------------------------------------------------------------
# Token expiry detection — pure unit tests, no mocking needed
# ---------------------------------------------------------------------------

def test_token_not_expired_for_far_future_exp():
    assert _is_token_expired(_VALID_TOKEN) is False


def test_token_expired_for_past_exp():
    assert _is_token_expired(_EXPIRED_TOKEN) is True


def test_token_expired_within_60s_buffer():
    """Token expiring in 30 seconds is treated as expired (60s buffer)."""
    assert _is_token_expired(_NEAR_EXPIRY_TOKEN) is True


def test_token_malformed_does_not_raise():
    """Malformed JWT must return False, not raise."""
    assert _is_token_expired("not.a.jwt") is False
    assert _is_token_expired("") is False
    assert _is_token_expired("only_one_part") is False


# ---------------------------------------------------------------------------
# Client behaviour — mocked via respx
# ---------------------------------------------------------------------------

@respx.mock
async def test_faostat_get_returns_parsed_json():
    """Successful 200 response is parsed and returned as a dict/list."""
    respx.get("https://faostatservices.fao.org/api/v1/ping").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    result = await faostat_get("/ping")
    assert result == {"status": "ok"}


@respx.mock
async def test_faostat_get_raises_auth_error_on_401_no_credentials():
    """401 with no credentials → FAOSTATAuthError (no infinite retry)."""
    respx.get("https://faostatservices.fao.org/api/v1/ping").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(FAOSTATAuthError):
        await faostat_get("/ping")


@respx.mock
async def test_faostat_get_raises_rate_limit_error_on_429():
    """429 response → FAOSTATRateLimitError."""
    respx.get("https://faostatservices.fao.org/api/v1/ping").mock(
        return_value=httpx.Response(429, text="Too Many Requests")
    )
    with pytest.raises(FAOSTATRateLimitError):
        await faostat_get("/ping")


@respx.mock
async def test_faostat_get_returns_status_dict_for_empty_body():
    """Empty response body returns {"status": <code>} instead of crashing."""
    respx.get("https://faostatservices.fao.org/api/v1/ping").mock(
        return_value=httpx.Response(200, content=b"")
    )
    result = await faostat_get("/ping")
    assert result == {"status": 200}


# ---------------------------------------------------------------------------
# Tool-level error handling — mock faostat_get/faostat_post at server level
# ---------------------------------------------------------------------------

async def test_tool_returns_error_dict_on_auth_error():
    """Tools catch FAOSTATAuthError and return a structured error dict."""
    with patch("faostat_mcp.server.faostat_get", side_effect=FAOSTATAuthError("Token expired")):
        result = json.loads(await faostat_ping())
    assert result["error"] == "FAOSTATAuthError"
    assert "Token expired" in result["message"]


async def test_tool_returns_error_dict_on_rate_limit():
    """Tools catch FAOSTATRateLimitError and return a structured error dict."""
    with patch("faostat_mcp.server.faostat_get", side_effect=FAOSTATRateLimitError("429")):
        result = json.loads(await faostat_list_groups())
    assert result["error"] == "FAOSTATRateLimitError"


# ---------------------------------------------------------------------------
# faostat_get_data — truncation logic
# ---------------------------------------------------------------------------

async def test_faostat_get_data_truncates_list_response():
    """List responses larger than limit are truncated with metadata."""
    big_list = [{"row": i} for i in range(600)]
    with patch("faostat_mcp.server.faostat_get", return_value=big_list):
        result = json.loads(await faostat_get_data(domain_code="QCL", limit=500))
    assert result["_truncated"] is True
    assert result["_total_rows"] == 600
    assert result["_returned_rows"] == 500
    assert len(result["data"]) == 500


async def test_faostat_get_data_truncates_dict_with_data_key():
    """Dict responses with a 'data' list key are also truncated correctly."""
    big_response = {"data": [{"row": i} for i in range(600)], "metadata": {}}
    with patch("faostat_mcp.server.faostat_get", return_value=big_response):
        result = json.loads(await faostat_get_data(domain_code="QCL", limit=500))
    assert result["_truncated"] is True
    assert len(result["data"]) == 500


async def test_faostat_get_data_no_truncation_when_under_limit():
    """Responses under the limit are returned unchanged (no _truncated key)."""
    small_list = [{"row": i} for i in range(10)]
    with patch("faostat_mcp.server.faostat_get", return_value=small_list):
        result = json.loads(await faostat_get_data(domain_code="QCL", limit=500))
    assert "_truncated" not in result
    assert result == small_list


async def test_faostat_get_data_limit_zero_disables_truncation():
    """Setting limit=0 disables truncation entirely."""
    big_list = [{"row": i} for i in range(1000)]
    with patch("faostat_mcp.server.faostat_get", return_value=big_list):
        result = json.loads(await faostat_get_data(domain_code="QCL", limit=0))
    assert "_truncated" not in result
    assert len(result) == 1000
