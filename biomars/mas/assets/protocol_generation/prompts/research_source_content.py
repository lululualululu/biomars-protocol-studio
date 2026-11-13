"""Fixed stage prompts; inputs are assembled by the caller."""
SEG_SYSTEM_25 = (
"You are an expert at identifying the experimental METHODS/PROTOCOL section "
    "inside full scientific articles.\n"
    "You will receive the entire article as numbered lines.\n"
    "You MUST reply with line indices only, never include article text."
)

SEG_USER_TEMPLATE_33 = (
"""You are given the full article as numbered lines.

Each line is prefixed with "L<line_number>:" starting at 1.
Your task: pick ONE contiguous block of lines that corresponds to the main
experimental METHODS/PROTOCOL/PROCEDURE section, including materials/reagents
and stepwise instructions, but EXCLUDING:
- abstract, introduction, video captions, website boilerplate,
- results, discussion, conclusions, acknowledgments, references.

Rules:
- Answer with EXACTLY one line of the form:
  START=<start_line> END=<end_line>
- <start_line> and <end_line> are positive integers (1-based, inclusive).
- Do NOT include any article text or extra explanation.
- If the entire article is essentially a protocol, you may choose START=1 and END=<last line>.

ARTICLE:
{numbered_doc}
"""
)
