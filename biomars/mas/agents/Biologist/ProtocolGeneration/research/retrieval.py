"""Build bounded queries and gather candidate evidence with existing search tools."""

from typing import Any, Callable, Dict, List, Tuple, Optional
from logging import Logger
import asyncio
import json
import os
import re

from ..protocol_schemas import DomainPack, EvidenceItem
from .....tools.ProtocolGeneration.biomars_jina_client import BioMarsJinaClient
from .....tools.ProtocolGeneration.biomars_semantic_client import BioMarsSemanticClient


RETRIEVAL_RECOVERY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "central_operation": {"type": "string"},
        "required_facets": {
            "type": "array",
            "items": {"type": "string"},
        },
        "search_queries": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reason": {"type": "string"},
    },
    "required": ["central_operation", "required_facets", "search_queries", "reason"],
}


DOMAIN_KNOWLEDGE_BASE = {
    "Stem Cells & Organoids": [
        "stemcell.com", "thermofisher.com", "miltenyibiotec.com", "corning.com",
        "reprocell.com", "axolbio.com"
    ],
    "General Cell Culture": [
        "atcc.org", "dsmz.de", "sigmaaldrich.com", "thermofisher.com",
        "corning.com", "lonza.com", "promocell.com"
    ],
    "Molecular Biology (PCR, Cloning)": [
        "neb.com", "thermofisher.com", "promega.com", "qiagen.com",
        "takara-bio.com", "bio-rad.com"
    ],
    "Immunology & Antibodies": [
        "cellsignal.com", "abcam.com", "bio-rad.com", "miltenyibiotec.com",
        "bd.com", "rndsystems.com"
    ],
    "Microbiology": [
        "atcc.org", "dsmz.de", "thermofisher.com", "bd.com", "oxoid.com"
    ],
    "Protein Biochemistry": [
        "cytiva.com", "bio-rad.com", "thermofisher.com", "sigmaaldrich.com",
        "merckmillipore.com"
    ]
}


def build_core_query(domain_pack: DomainPack) -> str:
    """
        Construct a search-optimized query string from the DomainPack.
        Combines task query, species, cell line, and keywords.
        """
    base_query = domain_pack.task.query
    parts = [base_query]

    # Add species if not already in query
    if domain_pack.task.species and domain_pack.task.species.lower() not in base_query.lower():
        parts.append(domain_pack.task.species)

    # Add cell line if specific
    if domain_pack.task.cell_line:
        parts.append(domain_pack.task.cell_line)

    # Add protocol keywords if not present
    keywords = ["protocol", "method"]
    for kw in keywords:
        if kw not in base_query.lower():
            parts.append(kw)

    return " ".join(parts)


async def build_recovery_retrieval_plan(
    domain_pack: DomainPack,
    attempted_queries: List[str],
    *,
    call_model: Callable[..., Any],
    RETRIEVAL_RECOVERY_SCHEMA: Any,
    logger: Logger,
) -> Dict[str, Any]:
    """Build one bounded, domain-agnostic semantic retrieval contract."""
    attempted = [str(query).strip() for query in attempted_queries if str(query).strip()]
    prompt = f"""
        Plan a scientific protocol literature search before candidate selection.

        Original user request:
        {domain_pack.task.query}

        Species constraint: {domain_pack.task.species or 'not specified'}
        Cell-line constraint: {domain_pack.task.cell_line or 'not specified'}

        Queries already reserved or attempted:
        {attempted}

        Produce a semantic retrieval contract that is faithful to the original request.

        Requirements:
        1. Infer the central executable laboratory operation.
        2. List the minimum required semantic facets needed for a document to be a direct match.
        3. Produce exactly two complementary, concise literature-search queries when possible:
           - first: a precision query centered on the requested biological system and operation;
           - second: a recall query that keeps the system and operation but omits desired outcomes,
             quality metrics, and explanatory wording that need not appear in a paper title or abstract.
        4. Preserve explicitly requested species, biological system, target, method, and essential conditions.
        5. Do not invent a species, cell type, reagent, assay, or condition.
        6. Do not repeat an already attempted query and do not append generic boilerplate merely to lengthen it.
        7. Prefer terminology likely to appear in paper titles or abstracts. Do not require every
           acceptance criterion from the user request to appear in the search string; those facets
           will be enforced later by semantic anchor validation.
        8. Preserve the exact requested operation semantics and operation verbs. Do not replace
           isolation, establishment, generation, culture, maintenance, expansion, imaging, or
           measurement with a different laboratory operation. A lineage-conversion operation is
           allowed only when the request explicitly requires it or makes it unavoidable.
        9. Preserve the named target in the precision query. Use a broader or synonymous target in
           the recall query only when it is genuinely compatible, never as an automatic substitution.
        10. If the request applies a general platform method (for example a transposon, editing,
            imaging, or culture platform) to a named transgene, readout, or downstream application,
            the recall query MUST center on the platform plus the required biological system and
            omit application-specific transgenes, reporters, outcomes, and downstream lineages.
            Keep every omitted application detail as a required validation facet.
            Broadening retrieval must never broaden final acceptance. Do not replace the named
            platform with a neighboring delivery method such as viral transduction.
        11. Keep the recall query deliberately short: normally 2 to 4 biomedical terms, without
            prose, requested concentrations, timing, selection details, or validation readouts.
            Prefer a standard system acronym (for example hPSC or iPSC) when literature commonly
            uses it. When the named platform is the durable mechanism and a delivery reagent may
            appear only in Methods, keep the platform and system in the recall query and enforce
            the delivery reagent later as a required validation facet. PubMed combines excessive
            concepts restrictively, so a recall query must not simply restate the full request.

        Return JSON with exactly these keys:
        central_operation, required_facets, search_queries, reason.
        """
    try:
        response = await call_model(
            prompt=prompt,
            system_prompt=(
                "You are a biomedical literature-retrieval planner. "
                "Return only valid JSON matching the requested schema."
            ),
            schema=RETRIEVAL_RECOVERY_SCHEMA,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("LLM semantic retrieval planning failed: %s", exc)
        return {}

    if not isinstance(response, dict):
        logger.warning("LLM semantic retrieval plan had invalid type: %s", type(response).__name__)
        return {}

    attempted_keys = {" ".join(query.lower().split()) for query in attempted}
    search_queries: List[str] = []
    raw_queries = response.get("search_queries") or response.get("recovery_queries") or []

    # Some providers honor the requested content but serialize the two
    # queries as an object such as
    # {"precision_query": "...", "recall_query": "..."}. Iterating that
    # object directly searches the literal field names and destroys recall.
    # Normalize provider-shaped objects (including objects nested in a
    # list) to their string values before applying the existing bound.
    def _query_values(value: Any) -> List[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            ordered_values: List[Any] = []
            for key in ("precision_query", "recall_query", "query"):
                if key in value:
                    ordered_values.append(value.get(key))
            ordered_values.extend(
                item
                for key, item in value.items()
                if key not in {"precision_query", "recall_query", "query"}
            )
            flattened: List[str] = []
            for item in ordered_values:
                flattened.extend(_query_values(item))
            return flattened
        if isinstance(value, (list, tuple)):
            flattened = []
            for item in value:
                flattened.extend(_query_values(item))
            return flattened
        return []

    placeholder_queries = {"precision_query", "recall_query", "query"}
    def _new_queries(values: Any) -> List[str]:
        queries: List[str] = []
        for raw_query in _query_values(values):
            query = " ".join(str(raw_query).split()).strip()
            key = query.lower()
            if (
                not query
                or key in placeholder_queries
                or key in attempted_keys
                or key in {q.lower() for q in queries}
            ):
                continue
            queries.append(query)
            if len(queries) >= 2:
                break
        return queries

    search_queries = _new_queries(raw_queries)
    query_repair = None
    if not search_queries:
        query_repair = {"initial_queries": raw_queries, "outcome": "unavailable"}
        try:
            repaired = await call_model(
                prompt=f"""
                    {prompt}

                    The previous proposal produced no usable new query after deduplication:
                    {response}

                    Repair search_queries only. Every query must differ from the reserved or
                    attempted queries above after case and whitespace normalization. Use a
                    scientifically equivalent synonym or concise standard terminology, not
                    punctuation changes or filler words. Preserve the original central operation
                    and validation facets. Return JSON with search_queries only, at most two.
                    """,
                system_prompt="You repair scientific retrieval queries. Return only valid JSON.",
                schema={
                    "type": "object",
                    "properties": {
                        "search_queries": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["search_queries"],
                },
                temperature=0.1,
            )
            search_queries = _new_queries(
                repaired.get("search_queries") if isinstance(repaired, dict) else []
            )
            query_repair["outcome"] = "recovered" if search_queries else "no_new_query"
        except Exception as exc:
            logger.warning("Duplicate-query repair unavailable: %s", exc)

    required_facets = []
    for raw_facet in response.get("required_facets") or []:
        facet = " ".join(str(raw_facet).split()).strip()
        if facet and facet.lower() not in {x.lower() for x in required_facets}:
            required_facets.append(facet)

    # Models occasionally restate most of the request even when asked for a
    # broad recall query. PubMed then combines the surplus concepts
    # restrictively and can return zero results. Repair only that second
    # query once; this is bounded and does not relax downstream validation.
    if len(search_queries) >= 2 and len(search_queries[1].split()) > 4:
        proposed_recall = search_queries[1]
        compact_prompt = f"""
            Compress one biomedical literature recall query.

            Original request:
            {domain_pack.task.query}

            Central operation:
            {response.get('central_operation') or ''}

            Required facets (these remain enforced during candidate validation):
            {required_facets}

            Over-specific proposed recall query:
            {proposed_recall}

            Return a JSON object with one key, query. The query must contain at most
            4 whitespace-separated search terms. Keep only the general platform,
            biological system, and broad operation class. Prefer a standard acronym
            such as hPSC or iPSC. Omit delivery reagents and delivery suboperations
            such as transfection/electroporation, as well as transgenes, reporters,
            selection, concentrations, timing, outcomes, and downstream applications.
            Do not copy or guess an article title.
            """
        try:
            compact_response = await call_model(
                prompt=compact_prompt,
                system_prompt=(
                    "You compact biomedical database queries without changing "
                    "the validation contract. Return only valid JSON."
                ),
                schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                temperature=0.0,
            )
            if (
                isinstance(compact_response, list)
                and len(compact_response) == 1
                and isinstance(compact_response[0], dict)
            ):
                compact_response = compact_response[0]
            compact_query = " ".join(
                str(
                    compact_response.get("query")
                    if isinstance(compact_response, dict)
                    else ""
                ).split()
            ).strip()
            compact_key = compact_query.lower()
            if (
                compact_query
                and len(compact_query.split()) <= 4
                and compact_key not in attempted_keys
                and compact_key != search_queries[0].lower()
            ):
                logger.info(
                    "Compacted over-specific recall query from %r to %r",
                    proposed_recall,
                    compact_query,
                )
                search_queries[1] = compact_query
            else:
                logger.warning(
                    "Recall-query compaction returned an invalid query; keeping "
                    "the original bounded retrieval plan."
                )
        except Exception as exc:
            logger.warning("Recall-query compaction unavailable: %s", exc)

    if len(search_queries) >= 2:
        # These modifiers remain mandatory validation facets, but retaining
        # them in PubMed's broad query frequently converts recall into an
        # accidental all-terms match. Normalize even a short model response;
        # a four-token query can still be over-constrained.
        recall_only_qualifiers = {
            "antibiotic",
            "delivery",
            "electroporation",
            "lipofection",
            "non-viral",
            "nonviral",
            "protocol",
            "selection",
            "transfection",
        }
        proposed_recall = search_queries[1]
        normalized_recall = " ".join(
            token
            for token in proposed_recall.split()
            if token.lower().strip(",.;:()") not in recall_only_qualifiers
        ).strip()
        normalized_key = normalized_recall.lower()
        if (
            normalized_recall
            and normalized_key not in attempted_keys
            and normalized_key != search_queries[0].lower()
            and normalized_recall != proposed_recall
        ):
            logger.info(
                "Removed validation-only qualifiers from recall query %r -> %r",
                proposed_recall,
                normalized_recall,
            )
            search_queries[1] = normalized_recall

    return {
        "central_operation": " ".join(str(response.get("central_operation") or "").split()),
        "required_facets": required_facets,
        "search_queries": search_queries,
        "reason": str(response.get("reason") or "").strip(),
        **({"query_repair": query_repair} if query_repair else {}),
    }


async def search_planned_academic_queries(
    domain_pack: DomainPack,
    retrieval_plan: Optional[Dict[str, Any]],
    k: int,
    *,
    search_academic_sources: Callable[..., Any],
    BioMarsJinaClient: Any,
    BioMarsSemanticClient: Any,
    logger: Logger,
) -> Tuple[List[EvidenceItem], List[str]]:
    """Run at most two LLM-planned academic queries serially."""
    queries = list((retrieval_plan or {}).get("search_queries") or [])[:2]
    if not queries:
        return [], []

    items: List[EvidenceItem] = []
    attempted: List[str] = []
    academic_k = max(10, int(k * 0.5))
    async with BioMarsJinaClient() as jina, BioMarsSemanticClient() as sem:
        for query in queries:
            attempted.append(query)
            try:
                items.extend(
                    await search_academic_sources(
                        domain_pack,
                        query,
                        academic_k,
                        sem,
                        jina,
                        force_pubmed_supplement=True,
                    )
                )
            except Exception as exc:
                logger.warning("Planned academic query failed for '%s': %s", query, exc)
    return items, attempted


async def recover_missing_anchors_with_llm(
    initial_items: List[EvidenceItem],
    initial_merged_items: List[EvidenceItem],
    domain_pack: DomainPack,
    k: int,
    attempted_queries: List[str],
    *,
    apply_contract_anchor_metadata: Callable[..., Any],
    build_recovery_retrieval_plan: Callable[..., Any],
    merge_and_rank: Callable[..., Any],
    search_planned_academic_queries: Callable[..., Any],
    validate_anchor_contract_with_llm: Callable[..., Any],
) -> Tuple[List[EvidenceItem], List[str], Dict[str, Any]]:
    """Run at most one LLM-planned academic recovery round after anchor loss."""
    trace: Dict[str, Any] = {
        "triggered": True,
        "reason": "initial_anchor_missing",
        "attempted_queries": list(attempted_queries),
        "initial_item_count": len(initial_items),
        "initial_merged_item_count": len(initial_merged_items),
    }
    recovery_plan = await build_recovery_retrieval_plan(domain_pack, attempted_queries)
    trace["plan"] = recovery_plan
    recovery_queries = recovery_plan.get("search_queries") or []
    if not recovery_queries:
        trace["outcome"] = "no_valid_recovery_query"
        return initial_merged_items, [], trace

    recovered_items, recovery_attempts = await search_planned_academic_queries(
        domain_pack,
        recovery_plan,
        k=k,
    )
    trace["attempted_queries"].extend(recovery_attempts)

    trace["recovered_item_count"] = len(recovered_items)
    if not recovered_items:
        trace["outcome"] = "no_recovered_items"
        return initial_merged_items, [], trace

    recovery_validation: Dict[str, Any] = {}
    recovered_merged, candidate_anchor_ids = await merge_and_rank(
        list(initial_items) + recovered_items,
        domain_pack=domain_pack,
        k=k,
        retrieval_plan=recovery_plan,
        contract_audit=recovery_validation,
    )

    merged_ids = {
        str(item.id) for item in recovered_merged if getattr(item, "id", None)
    }
    merged_id_by_title = {
        re.sub(r"[^a-z0-9]+", " ", (item.title or "").lower()).strip(): str(item.id)
        for item in recovered_merged
        if getattr(item, "id", None) and (item.title or "").strip()
    }
    validation_candidate_ids: List[str] = []
    for evidence_id in candidate_anchor_ids:
        evidence_id = str(evidence_id)
        if evidence_id in merged_ids and evidence_id not in validation_candidate_ids:
            validation_candidate_ids.append(evidence_id)
    for item in recovered_items:
        evidence_id = str(getattr(item, "id", "") or "")
        if evidence_id not in merged_ids:
            normalized_title = re.sub(
                r"[^a-z0-9]+",
                " ",
                (getattr(item, "title", "") or "").lower(),
            ).strip()
            evidence_id = merged_id_by_title.get(normalized_title, "")
        if evidence_id and evidence_id not in validation_candidate_ids:
            validation_candidate_ids.append(evidence_id)

    if recovery_validation:
        accepted_ids = candidate_anchor_ids
        validation = recovery_validation
        audited_ids = validation.get("candidate_ids") or []
        if isinstance(audited_ids, list):
            validation_candidate_ids = [str(value) for value in audited_ids]
    else:
        accepted_ids, validation = await validate_anchor_contract_with_llm(
            recovered_merged,
            validation_candidate_ids,
            domain_pack,
            recovery_plan,
        )
    trace["candidate_anchor_ids"] = candidate_anchor_ids
    trace["validation_candidate_ids"] = validation_candidate_ids
    trace["validation"] = validation
    decision = str(validation.get("decision") or "")
    if accepted_ids and decision in {"accept", "needs_fulltext"}:
        apply_contract_anchor_metadata(
            recovered_merged,
            accepted_ids,
            validation,
            domain_pack,
        )
    if accepted_ids and decision == "needs_fulltext":
        trace["outcome"] = "anchor_recovered_pending_fulltext"
    elif accepted_ids:
        trace["outcome"] = "anchor_recovered"
    elif decision == "validation_unavailable":
        trace["outcome"] = "recovered_validation_unavailable"
    else:
        trace["outcome"] = "recovered_candidates_rejected"
    return recovered_merged, accepted_ids, trace


async def route_domains_with_llm(
    query: str,
    *,
    call_model: Callable[..., Any],
    agent_model: Any,
    DOMAIN_KNOWLEDGE_BASE: Any,
    logger: Logger,
) -> List[str]:
    """
        Dynamically select relevant supplier domains based on the user query.
        """
    import json
    # 1. Construct prompt
    categories = list(DOMAIN_KNOWLEDGE_BASE.keys())
    prompt = f"""
        You are a biological domain expert. Your task is to route a user's query to the most relevant supplier categories.
        
        User Query: "{query}"
        
        Available Categories:
        {json.dumps(categories, indent=2)}
        
        Task:
        1. Analyze the query to understand the method/technique (e.g., "Western Blot", "PCR", "Organoid Culture").
        2. Select the top 1-2 most relevant categories from the list.
        3. Return the corresponding domains as a flat list of strings.
        
        Output Format:
        Return a JSON object with a single key "domains" containing the list of domain strings.
        Example: {{"domains": ["neb.com", "thermofisher.com"]}}
        """

    # 2. Call LLM
    try:
        # Assuming self.model.generate_json or _call_model
        system_prompt = "You are a domain router."

        if hasattr(agent_model, "generate_json"):
             response = await agent_model.generate_json(
                 prompt=prompt,
                 schema={"type": "object", "properties": {"domains": {"type": "array", "items": {"type": "string"}}}}
             )
             if isinstance(response, dict):
                 return response.get("domains", [])

        # Fallback
        response_text = await call_model(
            prompt=prompt,
            system_prompt=system_prompt,
        )
        # Simple parsing
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start >= 0:
            data = json.loads(response_text[start:end])
            return data.get("domains", [])

    except Exception as e:
        logger.warning(f"Domain routing failed: {e}")

    # Fallback to general cell culture + molecular biology if LLM fails
    return DOMAIN_KNOWLEDGE_BASE["General Cell Culture"] + DOMAIN_KNOWLEDGE_BASE["Molecular Biology (PCR, Cloning)"]


async def search_official_sources(
    domain_pack: DomainPack,
    query: str,
    k_official: int,
    jina: BioMarsJinaClient,
    *,
    route_domains_with_llm: Callable[..., Any],
    logger: Logger,
) -> List[EvidenceItem]:
    """
        Search official sources using dynamic domain routing.
        """
    logger.info(f"Searching official sources (k={k_official})...")

    # 1. Dynamic Domain Routing
    target_domains = await route_domains_with_llm(query)
    if not target_domains:
         # Fallback
         target_domains = ["thermofisher.com", "sigmaaldrich.com"]

    logger.info(f"Routed to domains: {target_domains}")

    # 2. Construct Queries
    # Chunk domains to avoid query length limits (e.g. 4 domains per query)
    chunk_size = 4
    queries = []

    # Core search terms
    search_terms = f'"{query}" protocol method "user guide" filetype:pdf'

    for i in range(0, len(target_domains), chunk_size):
        chunk = target_domains[i:i+chunk_size]
        site_operator = " OR ".join([f"site:{d}" for d in chunk])
        q = f"{search_terms} ({site_operator})"
        queries.append(q)

    # Add specific query for high-value domains if present
    if "thermofisher.com" in target_domains:
         queries.append(f'"{query}" site:thermofisher.com protocol')

    # 3. Execute searches via Serper
    from .....tools.ProtocolGeneration.serper_client import SerperClient
    serper = SerperClient()

    all_links = []
    for q in queries:
         try:
             results = await serper.search(q, k=5)
             if results and isinstance(results, dict) and "organic" in results:
                all_links.extend(results["organic"])
             elif results and isinstance(results, list):
                all_links.extend(results)
         except Exception as e:
             logger.warning(f"Serper search failed: {e}")

    # 4. Filter by target_domains
    filtered_links = []
    for link_obj in all_links:
        url = link_obj.get("link", "")
        if any(d in url for d in target_domains):
            filtered_links.append(link_obj)

    # Deduplicate
    unique_links = {obj["link"]: obj for obj in filtered_links}.values()
    target_links = list(unique_links)[:k_official]

    # 5. Fetch content via Jina Reader
    evidence_items = []
    for i, link_obj in enumerate(target_links):
        url = link_obj.get("link")
        title = link_obj.get("title", "Unknown Title")
        snippet = link_obj.get("snippet", "")

        try:
            # Fetch markdown
            markdown_content = await jina.fetch_markdown(url)

            if not markdown_content or len(markdown_content) < 100:
                logger.warning(f"Skipping empty/short content from {url}")
                continue

            # Create EvidenceItem
            item = EvidenceItem(
                id=f"official_{i}_{abs(hash(url))}",
                source="official",
                title=title,
                url=url,
                authority_score=0.95, # High score for official sources
                markdown=markdown_content,
                tags=["official", "datasheet", "protocol"],
                meta={
                    "domain": url.split("/")[2] if len(url.split("/")) > 2 else "unknown",
                    "query_used": query,
                    "snippet": snippet
                }
            )
            evidence_items.append(item)

        except Exception as e:
            logger.warning(f"Failed to fetch content from {url}: {e}")

    return evidence_items


async def search_academic_sources(
    domain_pack: DomainPack,
    query: str,
    k_academic: int,
    sem: BioMarsSemanticClient,
    jina: BioMarsJinaClient,
    force_pubmed_supplement: bool = False,
    *,
    extract_anchor_query_terms: Callable[..., Any],
    search_by_semantic_only: Callable[..., Any],
    ANCHOR_PROTOCOL_TERMS: Any,
    logger: Logger,
) -> List[EvidenceItem]:
    """
        Search academic literature (PubMed AND Semantic Scholar) for high-impact Method papers.
        Uses a Hybrid Search Strategy to maximize coverage.
        """
    import re
    logger.info(f"Searching academic sources (k={k_academic}) via S2-first Strategy (S2 primary + PubMed supplement)...")

    # 1. Query Cleaning
    safe_query = re.sub(r'[^\w\s]', ' ', query)
    safe_query = re.sub(r'\s+', ' ', safe_query).strip()
    if len(safe_query) > 300:
         safe_query = safe_query[:300]

    # PubMed Query Construction
    pm_query = f"({safe_query}) AND (methods[tiab] OR protocol[tiab])"
    if domain_pack.task.species:
        species = domain_pack.task.species.strip()
        pm_query += f" AND {species}[tiab]"

    min_s2_candidates = int(os.getenv("S2_PRIMARY_MIN_CANDIDATES", str(max(12, k_academic // 2))))
    pubmed_enrich_limit = int(os.getenv("S2_PUBMED_ENRICH_LIMIT", "5"))

    # Task 1: PubMed Search + limited S2 hydration
    async def run_pubmed(remaining_needed: int):
        enrich_limit = max(0, min(pubmed_enrich_limit, remaining_needed))
        if enrich_limit <= 0:
            return []
        try:
            from .....tools.ProtocolGeneration.ncbi_client import NCBIClient
            ncbi = NCBIClient()
            pmids = ncbi.search_pubmed(pm_query, top_k=max(enrich_limit * 4, 8))
            raw_query = (domain_pack.task.query or "").strip()
            if raw_query and len(pmids) < enrich_limit:
                raw_pm_query = f"({raw_query}) AND (methods[tiab] OR protocol[tiab])"
                raw_pmids = ncbi.search_pubmed(raw_pm_query, top_k=max(enrich_limit * 4, 8))
                for pmid in raw_pmids:
                    if pmid not in pmids:
                        pmids.append(pmid)
            if not pmids:
                return []
            meta_rows = ncbi.fetch_pubmed_meta(pmids[:max(enrich_limit * 4, 8)])
            meta_by_pmid = {str(row.get("pmid")): row for row in meta_rows if row.get("pmid")}
            query_terms = set(extract_anchor_query_terms(domain_pack.task.query))

            def _pmid_rank_score(pmid: str) -> float:
                row = meta_by_pmid.get(str(pmid)) or {}
                title_lower = str(row.get("title") or "").lower()
                title_term_weight = sum(len(term) for term in query_terms if term in title_lower)
                protocol_hits = sum(1 for term in ANCHOR_PROTOCOL_TERMS if term in title_lower)
                return (0.25 * title_term_weight) + (1.5 * protocol_hits)

            ranked_pmids = sorted(
                pmids,
                key=lambda pmid: (_pmid_rank_score(str(pmid)), -pmids.index(pmid)),
                reverse=True,
            )

            selected_pmids = [str(pmid) for pmid in ranked_pmids[:enrich_limit]]
            details_by_pmid: Dict[str, Dict[str, Any]] = {}
            batch_getter = getattr(sem, "get_papers_batch", None)
            batch_supported = callable(batch_getter)
            if batch_supported:
                try:
                    batch_rows = await batch_getter(
                        [f"PMID:{pmid}" for pmid in selected_pmids]
                    )
                    for pmid, details in zip(selected_pmids, batch_rows or []):
                        if isinstance(details, dict):
                            details_by_pmid[pmid] = details
                except Exception as e:
                    logger.warning(f"S2 batch lookup failed for PubMed shortlist: {e}")

            missing_details = [
                pmid for pmid in selected_pmids if pmid not in details_by_pmid
            ]
            abstracts_by_pmid: Dict[str, str] = {}
            abstract_fetcher = getattr(ncbi, "fetch_pubmed_abstracts", None)
            if missing_details and callable(abstract_fetcher):
                try:
                    abstracts_by_pmid = await asyncio.to_thread(
                        abstract_fetcher,
                        missing_details,
                    )
                except Exception as e:
                    logger.warning(f"PubMed abstract fallback failed: {e}")

            papers = []
            for pmid in selected_pmids:
                details = details_by_pmid.get(pmid)
                if not batch_supported:
                    try:
                        details = await sem.get_paper(f"PMID:{pmid}")
                    except Exception as e:
                        logger.warning(f"S2 lookup failed for PMID {pmid}: {e}")
                if details:
                    papers.append(details)
                    continue
                meta = meta_by_pmid.get(str(pmid))
                if not meta:
                    continue
                pubdate = str(meta.get("pubdate") or "")
                year_match = re.search(r"(19|20)\d{2}", pubdate)
                doi = str(meta.get("doi") or "").strip()
                external_ids: Dict[str, str] = {"PubMed": str(pmid)}
                if doi:
                    external_ids["DOI"] = doi
                papers.append(
                    {
                        "paperId": f"PMID:{pmid}",
                        "title": meta.get("title"),
                        "abstract": abstracts_by_pmid.get(pmid) or meta.get("title") or "",
                        "year": int(year_match.group(0)) if year_match else None,
                        "citationCount": 5,
                        "url": meta.get("link"),
                        "openAccessPdf": {},
                        "authors": [],
                        "venue": meta.get("journal"),
                        "externalIds": external_ids,
                        "publicationDate": pubdate,
                    }
                )
            return papers
        except Exception as e:
            logger.warning(f"PubMed search failed: {e}")
            return []

    # Task 2: Semantic Scholar Direct Search
    async def run_semantic():
        try:
            # Use search_papers directly
            # Request 2x candidates to allow for merging and filtering
            # CRITICAL: Request externalIds explicitly
            return await sem.search_papers(
                safe_query,
                limit=k_academic * 2,
                fields="paperId,title,abstract,year,citationCount,url,openAccessPdf,authors,venue,externalIds,publicationDate,publicationTypes,journal"
            )
        except Exception as e:
            logger.warning(f"Semantic Scholar search failed: {e}")
            return []

    semantic_papers = await run_semantic()
    pubmed_papers = []
    if force_pubmed_supplement or len(semantic_papers or []) < min_s2_candidates:
        remaining_needed = (
            max(pubmed_enrich_limit, 1)
            if force_pubmed_supplement
            else max(min_s2_candidates - len(semantic_papers or []), 1)
        )
        logger.info(
            "S2 returned %d candidates (threshold %d; forced supplement=%s). "
            "Supplementing with up to %d PubMed->S2 hydrations.",
            len(semantic_papers or []),
            min_s2_candidates,
            force_pubmed_supplement,
            min(pubmed_enrich_limit, remaining_needed),
        )
        pubmed_papers = await run_pubmed(remaining_needed)
    else:
        logger.info(
            "S2 returned %d candidates, meeting threshold %d. "
            "Skipping PubMed->S2 hydration path.",
            len(semantic_papers or []),
            min_s2_candidates,
        )

    # Merge and Deduplicate
    # Prefer PubMed-sourced items if overlap? Actually S2 data structure is same.
    # Dedupe by paperId.
    all_papers = (pubmed_papers or []) + (semantic_papers or [])
    unique_papers = {}
    for p in all_papers:
        pid = p.get('paperId')
        if pid and pid not in unique_papers:
            unique_papers[pid] = p

    candidate_papers = list(unique_papers.values())
    logger.info(f"Total unique academic papers found: {len(candidate_papers)}")

    # 4. Filter and Score
    evidence_items = []

    # Preserve source relevance order. Citation count is retained as an authority
    # feature downstream, but must not erase a recent, directly matching protocol.
    for retrieval_rank, paper in enumerate(candidate_papers[:k_academic], start=1):
        citations = paper.get("citationCount", 0) or 0

        # Authority score calculation
        score = min(1.0, 0.4 + 0.006 * citations)

        source_url = paper.get("url") or paper.get("openAccessPdf", {}).get("url")

        # Use abstract as markdown initially
        abstract = paper.get("abstract") or "No abstract available."
        markdown_content = f"# {paper.get('title')}\n\n## Abstract\n{abstract}\n\n[Full Text Link]({source_url})"

        # Consistent ID generation: sem_{paperId}
        # (Previously pmc_ for pubmed, but now unified as they are all S2 paper objects)
        # To allow downstream to know it came from a paper source.
        # We use 'sem_' prefix for all academic papers now for consistency,
        # or 'pmc_' if we want to preserve old behavior.
        # The prompt asked to reuse EvidenceItem creation logic, e.g. f"sem_{paperId}"

        item = EvidenceItem(
            id=f"sem_{paper.get('paperId')}",
            source="academic_paper", # unified source type
            title=paper.get("title"),
            year=paper.get("year"),
            citation_count=citations,
            authority_score=score,
            url=source_url,
            markdown=markdown_content,
            tags=["paper", "methods", "academic"],
            meta={
                "paperId": paper.get("paperId"),
                "externalIds": paper.get("externalIds", {}),
                "doi": paper.get("externalIds", {}).get("DOI"),
                "publicationDate": paper.get("publicationDate"),
                "venue": paper.get("venue"),
                "publicationTypes": paper.get("publicationTypes") or [],
                "journal": paper.get("journal") or {},
                "openAccessPdf": paper.get("openAccessPdf") or {},
                "authors": [a.get("name") for a in paper.get("authors", [])[:5]],
                "snippet": paper.get("abstract"),
                "retrieval_rank": retrieval_rank,
            }
        )
        evidence_items.append(item)

    if evidence_items:
        return evidence_items

    fallback_queries: List[str] = []
    for candidate_query in (domain_pack.task.query, safe_query):
        candidate_query = (candidate_query or "").strip()
        if candidate_query and candidate_query not in fallback_queries:
            fallback_queries.append(candidate_query)

    for fallback_query in fallback_queries:
        if fallback_query == safe_query:
            continue
        logger.warning(
            "Academic search returned no filtered evidence for query '%s'. "
            "Trying Semantic Scholar fallback with query '%s'.",
            query,
            fallback_query,
        )
        fallback_items = await search_by_semantic_only(fallback_query, k_academic, sem, jina)
        if fallback_items:
            return fallback_items

    return evidence_items


async def search_by_semantic_only(
    raw_query: str,
    k_academic: int,
    sem: BioMarsSemanticClient,
    jina: BioMarsJinaClient,
    *,
    logger: Logger,
) -> List[EvidenceItem]:
    """
        Fallback method: Search directly via Semantic Scholar if PubMed fails.
        """
    logger.info(f"Fallback: Searching Semantic Scholar directly for '{raw_query}'")

    try:
        # Use search_papers from BioMarsSemanticClient
        papers = await sem.search_papers(raw_query, limit=k_academic * 3)

        if not papers:
            return []

        # Filter valid papers
        candidate_papers = [p for p in papers if p.get("paperId")]

        evidence_items = []
        for retrieval_rank, paper in enumerate(candidate_papers[:k_academic], start=1):
            citations = paper.get("citationCount", 0) or 0

            score = min(1.0, 0.4 + 0.006 * citations)

            source_url = paper.get("url") or paper.get("openAccessPdf", {}).get("url")

            # Use abstract as markdown initially (No Jina Fetch)
            abstract = paper.get("abstract") or "No abstract available."
            markdown_content = f"# {paper.get('title')}\n\n## Abstract\n{abstract}\n\n[Full Text Link]({source_url})"

            item = EvidenceItem(
                id=f"sem_{paper.get('paperId')}",
                source="semantic_scholar_fallback", # distinct source
                title=paper.get("title"),
                year=paper.get("year"),
                citation_count=citations,
                authority_score=score,
                url=source_url,
                markdown=markdown_content,
                tags=["paper", "methods", "academic", "fallback"],
                meta={
                    "paperId": paper.get("paperId"),
                    "authors": [a.get("name") for a in paper.get("authors", [])[:3]],
                    "externalIds": paper.get("externalIds", {}) or {},
                    "doi": (paper.get("externalIds") or {}).get("DOI"),
                    "venue": paper.get("venue"),
                    "publicationTypes": paper.get("publicationTypes") or [],
                    "journal": paper.get("journal") or {},
                    "openAccessPdf": paper.get("openAccessPdf") or {},
                    "snippet": paper.get("abstract"),
                    "retrieval_rank": retrieval_rank,
                }
            )
            evidence_items.append(item)

        return evidence_items

    except Exception as e:
        logger.error(f"Semantic Scholar fallback search failed: {e}")
        return []


async def search_protocols_io(
    domain_pack: DomainPack,
    query: str,
    k_protocols: int,
    *,
    extract_anchor_query_terms: Callable[..., Any],
    BeautifulSoup: Any,
    logger: Logger,
) -> List[EvidenceItem]:
    """
        Search open protocol repositories like protocols.io.
        Queries the API, retrieves structured steps, and converts to Markdown.
        """
    logger.info(f"Searching protocols.io (k={k_protocols})...")

    from .....tools.ProtocolGeneration.protocols_io_client import ProtocolsIOClient
    client = ProtocolsIOClient()

    # Construct simplified queries for keyword search
    # "retinal organoid mouse PSC"
    queries = []

    # Optimize query: Extract core keywords to avoid long sentence issues
    import re
    # Remove common stopwords and keep only significant words
    stopwords = {"of", "the", "a", "an", "from", "and", "to", "in", "for", "with"}
    raw_words = re.sub(r'[^\w\s]', ' ', query).lower().split()
    keywords = [w for w in raw_words if w not in stopwords and len(w) > 3]

    base_query = " ".join(keywords)

    # Strategy 1: Task keywords + Species
    q1 = base_query
    if domain_pack.task.species:
         q1 += f" {domain_pack.task.species}"
    queries.append(q1)

    # Strategy 2: Fallback to just "Retinal Organoid" (or similar core noun phrase) if possible
    # For now, rely on the keyword extraction above.

    all_results = []
    for q in queries:
        results = await client.search_protocols(q)
        if results:
            all_results.extend(results)

    # protocols.io keyword search is much more literal than S2. If the
    # natural-language query misses, retry a bounded sequence of positive
    # identifiers (for example NGN2 or KOLF2) and one extra page only when
    # page 1 is full. Explicitly excluded identifiers are not used.
    if not all_results:
        positive_query = re.split(
            r"\bexclude(?:s|d|ing)?\b",
            str(query or ""),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        raw_identifiers = re.findall(
            r"\b(?:[A-Za-z]+\d[A-Za-z0-9-]*|[a-z]+[A-Z][A-Za-z0-9-]*)\b",
            positive_query,
        )
        identifier_tokens: List[str] = []
        for identifier in raw_identifiers:
            for variant in [identifier] + identifier.split("-"):
                if variant and variant not in identifier_tokens:
                    identifier_tokens.append(variant)
        for identifier in list(dict.fromkeys(identifier_tokens))[:3]:
            results = await client.search_protocols(identifier, page_id=1)
            if not results:
                continue
            all_results.extend(results)
            if len(results) >= 20:
                all_results.extend(
                    await client.search_protocols(identifier, page_id=2)
                )
            logger.info(
                "Recovered protocols.io candidates with identifier query %r",
                identifier,
            )
            break

    # Deduplicate by protocol ID
    unique_protocols = list({p.get("id"): p for p in all_results if p.get("id")}.values())
    query_terms = set(extract_anchor_query_terms(domain_pack.task.query))

    def _protocol_title_score(proto: Dict[str, Any]) -> Tuple[int, int]:
        title_lower = str(proto.get("title") or "").lower()
        overlap = sum(len(term) for term in query_terms if term in title_lower)
        return overlap, -len(title_lower)

    unique_protocols.sort(key=_protocol_title_score, reverse=True)

    evidence_items = []
    for i, proto in enumerate(unique_protocols[:k_protocols]):
        proto_id = proto.get("id")
        title = proto.get("title", "Unknown Protocol")
        uri = str(proto.get("uri") or proto.get("slug") or "").strip()
        url = uri if uri.startswith("http") else f"https://www.protocols.io/view/{uri}"

        # Fetch full details to get steps
        details = await client.get_protocol_details(proto_id)
        if not details:
            continue

        steps = details.get("steps") or []
        markdown_content = f"# {title}\n\n"
        for detail_key in ("description", "materials_text", "guidelines"):
            detail_text = details.get(detail_key)
            if detail_text:
                detail_text = BeautifulSoup(
                    str(detail_text), "html.parser"
                ).get_text(" ", strip=True)
                markdown_content += detail_text + "\n\n"
        markdown_content += client.format_steps_to_markdown(steps)

        # If no steps found, try to use description
        if not steps:
            markdown_content += details.get("description", "No detailed steps available.")

        item = EvidenceItem(
            id=f"protio_{proto_id}",
            source="protocols_io",
            title=title,
            year=None, # Extract from published_on if needed
            citation_count=None,
            authority_score=0.6, # Medium authority
            url=url,
            markdown=markdown_content,
            tags=["protocols_io", "steps", "open_source"],
            meta={
                "protocol_id": proto_id,
                "doi": details.get("doi") or proto.get("doi"),
                "snippet": markdown_content,
                "fulltext_strategy": "protocols_io_api",
                "fulltext_quality": "HIGH",
            }
        )
        evidence_items.append(item)

    return evidence_items


async def execute_search_queries(
    domain_pack: DomainPack,
    query: str,
    k: int,
    *,
    search_academic_sources: Callable[..., Any],
    search_official_sources: Callable[..., Any],
    search_protocols_io: Callable[..., Any],
    BioMarsJinaClient: Any,
    BioMarsSemanticClient: Any,
    logger: Logger,
) -> List[EvidenceItem]:
    """
        Execute the concurrent searches across Official, Academic, and Protocols.io sources.
        """
    # Heuristic distribution of K (total ~ k)
    # For k=50: official~15, academic~25, protocols~10
    k_official = max(5, int(k * 0.3))
    k_academic = max(10, int(k * 0.5))
    k_protocols = max(5, int(k * 0.2))

    async with BioMarsJinaClient() as jina, BioMarsSemanticClient() as sem:
        official_task = search_official_sources(domain_pack, query, k_official, jina=jina)
        academic_task = search_academic_sources(domain_pack, query, k_academic, sem=sem, jina=jina)
        protocols_task = search_protocols_io(domain_pack, query, k_protocols)

        results = await asyncio.gather(official_task, academic_task, protocols_task, return_exceptions=True)

        items = []
        if not isinstance(results[0], Exception):
            items.extend(results[0])
        else:
            logger.error(f"Official search failed: {results[0]}")

        if not isinstance(results[1], Exception):
            items.extend(results[1])
        else:
            logger.error(f"Academic search failed: {results[1]}")

        if not isinstance(results[2], Exception):
            items.extend(results[2])
        else:
            logger.error(f"Protocols.io search failed: {results[2]}")

        return items


async def check_species_bias_and_refine(
    items: List[EvidenceItem],
    current_query: str,
    target_species: str,
    *,
    call_model: Callable[..., Any],
    logger: Logger,
) -> str:
    """
        [NEW] Analyze search results for species mismatch and suggest a negative keyword query.
        Returns the refined query string or None.
        """
    if not items:
        return None

    # Prepare context for LLM
    titles = [f"- {item.title}" for item in items[:20]]
    titles_str = "\n".join(titles)

    system_prompt = "You are a research query optimizer."
    user_prompt = f"""
        **Task**: I am searching for protocols for Species: "{target_species}".
        **Current Query**: "{current_query}"
        
        **Search Results (Titles)**:
        {titles_str}

        **Goal**: Check if the results are dominated by a DIFFERENT, conflicting species (e.g. searching for Mouse but getting mostly Human results).
        
        **Instructions**:
        1. If the results look relevant to {target_species}, return "NO".
        2. If the results are polluted by a specific wrong species (e.g. Human), return a REFINED query that adds negative keywords.
           Format: "{current_query} +{target_species} -WrongSpecies"
           Example: "Primary cell isolation +Mouse -Human"
        
        Return ONLY the refined query string or "NO".
        """

    try:
        response = await call_model(
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
        response = response.strip().strip('"')
        if "NO" in response.upper() and len(response) < 10:
            return None
        return response
    except Exception as e:
        logger.warning(f"Species bias check failed: {e}")
        return None
