"""
FAOSTAT MCP Server

Exposes the full FAOSTAT REST API as MCP tools, usable by Claude Desktop,
Claude Code, Cursor, and any other MCP-compatible AI client.

Run in development mode:
  mcp dev faostat_mcp/server.py

Run as a module (for Claude Desktop config):
  python -m faostat_mcp.server
"""

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

from .client import (
    faostat_get,
    faostat_post,
    DEFAULT_LANG,
    _get_token_manager,
    FAOSTATAuthError,
    FAOSTATRateLimitError,
    FAOSTATServerError,
)

load_dotenv()

# Initialise the FastMCP server
mcp = FastMCP(
    name="faostat",
    instructions=(
        "You have access to the FAOSTAT database — the UN Food and Agriculture Organization's "
        "statistical database covering agriculture, food security, trade, emissions, and more "
        "for ~245 countries. Use these tools to answer questions about global food and agriculture "
        "data. Always start by exploring available groups and domains if you are unsure which "
        "domain contains the data you need."
    ),
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@mcp.tool()
async def faostat_ping() -> str:
    """
    Check the FAOSTAT API health status.
    Returns a status message indicating if the API is online.
    """
    try:
        result = await faostat_get("/ping")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

@mcp.tool()
async def faostat_refresh_token() -> str:
    """
    Force-refresh the FAOSTAT API authentication token.

    Use this tool when other FAOSTAT tools fail with 401 Unauthorized or
    token-expiry errors. It logs in with the configured credentials
    (FAOSTAT_USERNAME + FAOSTAT_PASSWORD) and obtains a fresh JWT token.

    Requires FAOSTAT_USERNAME and FAOSTAT_PASSWORD to be set in the .env file.
    """
    tm = _get_token_manager()
    try:
        await tm.force_refresh()
        return json.dumps({"status": "ok", "message": "Token refreshed successfully."})
    except FAOSTATAuthError as exc:
        return json.dumps({"status": "error", "message": str(exc)})


# ---------------------------------------------------------------------------
# Discovery: groups, domains, structure
# ---------------------------------------------------------------------------

@mcp.tool()
async def faostat_list_groups(lang: str = DEFAULT_LANG) -> str:
    """
    List all top-level FAOSTAT data groups (e.g. Production, Trade, Food Security).
    Use this to discover what categories of data are available.

    Args:
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/groups/")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_groups_and_domains(lang: str = DEFAULT_LANG) -> str:
    """
    Get the full hierarchical tree of all FAOSTAT groups and their domains.
    Use this for a complete overview of all available datasets.

    Args:
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/groupsanddomains")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_list_domains(group_code: str, lang: str = DEFAULT_LANG) -> str:
    """
    List all datasets (domains) within a FAOSTAT group.

    Args:
        group_code: The group code (e.g. 'Q' for Production, 'T' for Trade,
                    'FS' for Food Security). Get codes from faostat_list_groups.
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/domains/{group_code}/")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_get_dimensions(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """
    Get the structure of a domain — what dimensions (filters) are available,
    such as area (country), item (commodity), element (measure), and year.

    Args:
        domain_code: Domain code (e.g. 'QCL' for Crops and Livestock,
                     'TM' for Trade, 'FS' for Food Security)
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/dimensions/{domain_code}/")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Codes (lookup tables for filter values)
# ---------------------------------------------------------------------------

@mcp.tool()
async def faostat_get_codes(
    dimension_id: str,
    domain_code: str,
    lang: str = DEFAULT_LANG,
) -> str:
    """
    Get the list of available FILTER codes for a specific dimension in a domain.
    You MUST call this before faostat_get_data to get the correct codes for filtering.

    IMPORTANT: For the 'element' dimension, filter codes differ from the display
    codes shown in data responses. For example in QCL, faostat_get_codes returns
    filter code '2510' for Production, but the data response shows '5510' in the
    Element Code column. Always use the codes from this tool when filtering.

    Args:
        dimension_id: Dimension identifier (e.g. 'area', 'item', 'element', 'year')
        domain_code: Domain code (e.g. 'QCL', 'TM', 'FS')
        lang: Language code (default: 'en')

    Examples:
        faostat_get_codes(dimension_id='element', domain_code='QCL')
        → Returns element filter codes: 2510=Production, 2312=Area harvested, etc.

        faostat_get_codes(dimension_id='area', domain_code='QCL')
        → Returns country/area codes: 2=Afghanistan, 3=Albania, etc.
    """
    try:
        result = await faostat_get(f"/{lang}/codes/{dimension_id}/{domain_code}")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Data retrieval
# ---------------------------------------------------------------------------

@mcp.tool()
async def faostat_get_data(
    domain_code: str,
    lang: str = DEFAULT_LANG,
    area: str | None = None,
    element: str | None = None,
    item: str | None = None,
    year: str | None = None,
    area_cs: str | None = None,
    element_cs: str | None = None,
    item_cs: str | None = None,
    year_cs: str | None = None,
    show_codes: bool = True,
    show_unit: bool = True,
    show_flags: bool = True,
    null_values: bool = False,
    limit: int = 500,
) -> str:
    """
    Fetch statistical data from a FAOSTAT domain.
    This is the primary tool for retrieving actual data values.

    IMPORTANT: For large domains, always filter by area/item/year to avoid
    very large responses. Check query size first with faostat_get_datasize.

    IMPORTANT: Element codes used for filtering differ from the display codes
    returned in the response. Always use faostat_get_codes(dimension_id='element',
    domain_code=...) to get the correct filter codes. For example, in QCL:
      - Filter with element='2510' → response shows Element Code '5510' (Production)
      - Filter with element='2312' → response shows Element Code '5312' (Area harvested)

    Args:
        domain_code: Domain code (e.g. 'QCL' for Crops and Livestock Products)
        lang: Language code (default: 'en')
        area: Country/area codes, comma-separated (e.g. '2' for Afghanistan).
              Use faostat_get_codes(dimension_id='area', domain_code=...) to find codes.
        element: Element FILTER codes, comma-separated (e.g. '2510' for Production,
                 '2312' for Area harvested in QCL). These differ from the display codes
                 in the response. Always look up via faostat_get_codes first.
        item: Item/commodity codes, comma-separated (e.g. '515' for Apples, '15' for Wheat)
        year: Year codes, comma-separated (e.g. '2020' or '2018,2019,2020')
        area_cs: Area code set name (alternative to individual area codes)
        element_cs: Element code set name
        item_cs: Item code set name
        year_cs: Year code set name (e.g. 'FAO_YEAR_RECENT' for recent years)
        show_codes: Include code columns in response (default: True)
        show_unit: Include unit column in response (default: True)
        show_flags: Include data quality flags (default: True)
        null_values: Include rows with null values (default: False)
        limit: Maximum number of rows to return (default: 500). Set to 0 for no limit.
               Use faostat_get_datasize first if you expect a large result set.

    Examples:
        # Apple production in Afghanistan 2024 (element 2510 = Production filter code)
        faostat_get_data('QCL', area='2', item='515', element='2510', year='2024')

        # Food security indicators for all African countries
        faostat_get_data('FS', area_cs='AFRICA')
    """
    try:
        params: dict[str, Any] = {
            "show_codes": show_codes,
            "show_unit": show_unit,
            "show_flags": show_flags,
            "null_values": null_values,
            "output_type": "objects",
        }
        for key, val in [
            ("area", area), ("element", element), ("item", item), ("year", year),
            ("area_cs", area_cs), ("element_cs", element_cs),
            ("item_cs", item_cs), ("year_cs", year_cs),
        ]:
            if val is not None:
                params[key] = val

        result = await faostat_get(f"/{lang}/data/{domain_code}/", params=params)

        # Apply row limit to prevent context window overflow
        if limit > 0:
            if isinstance(result, list):
                total = len(result)
                if total > limit:
                    return json.dumps({
                        "data": result[:limit],
                        "_truncated": True,
                        "_total_rows": total,
                        "_returned_rows": limit,
                        "_hint": f"Results truncated. Use faostat_get_datasize to check size, then filter further or increase limit.",
                    })
            elif isinstance(result, dict) and isinstance(result.get("data"), list):
                data = result["data"]
                total = len(data)
                if total > limit:
                    result = {
                        **result,
                        "data": data[:limit],
                        "_truncated": True,
                        "_total_rows": total,
                        "_returned_rows": limit,
                        "_hint": f"Results truncated. Use faostat_get_datasize to check size, then filter further or increase limit.",
                    }

        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_get_datasize(
    domain_code: str,
    lang: str = DEFAULT_LANG,
    area: str | None = None,
    element: str | None = None,
    item: str | None = None,
    year: str | None = None,
    area_cs: str | None = None,
    element_cs: str | None = None,
    item_cs: str | None = None,
    year_cs: str | None = None,
) -> str:
    """
    Estimate the number of rows a data query will return BEFORE fetching.
    Use this to check if a query is too large before calling faostat_get_data.
    Accepts the same filter parameters as faostat_get_data.

    Args:
        domain_code: Domain code (e.g. 'QCL', 'TM', 'FS')
        lang: Language code (default: 'en')
        area: Country/area codes, comma-separated
        element: Element filter codes, comma-separated
        item: Item/commodity codes, comma-separated
        year: Year codes, comma-separated
        area_cs: Area code set name
        element_cs: Element code set name
        item_cs: Item code set name
        year_cs: Year code set name
    """
    try:
        payload: dict[str, Any] = {"domain_code": domain_code}
        for key, val in [
            ("area", area), ("element", element), ("item", item), ("year", year),
            ("area_cs", area_cs), ("element_cs", element_cs),
            ("item_cs", item_cs), ("year_cs", year_cs),
        ]:
            if val is not None:
                payload[key] = val
        result = await faostat_post(f"/{lang}/datasize/", json=payload)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Definitions & Metadata
# ---------------------------------------------------------------------------

@mcp.tool()
async def faostat_get_definitions(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """
    Get all definitions (descriptions of items, elements, flags) for a domain.

    Args:
        domain_code: Domain code (e.g. 'QCL', 'FS', 'TM')
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/definitions/domain/{domain_code}")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_get_definitions_by_type(
    domain_code: str,
    definition_type: str,
    lang: str = DEFAULT_LANG,
) -> str:
    """
    Get definitions for a domain filtered by type (e.g. items, elements, flags).

    Args:
        domain_code: Domain code (e.g. 'QCL')
        definition_type: Type of definition. Use faostat_definition_types to see options.
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/definitions/domain/{domain_code}/{definition_type}")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_definition_types(lang: str = DEFAULT_LANG) -> str:
    """
    List all available definition types (used with faostat_get_definitions_by_type).

    Args:
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/definitions/types")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_get_metadata(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """
    Get full methodology and metadata for a domain — including data sources,
    collection methods, coverage, and limitations.

    Args:
        domain_code: Domain code (e.g. 'QCL', 'FS', 'GCE')
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/metadata/{domain_code}")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_get_metadata_print(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """
    Get metadata for a domain in a printable/simplified format.

    Args:
        domain_code: Domain code (e.g. 'QCL', 'FS')
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/metadata_print/{domain_code}")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Bulk downloads & Documents
# ---------------------------------------------------------------------------

@mcp.tool()
async def faostat_list_bulk_downloads(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """
    List available bulk download files for a domain (ZIP/CSV archives).
    These contain the full domain dataset and can be very large.

    Args:
        domain_code: Domain code (e.g. 'QCL', 'TM')
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/bulkdownloads/{domain_code}/")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_list_documents(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """
    List related documents (methodology papers, questionnaires) for a domain.

    Args:
        domain_code: Domain code (e.g. 'QCL', 'FS')
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_get(f"/{lang}/documents/{domain_code}/")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------

@mcp.tool()
async def faostat_get_rankings(
    domain_code: str,
    element_code: str,
    item_code: str,
    year: str,
    lang: str = DEFAULT_LANG,
    limit: int = 10,
) -> str:
    """
    Get rankings — e.g. top countries by production, yield, or trade value.
    Use this to answer "which country produces the most X?" questions.

    NOTE: element_code here is the DISPLAY code (e.g. '5510'), not the filter code
    used in faostat_get_data. Rankings use the same codes shown in data responses.

    Args:
        domain_code: Domain to rank within (e.g. 'QCL')
        element_code: Display element code to rank by (e.g. '5510' for Production in QCL)
        item_code: Commodity code (e.g. '56' for Maize, '15' for Wheat)
        year: The year to rank for (e.g. '2022')
        lang: Language code (default: 'en')
        limit: Number of top results to return (default: 10)

    Example:
        faostat_get_rankings(domain_code='QCL', element_code='5510',
                             item_code='56', year='2022', limit=10)
        → Top 10 maize-producing countries in 2022
    """
    try:
        payload: dict[str, Any] = {
            "domain_code": domain_code,
            "element_code": element_code,
            "item_code": item_code,
            "year": year,
            "limit": limit,
        }
        result = await faostat_post(f"/{lang}/rankings/", json=payload)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@mcp.tool()
async def faostat_get_report_data(payload: dict[str, Any], lang: str = DEFAULT_LANG) -> str:
    """
    Get structured report data from FAOSTAT.

    Args:
        payload: Report query parameters (structure depends on report type)
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_post(f"/{lang}/report/data/", json=payload)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@mcp.tool()
async def faostat_get_report_headers(payload: dict[str, Any], lang: str = DEFAULT_LANG) -> str:
    """
    Get the column headers/schema for a report before fetching its data.

    Args:
        payload: Report query parameters
        lang: Language code (default: 'en')
    """
    try:
        result = await faostat_post(f"/{lang}/report/headers/", json=payload)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server (stdio transport for Claude Desktop/Code)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
