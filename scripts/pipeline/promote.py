# -*- coding: utf-8 -*-
"""`promote` — 원장에 쌓인 지적을 규칙으로 올린다. **07 에서 런당 한 번.**

05 는 후보(`staged`)까지만 만들었다. 실제 쓰기를 07 로 미룬 이유가 둘이다 —
dedup 이 로직이 아니라 **시점**으로 성립하고, 기능 PR 의 diff 에 규칙 문서
변경이 섞이지 않는다.

네 플래그가 한 흐름이다:

- `--scan`  — 원장을 다시 집계한다. **후보가 0 이면 모델을 부르지 않고
  종결한다.** 초기 런에서는 이것이 최빈 경로다.
- `--stage` — 후보를 `state.promotions` 에 `staged` 로 적재한다.
- `--apply` — 판정(`verdict`)을 받아 실제로 쓴다.
- `--flush` — 08 진입 전 잔여 `staged` 를 강제 종결한다.

**"일단 붙이기" 를 선택지에서 없앤 것이 이 모듈의 설계 전부다.** 판정이
`duplicate` 면 `create` 가 금지되고, `contradicts` 면 자동 쓰기가 차단되며
에스컬레이션이다. 그리고 `lint` 승격은 **베이스라인 diff 를 반드시 남긴다** —
없으면 "규칙은 추가했는데 아무것도 안 막는다"가 조용히 통과한다.
"""

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import adapters  # noqa: E402
import harness  # noqa: E402
import ledger  # noqa: E402

# `promotions[].status` 의 닫힌 어휘. 05 는 `staged` 만 쓰고 07 이 나머지를 쓴다.
STATUSES = ("staged", "applied", "rejected", "skipped")

# 판정 어휘. `duplicate` 에서 `create` 가 금지되는 것이 요점이다.
JUDGEMENTS = ("new", "duplicate", "contradicts")
ACTIONS = ("create", "amend", "skip")

# **미검증 상속값이다.** 원본 명세에서 왔고 이 리포에서 재본 적이 없다.
# 매 런 상한에 닿으면 낮은 것이다. 판정 시한은 `THRESHOLDS` 와 같은 것을
# 쓴다 — `ledger.PROMOTION_VERDICT_AT_RUNS` (ADR-H033).
CREATE_MAX_PER_RUN = 3


def scan(root):
    """원장을 다시 집계한다. `ledger.stage_promotions` 를 그대로 쓴다.

    **집계 파일을 두지 않고 매번 재계산한다** — 수백 줄 규모라 밀리초고,
    파생 파일을 두면 동기화 버그만 생긴다.
    """
    got = ledger.stage_promotions(root)
    got["needs_model"] = bool(got["candidates"])
    if not got["candidates"]:
        got["note"] = ("후보가 0 이다 — 모델을 부르지 않고 종결한다. "
                       "초기 런의 최빈 경로이고, 임계에 닿을 표본이 아직 없다는 "
                       "뜻이지 지적이 없었다는 뜻이 아니다.")
    return got


def _identity(d):
    """승격 행·판정의 신원. `rule_key` 우선, 없으면 `finding_key` (ADR-H034).

    **낙하가 하위 호환을 맡는다** — 이미 쌓인 `state.promotions` 행과 옛
    원장 줄에는 `rule_key` 가 없고, 폴백 버킷에서는 둘이 같은 값이다.
    """
    return d.get("rule_key") or d.get("finding_key")


def stage(candidates):
    """후보를 `state.promotions` 모양으로 만든다. **전부 `staged` 다.**

    신원은 `rule_key` 다. 대표 `finding_key` 를 신원으로 쓰면 런마다 다른
    인스턴스가 실려 나가(P9 는 `ErrorBanner`, P10 은 `RATE_LIMIT_WINDOW_MS`)
    `merge_staged` 가 같은 규칙을 두 행으로 갈라 놓는다. 접은 인스턴스는
    `finding_keys` 로 **전부** 싣는다 — 수리하는 쪽이 무엇을 고칠지 알아야
    하고, 그것은 신원과 다른 질문이다.
    """
    return [{"rule_id": c.get("rule") or c["category"].lower().replace("_", "-"),
             "rule_key": c.get("rule_key"),
             "rule_slug": c.get("rule_slug"),
             "finding_key": c.get("finding_key"),
             "finding_keys": list(c.get("finding_keys") or []),
             "category": c["category"],
             "enforceable": c.get("enforceable"),
             "severity": c["severity"],
             "count": c["count"],
             "distinct_runs": c["distinct_runs"],
             "status": "staged",
             "reason": None}
            for c in candidates]


def merge_staged(promos, fresh):
    """새 후보 목록을 기존 `promotions` 에 **병합**한다. 제자리 갱신.

    종단 상태(`applied`·`rejected`·`skipped`)는 손대지 않는다 — 이미 일어난
    일이고 되돌릴 수 있으면 그것이 조용한 통과의 자리가 된다. 기존 `staged`
    는 근거 수치만 갱신하고, 새 후보만 덧붙인다.
    """
    by_key = {_identity(p): p for p in promos}
    for f in fresh:
        cur = by_key.get(_identity(f))
        if cur is None:
            promos.append(f)
            by_key[_identity(f)] = f
            continue
        if cur.get("status") != "staged":
            continue
        for k in ("severity", "count", "distinct_runs", "enforceable",
                  "category"):
            cur[k] = f[k]
        # 접힌 인스턴스는 런을 가로질러 **늘어난다.** 근거 수치와 같이 갱신하지
        # 않으면 수리하는 쪽이 첫 런의 목록만 보게 된다.
        if f.get("finding_keys"):
            cur["finding_keys"] = f["finding_keys"]
    return promos


def check_verdicts(root, verdicts, promotions=None):
    """판정을 검사한다. 반환: (errors, blocked).

    `blocked` 가 참이면 **자동 쓰기를 하지 않고 에스컬레이션**한다 — 규칙끼리
    싸우는 상태를 파이프라인이 혼자 정리하려 들면 안 된다.

    판정은 **어느 후보를 올리는지 가리켜야 한다** (`category`, 또는 후보의
    신원인 `rule_key` — 옛 행은 `finding_key`). `rule_id` 는 새로 짓는 목적지
    이름이라 후보와 이름이 다를 수 있고, 그것으로 매칭하면 엉뚱한 후보가
    승격된다.
    """
    errors, blocked = [], False
    creates = 0
    taken = set()
    for v in verdicts or []:
        rid0 = v.get("rule_id") or "(이름 없음)"
        if not _resolve_category(root, v, promotions):
            errors.append("%s: 어느 후보를 승격하는지 가리키지 않았다 — "
                          "`category` 또는 `rule_key` 가 필요하다." % rid0)
        elif promotions is not None:
            # **`apply` 와 같은 함수로 본다.** 두 함수가 각자 매칭하면 검사가
            # 통과한 판정이 다른 후보를 승격시킬 수 있다.
            hit = resolve_target(root, v, promotions, taken)
            if hit is None:
                errors.append(
                    "%s: 가리킨 후보를 찾지 못했다 (rule_key=%r · "
                    "finding_key=%r) — 그것은 후보의 신원이라 category 로 "
                    "낙하시키지 않는다. 이름을 부른 것과 다른 지적이 "
                    "승격되느니 거부한다."
                    % (rid0, v.get("rule_key"), v.get("finding_key")))
            else:
                taken.add(id(hit))
        j, a = v.get("judgement"), v.get("action")
        rid = v.get("rule_id") or "(이름 없음)"
        if j not in JUDGEMENTS:
            errors.append("%s: 알 수 없는 judgement %r (%s)"
                          % (rid, j, " · ".join(JUDGEMENTS)))
            continue
        if a not in ACTIONS:
            errors.append("%s: 알 수 없는 action %r (%s)"
                          % (rid, a, " · ".join(ACTIONS)))
            continue
        if a == "skip" and not (v.get("rationale") or "").strip():
            # **미룸에는 사유가 있다** (ADR-H051). 파일럿 changelog 13행이 전부
            # skipped 인데 왜 미뤘는지 한 줄도 없었다 — 다음 런의 판정자가
            # 같은 후보를 놓고 같은 고민을 처음부터 다시 한다.
            errors.append("%s: action 이 skip 인데 rationale 이 비었다 — 왜 "
                          "미루는지 한 줄이 필요하다. 다음 런의 판정자가 그것을 "
                          "읽는다." % rid)
        if j == "duplicate" and a == "create":
            errors.append("%s: judgement 가 duplicate 인데 action 이 create 다 — "
                          "같은 규칙이 두 벌 생긴다. skip 또는 amend 만 허용된다."
                          % rid)
        if j == "contradicts":
            blocked = True
        if a == "create":
            creates += 1
        code = _resolve_category(root, v, promotions)
        if code:
            try:
                ledger.check_destination(root, code, v.get("enforceable"))
            except ValueError as exc:
                errors.append("%s: %s" % (rid, exc))
    if creates > CREATE_MAX_PER_RUN:
        errors.append("이 런의 create 가 %d 건이다 — 상한 %d 를 넘는다. "
                      "**미검증 상속값이지만 넘는 것을 조용히 통과시키지는 "
                      "않는다.**" % (creates, CREATE_MAX_PER_RUN))
    return errors, blocked


def _resolve_category(root, v, promotions=None):
    """판정이 가리키는 category. 없으면 None.

    순서가 요점이다 — 명시된 `category` → 후보 신원(`rule_key`, 옛 행은
    `finding_key`) → 목적지 이름(`rule_id`) 이 taxonomy 의 `rule` 과 정확히
    같을 때. 마지막은 기계 강제 규칙에만 성립하고 `prose` 카테고리에는
    `rule` 이 없다.
    """
    code = v.get("category")
    if code:
        return code
    ident = _identity(v)
    if ident:
        for p in promotions or []:
            if _identity(p) == ident:
                return p.get("category")
    rid = v.get("rule_id")
    for p in promotions or []:
        if p.get("rule_id") == rid:
            return p.get("category")
    for c, cat in ledger.categories(root).items():
        if cat.get("rule") and cat.get("rule") == rid:
            return c
    return None


def resolve_target(root, v, promotions, taken=None):
    """판정이 가리키는 후보 행. 없으면 None.

    **두 패스로 나눈다.** 한 루프 안에서 신원 정확 일치와 `category` 약한
    일치를 섞으면 배열 앞쪽의 약한 일치가 뒤쪽의 정확한 일치를 이긴다 —
    엉뚱한 규칙이 changelog 에 쓰이고 근거 열도 다른 버킷에서 온다 (G-1).

    `taken` 은 이미 다른 판정에 바인딩된 행이다. 표시하지 않으면 두 판정이
    같은 행을 잡아 **뒤 판정이 앞 판정의 status 를 조용히 덮는다.**

    신원(`rule_key`, 옛 행은 `finding_key`)을 줬는데 후보에 없으면
    **category 로 낙하하지 않는다** — 이름을 부른 것과 다른 지적이 승격되느니
    못 찾는 편이 낫다. 신원을 가리켰는데 다른 것이 올라가면 그 사실이
    어디에도 안 드러난다.
    """
    taken = taken if taken is not None else set()
    ident = _identity(v)
    if ident:
        for p in promotions:
            if _identity(p) == ident and id(p) not in taken:
                return p
        return None
    code = _resolve_category(root, v, promotions)
    if not code:
        return None
    for p in promotions:
        if (p.get("category") == code and p.get("status") == "staged"
                and id(p) not in taken):
            return p
    return None


# `baseline_cmd` 를 실행조차 못 한 것. 린터가 위반을 찾아 0 이 아닌 것과 **다르다.**
_INFRA_EXITS = (124, 127)


def wants_baseline(verdicts):
    """`lint` 승격이 실제로 하나라도 있는가. 재는 비용을 거기에만 쓴다."""
    return any(v.get("enforceable") == "lint" and v.get("action") != "skip"
               for v in verdicts or [])


def measure_baseline(root, adapter, runner=None):
    """`baseline_cmd` 를 직접 돌리고 **베이스라인이 실제로 바뀌었는지 잰다.**

    반환: {"state", "changed", "diff", "file", "exit", "reason"}
    `state` 는 `measured` · `unavailable` · `infra` 셋이다.

    **왜 기계가 재는가.** 이 값은 "규칙이 무엇을 막는가" 의 유일한 증거인데,
    모델의 자진 신고를 그대로 믿으면 아무 문자열이나 통과한다. 07 의 `external`
    을 `review07` 이 봇 원문에서 다시 세는 것과 같은 규율이다.

    **종료 코드를 성패로 읽지 않는다.** 린터가 위반을 찾으면 0 이 아니고 그것이
    정상이다. 판정은 오직 diff 가 한다. 실행 자체가 불가능했던 것(127)과
    타임아웃(124)만 `infra` 이고, 그것은 데이터 문제가 아니라 시스템 문제라
    `rejected` 로 적지 않는다.
    """
    root = Path(root)
    rel = adapters.baseline_file(adapter)
    argv = adapters.baseline_argv(root, adapter)
    if argv is None:
        return {"state": "unavailable", "changed": False, "diff": "",
                "file": rel, "exit": None,
                "reason": "어댑터에 `baseline_cmd`(또는 `baseline_file`)가 없다 "
                          "— lint 승격이 무엇을 막는지 재지 못했다"}

    timeout, _src = adapters.stage_timeout(adapter, None, "lint")
    run = runner or adapters._default_runner
    code, out = run("lint-baseline", argv, str(root), timeout)
    if code in _INFRA_EXITS:
        return {"state": "infra", "changed": False, "diff": "", "file": rel,
                "exit": code,
                "reason": "`baseline_cmd` 를 실행하지 못했다 (exit %s) — "
                          "시스템 문제이지 규칙이 아무것도 안 막는다는 뜻이 "
                          "아니다: %s" % (code, (out or "")[:200])}

    changed, diff = _baseline_delta(root, rel)
    return {"state": "measured", "changed": changed, "diff": diff,
            "file": rel, "exit": code,
            "reason": None if changed else
            "`%s` 이 그대로다 — 규칙은 추가했는데 아무것도 안 막는다는 뜻이다" % rel}


def _baseline_delta(root, rel):
    """(바뀌었는가, diff 텍스트).

    **`git diff` 만 보면 안 된다** — 첫 승격에서 베이스라인 파일은 아직 추적되지
    않고, `git diff` 는 미추적 파일을 한 줄도 보여 주지 않는다. 그러면 실제로
    막는 규칙이 "아무것도 안 막는다" 로 거절된다.
    """
    st_r = harness._git(root, "status", "--porcelain", "--", rel)
    if st_r is None or st_r.returncode != 0:
        return False, ""
    if not st_r.stdout.strip():
        return False, ""

    d = harness._git(root, "diff", "HEAD", "--", rel)
    text = (d.stdout if d is not None and d.returncode == 0 else "") or ""
    if text.strip():
        return True, text
    # 미추적 새 파일 — diff 로는 안 보인다. 무엇이 생겼는지 그대로 적는다.
    try:
        size = (Path(root) / rel).stat().st_size
    except OSError:
        size = 0
    return True, "새 베이스라인 파일 %s (%d bytes · 아직 추적되지 않는다)" % (rel, size)


def baseline_mismatch(reported, measured):
    """모델이 실은 값과 기계값이 다른가. **공백만 정규화해 비교한다.**

    신고하지 않는 것이 정상 경로다. 실렸으면 버리지 않고 대조한다 — 자진 신고
    중 기계로 확인 가능한 것은 기계로 확인한다(불변식 8).
    """
    said = (reported or "").strip()
    if not said:
        return False
    return " ".join(said.split()) != " ".join((measured or "").split())


def apply(root, run_id, promotions, verdicts, baseline=None):
    """판정대로 승격 상태를 정한다. 반환: (promotions, rows).

    **파일을 쓰기 전에 거절 사유를 먼저 정한다.** `lint` 승격인데 **기계가 잰**
    베이스라인이 그대로면 여기서 `rejected` 이고, 규칙 파일은 손대지 않는다.

    `baseline` 은 `measure_baseline` 의 반환이다. `unavailable` 이면 막지 않고
    통과시키되 `baseline: "unverified"` 를 남긴다 — 호출자가 그것을 갭으로
    올린다. **스킵을 통과로 적지 않는다.**
    """
    rows = []
    taken = set()
    for v in verdicts or []:
        rid = v.get("rule_id")
        code = _resolve_category(root, v, promotions)
        target = resolve_target(root, v, promotions, taken)
        if target is not None:
            taken.add(id(target))
            # 목적지 이름은 판정이 짓는다 — 후보의 기본 이름을 덮어쓴다.
            target["rule_id"] = rid or target["rule_id"]
        if target is None:
            # 후보에 없는 규칙을 승격하려 한다 — 조용히 버리지 않고 skipped 로
            # 남긴다. 어디서 왔는지 모르는 규칙이 규칙 문서에 들어가지 않게.
            # (`check_verdicts` 가 앞에서 거부하므로 여기까지 오는 것은
            #  검사를 건너뛴 경로뿐이다.)
            target = {"rule_id": rid, "finding_key": v.get("finding_key"),
                      "category": code,
                      "enforceable": v.get("enforceable"),
                      "severity": None, "count": 0, "distinct_runs": 0,
                      "status": "staged", "reason": None}
            promotions.append(target)

        is_lint = v.get("enforceable") == "lint"
        # 기계가 잰 값이 유일한 출처다. 모델이 실은 값은 앞에서 이미 대조했다.
        measured = (baseline or {}).get("diff") or ""

        if v.get("action") == "skip":
            target["status"] = "skipped"
            target["reason"] = v.get("rationale") or "판정이 skip 이다"
        elif is_lint and (baseline or {}).get("state") == "measured" \
                and not baseline.get("changed"):
            target["status"] = "rejected"
            target["reason"] = ("lint 승격인데 베이스라인이 그대로다 — "
                                "규칙은 추가했는데 아무것도 안 막는다는 뜻이다")
        else:
            target["status"] = "applied"
            target["reason"] = v.get("rationale")
            if is_lint and (baseline or {}).get("state") == "unavailable":
                target["baseline"] = "unverified"

        rows.append({
            "run_id": run_id, "rule_id": rid,
            "category": target.get("category"),
            "enforceable": v.get("enforceable"),
            "evidence": "%s회 / %s런" % (target.get("count"),
                                        target.get("distinct_runs")),
            "judgement": v.get("judgement"),
            "action": target["status"],
            "baseline_diff": (measured.strip() if is_lint else "") or
                             ("미측정" if is_lint else "해당 없음"),
            "retired_reason": v.get("retired_reason") or "",
        })
    return promotions, rows


def flush(promotions):
    """잔여 `staged` 를 종결한다. **08 은 미종결을 exit 6 으로 막는다.**

    종결 어휘는 `skipped` 다 — 승격하지 **않았다**는 사실이고, 실패(`rejected`)
    와 구분된다. 임계는 다시 충족되면 다음 런에서 또 후보가 된다.
    """
    n = 0
    for p in promotions or []:
        if p.get("status") == "staged":
            p["status"] = "skipped"
            p["reason"] = ("이 런에서 판정을 받지 못했다 — 임계가 다시 충족되면 "
                           "다음 런에서 재승격 후보가 된다")
            n += 1
    return n


def changelog_append(root, rows):
    """`rules_changelog.md` 에 줄을 더한다. 반환: 쓴 줄 수.

    표는 열 열 개다. 열 이름이 산문과 어긋나 있던 것을 여기서 맞춘다 —
    「철회 사유」가 산문에는 있고 표에는 없었다.
    """
    if not rows:
        return 0
    p = Path(root) / "docs" / "harness" / "pipeline" / "ledger" / "rules_changelog.md"
    if not p.exists():
        ledger.seed(root)
    text = p.read_text(encoding="utf-8")
    stamped = []
    for r in rows:
        stamped.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            _today(), r["run_id"], r["rule_id"], r["category"],
            r["enforceable"], r["evidence"], r["judgement"], r["action"],
            r["baseline_diff"].replace("|", "\\|")[:80],
            r["retired_reason"] or "-"))
    if not text.endswith("\n"):
        text += "\n"
    # §E4 — UTF-8 을 명시한다.
    p.write_text(text + "\n".join(stamped) + "\n", encoding="utf-8")
    return len(stamped)


def _today():
    import datetime
    return datetime.date.today().isoformat()


def applied_payload(run_id, promotions, rows, scanned):
    """`07_promo_applied.json`. 종결된 것만이 아니라 **전부**를 담는다."""
    return {"schema": 1, "run_id": run_id,
            "promotions": promotions,
            "changelog_rows": rows,
            "thresholds": scanned.get("thresholds"),
            "held": scanned.get("held") or [],
            "note": ("승격은 런당 한 번이고 별도 브랜치로 간다. 기능 PR 에 "
                     "규칙 변경을 섞지 않는다.")}
