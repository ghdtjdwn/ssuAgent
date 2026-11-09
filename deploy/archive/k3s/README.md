# k3s 경로 manifest (보관용)

이 디렉터리의 Helm chart(`charts/ssu-agent/`)와 ArgoCD Application(`argocd/application-ssu-agent.yaml`)은
**2026-06-14부터 2026-10-15까지** ssuAgent production의 desired state였다. 2026-10-15에 production을
단일 ARM64 VM의 Docker Compose로 옮기면서 더 이상 사용하지 않으며, 참고용으로만 남긴다.

- 갱신하지 않는다. `values.yaml`의 `image.tag`는 ArgoCD Image Updater의 마지막 write-back
  (2026-10-08, `sha-1b9b4e29…`)에 멈춰 있고, 이 tag는 cluster CPU 부족으로 Pending에 머물러 실행된 적이
  없다.
- CI는 이 chart를 lint하거나 render하지 않는다.
- 현재 배포 경로와 그 이유는 [docs/deploy.md](../../../docs/deploy.md)와
  [ADR 0024](../../../docs/adr/0024-single-vm-compose-timer-deploy.md)를 본다. 현재 실행 정의(Compose,
  env, 자동 반영 스크립트)는 비공개 인프라 저장소에서 관리한다.
