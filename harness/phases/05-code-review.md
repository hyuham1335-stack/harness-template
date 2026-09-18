---
{
  "id": "05-code-review",
  "index": 5,
  "owner": "main",
  "approval": "none",
  "requires": [
    {"kind": "state", "pointer": "phases.04-gate.status", "equals": "passed"},
    {"kind": "file", "path": "${run.contract_file}", "min_bytes": 200,
     "must_contain": "${config.contract.sections.units}",
     "unless": "state.contract.mode == \"no_contract\""}
  ],
  "produces": [
    {"key": "trace", "path": "${run.dir}/05_trace.json", "kind": "json",
     "schema": "trace"},
    {"key": "review", "path": "${run.dir}/05_review.json", "kind": "json",
     "schema": "review"},
    {"key": "promo_staged", "path": "${run.dir}/05_promo_staged.json",
     "kind": "json", "schema": "promotions"}
  ],
  "review": {
    "routing": "config.reviewers",
    "cap": "config.review.profile_caps",
    "merge_below": "config.review.merge_below_diff_lines"
  },
  "gate": {
    "runner": "adapter", "fail_fast": true, "rerun_failed_once": true,
    "steps": [
      {"id": "compile", "loop_stage": true},
      {"id": "scoped", "tests_from": "contract", "loop_stage": true},
      {"id": "full", "once_after_loop": true, "assert_tests_ran": true}
    ]
  },
  "loop": {
    "counter": "review_repair", "max": 2, "stuck_after_identical": 2,
    "on_exceed": "escalate", "rereview": "delta_single_reviewer",
    "local_repair": {"by": "main", "max_per_run": 3,
                     "criteria": {"failures_max": 2, "files_max": 1,
                                  "lines_max": 20},
                     "accounting": ["resolved_from_previous",
                                    "reraised_from_previous"]}
  },
  "allow": {"agents": "config.roles[].agent"},
  "on_success": "06-pr"
}
---

## 목적

**04 가 "돌아가는가"를 봤다면 05 는 "계약대로인가, 그리고 봐야 할 눈이 봤는가"를
본다.**

이 페이즈가 막는 실패는 하나다 — 리뷰어가 전부 실패해도 findings 는 0건이고,
그 0을 "지적이 없다"로 읽으면 **아무도 보지 않은 코드가 통과한다.** 게이트는
초록불이고 보고서도 성공처럼 보인다. 그래서 "리뷰가 수행됐는가"를 findings
개수와 **분리된 신호**(`review05.status`)로 만든다.

## 진입 조건

- 04 가 `passed` 이고 워크트리 지문이 유효하다
- 계약 파일이 필수 절을 담고 있다 (`no_contract` 모드가 아닌 한)
- `precheck` 가 통과했다 — 예산 · 브랜치 · base · 인프라

## 절차

**비용 오름차순이고 첫 실패에서 멈춘다.** 1·2번이 무료다 — 뒤에서 되돌릴 일을
여기서 먼저 잡는다.

```
1. precheck --scope pr      정적 · 무료   예산 · 브랜치 · divergence · 인프라
2. contract-trace           정적 · 무료   계약 ↔ 코드 대조 5종
3. Critical 있으면 선수리 + gate --phase 05 --stage loop             → 2로 복귀
4. 리뷰:  diff ≤ merge_below_diff_lines  → 단일 에이전트 · 다중 체크리스트
          그보다 크면                      → 병렬 fan-out (profile 상한까지)
          인라인 상한 초과                  → 경로 전달 폴백
5. 병합 → 수리 → 델타 재리뷰 1명 (**봉투가 그 한 명을 이름으로 부른다**)
6. 코드 확정 → 전체 회귀 1회 → 원장 staged
```

### 1·2번 — 모델을 부르지 않는다

```bash
python scripts/pipeline/cli.py precheck --scope pr --run-id {run_id}
python scripts/pipeline/cli.py contract-trace --run-id {run_id}
```

- `precheck` **exit 9** 는 사람의 판단이다. **자동으로 쪼개거나 리베이스하지
  마라** — 범위와 히스토리는 사람의 것이다. **exit 10** 은 인프라이고 카운터를
  소모하지 않는다.
- `contract-trace` **exit 8** 은 "리뷰어를 부르기 전에 고쳐라"다. 고친 뒤
  `gate --phase 05 --stage loop` 로 재게이트하고 다시 친다. **`loop` 는 이
  페이즈가 선언한 루프 구간 전부**(compile → scoped)다 — scoped 만 돌리면
  테스트 러너가 타입체크 없이 통과시킨 타입 에러가 PR 까지 흘러간다
  (ADR-H046). 수리 뒤 재게이트도 같은 명령이다.
- `entrypoint_resolver` 가 없으면 그 검사만 빠지고 `skipped` 에 남는다.
  **스킵을 통과로 적지 마라.**

### 4번 — 리뷰어 라우팅은 결정론이다

누구를 부를지 **네가 정하지 않는다.** 봉투가 준 목록을 그대로 쓴다. 네가 정하면
같은 diff 가 런마다 다른 리뷰를 받고, 그러면 `escaped_05` 를 세는 것이 의미를
잃는다.

**`gen`(일반 정합성)은 소스 변경이 있으면 항상 켜진다** — glob 이 아니라
`roles[].owns` 로 켜지므로 프로젝트 레이아웃과 무관하다 (ADR-H043). "구현이
계약대로 동작하는가" 를 보는 관점이 여기 있어서 07 이 깨끗한 런의 내장 리뷰를
생략한다. gen 이 빠진 채 05 가 `ok` 가 되는 경로는 없다 — 계획된 리뷰어가
실패하면 `review05.status` 가 `degraded` 이고 07 이 `medium` 으로 메운다.

각 리뷰어에게 주는 것:

- **인라인 diff · 계약 · `05_trace.json`**. 그게 전부다
- 프롬프트 첫 줄은 **스킬 파일을 읽으라는 지시**다. 스킬 본문을 복사해 싣지 마라 —
  리뷰어 수만큼 그 고정비가 곱해진다
- 봉투가 준 **"검토 제외" 목록**을 그대로 전달한다. 이미 기계가 막는 것을 다시
  지적하면 원장이 같은 것을 두 번 센다

### 6번 — 원장은 `staged` 까지다

05 는 후보만 만든다. **실제 승격 쓰기는 07 이 런당 한 번 한다.** dedup 이
로직이 아니라 시점으로 성립하고, PR diff 에 규칙 문서 변경이 섞이지 않는다.

## 역할 프롬프트 템플릿

리뷰어에게 보내는 형태다. **역할(작성자)에게 보내는 것이 아니다** — 수리 지시는
04 의 템플릿을 그대로 쓴다.

```
## 리뷰 요청 — {reviewer.code}

`.claude/skills/{reviewer.skill}/SKILL.md` 를 먼저 읽어라. 관점과 제출 형식이
거기 있다.

## 변경 (인라인 diff)
{diff}

## 계약
{계약 전문}

## 계약 대조 결과
{05_trace.json 의 findings 요약 — 기계가 이미 찾은 것이다. 다시 지적하지 마라}

## 검토 제외
아래 category 는 이미 기계가 막고 있다. 지적하지 마라:
{excluded_categories}

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
     "rule_slug":"어휘를 선언한 category 에서만 · 봉투의 「규칙 슬러그」 절 참고",
     "quote":"raw 원문의 부분문자열","evidence":"…","suggestion":"…"}]},
 "resolved_from_previous":[{"id":"F-2","resolved_by":"…"}],
 "need_more_context":[],
 "model_used":"선택 — 실제로 쓴 모델 id. 자진신고이고 실측이 아니다"}
```

- **`by_checklist` 는 0건인 체크리스트도 명시한다.** 빈 배열로 적는다. 안 적으면
  "안 봤다"와 "보고 아무것도 없었다"가 같은 침묵이 된다
- `reviewer` 가 작성자 역할이면 거부된다. 자기 코드를 리뷰한 것은 독립 관측이 아니다
- **`category` 는 원장 어휘(`taxonomy.json`) 안이어야 한다.** 밖이면 그 제출이
  exit 8 로 되돌아오고 **어느 finding 이 무엇을 썼는지와 쓸 수 있는 코드 전부**를
  봉투가 보여 준다. 어휘는 아래 「원장 어휘」 절에 있다 — 새 코드가 필요하면
  지어내지 말고 `CONTRACT_DEFECT` 나 `OTHER` 로 내고 그 사실을 evidence 에 적는다
- **어휘를 선언한 `category` 는 `rule_slug` 가 필수다** — 승격의 축이기 때문이다.
  어느 카테고리가 그런지와 각 슬러그의 뜻은 봉투의 「규칙 슬러그」 절이 말한다.
  **슬러그를 지어내지 마라** — 맞는 것이 없으면 `category: OTHER` 로 내고 무엇이
  없는지를 evidence 에 적는다. 승격이 배우는 것은 "이 자리를 고쳐라" 가 아니라
  "이 규칙이 반복된다" 이고, 매번 다른 문장은 규칙이 아니다
- `config.reviewers` 에 없는 `code` 도 거부된다 — 라우팅이 부르지 않은 리뷰어의
  제출은 받지 않는다. **`next` 가 확정한 `planned` 밖이면 exit 8 이다**(델타
  라운드에서는 지목된 한 명 밖이면 그렇다). 분모를 제출자에서 유도하면 누가
  리뷰했는지가 리뷰한 사람의 주장이 된다
- **`merged` 는 실행 방식이지 제출 형태가 아니다** (M37). 모드가 `merged` 여도
  한 에이전트가 관점을 순차로 적용할 뿐, **제출은 라우팅된 코드 수만큼 그대로
  갈라진다.** `--reviewer merged` 는 `planned` 밖이라 exit 8 이다. 봉투가 그
  라우팅 코드로 명령 줄을 직접 찍어 준다
- **규약 위반 제출은 1회 되돌린다. 2회째는 그 리뷰어를 `failed` 로 확정하고
  흐름을 잇는다** — 계속 튕기면 그 리뷰어가 영원히 슬롯에 못 들어가고, 결손이
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

**`resolved_from_previous` 의 `id` 는 같은 리뷰어의 지적만 닫는다.** 리뷰어가
다섯이면 전부 `F-1` 부터 매기므로 id 를 전역 대조하면 한 줄이 서로 다른 지적
여럿을 동시에 닫는다. 남의 것은 `key` 로 닫는다. **05 는 이 위험이 리뷰어 수만큼
커진다.**

### `.raw.md` 의 형태 — 기계가 이것을 검사한다

원문은 **지적 하나에 심각도 헤딩 하나**다. 헤딩 텍스트는 `critical` · `major` ·
`minor` 셋 중 하나이고 `#` 개수는 자유다.

```markdown
# 리뷰 — sec

## critical
`route.ts` 의 새 경로가 인가 캐치올 밖에 있다.

## minor
오류 코드 이름이 규약과 다르다.
```

- **헤딩 개수와 findings 개수가 같아야 한다.** 다르면 제출이 exit 8 로 튕긴다
- 각 `quote` 는 이 원문의 **부분문자열**이어야 한다. 공백만 정규화해 대조한다
- 지적이 0건이면 헤딩도 0개다. 원문은 그래도 낸다

**메인이 사후에 헤딩을 붙여 맞추면 이 손잡이가 사라진다** — 리뷰어가 처음부터
이 형태로 써야 한다. 05 는 리뷰어가 여럿이라 그 비용이 인원수만큼 곱해진다.

### 병합 규칙

- **2인 이상이 지적한 항목은 severity 가 한 단계 오른다.** 독립 관측의 합치는
  한 관측보다 강한 증거다. 같은 리뷰어가 두 번 낸 것은 합치가 아니다
- findings 가 `config.review.findings_max` 를 넘으면 Critical/Major 만 남기고
  절단하되 **`truncated: true` 로 드러낸다**

## 금지

- **누구를 부를지 다시 정하지 마라.** 이유: 라우팅은 `when` glob 이 정하는
  결정론이다. 모델이 정하면 같은 diff 가 런마다 다른 리뷰를 받는다
- **리뷰어에게 리포 탐색을 허용하지 마라.** 이유: 탐색한 맥락으로 지적하면
  요청에 없는 요구가 끌려 들어온다. 부족하면 `need_more_context` 에 적게 한다
- **계약을 고치지 마라.** 이유: 계약은 메인 단독 소유이고, 리뷰어가 계약 결함을
  발견하면 그것은 수리가 아니라 **에스컬레이션**이다(`CONTRACT_DEFECT`)
- **스킬 본문을 프롬프트에 복사하지 마라.** 이유: 리뷰어 수만큼 고정비가 곱해진다.
  첫 줄에서 파일을 읽으라고 지시한다
- **Minor 를 고치려 들지 마라.** 이유: 수리 대상은 Critical/Major 뿐이다.
  Minor 는 원장에 쌓이고 보고서로 간다. **다만 다음 회차 제출에서 회계는 한다**
  (M38) — 단조성 검사는 심각도를 가리지 않고 열린 지적 전부를 요구하고, 하나라도
  빠지면 "조용히 증발했다"로 exit 8 이다. **수리 면제이지 회계 면제가 아니다.**
  회계할 목록은 수리 봉투가 직접 적어 준다
- **소스를 고친 뒤 재게이트 없이 넘어가지 마라.** 이유: 지문이 어긋나 06 이
  자동으로 막는다. 막히는 것이 정상 동작이다
- **여기서 push 하거나 PR 을 만들지 마라.** 이유: 그것은 06 의 일이고 06 은 아직
  구현되지 않았다

## 실패 시

| 무엇 | 분류 | 어떻게 |
|---|---|---|
| `precheck` 예산 초과 · base behind | 정책 | **exit 9 즉시 사용자 판단.** 자동 분할·자동 리베이스 금지 |
| `infra_preflight` 프로브 실패 (`on_missing: fail`) | infra | exit 10 · **카운터 미소모** · 즉시 에스컬레이션 |
| 〃 (`on_missing: warn`) | — | exit 0 + `infra_skipped:{name}` gap · 등급 `PASS_WITH_GAPS`. **면제는 통과가 아니다** — 키 없이도 목업으로 도는 경로가 있을 때만 쓰고, 그 이유를 어댑터의 `why` 에 적는다 (M44) |
| 계약 부재 | — | `no_contract` 모드로 진행. `skipped_no_contract` 로 기록 |
| 계약이 재개 사이에 변경됨 | 정책 | exit 3 + "03 부터 재실행" |
| 리뷰어 호출 실패·타임아웃 | infra | 그 리뷰어만 1회 재시도 → 실패 시 `--failed --reason` 으로 신고. **비차단 스킵** + `degraded` |
| **리뷰어 전원 실패** | 정책 | `review05.status = failed` → 등급 `PASS_WITH_GAPS` |
| **계획된 리뷰어가 0개** | 정책 | 같은 처리. 아무도 안 부른 것은 통과가 아니라 미수행이다. **`next` 가 그 자리에서 상태에 확정한다** — 제출이 0건이면 판정이 아예 안 불리므로 여기서 안 쓰면 아무도 안 쓴다 |
| 리뷰어가 JSON 대신 산문 | 제출물 | exit 8 · 재제출 1회 → 2회 실패 시 **슬롯에 `failed` 로 확정**하고 흐름을 잇는다 |
| 실패 신고인데 제출 파일이 있다 | 제출물 | exit 8. 자진 신고 중 기계로 확인 가능한 것은 기계로 확인한다 |
| quote 위조 · 단조성 위반 | 제출물 | exit 8 · 같은 회차 재제출(카운터 소모) |
| `need_more_context` 계속 참 | 판단 | 1회에 한해 파일 목록 명시 추가. 반복되면 라우팅 결함으로 보고 |
| 두 리뷰어 지적이 상반 | 판단 | **계약 우선** → `rules_dir` 우선. 판정을 원장에 |
| `CONTRACT_DEFECT` 발견 | 정책 | 수리하지 않는다 → **에스컬레이션** |
| diff 가 인라인 상한 초과 | — | **기계가 정한다** — `next` 가 `review.inline_max` 로 재고 봉투가 "경로로 전달하라" 고 말한다. 네 재량이 아니다 (ADR-H042). 폴백 사실이 상태에 남는다 |
| `review_repair` 초과 · 동일 sig 2회 | 정책 | 에스컬레이션. **계약 결함을 먼저 의심**하라고 패킷에 적는다 |
| 제출이 내용은 그대로인데 회계 필드만 틀려 exit 8 | 기계 | `format_reject` 이벤트로 센다. `loop.local_repair.accounting` 의 필드는 메인이 고쳐 재제출해도 된다 — quote·헤딩 수·severity 는 여전히 금지 (ADR-H052) |
| 이전 라운드 지적이 더 높은 심각도로 재상정 | 기계 | `review_repair` 를 **1 지급**(`severity_raised`, 런당 1회). 리뷰어가 처음에 낮게 본 비용을 수리자 예산에서 빼지 않는다. 새 키의 major 는 지급 아님 (ADR-H048) |

**`review_repair.max: 2` · `stuck_after_identical: 2` · `local_repair.max_per_run: 3`
과 그 판정 기준 세 숫자는 미검증 상속값이다.** 원본 명세에서 왔고 이 리포에서
재본 적이 없다. 첫 세 런의 원장이 이 값을 검사한다.

**`loop.counter` · `loop.max` · `loop.on_exceed` 는 코드가 여기서 읽는다** (M36).
예전에는 카운터 이름이 코드에 박혀 있었고 상한에는 `or 2` 폴백이 있었다 —
**폴백은 곧 새 하드코딩이다.** 지금은 선언이 없으면 exit 2 이고, 그 사실을
`lint-phases` 가 런 전에 먼저 잡는다.
