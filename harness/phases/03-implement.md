---
{
  "id": "03-implement",
  "index": 3,
  "owner": "config.roles[]",
  "approval": "none",
  "requires": [
    {"kind": "state", "pointer": "phases.02-cross-verify.status",
     "in": ["passed", "skipped"]},
    {"kind": "file", "path": "${run.dir}/01_plan.md", "min_bytes": 200},
    {"kind": "file", "path": "${run.contract_file}", "min_bytes": 200,
     "must_contain": "${config.contract.sections.units}",
     "unless": "state.contract.mode == \"no_contract\""}
  ],
  "produces": [
    {"key": "contract", "path": "${run.contract_file}", "kind": "markdown",
     "owner": "main", "min_bytes": 200,
     "must_contain": "${config.contract.sections.units}",
     "unless": "state.contract.mode == \"no_contract\""},
    {"key": "claims", "path": "${run.dir}/03_claims.json", "kind": "json",
     "schema": "claims"}
  ],
  "submit_checks": [
    {"id": "rules_read_sha", "from": "config.project.instruction_file",
     "and": "config.project.rules_dir", "claims": "${run.dir}/03_claims.json",
     "on_fail": 8},
    {"id": "clean_ownership", "from": "config.roles",
     "except": "config.main_owned_paths",
     "claims": "${run.dir}/03_claims.json", "on_fail": 8}
  ],
  "gate": {"runner": "adapter", "fail_fast": true, "steps": [{"id": "compile"}]},
  "allow": {"agents": "config.roles[].agent", "parallel": true,
            "unless": "state.profile.name == \"docs\""},
  "on_success": "04-gate"
}
---

## 목적

계약을 고정하고, 역할 전원이 **동시에** 자기 소유 경계 안을 채운다.

> **계약이 "테스트를 먼저"의 대역이다.**
>
> 메인이 단독으로 쓴 계약이 시그니처·오류 어휘·진입점을 먼저 고정한 뒤에 두
> 역할이 동시에 쓰므로, 구현이 테스트에 맞춰 형태를 바꾸거나 테스트가 구현을
> 보고 사후 작성되는 일이 **구조적으로 불가능**하다. 프로젝트 지시 파일의
> 테스트-우선 규칙이 막으려는 것이 정확히 그 두 가지다.
>
> 순차로 돌리면(테스트 먼저 → 실패 확인 → 구현) 같은 것을 얻으면서 왕복이 하나
> 늘고, 계약이 이미 고정하는 것을 한 번 더 고정하게 된다.

## 진입 조건

- 02 가 `passed` 이거나 `skipped` 다
- 플랜이 있다
- 계약 파일이 있고 필수 절을 담고 있다 (`no_contract` 모드가 아닌 한)

## 절차

1. **메인이 계약 파일을 직접 쓴다.** 역할 에이전트에게 위임하지 않는다.
   템플릿은 `harness/templates/contract.md` 이고 절 제목은
   `config.contract.sections` 가 단일 출처다. 한쪽만 고치면 계약 추적이 조용히
   아무것도 못 찾고 통과한다.
   **`## 유닛`·`## 진입점` 은 최상위 `- ` 불릿 하나에 항목 하나다.** `### `
   헤딩·들여쓴 불릿·산문은 파서가 유닛으로 세지 않는다 — 유닛이 0 인 계약은
   `record` 가 exit 8 로 거부한다 (ADR-H049). 라우트 파일이 관례로 내보내는
   `POST`·`maxDuration` 같은 이름은 어댑터 `implied_exports` 가 말하므로
   유닛에 다시 적지 않는다. 진입점 경로의 파라미터는 `{id}` 로 적어도
   어댑터 `param_styles` 가 실제 폴더명(`[id]`)으로 찾는다.
2. 계약의 유닛·진입점 항목 수를 세어 프로파일을 확정한다.
3. **역할 전원을 한 메시지 안에서 동시 호출한다.** 각 역할에게 지시문 패킷을
   준다 — 패킷에 **소유권 표**가 들어 있고, 그 표가 소유 경계의 유일한 출처다.
4. 각 역할의 제출물을 받아 `03_claims.json` 으로 합친다.
5. 소유 검사와 컴파일 게이트를 돌린다.

### docs 레인 — 역할 0명 (ADR-H044)

00 이 `docs` 로 예측한 런은 **역할 에이전트를 부르지 않고 계약도 쓰지 않는다**
(`allow.unless` · `state.contract.mode == "no_contract"`). 메인이 직접 문서를
고치고 `03_claims.json` 을 `{"schema":1,"roles":[]}` 로 낸다 — 문서는
`main_owned_paths` 라 소유 검사가 그것을 위반으로 보지 않는다.

**역할 소유 경로(소스)를 건드리면 제출이 exit 3 으로 되돌아온다.** 예측이
빗나간 것이다 — 프로파일이 `normal` 로 오르고 `triage_miss` 가 gap 으로
남으며(01 리뷰어 · 02 · 역할을 건너뛴 채 여기까지 왔으므로), 계약을 쓰고
`next` 로 역할 패킷을 받는다. 이미 고친 소스는 그 역할이 claim 한다.

## 역할 프롬프트 템플릿

각 역할에게 다음을 준다. **소유 glob 을 문장으로 옮겨 적지 않는다** — 표를
그대로 싣는다. 옮겨 적으면 설정과 갈라지고, 갈라진 날 워커가 자기 것이 아닌
파일을 조용히 고친다.

```
## 네 소유 경계
| 역할 | 소유 | 제외 |
|---|---|---|
| {role.id} | {role.owns} | {role.excludes} |

메인 단독 소유(누구도 만지지 않는다): {config.main_owned_paths}

## 계약
{계약 파일 전문}

## 읽을 곳
- {config.project.instruction_file}
- {config.project.rules_dir}/ 의 직속 .md
- 계약 파일 (위)
- .claude/agent-memory/{role.id}/

위 두 줄의 파일은 **읽은 뒤 sha256 을 계산해** 제출의 `rules_read` 에
`{path, sha256}` 로 적는다 — 게이트가 현재 해시와 대조한다 (ADR-H055).

## 제출
{역할별 JSON — 아래 제출 형식}
```

## 제출 형식

```json
{"schema":1,"roles":[
  {"role":"impl","agent":"…","status":"ok|blocked",
   "rules_read":[{"path":"CLAUDE.md","sha256":"…"},{"path":"docs/PRD.md","sha256":"…"}],
   "claimed_files":["…"],
   "contract_symbols_implemented":["…"],
   "blocked":[],"notes":"…"},
  {"role":"test","agent":"…","status":"ok|blocked",
   "rules_read":[{"path":"CLAUDE.md","sha256":"…"}],
   "claimed_files":["…"],
   "contract_symbols_covered":["…"],
   "blocked":[]}]}
```

`claimed_files` 에 **실제로 쓴 파일 전부**를 적는다. 빠뜨리면 그 파일이
orphan(아무도 claim 하지 않은 변경)으로 잡혀 03 전체가 거부된다.

**`rules_read` 는 역할마다 필수다** (ADR-H055). `config.project.instruction_file`
과 `rules_dir` 직속 `.md` 전부의 sha256 을 워커가 파일을 읽어 직접 계산해
적는다. 누락·불일치는 exit 8 이고 봉투는 경로와 상태만 말한다 — 해시를 주면
안 열고도 맞추기 때문이다. **해시 일치는 "읽었다" 의 증명이 아니다.** 그러나
"열어 보지도 않고 지켰다고 보고" 는 막힌다 — 이 하네스가 최악으로 치는 실패가
그것이다. 규칙 파일이 런 중에 바뀌면(메인이 문서를 고치면) 재제출이 필요하다 —
바뀐 규칙을 안 본 제출이기 때문이고, 그것이 의도다.

## 금지

- **계약을 역할 에이전트에게 쓰게 하지 마라.** 이유: 계약은 메인 단독 소유이고,
  구현자가 계약을 쓰면 계약이 구현을 따라간다
- **소유 경계 밖의 파일을 만들거나 고치지 마라.** 이유: 병렬이 안전한 유일한
  근거가 소유가 겹치지 않는다는 것이다. 겹치면 두 역할이 서로를 덮는다
- **계약이 틀렸다고 고치지 마라.** 이유: 발견은 값지지만 수정은 메인의 몫이다.
  `CONTRACT_DEFECT` 로 보고하면 메인이 고치고 델타를 다시 내린다
- **커밋·푸시하지 마라.** 이유: 파이프라인이 지문을 잡는 시점이 정해져 있고,
  중간 커밋은 그 지문을 앞당겨 게이트 영수증을 어긋나게 한다

## 실패 시

| 무엇 | 어떻게 |
|---|---|
| `rules_read` 누락·불일치 | exit 8 — 어느 역할의 어느 파일이 빠졌거나 낡았는지 봉투에 나온다. 해시는 안 준다 |
| 소유 경계 침범 | exit 8 — 어느 파일을 어느 역할이 되돌릴지 봉투에 나온다 |
| orphan 파일 | exit 8 — claim 에 없는 변경이다. 적었거나 지웠어야 한다 |
| 컴파일 실패 | 04 의 귀속 규칙으로 소유자를 정해 그 역할에게만 되돌린다 |
| 계약 결함 보고 | 메인이 계약을 고치고 델타를 내린다. 카운터를 소모하지 않는다 |
