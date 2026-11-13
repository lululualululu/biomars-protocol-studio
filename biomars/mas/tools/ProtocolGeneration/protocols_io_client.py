import os
import logging
import httpx
import html
import re
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

class ProtocolsIOClient:
    """
    Client for interacting with Protocols.io API v3.
    """
    def __init__(self):
        self.base_url = os.getenv("PROTOCOLS_BASE_URL", "https://www.protocols.io/api/v3")
        # Ensure base_url doesn't end with slash
        self.base_url = self.base_url.rstrip("/")
        
        self.access_token = os.getenv("PROTOCOLS_CLIENT_ACCESS_TOKEN")
        
        if not self.access_token:
            logger.warning("PROTOCOLS_CLIENT_ACCESS_TOKEN not set. Protocols.io search will likely fail.")
            
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }

    async def search_protocols(self, query: str, page_id: int = 1) -> List[Dict[str, Any]]:
        """
        Search for public protocols.
        Note: The API endpoint structure for search might vary. 
        Using /protocols endpoint with keywords parameter based on typical API patterns.
        """
        if not self.access_token:
             logger.warning("Skipping protocols.io search: No access token provided.")
             return []

        url = f"{self.base_url}/protocols"
        params = {
            "key": query,
            "filter": "public", # Required param to avoid 400
            "page_id": page_id,
            "page_size": 20
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=self.headers, params=params, timeout=10.0)
                
                if response.status_code == 200:
                    data = response.json()
                    # Response structure typically has 'items' or similar
                    return data.get("items", [])
                else:
                    logger.warning(f"Protocols.io search failed: {response.status_code} - {response.text}")
                    return []
        except Exception as e:
            logger.error(f"Error searching protocols.io: {e}")
            return []

    async def get_protocol_details(self, protocol_id: int) -> Optional[Dict[str, Any]]:
        """
        Get full details for a protocol, including steps.
        """
        url = f"{self.base_url}/protocols/{protocol_id}"
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=self.headers, timeout=10.0)
                
                if response.status_code == 200:
                    data = response.json()
                    payload = data.get("payload", data)
                    if isinstance(payload, dict) and isinstance(payload.get("protocol"), dict):
                        return payload["protocol"]
                    return payload
                else:
                    logger.warning(f"Failed to fetch protocol {protocol_id}: {response.status_code}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching protocol details: {e}")
            return None

    def format_steps_to_markdown(self, steps: List[Dict[str, Any]]) -> str:
        """
        Convert structured steps into Markdown.
        """
        md_lines = ["## Steps"]
        
        for i, step in enumerate(steps, 1):
            # Extract description/body
            # Structure depends on API, usually 'description' or 'body'
            # Some steps might be sections
            
            section = step.get("section")
            if section:
                md_lines.append(f"\n### {section}")
                
            body = (
                step.get("description", "")
                or step.get("body", "")
                or step.get("step", "")
            )
            body = html.unescape(re.sub(r"<[^>]+>", " ", str(body)))
            body = " ".join(body.split())
            
            md_lines.append(f"{i}. {body}")
            
            # Components/reagents
            components = step.get("components", [])
            if components:
                component_names = []
                for component in components:
                    source = component.get("source") or {}
                    name = component.get("name") or source.get("title") or ""
                    if name:
                        component_names.append(str(name))
                if component_names:
                    md_lines.append("   * **Components:** " + ", ".join(component_names))
                
        return "\n".join(md_lines)
