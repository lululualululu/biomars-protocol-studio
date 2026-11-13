"""Load prior source artifacts and persist selected anchor documents."""

from typing import Any, Callable, Dict, List, Tuple, Optional
from logging import Logger
import json
import os

from ..protocol_schemas import DomainPack, EvidenceItem


def load_prior_anchor_candidate(
    task_query: str,
    *,
    find_prior_anchor_for_query: Any,
    normalize_doi: Any,
) -> Optional[EvidenceItem]:
    """Load a prior primary only for explicitly opted-in repeatability tests."""
    if os.getenv("PROTOCOL_ANCHOR_STABILITY", "0") != "1":
        return None
    anchors_root = os.getenv(
        "PROTOCOL_ANCHORS_ROOT",
        os.path.join("results", "AutoProtocol", "anchors"),
    )
    prior = find_prior_anchor_for_query(anchors_root, task_query)
    if not prior:
        return None

    entry = prior.get("entry") or {}
    evidence_id = str(entry.get("evidence_id") or "").strip()
    if not evidence_id:
        return None
    try:
        with open(prior["raw_fulltext_md_path"], encoding="utf-8", errors="replace") as handle:
            raw_fulltext = handle.read()
        with open(prior["methods_md_path"], encoding="utf-8", errors="replace") as handle:
            methods_text = handle.read()
    except (KeyError, OSError):
        return None

    external_ids = dict(entry.get("external_ids") or entry.get("externalIds") or {})
    doi = normalize_doi(entry.get("doi") or external_ids.get("DOI"))
    if doi:
        external_ids["DOI"] = doi
    meta = {
        "externalIds": external_ids,
        "doi": doi,
        "pmid": entry.get("pmid") or external_ids.get("PubMed") or "",
        "pmcid": entry.get("pmcid") or external_ids.get("PubMedCentral") or "",
        "snippet": methods_text[:1800],
        "fulltext_strategy": (
            "validated_historical_anchor_cache"
            if prior.get("cache_reusable")
            else entry.get("fulltext_strategy") or "unknown"
        ),
        "fulltext_quality": entry.get("fulltext_quality") or "UNKNOWN",
        "anchor_stability_preference": True,
        "anchor_stability_source_manifest": prior.get("manifest_path"),
        "anchor_stability_selection_count": prior.get("selection_count", 1),
    }
    return EvidenceItem(
        id=evidence_id,
        source=str(entry.get("source") or "academic_paper"),
        title=str(entry.get("title") or ""),
        authority_score=0.8,
        url=entry.get("url"),
        markdown=raw_fulltext,
        meta=meta,
    )


def save_anchor_artifacts(
    merged_items: List[EvidenceItem],
    anchor_ids: List[str],
    raw_fulltexts: Dict[str, str],
    domain_pack: DomainPack,
    session_id: str,
    active_retrieval_plan: Dict[str, Any],
    post_fulltext_contract_validation: Dict[str, Any],
    initial_contract_validation: Dict[str, Any],
    *,
    logger: Logger,
    normalize_protocol_query: Callable[..., str],
) -> Tuple[Dict[str, str], Dict[str, str], List[Dict[str, Any]], Dict[str, List[str]]]:
    anchor_methods_paths = {}
    anchor_raw_paths = {}
    manual_pdf_requests: List[Dict[str, Any]] = []
    analysis_markers_by_anchor: Dict[str, List[str]] = {}
    try:
        import json
        anchor_dir = os.path.join("results", "AutoProtocol", "anchors", session_id)
        os.makedirs(anchor_dir, exist_ok=True)

        manifest = []
        for item in merged_items:
            if item.id in anchor_ids:
                # Determine filename
                safe_filename = "".join(c for c in item.id if c.isalnum() or c in "_-") + ".md"
                methods_md_path = os.path.abspath(os.path.join(anchor_dir, safe_filename))
                base_name = safe_filename[:-3] if safe_filename.endswith(".md") else safe_filename
                raw_fulltext_md_path = os.path.abspath(os.path.join(anchor_dir, f"{base_name}__raw.md"))

                # Write raw markdown content (original fulltext)
                raw_fulltext_md = raw_fulltexts.get(item.id, item.markdown or "")
                with open(raw_fulltext_md_path, "w", encoding="utf-8") as f:
                    f.write(raw_fulltext_md)

                # Write methods-only markdown content
                with open(methods_md_path, "w", encoding="utf-8") as f:
                    f.write(item.markdown or "")

                anchor_methods_paths[item.id] = methods_md_path
                anchor_raw_paths[item.id] = raw_fulltext_md_path

                meta = item.meta or {}
                external_ids = dict(meta.get("externalIds") or {})
                doi = str(external_ids.get("DOI") or meta.get("doi") or "").strip()
                pmid = str(external_ids.get("PubMed") or meta.get("pmid") or "").strip()
                pmcid = str(
                    external_ids.get("PubMedCentral") or meta.get("pmcid") or ""
                ).strip()
                fulltext_strategy = meta.get("fulltext_strategy", "unknown")
                fulltext_quality = meta.get("fulltext_quality", "UNKNOWN")
                is_paper_like = (
                    str(item.id).startswith("sem_")
                    or str(item.id).startswith("pmc_")
                    or str(item.source).lower() in {"academic", "paper", "pmc", "semantic_scholar"}
                )
                needs_manual_pdf = is_paper_like and (
                    fulltext_strategy == "landing_page"
                    or "Abstract Only" in str(fulltext_quality)
                    or len(raw_fulltext_md or "") < 5000
                )
                if needs_manual_pdf:
                    expected_pdf_filename = f"{safe_filename[:-3] if safe_filename.endswith('.md') else safe_filename}.pdf"
                    active_contract_validation = (
                        post_fulltext_contract_validation
                        or initial_contract_validation
                        or {}
                    )
                    manual_pdf_requests.append(
                        {
                            "evidence_id": item.id,
                            "title": item.title,
                            "url": item.url,
                            "reason": f"fulltext_strategy={fulltext_strategy}, fulltext_quality={fulltext_quality}",
                            "expected_pdf_filename": expected_pdf_filename,
                            "doi": doi,
                            "pmid": pmid,
                            "pmcid": pmcid,
                            "semantic_contract": {
                                "central_operation": active_retrieval_plan.get("central_operation") or "",
                                "required_facets": (
                                    active_contract_validation.get("required_facets")
                                    or active_retrieval_plan.get("required_facets")
                                    or []
                                ),
                            },
                        }
                    )
                analysis_markers = meta.get("analysis_markers", []) if isinstance(meta, dict) else []
                if analysis_markers:
                    analysis_markers_by_anchor[item.id] = analysis_markers
                manifest.append({
                    "evidence_id": item.id,
                    "task_query": domain_pack.task.query,
                    "task_query_key": normalize_protocol_query(domain_pack.task.query),
                    "title": item.title,
                    "url": item.url,
                    "source": item.source,
                    "doi": doi,
                    "pmid": pmid,
                    "pmcid": pmcid,
                    "external_ids": external_ids,
                    "methods_md_path": methods_md_path,
                    "raw_fulltext_md_path": raw_fulltext_md_path,
                    "file_path": methods_md_path,
                    "raw_file_path": raw_fulltext_md_path,
                    "fulltext_strategy": fulltext_strategy,
                    "fulltext_quality": fulltext_quality,
                    "methods_extract_mode": meta.get("methods_extract_mode", "unknown"),
                    "analysis_markers": analysis_markers,
                    "anchor_selection": meta.get("anchor_selection"),
                    "anchor_query_focus": meta.get("anchor_query_focus"),
                    "anchor_selection_strategy": meta.get("anchor_selection_strategy"),
                    "anchor_selection_final_role": meta.get("anchor_selection_final_role"),
                    "anchor_selected": meta.get("anchor_selected", False),
                    "anchor_primary_selected": meta.get("anchor_primary_selected", False),
                })

        # Save manifest
        with open(os.path.join(anchor_dir, "anchors_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        logger.info(f"Saved {len(anchor_methods_paths)} anchor texts to {anchor_dir}")

    except Exception as e:
        logger.error(f"Failed to persist anchor texts: {e}")
    return anchor_methods_paths, anchor_raw_paths, manual_pdf_requests, analysis_markers_by_anchor
