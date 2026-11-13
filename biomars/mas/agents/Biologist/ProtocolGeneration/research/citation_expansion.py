"""
Citation Expansion Module for Bio-MARS / InternAgent

Implements "Shadow Expansion" - a bypass/extension that:
1. Takes existing anchor papers (1-3)
2. Uses OpenAlex to expand citation/reference network
3. Scores candidates by: relevance + citations + source quality + fulltext availability
4. Forms 3 clusters centered on each anchor
5. Saves results WITHOUT modifying existing anchor_ids/evidence_bundle

This module is designed to be called AFTER anchor selection in ProtocolResearchAgent.
Results are persisted to disk for optional downstream use.

Reference: OpenAlex API Docs (https://docs.openalex.org/)
"""

import os
import json
import math
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Set, Tuple
from dataclasses import dataclass, field, asdict

from .....tools.ProtocolGeneration.openalex_client import OpenAlexClient

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes for Cluster Results
# =============================================================================

@dataclass
class ScoredCandidate:
    """A scored candidate work from citation expansion."""
    openalex_id: str
    title: str
    doi: Optional[str] = None
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    
    # Scoring components
    relevance_score: float = 0.0  # From OpenAlex search (0-1 normalized)
    citation_score: float = 0.0   # log-normalized citation count
    source_quality_score: float = 0.0  # is_core source bonus
    fulltext_score: float = 0.0   # has_fulltext / is_oa bonus
    
    # Combined score
    total_score: float = 0.0
    
    # Metadata
    cited_by_count: int = 0
    publication_year: Optional[int] = None
    is_retracted: bool = False
    has_fulltext: bool = False
    is_oa: bool = False
    oa_url: Optional[str] = None
    source_name: Optional[str] = None
    
    # Cluster membership
    cluster_ids: List[str] = field(default_factory=list)
    expansion_type: str = ""  # "citing" or "cited"
    
    # Scoring breakdown for debugging
    score_breakdown: Dict[str, float] = field(default_factory=dict)


@dataclass
class ClusterResult:
    """Results for a single anchor-centered cluster."""
    cluster_id: str
    anchor_openalex_id: str
    anchor_title: str
    anchor_external_id: str  # Original ID (DOI/PMID)
    
    # Candidates
    candidates_citing: List[ScoredCandidate] = field(default_factory=list)
    candidates_cited: List[ScoredCandidate] = field(default_factory=list)
    top_k_combined: List[ScoredCandidate] = field(default_factory=list)
    
    # Stats
    total_citing: int = 0
    total_cited: int = 0
    query_used: Optional[str] = None


@dataclass
class CitationExpansionResult:
    """Complete result of citation expansion."""
    session_id: str
    query: str
    timestamp: str
    
    # Seed mapping
    anchor_to_openalex: Dict[str, str] = field(default_factory=dict)
    
    # Cluster results
    clusters: List[ClusterResult] = field(default_factory=list)
    
    # Global deduped top-K across all clusters
    global_top_k: List[ScoredCandidate] = field(default_factory=list)
    
    # Credit usage
    credits_usage: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Scoring Functions
# =============================================================================

class CandidateScorer:
    """
    Scores candidates based on multiple factors.
    
    Default weights (adjustable via env vars):
    - Relevance: 0.55
    - Citation: 0.25  
    - Source Quality: 0.10
    - Fulltext: 0.10
    """
    
    def __init__(self):
        # Load weights from environment (configurable)
        self.weight_relevance = float(os.getenv("CITATION_SCORE_WEIGHT_RELEVANCE", "0.55"))
        self.weight_citation = float(os.getenv("CITATION_SCORE_WEIGHT_CITATION", "0.25"))
        self.weight_source = float(os.getenv("CITATION_SCORE_WEIGHT_SOURCE", "0.10"))
        self.weight_fulltext = float(os.getenv("CITATION_SCORE_WEIGHT_FULLTEXT", "0.10"))
        
        # Citation normalization parameters
        self.citation_log_base = 10
        self.citation_max_log = 4  # log10(10000) = 4
        
        # Recency bonus parameters
        self.recency_enabled = os.getenv("CITATION_SCORE_RECENCY_ENABLED", "0") == "1"
        self.recency_year_threshold = int(os.getenv("CITATION_SCORE_RECENCY_YEAR", "2020"))
    
    def score_candidate(
        self,
        work: Dict[str, Any],
        relevance_score: Optional[float] = None
    ) -> ScoredCandidate:
        """
        Score a single candidate work.
        
        Args:
            work: Raw work data from OpenAlex
            relevance_score: Optional relevance score from search (0-1)
        
        Returns:
            ScoredCandidate with all scores computed
        """
        from .....tools.ProtocolGeneration.openalex_client import OpenAlexClient
        
        # Extract scoring fields using client helper
        client = OpenAlexClient()
        fields = client.extract_scoring_fields(work)
        
        # Check for retraction (hard filter)
        if fields.get("is_retracted", False):
            # Return with zero score but keep for logging
            return ScoredCandidate(
                openalex_id=fields.get("openalex_id", ""),
                title=fields.get("title", ""),
                is_retracted=True,
                total_score=0.0,
                score_breakdown={"rejected": "retracted"}
            )
        
        # 1. Relevance Score (R)
        # If provided from search, use it; otherwise default to 0.5
        r_score = relevance_score if relevance_score is not None else 0.5
        
        # 2. Citation Score (C)
        # Log-normalize: log10(1 + count) / max_log, capped at 1.0
        count = fields.get("cited_by_count", 0) or 0
        c_raw = math.log10(1 + count) / self.citation_max_log
        c_score = min(1.0, c_raw)
        
        # 3. Source Quality Score (V)
        # is_core = 1.0, otherwise 0.0
        v_score = 1.0 if fields.get("is_core_source", False) else 0.0
        
        # 4. Fulltext Score (F)
        # has_fulltext or is_oa = 1.0
        f_score = 1.0 if (fields.get("has_fulltext") or fields.get("is_oa")) else 0.0
        
        # 5. Recency Bonus (optional)
        recency_bonus = 0.0
        if self.recency_enabled:
            pub_year = fields.get("publication_year")
            if pub_year and pub_year >= self.recency_year_threshold:
                recency_bonus = 0.05
        
        # Compute total score
        total = (
            self.weight_relevance * r_score +
            self.weight_citation * c_score +
            self.weight_source * v_score +
            self.weight_fulltext * f_score +
            recency_bonus
        )
        
        return ScoredCandidate(
            openalex_id=fields.get("openalex_id", ""),
            title=fields.get("title", ""),
            doi=fields.get("doi"),
            pmid=fields.get("pmid"),
            pmcid=fields.get("pmcid"),
            relevance_score=r_score,
            citation_score=c_score,
            source_quality_score=v_score,
            fulltext_score=f_score,
            total_score=total,
            cited_by_count=count,
            publication_year=fields.get("publication_year"),
            is_retracted=False,
            has_fulltext=fields.get("has_fulltext", False) or fields.get("is_oa", False),
            is_oa=fields.get("is_oa", False),
            oa_url=fields.get("oa_url"),
            source_name=fields.get("source_name"),
            score_breakdown={
                "R": r_score,
                "C": c_score,
                "V": v_score,
                "F": f_score,
                "recency": recency_bonus,
                "weights": f"R:{self.weight_relevance},C:{self.weight_citation},V:{self.weight_source},F:{self.weight_fulltext}"
            }
        )


# =============================================================================
# Main Expansion Class
# =============================================================================

class CitationExpander:
    """
    Main class for running citation network expansion.
    
    Usage:
        async with CitationExpander() as expander:
            result = await expander.expand_from_anchors(
                anchor_ids=["doi:10.1038/...", "pmid:12345"],
                query="retinal organoids mouse",
                session_id="session_123"
            )
    """
    
    def __init__(self):
        self.client: Optional[OpenAlexClient] = None
        self.scorer = CandidateScorer()
        
        # Configuration
        self.citing_limit = int(os.getenv("CITATION_EXPAND_CITING_LIMIT", "200"))
        self.cited_limit = int(os.getenv("CITATION_EXPAND_CITED_LIMIT", "100"))
        self.top_k_per_cluster = int(os.getenv("CITATION_EXPAND_TOP_K_PER_CLUSTER", "30"))
        self.global_top_k = int(os.getenv("CITATION_EXPAND_GLOBAL_TOP_K", "50"))
        
        # Fields to retrieve from OpenAlex (minimal for efficiency)
        self.select_fields = [
            "id", "title", "publication_year", "cited_by_count",
            "is_retracted", "primary_location", "open_access",
            "doi", "ids"
        ]
    
    async def __aenter__(self):
        self.client = OpenAlexClient()
        await self.client.__aenter__()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.__aexit__(exc_type, exc_val, exc_tb)
    
    async def expand_from_anchors(
        self,
        anchor_ids: List[str],
        query: str,
        session_id: str,
        out_dir: Optional[str] = None
    ) -> CitationExpansionResult:
        """
        Main entry point: expand citation network from anchor papers.
        
        Args:
            anchor_ids: List of anchor identifiers (DOI/PMID/PMCID)
            query: Task query for relevance filtering
            session_id: Session identifier for output naming
            out_dir: Output directory (defaults to results/AutoProtocol/research_expansion/<session_id>/)
        
        Returns:
            CitationExpansionResult with all clusters and scored candidates
        """
        timestamp = datetime.utcnow().isoformat() + "Z"
        
        # Set up output directory
        if not out_dir:
            out_dir = os.path.join("results", "AutoProtocol", "research_expansion", session_id)
        os.makedirs(out_dir, exist_ok=True)
        
        logger.info(f"Starting citation expansion for {len(anchor_ids)} anchors")
        logger.info(f"Query: {query}")
        logger.info(f"Output dir: {out_dir}")
        
        # Step 0: Resolve anchors to OpenAlex IDs
        anchor_to_openalex = await self._resolve_anchors(anchor_ids)
        
        if not anchor_to_openalex:
            logger.warning("No anchors could be resolved to OpenAlex IDs")
            return CitationExpansionResult(
                session_id=session_id,
                query=query,
                timestamp=timestamp,
                anchor_to_openalex={},
                clusters=[],
                global_top_k=[],
                credits_usage=self.client.get_credits_usage()
            )
        
        # Save seed mapping
        self._save_json(os.path.join(out_dir, "cluster_seed_map.json"), anchor_to_openalex)
        
        # Step 1-3: Expand each anchor into a cluster
        clusters = []
        all_candidates_raw: List[Dict] = []
        
        for anchor_ext_id, openalex_id in anchor_to_openalex.items():
            cluster = await self._expand_single_anchor(
                anchor_external_id=anchor_ext_id,
                anchor_openalex_id=openalex_id,
                query=query,
                cluster_index=len(clusters)
            )
            clusters.append(cluster)
            
            # Collect raw candidates for JSONL output
            for cand in cluster.candidates_citing + cluster.candidates_cited:
                all_candidates_raw.append(asdict(cand))
        
        # Save raw candidates
        self._save_jsonl(os.path.join(out_dir, "cluster_candidates_raw.jsonl"), all_candidates_raw)
        
        # Step 4: Global deduplication and selection
        global_top = self._dedupe_and_select_global(clusters)
        
        # Save scored candidates
        scored_candidates = [asdict(c) for c in global_top]
        self._save_jsonl(os.path.join(out_dir, "cluster_candidates_scored.jsonl"), scored_candidates)
        
        # Save per-cluster top-K
        cluster_topk = {}
        for cluster in clusters:
            cluster_topk[cluster.cluster_id] = {
                "anchor_id": cluster.anchor_external_id,
                "anchor_openalex_id": cluster.anchor_openalex_id,
                "anchor_title": cluster.anchor_title,
                "top_k": [asdict(c) for c in cluster.top_k_combined]
            }
        self._save_json(os.path.join(out_dir, "cluster_topk.json"), cluster_topk)
        
        # Save credits usage
        credits = self.client.get_credits_usage()
        self._save_json(os.path.join(out_dir, "credits_usage.json"), credits)
        
        # Build final result
        result = CitationExpansionResult(
            session_id=session_id,
            query=query,
            timestamp=timestamp,
            anchor_to_openalex=anchor_to_openalex,
            clusters=clusters,
            global_top_k=global_top,
            credits_usage=credits
        )
        
        logger.info(f"Citation expansion complete: {len(global_top)} global top candidates")
        logger.info(f"Credits used: {credits.get('total_credits', 0)}")
        
        return result
    
    async def _resolve_anchors(self, anchor_ids: List[str]) -> Dict[str, str]:
        """Resolve anchor identifiers to OpenAlex Work IDs."""
        result = {}
        
        for anchor_id in anchor_ids:
            # Detect ID type from prefix/format
            id_type = "auto"
            clean_id = anchor_id
            
            if anchor_id.startswith("sem_"):
                # Semantic Scholar ID - try to get DOI from evidence
                logger.warning(f"Skipping S2 ID {anchor_id} - not directly resolvable")
                continue
            elif anchor_id.startswith("pmc_"):
                clean_id = anchor_id.replace("pmc_", "")
                id_type = "pmcid"
            elif anchor_id.startswith("official_") or anchor_id.startswith("protio_"):
                # Non-academic sources - skip
                logger.debug(f"Skipping non-academic anchor: {anchor_id}")
                continue
            elif anchor_id.startswith("10."):
                id_type = "doi"
            elif anchor_id.upper().startswith("PMC"):
                id_type = "pmcid"
            elif anchor_id.isdigit():
                id_type = "pmid"
            
            openalex_id = await self.client.resolve_to_openalex_id(clean_id, id_type)
            
            if openalex_id:
                result[anchor_id] = openalex_id
                logger.info(f"Resolved {anchor_id} -> {openalex_id}")
            else:
                logger.warning(f"Could not resolve anchor {anchor_id} to OpenAlex ID")
        
        return result
    
    async def _expand_single_anchor(
        self,
        anchor_external_id: str,
        anchor_openalex_id: str,
        query: str,
        cluster_index: int
    ) -> ClusterResult:
        """Expand citation network for a single anchor."""
        cluster_id = f"cluster_{cluster_index:02d}"
        
        # Get anchor metadata for title
        anchor_work = await self.client.get_work(anchor_openalex_id, select_fields=["id", "title"])
        anchor_title = anchor_work.get("title", "Unknown") if anchor_work else "Unknown"
        
        logger.info(f"Expanding {cluster_id}: {anchor_title[:50]}...")
        
        # Get citing works (with relevance filtering via search)
        citing_raw = await self.client.get_citing_works(
            anchor_openalex_id,
            search_query=query,
            per_page=self.citing_limit,
            select_fields=self.select_fields
        )
        
        # Get cited works (references)
        cited_raw = await self.client.get_cited_works(
            anchor_openalex_id,
            per_page=self.cited_limit,
            select_fields=self.select_fields
        )
        
        logger.info(f"  Found {len(citing_raw)} citing, {len(cited_raw)} cited")
        
        # Score all candidates
        citing_scored = []
        for i, work in enumerate(citing_raw):
            # Use position as a proxy for relevance (search returns sorted by relevance)
            # Normalize: first result = 1.0, last = 0.5
            if len(citing_raw) > 1:
                rel_score = 1.0 - (0.5 * i / (len(citing_raw) - 1))
            else:
                rel_score = 1.0
            
            cand = self.scorer.score_candidate(work, relevance_score=rel_score)
            cand.expansion_type = "citing"
            cand.cluster_ids = [cluster_id]
            citing_scored.append(cand)
        
        cited_scored = []
        for work in cited_raw:
            # No relevance score for references (no search)
            cand = self.scorer.score_candidate(work, relevance_score=0.5)
            cand.expansion_type = "cited"
            cand.cluster_ids = [cluster_id]
            cited_scored.append(cand)
        
        # Filter out retracted papers
        citing_scored = [c for c in citing_scored if not c.is_retracted]
        cited_scored = [c for c in cited_scored if not c.is_retracted]
        
        # Sort by total score
        citing_scored.sort(key=lambda x: x.total_score, reverse=True)
        cited_scored.sort(key=lambda x: x.total_score, reverse=True)
        
        # Select top-K for this cluster
        # Strategy: 70% from citing (more relevant), 30% from cited
        citing_k = int(self.top_k_per_cluster * 0.7)
        cited_k = self.top_k_per_cluster - citing_k
        
        top_combined = citing_scored[:citing_k] + cited_scored[:cited_k]
        top_combined.sort(key=lambda x: x.total_score, reverse=True)
        
        return ClusterResult(
            cluster_id=cluster_id,
            anchor_openalex_id=anchor_openalex_id,
            anchor_title=anchor_title,
            anchor_external_id=anchor_external_id,
            candidates_citing=citing_scored,
            candidates_cited=cited_scored,
            top_k_combined=top_combined[:self.top_k_per_cluster],
            total_citing=len(citing_raw),
            total_cited=len(cited_raw),
            query_used=query
        )
    
    def _dedupe_and_select_global(self, clusters: List[ClusterResult]) -> List[ScoredCandidate]:
        """
        Deduplicate candidates across clusters and select global top-K.
        
        Strategy: 
        - If a paper appears in multiple clusters, merge cluster_ids and keep highest score
        - Sort by total score
        - Return top-K
        """
        # Collect all candidates
        all_candidates: Dict[str, ScoredCandidate] = {}
        
        for cluster in clusters:
            for cand in cluster.top_k_combined:
                if cand.openalex_id in all_candidates:
                    # Merge: keep higher score, add cluster_id
                    existing = all_candidates[cand.openalex_id]
                    if cand.total_score > existing.total_score:
                        # Update with higher-scoring version but keep cluster membership
                        existing_clusters = existing.cluster_ids
                        all_candidates[cand.openalex_id] = cand
                        all_candidates[cand.openalex_id].cluster_ids = list(
                            set(existing_clusters + cand.cluster_ids)
                        )
                    else:
                        # Just add cluster_id
                        existing.cluster_ids = list(
                            set(existing.cluster_ids + cand.cluster_ids)
                        )
                else:
                    all_candidates[cand.openalex_id] = cand
        
        # Sort by score and return top-K
        sorted_candidates = sorted(
            all_candidates.values(),
            key=lambda x: x.total_score,
            reverse=True
        )
        
        return sorted_candidates[:self.global_top_k]
    
    def _save_json(self, path: str, data: Any):
        """Save data as JSON."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    
    def _save_jsonl(self, path: str, records: List[Dict]):
        """Save records as JSONL."""
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# =============================================================================
# Convenience Function for Integration
# =============================================================================

async def run_citation_expansion_shadow(
    anchor_ids: List[str],
    query: str,
    session_id: str,
    out_dir: Optional[str] = None
) -> Optional[CitationExpansionResult]:
    """
    Convenience function to run citation expansion as a shadow process.
    
    This is the main entry point for integration into ProtocolResearchAgent.
    
    Usage:
        if os.getenv("RESEARCH_CITATION_EXPAND") == "1":
            await run_citation_expansion_shadow(anchor_ids, query, session_id)
    
    Returns:
        CitationExpansionResult or None if disabled/failed
    """
    # Check if enabled
    if os.getenv("RESEARCH_CITATION_EXPAND", "0") != "1":
        logger.debug("Citation expansion disabled (RESEARCH_CITATION_EXPAND != 1)")
        return None
    
    # Check if shadow mode
    shadow_mode = os.getenv("RESEARCH_CITATION_SHADOW", "1") == "1"
    if not shadow_mode:
        logger.info("Citation expansion running in non-shadow mode (results may modify pipeline)")
    
    try:
        async with CitationExpander() as expander:
            result = await expander.expand_from_anchors(
                anchor_ids=anchor_ids,
                query=query,
                session_id=session_id,
                out_dir=out_dir
            )
            return result
    except Exception as e:
        logger.error(f"Citation expansion failed: {e}")
        return None
