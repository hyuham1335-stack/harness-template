#!/usr/bin/env python3
"""`precheck` — 05 의 첫 검사이고 무료다.

예산 · 브랜치 · base divergence · 인프라를 정적으로 본다. 모델을 부르지 않고
러너도 돌리지 않는다. **뒤에서 되돌릴 일을 여기서 먼저 잡는다.**

종료 코드가 둘로 갈리는 것이 요점이다:

- **exit 9** — 사용자 판단 대기. 상태를 잠그지 않고 **카운터도 소모하지
  않는다** (§2.5 — 정책은 소모하지 않는다). 예산 초과·브랜치 불일치·base
  behind 가 여기다. **자동 분할도 자동 리베이스도 하지 않는다** — 범위와
  히스토리는 사람의 것이다.
- **exit 10** — 인프라 실패. 상태를 잠그고, **카운터를 소모하지 않는다** (§E9).
  프로브가 실패한 채로 전체 회귀를 돌리면 한꺼번에 빨간불이 되고, 그것을
  코드 문제로 읽게 된다.

§E13 이 **재개 시 재검사를 필수**로 두므로 이 함수는 05 진입에서 한 번,
06 에서 다시, 재개마다 또 불린다. 그래서 싸야 한다.
"""

import os
import socket
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402
import adapters  # noqa: E402
import attribution  # noqa: E402


# `--scope` 의 어휘. **셋째 값을 만들지 않는다** — 소비자 없는 어휘를 두는
# 것이 M36 의 모양이다.
SCOPES = ("pr", "worktree")


# 캘리브레이션 측정 뒤 이만큼 완주 런이 쌓이면 재측정을 권한다 (ADR-H047 결정 2).
# 미검증 초기값이다 — 2차 파일럿은 1회 측정 뒤 15런을 돌았고 그동안 테스트가
# 113 → 652 개로 늘었다. 재본 뒤 옮긴다 (ADR-H007).
CALIBRATION_STALE_RUNS = 5


def run(root, scope="pr", changed=None, config=None, adapter=None,
        calibration=None):
    """반환: {"exit", "checks":[...], "budget":{...}, "classification", ...}

    `classification` 은 실패 3분류의 어휘다 — `policy` / `infra` / None.

    `scope` 는 **무엇을 잴 것인가**다 (M40 · ADR-H028):

    - `pr` — base 에서 워크트리까지. 커밋된 것 + 미커밋 + 새 파일. 이 검사가
      묻는 질문이 "이 PR 이 예산 안인가" 이므로 이것이 기본이다
    - `worktree` — 미커밋만. "03 이 방금 쓴 것이 예산 안인가"

    예전에는 이 인자를 받고 **한 번도 쓰지 않았다.** 늘 미커밋만 재서, 06 이
    커밋 뒤에 부르면 `at_06` 이 항상 0파일/0줄이었다.
    """
    root = Path(root)
    if scope not in SCOPES:
        raise ValueError("알 수 없는 scope: %r (%s)" % (scope, ", ".join(SCOPES)))
    if config is None or adapter is None:
        config, adapter, calibration = adapters.load(root)

    checks = []
    if changed is None:
        changed = changed_files(root, scope, config)

    budget = _check_budget(root, config, changed, checks, scope, adapter)
    _check_branch(root, config, checks)
    _check_divergence(root, config, checks)
    infra, gaps = _check_infra(adapter, changed, checks)
    _check_calibration_stale(root, calibration, checks, gaps)

    policy_failed = [c for c in checks if not c["ok"] and c["kind"] == "policy"]
    if infra:
        return _result(10, checks, budget, "infra", changed,
                       counter_consumed=False, infra=infra, gaps=gaps)
    if policy_failed:
        # **정책 실패도 카운터를 소모하지 않는다** (§2.5 의 실패 3분류).
        # 전에는 True 를 반환했으나 그것을 읽는 코드가 없어 실제로는 아무것도
        # 태우지 않았다 — 선언과 실제가 갈라져 있었고, 명세에 정책이 예산을
        # 태워야 한다는 근거는 없다. 선언을 실제에 맞췄다.
        return _result(9, checks, budget, "policy", changed,
                       counter_consumed=False, gaps=gaps)
    return _result(0, checks, budget, None, changed, counter_consumed=False,
                   gaps=gaps)


def _result(exit_, checks, budget, classification, changed, counter_consumed,
            infra=None, gaps=None):
    return {"exit": exit_, "checks": checks, "budget": budget,
            "classification": classification,
            "counter_consumed": counter_consumed,
            "changed_count": len(changed),
            "infra_failures": infra or [],
            # 면제된 프로브. **조용히 통과시키지 않는다** — 부르는 쪽이 등급을
            # 내리고 보고서·PR 본문이 이름으로 적는다 (§E9 · M44).
            "gaps": gaps or [],
            "note": ("인프라 실패는 카운터를 소모하지 않는다 — 코드가 아니라 "
                     "환경의 문제이므로 재시도 예산을 태울 이유가 없다."
                     if classification == "infra" else
                     "예산·브랜치·divergence 는 사람이 판단한다. 자동으로 "
                     "쪼개거나 리베이스하지 않는다.")}


def _add(checks, name, ok, kind, message="", **extra):
    """`extra` 는 사람이 읽는 문구 말고 **기계가 쓰는 값**이다 (behind 수 등).
    문구에서 숫자를 다시 파싱하는 것은 문구를 고치는 순간 조용히 깨진다."""
    checks.append(dict({"name": name, "ok": ok, "kind": kind,
                        "message": message}, **extra))


# ------------------------------------------------------------------- 변경 집합

def changed_files(root, scope="worktree", config=None):
    """변경 파일 집합. `scope` 가 무엇까지 세는지를 정한다.

    미커밋 변경 + 새 파일은 **어느 scope 에서도 센다.** 빼면 예산이 사실보다
    작게 잡힌다 — `git diff` 는 추적되지 않는 파일을 못 보고, 03 이 방금 만든
    파일이 정확히 그 상태다.

    `scope="pr"` 이면 **base 이후의 커밋분까지 합친다.** 두 점 diff(`git diff
    {base}`)를 쓰므로 base 가 앞서 있으면 base 쪽 변경까지 끌어오지만,
    `_check_divergence` 가 `behind > 0` 을 exit 9 로 이미 막는다 — 예산 검사가
    뜻을 갖는 상태에서는 두 점과 세 점이 같다. 그 의존이 깨지면 divergence 가
    먼저 잡는다.

    기본값이 `worktree` 인 것은 이 함수를 직접 부르는 자리(05 라우팅)가 오늘의
    동작을 그대로 원하기 때문이다. 범위를 넓히면 05 가 브랜치의 앞선 커밋까지
    리뷰 라우팅에 넣게 되고, 그것은 근거가 따로 필요한 별개 결정이다.
    """
    out = set()
    if scope == "pr":
        base = ((config or {}).get("vcs") or {}).get("base_branch") or "main"
        r = harness._git(root, "diff", "--name-only", base)
        if r is not None and r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.strip():
                    out.add(line.strip().strip('"').replace("\\", "/"))
    # `-uall` 이 없으면 git 이 **새 디렉터리를 한 줄로 뭉친다**(`?? src/x/`).
    # 그러면 파일 열 개짜리 새 폴더가 예산에 1 로 잡히고, 03 이 만든 새
    # 모듈이 정확히 그 형태다 — 예산이 사실보다 작게 잡히는 경로다.
    r = harness._git(root, "status", "--porcelain", "-uall")
    if r is not None and r.returncode == 0:
        for line in r.stdout.splitlines():
            if len(line) > 3:
                out.add(line[3:].strip().strip('"').replace("\\", "/"))
    return sorted(out)


def _changed_lines(root, changed, scope="worktree", config=None):
    """추적분은 `git diff --numstat`, 새 파일은 줄 수를 직접 센다.

    `scope="pr"` 이면 기준이 `HEAD` 가 아니라 base 다. 한 파일이 커밋과
    미커밋 양쪽에 있어도 base→워크트리 **순변화** 한 번으로 세어진다.
    """
    total = 0
    ref = "HEAD"
    if scope == "pr":
        ref = ((config or {}).get("vcs") or {}).get("base_branch") or "main"
    r = harness._git(root, "diff", "--numstat", ref)
    counted = set()
    if r is not None and r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                add, dele, path = parts
                counted.add(path.strip().replace("\\", "/"))
                for n in (add, dele):
                    if n.isdigit():
                        total += int(n)
    for rel in changed:
        if rel in counted:
            continue
        p = Path(root) / rel
        try:
            total += len(p.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
    return total


# --------------------------------------------------------------------- 검사들

def _check_budget(root, config, changed, checks, scope="worktree", adapter=None):
    budget = config.get("budget") or {}
    # **파일 수는 소스만 센다** (ADR-H066). 테스트 파일은 어댑터의 글롭이
    # 가르고, 줄 수는 그대로 전체다 — 테스트가 과대한 PR 은 `lines_max` 가 잡는다.
    tests = [p for p in changed if attribution.is_test_file(adapter or {}, p)]
    files = len(changed) - len(tests)
    lines = _changed_lines(root, changed, scope, config)
    over = []
    if budget.get("files_max") and files > budget["files_max"]:
        over.append("파일 %d > %d" % (files, budget["files_max"]))
    if budget.get("lines_max") and lines > budget["lines_max"]:
        over.append("줄 %d > %d" % (lines, budget["lines_max"]))
    _add(checks, "예산", not over, "policy",
         ("범위가 예산을 넘었다 (%s). **자동으로 쪼개지 않는다** — 나눌지 "
          "그대로 갈지는 사람이 정한다." % ", ".join(over)) if over else
         "파일 %d · 줄 %d · 테스트 %d 제외" % (files, lines, len(tests)))
    return {"files": files, "lines": lines, "test_files_excluded": len(tests),
            "files_max": budget.get("files_max"),
            "lines_max": budget.get("lines_max")}


def _check_branch(root, config, checks):
    vcs = config.get("vcs") or {}
    r = harness._git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = r.stdout.strip() if r is not None and r.returncode == 0 else None
    if branch is None:
        _add(checks, "브랜치", True, "policy", "git 이 답하지 못했다 — 검사를 건너뛴다")
        return
    if branch in (vcs.get("protected") or []):
        _add(checks, "브랜치", False, "policy",
             "보호 브랜치 %r 위에서 작업 중이다. **브랜치를 자동으로 만들지 "
             "않는다** — 어디서 갈라질지는 사람이 정한다." % branch)
        return
    pattern = vcs.get("branch_pattern")
    if pattern:
        import re
        if not re.match(pattern, branch):
            _add(checks, "브랜치", False, "policy",
                 "브랜치 %r 이 규약 %r 과 맞지 않는다" % (branch, pattern))
            return
    _add(checks, "브랜치", True, "policy", branch)


def _check_divergence(root, config, checks):
    """base 가 앞서 있으면 멈춘다. **자동 리베이스 금지.**"""
    base = (config.get("vcs") or {}).get("base_branch")
    if not base:
        _add(checks, "base", True, "policy", "base 브랜치가 설정에 없다")
        return
    r = harness._git(root, "rev-list", "--left-right", "--count",
                     "%s...HEAD" % base)
    if r is None or r.returncode != 0:
        _add(checks, "base", True, "policy",
             "base %r 을 찾지 못했다 — 그린필드일 수 있다. 06 이 다시 본다" % base)
        return
    parts = r.stdout.split()
    behind = int(parts[0]) if parts and parts[0].isdigit() else 0
    ahead = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if behind:
        _add(checks, "base", False, "policy",
             "base(%s) 가 %d 커밋 앞서 있다. **자동으로 리베이스하지 않는다** — "
             "머지할지 리베이스할지는 사람이 정한다." % (base, behind),
             behind=behind, ahead=ahead)
        return
    _add(checks, "base", True, "policy", "%s 대비 +%d" % (base, ahead),
         behind=0, ahead=ahead)


def _check_infra(adapter, changed, checks):
    """`required_when_touched` 에 걸린 프로브만 실행한다.

    안 건드린 영역의 프로브까지 요구하면 키 하나 없다고 온 파이프라인이
    멈춘다. 반대로 건드렸는데 프로브를 건너뛰면 전체 회귀가 한꺼번에
    빨간불이 되고 그것을 코드 문제로 읽게 된다 (§E9).
    """
    failures, gaps = [], []
    for probe in adapter.get("infra_preflight") or []:
        touched = probe.get("required_when_touched")
        if touched and not any(harness.glob_any(touched, c) for c in changed):
            continue
        ok, detail = _probe(probe)
        # **M44.** "건드렸는가" 만 묻고 "키 없이도 도는가" 를 안 물으면, 목업으로
        # 떨어지는 서비스에서 키 부재가 런을 죽인다 — P4 가 그렇게 죽었다.
        # 다만 면제가 조용하면 그것은 면제가 아니라 구멍이다. 명세가 이미
        # 답을 적어 뒀다(§E9): "프로브가 실패해 스킵된 검증은 통과가 아니라
        # 미검증이다 — PASS_WITH_GAPS + 명시."
        if not ok and (probe.get("on_missing") or "fail") == "warn":
            _add(checks, "인프라:%s" % probe.get("name"), True, "infra",
                 "%s — 면제: %s" % (detail, probe.get("why") or "이유 미기재"),
                 waived=True)
            gaps.append("infra_skipped:%s" % probe.get("name"))
            continue
        # detail 에 값은 절대 싣지 않는다 — precheck 결과는 원장·보고서로 가고
        # 그것들은 리포에 남는다. 존재 여부만 남긴다.
        _add(checks, "인프라:%s" % probe.get("name"), ok, "infra", detail)
        if not ok:
            failures.append({"name": probe.get("name"), "kind": probe.get("kind"),
                             "detail": detail})
    return failures, gaps


def _check_calibration_stale(root, calibration, checks, gaps):
    """`measured_at` 이후 완주 런이 `CALIBRATION_STALE_RUNS` 이상이면 gap.

    **표시다 — 등급은 내리지 않는다.** 재측정은 사람이 하는 일이고, 그것을
    안 했다고 이 런의 코드가 덜 검증된 것은 아니다. 다만 게이트의 타임아웃·
    테스트 수 하한이 옛 실측에서 유도된 것이라는 사실은 남아야 한다.
    `measured_at` 이 없으면(템플릿 자신 · ADR-H039) 검사하지 않는다.
    """
    measured_at = (calibration or {}).get("measured_at")
    if not measured_at:
        return
    since = [s for s in harness.completed_runs(root)
             if (s.get("closed_at") or "") > measured_at]
    n = len(since)
    if n < CALIBRATION_STALE_RUNS:
        _add(checks, "캘리브레이션", True, "policy",
             "측정 %s 이후 완주 런 %d — 재측정 기준 %d 미만"
             % (measured_at, n, CALIBRATION_STALE_RUNS), stale_runs=n)
        return
    _add(checks, "캘리브레이션", True, "policy",
         "측정 %s 이후 완주 런 %d ≥ %d — 게이트의 타임아웃·테스트 수 하한이 옛 "
         "실측에서 왔다. `python scripts/harness.py calibrate` 로 다시 잰다. "
         "등급은 내리지 않는다" % (measured_at, n, CALIBRATION_STALE_RUNS),
         stale_runs=n)
    gaps.append("calibration_stale")


def _probe(probe):
    kind = probe.get("kind")
    if kind == "env":
        var = probe.get("var") or ""
        present = bool(os.environ.get(var))
        return present, ("환경변수 %s 가 설정돼 있다" % var if present else
                         "환경변수 %s 가 없다 — 이 영역을 건드렸으므로 회귀가 "
                         "환경 때문에 실패한다" % var)
    if kind == "tcp":
        host, port = probe.get("host") or "127.0.0.1", probe.get("port")
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                return True, "%s:%s 에 붙었다" % (host, port)
        except (OSError, TypeError, ValueError):
            return False, "%s:%s 에 붙지 못했다" % (host, port)
    if kind == "cmd":
        return (harness._resolve_bin(Path.cwd(), probe.get("bin") or "") is not None,
                "실행 파일 %r" % probe.get("bin"))
    return True, "알 수 없는 프로브 종류 %r — 건너뛴다" % kind
