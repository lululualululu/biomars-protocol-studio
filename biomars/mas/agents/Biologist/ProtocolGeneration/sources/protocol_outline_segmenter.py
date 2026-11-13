"""
Protocol Outline Segmenter (Simplified)

This module provides a simple outline + segmentation pass for Markdown documents.
It splits documents by markdown headings (lines starting with #) into separate sections.

Key features:
- Detect all markdown headings (# ## ### etc.)
- Split document at each heading boundary
- Save each section as a separate file
- Generate outline JSON with structure info
"""

from __future__ import annotations
from biomars.mas.assets.protocol_generation.prompts.sources_protocol_outline_segmenter import SEGMENT_ANALYSIS_PROMPT_574

import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


# ===========================
# Text Utilities
# ===========================
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_control_chars(text: str) -> str:
    """Remove problematic control characters while preserving newlines and tabs."""
    if not text:
        return ""
    text = text.replace("\u00ad", "")  # soft hyphen
    return CONTROL_CHARS_RE.sub("", text)


def normalize_text(text: str) -> str:
    """Basic text normalization."""
    if not text:
        return ""
    # Remove soft hyphens and control chars
    text = strip_control_chars(text)
    # Normalize whitespace (but preserve newlines)
    lines = text.splitlines()
    normalized_lines = []
    for line in lines:
        # Collapse multiple spaces to single space
        line = re.sub(r"[ \t]+", " ", line)
        normalized_lines.append(line.rstrip())
    return "\n".join(normalized_lines)


# ===========================
# Heading Detection
# ===========================
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
WRAPPER_SECTION_TITLES = {"full text", "fulltext"}
LOW_STRUCTURE_PROMOTION_MAX_HEADINGS = 4
LOW_STRUCTURE_PROMOTION_MIN_CHARS = 12000

COMMON_BARE_SECTION_TITLES = {
    "abstract",
    "introduction",
    "development of the protocol",
    "experimental design",
    "starting material",
    "materials",
    "material",
    "reagents",
    "reagent setup",
    "equipment",
    "procedure",
    "protocol",
    "methods",
    "method",
    "timing",
    "troubleshooting",
    "anticipated results",
    "expected results",
    "notes",
    "references",
}

BARE_TIMEPOINT_RE = re.compile(
    r"^(?:from\s+)?(?:days?|weeks?|hours?)\s*\d+(?:\s*[–-]\s*\d+)?(?:\s*[:(].*)?$",
    flags=re.IGNORECASE,
)
BARE_NUMBERED_TITLE_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)*\.?)\s+(?P<title>.+)$")
BARE_ACTION_START_RE = re.compile(
    r"^(add|adjust|aliquot|aspirate|centrifuge|change|collect|count|cut|"
    r"continue|discard|dissociate|dispense|filter|fix|incubate|isolate|make|measure|"
    r"mince|mix|observe|pass|pellet|pipet|pipette|place|plate|prewarm|"
    r"prepare|recover|remove|replace|resuspend|seed|stain|supplement|thaw|"
    r"transfer|use|wash)\b",
    flags=re.IGNORECASE,
)
BARE_STEP_CONTEXT_START_RE = re.compile(
    r"^(at|if|on|standard|to|until|when|while|without)\b",
    flags=re.IGNORECASE,
)
NUMBERED_LIST_UNIT_RE = re.compile(
    r"\b(?:ml|ul|µl|l|mm|µm|um|nm|mg|µg|ug|ng|dm[em]|fbs|pbs|supplement)\b|"
    r"\d",
    flags=re.IGNORECASE,
)
TABLE_OR_FIGURE_RE = re.compile(r"^(fig(?:ure)?\.?|table|box)\b", flags=re.IGNORECASE)


def get_heading_level(line: str) -> int:
    """
    Get the heading level from a markdown line.
    Returns 0 if not a heading, 1-6 for # to ######
    """
    line = (line or "").strip()
    match = HEADING_RE.match(line)
    if match:
        return len(match.group(1))
    return 0


def get_heading_title(line: str) -> str:
    """
    Extract the title text from a heading line.
    Returns empty string if not a heading.
    """
    line = (line or "").strip()
    match = HEADING_RE.match(line)
    if match:
        return match.group(2).strip()
    return line.strip()


def normalize_heading_title(title: str) -> str:
    """Normalize a heading title for wrapper-title comparisons."""
    return re.sub(r"\s+", " ", (title or "").strip()).lower()


def is_heading(line: str) -> bool:
    """Check if a line is a markdown heading."""
    return get_heading_level(line) > 0


def _canonical_plain_line(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "").strip())


def _looks_like_bare_heading(line: str) -> bool:
    """Detect standalone protocol headings in PDF text that lost Markdown marks."""
    title = _canonical_plain_line(line)
    if not title or len(title) > 120:
        return False
    if get_heading_level(title) > 0:
        return False
    if "|" in title or title.endswith((".", ";", ",")):
        return False
    if TABLE_OR_FIGURE_RE.match(title):
        return False

    lowered = title.lower().strip(":")
    if lowered == "protocol" and title == title.upper():
        return False
    if lowered in COMMON_BARE_SECTION_TITLES:
        return True
    if BARE_TIMEPOINT_RE.match(title):
        return True

    numbered = BARE_NUMBERED_TITLE_RE.match(title)
    if numbered:
        number = numbered.group("num").rstrip(".")
        rest = numbered.group("title").strip()
        if not rest or len(rest) > 100 or rest.endswith((".", ";", ",")):
            return False
        first_word = re.split(r"\W+", rest, maxsplit=1)[0]
        if not first_word or first_word[:1].islower():
            return False
        if BARE_ACTION_START_RE.match(rest) or BARE_STEP_CONTEXT_START_RE.match(rest):
            return False
        if "." in number:
            return True
        rest_lower = rest.lower().strip(":")
        if rest_lower in COMMON_BARE_SECTION_TITLES:
            return True
        try:
            if int(number) > 9:
                return False
        except ValueError:
            pass
        if NUMBERED_LIST_UNIT_RE.search(rest):
            return False
        # Top-level numbered headings should be recognized by document shape,
        # not by a closed list of biological topics. Operational list items
        # and parameter-bearing lines have already been rejected above.
        return len(re.findall(r"\b[\w-]+\b", rest)) <= 12

    return False


def promote_bare_headings_for_low_structure_text(
    normalized_text: str,
    *,
    max_split_level: int = 2,
) -> str:
    """
    Promote standalone protocol headings when PDF extraction flattened structure.

    MinerU/no-TOC fallbacks can produce long text with section titles such as
    "Materials", "Procedure", "3.1.1 Cell Thawing", or "Day 0" but no Markdown
    heading markers. This lightweight pass only activates for low-structure,
    long documents so normal Markdown outlines keep their original hierarchy.
    """
    if not normalized_text or len(normalized_text) < LOW_STRUCTURE_PROMOTION_MIN_CHARS:
        return normalized_text

    lines = normalized_text.splitlines()
    markdown_headings = [
        line for line in lines
        if 0 < get_heading_level(line) <= max_split_level
        and normalize_heading_title(get_heading_title(line)) not in WRAPPER_SECTION_TITLES
    ]
    if len(markdown_headings) > LOW_STRUCTURE_PROMOTION_MAX_HEADINGS:
        return normalized_text

    promoted: List[str] = []
    changed = False
    for line in lines:
        if get_heading_level(line) > 0 or not _looks_like_bare_heading(line):
            promoted.append(line)
            continue
        promoted.append(f"## {_canonical_plain_line(line)}")
        changed = True
    return "\n".join(promoted) if changed else normalized_text


# ===========================
# Timepoint Extraction (kept for compatibility)
# ===========================
TIMEPOINT_RE = re.compile(
    r"\b("
    r"Day\s*\d+|"
    r"Days?\s*\d+\s*[–-]\s*\d+|"
    r"Week\s*\d+|"
    r"Weeks?\s*\d+\s*[–-]\s*\d+|"
    r"Hour\s*\d+|"
    r"Hours?\s*\d+\s*[–-]\s*\d+"
    r")\b",
    flags=re.IGNORECASE,
)


def extract_timepoints(text: str) -> List[str]:
    """Extract canonical timepoint labels in the order they appear."""
    if not text:
        return []
    out: List[str] = []
    for m in TIMEPOINT_RE.finditer(text):
        label = m.group(1).strip()
        label = re.sub(r"\s+", " ", label)
        label = label.replace("–", "-")
        if label not in out:
            out.append(label)
    return out


MAJOR_TIMEPOINT_HEADING_RE = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?\s*"
    r"("
    r"Day\s*\d+(?:\s*[–-]\s*\d+)?|"
    r"Days?\s*\d+(?:\s*[–-]\s*\d+)?|"
    r"Week\s*\d+(?:\s*[–-]\s*\d+)?|"
    r"Weeks?\s*\d+(?:\s*[–-]\s*\d+)?|"
    r"Hour\s*\d+(?:\s*[–-]\s*\d+)?|"
    r"Hours?\s*\d+(?:\s*[–-]\s*\d+)?"
    r")"
    r"(?:\s*\+|\s*onward)?\s*(?:\*\*)?\s*[:\.]?\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)


def extract_major_timepoints(text: str) -> List[str]:
    """Extract timepoint labels that appear as section headings."""
    if not text:
        return []
    out: List[str] = []
    for m in MAJOR_TIMEPOINT_HEADING_RE.finditer(text):
        label = m.group(1).strip()
        label = re.sub(r"\s+", " ", label)
        label = label.replace("–", "-")
        full_match = m.group(0).strip()
        if "+" in full_match or "onward" in full_match.lower():
            label = label + "+"
        if label not in out:
            out.append(label)
    return out


# ===========================
# Section Data Structure
# ===========================
@dataclass
class Section:
    """Represents a section of the document."""
    index: int
    title: str
    level: int  # heading level (1-6), 0 for preamble
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, exclusive
    line_count: int
    char_count: int
    content: str
    segment_path: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "level": self.level,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "line_count": self.line_count,
            "char_count": self.char_count,
            "segment_path": self.segment_path,
        }


# ===========================
# Core Segmentation Logic
# ===========================

# Maximum heading level to split on (1=# only, 2=# and ##, etc.)
MAX_SPLIT_LEVEL = 2  # Only split at Level 1 and Level 2 headings


def build_outline_from_markdown(
    markdown_text: str,
    max_split_level: int = MAX_SPLIT_LEVEL,
) -> Tuple[str, List[Section]]:
    """
    Build an outline by splitting markdown at heading boundaries.
    
    Only splits at Level 1 (#) and Level 2 (##) headings.
    Level 3 (###) and deeper headings are included in their parent section.
    
    Args:
        markdown_text: The full markdown text to segment
        max_split_level: Maximum heading level to split on (default: 2)
        
    Returns:
        Tuple of (normalized_text, list of Section objects)
    """
    # Normalize text
    normalized = normalize_text(markdown_text or "")
    normalized = promote_bare_headings_for_low_structure_text(
        normalized,
        max_split_level=max_split_level,
    )
    lines = normalized.splitlines()
    
    if not lines:
        return normalized, []
    
    # Find heading positions - ONLY Level 1 and Level 2.
    # Special case: some sources inject wrapper headings like "## Full Text"
    # above the real article body. When that happens, promote nested headings
    # by one level so real sections still split cleanly.
    heading_indices: List[Tuple[int, int, str]] = []  # (line_idx, level, title)
    wrapper_level: Optional[int] = None
    for i, line in enumerate(lines):
        level = get_heading_level(line)
        if level <= 0:
            continue

        if wrapper_level is not None and level <= wrapper_level:
            wrapper_level = None

        title = get_heading_title(line)
        normalized_title = normalize_heading_title(title)

        if level <= max_split_level and normalized_title in WRAPPER_SECTION_TITLES:
            wrapper_level = level
            continue

        effective_level = level
        if wrapper_level is not None and level > wrapper_level:
            effective_level = max(1, level - 1)

        if 0 < effective_level <= max_split_level:
            heading_indices.append((i, effective_level, title))
    
    # Build sections
    sections: List[Section] = []
    
    # Handle preamble (content before first heading)
    first_heading_idx = heading_indices[0][0] if heading_indices else len(lines)
    if first_heading_idx > 0:
        preamble_lines = lines[:first_heading_idx]
        preamble_content = "\n".join(preamble_lines).strip()
        if preamble_content:  # Only add if there's actual content
            sections.append(Section(
                index=1,
                title="Preamble",
                level=0,
                start_line=1,
                end_line=first_heading_idx,
                line_count=first_heading_idx,
                char_count=len(preamble_content),
                content=preamble_content,
            ))
    
    # Process each heading section (Level 1 and Level 2 only)
    for i, (line_idx, level, title) in enumerate(heading_indices):
        # Determine section end - next Level 1 or Level 2 heading
        if i + 1 < len(heading_indices):
            end_idx = heading_indices[i + 1][0]
        else:
            end_idx = len(lines)
        
        # Extract section content (includes all Level 3+ subsections)
        section_lines = lines[line_idx:end_idx]
        content = "\n".join(section_lines).strip()
        
        sections.append(Section(
            index=len(sections) + 1,
            title=title,
            level=level,
            start_line=line_idx + 1,
            end_line=end_idx,
            line_count=end_idx - line_idx,
            char_count=len(content),
            content=content,
        ))
    
    return normalized, sections


def persist_outline_artifacts(
    *,
    session_id: str,
    evidence_id: str,
    anchor_file_path: str,
    markdown_text: str,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Persist outline artifacts to disk.
    
    Writes:
    - <evidence_id>__normalized.md      (full normalized text)
    - <evidence_id>__outline.json       (outline structure)
    - <evidence_id>__segments/          (directory with individual sections)
        - 01_<title>.md
        - 02_<title>.md
        - ...
    
    Args:
        session_id: Session identifier
        evidence_id: Evidence/document identifier  
        anchor_file_path: Path to the original anchor file
        markdown_text: The markdown text to segment
        output_dir: Output directory (defaults to anchor file's directory)
        
    Returns:
        Dict with paths to generated artifacts
    """
    anchor_dir = os.path.dirname(os.path.abspath(anchor_file_path))
    out_dir = os.path.abspath(output_dir or anchor_dir)
    os.makedirs(out_dir, exist_ok=True)
    
    # Build outline
    normalized_text, sections = build_outline_from_markdown(markdown_text)
    
    # Write normalized full text
    normalized_path = os.path.join(out_dir, f"{evidence_id}__normalized.md")
    with open(normalized_path, "w", encoding="utf-8") as f:
        f.write(normalized_text)
    
    # Create segments directory
    segments_dir = os.path.join(out_dir, f"{evidence_id}__segments")
    os.makedirs(segments_dir, exist_ok=True)
    
    # Write each section as a separate file
    for sec in sections:
        # Create safe filename from title
        safe_title = re.sub(r"[^\w\s-]", "", sec.title)[:50].strip()
        safe_title = re.sub(r"[\s]+", "_", safe_title)
        if not safe_title:
            safe_title = "section"
        
        seg_name = f"{sec.index:02d}_{safe_title}.md"
        seg_path = os.path.join(segments_dir, seg_name)
        
        with open(seg_path, "w", encoding="utf-8") as f:
            f.write(sec.content + "\n")
        
        sec.segment_path = os.path.abspath(seg_path)
    
    # Build outline JSON
    outline = {
        "version": "2.0",
        "generated_at": datetime.now().isoformat(),
        "source": {
            "session_id": session_id,
            "evidence_id": evidence_id,
            "anchor_file_path": os.path.abspath(anchor_file_path),
            "normalized_file_path": os.path.abspath(normalized_path),
        },
        "stats": {
            "total_sections": len(sections),
            "total_lines": len(normalized_text.splitlines()),
            "total_chars": len(normalized_text),
        },
        "sections": [sec.to_dict() for sec in sections],
    }
    
    outline_path = os.path.join(out_dir, f"{evidence_id}__outline.json")
    with open(outline_path, "w", encoding="utf-8") as f:
        json.dump(outline, f, ensure_ascii=False, indent=2)
    
    return {
        "outline_path": os.path.abspath(outline_path),
        "normalized_path": os.path.abspath(normalized_path),
        "segments_dir": os.path.abspath(segments_dir),
        "sections_count": len(sections),
        "sections": sections,
    }


# ===========================
# Convenience Functions
# ===========================
def segment_markdown_file(
    input_path: str,
    output_dir: Optional[str] = None,
    evidence_id: Optional[str] = None,
    session_id: str = "default",
) -> Dict[str, Any]:
    """
    Convenience function to segment a markdown file directly.
    
    Args:
        input_path: Path to the markdown file
        output_dir: Output directory (defaults to input file's directory)
        evidence_id: ID for the evidence (defaults to filename stem)
        session_id: Session ID (defaults to "default")
        
    Returns:
        Dict with paths to generated artifacts
    """
    input_path = os.path.abspath(input_path)
    
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    
    with open(input_path, "r", encoding="utf-8") as f:
        markdown_text = f.read()
    
    if evidence_id is None:
        evidence_id = os.path.splitext(os.path.basename(input_path))[0]
    
    return persist_outline_artifacts(
        session_id=session_id,
        evidence_id=evidence_id,
        anchor_file_path=input_path,
        markdown_text=markdown_text,
        output_dir=output_dir,
    )


# ===========================
# Segment Analysis Utilities
# ===========================

# LLM prompt template for analyzing a single segment
SEGMENT_ANALYSIS_PROMPT = SEGMENT_ANALYSIS_PROMPT_574


def prepare_segments_for_analysis(
    outline_path: str,
    task_query: str = "",
) -> List[Dict[str, Any]]:
    """
    Load outline and prepare segment data for LLM analysis.
    
    Args:
        outline_path: Path to the __outline.json file
        
    Returns:
        List of dicts with segment info ready for LLM analysis
    """
    if not os.path.exists(outline_path):
        raise FileNotFoundError(f"Outline file not found: {outline_path}")
    
    with open(outline_path, "r", encoding="utf-8") as f:
        outline = json.load(f)
    
    segments_dir = os.path.dirname(outline_path)
    sections_data = outline.get("sections", [])
    
    result = []
    for sec in sections_data:
        seg_path = sec.get("segment_path", "")
        if not seg_path or not os.path.exists(seg_path):
            # Try to reconstruct path from segments directory
            idx = sec.get("index", 0)
            title = sec.get("title", "section")
            safe_title = re.sub(r"[^\w\s-]", "", title)[:50].strip()
            safe_title = re.sub(r"[\s]+", "_", safe_title)
            if not safe_title:
                safe_title = "section"
            evidence_id = os.path.basename(outline_path).replace("__outline.json", "")
            seg_name = f"{idx:02d}_{safe_title}.md"
            seg_path = os.path.join(segments_dir, f"{evidence_id}__segments", seg_name)
        
        # Read segment content
        content = ""
        if os.path.exists(seg_path):
            with open(seg_path, "r", encoding="utf-8") as f:
                content = f.read()
        
        body_text = "\n".join(
            line for line in content.splitlines()
            if line.strip() and not is_heading(line)
        ).strip()
        if not body_text:
            continue

        result.append({
            "segment_id": f"seg_{sec.get('index', 0):02d}",
            "segment_file": seg_path,
            "title": sec.get("title", "Untitled"),
            "level": sec.get("level", 0),
            "order": sec.get("index", 0),
            "char_count": sec.get("char_count", len(content)),
            "content": content,
            "prompt": SEGMENT_ANALYSIS_PROMPT.format(
                task_query=task_query or "Not provided; classify by the document's main protocol scope.",
                title=sec.get("title", "Untitled"),
                level=sec.get("level", 0),
                content=content[:15000],  # Limit content length for LLM
            ),
        })
    
    return result


def save_segment_analysis(
    evidence_id: str,
    source_file: str,
    segments_dir: str,
    analyses: List[Dict[str, Any]],
    output_dir: Optional[str] = None,
) -> str:
    """
    Save segment analysis results to JSON file.
    
    Args:
        evidence_id: Evidence/anchor ID
        source_file: Path to the original document
        segments_dir: Path to the segments directory
        analyses: List of segment analysis dicts (from LLM)
        output_dir: Output directory (defaults to segments_dir parent)
        
    Returns:
        Path to the saved analysis JSON file
    """
    out_dir = output_dir or os.path.dirname(segments_dir)
    os.makedirs(out_dir, exist_ok=True)
    
    bundle = {
        "evidence_id": evidence_id,
        "source_file": source_file,
        "segments_dir": segments_dir,
        "analyses": analyses,
        "total_segments": len(analyses),
        "analyzed_at": datetime.now().isoformat(),
    }
    
    output_path = os.path.join(out_dir, f"{evidence_id}__segment_analysis.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    
    return output_path


def load_segment_analysis(analysis_path: str) -> Dict[str, Any]:
    """
    Load segment analysis from JSON file.
    
    Args:
        analysis_path: Path to the __segment_analysis.json file
        
    Returns:
        Dict with segment analysis bundle data
    """
    if not os.path.exists(analysis_path):
        raise FileNotFoundError(f"Analysis file not found: {analysis_path}")
    
    with open(analysis_path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_segment_analysis_response(response_text: str) -> Dict[str, Any]:
    """
    Parse LLM response for segment analysis.
    
    Args:
        response_text: Raw LLM response text
        
    Returns:
        Parsed analysis dict
    """
    if not response_text:
        return {
            "content_summary": "Analysis unavailable",
            "content_types": ["OTHER"],
            "primary_view": "",
            "query_relevance": "",
            "suggested_uses": [],
            "key_entities": [],
            "has_tables": False,
            "has_lists": False,
            "has_parameters": False,
        }

    text = response_text.strip()
    
    # Try to extract JSON from markdown fences
    import re
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if json_match:
        text = json_match.group(1)
    
    # Try to find JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end+1]
    
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Return minimal fallback
        return {
            "content_summary": "Analysis failed to parse",
            "content_types": ["OTHER"],
            "primary_view": "",
            "query_relevance": "",
            "suggested_uses": [],
            "key_entities": [],
            "has_tables": False,
            "has_lists": False,
            "has_parameters": False,
        }
