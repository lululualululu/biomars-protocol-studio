"""Fixed stage prompts; inputs are assembled by the caller."""
LLM_RECOVERY_SYSTEM_17 = (
"You recover structure from flattened scientific protocol text.\n"
    "The input is a full article or PDF-extracted text as numbered lines.\n"
    "Identify the best contiguous line ranges for the protocol-relevant core blocks.\n"
    "Prefer operational content over narrative prose.\n"
    "Return JSON only."
)

SYSTEM_PROMPT_864 = (
"You are a biomedical source-identity verifier. "
            "Do not approve a recovered chapter based on title keywords alone."
)
