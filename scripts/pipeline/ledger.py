#!/usr/bin/env python3
"""규칙 원장 — 지적을 쌓고, 임계를 넘은 것을 승격 후보로 올린다.

원장은 **파일 하나**다(`findings.jsonl`). append-only 라서 머지 충돌이 자명하게
union 이고, 집계 파일을 따로 두지 않는다 — 수백 줄 규모라 매번 재계산하는 편이
싸고, 파생 파일을 두면 동기화 버그만 생긴다.

`taxonomy.json` 은 셋의 단일 출처다: **원장 어휘 · 승격 목적지 · 리뷰 범위.**
그래서 이 파일이 손상되면 셋이 동시에 조용히 틀어지고, `lint-phases` 와
`doctor` 가 그것을 잡는다.

이 모듈은 **읽고 쓸 뿐 판단하지 않는다.** 어느 규칙을 실제로 승격할지는 07 의
일이고(05 는 `staged` 까지), 중복·충돌 판정은 사람과 모델의 몫이다.
"""

import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402
import state as st  # noqa: E402
import verdict  # noqa: E402

LEDGER_DIR_REL = "docs/harness/pipeline/ledger"
TAXONOMY_REL = LEDGER_DIR_REL + "/taxonomy.json"
FINDINGS_REL = LEDGER_DIR_REL + "/findings.jsonl"
CHANGELOG_REL = LEDGER_DIR_REL + "/rules_changelog.md"
# 열린 `deferred` 의 경로별 뷰. **원장의 파생이지 출처가 아니다** — 08 이 매 런
# 통째로 다시 만든다 (ADR-H051 결정 3). 머리말의 "집계 파일을 두지 않는다" 와
# 긴장하는 자리라 손으로 고치지 않고 늘 재생성한다.
DEFERRED_REL = LEDGER_DIR_REL + "/deferred.md"
NO_PATH = "(경로 미기재)"

# 승격 목적지의 어휘. `none` 은 승격하지 않는 것(에스컬레이션 전용)이다.
ENFORCEABLE = ("lint", "check", "prose", "none")
# 기계가 막을 수 있는 것들. 이 목적지를 가진 category 를 산문으로 승격하면 거부한다.
MACHINE_ENFORCED = ("lint", "check")

STATUS = ("active", "proposed", "retired", "escalate_only", "unpromotable")
# 승격 자체가 성립하지 않는 상태. 임계를 넘어도 후보가 되지 않는다.
NEVER_PROMOTE = ("escalate_only", "unpromotable", "retired")

# `false_positive` — 리뷰어가 낸 지적이 **틀렸다**고 메인이 확인한 것 (ADR-H050).
# 파일럿 140건에 이 축이 없어 오탐이 `deferred` 로 남았고, 승격 집계가 그것을
# "반복되는 미해결" 로 학습했다. 리뷰어 품질을 세는 유일한 축이라 08 이
# 리뷰어별로 센다 (`by_reporter`).
RESOLUTIONS = ("repaired", "deferred", "dropped_by_enforcement", "warn_only",
               "false_positive")
SOURCES = ("reviewer", "code-review", "external", "human", "contract-trace")

# (누적 횟수, 최소 distinct_runs). **여섯 숫자 전부 미검증 상속값이다** —
# 원본에서 왔고 이 리포에서 재본 적이 없다. 승격이 한 번도 없으면 임계가
# 높은 것이고, 매 런 발생하면 낮은 것이다.
THRESHOLDS = {"critical": (2, 2), "major": (3, 2), "minor": (5, 3)}

# 위 여섯 숫자와 승격의 축을 **판정할 시한**이다 (ADR-H033).
#
# 여기 원래 적혀 있던 것은 *"첫 세 런의 원장이 이 값을 검사한다"* 였다.
# 그런데 그 약속에 기계가 읽는 시한이 없어서 `distinct_runs` 가 6 이 될
# 때까지 아무도 판정하지 않았다 — **지나간 것조차 몰랐다.** ADR-H026 이
# `in_baseline` 에 대해 적은 *"시한이 있었던 것이 아니라 이미 지났다"* 와
# 같은 모양이다.
#
# **단위가 `distinct_runs` 인 것이 중요하다** — 임계 판정이 쓰는 축과 같아야
# "임계가 높다" 와 "표본이 모자라다" 가 섞이지 않는다. 달력의 런 수와는
# 다르다(`distinct_runs` 는 지적을 0건 낸 런을 안 센다).
#
# **게이트가 아니다.** 시한이 지나도 종료 코드를 바꾸지 않는다 — 보고서와
# `promote --scan` 이 찍기만 한다. 그때 무엇을 보고 어떻게 가를지는 ADR-H033
# 에 미리 적혀 있다.
PROMOTION_VERDICT_AT_RUNS = 9

# 승격 집계에서 빼는 resolution. baseline 기간(§E6)의 관측은 오탐률을 아직
# 모르는 상태의 것이라 학습 근거가 될 수 없다.
EXCLUDED_FROM_COUNT = ("warn_only", "false_positive")

_SEVERITY_RANK = {"minor": 0, "major": 1, "critical": 2}

# 카테고리 코드의 형태. 글롭처럼 보이는 코드를 새로 만들지 못하게 한다 (M39).
_CODE_SHAPE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 승격 축의 슬러그 형태와, 그것을 줄 수 있는 생산자 (ADR-H034 · ADR-H035).
#
# **신뢰 경계다.** 슬러그를 아무나 주면 무관한 지적이 한 버킷에 뭉치고,
# 승격은 "무엇이 반복되는가" 를 묻는 장치라 그 오염이 곧바로 규칙이 된다.
#
# **C4 는 `contract-trace` 하나만 받았고 C5 가 셋으로 넓혔다. 그러면서
# 보증의 종류가 바뀌었다 — 약해졌고, 그 사실을 여기 적는다.**
# `trace_contract.CATEGORY` 는 **코드에서** 닫혀 생산자가 다른 값을 *낼 수
# 없다.* `taxonomy.json` 의 `slugs` 는 **데이터에서** 닫히고 검사로
# 강제되어, 생산자는 무엇이든 내고 *exit 8 을 받는다.* 즉 슬러그의 보증은
# 이제 `category` 가 M46 이후 갖고 있던 것과 정확히 같다 — 더 강하지도
# 약하지도 않다. 남는 잔여 위험은 **어휘 안에서 틀린 것을 고르는 것**이고,
# 그것은 제거되지 않고 완화만 된다 (ADR-H035 한계 절).
#
# `external`(봇 요약)과 `human` 은 안 넓힌다 — 둘 다 봉투가 어휘를 찍어 준
# 적이 없다. 그쪽 슬러그는 **거부가 아니라 폴백**이다: 행 자체는 정상 관측이다.
_SLUG_SOURCES = ("contract-trace", "reviewer", "code-review")
_SLUG_SHAPE = re.compile(r"^[a-z][a-z0-9_]*$")

SEED_TAXONOMY = {
    "version": 1,
    "_note": ("스택 비종속 코드만 시드로 배포한다. 프로젝트가 코드를 **추가**하는 "
              "것은 자유이고, doctor 와 lint-phases 가 유니크·어휘·rule 참조 "
              "실재를 검증한다. status 가 active 이고 enforceable 이 prose 가 "
              "아닌 것만 05 의 '검토 제외' 목록에 들어간다 — 기계 강제 규칙이 "
              "늘수록 05 가 자동으로 싸지고 좁아진다. "
              "`slugs` 는 승격 축의 통제 어휘다 (ADR-H035): 선언한 카테고리는 "
              "그중 하나를 **필수로** 받고, 안 선언한 카테고리는 면제다. "
              "**카테고리를 가로지르는 슬러그 중복은 허용한다** — rule_key 가 "
              "category 를 포함하므로 다른 규칙이고, 금지하면 없는 제약이 "
              "된다. 승격 못 하는 카테고리는 슬러그를 선언할 수 없다."),
    "categories": [
        {"code": "BOUNDARY_VIOLATION", "enforceable": "lint",
         "rule": "no-restricted-imports", "status": "active",
         "note": "레이어 경계를 넘는 import"},
        {"code": "NAMING", "enforceable": "lint",
         "rule": "naming-convention", "status": "active",
         "note": "네이밍 규약 위반"},
        {"code": "RESPONSE_SHAPE", "enforceable": "lint",
         "rule": "response-shape", "status": "proposed",
         "note": "응답 형태의 일관성. 어휘만 두고 규칙은 아직 없다"},
        {"code": "INPUT_VALIDATION", "enforceable": "lint",
         "rule": "input-validation", "status": "proposed",
         "note": "경계에서의 입력 검증 누락"},
        {"code": "AUTHZ_MISSING_RULE", "enforceable": "prose", "status": "active",
         "note": "인가 규칙 누락. 캐치올 위치는 기계가 판정하지 못한다"},
        {"code": "MIG_DESTRUCTIVE", "enforceable": "check", "status": "active",
         "rule": "migration", "note": "되돌릴 수 없는 스키마 변경"},
        {"code": "MIG_MISSING", "enforceable": "check", "status": "proposed",
         "rule": "migration", "note": "스키마 변경에 마이그레이션이 없다"},
        {"code": "TX_BOUNDARY", "enforceable": "prose", "status": "active",
         "note": "트랜잭션 경계"},
        {"code": "CONCURRENCY", "enforceable": "prose", "status": "active",
         "note": "동시성·락 순서"},
        {"code": "TEST_MISSING_FAILURE_PATH", "enforceable": "prose",
         "status": "active", "note": "실패 경로가 테스트되지 않았다",
         # test-quality-reviewer 의 「볼 것」 다섯 행에서 왔다. 「모킹 남용」과
         # 「사후 작성의 흔적」은 **한 슬러그로 합쳤다** — 둘 다 "테스트가
         # 구현을 베꼈다" 는 같은 결함이고, 가르면 한 규칙이 두 버킷으로
         # 갈려 임계에 못 닿는다.
         "slugs": [
             {"slug": "nothing_locked",
              "note": "구현을 되돌려도 이 테스트가 빨간불이 안 된다"},
             {"slug": "asserts_implementation",
              "note": "단언이 구현의 내부 구조(호출 순서·내부 상태)를 "
                      "따라간다. 모킹이 너무 깊은 것도 여기다"},
             {"slug": "boundary_untested",
              "note": "0 · 빈 값 · 상한 · 하나 넘김이 안 잠겼다"},
             {"slug": "failure_path_untested",
              "note": "계약이 말한 예외가 테스트되지 않았다"}]},
        {"code": "CONTRACT_DEFECT", "enforceable": "none",
         "status": "escalate_only",
         "note": "계약은 메인 단독 소유다. 수리가 아니라 에스컬레이션이고 "
                 "자동 승격 대상이 아니다"},
        {"code": "DOC_CODE_DRIFT", "enforceable": "prose", "status": "active",
         "note": ("문서·주석이 코드가 말하는 사실과 어긋난다. 기계가 못 "
                  "막는다 — 어느 문장이 어느 상수를 참칭하는지는 판정이다"),
         # docs-reviewer 의 「볼 것」 다섯 행과 1:1 이다. **그 스킬만 쓰는
         # 어휘가 아니다** — 원장 실측에서 이 카테고리를 낸 것은 arch 7 ·
         # data 5 · sec 3 이고 docs 는 0 이다(`only_when_no_source_change` 라
         # 여섯 런에서 한 번도 안 켜졌다). 카테고리가 스킬을 가로지르므로
         # 어휘의 거처는 스킬 파일이 아니라 여기다.
         "slugs": [
             {"slug": "doc_contradicts_code",
              "note": "문서·주석이 코드가 하는 일과 다른 것을 말한다"},
             {"slug": "same_fact_two_places",
              "note": "같은 사실이 두 곳에 각각 적혀 갈라졌다"},
             {"slug": "estimate_as_measured",
              "note": "재지 않은 값이 실측처럼 적혔다"},
             {"slug": "skip_as_pass",
              "note": "수행하지 않은 것이 통과한 것처럼 적혔다"},
             {"slug": "dead_reference",
              "note": "링크·경로·심볼 이름이 실재하지 않는다"}]},
        {"code": "CONTRACT_MISMATCH", "enforceable": "prose",
         "status": "unpromotable",
         # general-reviewer 의 기본 어휘다 (ADR-H043). 인스턴스 결함이지 규칙이
         # 아니라 승격하지 않는다. OTHER 와 가르는 이유는 보고서와
         # `escaped_05` 대조에서 이름이 필요해서다.
         "note": ("구현이 계약·플랜이 말한 동작과 다르다. 인스턴스 결함이지 "
                  "규칙이 아니라 승격하지 않는다 — OTHER 와 가르는 이유는 "
                  "보고서와 escaped_05 대조에서 이름이 필요해서다")},
        {"code": "OTHER", "enforceable": "prose", "status": "unpromotable",
         "note": ("분류되지 않은 것. **글롭이 아니라 문자열 그대로의 코드다** "
                  "— categories() 가 만드는 dict 의 키이고 append() 는 "
                  "`code not in known` 으로만 본다. 받아 두되 승격하지 않는다")},
        {"code": "other/*", "enforceable": "prose", "status": "retired",
         "note": ("OTHER 로 갈렸다 (M39). 원장의 옛 줄이 이 코드를 쓰므로 "
                  "지우지 않는다 — append-only 원장의 과거를 읽을 수 있게 "
                  "남긴다. retired 는 이미 NEVER_PROMOTE 라 거동이 안 바뀐다")},
    ],
}

CHANGELOG_HEADER = """# 규칙 승격 이력

> 이 파일은 `promote` 가 쓴다. 사람이 직접 적지 않는다.
> 승격은 **07 에서 런당 한 번**이고, 05 는 `staged` 까지만 만든다.

각 줄이 담는 것 — 날짜 / `run_id` / `rule_id` / category / `enforceable` /
근거 런과 횟수 / 중복·충돌 판정 / 실제 조치 / 베이스라인 diff / 철회 사유.

**`lint` 승격은 베이스라인 diff 를 반드시 남긴다.** 없으면 "규칙은 추가했는데
아무것도 안 막는다"가 조용히 통과한다.

| 날짜 | run_id | rule_id | category | enforceable | 근거 | 판정 | 조치 | 베이스라인 diff | 철회 사유 |
|---|---|---|---|---|---|---|---|---|---|
"""


# --------------------------------------------------------------------- 시드

def seed(root):
    """세 파일을 만든다. **이미 있으면 손대지 않는다** — 원장은 append-only 다."""
    root = Path(root)
    d = root / LEDGER_DIR_REL
    d.mkdir(parents=True, exist_ok=True)

    tax = root / TAXONOMY_REL
    if not tax.exists():
        tax.write_text(json.dumps(SEED_TAXONOMY, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
    findings = root / FINDINGS_REL
    if not findings.exists():
        findings.write_text("", encoding="utf-8")
    log = root / CHANGELOG_REL
    if not log.exists():
        log.write_text(CHANGELOG_HEADER, encoding="utf-8")
    return d


# ------------------------------------------------------------------ taxonomy

def load_taxonomy(root):
    return harness._read_json(Path(root) / TAXONOMY_REL)


def categories(root):
    """{code: category}. 파일이 없으면 빈 dict — 없는 것과 빈 것을 구분한다."""
    try:
        data = load_taxonomy(root)
    except (OSError, ValueError):
        return {}
    return {c.get("code"): c for c in data.get("categories") or []}


def slug_vocabulary(root, code):
    """그 카테고리의 통제 어휘 `[{slug, note}]`. 선언이 없으면 빈 목록.

    **없는 것과 빈 것을 구분하지 않는 것이 여기서는 맞다** — 스키마가
    `slugs: []` 를 이미 거부하므로(선언했는데 비면 "선언 안 했다" 와 같은
    침묵이 된다) 빈 목록이 뜻하는 것은 언제나 "이 카테고리는 어휘를 안
    선언했다" 하나다. 호출부가 분기를 안 써도 되게 한다.
    """
    return list((categories(root).get(code) or {}).get("slugs") or [])


def excluded_categories(root):
    """05 의 "검토 제외" 목록. `active` + 기계 강제 가능한 것만.

    이 목록이 길어질수록 리뷰어 프롬프트가 짧아지고 05 가 싸진다 — 규칙 승격의
    복리가 실현되는 지점이다. 반대로 `prose` 는 기계가 못 막으므로 여기 넣으면
    아무도 안 보는 규칙이 된다.
    """
    return [code for code, c in sorted(categories(root).items())
            if c.get("status") == "active"
            and c.get("enforceable") in MACHINE_ENFORCED]


def validate_taxonomy(data):
    """[오류 문자열]. 빈 리스트면 통과다."""
    errors = []
    cats = data.get("categories")
    if not isinstance(cats, list) or not cats:
        return ["categories 가 비어 있거나 배열이 아니다"]

    codes = [c.get("code") for c in cats]
    if len(codes) != len(set(codes)):
        dup = sorted({c for c in codes if codes.count(c) > 1})
        errors.append("코드가 유니크하지 않다: %s" % ", ".join(map(str, dup)))

    for c in cats:
        code = c.get("code")
        if not code:
            errors.append("code 가 없는 항목이 있다")
            continue
        # **"이 코드는 글롭인가" 가 제출 1회를 무르게 한 질문이다** (M39).
        # 규칙이 그 질문에 영구히 답한다. `retired` 는 면제한다 — 원장의 옛
        # 줄이 그 코드를 쓰고, append-only 원장의 과거를 읽을 수 있어야 한다.
        if c.get("status") != "retired" and not _CODE_SHAPE.match(code):
            errors.append("%s 의 코드 형태가 규약 밖이다 — 대문자·숫자·밑줄만 "
                          "쓴다(글롭처럼 보이는 코드는 글롭이 아니다)" % code)
        enf = c.get("enforceable")
        if enf not in ENFORCEABLE:
            errors.append("%s 의 enforceable 이 어휘 밖이다: %r (%s)"
                          % (code, enf, ", ".join(ENFORCEABLE)))
        stat = c.get("status")
        if stat not in STATUS:
            errors.append("%s 의 status 가 어휘 밖이다: %r (%s)"
                          % (code, stat, ", ".join(STATUS)))
        # 기계 강제 목적지인데 어느 규칙인지 안 적으면 승격이 갈 곳을 모른다.
        if enf in MACHINE_ENFORCED and not c.get("rule"):
            errors.append("%s 는 enforceable=%s 인데 rule 참조가 없다 — "
                          "어디로 승격할지 아무도 모른다" % (code, enf))
        errors += _validate_slugs(code, c)
    return errors


def _validate_slugs(code, c):
    """승격 축의 통제 어휘 검사 (ADR-H035). [오류 문자열].

    **형태는 `_SLUG_SHAPE` 그 객체를 쓴다.** 정규식을 여기서 다시 쓰면
    taxonomy 가 선언한 슬러그를 `_accept_slug` 가 exit 8 로 튕기고, **선언과
    수용이 갈렸다는 사실이 어디에도 안 드러난다.**
    """
    if "slugs" not in c:
        return []                       # 선언 안 함 — 그 카테고리는 면제다
    errors = []
    slugs = c.get("slugs")
    if not isinstance(slugs, list):
        return ["%s 의 slugs 가 배열이 아니다: %r" % (code, slugs)]
    if not slugs:
        return ["%s 의 slugs 가 비었다 — 어휘를 두거나 키를 안 두거나 "
                "둘 중 하나다. 빈 선언은 '선언 안 했다' 와 같은 침묵이 된다"
                % code]
    # 승격 못 하는 카테고리의 어휘는 소비자가 없다. **이 한 줄이 「어휘가
    # 선언된 곳에서만 필수」를 구조로 만든다** — OTHER·CONTRACT_DEFECT·
    # other/* 의 면제가 코드의 목록이 아니라 스키마에서 나온다.
    if c.get("status") in NEVER_PROMOTE:
        errors.append("%s 는 status=%s 라 승격되지 않는다 — slugs 를 선언할 "
                      "수 없다. 어휘를 둘 곳은 승격 대상 카테고리뿐이다"
                      % (code, c.get("status")))
    seen = []
    for s in slugs:
        if not isinstance(s, dict):
            errors.append("%s 의 슬러그 항목이 객체가 아니다: %r" % (code, s))
            continue
        slug = s.get("slug")
        if not isinstance(slug, str) or not _SLUG_SHAPE.match(slug):
            errors.append("%s 의 슬러그 형태가 규약 밖이다: %r — %s 여야 한다"
                          % (code, slug, _SLUG_SHAPE.pattern))
            continue
        # note 가 봉투의 유일한 화물이다. 없으면 그 카테고리를 내는 리뷰어가
        # 두 슬러그를 언제 가르는지 모른 채 고른다.
        if not (s.get("note") or "").strip():
            errors.append("%s.%s 에 note 가 없다 — 봉투가 실을 것이 없으면 "
                          "리뷰어가 뜻을 모른 채 고른다" % (code, slug))
        seen.append(slug)
    dup = sorted({s for s in seen if seen.count(s) > 1})
    if dup:
        errors.append("%s 의 슬러그가 유니크하지 않다: %s"
                      % (code, ", ".join(dup)))
    return errors


def check_destination(root, code, enforceable):
    """산문 승격이 허용되는가. 허용되면 None, 아니면 ValueError.

    **기계로 막을 수 있는 규칙의 산문 승격은 거부한다** (exit 8). 이것이
    `instruction_slot_budget` 과 맞물려, 그 예산이 진짜 기계가 못 잡는 규칙에만
    쓰이게 만든다.
    """
    cat = categories(root).get(code)
    if cat is None:
        raise ValueError("taxonomy 에 없는 category 다: %r" % code)
    if enforceable == "prose" and cat.get("enforceable") in MACHINE_ENFORCED:
        raise ValueError(
            "%s 는 %s 로 막을 수 있는데 산문으로 승격하려 한다 — 산문은 "
            "영구 비용이고 기계 규칙은 런타임 비용이 0 이다"
            % (code, cat.get("enforceable")))
    return None


# ------------------------------------------------------------------ findings

def finding_key(f):
    """`sha1(category | target_role | normalize_title)`.

    **경로를 키에 넣지 않는다** — "동일 유형"은 파일을 가로질러야 의미가 있다.
    01 의 판정과 같은 함수를 쓴다. 두 곳에서 키가 갈라지면 05 의 단조성과
    승격 집계가 서로 다른 것을 세게 된다.
    """
    return verdict.finding_key(f)


def rule_key(f):
    """`sha1(category | target_role | rule_slug)`. **승격 집계의 축이다.**

    `finding_key` 와 같은 이유로 **01 과 같은 함수를 쓴다**. 슬러그가 없으면
    `finding_key` 와 같은 값이라, 슬러그가 한 줄도 없는 옛 원장의 집계가
    변하지 않는다 (ADR-H034).
    """
    return verdict.rule_key(f)


def _accept_slug(f):
    """행에 실을 `rule_slug`. 못 실으면 None.

    **두 종류의 거절을 가른다.** 신뢰 경계 밖(`external`·`human`)이 준
    슬러그는 예상된 입력이라 조용히 버리고 폴백한다 — 그 행은 여전히 정상
    관측이다. 반면 형태가 어긋난 슬러그는 생산자가 **스스로 깨진 것**이라
    조용히 받지 않는다.

    **어휘 대조는 여기서 안 한다** (2층 규율 · M46). 어휘는 제출자 층
    (`review.check` · `_record_07`)이 막아 위반한 그 제출자가 `attempts`
    예산과 강등 경로를 타게 하고, 여기는 05 밖 경로의 **마지막 방어선**이라
    신뢰 경계와 형태만 본다. 여기서 어휘를 강제하면 `contract-trace` 의
    `out_of_contract` 를 `NAMING.slugs` 에 적어야 하고, 두 생산자의 어휘가
    한 배열에서 섞인다.
    """
    slug = f.get("rule_slug")
    if slug is None or f.get("source") not in _SLUG_SOURCES:
        return None
    if not isinstance(slug, str) or not _SLUG_SHAPE.match(slug):
        raise ValueError("rule_slug 의 형태가 어긋났다: %r — %s 여야 한다. "
                         "생산자 안의 닫힌 집합이라 이것은 버그다"
                         % (slug, _SLUG_SHAPE.pattern))
    return slug


def append(root, run_id, phase, findings):
    """원장에 줄을 더한다. 반환: 쓴 줄 수.

    어휘 밖의 category·resolution 은 **조용히 받지 않는다.** 받으면 승격 집계가
    아무도 모르는 축으로 갈라지고, 그 사실이 어디에도 드러나지 않는다.

    `rule_slug` 의 신뢰 경계도 여기서 쥔다 (`_accept_slug` · ADR-H034).
    """
    root = Path(root)
    known = categories(root)
    rows = []
    for f in findings or []:
        code = f.get("category")
        if code not in known:
            raise ValueError("taxonomy 에 없는 category 다: %r — 새 코드를 쓰려면 "
                             "%s 에 먼저 넣는다" % (code, TAXONOMY_REL))
        res = f.get("resolution")
        if res not in RESOLUTIONS:
            raise ValueError("resolution 이 어휘 밖이다: %r (%s)"
                             % (res, ", ".join(RESOLUTIONS)))
        src = f.get("source")
        if src is not None and src not in SOURCES:
            raise ValueError("source 가 어휘 밖이다: %r (%s)"
                             % (src, ", ".join(SOURCES)))
        slug = _accept_slug(f)
        row = {
            "run_id": run_id,
            "phase": phase,
            "finding_key": finding_key(f),
            "category": code,
            "severity": f.get("severity"),
            "target_role": f.get("target_role"),
            "title_norm": verdict.normalize_ws(f.get("title") or ""),
            "resolution": res,
            "repaired_by": f.get("repaired_by"),
            "reported_by": f.get("reported_by") or [],
            "source": src,
            "ts": st.stamp(),
        }
        # **슬러그는 받았을 때만 적는다.** 없는 것을 `null` 로 적으면 "안 줬다"
        # 와 "줬는데 안 받았다" 가 같은 모양이 된다. `rule_key` 는 항상 적어
        # 행이 자기 축을 스스로 말하게 한다 — 옛 줄에는 없으므로 읽는 쪽이
        # `finding_key` 로 낙하한다.
        if slug is not None:
            row["rule_slug"] = slug
        # **경로는 받았을 때만 적는다** (ADR-H051 결정 3). 이월 표가 경로별로
        # 모이는 근거이고, 옛 행은 이 키가 없어 `(경로 미기재)` 버킷이다.
        if f.get("path"):
            row["path"] = str(f["path"]).replace("\\", "/")
        row["rule_key"] = rule_key(dict(f, rule_slug=slug))
        rows.append(row)

    if not rows:
        return 0
    path = root / FINDINGS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def supersede(root, run_id, phase, keys, **updates):
    """이미 쌓인 관측의 상태를 **덧붙여** 갱신한다. 반환: 쓴 줄 수.

    줄을 고쳐 쓰지 않는다 — 같은 신원의 새 줄을 append 하고 `observations`
    가 마지막을 채택한다. 그래서 append-only 의 union-머지 성질이 유지된다.

    `keys` 중 이 (run_id, phase) 에 **실재하지 않는** 것은 조용히 만들지
    않는다. 없는 것을 갱신하는 것은 갱신이 아니라 날조다.
    """
    prior = {}
    for r in read_all(root):
        if r.get("_corrupt"):
            continue
        if r.get("run_id") == run_id and r.get("phase") == phase:
            prior[r.get("finding_key")] = r
    rows = []
    for key in keys or []:
        base = prior.get(key)
        if base is None:
            continue
        row = dict(base)
        row.update(updates)
        row["ts"] = st.stamp()
        rows.append(row)
    if not rows:
        return 0
    for row in rows:
        if row.get("resolution") not in RESOLUTIONS:
            raise ValueError("resolution 이 어휘 밖이다: %r (%s)"
                             % (row.get("resolution"), ", ".join(RESOLUTIONS)))
    path = Path(root) / FINDINGS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def read_all(root):
    """원장 전부. 손상된 줄은 **건너뛰지 않고 드러낸다** — 조용히 줄면 집계가 틀린다."""
    path = Path(root) / FINDINGS_REL
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"_corrupt": True, "_line": i})
    return out


def observations(root):
    """원장을 **관측 단위**로 접은 것. 집계는 전부 이것을 쓴다.

    행의 신원은 `(run_id, phase, finding_key)` 이고, 같은 신원의 뒷줄은 새
    발생이 아니라 **승계**다 — 가변 필드(`resolution`·`repaired_by`)는 마지막
    줄이 이기고 `severity` 는 최대다.

    **왜 접기를 읽기에 두는가.** 원장 파일은 append-only 다(`open("a")`). 그
    성질이 값을 하는 이유가 있다 — tracked 파일이라 브랜치 머지가 자명하게
    union 이고, 줄을 제자리 수정하면 동시 런의 lost-update 가 생긴다. 그래서
    가변성을 쓰기가 아니라 읽기로 옮긴다. `read_all` 은 원문 감사용으로
    그대로 남고 **한 줄도 사라지지 않는다.**

    `phase` 를 신원에 넣는 것이 선택이다. `(run_id, finding_key)` 로만 접으면
    05 와 07 의 관측이 합쳐져 `count` 가 `distinct_runs` 를 완전히 흡수하고,
    임계 여섯 숫자 중 셋이 죽은 값이 된다. M30 이 지적한 것은 라운드 중복이지
    페이즈 간 합산이 아니다.

    **머지 시 순서 비결정성**: 두 브랜치가 같은 신원에 다른 resolution 을
    덧붙이면 접기 결과가 인터리브 순서에 달린다. `ts` 로 정렬해 결정론으로
    만들되, "뒤 `ts` 가 이긴다" 는 임의 선택임을 적어 둔다.
    """
    folded = {}
    order = []
    rows = [r for r in read_all(root) if not r.get("_corrupt")]
    rows.sort(key=lambda r: r.get("ts") or "")
    for row in rows:
        ident = (row.get("run_id"), row.get("phase"), row.get("finding_key"))
        prev = folded.get(ident)
        if prev is None:
            folded[ident] = dict(row, first_seen=row.get("ts"),
                                 last_seen=row.get("ts"), superseded=0)
            order.append(ident)
            continue
        rank = _SEVERITY_RANK.get(row.get("severity"), -1)
        keep_sev = prev.get("severity")
        if rank > _SEVERITY_RANK.get(keep_sev, -1):
            keep_sev = row.get("severity")
        first = prev.get("first_seen")
        n = prev.get("superseded", 0) + 1
        folded[ident] = dict(row, severity=keep_sev, first_seen=first,
                             last_seen=row.get("ts"), superseded=n)
    return [folded[i] for i in order]


def open_deferred(root):
    """열린 `deferred` 를 경로별로. [{"path", "rows"}] — `(경로 미기재)` 는 맨 뒤."""
    groups, order = {}, []
    for row in observations(root):
        if row.get("resolution") != "deferred":
            continue
        p = row.get("path") or NO_PATH
        if p not in groups:
            groups[p] = []
            order.append(p)
        groups[p].append(row)
    order = [p for p in order if p != NO_PATH] + ([NO_PATH] if NO_PATH in groups else [])
    return [{"path": p, "rows": groups[p]} for p in order]


def write_deferred(root):
    """`deferred.md` 를 **통째로** 다시 쓴다. 반환: 경로."""
    groups = open_deferred(root)
    total = sum(len(g["rows"]) for g in groups)
    lines = ["# 이월 미해결 (deferred)", "",
             "> 원장(`findings.jsonl`)의 **파생 뷰**이지 출처가 아니다. 08 이 매 런 "
             "통째로 다시 만든다 — 손으로 고치지 않는다 (ADR-H051). 옛 행은 `path` 가 "
             "없어 `%s` 한 버킷이다. 00 봉투가 요청 경로와 겹치는 건수를 표기한다."
             % NO_PATH, "",
             "열린 `deferred` **%d건** · 경로 %d개" % (total, len(groups)), ""]
    for g in groups:
        lines += ["## `%s` — %d건" % (g["path"], len(g["rows"])), "",
                  "| run_id | severity | category | 제목 | 리뷰어 |",
                  "|---|---|---|---|---|"]
        lines += ["| `%s` | %s | %s | %s | %s |"
                  % (r.get("run_id"), r.get("severity"), r.get("category"),
                     r.get("title_norm"), ", ".join(r.get("reported_by") or []))
                  for r in g["rows"]]
        lines.append("")
    path = Path(root) / DEFERRED_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return path


def _prefix_match(a, b):
    a, b = a.strip("/"), b.strip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def deferred_overlap(root, request_paths):
    """요청 경로와 접두로 겹치는 열린 `deferred`. {"count", "paths"}."""
    hits, count = [], 0
    for g in open_deferred(root):
        if g["path"] == NO_PATH:
            continue
        if any(_prefix_match(g["path"], q) for q in request_paths or []):
            hits.append(g["path"])
            count += len(g["rows"])
    return {"count": count, "paths": hits}


def distinct_runs(root):
    """원장이 본 런의 수.

    **한계를 적어 둔다**: 지적을 한 건도 내지 않은 런은 여기 세어지지 않는다.
    baseline 기간(§E6)이 재려는 것이 `untested_contract_item` 의 **오탐률**이므로,
    관측이 0건인 런은 그 비율에 대해 아무 증거도 주지 않는다 — 그래서 세지 않는
    것이 맞다. 다만 "3런"이 달력의 3런이 아니라 **관측이 있었던 3런**이라는 뜻이다.
    """
    return len({r.get("run_id") for r in read_all(root)
                if not r.get("_corrupt") and r.get("run_id")})


def by_reporter(root):
    """리뷰어(`reported_by`)별 resolution 집계 (ADR-H050). **승격과 무관한 관측이다.**

    `repaired / deferred / false_positive` 의 비율이 리뷰어 품질의 첫 실측이다 —
    파일럿에서는 오탐 축이 없어 "리뷰어 지적 62건 중 27% 수리" 까지만 셌고
    나머지가 틀린 지적인지 미룬 지적인지 가를 수 없었다. 여러 리뷰어가 함께
    낸 지적은 각자에게 1 로 센다.
    """
    buckets = {}
    for row in observations(root):
        res = row.get("resolution")
        for code in row.get("reported_by") or ["?"]:
            b = buckets.setdefault(code, {"reporter": code, "total": 0})
            b["total"] += 1
            b[res] = b.get(res, 0) + 1
    out = []
    for b in buckets.values():
        for res in RESOLUTIONS:
            b.setdefault(res, 0)
        out.append(b)
    out.sort(key=lambda b: (-b["total"], b["reporter"]))
    return out


def verdict_deadline(root):
    """승격 임계·축을 판정할 시한 (ADR-H033). **게이트가 아니라 표시다.**

    `seen` 의 단위는 `distinct_runs` 이고 그 한계를 그대로 물려받는다 —
    지적을 0건 낸 런은 세어지지 않으므로 달력의 런 수보다 작을 수 있다.
    시한이 지나도 `remaining` 을 음수로 적지 않는다: "얼마나 남았나" 와
    "얼마나 지났나" 는 다른 질문이고 뒤엣것은 `seen` 과 `at` 이 답한다.
    """
    seen = distinct_runs(root)
    return {"at": PROMOTION_VERDICT_AT_RUNS, "seen": seen,
            "remaining": max(0, PROMOTION_VERDICT_AT_RUNS - seen),
            "due": seen >= PROMOTION_VERDICT_AT_RUNS}


def in_baseline(root, baseline_runs):
    """baseline 기간 안인가 — 안이면 오탐이 잦은 검사를 `warn_only` 로 낮춘다."""
    return distinct_runs(root) < (baseline_runs or 0)


# ------------------------------------------------------------------- 승격 계산

def stage_promotions(root):
    """임계를 넘은 **규칙**(`rule_key`)을 후보로 올린다. **05 는 여기까지다.**

    반환: {"candidates": [...], "held": [...], "distinct_runs": n,
           "thresholds": {...}}

    `held` 는 **누적은 넘었는데 `distinct_runs` 에서 막힌 것**이다. 조용히
    빠뜨리면 "임계가 높다"와 "런이 모자라다"가 같은 침묵이 된다.

    **축은 `finding_key` 가 아니라 `rule_key` 다** (ADR-H034). 승격이 배우는
    것은 "이 심볼을 고쳐라" 가 아니라 "이 규칙이 반복된다" 이기 때문이다.
    버킷은 자기가 접은 `finding_keys` 를 전부 싣는다 — 대표 하나만 실으면
    런마다 다른 인스턴스가 신원이 되어 같은 규칙이 두 승격 행으로 갈라진다.
    `observations` 의 접기 신원은 **그대로 `finding_key`** 라, 한 런이 낸
    여섯 건은 여전히 6관측이고 그래서 `count` 가 6 이 된다.
    """
    cats = categories(root)
    buckets = {}
    for row in observations(root):
        if row.get("resolution") in EXCLUDED_FROM_COUNT:
            continue
        code = row.get("category")
        cat = cats.get(code) or {}
        if cat.get("status") in NEVER_PROMOTE:
            continue
        # **옛 줄에는 `rule_key` 가 없다.** 낙하가 곧 소급 무오염이다 —
        # 슬러그가 한 줄도 없던 원장은 변경 전과 정확히 같은 버킷을 만든다.
        fk = row.get("finding_key")
        key = row.get("rule_key") or fk
        b = buckets.setdefault(key, {
            "rule_key": key, "rule_slug": row.get("rule_slug"),
            # 접힌 버킷에는 대표 `finding_key` 를 두지 않는다 — 런마다 다른
            # 인스턴스가 신원 행세를 하면 같은 규칙이 두 승격 행으로 갈라진다.
            # 폴백 버킷(축이 곧 finding_key)에서만 채운다.
            "finding_key": fk if key == fk else None,
            "finding_keys": set(),
            "category": code,
            "target_role": row.get("target_role"),
            "title_norm": row.get("title_norm"),
            "enforceable": cat.get("enforceable"),
            "rule": cat.get("rule"),
            "count": 0, "runs": set(), "severity": "minor"})
        b["count"] += 1
        if fk:
            b["finding_keys"].add(fk)
        if row.get("run_id"):
            b["runs"].add(row["run_id"])
        if _SEVERITY_RANK.get(row.get("severity"), -1) > _SEVERITY_RANK[b["severity"]]:
            b["severity"] = row["severity"]

    candidates, held = [], []
    for b in buckets.values():
        need_count, need_runs = THRESHOLDS.get(b["severity"], (99, 99))
        runs = len(b["runs"])
        item = dict(b, runs=sorted(b["runs"]), distinct_runs=runs,
                    finding_keys=sorted(b["finding_keys"]),
                    needs={"count": need_count, "distinct_runs": need_runs})
        if b["count"] < need_count:
            continue
        if runs < need_runs:
            # 여러 페이즈·소스가 같은 것을 본 것은 그 런의 특성이지 학습
            # 대상이 아니다. 막았다는 사실을 드러낸다.
            item["held_because"] = ("누적 %d 로 임계를 넘었지만 distinct_runs 가 "
                                    "%d 라 %d 에 못 미친다"
                                    % (b["count"], runs, need_runs))
            held.append(item)
            continue
        candidates.append(item)

    candidates.sort(key=lambda c: (-_SEVERITY_RANK[c["severity"]], c["category"]))
    return {"candidates": candidates, "held": held,
            "by_category": _by_category(root, cats),
            "distinct_runs": distinct_runs(root),
            "verdict_deadline": verdict_deadline(root),
            "thresholds": {k: {"count": v[0], "distinct_runs": v[1]}
                           for k, v in THRESHOLDS.items()},
            "axis_note": (
                "승격 버킷의 축은 rule_key = sha1(category|target_role|"
                "rule_slug)이고, rule_slug 가 없으면 finding_key 로 낙하한다 "
                "(ADR-H034). `by_category` 는 **보고용이고 승격하지 않는다** "
                "— 카테고리가 잦은 것과 같은 규칙이 잦은 것은 다른 사실이다. "
                "통제 어휘를 쓰는 생산자(contract-trace)의 지적은 심볼이 "
                "달라도 규칙으로 접히고, 슬러그가 없는 자유 서술은 제목마다 "
                "갈려 임계에 닿지 않는다 — 뒤엣것은 결함이 아니라 '승격의 "
                "산물이 규칙' 이라는 정의의 결과다 (M39 · ADR-H026)."),
            "note": "05 는 staged 까지다. 실제 쓰기는 07 에서 런당 한 번이다."}


def _by_category(root, cats):
    """카테고리 축의 빈도. **승격하지 않는 관측이다** (M39 · ADR-H026).

    `distinct_keys` 는 **승격 축과 같은 단위**다 (`rule_key`) — 칸 이름이
    단위를 말해야 하므로 보고서도 「서로 다른 규칙」으로 읽는다. 축이 갈리면
    이 숫자와 승격 임계가 서로 다른 것을 세게 된다.


    `held` 가 "임계가 높다" 와 "런이 모자라다" 를 갈라 놓듯, 이 롤업은 셋째
    침묵 — "축이 틀렸다" — 를 갈라 놓는다. 이 리포에서 가장 자주 나는 결함이
    무엇인지 보고서가 말할 수 있게 하는 것이 전부이고, `candidates` 와
    `held` 는 한 비트도 달라지지 않는다.

    `warn_only` 와 승격 불가 상태도 **세되 따로 표시한다** — 빼면 같은 침묵을
    반복한다.
    """
    buckets = {}
    for row in observations(root):
        code = row.get("category")
        cat = cats.get(code) or {}
        b = buckets.setdefault(code, {
            "category": code, "count": 0, "runs": set(), "keys": set(),
            "excluded_from_promotion_count": 0,
            "status": cat.get("status"), "enforceable": cat.get("enforceable"),
            "promotable": cat.get("status") not in NEVER_PROMOTE})
        b["count"] += 1
        if row.get("run_id"):
            b["runs"].add(row["run_id"])
        rk = row.get("rule_key") or row.get("finding_key")
        if rk:
            b["keys"].add(rk)
        if row.get("resolution") in EXCLUDED_FROM_COUNT:
            b["excluded_from_promotion_count"] += 1
    out = []
    for b in buckets.values():
        out.append({k: v for k, v in b.items() if k not in ("runs", "keys")})
        out[-1]["distinct_runs"] = len(b["runs"])
        out[-1]["distinct_keys"] = len(b["keys"])
    out.sort(key=lambda b: (-b["count"], b["category"] or ""))
    return out
