---
{
  "id": "01-plan",
  "index": 1,
  "requires": [
    {"kind": "file", "path": "${run.dir}/00_original_request.md", "min_bytes": 1,
     "sha256_pointer": "request.sha256"},
    {"kind": "adapter_stage", "steps": ["compile", "lint", "check", "scoped", "full"],
     "mode": "warn"}
  ],
  "produces": [
    {"key": "plan", "path": "${run.dir}/01_plan.md", "kind": "markdown", "min_bytes": 200}
  ],
  "review": {
    "unless": "state.profile.name == \"docs\"",
    "reviewers": [
      {"code": "plan", "agent": "plan-reviewer"}
    ]
  },
  "converge": {
    "blocking_severities": ["critical"],
    "focus_round_2": "요청의 요구 중 플랜이 가리키지 않은 것 · 범위 밖 항목 · 인수 조건의 검증 가능성"
  },
  "submit_checks": [
    {"id": "reviewer_not_main", "on_fail": 8},
    {"id": "source_quote_substring", "on_fail": 8},
    {"id": "raw_json_severity_match", "on_fail": 8},
    {"id": "false_positive_evidence", "on_fail": 8}
  ],
  "gate": {"runner": "none"},
  "loop": {"counter": "round", "max_by_profile": {"fix": 1, "normal": 2},
           "on_exceed": "escalate"},
  "allow": {"agents": []},
  "on_success": "03-implement"
}
---

## 목적

원본 요청을 **빠짐없이 덮는 플랜**을 만들고, 다른 눈이 그것을 코드에 대고 검토한다.

요청은 `init` 이 바이트 그대로 동결했고 sha256 이 박혀 있으므로, 이후 어느
페이즈도 "요청이 원래 이랬다"를 새로 지어낼 수 없다. 이 페이즈가 하는 일은 그
요청을 **03 이 계약으로 옮길 수 있는 플랜**으로 만드는 것이다. 플랜의 내용을
기계는 보지 않는다 — 보는 것은 **plan-reviewer** 이고, 리뷰어는 플랜이 가리키는
리포 파일을 열어 근거를 확인한다(02 교차검증이 하던 "코드로 반박" 이 여기로
옮겨 왔다).

산출물은 **`01_plan.md` 하나뿐이다.** 형식은 자유다.

## 진입 조건

- 동결된 요청 원문이 있고 sha256 이 상태의 것과 일치한다
- 어댑터가 선언한 스테이지들이 실재한다 (여기서는 **경고만** 한다 — 뒤 페이즈가
  파일을 고치므로 사전 검사는 예측이지 보장이 아니다. 04 진입 시 다시 강제한다)

## 절차

1. **요청을 읽는다.** 요약하지 말고 그대로 읽는다.
2. **플랜을 쓴다.** 요청의 요구 하나하나가 플랜의 어느 절에 있는지 사람이 찾을 수
   있게 절을 나눈다 — 범위 밖으로 두는 것도 적는다. 리뷰어가 커버리지를 이것으로
   본다. 200바이트 미만이면 제출이 거부된다.
3. **plan-reviewer** 를 돌린다 (ADR-H045). 리뷰어는 요청 원문 · 플랜 · **플랜이
   가리키는 리포 파일(읽기만)** 을 본다. **2라운드부터는 열린 Critical 이 있을
   때만** 다시 온다 — 봉투의 `planned` 가 누구인지 말한다.
4. **라운드를 강제하는 것은 `converge.blocking_severities`(지금은 Critical)뿐이다.**
   Major·Minor 는 기록되고 보고서로 가되 다음 라운드를 열지 않는다. 열린 Critical
   이 0 이면 그 회차에서 닫힌다. 단조성 검사는 01 에 없다 — 다음 회차에 그 Critical
   이 안 나오면 닫힌 것이고, 남았으면 리뷰어가 다시 낸다.
5. 지적을 반영할 때 **플랜을 통째로 다시 쓰지 않는다.** 부분 편집으로 고친다 —
   전문이 라운드마다 다시 쌓이면 접두부가 라운드 수만큼 곱해진다.
6. **Critical 이 틀렸다고 보면 플랜을 억지로 맞추지 말고 코드 근거로 기각한다.**
   리뷰 JSON 을 `record` 하기 **전에** 최상위에 `false_positive` 를 단다 (아래
   제출 형식). 실행기는 `id` 가 이 회차 findings 에 있는지 · `reason` 이 있는지 ·
   `evidence` 의 경로가 리포에 실재하는지만 본다 — 사유의 진위는 사람이 08 에서
   읽는다. 기각된 지적은 차단 계수에서 빠지되 `findings` · 원문 · 상태에 그대로
   남는다.

## 제출 형식

리뷰어는 회차마다 **두 파일**을 낸다 — 출력 원문 `.raw.md` 와 구조화 `.json`.

```json
{"reviewer":"plan","round":1,
 "findings":[{"id":"F-1","severity":"critical|major|minor","category":"…",
              "title":"…","quote":"raw 원문의 부분문자열","evidence":"…",
              "suggestion":"…"}],
 "need_more_context":[]}
```

`reviewer` 가 `main` 이면 거부된다. 작성자가 자기 글을 리뷰한 것은 독립 관측이 아니다.

**메인의 기각 블록** — 리뷰어가 낸 JSON 최상위에 메인이 더한다. 리뷰어는 쓰지 않는다.

```json
{"reviewer":"plan","round":1,"findings":[…],
 "false_positive":[{"id":"F-1","reason":"왜 틀렸는가 — 코드가 이미 그렇게 한다 등",
                    "evidence":"src/lib/match.ts:12"}]}
```

- `id` 는 **이 회차 findings 안의 것**이어야 한다. `reason` 은 비지 않고, `evidence`
  는 리포에 실재하는 경로(`path` 또는 `path:줄`)다. 셋 중 하나라도 빠지면 exit 8
- 기각한 finding 을 `findings` 나 `.raw.md` 에서 **지우지 않는다** — 헤딩 대조가
  전체를 세므로 지우면 exit 8 이고, 무엇을 왜 기각했는지가 기록으로 남아야 한다

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
- **플랜을 전체 재작성하지 마라.** 이유: 라운드마다 전문이 다시 쌓인다
- **리뷰어 지적을 조용히 없애지 마라.** 이유: 틀린 지적은 `false_positive` 로
  근거를 대고 기각한다. 파일에서 지우면 헤딩 대조가 exit 8 을 내고, 근거 없는
  기각도 exit 8 이다
- **`quote` 를 다듬지 마라.** 이유: 부분문자열 검증이 그 다듬기를 위조와 구분하지
  못한다. 원문이 어색해도 그대로 인용한다

## 실패 시

| 무엇 | 어떻게 |
|---|---|
| 플랜이 200바이트 미만이다 | exit 6 — 전이 거부. 플랜을 채워 다시 낸다 |
| `quote` 가 리뷰어 원문에 없다 · 헤딩 수가 findings 수와 다르다 | exit 8 — 리뷰어가 원문 그대로 다시 낸다. 메인이 헤딩을 붙여 맞추지 않는다 |
| `false_positive` 의 `id`·`reason`·`evidence` 중 하나가 없거나 경로가 리포에 없다 | exit 8 — 근거를 채우거나 기각을 거둔다 |
| 라운드 상한 초과 | exit 10 → 에스컬레이션. 미해결 Critical 전문과 3지선다 |

**라운드 상한은 `loop.max_by_profile` 이 레인별로 정한다** (ADR-H041). 수렴 규칙이
"열린 Critical 0건" 하나라 상한은 천장이지 경로가 아니다. `loop.counter` 와
`loop.on_exceed` 는 코드가 실제로 읽는다 (ADR-H025) — `on_exceed` 의 어휘는 `escalate`
하나이고 어휘 밖 값은 `lint-phases` 와 런타임이 둘 다 거부한다. `converge` 에는
읽히는 둘(`blocking_severities`·`focus_round_2`)만 있다 (ADR-H076).

**`review.unless` 가 `docs` 레인에서 리뷰어를 0명으로 만든다.** 문서만 바뀌는
런에서 plan-reviewer 는 관측이 아니라 고정비다 — 플랜 제출이 이 페이즈의 전부이고
1라운드에 닫힌다. 그 사실이 `profile.applied` 에 `01:reviewers=0` 으로 남아,
선언이 빗나가면 gap 이름이 된다.
