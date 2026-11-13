import os
import time
import logging
import requests
import asyncio
import aiohttp
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class RateLimiter:
    def __init__(self, rps: float):
        self.rps = max(0.1, float(rps))
        self.win = 1.0
        self.ts = []
        
    def acquire(self):
        now = time.monotonic()
        self.ts = [t for t in self.ts if now - t < self.win]
        if len(self.ts) >= self.rps:
            sleep_for = self.win - (now - self.ts[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
        self.ts.append(time.monotonic())

class NCBIClient:
    """
    Client for NCBI E-utilities (PubMed) and PMC.
    Fixed to handle XML responses from OA service and robust PubMed searching.
    """
    def __init__(self):
        self.EUTILS_BASE = os.getenv("NCBI_META_BASE", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils")
        self.PMC_UTILS = os.getenv("NCBI_OA_BASE", "https://www.ncbi.nlm.nih.gov/pmc/utils")
        self.NCBI_EMAIL = os.getenv("NCBI_EMAIL", "example@example.com")
        
        # Normalize the API key to handle None
        self.META_API_KEY = (os.getenv("NCBI_META_API_KEY") or "").strip() or None
        self.OA_API_KEY = (os.getenv("NCBI_OA_API_KEY") or "").strip() or self.META_API_KEY
        
        self.META_HEADER_NAME = os.getenv("NCBI_META_HEADER_NAME", "X-API-KEY")
        
        # Select the rate limit based on API-key availability
        limit = 9.0 if self.META_API_KEY else 2.5
        self._meta_rl = RateLimiter(limit)
        self._oa_rl = RateLimiter(limit)

    def _ua(self):
        return {"User-Agent": f"Biomars/1.0 ({self.NCBI_EMAIL})"}

    def _meta_headers(self):
        h = self._ua()
        # E-utils recommends passing api_key as a URL parameter
        return h

    def _meta_params(self, extra=None):
        # Base parameters
        p = {"retmode": "json", "tool": "Biomars", "email": self.NCBI_EMAIL}
        if self.META_API_KEY:
            p["api_key"] = self.META_API_KEY
        if extra: 
            p.update(extra)
        return p

    async def recover_doi_from_pmid(self, pmid: str) -> Optional[str]:
        """
        Recover DOI from PMID using NCBI eSummary (JSON).
        """
        if not pmid: return None
        url = f"{self.EUTILS_BASE}/esummary.fcgi"
        params = {"db": "pubmed", "id": str(pmid), "retmode": "json", "api_key": self.META_API_KEY}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Parse result[pmid]['articleids']
                        try:
                            ids = data['result'][str(pmid)]['articleids']
                            for x in ids:
                                if x['idtype'] == 'doi': return x['value']
                        except: pass
        except Exception as e:
            logger.warning(f"DOI recovery failed: {e}")
        return None

    def search_pubmed(self, query: str, top_k: int = 10) -> List[str]:
        """
        Search PubMed. 
        Switched to specific exception handling and robust parameter passing.
        """
        self._meta_rl.acquire()
        
        # Normalize query whitespace to reduce malformed requests
        clean_query = " ".join(query.split())
        
        for attempt in range(3):
            try:
                return self._execute_esearch(clean_query, top_k)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 400:
                    logger.warning(f"[NCBI] esearch 400 for query '{clean_query[:50]}...'. Check query syntax.")
                    return []
                if attempt == 2:
                    logger.error(f"[NCBI] Search failed after retries: {e}")
                    return []
                time.sleep(1.5 * (attempt + 1))
            except Exception as e:
                if attempt == 2:
                    logger.error(f"[NCBI] Search failed: {e}")
                    return []
                time.sleep(1.5 * (attempt + 1))
        return []

    def _execute_esearch(self, query: str, top_k: int) -> List[str]:
        # Let requests handle URL encoding
        params = self._meta_params({"db": "pubmed", "term": query, "retmax": top_k})
        
        r = requests.get(f"{self.EUTILS_BASE}/esearch.fcgi", 
                         params=params, 
                         headers=self._meta_headers(), 
                         timeout=30)
        r.raise_for_status()
        
        # NCBI may return non-JSON content even with retmode=json
        try:
            data = r.json()
        except ValueError:
            logger.error(f"[NCBI] Response was not JSON: {r.text[:100]}")
            return []
            
        return data.get("esearchresult", {}).get("idlist", [])

    def fetch_pubmed_meta(self, pmids: List[str]) -> List[Dict[str, Any]]:
        if not pmids:
            return []
        self._meta_rl.acquire()
        
        params = self._meta_params({"db": "pubmed", "id": ",".join(pmids)})

        res = {}
        for attempt in range(3):
            try:
                r = requests.get(f"{self.EUTILS_BASE}/esummary.fcgi", 
                                 params=params, 
                                 headers=self._meta_headers(), 
                                 timeout=30)
                r.raise_for_status()
                res = r.json().get("result", {})
                break
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"[NCBI] Fetch meta failed: {e}")
                    return []
                time.sleep(1.5 * (attempt + 1))

        out = []
        for pid in pmids:
            # esummary may use string or integer identifiers
            it = res.get(pid) or res.get(str(pid))
            if not it: continue
            
            out.append({
                "pmid": str(pid),
                "title": it.get("title", ""),
                "journal": it.get("fulljournalname", ""),
                "pubdate": it.get("pubdate", ""),
                "doi": (it.get("elocationid", "") or "").replace("doi:", "").strip(),
                "link": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            })
        return out

    def fetch_pubmed_abstracts(self, pmids: List[str]) -> Dict[str, str]:
        """Fetch PubMed abstracts for a shortlist in one E-utilities request."""
        if not pmids:
            return {}
        self._meta_rl.acquire()
        params = self._meta_params(
            {
                "db": "pubmed",
                "id": ",".join(str(pmid) for pmid in pmids),
                "retmode": "xml",
            }
        )
        for attempt in range(3):
            try:
                response = requests.get(
                    f"{self.EUTILS_BASE}/efetch.fcgi",
                    params=params,
                    headers=self._meta_headers(),
                    timeout=30,
                )
                response.raise_for_status()
                root = ET.fromstring(response.text)
                abstracts: Dict[str, str] = {}
                for article in root.findall(".//PubmedArticle"):
                    pmid_node = article.find(".//MedlineCitation/PMID")
                    if pmid_node is None or not (pmid_node.text or "").strip():
                        continue
                    parts = []
                    for abstract_node in article.findall(
                        ".//Article/Abstract/AbstractText"
                    ):
                        text = " ".join(
                            token.strip()
                            for token in abstract_node.itertext()
                            if token and token.strip()
                        )
                        if not text:
                            continue
                        label = (abstract_node.attrib.get("Label") or "").strip()
                        parts.append(f"{label}: {text}" if label else text)
                    if parts:
                        abstracts[(pmid_node.text or "").strip()] = "\n".join(parts)
                return abstracts
            except Exception as exc:
                if attempt == 2:
                    logger.warning("[NCBI] Fetch abstracts failed: %s", exc)
                    return {}
                time.sleep(1.5 * (attempt + 1))
        return {}

    def _chunk(self, lst: List[str], n: int = 100) -> List[List[str]]:
        return [lst[i:i+n] for i in range(0, len(lst), n)]

    # ==========================================
    # Parse PMC OA responses as XML
    # ==========================================
    def pmc_oa_from_pmids(self, pmids: List[str]) -> Dict[str, Dict[str, Any]]:
        res: Dict[str, Dict[str, Any]] = {}
        if not pmids:
            return res
            
        url = f"{self.PMC_UTILS}/oa/oa.fcgi"
        
        # Process IDs in chunks for the OA endpoint
        for part in self._chunk(pmids, 50):
            self._oa_rl.acquire()
            
            # Build parameters without retmode=json, which oa.fcgi does not support
            # Include tool/email when sending PMIDs
            # Some requests require id="PMID:12345"; try the supplied ID directly here
            params = {
                "id": ",".join(part),
                "tool": "Biomars",
                "email": self.NCBI_EMAIL
            }
            if self.OA_API_KEY:
                params["api_key"] = self.OA_API_KEY

            try:
                r = requests.get(url, params=params, headers=self._ua(), timeout=30)
                r.raise_for_status()
                
                # --- XML parsing ---
                root = ET.fromstring(r.content)
                
                # Iterate over record elements
                for rec in root.iter("record"):
                    rec_id = rec.attrib.get("id") # Usually a PMC ID or the supplied ID
                    # Read the corresponding PMID if present in the XML
                    # The oa.fcgi id attribute usually matches the input identifier.
                    # Store the returned record.
                    
                    files = []
                    best_pdf = None
                    
                    for link in rec.findall("link"):
                        fmt = link.attrib.get("format", "")
                        href = link.attrib.get("href", "")
                        if href:
                            if href.startswith("ftp://"):
                                href = href.replace("ftp://", "https://")
                            files.append({"format": fmt, "url": href})
                            if fmt == "pdf":
                                best_pdf = href
                    
                    # Accept records only when a file is available
                    if files:
                        # Map to the input identifier where possible; oa.fcgi may return a PMC ID
                        # Use the identifier returned in the record
                        pmcid = rec.attrib.get("pmcid", "")
                        
                        # Store the result; callers may need to map the key back to a PMID
                        # Identifier matching is best-effort
                        res[rec_id] = {
                            "pmcid": pmcid,
                            "files": files,
                            "best_pdf": best_pdf,
                            "license": rec.attrib.get("license", "unknown")
                        }

            except Exception as e:
                logger.warning(f"[NCBI] PMC OA fetch failed for chunk: {e}")
                continue
                
        return res

    def pubmed_open_access(self, pmids: List[str]) -> List[Dict[str, Any]]:
        if not pmids:
            return []
        
        # An oa.fcgi key may not match the requested PMID
        # Research primarily uses Semantic Scholar; this lookup is a fallback
        via_pmid = self.pmc_oa_from_pmids(pmids)
        
        out = []
        for pmid in pmids:
            # Try alternate identifier keys
            e = via_pmid.get(pmid) or via_pmid.get(f"PMID:{pmid}") or {}
            out.append({
                "pmid": pmid,
                "pmcid": e.get("pmcid"),
                "best_pdf": e.get("best_pdf"),
                "license": e.get("license",""),
                "files": e.get("files", [])
            })
        return out

    async def _pmid_to_pmcid(self, pmid: str) -> Optional[str]:
        """
        Convert PMID to PMCID using NCBI ID Converter API.
        """
        url = f"{self.PMC_UTILS}/idconv/v1.0/"
        params = {
            "ids": pmid,
            "format": "json",
            "tool": "Biomars",
            "email": self.NCBI_EMAIL
        }
        if self.OA_API_KEY:
            params["api_key"] = self.OA_API_KEY
            
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    records = data.get("records", [])
                    if records:
                        return records[0].get("pmcid")
        except Exception as e:
            logger.warning(f"ID conversion failed for PMID {pmid}: {e}")
        return None

    def _xml_to_markdown(self, xml_content: str) -> str:
        """
        Convert PMC XML to Markdown using BeautifulSoup for better structure retention.
        Handles lists, tables, and nested sections.
        """
        if not xml_content:
            return ""

        # NOTE:
        # BeautifulSoup XML parsing requires an XML tree builder (typically lxml).
        # In minimal environments, this may fail with:
        # "Couldn't find a tree builder with the features you requested: xml"
        # To keep the pipeline working (and to preserve headings/newlines for downstream
        # segmentation), we provide a stdlib ElementTree fallback.
        try:
            soup = BeautifulSoup(xml_content, "xml")
            
            # Helper to process text with formatting
            def process_text(tag):
                if not tag: return ""
                # Replace formatting tags with markdown
                for bold in tag.find_all(['bold', 'b']):
                    bold.replace_with(f"**{bold.get_text()}**")
                for italic in tag.find_all(['italic', 'i']):
                    italic.replace_with(f"*{italic.get_text()}*")
                for sup in tag.find_all('sup'):
                    sup.replace_with(f"^{sup.get_text()}")
                for sub in tag.find_all('sub'):
                    sub.replace_with(f"_{sub.get_text()}")
                
                # Handle xrefs (citations) - simplify them
                for xref in tag.find_all('xref'):
                    # Option: keep citation numbers or remove
                    xref.replace_with(f"[{xref.get_text()}]")
                    
                return tag.get_text(separator=" ").strip()

            md = []
            
            # 1. Title
            article_title = soup.find("article-title")
            title_text = process_text(article_title) if article_title else "Untitled"
            md.append(f"# {title_text}\n")
            
            # Metadata
            pmcid_tag = soup.find("article-id", {"pub-id-type": "pmc"})
            pmcid = pmcid_tag.get_text() if pmcid_tag else "Unknown"
            md.append(f"> **ID:** PMC{pmcid} | **Source:** PubMed Central (XML)\n")
            
            # 2. Abstract
            abstract = soup.find("abstract")
            if abstract:
                md.append("## Abstract")
                # Handle structured abstract
                for sec in abstract.find_all("sec", recursive=False):
                    title = sec.find("title")
                    if title:
                        md.append(f"### {process_text(title)}")
                        title.decompose() # Remove title to avoid duplication
                    
                    for p in sec.find_all("p", recursive=False):
                        md.append(process_text(p) + "\n")
                
                # Handle unstructured abstract paragraphs
                for p in abstract.find_all("p", recursive=False):
                    md.append(process_text(p) + "\n")
                md.append("")

            # 3. Body
            body = soup.find("body")
            if body:
                md.append("## Full Text")
                
                def process_section(sec_node, level=3):
                    # Section Title
                    title_node = sec_node.find("title", recursive=False)
                    if title_node:
                        heading = "#" * min(level, 6)
                        md.append(f"\n{heading} {process_text(title_node)}\n")
                    
                    # Process children in order
                    for child in sec_node.children:
                        if child.name == "p":
                            md.append(process_text(child) + "\n")
                        elif child.name == "sec":
                            process_section(child, level + 1)
                        elif child.name == "list":
                            # Process Lists
                            list_type = child.get("list-type", "bullet")
                            for idx, item in enumerate(child.find_all("list-item", recursive=False)):
                                prefix = "- " if list_type != "order" else f"{idx+1}. "
                                # Item content might be complex, simplify to text
                                item_text = process_text(item)
                                md.append(f"{prefix}{item_text}")
                            md.append("")
                        elif child.name == "table-wrap":
                            # Simple Table Placeholder or Caption
                            label = child.find("label")
                            caption = child.find("caption")
                            label_text = label.get_text() if label else "Table"
                            caption_text = process_text(caption) if caption else ""
                            md.append(f"\n> **{label_text}**: {caption_text}\n")
                            # Extract table content if possible (simplified)
                            table = child.find("table")
                            if table:
                                # Very basic table text extraction
                                for row in table.find_all("tr"):
                                    cells = [process_text(c) for c in row.find_all(["td", "th"])]
                                    md.append(" | ".join(cells))
                                md.append("")
                        elif child.name == "fig":
                            # Figure Caption
                            label = child.find("label")
                            caption = child.find("caption")
                            label_text = label.get_text() if label else "Figure"
                            caption_text = process_text(caption) if caption else ""
                            md.append(f"\n> **{label_text}**: {caption_text}\n")

                # Start processing top-level sections
                for sec in body.find_all("sec", recursive=False):
                    process_section(sec)

            # 4. References
            ref_list = soup.find("ref-list")
            if ref_list:
                md.append("## References")
                for ref in ref_list.find_all("ref"):
                    ref_id = ref.get("id", "")
                    label_tag = ref.find("label")
                    
                    # Determine display ID (use label content if available, else XML ID)
                    display_id = ref_id
                    if label_tag:
                         label_text = label_tag.get_text().strip("[]().")
                         if label_text: 
                             display_id = label_text
                         # Remove label from parsing to avoid duplication
                         label_tag.decompose()
                    
                    # Try element-citation (structured) or mixed-citation (semi-structured)
                    citation = ref.find(["element-citation", "mixed-citation"])
                    
                    if citation:
                        # Extract Authors
                        authors = []
                        person_group = citation.find("person-group")
                        if person_group:
                            for name in person_group.find_all("name"):
                                surname = name.find("surname")
                                given = name.find("given-names")
                                if surname:
                                    s_text = surname.get_text()
                                    g_text = f" {given.get_text()[0]}." if given and given.get_text() else ""
                                    authors.append(f"{s_text}{g_text}")
                        
                        author_text = ", ".join(authors[:3])
                        if len(authors) > 3:
                            author_text += " et al"
                        
                        # Year
                        year_tag = citation.find("year")
                        year = year_tag.get_text() if year_tag else ""
                        
                        # Title
                        at_tag = citation.find("article-title")
                        title = process_text(at_tag) if at_tag else ""
                        
                        # Journal
                        source_tag = citation.find("source")
                        source = source_tag.get_text() if source_tag else ""
                        
                        # Construct line: [ID] Authors (Year). Title. *Source*.
                        ref_parts = []
                        if author_text:
                            ref_parts.append(f"{author_text}")
                        if year:
                            ref_parts.append(f"({year})")
                        
                        # Combine title and source
                        rest = []
                        if title: rest.append(title)
                        if source: rest.append(f"*{source}*")
                        
                        if not author_text and not title:
                            # Fallback: Extraction likely failed, use raw text
                            full_cit_text = process_text(citation)
                            md.append(f"[{display_id}] {full_cit_text}\n")
                        else:
                            # Assemble structured reference
                            prefix = " ".join(ref_parts)
                            suffix = ". ".join(rest)
                            line = f"{prefix}. {suffix}" if prefix and suffix else (prefix or suffix)
                            # Cleanup
                            line = line.replace("..", ".").replace(". .", ".")
                            md.append(f"[{display_id}] {line}\n")
                    else:
                         # No citation tag, just dump text
                         md.append(f"[{display_id}] {process_text(ref)}\n")
            
            return "\n".join(md)
        except Exception as e:
            logger.warning(f"XML to Markdown conversion failed (BS4): {e}")
            return self._xml_to_markdown_etree(xml_content)

    def _xml_to_markdown_etree(self, xml_content: str) -> str:
        """
        Convert PMC JATS XML to a lightweight Markdown using stdlib ElementTree.

        Goal: preserve newlines + hierarchical section titles so that downstream
        segmentation (by '#/##') can work even when BeautifulSoup/lxml is unavailable.
        """
        if not xml_content:
            return ""

        def local(tag: str) -> str:
            if not tag:
                return ""
            # Handle namespaces: "{ns}name" -> "name"
            return tag.split("}", 1)[-1] if "}" in tag else tag

        def itertext(elem) -> str:
            if elem is None:
                return ""
            s = " ".join(" ".join(t.split()) for t in elem.itertext() if t and t.strip())
            return s.strip()

        try:
            root = ET.fromstring(xml_content)
        except Exception as e:
            logger.warning(f"XML to Markdown conversion failed (ElementTree parse): {e}")
            # Return a trimmed fallback to avoid storing megabytes of raw XML
            return (xml_content[:200000] + ("\n\n[TRUNCATED]\n" if len(xml_content) > 200000 else "")).strip()

        # Find common nodes
        title_elem = next((el for el in root.iter() if local(el.tag) == "article-title"), None)
        pmcid_elem = next(
            (
                el
                for el in root.iter()
                if local(el.tag) == "article-id" and (el.attrib or {}).get("pub-id-type") == "pmc"
            ),
            None,
        )
        abstract_elem = next((el for el in root.iter() if local(el.tag) == "abstract"), None)
        body_elem = next((el for el in root.iter() if local(el.tag) == "body"), None)
        ref_list_elem = next((el for el in root.iter() if local(el.tag) == "ref-list"), None)

        md: List[str] = []
        title_text = itertext(title_elem) or "Untitled"
        md.append(f"# {title_text}")
        pmcid_text = (itertext(pmcid_elem) or "Unknown").strip()
        md.append(f"> **ID:** PMC{pmcid_text} | **Source:** PubMed Central (XML)")
        md.append("")

        # Abstract
        if abstract_elem is not None:
            md.append("## Abstract")
            # Some JATS abstracts contain nested <sec>
            secs = [c for c in list(abstract_elem) if local(getattr(c, "tag", "")) == "sec"]
            if secs:
                for sec in secs:
                    st = next((c for c in list(sec) if local(getattr(c, "tag", "")) == "title"), None)
                    st_text = itertext(st)
                    if st_text:
                        md.append(f"### {st_text}")
                    # paragraphs directly under sec
                    for p in [c for c in list(sec) if local(getattr(c, "tag", "")) == "p"]:
                        pt = itertext(p)
                        if pt:
                            md.append(pt)
                            md.append("")
            else:
                # Unstructured paragraphs directly under <abstract>
                for p in [c for c in list(abstract_elem) if local(getattr(c, "tag", "")) == "p"]:
                    pt = itertext(p)
                    if pt:
                        md.append(pt)
                        md.append("")

        # Body with hierarchical sections
        def render_sec(sec, level: int) -> None:
            # Title
            st = next((c for c in list(sec) if local(getattr(c, "tag", "")) == "title"), None)
            st_text = itertext(st)
            if st_text:
                heading = "#" * max(2, min(level, 6))
                md.append(f"{heading} {st_text}")
                md.append("")

            for child in list(sec):
                t = local(getattr(child, "tag", ""))
                if t == "title":
                    continue
                if t == "p":
                    pt = itertext(child)
                    if pt:
                        md.append(pt)
                        md.append("")
                elif t == "sec":
                    render_sec(child, level + 1)
                elif t == "list":
                    # list-item may contain <p> or nested tags; itertext works
                    items = [c for c in child.iter() if local(getattr(c, "tag", "")) == "list-item"]
                    for li in items:
                        li_text = itertext(li)
                        if li_text:
                            md.append(f"- {li_text}")
                    if items:
                        md.append("")

        if body_elem is not None:
            md.append("## Full Text")
            md.append("")
            top_secs = [c for c in list(body_elem) if local(getattr(c, "tag", "")) == "sec"]
            if top_secs:
                for sec in top_secs:
                    render_sec(sec, level=3)
            else:
                # Worst case: no <sec>, just dump paragraphs to maintain newlines
                for p in [c for c in body_elem.iter() if local(getattr(c, "tag", "")) == "p"]:
                    pt = itertext(p)
                    if pt:
                        md.append(pt)
                        md.append("")

        # References (lightweight)
        if ref_list_elem is not None:
            md.append("## References")
            md.append("")
            for ref in [c for c in ref_list_elem.iter() if local(getattr(c, "tag", "")) == "ref"]:
                label = next((c for c in list(ref) if local(getattr(c, "tag", "")) == "label"), None)
                label_text = itertext(label).strip("[](). ") if label is not None else ""
                cit = next(
                    (
                        c
                        for c in ref.iter()
                        if local(getattr(c, "tag", "")) in ("element-citation", "mixed-citation")
                    ),
                    None,
                )
                cit_text = itertext(cit) if cit is not None else itertext(ref)
                if cit_text:
                    if label_text:
                        md.append(f"- [{label_text}] {cit_text}")
                    else:
                        md.append(f"- {cit_text}")
            md.append("")

        # Final cleanup: remove excessive blank lines
        out_lines: List[str] = []
        blank = 0
        for line in md:
            if not str(line).strip():
                blank += 1
                if blank <= 2:
                    out_lines.append("")
                continue
            blank = 0
            out_lines.append(str(line).rstrip())

        return ("\n".join(out_lines)).strip() + "\n"

    async def fetch_pmc_via_efetch(self, pmid_or_pmcid: str) -> Optional[str]:
        """
        Fetch full text XML directly from NCBI eFetch API (db=pmc).
        This is more robust than OA service for fetching content if ID is known.
        """
        import aiohttp
        
        # Clean ID
        clean_id = pmid_or_pmcid.upper().replace("PMC", "").strip()
        if pmid_or_pmcid.startswith("PMID:"):
             clean_id = pmid_or_pmcid.replace("PMID:", "").strip()
             # Try to convert PMID to PMCID first
             converted_pmcid = await self._pmid_to_pmcid(clean_id)
             if converted_pmcid:
                 clean_id = converted_pmcid.replace("PMC", "")
                 logger.info(f"Converted PMID {pmid_or_pmcid} to PMCID {converted_pmcid} for eFetch")
             else:
                 # If conversion fails, eFetch db=pmc won't work with PMID.
                 # Return None to let OA service try (OA service accepts PMID).
                 logger.info(f"Could not convert {pmid_or_pmcid} to PMCID. Skipping eFetch.")
                 return None

        url = f"{self.EUTILS_BASE}/efetch.fcgi"
        params = {
            "db": "pmc",
            "id": clean_id,
            "retmode": "xml",
            "tool": "Biomars",
            "email": self.NCBI_EMAIL
        }
        if self.META_API_KEY:
            params["api_key"] = self.META_API_KEY

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=45) as resp:
                    if resp.status != 200:
                        logger.warning(f"eFetch failed for {clean_id}: status {resp.status}")
                        return None
                    
                    content = await resp.text()
                    if not content or "<pmc-articles>" not in content and "<article " not in content:
                        return None
                        
                    return self._xml_to_markdown(content)
        except Exception as e:
            logger.warning(f"eFetch exception for {clean_id}: {e}")
            return None

    async def fetch_pmc_fulltext_markdown(self, pmid_or_pmcid: str) -> str:
        """
        Async fetch PMC full text and convert to Markdown.
        Strategy:
        1. Try eFetch (db=pmc) directly (fastest, most reliable for XML).
        2. Fallback to OA service (oa.fcgi) if eFetch fails or returns nothing.
        """
        # 1. Try Direct eFetch
        # eFetch is preferred because it gives the full XML directly.
        # It handles PMC IDs best. PMIDs need conversion (handled inside fetch_pmc_via_efetch).
        
        # We attempt eFetch for ALL inputs now, because we added PMID->PMCID conversion inside.
        logger.info(f"Attempting eFetch for {pmid_or_pmcid}")
        efetch_content = await self.fetch_pmc_via_efetch(pmid_or_pmcid)
        if efetch_content:
            return efetch_content

        # 2. Fallback to OA Service
        logger.info(f"eFetch failed/skipped. Attempting OA Service for {pmid_or_pmcid}")
        import aiohttp
        
        # Resolve to PMC OA URL
        url = f"{self.PMC_UTILS}/oa/oa.fcgi"
        params = {"id": pmid_or_pmcid, "tool": "Biomars", "email": self.NCBI_EMAIL}
        if self.OA_API_KEY:
            params["api_key"] = self.OA_API_KEY
            
        xml_url = ""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        return ""
                    content = await resp.read()
                    
                    # Parse XML to find PDF or XML link
                    root = ET.fromstring(content)
                    # <record>...<link format="xml" href="...">...</record>
                    for link in root.iter("link"):
                        if link.attrib.get("format") == "xml":
                            xml_url = link.attrib.get("href")
                            break
                            
                if not xml_url:
                    return ""
                    
                # Fetch XML content
                if xml_url.startswith("ftp://"):
                    xml_url = xml_url.replace("ftp://", "https://")
                    
                async with session.get(xml_url) as resp:
                    if resp.status != 200:
                        return ""
                    full_xml = await resp.text()
                    
                    # Convert to Markdown
                    return self._xml_to_markdown(full_xml)
                    
        except Exception as e:
            logger.warning(f"Async PMC fetch failed: {e}")
            return ""
