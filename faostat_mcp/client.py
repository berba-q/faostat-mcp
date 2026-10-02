"""
FAOSTAT HTTP client for the MCP server.
Rate-limited to 2 req/s, with automatic retries on transport and 5xx errors,
and transparent JWT token refresh via the FAOSTAT /auth/login endpoint.
"""

import asyncio
import base64
import json as _json
import logging
import os
import pathlib
import sqlite3
import time
from typing import Any, Dict
import redis
import heapq
import json
import hashlib

import httpx
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("FAOSTAT_BASE_URL", "https://faostatservices.fao.org/api/v1")
DEFAULT_LANG = os.getenv("FAOSTAT_DEFAULT_LANG", "en")

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
# SQLite disk cache (cross-session persistence, no external dependencies)
# ---------------------------------------------------------------------------

_DISK_CACHE_PATH = pathlib.Path.home() / ".cache" / "faostat-mcp" / "cache.db"
_DEFAULT_DISK_CACHE_TTL = 86400  # 24 hours


class DiskCache:
    """SQLite-backed persistent cache. Survives MCP server process restarts.

    Cache location: ~/.cache/faostat-mcp/cache.db
    Default TTL: 24 hours (appropriate since FAOSTAT data updates at most daily).
    Bloat prevention: LRU eviction when entry count exceeds DISK_CACHE_MAX_ENTRIES.
    Disable entirely: set env var FAOSTAT_DISK_CACHE=false.
    """

    def __init__(
        self,
        db_path: pathlib.Path = _DISK_CACHE_PATH,
        ttl: int = _DEFAULT_DISK_CACHE_TTL,
    ):
        self._ttl = ttl
        self._enabled = os.getenv("FAOSTAT_DISK_CACHE", "true").lower() != "false"
        if not self._enabled:
            self._conn = None
            return
        self._max_entries = int(os.getenv("DISK_CACHE_MAX_ENTRIES", "1000"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastMCP async tools may run on different threads
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(key TEXT PRIMARY KEY, data TEXT, expires_at REAL)"
        )
        self._conn.commit()
        # On startup: purge expired rows, then compact freed pages
        self._conn.execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))
        self._conn.commit()
        # VACUUM must run outside a transaction (sets autocommit temporarily)
        self._conn.isolation_level = None
        try:
            self._conn.execute("VACUUM")
        finally:
            self._conn.isolation_level = ""

    def get(self, key: str) -> Any:
        """Return cached value for key, or None if missing/expired."""
        if not self._enabled or self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT data, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row and row[1] > time.time():
            try:
                return _json.loads(row[0])
            except (ValueError, TypeError):
                return None
        return None

    def set(self, key: str, data: Any) -> None:
        """Store data under key with configured TTL. Evicts oldest entries if over limit."""
        if not self._enabled or self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, data, expires_at) VALUES (?, ?, ?)",
                (key, _json.dumps(data), time.time() + self._ttl),
            )
            # LRU eviction: delete oldest entries when over max_entries limit
            count = self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            if count > self._max_entries:
                overflow = count - self._max_entries
                self._conn.execute(
                    "DELETE FROM cache WHERE key IN "
                    "(SELECT key FROM cache ORDER BY expires_at ASC LIMIT ?)",
                    (overflow,),
                )
            self._conn.commit()
        except (sqlite3.Error, TypeError, ValueError) as exc:
            logger.warning("DiskCache.set failed: %s", exc)


# ---------------------------------------------------------------------------
# Hybrid caching management
# ---------------------------------------------------------------------------

def _get_redis_connector() -> redis.Redis | None:
    """Redis connection management."""
    try:
        ip_address = os.getenv("REDIS_HOST_IP_ADDRESS", "localhost")
        port = os.getenv("REDIS_HOST_PORT_NUMBER", "6379")
        data_base = os.getenv("REDIS_DATABASE", "0")
        username = os.getenv("REDIS_USERNAME", None)
        password = os.getenv("REDIS_PASSWORD", None)
        redis_conn = redis.from_url(
            f"redis://{ip_address}:{port}/{data_base}",
            username=username,
            password=password, 
            decode_responses=True
            )
        if not redis_conn.ping():
            raise Exception("The Redis server instance seems to be down!")
        logger.info("Successfully initialize the redis caching!")
        return redis_conn
    except (redis.ConnectionError ,redis.AuthenticationError) as error_msg:
        logger.warning(
            f"Unable to initialize the redis caching due to {type(error_msg).__name__} : {str(error_msg)}!")
        return None
    except Exception as error_msg:
        logger.warning(
            f"An unpexctedt error occurs. {type(error_msg).__name__} : {str(error_msg)}!")
        return None
    

class HybridCaching:
    """In-Memory and Redis caching combined in a unified caching strategy"""

    # Initialise in-memory cache using a hash map to handle lightweight data
    user_caches: Dict[str, Dict[str, Any]] = {}
    # Initialise a min-heap to detect expired element in the mem-cache
    min_heap_ttl = []
    
    def __init__(self,
                  mem_cache_ttl : int = 1200, 
                  redis_cache_ttl : int = 1800,
                  max_mem_cache_size: int = 128,
                  user_token: str = None,
                  redis_conn: redis.Redis = None,
                  ):
        
        # Initialise in-memory cache duration
        self.MEM_CACHE_TTL = mem_cache_ttl
        # Initialise redis cache duration
        self.REDIS_CACHE_TTL = redis_cache_ttl
        # Maximum cache size
        self.MAX_CACHE_SIZE = max_mem_cache_size
        # Get FAOSTAT token from the env
        self.user_token = user_token
        # Initialise redis connection to handle large scale distributed caching
        self.redis_conn = redis_conn
        # Initialise SQLite disk cache for cross-session persistence
        self.disk_cache = DiskCache(
            ttl=int(os.getenv("DISK_CACHE_TTL", str(_DEFAULT_DISK_CACHE_TTL)))
        )


    def get_data(self, tool_name: str, args: dict) -> Any:
        """Smart retrieval: Memory → Disk → Redis (promotes to memory on disk/Redis hit)."""
        # 1. In-memory (fastest)
        val = self.get_mem_cache(tool_name, args)
        if val is not None:
            return val
        # 2. Disk (cross-session persistence, no external dependency)
        disk_key = f"{tool_name}:{HybridCaching.__create_cache_key(args)}"
        val = self.disk_cache.get(disk_key)
        if val is not None:
            self.set_mem_cache(tool_name, args, val)  # promote to memory
            return val
        # 3. Redis (distributed / high-volume deployments)
        if self.redis_conn:
            val = self.get_redis_cache(tool_name, args)
            if val is not None:
                return val
        return None

    def set_data(self, tool_name: str, args: dict, data: Any):
        """Smart storage: always writes to disk; also writes to Redis or memory."""
        disk_key = f"{tool_name}:{HybridCaching.__create_cache_key(args)}"
        self.disk_cache.set(disk_key, data)
        if self.redis_conn:
            self.set_redis_cache(tool_name, args, data)
        else:
            self.set_mem_cache(tool_name, args, data)

    def set_mem_cache(self, tool_name: str, args: dict, data: Any) -> bool:
        """Store data in memory-based cache."""
        # Cache data if it is not already cache
        if not self.get_mem_cache(tool_name, args):
            args_hash = HybridCaching.__create_cache_key(args)
            if self.user_token not in HybridCaching.user_caches:
                HybridCaching.user_caches[self.user_token] = {}
            cache_key = f"{tool_name}:{args_hash}"
            curr_time = time.time()
            HybridCaching.user_caches[self.user_token][cache_key] = [curr_time + self.MEM_CACHE_TTL, data]
            # Use min-heap to keep tracking of expired elements in the cache
            heapq.heappush(HybridCaching.min_heap_ttl, (curr_time + self.MEM_CACHE_TTL, self.user_token, cache_key))
            # Make sure that there are no expired elements in cache
            self.__remove_expired_mem_cache()
            return False
        return True
   
    def get_mem_cache(self, tool_name: str, args: dict) -> Any:
        """Retrieve in-memmory-based cache if it exists and hasn't expired."""
        args_hash = HybridCaching.__create_cache_key(args)
        user_cache = HybridCaching.user_caches.get(self.user_token, {})
        cache_key = f"{tool_name}:{args_hash}"
        if cache_key in user_cache:
            expiry, data = user_cache[cache_key]
            if time.time() < expiry:
                # Re-initialize the TTL of the data after a cache hit
                curr_time = time.time() 
                HybridCaching.user_caches[self.user_token][cache_key][0] = curr_time + self.MEM_CACHE_TTL
                # Add the cached data new ttl to the heap
                heapq.heappush(HybridCaching.min_heap_ttl, (curr_time + self.MEM_CACHE_TTL, self.user_token, cache_key))
                logger.info(f" {tool_name} : cache hit!")
                return data
        return None
    
    def set_redis_cache(self, tool_name: str, args: dict, data: Any) -> bool:
        """Store data in redis-based cache."""
        args_hash = HybridCaching.__create_cache_key(args)
        # Cache the data if it is not already cache
        if self.redis_conn and not self.get_redis_cache(tool_name, args):
            try:
                self.redis_conn.setex(
                    f"mcp:cache:{self.user_token}:{tool_name}:{args_hash}", 
                    self.REDIS_CACHE_TTL, 
                    json.dumps(data)
                    )
                return False
            except (redis.RedisError, json.JSONDecodeError) as error_msg:
                logger.error(f"Unable to set redis cache due to {type(error_msg).__name__} : {str(error_msg)}!")
                return True
        return True

    def get_redis_cache(self, tool_name: str, args: dict) -> str | None:
        """Retrieve redis cache if it exists and hasn't expired."""
        args_hash = HybridCaching.__create_cache_key(args)
        if self.redis_conn:
            cache_key = f"mcp:cache:{self.user_token}:{tool_name}:{args_hash}"
            try:
                pipe = self.redis_conn.pipeline()
                pipe.get(cache_key)
                pipe.expire(cache_key, self.REDIS_CACHE_TTL)
                cached_data = pipe.execute()[0]
                return json.loads(cached_data) if cached_data else None
            except (redis.RedisError, json.JSONDecodeError) as error_msg:
                logger.error(f"Unable to get redis cache value due to {type(error_msg).__name__} : {str(error_msg)}!")
                return None
        return None

    def __remove_expired_mem_cache(self):
        """Search and remove expired cache to free up memory."""
        oldest_expiry = HybridCaching.min_heap_ttl[0][0] if HybridCaching.min_heap_ttl else None
        while oldest_expiry:
            if not (time.time() > oldest_expiry):
                break
            # Remove expired cache ttl from the heap
            oldest_expiry, oldest_user_token, oldest_cache_key = heapq.heappop(HybridCaching.min_heap_ttl)
            if HybridCaching.user_caches[oldest_user_token][oldest_cache_key]:
                # Verify if the cache ttl has not been changed by a previous cache hit
                if HybridCaching.user_caches[oldest_user_token][oldest_cache_key][0] == oldest_expiry:
                    # Remove expired element from the memory cache
                    del HybridCaching.user_caches[oldest_user_token][oldest_cache_key]
                    if not len(HybridCaching.user_caches[oldest_user_token]):
                        del HybridCaching.user_caches[oldest_user_token]
            oldest_expiry = HybridCaching.min_heap_ttl[0][0] if HybridCaching.min_heap_ttl else None
        
        # Prevent cache to grow indefinitely by constraining its size
        oldest_user_token = HybridCaching.min_heap_ttl[0][1] if HybridCaching.min_heap_ttl else None
        while oldest_user_token:
            if len(HybridCaching.user_caches[oldest_user_token]) <= self.MAX_CACHE_SIZE:
                break
            oldest_expiry, oldest_user_token, oldest_cache_key = heapq.heappop(HybridCaching.min_heap_ttl)
            if HybridCaching.user_caches[oldest_user_token][oldest_cache_key]:
                del HybridCaching.user_caches[oldest_user_token][oldest_cache_key]
                if not len(HybridCaching.user_caches[oldest_user_token]):
                        del HybridCaching.user_caches[oldest_user_token]
            oldest_user_token = HybridCaching.min_heap_ttl[0][1] if HybridCaching.min_heap_ttl else None

    @staticmethod
    def __create_cache_key(args: dict) -> str:
        """Create a stable cache key using the tool arguments."""
        args_json = json.dumps(args, sort_keys=True)
        args_hash = hashlib.md5(args_json.encode()).hexdigest()
        return args_hash
         

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
    """Manages JWT tokens with automatic refresh via the FAOSTAT /auth/login endpoint."""

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
        """POST credentials to /auth/login to obtain a fresh access token."""
        logger.info("Token expired or missing — authenticating via /auth/login …")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._base_url}/auth/login",
                data={
                    "username": self._username,
                    "password": self._password,
                },
            )
            if resp.status_code in (400, 401):
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
                    "FAOSTAT is not authenticated. "
                    "Call faostat_setup(username='...', password='...') to configure credentials, "
                    "or set FAOSTAT_API_TOKEN (or FAOSTAT_USERNAME + FAOSTAT_PASSWORD) "
                    "as environment variables."
                )
            raise FAOSTATAuthError(
                "Your FAOSTAT_API_TOKEN has expired and no credentials are configured for "
                "automatic refresh. Call faostat_setup(username='...', password='...') to set up "
                "persistent credentials, or update FAOSTAT_API_TOKEN in your environment."
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
                "Call faostat_setup(username='...', password='...') to configure credentials, "
                "or set FAOSTAT_USERNAME + FAOSTAT_PASSWORD environment variables."
            )
        async with self._lock:
            self._token = await self._login()
            return self._token


# ---------------------------------------------------------------------------
# Credential storage (keyring → config file fallback)
# ---------------------------------------------------------------------------

_CREDENTIALS_FILE = pathlib.Path.home() / ".config" / "faostat-mcp" / "credentials.json"


def _load_credentials_from_storage() -> tuple[str, str]:
    """Return (username, password) from keyring or config file, or ('', '')."""
    # 1. Try system keychain (macOS Keychain, Windows Credential Manager)
    try:
        import keyring  # optional dependency: pip install faostat-mcp[keyring]
        u = keyring.get_password("faostat-mcp", "username") or ""
        p = keyring.get_password("faostat-mcp", "password") or ""
        if u and p:
            return u, p
    except Exception:
        pass
    # 2. Fall back to config file (~/.config/faostat-mcp/credentials.json)
    if _CREDENTIALS_FILE.exists():
        try:
            data = _json.loads(_CREDENTIALS_FILE.read_text())
            return data.get("username", ""), data.get("password", "")
        except Exception:
            pass
    return "", ""


def _save_credentials_to_storage(username: str, password: str) -> str:
    """Store credentials in keyring (if available) and config file (mode 600).

    Returns a human-readable description of where the credentials were saved.
    """
    saved = []
    try:
        import keyring
        keyring.set_password("faostat-mcp", "username", username)
        keyring.set_password("faostat-mcp", "password", password)
        saved.append("system keychain")
    except Exception:
        pass
    # Always write config file as a reliable fallback
    _CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CREDENTIALS_FILE.write_text(
        _json.dumps({"username": username, "password": password})
    )
    _CREDENTIALS_FILE.chmod(0o600)  # owner read/write only
    saved.append(str(_CREDENTIALS_FILE))
    return " and ".join(saved)


def _reset_token_manager() -> None:
    """Reset the singleton so the next _get_token_manager() call re-reads credentials."""
    global _token_manager
    _token_manager = None


# ---------------------------------------------------------------------------
# Lazily-initialised singleton
# ---------------------------------------------------------------------------

_token_manager: TokenManager | None = None


def _get_token_manager() -> TokenManager:
    """Return the module-level TokenManager singleton.

    Credential resolution order:
    1. FAOSTAT_API_TOKEN env var
    2. FAOSTAT_USERNAME + FAOSTAT_PASSWORD env vars
    3. System keychain (set by faostat_setup tool)
    4. ~/.config/faostat-mcp/credentials.json (set by faostat_setup tool)
    """
    global _token_manager
    if _token_manager is None:
        token    = os.getenv("FAOSTAT_API_TOKEN", "")
        username = os.getenv("FAOSTAT_USERNAME", "")
        password = os.getenv("FAOSTAT_PASSWORD", "")
        # If env vars don't provide credentials, try persistent storage
        if not (username and password):
            username, password = _load_credentials_from_storage()
        _token_manager = TokenManager(
            base_url=BASE_URL,
            token=token,
            username=username,
            password=password,
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
            "The token was rejected (expired or revoked). Set FAOSTAT_USERNAME and "
            "FAOSTAT_PASSWORD (or run faostat_setup) to enable automatic refresh."
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
        # Auto-refresh on 401/403 and retry once. The API answers 403
        # "Authentication Failed" for a revoked token that is not yet expired.
        if response.status_code in (401, 403) and tm.has_credentials:
            logger.info("Got %s — refreshing token and retrying …", response.status_code)
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
        # Auto-refresh on 401/403 and retry once. The API answers 403
        # "Authentication Failed" for a revoked token that is not yet expired.
        if response.status_code in (401, 403) and tm.has_credentials:
            logger.info("Got %s — refreshing token and retrying …", response.status_code)
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
