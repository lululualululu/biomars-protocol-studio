import os
import aiohttp
from typing import Optional, List, Dict

class SerperClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("SERPER_API_KEY", "")
        self.base_url = "https://google.serper.dev/search"

    async def search(self, query: str, k: int = 10) -> List[Dict]:
        if not self.api_key:
            return []
        
        headers = {
            "X-API-KEY": self.api_key, 
            "Content-Type": "application/json"
        }
        payload = {"q": query, "num": k}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.base_url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    
                    # Return the raw structure or standardized one?
                    # The caller (protocol_research_agent) expects 'organic' list inside data or list of items
                    # Let's return the full data dict to be flexible, or list of organic results.
                    # The agent code does: results = await serper.search(q, k=5); if results and isinstance(results, dict) and "organic" in results...
                    
                    return data # Returning the full response dict
        except Exception:
            return []

# Deprecated function for backward compatibility if needed
async def search_google(query: str, k: int = 10, api_key: Optional[str] = None) -> List[Dict]:
    client = SerperClient(api_key)
    result = await client.search(query, k)
    if isinstance(result, dict):
        items = []
        for it in (result.get("organic") or []):
            items.append({"title": it.get("title"), "url": it.get("link"), "snippet": it.get("snippet")})
        return items
    return []
