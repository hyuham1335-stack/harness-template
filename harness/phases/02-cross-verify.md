---
{
  "id": "02-cross-verify",
  "index": 2,
  "owner": "main",
  "approval": "none",
  "requires": [
    {"kind": "state", "pointer": "phases.01-plan.status", "equals": "passed"},
    {"kind": "file", "path": "${run.dir}/01_plan.md", "min_bytes": 200}
  ],
  "produces": [
    {"key": "verdict", "path": "${run.dir}/02_verdict.json", "kind": "json",
     "schema": "verdict"}
  ],
  "skip_when": "state.cross_verify.mode == \"skipped\"",
  "on_skip": {"status": "skipped", "grade": "PASS_WITH_GAPS",
              "gap": "cross_verify_unavailable"},
  "skip_policy": [
    {"when": "state.profile.name == \"docs\"",
     "status": "skipped", "reason": "docs_profile"},
    {"when": "state.phases.01-plan.converged_at_round == 1",
     "status": "skipped", "reason": "plan_unedited"}
  ],
  "submit_checks": [
    {"id": "reviewer_not_main", "on_fail": 8},
    {"id": "source_quote_substring", "on_fail": 8},
    {"id": "critical_zero", "on_fail": 4}
  ],
  "gate": {"runner": "none"},
  "loop": {"counter": "xverify_return", "max": 1, "on_exceed": "escalate",
           "on_fail_return_to": "01-plan"},
  "allow": {"agents": []},
  "on_success": "03-implement"
}
---

## 목적

확정된 플랜 **전문**에 대한 최종 확인 1회.

01 의 매 라운드에서 이미 교차검증이 걸렸으므로 여기서 새 지적이 나오는 일은
드물다. 그럼에도 이 페이즈를 두는 이유는, 01 의 리뷰가 **회차마다 그때의 플랜**을
봤지 완성된 전문을 본 적이 없기 때문이다. 부분 편집으로 고쳐 온 문서는 부분끼리
모순될 수 있고, 그 모순은 전문을 한 번 읽어야 보인다.

**그래서 01 이 1라운드에 수렴했으면 이 페이즈는 건너뛴다** (`skip_policy` 의
`plan_unedited`, ADR-H042). 편집이 없었으니 1라운드의 교차검증기가 본 텍스트가
곧 전문이고, 같은 관측기를 같은 텍스트에 한 번 더 부르는 것이다. 이 생략은
**등급을 내리지 않는다** — 관측이 없었던 것(`skip_when`, 관측기 부재)과 다르다.
사유가 상태와 보고서에 남는다.

**`docs` 레인도 건너뛴다 — 그러나 사유가 다르다** (`docs_profile`, ADR-H044).
docs 레인의 01 은 리뷰어가 0명이라 1라운드에 닫히므로 `plan_unedited` 조건도
참이 되지만, 그 사유("교차검증기가 본 텍스트가 곧 전문")는 거짓이다 — 아무
교차검증기도 보지 않았다. `skip_policy` 는 리스트이고 **첫 일치가 이긴다.**
`docs_profile` 이 앞에 있는 이유가 그것이다. 이 생략도 등급을 내리지 않는다 —
정책 스킵이고, 예측이 빗나가면 `triage_miss` 가 그것을 gap 으로 만든다.

## 진입 조건

- 01 이 `passed` 다
- `01_plan.md` 가 있다

교차검증기가 **primary 도 fallback 도 불가**하면 이 페이즈를 스킵한다. 단
스킵은 통과가 아니다 — 등급이 `PASS_WITH_GAPS` 로 내려가고 보고서에
"교차검증 없음"이 박힌다.

## 절차

1. 확정된 `01_plan.md` 전문을 교차검증기에 넘긴다.
2. 판정을 `02_verdict.json` 으로 받는다.
3. Critical 이 남아 있으면 `loop.on_fail_return_to` 가 가리키는 페이즈로
   되돌린다 — 왕복 횟수는 `loop.max`(현재 1)다. **셋 다 코드가 이 선언에서
   읽는다** (M36): 되돌아갈 페이즈도 상한도 코드에 박혀 있지 않고, 선언이
   없으면 기본값으로 낙하하는 대신 exit 2 다. `lint-phases` 가 되돌아갈
   페이즈는 자기보다 **앞**이어야 함을 검사한다.
4. 채택 여부를 `adopted[]` 에 남긴다. **06 이 PR 본문의 "기술적 고려사항"
   절로 이것을 쓴다** — `reject` 도 함께 나간다. `adopted[]` 는 "채택된 것" 이
   아니라 **판정**이고, 거부를 감추면 본문이 거짓말을 한다. 소급 생성할 수
   없는 종류의 기록이기도 하다.

## 제출 형식

```json
{"reviewer":"xv","mode":"primary|fallback","primary_error":null,"status":"ok",
 "findings":[{"id":"F-1","severity":"critical|major|minor","category":"…",
              "title":"…","quote":"…","evidence":"…","suggestion":"…"}],
 "adopted":[{"id":"F-1","verdict":"accept|reject","reason":"…"}],
 "resolved_from_previous":[]}
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


재제기(`reraised_from_previous`, finding 안에 적는다)와 해소의 규칙은
`01-plan.md` 와 같다 — 같은 판정기를 지난다.

`mode` 가 `fallback` 이면 그 사실이 상태와 보고서에 남는다. 폴백은 독립 관측이
약해진 것이고, 약해졌다는 사실이 드러나야 그 뒤의 판단이 정직해진다.

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

- **플랜을 여기서 고치지 마라.** 이유: 이 페이즈는 판정만 한다. 고치는 것은 01 이고,
  되돌아가지 않고 여기서 고치면 01 의 커버리지 검사를 우회하게 된다
- **`reject` 에 이유를 비우지 마라.** 이유: 근거 없는 기각은 다음 런에서 같은
  지적을 다시 받는다
- **Critical 을 minor 로 내려 통과시키지 마라.** 이유: 심각도는 리뷰어의 것이고,
  옮겨 적는 쪽이 바꾸면 `.raw.md` 대조에서 잡힌다

## 실패 시

| 무엇 | 어떻게 |
|---|---|
| Critical 이 남았다 | exit 4 — 01 로 1회 되돌린다 |
| 두 번째 되돌림 | exit 7 → 에스컬레이션. 왕복 상한이 1이다 |
| 교차검증기 둘 다 불가 | 스킵 + 등급 `PASS_WITH_GAPS`. **조용히 통과가 아니다** |
