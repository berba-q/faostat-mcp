"""
FAOSTAT MCP Server

Exposes the full FAOSTAT REST API as MCP tools, usable by Claude Desktop,
Claude Code, Cursor, and any other MCP-compatible AI client.

Run in development mode:
  mcp dev faostat_mcp/server.py

Run as a module (for Claude Desktop config):
  python -m faostat_mcp.server
"""

import csv
import difflib
import functools
import io
import json
import os
import logging
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

from . import __version__
from .client import (
    faostat_get,
    faostat_post,
    DEFAULT_LANG,
    BASE_URL,
    TokenManager,
    _get_token_manager,
    _get_redis_connector,
    _save_credentials_to_storage,
    _reset_token_manager,
    HybridCaching,
    FAOSTATAuthError,
    FAOSTATRateLimitError,
    FAOSTATServerError,
)

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

# ---------------------------------------------------------------------------
# Response formatting helpers
# ---------------------------------------------------------------------------

def _format_rows(
    rows: list[dict[str, Any]],
    response_format: str = "objects",
    fields: list[str] | None = None,
) -> str:
    """Convert a list of row-dicts to the requested output format.

    Args:
        rows: List of dicts (each dict is one data row).
        response_format: "objects" | "compact" | "csv"
        fields: If provided, only include these column names.

    Returns:
        A string: JSON for objects/compact, plain CSV text for csv.
    """
    if not rows:
        return json.dumps([])

    # Field filtering
    if fields:
        valid = [f for f in fields if f in rows[0]]
        if valid:
            rows = [{k: row.get(k) for k in valid} for row in rows]

    if response_format == "objects":
        return json.dumps(rows)

    # Derive column names from the first row
    columns = list(rows[0].keys())

    if response_format == "csv":
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(c, "") for c in columns])
        return buf.getvalue()

    # "compact" — columnar format
    return json.dumps({
        "columns": columns,
        "rows": [[row.get(c) for c in columns] for row in rows],
    })

# Initialise the FastMCP server
mcp = FastMCP(
    name="faostat",
    instructions=(
        "You have access to the FAOSTAT database — the UN Food and Agriculture Organization's "
        "statistical database covering agriculture, food security, trade, emissions, and more "
        "for ~245 countries. Use these tools to answer questions about global food and agriculture "
        "data. Always start by exploring available groups and domains if you are unsure which "
        "domain contains the data you need. "
        "When you need to find a code by name (e.g. 'production', 'wheat', 'Nigeria'), "
        "use faostat_search_codes — it tells you whether the match is unambiguous or whether "
        "you must ask the user to choose before proceeding. "
        "Only use regions, country groups and indicators that FAO defines. When the domain is known, "
        "use faostat_search_codes to resolve names; otherwise use faostat_resolve_name. If a lookup finds no "
        "matching definition, report that and offer the listed alternatives. Never "
        "invent, aggregate or approximate a region or indicator on your own; if the user "
        "explicitly asks for one, label it clearly as a custom, non-FAO construction. "
        "faostat_get_definition_type lists FAO's official definitions by type "
        "(e.g. 'areagroup' for regions) without needing a domain."
    ),
)

# FastMCP has no version argument (mcp 1.2–1.30) and otherwise reports the mcp
# library's own version to clients during initialize.
mcp._mcp_server.version = __version__

# ---------------------------------------------------------------------------
# Update notice — one PyPI check per process, logged to stderr
# ---------------------------------------------------------------------------

_PYPI_URL = "https://pypi.org/pypi/faostat-mcp/json"
_update: dict[str, Any] = {"checked": False, "notice": None}


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


async def _check_for_update() -> None:
    """Fetch the latest release from PyPI once. Never raises; uses a 2s HTTP timeout."""
    _update["checked"] = True
    if os.getenv("FAOSTAT_NO_UPDATE_CHECK"):
        return
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            latest = (await client.get(_PYPI_URL)).json()["info"]["version"]
        if _version_tuple(latest) > _version_tuple(__version__):
            _update["notice"] = (
                f"faostat-mcp {latest} is available (this server runs {__version__}). "
                "Update with `uvx faostat-mcp@latest` "
                "(or `pip install -U faostat-mcp`), then restart the AI client."
            )
            logger.warning(_update["notice"])
    except Exception as exc:
        logger.info("Update check skipped: %s", exc)


def _tool():
    """Register a tool and check for updates once; notices go to stderr."""
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            if not _update["checked"]:
                await _check_for_update()
            return await fn(*args, **kwargs)
        return mcp.tool()(wrapper)
    return decorator

# Initialise the HybridCaching Manager
try:
    caching_manager = HybridCaching(
        mem_cache_ttl=int(os.getenv("MEM_CACHE_TTL", "1200")),
        redis_cache_ttl=int(os.getenv("REDIS_CACHE_TTL", "1800")),
        max_mem_cache_size=int(os.getenv("MAX_MEM_CACHE_SIZE", "256")),
        user_token=str(os.getenv("FAOSTAT_API_TOKEN", "")),
        redis_conn=_get_redis_connector(),
    )
except ValueError:
    caching_manager = HybridCaching()

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@_tool()
async def faostat_ping() -> str:
    """Check the FAOSTAT API health status. Returns a status message indicating if the API is online."""
    try:
        result = await faostat_get("/ping")
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

@_tool()
async def faostat_refresh_token() -> str:
    """Force-refresh the FAOSTAT API authentication token."""
    tm = _get_token_manager()
    try:
        await tm.force_refresh()
        return json.dumps({"status": "ok", "message": "Token refreshed successfully."})
    except FAOSTATAuthError as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@_tool()
async def faostat_setup(username: str, password: str) -> str:
    """Validate and persist FAOSTAT credentials for automatic authentication across sessions. username is the account email. Saves to the system keychain when available and ~/.config/faostat-mcp/credentials.json (mode 600)."""
    try:
        # Validate credentials by attempting a real login before storing anything
        tm = TokenManager(
            base_url=os.getenv("FAOSTAT_BASE_URL", BASE_URL).rstrip("/"),
            username=username,
            password=password,
        )
        await tm.force_refresh()  # raises FAOSTATAuthError on bad credentials

        # Persist credentials to keychain / config file
        stored_at = _save_credentials_to_storage(username, password)

        # Reload the singleton so subsequent tool calls use the new credentials
        _reset_token_manager()

        return json.dumps({
            "status": "ok",
            "message": (
                f"Credentials validated and saved to {stored_at}. "
                "All FAOSTAT tools are now ready to use."
            ),
        })
    except FAOSTATAuthError as exc:
        return json.dumps({
            "status": "error",
            "message": f"Authentication failed: {exc} — check your username and password.",
        })


# ---------------------------------------------------------------------------
# Discovery: groups, domains, structure
# ---------------------------------------------------------------------------

@_tool()
async def faostat_list_groups(lang: str = DEFAULT_LANG) -> str:
    """List all top-level FAOSTAT data groups (e.g. Production, Trade, Food Security). Use this to discover what categories of data are available."""
    try:
        # Check memory Cache
        arg_dict = {'lang':lang}
        cached_val = caching_manager.get_data("faostat_list_groups", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/groups/")
        # Save to memory Cache
        caching_manager.set_data("faostat_list_groups", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_groups_and_domains(lang: str = DEFAULT_LANG) -> str:
    """Get the full hierarchical tree of all FAOSTAT groups and their domains. Use this for a complete overview of all available datasets."""
    try:
        arg_dict = {'lang':lang}
        cached_val = caching_manager.get_data("faostat_groups_and_domains", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/groupsanddomains")
        caching_manager.set_data("faostat_groups_and_domains", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_list_domains(group_code: str, lang: str = DEFAULT_LANG) -> str:
    """List all datasets (domains) within a FAOSTAT group."""
    try:
        arg_dict = {'group_code':group_code,'lang':lang}
        cached_val = caching_manager.get_data("faostat_list_domains", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/domains/{group_code}/")
        caching_manager.set_data("faostat_list_domains", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_get_dimensions(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """Get the structure of a domain — what dimensions (filters) are available, such as area (country), item (commodity), element (measure), and year."""
    try:
        arg_dict = {'domain_code': domain_code, 'lang': lang}
        cached_val = caching_manager.get_data("faostat_get_dimensions", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/dimensions/{domain_code}/")
        caching_manager.set_data("faostat_get_dimensions", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Codes (lookup tables for filter values)
# ---------------------------------------------------------------------------

@_tool()
async def faostat_get_codes(
    dimension_id: str,
    domain_code: str,
    lang: str = DEFAULT_LANG,
    limit: int = 0,
) -> str:
    """Browse domain FILTER codes. Prefer faostat_search_codes for named lookups. Element filter codes differ from display codes (QCL: Production filter 2510, display 5510). limit=0 returns all codes."""
    try:
        arg_dict = {
            'dimension_id': dimension_id,
            'domain_code': domain_code,
            'lang': lang,
            'limit': limit,
            }
        cached_val = caching_manager.get_data("faostat_get_codes", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await _code_table(dimension_id, domain_code, lang)
        codes_list = result.get("data", result) if isinstance(result, dict) else result
        if limit > 0 and isinstance(codes_list, list) and len(codes_list) > limit:
            truncated = {
                "data": codes_list[:limit],
                "_truncated": True,
                "_total_codes": len(codes_list),
                "_returned_codes": limit,
            }
            caching_manager.set_data("faostat_get_codes", arg_dict, truncated)
            return json.dumps(truncated)
        caching_manager.set_data("faostat_get_codes", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Code search / disambiguation
# ---------------------------------------------------------------------------

@_tool()
async def faostat_search_codes(
    domain_code: str,
    dimension_id: str,
    query: str,
    lang: str = DEFAULT_LANG,
    limit: int = 25,
) -> str:
    """Find domain FILTER codes by name (case-insensitive substring). Use before querying unknown codes. A unique match is usable; multiple matches require user selection. Default limit=25; narrow the query when truncated. query must be nonblank."""
    try:
        if not query.strip() or limit < 1:
            return json.dumps({"error": "ValueError", "message": "Provide a nonblank query and a positive limit."})
        arg_dict = {
            'domain_code': domain_code,
            'dimension_id': dimension_id,
            'query': query,
            'limit': limit,
            'lang': lang,
        }
        cached_val = caching_manager.get_data("faostat_search_codes", arg_dict)
        if cached_val:
            return json.dumps(cached_val)

        raw = await _code_table(dimension_id, domain_code, lang)

        if isinstance(raw, dict):
            codes_list = raw.get("data", [])
        elif isinstance(raw, list):
            codes_list = raw
        else:
            codes_list = []

        query_lower = query.strip().lower()

        def _matches(entry: dict) -> bool:
            return any(
                query_lower in str(v).lower()
                for v in entry.values()
                if isinstance(v, str)
            )

        def _extract(entry: dict) -> dict:
            code_val = (
                entry.get("code") or entry.get("Code")
                or entry.get("id") or ""
            )
            label_val = next(
                (v for v in entry.values()
                 if isinstance(v, str) and v != str(code_val) and v.strip()),
                "",
            )
            return {"code": str(code_val), "label": label_val}

        hits = [_extract(e) for e in codes_list if _matches(e)]

        if len(hits) == 1:
            result = {
                "match": hits[0],
                "requires_confirmation": False,
                "message": (
                    f"Unique match found. Use code '{hits[0]['code']}' "
                    f"as the '{dimension_id}' filter in faostat_get_data."
                ),
            }
        elif hits:
            result = {
                "matches": hits[:limit],
                "_total_matches": len(hits),
                "_truncated": len(hits) > limit,
                "requires_confirmation": True,
                "message": (
                    f"Multiple matches found for '{query}' in "
                    f"{dimension_id}/{domain_code}. "
                    "Narrow the query if truncated; otherwise you MUST ask the user to choose."
                ),
            }
        else:
            result = {
                "matches": [],
                "requires_confirmation": False,
                "message": (
                    f"No codes match '{query}' in {dimension_id}/{domain_code}. "
                    "Try a different search term, or use faostat_get_codes to "
                    "browse all available codes for this dimension."
                ),
            }

        caching_manager.set_data("faostat_search_codes", arg_dict, result)
        return json.dumps(result)

    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# FAO-defined names only — shared lookups for resolution and code validation
# ---------------------------------------------------------------------------

async def _definition_rows(definition_type: str, lang: str) -> list[dict[str, Any]]:
    """Rows of /definitions/types/{type}, cached. Caller must pass a valid type."""
    arg_dict = {'definition_type': definition_type, 'lang': lang}
    raw = caching_manager.get_data("faostat_get_definition_type", arg_dict)
    if not raw:
        raw = await faostat_get(f"/{lang}/definitions/types/{definition_type}")
        caching_manager.set_data("faostat_get_definition_type", arg_dict, raw)
    return raw.get("data", []) if isinstance(raw, dict) else raw


async def _code_table(dimension_id: str, domain_code: str, lang: str) -> dict[str, Any] | list[dict[str, Any]]:
    """Cache the complete domain filter-code table for browsing/search/validation."""
    args = {'dimension_id': dimension_id, 'domain_code': domain_code, 'lang': lang}
    rows = caching_manager.get_data("_code_table", args)
    if rows is None:
        rows = await faostat_get(f"/{lang}/codes/{dimension_id}/{domain_code}")
        caching_manager.set_data("_code_table", args, rows)
    return rows


async def _domain_codes(dimension_id: str, domain_code: str, lang: str) -> set[str]:
    raw = await _code_table(dimension_id, domain_code, lang)
    rows = raw.get("data", []) if isinstance(raw, dict) else raw
    return {str(r["code"]) for r in rows if isinstance(r, dict) and "code" in r}


async def _unknown_codes(domain_code: str, lang: str, **dims: str | None) -> str | None:
    """Return an UnknownCode error (JSON) if any code is not defined for the domain.

    Fails open: if a code list can't be fetched, that dimension is not checked,
    so a lookup outage never blocks an otherwise valid query.
    """
    unknown: dict[str, list[str]] = {}
    for dim, value in dims.items():
        if not value:
            continue
        try:
            valid = await _domain_codes(dim, domain_code, lang)
        except Exception as exc:
            logger.warning("Skipping %s code validation for %s: %s", dim, domain_code, exc)
            continue
        if not valid:
            continue
        bad = [c.strip() for c in value.split(",") if c.strip() and c.strip() not in valid]
        if bad:
            unknown[dim] = bad
    if not unknown:
        return None
    return json.dumps({
        "error": "UnknownCode",
        "unknown": unknown,
        "message": (
            f"These codes are not defined by FAOSTAT for domain {domain_code}: {unknown}. "
            "Do NOT substitute, invent, or construct a replacement. Look the name up with "
            "faostat_search_codes (or faostat_resolve_name), and tell the user if FAO does "
            "not define it. Element filters must come from faostat_get_codes."
        ),
    })


# ---------------------------------------------------------------------------
# Data retrieval
# ---------------------------------------------------------------------------

@_tool()
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
    show_codes: bool = False,
    show_unit: bool = True,
    show_flags: bool = False,
    null_values: bool = False,
    limit: int = 50,
    response_format: str = "compact",
    fields: str | None = None,
) -> str:
    """Fetch domain data. Resolve filter codes with faostat_search_codes first. Element FILTER codes differ from display codes (QCL Production: filter 2510, display 5510). Filter large queries by area/item/year; check datasize when unsure. Prefer response_format="compact" and fields to save tokens; objects and csv are supported. limit=50; 0 returns all rows. Preserve units when selecting fields."""
    try:
        # Validate response_format
        if limit < 0 or response_format not in ("objects", "compact", "csv"):
            return json.dumps({
                "error": "ValueError",
                "message": "Use a nonnegative limit and response_format 'objects', 'compact', or 'csv'.",
            })

        arg_dict = {
            'domain_code': domain_code,
            'lang': lang,
            'area': area,
            'element': element,
            'item': item,
            'year': year,
            'area_cs': area_cs,
            'element_cs': element_cs,
            'item_cs': item_cs,
            'year_cs': year_cs,
            'show_codes': show_codes,
            'show_unit': show_unit,
            'show_flags': show_flags,
            'null_values': null_values,
        }
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

        result = caching_manager.get_data("faostat_get_data_raw", arg_dict)
        if result is None:
            error = await _unknown_codes(domain_code, lang, area=area, element=element, item=item)
            if error:
                return error
            result = await faostat_get(f"/{lang}/data/{domain_code}/", params=params)
            caching_manager.set_data("faostat_get_data_raw", arg_dict, result)

        # Extract data rows and optional envelope
        truncated_meta: dict[str, Any] | None = None

        if isinstance(result, list):
            data_rows = result
        elif isinstance(result, dict) and isinstance(result.get("data"), list):
            data_rows = result["data"]
        else:
            # Not tabular data — return as-is
            return json.dumps(result)

        # Apply row limit to prevent context window overflow
        if limit > 0:
            total = len(data_rows)
            if total > limit:
                data_rows = data_rows[:limit]
                truncated_meta = {
                    "_truncated": True,
                    "_total_rows": total,
                    "_returned_rows": limit,
                    "_hint": "Results truncated. Use faostat_get_datasize to check size, then filter further or increase limit.",
                }

        # Parse fields parameter
        parsed_fields = [f.strip() for f in fields.split(",")] if fields else None

        # Format the data rows
        formatted = _format_rows(data_rows, response_format=response_format, fields=parsed_fields)

        # CSV returns plain text
        if response_format == "csv":
            if truncated_meta:
                meta = f"# truncated: {truncated_meta['_total_rows']} total rows, {truncated_meta['_returned_rows']} returned\n"
                result = meta + formatted
            else:
                result = formatted
            return result

        # For objects/compact, attach truncation metadata if needed
        parsed = json.loads(formatted)
        if truncated_meta:
            if response_format == "compact":
                result = json.dumps({**parsed, **truncated_meta})
            else:
                result = json.dumps({"data": parsed, **truncated_meta})
        else:
            result = formatted

        return result
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
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
    """Estimate the number of rows a data query will return BEFORE fetching. Use this to check if a query is too large before calling faostat_get_data. Accepts the same filter parameters as faostat_get_data."""
    try:
        arg_dict = {
            'domain_code': domain_code, 
            'lang': lang,
            'area': area,
            'element': element,
            'item': item,
            'year': year,
            'area_cs': area_cs,
            'element_cs': element_cs,
            'item_cs': item_cs,
            'year_cs': year_cs,
            }

        cached_val = caching_manager.get_data("faostat_get_datasize", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        error = await _unknown_codes(domain_code, lang, area=area, element=element, item=item)
        if error:
            return error
        payload: dict[str, Any] = {"domain_code": domain_code}
        for key, val in [
            ("area", area), ("element", element), ("item", item), ("year", year),
            ("area_cs", area_cs), ("element_cs", element_cs),
            ("item_cs", item_cs), ("year_cs", year_cs),
        ]:
            if val is not None:
                payload[key] = val
        result = await faostat_post(f"/{lang}/datasize/", json=payload)
        caching_manager.set_data("faostat_get_datasize", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Definitions & Metadata
# ---------------------------------------------------------------------------

@_tool()
async def faostat_get_definitions(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """Get all definitions (descriptions of items, elements, flags) for a domain."""
    try:
        arg_dict = {
            'domain_code': domain_code, 
            'lang': lang,
            }
        cached_val = caching_manager.get_data("faostat_get_definitions", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/definitions/domain/{domain_code}")
        caching_manager.set_data("faostat_get_definitions", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_get_definitions_by_type(
    domain_code: str,
    definition_type: str,
    lang: str = DEFAULT_LANG,
) -> str:
    """Get definitions for a domain filtered by type (e.g. items, elements, flags)."""
    try:
        arg_dict = {
            'domain_code': domain_code, 
            'definition_type': definition_type, 
            'lang': lang,
            }
        cached_val = caching_manager.get_data("faostat_get_definitions_by_type", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/definitions/domain/{domain_code}/{definition_type}")
        caching_manager.set_data("faostat_get_definitions_by_type", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_definition_types(lang: str = DEFAULT_LANG) -> str:
    """List all available definition types (used with faostat_get_definition_type for FAO-wide definitions, or faostat_get_definitions_by_type within one domain)."""
    try:
        arg_dict = {'lang': lang}
        cached_val = caching_manager.get_data("faostat_definition_types", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/definitions/types")
        caching_manager.set_data("faostat_definition_types", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_get_definition_type(
    definition_type: str,
    lang: str = DEFAULT_LANG,
    search: str | None = None,
    limit: int = 100,
    response_format: str = "compact",
) -> str:
    """Get FAOSTAT-wide definitions without a domain. Types include areagroup, area, indicator, element, item, unit, flag; use faostat_definition_types for all types. search filters text columns; limit=100, 0 returns all. response_format: compact (default) or objects. Definition codes are not necessarily query filter codes."""
    try:
        if limit < 0 or response_format not in ("objects", "compact"):
            return json.dumps({"error": "ValueError", "message": "Use a nonnegative limit and objects or compact format."})
        arg_dict = {'lang': lang}
        types = caching_manager.get_data("faostat_definition_types", arg_dict)
        if not types:
            types = await faostat_get(f"/{lang}/definitions/types")
            caching_manager.set_data("faostat_definition_types", arg_dict, types)
        valid_types = [t["code"] for t in types.get("data", [])]
        if definition_type not in valid_types:
            # The API answers an unknown type with an empty HTTP 500, so check first.
            return json.dumps({
                "error": "UnknownDefinitionType",
                "message": f"'{definition_type}' is not a FAOSTAT definition type.",
                "valid_types": valid_types,
            })

        rows = await _definition_rows(definition_type, lang)
        if search:
            needle = search.strip().lower()
            rows = [
                r for r in rows
                if any(needle in v.lower() for v in r.values() if isinstance(v, str))
            ]
        returned = rows[:limit] if limit > 0 else rows
        return json.dumps({
            "definition_type": definition_type,
            "columns": list(rows[0].keys()) if rows else [],
            **({"rows": [[row.get(column) for column in rows[0]] for row in returned] if rows else []}
               if response_format == "compact" else {"data": returned}),
            "_total_rows": len(rows),
            "_returned_rows": len(returned),
            "_truncated": len(returned) < len(rows),
        })
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# kind -> [(definition_type, code column, label column)]
_RESOLVE_SOURCES = {
    "region": [("areagroup", "Country Group Code", "Country Group")],
    "country": [("area", "Country Code", "Country")],
    "indicator": [
        ("indicator", "Indicator Code", "Indicator"),
        ("element", "Element Code", "Element"),
        ("item", "Item Code", "Item"),
    ],
}
_MAX_MATCHES = 25


@_tool()
async def faostat_resolve_name(kind: str, name: str, lang: str = DEFAULT_LANG) -> str:
    """Find FAOSTAT definitions by name when the domain is unknown. kind: region, country, indicator (includes elements/items). definition_code identifies a definition, NOT a domain query filter; use faostat_search_codes for query codes. Exact matches are defined; partial matches require confirmation. no_matching_definition means this lookup found no match, not proof FAO never defines the concept. Narrow truncated matches. Custom constructions require explicit user request and a non-FAO label."""
    try:
        if not name.strip():
            return json.dumps({"error": "ValueError", "message": "Provide a nonblank name."})
        sources = _RESOLVE_SOURCES.get(kind)
        if not sources:
            return json.dumps({
                "error": "ValueError",
                "message": f"Invalid kind '{kind}'. Use one of: {sorted(_RESOLVE_SOURCES)}.",
            })

        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        for def_type, code_col, label_col in sources:
            for row in await _definition_rows(def_type, lang):
                code, label = str(row.get(code_col, "")), str(row.get(label_col, ""))
                if not (code and label):
                    continue
                cand = candidates.setdefault((def_type, code), {"type": def_type, "definition_code": code, "label": label})
                # Same label can mean different codes per domain (e.g. 6 'Production' elements)
                if row.get("Domain Code"):
                    cand.setdefault("domains", [])
                    if row["Domain Code"] not in cand["domains"]:
                        cand["domains"].append(row["Domain Code"])

        needle = name.strip().lower()
        exact = [c for c in candidates.values() if c["label"].lower() == needle]
        partial = [c for c in candidates.values() if needle in c["label"].lower()]

        if len(exact) == 1:
            return json.dumps({
                "status": "defined",
                "requires_confirmation": False,
                "match": exact[0],
                "message": f"'{exact[0]['label']}' is an FAO-defined {kind} (code {exact[0]['definition_code']}).",
            })
        hits = exact or partial
        if hits:
            return json.dumps({
                "status": "ambiguous",
                "requires_confirmation": True,
                "matches": hits[:_MAX_MATCHES],
                "_total_matches": len(hits),
                "_truncated": len(hits) > _MAX_MATCHES,
                "message": (
                    f"'{name}' matches several FAO-defined names. Present these to the "
                    "user and ask them to choose before querying data."
                ),
            })

        labels = sorted({c["label"] for c in candidates.values()})
        close = difflib.get_close_matches(needle, [label.lower() for label in labels], n=5, cutoff=0.6)
        return json.dumps({
            "status": "no_matching_definition",
            "requires_confirmation": True,
            "suggestions": [c for c in candidates.values() if c["label"].lower() in close][:_MAX_MATCHES],
            "message": (
                f"No matching FAOSTAT definition found for '{name}' as a {kind}. Tell the user this before "
                "going further. Do not create or approximate it unless the user explicitly "
                "asks, and then label it as a custom, non-FAO grouping or measure."
                + (" If the name combines a commodity and a measure (e.g. 'wheat "
                   "production'), resolve each part separately first."
                   if kind == "indicator" else "")
            ),
        })
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_get_metadata(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """Get full methodology and metadata for a domain — including data sources, collection methods, coverage, and limitations."""
    try:
        arg_dict = {
            'domain_code': domain_code,
            'lang': lang,
            }
        cached_val = caching_manager.get_data("faostat_get_metadata", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/metadata/{domain_code}")
        caching_manager.set_data("faostat_get_metadata", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_get_metadata_print(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """Get metadata for a domain in a printable/simplified format."""
    try:
        arg_dict = {
            'domain_code': domain_code,
            'lang': lang,
            }
        cached_val = caching_manager.get_data("faostat_get_metadata_print", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/metadata_print/{domain_code}")
        caching_manager.set_data("faostat_get_metadata_print", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Bulk downloads & Documents
# ---------------------------------------------------------------------------

@_tool()
async def faostat_list_bulk_downloads(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """List available bulk download files for a domain (ZIP/CSV archives). These contain the full domain dataset and can be very large."""
    try:
        arg_dict = {
            'domain_code': domain_code,
            'lang': lang,
            }
        cached_val = caching_manager.get_data("faostat_list_bulk_downloads", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/bulkdownloads/{domain_code}/")
        caching_manager.set_data("faostat_list_bulk_downloads", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_list_documents(domain_code: str, lang: str = DEFAULT_LANG) -> str:
    """List related documents (methodology papers, questionnaires) for a domain."""
    try:
        arg_dict = {
            'domain_code': domain_code,
            'lang': lang,
            }
        cached_val = caching_manager.get_data("faostat_list_documents", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_get(f"/{lang}/documents/{domain_code}/")
        caching_manager.set_data("faostat_list_documents", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------

@_tool()
async def faostat_get_rankings(
    domain_code: str,
    element_code: str,
    item_code: str,
    year: str,
    lang: str = DEFAULT_LANG,
    limit: int = 10,
    response_format: str = "compact",
) -> str:
    """Rank countries by a domain item/element/year. element_code is a DISPLAY code (QCL Production: 5510), unlike get_data filter code 2510. limit=10. response_format defaults to compact; objects and csv are supported."""
    try:
        if response_format not in ("objects", "compact", "csv"):
            return json.dumps({
                "error": "ValueError",
                "message": f"Invalid response_format '{response_format}'. Use 'objects', 'compact', or 'csv'.",
            })

        arg_dict = {
            'domain_code': domain_code,
            'element_code': element_code,
            'item_code': item_code,
            'year': year,
            'lang': lang,
            'limit': limit,
        }
        cached_val = caching_manager.get_data("faostat_get_rankings", arg_dict)
        if cached_val is not None:
            return _format_rows(cached_val, response_format) if isinstance(cached_val, list) else json.dumps(cached_val)

        payload: dict[str, Any] = {
            "domain_code": domain_code,
            "element_code": element_code,
            "item_code": item_code,
            "year": year,
            "limit": limit,
        }
        result = await faostat_post(f"/{lang}/rankings/", json=payload)
        caching_manager.set_data("faostat_get_rankings", arg_dict, result)

        if isinstance(result, list) and result:
            return _format_rows(result, response_format=response_format)

        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@_tool()
async def faostat_get_report_data(payload: dict[str, Any], lang: str = DEFAULT_LANG) -> str:
    """Get structured report data from FAOSTAT."""
    try:
        arg_dict = {'lang': lang}
        arg_dict.update(payload)
        cached_val = caching_manager.get_data("faostat_get_report_data", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_post(f"/{lang}/report/data/", json=payload)
        caching_manager.set_data("faostat_get_report_data", arg_dict, result)
        return json.dumps(result)
    except (FAOSTATAuthError, FAOSTATRateLimitError, FAOSTATServerError) as exc:
        return json.dumps({"error": type(exc).__name__, "message": str(exc)})


@_tool()
async def faostat_get_report_headers(payload: dict[str, Any], lang: str = DEFAULT_LANG) -> str:
    """Get the column headers/schema for a report before fetching its data."""
    try:
        arg_dict = {'lang': lang}
        arg_dict.update(payload)
        cached_val = caching_manager.get_data("faostat_get_report_headers", arg_dict)
        if cached_val:
            return json.dumps(cached_val)
        result = await faostat_post(f"/{lang}/report/headers/", json=payload)
        caching_manager.set_data("faostat_get_report_headers", arg_dict, result)
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
