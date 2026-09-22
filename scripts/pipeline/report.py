# -*- coding: utf-8 -*-
"""`08-report` — 런이 스스로에 대해 말하는 자리.

**08 은 diff 도 코드도 읽지 않는다.** 입력은 `08_report_data.json` 하나뿐이고
전문은 파일 경로로만 가리킨다. 이 제약이 보고서의 비용을 런 크기와 무관하게
만든다.

분업이 요점이다 — **표는 실행기가 조립하고 서술은 모델이 쓴다.**
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

REQUIRED_SECTIONS = ("## 완료 등급", "## 건너뛴 게이트", "## 비용과 시간",
                     "## 리뷰", "## 서술")

# 서술 4절의 하한 (ADR-H052 결정 5). `5568` 의 08 은 서술이 통째로 비었고
# 그 런은 07 major 6건으로 최다였다 — 「왜 그랬는가는 이 런이 말하지 않았다」.
# **등급은 건드리지 않는다.** 봉투가 되묻는다. 「계약이 부족한 곳이 없었다」도
# 왜 그렇게 보는지를 적게 한다 — 짧은 답을 받으면 그 절이 빈 채로 굳는다.
NARRATIVE_MIN_CHARS = 80
NARRATIVE_REQUIRED = (("narrative", "문제"), ("narrative", "원인"),
                      ("narrative", "해결"), ("contract_gaps",))

# `gaps[]` 의 어휘. **명세가 열거형으로 주지 않았다** — 문서 전체에 흩어진
# `PASS_WITH_GAPS` 유발 사유를 여기 모은 것이고, 그 사실을 적어 둔다.
# 모아 두지 않으면 새 사유가 어휘 없이 들어가 보고서가 그것을 설명하지 못한다.
GAP_REASONS = {
    "stage_absent": "어댑터에 그 스테이지가 없다 (`cmd: null`)",
    "stage_na": ("이 스택에 구조적으로 없는 스테이지다 — 어댑터가 "
                 "`not_applicable` 로 사유를 선언했다. 표시이고 등급은 "
                 "내리지 않는다 (ADR-H047 추기)"),
    "stage_not_touched": "그 스테이지가 볼 변경이 없었다",
    "review05": "05 의 리뷰어가 전부 또는 일부 실패했다",
    "infra_skipped": "인프라 프로브 실패로 건너뛴 검증이 있다",
    "pr_closed": "PR 이 닫혔다 — 수리·코멘트를 하지 않았다",
    "pr_merged": "PR 이 이미 머지됐다 — 수리·코멘트를 하지 않았다",
    "pr_review_skipped": ("07 의 `/code-review` 를 사유를 적고 건너뛰었다 — 05 가 "
                          "놓친 것을 잴 표본이 이 런에는 없다"),
    "pr_review_open": ("07 의 `/code-review` 가 05 가 낸 키를 가리키지 않는 "
                       "Critical/Major 를 냈다 — 05 가 놓친 것이고, 수리는 사람이 정한다"),
    "lane_miss": ("선언한 docs 레인이 빗나가 앞 페이즈가 그 양보(콜론 뒤)를 "
                  "적용한 채 지나갔다 — 03·05 의 실물에서 역할 소유 경로가 바뀌었다"),
    # 아래 다섯은 gate.py 가 처음부터 만들던 사유인데 어휘에 없었다 — 파일럿
    # 40dc 의 보고서가 `stage_no_selector:scoped` 를 "어휘에 없는 사유다" 로
    # 적었다 (ADR-H050). 코어가 만드는 사유는 전부 여기 있어야 하고, 그것을
    # `TestGapVocabulary` 가 코드를 스캔해 강제한다.
    "stage_no_selector": ("계약에서 테스트 선택자를 하나도 못 뽑아 scoped 를 "
                          "건너뛰었다 — 계약의 `## 유닛` 이 비었거나 형식이 "
                          "다르다. 전체 회귀로 낙하시키지 않았다"),
    "scoped_degenerate": ("scoped 선택자가 사실상 전체 회귀다 — 절감이 없는데 "
                          "\"scoped 통과\" 로 적히는 것을 막는다"),
    "test_report_missing": ("테스트 리포트를 한 건도 찾지 못했다 — 리포터 "
                            "경로 설정 오류일 수 있어 인프라로 다룬다"),
    "tests_ran_zero": "테스트가 0개 돌았다 — 빈 스위트의 초록불은 통과가 아니다",
}

# 등급을 내리지 않는 gap. 정확 일치 목록은 비었고 `stage_na:<id>` 접두만 남았다
# (ADR-H047 추기). 어휘를 늘리려면 그 gap 을 내는 자리를 먼저 만든다 (M36).
NON_DEMOTING_GAPS = ()


def is_non_demoting(gap):
    """비강등 판정의 단일 출처 — 게이트 등급·precheck demote·렌더가 같이 쓴다.

    `stage_na:<id>` 는 스테이지마다 이름이 달라 정확 일치 목록에 넣을 수 없다.
    부르는 곳마다 접두 검사를 따로 적으면 어느 한 곳이 빠져 갈라진다.
    """
    gap = str(gap)
    return gap in NON_DEMOTING_GAPS or gap.startswith("stage_na:")


def short_narrative(data):
    """하한 미달인 서술 필드 [(경로, 글자 수)]. 비어 있으면 통과다."""
    out = []
    for path in NARRATIVE_REQUIRED:
        node = data or {}
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
        n = len(str(node or "").strip())
        if n < NARRATIVE_MIN_CHARS:
            out.append((".".join(path), n))
    return out


def gap_reason(gap):
    """어휘 조회의 단일 출처 — **전체 키가 먼저, 머리가 그다음**이다.

    콜론까지가 키인 항목이 생기면 머리만 찾을 때 "어휘에 없는 사유" 가 된다.
    08 보고서와 06 PR 본문이 같은 함수를 쓴다.
    없으면 None.
    """
    gap = str(gap)
    return GAP_REASONS.get(gap) or GAP_REASONS.get(gap.split(":")[0])


def explain_gap(gap):
    """gap 하나를 사람이 읽는 한 줄로. 모르는 것은 **모른다고 적는다.**"""
    known = gap_reason(gap)
    if known:
        return "`%s` — %s" % (gap, known)
    return "`%s` — 어휘에 없는 사유다 (보고서가 설명하지 못한다)" % gap


def _profile_cell(node):
    """`이름 (출처)`. 선언이 빗나갔으면 `무엇에서 무엇으로` 까지 (ADR-H044)."""
    if not node:
        return None
    cell = "%s (%s)" % (node.get("name"), node.get("source"))
    miss = node.get("lane_miss")
    if miss:
        cell = "%s — **빗나감**: %s → %s (%s)" % (
            cell, miss.get("was"), miss.get("became"), miss.get("at"))
    return cell


def _models_cell(state):
    """리뷰어의 자진신고(`model_used`)뿐이다. **실측이 아니다** — blind spot 을
    함께 적는다 (ADR-H052 결정 2)."""
    node = state.get("models") or {}
    reported = node.get("reported") or {}
    if not reported:
        return None
    seen = {}
    for m in reported.values():
        seen[m] = seen.get(m, 0) + 1
    head = " · ".join("%s: %d" % (k, v) for k, v in sorted(seen.items()))
    return "%s\n  기준: **%s** — 리뷰어의 자진신고다.\n%s" % (
        head, node.get("basis"),
        "\n".join("  - %s" % b for b in node.get("blind_spots") or []))


def _false_positive_count(state):
    rounds = ((state.get("phases") or {}).get("01-plan") or {}).get("rounds") or {}
    return sum(len(sub.get("false_positive") or [])
               for subs in rounds.values() for sub in subs.values())


def _counter_cell(node):
    """`used / max` 와 무엇에 썼는지."""
    if not node:
        return None
    used, max_ = node.get("used"), node.get("max")
    cell = "%s / %s" % (used, max_) if max_ is not None else used
    # **무엇에 썼는지가 드러나야 한다** (M47). `used` 만 적으면 "수리 2회로
    # 안 됐다"와 "형식으로 2회 튕겼다"가 보고서에서 같은 칸이 된다 — P6 이
    # 정확히 그랬고, 실제로는 수리를 한 번도 시도하기 전에 에스컬레이션했다.
    spent = node.get("spent") or []
    if spent:
        counts = {}
        for e in spent:
            r = e.get("reason") or "?"
            counts[r] = counts.get(r, 0) + 1
        cell = "%s — %s" % (cell, " · ".join(
            "%s %d" % (r, n) for r, n in sorted(counts.items())))
    return cell


UNMEASURED_DURATION = ("**소요 시간은 미측정이다** — 8페이즈 실행기가 페이즈별 "
                      "소요를 아직 기록하지 않는다. 재는 것을 만들기 전에는 "
                      "값을 지어내지 않는다.")


def _hms(sec):
    if sec is None:
        return None
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _timing_lines(timing):
    """페이즈별 소요 표. `timing` 이 없으면 **미측정이라고 적는다.**

    **칸 이름이 벽시계라고 말해야 한다.** 이 값에는 사람이 답을 쓰는 대기가
    섞여 있고, P8 은 7시간 48분 중 4시간 42분(60.2%)이 그것이었다. 이름이
    그 사실을 말하지 않으면 다음 사람이 순 작업 시간으로 읽는다 — 그래서
    에스컬레이션 대기를 **같은 표의 옆 칸**으로 뺀다. 총계 한 줄로는
    "어느 페이즈에서 기다렸는가" 가 안 보인다.

    구간 수는 소요의 분모가 아니라 **별개 사실**이다. 같은 벽시계라도 한 번에
    지난 페이즈와 세 번 되돌아온 페이즈는 다른 일이다.
    """
    if not timing or not timing.get("phases"):
        return ["", UNMEASURED_DURATION, ""]

    # **순 작업과 두 종류의 대기를 갈라 적는다** (ADR-H052). `728c` 는 2h48m
    # 중 1h09m 이 05 의 사람 대기였는데 "05 가 1h50m 걸렸다" 로 읽혔다.
    # 형식 반려 수는 소요가 아니라 **왕복 비용**이다 — 같은 표에 두는 이유는
    # 그것이 그 페이즈의 벽시계를 먹기 때문이다.
    rows = ["", "| 페이즈 | 벽시계(대기 포함) | 순 작업 | 에스컬레이션 대기 "
                "| 사람 판단 대기 | 형식 반려 | 구간 |",
            "|---|---|---|---|---|---|---|"]
    for name in sorted(timing["phases"]):
        cell = timing["phases"][name]
        wait = cell.get("escalation_wait_sec")
        human = cell.get("human_wait_sec")
        rejects = cell.get("format_rejects") or 0
        segs = "%s구간" % cell.get("segments")
        entries = cell.get("entries") or 0
        if entries != 1:
            # 진입 이벤트가 0 이거나 여럿인 것 자체가 사실이다 — 08 은 0 이고
            # 되돌아간 01 은 여러 번이다. 구간 수와 다른 것을 말한다.
            segs = "%s · 진입 %s" % (segs, entries)
        rows.append("| %s | %s | %s | %s | %s | %s | %s |"
                    % (name, _hms(cell.get("wall_sec")),
                       _hms(cell.get("work_sec")),
                       _hms(wait) if wait else "—",
                       _hms(human) if human else "—",
                       rejects if rejects else "—", segs))

    total_wait = timing.get("escalation_wait_sec")
    total_human = timing.get("human_wait_sec")
    wall = timing.get("wall_sec")

    def _share(v):
        if v and wall:
            return "%s (%.1f%%)" % (_hms(v), 100.0 * v / wall)
        return _hms(v) if v else "—"

    work = None
    if wall is not None:
        work = wall - (total_wait or 0) - (total_human or 0)
    rejects_total = sum(c.get("format_rejects") or 0
                        for c in timing["phases"].values())
    rows.append("| **합계** | **%s** | **%s** | **%s** | **%s** | **%s** | |"
                % (_hms(wall), _hms(work) if work is not None else "—",
                   _share(total_wait), _share(total_human),
                   rejects_total if rejects_total else "—"))

    if timing.get("unresumed_escalations"):
        rows += ["", "재개되지 않은 에스컬레이션 %s건 — **대기 길이는 아직 없다.**"
                 % timing["unresumed_escalations"]]
    if timing.get("unanswered_waits"):
        rows += ["", "답이 오지 않은 사람 판단 대기 %s건 — **대기 길이는 아직 없다.**"
                 % timing["unanswered_waits"]]

    rows += ["", "기준: **%s** — 이벤트를 seq 순으로 걸으며 인접한 두 `ts` 의 "
                 "차를 그때 활성인 페이즈에 더한다. `Σ 페이즈 소요 == 런 "
                 "벽시계` 가 검산된다."
             % timing.get("basis")]
    rows += ["- %s" % s for s in timing.get("blind_spots") or []]
    rows.append("")
    return rows


def _tbl(rows):
    """2열 표. 값이 없으면 **`미측정` 이라고 적는다** — 빈칸은 거짓말이다."""
    out = ["| 항목 | 값 |", "|---|---|"]
    for k, v in rows:
        out.append("| %s | %s |" % (k, "미측정" if v in (None, "") else v))
    return out


def build(state, data, timing=None):
    """보고서 마크다운. 반환: (text, missing_sections).

    **필수 섹션이 빠져도 파이프라인을 실패시키지 않는다** — 기록만
    한다. 보고서가 런을 실패시키면, 보고서를 안 쓰는 것이 이득이 된다.
    """
    grade = state.get("grade") or "미정"
    gaps = state.get("gaps") or []
    narrative = (data.get("narrative") or {})
    budget = ((state.get("budget") or {}).get("model_calls") or {})
    tests = state.get("tests") or {}
    r05 = state.get("review05") or {}
    r07 = state.get("review07") or {}

    lines = ["# 런 보고서 — %s" % state.get("run_id"), ""]
    lines += ["> 요청 슬러그: `%s`" % (state.get("slug") or "?"), ""]

    lines += ["## 완료 등급", "", "**%s**" % grade, ""]
    if gaps:
        lines.append("건너뛴 비차단을 아래 `## 건너뛴 게이트` 에 나열한다.")
    else:
        lines.append("건너뛴 비차단이 없다.")
    lines.append("")

    lines += ["## 건너뛴 게이트", ""]
    if gaps:
        lines += ["- %s" % explain_gap(g) for g in gaps]
    else:
        lines.append("없다.")
    lines.append("")

    lines += ["## 비용과 시간", ""]
    lines += _tbl([
        ("모델 호출 수", "%s / %s%s" % (
            budget.get("total"), budget.get("max"),
            ("\n  기준: **%s** — 봉투가 에이전트 기동을 지시한 횟수다.\n%s"
             % (budget.get("basis"),
                "\n".join("  - %s" % b
                          for b in budget.get("blind_spots") or [])))
            if budget.get("basis") else "")),
        # **05 와 07 을 나란히 본다** (ADR-H043). 07 을 조건부로 만든 정책의
        # 근거가 여기서 쌓인다 — 페이즈별로 갈라 적지 않으면 총계만 남는다.
        ("페이즈별 호출", " · ".join(
            "%s: %s" % (k, v)
            for k, v in sorted((budget.get("by_phase") or {}).items()))
         or None),
        ("라운드", _counter_cell((state.get("counters") or {}).get("round"))),
        ("수리", _counter_cell((state.get("counters") or {}).get("repair"))),
        ("리뷰 수리",
         _counter_cell((state.get("counters") or {}).get("review_repair"))),
        ("테스트 실행 수", tests.get("ran")),
        ("테스트 상태", tests.get("status")),
        # **자진신고이지 실측이 아니다** (ADR-H052). 어느 모델이 돌았는지
        # 실행기는 보지 못한다 — blind spot 이 셀 안에 같이 적힌다.
        ("자진신고 모델(model_used)", _models_cell(state)),
    ])
    lines += _timing_lines(timing)

    lines += ["## 리뷰", ""]
    lines += _tbl([
        ("05 상태", r05.get("status")),
        ("05 리뷰어", "%s / %s" % (r05.get("reviewers_ok"),
                                   r05.get("reviewers_planned"))),
        # 레인이 정한 지시 범위다 (ADR-H059). `diff+refs` 로 05 벽시계가 늘면
        # 이 행과 「07 escaped」 를 나란히 놓고 depth 값을 다시 정한다.
        ("05 리뷰 범위", r05.get("depth")),
        ("절단됨", r05.get("truncated")),
        ("맥락 부족 요청", len(r05.get("need_more_context") or []) or 0),
        # 07 은 `/code-review` 1회의 계수다. escaped 는 **메인의 선언**이다 —
        # 05 가 낸 키를 가리키지 않은 Critical/Major 만 센다 (dup_05=false).
        ("07 /code-review", ("%s — %s" % (r07.get("code_review"), r07.get("skip_reason"))
                             if r07.get("skip_reason") else r07.get("code_review"))),
        ("07 escaped (dup_05=false 인 Major+)",
         ("%d (findings %s · dup_05 %s)"
          % (len(r07.get("escaped") or []), r07.get("findings"), r07.get("dup_05")))
         if r07.get("findings") is not None else None),
        # **프로파일이 리뷰어 상한을 정한다.** 그 값이 어디서 나왔는지가
        # 보고서에 없으면 "리뷰어 1명" 이 계획인지 결함인지 갈리지 않는다 (M34).
        ("프로파일", _profile_cell(state.get("profile"))),
        # 메인이 코드 근거로 기각한 01 지적 수 — 기각이 잦으면 리뷰어가 아니라
        # 기각이 검토 대상이다. 근거는 `01_review_r{n}.json` 의 `false_positive` 다.
        ("01 기각(false_positive)", _false_positive_count(state)),
        # 레인이 실제로 적용한 양보 (ADR-H044).
        ("레인 양보",
         " · ".join((state.get("profile") or {}).get("applied") or []) or None),
    ])
    # 05 가 낸 키를 가리키지 않은 Critical/Major — 사람이 정할 목록이다.
    for f in r07.get("escaped") or []:
        lines.append("- **07 escaped** `%s` — %s (`%s`)"
                     % (f.get("severity"), f.get("title"), f.get("path") or "경로 없음"))
    lines.append("")


    lines += ["## 서술", ""]
    if narrative:
        for k in ("문제", "원인", "해결", "결과", "배운 점"):
            if narrative.get(k):
                lines += ["### %s" % k, "", str(narrative[k]), ""]
        for k, v in narrative.items():
            if k not in ("문제", "원인", "해결", "결과", "배운 점") and v:
                lines += ["### %s" % k, "", str(v), ""]
    else:
        lines += ["_서술이 비어 있다. 표는 실행기가 조립했으므로 사실은 "
                  "남았지만, 왜 그랬는가는 이 런이 말하지 않았다._", ""]

    for k, head in (("contract_gaps", "계약이 어디서 부족했는가"),
                    ("review_scope", "05 리뷰 범위가 적절했는가"),
                    ("next_run", "다음 런에서 바꿀 것")):
        if data.get(k):
            lines += ["### %s" % head, "", str(data[k]), ""]

    text = "\n".join(lines)
    missing = [s for s in REQUIRED_SECTIONS if s not in text]
    return text, missing
