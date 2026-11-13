# Deployment

## Static product preview

Run `python scripts/build_site.py` and host `dist/` with any static host. Hash-based routing needs no server rewrites. The preview offers examples, source inspection, Markdown export, printing, and language switching. It does not upload user inputs or run generation. Google Fonts may be fetched for typography; system fonts are the fallback.

The deployed preview is separate from the private Python runtime. A static website or Cloudflare Worker cannot execute the extracted Python pipeline directly.

## Real generation service

1. Copy `.env.example` to `.env`. Supply `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, and a long random `STUDIO_ACCESS_TOKEN`. Configure retrieval credentials appropriate to your workflow. Do not reuse test credentials.
2. Run `docker compose up --build`, or use the README's virtual environment commands. Docker publishes only to loopback by default. No GPU is required for text-native PDFs.
3. Open the local website and enter the service access code under Service settings. Request text and reference text will be sent to the configured model/retrieval services.
4. For remote use, place the service behind HTTPS, set `STUDIO_SECURE_COOKIES=1`, and preserve the original Host header. Keep one Uvicorn worker and mount persistent storage for `/app/var`.
5. Validate a real end-to-end task with your provider, then a missing-paper resume and a failed-provider case before inviting others. The repository's tests use synthetic requests and stubbed model behavior; they do not establish live generation quality or cost.

Use the Dockerfile on a Python/container host with persistent disk. No paid host, subscription, domain, or provider credential is provisioned by this repository. The intended next product step is a small invitation-only trial, followed by account-based quotas and durable worker infrastructure if needed.

## Operational limits

- One running task; at most `STUDIO_MAX_PENDING` queued/running tasks, default 5.
- Per-task timeout `STUDIO_JOB_TIMEOUT`, default 1800 seconds.
- Five references, at most 10 MB each; PDFs must be unencrypted and at most 30 pages. Text-native PDFs are supported in the default install.
- Files and jobs remain until deleted or removed by the operator. Browser access is cookie-scoped for 30 days; losing the cookie loses self-service access to those jobs.
- Session files and private logs can contain research text. Keep the data volume private; no raw-log endpoint is provided.
- The invitation code is for a small trusted deployment. It is not a substitute for individual accounts and budget controls at public scale.

The model configured in the extracted snapshot is preserved as a default, not a promise of continuing provider availability. Set `OPENROUTER_MODEL` to a currently available model supporting the pipeline's JSON outputs; verify its behavior and cost with a limited trial.
