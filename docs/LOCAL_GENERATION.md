# Local generation verification — 2026-11-13

The personal Python service is served at `http://127.0.0.1:8745`. This work has not been pushed to GitHub or deployed to the public static site.

## Run locally

From the repository, with dependencies installed and the ignored `.env` configured:

```bash
.venv/bin/python -m studio.local
```

Leave the process running while using the site. The launcher binds only to `127.0.0.1`, ignores proxy headers and enables loopback-only access without a separate service code. A remote/container deployment must keep `STUDIO_LOCAL_MODE=0` and configure `STUDIO_ACCESS_TOKEN`.

The owner's provided OpenRouter credential is stored only in the ignored server environment file with mode `0600`. Existing retrieval settings were copied from the original local project. No credential is included in source files, browser configuration or static build output.

## Verified real run

- Browser-created job: `3945dc009fa14610be95b19d04a6cf83`.
- Engine session: `session_1794561020`.
- Query: “Optimized Scratch Assay for In Vitro Testing of Cell Migration with an Automated Optical Camera”.
- Model: `qwen/qwen3.6-flash`, using the real OpenRouter endpoint.
- Source: DOI `10.3791/57691`.
- Started 17:10:20; completed 17:22:13 local time, including the pause for uploading the source PDF.
- Final engine state: `completed`; public state: `review_required`.
- Deterministic integrity gate: passed, 28 steps, no reported issues. Marker, source-span, source-index and numeric fidelity coverage were all 1.0. This is a structural check, not experimental validation.
- Markdown: 6,559 bytes; SHA-256 `01575ecc4ad14942f09d759990f679ed26ee1c2a6d5922df810edc49abbcc66e`.

The request went through domain interpretation and live academic retrieval. Semantic Scholar rate limits and Jina connection timeouts slowed retrieval; the workflow found the intended paper but could not obtain reliable full text online. It paused as `awaiting_papers`. The existing project PDF was uploaded through the browser, and the same job/session continued through PDF identity checking, text extraction, planning, grounded writing and integrity validation. No historical protocol was substituted for this result. The full-text availability limitation still applies to future requests.

The native browser file picker was used because the automation extension's direct file-upload capability lacked file-URL permission. No extension permissions were changed. The website's upload/resume API completed successfully.

The result is available in the creating browser under **My protocols** and at `/#job/3945dc009fa14610be95b19d04a6cf83`. The browser cookie controls job visibility. Private job files live under `var/studio/<job-id>/`; the exported browser download `biomars-protocol.md` matched the generated Markdown hash exactly. The inherited renderer also produced a PDF through its ReportLab fallback; the web UI offers browser Print / PDF.

After completion, the display summary was regenerated from the saved trace to expose the structural check and remove a resolved missing-paper notice. The generated Markdown and engine trace were not changed.

## Models and examples

The composer and experiment settings expose Qwen3.6 Flash (default), Qwen3.6 Plus, Gemini 3 Flash Preview and DeepSeek V3.2. Exact IDs were checked against the OpenRouter model catalog. This end-to-end run validates **Flash only**; the other three selections were not separately run end to end. The server validates the selection and persists it for resumed jobs.

The 50 historical examples are separate static assets with provenance and original hashes. They are searchable by English/Chinese labels, request, ID or DOI, filterable by category, and reusable as new requests. Source and review notes are retained. See EXAMPLES.md.

## Verification

- 29 Python tests pass: authentication, loopback/Host/origin restrictions, model allowlisting and persistence, ownership, uploads, queue and resume behavior, result sanitization, and historical draft integrity.
- Four JavaScript Markdown safety/rendering tests pass; modified modules pass syntax checks.
- Publication scan includes tracked and untracked non-ignored files; no obvious secrets or private runtime artifacts found.
- Static build passes and explicitly disables generation; it does not bundle `.env` or `var`.
- Browser checks cover real submission, missing-PDF upload/resume, completed draft, integrity panel, Markdown export, model preference changes/restoration, example search/filter/reuse, and a 390px mobile viewport without horizontal overflow.
- Temporary browser viewport overrides were reset. The default model was restored to Qwen3.6 Flash after testing.

Screenshots are local-only under `var/previews/`.
