# Design rationale

## Silver glass revision

The current presentation layer is `web/glass.css`, loaded after `web/studio.css`. The home specimen is an unframed, masked background behind a centered composer. A silver-white navigation surface and frosted input use translucent fills, blur, edge highlights and soft shadows. Blue-violet accents relate to the cell artwork; dark ink remains opaque. Existing generated artwork is reused without a new image dependency.

The composer has a pointer-following light on fine-pointer devices. Controls have brief hover/press feedback; no large background object moves. System and in-app reduced-motion settings disable the light and animated transitions. Reduced-transparency and increased-contrast preferences receive solid surfaces. Protocol body text stays on opaque paper, preserving reading size, font and warm-paper preferences.

The example collection uses generated transparent specimen icons in its category bands. The home starting-point buttons use matching icons; the hero background is unchanged. See [asset paths and exact GPT Image prompts](IMAGE_ASSETS_V5.md). Generation retains six distinct milestones driven by actual backend state, with orbit and scan feedback. No generation or source content changes are involved. Desktop Chinese/English layouts and 390 px home, examples, reader and generation viewports were visually checked in Chrome. The generation preview uses a local fixture and submits no paid task. The four changed frontend files were hot-synced to the existing Python host, with a rollback copy, and matched byte-for-byte through the public URL. Both systemd services remained active; the public API reported generation available.

The current local iteration follows the selected **01 Soft Precision** direction, refined for biomedical research. See [the page structure, behavior and asset record](BIOMEDICAL_LOCAL.md). The published gallery remains unchanged until the design is finalized.

BioMars Protocol Studio is a reading and drafting workspace for researchers. Its first screen answers one question: where do I describe the experiment?

The main action sits inside the request composer. References and experimental conditions open on demand. The 50-example collection opens finished historical drafts; a reuse button takes an example's request back to the composer. An unavailable generation service is disclosed beside the input and explained on submission; it never simulates progress.

## Visual system

| Role | Choice |
| --- | --- |
| Background | Cool silver, `#f1f3f8`, with blue/lavender ambient color |
| Surfaces | Frosted white controls; opaque white document; optional ivory paper `#fbf8f0` |
| Main ink | Ink blue, `#202b43` |
| Primary action | Blue, `#536fb5` → `#3d5597` |
| Rules | Cool gray, `#dce3eb` |
| Editorial headings | System sans-serif / PingFang fallback |
| Interface/body | System sans-serif; optional Iowan / Palatino / Songti reading face |
| Shape | 22–26 px glass surfaces; 12–16 px controls |
| Motion | Orbit rings during generation; brief control feedback; reduced-motion respected |

References: [Elicit](https://elicit.com/) informed the question-first research journey; [Linear](https://linear.app/) informed visual restraint and the distinction between navigation and work. This is an original composition; no brand assets or page layouts were copied.

## Working state

The document takes most of the width on its own paper surface, with section navigation on the left and sources/review notes on the right. Numbered steps, chapter headings and day/stage subheadings establish the hierarchy. Citations open the matching source. The compact toolbar contains focus reading, typography, copy and export. Real stage labels accompany the generation animation; unchanged polling responses preserve the animation's DOM. Missing papers name the needed document and expected filename; resume belongs to the same task. Export preserves the original source context and unresolved details.

On mobile, examples become single-column cards and sticky Document / Contents / Sources / Checks controls switch between workspace panels. Language, font, reading size, paper color, reduced motion and experiment defaults are browser-local. Draft text/context and access credentials use tab-session storage; reference files remain in memory. Keyboard focus is visible; dialogs use native focus handling, inputs have labels, and feedback uses alert/status regions. This release has been checked at 390 px and the browser's normal desktop width; it has not had an independent accessibility audit.

## Deliberate limits

No visual claim of experimental validation. No fake usage counts, testimonials, performance statistics, or citation totals. No mandatory model picker, agent controls, account wall, or pricing interruption on the preview path. The product prioritizes a readable draft and checkable evidence before collaboration or billing features.
