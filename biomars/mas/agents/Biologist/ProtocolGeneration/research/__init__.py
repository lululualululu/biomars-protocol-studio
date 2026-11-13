"""Protocol Research business logic.

retrieval: bounded query planning and searches through external clients.
anchors: candidate ranking, contract validation, and selection metadata.
fulltext: acquire source text through the existing client cascade.
source_content: Methods extraction, chunks, and targeted source rereads.
artifacts: load prior evidence and persist selected source documents.

ProtocolResearchAgent owns stage order and downstream handoff. Existing Agent
methods forward their overridable callbacks and legacy module-level dependencies.
"""
