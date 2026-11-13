import os
import aiohttp
import logging
import asyncio
import random
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class BioMarsJinaClient:
    """
    Robust client for Jina Reader API to convert URLs to Markdown.
    
    Features:
    - Session reuse with connection pooling
    - Exponential backoff retry logic
    - Two-pass fallback strategy (Targeted -> Generic)
    - Advanced diagnostics and headers
    """
    
    def __init__(self):
        self.base_url = os.getenv("JINA_READER_BASE_URL", "https://r.jina.ai")
        self.base_url = self.base_url.rstrip("/")
        # Read the API key
        self.api_key = os.getenv("JINA_API_KEY") 
        self.total_timeout = float(os.getenv("JINA_TOTAL_TIMEOUT", "60"))
        self.connect_timeout = float(os.getenv("JINA_CONNECT_TIMEOUT", "10"))
        self.sock_read_timeout = float(os.getenv("JINA_SOCK_READ_TIMEOUT", "60"))
        self.retry_count = max(0, int(os.getenv("JINA_RETRY_COUNT", "3")))
        self.generic_fallback = os.getenv("JINA_GENERIC_FALLBACK", "1") == "1"
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._connector: Optional[aiohttp.TCPConnector] = None
        self._init_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(int(os.getenv("JINA_MAX_CONCURRENCY", "8")))

    async def _ensure_session(self):
        """Lazy initialization of the aiohttp session."""
        async with self._init_lock:
            if self._session is None or self._session.closed:
                # TCPConnector with limit and DNS caching as requested
                self._connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
                
                # Default timeout configuration
                timeout = aiohttp.ClientTimeout(
                    total=self.total_timeout,
                    connect=self.connect_timeout,
                    sock_connect=self.connect_timeout,
                    sock_read=self.sock_read_timeout,
                )
                
                # Build request headers
                headers = {"User-Agent": "BioMars-InternAgent/1.0"}
                if self.api_key:
                    # Jina requires the format "Bearer <YOUR_KEY>"
                    headers["Authorization"] = f"Bearer {self.api_key}"

                self._session = aiohttp.ClientSession(
                    connector=self._connector,
                    timeout=timeout,
                    headers=headers  # Include authentication headers
                )

    async def close(self):
        """Close the underlying session."""
        if self._session and not self._session.closed:
            await self._session.close()
        if self._connector and not self._connector.closed:
            await self._connector.close()
        self._session = None
        self._connector = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _fetch_with_retry(
        self,
        url: str,
        headers: Dict[str, str],
        retry_count: Optional[int] = None,
    ) -> Optional[str]:
        """
        Fetch content with exponential backoff retry logic.
        """
        await self._ensure_session()
        if retry_count is None:
            retry_count = self.retry_count
        
        attempt = 0
        while attempt <= retry_count:
            try:
                start_time = time.time()
                async with self._sem:
                    async with self._session.get(url, headers=headers) as response:
                        elapsed = time.time() - start_time
                        
                        # Log diagnostics
                        logger.debug(
                            f"Jina Fetch: {url} | Status: {response.status} | "
                            f"Time: {elapsed:.2f}s | Headers: {dict(response.headers)}"
                        )

                        if response.status == 200:
                            content = await response.text()
                            if not content or len(content.strip()) < 50:
                                # Treat empty/too short content as a failure to trigger retry/fallback
                                logger.warning(f"Content too short ({len(content)} chars) for {url}")
                                if attempt < retry_count:
                                    raise ValueError("Content too short")
                                return None
                            return content
                        
                        if response.status in [429, 500, 502, 503, 504]:
                            # Retryable errors
                            logger.warning(f"Retryable error {response.status} for {url}")
                            raise aiohttp.ClientError(f"Status {response.status}")
                        
                        # Non-retryable error
                        logger.error(f"Non-retryable error {response.status} for {url}")
                        return None

            except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as e:
                attempt += 1
                if attempt > retry_count:
                    logger.error(f"Max retries reached for {url}: {e}")
                    return None
                
                # Exponential backoff with jitter: 2^attempt + random(0-1)
                delay = min(8, (2 ** attempt) + random.random())
                logger.info(f"Retrying {url} in {delay:.2f}s (Attempt {attempt}/{retry_count})")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"Unexpected error fetching {url}: {e}")
                return None
        
        return None

    async def fetch_markdown(self, url: str) -> str:
        """
        Fetch the markdown content of a URL using Jina Reader with two-pass fallback.
        
        Pass 1: Targeted selector + text response
        Pass 2: Generic selector + markdown response
        """
        if not url:
            return "Error: Empty URL provided"

        target_url = f"{self.base_url}/{url}"
        
        # --- Pass 1: Targeted Selector ---
        headers_pass1 = {
            "X-Respond-With": "text",
            "X-Target-Selector": "#main-content, article, .content, main",
            "X-Return-Format": "markdown"
        }
        
        logger.info(f"Attempting Pass 1 (Targeted) for {url}")
        content = await self._fetch_with_retry(target_url, headers_pass1)
        
        if content:
            return content

        if not self.generic_fallback:
            logger.info(
                "Pass 1 failed/empty and generic Jina fallback is disabled for %s",
                url,
            )
            return f"Error: Failed to fetch content from {url} after targeted pass."

        # --- Pass 2: Fallback Generic ---
        logger.info(f"Pass 1 failed/empty. Attempting Pass 2 (Generic) for {url}")
        headers_pass2 = {
            "Accept": "text/html,application/xhtml+xml", 
            "X-Respond-With": "markdown",
            "X-Target-Selector": "body", # Fallback to everything
            "X-Return-Format": "markdown"
        }
        
        content = await self._fetch_with_retry(target_url, headers_pass2)
        
        if content:
            return content

        return f"Error: Failed to fetch content from {url} after retries and fallback."
