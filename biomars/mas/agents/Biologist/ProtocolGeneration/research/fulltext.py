"""Fetch selected source text through the existing external clients."""

from typing import Any, Callable, List, Tuple
from logging import Logger
import asyncio
import os
import aiohttp
import re

from ..protocol_schemas import EvidenceItem
from .....tools.ProtocolGeneration.biomars_jina_client import BioMarsJinaClient
from .....tools.ProtocolGeneration.biomars_semantic_client import BioMarsSemanticClient


async def enrich_shortlisted_candidates_with_pubmed_oa(
    merged_items: List[EvidenceItem],
    anchor_ids: List[str],
    shortlist_limit: int = 3,
) -> None:
    if shortlist_limit <= 0 or not anchor_ids:
        return

    from .....tools.ProtocolGeneration.ncbi_client import NCBIClient

    by_id = {str(item.id): item for item in merged_items if getattr(item, "id", None)}
    ncbi = NCBIClient()

    for evidence_id in anchor_ids[:shortlist_limit]:
        item = by_id.get(str(evidence_id))
        if not item:
            continue

        meta = item.meta or {}
        external_ids = dict(meta.get("externalIds") or {})
        if external_ids.get("PubMedCentral"):
            continue

        title = (item.title or "").strip()
        if not title:
            continue

        exact_title_query = f"\"{title}\"[Title]"
        pmids = await asyncio.to_thread(ncbi.search_pubmed, exact_title_query, 3)
        if not pmids:
            continue

        pmid = str(pmids[0])
        external_ids["PubMed"] = pmid

        summary_rows = await asyncio.to_thread(ncbi.fetch_pubmed_meta, [pmid])
        if summary_rows:
            summary = summary_rows[0]
            doi = (summary.get("doi") or "").strip()
            if doi:
                external_ids["DOI"] = doi
                meta.setdefault("doi", doi)

        pmcid = await ncbi._pmid_to_pmcid(pmid)
        if pmcid:
            external_ids["PubMedCentral"] = pmcid

        oa_rows = await asyncio.to_thread(ncbi.pubmed_open_access, [pmid])
        if oa_rows:
            oa_row = oa_rows[0]
            best_pdf = oa_row.get("best_pdf")
            if best_pdf and not meta.get("openAccessPdf"):
                meta["openAccessPdf"] = {"url": best_pdf}
            license_name = oa_row.get("license")
            if license_name:
                meta["ncbi_oa_license"] = license_name

        meta["externalIds"] = external_ids
        item.meta = meta


async def get_unpaywall_pdf(doi: str, *, logger: Logger) -> str:
    """
        [NEW] Fetch PDF URL from Unpaywall API.
        """
    import aiohttp

    email = os.getenv("NCBI_EMAIL", "2646292869@qq.com") # Use same email as NCBI
    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()

                best_loc = data.get("best_oa_location", {}) or {}
                pdf_url = best_loc.get("url_for_pdf")
                return pdf_url or ""
    except Exception as e:
        logger.warning(f"Unpaywall check failed for DOI {doi}: {e}")
        return ""


async def fetch_text(session, url, headers = None, *, logger: Logger) -> str:
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                return await resp.text()
    except Exception as e:
        logger.warning(f"Fetch failed for {url}: {e}")
    return ""


def html_to_text(html: str, *, BeautifulSoup: Any) -> str:
    if not html: return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        # Remove scripts and styles
        for script in soup(["script", "style"]):
            script.decompose()
        return soup.get_text(separator="\n").strip()
    except Exception:
        # Fallback regex
        text = re.sub(r'<[^>]+>', '', html)
        return re.sub(r'\s+', ' ', text).strip()


def looks_like_fulltext(content: str) -> bool:
    if not content: return False
    if len(content) < 8000: return False

    keywords = ["introduction", "methods", "results", "discussion", "references"]
    hits = sum(1 for k in keywords if k in content.lower())
    return hits >= 3


async def fetch_anchor_fulltext(
    item: EvidenceItem,
    sem: BioMarsSemanticClient,
    jina: BioMarsJinaClient,
    *,
    fetch_text: Callable[..., Any],
    get_unpaywall_pdf: Callable[..., Any],
    html_to_text: Callable[..., Any],
    looks_like_fulltext: Callable[..., Any],
    logger: Logger,
) -> Tuple[str, str, str]:
    """
        Fetch full text using a Cascade Strategy:
        1. PMC HTML Direct (Gold)
        2. PMC XML API (Silver)
        3. Unpaywall PDF (Bronze)
        4. S2 OpenAccess PDF
        5. Landing Page (Fallback)
        """
    paper_id = None
    if item.id.startswith("sem_"):
        paper_id = item.id.replace("sem_", "")
    elif item.id.startswith("pmc_"):
        paper_id = item.id.replace("pmc_", "")

    # 1. Hydrate Metadata if missing IDs
    external_ids = (item.meta or {}).get("externalIds", {})

    # Ensure external_ids is not None
    if external_ids is None:
        external_ids = {}

    if not external_ids.get("PubMedCentral") and not external_ids.get("DOI") and paper_id:
        try:
            logger.info(f"Hydrating metadata for {paper_id}...")
            details = await sem.get_paper(paper_id, fields="externalIds,openAccessPdf,url")
            if details:
                external_ids = details.get("externalIds", {}) or {}
                # Update item.meta with hydrated data
                if item.meta:
                    item.meta["externalIds"] = external_ids
                    if details.get("openAccessPdf"):
                        item.meta["openAccessPdf"] = details.get("openAccessPdf")
        except Exception as e:
            logger.warning(f"Metadata hydration failed: {e}")

    # Extract IDs
    doi = external_ids.get("DOI") or (item.meta or {}).get("doi")
    pmid = external_ids.get("PubMed")
    pmc_id = external_ids.get("PubMedCentral")

    # Normalize IDs
    if pmc_id and not pmc_id.startswith("PMC"):
        pmc_id = f"PMC{pmc_id}"

    # Hydrate DOI from PMID if missing
    if pmid and not doi:
         try:
             from .....tools.ProtocolGeneration.ncbi_client import NCBIClient
             ncbi = NCBIClient()
             recovered_doi = await ncbi.recover_doi_from_pmid(pmid)
             if recovered_doi:
                 doi = recovered_doi
                 logger.info(f"Recovered DOI {doi} from PMID {pmid}")
         except Exception as e:
             logger.warning(f"DOI recovery failed: {e}")

    # Check for openAccessPdf in hydrated meta
    s2_pdf_url = ((item.meta or {}).get("openAccessPdf") or {}).get("url")

    # --- Step 1: PMC XML API (Gold Standard) ---
    # Updated to use eFetch (direct XML) via NCBIClient, which is cleaner than HTML scraping.
    # We MUST prioritize PMC ID because eFetch db=pmc requires it.
    # If only PMID is available, we pass it with prefix so OA service (fallback) can use it.
    target_id = pmc_id
    if not target_id and pmid:
        target_id = f"PMID:{pmid}"

    if target_id:
        try:
            from .....tools.ProtocolGeneration.ncbi_client import NCBIClient
            ncbi = NCBIClient()

            # fetch_pmc_fulltext_markdown uses eFetch first (if PMC ID) then OA
            # It now also handles PMID->PMCID conversion internally.
            content = await ncbi.fetch_pmc_fulltext_markdown(target_id)

            # Validation: Accept if decent length OR contains markdown headers
            is_valid_xml_content = False
            if content:
                if len(content) > 2000:
                    is_valid_xml_content = True
                elif len(content) > 500 and ("## " in content or "**ID:**" in content):
                    is_valid_xml_content = True

            if is_valid_xml_content:
                logger.info(f"Fetched full text via PMC XML (eFetch/OA) for {item.id}")
                logger.info(f"Fetched content length: {len(content)} chars via strategy: pmc_xml")
                logger.info("Skipping Jina/Unpaywall fallback as NCBI content is valid.")
                return "pmc_xml", "HIGH", content
            elif content:
                logger.warning(f"PMC XML content retrieved but too short/invalid ({len(content)} chars). Falling back.")

        except Exception as e:
            logger.warning(f"PMC XML fetch failed for {item.id}: {e}")

    # --- Step 2: PMC HTML Direct (Silver Standard) ---
    if pmc_id:
        logger.info(f"Attempting PMC HTML Direct for {pmc_id}...")
        pmc_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/"
        try:
            async with aiohttp.ClientSession() as session:
                html = await fetch_text(session, pmc_url, headers={"User-Agent": "Mozilla/5.0"})
                if html:
                    text = html_to_text(html)
                    if looks_like_fulltext(text):
                        logger.info(f"Fetched full text via PMC HTML for {item.id}")
                        logger.info(f"Fetched content length: {len(text)} chars via strategy: pmc_html_direct")
                        return "pmc_html_direct", "HIGH", text
        except Exception as e:
            logger.warning(f"PMC HTML fetch failed: {e}")

    # --- Step 3: Unpaywall / OA PDF (Bronze Standard) ---
    if doi:
        try:
            pdf_url = await get_unpaywall_pdf(doi)
            if pdf_url:
                logger.info(f"Unpaywall found PDF for {doi}: {pdf_url}")
                content = await jina.fetch_markdown(pdf_url)
                if content and len(content) > 5000:
                    logger.info(f"Fetched full text via Unpaywall PDF for {item.id}")
                    logger.info(f"Fetched content length: {len(content)} chars via strategy: unpaywall_pdf")
                    return "unpaywall_pdf", "HIGH", content
        except Exception as e:
            logger.warning(f"Unpaywall PDF fetch failed: {e}")

    # --- Step 4: Semantic Scholar OpenAccessPdf ---
    if s2_pdf_url and "pdf" in s2_pdf_url.lower():
        try:
            content = await jina.fetch_markdown(s2_pdf_url)
            if content and len(content) > 5000:
                logger.info(f"Fetched full text via S2 PDF for {item.id}")
                logger.info(f"Fetched content length: {len(content)} chars via strategy: s2_pdf")
                return "s2_pdf", "MEDIUM", content
        except Exception as e:
            logger.warning(f"S2 PDF fetch failed: {e}")

    # --- Step 5: Landing Page Fallback (Last Resort) ---
    logger.warning(f"Falling back to landing page for {item.id}. Content might be abstract-only.")
    content = await jina.fetch_markdown(item.url)
    if content:
         logger.info(f"Fetched content length: {len(content)} chars via strategy: landing_page")

    quality = "LOW"
    if content:
        if len(content) > 5000 and "References" in content:
            quality = "MEDIUM"
        elif len(content) < 3000:
            quality = "LOW (Abstract Only)"

    return "landing_page", quality, content or item.markdown
