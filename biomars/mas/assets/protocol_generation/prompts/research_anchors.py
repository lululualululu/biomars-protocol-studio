"""Fixed stage prompts; inputs are assembled by the caller."""
UNIVERSAL_RELEVANCE_PROMPT_13 = (
"""
You are a Senior Scientific Content Curator for an automated protocol generation system.
Your task is to evaluate if a Candidate Document is suitable for a specific User Request.

### INPUTS:
1. User Request: "{query}"
2. Semantic Retrieval Contract:
{semantic_contract}
3. Candidate Document Metadata:
   - Title: "{title}"
   - Snippet: "{snippet}"
   - Source Type: "{source}"
   - Bibliographic Context: {bibliographic_context}

### EVALUATION LOGIC (Universal Compatibility Check):

Step 1: Analyze the User Request to identify key constraints:
   - Target Subject (e.g., Species, Cell Line, Protein).
   - Target Methodology (e.g., Cell Isolation, CRISPR, Western Blot).
   - Specific Conditions (e.g., Feeder-free, Serum-free, In vivo).
   - Treat only literal constraints or unavoidable parts of the central operation as required;
     do not add preferred conditions merely because they are common modern practice.

Step 2: Analyze the Candidate Document:
   - What is the primary subject?
   - Is it a protocol/method, or just a product catalog/price list?
   - Does it explicitly contradict any user constraints?
   - Do the requested entities, reagents, conditions, and endpoints participate in the
     requested core workflow, or are matching words used only in a later stimulation,
     readout, comparison, or background statement?
   - Is the document scope an exact match, broader than requested, narrower than requested,
     conflicting, or uncertain from the snippet?
     Use exact only when the requested output/target is explicitly named or is an
     unambiguous synonym in the title/snippet. If a broader output category might contain the
     requested subtype but the subtype is not explicit, use broader; do not infer it from an
     expected phenotype or from general domain knowledge.
   - Distinguish missing abstract/snippet detail from contradictory evidence. When the title
     and available abstract directly match the requested subject and central operation, but
     the snippet omits reagent amounts, timing, or other executable details, keep the document
     relevant with lower confidence and use "methods_capable_application" or an uncertain
     scope as appropriate. The later anchor contract/full-text gate must resolve those missing
     details. Do not label such a paper "primary_protocol" unless executable methods are
     actually visible in the supplied evidence.

Step 3: Determine Relevance Status:
   - **CRITICAL MISMATCH**: The document is about a different species/subject (e.g., Human vs Mouse) and is NOT a comparative study.
   - **TYPE MISMATCH**: The document is a commercial flyer, clinical trial, or review without methods.
   - **CONTEXT MISMATCH**: The document uses a method explicitly excluded by the user (e.g., user wants "feeder-free", doc is "on MEFs").
   - **RELEVANT**: The document discusses the correct subject and method.
   - **INSUFFICIENT DETAIL IS NOT A MISMATCH**: Absence of a requested parameter from this
     short snippet is uncertainty, not proof that the full paper lacks it. Reject only for an
     explicit conflict, wrong subject/workflow, prohibited document type, or clearly
     supporting-only use of the matching terms.

### TRAP WARNINGS:
- Do not be fooled by keywords appearing in secondary contexts (e.g., "Mouse" mentioned only as an antibody host or feeder layer when the target is Human).
- Commercial "Official" sources often list products for multiple species; verify the *specific* kit/manual matches the request.

### OUTPUT FORMAT (JSON ONLY):
{{
    "is_relevant": boolean,
    "match_confidence": float,  // 0.0 to 1.0
    "primary_suitability": "primary_protocol | methods_capable_application | supporting_only | reject",
    "scope_fit": "exact | broader | narrower | conflict | uncertain",
    "core_workflow_relation_match": boolean,
    "analysis": {{
        "user_constraints": ["list of inferred constraints"],
        "doc_attributes": ["list of inferred doc attributes"],
        "conflict_reason": "Explanation if rejected, or 'None'"
    }}
}}
"""
)

SYSTEM_PROMPT_843 = (
"You are a senior biological protocol retrieval and ranking specialist. "
        "Your job is to choose the best anchor documents for protocol generation."
)
