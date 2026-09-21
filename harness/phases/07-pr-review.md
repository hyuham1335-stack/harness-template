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
    {"key": "pr_review", "path": "${run.dir}/07_pr_review.json", "kind": "json"}
  ],
  "gate": {"runner": "none"},
  "submit_checks": [],
  "allow": {"agents": []},
  "on_success": "08-report"
}
---

## 목적

**05 는 우리가 우리 코드를 봤다. 07 은 다른 눈이 한 번 더 본다.**

이 페이즈가 재는 것은 하나다 — **05 가 놓친 것이 얼마나 되는가.** `/code-review`
를 **항상 1회** 부르고, 그 결과 중 05 가 이미 낸 것과 같은 결함은 키로 가리켜
접는다. 남는 Critical/Major 가 05 가 놓친 것(escaped)이고, 그 수가 05 리뷰
정책의 근거가 된다. 생략 조건도 effort 선택도 없다 — 조건부로 돌리면 표본이
빠진 런과 깨끗한 런을 가를 수 없다.

## 진입 조건

- 06 이 `passed` 이고 `state.pr.pushed` 가 참이다. 승인은 **06 에서 상속한다**
  (`inherited:06`) — 07 이 따로 받지 않는다
- **PR 상태를 먼저 본다.** 닫혔거나 머지됐으면 리뷰도 수리도 하지 않고 `record`
  로 정상 종료한다 — 등급은 `pr_closed`/`pr_merged` 로 내려간다 (§E8)

## 절차

```
1. PR 상태 확인         너 · forge 도구   닫힘·머지면 3번으로 (findings 빈 배열)
2. /code-review         모델 · 1회         effort 는 스킬 기본값 그대로
3. 07_pr_review.json    너                 아래 형식. 05 와 같은 결함엔 finding_key
4. record --phase 07    제출               dup_05 대조 · escaped 계수 · 등급
```

```bash
python scripts/pipeline/cli.py record --phase 07 --file {run_dir}/07_pr_review.json --run-id {run_id}
```

수리는 여기서 하지 않는다. Critical/Major 가 새로 나오면 기록과 등급
(`pr_review_open`)으로 드러내고 **사람이 정한다** — 07 에 수리 루프를 두면 PR
리뷰가 두 번째 05 가 된다.

## 제출 형식

`{run_dir}/07_pr_review.json` 하나.

```json
{"code_review": "done|skipped",
 "skip_reason": "skipped 일 때만 · 비면 exit 8",
 "findings": [
   {"severity": "critical|major|minor", "title": "…", "path": "src/…",
    "finding_key": "05 가 낸 같은 결함일 때만 · 봉투의 「05 가 낸 지적」 절의 키"}]}
```

- `code_review` 는 `done` 이 기본이다. `skipped` 는 사유가 있어야 받고, 받아도
  gap `pr_review_skipped` 로 등급이 내려간다 — **스킵은 통과가 아니다**
- **`finding_key` 는 봉투의 목록에서만 고른다.** 목록에 없는 키는 exit 8 이다.
  같은 결함에 키를 안 달면 새 것으로 세어 05 를 실제보다 나쁘게 적고, 다른
  결함에 키를 달면 놓친 것을 숨긴다 — 안 다는 것이 기본이고 다는 것이 주장이다
- `severity` 는 `critical` · `major` · `minor` 다. Minor 는 기록만 된다

## 금지

- **머지하지 마라.** 이유: 명세가 머지 자동화를 범위 밖으로 둔다
- **닫히거나 머지된 PR 을 손대지 마라.** 이유: 이미 끝난 것을 수리하는 것이고,
  머지된 코드에 코멘트를 다는 것은 소음이다 (§E8)
- **`finding_key` 를 지어내지 마라.** 이유: `dup_05` 는 네 선언이고, 기계는 키가
  목록에 있는지만 본다. 지어낸 키는 exit 8 이지만 잘못 단 키는 잡지 못한다
- **여기서 수리하지 마라.** 이유: 07 의 산출은 계수이고, 수리는 사람의 판단이다

## 실패 시

| 무엇 | 분류 | 어떻게 |
|---|---|---|
| PR 이 닫힘 · 머지됨 | — | 리뷰 없이 **정상 종료** + gap `pr_closed`/`pr_merged` (§E8) |
| `/code-review` 를 부르지 못함 | infra | `code_review: "skipped"` + 사유 → gap `pr_review_skipped`. 도구 문제를 「지적 0건」으로 적지 않는다 |
| 새 Critical/Major (dup_05=false) | 판단 | 기록 + gap `pr_review_open`. **수리는 사람이 정한다** — 07 은 멈추지 않는다 |
| `finding_key` 가 목록 밖 | 제출물 | **exit 8** — 봉투의 목록에서 다시 고른다 |
| `skipped` 인데 사유 없음 | 제출물 | **exit 8** |
