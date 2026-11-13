"""
OpenAlex Client for Bio-MARS / InternAgent

Provides async access to OpenAlex API for:
- Work ID resolution (from DOI/PMID/PMCID)
- Citation network expansion (cites/cited_by)
- Batch metadata retrieval with OR syntax
- Scoring fields (cited_by_count, has_fulltext, is_retracted, etc.)

API Reference: https://docs.openalex.org/
Rate Limiting: Credit-based (singleton=1, list=10, content=100)
Authentication: api_key parameter (required from 2026-02-13)
"""

import os
import asyncio
import aiohttp
import logging
import time
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlencode, quote

logger = logging.getLogger(__name__)


class AsyncCreditLimiter:
    """
    Credit-based rate limiter for OpenAlex.
    Tracks estimated credits and enforces a per-minute budget.
    """
    def __init__(self, credits_per_minute: int = 100):
        self.credits_per_minute = credits_per_minute
        self.credits_used = 0
        self.window_start = time.monotonic()
        self._lock = asyncio.Lock()
    
    async def acquire(self, credits: int = 1):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.window_start
            
            # Reset window every 60 seconds
            if elapsed >= 60:
                self.credits_used = 0
                self.window_start = now
            
            # If we'd exceed budget, wait until window resets
            if self.credits_used + credits > self.credits_per_minute:
                wait_time = 60 - elapsed + 0.1
                logger.info(f"OpenAlex credit limit reached, waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
                self.credits_used = 0
                self.window_start = time.monotonic()
            
            self.credits_used += credits
    
    def get_usage(self) -> Dict[str, Any]:
        return {
            "credits_used": self.credits_used,
            "credits_per_minute": self.credits_per_minute,
            "window_elapsed": time.monotonic() - self.window_start
        }


class OpenAlexClient:
    """
    Async client for OpenAlex API.
    
    Features:
    - API key authentication (required from 2026-02-13)
    - Credit-based rate limiting
    - Batch retrieval with OR filter syntax
    - Citation network traversal (cites/cited_by)
    """
    
    BASE_URL = "https://api.openalex.org"
    
    # Credit costs per request type
    CREDIT_SINGLETON = 1
    CREDIT_LIST = 10
    CREDIT_CONTENT = 100
    
    def __init__(self):
        self.api_key = os.getenv("OPENALEX_API_KEY")
        self.mailto = os.getenv("OPENALEX_MAILTO", "")
        
        # Rate limiting
        credits_per_min = int(os.getenv("OPENALEX_CREDITS_PER_MINUTE", "100"))
        self.limiter = AsyncCreditLimiter(credits_per_min)
        
        self.timeout = int(os.getenv("OPENALEX_TIMEOUT", "30"))
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._init_lock = asyncio.Lock()
        
        # Track credits for reporting
        self._total_credits_used = 0
        self._request_count = 0
    
    async def _ensure_session(self):
        async with self._init_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                self._session = aiohttp.ClientSession(timeout=timeout)
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
    
    async def __aenter__(self):
        await self._ensure_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    def _build_params(self, extra_params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Build request params with auth."""
        params = {}
        
        # Add API key (required from 2026-02-13)
        if self.api_key:
            params["api_key"] = self.api_key
        
        # Add mailto for polite requests
        if self.mailto:
            params["mailto"] = self.mailto
        
        if extra_params:
            params.update(extra_params)
        
        return params
    
    async def _request(
        self, 
        endpoint: str, 
        params: Dict[str, Any] = None,
        credits: int = 1,
        retries: int = 3
    ) -> Optional[Dict[str, Any]]:
        """
        Make an authenticated request to OpenAlex.
        
        Args:
            endpoint: API endpoint (e.g., "/works/W123")
            params: Query parameters
            credits: Credit cost for rate limiting
            retries: Number of retries on transient errors
        """
        await self._ensure_session()
        await self.limiter.acquire(credits)
        
        url = f"{self.BASE_URL}{endpoint}"
        full_params = self._build_params(params)
        
        attempt = 0
        while attempt <= retries:
            try:
                async with self._session.get(url, params=full_params) as resp:
                    self._request_count += 1
                    self._total_credits_used += credits
                    
                    if resp.status == 200:
                        return await resp.json()
                    
                    if resp.status == 404:
                        logger.debug(f"OpenAlex 404: {url}")
                        return None
                    
                    if resp.status in [429, 500, 502, 503, 504]:
                        attempt += 1
                        if attempt > retries:
                            logger.error(f"OpenAlex max retries for {url}")
                            return None
                        wait = 2 ** attempt
                        logger.warning(f"OpenAlex {resp.status}, retry {attempt}/{retries} in {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    
                    # Other errors
                    text = await resp.text()
                    logger.error(f"OpenAlex {resp.status}: {text[:500]}")
                    return None
                    
            except asyncio.TimeoutError:
                attempt += 1
                if attempt > retries:
                    logger.error(f"OpenAlex timeout for {url}")
                    return None
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"OpenAlex request error: {e}")
                return None
        
        return None
    
    # =========================================================================
    # Step 0: ID Resolution
    # =========================================================================
    
    async def resolve_to_openalex_id(
        self, 
        identifier: str,
        id_type: str = "auto"
    ) -> Optional[str]:
        """
        Resolve external identifiers to OpenAlex Work ID.
        
        Args:
            identifier: The identifier value (e.g., "10.1038/...", "12345678", "PMC123456")
            id_type: One of "auto", "doi", "pmid", "pmcid", "openalex"
        
        Returns:
            OpenAlex Work ID (e.g., "W2741809807") or None
        """
        # If already OpenAlex ID
        if identifier.startswith("W") and identifier[1:].isdigit():
            return identifier
        if identifier.startswith("https://openalex.org/W"):
            return identifier.split("/")[-1]
        
        # Auto-detect type
        if id_type == "auto":
            if identifier.startswith("10."):
                id_type = "doi"
            elif identifier.upper().startswith("PMC"):
                id_type = "pmcid"
            elif identifier.isdigit():
                id_type = "pmid"
            else:
                id_type = "doi"  # Default guess
        
        # Build lookup path
        if id_type == "doi":
            # URL-encode the DOI
            path = f"/works/doi:{quote(identifier, safe='')}"
        elif id_type == "pmid":
            path = f"/works/pmid:{identifier}"
        elif id_type == "pmcid":
            # Remove PMC prefix if present
            pmc_num = identifier.upper().replace("PMC", "")
            path = f"/works/pmcid:PMC{pmc_num}"
        else:
            path = f"/works/{identifier}"
        
        data = await self._request(path, credits=self.CREDIT_SINGLETON)
        if data and "id" in data:
            # Extract just the ID part: "https://openalex.org/W123" -> "W123"
            return data["id"].split("/")[-1]
        
        return None
    
    async def batch_resolve_ids(
        self,
        identifiers: List[Tuple[str, str]]  # List of (identifier, id_type)
    ) -> Dict[str, str]:
        """
        Batch resolve multiple identifiers to OpenAlex IDs.
        
        Args:
            identifiers: List of (identifier, id_type) tuples
        
        Returns:
            Dict mapping original identifier to OpenAlex ID
        """
        results = {}
        
        # Process in batches of 10 to avoid too many concurrent requests
        batch_size = 10
        for i in range(0, len(identifiers), batch_size):
            batch = identifiers[i:i+batch_size]
            tasks = [self.resolve_to_openalex_id(ident, id_type) for ident, id_type in batch]
            resolved = await asyncio.gather(*tasks)
            
            for (ident, _), oa_id in zip(batch, resolved):
                if oa_id:
                    results[ident] = oa_id
        
        return results
    
    # =========================================================================
    # Step 1: Citation Network Expansion
    # =========================================================================
    
    async def get_citing_works(
        self,
        work_id: str,
        search_query: Optional[str] = None,
        per_page: int = 200,
        sort: str = "relevance_score:desc",
        select_fields: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Get works that CITE the given work (incoming citations / backlinks).
        
        This answers: "Who cited this paper?"
        
        Args:
            work_id: OpenAlex Work ID (e.g., "W2741809807")
            search_query: Optional query to filter by relevance
            per_page: Number of results (max 200)
            sort: Sort order (relevance_score only works with search)
            select_fields: Fields to retrieve (reduces response size)
        
        Returns:
            List of work metadata dicts
        """
        params = {
            "filter": f"cites:{work_id}",
            "per-page": min(per_page, 200)
        }
        
        if search_query:
            params["search"] = search_query
            params["sort"] = sort
        
        if select_fields:
            params["select"] = ",".join(select_fields)
        
        data = await self._request("/works", params=params, credits=self.CREDIT_LIST)
        
        if data and "results" in data:
            return data["results"]
        return []
    
    async def get_cited_works(
        self,
        work_id: str,
        per_page: int = 200,
        select_fields: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Get works that are CITED BY the given work (outgoing references).
        
        This answers: "What did this paper cite?"
        
        Args:
            work_id: OpenAlex Work ID
            per_page: Number of results
            select_fields: Fields to retrieve
        
        Returns:
            List of work metadata dicts
        """
        params = {
            "filter": f"cited_by:{work_id}",
            "per-page": min(per_page, 200)
        }
        
        if select_fields:
            params["select"] = ",".join(select_fields)
        
        data = await self._request("/works", params=params, credits=self.CREDIT_LIST)
        
        if data and "results" in data:
            return data["results"]
        return []
    
    async def expand_citation_network(
        self,
        work_id: str,
        search_query: Optional[str] = None,
        citing_limit: int = 200,
        cited_limit: int = 100,
        select_fields: Optional[List[str]] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Expand both directions of citation network for a work.
        
        Args:
            work_id: OpenAlex Work ID
            search_query: Optional relevance filter for citing works
            citing_limit: Max incoming citations
            cited_limit: Max outgoing references
            select_fields: Fields to retrieve
        
        Returns:
            Dict with "citing" and "cited" lists
        """
        citing_task = self.get_citing_works(
            work_id, 
            search_query=search_query,
            per_page=citing_limit,
            select_fields=select_fields
        )
        cited_task = self.get_cited_works(
            work_id,
            per_page=cited_limit,
            select_fields=select_fields
        )
        
        citing, cited = await asyncio.gather(citing_task, cited_task)
        
        return {
            "citing": citing,
            "cited": cited
        }
    
    # =========================================================================
    # Step 2: Batch Metadata Retrieval
    # =========================================================================
    
    async def batch_get_works(
        self,
        work_ids: List[str],
        select_fields: Optional[List[str]] = None,
        batch_size: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Batch retrieve work metadata using OR filter syntax.
        
        OpenAlex supports "filter=openalex:W1|W2|W3" to get multiple works in one request.
        This is much more efficient than individual requests.
        
        Args:
            work_ids: List of OpenAlex Work IDs
            select_fields: Fields to retrieve
            batch_size: Max IDs per request (recommend 50)
        
        Returns:
            List of work metadata dicts
        """
        all_results = []
        
        # Default fields for scoring
        if not select_fields:
            select_fields = [
                "id", "title", "publication_year", "cited_by_count",
                "is_retracted", "primary_location", "open_access",
                "authorships", "doi", "ids"
            ]
        
        for i in range(0, len(work_ids), batch_size):
            batch = work_ids[i:i+batch_size]
            
            # Build OR filter: "openalex:W1|W2|W3"
            or_filter = "|".join(batch)
            
            params = {
                "filter": f"openalex:{or_filter}",
                "per-page": len(batch),
                "select": ",".join(select_fields)
            }
            
            data = await self._request("/works", params=params, credits=self.CREDIT_LIST)
            
            if data and "results" in data:
                all_results.extend(data["results"])
        
        return all_results
    
    # =========================================================================
    # Step 3: Single Work Details
    # =========================================================================
    
    async def get_work(
        self,
        work_id: str,
        select_fields: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get detailed metadata for a single work.
        
        Args:
            work_id: OpenAlex Work ID
            select_fields: Fields to retrieve
        
        Returns:
            Work metadata dict or None
        """
        params = {}
        if select_fields:
            params["select"] = ",".join(select_fields)
        
        return await self._request(f"/works/{work_id}", params=params, credits=self.CREDIT_SINGLETON)
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def extract_scoring_fields(self, work: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract fields needed for scoring from a work object.
        
        Returns dict with:
        - cited_by_count: int
        - publication_year: int
        - is_retracted: bool
        - has_fulltext: bool
        - is_core_source: bool (from primary_location.source.is_core)
        - is_oa: bool
        - doi: str or None
        - pmid: str or None
        """
        # Safely extract nested fields
        primary_loc = work.get("primary_location") or {}
        source = primary_loc.get("source") or {}
        open_access = work.get("open_access") or {}
        ids = work.get("ids") or {}
        
        return {
            "openalex_id": work.get("id", "").split("/")[-1] if work.get("id") else None,
            "title": work.get("title"),
            "cited_by_count": work.get("cited_by_count", 0) or 0,
            "publication_year": work.get("publication_year"),
            "is_retracted": work.get("is_retracted", False),
            "has_fulltext": open_access.get("is_oa", False),  # Approximation
            "is_oa": open_access.get("is_oa", False),
            "oa_url": open_access.get("oa_url"),
            "is_core_source": source.get("is_core", False),
            "source_name": source.get("display_name"),
            "doi": ids.get("doi", "").replace("https://doi.org/", "") if ids.get("doi") else None,
            "pmid": ids.get("pmid", "").replace("https://pubmed.ncbi.nlm.nih.gov/", "") if ids.get("pmid") else None,
            "pmcid": ids.get("pmcid", "").replace("https://www.ncbi.nlm.nih.gov/pmc/articles/", "").rstrip("/") if ids.get("pmcid") else None,
        }
    
    def get_credits_usage(self) -> Dict[str, Any]:
        """Get total credits used in this session."""
        return {
            "total_credits": self._total_credits_used,
            "total_requests": self._request_count,
            "current_window": self.limiter.get_usage()
        }
