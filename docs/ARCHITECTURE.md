# Architecture

```text
Browser workspace (web/)
       | same-origin API + opaque HttpOnly visitor cookie
FastAPI (studio/api.py)
       | ownership checks, invitation code, bounded queue
SQLite job index + private job directories
       | one isolated child process per generation/resume
Studio worker (studio/worker.py)
       | original interface and workflow state
Domain context → Research → Full text → Planning → Writing → Integrity
```

The frontend has no build dependencies. `web/site-config.json` enables the local API. `scripts/build_site.py` creates a static `dist/` with API calls disabled; the example never masquerades as an actual generation.

Each task owns its working directory, references, session trajectory, public status snapshot, and private log. The subprocess loads the same extracted agent implementation as the CLI. Resume preserves the original session ID and submits requested papers through the existing feedback path. Stage changes are written atomically; the browser polls task status. The API does not return arbitrary filesystem paths, full session context, or provider logs.

The initial queue is intentionally single-instance: one Uvicorn worker and one generation subprocess. A restart marks active tasks interrupted and preserves queued jobs. Awaiting-paper sessions can resume after restart. Other interrupted stages currently require a new request or operator-led CLI recovery. Task deletion removes the task directory and database row after execution has stopped.

This release does not include account identities, multi-instance coordination, public anonymous quotas, automatic storage expiry, billing, editable protocol revisions, robot execution, or a guarantee of scientific correctness. Before a public generation service, use durable external job coordination, persistent object storage, authenticated identities, per-user limits, retention/deletion policy, and operational monitoring.
