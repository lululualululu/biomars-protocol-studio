# Protocol Generation

See the [usage guide](../../../../../README.md#protocol) for configuration, source collection, manual PDF handoff and output checks. `launch_protocol.py` runs the Structure-First workflow.

## Architecture

| Entry or module | Responsibility |
| --- | --- |
| `protocol_domain_pack_agent.py` → `domain_pack.py` | User materials and DomainPack assembly |
| `protocol_research_agent.py` → `research/` | Query planning, literature selection, full text, evidence and citation expansion |
| `protocol_planning_agent.py` → `planning/` | Source input, Plan parsing, validation, repair and scope |
| `protocol_grounded_writer_agent.py` → `writer/`, `renderers/` | Writer inputs, response validation, text and citations |
| `protocol_integrity_validator.py` | Final Plan and text integrity checks |
| `protocol_schemas.py` | Shared Agent data structures |
| `protocol_scope.py` | Shared scope and step-display rules |
| `sources/` | Methods extraction, segmentation, PDF ingestion and identity checks |

Start with each Agent's `execute` or `run_*` method, then follow its business modules. `planning/initial_plan.py` handles initial generation and parsing; `plan_parser.py` converts model responses into data structures. `plan_parsing.py` exposes the same `initial_plan` module to callers.

Structure-First reads full source text, segments it into Blocks and Steps, then uses the Writer to produce text with references. Agent and CLI calls use the same route. The accepted `--structure_first` and `--methods_first` flags do not switch the route.

## Tools and documents

External clients and file conversion live in `biomars.mas.tools.ProtocolGeneration`: Serper, OpenAlex, NCBI, Semantic Scholar, Jina, protocols.io, `domain_doc_loader` and `pdf_conversion`.

Agent modules contain planning and source-handling decisions. Tool modules provide external services and document reading/conversion without importing Agent implementations.

```text
Research / Orchestration
  → sources.protocol_pdf_ingest
  → tools.ProtocolGeneration.pdf_conversion
  → tools.ProtocolGeneration.mineru_pipeline
```

`scripts/protocol_generation/protocol_pdf_to_md_v2.py` exposes the PDF conversion CLI and the `mineru_pipeline` module. `sources/methods_extract_rules.py` provides shared Methods extraction. The planning prompt is read from `biomars/mas/assets/protocol_generation/`.

`scripts/protocol_generation/run_pipeline.sh` invokes `launch_protocol.py` from the repository root and forwards arguments. Relative inputs are resolved from the root. Set `PYTHON` to select an interpreter; the default is `python`.

## Python interface

```python
from biomars.mas.agents.Biologist.ProtocolGeneration import ProtocolPlanningAgent

agent = ProtocolPlanningAgent(model, config)
result = await agent.execute(context, params)
```

Business modules and external clients can also be imported directly:

```python
from biomars.mas.agents.Biologist.ProtocolGeneration.planning import plan_parser
from biomars.mas.agents.Biologist.ProtocolGeneration.sources import protocol_pdf_ingest
from biomars.mas.tools.ProtocolGeneration.biomars_jina_client import BioMarsJinaClient
from biomars.mas.tools.ProtocolGeneration import pdf_conversion
```

Agent classes are loaded on demand. Business modules receive capabilities through arguments and callbacks rather than importing Agent implementations. The Writer and integrity validator share `protocol_scope` rules.
