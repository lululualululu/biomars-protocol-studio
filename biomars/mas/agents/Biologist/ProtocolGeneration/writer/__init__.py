"""Grounded Writer business logic.

inputs: resolve evidence files and Plan scope.
response: parse rewrites and audit source facts.
rendering: assemble Markdown, stable step markers, and citations.

ProtocolGroundedWriterAgent retains the workflow and compatibility entrypoints.
Leaf functions receive the specific callbacks they need; they never import an Agent.
"""
