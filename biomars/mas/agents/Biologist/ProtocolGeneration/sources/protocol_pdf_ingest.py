from biomars.mas.assets.protocol_generation.prompts.sources_protocol_pdf_ingest import LLM_RECOVERY_SYSTEM_17, SYSTEM_PROMPT_864
import json
import os
import re
import unicodedata
from math import ceil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .methods_extract_rules import extract_methods_region

from .....tools.ProtocolGeneration.pdf_conversion import (
    count_pdf_pages, extract_text_with_pymupdf, convert_pdf_to_markdown_v2,
)


LLM_RECOVERY_SYSTEM = (
    LLM_RECOVERY_SYSTEM_17
)

LLM_RECOVERY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section_id": {
                        "type": "string",
                        "enum": ["background", "materials", "procedure", "analysis"],
                    },
                    "present": {"type": "boolean"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["section_id", "present", "start_line", "end_line", "reason"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["sections"],
}

PDF_IDENTITY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["accept", "reject", "uncertain"],
        },
        "matched_facets": {
            "type": "array",
            "items": {"type": "string"},
        },
        "missing_facets": {
            "type": "array",
            "items": {"type": "string"},
        },
        "contradictions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recovered_scope": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": [
        "decision",
        "matched_facets",
        "missing_facets",
        "contradictions",
        "recovered_scope",
        "reason",
    ],
}

RECOVERY_SECTION_TITLES = {
    "background": "Background",
    "materials": "Materials and Reagents",
    "procedure": "Procedure",
    "analysis": "Data Analysis and Quality Control",
}
RECOVERY_SECTION_ORDER = ["background", "materials", "procedure", "analysis"]
RECOVERY_MIN_SECTION_CHARS = 40
RECOVERY_MAX_LINES = 1400
DEFAULT_MAX_PDF_PAGES = 30
PYMUPDF_MIN_TOTAL_TEXT_CHARS = 400
PYMUPDF_MIN_CHARS_PER_PAGE = 80
PYMUPDF_MIN_TEXT_PAGE_RATIO = 0.4
PYMUPDF_MAX_REPLACEMENT_CHAR_RATIO = 0.01
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_PDF_DOI_INDEX_CACHE: Dict[Tuple[str, int, int], Dict[str, List[str]]] = {}


class PdfPageLimitExceeded(ValueError):
    """Raised before extraction when a supplied PDF exceeds the ingest page limit."""

    def __init__(self, pdf_path: str, page_count: int, max_pages: int):
        self.pdf_path = str(pdf_path)
        self.page_count = int(page_count)
        self.max_pages = int(max_pages)
        super().__init__(
            f"PDF has {self.page_count} pages, exceeding the {self.max_pages}-page ingest limit: "
            f"{self.pdf_path}"
        )


def normalize_doi(value: Any) -> str:
    """Return a canonical DOI without URL/prefix decoration."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = DOI_PATTERN.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:").lower()


def normalize_protocol_query(value: Any) -> str:
    """Return a stable key for matching repeated protocol requests."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def _pdf_directory_signature(folder: Path) -> Tuple[str, int, int]:
    pdf_paths = list(folder.glob("*.pdf")) if folder.is_dir() else []
    try:
        directory_mtime = folder.stat().st_mtime_ns
    except OSError:
        directory_mtime = 0
    return str(folder), int(directory_mtime), len(pdf_paths)


def _build_pdf_doi_index(folder: Path) -> Dict[str, List[str]]:
    """Index DOI identifiers from PDF metadata and the first three pages."""
    import fitz

    signature = _pdf_directory_signature(folder)
    cached = _PDF_DOI_INDEX_CACHE.get(signature)
    if cached is not None:
        return cached

    index: Dict[str, List[str]] = {}
    for pdf_path in sorted(folder.glob("*.pdf")):
        try:
            document = fitz.open(str(pdf_path))
            try:
                metadata = document.metadata or {}
                snippets = [str(value) for value in metadata.values() if value]
                snippets.extend(
                    document[page_number].get_text("text") or ""
                    for page_number in range(min(3, document.page_count))
                )
            finally:
                document.close()
        except Exception:
            continue
        for match in DOI_PATTERN.finditer("\n".join(snippets)):
            doi = normalize_doi(match.group(0))
            if doi:
                index.setdefault(doi, []).append(str(pdf_path.resolve()))

    _PDF_DOI_INDEX_CACHE.clear()
    _PDF_DOI_INDEX_CACHE[signature] = index
    return index


def resolve_manual_pdf_path(
    manual_pdf_dir: str,
    request: Dict[str, Any],
) -> Optional[str]:
    """Resolve a requested PDF by filename aliases, then by exact DOI identity."""
    folder = Path(manual_pdf_dir).expanduser().resolve()
    if not folder.is_dir():
        return None

    filename_candidates = [request.get("expected_pdf_filename")]
    aliases = request.get("pdf_alias_filenames") or []
    if isinstance(aliases, list):
        filename_candidates.extend(aliases)
    for filename in filename_candidates:
        if not filename:
            continue
        candidate = folder / Path(str(filename)).name
        if candidate.is_file():
            return str(candidate.resolve())

    doi = normalize_doi(request.get("doi"))
    if not doi:
        return None
    matches = _build_pdf_doi_index(folder).get(doi) or []
    if not matches:
        return None
    return sorted(matches, key=lambda path: (len(Path(path).name), Path(path).name))[0]


def _manifest_entry_doi(entry: Dict[str, Any]) -> str:
    external_ids = entry.get("external_ids") or entry.get("externalIds") or {}
    if not isinstance(external_ids, dict):
        external_ids = {}
    return normalize_doi(entry.get("doi") or external_ids.get("DOI"))


def _manifest_content_path(manifest_path: Path, value: Any) -> Optional[Path]:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return resolved if resolved.is_file() else None


def _reusable_manifest_entry(
    manifest_path: Path,
    entry: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return reusable full-text paths for a validated manifest entry."""
    strategy = str(entry.get("fulltext_strategy") or "").lower()
    quality = str(entry.get("fulltext_quality") or "").upper()
    identity_decision = str(entry.get("pdf_identity_decision") or "").lower()
    if strategy == "landing_page" or "ABSTRACT" in quality:
        return None
    if quality in {"", "UNKNOWN", "LOW"} and identity_decision != "accept":
        return None
    if identity_decision == "reject":
        return None

    methods_path = _manifest_content_path(
        manifest_path,
        entry.get("methods_md_path") or entry.get("file_path"),
    )
    raw_path = _manifest_content_path(
        manifest_path,
        entry.get("raw_fulltext_md_path") or entry.get("raw_file_path"),
    )
    if not methods_path or not raw_path:
        return None
    try:
        if methods_path.stat().st_size < 200 or raw_path.stat().st_size < 5000:
            return None
    except OSError:
        return None
    return {
        "manifest_path": str(manifest_path.resolve()),
        "entry": entry,
        "methods_md_path": str(methods_path),
        "raw_fulltext_md_path": str(raw_path),
    }


def find_prior_anchor_for_query(
    anchors_root: str,
    task_query: str,
) -> Optional[Dict[str, Any]]:
    """Find the most repeatable reusable primary for the same normalized query."""
    query_key = normalize_protocol_query(task_query)
    root = Path(anchors_root).expanduser().resolve()
    if not query_key or not root.is_dir():
        return None

    focus_prefix = normalize_protocol_query("Direct executable protocol for:")
    manifests = sorted(
        root.glob("*/anchors_manifest.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    by_identity: Dict[str, Dict[str, Any]] = {}
    for manifest_path in manifests:
        try:
            entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("anchor_primary_selected"):
                continue
            entry_query = normalize_protocol_query(entry.get("task_query"))
            if not entry_query:
                focus = normalize_protocol_query(entry.get("anchor_query_focus"))
                if focus.startswith(focus_prefix):
                    entry_query = focus[len(focus_prefix):].strip()
            if entry_query != query_key:
                continue
            if str(entry.get("pdf_identity_decision") or "").lower() == "reject":
                continue
            reusable = _reusable_manifest_entry(manifest_path, entry)
            methods_path = _manifest_content_path(
                manifest_path,
                entry.get("methods_md_path") or entry.get("file_path"),
            )
            raw_path = _manifest_content_path(
                manifest_path,
                entry.get("raw_fulltext_md_path") or entry.get("raw_file_path"),
            )
            representative = {
                "manifest_path": str(manifest_path.resolve()),
                "entry": entry,
                "methods_md_path": str(methods_path) if methods_path else "",
                "raw_fulltext_md_path": str(raw_path) if raw_path else "",
                "cache_reusable": bool(reusable),
            }
            doi = _manifest_entry_doi(entry)
            normalized_title = re.sub(
                r"[^a-z0-9]+",
                " ",
                str(entry.get("title") or "").casefold(),
            ).strip()
            identity = f"doi:{doi}" if doi else f"title:{normalized_title}"
            if identity == "title:":
                identity = f"id:{entry.get('evidence_id') or ''}"
            record = by_identity.setdefault(
                identity,
                {
                    "count": 0,
                    "latest_mtime": -1,
                    "latest": representative,
                    "latest_reusable_mtime": -1,
                    "latest_reusable": None,
                },
            )
            record["count"] += 1
            manifest_mtime = manifest_path.stat().st_mtime_ns
            if manifest_mtime > record["latest_mtime"]:
                record["latest_mtime"] = manifest_mtime
                record["latest"] = representative
            if reusable and manifest_mtime > record["latest_reusable_mtime"]:
                reusable["cache_reusable"] = True
                record["latest_reusable_mtime"] = manifest_mtime
                record["latest_reusable"] = reusable
    if not by_identity:
        return None
    best = max(
        by_identity.values(),
        key=lambda record: (record["count"], record["latest_mtime"]),
    )
    result = dict(best["latest_reusable"] or best["latest"])
    result["query_key"] = query_key
    result["selection_count"] = best["count"]
    return result


def find_reusable_anchor_cache(
    anchors_root: str,
    request: Dict[str, Any],
    current_manifest_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find a prior, usable full-text cache for the exact ID or exact DOI."""
    root = Path(anchors_root).expanduser().resolve()
    if not root.is_dir():
        return None

    requested_id = str(request.get("evidence_id") or "").strip()
    requested_doi = normalize_doi(request.get("doi"))
    current_manifest = (
        Path(current_manifest_path).expanduser().resolve()
        if current_manifest_path
        else None
    )
    manifests = sorted(
        root.glob("*/anchors_manifest.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for manifest_path in manifests:
        if current_manifest and manifest_path.resolve() == current_manifest:
            continue
        try:
            entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("evidence_id") or "").strip()
            entry_doi = _manifest_entry_doi(entry)
            if requested_id and entry_id == requested_id:
                match_kind = "evidence_id"
            elif requested_doi and entry_doi and requested_doi == entry_doi:
                match_kind = "doi"
            else:
                continue

            reusable = _reusable_manifest_entry(manifest_path, entry)
            if not reusable:
                continue
            reusable["match_kind"] = match_kind
            return reusable
    return None






def assess_pymupdf_text_candidate(stats: Dict[str, Any]) -> Tuple[bool, str]:
    """Apply only parser-level viability checks; no topic or protocol keywords."""
    page_count = max(1, int(stats.get("page_count") or 0))
    text_pages = max(0, int(stats.get("text_page_count") or 0))
    compact_chars = max(0, int(stats.get("compact_char_count") or 0))
    replacement_chars = max(0, int(stats.get("replacement_char_count") or 0))

    required_chars = max(
        PYMUPDF_MIN_TOTAL_TEXT_CHARS,
        page_count * PYMUPDF_MIN_CHARS_PER_PAGE,
    )
    if compact_chars < required_chars:
        return False, "insufficient_text_density"
    if text_pages < max(1, ceil(page_count * PYMUPDF_MIN_TEXT_PAGE_RATIO)):
        return False, "too_few_text_pages"
    if replacement_chars / max(compact_chars, 1) > PYMUPDF_MAX_REPLACEMENT_CHAR_RATIO:
        return False, "excessive_replacement_characters"
    return True, "text_native_pdf"


def _write_pymupdf_candidate(
    pdf_path: str,
    ingest_dir: str,
) -> Tuple[str, Dict[str, Any]]:
    raw_text, stats = extract_text_with_pymupdf(pdf_path)
    output_path = os.path.join(
        ingest_dir,
        f"{Path(pdf_path).expanduser().resolve().stem}.final.md",
    )
    if raw_text.strip():
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(raw_text)
    return output_path, stats


def _resolve_max_pdf_pages(max_pages: Optional[int] = None) -> int:
    configured = max_pages
    if configured is None:
        configured = os.getenv("PROTOCOL_PDF_MAX_PAGES", str(DEFAULT_MAX_PDF_PAGES))
    try:
        resolved = int(configured)
    except (TypeError, ValueError):
        resolved = DEFAULT_MAX_PDF_PAGES
    return max(1, resolved)


def _safe_id(evidence_id: str) -> str:
    if not evidence_id:
        return "unknown"
    return "".join(c for c in str(evidence_id) if c.isalnum() or c in "_-")


def _resolve_recovery_model_config(
    global_config: Optional[Dict[str, Any]] = None,
    llm_model: Optional[str] = None,
) -> Dict[str, Any]:
    config = global_config or {}
    models_cfg = config.get("models", {}) or {}
    default_provider = str(models_cfg.get("default_provider") or "openrouter").strip()
    provider_cfg = models_cfg.get(default_provider) or {}
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}
    resolved = dict(provider_cfg)
    resolved["provider"] = default_provider
    resolved["default_provider"] = default_provider
    if llm_model:
        resolved["model_name"] = llm_model
    return resolved


def _build_recovery_prompt(numbered_doc: str, task_query: str = "") -> str:
    query_text = (task_query or "").strip()
    if query_text:
        query_text = f"Task query: {query_text}\n\n"
    return (
        "You are given flattened scientific text as numbered lines.\n\n"
        f"{query_text}"
        "Recover protocol-relevant source ranges from the supplied article window. "
        "Use these exact section_id values:\n"
        "- background\n"
        "- materials\n"
        "- procedure\n"
        "- analysis\n\n"
        "Interpretation rules:\n"
        "- background: brief experimental framing that is directly useful for executing the method.\n"
        "- materials: source-defined inputs, mixtures, reagents, buffers, equipment, and sample requirements.\n"
        "- procedure: executable operations, transitions, branches, repetitions, and timing-dependent actions.\n"
        "- analysis: QC, readouts, acceptance criteria, troubleshooting, and expected outcomes.\n\n"
        "Constraints:\n"
        "- Return every relevant contiguous range visible in this window.\n"
        "- The same section_id may appear more than once when relevant content is separated.\n"
        "- Prefer method sections and avoid abstract, references, acknowledgments, website boilerplate, figure legends, and discussion.\n"
        "- Procedure is mandatory if a runnable protocol is present.\n"
        "- A section may be omitted by setting present=false.\n"
        "- Ranges should be non-overlapping unless overlap is unavoidable.\n"
        "- Do not rewrite article text; only identify ranges.\n\n"
        "Return JSON only, with every required field in this schema:\n"
        f"{json.dumps(LLM_RECOVERY_SCHEMA, ensure_ascii=False)}\n\n"
        f"ARTICLE:\n{numbered_doc}"
    )


def _extract_json_object(text: str) -> Dict[str, Any]:
    payload = (text or "").strip()
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except Exception:
        pass
    start = payload.find("{")
    end = payload.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(payload[start : end + 1])
        except Exception:
            return {}
    return {}


def _coerce_recovery_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("sections"), list):
        return data

    sections: List[Dict[str, Any]] = []
    for section_id in RECOVERY_SECTION_ORDER:
        payload = data.get(section_id)
        if not isinstance(payload, dict):
            continue
        sections.append(
            {
                "section_id": section_id,
                "present": bool(payload.get("present")),
                "start_line": payload.get("start_line", 0),
                "end_line": payload.get("end_line", 0),
                "reason": str(payload.get("reason") or ""),
            }
        )
    if sections:
        coerced = dict(data)
        coerced["sections"] = sections
        return coerced
    return data


async def _generate_recovery_plan(
    normalized_text: str,
    global_config: Optional[Dict[str, Any]] = None,
    llm_model: Optional[str] = None,
    task_query: str = "",
    max_lines: int = RECOVERY_MAX_LINES,
) -> Dict[str, Any]:
    from biomars.mas.models.model_factory import ModelFactory

    lines = normalized_text.splitlines()
    if not lines:
        return {"sections": [], "notes": "empty_input"}

    model = ModelFactory.create_model(
        _resolve_recovery_model_config(global_config=global_config, llm_model=llm_model)
    )

    window_size = max(1, int(max_lines))
    overlap = min(80, window_size // 10)
    stride = max(1, window_size - overlap)
    window_starts = list(range(0, len(lines), stride))
    all_sections: List[Dict[str, Any]] = []
    notes: List[str] = []

    for window_start in window_starts:
        window_end = min(len(lines), window_start + window_size)
        numbered_doc = "\n".join(
            f"L{i + 1}: {lines[i]}"
            for i in range(window_start, window_end)
        )
        prompt = _build_recovery_prompt(
            numbered_doc=numbered_doc,
            task_query=task_query,
        )
        data = await model.generate_json(
            prompt=prompt,
            schema=LLM_RECOVERY_SCHEMA,
            system_prompt=LLM_RECOVERY_SYSTEM,
            temperature=0.1,
        )
        if not isinstance(data, dict) or not data.get("sections"):
            fallback_text = await model.generate(
                prompt=prompt,
                system_prompt=LLM_RECOVERY_SYSTEM,
                temperature=0.0,
            )
            repaired = _extract_json_object(fallback_text)
            if isinstance(repaired, dict) and repaired:
                data = repaired
        data = _coerce_recovery_payload(data if isinstance(data, dict) else {})
        if isinstance(data, dict):
            all_sections.extend(data.get("sections") or [])
            note = str(data.get("notes") or "").strip()
            if note:
                notes.append(note)
        if window_end >= len(lines):
            break

    return {
        "sections": all_sections,
        "notes": "; ".join(dict.fromkeys(notes)),
        "max_lines_used": min(window_size, len(lines)),
        "total_lines": len(lines),
        "window_count": len(window_starts),
    }


def _normalize_recovery_sections(
    sections: Any,
    total_lines: int,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen_ranges = set()
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or "").strip().lower()
        if section_id not in RECOVERY_SECTION_TITLES:
            continue
        present = bool(section.get("present"))
        if not present:
            continue
        try:
            start_line = int(section.get("start_line"))
            end_line = int(section.get("end_line"))
        except Exception:
            continue
        if total_lines <= 0:
            continue
        start_line = max(1, min(start_line, total_lines))
        end_line = max(start_line, min(end_line, total_lines))
        range_key = (section_id, start_line, end_line)
        if range_key in seen_ranges:
            continue
        normalized.append(
            {
                "section_id": section_id,
                "title": RECOVERY_SECTION_TITLES[section_id],
                "start_line": start_line,
                "end_line": end_line,
                "reason": str(section.get("reason") or "").strip(),
            }
        )
        seen_ranges.add(range_key)

    normalized.sort(key=lambda item: (item["start_line"], item["end_line"]))
    merged: List[Dict[str, Any]] = []
    for item in normalized:
        previous = next(
            (
                candidate
                for candidate in reversed(merged)
                if candidate["section_id"] == item["section_id"]
            ),
            None,
        )
        if previous and item["start_line"] <= previous["end_line"] + 1:
            previous["end_line"] = max(previous["end_line"], item["end_line"])
            if item["reason"] and item["reason"] not in previous["reason"]:
                previous["reason"] = "; ".join(
                    part for part in [previous["reason"], item["reason"]] if part
                )
            continue
        merged.append(item)
    return merged


def _build_recovered_markdown(normalized_text: str, sections: List[Dict[str, Any]]) -> str:
    lines = normalized_text.splitlines()
    blocks: List[str] = []
    for section_id in RECOVERY_SECTION_ORDER:
        section_items = sorted(
            (
                section
                for section in sections
                if section.get("section_id") == section_id
            ),
            key=lambda item: item["start_line"],
        )
        if not section_items:
            continue
        bodies: List[str] = []
        for item in section_items:
            start_idx = max(0, item["start_line"] - 1)
            end_idx = min(len(lines), item["end_line"])
            body = "\n".join(lines[start_idx:end_idx]).strip()
            if len(body) >= RECOVERY_MIN_SECTION_CHARS:
                bodies.append(body)
        if not bodies:
            continue
        blocks.append(
            f"# {RECOVERY_SECTION_TITLES[section_id]}\n\n"
            + "\n\n".join(bodies)
        )
    return "\n\n".join(blocks).strip() + ("\n" if blocks else "")


async def recover_methods_markdown_with_llm(
    raw_fulltext_md_path: str,
    methods_md_path: str,
    global_config: Optional[Dict[str, Any]] = None,
    llm_model: Optional[str] = None,
    task_query: str = "",
    max_lines: int = RECOVERY_MAX_LINES,
) -> Dict[str, Any]:
    from .methods_extract_rules import normalize_markdown

    report: Dict[str, Any] = {
        "success": False,
        "methods_extract_mode": "fallback_full",
        "report_path": "",
    }
    raw_path = Path(raw_fulltext_md_path).expanduser().resolve()
    methods_path = Path(methods_md_path).expanduser().resolve()
    report_path = methods_path.with_name(methods_path.stem + "__recovery.json")
    report["report_path"] = str(report_path)

    if not raw_path.exists():
        report["error"] = f"raw_fulltext_md_path_missing:{raw_path}"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
    normalized = normalize_markdown(raw_text)
    plan = await _generate_recovery_plan(
        normalized_text=normalized,
        global_config=global_config,
        llm_model=llm_model,
        task_query=task_query,
        max_lines=max_lines,
    )
    total_lines = int(plan.get("total_lines") or len(normalized.splitlines()) or 0)
    sections = _normalize_recovery_sections(plan.get("sections"), total_lines=total_lines)
    recovered_md = _build_recovered_markdown(normalized, sections)

    procedure_present = any(item.get("section_id") == "procedure" for item in sections)
    report.update(
        {
            "success": bool(recovered_md.strip() and procedure_present),
            "methods_extract_mode": "llm_block_recovery" if recovered_md.strip() and procedure_present else "fallback_full",
            "sections": sections,
            "notes": plan.get("notes") or "",
            "max_lines_used": plan.get("max_lines_used"),
            "total_lines": plan.get("total_lines"),
            "window_count": plan.get("window_count"),
        }
    )

    if report["success"]:
        methods_path.write_text(recovered_md, encoding="utf-8")
    else:
        report["error"] = "procedure_section_missing_or_empty"

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


async def verify_anchor_pdf_identity_with_llm(
    raw_fulltext_md_path: str,
    methods_md_path: str,
    expected_title: str,
    task_query: str,
    semantic_contract: Optional[Dict[str, Any]] = None,
    global_config: Optional[Dict[str, Any]] = None,
    llm_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify that recovered PDF methods belong to the selected anchor and query."""
    from biomars.mas.models.model_factory import ModelFactory

    methods_path = Path(methods_md_path).expanduser().resolve()
    raw_path = Path(raw_fulltext_md_path).expanduser().resolve()
    report_path = methods_path.with_name(methods_path.stem + "__identity.json")
    report: Dict[str, Any] = {
        "decision": "validation_unavailable",
        "matched_facets": [],
        "missing_facets": [],
        "contradictions": [],
        "recovered_scope": "",
        "reason": "",
        "report_path": str(report_path),
    }

    if not methods_path.exists() or not raw_path.exists():
        report["reason"] = "anchor_markdown_missing"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    methods_text = methods_path.read_text(encoding="utf-8", errors="replace")
    methods_excerpt = methods_text[:16000]
    raw_excerpt = raw_path.read_text(encoding="utf-8", errors="replace")[:5000]
    if not methods_excerpt.strip():
        report.update(
            {
                "decision": "reject",
                "reason": "recovered_methods_empty",
            }
        )
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    contract = semantic_contract if isinstance(semantic_contract, dict) else {}
    prompt = f"""
    Verify the identity and semantic scope of methods recovered from a manually supplied PDF.

    User request:
    {task_query}

    Selected anchor title:
    {expected_title}

    Semantic contract:
    {json.dumps(contract, ensure_ascii=False)}

    Beginning of recovered document:
    {raw_excerpt}

    Recovered methods content:
    {methods_excerpt}

    Decide whether the recovered methods content belongs to the selected anchor's requested
    workflow. A book, proceedings volume, or multi-chapter PDF may contain the selected title
    somewhere while the recovered methods come from another chapter; in that case reject.

    Evaluate semantic relationships, not isolated keyword overlap. Check the requested species,
    starting material, target product, central operation, essential conditions, and workflow role.
    Do not invent requirements absent from the request or semantic contract.

    Use:
    - accept: the recovered methods execute the requested workflow and do not contradict an
      essential facet;
    - reject: they clearly concern another chapter, biological system, starting material, target,
      or central operation;
    - uncertain: the supplied excerpts are insufficient, but contain no explicit contradiction.

    A relevant subprotocol can satisfy the request even if the article's main title is broader.
    State the supported cell type and workflow limits; do not generalize to unsupported systems.
    Missing essential procedures delegated to unavailable supplements warrant uncertain.
    Return JSON only, including every required field in this schema:
    {json.dumps(PDF_IDENTITY_SCHEMA, ensure_ascii=False)}
    Give a nonempty reason grounded in the supplied methods. For accept, provide matched_facets
    and recovered_scope. For reject, provide explicit contradictions.
    """

    try:
        model = ModelFactory.create_model(
            _resolve_recovery_model_config(
                global_config=global_config,
                llm_model=llm_model,
            )
        )
        system_prompt = (
            SYSTEM_PROMPT_864
        )

        def validate_response(response: Any) -> Dict[str, Any]:
            if isinstance(response, list) and len(response) == 1:
                response = response[0]
            if not isinstance(response, dict):
                raise ValueError(f"invalid_identity_response:{type(response).__name__}")
            missing_keys = [
                key for key in PDF_IDENTITY_SCHEMA["required"] if key not in response
            ]
            if missing_keys:
                raise ValueError("identity_required_fields_missing:" + ",".join(missing_keys))
            for key in ("matched_facets", "missing_facets", "contradictions"):
                if not isinstance(response[key], list) or any(
                    not isinstance(item, str) or not item.strip() for item in response[key]
                ):
                    raise ValueError(f"invalid_identity_field:{key}")
            for key in ("decision", "reason", "recovered_scope"):
                if not isinstance(response[key], str):
                    raise ValueError(f"invalid_identity_field:{key}")
            decision = response["decision"].strip().lower()
            if decision not in {"accept", "reject", "uncertain"} or not response["reason"].strip():
                raise ValueError("identity_decision_or_reason_invalid")
            # The schema permits an accepted document to note non-essential scope
            # gaps.  Only an explicit contradiction makes ``accept`` internally
            # inconsistent; the prompt already tells the model to use ``uncertain``
            # when an *essential* procedure is missing.
            if decision == "accept" and response["contradictions"]:
                raise ValueError("identity_accept_with_contradictory_evidence")
            if decision == "reject" and not response["contradictions"]:
                raise ValueError("identity_reject_without_contradictions")
            normalized = {key: response[key] for key in PDF_IDENTITY_SCHEMA["required"]}
            normalized["decision"] = decision
            return normalized

        async def request_verdict(current_prompt: str) -> Dict[str, Any]:
            response = await model.generate_json(
                prompt=current_prompt,
                schema=PDF_IDENTITY_SCHEMA,
                system_prompt=system_prompt,
                temperature=0.0,
            )
            try:
                return validate_response(response)
            except ValueError as structured_error:
                # Some OpenRouter models honor json_object mode but omit schema fields.
                # One free-form JSON repair call preserves the strict validator while
                # avoiding model-specific response-format branching in the adapter.
                repaired_text = await model.generate(
                    prompt=(
                        current_prompt
                        + "\nYour previous JSON was incomplete. Return one complete JSON object "
                        "matching the schema exactly; include no prose outside it."
                    ),
                    system_prompt=system_prompt,
                    temperature=0.0,
                )
                try:
                    return validate_response(_extract_json_object(repaired_text))
                except ValueError as repair_error:
                    raise ValueError(
                        f"{structured_error}; identity_repair_failed:{repair_error}"
                    ) from repair_error

        report["attempts"] = []
        for attempt in range(2):
            # Expand once on insufficient evidence; never approve by default.
            current_prompt = prompt
            if attempt:
                current_prompt = prompt.replace(methods_excerpt, methods_text[:96000], 1)
                current_prompt += "\nRecheck with the expanded evidence. Return the complete schema."
            try:
                response = await request_verdict(current_prompt)
                decision = response["decision"]
                report.update(response)
                report["decision"] = decision
                report["attempts"].append({"decision": decision, "reason": response["reason"]})
                if decision != "uncertain":
                    break
            except Exception as exc:
                report["attempts"].append({"decision": "validation_unavailable", "reason": str(exc)})
                # Retain an earlier valid uncertain verdict if a retry fails.
                if report["decision"] == "validation_unavailable":
                    report["reason"] = f"identity_validation_unavailable:{exc}"
    except Exception as exc:
        report["reason"] = f"identity_validation_unavailable:{exc}"

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report




def ingest_pdf_to_anchor(
    pdf_path: str,
    session_id: str,
    evidence_id: str,
    out_root: Optional[str] = None,
    llm_model: Optional[str] = None,
    max_pages: Optional[int] = None,
    prefer_pymupdf: bool = True,
) -> Dict[str, Any]:
    resolved_max_pages = _resolve_max_pdf_pages(max_pages)
    page_count = count_pdf_pages(pdf_path)
    if page_count > resolved_max_pages:
        raise PdfPageLimitExceeded(
            pdf_path=pdf_path,
            page_count=page_count,
            max_pages=resolved_max_pages,
        )

    out_root = out_root or os.path.join("results", "AutoProtocol", "pdf_ingest", session_id)
    os.makedirs(out_root, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ingest_dir = os.path.join(out_root, f"{_safe_id(evidence_id)}_{stamp}")
    os.makedirs(ingest_dir, exist_ok=True)

    pymupdf_stats: Dict[str, Any] = {}
    pymupdf_decision = "not_attempted"
    pymupdf_path = ""
    if prefer_pymupdf:
        try:
            pymupdf_path, pymupdf_stats = _write_pymupdf_candidate(
                pdf_path,
                ingest_dir,
            )
            pymupdf_usable, pymupdf_decision = assess_pymupdf_text_candidate(
                pymupdf_stats
            )
        except Exception as exc:
            pymupdf_usable = False
            pymupdf_decision = f"extraction_failed:{type(exc).__name__}"
        if pymupdf_usable:
            md_path = pymupdf_path
            fulltext_strategy = "pymupdf_text_first"
            fulltext_quality = "MEDIUM"

    if not (prefer_pymupdf and pymupdf_decision == "text_native_pdf"):
        try:
            md_path, fulltext_strategy, fulltext_quality = convert_pdf_to_markdown_v2(
                pdf_path=pdf_path,
                out_dir=ingest_dir,
                llm_model=llm_model,
            )
        except Exception:
            if not pymupdf_path:
                pymupdf_path, pymupdf_stats = _write_pymupdf_candidate(
                    pdf_path,
                    ingest_dir,
                )
                _, pymupdf_decision = assess_pymupdf_text_candidate(
                    pymupdf_stats
                )
            if not os.path.exists(pymupdf_path):
                raise
            with open(pymupdf_path, "r", encoding="utf-8") as handle:
                fallback_text = handle.read()
            if not fallback_text.strip():
                raise
            md_path = pymupdf_path
            fulltext_strategy = "pymupdf_text_fallback"
            fulltext_quality = "LOW" if pymupdf_decision != "text_native_pdf" else "MEDIUM"

    with open(md_path, "r", encoding="utf-8") as f:
        raw_fulltext_md = f.read()

    methods_md, methods_extract_mode = extract_methods_region(raw_fulltext_md)

    anchor_dir = os.path.join("results", "AutoProtocol", "anchors", session_id)
    os.makedirs(anchor_dir, exist_ok=True)

    safe_evidence = _safe_id(evidence_id)
    methods_md_path = os.path.abspath(os.path.join(anchor_dir, f"{safe_evidence}.md"))
    raw_fulltext_md_path = os.path.abspath(os.path.join(anchor_dir, f"{safe_evidence}__raw.md"))

    with open(raw_fulltext_md_path, "w", encoding="utf-8") as f:
        f.write(raw_fulltext_md)
    with open(methods_md_path, "w", encoding="utf-8") as f:
        f.write(methods_md)

    return {
        "methods_md_path": methods_md_path,
        "raw_fulltext_md_path": raw_fulltext_md_path,
        "fulltext_strategy": fulltext_strategy,
        "fulltext_quality": fulltext_quality,
        "methods_extract_mode": methods_extract_mode,
        "pdf_page_count": page_count,
        "pdf_max_pages": resolved_max_pages,
        "pymupdf_preferred": bool(prefer_pymupdf),
        "pymupdf_candidate_decision": pymupdf_decision,
        "pymupdf_candidate_stats": pymupdf_stats,
    }
