#!/usr/bin/env python3
"""04-gate — 스테이지 체인 실행과 등급 산정.

체인은 두 구간이다. `loop_stage: true` 까지가 재작업 루프이고, 전체 회귀는 루프가
끝난 뒤 한 번이다. **루프 안에서 전체 회귀를 돌리지 않는 것이 이 설계의 가장 큰
절감이다.**

`--replay` 는 러너만 갈아끼운다. 귀속·시그니처·flip·등급·리포트 쓰기는 실행
경로와 문자 그대로 같다 — 그래야 픽스처가 실물의 대역이 된다.
"""

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402
import adapters  # noqa: E402
import attribution as attr  # noqa: E402
import contract as contract_mod  # noqa: E402
import state as st  # noqa: E402

# 등급 어휘의 단일 출처는 `state.GRADES` 다. 여기서 문자열을 다시 적으면
# 두 곳이 갈라지고, 갈라진 것을 알아차리는 것은 갈라진 뒤다.
GRADE_PASS, GRADE_GAPS, GRADE_INCOMPLETE = st.GRADES


def replay_runner(fixture_dir):
    """픽스처의 manifest 로 러너를 대신한다. **서브프로세스를 부르지 않는다.**"""
    fixture = Path(fixture_dir)
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))

    def run(name, argv, cwd, timeout_sec):
        spec = (manifest.get("stages") or {}).get(name)
        if spec is None:
            return 0, ""
        text = ""
        if spec.get("stdout"):
            path = fixture / spec["stdout"]
            if path.exists():
                text = path.read_text(encoding="utf-8")
        return spec.get("exit", 0), text

    run.manifest = manifest
    run.fixture = fixture
    return run


def run_gate(root, config, adapter, calibration, state, phase_front,
             run_dir, only_stage=None, replay=None, log_path=None):
    """게이트 한 회차. 반환은 리포트 dict 이고 상태를 고치지 않는다."""
    root = Path(root)
    runner = report_root = None
    repo_files = changed = None
    if replay:
        runner = replay_runner(replay)
        manifest = runner.manifest
        report_root = Path(replay)
        repo_files = manifest.get("repo_files")
        changed = manifest.get("changed_paths") or []
    else:
        changed = attr._changed_paths(root) or []

    steps = ((phase_front.get("gate") or {}).get("steps") or [])
    if only_stage:
        wanted = resolve_stage_selector(steps, only_stage)
        steps = [s for s in steps if s.get("id") in wanted]

    parsed = _parse_contract(root, config, state, replay)
    symbols = contract_mod.symbols(parsed) if parsed else set()

    results, gaps = [], []
    loop_failed = None
    for step in steps:
        sid = step.get("id")
        in_loop = bool(step.get("loop_stage")) or _before_loop_end(steps, sid)
        if loop_failed and in_loop:
            break
        result = _run_one(root, adapter, calibration, sid, step, changed,
                          parsed, repo_files, runner, log_path)
        results.append(result)
        if result["state"] == "skipped":
            gaps.append("stage_%s:%s" % (result["reason"], sid))
            continue
        if result["exit"] != 0:
            loop_failed = result
            if (phase_front.get("gate") or {}).get("fail_fast", True):
                break

    tests = _tests_signal(root, adapter, calibration, results, report_root)
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
        "calibration": {"present": bool(calibration),
                        "partial": bool((calibration or {}).get("partial")),
                        "adapter_verified": bool(adapter.get("verified"))},
        "rules_inactive": attr.rules_inactive(adapter),
    }
    # **scoped 가 사실상 full 이면 그렇게 부르지 않는다.** 선택자를 넓히면
    # 이 자리가 새 조용한 통과가 된다 — "scoped 통과" 라고 적으면서 전체를
    # 도는 것은 M16 이 잰 절감이 사라진 것이고, 아무도 모른다.
    if parsed and (parsed.get("scope") or {}).get("degenerate"):
        gaps.append("scoped_degenerate")
    if not calibration:
        gaps.append("uncalibrated_run")
    if not adapter.get("verified"):
        gaps.append("adapter_unverified")
    if tests is not None:
        report["tests"] = tests

    report["failed"] = loop_failed
    report["symbols"] = sorted(symbols)
    report["grade"] = GRADE_PASS if (not gaps and loop_failed is None) else (
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


def _run_one(root, adapter, calibration, sid, step, changed, parsed,
             repo_files, runner, log_path):
    hit = adapters.when_touched_hit(adapter, sid, changed)
    if hit is False:
        return {"id": sid, "state": "skipped", "reason": "not_touched"}

    select = None
    if step.get("tests_from") == "contract":
        select = _selectors(root, adapter, parsed, repo_files)
        if not select:
            # 전체 회귀로 낙하시키지 않는다. 낙하시키면 경로 필터의 절감이
            # 조용히 사라지고, 계약 파싱이 실패한 사실이 초록불에 묻힌다.
            return {"id": sid, "state": "skipped", "reason": "no_selector"}

    return adapters.run_stage(root, adapter, sid, select=select,
                              log_path=log_path, calibration=calibration,
                              runner=runner)


def _selectors(root, adapter, parsed, repo_files):
    if not parsed:
        return None
    return parsed.get("selectors") or None


def _parse_contract(root, config, state, replay):
    """계약을 읽고 선택자를 미리 조립한다. 없으면 None."""
    text = None
    if replay:
        p = Path(replay) / "contract.md"
        if p.exists():
            text = p.read_text(encoding="utf-8")
    else:
        rel = ((state or {}).get("contract") or {}).get("path")
        if rel and (Path(root) / rel).exists():
            text = (Path(root) / rel).read_text(encoding="utf-8")
    if text is None:
        return None

    _config, adapter, _cal = adapters.load(root)
    parsed = contract_mod.parse(text, config)
    repo_files = None
    if replay:
        manifest = json.loads(
            (Path(replay) / "manifest.json").read_text(encoding="utf-8"))
        repo_files = manifest.get("repo_files")
    sel = contract_mod.test_selectors(root, config, adapter, parsed, repo_files)
    parsed["selectors"] = sel["paths"]
    parsed["unmatched"] = sel["unmatched"]
    parsed["entrypoint_resolver"] = sel["entrypoint_resolver"]
    parsed["scope"] = {"selected": sel.get("selected"),
                       "test_files": sel.get("test_files"),
                       "repo_files": sel.get("repo_files"),
                       "ratio": sel.get("selected_ratio"),
                       "degenerate": bool(sel.get("degenerate"))}
    return parsed


def _tests_signal(root, adapter, calibration, results, report_root):
    """**"테스트가 몇 개 돌았는가"를 별도 신호로 본다.**

    빈 테스트 스위트는 통과하고, 통과는 초록불로 보인다. 그래서 게이트가
    초록불인 것과 테스트가 돈 것을 갈라 놓는다 (team-spec P1).
    """
    ran_full = any(r["id"] == "full" and r["state"] == "ran" for r in results)
    if not ran_full:
        return None                     # 안 돌았다. **0 을 만들지 않는다**

    got = adapters.parse_report(root, adapter, report_root)
    floor = adapters.derived(calibration, "tests_ran_floor")
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
                "note": "미캘리브레이션 런 — 하한을 모른다"}
    if ran < floor:
        return {"ran": ran, "expected_min": floor, "status": "shrank",
                "source": "calibration"}
    return {"ran": ran, "expected_min": floor, "status": "ok",
            "source": "calibration"}


# ------------------------------------------------------------------ 귀속

def attribute(root, config, adapter, report, state, replay=None, log_text="",
              stuck_after=2):
    """실패를 역할에 귀속한다. 반환은 dispatch dict 또는 None(실패 없음).

    `stuck_after` 는 04 프론트매터의 `loop.stuck_after_identical` 이다. 호출부가
    그 값을 넘기지 않으면 명세의 기본값 2 를 쓴다 — 예전에는 넘길 자리조차 없어
    선언이 코드에 닿지 않았다 (M33).
    """
    failed = report.get("failed")
    if failed is None:
        return None

    infra = attr.classify_infra(adapter, failed.get("exit", 1),
                                log_text or failed.get("output") or "")
    if infra:
        return {"owner": "infra", "infra": infra, "stuck": False,
                "by_owner": {}, "failures": [], "deferred": [], "parallel": False}

    symbols = set(report.get("symbols") or [])
    repo_files = None
    if replay:
        manifest = json.loads(
            (Path(replay) / "manifest.json").read_text(encoding="utf-8"))
        repo_files = manifest.get("repo_files")
    else:
        # 03 이 방금 만든 파일도 프레임으로 센다 — 추적분만 보면 이 런이
        # 새로 쓴 코드가 귀속에서 통째로 빠진다 (M50).
        repo_files = harness.list_files_with_untracked(root)

    text = log_text or failed.get("output") or ""
    failures = attr.attribute_compile(adapter, config, symbols, text)
    if not failures:
        report_root = Path(replay) if replay else None
        got = adapters.parse_report(root, adapter, report_root)
        failures = attr.attribute_tests(adapter, config, symbols,
                                        got.get("failed_units") or [],
                                        repo_files=repo_files)
    if not failures:
        failures = [{"id": "S-1", "kind": "stage", "unit": failed["id"],
                     "file": None, "ftype": "stage", "message": text[:400],
                     "frames": [], "owner": "ambiguous",
                     "owner_reason": "스테이지가 실패했지만 실패 항목을 못 읽었다",
                     "sig": attr.signature("ambiguous", failed["id"], "stage", text)}]

    flip = (state or {}).setdefault("flip", {})
    # **쌍이다.** `sig_chain` 은 `owner|sig` 를 쌓는다 — 시그니처만 세면 flip 이
    # 값을 낼 바로 그 라운드에 정체 감지가 먼저 멈춘다 (M33).
    prev = (state or {}).get("sig_chain") or []
    return attr.dispatch(failures, config, prev, flip, stuck_after=stuck_after)
