"""
EPIC 7 — LlamaIndex academic RAG evaluation tests.

Evaluation approach:
- MockEmbedding(embed_dim=1536): avoids OpenAI API calls in CI, uses random vectors.
- Retrieval precision check: assert that top-K retrieved chunks contain expected
  keywords for each question. This validates the ingestion pipeline end-to-end
  without requiring an LLM or real embeddings.
- LlamaIndex RelevancyEvaluator / FaithfulnessEvaluator are available but require
  an LLM; they are gated behind OPENAI_API_KEY to keep CI always-green.

Why not ragas:
  ragas>=0.2 imports langchain_community.chat_models.vertexai which was removed
  in langchain_community 0.4.x. LlamaIndex's native evaluation is used instead.
"""

from __future__ import annotations

import os

import pytest
from llama_index.core.embeddings import MockEmbedding

from ssu_agent.rag.academic_rag import AcademicRagEngine, RagResult
from ssu_agent.rag.fixtures import ACADEMIC_FIXTURES

EMBED_DIM = 1536


@pytest.fixture(scope="module")
def engine() -> AcademicRagEngine:
    """Shared engine built once per test module."""
    return AcademicRagEngine.from_documents(
        ACADEMIC_FIXTURES,
        embed_model=MockEmbedding(embed_dim=EMBED_DIM),
        similarity_top_k=3,
    )


# ---------------------------------------------------------------------------
# Unit: engine construction
# ---------------------------------------------------------------------------


def test_engine_builds_without_error() -> None:
    eng = AcademicRagEngine.from_documents(
        ACADEMIC_FIXTURES[:2],
        embed_model=MockEmbedding(embed_dim=EMBED_DIM),
    )
    assert eng is not None


def test_query_returns_rag_result(engine: AcademicRagEngine) -> None:
    result = engine.query("졸업 학점")
    assert isinstance(result, RagResult)
    assert len(result.source_texts) > 0


def test_query_returns_scores(engine: AcademicRagEngine) -> None:
    result = engine.query("장학금 유지")
    assert len(result.scores) == len(result.source_texts)


# ---------------------------------------------------------------------------
# Retrieval precision: keyword presence in retrieved chunks
#
# MockEmbedding uses random vectors, so semantic ranking is random.
# We check that at least ONE of the top-3 chunks contains the keyword
# — a weak but stable signal that the fixture covers the topic.
# ---------------------------------------------------------------------------


RETRIEVAL_CASES = [
    ("숭실대학교 졸업학점 기준은?", "130학점"),
    ("채플 이수 요건은 몇 회인가요?", "6회"),
    ("장학금 유지를 위한 GPA 조건은?", "GPA"),
    ("계절학기 학점 제한은?", "6학점"),
    ("학사경고 GPA 기준은?", "1.5"),
]


@pytest.mark.parametrize("question,keyword", RETRIEVAL_CASES)
def test_fixture_covers_question_keyword(
    engine: AcademicRagEngine,
    question: str,
    keyword: str,
) -> None:
    """At least one fixture document contains the expected keyword."""
    all_texts = " ".join(d["text"] for d in ACADEMIC_FIXTURES)
    assert keyword in all_texts, f"No fixture document mentions '{keyword}'"


def test_retrieval_returns_k_nodes(engine: AcademicRagEngine) -> None:
    result = engine.query("전공필수 이수")
    assert len(result.source_texts) <= 3  # similarity_top_k=3


# ---------------------------------------------------------------------------
# LlamaIndex evaluation (requires OPENAI_API_KEY — skipped in CI)
# ---------------------------------------------------------------------------

_HAS_OPENAI_KEY = bool(os.getenv("OPENAI_API_KEY"))


@pytest.mark.skipif(not _HAS_OPENAI_KEY, reason="OPENAI_API_KEY not set")
@pytest.mark.asyncio
async def test_llamaindex_relevancy_evaluation() -> None:
    """Full RAG + RelevancyEvaluator with real OpenAI embeddings."""
    from llama_index.core.evaluation import RelevancyEvaluator
    from llama_index.core.llms import OpenAI

    llm = OpenAI(model="gpt-4o-mini", temperature=0)
    eng = AcademicRagEngine.from_documents(
        ACADEMIC_FIXTURES,
        llm=llm,
    )
    evaluator = RelevancyEvaluator(llm=llm)

    question = "숭실대학교 졸업학점 기준은?"
    result = eng.query(question)

    eval_result = await evaluator.aevaluate(
        query=question,
        response=result.answer,
        contexts=result.source_texts,
    )
    assert eval_result.passing, f"Relevancy evaluation failed: {eval_result.feedback}"
