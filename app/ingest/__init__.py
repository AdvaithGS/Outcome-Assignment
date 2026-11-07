"""Ingestion branches: each document type becomes a `Document` (Markdown + anchors)."""
from app.ingest.document import Anchor, Document, MarkdownBuilder, PageInfo

__all__ = ["Anchor", "Document", "MarkdownBuilder", "PageInfo"]
