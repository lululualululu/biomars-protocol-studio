"""DomainPack document loading and model-response normalization.

The Agent owns model prompting and orchestration. Document loading delegates to
its existing external tool; this module keeps the domain-specific data conversion.
"""

import json
from typing import Any, Callable, Dict, List

from .protocol_schemas import DomainPackLLMContent, LoadedDoc


async def load_domain_docs(paths: List[str], *, loader: Any) -> List[LoadedDoc]:
    if not paths:
        return []

    docs: List[LoadedDoc] = []
    for path in paths:
        # Using domain_doc_loader tool
        loaded = await loader.load(path)

        # Map tool output to LoadedDoc model
        docs.append(
            LoadedDoc(
                doc_id=loaded.get("doc_id") or path,
                path=path,
                title=loaded.get("title"),
                source=loaded.get("source", "user"),
                content=loaded.get("content", ""),
                summary=loaded.get("summary"),
                meta=loaded.get("meta", {}),
            )
        )
    return docs


def normalize_domain_pack_response(
    response: Any,
    *,
    coerce_object_field: Callable[[Any], Dict[str, Any]],
) -> DomainPackLLMContent:
    data: Any = response
    if isinstance(response, str):
        clean_response = response.replace("```json", "").replace("```", "").strip()
        try:
            data = json.loads(clean_response)
        except json.JSONDecodeError as exc:
            raise ValueError("response is not valid JSON") from exc

    if not isinstance(data, dict):
        raise TypeError(
            "top-level JSON value must be an object, "
            f"got {type(data).__name__}"
        )

    required_fields = {
        "lab_profile",
        "constraints",
        "safety_rules",
        "env_capabilities",
    }
    missing_fields = sorted(required_fields.difference(data))
    if missing_fields:
        raise ValueError(
            "missing required fields: " + ", ".join(missing_fields)
        )

    data = dict(data)
    lab_profile = data.get("lab_profile")
    if isinstance(lab_profile, dict):
        data["lab_profile"] = json.dumps(lab_profile, ensure_ascii=False, indent=2)
    elif not isinstance(lab_profile, str):
        raise TypeError(
            "lab_profile must be a string or object, "
            f"got {type(lab_profile).__name__}"
        )

    # The model sometimes returns list/string payloads for object-typed fields.
    # Normalize them to dicts so downstream code can stay schema-stable.
    for field_name in ("constraints", "safety_rules", "env_capabilities"):
        data[field_name] = coerce_object_field(data.get(field_name))

    return DomainPackLLMContent(**data)


def coerce_object_field(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return {f"item_{idx + 1:02d}": item for idx, item in enumerate(value)}
        return {"items": value}
    if isinstance(value, str):
        return {"summary": value}
    return {"value": value}
