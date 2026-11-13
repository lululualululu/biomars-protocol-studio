# Deployment

## Static product preview

Run `python scripts/build_site.py` and host `dist/` with any static host. Hash-based routing needs no server rewrites. The preview offers examples, source inspection, Markdown export, printing, and language switching. It does not upload user inputs or run generation. Google Fonts may be fetched for typography; system fonts are the fallback.

The deployed preview is separate from the private Python runtime. A static website or Cloudflare Worker cannot execute the extracted Python pipeline directly.

## Personal local service

Set provider and retrieval credentials in the ignored `.env`, then run `.venv/bin/python -m studio.local`. Open http://127.0.0.1:8745. The launcher enables `STUDIO_LOCAL_MODE=1`, binds to loopback, and ignores proxy headers. The API rejects non-loopback peers, unrecognized Host headers, and cross-origin mutations. No access code is needed in this mode. Keep the terminal running while using the site.

## Remote or container generation service

1. Keep `STUDIO_LOCAL_MODE=0`. Copy `.env.example` to `.env`. Supply `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, and a long random `STUDIO_ACCESS_TOKEN`. To intentionally allow visitors to generate without an invitation code, set `STUDIO_PUBLIC_MODE=1` instead. Provider credentials remain required, and browser ownership, origin checks and queue limits still apply. Configure retrieval credentials appropriate to your workflow. Do not reuse test credentials.
2. Run `docker compose up --build`, or use the README's virtual environment commands. Docker publishes only to loopback by default. No GPU is required for text-native PDFs.
3. Open the local website and enter the service access code under Service settings. Request text and reference text will be sent to the configured model/retrieval services.
4. For remote use, place the service behind HTTPS, set `STUDIO_SECURE_COOKIES=1`, and preserve the original Host header. Keep one Uvicorn worker and mount persistent storage for `/app/var`.
5. Validate a real end-to-end task with your provider, then a missing-paper resume and a failed-provider case before inviting others. The repository's tests use synthetic requests and stubbed model behavior; they do not establish live generation quality or cost.

Use the Dockerfile on a Python/container host with persistent disk. No paid host, subscription, domain, or provider credential is provisioned by this repository. The intended next product step is a small invitation-only trial, followed by account-based quotas and durable worker infrastructure if needed.

## User-level Linux deployment

The server deployment uses a dedicated Python 3.11 environment under `~/apps/biomars-protocol-studio/.venv`, with dependencies installed from `.[dev]`. It does not require a GPU or modify existing Conda environments. The exact installed versions are recorded in `var-installed-requirements.txt` on that server.

Two user systemd units run the API and Cloudflare Quick Tunnel. The API listens only on `127.0.0.1:8745`; the tunnel provides HTTPS. Public generation is explicitly enabled, secure cookies are enabled, and user lingering keeps the services running after SSH logout and across reboot. Both units restart after a process failure.

```bash
systemctl --user status biomars-studio biomars-tunnel
systemctl --user restart biomars-studio
journalctl --user -u biomars-studio -n 50 --no-pager
journalctl --user -u biomars-tunnel --no-pager | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
```

Restarting `biomars-studio` preserves the tunnel URL. Restarting `biomars-tunnel` obtains a new temporary URL; use the last command to find it. A reboot also obtains a new URL. Use a named Cloudflare Tunnel and a domain when a permanent address is needed. See [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

### Remote verification, 2026-11-13

- 30 Python tests passed in the dedicated server environment; six frontend tests passed locally.
- Public HTTPS submission using Qwen3.6 Flash completed the scratch-assay request for DOI `10.3791/57691`, including the missing-paper PDF upload/resume path.
- The result contains 35 canonical steps; the structural integrity check passed with no reported issues. The Markdown export is 8,017 bytes and exactly matches the API's document. This is not a validation of experimental correctness.
- Another visitor receives 404 for the private task. Secure/HttpOnly/SameSite cookies, same-origin submission, cross-origin rejection and private-path denial were checked through the public endpoint.
- Deployed HTML, stylesheet, view modules and example catalog match the local source hashes. Automated browser preview was unavailable during this deployment, so these remote checks used HTTPS requests; the same UI had already been visually checked locally.

## Operational limits

- One running task; at most `STUDIO_MAX_PENDING` queued/running tasks, default 5.
- Per-task timeout `STUDIO_JOB_TIMEOUT`, default 1800 seconds.
- Five references, at most 10 MB each; PDFs must be unencrypted and at most 30 pages. Text-native PDFs are supported in the default install.
- Files and jobs remain until deleted or removed by the operator. Browser access is cookie-scoped for 30 days; losing the cookie loses self-service access to those jobs.
- Session files and private logs can contain research text. Keep the data volume private; no raw-log endpoint is provided.
- The invitation code is for a small trusted deployment. It is not a substitute for individual accounts and budget controls at public scale.

Qwen3.6 Flash is the default. `studio/models.py` contains the curated model allowlist exposed by `/api/config`. `OPENROUTER_MODEL` can select a different allowlisted default; request payloads persist the actual selection. Provider availability and behavior can change; test a selected model with a limited trial. The live local verification is documented in LOCAL_GENERATION.md.
