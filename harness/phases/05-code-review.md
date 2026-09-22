---
{
  "id": "05-code-review",
  "index": 5,
  "requires": [
    {"kind": "state", "pointer": "phases.04-gate.status", "equals": "passed"},
    {"kind": "file", "path": "${run.contract_file}", "min_bytes": 200,
     "must_contain": "${config.contract.sections.units}",
     "unless": "state.contract.mode == \"no_contract\""}
  ],
  "produces": [
    {"key": "review_raw", "path": "${run.dir}/05_review_${config.reviewers.0.code}.raw.md",
     "kind": "markdown"},
    {"key": "review_json", "path": "${run.dir}/05_review_${config.reviewers.0.code}.json",
     "kind": "json"},
    {"key": "trace", "path": "${run.dir}/05_trace.json", "kind": "json",
     "owner": "executor"},
    {"key": "review", "path": "${run.dir}/05_review.json", "kind": "json",
     "owner": "executor"}
  ],
  "gate": {
    "runner": "adapter", "fail_fast": true,
    "steps": [
      {"id": "compile", "loop_stage": true},
      {"id": "scoped", "tests_from": "contract", "loop_stage": true},
      {"id": "full"}
    ]
  },
  "submit_checks": [
    {"id": "reviewer_not_main", "on_fail": 8},
    {"id": "source_quote_substring", "on_fail": 8},
    {"id": "raw_json_severity_match", "on_fail": 8},
    {"id": "monotonicity", "on_fail": 8}
  ],
  "loop": {"counter": "review_repair", "max": 2, "on_exceed": "escalate"},
  "trace_loop": {"counter": "trace_repair", "max": 2, "on_exceed": "escalate"},
  "allow": {"agents": "config.roles[].agent"},
  "on_success": "06-pr"
}
---

## 목적

**04 가 "돌아가는가"를 봤다면 05 는 "계약대로인가, 그리고 봐야 할 눈이 봤는가"를
본다.**

이 페이즈가 막는 실패는 하나다 — 리뷰어가 실패해도 findings 는 0건이고, 그 0을
"지적이 없다"로 읽으면 **아무도 보지 않은 코드가 통과한다.** 게이트는 초록불이고
보고서도 성공처럼 보인다. 그래서 "리뷰가 수행됐는가"를 findings 개수와 **분리된
신호**(`review05.status`)로 만든다.

리뷰어는 **하나**다 — `config.reviewers[0]`(`gen`, 일반 정합성). 실물 4런에서
data·sec 리뷰어가 낸 Critical 은 전부 gen 이 같이 잡았고, 나머지 관점의 수확은
마찰(오탐·형식 왕복·`merged` 혼동)보다 작았다 (ADR-H075). 다른 눈은 07 의
`/code-review` 다.

## 진입 조건

- 04 가 `passed` 이고 워크트리 지문이 유효하다
- 계약 파일이 필수 절을 담고 있다 (`no_contract` 모드가 아닌 한)
- `precheck` 가 통과했다 — 브랜치 · base · 인프라. 예산(파일·줄)은 정보 행이고
  파일 수는 어댑터 `attribution.test_file_globs` 에 걸린 테스트 파일을 뺀 소스
  파일 수다 (ADR-H066)

## 절차

**비용 오름차순이고 첫 실패에서 멈춘다.** 1·2번이 무료다 — 뒤에서 되돌릴 일을
여기서 먼저 잡는다.

```
1. precheck --scope pr      정적 · 무료   예산 · 브랜치 · divergence · 인프라
2. contract-trace           정적 · 무료   계약 ↔ 코드 대조 5종
3. Critical 있으면 선수리 + gate --phase 05 --stage loop             → 2로 복귀
   (trace_repair 상한 — 초과면 에스컬레이션)
4. 리뷰:  gen 1명. 인라인 상한 초과면 diff 대신 경로 전달
5. 수리 → gate --phase 05 --stage loop → next → 델타 재리뷰 (같은 gen 1명)
6. 코드 확정 → gate --phase 05 --stage full  (05 수리가 있었으면 approve 가 exit 3 으로 요구한다)
```

### 1·2번 — 모델을 부르지 않는다

```bash
python scripts/pipeline/cli.py precheck --scope pr --run-id {run_id}
python scripts/pipeline/cli.py contract-trace --run-id {run_id}
```

- `precheck` **exit 9** 는 브랜치·base 뿐이고 사람의 판단이다. **자동으로
  리베이스하지 마라** — 히스토리는 사람의 것이다. 브랜치를 옮기거나 리베이스한 뒤
  같은 명령을 다시 친다. 예산(파일·줄)은 정보 행이라 멈추지 않는다 (ADR-H075).
  **exit 10** 은 인프라이고 카운터를 소모하지 않는다.
- `contract-trace` **exit 8** 은 "리뷰어를 부르기 전에 고쳐라"다. 고친 뒤
  `gate --phase 05 --stage loop` 로 재게이트하고 다시 친다. **`loop` 는 이
  페이즈가 선언한 루프 구간 전부**(compile → scoped)다 — scoped 만 돌리면
  테스트 러너가 타입체크 없이 통과시킨 타입 에러가 PR 까지 흘러간다
  (ADR-H046). 수리 뒤 재게이트도 같은 명령이다.
- `entrypoint_resolver` 가 없으면 진입점을 푸는 검사 둘만 빠지고 사유와 함께
  `skipped`·`skip_reasons` 에 남는다.
- 테스트 존재 검사(`untested_entrypoint`·`untested_error_symbol`)는 03 제출이 같은
  함수로 이미 요구했다 — 여기서는 보통 0건이고, 04·05 수리 중에 테스트가 사라졌을 때
  잡는다 (ADR-H058).
  **스킵을 통과로 적지 마라.**

### 4번 — 리뷰어는 하나다

누구를 부를지 **네가 정하지 않는다.** 봉투가 `gen` 을 이름 짓고 Agent 호출의
`subagent_type` 을 준다. 소스 변경이 있으면 계획되고, 없으면 0명이다 — 0명은 `review05.status` 가
`failed` 이고 등급이 `PASS_WITH_GAPS` 로 떨어진다. 아무도 안 부른 것은 통과가
아니라 미수행이다. docs 레인은 문서 변경만으로도 계획된다 — 문서와 요청의 정합을
본다.

리뷰어에게 주는 것 — 봉투의 **「리뷰 범위」** 줄이 정한다 (`review.depth`, 값은
하나 `diff+refs` · ADR-H059): **인라인 diff · 계약 · `05_trace.json`** 에 더해
**계약 `## 유닛` 이 참조하는 기존 파일의 경로**. diff 밖 상호작용(낙관적 잠금 ·
상태 가드 · 기존 전이 함수)을 보는 것이 이 범위의 목적이다 — FR-007 의 동시성
결함이 05 를 지나 07 에서 잡혔다. 파일 본문을 인라인하지 말고 경로로 준다.
관점·제출 형식은 에이전트 정의(`.claude/agents/{reviewer.agent}.md`)가 든다 —
본문을 복사해 싣지 마라. `model` 인자를 주지 마라 — 모델·effort 는 그 프론트매터가
정한다 (ADR-H061 · ADR-H076).

## 역할 프롬프트 템플릿

리뷰어에게 보내는 형태다. **작성자에게 보내는 것이 아니다** — 수리 지시는
04 의 템플릿을 그대로 쓴다. 수리 작성자의 모델은 그 역할 에이전트의 프론트매터가
정한다 — 04 수리와 같은 작성자다 (ADR-H064).

```
## 리뷰 요청 — {reviewer.code}

## 변경 (인라인 diff)
{diff}

## 계약
{계약 전문}

## 계약이 참조하는 기존 파일 (경로)
{diff+refs — 계약 `## 유닛` 이 재사용·참조하는 기존 파일 경로 목록}

## 계약 대조 결과
{05_trace.json 의 findings 요약 — 기계가 이미 찾은 것이다. 다시 지적하지 마라}

## 이전 회차에서 열려 있는 네 지적
{previous_open — 닫힌 것은 빠져 있다}

## 낼 것
- `{run_dir}/05_review_{code}.raw.md`  — 출력 원문
- `{run_dir}/05_review_{code}.json`    — 구조화
```

**`previous_open` 에는 이미 닫힌 지적을 넣지 마라.** 넣으면 3라운드 제출이
1라운드에 해소된 지적까지 다시 적어야 하고, 그 목록이 프롬프트에 실려 접두부가
라운드마다 자란다.

## 제출 형식

리뷰어는 회차마다 **두 파일**을 낸다 — 출력 원문 `.raw.md` 와 구조화 `.json`.

```json
{"reviewer":"{code}","round":1,"status":"ok",
 "by_checklist":{"{체크리스트 이름}":[
    {"id":"F-1","category":"AUTHZ_MISSING_RULE","severity":"critical|major|minor",
     "target_role":"impl","title":"…","path":"…","line":34,
     "quote":"raw 원문의 부분문자열","evidence":"…","suggestion":"…"}]},
 "resolved_from_previous":[{"id":"F-2","resolved_by":"…"}],
 "need_more_context":[],
 "model_used":"선택 — 실제로 쓴 모델 id. 자진신고이고 실측이 아니다"}
```

- **`by_checklist` 는 0건인 체크리스트도 명시한다.** 빈 배열로 적는다. 안 적으면
  "안 봤다"와 "보고 아무것도 없었다"가 같은 침묵이 된다
- `reviewer` 가 작성자 역할이면 거부된다. 자기 코드를 리뷰한 것은 독립 관측이 아니다
- **`category` 는 리뷰어 자신의 한 단어 태그(대문자 스네이크)다.** 같은 결함에는
  라운드를 가로질러 같은 값을 쓴다 — `finding_key` 의 재료라 이름이 바뀌면 기계는
  새 지적으로 센다. 계약 자체가 틀렸다고 보면 `CONTRACT_DEFECT` 로 낸다
- `config.reviewers` 에 없는 `code` 는 거부된다. **`next` 가 확정한 `planned` 밖이면
  exit 8 이다** — 분모를 제출자에서 유도하면 누가 리뷰했는지가 리뷰한 사람의
  주장이 된다
- **규약 위반 제출은 1회 되돌린다. 2회째는 그 리뷰어를 `failed` 로 확정하고
  흐름을 잇는다** — 계속 튕기면 리뷰어가 영원히 슬롯에 못 들어가고, 결손이
  등급에 드러날 자리가 없어진다
- **리뷰어 호출 자체가 실패했으면 빈 JSON 을 짓지 마라.** 그것은 깨끗한 리뷰와
  기계적으로 구분되지 않는다. 대신:

```bash
python scripts/pipeline/cli.py record --phase 05 --reviewer {code} \
  --failed --reason "<무엇이 실패했는가>" --round {n} --run-id {run_id}
```

  이 신고는 자진 신고이므로 **기계로 확인 가능한 만큼만 받는다** — 그 라운드의
  제출 파일이 실재하면 신고를 거부한다(exit 8)

**지적의 신원은 제목이다** (`sha1(category|target_role|title)`). 같은 지적을 다음
회차에 **다른 제목으로** 올리면 기계가 그것을 신규 지적이자 동시에 증발한
지적으로 보아 한 번의 재제기가 오탐을 두 번 낸다. 그래서 재제기는 1급 어휘다 —
그 finding 에 `"reraised_from_previous": "F-1"` 을 단다.

### `.raw.md` 의 형태 — 기계가 이것을 검사한다

원문은 **지적 하나에 심각도 헤딩 하나**다. 헤딩 텍스트는 `critical` · `major` ·
`minor` 셋 중 하나이고 `#` 개수는 자유다.

```markdown
# 리뷰 — gen

## critical
`route.ts` 의 새 경로가 인가 캐치올 밖에 있다.

## minor
오류 코드 이름이 규약과 다르다.
```

- **헤딩 개수와 findings 개수가 같아야 한다.** 다르면 제출이 exit 8 로 튕긴다
- 각 `quote` 는 이 원문의 **부분문자열**이어야 한다. 공백만 정규화해 대조한다
- 지적이 0건이면 헤딩도 0개다. 원문은 그래도 낸다

**메인이 사후에 헤딩을 붙여 맞추면 이 손잡이가 사라진다** — 리뷰어가 처음부터
이 형태로 써야 한다.

### 절단 규칙

- findings 가 `config.review.findings_max` 를 넘으면 Critical/Major 만 남기고
  절단하되 **`truncated: true` 로 드러낸다**

## 금지

- **리뷰어를 늘리지 마라.** 이유: 한 명이고 봉투가 이름 짓는다. 모델이 더 부르면
  같은 diff 가 런마다 다른 리뷰를 받는다
- **리뷰어에게 리포 탐색을 허용하지 마라.** 이유: 탐색한 맥락으로 지적하면
  요청에 없는 요구가 끌려 들어온다. 부족하면 `need_more_context` 에 적게 한다.
  계약이 참조하는 기존 파일과 재사용 심볼의 정의는 예외다 — 경로를 준다
- **계약을 고치지 마라.** 이유: 계약은 메인 단독 소유이고, 리뷰어가 계약 결함을
  발견하면 그것은 수리가 아니라 **에스컬레이션**이다(`CONTRACT_DEFECT`)
- **에이전트 정의 본문을 프롬프트에 복사하지 마라.** 이유: Agent 호출이 정의를
  싣는다 — 두 번 실으면 토큰만 든다
- **Minor 를 고치려 들지 마라.** 이유: 수리 대상은 Critical/Major 뿐이다.
  Minor 는 보고서로 간다. **다만 다음 회차 제출에서 회계는 한다**
  (M38) — 단조성 검사는 심각도를 가리지 않고 열린 지적 전부를 요구하고, 하나라도
  빠지면 "조용히 증발했다"로 exit 8 이다. **수리 면제이지 회계 면제가 아니다.**
  회계할 목록은 수리 봉투가 직접 적어 준다. 제출이 내용은 그대로이고 회계 필드만
  틀려 exit 8 로 되돌아오면 메인이 그 필드를 고쳐 재제출해도 된다 — quote·헤딩 수·
  severity 는 여전히 손대지 않는다 (ADR-H052)
- **소스를 고친 뒤 재게이트 없이 넘어가지 마라.** 이유: 지문이 어긋나 06 이
  자동으로 막는다. 막히는 것이 정상 동작이다
- **여기서 push 하거나 PR 을 만들지 마라.** 이유: 그것은 06 의 일이다. 06 이 승인과
  지문을 확인한 뒤에야 push 한다

## 실패 시

| 무엇 | 분류 | 어떻게 |
|---|---|---|
| `precheck` 브랜치 불일치 · base behind | 정책 | **exit 9 즉시 사용자 판단.** 자동 리베이스 금지 — 옮기거나 리베이스한 뒤 다시 부른다 |
| `infra_preflight` 프로브 실패 (`on_missing: fail`) | infra | exit 10 · **카운터 미소모** · 즉시 에스컬레이션 |
| 〃 (`on_missing: warn`) | — | exit 0 + `infra_skipped:{name}` gap · 등급 `PASS_WITH_GAPS`. **면제는 통과가 아니다** — 키 없이도 목업으로 도는 경로가 있을 때만 쓰고, 그 이유를 어댑터의 `why` 에 적는다 (M44) |
| 계약 부재 | — | `no_contract` 모드로 진행. `skipped_no_contract` 로 기록 |
| 계약이 재개 사이에 변경됨 | 정책 | exit 3 + "03 부터 재실행" |
| 리뷰어 호출 실패·타임아웃 | infra | 1회 재시도 → 실패 시 `--failed --reason` 으로 신고. `review05.status = failed` → 등급 `PASS_WITH_GAPS` |
| **계획된 리뷰어가 0명** (소스 변경 없음) | 정책 | 같은 처리. 아무도 안 부른 것은 통과가 아니라 미수행이다. **`next` 가 그 자리에서 상태에 확정한다** — 제출이 0건이면 판정이 아예 안 불리므로 여기서 안 쓰면 아무도 안 쓴다 |
| 커밋만 있고 워킹트리에 소스 변경이 없다 | 절차 | exit 3 — 05 통과 전에 커밋했다. `git reset --mixed HEAD~1` 로 되돌리고 `next` 를 다시 친다 (ADR-H046) |
| 리뷰어가 JSON 대신 산문 | 제출물 | exit 8 · 재제출 1회 → 2회 실패 시 **슬롯에 `failed` 로 확정**하고 흐름을 잇는다 |
| 실패 신고인데 제출 파일이 있다 | 제출물 | exit 8. 자진 신고 중 기계로 확인 가능한 것은 기계로 확인한다 |
| quote 위조 · 단조성 위반 | 제출물 | exit 8 · 같은 회차 재제출(카운터 소모) |
| `need_more_context` 계속 참 | 판단 | 1회에 한해 파일 목록 명시 추가 |
| `CONTRACT_DEFECT` 발견 | 정책 | 수리하지 않는다 → **에스컬레이션** |
| diff 가 인라인 상한 초과 | — | **기계가 정한다** — `next` 가 `review.inline_max` 로 재고 봉투가 "경로로 전달하라" 고 말한다. 네 재량이 아니다 (ADR-H042). 폴백 사실이 상태에 남는다 |
| `review_repair` 초과 | 정책 | 에스컬레이션 — 선택지 없이 자유 서술로 사람에게. **계약 결함을 먼저 의심**하라고 패킷에 적는다 |
| `trace_repair` 초과 (contract-trace Critical 이 2회) | 정책 | 에스컬레이션 — 같은 처리. 선수리 루프도 천장이 있다 |
| 수리 뒤 재게이트 없이 `next`·`record` | 정책 | **exit 6** — 게이트 영수증 지문이 낡았다. `gate --phase 05 --stage loop` 뒤 다시 친다. 리뷰어는 게이트된 코드만 본다 |
| 제출이 내용은 그대로인데 회계 필드만 틀려 exit 8 | 기계 | `format_reject` 이벤트로 센다. 회계 필드는 메인이 고쳐 재제출해도 된다 — quote·헤딩 수·severity 는 여전히 금지 (ADR-H052) |

**`review_repair.max: 2` 와 `findings_max`·`inline_max` 는 미검증 상속값이다.**
원본 명세에서 왔고 이 리포에서 재본 적이 없다. 실측이 이 값을 검사한다.

**`loop.counter` · `loop.max` · `loop.on_exceed` 는 코드가 여기서 읽는다** (M36).
`trace_loop` 도 같은 모양으로 읽는다 — contract-trace 선수리 루프의 상한이다 (ADR-H076).
예전에는 카운터 이름이 코드에 박혀 있었고 상한에는 `or 2` 폴백이 있었다 —
**폴백은 곧 새 하드코딩이다.** 지금은 선언이 없으면 exit 2 이고, 그 사실을
`lint-phases` 가 런 전에 먼저 잡는다.
