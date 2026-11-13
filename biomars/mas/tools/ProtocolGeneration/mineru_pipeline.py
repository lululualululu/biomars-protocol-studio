#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Protocol PDF to Markdown Converter v2.1
(MinerU-as-Truth + TOC LLM-assisted match + FINAL strict TOC-level enforcement + FINAL TOC insertion)

Core principles:
1) Final Markdown BODY always comes from MinerU direct .md output (verbatim, no reflow).
2) If TOC exists:
   - Pass A: Match TOC headings to MinerU-md heading candidates sequentially.
             Fuzzy first; if below threshold -> LLM-assisted match among limited candidates.
             Rewrite matched heading lines using TOC levels/titles.
             Insert unmatched TOC headings safely without moving MinerU content (neighbor/page hint + optional LLM).
   - Pass B (NEW FINAL PASS): Re-scan the *final* Markdown and strictly enforce heading levels by TOC.
             - For TOC headings found: force '#' count to TOC md_level (clamped by max_heading_depth)
             - For TOC headings still missing: insert them using page anchor + LLM insertion chooser (no body rewrite)
             - For headings not in TOC: demote so they can’t be higher than the current TOC context;
               optionally turn special blocks (BOX/FIGURE/TABLE/...) into bold lines instead of headings.
3) If NO TOC:
   - Fallback heading detection + optional LLM to fix heading levels only (no title rewrite, no reorder).

Deps:
    pip install pymupdf rich httpx python-dotenv rapidfuzz pyyaml

Env (.env):
    OPENROUTER_API_KEY=...
    OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
    OPENROUTER_MODEL=openai/gpt-4o

Usage:
    python protocol_pdf_to_md_v2_1.py --pdf paper.pdf --out ./output --llm_model openai/gpt-4o
    python protocol_pdf_to_md_v2_1.py --pdf paper.pdf --out ./output --strategy strategy.yaml
"""

import os
import re
import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import fitz  # pymupdf
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.theme import Theme

try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None

try:
    import httpx
    from dotenv import load_dotenv
except ImportError:
    httpx = None
    load_dotenv = None


# ===========================
# Console
# ===========================
custom_theme = Theme(
    {
        "info": "dim cyan",
        "warning": "yellow",
        "danger": "bold red",
        "success": "bold green",
        "phase": "bold magenta",
        "highlight": "bold cyan",
    }
)
console = Console(theme=custom_theme)

def postprocess_procedure_table_headings(markdown: str) -> str:
    if not markdown:
        return markdown

    lines = markdown.splitlines()
    out: List[str] = []
    in_procedure = False

    def _is_heading(line: str) -> Optional[Tuple[int, str]]:
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not m:
            return None
        return len(m.group(1)), m.group(2).strip()

    def _is_procedure_title(title: str) -> bool:
        t = (title or "").strip().lower()
        return "procedure" in t and "troubleshoot" not in t

    def _is_section_boundary(title: str) -> bool:
        t = (title or "").strip().lower()
        if _is_procedure_title(t):
            return False
        return any(
            tok in t
            for tok in (
                "troubleshooting",
                "timing",
                "materials",
                "reagents",
                "reagent",
                "equipment",
                "references",
                "reference",
                "abstract",
                "introduction",
                "results",
                "discussion",
                "conclusion",
                "acknowledg",
                "supplement",
            )
        )

    def _is_table_like(title: str) -> bool:
        t = (title or "").strip().lower()
        return any(tok in t for tok in ("table", "figure", "fig.", "box"))

    for line in lines:
        info = _is_heading(line)
        if info:
            level, title = info
            if level == 1:
                in_procedure = False
            elif level == 2:
                if _is_procedure_title(title):
                    in_procedure = True
                elif in_procedure and _is_table_like(title):
                    line = f"### {title}"
                elif in_procedure and _is_section_boundary(title):
                    in_procedure = False
        out.append(line)

    fixed = "\n".join(out)
    if markdown.endswith("\n"):
        return fixed + "\n"
    return fixed


# ===========================
# Default Strategy (NEUTRAL)
# ===========================
DEFAULT_STRATEGY: Dict[str, Any] = {
    # ✅ Recommend 6 to preserve real TOC depth (you can lower in YAML if needed)
    "max_heading_depth": 6,
    # MinerU extraction settings
    "mineru_mode": "txt",            # "txt" for text PDFs (stable), "ocr" for scanned, "auto" for auto-detect
    "mineru_disable_formula": True,  # Disable formula parsing to prevent LaTeX explosion
    # fuzzy threshold for TOC->MD heading matching (Pass A)
    "anchor_threshold": 78,
    # Optional: ignore some TOC items by regex (opt-in)
    "ignore_toc_regex": [],
    # Assets handling
    "copy_assets": True,
    "assets_dirname": "images",
    # Final markdown source must be MinerU md
    "final_source": "mineru_md",
    # Heading detection (universal, used in no-TOC fallback and TOC matching candidates)
    "heading_detection": {
        "use_hash_headings": True,
        "use_bold_single_line": True,
        "use_allcaps_plain_line": True,
        "use_numbered_titles": True,
        "max_title_len": 120,
        "require_isolated_line": True,
    },
    # Opt-in style rules via YAML only (empty by default)
    "special_blocks": {},
    # No-TOC fallback cleanup
    "fallback_normalize_blank_lines": True,
    "fallback_max_blank_run": 2,
    # LLM config
    "llm_model": None,
    "llm_timeout_sec": 120,
    # No-TOC fallback with LLM
    "fallback_use_llm": True,
    "llm_max_items": 160,
    # TOC unmatched insertion (Pass A)
    "toc_insert_unmatched": True,
    "toc_insert_use_llm": True,
    "toc_insert_page_hint": True,
    "toc_insert_window_size": 5,
    "toc_insert_options_max": 5,
    "toc_insert_context_lines": 12,
    "toc_insert_min_confidence": 0.35,
    # TOC matching LLM assist (Pass A)
    "toc_match_use_llm": True,
    "toc_match_lookahead": 120,
    "toc_match_options_max": 10,
    "toc_match_min_confidence": 0.45,
    "toc_match_soft_min_score": 55,
    "toc_match_use_page_hint": True,
    "toc_match_context_lines": 10,
    # ===========================
    # ✅ NEW FINAL PASS (Pass B)
    # ===========================
    "toc_final_enforce": True,
    "toc_final_exact_first": True,
    "toc_final_fuzzy_threshold": 88,
    "toc_final_lookahead": 240,
    "toc_final_dedupe_window": 8,
    "toc_final_insert_unmatched": True,
    "toc_final_insert_use_llm": True,
    "toc_final_insert_min_confidence": 0.40,
    "toc_final_insert_options_max": 5,
    "toc_final_insert_context_lines": 12,
    "toc_final_demote_non_toc": True,
    "toc_final_bold_special_blocks": True,
    "toc_final_special_regex": [
        r"^(box|figure|fig\.|table|troubleshooting|timing|pause point|critical step|caution|note)\b"
    ],
}


# ===========================
# Stats
# ===========================
@dataclass
class Stats:
    toc_entries_found: int = 0

    # Pass A
    toc_matched: int = 0
    toc_matched_by_llm: int = 0
    toc_reheaded: int = 0
    toc_inserted: int = 0
    toc_unmatched_remaining: int = 0

    # Pass B (FINAL)
    toc_final_matched: int = 0
    toc_final_releveled: int = 0
    toc_final_inserted: int = 0
    toc_final_unmatched_remaining: int = 0
    toc_final_special_bolded: int = 0
    toc_final_demoted_non_toc: int = 0

    mineru_md_path: str = ""
    mineru_blocks_loaded: int = 0

    llm_used_toc_match: bool = False
    llm_used_toc_insert: bool = False
    llm_used_no_toc_fix: bool = False
    llm_used_toc_final_insert: bool = False

    blank_lines_normalized: bool = False

    images_copied: int = 0
    images_copied_for_md_refs: int = 0

    output_md_path: str = ""
    qa_path: str = ""


# ===========================
# TOC node
# ===========================
@dataclass
class OutlineNode:
    title: str
    level: int
    md_level: int
    page: int  # 1-based page number


# ===========================
# Utils
# ===========================
def llm_enabled() -> bool:
    return httpx is not None and load_dotenv is not None


def load_strategy(strategy_path: Optional[Path]) -> Dict[str, Any]:
    if not strategy_path:
        return dict(DEFAULT_STRATEGY)

    if not strategy_path.exists():
        console.print(f"[warning]Strategy file not found, using defaults: {strategy_path}[/warning]")
        return dict(DEFAULT_STRATEGY)

    try:
        import yaml

        with open(strategy_path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg = dict(DEFAULT_STRATEGY)

        cfg.update({k: v for k, v in user_cfg.items() if k not in ("heading_detection", "special_blocks")})

        hd = dict(DEFAULT_STRATEGY.get("heading_detection", {}))
        hd.update(user_cfg.get("heading_detection", {}) or {})
        cfg["heading_detection"] = hd

        sb = dict(user_cfg.get("special_blocks", {}) or {})
        cfg["special_blocks"] = sb

        return cfg
    except Exception as e:
        console.print(f"[warning]Failed to load strategy ({e}), using defaults.[/warning]")
        return dict(DEFAULT_STRATEGY)


def regex_any(patterns: List[str], text: str, flags=re.IGNORECASE) -> bool:
    for p in patterns:
        if re.search(p, text or "", flags):
            return True
    return False


def normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[\s\-–—\.:;,\(\)\[\]{}]+", "", s)
    return s


def clean_heading_text(s: str) -> str:
    """Used for matching only; do NOT apply to body."""
    if not s:
        return ""
    t = s.strip()
    t = re.sub(r"^[\s\u2022\u00b7\u25cf\?\uf0b7]+", "", t).strip()
    t = re.sub(r"\s+", " ", t)

    raw = re.sub(r"[^A-Za-z\s]", " ", t)
    toks = [x for x in raw.split() if x]
    if len(toks) >= 2:
        alpha = [ch for ch in raw if ch.isalpha()]
        upper_ratio = sum(1 for ch in alpha if ch.isupper()) / max(1, len(alpha))
        avg_len = sum(len(x) for x in toks) / len(toks)
        # if MinerU breaks kerning into small caps tokens: join them
        if upper_ratio > 0.9 and avg_len <= 4.2:
            joined = "".join(toks)
            return joined.upper() if t.isupper() else joined
    return t


def score_match(a: str, b: str) -> float:
    if fuzz:
        return float(fuzz.token_set_ratio(a, b))
    import difflib
    return difflib.SequenceMatcher(a=a, b=b).ratio() * 100.0


def strip_md_emphasis(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^\*\*(.+)\*\*$", r"\1", s.strip())
    s = re.sub(r"^__(.+)__$", r"\1", s.strip())
    s = s.replace("**", "").replace("__", "").strip()
    return s


def is_allcaps_like(s: str) -> bool:
    t = strip_md_emphasis(s)
    if not t:
        return False
    letters = [ch for ch in t if ch.isalpha()]
    if len(letters) < 3:
        return False
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / max(1, len(letters))
    return upper_ratio > 0.9


def looks_like_numbered_title(s: str) -> bool:
    t = s.strip()
    if len(t) > 160:
        return False
    return bool(re.match(r"^\s*(\(?\d+(\.\d+){0,4}\)?|[IVXLCM]{1,8}|[A-Z])[\.\)]\s+\S", t))


def line_is_isolated(md_lines: List[str], idx: int) -> bool:
    prev_blank = True
    next_blank = True

    for j in range(idx - 1, max(-1, idx - 4), -1):
        if md_lines[j].strip() == "":
            prev_blank = True
            continue
        prev_blank = False
        break

    for j in range(idx + 1, min(len(md_lines), idx + 4)):
        if md_lines[j].strip() == "":
            next_blank = True
            continue
        next_blank = False
        break

    return prev_blank and next_blank


_HEADING_HASH_RE = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*$")
_HEADING_BOLD_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*$")


def parse_heading_candidate_line(line: str) -> Optional[Tuple[str, str, int]]:
    m = _HEADING_HASH_RE.match(line)
    if m:
        lvl = len(m.group(1))
        title = strip_md_emphasis(m.group(2))
        return ("hash", title, lvl)

    m = _HEADING_BOLD_RE.match(line)
    if m:
        title = strip_md_emphasis(m.group(1))
        return ("bold", title, 2)

    raw = line.strip()
    if raw and looks_like_numbered_title(raw):
        return ("numbered", strip_md_emphasis(raw), 3)

    if raw and is_allcaps_like(raw):
        return ("plain", strip_md_emphasis(raw), 2)

    return None


def classify_special(line_title: str, strategy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Opt-in style rules only. Default empty -> always None."""
    sb = strategy.get("special_blocks", {}) or {}
    for _, cfg in sb.items():
        pats = cfg.get("patterns", []) or []
        if pats and regex_any(pats, line_title):
            return cfg
    return None


def collect_md_heading_candidates(md_text: str, strategy: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines = md_text.splitlines(keepends=False)
    hd = strategy.get("heading_detection", {})
    max_len = int(hd.get("max_title_len", 120))
    require_iso = bool(hd.get("require_isolated_line", True))

    cands: List[Dict[str, Any]] = []
    for i, line in enumerate(lines):
        parsed = parse_heading_candidate_line(line)
        if not parsed:
            continue
        kind, title, lvl = parsed
        if not title:
            continue
        if len(title) > max_len:
            continue

        # opt-in special block: if keep_as_content & level==0 -> skip as heading candidate
        sp = classify_special(title, strategy)
        if sp:
            if bool(sp.get("keep_as_content", False)) and int(sp.get("level", 0)) == 0:
                continue
            forced = int(sp.get("level", 0))
            if forced > 0:
                lvl = forced

        if kind == "hash" and not hd.get("use_hash_headings", True):
            continue
        if kind == "bold" and not hd.get("use_bold_single_line", True):
            continue
        if kind == "plain" and not hd.get("use_allcaps_plain_line", True):
            continue
        if kind == "numbered" and not hd.get("use_numbered_titles", True):
            continue

        if require_iso and kind in ("bold", "plain", "numbered"):
            if not line_is_isolated(lines, i):
                continue

        if kind in ("bold", "plain", "numbered") and re.search(r"[.;:]\s*$", title):
            continue

        cands.append(
            {
                "line_idx": i,
                "kind": kind,
                "title": title,
                "title_norm": normalize_text(clean_heading_text(title)),
                "level_guess": lvl,
                "raw": line,
            }
        )
    return cands


# ===========================
# MinerU
# ===========================
def run_mineru(
    pdf_path: Path,
    out_dir: Path,
    backend: str,
    device: Optional[str],
    progress: Optional[Progress],
    task_id: Optional[int],
    mode: str = "txt",
    disable_formula: bool = True,
) -> None:
    """
    Run MinerU extraction.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["MINERU_MODEL_SOURCE"] = "modelscope"

    if disable_formula:
        env["MINERU_FORMULA_ENABLE"] = "false"

    cmd = [
        "mineru",
        "-p",
        str(pdf_path),
        "-o",
        str(out_dir),
        "-b",
        backend,
        "-m",
        mode,
        "--source",
        "modelscope",
    ]

    if disable_formula:
        cmd += ["-f", "false"]

    if device:
        cmd += ["-d", device]

    console.print(f"  [info]Running: {' '.join(cmd)}[/info]")
    timeout_sec = int(os.environ.get("MINERU_TIMEOUT_SEC", "180"))
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"MinerU timed out after {timeout_sec}s") from exc

    output_lines = (completed.stdout or "").splitlines()
    if progress and task_id is not None:
        for line in output_lines[-5:]:
            line = line.rstrip()
            if line:
                short = (line[:60] + "...") if len(line) > 60 else line
                progress.update(task_id, description=f"[cyan]MinerU:[/cyan] {short}")

    if completed.returncode != 0:
        raise SystemExit(f"MinerU failed with code {completed.returncode}")


def _snapshot_output_dir(pdf_out_dir: Path, limit: int = 20) -> str:
    if not pdf_out_dir.exists():
        return "<missing>"
    entries: List[str] = []
    for path in sorted(pdf_out_dir.rglob("*")):
        rel = path.relative_to(pdf_out_dir)
        suffix = "/" if path.is_dir() else ""
        entries.append(f"{rel}{suffix}")
        if len(entries) >= limit:
            break
    return ", ".join(entries) if entries else "<empty>"


def _clear_output_dir(pdf_out_dir: Path) -> None:
    if not pdf_out_dir.exists():
        return
    for child in pdf_out_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def find_mineru_artifacts(pdf_out_dir: Path, pdf_name: str) -> Path:
    candidates = list(pdf_out_dir.rglob(f"{pdf_name}_content_list.json"))
    if not candidates:
        anyc = list(pdf_out_dir.rglob("*_content_list.json"))
        if len(anyc) == 1:
            candidates = anyc
        else:
            snapshot = _snapshot_output_dir(pdf_out_dir)
            raise FileNotFoundError(
                f"Cannot locate *_content_list.json under: {pdf_out_dir}. "
                f"Available entries: {snapshot}"
            )
    return candidates[0]


def load_json_list(p: Path) -> List[Dict[str, Any]]:
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{p} is not a list JSON")
    return data


def find_mineru_md(pdf_out_dir: Path, pdf_path: Path, user_mineru_md: Optional[str]) -> Path:
    if user_mineru_md:
        p = Path(user_mineru_md).expanduser()
        if p.exists():
            return p.resolve()
        raise FileNotFoundError(f"--mineru_md not found: {p}")

    pdf_stem = pdf_path.stem
    direct = [p for p in pdf_out_dir.rglob(f"{pdf_stem}.md") if not p.name.endswith(".final.md")]
    if direct:
        return direct[0].resolve()

    md_candidates = [p for p in pdf_out_dir.rglob("*.md") if not p.name.endswith(".final.md")]
    if not md_candidates:
        snapshot = _snapshot_output_dir(pdf_out_dir)
        raise FileNotFoundError(
            f"Cannot locate MinerU .md under: {pdf_out_dir}. "
            f"Available entries: {snapshot}"
        )

    md_candidates.sort(key=lambda x: x.stat().st_size if x.exists() else 0, reverse=True)
    return md_candidates[0].resolve()


# ===========================
# TOC
# ===========================
def extract_toc_outline(pdf_path: Path, strategy: Dict[str, Any]) -> List[OutlineNode]:
    doc = fitz.open(str(pdf_path))
    toc = doc.get_toc(simple=False)
    doc.close()
    if not toc:
        return []

    ignore_re = strategy.get("ignore_toc_regex", []) or []
    max_depth = int(strategy["max_heading_depth"])
    nodes: List[OutlineNode] = []

    for row in toc:
        if len(row) < 3:
            continue
        level, title, page = row[0], row[1], row[2]
        if not isinstance(page, int) or page <= 0:
            continue

        t = clean_heading_text(str(title or ""))
        if not t:
            continue
        if ignore_re and regex_any(ignore_re, t):
            continue

        logical = int(level)
        md_level = min(max_depth, max(1, logical))
        nodes.append(OutlineNode(title=t, level=logical, md_level=md_level, page=page))

    return nodes


# ===========================
# Page-hint: estimate an md anchor line for a TOC page
# ===========================
def build_page_text_hints(blocks: List[Dict[str, Any]], max_per_page: int = 6) -> Dict[int, List[str]]:
    page_map: Dict[int, List[str]] = {}
    for b in blocks:
        if b.get("type") != "text":
            continue
        page_idx = b.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        txt = (b.get("text") or "").strip()
        if not txt or len(txt) < 20:
            continue
        page = page_idx + 1
        page_map.setdefault(page, [])
        if len(page_map[page]) >= max_per_page:
            continue
        snippet = re.sub(r"\s+", " ", txt)[:220]
        page_map[page].append(snippet)
    return page_map


def find_best_line_for_snippet(md_lines: List[str], snippet: str, window_size: int) -> Optional[int]:
    if not snippet:
        return None
    tgt = normalize_text(clean_heading_text(snippet))
    if not tgt:
        return None

    best_idx = None
    best_s = -1.0
    n = len(md_lines)
    w = max(2, int(window_size))

    for i in range(0, max(1, n - w + 1)):
        win = " ".join(x.strip() for x in md_lines[i : i + w] if x.strip())
        if not win:
            continue
        cand = normalize_text(clean_heading_text(win[:260]))
        if not cand:
            continue
        s = score_match(tgt, cand)
        if s > best_s:
            best_s = s
            best_idx = i
        if best_s >= 98:
            break

    if best_idx is not None and best_s >= 70:
        return best_idx
    return None


def estimate_page_anchor_line(md_text: str, blocks: List[Dict[str, Any]], page: int, window_size: int) -> Optional[int]:
    md_lines = md_text.splitlines(keepends=False)
    page_hints = build_page_text_hints(blocks)
    hints = page_hints.get(page, [])
    if not hints:
        return None

    best = None
    best_score = -1.0
    for snip in hints:
        idx = find_best_line_for_snippet(md_lines, snip, window_size=window_size)
        if idx is None:
            continue
        win = " ".join(x.strip() for x in md_lines[idx : idx + window_size] if x.strip())
        s = score_match(
            normalize_text(clean_heading_text(snip)),
            normalize_text(clean_heading_text(win[:260])),
        )
        if s > best_score:
            best_score = s
            best = idx

    if best is not None and best_score >= 70:
        return best
    return None


# ===========================
# LLM: OpenRouter JSON
# ===========================
def openrouter_chat_json(
    user_obj: Dict[str, Any],
    system_prompt: str,
    model: Optional[str],
    temperature: float,
    timeout: int,
) -> Dict[str, Any]:
    if not llm_enabled():
        raise RuntimeError("httpx/python-dotenv not installed")

    load_dotenv()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
    chosen = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")

    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY missing in environment/.env")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload = {
        "model": chosen,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_obj, ensure_ascii=False)},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }

    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        out = r.json()

    content = out["choices"][0]["message"]["content"]
    return json.loads(content)


# ===========================
# Pass A: LLM-assisted TOC matching (existing)
# ===========================
def extract_follow_context(md_lines_plain: List[str], line_idx: int, max_lines: int) -> str:
    ctx = []
    for j in range(line_idx + 1, min(len(md_lines_plain), line_idx + 1 + max_lines)):
        t = md_lines_plain[j].strip()
        if not t:
            continue
        if parse_heading_candidate_line(md_lines_plain[j]) is not None:
            break
        ctx.append(t)
        if len(ctx) >= 2:
            break
    return " | ".join(ctx)[:260]


def pick_topk_diverse(options: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    out = []
    seen_norm = set()
    for opt in options:
        n = opt.get("title_norm", "")
        if n in seen_norm:
            continue
        seen_norm.add(n)
        out.append(opt)
        if len(out) >= k:
            break
    return out


def llm_choose_best_toc_match(
    toc: OutlineNode,
    option_list: List[Dict[str, Any]],
    strategy: Dict[str, Any],
) -> Tuple[Optional[int], float]:
    model = strategy.get("llm_model")
    timeout = int(strategy.get("llm_timeout_sec", 120))

    system_prompt = """You are a strict title matcher.

Given ONE TOC heading and a list of candidate headings extracted from a Markdown (in reading order),
choose the SINGLE best matching candidate. Titles may have kerning issues (e.g., 'INTRO DUCT ION'),
minor punctuation differences, or missing words.

Rules:
- You MUST choose one of the provided options OR choose -1 if none matches.
- Do NOT invent or rewrite titles.
- Prefer semantic/title equivalence. Use local context as a tie-breaker.
Return JSON only:
{
  "choice": int,              // option_idx or -1
  "confidence": float,        // 0..1
  "reason": "short"
}
"""
    user_obj = {"toc": {"title": toc.title, "page": toc.page, "level": toc.md_level}, "options": option_list}

    out = openrouter_chat_json(
        user_obj=user_obj,
        system_prompt=system_prompt,
        model=model,
        temperature=0.0,
        timeout=timeout,
    )

    choice = int(out.get("choice", -1))
    conf = float(out.get("confidence", 0.0))

    if choice < 0:
        return None, conf

    for opt in option_list:
        if int(opt.get("option_idx", -2)) == choice:
            return int(opt["cand_global_index"]), conf

    return None, conf


def match_toc_to_md_headings_sequential_llm(
    toc_nodes: List[OutlineNode],
    md_cands: List[Dict[str, Any]],
    md_lines_plain: List[str],
    blocks: List[Dict[str, Any]],
    mineru_md_text: str,
    strategy: Dict[str, Any],
    stats: Stats,
) -> Tuple[Dict[int, int], List[int]]:
    matched: Dict[int, int] = {}
    if not toc_nodes or not md_cands:
        return matched, list(range(len(toc_nodes)))

    threshold = float(strategy.get("anchor_threshold", 78))
    use_llm = bool(strategy.get("toc_match_use_llm", True)) and llm_enabled()
    lookahead = int(strategy.get("toc_match_lookahead", 120))
    opt_max = int(strategy.get("toc_match_options_max", 10))
    min_conf = float(strategy.get("toc_match_min_confidence", 0.45))
    soft_min = float(strategy.get("toc_match_soft_min_score", 55))
    use_page_hint = bool(strategy.get("toc_match_use_page_hint", True))
    ctx_lines = int(strategy.get("toc_match_context_lines", 10))

    ptr = 0
    page_anchor_cache: Dict[int, Optional[int]] = {}

    for ti, toc in enumerate(toc_nodes):
        tgt = normalize_text(clean_heading_text(toc.title))

        win_end = min(len(md_cands), ptr + max(10, lookahead))
        scored: List[Tuple[int, float]] = []
        best_j = -1
        best_s = -1.0

        for j in range(ptr, win_end):
            c = md_cands[j]["title_norm"]
            if not c:
                continue
            if c == tgt:
                s = 100.0
            elif c.startswith(tgt) and tgt:
                s = 95.0
            elif tgt in c and tgt:
                s = 88.0
            else:
                s = score_match(tgt, c)

            scored.append((j, s))
            if s > best_s:
                best_s = s
                best_j = j
            if best_s >= 100.0:
                break

        if best_j >= 0 and best_s >= threshold:
            matched[ti] = best_j
            ptr = best_j + 1
            continue

        if not use_llm:
            continue

        if best_s < soft_min:
            continue

        page_anchor = None
        if use_page_hint:
            if toc.page not in page_anchor_cache:
                try:
                    page_anchor_cache[toc.page] = estimate_page_anchor_line(
                        md_text=mineru_md_text,
                        blocks=blocks,
                        page=toc.page,
                        window_size=int(strategy.get("toc_insert_window_size", 5)),
                    )
                except Exception:
                    page_anchor_cache[toc.page] = None
            page_anchor = page_anchor_cache.get(toc.page)

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[: min(len(scored), max(opt_max * 2, 20))]

        option_objs = []
        for j, s in top:
            li = md_cands[j]["line_idx"]
            option_objs.append(
                {
                    "cand_index": j,
                    "line_idx": li,
                    "title": md_cands[j]["title"],
                    "title_norm": md_cands[j]["title_norm"],
                    "score": round(float(s), 2),
                    "context": extract_follow_context(md_lines_plain, li, ctx_lines),
                    "dist_to_page_anchor": None if page_anchor is None else abs(li - page_anchor),
                }
            )

        option_objs.sort(
            key=lambda o: (-o["score"], o["dist_to_page_anchor"] if o["dist_to_page_anchor"] is not None else 10**9)
        )
        option_objs = pick_topk_diverse(option_objs, opt_max)

        llm_options = []
        for idx, o in enumerate(option_objs):
            llm_options.append(
                {
                    "option_idx": idx,
                    "cand_global_index": int(o["cand_index"]),
                    "title": o["title"],
                    "score": o["score"],
                    "context_after": o["context"],
                    "dist_to_page_anchor": o["dist_to_page_anchor"],
                }
            )

        try:
            chosen_global, conf = llm_choose_best_toc_match(toc, llm_options, strategy)
        except Exception:
            chosen_global, conf = None, 0.0

        if chosen_global is None or conf < min_conf:
            continue

        if chosen_global < ptr or chosen_global >= win_end:
            continue

        matched[ti] = chosen_global
        ptr = chosen_global + 1

        stats.llm_used_toc_match = True
        stats.toc_matched_by_llm += 1

    unmatched = [i for i in range(len(toc_nodes)) if i not in matched]
    stats.toc_matched = len(matched)
    return matched, unmatched


# ===========================
# TOC insertion helpers (used by Pass A and Pass B)
# ===========================
def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def build_insert_candidates(
    md_lines: List[str],
    prev_line: Optional[int],
    next_line: Optional[int],
    page_anchor: Optional[int],
    max_options: int,
) -> List[int]:
    n = len(md_lines)
    lo = 0 if prev_line is None else clamp(prev_line + 1, 0, n)
    hi = n if next_line is None else clamp(next_line, 0, n)
    if lo > hi:
        lo, hi = hi, lo

    cands = set()
    cands.add(lo)
    cands.add(hi)
    cands.add((lo + hi) // 2)

    if page_anchor is not None:
        cands.add(clamp(page_anchor, lo, hi))

    cands.add(clamp(lo + 1, lo, hi))
    cands.add(clamp(hi - 1, lo, hi))

    out = sorted(cands)
    out = [x for x in out if lo <= x <= hi]

    if len(out) > max_options:
        picks = [out[0]]
        if len(out) > 2:
            picks.append(out[len(out) // 2])
        picks.append(out[-1])
        for x in out:
            if len(picks) >= max_options:
                break
            if x not in picks:
                picks.append(x)
        out = sorted(set(picks))
    return out


def excerpt_around(md_lines: List[str], line: int, k: int) -> Tuple[str, str]:
    n = len(md_lines)
    a = max(0, line - k)
    b = min(n, line + k)
    before = "\n".join(md_lines[a:line])
    after = "\n".join(md_lines[line:b])
    return before, after


def llm_choose_insertion_line(
    toc_item: OutlineNode,
    md_lines: List[str],
    candidates: List[int],
    prev_toc: Optional[OutlineNode],
    next_toc: Optional[OutlineNode],
    strategy: Dict[str, Any],
    ctx_lines_override: Optional[int] = None,
) -> Tuple[int, float]:
    model = strategy.get("llm_model")
    timeout = int(strategy.get("llm_timeout_sec", 120))
    ctx_k = int(ctx_lines_override if ctx_lines_override is not None else strategy.get("toc_insert_context_lines", 12))

    options = []
    for line in candidates:
        before, after = excerpt_around(md_lines, line, ctx_k)
        options.append({"line": line, "before": before[-1200:], "after": after[:1200]})

    system_prompt = """You are a strict insertion-point chooser for adding a missing TOC heading into an existing Markdown.

Rules:
- You MUST choose one of the given options by index.
- Do NOT propose new content other than selecting the best insertion location.
- Prefer inserting where the subsequent content belongs under this TOC heading.
- Use neighboring TOC titles (prev/next) and local context to decide.
- Output JSON only:
  {"option_index": int, "confidence": float (0..1), "note": "short string"}
"""

    user_obj = {
        "toc_heading": {"title": toc_item.title, "level": toc_item.md_level, "page": toc_item.page},
        "prev_toc": None if not prev_toc else {"title": prev_toc.title, "level": prev_toc.md_level, "page": prev_toc.page},
        "next_toc": None if not next_toc else {"title": next_toc.title, "level": next_toc.md_level, "page": next_toc.page},
        "options": [{"idx": i, **opt} for i, opt in enumerate(options)],
    }

    out = openrouter_chat_json(
        user_obj=user_obj,
        system_prompt=system_prompt,
        model=model,
        temperature=0.0,
        timeout=timeout,
    )

    oi = int(out.get("option_index", 0))
    conf = float(out.get("confidence", 0.4))
    oi = clamp(oi, 0, len(candidates) - 1)
    return candidates[oi], conf


# ===========================
# Pass A: Apply TOC reheader + insert unmatched
# ===========================
def apply_toc_reheader_and_insert(
    mineru_md_text: str,
    toc_nodes: List[OutlineNode],
    blocks: List[Dict[str, Any]],
    strategy: Dict[str, Any],
    stats: Stats,
) -> str:
    max_depth = int(strategy["max_heading_depth"])

    insert_unmatched = bool(strategy.get("toc_insert_unmatched", True))
    use_insert_llm = bool(strategy.get("toc_insert_use_llm", True)) and llm_enabled()
    use_page_hint = bool(strategy.get("toc_insert_page_hint", True))
    window_size = int(strategy.get("toc_insert_window_size", 5))
    max_options = int(strategy.get("toc_insert_options_max", 5))
    min_conf = float(strategy.get("toc_insert_min_confidence", 0.35))

    md_lines_kept = mineru_md_text.splitlines(keepends=True)
    md_lines_plain = [x.rstrip("\n") for x in md_lines_kept]

    cands = collect_md_heading_candidates(mineru_md_text, strategy)

    matched, unmatched = match_toc_to_md_headings_sequential_llm(
        toc_nodes=toc_nodes,
        md_cands=cands,
        md_lines_plain=md_lines_plain,
        blocks=blocks,
        mineru_md_text=mineru_md_text,
        strategy=strategy,
        stats=stats,
    )

    toc_to_line: Dict[int, int] = {}
    reheaded = 0
    for ti, ci in matched.items():
        line_idx = cands[ci]["line_idx"]
        toc = toc_nodes[ti]
        lvl = min(max_depth, max(1, toc.md_level))
        title = toc.title
        ending = "\n" if md_lines_kept[line_idx].endswith("\n") else ""
        md_lines_kept[line_idx] = ("#" * lvl) + " " + title + ending
        md_lines_plain[line_idx] = ("#" * lvl) + " " + title
        toc_to_line[ti] = line_idx
        reheaded += 1
    stats.toc_reheaded = reheaded

    if not insert_unmatched or not unmatched:
        stats.toc_unmatched_remaining = len(unmatched)
        return "".join(md_lines_kept)

    inserted = 0
    stats.llm_used_toc_insert = False

    def find_prev_next_line(ti: int) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        prev_line = None
        next_line = None
        prev_ti = None
        next_ti = None
        for j in range(ti - 1, -1, -1):
            if j in toc_to_line:
                prev_line = toc_to_line[j]
                prev_ti = j
                break
        for j in range(ti + 1, len(toc_nodes)):
            if j in toc_to_line:
                next_line = toc_to_line[j]
                next_ti = j
                break
        return prev_line, next_line, prev_ti, next_ti

    for ti in unmatched:
        toc = toc_nodes[ti]
        prev_line, next_line, prev_ti, next_ti = find_prev_next_line(ti)
        prev_toc = toc_nodes[prev_ti] if prev_ti is not None else None
        next_toc = toc_nodes[next_ti] if next_ti is not None else None

        page_anchor = None
        if use_page_hint:
            try:
                page_anchor = estimate_page_anchor_line(
                    md_text="\n".join(md_lines_plain),
                    blocks=blocks,
                    page=toc.page,
                    window_size=window_size,
                )
            except Exception:
                page_anchor = None

        candidates = build_insert_candidates(
            md_lines=md_lines_plain,
            prev_line=prev_line,
            next_line=next_line,
            page_anchor=page_anchor,
            max_options=max_options,
        )
        if not candidates:
            continue

        chosen = candidates[0]
        conf = 0.0
        if use_insert_llm and len(candidates) >= 2:
            try:
                chosen, conf = llm_choose_insertion_line(
                    toc_item=toc,
                    md_lines=md_lines_plain,
                    candidates=candidates,
                    prev_toc=prev_toc,
                    next_toc=next_toc,
                    strategy=strategy,
                )
                stats.llm_used_toc_insert = True
            except Exception:
                chosen, conf = candidates[0], 0.0

        if conf < min_conf:
            if page_anchor is not None:
                chosen = clamp(page_anchor, 0, len(md_lines_plain))
            elif prev_line is not None:
                chosen = clamp(prev_line + 1, 0, len(md_lines_plain))
            elif next_line is not None:
                chosen = clamp(next_line, 0, len(md_lines_plain))

        lvl = min(max_depth, max(1, toc.md_level))
        heading_line = ("#" * lvl) + " " + toc.title + "\n"

        norm = normalize_text(clean_heading_text(toc.title))
        near = []
        for k in range(max(0, chosen - 8), min(len(md_lines_plain), chosen + 8)):
            parsed = parse_heading_candidate_line(md_lines_plain[k])
            if not parsed:
                continue
            _, t, _ = parsed
            if normalize_text(clean_heading_text(t)) == norm:
                near.append(k)
        if near:
            toc_to_line[ti] = near[0]
            continue

        md_lines_kept.insert(chosen, heading_line)
        md_lines_plain.insert(chosen, heading_line.rstrip("\n"))

        inserted += 1
        for k in list(toc_to_line.keys()):
            if toc_to_line[k] >= chosen:
                toc_to_line[k] += 1
        toc_to_line[ti] = chosen

    stats.toc_inserted = inserted
    still = [i for i in range(len(toc_nodes)) if i not in toc_to_line]
    stats.toc_unmatched_remaining = len(still)

    return "".join(md_lines_kept)


# ===========================
# No-TOC fallback (heuristic + optional LLM)
# ===========================
def normalize_blank_lines(md_text: str, max_blank_run: int) -> Tuple[str, bool]:
    lines = md_text.splitlines(keepends=True)
    out = []
    blank_run = 0
    changed = False
    for ln in lines:
        if ln.strip() == "":
            blank_run += 1
            if blank_run <= max_blank_run:
                out.append(ln)
            else:
                changed = True
        else:
            blank_run = 0
            out.append(ln)
    return "".join(out), changed


def heuristic_fix_headings_without_toc(md_text: str, strategy: Dict[str, Any]) -> str:
    max_depth = int(strategy["max_heading_depth"])
    lines = md_text.splitlines(keepends=True)
    cands = collect_md_heading_candidates(md_text, strategy)
    for c in cands:
        li = c["line_idx"]
        kind = c["kind"]
        title = c["title"]
        lvl = c["level_guess"]
        if kind == "hash":
            lvl = min(max_depth, max(1, lvl))
        elif kind == "numbered":
            lvl = min(max_depth, 3)
        elif kind == "plain" and is_allcaps_like(title):
            lvl = 1
        else:
            lvl = min(max_depth, 2)
        ending = "\n" if lines[li].endswith("\n") else ""
        lines[li] = ("#" * lvl) + " " + title + ending
    return "".join(lines)


def llm_fix_headings_without_toc(md_text: str, strategy: Dict[str, Any], stats: Stats) -> str:
    max_depth = int(strategy["max_heading_depth"])
    model = strategy.get("llm_model")
    timeout = int(strategy.get("llm_timeout_sec", 120))
    max_items = int(strategy.get("llm_max_items", 160))

    cands = collect_md_heading_candidates(md_text, strategy)
    if not cands:
        return md_text

    lines = md_text.splitlines(keepends=True)

    def next_context(li: int) -> str:
        ctx = []
        for j in range(li + 1, min(len(lines), li + 20)):
            t = lines[j].strip()
            if not t:
                continue
            if parse_heading_candidate_line(lines[j]) is not None:
                break
            ctx.append(t)
            if len(ctx) >= 2:
                break
        return " | ".join(ctx)[:220]

    items = []
    for k, c in enumerate(cands[:max_items]):
        items.append(
            {
                "idx": k,
                "line_idx": c["line_idx"],
                "title": c["title"],
                "kind": c["kind"],
                "level_guess": c["level_guess"],
                "context": next_context(c["line_idx"]),
            }
        )

    system_prompt = f"""You are a strict Markdown heading hierarchy fixer.

Task:
- Fix ONLY heading levels (1..{max_depth}) for detected heading candidates.
- Do NOT rewrite titles. Do NOT invent new titles. Do NOT reorder.
- You may set keep=false ONLY for obvious immediate duplicates of the same title.
- If unsure, keep the level_guess.

Return JSON only:
{{
  "headings": [
    {{"idx": int, "level": int (1..{max_depth}), "keep": bool, "confidence": float (0..1)}}
  ],
  "notes": "string"
}}
"""
    user_obj = {"max_depth": max_depth, "headings_detected": items}

    try:
        out = openrouter_chat_json(
            user_obj=user_obj,
            system_prompt=system_prompt,
            model=model,
            temperature=0.0,
            timeout=timeout,
        )
        arr = out.get("headings", [])
        by_idx = {x.get("idx"): x for x in arr if isinstance(x, dict)}

        for k, c in enumerate(cands[:max_items]):
            rec = by_idx.get(k)
            if not rec:
                continue
            keep = rec.get("keep", True)
            lvl = int(rec.get("level", c["level_guess"]))
            lvl = max(1, min(max_depth, lvl))
            li = c["line_idx"]
            title = c["title"]
            ending = "\n" if lines[li].endswith("\n") else ""
            if keep is False:
                lines[li] = f"**{title}**" + ending
            else:
                lines[li] = ("#" * lvl) + " " + title + ending

        stats.llm_used_no_toc_fix = True
        return "".join(lines)

    except Exception as e:
        console.print(f"[warning]LLM no-TOC fix failed ({e}); using heuristic fix[/warning]")
        return heuristic_fix_headings_without_toc(md_text, strategy)


# ===========================
# Assets copy (no body rewrite)
# ===========================
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_HTML_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']")


def extract_md_image_paths(md_text: str) -> List[str]:
    paths = []
    for m in _MD_IMAGE_RE.finditer(md_text):
        p = m.group(1).strip()
        if p:
            paths.append(p)
    for m in _HTML_IMG_RE.finditer(md_text):
        p = m.group(1).strip()
        if p:
            paths.append(p)
    seen = set()
    out = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def resolve_img_source(raw: str, json_path: Path) -> Optional[Path]:
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute() and p.exists():
        return p
    fname = p.name
    bases = [json_path.parent] + list(json_path.parents)[:6]
    for base in bases:
        cand = base / raw
        if cand.exists():
            return cand
        for sub in ("images", "auto/images", "auto", ""):
            cand2 = base / sub / fname
            if cand2.exists():
                return cand2
    return None


def build_block_image_index(blocks: List[Dict[str, Any]], json_path: Path) -> Dict[str, Path]:
    idx: Dict[str, Path] = {}
    for b in blocks:
        raw = b.get("img_path")
        if not raw:
            continue
        src = resolve_img_source(raw, json_path)
        if not src:
            continue
        idx[src.name] = src
    return idx


def copy_assets_from_blocks(blocks: List[Dict[str, Any]], json_path: Path, out_dir: Path, assets_dirname: str) -> int:
    target_dir = out_dir / assets_dirname
    target_dir.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    copied = 0
    for b in blocks:
        raw = b.get("img_path")
        if not raw:
            continue
        src = resolve_img_source(raw, json_path)
        if not src:
            continue
        if src.name in seen:
            continue
        seen.add(src.name)
        dst = target_dir / src.name
        try:
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dst)
                copied += 1
        except Exception:
            continue
    return copied


def copy_assets_for_md_references(md_text: str, out_dir: Path, json_path: Path, blocks: List[Dict[str, Any]]) -> int:
    refs = extract_md_image_paths(md_text)
    if not refs:
        return 0
    idx = build_block_image_index(blocks, json_path)
    copied = 0
    for ref in refs:
        if re.match(r"^(https?|data):", ref, re.IGNORECASE):
            continue
        ref_path = Path(str(ref).split("#", 1)[0].split("?", 1)[0])
        if ref_path.is_absolute():
            continue
        src = idx.get(ref_path.name)
        if not src:
            continue
        dst = (out_dir / ref_path).resolve()
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dst)
                copied += 1
        except Exception:
            continue
    return copied


# ===========================
# ✅ Pass B: FINAL strict TOC enforcement + FINAL insertion
# ===========================
def _is_hash_heading(line: str) -> Optional[Tuple[int, str]]:
    m = _HEADING_HASH_RE.match(line)
    if not m:
        return None
    lvl = len(m.group(1))
    title = strip_md_emphasis(m.group(2))
    return lvl, title


def _collect_hash_headings(md_lines_plain: List[str]) -> List[Dict[str, Any]]:
    hs = []
    for i, ln in enumerate(md_lines_plain):
        parsed = _is_hash_heading(ln)
        if not parsed:
            continue
        lvl, title = parsed
        hs.append(
            {
                "hi": len(hs),
                "line_idx": i,
                "lvl": lvl,
                "title": title,
                "norm": normalize_text(clean_heading_text(title)),
            }
        )
    return hs


def _dedupe_nearby_same_title(md_lines_plain: List[str], line_idx: int, title_norm: str, window: int) -> Optional[int]:
    """If same normalized title exists within +/-window as a hash heading, return its line idx."""
    for k in range(max(0, line_idx - window), min(len(md_lines_plain), line_idx + window + 1)):
        parsed = _is_hash_heading(md_lines_plain[k])
        if not parsed:
            continue
        _, t = parsed
        if normalize_text(clean_heading_text(t)) == title_norm:
            return k
    return None


def enforce_toc_levels_and_insert_final(
    md_text: str,
    toc_nodes: List[OutlineNode],
    blocks: List[Dict[str, Any]],
    strategy: Dict[str, Any],
    stats: Stats,
) -> str:
    """
    Final pass:
    1) Re-locate TOC headings in the FINAL md (strict).
    2) Insert TOC headings still missing using page anchor + LLM insertion chooser.
    3) Force heading levels exactly to TOC levels.
    4) Demote non-TOC headings and/or bold special blocks (Box/Figure/Table/...) so TOC remains authoritative.
    """
    if not toc_nodes:
        return md_text

    max_depth = int(strategy.get("max_heading_depth", 6))

    exact_first = bool(strategy.get("toc_final_exact_first", True))
    fuzzy_thr = float(strategy.get("toc_final_fuzzy_threshold", 88))
    lookahead = int(strategy.get("toc_final_lookahead", 240))
    dedupe_window = int(strategy.get("toc_final_dedupe_window", 8))

    do_insert = bool(strategy.get("toc_final_insert_unmatched", True))
    use_insert_llm = bool(strategy.get("toc_final_insert_use_llm", True)) and llm_enabled()
    insert_min_conf = float(strategy.get("toc_final_insert_min_confidence", 0.40))
    insert_opt_max = int(strategy.get("toc_final_insert_options_max", 5))
    insert_ctx_lines = int(strategy.get("toc_final_insert_context_lines", 12))

    do_demote = bool(strategy.get("toc_final_demote_non_toc", True))
    do_bold_special = bool(strategy.get("toc_final_bold_special_blocks", True))
    special_re = strategy.get("toc_final_special_regex", []) or []

    # work on lines
    lines = md_text.splitlines(keepends=True)
    plain = [x.rstrip("\n") for x in lines]

    page_anchor_cache: Dict[int, Optional[int]] = {}

    def get_anchor(page: int) -> Optional[int]:
        if page not in page_anchor_cache:
            try:
                page_anchor_cache[page] = estimate_page_anchor_line(
                    md_text="\n".join(plain),
                    blocks=blocks,
                    page=page,
                    window_size=int(strategy.get("toc_insert_window_size", 5)),
                )
            except Exception:
                page_anchor_cache[page] = None
        return page_anchor_cache[page]

    # Build hash heading list for matching
    headings = _collect_hash_headings(plain)

    toc_to_line: Dict[int, int] = {}
    hptr = 0

    # ---------
    # 1) strict matching (sequential)
    # ---------
    for ti, toc in enumerate(toc_nodes):
        tgt_norm = normalize_text(clean_heading_text(toc.title))
        if not tgt_norm:
            continue

        # Search window in hash headings
        win_end = min(len(headings), hptr + max(30, lookahead))
        candidates = headings[hptr:win_end]

        chosen = None
        chosen_hi = None

        if exact_first:
            exact_hits = [h for h in candidates if h["norm"] == tgt_norm]
            if len(exact_hits) == 1:
                chosen = exact_hits[0]["line_idx"]
                chosen_hi = exact_hits[0]["hi"]
            elif len(exact_hits) > 1:
                # prefer closest to page anchor
                anchor = get_anchor(toc.page)
                exact_hits.sort(key=lambda h: abs(h["line_idx"] - anchor) if anchor is not None else h["line_idx"])
                chosen = exact_hits[0]["line_idx"]
                chosen_hi = exact_hits[0]["hi"]

        if chosen is None:
            # fuzzy best
            best = None
            best_s = -1.0
            for h in candidates:
                s = score_match(tgt_norm, h["norm"])
                if s > best_s:
                    best_s, best = s, h
            if best is not None and best_s >= fuzzy_thr:
                chosen = best["line_idx"]
                chosen_hi = best["hi"]

        if chosen is not None:
            # dedupe safety
            near = _dedupe_nearby_same_title(plain, chosen, tgt_norm, dedupe_window)
            if near is not None:
                chosen = near

            toc_to_line[ti] = chosen
            if chosen_hi is not None:
                hptr = chosen_hi + 1

    stats.toc_final_matched = len(toc_to_line)

    # ---------
    # 2) insert missing TOC headings (page anchor + LLM insertion chooser)
    # ---------
    if do_insert:
        inserted = 0

        def find_prev_next_line(ti: int) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
            prev_line = None
            next_line = None
            prev_ti = None
            next_ti = None
            for j in range(ti - 1, -1, -1):
                if j in toc_to_line:
                    prev_line = toc_to_line[j]
                    prev_ti = j
                    break
            for j in range(ti + 1, len(toc_nodes)):
                if j in toc_to_line:
                    next_line = toc_to_line[j]
                    next_ti = j
                    break
            return prev_line, next_line, prev_ti, next_ti

        missing = [i for i in range(len(toc_nodes)) if i not in toc_to_line]
        for ti in missing:
            toc = toc_nodes[ti]
            tgt_norm = normalize_text(clean_heading_text(toc.title))
            if not tgt_norm:
                continue

            # if already exists nearby as a hash heading, attach instead of insert
            # (sometimes matching failed due to sequential window constraints)
            found_any = None
            for i, ln in enumerate(plain):
                parsed = _is_hash_heading(ln)
                if not parsed:
                    continue
                _, t = parsed
                if normalize_text(clean_heading_text(t)) == tgt_norm:
                    found_any = i
                    break
            if found_any is not None:
                toc_to_line[ti] = found_any
                continue

            prev_line, next_line, prev_ti, next_ti = find_prev_next_line(ti)
            prev_toc = toc_nodes[prev_ti] if prev_ti is not None else None
            next_toc = toc_nodes[next_ti] if next_ti is not None else None

            page_anchor = get_anchor(toc.page)

            candidates = build_insert_candidates(
                md_lines=plain,
                prev_line=prev_line,
                next_line=next_line,
                page_anchor=page_anchor,
                max_options=insert_opt_max,
            )
            if not candidates:
                continue

            chosen = candidates[0]
            conf = 0.0
            if use_insert_llm and len(candidates) >= 2:
                try:
                    chosen, conf = llm_choose_insertion_line(
                        toc_item=toc,
                        md_lines=plain,
                        candidates=candidates,
                        prev_toc=prev_toc,
                        next_toc=next_toc,
                        strategy=strategy,
                        ctx_lines_override=insert_ctx_lines,
                    )
                    stats.llm_used_toc_final_insert = True
                except Exception:
                    chosen, conf = candidates[0], 0.0

            if conf < insert_min_conf:
                # fallback: page anchor > between prev/next
                if page_anchor is not None:
                    chosen = clamp(page_anchor, 0, len(plain))
                elif prev_line is not None:
                    chosen = clamp(prev_line + 1, 0, len(plain))
                elif next_line is not None:
                    chosen = clamp(next_line, 0, len(plain))

            lvl = min(max_depth, max(1, int(toc.md_level)))
            heading_line = ("#" * lvl) + " " + toc.title + "\n"

            # avoid inserting duplicates nearby
            near = _dedupe_nearby_same_title(plain, chosen, tgt_norm, dedupe_window)
            if near is not None:
                toc_to_line[ti] = near
                continue

            # insert line
            lines.insert(chosen, heading_line)
            plain.insert(chosen, heading_line.rstrip("\n"))

            # shift existing toc_to_line indices after insertion
            for k in list(toc_to_line.keys()):
                if toc_to_line[k] >= chosen:
                    toc_to_line[k] += 1
            toc_to_line[ti] = chosen

            inserted += 1

        stats.toc_final_inserted = inserted

    # ---------
    # 3) force TOC levels exactly (and titles)
    # ---------
    releveled = 0
    for ti, line_idx in toc_to_line.items():
        toc = toc_nodes[ti]
        lvl = min(max_depth, max(1, int(toc.md_level)))
        ending = "\n" if lines[line_idx].endswith("\n") else ""
        new_line = ("#" * lvl) + " " + toc.title + ending
        if lines[line_idx] != new_line:
            lines[line_idx] = new_line
            plain[line_idx] = new_line.rstrip("\n")
            releveled += 1
        else:
            # still refresh plain to keep consistent
            plain[line_idx] = new_line.rstrip("\n")

    stats.toc_final_releveled = releveled

    # ---------
    # 4) demote / bold headings not in TOC so TOC hierarchy stays authoritative
    # ---------
    toc_line_to_level = {toc_to_line[ti]: min(max_depth, max(1, int(toc_nodes[ti].md_level))) for ti in toc_to_line}
    toc_line_set = set(toc_line_to_level.keys())

    special_bolded = 0
    demoted = 0
    current_toc_level = 1

    for i, ln in enumerate(plain):
        parsed = _is_hash_heading(ln)
        if not parsed:
            continue
        cur_lvl, title = parsed
        title_clean = strip_md_emphasis(title)

        if i in toc_line_set:
            current_toc_level = int(toc_line_to_level[i])
            continue

        # special blocks -> bold
        if do_bold_special and special_re and regex_any(special_re, title_clean):
            ending = "\n" if lines[i].endswith("\n") else ""
            lines[i] = f"**{title_clean}**" + ending
            plain[i] = f"**{title_clean}**"
            special_bolded += 1
            continue

        if do_demote:
            min_allowed = min(max_depth, current_toc_level + 1)
            if cur_lvl < min_allowed:
                ending = "\n" if lines[i].endswith("\n") else ""
                lines[i] = ("#" * min_allowed) + " " + title_clean + ending
                plain[i] = ("#" * min_allowed) + " " + title_clean
                demoted += 1

    stats.toc_final_special_bolded = special_bolded
    stats.toc_final_demoted_non_toc = demoted

    still_missing = [i for i in range(len(toc_nodes)) if i not in toc_to_line]
    stats.toc_final_unmatched_remaining = len(still_missing)

    return "".join(lines)


# ===========================
# Main
# ===========================
def print_phase(phase: int, total: int, description: str) -> None:
    console.print(f"\n[phase][Phase {phase}/{total}][/phase] {description}")


def main(
    pdf: str,
    out: str,
    backend: str = "pipeline",
    device: Optional[str] = None,
    mineru_mode: str = "txt",
    enable_formula: bool = False,
    strategy_path: Optional[str] = None,
    skip_mineru: bool = False,
    llm_model: Optional[str] = None,
    mineru_md: Optional[str] = None,
) -> None:
    pdf_path = Path(pdf).expanduser().resolve()
    out_root = Path(out).expanduser().resolve()

    if not pdf_path.exists():
        console.print(f"[danger]PDF not found: {pdf_path}[/danger]")
        return

    strategy = load_strategy(Path(strategy_path) if strategy_path else None)
    if llm_model:
        strategy["llm_model"] = llm_model

    # Override MinerU settings from CLI args
    strategy["mineru_mode"] = mineru_mode
    strategy["mineru_disable_formula"] = not enable_formula

    pdf_name = pdf_path.name
    pdf_stem = pdf_path.stem
    pdf_out_dir = out_root / pdf_name
    pdf_out_dir.mkdir(parents=True, exist_ok=True)

    assets_dirname = str(strategy.get("assets_dirname", "images"))

    out_md = pdf_out_dir / f"{pdf_stem}.final.md"
    qa_path = pdf_out_dir / f"{pdf_stem}.qa.json"
    stats = Stats()

    console.print(
        Panel.fit(
            f"[bold]Protocol PDF to Markdown Converter v2.1 (TOC + FINAL strict enforcement)[/bold]\n"
            f"PDF: [highlight]{pdf_path}[/highlight]\n"
            f"Output: [highlight]{pdf_out_dir}[/highlight]",
            title="[success]Starting Pipeline[/success]",
        )
    )

    cols = [
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ]

    with Progress(*cols, console=console, expand=True) as progress:
        # Phase 1
        print_phase(1, 6, "MinerU Full Text Extraction")
        content_list_path: Optional[Path] = None
        mineru_md_path: Optional[Path] = None
        if not skip_mineru:
            t1 = progress.add_task("[magenta]Running MinerU...", total=None)
            last_artifact_error: Optional[Exception] = None
            for attempt in range(1, 3):
                if attempt > 1:
                    progress.update(t1, description="[yellow]Retrying MinerU after missing outputs...[/yellow]")
                    _clear_output_dir(pdf_out_dir)
                run_mineru(
                    pdf_path,
                    pdf_out_dir,
                    backend=backend,
                    device=device,
                    progress=progress,
                    task_id=t1,
                    mode=str(strategy.get("mineru_mode", "txt")),
                    disable_formula=bool(strategy.get("mineru_disable_formula", True)),
                )
                try:
                    content_list_path = find_mineru_artifacts(pdf_out_dir, pdf_name)
                    mineru_md_path = find_mineru_md(pdf_out_dir, pdf_path, mineru_md)
                    last_artifact_error = None
                    break
                except FileNotFoundError as exc:
                    last_artifact_error = exc
                    console.print(f"[warning]{exc}[/warning]")
            if last_artifact_error is not None:
                raise last_artifact_error
            progress.update(t1, total=1, completed=1, description="[success]MinerU complete[/success]")
        else:
            progress.add_task("[dim]Skipping MinerU (reusing existing)[/dim]", total=1, completed=1)
            content_list_path = find_mineru_artifacts(pdf_out_dir, pdf_name)
            mineru_md_path = find_mineru_md(pdf_out_dir, pdf_path, mineru_md)

        # blocks for page-hint/assets only
        assert content_list_path is not None
        blocks = load_json_list(content_list_path)
        stats.mineru_blocks_loaded = len(blocks)
        console.print(f"  [info]Loaded {len(blocks)} blocks (assets/page-hint only)[/info]")

        # MinerU md (truth)
        assert mineru_md_path is not None
        stats.mineru_md_path = str(mineru_md_path)
        mineru_md_text = mineru_md_path.read_text(encoding="utf-8", errors="replace")
        console.print(f"  [info]MinerU MD as truth: {mineru_md_path}[/info]")

        # Phase 2
        print_phase(2, 6, "TOC Detection (PyMuPDF)")
        t2 = progress.add_task("[cyan]Extracting TOC...", total=1)
        toc_nodes = extract_toc_outline(pdf_path, strategy)
        stats.toc_entries_found = len(toc_nodes)
        progress.update(t2, advance=1, description=f"[success]TOC: {len(toc_nodes)} entries[/success]")

        # Phase 3
        print_phase(3, 6, "TOC Strategy")
        if toc_nodes:
            progress.add_task("[success]TOC found: Pass A match+insert, Pass B strict enforce[/success]", total=1, completed=1)
        else:
            progress.add_task("[warning]No TOC: universal fallback[/warning]", total=1, completed=1)

        # Phase 4
        print_phase(4, 6, "No-TOC Fallback (Optional LLM)")
        final_md = mineru_md_text

        if (not toc_nodes) and bool(strategy.get("fallback_use_llm", True)) and llm_enabled():
            t4 = progress.add_task("[green]LLM fixing heading hierarchy...", total=1)
            final_md = llm_fix_headings_without_toc(final_md, strategy, stats)
            progress.update(t4, advance=1, description="[success]LLM no-TOC fix applied[/success]")
        else:
            progress.add_task("[dim]Skipping LLM no-TOC fix[/dim]", total=1, completed=1)
            if not toc_nodes:
                final_md = heuristic_fix_headings_without_toc(final_md, strategy)

        # Phase 5
        print_phase(5, 6, "Assets Handling")
        if strategy.get("copy_assets", True):
            t5 = progress.add_task("[cyan]Copying assets...", total=1)
            stats.images_copied = copy_assets_from_blocks(blocks, content_list_path, pdf_out_dir, assets_dirname)
            stats.images_copied_for_md_refs = copy_assets_for_md_references(final_md, pdf_out_dir, content_list_path, blocks)
            progress.update(
                t5,
                advance=1,
                description=f"[success]Images: {stats.images_copied} (+{stats.images_copied_for_md_refs} for md refs)[/success]",
            )
        else:
            progress.add_task("[dim]Skipping assets copy[/dim]", total=1, completed=1)

        # Phase 6
        print_phase(6, 6, "Exporting Markdown (MinerU-as-Truth)")
        t6 = progress.add_task("[magenta]Building final Markdown...", total=1)

        if toc_nodes:
            # Pass A
            final_md = apply_toc_reheader_and_insert(final_md, toc_nodes, blocks, strategy, stats)
            # ✅ Pass B (your requirement): strict enforce TOC levels + insert remaining using page anchor + LLM
            if bool(strategy.get("toc_final_enforce", True)):
                final_md = enforce_toc_levels_and_insert_final(final_md, toc_nodes, blocks, strategy, stats)
        else:
            if strategy.get("fallback_normalize_blank_lines", True):
                final_md, changed = normalize_blank_lines(final_md, int(strategy.get("fallback_max_blank_run", 2)))
                stats.blank_lines_normalized = changed

        final_md = postprocess_procedure_table_headings(final_md)
        out_md.write_text(final_md, encoding="utf-8")
        progress.update(t6, advance=1, description="[success]Markdown exported[/success]")

        stats.output_md_path = str(out_md)
        stats.qa_path = str(qa_path)
        qa_path.write_text(json.dumps(asdict(stats), ensure_ascii=False, indent=2), encoding="utf-8")

    console.print()
    console.rule("[bold green]Pipeline Complete[/bold green]")

    table = Table(title="Results", show_header=True, header_style="bold")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("MinerU MD (truth)", stats.mineru_md_path)
    table.add_row("TOC Entries", str(stats.toc_entries_found))

    table.add_row("TOC Matched (Pass A)", str(stats.toc_matched))
    table.add_row("TOC Matched by LLM (Pass A)", str(stats.toc_matched_by_llm))
    table.add_row("TOC Reheaded (Pass A)", str(stats.toc_reheaded))
    table.add_row("TOC Inserted (Pass A)", str(stats.toc_inserted))
    table.add_row("TOC Unmatched Remaining (Pass A)", str(stats.toc_unmatched_remaining))
    table.add_row("LLM Used (TOC match)", str(stats.llm_used_toc_match))
    table.add_row("LLM Used (TOC insert)", str(stats.llm_used_toc_insert))

    table.add_row("TOC Matched (FINAL)", str(stats.toc_final_matched))
    table.add_row("TOC Releveled (FINAL)", str(stats.toc_final_releveled))
    table.add_row("TOC Inserted (FINAL)", str(stats.toc_final_inserted))
    table.add_row("TOC Unmatched Remaining (FINAL)", str(stats.toc_final_unmatched_remaining))
    table.add_row("Special bolded (FINAL)", str(stats.toc_final_special_bolded))
    table.add_row("Non-TOC demoted (FINAL)", str(stats.toc_final_demoted_non_toc))
    table.add_row("LLM Used (FINAL insert)", str(stats.llm_used_toc_final_insert))

    table.add_row("LLM Used (no TOC fix)", str(stats.llm_used_no_toc_fix))
    table.add_row("Blank lines normalized", str(stats.blank_lines_normalized))
    table.add_row("Images copied", str(stats.images_copied))
    table.add_row("Images copied for md refs", str(stats.images_copied_for_md_refs))
    table.add_row("Output Markdown", stats.output_md_path)
    table.add_row("QA Report", stats.qa_path)
    console.print(table)
