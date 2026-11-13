# BioMars Protocol Studio

Turn an experimental question into a structured protocol draft, with sources kept close.

[Try the interactive preview](https://biomars-protocol-studio.zylu0301.chatgpt.site) · [25 design perspectives](https://biomars-protocol-studio.zylu0301.chatgpt.site/directions/) · [产品方案](docs/PRODUCT_PLAN.zh-CN.md) · [Design](docs/DESIGN.md)

BioMars Protocol Studio extracts the Protocol Generation workflow from BioMARS into an independent, MIT-licensed project. It includes the Python generation engine, a focused browser workspace, and a self-hosted API.

**Status:** early release. Static builds support example browsing. Real generation runs in the Python service with provider credentials. Personal local mode needs no access code; remote deployments require one unless public generation is explicitly enabled with `STUDIO_PUBLIC_MODE=1`. Generated drafts require review; integrity checks are not experimental validation.

## Start locally

```bash
git clone https://github.com/lululualululu/biomars-protocol-studio.git
cd biomars-protocol-studio
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
python -m studio.local
```

Open http://127.0.0.1:8745. Set `OPENROUTER_API_KEY` and the base URL in `.env`, add your retrieval credentials, then restart. `studio.local` binds only to loopback and disables proxy-header trust; local mode checks both the peer and Host before allowing access. Keep one worker.

The default is **Qwen3.6 Flash** (`qwen/qwen3.6-flash`). The composer also offers Qwen3.6 Plus, Gemini 3 Flash Preview, and DeepSeek V3.2. Model IDs are allowlisted in `studio/models.py`; each task stores its selection so uploading missing papers resumes with the same model. Choose a browser default in Settings → Experiment defaults.

Browse 50 historical project drafts under **Examples**, search by method or category, inspect sources and original queries, and reuse a request in the composer. These are generated drafts with retained review notes. See [example provenance](docs/EXAMPLES.md).

For a locked environment with [uv](https://docs.astral.sh/uv/), use `uv sync --frozen --extra dev`. The checked-in `uv.lock` records the resolved dependency versions.

The API runs one generation subprocess at a time with a bounded queue and timeout. Keep one Uvicorn worker. Tasks and uploaded files persist under `var/studio`. A browser cookie isolates task access; the access code limits generation to invited users. This is an initial self-hosted service, not a managed multi-tenant platform.

## CLI

```bash
python launch_protocol.py --query "Prepare a literature-grounded protocol draft for routine adherent-cell culture" \
  --config config/protocol/protocol_config.yaml
```

When a run needs full text, retain the session ID and configuration:

```bash
python launch_protocol.py --session_id EXISTING_SESSION_ID \
  --manual_pdf_dir /absolute/path/papers \
  --config config/protocol/protocol_config.yaml
```

The default PDF path extracts text with PyMuPDF. Scanned documents may require a separately installed MinerU backend or a text-native replacement; GPU/CUDA packages are not part of the default installation.

## Design directions

Explore 25 separately composed visual directions at [`/directions/`](https://biomars-protocol-studio.zylu0301.chatgpt.site/directions/): overview, full-screen previews, local favorites and side-by-side comparison. Each concept has its own typography, layout, color and interaction. Experimental requests transfer into the existing Studio composer. The showcase itself does not generate protocols. See [artwork and prompts](docs/design-assets.md).

## Browser experience

- Start with one experimental request; add context or reference files only when useful.
- Follow real workflow stages and resume a task when full text is missing.
- Review draft text, source links, and unresolved items in one workspace.
- Export Markdown or print/save PDF using the browser.
- Reopen tasks from the same browser; delete tasks and their local server files.

The static site never sends requests or files to a model. The self-hosted service sends supplied text to configured model/retrieval providers during generation. Do not upload material you cannot share with those services.

## Development and deployment

```bash
pytest
python scripts/check_publication.py
python scripts/build_site.py
docker compose up --build
```

See [deployment](docs/DEPLOYMENT.md), [architecture](docs/ARCHITECTURE.md), [中文产品与构建方案](docs/PRODUCT_PLAN.zh-CN.md), and [source provenance](docs/PROVENANCE.md). The static build is `dist/`; Python hosting uses the included Dockerfile. A static deployment does not run the generation engine.

## What is included

Protocol research, full-text ingestion, structure-first planning, grounded writing, integrity validation, and necessary shared runtime types. Drug, optimization, robot actions, raw datasets, credentials, paper PDFs, private run logs, and original Git history are excluded. The 50 explicitly selected historical draft examples are included with provenance. Shared orchestration compatibility code is retained to minimize behavioral changes to the extracted pipeline.

## Contributing

Small changes with a reproducible case are welcome. Keep inputs synthetic or licensed for redistribution, run the tests, and never commit credentials or research uploads. See [CONTRIBUTING](CONTRIBUTING.md). [MIT license](LICENSE).
