#!/usr/bin/env python3
"""어댑터 계층 — 스택 지식이 코어로 새지 않게 하는 유일한 문.

여기가 갖는 것: 어댑터·캘리브레이션 읽기 · 스테이지 실행 · 리포트 파싱 위임 ·
타임아웃 유도 · 변경 파일 매칭 · 인프라 패턴 · 선택자 조립의 어댑터 쪽 절반.

여기가 갖지 않는 것: 판정(등급·귀속·카운터·소유자) · 상태 쓰기 · stdout.

**어댑터는 선언만 한다.** 어댑터에 조건문이 필요해지면 그건 코어에 빠진 개념이라는
신호이므로, 필드를 늘리지 말고 코어에 스테이지·신호를 추가한다.
"""

import json
import math
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402

# 이름 8종은 코어가 고정한다. 명령만 스택이 정한다.
STAGE_ORDER = harness.STAGE_ORDER

DEFAULT_TIMEOUT_SEC = 600


def load(root):
    """(config, adapter, calibration). 캘리브레이션은 없을 수 있다."""
    root = Path(root)
    config = harness._read_json(root / harness.CONFIG_REL)
    adapter = harness._read_json(
        root / harness.ADAPTER_DIR_REL / ("%s.json" % config["adapter"]))
    calibration = None
    cal_rel = config.get("calibration_file")
    if cal_rel and (root / cal_rel).exists():
        try:
            calibration = harness._read_json(root / cal_rel)
        except (OSError, ValueError):
            calibration = None
    return config, adapter, calibration


def raise_tests_floor(root, config, ran, run_id):
    """`derived.tests_ran_floor` 를 **단조 증가**로 올린다. 반환 {from, to} 또는 None.

    캘리브레이션은 1회 측정이라 그 뒤 테스트가 늘어도 하한이 안 움직인다 —
    파일럿 15런이 전부 `expected_min: 14` 로 돌았고 마지막 런은 652개였다
    (ADR-H047). 07 의 `promote --flush` 가 런당 한 번 도는 자리라 거기서
    부른다. 내리지는 않는다 — 급감을 잡는 것이 이 값의 목적이다.
    `TESTS_FLOOR_RATIO` 는 `calibrate` 와 같은 상수를 쓴다.
    """
    if not ran or ran <= 0:
        return None
    cal_rel = (config or {}).get("calibration_file")
    if not cal_rel:
        return None
    path = Path(root) / cal_rel
    if not path.exists():
        return None
    try:
        cal = harness._read_json(path)
    except (OSError, ValueError):
        return None
    derived = cal.setdefault("derived", {})
    old = derived.get("tests_ran_floor")
    new = int(math.floor(ran * harness.TESTS_FLOOR_RATIO))
    if old is not None and new <= old:
        return None
    derived["tests_ran_floor"] = new
    derived["tests_ran_source"] = {"run_id": run_id, "ran": ran}
    path.write_text(json.dumps(cal, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return {"from": old, "to": new}


def stage_spec(adapter, name):
    return (adapter.get("stages") or {}).get(name)


def stage_state(adapter, name):
    """`absent` 는 **없는 것**이지 통과한 것이 아니다.

    게이트가 이 둘을 같은 칸에 넣으면 스크립트를 지운 스택이 조용히 초록불이 된다.
    `na` 는 어댑터가 사유(`not_applicable`)를 적어 **구조적으로 없다**고 선언한
    것이다 — 미룬 부재(`absent`)와 같은 gap 코드면 둘이 구분되지 않는다
    (ADR-H047 추기).
    """
    spec = stage_spec(adapter, name)
    if spec is None or spec.get("cmd") is None:
        if spec and str(spec.get("not_applicable") or "").strip():
            return "na"
        return "absent"
    return "present"


def derived(calibration, key):
    """캘리브레이션의 유도 정책. **없으면 None 이고 0 이 아니다.**

    0 으로 채우면 "재지 않았다"와 "0 이었다"가 같은 칸에 들어간다.
    """
    if not calibration:
        return None
    return (calibration.get("derived") or {}).get(key)


def stage_timeout(adapter, calibration, name):
    """(초, 출처). 실측이 어댑터 선언을 이긴다 — 상수가 아니라 함수다."""
    if name == "full":
        got = derived(calibration, "full_timeout_sec")
        if got:
            return int(got), "calibration"
    spec = stage_spec(adapter, name) or {}
    if spec.get("timeout_sec"):
        return int(spec["timeout_sec"]), "adapter"
    return DEFAULT_TIMEOUT_SEC, "default"


def when_touched_hit(adapter, name, changed_paths):
    """None 이면 조건이 없다(= 무조건 돈다). 아니면 변경 파일과의 매칭 결과."""
    spec = stage_spec(adapter, name) or {}
    globs = spec.get("when_touched")
    if not globs:
        return None
    return any(harness.glob_any(globs, p) for p in (changed_paths or []))


def stage_argv(root, adapter, name, select=None):
    """실행할 argv. 선택자가 여럿이면 어댑터의 select 문법으로 조립한다.

    선택자가 하나면 계약 계층의 조립기에 그대로 위임한다 — 실행 바이너리 해석
    (플랫폼별 확장자 포함)이 두 곳에서 갈라지지 않게 한다.
    """
    if select is None or isinstance(select, str):
        return harness._stage_argv(root, adapter, name, select)
    items = list(select)
    if not items:
        return harness._stage_argv(root, adapter, name, None)
    if len(items) == 1:
        return harness._stage_argv(root, adapter, name, items[0])

    argv = harness._stage_argv(root, adapter, name, None)
    spec = stage_spec(adapter, name) or {}
    sel = spec.get("select")
    if not sel:
        # select 를 두지 않은 어댑터는 선택자를 인자로 그대로 받는다 — 경로 필터다.
        return argv + items
    flag, join = sel.get("flag"), sel.get("join")
    if join == "repeat":
        for item in items:
            argv += [flag, item]
    elif join == "comma":
        argv += [flag, ",".join(items)]
    else:                                    # space
        argv += [flag] + items
    return argv


def baseline_argv(root, adapter, name="lint"):
    """베이스라인을 다시 만드는 argv. 없으면 `None` 이다.

    `stage_argv` 와 **같은 방식으로** 조립한다 — 바이너리 해석(플랫폼별 확장자
    포함)과 `common_args` 가 두 곳에서 갈라지면 한쪽만 고쳐지는 날이 온다.

    `baseline_file` 이 함께 있어야 한다. 명령만 있고 파일이 없으면 무엇이
    바뀌었는지 잴 대상이 없어 **재지 않은 것을 잰 것처럼 적게 된다.**
    """
    spec = stage_spec(adapter, name) or {}
    cmd = spec.get("baseline_cmd")
    if not cmd or not spec.get("baseline_file"):
        return None
    runner_cfg = adapter.get("runner") or {}
    return ([harness._resolve_bin(root, runner_cfg["bin"])]
            + list(runner_cfg.get("common_args") or [])
            + list(cmd))


def baseline_file(adapter, name="lint"):
    return (stage_spec(adapter, name) or {}).get("baseline_file")


def parse_report(root, adapter, report_root=None):
    """테스트 리포트를 정규화한다. 형식 분기는 계약 계층에 있다 (코어 고유명사 0건)."""
    return harness.parse_test_report(Path(root), adapter, report_root)


def infra_match(adapter, exit_code, text):
    """외부 의존 실패인가. **종료 코드가 0 이 아닐 때만 본다.**

    무조건 매칭하면 로그에 스쳐 간 경고 한 줄이 전량을 인프라로 만든다.
    """
    if exit_code == 0:
        return None
    import re
    for pat in adapter.get("infra_failure_patterns") or []:
        try:
            if re.search(pat, text or ""):
                return pat
        except re.error:
            continue
    return None


def run_stage(root, adapter, name, select=None, log_path=None,
              calibration=None, runner=None):
    """스테이지 하나를 돌린다.

    반환에서 **못 잰 칸은 만들지 않는다** — 스킵된 스테이지에 `sec: 0` 을 넣으면
    "안 돌았다"와 "0초에 끝났다"가 같은 칸에 들어간다.
    """
    state = stage_state(adapter, name)
    if state != "present":
        return {"id": name, "state": "skipped", "reason": state}

    argv = stage_argv(root, adapter, name, select)
    timeout, t_src = stage_timeout(adapter, calibration, name)
    cwd = Path(root) / ((stage_spec(adapter, name) or {}).get("cwd")
                        or (adapter.get("runner") or {}).get("cwd") or ".")

    run = runner or _default_runner
    started = time.time()
    code, out = run(name, argv, cwd, timeout)
    elapsed = round(time.time() - started, 2)

    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(log_path).open("a", encoding="utf-8") as fh:
            fh.write("### stage %s (exit %s)\n%s\n" % (name, code, out))

    result = {"id": name, "state": "ran", "exit": code, "sec": elapsed,
              "timeout_sec": timeout, "timeout_source": t_src, "output": out}
    if select:
        result["selector"] = list(select) if not isinstance(select, str) else [select]
        result["selector_kind"] = "path"
    return result


def _default_runner(name, argv, cwd, timeout_sec):
    """(exit_code, 출력 텍스트). 출력은 게이트 로그로 흘린다."""
    import subprocess
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT after %ss: %s" % (timeout_sec, " ".join(argv))
    except OSError as exc:
        return 127, "실행할 수 없다: %s (%s)" % (" ".join(argv), exc)
    return r.returncode, (r.stdout or "") + (r.stderr or "")
