import os
import aiohttp
import asyncio
import hashlib
import time
import logging
import random
import threading
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

_SHARED_LIMITERS: Dict[tuple, "AsyncRPSLimiter"] = {}
_SHARED_LIMITERS_LOCK = threading.Lock()

def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

class AsyncRPSLimiter:
    """
    Simple token bucket or leaky bucket style limiter using time.monotonic.
    Enforces a maximum request rate.
    """
    def __init__(self, rps: float):
        if rps <= 0:
            raise ValueError("rps must be greater than zero")
        self.rps = rps
        self.interval = 1.0 / rps
        self.last_request_time = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_request_time
            wait_time = self.interval - elapsed
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self.last_request_time = time.monotonic()


def _shared_limiter(base_url: str, api_key: Optional[str], rps: float) -> AsyncRPSLimiter:
    """Share one limiter across all clients using the same endpoint and API key."""
    credential = api_key or "anonymous"
    credential_hash = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    key = (base_url, credential_hash, rps)
    with _SHARED_LIMITERS_LOCK:
        limiter = _SHARED_LIMITERS.get(key)
        if limiter is None:
            limiter = AsyncRPSLimiter(rps)
            _SHARED_LIMITERS[key] = limiter
        return limiter

class BioMarsSemanticClient:
    """
    Robust Async Client for Semantic Scholar Graph API.
    Features:
    - Session reuse
    - RPS Rate Limiting
    - Automatic Retries (429/5xx)
    - Full async context manager support
    - Support for AI4Scholar proxy
    """
    def __init__(self):
        self.use_proxy = _env_flag("SEMANTIC_SCHOLAR_USE_PROXY", default=False)

        default_base = os.getenv("SEMANTIC_SCHOLAR_BASE_URL", "https://api.semanticscholar.org").rstrip("/")
        if self.use_proxy:
            proxy_base = os.getenv("AI4SCHOLAR_BASE_URL", "").rstrip("/")
            base = proxy_base or default_base
        else:
            base = default_base
        self.base_url = f"{base}/graph/v1"

        self.api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY") or os.getenv("S2_API_KEY")
        if self.api_key:
            self.api_key = self.api_key.strip() or None
        if not self.api_key and self.use_proxy:
            self.api_key = os.getenv("AI4SCHOLAR_API_KEY")
            if self.api_key:
                self.api_key = self.api_key.strip() or None
        
        # Rate Limiting
        # Default to 0.9 RPS to stay safely under 1 RPS if key is present (or strict limit if not)
        # S2 public API without key is much lower, but we assume key or safe low default.
        default_rps = 0.9
        env_rps = os.getenv("SEMANTIC_SCHOLAR_RPS")
        self.rps = float(env_rps) if env_rps else default_rps
        self.limiter = _shared_limiter(self.base_url, self.api_key, self.rps)
        self.retry_base_seconds = float(os.getenv("SEMANTIC_SCHOLAR_RETRY_BASE_SECONDS", "5"))
        self.retry_max_seconds = float(os.getenv("SEMANTIC_SCHOLAR_RETRY_MAX_SECONDS", "60"))
        
        self.timeout = int(os.getenv("SEMANTIC_SCHOLAR_TIMEOUT", "20"))
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._init_lock = asyncio.Lock()

    async def _ensure_session(self):
        async with self._init_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                self._session = aiohttp.ClientSession(
                    timeout=timeout,
                    headers=self._headers()
                )

    def _headers(self):
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
            h["x-api-key"] = self.api_key
        return h

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Dict[str, Any] = None,
        retries: int = 3,
        json_body: Any = None,
    ) -> Any:
        await self._ensure_session()
        url = f"{self.base_url}{endpoint}"
        
        attempt = 0
        while attempt <= retries:
            # Enforce Rate Limit
            await self.limiter.acquire()
            
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    
                    if resp.status == 404:
                        logger.warning(f"S2 404 Not Found: {url}")
                        return None
                        
                    if resp.status in [429, 500, 502, 503, 504]:
                        logger.warning(f"S2 Retryable Error {resp.status} for {url}. Attempt {attempt+1}/{retries+1}")
                        attempt += 1
                        if attempt > retries:
                            logger.error(f"S2 Max retries reached for {url}")
                            return None
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            retry_after_seconds = float(retry_after) if retry_after else 0.0
                        except (TypeError, ValueError):
                            retry_after_seconds = 0.0
                        exponential_delay = self.retry_base_seconds * (2 ** (attempt - 1))
                        delay = min(
                            self.retry_max_seconds,
                            max(retry_after_seconds, exponential_delay),
                        )
                        delay += random.uniform(0.0, min(1.0, delay * 0.1))
                        await asyncio.sleep(delay)
                        continue

                    if resp.status == 403:
                        logger.error(
                            "S2 Error 403 Forbidden from %s (api_key_present=%s, use_proxy=%s): %s",
                            self.base_url,
                            bool(self.api_key),
                            self.use_proxy,
                            await resp.text(),
                        )
                        return None
                        
                    # Other client errors
                    logger.error(f"S2 Error {resp.status}: {await resp.text()}")
                    return None

            except Exception as e:
                logger.error(f"S2 Request Failed: {e}")
                attempt += 1
                if attempt > retries:
                    return None
                await asyncio.sleep(1)
                
        return None

    async def get_paper(self, paper_id: str, fields: str = None) -> Optional[Dict[str, Any]]:
        """
        Fetch details for a single paper.
        Default fields if None: paperId, title, abstract, year, citationCount, url, openAccessPdf, authors, venue
        """
        if not fields:
            fields = "paperId,title,abstract,year,citationCount,url,openAccessPdf,authors,venue,externalIds,publicationTypes,journal"
        
        return await self._request("GET", f"/paper/{paper_id}", params={"fields": fields})

    async def get_papers_batch(
        self,
        paper_ids: List[str],
        fields: str = None,
    ) -> List[Optional[Dict[str, Any]]]:
        """Fetch multiple paper records in one quota-controlled request."""
        if not paper_ids:
            return []
        if not fields:
            fields = "paperId,title,abstract,year,citationCount,url,openAccessPdf,authors,venue,externalIds,publicationTypes,journal"
        data = await self._request(
            "POST",
            "/paper/batch",
            params={"fields": fields},
            json_body={"ids": paper_ids},
        )
        return data if isinstance(data, list) else []

    async def search_papers(self, query: str, limit: int = 10, offset: int = 0, fields: str = None) -> List[Dict[str, Any]]:
        """
        Search for papers.
        """
        if not fields:
            fields = "paperId,title,abstract,year,citationCount,url,openAccessPdf,authors,venue,externalIds,publicationTypes,journal"
            
        data = await self._request("GET", "/paper/search", params={
            "query": query,
            "limit": limit,
            "offset": offset,
            "fields": fields
        })
        
        if data and "data" in data:
            return data["data"]
        return []
