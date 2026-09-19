---
{
  "id": "00-triage",
  "index": 0,
  "owner": "main",
  "approval": "none",
  "requires": [
    {"kind": "file", "path": "${run.dir}/00_original_request.md", "min_bytes": 1,
     "sha256_pointer": "request.sha256"}
  ],
  "produces": [
    {"key": "triage", "path": "${run.dir}/00_triage.json", "kind": "json",
     "schema": "triage"}
  ],
  "submit_checks": [
    {"id": "triage_profile_vocabulary", "on_fail": 8},
    {"id": "triage_paths_substring", "on_fail": 8},
    {"id": "triage_docs_paths_in_glob", "on_fail": 8},
    {"id": "triage_unclear", "on_fail": 9}
  ],
  "gate": {"runner": "none"},
  "allow": {"agents": []},
  "on_success": "01-plan"
}
---

## 목적

요청을 파이프라인에 넣기 **전에** 레인을 정한다 — `docs` · `fix` · `small` · `normal`.
그 값이 `state.profile` 이 되어 01 의 라운드 상한 · 02 의 생략 · 03 의 역할 ·
05 의 리뷰어 상한 · 07 의 생략을 **결정론적으로** 줄인다.

**`fix` 는 사람 또는 모델만 정한다** (ADR-H053) — `init --profile fix` · `unclear`
의 응답 · 모델 제출 세 경로다. 기계 신호는 `fix` 를 내지 않는다: "버그 수정" 은
언어 키워드이고 이 페이즈는 경로·글자 수만 본다. fix 레인은 01 1라운드 · 02
생략(`fix_profile`, gap 아님) · 05 리뷰어 1명(`gen`) · 07 생략(지적 0건이면
low)이고, 03 은 impl·test 둘 다 부른다 — 재현 테스트는 test 역할이 쓴다.
계약 유닛 수가 넘치면 03·05 가 `small`/`normal` 로 올리고 `triage_miss` 가 남는다.

이 페이즈가 막는 실패는 하나다 — 문서 한 줄 고치는 요청이 플랜 리뷰어 둘 ·
교차검증 · 역할 둘 · 리뷰어 넷을 다 내는 것. 전에는 프로파일이 03 에서야
계약 유닛 수로 정해져 01·02 는 언제나 `normal` 비용이었다.

**판정은 예측이다.** 실행기는 요청이 실제로 무엇을 바꿀지 볼 수 없다. 그래서
`source: triage` 로 남기고, 03(계약 유닛 수)·05(변경 파일)의 재판정에 밀린다.
상향이면 `triage_miss` 가 gap 으로 남아 등급이 `PASS_WITH_GAPS` 다 — 앞
페이즈가 양보를 적용한 채 지나갔기 때문이다.

## 진입 조건

- 동결된 요청 원문이 있고 sha256 이 상태의 것과 일치한다

## 절차

1. **기계가 먼저 본다.** 실행기가 요청 원문에서 경로 토큰을 뽑아 역할 소유 ·
   docs glob · 미해결로 나누고 글자 수를 센다 (`scripts/pipeline/triage.py`).
   언어 키워드는 보지 않는다 — 같은 경로를 어느 언어로 적어도 판정이 같다.
   - 역할 소유 경로가 있으면 `small` 또는 `normal` (개수·글자 수 임계는
     `config.triage`)
   - docs 경로만 있으면 `docs`
   - 확정되면 **모델을 부르지 않는다.** 실행기가 `00_triage.json` 을 쓰고 같은
     봉투에서 01 지시문을 낸다
   - 실행기가 원장의 열린 `deferred` 중 요청 경로와 겹치는 건수를 세어 00·01
     패킷에 「이월 미해결」로 적는다 (`ledger/deferred.md`, ADR-H051). 앞선
     런이 미룬 것이 이 런의 입력이다
2. **사람이 `init --profile` 을 줬으면** 판정을 덮지 않는다. 신호만 기록한다.
3. **미확정이면** 봉투가 저가 모델 1회를 지시한다 (`model:` 은
   `config.models.triage`). 너는 그 에이전트에게 **요청 원문만** 준다 — 리포
   탐색을 허용하지 마라. 제출은 아래 형식이고 `record --phase 00` 으로 낸다.
4. 제출이 `unclear` 면 exit 9 — 사람의 판단이다. 3지선다를 그대로 제시하고
   사람이 고른 값을 `decided_by: "user"` 로 다시 낸다.
5. 확정되면 `state.profile` 이 채워지고, `docs` 면 `state.contract.mode` 가
   `no_contract` 가 된다. 01 지시문이 바로 온다.

## 제출 형식

```json
{"profile": "docs|fix|small|normal|unclear",
 "expected_paths": ["요청 원문의 부분문자열인 경로"],
 "touches_source": false,
 "reasons": ["…"]}
```

- `profile` 은 어휘 넷 중 하나다. 밖이면 exit 8
- `expected_paths` 각각은 요청 원문의 **부분문자열**이어야 한다(공백만
  정규화). 없는 경로를 지어내면 exit 8 — 01 의 `source_quote` 와 같은 손잡이다
- `profile` 이 `docs` 면 `expected_paths` 전부가 docs glob(`config.reviewers`
  중 `only_when_no_source_change` 리뷰어의 `when`) 안이어야 하고 최소 하나
  있어야 한다. 하네스 소스를 문서라 부르는 것을 기계가 막는다
- `touches_source` 는 자진신고다. 기계는 형만 보고, 03·05 가 실물로 검증한다

## 금지

- **네가 프로파일을 고르지 마라.** 이유: 기계 신호가 확정하면 그것이고, 미확정이면
  지시된 모델이 낸다. 메인이 고르면 같은 요청이 세션마다 다른 레인을 탄다
- **요청에 없는 경로를 적지 마라.** 이유: 부분문자열 검사가 그것을 위조와
  구분하지 못한다
- **트리아지 에이전트에게 리포를 탐색시키지 마라.** 이유: 탐색한 맥락으로
  예측하면 요청에 없는 범위가 레인에 끌려 들어온다. 요청 원문이 전부다
- **`unclear` 를 네가 대신 정하지 마라.** 이유: exit 9 는 사람의 판단이다

## 실패 시

| 무엇 | 어떻게 |
|---|---|
| `profile` 이 어휘 밖 | exit 8 — 고쳐서 다시 낸다 |
| `expected_paths` 가 원문에 없다 | exit 8 — 원문 그대로 인용해 다시 낸다 |
| `docs` 인데 docs glob 밖 경로 | exit 8 — `small`/`normal` 로 다시 내거나 경로를 뺀다 |
| `unclear` | exit 9 — 4지선다(`docs` · `fix` · `small` · `normal`). 사람이 고른 값을 `decided_by: "user"` 로 재제출 |
| 예측이 03·05 에서 빗나감(상향) | `triage_miss` gap · `PASS_WITH_GAPS` · 07 내장 리뷰 `medium`. 03 의 docs 레인에서 소스가 바뀌었으면 exit 3 — 계약을 쓰고 `next` 로 역할 패킷을 받는다 |

**`config.triage` 의 임계값 셋과 `config.models` 의 등급 표는 전부 미검증
초기값이다.** 첫 세 런의 `00_triage.json` 과 `triage_miss` 이벤트가 그것을
검사한다 — miss 가 docs 예측에서만 나면 docs 규칙이 헐거운 것이고, 모델
호출이 매 런 나면 규칙이 너무 좁은 것이다.
