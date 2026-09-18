#!/usr/bin/env python3
"""세션 종료 원장 — `SessionEnd` 훅이 부르는 사실 수집기.

**이 스크립트는 해석을 쓰지 않는다.** 훅은 셸 명령이라 "이 결함이 무엇을
조용히 통과시킬 뻔했는가"를 쓸 수 없고, 쓰게 하려면 훅 안에서 모델을 부르는
수밖에 없는데 그러면 검증하는 사람 없이 문서에 추측이 쌓인다. 그래서 둘로
나눈다 — 여기는 **거짓말할 수 없는 부분**만 원장에 append 하고,
`docs/PIPELINE-LOG.md` §5 로의 승격은 `/log` 가 사람의 호출로 한다.

규율 넷:

1. **못 잰 값은 키를 만들지 않는다.** 0 이나 null 로 채우면 "재지 않았다"와
   "0 이었다"가 같은 칸에 들어간다 (ADR-H007).
2. 파일 I/O 는 UTF-8 명시 · 개행 변환 끔 · 비ASCII 이스케이프 안 함.
   깨지면 원장이 조용히 오염된다 (team-spec 2.1).
3. **실패해도 exit 0.** `SessionEnd` 는 종료를 막을 수 없고 JSON 출력이
   버려지므로 실패가 조용해진다. 그래서 실패를 원장 자신에 남긴다 —
   실패(못 모았다)와 "기록할 게 없음"을 같은 모양으로 뭉개지 않는다.
4. **훅 예산 안에 끝난다.** `SessionEnd` 기본 예산은 1.5초이고 설정의
   `timeout` 이 그것을 올린다. 그래도 여기서 테스트를 돌리지 않는다 —
   테스트 수는 세지 않고 이미 기록된 것을 읽는다.
"""

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "pipeline"))

import harness                # noqa: E402  — _git 단일 출처
import runtime                # noqa: E402  — 트랜스크립트 집계 · UTF-8 강제
import state as st            # noqa: E402  — stamp · _vcs_baseline · RUNS_REL

LEDGER_REL = "docs/pipeline-ledger.jsonl"

# 원장 한 줄이 커밋 목록으로 부풀지 않게 자른다. 잘렸다는 사실은 남긴다.
COMMIT_CAP = 20

# 트랜스크립트가 이보다 크면 읽지 않는다 — 훅 예산 안에 안 들어온다.
# **읽지 않은 것과 값이 없는 것을 구분해서** 적는다.
TRANSCRIPT_MAX_BYTES = 12 * 1024 * 1024

# 리포 루트의 표식. harness 설정을 먼저 본다 — .git 만 보면 워크트리에서 헷갈린다.
ROOT_MARKERS = ("harness/config.json", ".git")


# ------------------------------------------------------------------ 루트 찾기

def find_root(hook_input, env=None):
    """훅은 **현재 디렉터리**에서 돈다 — 세션이 하위 디렉터리에 있으면 거기서다.

    그래서 `cwd` 를 그대로 리포 루트로 쓰면 안 된다. 순서는 셋이다:
    `CLAUDE_PROJECT_DIR`(세션이 시작된 프로젝트 루트) → 훅 입력의 `cwd` 에서
    위로 올라가며 표식 찾기 → 프로세스의 cwd 에서 같은 탐색.
    """
    env = os.environ if env is None else env
    declared = env.get("CLAUDE_PROJECT_DIR")
    if declared:
        p = Path(declared)
        if p.is_dir():
            return p.resolve()

    starts = []
    cwd = (hook_input or {}).get("cwd")
    if cwd:
        starts.append(Path(cwd))
    starts.append(Path.cwd())

    for start in starts:
        try:
            here = start.resolve()
        except OSError:
            continue
        for cand in (here,) + tuple(here.parents):
            if any((cand / m).exists() for m in ROOT_MARKERS):
                return cand
    return Path.cwd().resolve()


# ------------------------------------------------------------------ git 수집

def _numstat(root, *args):
    """`git diff --numstat` 집계. 리포에 이 호출이 없어 새로 짠 유일한 조각.

    바이너리 파일은 `-` 로 오므로 **파일 수에만 세고 줄 수에는 안 센다** —
    0 으로 세면 "안 바뀌었다"로 읽힌다.
    """
    r = harness._git(root, "diff", "--numstat", *args)
    if r is None or r.returncode != 0:
        return None
    files = insertions = deletions = binary = 0
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        try:
            insertions += int(parts[0])
            deletions += int(parts[1])
        except ValueError:
            binary += 1
    out = {"files": files, "insertions": insertions, "deletions": deletions}
    if binary:
        out["binary_files"] = binary
    return out


def _untracked(root):
    """`git diff` 는 **새 파일을 못 본다.** 안 세면 변경량이 사실보다 작게 적힌다.

    줄 수는 세지 않는다 — 새 파일의 "추가된 줄"은 `git diff` 의 것과 계산
    근거가 달라서, 한 칸에 합치면 두 숫자가 같은 뜻인 척하게 된다.
    """
    r = harness._git(root, "ls-files", "--others", "--exclude-standard")
    if r is None or r.returncode != 0:
        return None
    return len([line for line in r.stdout.splitlines() if line.strip()])


def _base_branch(root):
    try:
        cfg = json.loads((root / "harness" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (cfg.get("vcs") or {}).get("base_branch")


def _last_entry_head(root, branch):
    """같은 브랜치의 **직전 원장 줄**이 남긴 HEAD.

    이것이 있어야 "이 세션의 커밋"을 정직하게 셀 수 있다. 세션 **시작**을
    아무도 기록하지 않으므로, 세션의 경계는 **직전 기록 이후**로 정의한다.
    """
    path = root / LEDGER_REL
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("branch") == branch and rec.get("head"):
            return rec["head"]
    return None


def _log(root, *args):
    r = harness._git(root, "log", "--format=%h\t%s", "--no-merges", *args)
    if r is None or r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        sha, _, subject = line.partition("\t")
        if sha:
            rows.append({"sha": sha, "subject": subject})
    return rows


def _commits(root, branch):
    """직전 기록 이후의 커밋. 없으면 base 브랜치 이후, 그것도 안 되면 최근 것.

    **어느 기준으로 셌는지를 함께 적는다** — 기준을 안 적으면 두 세션의 숫자가
    같은 뜻인 줄 알고 비교하게 된다.
    """
    rows = since = None
    head = _last_entry_head(root, branch)
    if head:
        rows = _log(root, "%s..HEAD" % head)
        if rows is not None:
            since = {"kind": "prev_entry", "head": head}
    if rows is None:
        base = _base_branch(root)
        # **base 브랜치 위에 있으면 그 기준은 무의미하다** — `main..HEAD` 가
        # 정당하게 비므로, 커밋이 있는데도 없는 것처럼 적히게 된다.
        if base and base != branch:
            rows = _log(root, "%s..HEAD" % base)
            if rows is not None:
                since = {"kind": "base_branch", "ref": base}
    if rows is None:
        # base 브랜치가 없는 리포도 있다. 그때는 **최근 것**이라고 적는다 —
        # 이 세션의 것이라고 적으면 거짓이 된다.
        rows = _log(root, "--max-count=%d" % COMMIT_CAP, "HEAD")
        if rows is None:
            return None, None
        since = {"kind": "head_recent", "max_count": COMMIT_CAP}
    if len(rows) > COMMIT_CAP:
        since["truncated_from"] = len(rows)
        rows = rows[:COMMIT_CAP]
    return rows, since


# --------------------------------------------------------------- 런 · 테스트

def _latest_run(root):
    runs = root / st.RUNS_REL
    if not runs.is_dir():
        return None
    dirs = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.name)
    if not dirs:
        return None
    try:
        return json.loads((dirs[-1] / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _prev_ts(root):
    """직전 원장 줄의 `ts`. 세션 창의 **앞 경계**다 (M59).

    없으면 `None` 이고 그것은 판정 불가가 아니다 — 원장 첫 줄이라 창이 열려
    있다는 뜻이고, `state.session_touched_run` 이 그렇게 다룬다.
    """
    path = Path(root) / LEDGER_REL
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            return st._parse_stamp(json.loads(line).get("ts"))
        except ValueError:
            return None
    return None


def _touched(root, run_state, now_ts):
    """이 세션이 그 런을 만졌는가. 판정 불가는 `None` (M59).

    판정 자체는 `cost-state` 가 읽는 시점에 부르는 것과 **같은 함수**다.
    """
    return st.session_touched_run(
        st._parse_stamp((run_state or {}).get("created_at")),
        st._parse_stamp((run_state or {}).get("updated_at")),
        _prev_ts(root), st._parse_stamp(now_ts))


def _run_summary(run_state, touched=None):
    """런이 남긴 것 중 **원장에 의미가 있는 칸만** 옮긴다. 없는 칸은 안 만든다.

    **안 만진 런은 `run_id` 와 `basis` 만 남긴다** (M59). 등급·결손·라운드는
    *그 런의* 사실이지 이 세션의 것이 아니다. 그렇다고 `run` 칸을 통째로 빼면
    "파이프라인 런이 아예 없던 세션" 과 원장에서 구분되지 않는다 — 두 사실을
    같은 침묵으로 뭉개지 않는다.
    """
    if touched is False:
        rid = run_state.get("run_id")
        return {"run_id": rid, "basis": "latest_only"} if rid else None
    out = {}
    for key in ("run_id", "grade", "run_status"):
        if run_state.get(key) is not None:
            out[key] = run_state[key]
    if run_state.get("gaps"):
        out["gaps"] = run_state["gaps"]
    rounds = ((run_state.get("counters") or {}).get("round") or {}).get("used")
    if rounds is not None:
        out["rounds"] = rounds
    calls = ((run_state.get("budget") or {}).get("model_calls") or {}).get("total")
    if calls is not None:
        out["model_calls"] = calls
    if touched is True and out:
        out["basis"] = "touched"
    # `touched is None` 이면 키를 안 만든다 — 판정할 수 없는데 `latest_only`
    # 로 단정하면 못 잰 것이 "무관하다" 는 주장으로 바뀐다 (ADR-H007).
    return out or None


def _tests(root, run_state):
    """**세지 않고 읽는다.** 훅에서 테스트를 돌리면 예산을 즉시 넘긴다."""
    ran = ((run_state or {}).get("tests") or {}).get("ran")
    if ran is not None:
        return {"app": ran, "source": "run_state"}
    try:
        cal = json.loads((root / "harness" / "calibration.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ran = ((cal.get("stages") or {}).get("full") or {}).get("tests_ran")
    if ran is None:
        return None
    return {"app": ran, "source": "calibration"}


# --------------------------------------------------------------- 트랜스크립트

def _session_metrics(hook_input, transcript_root):
    """세션이 접두부 **밖에서** 끌어온 양. 실행기의 집계를 그대로 쓴다.

    `transcript_path` 는 비동기로 기록되므로 훅 시점에 최신 메시지가 없을 수
    있다. 그래서 이 값은 **하한**이고, 없으면 키를 만들지 않는다.
    """
    sid = (hook_input or {}).get("session_id")
    if not sid:
        return None
    root = transcript_root
    if root is None:
        tp = (hook_input or {}).get("transcript_path")
        root = Path(tp).parent.parent if tp else runtime.TRANSCRIPT_ROOT
    root = Path(root)
    try:
        found = sorted(root.glob("*/%s.jsonl" % sid))
    except OSError:
        return None
    if not found:
        return None
    try:
        size = found[0].stat().st_size
    except OSError:
        return None
    if size > TRANSCRIPT_MAX_BYTES:
        # 읽지 않은 것과 값이 없는 것은 다르다.
        return {"metrics_skipped": "transcript_too_large", "bytes": size}
    return runtime.read_session_metrics(sid, transcript_root=root) or None


# ------------------------------------------------------------------------ 수집

def collect(root, hook_input, *, transcript_root=None, now=None):
    """원장 한 줄. 모든 조각이 독립적으로 없을 수 있고, 없으면 칸이 없다."""
    root = Path(root)
    rec = {"ts": st.stamp(now)}
    for key in ("session_id", "reason"):
        if (hook_input or {}).get(key):
            rec[key] = hook_input[key]

    base = st._vcs_baseline(root)
    for key in ("head", "branch", "dirty"):
        if base.get(key) is not None:
            rec[key] = base[key]

    if rec.get("branch"):
        commits, since = _commits(root, rec["branch"])
        if commits is not None:
            rec["commits"] = commits
            rec["commits_since"] = since

    uncommitted = _numstat(root, "HEAD") or {}
    untracked = _untracked(root)
    if untracked:
        uncommitted["untracked"] = untracked
    if uncommitted.get("files") or uncommitted.get("untracked"):
        rec["uncommitted"] = uncommitted

    run_state = _latest_run(root)
    touched = None
    if run_state:
        touched = _touched(root, run_state, rec["ts"])
        summary = _run_summary(run_state, touched)
        if summary:
            rec["run"] = summary
    # 안 만진 런의 테스트 수도 이 세션의 사실이 아니다 — `calibration.json`
    # 으로 떨어지고 `source` 가 그 사실을 같은 칸에서 말한다 (M59).
    tests = _tests(root, None if touched is False else run_state)
    if tests:
        rec["tests"] = tests

    metrics = _session_metrics(hook_input, transcript_root)
    if metrics:
        rec["session"] = metrics

    rec["promoted"] = False
    return rec


def append(root, record):
    """append-only. 기존 줄은 절대 고치지 않는다 — 그래야 머지 충돌이 union
    으로 자명하게 풀린다 (team-spec 5.2)."""
    path = Path(root) / LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def pending(root):
    """`/log` 가 아직 옮기지 않은 세션 수. 규칙은 `/log` 0절과 같다 —
    `promoted: false` 인 줄 중 어느 `promote` 줄의 `sessions` 에도 없는 것.

    `error` 줄은 옮길 사실이 없어 세지 않는다. 원장이 없거나 못 읽으면 0 —
    이 함수는 알림용이라 "못 셌다" 를 따로 말하지 않는다.
    """
    path = Path(root) / LEDGER_REL
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    rows, promoted = [], set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if "promote" in rec:
            promoted.update((rec["promote"] or {}).get("sessions") or [])
        elif "error" not in rec and not rec.get("promoted", False):
            rows.append(rec)
    return sum(1 for r in rows if r.get("session_id") not in promoted)


# --------------------------------------------------------------------- 진입점

def main(argv=None, stdin=None, root=None):
    runtime.force_utf8_output()
    parser = argparse.ArgumentParser(description="세션 종료 사실을 원장에 남긴다")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--from-hook", action="store_true",
                      help="stdin 으로 훅 JSON 을 받는다")
    mode.add_argument("--pending", action="store_true",
                      help="미승격 세션 수를 한 줄 알린다 (0 이면 침묵). 원장에 쓰지 않는다")
    args = parser.parse_args(argv)

    if args.pending:
        # SessionStart 훅. 0 이면 아무것도 찍지 않는다 — 정보 없는 줄이
        # 세션마다 컨텍스트에 쌓이는 것을 막는다 (ADR-H007 과 같은 결).
        n = pending(Path(root) if root else find_root({}))
        if n:
            sys.stdout.write("미승격 세션 %d개 — /log\n" % n)
        return 0

    stream = sys.stdin if stdin is None else stdin
    hook_input = {}
    error = None
    record = None
    if args.from_hook:
        try:
            raw = stream.read()
            hook_input = json.loads(raw) if raw.strip() else {}
            if not isinstance(hook_input, dict):
                raise ValueError("최상위가 객체가 아니다")
        except Exception as exc:                        # noqa: BLE001
            error = "훅 입력을 읽지 못했다: %s" % exc
            hook_input = {}

    resolved = Path(root) if root else find_root(hook_input)

    if error is None:
        try:
            record = collect(resolved, hook_input)
        except Exception as exc:                        # noqa: BLE001
            error = "수집 실패: %s: %s" % (type(exc).__name__, exc)

    if error is not None:
        record = {"ts": st.stamp(), "error": error}
        sid = hook_input.get("session_id")
        if sid:
            record["session_id"] = sid

    try:
        append(resolved, record)
    except OSError as exc:
        # 원장 자체를 못 쓰면 그때만 사용자에게 보인다.
        sys.stderr.write("세션 원장을 쓰지 못했다: %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
