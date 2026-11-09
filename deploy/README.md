# deploy/

ssuAgent의 production 실행 정의(Docker Compose, env, 자동 반영 스크립트, systemd timer)는 비공개 인프라
저장소에서 관리하며 이 저장소에는 없다. 이 저장소가 책임지는 배포 artifact는 [`Dockerfile`](../Dockerfile)과
[CI workflow](../.github/workflows/ci.yml)가 publish하는 `ghcr.io/ghdtjdwn/ssuagent:sha-<commit>` image다.

- 현재 배포 경로, 검증 순서, 반영되지 않았을 때의 확인 절차: [docs/deploy.md](../docs/deploy.md)
- 2026-10-15 k3s/ArgoCD → 단일 VM Docker Compose 전환 결정: [ADR 0024](../docs/adr/0024-single-vm-compose-timer-deploy.md)
- [`archive/k3s/`](archive/k3s/): 2026-06-14 ~ 2026-10-15에 쓴 Helm chart와 ArgoCD Application. 참고용이며
  갱신하지 않고 CI도 검증하지 않는다.
