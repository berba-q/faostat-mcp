# Changelog

All notable changes to this project are documented here.
Releases from v1.1.0 onwards are generated automatically from [conventional commits](https://www.conventionalcommits.org) by [release-please](https://github.com/googleapis/release-please).

## [1.2.3](https://github.com/berba-q/faostat-mcp/compare/v1.2.2...v1.2.3) (2026-04-13)


### Documentation

* add MCP Registry badge, listing link, and shorten server.json description ([cba8a86](https://github.com/berba-q/faostat-mcp/commit/cba8a86b40031bc22b094ea5f7630bf15f23779c))

## [1.2.2](https://github.com/berba-q/faostat-mcp/compare/v1.2.1...v1.2.2) (2026-04-13)


### Bug Fixes

* add MCP Registry server.json and ownership verification comment ([e822734](https://github.com/berba-q/faostat-mcp/commit/e8227346cb74856504c6ff5b982e8910a8b26508))

## [1.2.1](https://github.com/berba-q/faostat-mcp/compare/v1.2.0...v1.2.1) (2026-04-13)


### Bug Fixes

* add workflow_dispatch to allow manual PyPI publish trigger ([487f44e](https://github.com/berba-q/faostat-mcp/commit/487f44e35f3862f10469a06500d25b9534176f61))
* consolidate publish into release-please workflow to bypass GITHUB_TOKEN event restriction ([cf5f7d2](https://github.com/berba-q/faostat-mcp/commit/cf5f7d2c943bc766ba93bc6bd8acf6b4fba76e3d))

## [1.2.0](https://github.com/berba-q/faostat-mcp/compare/v1.1.1...v1.2.0) (2026-04-13)


### Features

* Upgrade version to 1.2.0 and implement SQLite disk caching ([901e01a](https://github.com/berba-q/faostat-mcp/commit/901e01adc0e56afd95cf695bad553cdd9666412c))

## [1.1.1](https://github.com/berba-q/faostat-mcp/compare/v1.1.0...v1.1.1) (2026-04-13)


### Documentation

* add CHANGELOG.md as baseline for release-please automation ([dacc4b8](https://github.com/berba-q/faostat-mcp/commit/dacc4b8e47943bedca7d7d2f8f3a08a6ed483bdc))

## [1.1.0](https://github.com/berba-q/faostat-mcp/releases/tag/v1.1.0) — 2026-04-13

### Features

* Hybrid server-side caching — in-memory (dict + min-heap TTL tracking) and optional Redis tier, with graceful fallback when Redis is unavailable ([Tohokantche](https://github.com/Tohokantche))
* `faostat_get_data`: new `response_format` parameter (`objects` / `compact` / `csv`) and `fields` filter for column selection
* `faostat_get_codes`: new `limit` parameter for large dimensions (e.g. `item` with 1000+ entries)
* `faostat_get_rankings`: inherits `response_format` and `fields` support
* Token auto-refresh via `/auth/login` endpoint — tokens are renewed transparently when expired
* Issue templates for bug reports and feature requests

### Bug Fixes

* `faostat_get_codes`: `limit` was silently skipped because the FAOSTAT API returns `{"data": [...]}` (a dict), not a plain list — extracted `data` key before applying the limit
* `faostat_get_codes`: `limit` was not part of the cache key, causing a `limit=0` call to return a cached truncated result from a prior `limit=5` call
* `faostat_get_codes`: `set_data` call was missing `arg_dict`, which would have raised `TypeError` on every successful API response
* `HybridCaching.__remove_expired_mem_cache`: fixed TTL-refresh race — now checks expiry matches before deleting

### Code Refactoring

* Extracted `_get_redis_connector()` as a standalone module-level function, making `HybridCaching` reusable independently of Redis configuration
* Replaced `sys._getframe()` with string literals for tool name references throughout server

## [1.0.1](https://github.com/berba-q/faostat-mcp/releases/tag/v1.0.1) — Initial production release

* 18 MCP tools covering the full FAOSTAT API surface
* Rate-limited HTTP client (2 req/s) with exponential backoff auto-retry
* Compatible with Claude Desktop, Claude Code, Cursor, Windsurf, Zed, and any MCP stdio client
* FastMCP-based server with rich tool descriptions for automatic AI tool selection
