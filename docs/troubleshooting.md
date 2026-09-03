# 운영 장애 기록

실제로 확인한 장애만 기록한다. 운영 로그가 없는 원인은 추정으로 명시하고 검증하지 않은 결과는
성공으로 적지 않는다.

## 2026-07-18 — 개인 도구 실패 뒤 일반 답변 합성 및 ERROR 상태 고착

### 기대와 영향

연결된 u-SAINT나 LMS 도구가 실패하면 에이전트는 현재 개인 데이터를 가져오지 못했다고 명확히
알려야 한다. 실제로 졸업요건 도구가 실패한 뒤에는 일반적인 졸업 기준을 대신 생성했고, LMS는
credential grant가 남아 있어도 직전 `ERROR` health 때문에 실제 복구 호출을 시도하지 않았다.
사용자는 로그인 문제와 학교 시스템의 일시 장애를 구분할 수 없었다.

### 증거와 원인

- 학사 요청은 `get_auth_status`를 통과해 `check_graduation_requirements` 실행까지 진행한 뒤 실패했다.
- 공유 ReAct loop는 예외 원문을 노출하지 않기 위해 일반 tool error 문자열로 바꿨지만, 그 문자열을
  다음 모델 turn에 다시 전달했다. 모델은 개인 데이터 근거가 없는 일반 안내를 합성할 수 있었다.
- ssuMCP의 private response가 예외 대신 top-level `UPSTREAM_UNAVAILABLE` envelope를
  반환하면 기존 loop는 이를 성공 ToolMessage로 다루었다. 또한 일부 legacy LMS
  도구는 API 실패를 `status=OK`인 response의 string `data`에 넣어 반환했다.
- auth guard는 linked provider의 `ERROR`를 `UNAVAILABLE`로 처리했다. `ERROR`는 직전 upstream
  실패이고 grant 취소나 `EXPIRED`와 같지 않으므로, 일시 장애가 끝나도 다음 요청이 실제 도구를
  호출해 health를 `VALID`로 되돌릴 경로가 없었다.
- 당시 사용자 요청과 일치하는 trace가 없어 최초 upstream 실패가 credential, 네트워크, 학교 포털
  응답 변경 중 무엇이었는지는 확정하지 않는다.

### 해결과 대안

도구 invocation 예외를 `ToolMessage.status=error`로 표시하고 공유 loop가 즉시 도메인별 고정 서비스
장애 안내를 반환하도록 했다. masked 오류도 모델, checkpoint, SSE로 넘기지 않아 일반 졸업요건이나
임의 복구 절차를 만들 수 없다. top-level non-OK은 `retryable=true` 또는 `UPSTREAM_` status/code일
때만 operational failure로 분류하고, legacy LMS 호환 경로는 정확한 도구
이름과 오류 접두어로만 분류한다. 정상 data의 같은 단어와 non-retryable domain outcome은
통과시킨다.

linked `ERROR`는 명시적인 다음 사용자 요청에서 private tool 실행 예산을 1회로
제한한다. 같은 turn의 private batch는 실행 전에 거부하고, 예산을 사용한 뒤 모델이
추가 private call을 만들어도 재호출하지 않는다. 기존 transport wrapper의 한 번 retry는
하나의 논리적 호출 안에서 유지한다. 성공하면 ssuMCP가 health를 `VALID`로 갱신하고,
다음 사용자 요청이 새 provider preflight를 수행한다.

`ERROR`마다 재로그인을 강제하는 방식은 일시 장애와 credential 만료를 혼동해 제외했다. 반대로
모델에게 tool error를 설명하게 하는 방식은 근거 없는 fallback을 다시 허용하므로 제외했다. 누락,
malformed, non-OK auth status와 알 수 없는 health는 계속 fail-closed하고, unlinked와 `EXPIRED`만
재연결 경로로 보낸다.

### 검증과 남은 위험

- 전체 pytest 305개 통과
- Ruff check와 format check 통과
- 학사와 LMS 도구 예외가 모델의 두 번째 turn 없이 고정 안내로 끝나는 회귀 테스트 통과
- 학사 top-level operational envelope와 LMS legacy `status=OK`/string `data` 오류가 모델 합성
  전에 차단되고, 정상 data의 상태명과 non-retryable outcome은 통과하는 회귀 테스트 통과
- linked `ERROR`가 degraded 경로로 들어가고, private batch와 모델 재호출이 실행 전에
  차단되는 계약 테스트 통과

실제 학교 시스템의 일시 장애는 이 서비스가 제거할 수 없다. 배포 뒤에는 동일 요청이 성공할 때
health가 `VALID`로 회복되고, 계속 실패할 때 고정 안내와 프론트 degraded 표시가 함께 유지되는지
실계정으로 확인해야 한다. companion ssuMCP 변경은 LMS 목록·대시보드·내보내기 오류를 top-level
non-OK 계약으로 이전한다. 정확한 legacy 접두어 guard는 backend-first rolling deployment 동안만
구버전 응답을 보완하며, 두 서비스가 모두 배포된 뒤 제거할 수 있다.

## 2026-09-03 — MCP deep health 503과 진단 로그 경계

### 기대와 영향

최종 공개 점검에서 `/health`는 HTTP 200이었지만 `/healthz/deep`은 세 번 모두 HTTP 503과
`status=DEGRADED`, `mcp=DOWN`을 반환했다. deep health는 2초 안에 ssuMCP client의 `get_tools()`를
수행하므로, 프로세스는 살아 있어도 MCP 도구가 필요한 요청이 영향을 받을 수 있는 상태다. 실제 사용자
실패율은 확인 가능한 trace가 없어 추정하지 않았다.

### 증거와 원인 경계

- ssuMCP의 공개 식단 REST 경로는 같은 시각 HTTP 200이었다. 이는 backend와 해당 REST 경로의 도달성만
  증명하며 MCP initialize와 `tools/list` 성공을 증명하지 않는다.
- liveness는 `/health`, readiness는 `/ready`에 연결되어 있어 deep health 503이 Pod 재시작이나 traffic
  제외를 직접 만들지는 않는다.
- 당시 public deep-health 응답과 기존 `deep health MCP check failed: %s` 로그만으로는 DNS, TLS, ingress,
  HTTP status, MCP protocol, tools/list와 전체 timeout을 구분할 수 없다. 기존 로그는 exception message
  원문을 남겨 URL이나 session 값이 포함될 위험도 있었다.
- 최근 공개 문서 정리는 deep-health 코드와 설정을 바꾸지 않아 직접 회귀라는 근거가 없다. Argo CD와
  cluster log에 접근하지 못해 실제 실행 image와 최초 실패 계층은 확정하지 않았다.

### 변경과 대안

deep health의 외부 503 계약과 2초 제한은 유지한다. 내부 경고만 exception tree를 순회해 최대 네 개의
exception class와 `HTTPStatusError`의 숫자 status를 낮은 카디널리티 필드로 기록하도록 바꿨다. exception
message, request URL, response body, credential, token과 session identifier는 기록하지 않는다.

외부 응답에 원인을 추가하는 방식은 진단 정보를 공격자에게 노출하므로 제외했다. 모든 exception message를
정규식으로 지우는 방식도 새 credential 형식을 놓칠 수 있어 제외했다. deep health를 liveness에 연결하거나
근거 없이 timeout을 늘리는 방식은 downstream 장애를 재시작으로 증폭하거나 증상을 숨길 수 있어 채택하지
않았다.

### 검증과 다음 진단

- nested `ExceptionGroup` 안의 HTTP 429와 timeout type을 message 없이 고정된 필드로 만드는 단위 테스트
- 실제 deep-health handler가 외부 503 body를 유지하고 secret-bearing URL과 exception message를 로그에
  남기지 않는 endpoint 테스트
- Ruff check, format check와 전체 pytest를 통과한 뒤에만 image를 발행

다음 재현에서는 같은 시각의 새 type/status 로그와 Pod 내부
`initialize → notifications/initialized → tools/list` 단일 session probe를 대조한다. affinity cookie,
`Mcp-Session-Id`와 protocol header를 요청 사이에 유지하고, 성공한 session은 DELETE한다. Argo source
revision, 실제 Pod image, Ready 수와 restart 횟수도 desired state와 비교한다. 이 증거 전에는 DNS, TLS,
ingress, rate-limit 또는 protocol 중 하나를 root cause로 확정하거나 production 설정을 바꾸지 않는다.

후속 검토 질문은 deep health를 liveness와 분리한 이유, message-free log로 충분한 분기 정보를 얻는지,
rate window와 concurrency lease 거부를 metric으로 구분할 필요가 있는지다. cluster evidence가 확보될 때까지
실제 MCP 가용성 위험은 남는다.

### 전달 결과

[PR #86](https://github.com/ghdtjdwn/ssuAgent/pull/86)의 정확한 head
`8539b739364b6806bf55ca3afca4a6e723cd71b4`를 `main`에 fast-forward했다. PR의 Quality, Helm,
ARM64 image 검증, CodeQL과 Gitleaks가 모두 통과했고, [main CI](https://github.com/ghdtjdwn/ssuAgent/actions/runs/33706182247)는
동일 SHA의 ARM64 image를 GHCR에 발행했다. Image Updater commit
`ab11ebdc2de3ef6665e33dfd770f8ac6dc660cf6`은 desired tag를 `sha-8539b739364b6806bf55ca3afca4a6e723cd71b4`로
갱신했으며 해당 commit의 Security와 CodeQL도 통과했다.

갱신 뒤 공개 `/health`는 HTTP 200이었지만 `/healthz/deep`은 여전히 HTTP 503이었다. 접근 가능한 환경에서
Argo CD와 실제 Pod image를 확인하지 못했으므로 새 image의 rollout 완료나 새 진단 필드가 production log에
도달했다고 주장하지 않는다. production 설정과 수동 restart는 수행하지 않았다.
