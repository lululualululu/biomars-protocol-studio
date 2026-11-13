# Contributing

Use Python 3.11+ and install `pip install -e '.[dev]'`. Run `pytest -q`, `node --test tests/markdown.test.mjs`, and `python scripts/check_publication.py` before submitting a pull request.

Describe the user-visible problem, the change, and how it was verified. Use synthetic fixtures. Do not add real research uploads, provider keys, runtime logs, copyrighted papers, or absolute paths to your machine.

The extracted engine keeps its original package paths to reduce migration risk. Prefer small, well-tested changes over broad renaming. User-facing stages and errors belong in the Studio layer; provider specifics belong in the engine.

Do not represent a saved file, successful structural check, or model-generated text as experimental validation. Example material must remain clearly labeled.
