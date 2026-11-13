# Design rationale

BioMars Protocol Studio is a reading and drafting workspace for researchers. Its first screen answers one question: where do I describe the experiment?

The main action sits inside the request composer. References and structured context are secondary, so the first interaction does not require understanding a form schema. Example requests fill the composer; a separately labeled example opens a finished workspace. An unavailable generation service is disclosed beside the input and explained on submission; it never simulates progress.

## Visual system

| Role | Choice |
| --- | --- |
| Background | Warm off-white, `#f7f7f2` |
| Document/input surface | White, `#ffffff` |
| Main ink | Dark green, `#24372f` |
| Primary action | Forest green, `#285447` |
| Rules | Muted neutral, `#dedfd6` |
| Editorial headings | Instrument Serif, Georgia, Songti fallback |
| Interface/body | DM Sans, system sans-serif, PingFang fallback |
| Shape | Small control radii; a softer composer edge |
| Motion | Brief color/position feedback; reduced-motion respected |

References: [Elicit](https://elicit.com/) informed the question-first research journey; [Linear](https://linear.app/) informed visual restraint and the distinction between navigation and work. This is an original composition; no brand assets or page layouts were copied.

## Working state

The document takes most of the width. Sources and review notes live beside it, not in a distant settings screen. Real stage labels replace invented completion percentages. Missing papers name the needed document and expected filename; resume belongs to the same task. Export preserves source context and unresolved details.

On mobile, the composer actions stack, examples become compact rows, and the source panel moves below the document. The language preference is browser-local. Keyboard focus is visible; dialogs use native focus handling, inputs have labels, and feedback uses alert/status regions. This release has been checked at 390 px and the browser's normal desktop width; it has not had an independent accessibility audit.

## Deliberate limits

No visual claim of experimental validation. No fake usage counts, testimonials, performance statistics, or citation totals. No mandatory model picker, agent controls, account wall, or pricing interruption on the preview path. The product prioritizes a readable draft and checkable evidence before collaboration or billing features.
