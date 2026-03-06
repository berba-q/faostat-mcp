"""
Integration tests for the FAOSTAT MCP server against the production API.

Unit tests (no network) verify configuration is prod-ready.
Async integration tests call the live prod API to confirm endpoints respond correctly.

Run with:
    pytest tests/test_prod_api.py -v
"""

import pytest
from faostat_mcp.client import BASE_URL, _DEFAULT_PARAMS, faostat_get


# ---------------------------------------------------------------------------
# Unit tests — no network required
# ---------------------------------------------------------------------------

def test_no_dev_aws_param():
    """_DEFAULT_PARAMS must not contain datasource=DEV_AWS (prod does not need it)."""
    assert "datasource" not in _DEFAULT_PARAMS, (
        f"DEV_AWS datasource param found in _DEFAULT_PARAMS: {_DEFAULT_PARAMS}"
    )


def test_prod_base_url():
    """Default BASE_URL must point to the production API host."""
    assert "faostatservices.fao.org" in BASE_URL, (
        f"BASE_URL does not point to prod: {BASE_URL}"
    )


# ---------------------------------------------------------------------------
# Integration tests — require a valid FAOSTAT_API_TOKEN in .env
# ---------------------------------------------------------------------------

async def test_ping():
    """Production /ping endpoint should return a non-empty response."""
    result = await faostat_get("/ping")
    assert result is not None


async def test_list_groups():
    """Production /en/groups/ should return a list or dict of data groups."""
    result = await faostat_get("/en/groups/")
    assert isinstance(result, (list, dict))
    # Should have at least one group
    if isinstance(result, list):
        assert len(result) > 0
    else:
        assert len(result.get("data", result)) > 0


async def test_groups_and_domains():
    """Production /en/groupsanddomains should return the full domain tree."""
    result = await faostat_get("/en/groupsanddomains")
    assert result is not None
    assert isinstance(result, (list, dict))


async def test_get_dimensions_qcl():
    """Production /en/dimensions/QCL/ should return dimension info for the Crops domain."""
    result = await faostat_get("/en/dimensions/QCL/")
    assert result is not None


async def test_get_codes_area_qcl():
    """Production /en/codes/area/QCL should return area filter codes."""
    result = await faostat_get("/en/codes/area/QCL")
    assert result is not None
    # Response should contain data (list or dict with data key)
    data = result if isinstance(result, list) else result.get("data", [])
    assert len(data) > 0
