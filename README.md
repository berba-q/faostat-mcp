# FAOSTAT MCP Server

> Query UN food and agriculture statistics with AI — powered by the [Model Context Protocol](https://modelcontextprotocol.io)

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-compatible-green.svg)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An MCP (Model Context Protocol) server that exposes the full [FAOSTAT API](https://faostat.fao.org/dev-internal/en/#developer-portal) as tools for AI assistants. Connect any MCP-compatible client — Claude, Cursor, Windsurf, Zed, or your own agent — to the world's most comprehensive database of food, agriculture, fisheries, forestry, and nutrition statistics, covering 245 countries and territories from the UN's Food and Agriculture Organization (FAO).

**Keywords:** FAOSTAT, MCP server, Model Context Protocol, AI agriculture data, FAO statistics, food security AI, agricultural data Python, UN food data, crop production statistics, Claude, Cursor, Windsurf

---

## Why Use This?

Researchers, data journalists, policy analysts, and developers can ask natural-language questions and get answers directly from FAOSTAT — without writing a single API call. Your AI assistant handles domain discovery, filtering, and interpretation automatically.

**Who is this for?**
- Agricultural economists and food security researchers
- Journalists and policy analysts working with FAO data
- Developers building AI pipelines on top of FAOSTAT
- Anyone who wants to explore crop, trade, nutrition, or emissions data conversationally

---

## What is FAOSTAT?

[FAOSTAT](https://www.fao.org/faostat/en/) is the statistical database of the United Nations Food and Agriculture Organization (FAO). It is the world's most comprehensive freely available source of data on food and agriculture, covering:

- **Crop and livestock production** — yields, harvested area, and quantities for hundreds of commodities
- **Trade** — import/export volumes and values between countries
- **Food security** — prevalence of undernourishment, dietary energy supply, and access indicators
- **Emissions** — greenhouse gas emissions from agriculture, land use, and food systems
- **Forestry and fisheries** — production and trade data
- **Prices, inputs, and population** — producer prices, fertilizer use, and demographic context

Data spans from 1961 to the present, across 245 countries and territories, in multiple languages.

## What is MCP?

The [Model Context Protocol](https://modelcontextprotocol.io) is an open standard that lets AI assistants call external tools at runtime. This server registers all FAOSTAT API endpoints as discoverable tools — your AI assistant automatically selects and chains the right calls when you ask a question.

---

## Features

- **18 MCP tools** covering every FAOSTAT endpoint (data, metadata, rankings, bulk downloads, reports)
- **245 countries and territories** across dozens of domains: crops, livestock, trade, food security, emissions, forestry, fisheries, and more
- Built-in **rate limiting** (2 req/s) — safe for the FAOSTAT dev API out of the box
- **Auto-retry** with exponential backoff on transient network errors
- Rich tool descriptions so the AI knows exactly when and how to call each tool
- Works with **Claude Desktop, Claude Code, Cursor, Windsurf, Zed**, and any MCP-compatible client

---

## Quick Start

### Prerequisites

- Python 3.10+
- A [FAOSTAT API token](https://faostat.fao.org/dev-internal/en/#developer-portal)
- Any MCP-compatible client (Claude Desktop, Cursor, Windsurf, Zed, or a custom agent)

### Install

```bash
git clone https://github.com/your-username/faostat-mcp.git
cd faostat-mcp
pip install -e .
```

### Configure

```bash
cp .env.example .env
# Edit .env and add your FAOSTAT API token:
# FAOSTAT_API_TOKEN=your_token_here
```

---

## Running the Server

### Development mode (interactive MCP Inspector UI)

```bash
mcp dev faostat_mcp/server.py
```

Opens a browser UI at `http://localhost:5173` where you can browse and test all 18 tools interactively.

### Production mode (stdio transport, for Claude Desktop)

```bash
python -m faostat_mcp.server
# or, using the installed script:
faostat-mcp
```

---

## MCP Client Integration

The server speaks standard MCP over stdio, so it works with any compatible client. The JSON config block below is the same across clients — only the config file path differs.

```json
{
  "mcpServers": {
    "faostat": {
      "command": "python",
      "args": ["-m", "faostat_mcp.server"],
      "cwd": "/path/to/faostat-mcp",
      "env": {
        "FAOSTAT_API_TOKEN": "your_token_here"
      }
    }
  }
}
```

### Claude Desktop

Add the block above to:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Restart Claude Desktop — **faostat** will appear in the tools panel.

### Cursor

Add the block to `.cursor/mcp.json` in your project root, or to your global Cursor MCP settings. See the [Cursor MCP docs](https://docs.cursor.com/context/model-context-protocol) for details.

### Windsurf / Zed / other clients

Any client that supports MCP stdio servers accepts the same config shape. Consult your client's documentation for the config file location.

---

## Example Queries

Once connected, ask your AI assistant questions like:

| Domain | Example Question |
|--------|-----------------|
| Crop production | *"What were the top 10 wheat-producing countries in 2022?"* |
| Food security | *"Show me food security indicators for Ethiopia from 2015 to 2020"* |
| Trade | *"Which countries are most dependent on food imports?"* |
| Yield comparison | *"Compare maize yields between the USA and Brazil over the last decade"* |
| Emissions | *"What are greenhouse gas emissions from agriculture in Sub-Saharan Africa?"* |
| Discovery | *"What agricultural datasets does FAOSTAT have for trade?"* |

Your AI assistant will automatically:
1. Call `faostat_list_groups` or `faostat_groups_and_domains` to find the right domain
2. Call `faostat_get_codes` to resolve the correct country, item, and element filter codes
3. Call `faostat_get_data` or `faostat_get_rankings` with the right parameters
4. Interpret and summarize the results in plain language

---

## Available MCP Tools

| Tool | Description |
|------|-------------|
| `faostat_ping` | Check API health |
| `faostat_list_groups` | List all data groups |
| `faostat_groups_and_domains` | Full domain tree |
| `faostat_list_domains` | Domains within a group |
| `faostat_get_dimensions` | Available filters for a domain |
| `faostat_get_codes` | Country/item/element filter codes |
| `faostat_get_data` | **Fetch actual statistics** |
| `faostat_get_datasize` | Estimate query result size |
| `faostat_get_definitions` | Domain definitions |
| `faostat_get_definitions_by_type` | Definitions by type |
| `faostat_definition_types` | All definition types |
| `faostat_get_metadata` | Full domain metadata |
| `faostat_get_metadata_print` | Printable metadata |
| `faostat_list_bulk_downloads` | Bulk download file listing |
| `faostat_list_documents` | Related documents |
| `faostat_get_rankings` | Top-N country rankings |
| `faostat_get_report_data` | Report data |
| `faostat_get_report_headers` | Report column headers |

---

## Project Structure

```
faostat-mcp/
├── pyproject.toml
├── .env.example
├── mcp_config_example.json   ← Claude Desktop config snippet
└── faostat_mcp/
    ├── server.py             ← FastMCP server + all tool definitions
    └── client.py             ← Rate-limited HTTP client with auto-retry
```

---

## Important: Filter Codes vs Display Codes

The FAOSTAT API uses **two different code systems**: *filter codes* (used in query parameters) and *display codes* (shown in response data and bulk CSVs). Always use filter codes from `faostat_get_codes` when calling `faostat_get_data`.

**Area, item, and year codes are the same for both.** Only **element** codes differ:

### QCL — Crops and Livestock Products

| Filter Code | Display Code | Element |
|---|---|---|
| `2312` | `5312` | Area harvested |
| `2413` | `5412` | Yield |
| `2510` | `5510` | Production quantity |
| `2111` | `5111` | Stocks |
| `2313` | `5320` | Producing animals / slaughtered |

### TM — Trade Matrix

| Filter Code | Display Code | Element |
|---|---|---|
| `2610` | — | Import quantity |
| `2620` | — | Import value |
| `2910` | — | Export quantity |
| `2920` | — | Export value |

### FS — Food Security

| Filter Code | Display Code | Element |
|---|---|---|
| `6120` | — | Value |
| `6210` | — | Confidence interval |

> **Always call `faostat_get_codes(dimension_id='element', domain_code=...)` before querying data.** Filter codes vary by domain and cannot be inferred from display codes.

```python
# WRONG — uses display code 5510, returns empty data
faostat_get_data('QCL', area='2', item='515', element='5510', year='2024')

# CORRECT — uses filter code 2510, returns data
faostat_get_data('QCL', area='2', item='515', element='2510', year='2024')
```

---

## Limitations & Notes

- This server targets the **FAOSTAT dev environment** (`api-faostat.dev.fao.org`). Data may not be fully up to date with the production database.
- Rate limit: **2 requests/second**, enforced automatically via token bucket.
- No caching — all responses are fetched live from the FAOSTAT API.
- For large domains (e.g., Trade Matrix), always apply area, item, and year filters to keep response sizes manageable.

---

## Related Projects & Resources

- [Model Context Protocol](https://modelcontextprotocol.io) — the open standard powering this server
- [FAOSTAT](https://www.fao.org/faostat/en/) — UN FAO's official statistics portal
- [FAOSTAT API Docs](https://faostat.fao.org/dev-internal/en/#developer-portal) — developer reference
- [Claude Desktop](https://claude.ai/download) — one of the AI assistants this server works with
- [Cursor](https://www.cursor.com) — AI code editor with MCP support
- [Windsurf](https://windsurf.com) — AI IDE with MCP support

---

## GitHub Topics

If you fork or star this repo, suggested topics: `mcp`, `faostat`, `model-context-protocol`, `ai-tools`, `agriculture`, `food-security`, `fao`, `un-data`, `python`, `llm`, `unfao`, `undata`
