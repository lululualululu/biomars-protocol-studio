# Initial release validation

Verified locally on 2026-11-13:

- A clean Python 3.11 environment installs the extracted project without CUDA or other BioMARS workflows.
- All four generation agents import; the real interface constructs and resolves the packaged planning prompt using synthetic credentials, without a model request.
- 24 Python tests pass: authorization, browser ownership, reference extraction, malformed/oversized/streaming uploads, queue limits, resume continuity, cancellation/deletion, recovery, protected files, integrity failure handling, and public result shaping.
- Four JavaScript tests pass: raw HTML escaping, unsafe URL rejection, document structures/source links, and hiding internal step markers from reading view.
- JavaScript syntax checks and static-preview build pass.
- Desktop and 390 px mobile browser checks cover homepage, optional context, English/Chinese switching, example workspace, review notes, and Markdown view. The mobile document width equals the viewport width.
- Publication scan checks tracked files for obvious credential patterns, private artifacts, and machine-specific home paths. It is a bounded automated check, not an exhaustive security audit.

Not verified in this release: a paid live model/retrieval run, scientific quality, provider latency/cost, scanned-PDF MinerU behavior, Docker execution, multi-instance production operation, or an independent accessibility/security audit. CI repeats the code tests after push; its actual result should be checked on GitHub.

The public site is a static interactive product preview. It deliberately cannot submit real generation jobs. The Python service is included for self-hosting and the next integration stage.
