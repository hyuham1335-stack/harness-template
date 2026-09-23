#!/usr/bin/env python3
"""04-gate — 스테이지 체인 실행과 등급 산정.

체인은 두 구간이다. `loop_stage: true` 까지가 재작업 루프이고, 전체 회귀는 루프가
끝난 뒤 한 번이다. **루프 안에서 전체 회귀를 돌리지 않는 것이 이 설계의 가장 큰
절감이다.**

`runner` 는 스테이지 실행만 갈아끼운다(테스트가 스텁을 준다). 등급·리포트·테스트
수 신호는 실행 경로와 문자 그대로 같다. 실패를 역할에 귀속하지 않는다 — 작성자가
하나이고, 실패 출력은 `cli._gate_fail` 이 그대로 되돌린다 (ADR-H075).
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402
import adapters  # noqa: E402
import contract as contract_mod  # noqa: E402
import precheck as pc  # noqa: E402
import report as rep  # noqa: E402
import state as st  # noqa: E402

# 등급 어휘의 단일 출처는 `state.GRADES` 다. 여기서 문자열을 다시 적으면
# 두 곳이 갈라지고, 갈라진 것을 알아차리는 것은 갈라진 뒤다.
GRADE_PASS, GRADE_GAPS, GRADE_INCOMPLETE = st.GRADES


def run_gate(root, config, adapter, state, phase_front,
             run_dir, only_stage=None, runner=None, log_path=None):
    """게이트 한 회차. 반환은 리포트 dict 이고 상태를 고치지 않는다."""
    root = Path(root)
    changed = pc.changed_files(root, "worktree") or []

    steps = ((phase_front.get("gate") or {}).get("steps") or [])
    if only_stage:
        wanted = resolve_stage_selector(steps, only_stage)
        steps = [s for s in steps if s.get("id") in wanted]

    parsed = _parse_contract(root, config, state)

    results, gaps = [], []
    loop_failed = None
    for step in steps:
        sid = step.get("id")
        in_loop = bool(step.get("loop_stage")) or _before_loop_end(steps, sid)
        if loop_failed and in_loop:
            break
        result = _run_one(root, adapter, sid, step, changed, parsed, runner, log_path)
        results.append(result)
        if result["state"] == "skipped":
            gaps.append("stage_%s:%s" % (result["reason"], sid))
            continue
        if result["exit"] != 0:
            loop_failed = result
            if (phase_front.get("gate") or {}).get("fail_fast", True):
                break

    tests = _tests_signal(root, adapter, results)
    if tests:
        # **`ran is None`(리포트를 못 찾았다)과 `ran == 0`(테스트가 0개다)은
        # 다른 사실이다.** 앞의 것은 경로 설정 오류일 수 있어 인프라로 다루고,
        # 뒤의 것은 그린필드에서 정상이지만 초록불은 아니다.
        if tests.get("ran") is None:
            gaps.append("test_report_missing")
        elif tests["ran"] == 0:
            gaps.append("tests_ran_zero")

    report = {
        "schema": 1, "at": None, "stages": results, "gaps": gaps,
        "contract": {"units": len(parsed.get("units") or []) if parsed else 0,
                     "entrypoints": len(parsed.get("entrypoints") or []) if parsed else 0,
                     "unmatched": (parsed or {}).get("unmatched") or [],
                     "scope": (parsed or {}).get("scope")},
    }
    # **scoped 가 사실상 full 이면 그렇게 부르지 않는다.** 선택자를 넓히면
    # 이 자리가 새 조용한 통과가 된다 — "scoped 통과" 라고 적으면서 전체를
    # 도는 것은 파일럿이 잰 절감(ADR-H031)이 사라진 것이고, 아무도 모른다.
    if parsed and (parsed.get("scope") or {}).get("degenerate"):
        gaps.append("scoped_degenerate")
    if tests is not None:
        report["tests"] = tests

    report["failed"] = loop_failed
    demoting = [g for g in gaps if not rep.is_non_demoting(g)]
    report["grade"] = GRADE_PASS if (not demoting and loop_failed is None) else (
        GRADE_GAPS if loop_failed is None else None)
    report["gaps"] = gaps
    return report


def resolve_stage_selector(steps, selector):
    """`--stage` 인자를 스테이지 id 목록으로. **선언이 단일 출처다** (ADR-H046).

    - `loop` — 그 페이즈의 루프 구간 전부 (`loop_stage` 까지). 04 는
      compile·lint·check·scoped, 05 는 compile·scoped 다. 지시문이 스테이지
      이름을 나열하면 페이즈 선언을 고쳐도 지시문은 옛 목록을 돈다 — 파일럿
      e355 의 재게이트가 `--stage scoped` 하나만 돌아 타입 에러를 흘린 것이
      그 모양이다 (커밋 `de4760e`).
    - `a,b` — 쉼표 목록.
    - 그 밖 — 단일 id (종전 동작).
    """
    if selector == "loop":
        return [s.get("id") for s in steps
                if s.get("loop_stage") or _before_loop_end(steps, s.get("id"))]
    return [x.strip() for x in str(selector).split(",") if x.strip()]


def _before_loop_end(steps, sid):
    """루프 구간인가 — `loop_stage: true` 스테이지까지가 루프다."""
    for step in steps:
        if step.get("id") == sid:
            return True
        if step.get("loop_stage"):
            return False
    return False


def _run_one(root, adapter, sid, step, changed, parsed, runner, log_path):
    hit = adapters.when_touched_hit(adapter, sid, changed)
    if hit is False:
        return {"id": sid, "state": "skipped", "reason": "not_touched"}

    select = None
    if step.get("tests_from") == "contract":
        select = _selectors(parsed)
        if not select:
            # 전체 회귀로 낙하시키지 않는다. 낙하시키면 경로 필터의 절감이
            # 조용히 사라지고, 계약 파싱이 실패한 사실이 초록불에 묻힌다.
            return {"id": sid, "state": "skipped", "reason": "no_selector"}

    return adapters.run_stage(root, adapter, sid, select=select,
                              log_path=log_path, runner=runner)


def _selectors(parsed):
    if not parsed:
        return None
    return parsed.get("selectors") or None


def _parse_contract(root, config, state):
    """계약을 읽고 선택자를 미리 조립한다. 없으면 None."""
    rel = ((state or {}).get("contract") or {}).get("path")
    if not rel or not (Path(root) / rel).exists():
        return None
    text = (Path(root) / rel).read_text(encoding="utf-8")

    _config, adapter = adapters.load(root)
    parsed = contract_mod.parse(text, config)
    sel = contract_mod.test_selectors(root, config, adapter, parsed)
    parsed["selectors"] = sel["paths"]
    parsed["unmatched"] = sel["unmatched"]
    parsed["entrypoint_resolver"] = sel["entrypoint_resolver"]
    parsed["scope"] = {"selected": sel.get("selected"),
                       "test_files": sel.get("test_files"),
                       "repo_files": sel.get("repo_files"),
                       "ratio": sel.get("selected_ratio"),
                       "degenerate": bool(sel.get("degenerate"))}
    return parsed


def _tests_signal(root, adapter, results):
    """**"테스트가 몇 개 돌았는가"를 별도 신호로 본다.**

    빈 테스트 스위트는 통과하고, 통과는 초록불로 보인다. 그래서 게이트가
    초록불인 것과 테스트가 돈 것을 갈라 놓는다 (04-gate.md 「테스트 0개」).
    """
    ran_full = any(r["id"] == "full" and r["state"] == "ran" for r in results)
    if not ran_full:
        return None                     # 안 돌았다. **0 을 만들지 않는다**

    got = adapters.parse_report(root, adapter)
    sig = _tests_count(got, _tests_floor(root))
    if got.get("matched"):
        # 파일별 케이스 수 — 06 PR 본문의 검증 표가 읽는다 (ADR-H058 추기).
        # full 을 파싱하는 자리가 여기뿐이라 여기서 남긴다. 06 이 리포트를 다시
        # 읽으면 그 사이 scoped 가 덮어쓴 XML 을 full 의 실적으로 적는다.
        sig["by_file"] = got.get("by_file")
    return sig


def _tests_floor(root):
    """직전 완주 런의 `tests.ran` × 0.9. **없으면 None 이고 0 이 아니다.**

    캘리브레이션 파일이 하한을 들고 있던 때는 파일럿 15런 동안 14 로 굳어
    감지가 꺼져 있었다 — 값이 런과 같이 움직이게 완주 런에서 읽는다. `tests.ran`
    이 없는 완주 런(docs 레인처럼 full 이 안 돈 런)은 건너뛴다. `_workspace/`
    는 로컬 자료라 새 클론의 첫 런은 하한이 없다.
    """
    for s in reversed(harness.completed_runs(root)):
        ran = (s.get("tests") or {}).get("ran")
        if isinstance(ran, int) and ran > 0:
            return ran * 9 // 10
    return None


def _tests_count(got, floor):
    if not got.get("matched"):
        return {"ran": None, "expected_min": floor, "status": "none",
                "source": "report_glob",
                "note": "리포트를 한 건도 찾지 못했다 — 경로 설정을 확인한다"}
    ran = got.get("ran") or 0
    if ran == 0:
        return {"ran": 0, "expected_min": floor, "status": "none",
                "source": "report_glob"}
    if floor is None:
        return {"ran": ran, "expected_min": None, "status": "ok",
                "source": "report_glob",
                "note": "직전 완주 런이 없다 — 하한을 모른다"}
    if ran < floor:
        return {"ran": ran, "expected_min": floor, "status": "shrank",
                "source": "previous_run"}
    return {"ran": ran, "expected_min": floor, "status": "ok",
            "source": "previous_run"}
