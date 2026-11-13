"""Fixed stage prompts; inputs are assembled by the caller."""
SEGMENT_ANALYSIS_PROMPT_574 = (
"""
You are a scientific document analyzer. Analyze the following section from a biological protocol document.

## Task Query: {task_query}
## Section Title: {title}
## Section Level: {level} (1=Major section, 2=Subsection)
## Content:
```
{content}
```

## Instructions:
Analyze this section and output a JSON object with the following fields:

1. **content_summary**: A brief 1-2 sentence description of what this section contains.

2. **content_types**: An array of descriptive tags identifying what types of content this section provides.
   Be flexible and descriptive - use tags that accurately describe the content, such as:
   - "background" / "introduction" / "rationale"
   - "reagents" / "materials" / "chemicals" / "antibodies"
   - "equipment" / "instruments" / "apparatus"
   - "reagent_preparation" / "stock_solutions" / "buffer_recipes"
   - "procedure" / "methods" / "protocol_steps" / "workflow"
   - "timing" / "schedule" / "duration" / "timeline"
   - "troubleshooting" / "common_problems" / "tips"
   - "expected_results" / "quality_control" / "validation"
   - "figures" / "images" / "diagrams"
   - "references" / "citations" / "bibliography"
   - Or any other descriptive tag that fits the content

3. **primary_view**: Choose exactly one primary role for this section:
   - "procedure": executable experimental operations or ordered workflow actions
   - "materials": inventories of reagents, consumables, or equipment
   - "reagent_setup": recipes for preparing media, buffers, stocks, solutions, or coatings
   - "timing": schedules or duration summaries without the full operations
   - "troubleshooting": problems, causes, corrective actions, or cautions
   - "supporting_context": background, rationale, results, validation, references, or other non-executable context
   Judge from the section body, not from the title alone. A heading, cross-reference,
   passing mention, narrative overview, or summary of a method is not enough to
   label a section as "procedure". The body must contain concrete instructions
   that a reader could execute.

4. **query_relevance**: Choose exactly one relationship to the task query:
   - "core": directly advances or executes the biological transformation, isolation,
     treatment, measurement, or other operation explicitly requested by the query
   - "supporting": an immediate prerequisite required to execute that core operation;
     it must occur before or during the requested workflow, not merely after it
   - "off_query": a downstream application or characterization workflow, optional
     variant, background, or unrelated operation that the query did not request
   Base this decision on the operational goal of the query. Post-workflow sample
   processing is not "supporting" merely because it can be performed on the output.

5. **suggested_uses**: An array of 2-5 specific ways this section can help generate a protocol. Examples:
   - "extract reagent concentrations and catalog numbers"
   - "identify source-defined input quantities"
   - "obtain incubation times and temperatures"
   - "find troubleshooting conditions and corrective actions"
   - "determine stage boundaries and transition criteria"

6. **key_entities**: An array of important named entities (chemicals, genes, equipment, cell lines, etc.) mentioned in this section. Limit to 10 most important.

7. **has_tables**: Boolean - true if section contains tables (marked by | or table-like structures)

8. **has_lists**: Boolean - true if section contains bullet points or numbered lists

9. **has_parameters**: Boolean - true if section contains quantitative parameters (numbers with units)

## Output Format:
Output ONLY valid JSON, no markdown fences or extra text:
{{
  "content_summary": "...",
  "content_types": ["...", "..."],
  "primary_view": "procedure|materials|reagent_setup|timing|troubleshooting|supporting_context",
  "query_relevance": "core|supporting|off_query",
  "suggested_uses": ["...", "..."],
  "key_entities": ["...", "..."],
  "has_tables": false,
  "has_lists": false,
  "has_parameters": false
}}
"""
)
