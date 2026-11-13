# Example collection

The owner requested the existing 50 good project examples. The collection comes from `mas_qwen36_revised6_complete50_20261027/delivery/50题修订版汇总.json`, dataset `50题本地原文Query修订版_20261027`, updated 2026-10-27. It contains 44 retained historical drafts and six revised-query drafts, all recorded as `qwen/qwen3.6-flash` outputs.

The importer verifies every draft against the delivery manifest's SHA-256 hash. It copies the generated Markdown without changes, the original request, the primary paper's title/DOI, model, date and match status. Short bilingual browsing labels and categories are editorial additions. No PDF, extracted source full text, session file, private path, or runtime log is copied.

This is a historical collection, not a claim that all 50 passed every check. The original record identifies V2-08 as a conditional source match, V2-01 as not passing the recorded integrity check, and V2-33 as having unavailable PDF identity validation. These notes remain in each case's review panel. O10 and V2-06 have no DOI in the supplied manifest; the interface does not invent a source link.

The home page features O07 (scratch assay), O02 (iterative immunofluorescence), and O01 (kidney organoids). `web/examples/index.json` contains browsing metadata; each separate JSON holds its draft and provenance. Reusing an example fills the composer and clears prior attachments/context; a new generation starts only after submission.

To rebuild from the owner's local manifest:

```bash
python scripts/import_examples.py /path/to/50题修订版汇总.json
```

Browsing and exporting these drafts makes no model calls. Live generation uses the user's current model selection.
