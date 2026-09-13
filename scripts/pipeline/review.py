#!/usr/bin/env python3
"""05 의 리뷰어 라우팅과 판정.

**라우팅은 결정론이다.** `config.reviewers[].when` glob 이 변경 파일에 걸리면
그 리뷰어가 켜지고, 우선순위는 배열 순서다. 모델이 "누구를 부를까"를 판단하지
않는다 — 판단하면 같은 diff 가 런마다 다른 리뷰를 받는다.

여기서 막는 것 둘:

1. **리뷰어가 전부 실패해도 findings 는 0건이다.** 그러면 "지적이 없다"가
   "리뷰가 됐다"로 읽히고, 아무도 안 본 코드가 통과한다. 그래서 `status()` 가
   findings 개수와 **분리된** 신호를 낸다 (§E1).
2. **작성자는 리뷰어가 될 수 없다.** `config.roles[].agent` 와 겹치는 스킬은
   `validate()` 가 거부한다.

glob 매칭은 `harness.glob_any` 를 그대로 쓴다. 소유 판정과 라우팅이 서로 다른
glob 엔진을 쓰면 같은 경로가 두 곳에서 다르게 읽힌다 — 이 리포가 이미 겪은
실패다 (M11 · M17).
"""

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402
import verdict  # noqa: E402

SKILLS_REL = ".claude/skills"

DEFAULT_CAPS = {"small": 1, "normal": 3}
DEFAULT_MERGE_BELOW = 150
DEFAULT_FINDINGS_MAX = 50

# 05 가 산출하는 리뷰 파일의 이름. `code` 축약을 쓰는 것은 경로 240자 상한
# 때문이고(§E4), 리뷰어가 다섯이면 이름이 길 때 실제로 닿는다.
REVIEW_FILE = "05_review_%s.json"


def skill_path(root, skill):
    return Path(root) / SKILLS_REL / skill / "SKILL.md"


# ----------------------------------------------------------------------- 검증

def validate(root, config):
    """기동 **전에** 값싸게 잡는 것들. 반환: [오류 문자열].

    런 중간에 알게 되면 앞 페이즈에 쓴 시간이 이미 낭비된 뒤다 (§E10 첫 행).
    """
    errors = []
    reviewers = config.get("reviewers") or []
    if not reviewers:
        # 리뷰어가 0개인 것은 설정 오류일 수도, 의도일 수도 있다. 막지 않고
        # 드러낸다 — 다만 그 런의 review05.status 는 failed 다.
        return errors

    authors = {r.get("agent") for r in config.get("roles") or []}
    seen_code, seen_priority = {}, {}
    for r in reviewers:
        code, skill = r.get("code"), r.get("skill")
        if code in seen_code:
            errors.append("리뷰어 code 가 유니크하지 않다: %r" % code)
        seen_code[code] = skill
        pri = r.get("priority")
        if pri is not None and pri in seen_priority:
            errors.append("리뷰어 priority %r 가 %s 와 겹친다 — 순서가 "
                          "결정론이 아니게 된다" % (pri, seen_priority[pri]))
        seen_priority[pri] = code

        if skill in authors:
            errors.append(
                "리뷰어 %r 의 스킬 %r 이 작성자 역할과 같다 — **작성자 격리**가 "
                "깨진다. 자기 코드를 리뷰한 것은 독립 관측이 아니다" % (code, skill))
        if not skill_path(root, skill).is_file():
            errors.append("리뷰어 %r 의 스킬 파일이 없다: %s/%s/SKILL.md — "
                          "기동 전에 잡는다" % (code, SKILLS_REL, skill))
        if not (r.get("when") or []) and not r.get("when_role_owned"):
            errors.append("리뷰어 %r 에 when glob 도 when_role_owned 도 없다 — "
                          "영원히 켜지지 않는다" % code)
    return errors


# --------------------------------------------------------------------- 라우팅

def route(config, changed, profile="normal", source_globs=None):
    """변경 파일 → 켜질 리뷰어. 결정론이다.

    반환: {"reviewers":[...], "dropped":[...], "capped":bool, "cap":n,
           "profile":..., "source_changed":bool}

    `dropped` 는 **매칭됐지만 상한에 걸려 빠진** 리뷰어다. 조용히 버리면
    "그 관점은 볼 게 없었다"와 "예산이 없었다"가 같은 침묵이 된다.
    """
    changed = [c.replace("\\", "/") for c in (changed or [])]
    reviewers = sorted(config.get("reviewers") or [],
                       key=lambda r: (r.get("priority") if r.get("priority")
                                      is not None else 999))
    source_changed = _source_changed(config, changed, source_globs)

    matched = []
    roles = config.get("roles") or []
    for r in reviewers:
        if r.get("only_when_no_source_change") and source_changed:
            continue
        hits = [c for c in changed if harness.glob_any(r.get("when") or [], c)]
        if r.get("when_role_owned"):
            # **glob 이 아니라 역할 소유로 켠다** (ADR-H043). 소유 판정은
            # `_source_changed` 와 같은 술어다 — 프로젝트가 `roles[].owns` 를
            # 다른 레이아웃으로 바꿔도 따라간다. 07 이 깨끗한 런을 생략하는
            # 근거가 "05 의 gen 이 봤다" 이므로, 소스 변경이 있는데 gen 이
            # 안 켜지는 경로가 있으면 그 근거가 무너진다.
            hits += [c for c in changed if c not in hits
                     and any(harness.owns_file(role, c) for role in roles)]
        if hits:
            matched.append(dict(r, matched_paths=hits[:20],
                                matched_count=len(hits)))

    cap = _cap(config, profile)
    kept, dropped = matched[:cap], matched[cap:]
    return {
        "reviewers": kept,
        "dropped": [{"code": d["code"], "skill": d["skill"],
                     "why": "프로파일 %s 의 상한 %d 를 넘었다" % (profile, cap)}
                    for d in dropped],
        "capped": bool(dropped),
        "cap": cap,
        "profile": profile,
        "source_changed": source_changed,
    }


def _cap(config, profile):
    caps = ((config.get("review") or {}).get("profile_caps") or DEFAULT_CAPS)
    return caps.get(profile) or DEFAULT_CAPS.get(profile) or 1


def _source_changed(config, changed, source_globs=None):
    """소스 변경이 하나라도 있는가. **역할이 소유한 것이 소스의 정의다.**

    판정은 `harness.owns_file` 에 맡긴다 — `owns` 와 `excludes` 를 함께 보는
    규칙이고, 그 규칙이 여기서 갈라지면 03 의 소유 검사와 05 의 라우팅이 같은
    경로를 다르게 읽는다 (M11 · M17 이 기록한 실패다).
    """
    if source_globs is not None:
        return any(harness.glob_any(source_globs, c) for c in changed)
    return any(harness.owns_file(role, c)
               for role in config.get("roles") or []
               for c in changed)


def mode(config, diff_lines):
    """`merged` 또는 `fanout`.

    작은 diff 는 단일 에이전트가 체크리스트를 순차 적용한다 — 같은 diff 를
    관점 수만큼 다시 보내는 것이 그 크기에서는 손해이기 때문이다.
    """
    limit = ((config.get("review") or {}).get("merge_below_diff_lines")
             or DEFAULT_MERGE_BELOW)
    return "merged" if (diff_lines or 0) <= limit else "fanout"


def status(planned, ok):
    """`review05.status` — **findings 개수와 분리한다** (§E1).

    리뷰어가 전부 실패해도 findings 는 0건이다. 그 0을 "지적이 없다"로 읽으면
    아무도 리뷰하지 않은 코드가 통과한다. 그래서 "리뷰가 수행됐는가"를 별도
    신호로 만든다. **계획된 리뷰어가 0개인 것도 `failed` 다** — 라우팅이 아무도
    부르지 않은 것은 통과가 아니라 미수행이다.
    """
    if not planned or not ok:
        return "failed"
    if ok < planned:
        return "degraded"
    return "ok"


# 나쁜 쪽으로 갈수록 크다. `state.GRADES` 의 단조 강등과 같은 규율이다.
STATUS_RANK = {"ok": 0, "degraded": 1, "failed": 2}


def worst_status(statuses):
    """라운드별 status 들의 **최악**. 런의 `review05.status` 는 이 값이다.

    델타 재리뷰가 1명이면 그 라운드의 분모가 1 이라 깨끗한 재리뷰가
    `ok` 를 만든다. 그것을 런의 값으로 쓰면 **1라운드의 `degraded` 가
    조용히 지워진다** — G-4 가 막으려던 구멍이 옆문으로 다시 열린다.
    그래서 status 는 런 안에서 좋아지지 않는다.

    빈 목록은 `failed` 다. 라운드가 하나도 없는 것은 미수행이지 통과가 아니다.
    """
    ranked = [STATUS_RANK.get(x, 2) for x in statuses or []]
    if not ranked:
        return "failed"
    worst = max(ranked)
    for name, rank in STATUS_RANK.items():
        if rank == worst:
            return name
    return "failed"


# ------------------------------------------------------------------ 제출 판정

def flatten(payload):
    """`by_checklist` → 평면 findings. 01 의 검사를 그대로 재사용하기 위해서다.

    `findings` 를 직접 준 제출도 받는다 — 통합 모드가 아닌 리뷰어가 그렇게 낼
    수 있고, 형태 하나를 강요해서 exit 8 을 늘릴 이유가 없다.
    """
    if payload.get("findings") is not None:
        return list(payload["findings"])
    out = []
    for items in (payload.get("by_checklist") or {}).values():
        out += list(items or [])
    return out


def check(root, config, payload, raw_text, previous_open, excluded=None,
          known=None):
    """05 리뷰 제출의 판정. 01 의 `check_review` + 05 특화 넷.

    반환: {"ok","exit","errors","keys","closed","blocking","findings",
           "dropped_by_enforcement","truncated"}
    """
    errors = []
    excluded = set(excluded or [])

    # ① by_checklist 는 **0건인 체크리스트도 명시**해야 한다. 안 그러면
    #    "안 봤다"와 "보고 아무것도 없었다"가 같은 침묵이 된다 (§E10).
    if payload.get("findings") is None:
        by = payload.get("by_checklist")
        if not isinstance(by, dict) or not by:
            errors.append(
                "by_checklist 가 없거나 비었다 — 0건인 체크리스트도 빈 배열로 "
                "명시한다. 누락과 '보고 아무것도 없었다'를 구분해야 한다")

    # ② 작성자 격리. config 가 이미 검사하지만 제출 시점에도 막는다 —
    #    설정을 바꾸지 않고 reviewer 이름만 바꿔 내는 경로가 남는다.
    authors = {r.get("agent") for r in config.get("roles") or []}
    codes = {r.get("code") for r in config.get("reviewers") or []}
    skills = {r.get("code"): r.get("skill") for r in config.get("reviewers") or []}
    who = payload.get("reviewer")
    if who in authors or skills.get(who) in authors:
        errors.append("리뷰어 %r 이 작성자 역할이다 — 자기 코드를 리뷰한 것은 "
                      "독립 관측이 아니다" % who)
    elif codes and who not in codes:
        errors.append("리뷰어 %r 이 config.reviewers 에 없다 — 라우팅이 부르지 "
                      "않은 리뷰어의 제출은 받지 않는다" % who)

    findings = flatten(payload)

    # ②-b **어휘 밖 category 는 여기서 잡는다** (M46). 이 검사는 원래
    #     `ledger.append` 에만 있었고 그것은 **리뷰어 전원이 모여 병합된 뒤**
    #     에 돈다 — 셋 중 하나가 어휘 밖을 내면 exit 8 이 마지막 제출자에게
    #     가고, 그 제출자는 남의 findings 를 고칠 수 없어 스스로 빠져나올 수
    #     없다. 빠져나가는 유일한 길이 리뷰 회차 예산을 태우는 것이었다.
    #     제출자 층에는 이미 `attempts` 예산과 강등 경로가 있으므로,
    #     검사를 여기로 내리면 위반한 리뷰어가 그 기계를 그대로 탄다.
    #     `ledger.append` 의 검사는 **지우지 않는다** — 05 밖 경로(07·trace)의
    #     마지막 방어선이다.
    if known:
        allowed = ", ".join(sorted(known))
        for f in findings:
            code = f.get("category")
            if code not in known:
                errors.append(
                    "finding %s: taxonomy 에 없는 category 다 (%r). 쓸 수 있는 "
                    "것: %s" % (f.get("id"), code, allowed))
                continue
            errors += slug_errors(f, known[code])

    if errors:
        return _fail(errors)

    # ③ 01 의 검사를 **리뷰어가 낸 것 전부**에 건다. 드롭보다 먼저다 —
    #    원문의 심각도 헤딩은 리뷰어가 **쓴 만큼** 있고, 우리가 나중에 버릴
    #    것까지 세어 준다. 드롭을 먼저 하면 헤딩 개수가 안 맞아 exit 8 이 나고,
    #    그것은 리뷰어의 잘못이 아닌 것으로 리뷰어를 벌하는 것이다.
    got = verdict.check_review(dict(payload, findings=findings), raw_text,
                              previous_open)
    if not got["ok"]:
        return _fail(got["errors"])

    # ④ 이제 "검토 제외" 목록의 category 를 드롭한다. **조용히 버리지 않고
    #    센다** — 기계 강제 규칙이 늘수록 05 가 싸지는 것이 원장 승격의
    #    복리인데, 몇 건이었는지 안 세면 복리가 실현됐는지 알 수 없다.
    kept, dropped = [], []
    for f in findings:
        (dropped if f.get("category") in excluded else kept).append(f)
    dropped_keys = {verdict.finding_key(f) for f in dropped}
    got = dict(got,
               keys=[k for k in got["keys"] if k["key"] not in dropped_keys],
               blocking=sum(1 for f in kept
                            if f.get("severity") in verdict.BLOCKING))

    # ④ findings 상한. 넘으면 Critical/Major 만 남기고 절단하되 **절단 사실을
    #    남긴다** — 잘린 것이 없었던 것처럼 보이면 안 된다 (§E5).
    limit = ((config.get("review") or {}).get("findings_max")
             or DEFAULT_FINDINGS_MAX)
    truncated = False
    if len(kept) > limit:
        kept = [f for f in kept if f.get("severity") in verdict.BLOCKING][:limit]
        truncated = True

    return {"ok": True, "exit": 0, "errors": [], "keys": got["keys"],
            "closed": got["closed"], "blocking": got["blocking"],
            "findings": kept, "dropped_by_enforcement": len(dropped),
            "dropped_categories": sorted({f.get("category") for f in dropped}),
            "truncated": truncated}


def slug_errors(f, category):
    """②-c **승격 축의 통제 어휘** (ADR-H035). [오류 문자열].

    **어휘를 선언한 카테고리에서만 필수다.** 전면 선택이면 리뷰어가 그냥 안
    적어 축이 그대로 자유 서술로 남고, 전면 필수면 `OTHER`·`CONTRACT_DEFECT`
    처럼 어휘가 없는 곳에 억지 슬러그를 만들게 되어 M46 이 고친 회차 예산
    소진이 재현되는데 이번엔 **탈출구 자체가 없다.**

    면제 목록을 여기 적지 않는 것이 요점이다 — `validate_taxonomy` 가
    *"승격 못 하는 카테고리는 slugs 를 선언할 수 없다"* 를 강제하므로
    면제가 **스키마에서** 나온다. 어휘가 늘어도 이 함수는 안 바뀐다.

    거부 메시지가 `note` 까지 싣는 이유도 실측이다 — `DOC_CODE_DRIFT` 를
    내는 것은 arch·data·sec 이고 그들은 `docs-reviewer` 의 표를 읽지 않는다.
    이름만 나열하면 두 슬러그를 언제 가르는지 모른 채 고른다 (M20).
    """
    vocab = category.get("slugs") or []
    if not vocab:
        return []                       # 어휘를 안 선언한 카테고리는 면제다
    slug = f.get("rule_slug")
    known = [s.get("slug") for s in vocab]
    if slug in known:
        return []
    menu = "\n".join("  - `%s` — %s" % (s.get("slug"), s.get("note"))
                      for s in vocab)
    what = ("`rule_slug` 가 없다" if slug is None
            else "%s 의 어휘에 없는 `rule_slug` 다 (%r)"
                 % (f.get("category"), slug))
    return ["finding %s: %s. %s 는 승격 축의 통제 어휘를 선언한 카테고리라 "
            "그중 하나를 골라야 한다 — 맞는 것이 없으면 `category: OTHER` 로 "
            "내고 무엇이 없는지를 evidence 에 적는다:\n%s"
            % (f.get("id"), what, f.get("category"), menu)]

def _fail(errors, dropped=0):
    return {"ok": False, "exit": 8, "errors": errors, "keys": [], "closed": [],
            "blocking": 0, "findings": [], "dropped_by_enforcement": dropped,
            "dropped_categories": [], "truncated": False}


def merge(submissions):
    """여러 리뷰어의 findings 를 합친다.

    **2인 이상이 지적한 항목은 severity 를 한 단계 올린다** — 독립 관측의
    합치는 한 관측보다 강한 증거다.
    """
    ladder = ["minor", "major", "critical"]
    by_key = {}
    for sub in submissions:
        for f in sub.get("findings") or []:
            key = verdict.finding_key(f)
            slot = by_key.setdefault(key, {"finding": dict(f), "by": []})
            if sub.get("reviewer") not in slot["by"]:
                slot["by"].append(sub.get("reviewer"))

    out = []
    for key, slot in by_key.items():
        f = slot["finding"]
        f["reported_by"] = slot["by"]
        f["finding_key"] = key
        if len(slot["by"]) > 1:
            i = ladder.index(f.get("severity")) if f.get("severity") in ladder else 0
            if i < len(ladder) - 1:
                f["severity_raised_from"] = f.get("severity")
                f["severity"] = ladder[i + 1]
                f["why_raised"] = ("독립 리뷰어 %d명이 같은 것을 지적했다 — "
                                   "합치는 한 관측보다 강한 증거다" % len(slot["by"]))
        out.append(f)
    out.sort(key=lambda f: -ladder.index(f.get("severity"))
             if f.get("severity") in ladder else 0)
    return out


def open_findings(rounds):
    """런이 끝났을 때 **아직 열린** 지적. 전원 것이고 제목이 있다 (M52).

    `cli._previous_open` 의 **형제**이지 확장이 아니다 — 그 함수가 답하는 것은
    *"이 리뷰어가 이번 제출에서 회계해야 할 것"* 이고 리뷰어별 필터가 붙어야
    맞다(M21 ③: 두 리뷰어가 모두 `F-1` 을 쓰므로 id 대조를 전역으로 하면 한
    줄이 서로 다른 두 지적을 동시에 해소로 계수한다). 여기가 답하는 것은
    *"런 전체에서 무엇이 열린 채인가"* 다. **회계는 리뷰어별이고 보고는 런
    전체인데, 지금까지 보고가 회계의 경계를 물려받고 있었다** — 델타 라운드는
    설계상 한 명이므로 그 누수는 델타를 쓸 때마다 났다.

    원천은 `phases.05-code-review.rounds` 하나다. **파생 사본을 새로 쌓지
    않는다** (M31 · ADR-H022) — 늘어나는 것은 같은 원본에 대한 두 번째 질문뿐이다.

    접는 규칙 셋은 필드의 뜻이 정한다:

    - **라운드마다 따로 `merge` 한다.** 가로질러 한 번에 합치면 상승 규칙의
      전제인 "독립 관측"이 거짓이 된다 — 델타 리뷰어는 이전 회차의 열린 목록을
      프롬프트로 받고 그것을 회계하도록 **강제받는다.** 강제된 재진술은 두 번째
      관측이 아니다. 게다가 보고 표면에서만 오른 심각도는 원장·`review05.major`
      와 갈린다
    - **같은 키는 첫 등장이 이긴다.** 원장의 `ledgered_keys` 가 같은 규칙이다
      (M30) — 원장이 1회차 행을 남기는데 본문이 2회차 판정을 적으면 두 영수증이
      같은 키를 두고 다른 말을 한다
    - **닫힌 것은 전 라운드 `closed` 의 합집합으로 뺀다.** 그 값은 자진 신고가
      아니라 단조성 검사가 이미 검증한 것이다 (M29)

    severity 는 **그 회차의 병합 판정값**이다. 같은 라운드에서 둘이 minor 로 낸
    키는 원장에 major 로 적혀 있고, 제출 원본값을 쓰면 원장이 major 라 부르는
    것을 본문이 「미해결 Minor」에 싣는다.
    """
    by_key, closed = {}, set()
    for rn in sorted(rounds or {}, key=int):
        slot = rounds[rn] or {}
        # 실패 슬롯(`keys is None`)은 읽지 않는다 — `_judge_05` 의 병합이 쓰는
        # 것과 **같은 가드**다. 안 빼면 규약을 어겨 되돌려진 제출의 문장이
        # PR 본문에 실린다.
        subs = [dict(v, reviewer=code) for code, v in slot.items()
                if v.get("keys") is not None]
        for f in merge(subs):
            by_key.setdefault(f["finding_key"], f)
        for v in slot.values():
            closed |= set(v.get("closed") or [])
    return [f for key, f in by_key.items() if key not in closed]


# --------------------------------------------------------------- 인라인 상한

def inline_budget(config, diff_text):
    """인라인으로 실을 수 있는가. 넘으면 경로 전달로 폴백한다 (§E5).

    폴백했다는 **사실이 원장에 남아야 한다** — 리뷰어가 diff 를 인라인으로 못
    받은 런은 다른 런이고, 그것이 findings 품질에 영향을 준다.
    """
    limit = ((config.get("review") or {}).get("inline_max") or {})
    lines = len((diff_text or "").splitlines())
    size = len((diff_text or "").encode("utf-8"))
    over = []
    if limit.get("lines") and lines > limit["lines"]:
        over.append("줄 %d > %d" % (lines, limit["lines"]))
    if limit.get("bytes") and size > limit["bytes"]:
        over.append("바이트 %d > %d" % (size, limit["bytes"]))
    return {"inline": not over, "lines": lines, "bytes": size,
            "over": over,
            "fallback": "경로 전달" if over else None}


_SEVERITY_HEADING = re.compile(r"(?mi)^#{1,6}\s*(critical|major|minor)\b")


def severity_headings(raw_text):
    """`.raw.md` 의 심각도 헤딩 수. 검증기가 findings 개수와 대조한다."""
    return len(_SEVERITY_HEADING.findall(raw_text or ""))
