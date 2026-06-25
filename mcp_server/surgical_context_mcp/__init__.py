"""MCP server exposing surgical_context's axis retrieval to LLM chats.

A thin, in-process wrapper over ``context_engine.axis.pipeline.run_axis_retrieval``
— the same read path the ``/ask/axis`` HTTP route runs and the
``QA/axis_benchmark`` harness replays. No uvicorn: an LLM chat (Claude Code,
Cursor, Codex, Claude Desktop) calls the ``ask_code`` tool over MCP stdio and
gets ranked, graph-expanded code bundles to reason over.
"""

__version__ = "0.1.0"
