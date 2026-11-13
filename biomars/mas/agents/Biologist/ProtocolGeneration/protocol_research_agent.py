from typing import Any, Dict, List, Tuple, Optional
import logging
import asyncio
import json
import os
import aiohttp
import re
from bs4 import BeautifulSoup

from .research import source_content as research_source_content
from .research import fulltext as research_fulltext
from .research import retrieval as research_retrieval
from .research import anchors as research_anchors
from .research import artifacts as research_artifacts
from ...base_agent import BaseAgent
from .protocol_schemas import DomainPack, EvidenceBundle, EvidenceItem
from ....tools.ProtocolGeneration.biomars_jina_client import BioMarsJinaClient
from ....tools.ProtocolGeneration.biomars_semantic_client import BioMarsSemanticClient
from .sources.methods_extract_rules import normalize_markdown, find_methods_by_headings
from .research.citation_expansion import run_citation_expansion_shadow
from .sources.protocol_pdf_ingest import (
    find_prior_anchor_for_query,
    normalize_doi,
    normalize_protocol_query,
)

from .research.anchors import (
    UNIVERSAL_RELEVANCE_PROMPT,
    ANCHOR_SELECTION_SCHEMA,
    ANCHOR_CONTRACT_VALIDATION_SCHEMA,
    ANCHOR_PROTOCOL_TERMS,
    ANCHOR_COMPARATIVE_TERMS,
    ANCHOR_BACKGROUND_TERMS,
)
from .research.retrieval import (
    RETRIEVAL_RECOVERY_SCHEMA,
    DOMAIN_KNOWLEDGE_BASE,
)
from .research.source_content import (
    FULLTEXT_EVIDENCE_CHUNK_SELECTION_SCHEMA,
    SEG_SYSTEM,
    SEG_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)


class ProtocolResearchAgent(BaseAgent):
    """
    Agent responsible for gathering evidence (papers, protocols, datasheets)
    based on the DomainPack and ProtocolRequest.
    
    It orchestrates three sub-pipelines:
    1. Official Retriever (Official datasheets, manuals)
    2. Academic Researcher (Papers, PMC, Nature Protocols)
    3. Open Source Scout (Protocols.io, etc.)
    
    And then merges/ranks results into an EvidenceBundle.
    """

    agent_type = "protocol_research"

    async def execute(self, context: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the protocol research phase.

        Args:
            context: Dictionary containing:
                - domain_pack: The DomainPack object (Pydantic model or dict)
                - session_id: Optional session identifier
            params: Dictionary of execution parameters (e.g. k=50)

        Returns:
            Dict containing:
                - evidence_bundle: The resulting EvidenceBundle object
        """
        domain_pack_raw = context.get("domain_pack")
        if not domain_pack_raw:
            raise ValueError("DomainPack is required in context for ProtocolResearchAgent")

        # Ensure we have a DomainPack model instance
        if isinstance(domain_pack_raw, dict):
            domain_pack = DomainPack(**domain_pack_raw)
        else:
            domain_pack = domain_pack_raw

        # User requested increased k=50
        k = params.get("k", 50)
        session_id = context.get("session_id", "default")

        # Call the main research method
        evidence_bundle = await self.run_research(domain_pack, k=k, session_id=session_id)

        # Extract context updates if present (attached dynamically in run_research)
        result: Dict[str, Any] = {
            "evidence_bundle": evidence_bundle,
            "anchor_ids": getattr(evidence_bundle, "anchor_ids", []),
        }
        if hasattr(evidence_bundle, "context_updates"):
            result.update(evidence_bundle.context_updates)

        return result

    async def run_research(self, domain_pack: DomainPack, k: int = 50, session_id: str = "default", **kwargs) -> EvidenceBundle:
        """
        Main entry point for research logic.
        Orchestrates official, academic, and open-source searches concurrently.
        Supports automatic query refinement if species mismatch is detected.
        """
        core_query = self._build_core_query(domain_pack)
        logger.info(f"Built core research query: '{core_query}'")

        initial_attempted_queries = [core_query, domain_pack.task.query]
        semantic_retrieval_plan = await self._build_recovery_retrieval_plan(
            domain_pack,
            initial_attempted_queries,
        )
        prior_anchor_candidate = self._load_prior_anchor_candidate(domain_pack.task.query)
        active_retrieval_plan = semantic_retrieval_plan
        logger.info(
            "Semantic retrieval contract: operation=%r facets=%s planned_queries=%s",
            semantic_retrieval_plan.get("central_operation"),
            semantic_retrieval_plan.get("required_facets") or [],
            semantic_retrieval_plan.get("search_queries") or [],
        )

        # Initial Search
        items = await self._execute_search_queries(domain_pack, core_query, k)
        planned_items, planned_queries = await self._search_planned_academic_queries(
            domain_pack,
            semantic_retrieval_plan,
            k=k,
        )
        items.extend(planned_items)
        initial_attempted_queries.extend(planned_queries)
        if not items and core_query.strip() != (domain_pack.task.query or "").strip():
            logger.warning(
                "Initial search with core query returned no items. Retrying with raw task query: '%s'",
                domain_pack.task.query,
            )
            items = await self._execute_search_queries(domain_pack, domain_pack.task.query, k)

        if not items:
            logger.warning("All search sources returned no items. Falling back to direct Semantic Scholar search.")
            async with BioMarsJinaClient() as jina, BioMarsSemanticClient() as sem:
                items = await self._search_by_semantic_only(domain_pack.task.query, max(10, int(k * 0.5)), sem, jina)

        # [NEW] Check for Species Dominance / Mismatch and Refine
        target_species = domain_pack.task.species
        if target_species:
            refined_query = await self._check_species_bias_and_refine(items, core_query, target_species)
            if refined_query:
                logger.warning(f"Species bias detected. Re-running search with refined query: '{refined_query}'")
                # Re-run search with the refined query (replacing original results)
                items = await self._execute_search_queries(domain_pack, refined_query, k)

        if prior_anchor_candidate:
            items.append(prior_anchor_candidate)
            logger.info(
                "Revalidating prior primary anchor for stable query identity: %s",
                prior_anchor_candidate.id,
            )

        # Merge, rank, and (when progressive verification is enabled) stop only
        # after the selected primary passes the semantic anchor contract.
        initial_contract_validation: Dict[str, Any] = {}
        merged_items, anchor_ids = await self._merge_and_rank(
            items,
            domain_pack=domain_pack,
            k=k,
            retrieval_plan=semantic_retrieval_plan,
            contract_audit=initial_contract_validation,
        )
        preferred_primary_id = self._preferred_anchor_id(merged_items)
        if preferred_primary_id and not initial_contract_validation:
            anchor_ids = [preferred_primary_id] + [
                evidence_id for evidence_id in anchor_ids if evidence_id != preferred_primary_id
            ]
            anchor_ids = anchor_ids[:3]

        # Retain the old validation path for callers that do not opt into the
        # contract-aware merge. Normal research calls populate the audit above.
        if anchor_ids and not initial_contract_validation:
            validation_candidate_ids: List[str] = []
            for evidence_id in list(anchor_ids) + [
                str(item.id)
                for item in merged_items[:10]
                if getattr(item, "id", None)
            ]:
                evidence_id = str(evidence_id)
                if evidence_id not in validation_candidate_ids:
                    validation_candidate_ids.append(evidence_id)

            validated_ids, initial_contract_validation = await self._validate_anchor_with_stability_preference(
                merged_items,
                validation_candidate_ids,
                domain_pack,
                semantic_retrieval_plan,
                preferred_primary_id,
            )
            validation_decision = str(initial_contract_validation.get("decision") or "")
            anchor_ids = validated_ids

            if anchor_ids and validation_decision in {"accept", "needs_fulltext"}:
                self._apply_contract_anchor_metadata(
                    merged_items,
                    anchor_ids,
                    initial_contract_validation,
                    domain_pack,
                )
        elif anchor_ids:
            self._apply_contract_anchor_metadata(
                merged_items,
                anchor_ids,
                initial_contract_validation,
                domain_pack,
            )

        recovery_trace: Dict[str, Any] = {
            "triggered": False,
            "reason": "initial_anchor_available" if anchor_ids else "initial_anchor_missing",
            "attempted_queries": list(dict.fromkeys(initial_attempted_queries)),
            "initial_contract_validation": initial_contract_validation,
            "initial_semantic_retrieval_plan": semantic_retrieval_plan,
        }
        if not anchor_ids:
            merged_items, anchor_ids, recovery_trace = await self._recover_missing_anchors_with_llm(
                initial_items=items,
                initial_merged_items=merged_items,
                domain_pack=domain_pack,
                k=k,
                attempted_queries=initial_attempted_queries,
            )
            if initial_contract_validation:
                recovery_trace["initial_contract_validation"] = initial_contract_validation
            recovered_plan = recovery_trace.get("plan")
            if anchor_ids and isinstance(recovered_plan, dict) and recovered_plan:
                active_retrieval_plan = recovered_plan

        shortlist_oa_enrich_limit = int(os.getenv("PUBMED_SHORTLIST_OA_ENRICH_LIMIT", "3"))
        if shortlist_oa_enrich_limit > 0 and anchor_ids:
            try:
                await self._enrich_shortlisted_candidates_with_pubmed_oa(
                    merged_items,
                    anchor_ids,
                    shortlist_limit=shortlist_oa_enrich_limit,
                )
            except Exception as e:
                logger.warning(f"Shortlist OA enrichment failed: {e}")

        fetch_anchors_only = os.getenv("S2_FULLTEXT_FOR_ANCHORS_ONLY", "1") == "1"
        max_fulltext_docs = int(os.getenv("S2_MAX_FULLTEXT_DOCS", "3"))

        async with BioMarsJinaClient() as jina, BioMarsSemanticClient() as sem:
            if fetch_anchors_only:
                logger.info("Fetching full text for selected anchors only...")
                fetched_count = 0
                item_by_id = {str(item.id): item for item in merged_items}
                # Respect semantic anchor order: the primary must consume the
                # first (and possibly only) full-text budget slot.
                for evidence_id in anchor_ids:
                    item = item_by_id.get(str(evidence_id))
                    if (
                        item
                        and item.source == "protocols_io"
                        and item.markdown
                        and fetched_count < max_fulltext_docs
                    ):
                        # Protocols.io API details already contain the protocol's
                        # materials and executable steps. Preserve that richer
                        # source instead of replacing it with a short landing
                        # page or spending the Jina timeout budget on it.
                        meta = item.meta or {}
                        meta["fulltext_strategy"] = "protocols_io_api"
                        meta["fulltext_quality"] = "HIGH"
                        item.meta = meta
                        fetched_count += 1
                        continue
                    if (
                        item
                        and item.url
                        and fetched_count < max_fulltext_docs
                    ):
                        try:
                            logger.info(f"Fetching full text for anchor {item.id}: {item.title}")
                            strategy, quality, content = await self._fetch_anchor_fulltext(item, sem, jina)
                            if content and len(content) > len(item.markdown):
                                item.markdown = content
                                meta = item.meta or {}
                                meta["fulltext_strategy"] = strategy
                                meta["fulltext_quality"] = quality
                                item.meta = meta
                                fetched_count += 1
                        except Exception as e:
                            logger.warning(f"Failed to fetch full text for anchor {item.id}: {e}")

        methods_extract_enabled = os.getenv("METHODS_EXTRACT_ENABLE", "1") == "1"
        llm_fallback_enabled = os.getenv("METHODS_EXTRACT_LLM_FALLBACK", "1") == "1"
        max_lines = int(os.getenv("METHODS_EXTRACT_MAX_LINES", "1200"))
        raw_fulltexts: Dict[str, str] = {}

        if methods_extract_enabled:
            for item in merged_items:
                if item.id not in anchor_ids:
                    continue
                if not item.markdown:
                    continue
                raw_fulltext_md = item.markdown
                raw_fulltexts[item.id] = raw_fulltext_md
                try:
                    extracted, report, normalized = await self._extract_methods_text(
                        raw_fulltext_md,
                        use_llm_fallback=llm_fallback_enabled,
                        max_lines=max_lines,
                    )
                    if extracted and extracted.strip():
                        item.markdown = extracted
                    meta = item.meta or {}
                    chunks, analysis_markers = await self._build_methods_chunks(
                        item.markdown or "",
                        task_query=domain_pack.task.query,
                    )
                    if chunks:
                        meta["methods_chunks"] = chunks
                        meta["methods_chunk_count"] = len(chunks)
                    if analysis_markers:
                        meta["analysis_markers"] = analysis_markers
                    meta["methods_extract_report"] = report
                    meta["methods_extract_mode"] = report.get("mode")
                    meta["methods_extract_normalized_len"] = len(normalized)
                    item.meta = meta
                except Exception as e:
                    logger.warning(f"Methods extraction failed for anchor {item.id}: {e}")

        post_fulltext_contract_validation: Dict[str, Any] = {}
        primary_item = next(
            (item for item in merged_items if anchor_ids and str(item.id) == str(anchor_ids[0])),
            None,
        )
        anchor_id_set = {str(value) for value in anchor_ids}
        fulltext_candidate_ids = [
            str(item.id)
            for item in merged_items
            if str(item.id) in anchor_id_set
            and len(raw_fulltexts.get(str(item.id), "")) >= 5000
            and str((item.meta or {}).get("fulltext_strategy") or "") != "landing_page"
        ]
        has_primary_fulltext = bool(
            primary_item and str(primary_item.id) in fulltext_candidate_ids
        )
        if primary_item and has_primary_fulltext:
            validated_ids, post_fulltext_contract_validation = (
                await self._validate_anchor_with_stability_preference(
                    merged_items,
                    fulltext_candidate_ids,
                    domain_pack,
                    active_retrieval_plan,
                    preferred_primary_id,
                    validation_stage="fulltext",
                )
            )
            anchor_ids = validated_ids
            if anchor_ids:
                self._apply_contract_anchor_metadata(
                    merged_items,
                    anchor_ids,
                    post_fulltext_contract_validation,
                    domain_pack,
                )
            recovery_trace["post_fulltext_contract_validation"] = (
                post_fulltext_contract_validation
            )

        # [NEW] Persist anchor texts to disk for downstream agents
        (
            anchor_methods_paths,
            anchor_raw_paths,
            manual_pdf_requests,
            analysis_markers_by_anchor,
        ) = research_artifacts.save_anchor_artifacts(
            merged_items=merged_items,
            anchor_ids=anchor_ids,
            raw_fulltexts=raw_fulltexts,
            domain_pack=domain_pack,
            session_id=session_id,
            active_retrieval_plan=active_retrieval_plan,
            post_fulltext_contract_validation=post_fulltext_contract_validation,
            initial_contract_validation=initial_contract_validation,
            logger=logger,
            normalize_protocol_query=normalize_protocol_query,
        )

        bundle = EvidenceBundle(
            items=merged_items,
            anchor_ids=anchor_ids,
        )

        # [NEW] Shadow Citation Expansion via OpenAlex
        # This runs AFTER anchor selection and does NOT modify anchor_ids/evidence_bundle
        # Results are saved to results/AutoProtocol/research_expansion/<session_id>/
        if os.getenv("RESEARCH_CITATION_EXPAND", "0") == "1":
            try:
                # Collect external IDs for anchors (DOI/PMID preferred for OpenAlex resolution)
                anchor_external_ids = []
                for item in merged_items:
                    if item.id in anchor_ids:
                        meta = item.meta or {}
                        ext_ids = meta.get("externalIds", {}) or {}
                        doi = ext_ids.get("DOI") or meta.get("doi")
                        pmid = ext_ids.get("PubMed")
                        pmcid = ext_ids.get("PubMedCentral")

                        # Prefer DOI > PMID > PMCID > original ID
                        if doi:
                            anchor_external_ids.append(doi)
                        elif pmid:
                            anchor_external_ids.append(pmid)
                        elif pmcid:
                            anchor_external_ids.append(f"PMC{pmcid}" if not str(pmcid).startswith("PMC") else pmcid)
                        else:
                            anchor_external_ids.append(item.id)

                if anchor_external_ids:
                    logger.info(f"Running citation expansion for {len(anchor_external_ids)} anchors...")
                    expansion_result = await run_citation_expansion_shadow(
                        anchor_ids=anchor_external_ids,
                        query=domain_pack.task.query,
                        session_id=session_id
                    )
                    if expansion_result:
                        logger.info(f"Citation expansion complete: {len(expansion_result.global_top_k)} candidates found")
                else:
                    logger.warning("No valid external IDs found for citation expansion")
            except Exception as e:
                logger.error(f"Citation expansion failed (non-fatal): {e}")

        bundle.context_updates = {
            "anchor_methods_paths": anchor_methods_paths,
            "anchor_raw_paths": anchor_raw_paths,
        }
        try:
            anchor_dir = os.path.join("results", "AutoProtocol", "anchors", session_id)
            manifest_path = os.path.abspath(os.path.join(anchor_dir, "anchors_manifest.json"))
        except Exception:
            manifest_path = None
        bundle.context_updates["anchors_manifest_path"] = manifest_path
        bundle.context_updates["anchor_ids"] = anchor_ids
        bundle.context_updates["primary_anchor_id"] = anchor_ids[0] if anchor_ids else None
        bundle.context_updates["retrieval_recovery_trace"] = recovery_trace
        active_contract_validation = post_fulltext_contract_validation
        if not active_contract_validation:
            recovery_validation = recovery_trace.get("validation")
            if isinstance(recovery_validation, dict):
                active_contract_validation = recovery_validation
        if not active_contract_validation:
            active_contract_validation = initial_contract_validation
        contract_status = str(
            active_contract_validation.get("decision")
            or ("accept" if anchor_ids else "validation_unavailable")
        )
        if anchor_ids:
            primary_item = next(
                (item for item in merged_items if str(item.id) == str(anchor_ids[0])),
                None,
            )
            primary_meta = (primary_item.meta or {}) if primary_item else {}
            contract_status = str(primary_meta.get(
                "anchor_contract_status",
                contract_status,
            ))
        bundle.context_updates["anchor_contract_status"] = contract_status
        if analysis_markers_by_anchor:
            bundle.context_updates["analysis_markers_by_anchor"] = analysis_markers_by_anchor
        if manual_pdf_requests:
            bundle.context_updates["manual_pdf_requests"] = manual_pdf_requests

        logger.info(f"Research complete. Gathered {len(bundle.items)} items, {len(bundle.anchor_ids)} anchors.")
        return bundle

    # Query planning and retrieval: retain original entrypoints and override behavior.

    def _build_core_query(self, domain_pack: DomainPack) -> str:
        return research_retrieval.build_core_query(domain_pack)

    async def _build_recovery_retrieval_plan(
        self,
        domain_pack: DomainPack,
        attempted_queries: List[str],
    ) -> Dict[str, Any]:
        return await research_retrieval.build_recovery_retrieval_plan(
            domain_pack,
            attempted_queries,
            call_model=self._call_model,
            RETRIEVAL_RECOVERY_SCHEMA=RETRIEVAL_RECOVERY_SCHEMA,
            logger=logger,
        )

    async def _search_planned_academic_queries(
        self,
        domain_pack: DomainPack,
        retrieval_plan: Optional[Dict[str, Any]],
        k: int,
    ) -> Tuple[List[EvidenceItem], List[str]]:
        return await research_retrieval.search_planned_academic_queries(
            domain_pack,
            retrieval_plan,
            k,
            search_academic_sources=self._search_academic_sources,
            BioMarsJinaClient=BioMarsJinaClient,
            BioMarsSemanticClient=BioMarsSemanticClient,
            logger=logger,
        )

    async def _recover_missing_anchors_with_llm(
        self,
        initial_items: List[EvidenceItem],
        initial_merged_items: List[EvidenceItem],
        domain_pack: DomainPack,
        k: int,
        attempted_queries: List[str],
    ) -> Tuple[List[EvidenceItem], List[str], Dict[str, Any]]:
        return await research_retrieval.recover_missing_anchors_with_llm(
            initial_items,
            initial_merged_items,
            domain_pack,
            k,
            attempted_queries,
            apply_contract_anchor_metadata=self._apply_contract_anchor_metadata,
            build_recovery_retrieval_plan=self._build_recovery_retrieval_plan,
            merge_and_rank=self._merge_and_rank,
            search_planned_academic_queries=self._search_planned_academic_queries,
            validate_anchor_contract_with_llm=self._validate_anchor_contract_with_llm,
        )

    async def _route_domains_with_llm(self, query: str) -> List[str]:
        return await research_retrieval.route_domains_with_llm(
            query,
            call_model=self._call_model,
            agent_model=self.model,
            DOMAIN_KNOWLEDGE_BASE=DOMAIN_KNOWLEDGE_BASE,
            logger=logger,
        )

    async def _search_official_sources(self, domain_pack: DomainPack, query: str, k_official: int, jina: BioMarsJinaClient) -> List[EvidenceItem]:
        return await research_retrieval.search_official_sources(
            domain_pack,
            query,
            k_official,
            jina,
            route_domains_with_llm=self._route_domains_with_llm,
            logger=logger,
        )

    async def _search_academic_sources(
        self,
        domain_pack: DomainPack,
        query: str,
        k_academic: int,
        sem: BioMarsSemanticClient,
        jina: BioMarsJinaClient,
        force_pubmed_supplement: bool = False,
    ) -> List[EvidenceItem]:
        return await research_retrieval.search_academic_sources(
            domain_pack,
            query,
            k_academic,
            sem,
            jina,
            force_pubmed_supplement,
            extract_anchor_query_terms=self._extract_anchor_query_terms,
            search_by_semantic_only=self._search_by_semantic_only,
            ANCHOR_PROTOCOL_TERMS=ANCHOR_PROTOCOL_TERMS,
            logger=logger,
        )

    async def _search_by_semantic_only(self, raw_query: str, k_academic: int, sem: BioMarsSemanticClient, jina: BioMarsJinaClient) -> List[EvidenceItem]:
        return await research_retrieval.search_by_semantic_only(
            raw_query,
            k_academic,
            sem,
            jina,
            logger=logger,
        )

    async def _search_protocols_io(self, domain_pack: DomainPack, query: str, k_protocols: int) -> List[EvidenceItem]:
        return await research_retrieval.search_protocols_io(
            domain_pack,
            query,
            k_protocols,
            extract_anchor_query_terms=self._extract_anchor_query_terms,
            BeautifulSoup=BeautifulSoup,
            logger=logger,
        )

    async def _execute_search_queries(self, domain_pack: DomainPack, query: str, k: int) -> List[EvidenceItem]:
        return await research_retrieval.execute_search_queries(
            domain_pack,
            query,
            k,
            search_academic_sources=self._search_academic_sources,
            search_official_sources=self._search_official_sources,
            search_protocols_io=self._search_protocols_io,
            BioMarsJinaClient=BioMarsJinaClient,
            BioMarsSemanticClient=BioMarsSemanticClient,
            logger=logger,
        )

    async def _check_species_bias_and_refine(self, items: List[EvidenceItem], current_query: str, target_species: str) -> str:
        return await research_retrieval.check_species_bias_and_refine(
            items,
            current_query,
            target_species,
            call_model=self._call_model,
            logger=logger,
        )

    # Candidate ranking and acceptance: retain original entrypoints and override behavior.

    @staticmethod
    def _preferred_anchor_id(items: List[EvidenceItem]) -> str:
        return research_anchors.preferred_anchor_id(items)

    async def _validate_anchor_with_stability_preference(
        self,
        candidate_items: List[EvidenceItem],
        candidate_anchor_ids: List[str],
        domain_pack: DomainPack,
        retrieval_plan: Optional[Dict[str, Any]],
        preferred_primary_id: str,
        validation_stage: str = "abstract",
    ) -> Tuple[List[str], Dict[str, Any]]:
        return await research_anchors.validate_anchor_with_stability_preference(
            candidate_items,
            candidate_anchor_ids,
            domain_pack,
            retrieval_plan,
            preferred_primary_id,
            validation_stage,
            validate_anchor_contract_with_llm=self._validate_anchor_contract_with_llm,
        )

    async def _validate_anchor_contract_with_llm(
        self,
        candidate_items: List[EvidenceItem],
        candidate_anchor_ids: List[str],
        domain_pack: DomainPack,
        recovery_plan: Optional[Dict[str, Any]] = None,
        allow_reselection: bool = True,
        validation_stage: str = "abstract",
        allow_fulltext_evidence_retry: bool = True,
        evidence_overrides: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[str], Dict[str, Any]]:
        return await research_anchors.validate_anchor_contract_with_llm(
            candidate_items,
            candidate_anchor_ids,
            domain_pack,
            recovery_plan,
            allow_reselection,
            validation_stage,
            allow_fulltext_evidence_retry,
            evidence_overrides,
            call_model=self._call_model,
            select_targeted_fulltext_evidence=self._select_targeted_fulltext_evidence,
            validate_anchor_contract_with_llm=self._validate_anchor_contract_with_llm,
            ANCHOR_CONTRACT_VALIDATION_SCHEMA=ANCHOR_CONTRACT_VALIDATION_SCHEMA,
            logger=logger,
        )

    async def _select_anchors_with_llm(self, top_items: List[EvidenceItem], domain_pack: DomainPack) -> List[str]:
        return await research_anchors.select_anchors_with_llm(
            top_items,
            domain_pack,
            apply_anchor_selection_metadata=self._apply_anchor_selection_metadata,
            call_model=self._call_model,
            combine_llm_and_heuristic_anchor_order=self._combine_llm_and_heuristic_anchor_order,
            extract_anchor_query_terms=self._extract_anchor_query_terms,
            summarize_anchor_candidate=self._summarize_anchor_candidate,
            ANCHOR_SELECTION_SCHEMA=ANCHOR_SELECTION_SCHEMA,
            logger=logger,
        )

    async def _verify_candidates_with_llm(
        self,
        candidates: List[EvidenceItem],
        query: str,
        retrieval_plan: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[EvidenceItem, Dict]]:
        return await research_anchors.verify_candidates_with_llm(
            candidates,
            query,
            retrieval_plan,
            call_model=self._call_model,
            agent_model=self.model,
            UNIVERSAL_RELEVANCE_PROMPT=UNIVERSAL_RELEVANCE_PROMPT,
            logger=logger,
        )

    async def _merge_and_rank(
        self,
        all_items: List[EvidenceItem],
        domain_pack: DomainPack,
        k: int,
        retrieval_plan: Optional[Dict[str, Any]] = None,
        contract_audit: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[EvidenceItem], List[str]]:
        return await research_anchors.merge_and_rank(
            all_items,
            domain_pack,
            k,
            retrieval_plan,
            contract_audit,
            calculate_base_score=self._calculate_base_score,
            explicit_document_exclusion_reason=self._explicit_document_exclusion_reason,
            preferred_anchor_id=self._preferred_anchor_id,
            select_anchors_with_llm=self._select_anchors_with_llm,
            title_supports_central_operation=self._title_supports_central_operation,
            validate_anchor_with_stability_preference=self._validate_anchor_with_stability_preference,
            verify_candidates_with_llm=self._verify_candidates_with_llm,
            logger=logger,
            normalize_doi=normalize_doi,
        )

    @staticmethod
    def _explicit_document_exclusion_reason(
        query: str,
        item: EvidenceItem,
    ) -> str:
        return research_anchors.explicit_document_exclusion_reason(query, item)

    @staticmethod
    def _title_supports_central_operation(
        item: EvidenceItem,
        retrieval_plan: Optional[Dict[str, Any]],
    ) -> bool:
        return research_anchors.title_supports_central_operation(item, retrieval_plan)

    def _calculate_base_score(self, item: EvidenceItem, domain_pack: DomainPack) -> float:
        return research_anchors.calculate_base_score(item, domain_pack)

    def _select_anchors_heuristic(self, top_items: List[EvidenceItem], domain_pack: DomainPack) -> List[str]:
        return research_anchors.select_anchors_heuristic(
            top_items,
            domain_pack,
            score_anchor_candidate_heuristically=self._score_anchor_candidate_heuristically,
        )

    def _score_anchor_candidate_heuristically(
        self,
        item: EvidenceItem,
        domain_pack: DomainPack,
    ) -> float:
        return research_anchors.score_anchor_candidate_heuristically(
            item,
            domain_pack,
            extract_anchor_query_terms=self._extract_anchor_query_terms,
            extract_core_operation_terms=self._extract_core_operation_terms,
            ANCHOR_BACKGROUND_TERMS=ANCHOR_BACKGROUND_TERMS,
            ANCHOR_COMPARATIVE_TERMS=ANCHOR_COMPARATIVE_TERMS,
            ANCHOR_PROTOCOL_TERMS=ANCHOR_PROTOCOL_TERMS,
        )

    def _extract_anchor_query_terms(self, query: str) -> List[str]:
        return research_anchors.extract_anchor_query_terms(query)

    def _extract_core_operation_terms(self, query: str) -> List[str]:
        return research_anchors.extract_core_operation_terms(query)

    def _summarize_anchor_candidate(self, item: EvidenceItem, query_terms: List[str]) -> str:
        return research_anchors.summarize_anchor_candidate(
            item,
            query_terms,
            ANCHOR_BACKGROUND_TERMS=ANCHOR_BACKGROUND_TERMS,
            ANCHOR_COMPARATIVE_TERMS=ANCHOR_COMPARATIVE_TERMS,
            ANCHOR_PROTOCOL_TERMS=ANCHOR_PROTOCOL_TERMS,
        )

    def _apply_anchor_selection_metadata(
        self,
        top_items: List[EvidenceItem],
        selection_payload: Dict[str, Any],
    ) -> None:
        return research_anchors.apply_anchor_selection_metadata(top_items, selection_payload)

    def _apply_contract_anchor_metadata(
        self,
        items: List[EvidenceItem],
        anchor_ids: List[str],
        validation: Dict[str, Any],
        domain_pack: DomainPack,
    ) -> None:
        return research_anchors.apply_contract_anchor_metadata(
            items,
            anchor_ids,
            validation,
            domain_pack,
            apply_anchor_selection_metadata=self._apply_anchor_selection_metadata,
        )

    def _apply_heuristic_anchor_metadata(
        self,
        top_items: List[EvidenceItem],
        selected_ids: List[str],
        query_focus: str = "",
    ) -> None:
        return research_anchors.apply_heuristic_anchor_metadata(
            top_items,
            selected_ids,
            query_focus,
            apply_anchor_selection_metadata=self._apply_anchor_selection_metadata,
        )

    def _combine_llm_and_heuristic_anchor_order(
        self,
        candidate_items: List[EvidenceItem],
        ordered_ids: List[str],
        primary_id: Optional[str],
        ranked_candidates: List[Dict[str, Any]],
        domain_pack: DomainPack,
    ) -> List[str]:
        return research_anchors.combine_llm_and_heuristic_anchor_order(
            candidate_items,
            ordered_ids,
            primary_id,
            ranked_candidates,
            domain_pack,
            score_anchor_candidate_heuristically=self._score_anchor_candidate_heuristically,
        )

    # Full-text acquisition: retain original entrypoints and override behavior.

    async def _enrich_shortlisted_candidates_with_pubmed_oa(
        self,
        merged_items: List[EvidenceItem],
        anchor_ids: List[str],
        shortlist_limit: int = 3,
    ) -> None:
        return await research_fulltext.enrich_shortlisted_candidates_with_pubmed_oa(
            merged_items,
            anchor_ids,
            shortlist_limit,
        )

    async def _get_unpaywall_pdf(self, doi: str) -> str:
        return await research_fulltext.get_unpaywall_pdf(doi, logger=logger)

    async def _fetch_text(self, session, url, headers=None) -> str:
        return await research_fulltext.fetch_text(session, url, headers, logger=logger)

    def _html_to_text(self, html: str) -> str:
        return research_fulltext.html_to_text(html, BeautifulSoup=BeautifulSoup)

    def _looks_like_fulltext(self, content: str) -> bool:
        return research_fulltext.looks_like_fulltext(content)

    async def _fetch_anchor_fulltext(
        self,
        item: EvidenceItem,
        sem: BioMarsSemanticClient,
        jina: BioMarsJinaClient,
    ) -> Tuple[str, str, str]:
        return await research_fulltext.fetch_anchor_fulltext(
            item,
            sem,
            jina,
            fetch_text=self._fetch_text,
            get_unpaywall_pdf=self._get_unpaywall_pdf,
            html_to_text=self._html_to_text,
            looks_like_fulltext=self._looks_like_fulltext,
            logger=logger,
        )

    # Methods and source evidence: retain original entrypoints and override behavior.

    async def _extract_methods_text(
        self,
        raw_fulltext_md: str,
        use_llm_fallback: bool,
        max_lines: int,
    ) -> Tuple[str, Dict[str, Any], str]:
        return await research_source_content.extract_methods_text(
            raw_fulltext_md,
            use_llm_fallback,
            max_lines,
            segment_methods_with_llm=self._segment_methods_with_llm,
            find_methods_by_headings=find_methods_by_headings,
            normalize_markdown=normalize_markdown,
        )

    async def _segment_methods_with_llm(
        self,
        normalized: str,
        max_lines: int = 1200,
    ) -> Tuple[int, int]:
        return await research_source_content.segment_methods_with_llm(
            normalized,
            max_lines,
            call_model=self._call_model,
            SEG_SYSTEM=SEG_SYSTEM,
            SEG_USER_TEMPLATE=SEG_USER_TEMPLATE,
        )

    async def _build_methods_chunks(
        self,
        methods_text: str,
        task_query: str = "",
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        return await research_source_content.build_methods_chunks(
            methods_text,
            task_query,
            derive_analysis_markers=self._derive_analysis_markers,
            extract_day_ranges=self._extract_day_ranges,
            split_line_by_analysis_markers=self._split_line_by_analysis_markers,
            normalize_markdown=normalize_markdown,
        )

    async def _select_targeted_fulltext_evidence(
        self,
        item: EvidenceItem,
        domain_pack: DomainPack,
        recovery_plan: Optional[Dict[str, Any]],
        unresolved_facets: List[str],
    ) -> Tuple[str, Dict[str, Any]]:
        return await research_source_content.select_targeted_fulltext_evidence(
            item,
            domain_pack,
            recovery_plan,
            unresolved_facets,
            call_model=self._call_model,
            FULLTEXT_EVIDENCE_CHUNK_SELECTION_SCHEMA=FULLTEXT_EVIDENCE_CHUNK_SELECTION_SCHEMA,
        )

    async def _derive_analysis_markers(
        self,
        methods_text: str,
        task_query: str = "",
    ) -> List[str]:
        return await research_source_content.derive_analysis_markers(
            methods_text,
            task_query,
            call_model=self._call_model,
            derive_analysis_markers_from_text=self._derive_analysis_markers_from_text,
            logger=logger,
        )

    def _derive_analysis_markers_from_text(self, methods_text: str) -> List[str]:
        return research_source_content.derive_analysis_markers_from_text(methods_text)

    def _split_line_by_analysis_markers(
        self,
        line: str,
        analysis_markers: List[str],
    ) -> List[Tuple[str, bool]]:
        return research_source_content.split_line_by_analysis_markers(line, analysis_markers)

    def _extract_day_ranges(self, text: str) -> List[Dict[str, Any]]:
        return research_source_content.extract_day_ranges(text)

    # Persistent artifacts: retain original entrypoints and override behavior.

    def _load_prior_anchor_candidate(self, task_query: str) -> Optional[EvidenceItem]:
        return research_artifacts.load_prior_anchor_candidate(
            task_query,
            find_prior_anchor_for_query=find_prior_anchor_for_query,
            normalize_doi=normalize_doi,
        )
