---
{
  "id": "01-plan",
  "index": 1,
  "owner": "main",
  "approval": "none",
  "docs": ["${config.project.instruction_file}"],
  "requires": [
    {"kind": "file", "path": "${run.dir}/00_original_request.md", "min_bytes": 1,
     "sha256_pointer": "request.sha256"},
    {"kind": "adapter_stage", "steps": ["compile", "lint", "check", "scoped", "full"],
     "mode": "warn"}
  ],
  "produces": [
    {"key": "plan", "path": "${run.dir}/01_plan.md", "kind": "markdown",
     "min_bytes": 200, "must_contain": ["<!-- INTENT", "<!-- COVERAGE"],
     "unless": "state.profile.inv_skipped == true"}
  ],
  "review": {
    "parallel": true,
    "unless": "state.profile.name == \"docs\"",
    "reviewers": [
      {"code": "plan", "kind": "internal",
       "raw": "${run.dir}/01_review_r{n}.raw.md",
       "json": "${run.dir}/01_review_r{n}.json"},
      {"code": "xv", "kind": "cross_verify",
       "raw": "${run.dir}/01_xverify_r{n}.raw.md",
       "json": "${run.dir}/01_xverify_r{n}.json"}
    ]
  },
  "converge": {
    "counter": "round",
    "max_by_profile": {"small": 2, "normal": 3},
    "blocking_severities": ["critical"],
    "one_round_allowed_when": "all_reviewers_non_fallback AND blocking_free",
    "focus_round_2": "불변식 커버리지 · 범위 밖 항목 · 인수 조건의 검증 가능성",
    "on_exceed": "escalate"
  },
  "submit_checks": [
    {"id": "reviewer_not_main", "on_fail": 8},
    {"id": "source_quote_substring", "on_fail": 8},
    {"id": "raw_json_severity_match", "on_fail": 8},
    {"id": "monotonicity", "on_fail": 8},
    {"id": "coverage_exact_once", "on_fail": 8},
    {"id": "plan_section_exists", "on_fail": 8},
    {"id": "coverage_reason_required", "on_fail": 8},
    {"id": "drift_score_zero", "on_fail": 4}
  ],
  "gate": {"runner": "none"},
  "loop": {"counter": "round", "max_by_profile": {"small": 2, "normal": 3},
           "stuck_after_identical": 2, "on_exceed": "escalate"},
  "allow": {"agents": []},
  "on_success": "02-cross-verify"
}
---

## 목적

원본 요청을 **의도로 동결**하고, 그 의도를 빠짐없이 덮는 플랜을 만든다.

동결이 결정론의 앵커다. 요청은 `init` 이 바이트 그대로 복사했고 sha256 이 박혀
있으므로, 이후 어느 페이즈도 "요청이 원래 이랬다"를 새로 지어낼 수 없다. 이
페이즈가 하는 일은 그 바이트를 **검증 가능한 불변식과 인수 조건으로 옮기는 것**이고,
옮기는 과정에서 무엇이 새어 나갔는지를 기계가 셀 수 있게 만드는 것이다.

산출물은 **`01_plan.md` 하나뿐이다.** 불변식·플랜·커버리지를 세 파일로 나누면
파일마다 제출 왕복이 붙는다.

## 진입 조건

- 동결된 요청 원문이 있고 sha256 이 상태의 것과 일치한다
- 어댑터가 선언한 스테이지들이 실재한다 (여기서는 **경고만** 한다 — 뒤 페이즈가
  파일을 고치므로 사전 검사는 예측이지 보장이 아니다. 04 진입 시 다시 강제한다)

## 절차

1. **요청을 읽는다.** 요약하지 말고 그대로 읽는다.
2. **불변식과 인수 조건을 뽑는다.** 각 항목에 `source_quote` 를 단다 —
   요청 원문의 **문자 단위 부분문자열**이어야 한다. 공백만 정규화한 뒤 포함 검사를
   하며, 통과하지 못하면 제출이 거부된다. 이것이 "없는 요구를 지어내거나 실제
   요구를 자기 말로 바꿔치기"를 막는 유일한 기계적 손잡이다.
3. **플랜 본문을 쓴다.** 절 제목은 자유이고, 커버리지가 그 제목을 참조한다.
4. **커버리지 표를 채운다.** 모든 불변식을 정확히 한 번씩 덮어야 하고,
   `covered` 가 아니면 `reason` 이 필수다.
5. **1라운드는 리뷰어 둘을 병렬로** 돌린다. 하나는 내부 플랜 리뷰어, 하나는 외부
   교차검증기다. **2라운드부터는 열린 차단 지적을 낸 리뷰어만** 다시 온다 —
   봉투의 `planned` 가 누구인지 말하고, 그 밖의 리뷰어는 부르지 않는다
   (05 의 델타 재리뷰와 같은 규율, ADR-H041).
6. 지적을 반영할 때 **플랜을 통째로 다시 쓰지 않는다.** 부분 편집으로 고친다 —
   전문이 라운드마다 다시 쌓이면 접두부가 라운드 수만큼 곱해진다.
7. **라운드를 강제하는 것은 `converge.blocking_severities`(지금은 Critical)뿐이다.**
   Major·Minor 는 기록되고 02 패킷과 보고서로 가되 다음 라운드를 열지 않는다.
   02 가 Critical 만 01 로 되돌리므로 문턱이 같다. 열린 Major 를 고칠지는
   작성자의 판단이고, 고쳤으면 다음 회차 제출이 `resolved_from_previous` 로 닫는다.

### 산출물 형태

```markdown
<!-- INTENT
{"invariants":[{"id":"INV-1","kind":"must|must_not","text":"…",
                "source_quote":"요청 원문의 부분문자열"}],
 "out_of_scope":["…"],
 "acceptance":[{"id":"AC-1","text":"…","source_quote":"…"}]}
-->

# 플랜

…본문…

<!-- COVERAGE
{"covers":[{"id":"INV-1","status":"covered","plan_section":"§2 범위"}],
 "added_scope":[]}
-->
```

요청이 짧으면(`config.profile.inv_skip_below_chars` 미만) INV 블록을 생략한다.
한 문단짜리 요청에서 의도 이탈은 물리적으로 일어나기 어렵다.

## 제출 형식

리뷰어는 회차마다 **두 파일**을 낸다 — 출력 원문 `.raw.md` 와 구조화 `.json`.

```json
{"reviewer":"plan|xv","round":1,"mode":"primary|fallback","primary_error":null,
 "findings":[{"id":"F-1","severity":"critical|major|minor","category":"…",
              "title":"…","quote":"raw 원문의 부분문자열","evidence":"…",
              "suggestion":"…"}],
 "resolved_from_previous":[{"id":"F-0","resolved_by":"…"}]}
```
**`mode` 는 둘뿐이다. 부재와 일시 실패는 `primary_error` 로 가른다.**

- `primary_error` 가 **있으면** — primary 를 시도했는데 실패했다(일시). 다음 회차의
  봉투가 **다시 시도하라**고 말한다. 상류 과부하는 대개 한 라운드보다 먼저 끝난다
- `primary_error` 가 **없으면** — primary 가 애초에 없다(구조). 재시도할 것이 없다

셋째 `mode` 값을 만들지 않는 이유는 `converged` 의 `mode == "fallback"` 검사가 새 값을
놓치면 **조용히 1라운드 수렴이 열리기** 때문이다. 07 의 `external` 이 "상태 + 사유" 로
쓰는 것과 같은 형태다.

> **이것은 자진 신고다.** 실행기는 어느 도구가 실제로 불렸는지 볼 수 없다. 다만 유인이
> 안전한 방향이다 — 과잉 신고는 재시도 지시 한 번이고, 과소 신고는 지금과 같다.


`reviewer` 가 `main` 이면 거부된다. 작성자가 자기 글을 리뷰한 것은 독립 관측이 아니다.

**지적의 신원은 제목이다** (`sha1(category|target_role|title)`). 같은 지적을 다음
회차에 **다른 제목으로** 다시 올리면 기계가 그것을 신규 지적이자 동시에 증발한
지적으로 보아 한 번의 재제기가 오탐을 두 번 낸다. 그래서 재제기는 1급 어휘다 —
그 finding 에 `"reraised_from_previous": "F-1"` 을 달아 무엇을 다시 올리는지
말한다. 열려 있는 이전 지적을 가리켜야 하고, 아니면 제출이 거부된다.

`resolved_from_previous` 의 `id` 는 **자기 회차의 자기 지적만** 닫는다. 두
리뷰어가 모두 `F-1`·`F-2` 를 쓰므로 id 를 전역으로 대조하면 한 줄이 서로 다른
두 지적을 동시에 닫는다. 다른 리뷰어의 지적을 닫으려면 `key` 를 쓴다.

### `.raw.md` 의 형태 — 기계가 이것을 검사한다

원문은 **지적 하나에 심각도 헤딩 하나**다. 헤딩 텍스트는 `critical` · `major` ·
`minor` 셋 중 하나이고 `#` 개수는 자유다.

```markdown
# 리뷰

## major
계약이 `dedupeByIsbn` 재사용을 단언하는데 제약을 만족하지 못한다.

## minor
헬퍼 이름이 하는 일과 다르다.
```

- **헤딩 개수와 `findings` 개수가 같아야 한다.** 다르면 제출이 exit 8 로 튕긴다
- 각 `quote` 는 이 원문의 **부분문자열**이어야 한다. 공백만 정규화해 대조한다
- 지적이 0건이면 헤딩도 0개다. 원문은 그래도 낸다

이 대조가 "JSON 은 그럴듯한데 원문에는 없는 지적"을 잡는 유일한 손잡이다.
**메인이 사후에 헤딩을 붙여 맞추면 그 손잡이가 사라진다** — 리뷰어가 처음부터
이 형태로 써야 한다.

## 금지

- **요청 원문 파일을 고치지 마라.** 이유: 그 바이트가 이 런의 유일한 앵커이고,
  sha256 이 어긋나면 모든 페이즈가 진입을 거부한다
- **`source_quote` 를 다듬지 마라.** 이유: 부분문자열 검증이 그 다듬기를 위조와
  구분하지 못한다. 원문이 어색해도 그대로 인용한다
- **플랜을 전체 재작성하지 마라.** 이유: 라운드마다 전문이 다시 쌓인다
- **리뷰어 지적을 조용히 없애지 마라.** 이유: 이전 회차에 열려 있던 지적은 이번에
  다시 나오거나 `resolved_from_previous` 에 해소 근거와 함께 있어야 한다.
  그냥 사라지면 제출이 거부된다

## 실패 시

| 무엇 | 어떻게 |
|---|---|
| `source_quote` 가 원문에 없다 | exit 8 — 해당 항목을 원문 그대로 고쳐 재제출 |
| 커버리지가 불변식을 빠뜨렸다 | exit 8 — 빠진 id 가 봉투에 나온다 |
| `drift_score > 0` | exit 4 — 이탈 항목이 원문 인용과 함께 나온다. 예산이 남아 있다 |
| 라운드 상한 초과 | exit 7 → 에스컬레이션. 미해결 Critical 전문과 3지선다 |
| 같은 Critical 이 `stuck_after_identical` 라운드 반복 | exit 7 → 상한 전에 에스컬레이션. 플랜 수정이 지적을 닫지 못한다 |

**라운드 상한 2(small)/3(normal)은 ADR-H041 이 5 에서 내린 값이다.** 수렴 규칙을
고친 뒤(차단 심각도만 라운드를 강제 · 재제기는 신규가 아님 · 같은 차단 지적이
`stuck_after_identical` 회 반복되면 상한 전에 에스컬레이션) 상한은 천장이지
경로가 아니다. 첫 세 런의 원장(`run.rounds`)이 이 값을 검사한다.

**`stuck_after_identical: 2` 는 이제 01 도 읽는다.** 열린 차단 키 집합이 직전
라운드와 같으면 예산이 남아도 멈춘다 — 플랜 수정이 지적을 닫지 못하는 상태라
라운드를 더 돌아도 같은 것이 나온다.

**`loop.counter` 와 `loop.on_exceed` 는 이제 코드가 실제로 읽는다** (M36).
`on_exceed` 의 어휘는 `escalate` 하나이고, 어휘 밖 값은 `lint-phases` 와 런타임이
둘 다 거부한다 — **없는 동작을 어휘로 예고하지 않는다.** `converge.on_exceed` 는
`loop.on_exceed` 와 같아야 한다. 코드가 읽는 것은 `loop` 쪽이라 갈리면 `converge`
가 조용히 무시된다.

**`max_by_profile` 의 `small: 2` 는 00 의 예측으로 01 부터 산다** (ADR-H044).
00 이 요청 원문의 구조 신호로 `small` 을 예측하면 이 상한이 처음부터 적용되고,
03 이 계약을 세어 `normal` 로 올리면 `triage_miss` 가 gap 으로 남는다. 사람이
`init --profile` 을 줬으면 그 값이 이기고 재판정에 밀리지 않는다.

**`review.unless` 가 `docs` 레인에서 리뷰어를 0명으로 만든다.** 문서만 바뀌는
런에서 플랜 리뷰어 둘은 관측이 아니라 고정비다 — 기계 검사(인용 · 커버리지 ·
드리프트)가 이 페이즈의 전부이고 플랜 제출이 통과하면 1라운드에 닫힌다. 그
사실이 `profile.applied` 에 `01:reviewers=0` 으로 남아, 예측이 빗나가면 gap
이름이 된다.

**02 가 Critical 로 되돌리면 라운드 예산을 새로 지급받는다.** 리셋이 아니라
지급이라 `used` 는 그대로이고, 지급 사실이 `counters.round.grants` 에 남는다.
바뀐 설계는 새 설계이고 한 라운드로 수렴할 이유가 없다 (M32). 왕복 자체가
`02-cross-verify.md` 의 `loop.max`(현재 1)에 묶여 있어 지급도 런당 한 번뿐이다 —
**그 상한은 이제 코드가 그 선언에서 읽는다** (M36).
