# -*- coding: utf-8 -*-
"""`08-report` — 런이 스스로에 대해 말하는 자리.

**08 은 diff 도 코드도 읽지 않는다.** 입력은 `08_report_data.json` 하나뿐이고
전문은 파일 경로로만 가리킨다. 이 제약이 보고서의 비용을 런 크기와 무관하게
만든다.

분업이 요점이다 — **표는 실행기가 조립하고 서술은 모델이 쓴다.** 특히 승격
규칙 목록은 원장에서 자동으로 나오므로 **모델이 빠뜨릴 수 없다.**

그리고 `## 캘리브레이션 상태` 가 필수 섹션인 이유가 이 리포에서 지금 그대로
성립한다 — `calibration.json` 이 `partial: true` 이고 어댑터가
`verified: false` 다. 보고서가 그것을 적지 않으면 런은 초록불로 끝나고 다음
런이 같은 미검증 값을 물려받는다.
"""

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

REQUIRED_SECTIONS = ("## 완료 등급", "## 승격된 규칙", "## 건너뛴 게이트",
                     "## 비용과 시간", "## 캘리브레이션 상태")

# 서술 필드의 하한 (ADR-H052 결정 5). `5568` 의 08 은 서술이 통째로 비었고
# 그 런은 07 major 6건으로 최다였다 — 「왜 그랬는가는 이 런이 말하지 않았다」.
# **등급은 건드리지 않는다.** 봉투가 되묻는다. 미검증 초기값이다.
NARRATIVE_MIN_CHARS = 80
NARRATIVE_REQUIRED = (("narrative", "배운 점"), ("next_run",))

PILOT_LOG_REL = "docs/harness/PILOT-LOG.md"
PILOT_LOG_HEADING = "## 런 기록"
_PILOT_RUN_RE = re.compile(r"^## 파이프라인 런 P(\d+) — ", re.M)

# `gaps[]` 의 어휘. **명세가 열거형으로 주지 않았다** — 문서 전체에 흩어진
# `PASS_WITH_GAPS` 유발 사유를 여기 모은 것이고, 그 사실을 적어 둔다.
# 모아 두지 않으면 새 사유가 어휘 없이 들어가 보고서가 그것을 설명하지 못한다.
GAP_REASONS = {
    "stage_absent": "어댑터에 그 스테이지가 없다 (`cmd: null`)",
    "stage_na": ("이 스택에 구조적으로 없는 스테이지다 — 어댑터가 "
                 "`not_applicable` 로 사유를 선언했다. 표시이고 등급은 "
                 "내리지 않는다 (ADR-H047 추기)"),
    "stage_not_touched": "그 스테이지가 볼 변경이 없었다",
    "adapter_unverified": ("어댑터의 귀속 규칙이 실물 실패에서 판정을 낸 적이 "
                           "없다 — 아직 안 겪어봤다는 표시이지 이번 런의 결함이 "
                           "아니다. 등급은 내리지 않는다 (ADR-H069)"),
    "attribution_unparsed": ("스테이지가 실패했는데 귀속이 실패 항목을 하나도 "
                             "못 읽었다 — 어댑터의 파싱 규칙이 실물 출력에 "
                             "안 맞는다 (ADR-H069)"),
    "cross_verify_unavailable": "교차검증 primary·fallback 이 둘 다 불가였다",
    "cross_verify:fallback": ("01 의 교차검증이 폴백으로 돈 회차가 있다 — "
                              "독립 관측 둘이라는 전제가 그만큼 약해졌다"),
    "review05": "05 의 리뷰어가 전부 또는 일부 실패했다",
    "external": "외부 PR 리뷰를 받지 못했다",
    "infra_skipped": "인프라 프로브 실패로 건너뛴 검증이 있다",
    "tests_not_ran": "테스트가 한 건도 돌지 않았다",
    "pr_closed": "PR 이 닫혔다 — 수리·코멘트를 하지 않았다",
    "pr_merged": "PR 이 이미 머지됐다 — 수리·코멘트를 하지 않았다",
    "local_only": "원격이 없어 로컬 커밋까지만 했다",
    "promotion_baseline_unverified":
        "어댑터에 `baseline_cmd` 가 없어 lint 승격이 무엇을 막는지 재지 못했다",
    "promotion_selfgate_unverified":
        ("어댑터에 `lint` 또는 `check` 명령이 없어 승격 자체 게이트를 돌리지 "
         "못했다 — 규칙이 기존 코드를 깨는지 재지 못한 채 적용했다"),
    "triage_miss": ("00 의 레인 예측이 빗나가 앞 페이즈가 그 양보(콜론 뒤)를 "
                    "적용한 채 지나갔다 — 03·05 의 실물이 상향으로 재판정했다"),
    # 아래 다섯은 gate.py 가 처음부터 만들던 사유인데 어휘에 없었다 — 파일럿
    # 40dc 의 보고서가 `stage_no_selector:scoped` 를 "어휘에 없는 사유다" 로
    # 적었다 (ADR-H050). 코어가 만드는 사유는 전부 여기 있어야 하고, 그것을
    # `TestGapVocabulary` 가 코드를 스캔해 강제한다.
    "stage_no_selector": ("계약에서 테스트 선택자를 하나도 못 뽑아 scoped 를 "
                          "건너뛰었다 — 계약의 `## 유닛` 이 비었거나 형식이 "
                          "다르다. 전체 회귀로 낙하시키지 않았다"),
    "scoped_degenerate": ("scoped 선택자가 사실상 전체 회귀다 — 절감이 없는데 "
                          "\"scoped 통과\" 로 적히는 것을 막는다"),
    "uncalibrated_run": "캘리브레이션 파일이 없다 — 타임아웃·테스트 수 하한을 모른다",
    "test_report_missing": ("테스트 리포트를 한 건도 찾지 못했다 — 리포터 "
                            "경로 설정 오류일 수 있어 인프라로 다룬다"),
    "tests_ran_zero": "테스트가 0개 돌았다 — 빈 스위트의 초록불은 통과가 아니다",
    "promotion_overdue": ("승격 판정 시한(ADR-H033)이 지났는데 이 런이 후보를 "
                          "skip 으로 닫았다 — 미룸이 등급을 치른다 (ADR-H051)"),
    "run_record_missing": ("닫힌 런의 PR 갱신인데 런 기록 "
                           "`docs/harness/pipeline/runs/{run_id}.md` 가 diff 에 "
                           "없다 — 08 이 쓴 기록은 기능 PR 에 실린다 (ADR-H052)"),
    "calibration_stale": ("캘리브레이션 측정 뒤 완주 런이 기준 이상 쌓였다 — "
                          "게이트의 타임아웃·테스트 수 하한이 옛 실측이다. "
                          "`python scripts/harness.py calibrate` 로 다시 잰다. "
                          "표시이고 등급은 내리지 않는다 (ADR-H047)"),
    # 08 지시문 검토 (ADR-H056 추기). 앞 셋은 표시이고 등급을 내리지 않는다.
    "instruction_review_manual": ("08 지시문 검토를 스킬 없이 사람이 했다 — "
                                  "config 의 `instruction_review.skill` 이 null 이다"),
    "instruction_slot_over_budget": ("지시문 파일의 최상위 불릿 수가 "
                                     "`instruction_slot_budget` 을 넘었다 — 예산을 "
                                     "고치거나 규칙을 줄인다. 표시이고 등급은 "
                                     "내리지 않는다"),
    "instruction_slot_unmeasured": ("지시문 파일에 본문은 있는데 최상위 불릿이 "
                                    "0개다 — 규칙 수를 재지 못했다"),
    "instruction_changed": ("08 지시문 검토가 바꾼 지시문 파일이 기능 PR 에 "
                            "실렸다 — 06 승인 지문 밖이라 05·07 리뷰어가 보지 "
                            "않았다. PR 본문 「규칙 변경」 절을 본다"),
    "instruction_change_missing": ("08 지시문 검토가 바꿨다고 적은 파일이 닫힌 "
                                   "런의 PR 갱신에 커밋돼 있지 않다 — 커밋하고 "
                                   "`pr --run-id` 를 다시 돌린다"),
}

# 등급을 내리지 않는 gap. `gaps[]` 에는 남아 보고서·PR 본문이 이름으로 적되
# `demote` 는 등급을 건드리지 않는다 — "관측 결손" 이 아니라 "사람이 할 일이
# 밀렸다" 는 표시다 (ADR-H047 결정 2). 부르는 쪽(`cli.run_precheck`)이 이
# 목록으로 가른다.
# `adapter_unverified` 는 ADR-H069 에서 들어왔다. 이 gap 이 매 런 등급을 깎는
# 압력 때문에 ADR-H047 결정 3 이 승격 기준을 "완주 런 3개" 로 낮췄다 — 완주는
# 실패 경로의 근거가 아니다. 기준은 증거 기반으로 올리고 이 표시는 여기로 내린다.
NON_DEMOTING_GAPS = ("calibration_stale", "instruction_review_manual",
                     "instruction_slot_over_budget", "instruction_slot_unmeasured",
                     "instruction_changed", "adapter_unverified")


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


def _sum_phase(timing, key):
    return sum((c.get(key) or 0) for c in (timing or {}).get("phases", {}).values())


def pilot_log_section(state, timing, report_rel, number):
    """PILOT-LOG 의 런 절. **파일 상단의 골격 그대로**이고, 상태에 있는 값만
    채우고 나머지는 「미측정」이다 — 칸을 지우면 재본 적 없다는 사실도 같이
    사라진다 (ADR-H052 결정 4).
    """
    run_id = state.get("run_id")
    stamp = state.get("closed_at") or state.get("updated_at") or ""
    day = stamp[:10] if stamp else "미측정"
    adapter = state.get("adapter") or {}
    pr = state.get("pr") or {}
    gaps = state.get("gaps") or []
    tests = state.get("tests") or {}
    promos = [p for p in (state.get("promotions") or [])
              if p.get("status") == "applied"]
    lines = ["## 파이프라인 런 P%d — `%s` (%s)" % (number, state.get("slug") or "?",
                                                   day), "",
             "| 항목 | 값 |", "|------|-----|",
             "| 일자 | %s |" % day,
             "| 런 ID | `_workspace/runs/%s` |" % run_id,
             "| 브랜치 | %s |" % ("`%s`" % pr["head"] if pr.get("head") else "미측정"),
             "| 대상 | 런 보고서 `%s` 의 계약 절을 본다 (계약은 06 에서 지워진다) |"
             % report_rel,
             "| 어댑터 | `%s` (`verified: %s`) |"
             % (adapter.get("id") or "미측정",
                str(bool(adapter.get("verified"))).lower()),
             "| 결과 | %s · gaps %d%s |"
             % (state.get("grade") or "미정", len(gaps),
                (" (" + ", ".join(gaps) + ")") if gaps else ""),
             "| 머신 | 미측정 |", "",
             "### 스테이지 실측", "",
             "| 스테이지 | 소요 | 종료 코드 | 테스트 | 비고 |",
             "|---|---:|---:|---:|---|"]
    phases = (timing or {}).get("phases") or {}
    if phases:
        for name in sorted(phases):
            c = phases[name]
            waits = (c.get("escalation_wait_sec") or 0) + (c.get("human_wait_sec") or 0)
            lines.append("| %s | %s | — | — | 페이즈 벽시계 · 순 작업 %s · 대기 %s |"
                         % (name, _hms(c.get("wall_sec")), _hms(c.get("work_sec")),
                            _hms(waits) if waits else "—"))
    else:
        lines.append("| — | 미측정 | 미측정 | 미측정 | 이벤트가 없어 소요를 유도하지 못했다 |")
    lines.append("| 테스트 | — | — | %s | %s |"
                 % (tests.get("ran") if tests.get("ran") is not None else "미측정",
                    tests.get("status") or "미측정"))
    lines += ["", "### 이 런이 확인하기로 했던 것 — 그리고 결과", "",
              "미측정 — 런 전에 적은 예측이 상태에 없다. 런 보고서 `%s` 의 서술을 본다."
              % report_rel, "",
              "### 막힌 지점 · 수동 개입", ""]
    esc = _sum_phase(timing, "escalations")
    human = (timing or {}).get("human_wait_sec")
    if timing:
        lines.append("에스컬레이션 %d회 · 사람 판단 대기 %s · 형식 반려 %d회"
                     % (esc, _hms(human) if human else "—",
                        _sum_phase(timing, "format_rejects")))
    else:
        lines.append("미측정")
    lines += ["", "### 이 런이 연 하네스 결함", "",
              "| ID | 무엇 | 상태 |", "|---|---|---|",
              "| — | 미측정 — 사람이 적는다 | |", "",
              "### 이 런이 승격 판단에 주는 답", "",
              ("승격 적용 %d건: %s" % (len(promos), ", ".join(
                  "`%s`" % p.get("rule_id") for p in promos))
               if promos else "승격된 규칙 없음 — ROADMAP §6 표는 움직이지 않았다."),
              "", "### 다음 런에서 볼 것", "",
              "런 보고서 `%s` 의 「다음 런에서 바꿀 것」." % report_rel, ""]
    return "\n".join(lines)


def append_pilot_log(root, state, timing, report_rel):
    """`## 런 기록` 아래에 런 절을 붙인다. 같은 run_id 절은 **교체**한다(멱등).

    파일이나 헤딩이 없으면 아무것도 하지 않고 False — 템플릿 자신은 파일럿
    기록을 싣지 않는다 (ADR-H039). 클론의 PILOT-LOG 는 클론의 것이다.
    """
    path = Path(root) / PILOT_LOG_REL
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if PILOT_LOG_HEADING not in text:
        return False
    head, tail = text.split(PILOT_LOG_HEADING, 1)
    starts = [m.start() for m in _PILOT_RUN_RE.finditer(tail)]
    blocks = [tail[a:b] for a, b in zip(starts, starts[1:] + [len(tail)])]
    prefix = tail[:starts[0]] if starts else tail
    marker = "_workspace/runs/%s`" % state.get("run_id")
    replaced = False
    out = []
    for b in blocks:
        if marker in b and not replaced:
            m = _PILOT_RUN_RE.match(b)
            number = int(m.group(1)) if m else len(out) + 1
            out.append(pilot_log_section(state, timing, report_rel, number)
                       .rstrip("\n") + "\n\n")
            replaced = True
        else:
            out.append(b if b.endswith("\n") else b + "\n")
    if not replaced:
        out.append("\n" + pilot_log_section(state, timing, report_rel,
                                            len(blocks) + 1))
    new = head + PILOT_LOG_HEADING + prefix.rstrip("\n") + "\n" + "".join(out)
    path.write_text(new.rstrip("\n") + "\n", encoding="utf-8")
    return True


def _ledger_axis_lines(data):
    """원장의 카테고리 축 빈도. **승격하지 않는 관측이다** (M39 · ADR-H026).

    승격 버킷의 축은 **규칙**(`rule_key`)이고 통제 어휘가 없는 지적은
    `finding_key` 로 낙하한다 (ADR-H034). 그래서 카테고리가 아무리 잦아도
    자유 서술이면 임계에 닿지 않는다. 그 사실을 보고서가 말하지 않으면
    "승격 0건" 이 "지적이 없었다" 로 읽힌다.
    """
    roll = (data.get("ledger") or {}).get("by_category") or []
    if not roll:
        return []
    top = roll[:5]
    out = ["", "**원장의 카테고리 축** (승격 후보와는 다른 셈이다 — 승격은 "
               "규칙 단위이고 이 표는 카테고리 단위다):", "",
           "| category | 관측 | 런 | 서로 다른 규칙 | 승격 가능 |",
           "|---|---|---|---|---|"]
    out += ["| `%s` | %s | %s | %s | %s |"
            % (b.get("category"), b.get("count"), b.get("distinct_runs"),
               b.get("distinct_keys"),
               "예" if b.get("promotable") else "아니오")
            for b in top]
    out += ["", "**서로 다른 규칙 수가 관측 수와 같으면 그 카테고리는 임계에 "
                "닿지 않는다.** 자주 나는 것과 같은 것이 반복되는 것은 다른 "
                "사실이고, 승격이 배우는 것은 후자다. 통제 어휘를 쓰는 "
                "생산자만 이 칸이 관측 수보다 작아진다."]
    return out


def _prose_candidate_lines(data):
    """원장 승격이 아닌 두 갈래 (ADR-H056). 없으면 아무것도 안 찍는다."""
    led = data.get("ledger") or {}
    prose = led.get("prose_candidates") or []
    trace = led.get("trace_repeats") or []
    out = []
    if prose:
        out += ["", "**지시문 검토 후보 %d건** (08 검토 입력 — 목적지가 prose 라 "
                    "원장 승격하지 않는다, ADR-H056):" % len(prose), ""]
        out += ["- `%s`%s — %s회 / %s런"
                % (c.get("category"),
                   " / `%s`" % c["rule_slug"] if c.get("rule_slug") else "",
                   c.get("count"), c.get("distinct_runs"))
                for c in prose]
    if trace:
        out += ["", "**검사 반복 검출 %d건** — `contract-trace` 가 이미 막는 "
                    "규칙이라 후보가 아니다: %s"
                % (len(trace), " · ".join(
                    "`%s` %s회/%s런" % (c.get("rule_slug") or c.get("category"),
                                        c.get("count"), c.get("distinct_runs"))
                    for c in trace))]
    return out


def _instruction_review_cell(state):
    rv = state.get("instruction_review")
    if not rv:
        return None
    return "%s · 흡수 %d · 기각 %d · 바꾼 파일 %d" % (
        "`%s`" % rv["skill"] if rv.get("skill") else "사람 검토",
        len(rv.get("absorbed") or []), len(rv.get("declined") or []),
        len({c.get("file") for c in rv.get("changes") or []}))


def _slots_cell(state):
    sl = state.get("instruction_slots")
    if not sl:
        return None
    return "%s/%s (최상위 불릿 / `instruction_slot_budget`)" % (
        sl.get("used"), sl.get("budget"))


def _declined_lines(state, data):
    """기각된 지시문 검토 후보. 이월이 길어지는 것이 보이게 누적을 같이 적는다."""
    declined = (state.get("instruction_review") or {}).get("declined") or []
    if not declined:
        return []
    seen = {c.get("rule_key"): c for c in
            (data.get("ledger") or {}).get("prose_candidates") or []}
    out = ["", "**지시문 검토에서 기각한 후보**:", ""]
    for x in declined:
        c = seen.get(x.get("rule_key")) or {}
        out.append("- `%s`%s — %s" % (
            c.get("category") or x.get("rule_key"),
            " %s회 / %s런째 후보" % (c.get("count"), c.get("distinct_runs"))
            if c else "", x.get("reason")))
    return out


def _reporter_lines(data):
    """리뷰어별 해소 표 (ADR-H050). 리뷰어 품질의 첫 실측이고 승격과 무관하다."""
    rows = (data.get("ledger") or {}).get("by_reporter") or []
    if not rows:
        return []
    out = ["", "**리뷰어별 해소** (원장 누적 — `false_positive` 는 메인이 틀렸다고 "
               "확인한 지적이고 승격 집계에서 빠진다):", "",
           "| 리뷰어 | 관측 | repaired | deferred | false_positive | warn_only |",
           "|---|---|---|---|---|---|"]
    out += ["| `%s` | %s | %s | %s | %s | %s |"
            % (b.get("reporter"), b.get("total"), b.get("repaired", 0),
               b.get("deferred", 0), b.get("false_positive", 0),
               b.get("warn_only", 0))
            for b in rows[:8]]
    out += ["", "`deferred` 가 크면 미룬 것이고 `false_positive` 가 크면 그 "
                "리뷰어의 체크리스트를 고칠 때다 — 둘은 다른 처방이다."]
    return out


def _verdict_deadline_lines(data):
    """승격 임계·축을 **언제** 판정하는지 (ADR-H033).

    `## 승격된 규칙` 이 "없다" 로 끝나면 그 말이 몇 런까지 정상인지 아무도
    모른다 — `THRESHOLDS` 의 옛 약속(*"첫 세 런의 원장이 이 값을 검사한다"*)
    이 두 배 지나도록 아무도 판정하지 않은 이유가 그것이다. **게이트가
    아니라 표시다**: 시한이 지나도 등급을 바꾸지 않는다.
    """
    dl = (data.get("ledger") or {}).get("verdict_deadline") or {}
    if not dl:
        return []
    tail = ("**시한이 지났다 — 판정할 때다.**" if dl.get("due")
            else "남은 런 %d." % dl.get("remaining"))
    return ["", "**승격 판정 시한** — 원장이 본 런 %s / %s. %s"
            % (dl.get("seen"), dl.get("at"), tail),
            "",
            "이 셈의 단위는 `distinct_runs` 다 — **지적을 0건 낸 런은 "
            "세어지지 않는다.** 달력의 런 수와 다를 수 있다. 그때 무엇을 "
            "보고 어떻게 가를지는 **ADR-H033** 에 미리 적혀 있고, 판정할 "
            "때 고르는 것이 아니다."]


def gap_reason(gap):
    """어휘 조회의 단일 출처 — **전체 키가 먼저, 머리가 그다음**이다.

    `cross_verify:fallback` 처럼 콜론까지가 키인 항목이 있는데 머리만 찾으면
    "어휘에 없는 사유" 가 된다. 08 보고서와 06 PR 본문이 같은 함수를 쓴다.
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
    """`이름 (출처 · 유닛 n)`. 재판정이 있었으면 `무엇에서 무엇으로` 까지.

    00 이 예측한 런은 누가 정했는지(기계 · 모델 · 사람)와 빗나갔는지도 적는다 —
    임계값을 고칠 근거가 이 칸에서 나온다 (ADR-H044).
    """
    if not node:
        return None
    cell = "%s (%s · 유닛 %s)" % (node.get("name"), node.get("source"),
                                  node.get("units"))
    pred = node.get("predicted")
    if pred:
        cell = "%s — 00 예측 %s (%s)" % (cell, pred.get("profile"),
                                        pred.get("decided_by"))
    prev = node.get("previous")
    if prev:
        cell = "%s — 다시 셌다: %s(유닛 %s) → %s(유닛 %s)" % (
            cell, prev.get("name"), prev.get("units"),
            node.get("name"), node.get("units"))
    miss = node.get("triage_miss")
    if miss:
        cell = "%s — **빗나감**: %s → %s (%s)" % (
            cell, miss.get("was"), miss.get("became"), miss.get("at"))
    return cell


def _triage_cell(state):
    """00 이 무엇을 보고 정했나. 없으면 None — 표가 `미측정` 을 찍는다."""
    node = (state.get("phases") or {}).get("00-triage") or {}
    if not node.get("decided_by"):
        return None
    sig = node.get("signals") or {}
    return "%s → %s (소유 경로 %d · docs 경로 %d · 미해결 %d · %s자)" % (
        node.get("decided_by"), node.get("profile"),
        len(sig.get("paths_role_owned") or []),
        len(sig.get("paths_docs") or []),
        len(sig.get("paths_unresolved") or []),
        sig.get("request_chars"))


def _models_cell(state):
    """봉투가 지시한 등급 + 리뷰어의 자진신고. **둘 다 실측이 아니다** — blind
    spot 을 함께 적는다 (ADR-H052 결정 2)."""
    node = state.get("models") or {}
    inst = node.get("instructed") or {}
    reported = node.get("reported") or {}
    if not inst and not reported:
        return None
    by = {}
    for tier in inst.values():
        by[tier or "inherit"] = by.get(tier or "inherit", 0) + 1
    head = " · ".join("%s: %d" % (k, v) for k, v in sorted(by.items())) or "지시 없음"
    if reported:
        seen = {}
        for m in reported.values():
            seen[m] = seen.get(m, 0) + 1
        head += "\n  자진신고(`model_used`): %s" % " · ".join(
            "%s: %d" % (k, v) for k, v in sorted(seen.items()))
    return "%s\n  기준: **%s** — 봉투가 지시한 등급과 리뷰어의 자진신고다.\n%s" % (
        head, node.get("basis"),
        "\n".join("  - %s" % b for b in node.get("blind_spots") or []))


def _cost_cell(cost):
    """「비용(있으면)」. 값이 없으면 **`미계측`** 이라고 적는다 — 0 이 아니다.

    `cmd_cost` 는 트랜스크립트의 `cost-state` 를 읽고, 08 을 돌리는 세션 자신은
    아직 그 줄을 안 썼다 — 그래서 값이 있어도 **미완**이다 (ADR-H032 · H052).
    파일럿 클론처럼 `cmd_cost` 가 없는 리포에서는 항상 `미계측` 이다.
    """
    if not cost or "cost_usd" not in cost:
        return "미계측 — `cmd_cost` 가 값을 내지 못했다"
    sessions = [c for c in (cost.get("sessions") or [])
                if c.get("basis") == "touched"]
    return "$%.2f (세션 %d · 읽지 못한 세션 %d · 08 세션은 미완이라 제외)" % (
        cost["cost_usd"], len(sessions), cost.get("unread_sessions") or 0)


def _counter_cell(node):
    """`used / max` 와, 지급이 있었으면 그 사실까지.

    지급(`counter_grant`)은 상한만 올리고 `used` 는 안 건드린다. 그래서 `used`
    만 적으면 왕복 뒤 예산을 더 받았다는 것이 보고서에서 사라진다 (M32).
    """
    if not node:
        return None
    used, max_ = node.get("used"), node.get("max")
    cell = "%s / %s" % (used, max_) if max_ is not None else used
    grants = node.get("grants") or []
    if grants:
        cell = "%s (왕복 뒤 %d 지급: %s)" % (
            cell, sum(g.get("extra") or 0 for g in grants),
            "; ".join(g.get("reason") or "" for g in grants))
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


def build(state, data, calibration, promotions, timing=None, cost=None):
    """보고서 마크다운. 반환: (text, missing_sections).

    **필수 섹션이 빠져도 파이프라인을 실패시키지 않는다** — 원장에 기록만
    한다. 보고서가 런을 실패시키면, 보고서를 안 쓰는 것이 이득이 된다.
    """
    grade = state.get("grade") or "미정"
    gaps = state.get("gaps") or []
    narrative = (data.get("narrative") or {})
    budget = ((state.get("budget") or {}).get("model_calls") or {})
    tests = state.get("tests") or {}
    r05 = state.get("review05") or {}
    r07 = state.get("review07") or {}
    audit = state.get("audit") or {}
    cv = state.get("cross_verify") or {}

    lines = ["# 런 보고서 — %s" % state.get("run_id"), ""]
    lines += ["> 요청 슬러그: `%s`" % (state.get("slug") or "?"), ""]

    lines += ["## 완료 등급", "", "**%s**" % grade, ""]
    if gaps:
        lines.append("건너뛴 비차단을 아래 `## 건너뛴 게이트` 에 나열한다.")
    else:
        lines.append("건너뛴 비차단이 없다.")
    lines.append("")

    lines += ["## 승격된 규칙", ""]
    applied = [p for p in promotions or [] if p.get("status") == "applied"]
    if applied:
        lines += ["| 규칙 | category | 사유 |", "|---|---|---|"]
        lines += ["| `%s` | %s | %s |" % (p.get("rule_id"), p.get("category"),
                                          p.get("reason") or "-")
                  for p in applied]
    else:
        lines.append("이 런에서 승격된 규칙이 없다.")
    other = [p for p in promotions or [] if p.get("status") != "applied"]
    if other:
        lines += ["", "승격되지 않은 것 %d 건:" % len(other)]
        lines += ["- `%s` — **%s** · %s" % (p.get("rule_id"), p.get("status"),
                                            p.get("reason") or "사유 없음")
                  for p in other]
    lines += _prose_candidate_lines(data)
    lines += _ledger_axis_lines(data)
    lines += _reporter_lines(data)
    lines += _verdict_deadline_lines(data)
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
        # **지급이 드러나야 한다.** `used` 만 적으면 다섯 라운드를 쓴 런과 세
        # 라운드를 쓰고 둘을 더 받은 런이 같아 보인다 (M32).
        ("라운드", _counter_cell((state.get("counters") or {}).get("round"))),
        ("수리", _counter_cell((state.get("counters") or {}).get("repair"))),
        ("리뷰 수리",
         _counter_cell((state.get("counters") or {}).get("review_repair"))),
        ("테스트 실행 수", tests.get("ran")),
        ("테스트 상태", tests.get("status")),
        # **지시된 등급이지 실측이 아니다** (ADR-H044). 어느 모델이 돌았는지
        # 실행기는 보지 못한다 — blind spot 이 셀 안에 같이 적힌다.
        ("지시된 모델 등급", _models_cell(state)),
        # 비용은 있으면 적고 없으면 `미계측` 이다. 0 으로 적지 않는다.
        ("비용(있으면)", _cost_cell(cost)),
    ])
    lines += _timing_lines(timing)

    lines += ["## 리뷰", ""]
    lines += _tbl([
        ("05 상태", r05.get("status")),
        ("05 리뷰어", "%s / %s" % (r05.get("reviewers_ok"),
                                   r05.get("reviewers_planned"))),
        # 레인이 정한 지시 범위다 (ADR-H059). `diff+refs` 로 05 벽시계가 늘면
        # 이 행과 `escaped_05` 를 나란히 놓고 depth 값을 다시 정한다.
        ("05 리뷰 범위", r05.get("depth")),
        ("검토 제외로 드롭", r05.get("dropped_by_enforcement")),
        ("절단됨", r05.get("truncated")),
        ("맥락 부족 요청", len(r05.get("need_more_context") or []) or 0),
        ("외부 리뷰", (r07.get("external") or {}).get("status")),
        ("내장 리뷰", r07.get("code_review")),
        # **이 지표를 그대로 읽으면 안 된다** (M48). 대조는 키 일치와 07 의
        # 선언 둘이고, 07 이 같은 결함에 다른 이름을 붙이고 선언도 안 하면
        # 여전히 새 것으로 세어진다. 접힌 수를 함께 적어 그 성격을 드러낸다.
        ("escaped_05", "%s (05 와 접힘 %s · 키 일치 + 선언 대조)%s"
         % (r07.get("escaped_05"), r07.get("deduped"),
            # 생략 런의 0 은 "봤는데 없었다" 가 아니다. 정책의 표본은 감사
            # 런과 수리 런에서만 나온다 (ADR-H043).
            " — 내장 리뷰 생략, 표본 아님"
            if r07.get("code_review") == "skipped" else "")
         if r07.get("escaped_05") is not None else None),
        # **생략과 불가는 다르다.** `clean_05` 는 05 의 gen 이 같은 관점을 이미
        # 봤고 수리가 없었던 것이라 등급이 안 내려간다 (ADR-H043).
        ("07 생략 사유", r07.get("skip_reason")),
        ("감사 런", audit.get("is_audit_run")),
        # **01 의 관측 품질이 이 표에 없었다.** 05·07 만 적어서, 교차검증이
        # 다섯 라운드 내내 폴백이어도 보고서는 아무 말도 하지 않았다 (P3).
        # **프로파일이 리뷰어 상한을 정한다.** 그 값이 어디서 나왔는지가
        # 보고서에 없으면 "리뷰어 1명" 이 계획인지 결함인지 갈리지 않는다 (M34).
        ("프로파일", _profile_cell(state.get("profile"))),
        # 00 이 무엇을 보고 정했고 어떤 양보가 실제로 적용됐나 (ADR-H044).
        ("00 트리아지", _triage_cell(state)),
        ("트리아지 적용 양보",
         " · ".join((state.get("profile") or {}).get("applied") or []) or None),
        ("01 교차검증", cv.get("mode")),
        ("폴백 회차", "%s / %s" % (cv.get("degraded_rounds") or 0,
                                   len(cv.get("rounds") or {}))),
        # **생략과 불가는 다르다** (ADR-H042). `no_risk` 는 01 INTENT 의 `risk`
        # 가 비어 있고 Critical 도 없었던 것(ADR-H060), `docs_profile` ·
        # `fix_profile` 은 레인의 양보다 — 셋 다 정책 스킵이라 등급이 안 내려간다.
        ("02 생략 사유", cv.get("skip_reason")),
        # 08 지시문 검토와 슬롯 예산 (ADR-H056 추기).
        ("지시문 검토", _instruction_review_cell(state)),
        ("지시문 슬롯", _slots_cell(state)),
    ])
    if cv.get("last_primary_error"):
        lines += ["", "- **교차검증 primary 가 실패한 적이 있다** — `%s`. "
                  "부재가 아니라 일시 실패다." % cv["last_primary_error"]]
    lines += _declined_lines(state, data)
    lines.append("")

    lines += ["## 캘리브레이션 상태", ""]
    partial = calibration.get("partial")
    verified = calibration.get("adapter_verified")
    lines += _tbl([
        ("측정 시각", calibration.get("measured_at")),
        ("부분 측정(partial)", partial),
        ("어댑터 verified", verified),
    ])
    notes = []
    if partial:
        notes.append("**`partial: true` 다** — 옛 값을 쓰는 스테이지가 있고, "
                     "거기서 유도된 정책은 그만큼 오래된 것이다.")
    if verified is False:
        notes.append("**어댑터가 `verified: false` 다** — 실패 경로가 실물에서 "
                     "돈 적이 없다. 이 런의 초록불은 그만큼만 말한다.")
        ready = data.get("adapter_verify") or {}
        missing = ready.get("rules_missing") or []
        if missing:
            # 완주 수만 보고 "명령 한 번만 치면 된다" 고 적으면 거짓말이다 —
            # 그 상태로 치면 exit 3 이다 (ADR-H069).
            notes.append("**귀속 규칙이 아직 판정을 낸 적 없다** — %s. "
                         "이 규칙이 실물 실패에서 한 번 돌면 `verify-adapter` 가 "
                         "근거와 함께 올린다." % ", ".join("`%s`" % m for m in missing))
        elif ready.get("qualified", 0) >= ready.get("min_runs", 1) > 0:
            notes.append("**기준 충족** — 전 페이즈 passed 완주 런 %d / 기준 %d 이고 "
                         "귀속 규칙이 전부 실물에서 판정을 냈다. "
                         "`python scripts/harness.py verify-adapter` 로 올린다 "
                         "(ADR-H047 결정 3 · ADR-H069)." % (ready["qualified"],
                                                           ready["min_runs"]))
    if "calibration_stale" in gaps:
        notes.append("**측정 뒤 완주 런이 기준 이상 쌓였다** (`calibration_stale`) — "
                     "다음 런 전에 `python scripts/harness.py calibrate` 로 다시 "
                     "잰다. 등급은 내리지 않았다 (ADR-H047).")
    lines += ([""] + ["- %s" % n for n in notes] if notes
              else ["", "- 캘리브레이션에 표시할 결손이 없다."])
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
