# BioMars / 01 Biomedical

Historical record of the initial local biomedical iteration, based on 01 Soft Precision. The current interface and subsequent deployment are described in [DESIGN.md](DESIGN.md), [IMAGE_ASSETS_V5.md](IMAGE_ASSETS_V5.md) and [DEPLOYMENT.md](DEPLOYMENT.md).

## Pages and behavior

| Page | Primary purpose | Behavior |
| --- | --- | --- |
| New experiment | Describe a research task | One composer; references and biological context are optional. Three starting points open historical examples; their reuse action fills the request. |
| My protocols | Find past work | Real service tasks, title search, state and date. Empty/error states provide recovery. The example is separate from task history. |
| Protocol workspace | Read and verify a draft | Section navigation, numbered steps on a separate paper surface, references and review notes. Focus mode, citation navigation, Markdown, copy and print/PDF. Mobile panels stay available while scrolling. |
| Generation | Follow the current task | Orbit animation and the current backend stage; no fabricated percentages. Missing full text, cancellation and errors have distinct states. |
| Settings / General | Adapt reading | Chinese/English, sans/serif, 14/16/18 px document body, white/ivory paper, reduced motion. The reader toolbar exposes the same browser-local preferences. |
| Settings / Experiment | Reuse research context | Cell line/system, species and scope. Saved defaults never overwrite manually edited draft context; “Use defaults” explicitly reapplies them. |
| Settings / Service | Understand readiness | Live connection check, tab-session access code, clear/show controls and setup link. Model API keys remain server-side. |

Unsubmitted request text and biological context stay in the tab session across navigation and reload. File objects stay in memory and need reattaching after reload. Successful job submission clears the local draft. No generation is simulated when service credentials are absent.

## Local verification

Local preview: `http://127.0.0.1:8745/` (FastAPI/Uvicorn, loopback only). No GitHub push or hosted deployment was performed for this iteration.

- Reader checks cover all 50 historical drafts: canonical step IDs, order and counts survive rendering; the original Markdown remains unchanged. The existing HTML/URL escaping checks also pass.
- Browser checks include the completed 28-step scratch-assay task, desktop and 390 px mobile layouts, font/size/paper controls, section navigation, focus reading, citations, review notes and Markdown view.
- Generation visuals are checked with a separate local UI fixture for running, queued and missing-paper states. That fixture does not submit tasks or contact a model.
- The local Python service is configured and has already completed a real OpenRouter run. See [local generation](LOCAL_GENERATION.md). This visual iteration reuses that result without another paid generation.

Current preview screenshots are local runtime artifacts under `var/previews/studio-*-v2.png`.

## Visual system

Cool gray workspace (`#edf1f5`), white document paper, ink navy (`#182b42`) and deep navy actions (`#1c3451`). System UI typography and an optional serif reading face. The paper's header, chapter rules and numbered steps establish the reading hierarchy; the side rails stay quieter. Repeated explanations are replaced by controls or collapsed details. Orbit motion follows real stages and honors reduced-motion preferences. The original 25-direction gallery is retained unchanged for reference. Active stylesheet: `web/studio.css`; document/progress views: `web/protocol-view.js`.

## Artwork

Built-in GPT Image mode, generated 2026-11-13. Decorative cellular concept, not a micrograph or experimental evidence. Optimized JPEG delivery copy; the original PNG is retained in the image tool output directory.

Saved project asset: [biomedical-cells-v1.jpg](../web/assets/biomedical-cells-v1.jpg).

Final prompt:

```text
Use case: stylized-concept. Asset type: hero artwork for BioMars Protocol Studio, a premium biomedical research web app with a pure-white, quiet, precise interface. Primary request: a refined biomedical visualization of a small group of three translucent mammalian cells, the main cell large and two smaller companions, with softly irregular lipid membranes, clearly visible violet-blue nuclei, fine granular cytoplasm and restrained internal organelle details. A credible microscopic-inspired 3D scientific editorial illustration, not a literal experiment or anatomical diagram. Composition: square, all cells visible, group arranged on a gentle diagonal, large white margins, centered slightly to the right. White seamless background #ffffff, extremely soft subtle contact shadows, cool pale periwinkle and icy blue, pearlescent membranes, one nucleus in muted indigo. Delicate material depth, not transparent glass marbles, not planets, not a flower, not generic chrome blobs. The membrane texture and nuclei should immediately read as biology. Sophisticated scientific journal cover quality, calm and luminous, no clinical gore. No text, no labels, no logo, no HUD, no measurement scale, no watermark.
```
