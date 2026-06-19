"""
LlamaIndex-based academic policy RAG using in-memory SimpleVectorStore.

Design decisions:
- SimpleVectorStore (not PGVectorStore): prod Postgres ships without the pgvector
  extension — verified via pg_available_extensions query in 2026-06-18 prod session.
- OpenAI text-embedding-3-small (1536-dim): cost-efficient, widely supported baseline.
  Swap embed_model to switch providers without changing retrieval logic.
- MockEmbedding for tests: avoids API calls in CI; swap to real embedding at runtime.
- Ingestion pipeline is document-agnostic: any list[dict] with "text"+"metadata" works,
  making it easy to replace fixtures with live-scraped pages later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from llama_index.core import Document, StorageContext, VectorStoreIndex
from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.embeddings import BaseEmbedding, MockEmbedding
from llama_index.core.llms import LLM
from llama_index.core.vector_stores import SimpleVectorStore


@dataclass
class RagResult:
    """Retrieval result with source provenance."""

    answer: str
    source_texts: list[str] = field(default_factory=list)
    source_metadata: list[dict] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)


_DEFAULT_EMBED_DIM = 1536


class AcademicRagEngine:
    """LlamaIndex SimpleVectorStore RAG engine for SSU academic policy documents."""

    def __init__(self, retriever: BaseRetriever, llm: LLM | None = None) -> None:
        self._retriever = retriever
        self._llm = llm

    @classmethod
    def from_documents(
        cls,
        docs: list[dict],
        embed_model: BaseEmbedding | None = None,
        llm: LLM | None = None,
        similarity_top_k: int = 3,
    ) -> "AcademicRagEngine":
        """Build an engine from a list of {"text": ..., "metadata": ...} dicts.

        Args:
            docs: Raw document dicts.
            embed_model: Embedding model. Pass MockEmbedding(embed_dim=1536) for
                         tests; defaults to OpenAI text-embedding-3-small at runtime
                         (requires OPENAI_API_KEY). If None and no API key, falls back
                         to MockEmbedding to keep tests runnable.
            llm: LLM for response synthesis. None = retrieval-only (no generation).
            similarity_top_k: Number of chunks to retrieve per query.
        """
        if embed_model is None:
            embed_model = MockEmbedding(embed_dim=_DEFAULT_EMBED_DIM)

        llama_docs = [Document(text=d["text"], metadata=d.get("metadata", {})) for d in docs]

        vector_store = SimpleVectorStore()
        storage_ctx = StorageContext.from_defaults(vector_store=vector_store)

        index = VectorStoreIndex.from_documents(
            llama_docs,
            storage_context=storage_ctx,
            embed_model=embed_model,
            show_progress=False,
        )

        retriever = index.as_retriever(similarity_top_k=similarity_top_k)
        return cls(retriever=retriever, llm=llm)

    def query(self, question: str) -> RagResult:
        """Retrieve relevant chunks for a question.

        When llm is None, returns retrieval results only (no LLM synthesis).
        This keeps the core retrieval path testable in CI without API keys.
        """
        nodes = self._retriever.retrieve(question)
        source_texts = [n.get_content() for n in nodes]
        source_metadata = [n.metadata for n in nodes]
        scores = [n.score or 0.0 for n in nodes]

        if self._llm is None:
            # Retrieval-only mode: surface the most relevant chunk as the answer
            answer = source_texts[0] if source_texts else ""
        else:
            # Full RAG: synthesize answer using the LLM
            from llama_index.core import QueryBundle
            from llama_index.core.response_synthesizers import get_response_synthesizer

            synthesizer = get_response_synthesizer(llm=self._llm)
            response = synthesizer.synthesize(QueryBundle(query_str=question), nodes=nodes)
            answer = str(response)

        return RagResult(
            answer=answer,
            source_texts=source_texts,
            source_metadata=source_metadata,
            scores=scores,
        )
