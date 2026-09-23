---
description: 요청 하나를 계획 → 구현 → 게이트 → 코드리뷰 → PR → PR리뷰 → 보고서(01~08)까지 끌고 간다. 머지는 하지 않는다.
---

> **이 커맨드는 일곱 페이즈를 전부 돈다** — 계획 · 구현 · 게이트 · 코드리뷰 ·
> PR · PR리뷰 · 보고서. 레인이 `docs` 면 계획 리뷰어 · 역할이 빠진다 — 레인은
> 사용자가 선언하고 기본은 `normal` 이다.
> **06 부터는 되돌리기 어려운 외부 행동이 들어간다.** push 는 실행기가 하고,
> PR 생성·갱신과 코멘트 게시는 **네가 forge 도구로** 한다.
> **머지는 하지 않는다.** 이 파이프라인의 범위는 PR 까지다.

요청: $ARGUMENTS

---

## 0. 게이트

```bash
python scripts/pipeline/cli.py doctor
```

**exit 2 면 여기서 멈춘다.** FAIL 항목을 사용자에게 그대로 보고하고 **고치려 들지
마라** — 설정과 실물이 어긋난 것이고, 무엇을 맞출지는 사람이 정한다.

## 1. 요청을 파일로 동결한다

`_workspace/requests/{slug}.md` 에 **사용자가 친 문장을 한 글자도 바꾸지 않고**
쓴다. 요약·정리·다듬기·번역 전부 금지다.

slug 는 `^[a-z0-9][a-z0-9-]*$` 형태로 제안하고 사용자에게 확인받는다.
**레인은 기본 `normal` 이다.** `--profile` 은 **사용자가 `docs`(문서만) 나
`fix`(버그 하나) 를 이미 말했을 때만** 준다 — 네가 대신 정하지 마라. `docs` 를
선언한 런이 소스를 건드리면 03·05 가 exit 3 으로 되돌리고 `lane_miss` 가 등급에 남는다.

> **이 단계의 한계를 알고 있어라.** `init` 이 바이트와 sha256 을 박으므로 그
> **이후**의 변조는 기계가 잡는다. 그러나 네가 옮겨 적는 **그 순간**의 의역은
> 아무도 잡지 못한다. 여기가 이 파이프라인에서 사람의 의도가 새어 나갈 수 있는
> 유일한 자리다.

```bash
python scripts/pipeline/cli.py init --feature {slug} \
    --request-file _workspace/requests/{slug}.md [--profile docs|fix|normal]
```

## 2. 루프

```bash
python scripts/pipeline/cli.py next
```

봉투(stdout 의 JSON 하나)에서 **`render` 와 `next_command` 를 따른다.** `data` 는
`render` 가 가리키는 것만 본다 — `produces`(쓸 파일) · `repair_dispatch`(04 수리
배정) · `stage`(03 컴파일 실패 스테이지). 다른 필드로 판단하지 마라.

1. `render` 가 시키는 대로 한다
2. `render` 의 「쓸 파일」(= `data.produces`)에 파일을 쓴다
3. `next_command` 를 그대로 실행한다

### 종료 코드별 대처

| exit | 뜻 | 할 일 |
|---|---|---|
| 0 | 진행 | 봉투의 `next_command` 를 계속 따른다 |
| 3 | 선행조건 미충족 | `render` 가 말한 것을 채우고 같은 명령을 다시 친다 |
| 4 | 기계 판정 실패, 예산 남음 | 수리한다. 04 는 **`data.repair_dispatch` 의 배정을 그대로 쓴다**, 03 컴파일 실패는 `data.stage` 가 실패한 스테이지다 |
| 8 | 제출물이 스키마·정합성을 어겼다 | 고쳐서 다시 낸다 |
| 5 · 10 | 예산 소진 · 반복 한계·에스컬레이션 | **멈춘다.** `ESCALATION.md` 의 선택지를 그대로 사용자에게 제시한다 |
| 6 | 전이 거부 — 산출물 없음 · 지문 stale | `render` 가 말한 것을 채운다. 승인이 무효면 재승인이다 |
| 9 | **사람의 판단 대기.** 상태를 잠그지 않는다 | 사용자에게 선택지를 그대로 제시하고 답을 받는다. **네가 고르지 마라** |
| 11 | 런 완료 | 종료 보고로 간다 |

### 모든 페이즈에서 — 모델과 effort

모델과 effort 는 `.claude/agents/*.md` 프론트매터가 역할별로 정한다 (ADR-H061).
**Agent 호출에 `model` 인자를 주지 마라** — 두 출처가 생기면 어느 쪽이 이겼는지
실행기가 보지 못한다. 프롬프트에 "깊게 생각하라" 류의 effort 지시를 넣지 마라 —
손잡이가 아니고 접두부만 늘린다.

### 01-plan 에서

**봉투가 「리뷰어 — 0명」이라 하면 리뷰어를 부르지 않는다** — `docs` 레인이고,
플랜 제출이 통과하면 1라운드에 닫힌다. 계획에 없는 제출은 받지 않는다.

01 의 리뷰어는 `plan-reviewer` 하나다 (ADR-H045). 리뷰어에게 요청 원문과 플랜을
주고 **리포를 읽게 둔다** — 플랜이 가리키는 파일을 열어 근거를 확인하는 것이
그 리뷰어의 일이다(쓰기·상태 변경은 금지). **2라운드부터는 봉투의 `planned` 에
있을 때만** 부른다. 라운드를 강제하는 것은 Critical 뿐이고, 열린 Major·Minor 는
기록되어 보고서로 간다 — 고칠지는 네 판단이다.

리뷰어의 Critical 이 코드로 반박되면 플랜을 억지로 맞추지 말고 **`record` 하기
전에** 리뷰 JSON 최상위에 `false_positive: [{id, reason, evidence}]` 를 단다 —
`evidence` 는 리포에 실재하는 경로다. 근거 없는 기각은 exit 8 이고, 기각한
finding 을 파일에서 지우면 헤딩 대조가 exit 8 을 낸다.

### 03-implement 에서

**봉투가 「역할 — 0명」이라 하면** 역할을 부르지 않고 계약도 쓰지 않는다 —
`docs` 레인이다. 네가 직접 문서를 고치고 `03_claims.json` 을
`{"schema":1,"roles":[]}` 로 낸다. 역할 소유 경로(소스)를 건드리면 제출이
exit 3 으로 되돌아온다 — 선언이 빗나간 것이고, 계약을 쓰고 `next` 로 역할
패킷을 받는다.

그 밖의 런은 **봉투 헤더의 역할 하나(`impl→impl-writer`)를 부른다.** 봉투의 역할 프롬프트
템플릿을 채워 준다 — 소유권 표를 **그대로** 싣고 glob 을 문장으로 옮겨 적지 마라. 작성자는
계약대로 테스트를 먼저 쓰고 구현하며 typecheck·lint·테스트를 직접 돌려 확인한다 — 그것은
자기 확인이고, 영수증은 04 가 남긴다. 제출은 `03_claims.json` 하나다.

계약 파일은 **네가 직접 쓴다.** 작성자에게 위임하지 않는다. `## 유닛`·`## 진입점` 은
최상위 `- ` 불릿 하나에 항목 하나다.

### 04-gate 에서

```bash
python scripts/pipeline/cli.py gate --phase 04
```

실패하면 봉투가 실패 스테이지의 출력 브리프를 준다 — `impl-writer` 에게 **그대로**
되돌린다. 요약하지 마라. 인프라 패턴(외부 의존 미기동)에 걸리면 카운터를 안 태우고
에스컬레이션이다.

### 05-code-review 에서

**비용 오름차순이고, 앞의 두 개는 모델을 부르지 않는다.**

```bash
python scripts/pipeline/cli.py precheck --scope pr --run-id <id>
python scripts/pipeline/cli.py contract-trace --run-id <id>
```

| exit | 뜻 | 할 일 |
|---|---|---|
| 9 | 브랜치·base | **사람에게 묻는다.** 자동으로 리베이스하지 마라. 예산(파일·줄)은 정보 행이라 멈추지 않는다 |
| 10 | 인프라 프로브 실패 | 멈춘다. 카운터는 소모되지 않았다 |
| 8 (trace) | Critical 이 남았다 | 고치고 `gate --phase 05 --stage loop` 후 다시 친다 (compile 포함 — scoped 단독은 타입 에러를 흘린다, ADR-H046) |

그다음 **봉투가 이름 지은 리뷰어 1명(`gen`)**을 부른다.

- **리뷰어를 늘리지 마라.** 한 명이고 봉투가 정한다 — 소스 변경이 없으면 0명이고,
  그것은 `review05:failed` gap 이다
- Agent 호출 `subagent_type: general-reviewer` 로 부른다 — 관점·제출 형식은 에이전트
  정의가 든다. 본문을 복사하지 마라
- 봉투의 참조 파일 경로(`diff+refs`)를 그대로 전달한다
- 리뷰어에게 **리포 탐색을 허용하지 마라.** 부족하면 `need_more_context` 에 적게 한다

리뷰어의 `.raw.md` 와 `.json` **두 파일**을 받고 제출한다.

```bash
python scripts/pipeline/cli.py record --phase 05 --file <리뷰 json> \
    --reviewer <code> --round <n> --run-id <id>
```

**원문 헤딩 개수와 findings 개수가 다르면 exit 8 이다. 네가 사후에 헤딩을 붙여
맞추지 마라** — 원문 대조라는 검사의 취지가 그 순간 사라진다. 리뷰어에게 형태를
다시 알려 주고 다시 받는다.

exit 4 면 Critical/Major 수리다. **Minor 는 고치지 않는다** — 보고서로 간다.
**다만 다음 회차 제출에서 회계는 한다** (ADR-H025): 열려 있던 지적은
Minor 를 포함해 전부 다시 내거나 `resolved_from_previous`·
`reraised_from_previous` 로 처리한다. 빠지면 "조용히 증발했다"로 exit 8 이다.
회계할 목록은 수리 봉투가 적어 준다 — 네가 재구성하지 마라.

수리 뒤 순서는 봉투의 `next_command` 다: `gate --phase 05 --stage loop` → `next`(델타
지시·지문 갱신) → 델타 리뷰 → `record`. 재게이트를 건너뛰면 `next`·`record` 가 exit 6 이다.

### 05 와 06 사이 — **여기서 커밋한다**

**파이프라인은 `git commit` 을 하지 않는다.** 그런데 06 의 `pr` 은 push 를 하고
PR 본문의 diff 통계는 `main...HEAD`(커밋된 것)를 읽는다. **03 이 쓴 코드가
미커밋이면 push 해도 PR 에 안 들어간다.**

자리는 하나뿐이다 — **`record --phase 05` 가 통과한 뒤, `precheck --phase 06` 을
치기 전.**

| 시점 | 되는가 | 이유 |
|---|---|---|
| 04 게이트 전 | ✗ | 05 의 변경 파일 목록이 미커밋만 보므로 계획된 리뷰어가 0명이 되고 `review05.status` 가 `failed` 가 된다 |
| **05 통과 직후** | **✓** | `record --phase 05` 는 게이트 영수증 지문을 대조한다(재게이트 누락 → exit 6). 커밋은 내용 해시라 지문을 바꾸지 않는다 |
| `approve` 이후 | ✗ | `approve` 가 그 시점 지문을 박고 `pr` 이 push 직전에 다시 대조한다 → exit 6 재승인 |

### 06-pr 에서

**여기부터 밖으로 나간다.** 앞의 셋이 무료다.

`pr` 전에 **흐름 노트 `06_pr_notes.json`** 을 쓴다 — PR 본문의 「핵심 흐름」과
「직접 확인하는 법」이다(형식은 `06-pr.md` 1.5번). `step` 한 줄 이상과 `verify`
하나 이상이 있어야 한다 — 없으면 `pr` 이 exit 8 로 알린다. `refs` 는 선택이다.

```bash
python scripts/pipeline/cli.py precheck --scope pr --phase 06 --run-id <id>
python scripts/pipeline/cli.py pr --run-id <id>
```

`pr` 이 **exit 9** 를 내면 사람의 판단이다 — 승인 요청이거나 원격 3지선다다.
**선택지를 그대로 사용자에게 제시하고 네가 고르지 마라.** 승인이 오면:

```bash
python scripts/pipeline/cli.py approve --phase 06 --run-id <id>
python scripts/pipeline/cli.py pr --run-id <id>      # 이번엔 push 까지 간다
```

- **exit 3** — 브랜치가 규약과 안 맞거나 보호 브랜치 위다. **브랜치를 만들지 마라**
- **exit 3 (`approve`)** — 전체 회귀가 지금 코드에서 안 돌았다. `gate --phase 05 --stage full` 뒤 다시 친다
- **exit 6 (`approve`)** — 05 수리 뒤 재게이트가 없었다. `gate --phase 05 --stage loop` 뒤 다시 친다
- **exit 6 (`pr`)** — 승인 뒤 코드가 바뀌었다. 재승인이다
- **exit 10** — non-fast-forward 다. **force-push 는 금지**이고 에스컬레이션이다

`pr` 이 exit 0 이면 push 가 끝났고 `06_pr_req.json` 이 있다. **PR 은 네가 만든다:**

- 본문은 `06_pr_body.md` 를 **그대로** 쓴다. **다시 조립하지 마라** — 이미
  마스킹을 거쳤고, 새로 쓰면 그 마스킹을 우회한다
- `action` 이 `update` 면 **생성하지 말고 갱신한다.** 새로 만들면 PR 이 갈라진다
- **머지하지 마라**

```bash
python scripts/pipeline/cli.py record --phase 06 --file <06_pr_result.json> --run-id <id>
```

### 07-pr-review 에서

**PR 상태를 먼저 본다.** 닫혔거나 머지됐으면 리뷰 없이 `07_pr_review.json` 을
findings 빈 배열로 내고 바로 `record` 로 간다.

열려 있으면 `/code-review` 를 **1회** 부른다 — 생략 조건도 effort 선택도 없다.
결과를 `07_pr_review.json` 으로 옮겨 적되, 봉투의 「05 가 낸 지적」 목록과 **같은
결함**에만 그 `finding_key` 를 단다. 나머지는 새 것이다. 형식은 `07-pr-review.md`
「제출 형식」.

```bash
python scripts/pipeline/cli.py record --phase 07 --file <07_pr_review.json> --run-id <id>
```

Critical/Major 가 새로 나오면 `record` 가 gap `pr_review_open` 으로 등급을 내리고
넘어간다 — **수리하지 마라.** 보고서와 종료 보고에 남기고 사람이 정한다.


### 08-report 에서

```bash
python scripts/pipeline/cli.py report --run-id <id>
```

`08_report_data.json` 하나만 쓴다 (20KB 이하). **08 은 diff 도 코드도 읽지
않는다.** 표는 실행기가 조립하니 너는 서술만 쓴다 — **재지 않은 것을 숫자로
적지 마라.** `문제`·`원인`·`해결`·`contract_gaps` 는 80자 이상이다 — 미달이면
exit 8 로 되묻는다. 형식은 `08-report.md` 「제출 형식」.

`report` 가 exit 11 로 런을 닫으면 **런 기록을 기능 PR 에 싣는다** (ADR-H052):

```bash
git add docs/harness/pipeline/runs/<id>.md
git commit -m "chore: 파이프라인 실행 기록 반영 (<id> 런)"
python scripts/pipeline/cli.py pr --run-id <id>      # 닫힌 런의 PR 갱신 — 06 record 로 이어지지 않는다
```

## 3. 종료 보고

`status` 로 확인하고 아래를 사람이 읽을 수 있게 적는다.

- 등급과 `gaps` 전부 — **스킵된 것을 통과로 적지 마라**
- 스킵된 스테이지: **"이 스택에 없는 것"과 "이번에 안 건드려서 건너뛴 것"을 구분**
- **`review05.status`** — `degraded`·`failed` 면 **몇 명이 계획됐고 몇 명이
  성공했는지**까지 적는다. findings 0건과 "리뷰가 없었다"는 다른 사실이다
- `contract-trace` 가 **건너뛴 검사**가 있으면 그것도 (통과가 아니다)
- `truncated` 가 참이면 그 사실 (findings 상한으로 잘렸다)
- 카운터 사용량과 모델 호출 근사치(근사임을 명시)
- 런 디렉터리 경로
- **PR 번호와 상태**, 그리고 승인이 `user` 였는지 `auto` 였는지
- **07 `/code-review`** — `done`/`skipped`(사유) 와 escaped(05 가 낸 키를 가리키지
  않은 Major+) 수. **"리뷰가 없었다"를 "지적이 없었다"로 적지 마라**
- 보고서 경로
- 그리고 이 문장:
  > 이 런은 PR 까지 갔고 **머지하지 않았다.** 머지는 이 파이프라인의 범위가
  > 아니다.

## 다른 진입

```bash
python scripts/pipeline/cli.py next --run-id <id>     # 세션 복구
python scripts/pipeline/cli.py status                 # 현황
python scripts/pipeline/cli.py resume --ack --answer-file <경로>   # 잠금 해제
```

## 금지

- **`git push` 를 직접 하지 마라.** 이유: push 는 `pr` 이 한다 — 승인 지문을
  확인하고 계약을 지운 **뒤**에 해야 하고, 순서가 어긋나면 재개가 깨진다
- **브랜치를 만들지 마라.** 이유: 어디에 커밋할지는 사람이 정한다 (exit 3)
- **force-push 하지 마라.** 이유: 외부 리뷰 스레드와 승인이 깨진다
- **머지하지 마라.** 이유: 머지 자동화는 이 파이프라인의 범위 밖이다
- **승인을 대신하지 마라.** 이유: `--auto` 는 사람이 미리 켜는 것이다
- **PR 본문을 다시 조립하지 마라.** 이유: `06_pr_body.md` 는 마스킹을 거쳤다
- **봉투 없이 스테이지 명령을 직접 돌리지 마라.** 이유: 결과가 영수증에 남지 않아
  지문 대조가 성립하지 않는다
- **레인을 네가 정하지 마라.** 이유: 사용자가 말한 값이거나 기본 `normal` 이다.
  네가 정하면 같은 요청이 세션마다 다른 레인을 탄다
- **Agent 호출에 `model` 인자를 주지 마라.** 이유: 모델과 effort 는 에이전트
  프론트매터가 정한다. 호출마다 다르게 주면 같은 역할이 세션마다 다른 모델을 탄다
- **계약을 역할 에이전트에게 쓰게 하지 마라.** 이유: 메인 단독 소유다
- **`harness/config.json` · `harness/adapters/*` 를 고치지 마라.** 이유: 게이트가
  검사할 기준을 게이트를 통과하려고 고치는 것이다
- **실패를 요약해 없애지 마라.** 이유: 스킵·미측정·미검증이 보고서에 드러나는 것이
  이 파이프라인의 존재 이유다
