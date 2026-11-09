# ADR 0024 — k3s/ArgoCD에서 단일 VM Docker Compose와 timer 기반 image pull로 전환

## 상태

Accepted — 2026-10-15 (문서화 2026-11-09)

이 ADR은 `deploy/archive/k3s/`에 남긴 Helm chart와 ArgoCD Application이 더 이상 production의
desired state가 아님을 기록한다. [ADR 0003](0003-postgres-checkpointer.md), [ADR 0009](0009-agent-edge-hardening.md),
[ADR 0014](0014-mcp-connection-resilience.md)의 k3s·Helm·HPA 전제는 결정 당시 맥락으로 남고, 현재 runtime
전제는 이 문서를 따른다.

## 배경

2026-06-14부터 ssuAgent production은 ARM64 k3s cluster의 `ssuai-prod` namespace였다. CI가 `main`의
ARM64 image를 GHCR에 `sha-<commit>` tag로 publish하면 ArgoCD Image Updater가 tag를 감지해
`deploy/charts/ssu-agent/values.yaml`의 `image.tag`를 Git에 write-back하고 ArgoCD가 Helm chart를
auto-sync했다. 운영 기록([deploy.md](../deploy.md) 이전 경로 절)에는 live Application과 Git manifest의
drift로 새 image가 반영되지 않은 사례와, 마지막 write-back tag(2026-10-08, `sha-1b9b4e29…`)가 cluster
CPU 부족으로 Pending에 머물러 실행되지 못한 사실이 남아 있다.

2026-10-15에 개인 프로젝트 전체의 인프라를 바꿨다. 인프라 저장소의 작업 기록에 적힌 목표는 다음과
같다.

> 목표: 기존 2 OCPU/12GB/200GB k3s 서버의 개인 앱 전부를 무료 한도 안의 1 OCPU/6GB/50GB 서버에서
> 동시에 실제 동작시키기

같은 기록의 결정 사항:

> 모니터링(Prometheus/Grafana/Loki/Tempo/promtail), k3s, ArgoCD, cert-manager는 새 서버로 가져가지
> 않는다.

즉 전환 이유는 클라우드 무료 한도(ARM 코어·메모리·블록 스토리지) 안에서 개인 앱을 모두 돌리기 위해
VM 하나(1 vCPU, 약 6GB RAM)로 줄인 것이며, 그 예산 안에서 k3s control plane과 ArgoCD, 모니터링 스택을
유지하지 않기로 했다. 전환 검증은 2026-10-15에 끝났고 이전 cluster는 같은 날 종료됐다.

## 결정

1. **실행 환경**: 단일 ARM64 VM에서 Docker Compose project 하나로 ssuMCP 백엔드, PostgreSQL, Redis,
   Kafka, ssuAgent, ssu-ai-service를 실행한다. 앞단의 Caddy 하나가 TLS(Let's Encrypt 자동 발급)를
   종료하고 service로 프록시하며 SSE는 버퍼링하지 않는다. ssuAgent container는 `mem_limit` 512MiB,
   uid 1001, `restart: unless-stopped`, Docker healthcheck로 실행된다.
2. **반영 경로**: push가 아니라 pull이다. 호스트의 systemd timer가 15분마다 자동 반영 스크립트를
   실행한다. 스크립트는 공개 GitHub API로 `main` 최근 commit을 읽고, 가장 최근 commit부터 GHCR에
   `sha-<commit>` tag가 있는지 확인한 뒤, 그 digest가 현재 고정된 digest와 다를 때만
   `docker pull` → env file의 image 변수 한 줄 교체 → `docker compose up -d --no-deps --wait <service>` →
   공개 주소 `/ready` 확인(최대 3분)을 수행한다. 확인 실패 시 이전 image로 되돌려 다시 확인하고
   `rolled_back`으로 기록하며, 같은 digest는 새 commit이 나올 때까지 다시 시도하지 않는다.
3. **자격증명**: 호스트에 GitHub token이나 registry credential을 두지 않는다. 공개 저장소와 공개 GHCR
   package만 읽는다.
4. **source of truth**: Compose 정의, env file, 자동 반영 스크립트, timer unit은 비공개 인프라
   저장소에서 관리한다. 이 저장소는 image(Dockerfile, CI)와 애플리케이션 계약(`/health`, `/ready`)만
   책임진다.
5. **k3s manifest**: `deploy/charts/ssu-agent/`와 `deploy/argocd/`는 `deploy/archive/k3s/`로 옮기고
   CI의 Helm lint/template job을 제거한다. archive는 2026-06-14 ~ 2026-10-15의 desired state 기록이며
   갱신하지 않는다.

## 결과와 trade-off

얻은 것:

- 운영 면이 하나(Compose project + Caddy)로 줄어 `docker compose ps`, container log, 배포 이력 로그로
  상태를 전부 볼 수 있다. ArgoCD `Synced/Healthy`가 실제 running image를 보장하지 못하던 문제는 반영
  스크립트가 `/ready`를 성공 조건으로 두고 실패 시 스스로 되돌리는 방식으로 대체됐다.
- 6GB 메모리 예산 안에서 모든 service를 동시에 실행한다. 2026-10-15 측정에서 container 합계는 약
  3.9GiB였고, ssuAgent는 512MiB 상한 안에서 실제 약 190MiB를 쓴다(2026-11-09 `docker stats`).
- 공개 image를 서버가 당겨 가므로 CI에 배포용 SSH key나 cluster credential이 없다.

잃은 것, 받아들인 제약:

- **HPA·PDB·rolling update 없음.** replica는 1개이며 교체는 `compose up -d --wait`의 단일 container
  재생성이다. 교체 중 짧은 unavailability가 있고, 스케일 아웃하려면 shared rate limiter와 함께 별도
  설계가 필요하다([architecture.md](../architecture.md)).
- **GitOps write-back 없음.** 실행 중인 image digest는 Git이 아니라 호스트 env file에 있다. 어떤
  commit이 운영 중인지는 배포 이력 로그, running container의 image digest, 그리고 `/health`의
  `build_sha`로 확인한다.
- **반영 지연 최대 약 15~16분.** timer 주기와 image pull·기동 시간의 합이다. 즉시 반영이 필요하면
  호스트에서 스크립트를 수동 실행한다.
- **메모리 예산이 공유된다.** VM 하나의 약 6GB RAM을 ssuMCP(JVM), Kafka, PostgreSQL과 나눠 쓴다.
  OOM 안전망으로 2GiB swap(swappiness 10)과 container별 `oom_score_adj`를 두되, ssuAgent의
  `mem_limit`을 올리려면 다른 service의 예산을 함께 조정해야 한다.
- **모니터링 스택 없음.** Prometheus/Grafana/Loki/Tempo는 이관하지 않았다. 관측은 Docker healthcheck,
  container log, 배포 이력 로그, 공개 health endpoint에 한정된다.
- **unhealthy 자동 재시작 없음.** Compose는 healthcheck 실패만으로 container를 재시작하지 않는다.
  `restart: unless-stopped`는 process 종료에만 반응한다.

## 거부한 대안

k3s, ArgoCD, Image Updater, cert-manager와 모니터링 스택을 새 VM으로 함께 옮기는 선택은 위에 인용한 전환
기록에서 명시적으로 제외됐다. 이 ADR은 그 결정을 기록할 뿐 당시 검토한 다른 대안을 소급해 적지 않는다.

## 검증

- 2026-10-15 전환 검증 92 PASS / 0 FAIL(인프라 저장소 기록). 재부팅 뒤 같은 검증 92 PASS.
- 2026-10-15 ~ 2026-11-09 배포 이력 로그: ssuAgent `success` 7회, 현재 로그에 `rolled_back`·`failed` 항목 없음.
- 2026-11-09: `v0.2.0`(`10deb12`) image가 timer로 반영됐고 공개 `/health`·`/ready`·`/healthz/deep`이
  200이었다([deploy.md](../deploy.md) 반영 증거 표).
