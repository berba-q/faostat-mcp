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
import pathlib
import tempfile
import time
import redis
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
import respx
import logging

import faostat_mcp.client as client_module
from faostat_mcp.client import (
    HybridCaching,
    FAOSTATAuthError,
    FAOSTATRateLimitError,
    FAOSTATServerError,
    TokenManager,
    _is_token_expired,
    _get_redis_connector,
    faostat_get,
)
from faostat_mcp.server import (
    _format_rows,
    faostat_get_codes,
    faostat_get_data,
    faostat_get_datasize,
    faostat_get_definition_type,
    faostat_resolve_name,
    faostat_get_rankings,
    faostat_list_groups,
    faostat_ping,
    faostat_search_codes,
    faostat_setup,
    caching_manager,
)
from faostat_mcp.client import (
    DiskCache,
    _save_credentials_to_storage,
    _load_credentials_from_storage,
    _reset_token_manager,
)

logger = logging.getLogger(__name__)

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
    monkeypatch.setattr(client_module, "_load_credentials_from_storage", lambda: ("", ""))
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
        result = json.loads(await faostat_get_data(domain_code="QCL", limit=500, response_format="objects"))
    assert result["_truncated"] is True
    assert result["_total_rows"] == 600
    assert result["_returned_rows"] == 500
    assert len(result["data"]) == 500


async def test_faostat_get_data_truncates_dict_with_data_key():
    """Dict responses with a 'data' list key are also truncated correctly."""
    big_response = {"data": [{"row": i} for i in range(600)], "metadata": {}}
    with patch("faostat_mcp.server.faostat_get", return_value=big_response):
        result = json.loads(await faostat_get_data(domain_code="QCL", limit=500, response_format="objects"))
    assert result["_truncated"] is True
    assert result["_returned_rows"] == 500


async def test_faostat_get_data_no_truncation_when_under_limit():
    """Responses under the limit are returned unchanged (no _truncated key)."""
    small_list = [{"row": i} for i in range(10)]
    with patch("faostat_mcp.server.faostat_get", return_value=small_list):
        result = json.loads(await faostat_get_data(domain_code="QCL", limit=500, response_format="objects"))
    assert isinstance(result, list)
    assert result == small_list


async def test_faostat_get_data_limit_zero_disables_truncation():
    """Setting limit=0 disables truncation entirely."""
    big_list = [{"row": i} for i in range(1000)]
    with patch("faostat_mcp.server.faostat_get", return_value=big_list):
        result = json.loads(await faostat_get_data(domain_code="QCL", limit=0, response_format="objects"))
    assert isinstance(result, list)
    assert len(result) == 1000


# ---------------------------------------------------------------------------
# faostat_get_definition_type — global definitions by type (no domain)
# Shapes mirror the live API: /definitions/types and /definitions/types/{type}
# ---------------------------------------------------------------------------

_TYPES_RESPONSE = {"metadata": {}, "data": [
    {"code": "areagroup", "label": "Country Group"},
    {"code": "flag", "label": "Flags"},
]}
_AREAGROUP_RESPONSE = {"metadata": {}, "data": [
    {"Country Group Code": "5100", "Country Group": "Africa", "Country Code": "114", "Country": "Kenya"},
    {"Country Group Code": "5100", "Country Group": "Africa", "Country Code": "124", "Country": "Libya"},
    {"Country Group Code": "5300", "Country Group": "Asia", "Country Code": "2", "Country": "Afghanistan"},
]}


def _fake_definitions_get(path, params=None):
    if path.endswith("/definitions/types"):
        return _TYPES_RESPONSE
    if path.endswith("/definitions/types/areagroup"):
        return _AREAGROUP_RESPONSE
    raise AssertionError(f"unexpected path {path}")


async def test_definition_type_unknown_type_lists_valid_types():
    """An unknown type must not hit /definitions/types/{type} (the API 500s on it)."""
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_definitions_get):
        result = json.loads(await faostat_get_definition_type("bogus", response_format="objects"))
    assert result["error"] == "UnknownDefinitionType"
    assert result["valid_types"] == ["areagroup", "flag"]


async def test_definition_type_search_filters_rows_case_insensitively():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_definitions_get):
        result = json.loads(await faostat_get_definition_type("areagroup", search="AFRICA", response_format="objects"))
    assert result["_total_rows"] == 2
    assert [r["Country"] for r in result["data"]] == ["Kenya", "Libya"]
    assert result["_truncated"] is False


async def test_definition_type_limit_truncates_with_metadata():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_definitions_get):
        result = json.loads(await faostat_get_definition_type("areagroup", limit=1, response_format="objects"))
    assert result["_truncated"] is True
    assert result["_total_rows"] == 3
    assert result["_returned_rows"] == 1
    assert result["columns"] == ["Country Group Code", "Country Group", "Country Code", "Country"]


async def test_definition_type_limit_zero_returns_all():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_definitions_get):
        result = json.loads(await faostat_get_definition_type("areagroup", limit=0, response_format="objects"))
    assert result["_returned_rows"] == 3
    assert result["_truncated"] is False


# ---------------------------------------------------------------------------
# FAO-defined names only — faostat_resolve_name and code validation
# ---------------------------------------------------------------------------

_DEFS = {
    "areagroup": [
        {"Country Group Code": "5100", "Country Group": "Africa", "Country": "Kenya"},
        {"Country Group Code": "5100", "Country Group": "Africa", "Country": "Libya"},
        {"Country Group Code": "5101", "Country Group": "Eastern Africa", "Country": "Kenya"},
        {"Country Group Code": "5306", "Country Group": "Sub-Saharan Africa", "Country": "Kenya"},
    ],
    "area": [{"Country Code": "114", "Country": "Kenya"}],
    "indicator": [{"Indicator Code": "21010", "Indicator": "Average dietary energy supply adequacy"}],
    "element": [
        {"Domain Code": "QCL", "Element Code": "5510", "Element": "Production"},
        {"Domain Code": "QV", "Element Code": "5510", "Element": "Production"},
    ],
    "item": [{"Item Code": "15", "Item": "Wheat"}, {"Item Code": "16", "Item": "Flour, wheat"}],
}
_DEF_TYPES = {"data": [{"code": t, "label": t} for t in _DEFS]}
_QCL_CODES = {
    "area": {"data": [{"code": "114", "label": "Kenya"}, {"code": "5100>", "label": "Africa > (List)"}]},
    "item": {"data": [{"code": "15", "label": "Wheat"}]},
    "element": {"data": [{"code": "2510", "label": "Production"}]},
}


def _fake_get(path, params=None):
    parts = path.strip("/").split("/")
    if parts[1:] == ["definitions", "types"]:
        return _DEF_TYPES
    if parts[1:3] == ["definitions", "types"]:
        return {"data": _DEFS[parts[3]]}
    if parts[1] == "codes":
        return _QCL_CODES[parts[2]]
    if parts[1] == "data":
        return [{"Value": 1}]
    raise AssertionError(f"unexpected path {path}")


async def test_resolve_region_exact_match_is_defined():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        r = json.loads(await faostat_resolve_name("region", "sub-saharan africa"))
    assert r["status"] == "defined"
    assert r["match"] == {"type": "areagroup", "definition_code": "5306", "label": "Sub-Saharan Africa"}


async def test_resolve_region_partial_match_requires_confirmation():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        r = json.loads(await faostat_resolve_name("region", "africa east"))
    # no substring hit -> not defined, but close suggestions offered
    assert r["status"] == "no_matching_definition"
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        r = json.loads(await faostat_resolve_name("region", "Africa"))
    assert r["status"] == "defined"          # exact beats substring
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        r = json.loads(await faostat_resolve_name("region", "east"))
    assert r["status"] == "ambiguous" and r["requires_confirmation"] is True
    assert [m["label"] for m in r["matches"]] == ["Eastern Africa"]


async def test_resolve_invented_region_is_not_defined_by_fao():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        r = json.loads(await faostat_resolve_name("region", "Global South"))
    assert r["status"] == "no_matching_definition"
    assert r["requires_confirmation"] is True
    assert "No matching FAOSTAT definition" in r["message"]


async def test_resolve_indicator_searches_indicators_elements_items_deduped():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        r = json.loads(await faostat_resolve_name("indicator", "production"))
    assert r["status"] == "defined"
    assert r["match"] == {"type": "element", "definition_code": "5510", "label": "Production", "domains": ["QCL", "QV"]}
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        r = json.loads(await faostat_resolve_name("indicator", "carbon happiness index"))
    assert r["status"] == "no_matching_definition"


async def test_resolve_unknown_kind_is_an_error():
    r = json.loads(await faostat_resolve_name("planet", "Mars"))
    assert r["error"] == "ValueError"


async def test_get_data_rejects_codes_not_in_domain():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        r = json.loads(await faostat_get_data("QCL", area="114,9999", item="15", response_format="objects"))
    assert r["error"] == "UnknownCode"
    assert r["unknown"] == {"area": ["9999"]}


async def test_get_data_accepts_fao_aggregate_codes():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        r = json.loads(await faostat_get_data("QCL", area="5100>", item="15", element="2510", response_format="objects"))
    assert r == [{"Value": 1}]


async def test_get_data_validation_fails_open_when_code_list_unavailable():
    def flaky(path, params=None):
        if "/codes/" in path:
            raise FAOSTATServerError("boom")
        return [{"Value": 1}]
    with patch("faostat_mcp.server.faostat_get", side_effect=flaky):
        r = json.loads(await faostat_get_data("QCL", area="114", response_format="objects"))
    assert r == [{"Value": 1}]


async def test_get_datasize_rejects_codes_not_in_domain():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get), \
         patch("faostat_mcp.server.faostat_post") as post:
        r = json.loads(await faostat_get_datasize("QCL", element="5510"))
    assert r["error"] == "UnknownCode"
    post.assert_not_called()


# ---------------------------------------------------------------------------
# Update notice — PyPI check, attached once to the first JSON-object result
# ---------------------------------------------------------------------------

_PYPI_URL = "https://pypi.org/pypi/faostat-mcp/json"


@pytest.fixture
def update_check(monkeypatch):
    import faostat_mcp.server as server_module
    monkeypatch.delenv("FAOSTAT_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(server_module, "__version__", "1.2.2")
    server_module._update = {"checked": False, "notice": None}
    yield server_module


@respx.mock
async def test_update_notice_logged_once_when_newer_release(update_check, caplog):
    respx.get(_PYPI_URL).mock(return_value=httpx.Response(200, json={"info": {"version": "1.3.0"}}))
    with patch("faostat_mcp.server.faostat_get", return_value={"status": "ok"}):
        first = json.loads(await faostat_ping())
        second = json.loads(await faostat_ping())
    assert "_update_notice" not in first
    assert "1.3.0" in caplog.text
    assert sum("1.3.0" in record.message for record in caplog.records) == 1
    assert "_update_notice" not in second
    assert respx.calls.call_count == 1


@respx.mock
async def test_update_notice_absent_when_current(update_check):
    respx.get(_PYPI_URL).mock(return_value=httpx.Response(200, json={"info": {"version": "1.2.2"}}))
    with patch("faostat_mcp.server.faostat_get", return_value={"status": "ok"}):
        result = json.loads(await faostat_ping())
    assert "_update_notice" not in result


@respx.mock
async def test_update_check_failure_is_silent(update_check):
    respx.get(_PYPI_URL).mock(side_effect=httpx.ConnectError("offline"))
    with patch("faostat_mcp.server.faostat_get", return_value={"status": "ok"}):
        result = json.loads(await faostat_ping())
    assert result == {"status": "ok"}


@respx.mock
async def test_update_notice_does_not_change_tool_results(update_check):
    """Update checks preserve both list and object result shapes."""
    respx.get(_PYPI_URL).mock(return_value=httpx.Response(200, json={"info": {"version": "2.0.0"}}))
    with patch("faostat_mcp.server.faostat_get", return_value=[{"row": 1}]):
        listed = json.loads(await faostat_get_data(domain_code="QCL", response_format="objects"))
    with patch("faostat_mcp.server.faostat_get", return_value={"status": "ok"}):
        pinged = json.loads(await faostat_ping())
    assert listed == [{"row": 1}]
    assert "_update_notice" not in pinged
    assert "2.0.0" in update_check._update["notice"]


@respx.mock
async def test_update_check_opt_out(update_check, monkeypatch):
    monkeypatch.setenv("FAOSTAT_NO_UPDATE_CHECK", "1")
    route = respx.get(_PYPI_URL).mock(return_value=httpx.Response(200, json={"info": {"version": "9.0.0"}}))
    with patch("faostat_mcp.server.faostat_get", return_value={"status": "ok"}):
        result = json.loads(await faostat_ping())
    assert "_update_notice" not in result
    assert not route.called


# ---------------------------------------------------------------------------
# /auth/login endpoint — token refresh via the FAOSTAT backend
# ---------------------------------------------------------------------------

_BASE_URL = "https://faostatservices.fao.org/api/v1"
_AUTH_URL = f"{_BASE_URL}/auth/login"


def _make_auth_response(token: str) -> dict:
    """Build the AuthenticationResult payload returned by /auth/login."""
    return {
        "AuthenticationResult": {
            "AccessToken": token,
            "ExpiresIn": 3600,
            "IdToken": "id-token-value",
            "RefreshToken": "refresh-token-value",
            "TokenType": "Bearer",
        },
        "ChallengeParameters": {},
    }


@respx.mock
async def test_login_via_auth_endpoint_succeeds():
    """_login() POSTs form-encoded credentials to /auth/login and returns AccessToken."""
    fresh_token = _make_jwt(int(time.time()) + 3600)
    respx.post(_AUTH_URL).mock(
        return_value=httpx.Response(200, json=_make_auth_response(fresh_token))
    )
    tm = TokenManager(base_url=_BASE_URL, username="user@example.com", password="secret")
    token = await tm._login()
    assert token == fresh_token


@respx.mock
async def test_login_via_auth_endpoint_sends_form_encoded_body():
    """_login() must use application/x-www-form-urlencoded (not JSON)."""
    fresh_token = _make_jwt(int(time.time()) + 3600)
    captured = {}

    def capture(request):
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json=_make_auth_response(fresh_token))

    respx.post(_AUTH_URL).mock(side_effect=capture)
    tm = TokenManager(base_url=_BASE_URL, username="user@example.com", password="secret")
    await tm._login()

    assert "application/x-www-form-urlencoded" in captured["content_type"]
    assert "username=user%40example.com" in captured["body"] or "username=user@example.com" in captured["body"]
    assert "password=secret" in captured["body"]


@respx.mock
async def test_login_via_auth_endpoint_raises_auth_error_on_401():
    """_login() raises FAOSTATAuthError when /auth/login returns 401."""
    respx.post(_AUTH_URL).mock(return_value=httpx.Response(401))
    tm = TokenManager(base_url=_BASE_URL, username="wrong@example.com", password="bad")
    with pytest.raises(FAOSTATAuthError, match="invalid username or password"):
        await tm._login()


@respx.mock
async def test_login_via_auth_endpoint_raises_auth_error_on_400():
    """_login() raises FAOSTATAuthError when /auth/login returns 400 (bad request)."""
    respx.post(_AUTH_URL).mock(return_value=httpx.Response(400, json={"detail": "Bad Request"}))
    tm = TokenManager(base_url=_BASE_URL, username="user@example.com", password="wrong")
    with pytest.raises(FAOSTATAuthError, match="invalid username or password"):
        await tm._login()


@respx.mock
async def test_get_token_triggers_auth_endpoint_when_token_expired():
    """get_token() calls /auth/login when the stored token is expired."""
    fresh_token = _make_jwt(int(time.time()) + 3600)
    respx.post(_AUTH_URL).mock(
        return_value=httpx.Response(200, json=_make_auth_response(fresh_token))
    )
    tm = TokenManager(
        base_url=_BASE_URL,
        token=_EXPIRED_TOKEN,
        username="user@example.com",
        password="secret",
    )
    token = await tm.get_token()
    assert token == fresh_token


@respx.mock
async def test_force_refresh_uses_auth_endpoint():
    """force_refresh() fetches a new token from /auth/login and updates internal state."""
    fresh_token = _make_jwt(int(time.time()) + 3600)
    respx.post(_AUTH_URL).mock(
        return_value=httpx.Response(200, json=_make_auth_response(fresh_token))
    )
    tm = TokenManager(
        base_url=_BASE_URL,
        token=_EXPIRED_TOKEN,
        username="user@example.com",
        password="secret",
    )
    refreshed = await tm.force_refresh()
    assert refreshed == fresh_token
    assert tm._token == fresh_token


@respx.mock
async def test_faostat_get_auto_refreshes_via_auth_endpoint_on_401():
    """faostat_get() transparently refreshes via /auth/login when the API returns 401."""
    fresh_token = _make_jwt(int(time.time()) + 3600)

    # First API call → 401; second (after refresh) → 200
    api_route = respx.get("https://faostatservices.fao.org/api/v1/ping")
    api_route.side_effect = [
        httpx.Response(401, text="Unauthorized"),
        httpx.Response(200, json={"status": "ok"}),
    ]
    respx.post(_AUTH_URL).mock(
        return_value=httpx.Response(200, json=_make_auth_response(fresh_token))
    )

    # Seed the module-level manager with credentials so auto-refresh is enabled
    client_module._token_manager = TokenManager(
        base_url=_BASE_URL,
        token=_VALID_TOKEN,
        username="user@example.com",
        password="secret",
    )

    result = await faostat_get("/ping")
    assert result == {"status": "ok"}


@respx.mock
async def test_faostat_get_auto_refreshes_on_403_revoked_token():
    """The API returns 403 'Authentication Failed' for a revoked but unexpired token.

    faostat_get() must refresh and retry, as it does for 401.
    """
    fresh_token = _make_jwt(int(time.time()) + 3600)
    api_route = respx.get("https://faostatservices.fao.org/api/v1/ping")
    api_route.side_effect = [
        httpx.Response(403, text="Authentication Failed"),
        httpx.Response(200, json={"status": "ok"}),
    ]
    login_route = respx.post(_AUTH_URL).mock(
        return_value=httpx.Response(200, json=_make_auth_response(fresh_token))
    )
    client_module._token_manager = TokenManager(
        base_url=_BASE_URL,
        token=_VALID_TOKEN,
        username="user@example.com",
        password="secret",
    )

    result = await faostat_get("/ping")
    assert result == {"status": "ok"}
    assert login_route.call_count == 1


@respx.mock
async def test_faostat_get_raises_auth_error_on_403_after_refresh():
    """A 403 that persists after one refresh raises FAOSTATAuthError (no loop)."""
    fresh_token = _make_jwt(int(time.time()) + 3600)
    api_route = respx.get("https://faostatservices.fao.org/api/v1/ping").mock(
        return_value=httpx.Response(403, text="Authentication Failed")
    )
    respx.post(_AUTH_URL).mock(
        return_value=httpx.Response(200, json=_make_auth_response(fresh_token))
    )
    client_module._token_manager = TokenManager(
        base_url=_BASE_URL,
        token=_VALID_TOKEN,
        username="user@example.com",
        password="secret",
    )

    with pytest.raises(FAOSTATAuthError):
        await faostat_get("/ping")
    assert api_route.call_count == 2


# _format_rows helper — pure unit tests
# ---------------------------------------------------------------------------

_SAMPLE_ROWS = [
    {"Area": "India", "Item": "Wheat", "Year": 2024, "Value": "109590000"},
    {"Area": "USA", "Item": "Wheat", "Year": 2024, "Value": "49691000"},
    {"Area": "China", "Item": "Wheat", "Year": 2024, "Value": "136590000"},
]


def test_format_rows_objects():
    """'objects' format returns the original JSON array."""
    result = json.loads(_format_rows(_SAMPLE_ROWS, response_format="objects"))
    assert result == _SAMPLE_ROWS


def test_format_rows_compact():
    """'compact' format returns columns + rows arrays."""
    result = json.loads(_format_rows(_SAMPLE_ROWS, response_format="compact"))
    assert result["columns"] == ["Area", "Item", "Year", "Value"]
    assert len(result["rows"]) == 3
    assert result["rows"][0] == ["India", "Wheat", 2024, "109590000"]


def test_format_rows_csv():
    """'csv' format returns plain CSV text with header."""
    result = _format_rows(_SAMPLE_ROWS, response_format="csv")
    lines = result.strip().split("\n")
    assert lines[0] == "Area,Item,Year,Value"
    assert lines[1] == "India,Wheat,2024,109590000"
    assert len(lines) == 4  # header + 3 data rows


def test_format_rows_csv_with_commas_in_values():
    """CSV correctly quotes values containing commas."""
    rows = [{"Area": "China, mainland", "Value": "100"}]
    result = _format_rows(rows, response_format="csv")
    lines = result.strip().split("\n")
    assert '"China, mainland"' in lines[1]


def test_format_rows_field_selection():
    """fields parameter filters to specified columns."""
    result = json.loads(_format_rows(_SAMPLE_ROWS, fields=["Area", "Value"]))
    assert list(result[0].keys()) == ["Area", "Value"]
    assert len(result[0]) == 2


def test_format_rows_field_selection_with_compact():
    """fields + compact format returns filtered columns."""
    result = json.loads(_format_rows(
        _SAMPLE_ROWS, response_format="compact", fields=["Area", "Value"]
    ))
    assert result["columns"] == ["Area", "Value"]
    assert result["rows"][0] == ["India", "109590000"]


def test_format_rows_empty_list():
    """Empty input returns '[]' regardless of format."""
    assert _format_rows([], response_format="objects") == "[]"
    assert _format_rows([], response_format="compact") == "[]"
    assert _format_rows([], response_format="csv") == "[]"


def test_format_rows_invalid_fields_ignored():
    """Non-existent field names fall back to all columns."""
    result = json.loads(_format_rows(_SAMPLE_ROWS, fields=["NonExistent"]))
    # All original columns retained when no valid fields match
    assert list(result[0].keys()) == ["Area", "Item", "Year", "Value"]


# ---------------------------------------------------------------------------
# faostat_get_data — response format integration
# ---------------------------------------------------------------------------

async def test_faostat_get_data_default_limit_is_50():
    """Default limit is now 50 (not 500)."""
    big_list = [{"Area": "X", "Value": i} for i in range(100)]
    with patch("faostat_mcp.server.faostat_get", return_value=big_list):
        result = json.loads(await faostat_get_data(domain_code="QCL", response_format="objects"))
    assert result["_truncated"] is True
    assert result["_returned_rows"] == 50


async def test_faostat_get_data_show_codes_default_false():
    """show_codes defaults to False — verify param passed to API."""
    with patch("faostat_mcp.server.faostat_get", return_value=[]) as mock_get:
        await faostat_get_data(domain_code="QCL", response_format="objects")
    call_params = mock_get.call_args[1]["params"]
    assert call_params["show_codes"] is False
    assert call_params["show_flags"] is False


async def test_faostat_get_data_compact_format():
    """response_format='compact' returns columnar structure."""
    rows = [{"Area": "India", "Value": "100"}]
    with patch("faostat_mcp.server.faostat_get", return_value=rows):
        result = json.loads(await faostat_get_data(
            domain_code="QCL", response_format="compact"
        ))
    assert "columns" in result
    assert "rows" in result
    assert result["columns"] == ["Area", "Value"]


async def test_faostat_get_data_csv_format():
    """response_format='csv' returns plain CSV text."""
    rows = [{"Area": "India", "Value": "100"}]
    with patch("faostat_mcp.server.faostat_get", return_value=rows):
        result = await faostat_get_data(domain_code="QCL", response_format="csv")
    lines = result.strip().split("\n")
    assert lines[0] == "Area,Value"
    assert lines[1] == "India,100"


async def test_faostat_get_data_csv_truncated():
    """Truncated CSV includes metadata comment line."""
    big_list = [{"Area": "X", "Value": str(i)} for i in range(100)]
    with patch("faostat_mcp.server.faostat_get", return_value=big_list):
        result = await faostat_get_data(
            domain_code="QCL", response_format="csv", limit=10
        )
    assert result.startswith("# truncated:")
    lines = result.strip().split("\n")
    # Comment + header + 10 data rows
    assert len(lines) == 12


async def test_faostat_get_data_field_selection():
    """fields parameter filters columns in the response."""
    rows = [{"Area": "India", "Item": "Wheat", "Value": "100"}]
    with patch("faostat_mcp.server.faostat_get", return_value=rows):
        result = json.loads(await faostat_get_data(
            domain_code="QCL", fields="Area,Value"
        , response_format="objects"))
    assert list(result[0].keys()) == ["Area", "Value"]


async def test_faostat_get_data_invalid_format_returns_error():
    """Invalid response_format returns an error dict."""
    result = json.loads(await faostat_get_data(
        domain_code="QCL", response_format="xml"
    ))
    assert result["error"] == "ValueError"


# ---------------------------------------------------------------------------
# faostat_get_codes — limit parameter
# ---------------------------------------------------------------------------

async def test_faostat_get_codes_truncates_when_limit_set():
    """faostat_get_codes truncates large code lists when limit > 0.
    Uses a dict response matching the real FAOSTAT API shape: {"metadata": ..., "data": [...]}.
    """
    codes_list = [{"code": str(i), "description": f"Item {i}"} for i in range(300)]
    api_response = {"metadata": {}, "data": codes_list}
    with patch("faostat_mcp.server.faostat_get", return_value=api_response):
        result = json.loads(await faostat_get_codes(
            dimension_id="item", domain_code="QCL", limit=50
        ))
    assert result["_truncated"] is True
    assert result["_total_codes"] == 300
    assert len(result["data"]) == 50


async def test_faostat_get_codes_no_limit_returns_all():
    """faostat_get_codes with default limit=0 returns all codes.
    Uses a dict response matching the real FAOSTAT API shape: {"metadata": ..., "data": [...]}.
    """
    codes_list = [{"code": str(i)} for i in range(300)]
    api_response = {"metadata": {}, "data": codes_list}
    with patch("faostat_mcp.server.faostat_get", return_value=api_response):
        with patch.object(caching_manager, "get_data", return_value=None):
            result = json.loads(await faostat_get_codes(
                dimension_id="item", domain_code="QCL"
            ))
    assert result["metadata"] == {}
    assert len(result["data"]) == 300


async def test_faostat_get_codes_caches_result():
    """Reproduces bug: faostat_get_codes set_data call was missing arg_dict,
    causing TypeError on every cache store attempt after a successful API call.
    Fixed by passing arg_dict as the second argument to set_data."""
    codes = [{"code": str(i)} for i in range(10)]
    with patch("faostat_mcp.server.faostat_get", return_value=codes):
        with patch.object(caching_manager, "get_data", return_value=None):
            with patch.object(caching_manager, "set_data") as mock_set:
                result = json.loads(await faostat_get_codes(
                    dimension_id="item", domain_code="QCL"
                ))
    # Verify set_data was called with all 3 args: (tool_name, arg_dict, data)
    assert mock_set.call_count == 2
    call_args = mock_set.call_args[0]
    assert len(call_args) == 3, (
        f"set_data called with {len(call_args)} args instead of 3 — "
        "missing arg_dict causes TypeError at runtime"
    )
    assert call_args[0] == "faostat_get_codes"
    assert isinstance(call_args[1], dict)
    assert len(result) == 10


# ---------------------------------------------------------------------------
# faostat_get_rankings — response_format parameter
# ---------------------------------------------------------------------------

async def test_faostat_get_rankings_compact_format():
    """faostat_get_rankings supports compact format."""
    rankings = [{"Area": "China", "Value": "136M", "Rank": 1}]
    with patch("faostat_mcp.server.faostat_post", return_value=rankings):
        result = json.loads(await faostat_get_rankings(
            domain_code="QCL", element_code="5510",
            item_code="15", year="2022", response_format="compact"
        ))
    assert "columns" in result
    assert "rows" in result


# ---------------------------------------------------------------------------
# HybridCaching class — Caching features logic and fallback
# ---------------------------------------------------------------------------

class TestHybridCaching(unittest.TestCase):

    def setUp(self):
        # Need to reset class-level variables
        HybridCaching.user_caches = {}
        HybridCaching.min_heap_ttl = []

    def test_initialization(self):
        """Verify HybridCaching initialization."""
        mock_redis = MagicMock()
        cache = HybridCaching(user_token="user1_token", redis_conn=mock_redis)
        assert cache.user_token == "user1_token"
        assert cache.redis_conn == mock_redis

    @patch("faostat_mcp.client.time.time")
    def test_mem_cache_logic(self, mock_time):
        """Testing memory cache logic."""
        mock_time.return_value = 1000.0
        cache = HybridCaching(user_token="user1", mem_cache_ttl=60)
        cache.set_mem_cache("tool", {"arg": 1}, "data")
        assert "user1" in HybridCaching.user_caches
        assert cache.get_mem_cache("tool", {"arg": 1}) == "data"

    @patch("faostat_mcp.client.time.time")
    def test_mem_cache_hit_refreshes_ttl(self, mock_time):
        """Verify that a cache hit returns data and extends its life."""
        mock_time.return_value = 1000.0
        cache = HybridCaching(user_token="user1_token", mem_cache_ttl=100)

        tool = "faostat_list_domains"
        args = {"group_code": "Q"}
        data = {"lang": "en"}
        cache.set_mem_cache(tool, args, data)

        user_cache = HybridCaching.user_caches["user1_token"]
        initial_key = list(user_cache.keys())[0]
        initial_expiry = user_cache[initial_key][0]
        assert initial_expiry == 1100.0
        mock_time.return_value = 1050.0
        result = cache.get_mem_cache(tool, args)
        assert result == data
        new_expiry = user_cache[initial_key][0]
        assert new_expiry == 1150.0
        assert len(HybridCaching.min_heap_ttl) == 2

    @patch("faostat_mcp.client.time.time")
    def test_time_eviction(self, mock_time):
        """Verifies that expired items are cleared during the next 'set' operation."""
        mock_time.return_value = 1000.0
        cache = HybridCaching(mem_cache_ttl=10)
        cache.set_mem_cache("tool_1", {"arg": 1}, "data1")
        mock_time.return_value = 1011.0
        cache.set_mem_cache("tool_2", {"arg": 2}, "data2")
        user_cache = HybridCaching.user_caches[cache.user_token]
        assert len(user_cache) == 1
        assert "tool_2" in list(user_cache.keys())[0]

    @patch("faostat_mcp.client.time.time")
    def test_size_limit_eviction(self, mock_time):
        """Verifies that the oldest item is removed if MAX_CACHE_SIZE is exceeded."""
        mock_time.return_value = 1000.0
        cache = HybridCaching(max_mem_cache_size=1, user_token="user1")
        cache.set_mem_cache("tool_1", {"arg": 1}, "data1")
        mock_time.return_value = 1005.0
        cache.set_mem_cache("tool_2", {"arg": 2}, "data2")

        user_cache = HybridCaching.user_caches[cache.user_token]
        assert len(user_cache) == 1
        assert any("tool_2" in k for k in user_cache.keys())

    def test_bulk_eviction(self):
        """Add 220 items to a cache limited to 5 to see if it stabilizes."""
        limit = 5
        cache = HybridCaching(user_token="user1_token", max_mem_cache_size=limit)
        for i in range(220):
            cache.set_mem_cache(f"tool_{i}", {}, f"data_{i}")
        user_cache = HybridCaching.user_caches["user1_token"]
        assert len(user_cache) <= limit

    def test_redis_cache_set(self):
        """Directly verify interaction with the mock redis object."""
        mock_redis = MagicMock()
        cache = HybridCaching(user_token="user1", redis_conn=mock_redis)
        with patch.object(cache, 'get_redis_cache', return_value=None):
            cache.set_redis_cache("faostat_list_groups", {"lang": "en"}, {"result": "data"})
            mock_redis.setex.assert_called_once()
            call_args = mock_redis.setex.call_args[0]
            assert "mcp:cache:user1:faostat_list_groups" in call_args[0]

    def test_get_redis_cache_hit(self):
        """Verify redis retrieves data, refreshes TTL, and decodes JSON."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_data = {"row1": "data1", "row2": "data2"}
        mock_pipe.execute.return_value = [json.dumps(mock_data)]
        cache = HybridCaching(user_token="user1_token", redis_conn=mock_redis, redis_cache_ttl=500)
        result = cache.get_redis_cache("faostat_list_groups", {"lang": "en"})
        mock_pipe.get.assert_called_once()
        mock_pipe.expire.assert_called_with(unittest.mock.ANY, 500)
        assert result == mock_data

    def test_get_redis_cache_miss(self):
        """Verify behavior when Redis returns nothing."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [None]
        cache = HybridCaching(user_token="user1_token", redis_conn=mock_redis)
        result = cache.get_redis_cache("tool_1", {"item": "unknown"})
        assert result is None

    @patch("faostat_mcp.client.logger")
    def test_get_redis_cache_error(self, mock_logger):
        """Verify that Redis errors during a GET are caught and logged."""
        mock_redis = MagicMock()
        mock_redis.pipeline.side_effect = redis.RedisError("Redis Down")
        cache = HybridCaching(user_token="user1", redis_conn=mock_redis)
        result = cache.get_redis_cache("tool", {})
        assert result is None
        mock_logger.error.assert_called()

    @patch("faostat_mcp.client.logger")
    def test_get_redis_cache_corrupted_json(self, mock_logger):
        """Verify that invalid JSON in Redis does not crash the application."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        corrupted_data = '{"row1": "data1", "row2": "data2"'
        mock_pipe.execute.return_value = [corrupted_data]
        cache = HybridCaching(user_token="user1", redis_conn=mock_redis)
        try:
            result = cache.get_redis_cache("tool", {})
            assert result is None
        except json.JSONDecodeError:
            self.fail("get_redis_cache raised JSONDecodeError instead of returning None")

    def test_user_isolation(self):
        """Verify that different user tokens have isolated caches."""
        cache_a = HybridCaching(user_token="user1_token")
        cache_b = HybridCaching(user_token="User2_token")
        shared_args = {"arg": 100}
        cache_a.set_mem_cache("get_data", shared_args, "User1's Private Data")
        assert cache_b.get_mem_cache("get_data", shared_args) is None

    def test_cache_key_order_independence(self):
        """Ensure that the same dict content produces the same hash regardless of order."""
        args_v1 = {"param_a": 1, "param_b": 2, "param_c": 3}
        args_v2 = {"param_c": 3, "param_a": 1, "param_b": 2}
        key1 = HybridCaching._HybridCaching__create_cache_key(args_v1)
        key2 = HybridCaching._HybridCaching__create_cache_key(args_v2)
        assert key1 == key2

    def test_cache_key_is_different_for_different_data(self):
        """Ensure that different data produces different hashes."""
        args_a = {"param": 1}
        args_b = {"param": 2}
        key_a = HybridCaching._HybridCaching__create_cache_key(args_a)
        key_b = HybridCaching._HybridCaching__create_cache_key(args_b)
        assert key_a != key_b


class TestFallback(unittest.IsolatedAsyncioTestCase):
    async def test_graceful_fallback_to_memory(self):
        """Test that memory cache is automatically used when Redis is unavailable."""
        with patch.object(caching_manager, 'redis_conn', None):
            with patch.object(caching_manager, 'get_mem_cache', return_value=None) as mock_get_mem, \
                 patch.object(caching_manager, 'set_mem_cache') as mock_set_mem, \
                 patch('faostat_mcp.server.faostat_get', new_callable=AsyncMock) as mock_api:
                mock_api_data = {"data": "from_api"}
                mock_api.return_value = mock_api_data
                await faostat_list_groups()
                mock_get_mem.assert_called_once()
                mock_set_mem.assert_called_once()
                with self.assertRaises(AttributeError):
                    caching_manager.get_redis_cache.assert_not_called()


# ---------------------------------------------------------------------------
# _get_redis_connector method — Redis connection logic
# ---------------------------------------------------------------------------

class TestRedisConnector(unittest.TestCase):
    @patch("faostat_mcp.client.redis.from_url")
    @patch("faostat_mcp.client.os.getenv")
    def test_connector_success(self, mock_getenv, mock_redis_from_url):
        """Test successful Redis connection and ping."""
        mock_getenv.side_effect = lambda k, d=None: "127.0.0.1" if k == "REDIS_HOST_IP_ADDRESS" else d
        mock_conn = MagicMock()
        mock_conn.ping.return_value = True
        mock_redis_from_url.return_value = mock_conn
        result = _get_redis_connector()
        assert result == mock_conn
        mock_redis_from_url.assert_called_once()
        mock_conn.ping.assert_called_once()

    @patch("faostat_mcp.client.redis.from_url")
    def test_connector_ping_failure(self, mock_redis_from_url):
        """Test when connection is made but ping() returns False."""
        mock_conn = MagicMock()
        mock_conn.ping.return_value = False
        mock_redis_from_url.return_value = mock_conn
        result = _get_redis_connector()
        assert result is None

    @patch("faostat_mcp.client.redis.from_url")
    def test_connector_connection_error(self, mock_redis_from_url):
        """Test when redis.from_url raises a ConnectionError."""
        mock_redis_from_url.side_effect = redis.ConnectionError("Network down")
        result = _get_redis_connector()
        assert result is None

    @patch("faostat_mcp.client.redis.from_url")
    def test_connector_generic_exception(self, mock_redis_from_url):
        """Test when an unexpected Exception occurs."""
        mock_redis_from_url.side_effect = Exception("Unexpected crash")
        result = _get_redis_connector()
        assert result is None

    @patch("faostat_mcp.client.redis.from_url")
    @patch("faostat_mcp.client.os.getenv")
    def test_connector_authentication_failure(self, mock_getenv, mock_redis_from_url):
        """Test behavior when credentials (password/username) are wrong."""
        mock_getenv.side_effect = lambda k, d=None: "wrong_pass" if k == "REDIS_PASSWORD" else d
        mock_conn = MagicMock()
        mock_conn.ping.side_effect = redis.AuthenticationError("Invalid password")
        mock_redis_from_url.return_value = mock_conn
        result = _get_redis_connector()
        assert result is None
        mock_conn.ping.assert_called_once()


# ---------------------------------------------------------------------------
# faostat_search_codes — disambiguation logic
# ---------------------------------------------------------------------------

# Realistic QCL element codes response (subset of real API structure)
_QCL_ELEMENTS = {
    "metadata": {},
    "data": [
        {"code": "2510", "Element": "Production"},
        {"code": "2312", "Element": "Area harvested"},
        {"code": "2413", "Element": "Yield"},
        {"code": "2111", "Element": "Stocks"},
        {"code": "2512", "Element": "Gross Production Index Number"},
        {"code": "2611", "Element": "Export Quantity"},
    ],
}

_QCL_ITEMS = {
    "metadata": {},
    "data": [
        {"code": "15",  "Item": "Wheat"},
        {"code": "44",  "Item": "Durum wheat"},
        {"code": "56",  "Item": "Maize (corn)"},
        {"code": "71",  "Item": "Rye"},
        {"code": "515", "Item": "Apples"},
    ],
}

_QCL_AREAS = {
    "metadata": {},
    "data": [
        {"code": "231", "Area": "Nigeria"},
        {"code": "232", "Area": "Niger"},
        {"code": "2",   "Area": "Afghanistan"},
    ],
}


@patch("faostat_mcp.server.caching_manager")
async def test_search_codes_single_match_no_confirmation(mock_cache):
    """A query matching exactly one code returns requires_confirmation=False."""
    mock_cache.get_data.return_value = None
    with patch("faostat_mcp.server.faostat_get", return_value=_QCL_ELEMENTS):
        result = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="element", query="yield"
        ))
    assert result["requires_confirmation"] is False
    assert "match" in result
    assert result["match"]["code"] == "2413"
    assert "Yield" in result["match"]["label"]
    assert "2413" in result["message"]


@patch("faostat_mcp.server.caching_manager")
async def test_search_codes_multiple_matches_requires_confirmation(mock_cache):
    """A query matching multiple codes returns requires_confirmation=True with MUST."""
    mock_cache.get_data.return_value = None
    with patch("faostat_mcp.server.faostat_get", return_value=_QCL_ELEMENTS):
        result = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="element", query="production"
        ))
    assert result["requires_confirmation"] is True
    assert "matches" in result
    codes = [m["code"] for m in result["matches"]]
    assert "2510" in codes  # Production
    assert "2512" in codes  # Gross Production Index Number
    assert len(result["matches"]) >= 2
    assert "MUST" in result["message"]


@patch("faostat_mcp.server.caching_manager")
async def test_search_codes_no_matches(mock_cache):
    """A query with no matches returns empty list, requires_confirmation=False."""
    mock_cache.get_data.return_value = None
    with patch("faostat_mcp.server.faostat_get", return_value=_QCL_ELEMENTS):
        result = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="element", query="gross yield index"
        ))
    assert result["requires_confirmation"] is False
    assert result["matches"] == []
    assert "faostat_get_codes" in result["message"]


@patch("faostat_mcp.server.caching_manager")
async def test_search_codes_case_insensitive(mock_cache):
    """Search is case-insensitive: 'nigeria' and 'NIGERIA' return the same code."""
    mock_cache.get_data.return_value = None
    with patch("faostat_mcp.server.faostat_get", return_value=_QCL_AREAS):
        lower = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="area", query="nigeria"
        ))
        upper = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="area", query="NIGERIA"
        ))
    assert lower["requires_confirmation"] is False
    assert upper["requires_confirmation"] is False
    assert lower["match"]["code"] == upper["match"]["code"] == "231"


@patch("faostat_mcp.server.caching_manager")
async def test_search_codes_partial_match_ambiguous(mock_cache):
    """Partial match 'niger' matches both Nigeria and Niger — requires confirmation."""
    mock_cache.get_data.return_value = None
    with patch("faostat_mcp.server.faostat_get", return_value=_QCL_AREAS):
        result = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="area", query="niger"
        ))
    assert result["requires_confirmation"] is True
    codes = [m["code"] for m in result["matches"]]
    assert "231" in codes  # Nigeria
    assert "232" in codes  # Niger


@patch("faostat_mcp.server.caching_manager")
async def test_search_codes_item_dimension(mock_cache):
    """Item search: 'wheat' matches Wheat + Durum wheat → multiple matches."""
    mock_cache.get_data.return_value = None
    with patch("faostat_mcp.server.faostat_get", return_value=_QCL_ITEMS):
        result = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="item", query="wheat"
        ))
    assert result["requires_confirmation"] is True
    codes = [m["code"] for m in result["matches"]]
    assert "15" in codes   # Wheat
    assert "44" in codes   # Durum wheat


@patch("faostat_mcp.server.caching_manager")
async def test_search_codes_plain_list_response(mock_cache):
    """Tool handles a plain-list API response (no 'data' wrapper)."""
    mock_cache.get_data.return_value = None
    plain_list = [
        {"code": "15", "Item": "Wheat"},
        {"code": "44", "Item": "Durum wheat"},
    ]
    with patch("faostat_mcp.server.faostat_get", return_value=plain_list):
        result = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="item", query="durum"
        ))
    assert result["requires_confirmation"] is False
    assert result["match"]["code"] == "44"


@patch("faostat_mcp.server.caching_manager")
async def test_search_codes_auth_error_returns_error_dict(mock_cache):
    """FAOSTATAuthError is caught and returned as a structured error dict."""
    mock_cache.get_data.return_value = None
    with patch("faostat_mcp.server.faostat_get", side_effect=FAOSTATAuthError("expired")):
        result = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="element", query="production"
        ))
    assert result["error"] == "FAOSTATAuthError"
    assert "expired" in result["message"]


@patch("faostat_mcp.server.caching_manager")
async def test_search_codes_cache_hit_bypasses_api(mock_cache):
    """A cached result is returned immediately without calling faostat_get."""
    cached = {
        "match": {"code": "2413", "label": "Yield"},
        "requires_confirmation": False,
        "message": "cached",
    }
    mock_cache.get_data.return_value = cached
    with patch("faostat_mcp.server.faostat_get") as mock_get:
        result = json.loads(await faostat_search_codes(
            domain_code="QCL", dimension_id="element", query="yield"
        ))
    mock_get.assert_not_called()
    assert result == cached


# ---------------------------------------------------------------------------
# DiskCache — cross-session persistence
# ---------------------------------------------------------------------------


def test_disk_cache_stores_and_retrieves():
    """DiskCache stores a value and retrieves it before expiry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = DiskCache(db_path=pathlib.Path(tmpdir) / "test.db", ttl=60)
        cache.set("key1", {"answer": 42})
        result = cache.get("key1")
    assert result == {"answer": 42}


def test_disk_cache_expires_entries():
    """DiskCache returns None for entries past their TTL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = DiskCache(db_path=pathlib.Path(tmpdir) / "test.db", ttl=-1)  # already expired
        cache.set("key1", {"answer": 42})
        result = cache.get("key1")
    assert result is None


@patch.dict("os.environ", {"FAOSTAT_DISK_CACHE": "false"})
def test_disk_cache_disabled_when_env_false():
    """FAOSTAT_DISK_CACHE=false means all reads return None and writes are no-ops."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = DiskCache(db_path=pathlib.Path(tmpdir) / "test.db", ttl=60)
        cache.set("key1", {"answer": 42})
        result = cache.get("key1")
    assert result is None


# ---------------------------------------------------------------------------
# faostat_setup — one-time credential configuration
# ---------------------------------------------------------------------------

async def test_faostat_setup_stores_credentials_on_success():
    """faostat_setup validates credentials, saves them, and resets the token manager."""
    fresh_token = _make_jwt(int(time.time()) + 3600)
    with patch("faostat_mcp.server.TokenManager") as MockTM, \
         patch("faostat_mcp.server._save_credentials_to_storage", return_value="system keychain") as mock_save, \
         patch("faostat_mcp.server._reset_token_manager") as mock_reset:
        mock_instance = AsyncMock()
        mock_instance.force_refresh = AsyncMock(return_value=fresh_token)
        MockTM.return_value = mock_instance
        result = json.loads(await faostat_setup(
            username="user@example.com", password="secret123"
        ))
    assert result["status"] == "ok"
    assert "system keychain" in result["message"]
    mock_save.assert_called_once_with("user@example.com", "secret123")
    mock_reset.assert_called_once()


async def test_faostat_setup_returns_error_on_bad_creds():
    """faostat_setup returns status=error when credentials are invalid."""
    with patch("faostat_mcp.server.TokenManager") as MockTM:
        mock_instance = AsyncMock()
        mock_instance.force_refresh = AsyncMock(
            side_effect=FAOSTATAuthError("Login failed — invalid username or password.")
        )
        MockTM.return_value = mock_instance
        result = json.loads(await faostat_setup(
            username="bad@example.com", password="wrong"
        ))
    assert result["status"] == "error"
    assert "Authentication failed" in result["message"]


async def test_data_raw_cache_reused_across_formats_fields_and_limits():
    rows = [{"Area": "Kenya", "Year": 2023, "Unit": "t", "Value": 42},
            {"Area": "Kenya", "Year": 2024, "Unit": "t", "Value": 43}]
    with patch("faostat_mcp.server.faostat_get", return_value=rows) as get:
        default = json.loads(await faostat_get_data("QCL"))
        selected = json.loads(await faostat_get_data("QCL", fields="Year,Value", limit=1))
        objects = json.loads(await faostat_get_data("QCL", response_format="objects"))
        csv = await faostat_get_data("QCL", response_format="csv")
        assert csv == await faostat_get_data("QCL", response_format="csv")
    assert get.call_count == 1
    assert default["columns"] == ["Area", "Year", "Unit", "Value"]
    assert selected["columns"] == ["Year", "Value"]
    assert selected["rows"] == [[2023, 42]] and selected["_total_rows"] == 2
    assert objects == rows
    assert csv.startswith("Area,Year,Unit,Value\n")


async def test_code_table_shared_across_search_browse_and_validation():
    raw = {"metadata": {}, "data": [{"code": "114", "label": "Kenya"}]}
    with patch("faostat_mcp.server.faostat_get", return_value=raw) as get:
        match = json.loads(await faostat_search_codes("QCL", "area", "Kenya"))
        listed = json.loads(await faostat_get_codes("area", "QCL"))
        from faostat_mcp.server import _unknown_codes
        assert await _unknown_codes("QCL", "en", area="114") is None
    assert get.call_count == 1
    assert match["match"]["code"] == "114" and listed == raw


async def test_search_limits_and_blank_input():
    rows = [{"code": str(i), "label": f"Crop {i}"} for i in range(30)]
    with patch("faostat_mcp.server.faostat_get", return_value=rows) as get:
        blank = json.loads(await faostat_search_codes("QCL", "item", " "))
        get.assert_not_called()
        result = json.loads(await faostat_search_codes("QCL", "item", "Crop"))
    assert blank["error"] == "ValueError"
    assert len(result["matches"]) == 25 and result["_total_matches"] == 30
    assert result["_truncated"] and result["requires_confirmation"]


async def test_definition_compact_default_and_case_insensitive_suggestions():
    with patch("faostat_mcp.server.faostat_get", side_effect=_fake_get):
        definitions = json.loads(await faostat_get_definition_type("areagroup", limit=1))
        suggestion = json.loads(await faostat_resolve_name("region", "EASTRN AFRICA"))
        blank = json.loads(await faostat_resolve_name("region", " "))
    assert len(definitions["rows"]) == 1 and "data" not in definitions
    assert definitions["_truncated"]
    assert suggestion["status"] == "no_matching_definition"
    assert any(row["label"] == "Eastern Africa" for row in suggestion["suggestions"])
    assert blank["error"] == "ValueError"


async def test_rankings_cache_preserves_requested_format():
    rows = [{"Area": "Kenya", "Value": 42}]
    with patch("faostat_mcp.server.faostat_post", return_value=rows) as post:
        default = json.loads(await faostat_get_rankings("QCL", "5510", "15", "2023"))
        csv = await faostat_get_rankings("QCL", "5510", "15", "2023", response_format="csv")
    assert post.call_count == 1
    assert default["rows"] == [["Kenya", 42]]
    assert csv == "Area,Value\nKenya,42\n"
