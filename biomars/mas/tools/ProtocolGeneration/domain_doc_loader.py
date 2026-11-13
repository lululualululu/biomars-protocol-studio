import os
from typing import List, Dict, Any, Optional

def load_local_folder(folder: str, top_k: int = 20) -> List[Dict[str, Any]]:
    items = []
    if not folder or not os.path.isdir(folder):
        return items
    for root, _, files in os.walk(folder):
        for name in files:
            if name.lower().endswith((".txt",".md",".json")):
                p = os.path.join(root, name)
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        txt = f.read()
                    items.append({"title": name, "link": p, "snippet": txt[:600], "source": "local"})
                except:
                    pass
    return items[:top_k]


def _safe_read_text(path: str, max_chars: Optional[int] = None) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            if isinstance(max_chars, int) and max_chars > 0:
                return f.read(max_chars)
            return f.read()
    except Exception:
        return ""


async def load(path: str, top_k: int = 20) -> Dict[str, Any]:
    abs_path = os.path.abspath(path) if path else ""
    base = os.path.basename(abs_path) if abs_path else ""
    if not abs_path or not os.path.exists(abs_path):
        return {
            "doc_id": base or (path or "unknown"),
            "title": base or (path or "unknown"),
            "source": "missing",
            "content": "",
            "summary": None,
            "meta": {"path": abs_path, "exists": False},
        }

    if os.path.isdir(abs_path):
        items = load_local_folder(abs_path, top_k=top_k)
        parts: List[str] = []
        for it in items:
            title = (it.get("title") or "").strip()
            link = (it.get("link") or "").strip()
            snippet = (it.get("snippet") or "").strip()
            header = " - ".join([s for s in [title, link] if s])
            if header:
                parts.append(header)
            if snippet:
                parts.append(snippet)
            parts.append("")
        content = "\n".join(parts).strip()
        return {
            "doc_id": base or abs_path,
            "title": base or abs_path,
            "source": "local_folder",
            "content": content,
            "summary": f"Loaded {len(items)} files from folder",
            "meta": {"path": abs_path, "items": items},
        }

    content = _safe_read_text(abs_path)
    return {
        "doc_id": base or abs_path,
        "title": base or abs_path,
        "source": "local_file",
        "content": content,
        "summary": None,
        "meta": {"path": abs_path},
    }
