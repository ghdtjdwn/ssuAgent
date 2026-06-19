# ADR 0008 — EPIC 7: LlamaIndex SimpleVectorStore RAG

**날짜**: 2026-06-19  
**상태**: 적용됨  
**범위**: `ssuAgent/ssu_agent/rag/`

---

## 배경 (Background)

`ssuMCP`는 Spring Boot 내부에서 Java로 학칙/졸업/장학 RAG를 제공한다 (`AcademicEmbeddingClient`, `AcademicPolicyCorpusCache`). 하지만 포트폴리오에서 "Python LlamaIndex RAG 파이프라인 구축 + 평가"를 독립적으로 보여줄 수 없었다.

EPIC 7 목표:
- `ssuAgent` 내에서 LlamaIndex 기반 RAG 모듈을 독립적으로 구현
- 인제스트 파이프라인(문서 로딩 → 청킹 → 임베딩 → 저장) 시연
- 평가 프레임워크(`RelevancyEvaluator`)로 검색 품질 측정 가능성 확인

---

## 고려한 선택지

### 1. PGVectorStore (pgvector + Postgres)

**기각 이유**:  
- 2026-06-18 prod 세션에서 `pg_available_extensions` 조회 결과 pgvector 확장이 없음 확인  
- k3s 클러스터에서 Postgres 설정 변경 시 DB 재시작 필요, 운영 위험  
- 포트폴리오 데모에 추가 인프라 복잡도가 정당화되지 않음

### 2. Qdrant (별도 벡터 DB)

**기각 이유**:  
- k3s 클러스터에 StatefulSet 추가 필요 — 메모리/CPU 제약 있는 단일 노드에 부담  
- 이미 Redis가 캐시/pub-sub/분산락을 담당 중; 또 다른 DB는 과잉

### 3. ssuMCP Java 임베딩만 유지 (변경 없음)

**기각 이유**:  
- Python LlamaIndex 기술을 포트폴리오에서 증명할 수 없음  
- LlamaIndex 평가 프레임워크(`RelevancyEvaluator`, `FaithfulnessEvaluator`)가 Java에 없음

### 4. RAGAS 평가 프레임워크

**기각 이유**:  
- `ragas>=0.2`가 `langchain_community.chat_models.vertexai`를 임포트하는데, `langchain_community 0.4.x`에서 해당 모듈이 제거됨  
- 의존성 충돌로 CI에서 import 실패  
- LlamaIndex 내장 `RelevancyEvaluator` / `FaithfulnessEvaluator`로 동일한 평가 지표 확보 가능

### 5. ✅ 채택: SimpleVectorStore (인메모리)

**선택 이유**:  
- prod Postgres에 pgvector 없어도 동작  
- 추가 인프라 불필요  
- LlamaIndex가 공식 지원하는 기본 벡터 스토어  
- CI에서 MockEmbedding으로 API 키 없이 전체 파이프라인 테스트 가능

---

## 구현 결정 (Implementation Decisions)

### 임베딩 모델
| 선택지 | 결정 |
|---|---|
| OpenAI text-embedding-3-small (1536-dim) | ✅ 런타임 기본값 (비용 효율적, 업계 표준) |
| MockEmbedding(embed_dim=1536) | ✅ CI/테스트 기본값 (OPENAI_API_KEY 없을 때) |
| Local sentence-transformers | 검토했지만 추가 패키지 (~400MB) 부담 |

### `embed_model=None` fallback 전략
`from_documents(embed_model=None)` 호출 시 LlamaIndex의 `Settings.embed_model` 기본값 해석이 OPENAI_API_KEY를 요구한다. CI에서 예외 발생을 막기 위해:
- `embed_model=None` → `MockEmbedding(embed_dim=1536)`으로 폴백
- Settings 전역 상태를 건드리지 않음 (`VectorStoreIndex.from_documents(embed_model=...)` 직접 전달)

이 설계는 테스트 격리를 보장하고 전역 상태 오염을 방지한다.

### LLM 없는 retrieval-only 모드
`llm=None`이면 합성 단계를 건너뛰고 최상위 청크 텍스트를 answer로 반환. 이 모드는:
- CI 테스트에서 API 키 불필요
- 레이턴시 민감 경로에서 LLM 비용 절약 가능

### 평가 테스트 구조
1. `test_engine_builds_without_error`: 파이프라인 전체가 예외 없이 동작하는지 검증  
2. `test_retrieval_returns_k_nodes`: similarity_top_k 제한 확인  
3. `test_fixture_covers_question_keyword`: 픽스처 데이터가 평가 질문 주제를 커버하는지 확인 (의미 검색이 아닌 존재 검증)  
4. `test_llamaindex_relevancy_evaluation`: OPENAI_API_KEY 있을 때만 실행되는 전체 RAG + 평가

---

## 작동 방식

```
ACADEMIC_FIXTURES (list[dict])
    ↓ Document 변환
VectorStoreIndex.from_documents(embed_model=MockEmbedding|OpenAI)
    ↓ SimpleVectorStore에 벡터 적재
AcademicRagEngine.query(question)
    ↓ retriever.retrieve(question) → top-K NodeWithScore
    ↓ llm=None: source_texts[0] 반환
    ↓ llm=OpenAI: response_synthesizer.synthesize()
RagResult(answer, source_texts, source_metadata, scores)
```

---

## 포트폴리오 포인트

1. **LlamaIndex 인제스트 파이프라인**: `Document` → `VectorStoreIndex` → `SimpleVectorStore` 전체 흐름 구현
2. **평가 프레임워크 연동**: `RelevancyEvaluator` 사용 방법과 CI 스킵 전략 설명 가능
3. **환경별 fallback 설계**: MockEmbedding(CI) vs OpenAI(prod) 자동 전환 로직
4. **의존성 충돌 해결 경험**: ragas의 `langchain_community` 충돌을 진단하고 LlamaIndex 내장 평가로 대체

**예상 면접 질문**:
- Q. pgvector를 쓰지 않은 이유는? → "prod에 확장이 없어서 SimpleVectorStore로, 포트폴리오에서는 파이프라인 시연이 목적"
- Q. RAGAS vs LlamaIndex 평가의 차이는? → "RAGAS는 외부 의존성 충돌, LlamaIndex 내장 평가로 동일 지표 확보"
- Q. MockEmbedding을 쓰면 검색 품질을 어떻게 검증하나? → "랜덤 벡터라 의미 랭킹은 검증 못하지만, 인제스트→저장→조회 파이프라인 정확성을 검증. 실 임베딩 품질 평가는 OpenAI key guard 테스트로 분리"
