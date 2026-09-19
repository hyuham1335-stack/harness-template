---
{
  "id": "07-pr-review",
  "index": 7,
  "owner": "main",
  "approval": "inherited:06",
  "requires": [
    {"kind": "state", "pointer": "phases.06-pr.status", "equals": "passed"},
    {"kind": "state", "pointer": "pr.pushed", "equals": true}
  ],
  "produces": [
    {"key": "pr_review", "path": "${run.dir}/07_pr_review.json", "kind": "json",
     "schema": "pr_review"},
    {"key": "promo_applied", "path": "${run.dir}/07_promo_applied.json",
     "kind": "json", "schema": "promotions"}
  ],
  "gate": {"runner": "none"},
  "submit_checks": [
    {"id": "external_status_vocabulary", "on_fail": 8},
    {"id": "source_quote_substring", "on_fail": 8},
    {"id": "change_request_open", "on_fail": 10}
  ],
  "loop": {"counter": "pr_repair", "max": 2, "stuck_after_identical": 2,
           "on_exceed": "escalate"},
  "allow": {"agents": []},
  "on_success": "08-report"
}
---

## 목적

**05 는 우리가 우리 코드를 봤다. 07 은 밖이 그것을 어떻게 보는가를 받는다.**

이 페이즈가 막는 실패는 하나다 — **"아무도 안 봤다"가 "통과"가 되는 것.**
관측기를 **빼지 않고 바꾼다** (ADR-H043): 일반 정합성("구현이 계약대로
동작하는가")은 05 의 `gen` 리뷰어가 소스 변경마다 보고, 07 의 내장 리뷰는
**신호가 있는 런**에서만 돈다 — 05 가 `ok` 가 아니거나, 00 의 예측이 빗나갔거나,
05 의 지적이 0건이거나, 감사 런이다. **"Major 가 남았다" · "04·05 에 수리가
있었다" 는 신호가 아니다** (ADR-H059) — 05 는 매 런 Major 를 내므로 그 둘은
파일럿 11런 중 9런에서 07 을 강제했고, 그 Major 는 05 안에서 수리·델타
재리뷰를 이미 받은 것이었다. 두 번째 눈의 표본은 감사 런이 산다. 그 밖의 런은
`skipped` 이고 등급이 내려가지 않는다. 봇을 켜 놓고 무응답이면 그것은 있어야
할 관측기가 없는 것이라 여전히 gap 이고 내장 리뷰가 대신 돈다.

**`docs` 레인은 `docs_profile` 로 생략한다** — 소스 변경이 없고 05 의 docs
리뷰어가 봤으니 내장 코드 리뷰가 볼 코드가 없다. **`fix` 레인은 `fix_profile`
로 생략한다** — 05 의 `gen` 이 수리 하나를 봤다. 단 05 의 지적이 0건이면 low 로
한 번 돈다 (ADR-H050 · ADR-H053). **00 의 예측이 빗나간 런
(`profile.triage_miss`)은 `medium` 이다** — 앞 페이즈가 양보를 적용한 채
지나갔으므로 건너뛴 관측을 비싼 쪽으로 메운다 (ADR-H044). gap 은 miss 시점에
이미 적혔고 여기서 다시 세지 않는다.

승격도 여기서 한 번 일어난다. 05 는 후보(`staged`)만 만들었고, **실제 쓰기는
07 이 런당 한 번** 한다 — dedup 이 로직이 아니라 시점으로 성립하고, 기능 PR
diff 에 규칙 문서 변경이 섞이지 않는다.

## 진입 조건

- 06 이 `passed` 이고 `state.pr.pushed` 가 참이다
- 승인은 **06 에서 상속한다** (`inherited:06`) — 07 이 따로 받지 않는다.
  다만 **코멘트 게시는 등급을 재확인한 뒤**다 (§3.6 의 `--auto` 축소)
- **PR 상태를 먼저 본다.** 닫혔거나 머지됐으면 수리도 코멘트도 하지 않고
  정상 종료한다 (§E8)

## 절차

```
1. PR 상태 확인          너 · forge 도구    닫힘·머지면 여기서 정상 종료
2. 외부 리뷰 수집        너 · forge 도구    폴링 → 07_external.json
3. review07              정적 · 무료        생략 조건 판정 · effort 결정
4. /code-review          모델               3번이 부르라고 하면 그 effort 로
5. record --phase 07     제출               정규화 · dedup · escaped_05
6. promote --scan        정적 · 무료        후보 0 이면 모델 없이 종결
7. promote --apply       git · 스테이지         별도 브랜치 · 베이스라인과 자체 게이트를 기계가 잰다
8. 코멘트 게시           너 · forge 도구    단일 코멘트 하나 · mask 를 거친다
```

### 1·2번 — 외부 상태는 내가 안 보는 사이에 바뀐다

PR 상태를 forge 도구로 먼저 읽는다. **닫혔거나 머지됐으면 아무것도 하지
않고** 보고서에 그 사실을 적는다. 머지된 PR 에 코멘트를 달거나 이미 머지된
코드를 수리하지 않는다.

외부 리뷰는 **최소 출력 프로브로 존재만** 확인하고, 붙었으면 **1회만 전문**을
가져온다. 매번 전문을 끌면 폴링 비용이 그대로 접두부가 된다.

`external.status = "reviewed"` 의 판정은 **구조로 한다** — 봇 계정의 리뷰나
코멘트가 있고 그 안에 findings 구조(리뷰 상태 · 코멘트 개수 · 심각도 라벨)가
있어야 한다. **헤딩 텍스트로 판정하지 마라** — 봇의 출력 언어에 의존하게 된다.
구조 판정에 실패하면 `not_a_review` 다. **심각도를 못 가르면 Major 로
낙하시킨다** — 모르면 생략하지 않는 방향이다.

### 3·4번 — 누구를 부를지 정하는 것은 너가 아니다

```bash
python scripts/pipeline/cli.py review07 --external {07_external.json} --run-id {run_id}
```

봉투가 `--effort` 를 알려 준다. 그 값 그대로 `/code-review` 를 부른다.
**네가 effort 를 고르면** 같은 상황이 런마다 다른 리뷰를 받고, `escaped_05`
를 세는 것이 의미를 잃는다.

- `review05.status != ok` → **medium** (리뷰 결손을 비싼 쪽으로 메운다)
- 봇을 켜 놓았는데 `timeout` · `not_a_review` → **low** + 등급 `PASS_WITH_GAPS`
- 00 의 예측이 빗나갔다 (`profile.triage_miss`) → **medium**
- `docs` 레인 → **skipped** (`docs_profile`) · `fix` 레인 → **skipped**
  (`fix_profile`, 단 05 지적 0건이면 low)
- 외부가 `reviewed` 이고 `small` 레인 → **skipped** (`clean_05`)
- 외부 리뷰에 Major 가 있다 → **low**
- 05 의 지적이 0건이다 → **low**. 0 은 "봤는데 없었다" 와 "보지 않았다" 를
  가르지 못한다 (ADR-H050)
- 그 밖 → **skipped** (`skip_reason: clean_05`). 05 에 Major 가 남았거나
  04·05 에 수리가 있었어도 같다 — 그 Major 는 05 안에서 수리·델타 재리뷰를
  이미 받았고, 두 번째 눈은 감사 런이 산다 (ADR-H059). 봇이 config 로 꺼진
  `disabled` 여도 성립한다 — 일반 정합성은 05 의 `gen` 이 봤다 (ADR-H043).
  등급이 내려가지 않는다. **`/code-review` 를 부르지 말고** `07_pr_review.json`
  을 `code_review: "skipped"` · findings 빈 배열로 내고 바로 `record` 로 간다 —
  5~8번은 그대로 돈다
- `audit_run` — 5런마다 1회, 생략 조건을 만족해도 **high** 를 강제한다.
  **생략하면 `escaped_05` 를 셀 수 없기 때문**이고, 5런에 1회의 비용으로
  정책의 근거를 산다 (§E2). 표본이라 낮은 effort 는 과소측정이다 (ADR-H061)

### 6·7번 — 승격은 런당 한 번이고, 대개 아무 일도 없다

```bash
python scripts/pipeline/cli.py promote --scan --run-id {run_id}
```

**후보가 0 이면 모델을 부르지 않고 종결한다.** 초기 런에서는 이것이 최빈
경로다 — 원장이 비어 있고 임계값(critical 2회 · major 3회 · minor 5회, 전부
`distinct_runs` 조건과 함께)에 닿을 표본이 아직 없다.

후보가 있으면 판정(`create` / `amend` / `skip` / `retire`)을 **기록으로
남긴다.** `duplicate` 면 `create` 가 금지되고, `contradicts` 면 자동 쓰기가
차단되며 에스컬레이션이다. **"일단 붙이기"가 선택지에 없다.**

**후보는 기계 강제(`lint`·`check`) 목적지뿐이다** (ADR-H056). 스캔이 함께 찍는
「지시문 검토 후보」(prose)는 여기서 판정하지 않는다 — 08 지시문 검토로 간다.
「검사 반복 검출」은 `contract-trace` 가 이미 막는 규칙이라 후보가 아니다.
근본 원인을 하네스에서 고친 규칙은 어느 쪽이든 `action: retire` + `rule_key` +
`retired_reason` 으로 관측을 끊는다 — 이후 관측은 0 부터 다시 센다.

**`skip` 에는 `rationale` 이 필수다** — 비면 exit 8 (ADR-H051). 그리고
승격 판정 시한(원장이 본 런 ≥ 9, ADR-H033)이 지난 뒤에도 후보를 `skip` 으로
닫으면 `promote --flush` 가 gap `promotion_overdue` 로 등급을 내린다 — 시한은
더 이상 표시만이 아니다. 판정하거나 임계를 고친다.

`--apply` 는 **규칙 전용 브랜치**에서 돈다 — 규칙 파일을 그 브랜치에 쓴 뒤
부른다. **브랜치를 만들고 폐기하는 것은 실행기 밖이고 네 일이다.** 자체
게이트(`lint` + `check`)는 **실행기가 돌린다** (ADR-H065) — `--apply` 가 어댑터의
두 스테이지를 **현재 워크트리에서** 돌리고, 하나라도 0 이 아니면 기계 강제
승격을 전부 `rejected` + 사유로 적는다. 그때 브랜치를 폐기한다 — **기능 PR 은
영향받지 않는다.** 규칙 전용 브랜치가 아닌 곳에서 부르면 게이트는 기능 코드와
규칙을 함께 잰다 — 실행기는 그것을 구분하지 못한다.

- 여기서는 **종료 코드가 성패다** — 베이스라인과 다르다
- 실행 자체가 불가능했으면(127 · 124) `infra` 이고 **exit 10**, 아무것도 안 쓴다
- 어댑터에 `lint` · `check` 명령이 없으면 막지 않고 갭
  `promotion_selfgate_unverified` 로 등급이 내려간다
- 문서 승격 · `retire` · `skip` 만 있으면 게이트를 돌리지 않는다

**`lint` 승격의 베이스라인은 실행기가 직접 잰다.** `--apply` 가 어댑터의
`baseline_cmd` 를 돌리고 `baseline_file` 의 VCS 변화를 본다. 네가 미리 돌릴
필요도, 결과를 옮겨 적을 필요도 없다.

- **종료 코드를 성패로 읽지 않는다.** 린터가 위반을 찾으면 0 이 아니고 그것이
  정상이다. 판정은 오직 **베이스라인이 바뀌었는가**가 한다
- 실행 자체가 불가능했으면(127 · 124) `infra` 이고 **exit 10** 이다. 아무것도
  쓰이지 않았으니 다시 치면 된다 — `rejected` 가 아니다
- 어댑터에 `baseline_cmd` 가 없는 스택이면 막지 않고 통과시키되 갭
  `promotion_baseline_unverified` 를 남기고 등급이 내려간다. **스킵은 통과가
  아니다**

### 8번 — 단일 코멘트 하나다

인라인 pending 흐름을 쓰지 않는다. 왕복이 늘고 스레드 정리 부담만 커진다.
본문은 **`mask` 를 거친다.** 게시 실패는 infra 이고 2회 재시도 후 비차단
스킵이다 — findings 는 원장에 남으므로 유실이 아니다.

## 제출 형식

**`external` 을 신고하지 마라.** 외부 리뷰의 상태와 Major 수는 `review07` 이
봇 원문에서 **다시 세고**, `record` 는 그 값을 쓴다. 실으면 버리지 않고
**대조한다** — 다르면 exit 8 이고 두 값을 나란히 보여 준다. 자진 신고 중 기계로
확인 가능한 것은 기계로 확인한다(불변식 8). `review07` 을 안 돌리고 `record`
부터 치면 exit 3 이다.

**`baseline_diff` 도 신고하지 마라.** 같은 이유다 — `--apply` 가 `baseline_cmd`
를 직접 돌려 재고, `rules_changelog.md` 에는 **기계가 잰 값**이 들어간다. 실으면
버리지 않고 **대조한다** — 다르면 exit 8 이고 두 값을 나란히 보여 준다.


`{run_dir}/07_pr_review.json` 하나를 내고 `record --phase 07` 을 부른다.
finding 은 **05 와 같은 스키마**를 쓴다 — **`rule_slug` 규칙도 같다.** 어휘를
선언한 `category` 는 봉투의 「규칙 슬러그」 절에서 하나를 골라야 하고, 안 적거나
어휘 밖을 적으면 제출이 exit 8 로 되돌아온다. 05 와 07 이 같은 결함에 다른 축을
쓰면 `escaped_05` 계수가 그 경계에서 어긋난다.

```json
{"external": {"status": "reviewed|disabled|not_a_review|timeout", "major": 0},
 "code_review": "skipped|low|medium|high",
 "findings": [
   {"id": "G-1", "category": "AUTHZ_MISSING_RULE", "severity": "major",
    "target_role": "impl", "title": "…", "path": "…", "line": 34,
    "rule_slug": "어휘를 선언한 category 에서만 · 봉투의 「규칙 슬러그」 절 참고",
    "quote": "원문의 부분문자열", "source": "external|code-review|human",
    "evidence": "…", "suggestion": "…",
    "reraised_from_previous": "05 의 열린 지적 키 (같은 결함일 때만)"}],
 "change_requested": false,
 "human_comments": []}
```

- `source` 는 닫힌 어휘다 — `code-review` · `external` · `human`
- **`human` 은 수리 대상이 아니라 보고 대상이다.** 파이프라인이 사람과
  논쟁하지 않는다
- `quote` 는 외부 리뷰 **원문의 부분문자열**이어야 한다. 05 와 같은 검사다
- `change_requested: true` 인데 findings 가 비면 exit 8 — 무엇을 고치라는
  것인지 없이 차단만 하는 제출이다
- **네가 그 자리에서 고쳤으면 `"resolution": "repaired"` 와
  `"repaired_by": "main"` 을 적어라.** 안 적으면 `deferred` 이고, 그것이
  기본값이다 — **안 적은 것은 안 고친 것이다.** 07 에서 수리하는 주체는
  메인뿐이라 다른 값은 exit 8 이다.
  - **이 주장은 대조된다.** `repaired` 는 그 finding 의 `path` 를 건드린 변경이
    **PR push 이후에 실재할 때만** 받는다(커밋과 워킹트리 둘 다 본다). 없으면
    exit 8 — 자진 신고 중 기계로 확인 가능한 것은 기계로 확인한다(불변식 8).
    `path` 없이 `repaired` 를 주장할 수도 없다
  - 고쳐진 지적을 `deferred` 로 두면 **`EXCLUDED_FROM_COUNT` 밖이라 "반복되는
    미해결" 로 승격 집계에 학습된다** (M49)
- **지적이 틀렸다고 직접 확인했으면 `"resolution": "false_positive"` 를 적고
  `evidence` 에 왜 틀렸는지 적어라** (ADR-H050). `deferred` 로 두면 오탐이
  "미해결" 로 승격 집계에 들어가고, 리뷰어 품질을 셀 축이 사라진다 — 08 이
  리뷰어별 `repaired / deferred / false_positive` 를 센다. 확인하지 않은
  것을 오탐으로 적지 마라 — 그것은 `deferred` 다.
- **05 가 이미 낸 것과 같은 결함이면 `reraised_from_previous` 로 가리켜라.**
  `escaped_05` 의 대조는 `sha1(category|target_role|title)` 이라 **네가 같은
  결함에 다른 이름을 붙이면 새 것으로 센다** — 그러면 05 라우팅 품질의 유일한
  지표가 05 를 실제보다 나쁘게 적는다. 가리킬 키는 봉투의 「05 가 이미 낸 지적」
  절에 있고, **목록에 없는 키를 가리키면 exit 8** 이다. 새 것이면 아무것도
  달지 않는다 — **안 다는 것이 기본이고 다는 것이 주장이다**

## 금지

- **effort 를 네가 고르지 마라.** 이유: 생략 조건은 결정론이고, 모델이 정하면
  `escaped_05` 가 정책의 근거가 되지 못한다
- **사람 코멘트를 수리하지 마라.** 이유: 파이프라인이 사람과 논쟁하는 자리가
  된다. 보고서에 남기고 사람에게 넘긴다
- **닫히거나 머지된 PR 을 손대지 마라.** 이유: 이미 끝난 것을 수리하는 것이고,
  머지된 코드에 코멘트를 다는 것은 소음이다 (§E8)
- **변경 요청을 미해결로 두고 넘어가지 마라.** 이유: PR 체크가 빨간불인데
  파이프라인이 초록불인 척하게 된다 → exit 10
- **승격을 "일단 붙이기" 로 하지 마라.** 이유: `duplicate` 에서 `create` 하면
  같은 규칙이 두 벌 생기고, `contradicts` 를 무시하면 규칙끼리 싸운다
- **규칙 변경을 기능 PR 에 섞지 마라.** 이유: 리뷰어가 두 가지를 한 diff 에서
  봐야 하고, 승격이 반려되면 기능까지 막힌다
- **머지하지 마라.** 이유: 명세가 머지 자동화를 범위 밖으로 둔다

## 실패 시

| 무엇 | 분류 | 어떻게 |
|---|---|---|
| PR 이 닫힘 · 머지됨 | — | 수리·코멘트 없이 **정상 종료** + 보고서 명시 (§E8) |
| `external_pr_review.enabled == false` | — | `disabled` 는 gap 이 아니다. 내장 리뷰는 05 결과와 수리 흔적이 정한다 — 깨끗한 런은 `skipped`, 그 밖은 돈다 (ADR-H043) |
| 외부 봇 무응답 (켜 놓았는데) | 비차단 | 내장 리뷰 `--effort low` 1회 · `PASS_WITH_GAPS` · 사전 승인 게시 보류 |
| 외부 봇 출력 파싱 실패 | 판단 | `not_a_review` → 생략 불성립. **심각도 불명은 Major 로 보수 판정** (§E1) |
| 변경 요청 미해결 | 차단 | 수리 루프(`pr_repair`). 초과 시 에스컬레이션 |
| 타임아웃 후 외부 리뷰 도착 | — | 08 직전 재확인에서 **등급 강등.** 수리 루프로 되돌아가지 않는다 (무한 대기) |
| 코멘트 게시 실패 | infra | 2회 재시도 → **비차단 스킵**. findings 는 원장에 남는다 |
| 승격 자체 게이트 실패 | 기계 | `--apply` 가 `rejected` + 사유를 쓴다. 너는 브랜치를 폐기한다. **기능 PR 무영향** |
| 승격 push 실패 | 판단 | 브랜치 폐기 + `rejected` + 사유. **기능 PR 무영향** |
| `lint` 승격인데 **기계가 잰** 베이스라인이 그대로 | 판단 | "아무것도 안 막는 규칙" → `rejected` + 사유 |
| `baseline_cmd` 를 실행하지 못함 (127 · 124) | infra | **exit 10.** 아무것도 쓰지 않는다 — 시스템 문제를 "규칙이 아무것도 안 막는다" 로 적지 않는다 |
| 어댑터에 `baseline_cmd` 가 없음 | — | 막지 않고 통과시키되 갭 `promotion_baseline_unverified` + `PASS_WITH_GAPS`. **스킵은 통과가 아니다** |
| 신고한 `baseline_diff` 가 기계값과 다름 | 제출물 | **exit 8.** 두 값을 나란히 보여 준다 |
| 사람 코멘트와 계약이 충돌 | 판단 | **파이프라인이 판단하지 않는다** → 보고서에 남기고 사람에게 |
| forge 도구 불통 | infra | 카운터 미소모 · 에스컬레이션 |

**명세 미규정 셋 — 지어내지 않고 적어 둔다.** ① 명세에 07 의 절차표가 없다
(06·04 와 다르다). 위 절차는 §3.7 산문과 §8.2 실패 매트릭스에서 **재구성한
것**이다. ② `poll_sec` · `timeout_sec` 의 기본값이 없다 — `config` 의 30 · 300
은 **미검증 상속값**이고 캘리브레이션 대상도 아니다. `loop.counter` · `loop.max` ·
`loop.on_exceed` 는 이제 코드가 이 선언에서 읽는다 (M36) —
예전에는 `pr_repair` 와 `2` 가 둘 다 코드에 박혀 있었다. ③ `promote --apply` 의
단독 종료 코드가 없다 — 네 플래그가 `0/4/6/8` 한 행을 공유한다. ④ 명세 §3.7 의
생략 조건은 `reviewed` 를 요구했다 — **ADR-H043 이 뒤집었다.** 그 조건은 봇이
없는 프로젝트에서 07 을 무조건 돌게 했고, 일반 정합성 관점을 05 의 `gen` 으로
옮긴 뒤에는 근거가 사라졌다.
