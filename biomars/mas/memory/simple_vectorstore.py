from typing import List, Dict, Any
import json
import os
import logging

logger = logging.getLogger(__name__)

class SimpleVectorStore:
    def __init__(self):
        self.texts: List[str] = []
        self.metas: List[Dict[str,Any]] = []

    def add(self, items: List[Dict[str, Any]]):
        for it in items:
            txt = (it.get("title","") + " " + (it.get("snippet","") or "")).strip().lower()
            self.texts.append(txt)
            self.metas.append(it)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        if not self.texts:
            return []
        q = set(str(query).lower().split())
        scores = []
        for i, t in enumerate(self.texts):
            s = set(t.split())
            inter = len(q & s)
            denom = max(1, len(q))
            score = inter / denom
            scores.append((score, i))
        scores.sort(key=lambda x: -x[0])
        idx = [i for _, i in scores[:top_k]]
        out = []
        for i in idx:
            m = dict(self.metas[i])
            m["score"] = float([s for s in scores if s[1] == i][0][0])
            out.append(m)
        return out

    def save_to_file(self, filepath: str):
        """Save current store content to a JSON file."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            data = {
                "texts": self.texts,
                "metas": self.metas
            }
            
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                
            logger.info(f"VectorStore saved {len(self.texts)} items to {filepath}")
        except Exception as e:
            logger.error(f"Failed to save VectorStore to {filepath}: {e}")

    def load_from_file(self, filepath: str):
        """Load store content from a JSON file."""
        if not os.path.exists(filepath):
            logger.warning(f"VectorStore file not found: {filepath}")
            return
            
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            self.texts = data.get("texts", [])
            self.metas = data.get("metas", [])
            
            logger.info(f"VectorStore loaded {len(self.texts)} items from {filepath}")
        except Exception as e:
            logger.error(f"Failed to load VectorStore from {filepath}: {e}")
