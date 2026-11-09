# ssuAgent 문서 지도

이 저장소의 현재 코드, 테스트와 CI가 최종 기준이다. 문서와 충돌하면 해당 구현을 먼저 확인하고
문서를 함께 수정한다. production 실행 정의(Compose, env, 자동 반영)는 비공개 인프라 저장소에서 관리한다.

## 시작하기

- [프로젝트 개요와 로컬 실행](../README.md)
- [설정과 환경 변수](configuration.md)
- [아키텍처와 신뢰 경계](architecture.md)

## 설계 결정

- [ADR 색인](adr/README.md) — 20개 ADR의 번호·날짜·상태·한 줄 요약
- [Supervisor architecture](adr/0001-supervisor-architecture.md)
- [PostgreSQL checkpointer](adr/0003-postgres-checkpointer.md)
- [Agent edge hardening](adr/0009-agent-edge-hardening.md)
- [Thread ownership binding](adr/0010-agent-thread-ownership-binding.md)
- [Stable principal binding](adr/0011-thread-stable-principal-binding.md)
- [Optional Anthropic provider](adr/0015-optional-anthropic-provider.md)
- [Deterministic LMS export](adr/0022-deterministic-lms-export-download.md)
- [Conversation lifecycle and signed edge identity](adr/0023-conversation-lifecycle-and-edge-identity.md)
- [Single-VM Docker Compose and timer-based image pull](adr/0024-single-vm-compose-timer-deploy.md)

## 운영

- [배포와 운영 검증](deploy.md)
- [운영 장애 기록](troubleshooting.md)
- [GitHub Actions CI](../.github/workflows/ci.yml)
- [k3s 경로 manifest 보관](../deploy/archive/k3s/) — 2026-06-14 ~ 2026-10-15의 Helm chart와 ArgoCD Application, 참고용

## 검증 책임

- Python 코드: Ruff check/format과 pytest가 기준이다.
- Stream/HITL 계약: `tests/test_stream_interrupt.py`, `tests/test_supervisor.py`가 기준이다.
- 인증과 thread 소유권: `tests/test_main_security.py`, `tests/test_auth_guard.py`가 기준이다.
- 운영 배포: GitHub Actions image job, 호스트 배포 이력, running container image digest와
  `/ready`·`/healthz/deep`을 함께 확인한다.
