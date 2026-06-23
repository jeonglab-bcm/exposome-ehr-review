"""Manuscript summarization pipeline (Gemma 4 12B + Pydantic checklist)."""
from .schema import LLM_FIELDS_SCHEMA, ManuscriptChecklist, SummaryBatch
from .llm_client import get_client, summarize_text

__all__ = [
    "LLM_FIELDS_SCHEMA",
    "ManuscriptChecklist",
    "SummaryBatch",
    "get_client",
    "summarize_text",
]
