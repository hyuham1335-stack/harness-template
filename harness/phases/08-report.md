---
{
  "id": "08-report",
  "index": 8,
  "owner": "main",
  "approval": "none",
  "requires": [
    {"kind": "state", "pointer": "phases.07-pr-review.status",
     "equals": "passed"},
    {"kind": "file", "path": "${run.dir}/08_report_data.json", "min_bytes": 2}
  ],
  "produces": [
    {"key": "report", "path": "docs/harness/pipeline/runs/${run.id}.md",
     "kind": "markdown"}
  ],
  "gate": {"runner": "none"},
  "allow": {"agents": []},
  "on_success": "done"
}
---

## 목적

**보고서는 런이 스스로에 대해 말하는 유일한 자리다.** 그리고 이 페이즈가
막는 실패는 하나다 — **재지 못한 것이 조용히 통과하는 것.**

`## 캘리브레이션 상태` 가 필수 섹션인 이유가 그것이다. 지금 이 리포의
`calibration.json` 은 `partial: true` 이고 어댑터는 `verified: false` 다.
보고서가 그것을 적지 않으면 런은 초록불로 끝나고, 다음 런이 같은 미검증
값을 물려받는다.

## 진입 조건

- 07 이 `passed` 이고 `08_report_data.json` 이 있다
- **`grade == INCOMPLETE` 면 08 을 돌리지 않는다** — `ESCALATION.md` 가
  보고서를 겸한다 (§E12)
- `promote --flush` 가 먼저 돌았다. `staged` 가 남아 있으면 **exit 6**
- **PR 상태를 1회 재확인**했다 — 늦게 도착한 변경 요청이면 등급 강등 (§E8)
- `## 리뷰` 표의 「00 트리아지」·「트리아지 적용 양보」와 「프로파일」의 예측·
  빗나감, `## 비용과 시간` 의 「지시된 모델 등급」은 실행기가 조립한다 — **지시된
  등급이지 실측이 아니다** (ADR-H044). 재지 않은 것을 숫자로 적지 마라

## 절차

```
1. promote --flush         정적        잔여 승격을 강제 종결
2. PR 상태 재확인          너          늦게 온 변경 요청 → 등급 강등
3. 08_report_data.json     너          집계값 + 상위 N개 제목만 (<= 20KB)
4. report --out            정적        결정론 표 조립 + 필수 섹션 검사
                                       + PILOT-LOG 런 절 · ledger/deferred.md 재생성
5. 커밋 → pr --run-id      너          런 기록을 기능 PR 에 싣는다 (ADR-H052)
```

### 3번 — 08 은 diff 도 코드도 읽지 않는다

입력은 `08_report_data.json` **하나뿐이다.** 전문은 파일 경로로만 가리킨다.
이 제약이 08 의 비용을 런 크기와 무관하게 만든다.

**서술은 네가 쓰고, 표는 실행기가 조립한다.**

| 실행기가 조립한다 (결정론) | 네가 쓴다 (서술) |
|---|---|
| 완료 등급 + **건너뛴 비차단 게이트 나열** | 문제 → 원인 → 해결 → 결과 → 배운 점 |
| 페이즈별 소요 — **벽시계 · 순 작업 · 에스컬레이션 대기 · 사람 판단 대기 · 형식 반려 수** — 재시도, **모델 호출 수(근사)** · 지시된 등급과 자진신고 · **비용(있으면, 없으면 `미계측`)** | 계약이 어디서 부족했는가 |
| `review05.status` · `escaped_05` · `dropped_by_enforcement` · `need_more_context` (`dropped_by_enforcement`·`need_more_context`·`truncated` 는 **런 누적**이다 — 마지막 라운드가 아니다) | 05 리뷰 범위가 적절했는가 |
| **승격 규칙 목록(원장에서 자동 추출)** · 열린 `deferred` 의 경로별 이월 표(`ledger/deferred.md`) · `PILOT-LOG.md` 런 절 | 다음 런에서 바꿀 것 |
| 에스컬레이션 이력 · `audit_run` 여부 · **캘리브레이션 상태** | |

승격 목록이 원장에서 자동으로 나오는 것이 요점이다 — **네가 빠뜨릴 수 없다.**

### 4번

```bash
python scripts/pipeline/cli.py report --out docs/harness/pipeline/runs/{run_id}.md --run-id {run_id}
```

## 제출 형식

`{run_dir}/08_report_data.json` 하나. **20KB 이하다.**

```json
{"narrative": {"문제": "…", "원인": "…", "해결": "…", "결과": "…",
               "배운 점": "…"},
 "contract_gaps": "계약이 어디서 부족했는가",
 "review_scope": "05 리뷰 범위가 적절했는가",
 "next_run": "다음 런에서 바꿀 것"}
```

- **재지 않은 것을 숫자로 적지 마라.** 모르면 `미측정` 이라고 쓴다.
  이 리포의 규율이고, 보고서가 그것을 깨면 다음 런이 지어낸 값을 물려받는다
- **`배운 점` 과 `next_run` 은 80자 이상이다.** 미달이면 보고서는 쓰되 **exit 8**
  로 되묻고 런을 닫지 않는다 — 등급은 건드리지 않는다 (ADR-H052). 표가 말하지
  못하는 「왜」가 다음 런의 입력이다
- 서술이 비어도 보고서는 나온다 — **필수 섹션이 빠져도 파이프라인을
  실패시키지 않는다.** 원장에 기록만 한다

## 금지

- **diff 나 소스를 읽지 마라.** 이유: 08 의 입력은 파일 하나이고, 그 제약이
  보고서 비용을 런 크기와 무관하게 만든다
- **숫자를 지어내지 마라.** 이유: 보고서는 다음 런의 입력이다. 추정치가
  실측처럼 적히면 그 값이 정책이 된다
- **런 기록을 커밋하지 않은 채 끝내지 마라.** `docs/harness/pipeline/runs/{run_id}.md`
  · `PILOT-LOG.md` 의 런 절 · `ledger/deferred.md` 는 **기능 PR 에 실린다** —
  커밋하고 `pr --run-id` 를 다시 돌려 PR 을 갱신한다. 닫힌 런의 그 갱신에 런
  기록이 diff 에 없으면 gap `run_record_missing` 이다 (ADR-H052). 승격은 여전히
  별도 브랜치다
- **`INCOMPLETE` 인 런에서 08 을 돌리지 마라.** 이유: 에스컬레이션으로 멈춘
  런은 `ESCALATION.md` 가 보고서다. 그 위에 성공한 것 같은 문서를 얹지 않는다

## 실패 시

| 무엇 | 분류 | 어떻게 |
|---|---|---|
| `grade == INCOMPLETE` | — | **08 을 돌리지 않는다.** `ESCALATION.md` 가 보고서를 겸한다 (§E12) |
| 원장 누락 · 손상 | — | **"미측정" 으로 표기하고 산출한다.** 보고서는 파이프라인을 실패시키지 않는다 |
| `promotions` 미종결 | 정책 | **exit 6** → `promote --flush` |
| 필수 섹션 누락 | — | 원장에 기록하고 산출한다 |
| `배운 점`·`next_run` 80자 미만 | 정책 | 보고서는 쓰되 **exit 8** — 같은 명령으로 다시 낸다. 등급 X. 닫힌 런의 재작성은 되묻지 않는다 |
| 같은 `run_id` 로 재개해 다시 씀 | — | **덮어쓴다.** 최종본이 맞다 |
| 이미 닫힌 런에 다시 `report` | — | 보고서를 **덮어쓰고 exit 0.** 전이는 한 번뿐이고 `run_closed` 도 하나뿐이다 |
| 07 이 아직 `passed` 가 아님 | — | **보고서는 쓰고 런은 닫지 않는다** (exit 0). 전이 조건은 이 파일의 `requires` 그대로다 |

**명세 미규정 하나 — 지어내지 않고 적어 둔다.** `gaps[]` 의 어휘가 열거형으로
정의돼 있지 않다. 코드의 `GAP_REASONS` 는 명세 전체에서 **모은 것**이지 명세가
한자리에 준 목록이 아니다.

**~~② exit 11 을 누가 내는가~~ — 정해졌다 (M24, 2026-09-04).** `on_success` 가
`done` 인 페이즈를 통과시키는 커맨드가 낸다. 08 에서 그것은 `report` 이고,
`team-spec.md` §1 의 페이즈 표가 처음부터 08 의 성공 시 다음을 `done` 이라
적고 있었다 — 어긋난 것은 실행기였다. **페이즈는 자기 동사로 닫힌다**: 04 는
`gate`, 08 은 `report`, 나머지는 제출이 곧 그 페이즈의 일이라 `record` 다.
그래서 `record --phase 08` 은 이제 "미구현" 이 아니라 `report` 를 가리킨다.
