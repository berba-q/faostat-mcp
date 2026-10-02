<!-- mcp-name: io.github.berba-q/faostat-mcp -->
# FAOSTAT MCP Server

> Query UN food and agriculture statistics with AI — powered by the [Model Context Protocol](https://modelcontextprotocol.io)

[![Version](https://img.shields.io/github/v/release/berba-q/faostat-mcp)](https://github.com/berba-q/faostat-mcp/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/faostat-mcp)](https://pypi.org/project/faostat-mcp/)
[![MCP Registry](https://img.shields.io/badge/MCP_Registry-listed-orange)](https://registry.modelcontextprotocol.io/servers/io.github.berba-q/faostat-mcp)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-compatible-green.svg)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An MCP (Model Context Protocol) server that exposes the full [FAOSTAT API](https://www.fao.org/faostat/en/#developer-portal) as tools for AI assistants. Connect any MCP-compatible client — Claude, Cursor, Windsurf, Zed, or your own agent — to the world's most comprehensive database of food, agriculture, fisheries, forestry, and nutrition statistics, covering 245 countries and territories from the United Nations Food and Agriculture Organization (FAO).

**Keywords:** FAOSTAT, MCP server, Model Context Protocol, AI agriculture data, FAO statistics, food security AI, agricultural data Python, UN data, crop production statistics, Claude, Cursor, Windsurf

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

- **23 MCP tools** covering every FAOSTAT endpoint (data, metadata, rankings, bulk downloads, reports)
- **245 countries and territories** across dozens of domains: crops, livestock, trade, food security, emissions, forestry, fisheries, and more
- Built-in **rate limiting** (2 req/s) — safe for the FAOSTAT production API out of the box
- **Auto-retry** with exponential backoff on transient network errors
- Rich tool descriptions so the AI knows exactly when and how to call each tool
- **3-tier hybrid caching** — in-memory (20 min) → SQLite disk (24 h, cross-session) → Redis (optional, 30 min)
- **Zero-config auth** via `faostat_setup` — store credentials once, never touch a config file again
- **Disambiguation** via `faostat_search_codes` — agents ask before guessing ambiguous codes
- Works with **Claude Desktop, Claude Code, Cursor, Windsurf, Zed**, and any MCP-compatible client

---

## Quick Start

### Prerequisites

- Python 3.10+
- Any MCP-compatible client (Claude Desktop, Cursor, Windsurf, Zed, or a custom agent)

### Option A — Install via MCP Registry (recommended)

Listed on the [official MCP Registry](https://registry.modelcontextprotocol.io/servers/io.github.berba-q/faostat-mcp) — discoverable directly from Claude Desktop, Cursor, and any MCP-compatible client.

```bash
# Install with pip or uvx (no virtual env needed):
pip install faostat-mcp
uvx faostat-mcp
```

**Updating:** `uvx faostat-mcp` picks up new releases when your AI client restarts the server; if you still see an old version, run `uvx faostat-mcp@latest` once. `pip` and `uv tool install` don't upgrade on their own: run `pip install -U faostat-mcp` or `uv tool upgrade faostat-mcp`, then restart your client. The server also checks PyPI once per session and logs a notice to stderr when a newer version exists (opt out with `FAOSTAT_NO_UPDATE_CHECK=1`).

### Option B — Install from source

```bash
git clone https://github.com/berba-q/faostat-mcp.git
cd faostat-mcp
pip install -e .
```

### Configure credentials

**Easiest — use the `faostat_setup` tool (no config files needed):**

Once the server is running and connected to your AI client, ask your assistant:
> "Call faostat_setup with my FAOSTAT username and password."

The tool validates your credentials against the API, then stores them securely in your system keychain (macOS/Windows) or `~/.config/faostat-mcp/credentials.json` (Linux/Docker). All subsequent sessions authenticate automatically — no env vars or `.env` file required.

**Alternative — environment variables (CI/CD, Docker, advanced):**

```bash
cp .env.example .env
# Edit .env:
# FAOSTAT_USERNAME=your_email              ← recommended: tokens refresh automatically
# FAOSTAT_PASSWORD=your_password
# FAOSTAT_API_TOKEN=your_token_here        ← alternative: expires after 1 hour
```

Register for a free FAOSTAT API account at the [FAOSTAT Developer Portal](https://www.fao.org/faostat/en/#developer-portal).

### Optional — Redis caching (multi-user / high-volume deployments)

The server works without Redis (SQLite disk cache is used instead). For shared or high-volume setups, launch Redis via Docker:

```bash
docker run -p 6379:6379 -it redis/redis-stack:latest
```

Then set `REDIS_HOST_IP_ADDRESS`, `REDIS_HOST_PORT_NUMBER`, and `REDIS_DATABASE` in `.env`.

---

## Running the Server

### Development mode (interactive MCP Inspector UI)

```bash
mcp dev faostat_mcp/server.py
```

Opens a browser UI at `http://localhost:5173` where you can browse and test all 23 tools interactively.

### Production mode (stdio transport, for Claude Desktop)

```bash
python -m faostat_mcp.server
# or, using the installed script:
faostat-mcp
```

---

## Caching

The server uses a **3-tier cache** to minimise redundant API calls. FAOSTAT data updates at most daily, so most repeated queries are served instantly.

| Tier | TTL | Scope | Notes |
|------|-----|-------|-------|
| In-memory | 20 min | Current session | Fastest; reset on server restart |
| SQLite disk | 24 h | Cross-session | `~/.cache/faostat-mcp/cache.db`; no extra infra |
| Redis | 30 min | Multi-user shared | Optional; set `REDIS_*` env vars to enable |

Cache lookup order: memory → disk → Redis → API call. A disk or Redis hit promotes the value to memory for the rest of the session.

To disable the disk cache (e.g. on a read-only filesystem), set `FAOSTAT_DISK_CACHE=false`.

---

## MCP Client Integration

The server speaks standard MCP over stdio, so it works with any compatible client.

### Recommended config (PyPI install)

```json
{
  "mcpServers": {
    "faostat": {
      "command": "uvx",
      "args": ["faostat-mcp"]
    }
  }
}
```

### Dev / source config

```json
{
  "mcpServers": {
    "faostat": {
      "command": "python",
      "args": ["-m", "faostat_mcp.server"],
      "cwd": "/path/to/faostat-mcp",
      "env": {
        "FAOSTAT_USERNAME": "your_email",
        "FAOSTAT_PASSWORD": "your_password"
      }
    }
  }
}
```

### Claude Desktop

Add one of the blocks above to:
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
2. Call `faostat_search_codes` to look up a code by name — if multiple codes match (e.g. "production" matches both *Production* and *Gross Production Index*), the assistant **pauses and asks you to choose** before proceeding
   - Regions and indicators are checked with `faostat_resolve_name` — if no matching FAOSTAT definition is found for your request (e.g. *"Global South"*), the assistant **tells you so** and offers the FAO-defined alternatives (e.g. *"West Africa"* → *Western Africa*) instead of inventing one
3. Call `faostat_get_data` or `faostat_get_rankings` with the confirmed codes
4. Interpret and summarize the results in plain language

---

## Available MCP Tools

### Discovery & Metadata

| Tool | Description |
|------|-------------|
| `faostat_ping` | Check API health |
| `faostat_list_groups` | List all data groups |
| `faostat_groups_and_domains` | Full domain tree |
| `faostat_list_domains` | Domains within a group |
| `faostat_get_dimensions` | Available filters for a domain |
| `faostat_get_codes` | Browse all country/item/element filter codes |
| `faostat_search_codes` | **Search codes by name** — returns `requires_confirmation=true` when multiple codes match, forcing the agent to ask you before proceeding |
| `faostat_get_definitions` | Domain definitions |
| `faostat_get_definitions_by_type` | Definitions by type within a domain |
| `faostat_definition_types` | All definition types |
| `faostat_get_definition_type` | FAO-wide definitions for one type, no domain needed (regions/`areagroup`, countries, indicators, items, units, flags…) with `search` and `limit` |
| `faostat_resolve_name` | **Check a region, country or indicator name against FAO definitions** — returns `no_matching_definition` when the lookup finds no match; definition codes require domain filter lookup |
| `faostat_get_metadata` | Full domain metadata |
| `faostat_get_metadata_print` | Printable metadata |

### Data Retrieval

| Tool | Description |
|------|-------------|
| `faostat_get_data` | **Fetch actual statistics** |
| `faostat_get_datasize` | Estimate query result size before fetching |
| `faostat_get_rankings` | Top-N country rankings |
| `faostat_get_report_data` | Report data |
| `faostat_get_report_headers` | Report column headers |
| `faostat_list_bulk_downloads` | Bulk download file listing |
| `faostat_list_documents` | Related documents |

### Authentication

| Tool | Description |
|------|-------------|
| `faostat_setup` | **First-time setup** — validate and store credentials securely; subsequent sessions authenticate automatically |
| `faostat_refresh_token` | Manually refresh the API access token |

---

## Project Structure

```
faostat-mcp/
├── pyproject.toml
├── smithery.yaml             ← Smithery MCP registry manifest
├── .env.example
├── mcp_config_example.json   ← AI config snippet
└── faostat_mcp/
    ├── server.py             ← FastMCP server + all 23 tool definitions
    └── client.py             ← HTTP client, rate limiting, 3-tier cache, credential storage
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

- This server targets the **FAOSTAT production API** (`https://faostatservices.fao.org/api/v1`).
- Rate limit: **2 requests/second**, enforced automatically via token bucket.
- Responses are cached across 3 tiers (memory → SQLite disk → Redis) to reduce API calls — see `.env.example` for TTL and size configuration.
- The SQLite disk cache lives at `~/.cache/faostat-mcp/cache.db` and defaults to 24 h TTL with a 1,000-entry LRU cap. Set `FAOSTAT_DISK_CACHE=false` to disable.
- For large domains (e.g., Trade Matrix), always apply area, item, and year filters to keep response sizes manageable.

---

## Skills

Want guided analysis workflows on top of this server? Check out [**FAOSTAT Skills**](https://github.com/berba-q/faostat-skills) — 9 platform-agnostic AI skills for country profiles, commodity briefings, trade analysis, climate assessments, data visualization, and more. Works with Claude Code, OpenAI Codex, and any AI assistant that supports the SKILL.md format.

---

## Related Projects & Resources

- [FAOSTAT Skills](https://github.com/berba-q/faostat-skills) — analysis skills built on top of this MCP server
- [Model Context Protocol](https://modelcontextprotocol.io) — the open standard powering this server
- [FAOSTAT](https://www.fao.org/faostat/en/) — UN FAO's official statistics portal
- [FAOSTAT API Docs](https://www.fao.org/faostat/en/#developer-portal) — developer reference
- [Claude Desktop](https://claude.ai/download) — one of the AI assistants this server works with
- [Cursor](https://www.cursor.com) — AI code editor with MCP support
- [Windsurf](https://windsurf.com) — AI IDE with MCP support

---

## Contributors

Thanks to everyone who has contributed to this project.

| Contributor | Contribution |
|---|---|
| [berba-q](https://github.com/berba-q) | Project author — API client, MCP tool layer, response formatting |
| [Tohokantche](https://github.com/Tohokantche) | Hybrid caching — in-memory (dict + min-heap TTL) and Redis tiers with graceful fallback |

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## Citation

If you use this tool in academic work or research, please cite it:

**Plain text:**
> Obli-Laryea, G., & Contributors. (2026). FAOSTAT MCP Server: AI-assisted access to FAOSTAT (v1.2.2) [Computer software]. https://github.com/berba-q/faostat-mcp

**BibTeX:**
```bibtex
@software{faostat_mcp,
  author  = {Obli-Laryea, Griffiths and {Contributors}},
  title   = {FAOSTAT MCP Server: AI-assisted access to UN food and agriculture statistics},
  year    = {2026},
  url     = {https://github.com/berba-q/faostat-mcp},
  version = {1.2.2}
}
```

See the [Contributors](#contributors) section for a full list of authors.

When citing the underlying FAOSTAT data, use the FAO's recommended format with the specific domain:

> FAO, {year}. FAOSTAT: {Domain Name}, http://www.fao.org/faostat/en/#data/{domain_code}

For example:
> FAO, 2026. FAOSTAT: Crops and Livestock Products, http://www.fao.org/faostat/en/#data/QCL

> FAO, 2026. FAOSTAT: Emissions Totals, http://www.fao.org/faostat/en/#data/GT

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a full history of changes, generated automatically from [conventional commits](https://www.conventionalcommits.org).

---

## GitHub Topics

If you fork or star this repo, suggested topics: `mcp`, `faostat`, `model-context-protocol`, `ai-tools`, `agriculture`, `food-security`, `fao`, `un-data`, `python`, `llm`, `unfao`, `undata`

### Smaller tool responses

Data, rankings, and global definitions default to `response_format="compact"` (column names once, followed by row arrays). Use `response_format="objects"` for the previous object-per-row shape, or `"csv"` for data and rankings. Select `fields` on data queries while preserving units needed to interpret values. Search results default to 25 matches; narrow the query if `_truncated` is true.

When the domain is known, use `faostat_search_codes` directly. The global name resolver returns `definition_code`, which is not necessarily a query filter code. A `no_matching_definition` result describes this lookup, not all FAO terminology. Code browsing, search, and validation share a cached table; repeated data queries reuse raw responses across formats, field selections, and row limits.
