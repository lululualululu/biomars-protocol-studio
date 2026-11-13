"""PDF parser and converter adapters; Protocol selection policy lives in its business package."""

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple


def count_pdf_pages(pdf_path: str) -> int:
    """Read only the PDF page tree; do not render or extract document content."""
    import fitz

    resolved_path = Path(pdf_path).expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"PDF not found: {resolved_path}")
    document = fitz.open(str(resolved_path))
    try:
        return int(document.page_count)
    finally:
        document.close()


def extract_text_with_pymupdf(pdf_path: str) -> Tuple[str, Dict[str, Any]]:
    """Extract a text-native PDF in reading order and return technical statistics."""
    import fitz

    resolved_path = Path(pdf_path).expanduser().resolve()
    document = fitz.open(str(resolved_path))
    try:
        page_texts = [page.get_text("text", sort=True) or "" for page in document]
    finally:
        document.close()

    raw_text = "\n\n".join(page_texts).strip()
    compact_page_lengths = [
        len(re.sub(r"\s+", "", page_text)) for page_text in page_texts
    ]
    compact_chars = len(re.sub(r"\s+", "", raw_text))
    text_pages = sum(length >= 40 for length in compact_page_lengths)
    replacement_chars = raw_text.count("\ufffd")
    return raw_text, {
        "page_count": len(page_texts),
        "text_page_count": text_pages,
        "compact_char_count": compact_chars,
        "replacement_char_count": replacement_chars,
    }


def convert_pdf_to_markdown_v2(
    pdf_path: str,
    out_dir: str,
    backend: str = "pipeline",
    device: Optional[str] = None,
    mineru_mode: str = "txt",
    enable_formula: bool = False,
    skip_mineru: bool = False,
    llm_model: Optional[str] = None,
    mineru_md: Optional[str] = None,
) -> Tuple[str, str, str]:
    return _convert_pdf_with_backend(
        pdf_path, out_dir, backend, device, mineru_mode, enable_formula,
        skip_mineru, llm_model, mineru_md,
    )


def _convert_pdf_with_backend(
    pdf_path: str,
    out_dir: str,
    backend: str = "pipeline",
    device: Optional[str] = None,
    mineru_mode: str = "txt",
    enable_formula: bool = False,
    skip_mineru: bool = False,
    llm_model: Optional[str] = None,
    mineru_md: Optional[str] = None,
    *,
    converter: Optional[Callable[..., None]] = None,
) -> Tuple[str, str, str]:
    """Shared adapter; the optional backend preserves legacy script patch hooks."""
    pdf_path_obj = Path(pdf_path).expanduser().resolve()
    out_root = Path(out_dir).expanduser().resolve()

    if not pdf_path_obj.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path_obj}")

    pdf_name = pdf_path_obj.name
    pdf_stem = pdf_path_obj.stem
    pdf_out_dir = out_root / pdf_name
    final_md_path = pdf_out_dir / f"{pdf_stem}.final.md"
    qa_path = pdf_out_dir / f"{pdf_stem}.qa.json"

    if converter is None:
        from .mineru_pipeline import main as converter

    converter(
        pdf=str(pdf_path_obj),
        out=str(out_root),
        backend=backend,
        device=device,
        mineru_mode=mineru_mode,
        enable_formula=enable_formula,
        strategy_path=None,
        skip_mineru=skip_mineru,
        llm_model=llm_model,
        mineru_md=mineru_md,
    )

    if not final_md_path.exists():
        raise FileNotFoundError(f"Expected output not found: {final_md_path}")

    quality = "HIGH"
    if qa_path.exists():
        try:
            with open(qa_path, "r", encoding="utf-8") as f:
                qa = json.load(f)

            toc_found = qa.get("toc_entries_found", 0)
            toc_final_unmatched = qa.get("toc_final_unmatched_remaining", 0)
            toc_final_matched = qa.get("toc_final_matched", 0)

            if toc_found > 0:
                match_ratio = toc_final_matched / max(toc_found, 1)
                if match_ratio < 0.5:
                    quality = "LOW"
                elif match_ratio < 0.8 or toc_final_unmatched > 3:
                    quality = "MEDIUM"
            else:
                llm_used = qa.get("llm_used_no_toc_fix", False)
                quality = "MEDIUM" if llm_used else "LOW"
        except Exception:
            pass

    return str(final_md_path), "mineru_v2.1_toc_strict", quality
