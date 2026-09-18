---
description: 요청 하나를 레인 예측 → 계획 → 교차검증 → 구현 → 게이트 → 코드리뷰 → PR → PR리뷰 → 보고서(00~08)까지 끌고 간다. 머지는 하지 않는다.
---

> **이 커맨드는 트리아지와 여덟 페이즈를 전부 돈다** — 레인 예측 · 계획 ·
> 교차검증 · 구현 · 게이트 · 코드리뷰 · PR · PR리뷰 · 보고서. 레인이 `docs`
> 면 계획 리뷰어 · 교차검증 · 역할이 빠진다 — 그것은 기계가 정하고 네가
> 정하지 않는다.
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
**프로파일은 묻지 않는다** — 00 이 요청 원문의 구조 신호로 예측한다
(ADR-H044). `--profile` 은 **사용자가 이미 말했을 때만** 준다 — 그 값은
재판정에 밀리지 않으므로 네가 대신 정하면 기계의 검증이 그 값을 못 고친다.

> **이 단계의 한계를 알고 있어라.** `init` 이 바이트와 sha256 을 박으므로 그
> **이후**의 변조는 기계가 잡는다. 그러나 네가 옮겨 적는 **그 순간**의 의역은
> 아무도 잡지 못한다. 여기가 이 파이프라인에서 사람의 의도가 새어 나갈 수 있는
> 유일한 자리다.

```bash
python scripts/pipeline/cli.py init --feature {slug} \
    --request-file _workspace/requests/{slug}.md [--profile docs|small|normal]
```

## 2. 루프

```bash
python scripts/pipeline/cli.py next
```

봉투(stdout 의 JSON 하나)에서 **`render` 와 `next_command` 둘만 읽는다.**
다른 필드로 판단하지 마라.

1. `render` 가 시키는 대로 한다
2. `produces[].path` 에 파일을 쓴다
3. `next_command` 를 그대로 실행한다

### 종료 코드별 대처

| exit | 뜻 | 할 일 |
|---|---|---|
| 0 | 진행 | 봉투의 `next_command` 를 계속 따른다 |
| 3 | 선행조건 미충족 | `render` 가 말한 것을 채우고 같은 명령을 다시 친다 |
| 4 | 기계 판정 실패, 예산 남음 | 수리한다. **`data.repair_dispatch` 의 배정을 그대로 쓴다** |
| 8 | 제출물이 스키마·정합성을 어겼다 | 고쳐서 다시 낸다 |
| 5 · 7 · 10 | 예산 소진 · 반복 한계 · 에스컬레이션 | **멈춘다.** `ESCALATION.md` 의 선택지를 그대로 사용자에게 제시한다 |
| 6 | 전이 거부 — 산출물 없음 · 지문 stale · 승격 미종결 | `render` 가 말한 것을 채운다. 승인이 무효면 재승인이다 |
| 9 | **사람의 판단 대기.** 상태를 잠그지 않는다 | 사용자에게 선택지를 그대로 제시하고 답을 받는다. **네가 고르지 마라** |
| 11 | 런 완료 | 종료 보고로 간다 |

### 모든 페이즈에서 — 모델 등급

봉투가 `## 모델 등급` 절에 지시 키마다 `model: X` 를 찍는다. **그 값을 Agent
호출의 `model` 인자로 그대로 넘긴다** — `inherit` 면 인자를 주지 않는다. 네가
고르지 마라. 실행기는 실제 모델을 검증하지 못하므로 이것은 지시로만 남는다.

### 00-triage 에서

`init` 뒤 첫 `next` 가 00 이다. **기계 신호로 확정되면 봉투가 01 지시문을 바로
낸다** — 모델을 부르지 마라. 미확정이면 봉투가 00 패킷을 내고 `model:` 을
찍어 준다. 그 에이전트에게 **요청 원문만** 준다(`00_original_request.md`) —
리포 탐색을 허용하지 마라. 제출은 봉투의 형식대로 `00_triage.json` 이고
`record --phase 00` 으로 낸다.

exit 9 면 `unclear` 다 — 3지선다를 **그대로** 사용자에게 제시하고, 고른 값을
`profile` 에 넣고 `decided_by: "user"` 로 같은 파일을 다시 낸다.
**네가 고르지 마라.**

### 01-plan 에서

**봉투가 「리뷰어 — 0명」이라 하면 리뷰어를 부르지 않는다** — `docs` 레인이고,
플랜 제출이 기계 검사를 통과하면 1라운드에 닫힌다. 라우팅 밖의 제출은 받지
않는다.

1라운드는 리뷰어 둘(plan · xv)을 한 메시지 안에서 병렬 호출한다. **2라운드부터는
봉투의 `planned` 에 있는 리뷰어만** 부른다 — 열린 Critical 을 낸 쪽이다. 다른
리뷰어를 같이 부르지 마라. 라운드를 강제하는 것은 Critical 뿐이고, 열린
Major·Minor 는 기록되어 보고서로 간다 — 고칠지는 네 판단이다.

### 02-cross-verify 에서

01 이 **1라운드에 수렴했으면 02 는 기계가 건너뛴다** (`plan_unedited`). 봉투가
03 지시문을 바로 낸다 — 교차검증기를 부르지 마라. 2라운드 이상이면 평소대로
전문을 교차검증기에 넘긴다.

### 03-implement 에서

**봉투가 「역할 — 0명」이라 하면** 역할을 부르지 않고 계약도 쓰지 않는다 —
`docs` 레인이다. 네가 직접 문서를 고치고 `03_claims.json` 을
`{"schema":1,"roles":[]}` 로 낸다. 역할 소유 경로(소스)를 건드리면 제출이
exit 3 으로 되돌아온다 — 예측이 빗나간 것이고, 계약을 쓰고 `next` 로 역할
패킷을 받는다.

역할 전원을 **한 메시지 안에서 동시 호출**한다. 각 역할에게 봉투의 역할 프롬프트
템플릿을 채워 준다 — 소유권 표를 **그대로** 싣고 glob 을 문장으로 옮겨 적지 마라.

계약 파일은 **네가 직접 쓴다.** 역할 에이전트에게 위임하지 않는다.

역할 제출마다 `rules_read: [{path, sha256}]` 가 있어야 한다 — 지시 파일과
`rules_dir` 직속 `.md` 의 현재 해시다 (ADR-H055). 없거나 다르면 exit 8 이고
봉투가 경로만 알려준다. **네가 해시를 대신 계산해 넣지 마라** — 워커가 읽었다는
흔적이 이것뿐이다.

### 04-gate 에서

```bash
python scripts/pipeline/cli.py gate --phase 04
```

실패하면 봉투가 소유자와 브리프를 준다. **실패를 다시 분류하지 마라** — 배정은
게이트가 이미 했고, 네가 다시 정하면 핑퐁 방지와 시그니처 추적이 무너진다.

### 05-code-review 에서

**비용 오름차순이고, 앞의 두 개는 모델을 부르지 않는다.**

```bash
python scripts/pipeline/cli.py precheck --scope pr --run-id <id>
python scripts/pipeline/cli.py contract-trace --run-id <id>
```

| exit | 뜻 | 할 일 |
|---|---|---|
| 9 | 예산·브랜치·base | **사람에게 묻는다.** 자동으로 쪼개거나 리베이스하지 마라 |
| 10 | 인프라 프로브 실패 | 멈춘다. 카운터는 소모되지 않았다 |
| 8 (trace) | Critical 이 남았다 | 고치고 `gate --phase 05 --stage loop` 후 다시 친다 (compile 포함 — scoped 단독은 타입 에러를 흘린다, ADR-H046) |

그다음 **봉투가 준 리뷰어 목록을 그대로** 한 메시지 안에서 병렬 호출한다.

- **누구를 부를지 다시 정하지 마라.** 라우팅은 `when` glob 이 정하는 결정론이다
- 프롬프트 첫 줄은 **스킬 파일을 읽으라는 지시**다. 본문을 복사하지 마라 —
  리뷰어 수만큼 그 고정비가 곱해진다
- 봉투의 **"검토 제외"** 목록을 그대로 전달한다
- 리뷰어에게 **리포 탐색을 허용하지 마라.** 부족하면 `need_more_context` 에 적게 한다

리뷰어마다 `.raw.md` 와 `.json` **두 파일**을 받고 하나씩 제출한다.
**모드가 `merged` 여도 그렇다** — `merged` 는 한 에이전트가 관점을 순차로
적용한다는 뜻이지 제출이 하나라는 뜻이 아니다. `--reviewer merged` 는 라우팅
밖이라 exit 8 이다 (M37).

```bash
python scripts/pipeline/cli.py record --phase 05 --file <리뷰 json> \
    --reviewer <code> --round <n> --run-id <id>
```

**원문 헤딩 개수와 findings 개수가 다르면 exit 8 이다. 네가 사후에 헤딩을 붙여
맞추지 마라** — 원문 대조라는 검사의 취지가 그 순간 사라진다. 리뷰어에게 형태를
다시 알려 주고 다시 받는다.

exit 4 면 Critical/Major 수리다. **Minor 는 고치지 않는다** — 원장에 쌓이고
보고서로 간다. **다만 다음 회차 제출에서 회계는 한다** (M38): 열려 있던 지적은
Minor 를 포함해 전부 다시 내거나 `resolved_from_previous`·
`reraised_from_previous` 로 처리한다. 빠지면 "조용히 증발했다"로 exit 8 이다.
회계할 목록은 수리 봉투가 적어 준다 — 네가 재구성하지 마라.

### 05 와 06 사이 — **여기서 커밋한다**

**파이프라인은 `git commit` 을 하지 않는다.** 그런데 06 의 `pr` 은 push 를 하고
PR 본문의 diff 통계는 `main...HEAD`(커밋된 것)를 읽는다. **03 이 쓴 코드가
미커밋이면 push 해도 PR 에 안 들어간다.**

자리는 하나뿐이다 — **`record --phase 05` 가 통과한 뒤, `precheck --phase 06` 을
치기 전.**

| 시점 | 되는가 | 이유 |
|---|---|---|
| 04 게이트 전 | ✗ | 05 의 변경 파일 목록이 미커밋만 보므로 리뷰어 라우팅이 0명이 되고 `review05.status` 가 `failed` 가 된다 |
| **05 통과 직후** | **✓** | `record --phase 05` 는 지문을 검사하지 않는다. 지문을 보는 것은 `advance` 와 `pr` 이다 |
| `approve` 이후 | ✗ | `approve` 가 그 시점 지문을 박고 `pr` 이 push 직전에 다시 대조한다 → exit 6 재승인 |

> **~~이 커밋이 04 게이트의 영수증을 낡게 만든다~~ — 닫혔다 (M25, 2026-09-04).**
> 지문이 `HEAD` 를 포함했을 때 얘기다. 지금은 **소유 범위 파일의 내용 해시**라
> 커밋이 값을 바꾸지 않는다. 위 표의 세 줄은 그대로 유효하다 — 그것들은 지문이
> 아니라 05 라우팅과 승인 시점에서 오는 제약이다.

### 06-pr 에서

**여기부터 밖으로 나간다.** 앞의 셋이 무료다.

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
- **exit 6** — 승인 뒤 코드가 바뀌었다. 재승인이다
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

**PR 상태를 먼저 본다.** 닫혔거나 머지됐으면 수리도 코멘트도 하지 않는다.

외부 리뷰를 모아 `07_external.json` 으로 내고:

```bash
python scripts/pipeline/cli.py review07 --external <07_external.json> --run-id <id>
```

봉투가 `--effort` 를 준다. **그 값 그대로** `/code-review` 를 부른다 —
**effort 를 네가 고르지 마라.** 결정론이어야 `escaped_05` 가 근거가 된다.
봉투가 **`skipped`** 를 주면 `/code-review` 를 부르지 않는다 — `07_pr_review.json`
을 `code_review: "skipped"` · findings 빈 배열로 내고 바로 `record` 로 간다.
깨끗한 런(05 ok · Major 없음 · 04·05 수리 없음)이 그렇고, 일반 정합성은 05 의
`gen` 이 이미 봤다 (ADR-H043). 승격은 그 뒤에 그대로 돈다.

```bash
python scripts/pipeline/cli.py record --phase 07 --file <07_pr_review.json> --run-id <id>
python scripts/pipeline/cli.py promote --scan --run-id <id>
```

`promote --scan` 이 후보 0 이면 **모델을 부르지 않고 끝난다** — 초기 런의 최빈
경로다. 후보가 있으면 판정을 내고 `--apply` 한다. **`duplicate` 에서 `create` 는
금지고, `contradicts` 는 에스컬레이션이다.**

승격은 **별도 브랜치**로 간다. 기능 PR 에 규칙 변경을 섞지 마라.

### 08-report 에서

```bash
python scripts/pipeline/cli.py promote --flush --run-id <id>
python scripts/pipeline/cli.py report --run-id <id>
```

`08_report_data.json` 하나만 쓴다 (20KB 이하). **08 은 diff 도 코드도 읽지
않는다.** 표는 실행기가 조립하니 너는 서술만 쓴다 — **재지 않은 것을 숫자로
적지 마라.** `배운 점`·`next_run` 은 80자 이상이다 — 미달이면 exit 8 로 되묻는다.

`report` 가 exit 11 로 런을 닫으면 **런 기록을 기능 PR 에 싣는다** (ADR-H052):

```bash
git add docs/harness/pipeline/runs/<id>.md docs/harness/PILOT-LOG.md docs/harness/pipeline/ledger/
git commit -m "chore: 파이프라인 실행 기록 반영 (<id> 런)"
python scripts/pipeline/cli.py pr --run-id <id>      # 닫힌 런의 PR 갱신 — 06 record 로 이어지지 않는다
```

기록이 diff 에 없으면 gap `run_record_missing` 으로 등급이 내려간다.

## 3. 종료 보고

`status` 로 확인하고 아래를 사람이 읽을 수 있게 적는다.

- 등급과 `gaps` 전부 — **스킵된 것을 통과로 적지 마라**
- 스킵된 스테이지: **"이 스택에 없는 것"과 "이번에 안 건드려서 건너뛴 것"을 구분**
- **`review05.status`** — `degraded`·`failed` 면 **몇 명이 계획됐고 몇 명이
  성공했는지**까지 적는다. findings 0건과 "리뷰가 없었다"는 다른 사실이다
- `contract-trace` 가 **건너뛴 검사**가 있으면 그것도 (통과가 아니다)
- `dropped_by_enforcement` 와 `truncated` 가 0이 아니면 그 수
- 카운터 사용량과 모델 호출 근사치(근사임을 명시)
- 런 디렉터리 경로
- **PR 번호와 상태**, 그리고 승인이 `user` 였는지 `auto` 였는지
- **`review07`** — 외부 리뷰 상태와 `escaped_05`. 봇이 `disabled` 였으면 그렇게
  적는다. **"리뷰가 없었다"를 "지적이 없었다"로 적지 마라**
- 승격 결과 — `applied` / `rejected` / `skipped` 를 사유와 함께
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
- **머지하지 마라.** 이유: 명세가 머지 자동화를 범위 밖으로 둔다
- **승인을 대신하지 마라.** 이유: `--auto` 는 사람이 미리 켜는 것이다
- **PR 본문을 다시 조립하지 마라.** 이유: `06_pr_body.md` 는 마스킹을 거쳤다
- **봉투 없이 스테이지 명령을 직접 돌리지 마라.** 이유: 결과가 영수증에 남지 않아
  지문 대조가 성립하지 않는다
- **실패 귀속을 다시 하지 마라.** 이유: 배정은 게이트가 한다
- **프로파일(레인)을 네가 정하지 마라.** 이유: 00 의 기계 신호 · 지시된 모델 ·
  사람의 답 셋 중 하나가 정하고, 03·05 가 그것을 검증한다. 네가 정하면 같은
  요청이 세션마다 다른 레인을 탄다
- **봉투가 찍은 `model:` 을 바꾸지 마라.** 이유: 등급은 `config.models` 가
  레인별로 정한 결정론이고, 실행기가 검증할 수 없는 지시라 네가 바꾸면 아무도
  모른다
- **계약을 역할 에이전트에게 쓰게 하지 마라.** 이유: 메인 단독 소유다
- **`harness/config.json` · `harness/adapters/*` · `harness/calibration.json` 을
  고치지 마라.** 이유: 게이트가 검사할 기준을 게이트를 통과하려고 고치는 것이다
- **실패를 요약해 없애지 마라.** 이유: 스킵·미측정·미검증이 보고서에 드러나는 것이
  이 파이프라인의 존재 이유다
