#!/usr/bin/env python3
"""런 디렉터리 · 상태 · 이벤트 · 워크트리 지문 · 봉투.

이 파일은 `scripts/` 를 sys.path 에 넣으므로 `state` 라는 이름이 프로세스 전역
최상위 모듈이 된다. 형제 넷(cli·state·adapters·attribution) 다 stdlib 과
충돌하지 않는 것을 확인했다.

**모든 함수가 root 를 첫 인자로 받는다.** 모듈 전역 ROOT 에 의존하면 테스트가
실물 `_workspace/` 를 건드리게 된다.

**못 잰 값은 키를 만들지 않는다** — 0 이나 null 로 채우면 "재지 않았다"와
"0 이었다"가 같은 칸에 들어간다 (ADR-H007 이 attempts 에서 지난 자리).
"""

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))            # 형제 모듈
sys.path.insert(0, str(_HERE.parent))     # scripts/harness.py · runtime.py

import harness  # noqa: E402  — 소유 판정·스키마 검증·doctor 의 단일 출처
import runtime  # noqa: E402  — TZ. import 만으로 UTF-8 출력이 강제된다

# 코어는 순차 실행기를 물지 않는다 — 템플릿이 그것을 안 싣기 때문이다
# (ADR-H037). RunningFile 재수출은 소비자가 없어 여기서 지웠다.
TZ = runtime.TZ
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

WORKSPACE_REL = "_workspace"
RUNS_REL = harness.RUNS_REL      # 완주 런 세기(`harness.completed_runs`)와 같은 곳

# 페이즈 상태 어휘. `pending` 과 `submitted` 는 **없다** —
#   pending   : 키 부재가 그것이다. 미진입 칸을 미리 파 두면 "안 돌았다"와
#               "돌았는데 결과가 없다"가 같은 칸에 들어간다.
#   submitted : record 가 검증·판정·전이를 한 프로세스에서 하므로 중간 상태가
#               관측될 필요가 없다. 프로세스가 중간에 죽으면 running 으로 남고
#               그것이 정확한 서술이다.
PHASE_STATUS = ("running", "passed", "failed", "escalated", "skipped")

# 런 상태 어휘. 셋 다 예전에는 리터럴로 세 곳에 흩어져 있었고, `done` 은
# **거르는 쪽(`latest_run_id`)만 있고 쓰는 쪽이 없었다** — 그래서 어떤 런도
# 닫히지 않았다 (M24).
# `abandoned` 는 "이어질 일이 없다" 다. `done`(완주)과 갈라 두는 이유는
# 보고서와 원장이 둘을 같은 것으로 읽으면 안 되기 때문이고, `active` 로
# 남겨 두지 않는 이유는 **이어지지 않을 런이 이어질 것처럼 보이는 것 자체가
# 거짓**이기 때문이다.
RUN_STATUS = ("active", "escalated", "done", "abandoned")

# `latest_run_id` 가 기본값으로 집지 않는 상태. `escalated` 는 빠져 있다 —
# 재개 가능한 런이고, 안 집으면 사람의 판단을 기다리는 런이 화면에서 사라진다.
TERMINAL_STATUS = ("done", "abandoned")

# `on_success` 의 종단 센티널. team-spec §1 의 페이즈 표가 08 의 성공 시
# 다음을 `done` 이라 적는다 — 페이즈 id 가 아니라 "여기서 끝" 이라는 표식이다.
DONE = "done"

COUNTERS = ("round", "repair", "xverify_return", "review_repair", "pr_repair")

# 예산을 **무엇에 썼는가**. 카운터는 "몇 번 썼나"만 세므로, 사유가 없으면
# "수리 2회로 안 됐다"와 "형식으로 2회 튕겼다"가 원장에서 같은 줄로 보인다
# (M47). P6 이 정확히 그랬다 — M46 의 교착으로 예산을 태워 **수리를 한 번도
# 시도하기 전에** 05 에스컬레이션에 닿았는데 원장은 그것을 말하지 못했다.
#
# **어휘를 닫는 이유**: 호출처마다 문자열을 지어내면 집계가 불가능해지고,
# 그러면 사유를 남기는 목적 자체가 사라진다. 늘리려면 여기와 team-spec 을
# 함께 고친다.
COUNTER_REASONS = (
    "converged",              # 01 이 수렴해 라운드를 닫았다
    "not_converged",          # 01 이 한 라운드를 더 쓴다
    "xverify_critical",       # 02 의 Critical 이 01 로 되돌렸다
    "gate_failure",           # 04 게이트가 실패해 수리로 간다
    "review_blocking",        # 05 의 Critical/Major 를 수리한다
    "format_reject",          # 제출이 규약을 어겨 되돌아왔다 — 수리가 아니다
    "external_change_requested",  # 07 의 외부 변경 요청
    "manual",                 # `retry` — 사람이 사유를 따로 적는다
)

# 닫힌 어휘다. budget.model_calls 가 봉투의 지시에서 유도되므로, 어휘가
# 열려 있으면 그 값의 정의가 조용히 흔들린다.
EVENT_KINDS = (
    "run_created", "phase_enter", "phase_pass", "phase_fail", "phase_skip",
    "submit_received", "check_fail",
    # `check_fail` 은 "제출이 규약을 어겼다", `reviewer_failed` 는 "그 리뷰어가
    # 아예 안 돌았다" 다. 뭉치면 원장에서 **형식 문제와 미수행이 같아 보이고**,
    # 05 의 라우팅 결함 진단이 불가능해진다.
    "reviewer_failed",
    "stage_start", "stage_done", "stage_skipped",
    "attribution", "dispatch", "counter_inc",
    "escalated", "resumed", "horizon",
    # `horizon` 은 "다음 페이즈가 아직 없다", `run_closed` 는 "런이 끝났다" 다.
    # 하나로 뭉치면 미완성 실행기와 완주한 런을 원장에서 구분할 수 없다.
    "run_closed",
    # 06~08. 승인·PR·승격은 "일어났다"가 사후에 확인 가능해야 하는 사건이고,
    # 그 셋 다 외부 상태를 건드린다 — 이벤트가 없으면 되돌아볼 기록이 없다.
    "approved", "approval_revoked", "pr_pushed", "pr_opened", "promoted",
    # `counter_inc` 은 "예산을 썼다", `counter_grant` 는 "예산을 더 줬다" 다.
    # 뭉치면 원장에서 다섯 라운드를 쓴 런과 세 라운드를 쓰고 둘을 더 받은 런이
    # 같아 보인다 (M32).
    "counter_grant",
    # 계약이 바뀌어 프로파일이 다시 정해졌다. 리뷰어 상한이 그 값에서 나오므로
    # 언제 무엇에서 무엇으로 바뀌었는지가 사후에 필요하다 (M34).
    "profile_reconfirmed",
    # 사람을 기다리기 시작했다(`waiting_human`, exit 9 의 자리들) — 다음 이벤트
    # 까지가 대기다. exit 10 은 `escalated → resumed` 로 따로 잰다. 이것이
    # 없으면 `40dc` 의 00-triage 36분이 "트리아지가 36분 걸렸다" 로 읽힌다
    # (ADR-H052). `format_reject` 는 제출이 규약을 어겨 exit 8 로 되돌아온
    # 것을 **횟수로** 세는 자리다 — `check_fail` 이 그 사실을 남기지만
    # 절반의 exit 8 경로는 그것조차 없었고, 세지 않으면 `e7ff` 의 sec 처럼
    # 두 번 튕겨 failed 로 닫힌 리뷰어가 원장에 "실패" 로만 남는다.
    "waiting_human", "format_reject",
    # 00 이 레인을 정했다(`triage_decided`), 그 예측이 03·05 의 실물에서 상향으로
    # 빗나갔다(`triage_miss`). 둘을 뭉치면 임계값을 고칠 근거(어느 예측이
    # 얼마나 틀리나)가 원장에서 사라진다.
    "triage_decided", "triage_miss",
    # 05 라우팅이 매칭한 리뷰어의 위험을 01 INTENT 의 `risk` 가 안 적었다.
    # 게이트가 아니라 관측이다 — 오탐률을 본 뒤 승격을 정한다 (ADR-H067).
    "risk_undeclared",
)

GRADES = ("PASS", "PASS_WITH_GAPS", "INCOMPLETE")


# ------------------------------------------------------------------ 타임스탬프

def stamp(now=None):
    """실행기와 같은 형식·같은 TZ.

    RunningFile.age_sec 이 이 형식을 strptime 하므로 다른 형식을 쓰면
    생존 판정이 조용히 깨진다.
    """
    return (now or datetime.now(TZ)).strftime(STAMP_FORMAT)


# -------------------------------------------------------------------- 런 경로

class RunPaths:
    """런 디렉터리의 자리들. 경로 조립을 한 곳에 모은다."""

    __slots__ = ("root", "run_id", "run_dir")

    def __init__(self, root, run_id):
        self.root = Path(root)
        self.run_id = run_id
        self.run_dir = self.root / RUNS_REL / run_id

    @property
    def state(self):
        return self.run_dir / "state.json"

    @property
    def events(self):
        return self.run_dir / "events.jsonl"

    @property
    def gates(self):
        return self.run_dir / "gates"

    @property
    def escalation(self):
        return self.run_dir / "ESCALATION.md"

    @property
    def running(self):
        return self.run_dir / "RUNNING"

    @property
    def request(self):
        return self.run_dir / "00_original_request.md"

    def rel(self, path):
        """런 디렉터리 기준 상대 경로 — 보고서에 절대 경로를 싣지 않는다."""
        try:
            return Path(path).resolve().relative_to(self.run_dir.resolve()).as_posix()
        except ValueError:
            return str(path)


def new_run_id(now=None, seed_bytes=b""):
    """`YYYYMMDD-HHMM-xxxx` — 18자.

    경로 240자 상한(team-spec E4)이 있고 런 디렉터리 이름이 모든 산출물
    경로의 접두부가 되므로 짧게 유지한다.
    """
    head = (now or datetime.now(TZ)).strftime("%Y%m%d-%H%M")
    tail = hashlib.sha1(seed_bytes + str(os.getpid()).encode("utf-8")).hexdigest()[:4]
    return "%s-%s" % (head, tail)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def create_run(root, slug, request_path, profile=None, seed_bytes=None, now=None):
    """런을 만들고 요청을 **바이트 그대로** 동결한다.

    원격도 커밋 이력도 브랜치도 건드리지 않는다 (team-spec P6).
    """
    root = Path(root)
    raw = Path(request_path).read_bytes()
    run_id = new_run_id(now, seed_bytes if seed_bytes is not None else raw)
    paths = RunPaths(root, run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.gates.mkdir(exist_ok=True)
    # read_bytes -> write_bytes. 개행 변환도 인코딩 변환도 없다 — 의도 동결이
    # 결정론의 앵커이므로 여기서 한 바이트라도 달라지면 앵커가 앵커가 아니다.
    paths.request.write_bytes(raw)

    config = harness._read_json(root / harness.CONFIG_REL)
    adapter, calibration = _adapter_and_calibration(root, config)

    s = {
        "schema": 1,
        "run_id": run_id,
        "slug": slug,
        "created_at": stamp(now),
        "updated_at": stamp(now),
        "run_status": "active",
        "request": {
            "path": "00_original_request.md",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        },
        "profile": _initial_profile(profile),
        "adapter": {"id": config.get("adapter"),
                    "verified": bool((adapter or {}).get("verified"))},
        "calibration": _calibration_summary(calibration),
        "vcs": {"baseline": _vcs_baseline(root)},
        "phase": "00-triage",
        "phases": {},
        "counters": {},
        "escalated": False,
        "contract": {"mode": "contract", "present": False},
        "cross_verify": _cross_verify_init(config),
        "grade": None,
        "gaps": [],
        "budget": {"model_calls": {
            "total": 0,
            "max": (config.get("budget") or {}).get("model_calls_max"),
            "basis": BUDGET_BASIS,
            "blind_spots": list(BUDGET_BLIND_SPOTS),
            "by_phase": {}, "counted": []}},
        "models": _models_node(),
    }
    save(paths, s)
    append_event(paths, "run_created", cmd="init", phase="00-triage",
                 slug=slug, request_bytes=len(raw))
    return paths, s


def _initial_profile(profile):
    """`init` 시점의 프로파일. 사람이 줬으면 `user`, 아니면 00 이 정한다.

    00 이 요청 원문의 구조 신호로 **예측**하고(`source: triage`), 03 의
    계약(유닛·진입점 수)과 05 의 변경 파일이 그것을 재판정한다. 00 이 돌기
    전의 값은 `default` 이고 라운드 상한이 큰 쪽(normal)이다 — 보수적으로
    더 검토하는 쪽이다.
    """
    if profile:
        return {"name": profile, "source": "user"}
    return {"name": "normal", "source": "default",
            "reason": "00 이 아직 판정하지 않았다"}


# 모델 등급의 관측 단위. **실행기는 어느 모델이 돌았는지 볼 수 없다** —
# 봉투가 지시 키마다 등급을 찍고 `/feature` 가 그것을 Agent 호출의 `model`
# 인자로 넘길 뿐이다. `budget.model_calls` 와 같은 부류의 자진신고 없는 지시다.
# `instructed` 는 봉투가 지시한 등급, `reported` 는 리뷰어가 제출 JSON 의
# `model_used` 로 자진신고한 모델이다 (ADR-H052 결정 2). 둘은 다른 사실이고
# 어느 쪽도 실측이 아니다 — 자진신고는 대조할 값이 없다.
MODELS_BASIS = "instructed+reported"
MODELS_BLIND_SPOTS = (
    "지시한 등급이 실제로 쓰였는지 실행기는 보지 못한다",
    "reported 는 리뷰어의 자진신고다 — 대조할 실측이 없다 (선택 필드라 빈 것이 보통이다)",
    "inherit 로 지시된 키는 메인 세션의 모델이고 그 값은 상태에 없다",
    "effort 는 에이전트 프론트매터의 정적 선언이고 실행기는 무엇이 돌았는지 보지 못한다 (ADR-H061)",
)


def _models_node():
    return {"basis": MODELS_BASIS, "instructed": {}, "reported": {},
            "blind_spots": list(MODELS_BLIND_SPOTS)}


def note_model_reported(s, key, model):
    """리뷰어가 `model_used` 로 자진신고한 모델을 지시 키 `key` 에 남긴다."""
    node = s.setdefault("models", _models_node())
    node.setdefault("reported", {})[key] = model
    return node


def note_model_instruction(s, key, tier):
    """봉투가 지시 키 `key` 에 등급 `tier` 를 찍었다. `None` 이면 미선언이다."""
    node = s.setdefault("models", _models_node())
    node.setdefault("instructed", {})[key] = tier
    return node


def _adapter_and_calibration(root, config):
    adapter = calibration = None
    try:
        adapter = harness._read_json(
            root / harness.ADAPTER_DIR_REL / ("%s.json" % config["adapter"]))
    except (OSError, ValueError, KeyError):
        pass
    cal_rel = config.get("calibration_file")
    if cal_rel:
        try:
            calibration = harness._read_json(root / cal_rel)
        except (OSError, ValueError):
            pass
    return adapter, calibration


def _calibration_summary(calibration):
    if calibration is None:
        return {"present": False}
    return {"present": True,
            "partial": bool(calibration.get("partial")),
            "adapter_verified": bool(calibration.get("adapter_verified"))}


def _cross_verify_init(config):
    """런 시작 시의 교차검증 요약.

    **`configured` 와 `mode` 는 다른 것을 말한다.** `configured` 는 config 가
    무엇을 선언했는가이고 `mode` 는 **실제로 무엇이 관측했는가**다. 예전에는
    하나뿐이라 config 가 `primary` 를 선언하면 라운드가 전부 폴백으로 돌아도
    상태는 `primary` 라고 적었다 — P3 가 다섯 라운드 내내 그랬고, 그 사실이
    상태에도 보고서에도 남지 않았다.

    `mode` 는 라운드가 제출될 때마다 `note_cross_verify_round` 가 내린다.
    올리지는 않는다 — 한 번 약해진 관측은 뒤 라운드가 좋아도 그 런의 사실이다.
    """
    return {"mode": _cross_verify_mode(config),
            "configured": _cross_verify_mode(config),
            "rounds": {}, "degraded_rounds": 0, "last_primary_error": None}


def _cross_verify_mode(config):
    """primary 도 fallback 도 없으면 skipped — 02 가 등급에 드러낸다."""
    cv = config.get("cross_verify") or {}
    if cv.get("primary"):
        return "primary"
    if cv.get("fallback"):
        return "fallback"
    return "skipped"


def note_cross_verify_round(s, round_, mode, primary_error=None):
    """한 회차의 교차검증이 무엇으로 돌았는지 런 요약에 접는다.

    **부재와 일시 실패를 가른다** — `primary_error` 가 있으면 primary 를
    시도했다가 실패한 것이고(일시), 없으면 primary 가 애초에 없던 것이다(구조).
    앱 코드에 `lookup_failed` ≠ `no_match` 를 요구하면서(ADR-005) 하네스가
    그 둘을 한 어휘로 뭉개고 있었다.

    `mode` 는 **내려가기만 한다.** 3회차가 primary 로 회복돼도 1·2회차가
    폴백이었다는 것은 그 런의 사실이고, 등급이 그것을 말해야 한다.
    """
    node = s.setdefault("cross_verify", {})
    node.setdefault("rounds", {})[str(round_)] = mode
    if primary_error:
        node["last_primary_error"] = primary_error
    node["degraded_rounds"] = sum(
        1 for v in node["rounds"].values() if v == "fallback")
    if mode == "fallback":
        node["mode"] = "fallback"
    return node


def _vcs_baseline(root):
    head = harness._git(root, "rev-parse", "HEAD")
    branch = harness._git(root, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = harness._git(root, "status", "--porcelain")
    return {
        "head": (head.stdout.strip() if head and head.returncode == 0 else None),
        "branch": (branch.stdout.strip() if branch and branch.returncode == 0 else None),
        "dirty": bool(porcelain.stdout.strip()) if porcelain and porcelain.returncode == 0 else None,
    }


# ------------------------------------------------------------------ 읽기·쓰기

def latest_run_id(root, include_done=False):
    """가장 최근의 미완료 런. 없으면 None."""
    runs = Path(root) / RUNS_REL
    if not runs.is_dir():
        return None
    best = None
    for d in sorted((p for p in runs.iterdir() if p.is_dir()), reverse=True):
        try:
            s = json.loads((d / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if include_done or s.get("run_status") not in TERMINAL_STATUS:
            return s.get("run_id") or d.name
        best = best or (s.get("run_id") or d.name)
    return best


def load(root, run_id=None):
    """(RunPaths, state). 런이 없으면 (None, None)."""
    rid = run_id or latest_run_id(root)
    if rid is None:
        return None, None
    paths = RunPaths(root, rid)
    if not paths.state.exists():
        return None, None
    return paths, json.loads(paths.state.read_text(encoding="utf-8"))


def save(paths, s, now=None):
    s["updated_at"] = stamp(now)
    _write_json(paths.state, s)


# -------------------------------------------------------------------- 이벤트

def append_event(paths, kind, cmd=None, phase=None, now=None, **data):
    """한 줄 한 JSON. 어휘 밖 kind 는 즉시 예외다."""
    if kind not in EVENT_KINDS:
        raise ValueError(
            "알 수 없는 이벤트 kind: %r — 어휘는 닫혀 있다 (%s)"
            % (kind, ", ".join(EVENT_KINDS)))
    paths.events.parent.mkdir(parents=True, exist_ok=True)
    seq = 0
    if paths.events.exists():
        seq = sum(1 for line in
                  paths.events.read_text(encoding="utf-8").splitlines() if line.strip())
    rec = {"ts": stamp(now), "run_id": paths.run_id, "seq": seq + 1,
           "cmd": cmd, "phase": phase, "kind": kind, "data": data}
    with paths.events.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_events(paths):
    """`events.jsonl` 을 seq 순 리스트로. **깨진 줄 하나로 나머지를 버리지 않는다.**

    `_read_session_metrics`(execute.py)와 같은 규율이다 — 쓰이는 중인 파일은
    마지막 줄이 잘려 있을 수 있고, 그 한 줄 때문에 실측 전부를 잃으면
    "못 읽었다"가 "아무 일도 없었다"로 보고된다.
    """
    if not paths.events.exists():
        return []
    out = []
    try:
        raw = paths.events.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    out.sort(key=lambda e: e.get("seq") or 0)
    return out


def _parse_stamp(value):
    """`stamp` 의 역함수. 형식 상수를 새로 만들지 않는다."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, STAMP_FORMAT)
    except ValueError:
        return None


def session_touched_run(born, updated, start, end):
    """이 세션이 그 런을 **만졌는가**. 판정할 수 없으면 `None` (M59).

    귀속을 **한 시점**으로 보면 안 된다. 세션 창은 `[직전 원장 줄의 ts, 이
    줄의 ts]` 이고 런 구간은 `[created_at, updated_at]` 이며, 둘은 서로를
    가로지른다 — P8 은 17:20 에 시작해 다음날 01:08 에 닫혔고 세션 둘이
    걸쳐 있어서 `updated_at` 만 보면 앞 세션이 통째로 빠진다.

    `start` 가 `None` 인 것은 **판정 불가가 아니다** — 원장 첫 줄이라 앞
    경계가 없을 뿐이고 창이 열려 있다. 판정 불가는 런의 구간을 모르는
    경우(`born`·`updated` 부재)이고, 그때 `latest_only` 로 단정하면 못 잰
    것이 "무관하다" 는 주장으로 바뀐다 ([[ADR-H007]]).

    **쓰는 쪽(`session_log`)과 읽는 쪽(`cost-state`)이 같은 함수를 부른다.**
    같은 식을 두 곳이 각자 쓰면 갈라지고, 그때 원장이 자기와 모순된다.
    """
    if born is None or updated is None or end is None:
        return None
    return born <= end and (start is None or updated > start)


def phase_durations(paths):
    """페이즈별 벽시계를 `events.jsonl` 에서 유도한다 (`PHASE_DURATION_BASIS`).

    **새 계측 장치가 아니다.** 이벤트는 이미 `ts` 를 적고 있었고 읽는 쪽만
    없었다. 그래서 여섯 런의 보고서가 전부 "소요 시간은 미측정" 을 적었다.

    구간을 여는 것은 `phase_enter` 가 아니라 **이벤트의 `phase` 가 바뀌는
    것**이다. `phase` 가 없는 이벤트(`counter_inc`)는 직전 페이즈를 잇는다 —
    새 구간을 열면 그 시간이 어느 페이즈에도 안 들어가 벽시계가 샌다.

    에스컬레이션 대기는 `escalated` → **다음 `resumed`** 다. "다음 이벤트"로
    잡으면 재개 뒤 첫 작업 시간까지 대기로 셈해진다. 재개되지 않은
    에스컬레이션은 **길이가 없으므로 키를 만들지 않고** 횟수만 센다.

    이 값은 **벽시계이고 사람이 답을 쓰는 대기가 섞여 있다.** 그래서
    `escalation_wait_sec` 을 같은 표에 따로 뺀다 — P8 은 7시간 48분 중
    4시간 42분(60.2%)이 그것이었고 다섯 건 전부 01-plan 이었다.
    총계 한 줄로는 그 사실이 안 보인다.

    구간이 하나도 없으면 **빈 dict** 다. 0 으로 채우면 "안 쟀다"와
    "0 이었다"가 같은 칸에 들어간다 (ADR-H007).
    """
    events = read_events(paths)
    stamped = [(e, _parse_stamp(e.get("ts"))) for e in events]
    stamped = [(e, t) for e, t in stamped if t is not None]
    if len(stamped) < 2:
        return {}

    phases = {}
    current = None
    pending_escalation = None       # (페이즈, 시각)
    pending_human = None            # (페이즈, 시각) — exit 9 뒤 다음 명령까지

    def node(name):
        return phases.setdefault(name, {"wall_sec": 0, "segments": 0,
                                        "entries": 0, "passes": 0,
                                        "escalations": 0, "format_rejects": 0})

    for idx, (event, when) in enumerate(stamped):
        name = event.get("phase") or current
        if name is None:
            # 런 최초 이벤트가 페이즈를 안 말하면 귀속할 곳이 없다.
            continue
        if name != current:
            node(name)["segments"] += 1
            current = name
        cell = node(name)

        kind = event.get("kind")
        # **사람 대기는 다음 이벤트까지다** (ADR-H052). exit 9 뒤에 오는 첫
        # 명령이 무엇이든 그것이 답이다 — `status` 처럼 이벤트를 안 남기는
        # 명령은 안 보이고, 그때는 그 다음 이벤트까지가 대기로 잡힌다(과다).
        if pending_human is not None:
            owner, since = pending_human
            waited = int((when - since).total_seconds())
            if waited >= 0:
                node(owner)["human_wait_sec"] = \
                    node(owner).get("human_wait_sec", 0) + waited
            pending_human = None
        if kind == "waiting_human":
            pending_human = (name, when)
        elif kind == "format_reject":
            cell["format_rejects"] += 1
        if kind == "phase_enter":
            cell["entries"] += 1
        elif kind == "phase_pass":
            cell["passes"] += 1
        elif kind == "escalated":
            cell["escalations"] += 1
            pending_escalation = (name, when)
        elif kind == "resumed" and pending_escalation is not None:
            owner, since = pending_escalation
            waited = int((when - since).total_seconds())
            if waited >= 0:
                node(owner)["escalation_wait_sec"] = \
                    node(owner).get("escalation_wait_sec", 0) + waited
            pending_escalation = None

        if idx + 1 < len(stamped):
            cell["wall_sec"] += int(
                (stamped[idx + 1][1] - when).total_seconds())

    unresumed = 1 if pending_escalation is not None else 0
    waits = [p["escalation_wait_sec"] for p in phases.values()
             if "escalation_wait_sec" in p]
    human = [p["human_wait_sec"] for p in phases.values()
             if "human_wait_sec" in p]
    # 순 작업 = 벽시계 − 에스컬레이션 대기 − 사람 판단 대기. 세 값의 합이
    # 벽시계와 같은 것이 검산이다.
    for p in phases.values():
        p["work_sec"] = (p["wall_sec"] - p.get("escalation_wait_sec", 0)
                         - p.get("human_wait_sec", 0))

    out = {
        "basis": PHASE_DURATION_BASIS,
        "blind_spots": list(PHASE_DURATION_BLIND_SPOTS),
        "first_ts": stamped[0][0].get("ts"),
        "last_ts": stamped[-1][0].get("ts"),
        "wall_sec": int((stamped[-1][1] - stamped[0][1]).total_seconds()),
        "phases": phases,
        "unresumed_escalations": unresumed,
        "unanswered_waits": 1 if pending_human is not None else 0,
    }
    if waits:
        out["escalation_wait_sec"] = sum(waits)
    if human:
        out["human_wait_sec"] = sum(human)
    return out


# --------------------------------------------------------------- 페이즈·카운터

def phase_status(s, phase_id):
    """키가 없으면 None — 미진입이다."""
    return ((s.get("phases") or {}).get(phase_id) or {}).get("status")


def set_phase_status(s, phase_id, status, now=None, **fields):
    if status not in PHASE_STATUS:
        raise ValueError(
            "알 수 없는 페이즈 status: %r — 어휘는 %s 여섯이 아니라 다섯이다"
            % (status, ", ".join(PHASE_STATUS)))
    node = s.setdefault("phases", {}).setdefault(phase_id, {})
    node["status"] = status
    node["at"] = stamp(now)
    node.update(fields)
    return node


def _granted(node):
    """이 카운터가 지금까지 **추가 지급**받은 총량. 없으면 0.

    지급의 원장은 `grants` 하나이고 `max` 는 그것을 반영한 **파생값**이다.
    파생값을 원장처럼 다루면 그것을 덮어쓰는 코드가 지급을 지운다 (M56).
    """
    return sum(g.get("extra") or 0 for g in node.get("grants") or [])


def counter_inc(s, name, max_, reason, paths=None, now=None, note=None):
    """(used, **실효 상한**, exceeded). 어휘 밖 카운터·사유는 예외.

    **인자 `max_` 는 선언값이고 실효 상한은 `max_ + grants 합` 이다.** 예전에는
    `node["max"] = max_` 로 선언값을 그대로 대입해 `counter_grant` 가 올린
    상한을 **다음 소모 한 번이 지웠다** (M56). P8 에서 라운드 7·8·9 가 실효 10
    인 예산을 5 로 보고 잘못 에스컬레이션했고, 사람이 답변 셋을 손으로 써서
    [[ADR-H024]] 가 만든 지급 경로의 대역을 했다.

    **셋이 모두 실효값을 말한다** — 반환 2항 · `node["max"]` · `counter_inc`
    이벤트의 `max`. 한 곳이라도 선언값을 말하면 "어느 예산으로 돌았는가" 가 그
    자리에서 갈린다. P8 의 `events.jsonl` 이 지급 뒤에도 `max: 5` 를 적어
    **그 런이 왜 세 번 멈췄는지 원장만으로는 설명되지 않았다.**

    **`reason` 은 필수다.** 기본값을 두면 그 기본값이 곧 새 하드코딩이고,
    "무엇에 썼는지 모른다" 가 조용히 통과한다 ([[ADR-H025]] 의 교훈).

    `paths` 를 주면 `counter_inc` 이벤트도 남긴다. 상태는 마지막 모습이고
    이벤트는 순서다 — 어느 라운드가 무엇으로 탔는지는 순서에만 있다.
    """
    if name not in COUNTERS:
        raise ValueError("알 수 없는 카운터: %r (%s)" % (name, ", ".join(COUNTERS)))
    if reason not in COUNTER_REASONS:
        raise ValueError("예산 소모 사유가 어휘 밖이다: %r (%s) — 무엇에 썼는지 "
                         "없이 예산을 태우면 원장이 그 런을 설명하지 못한다"
                         % (reason, ", ".join(COUNTER_REASONS)))
    node = s.setdefault("counters", {}).setdefault(name, {"used": 0, "max": max_})
    # **실효 상한은 매번 다시 계산한다** (M56). `grants` 가 원장이고 `max` 는
    # 파생값이므로, 여기가 유일한 재계산 지점이다 — `node["max"]` 를 직접 읽는
    # 셋(런 헤더·왕복 봉투·보고서의 `_counter_cell`)이 각자 합산하지 않아도 된다.
    eff = None if max_ is None else max_ + _granted(node)
    node["max"] = eff
    node["used"] = node.get("used", 0) + 1
    entry = {"n": node["used"], "reason": reason, "ts": stamp(now)}
    if note:
        # `manual` 의 사람 사유처럼 어휘로 담을 수 없는 것. 어휘를 늘리는 대신
        # 자유 텍스트를 옆 칸에 둔다 — 집계는 `reason` 이, 서술은 `note` 가 한다.
        entry["note"] = note
    node.setdefault("spent", []).append(entry)
    if paths is not None:
        append_event(paths, "counter_inc", counter=name, used=node["used"],
                     max=eff, reason=reason, note=note, now=now)
    return node["used"], eff, node["used"] >= eff if eff is not None else False


def counter_grant(s, name, extra, reason, now=None):
    """예산을 **추가 지급**한다. 반환: (used, max).

    **리셋이 아니다.** `used` 를 되돌리면 "이 런이 라운드를 몇 번 돌았는가"가
    사라지고, 그것이 M31 이 낸 손실의 모양이다. 상한만 올리고 지급 사실을
    `grants` 에 남긴다 — 보고서가 "왕복 뒤 몇 라운드를 더 줬는가"를 말할 수
    있는 것이 여기서 나온다.

    지급은 무한 연장이 아니다. 부르는 쪽이 자기 왕복 예산(`xverify_return`
    상한 1)에 묶여 있어 런당 한 번뿐이다 (M32 · ADR-H024).

    **여기서 올리는 `max` 는 `grants` 의 파생값이다** — `counter_inc` 이 매
    소모마다 `선언값 + grants 합` 으로 다시 계산하므로 두 값이 어긋나지 않는다.
    이 대입을 중복 가산으로 오해해 지우면, 지급 직후부터 다음 소모 전까지
    봉투(`record` 의 왕복 렌더)와 보고서가 **옛 상한**을 말한다.
    """
    if name not in COUNTERS:
        raise ValueError("알 수 없는 카운터: %r (%s)" % (name, ", ".join(COUNTERS)))
    if not extra or extra < 0:
        raise ValueError("지급량은 양수여야 한다: %r" % (extra,))
    # 초기값은 **0 이다** (M58). `extra` 로 만들면 바로 아래가 그것에 `extra` 를
    # 또 더해 소모 전 첫 지급이 상한을 두 배로 만든다. `max` 는 `grants` 의
    # 파생값이므로 소모가 없는 상태의 상한은 지급 합계와 같아야 한다 —
    # 선언값은 이 함수가 모르고 `counter_inc` 이 인자로 받는다.
    node = s.setdefault("counters", {}).setdefault(name, {"used": 0, "max": 0})
    node["max"] = (node.get("max") or 0) + extra
    node.setdefault("grants", []).append(
        {"at": stamp(now), "extra": extra, "reason": reason})
    return node.get("used", 0), node["max"]


def demote(s, grade, gap=None):
    """등급을 **강등만** 한다. 반환: 최종 등급.

    이 함수가 있기 전에는 세 곳이 `s["grade"] = ...` 를 직접 대입했고
    (게이트 · 02 스킵 · 05 판정), **나중에 쓰는 쪽이 이겼다.** 게이트가
    나중에 돌면 05 가 남긴 `PASS_WITH_GAPS` 가 `PASS` 로 되돌아간다 —
    한 번 드러난 결손이 조용히 사라지는 경로다.

    등급은 `GRADES` 의 인덱스가 클수록 나쁘고, 더 나쁜 쪽으로만 움직인다.
    `gap` 을 주면 `gaps` 에 중복 없이 더한다 — 등급만 떨어지고 사유가 없으면
    보고서가 "무엇을 건너뛰었는가"를 적을 수 없다 (§E12 가 나열을 요구한다).
    """
    if grade is not None and grade not in GRADES:
        raise ValueError("알 수 없는 등급: %r (%s)" % (grade, ", ".join(GRADES)))
    if gap and gap not in s.setdefault("gaps", []):
        s["gaps"].append(gap)
    cur = s.get("grade")
    if grade is None:
        return cur
    if cur is None or GRADES.index(grade) > GRADES.index(cur):
        s["grade"] = grade
    return s.get("grade")


# `model_calls` 의 관측 단위. **실행기는 모델 호출을 볼 수 없다** — 모델이
# 기동하고 실행기는 결과만 받는다(ADR-H014). 그래서 볼 수 있는 것을 센다:
# **봉투가 에이전트 기동을 지시한 횟수.**
BUDGET_BASIS = "instructed"

# 이 기준이 틀리는 두 방향. 보고서가 이것을 그대로 적는다 — 한쪽으로만
# 틀리는 값이 아니므로 "하한" 이라고 부르면 그것도 재지 않은 주장이 된다.
BUDGET_BLIND_SPOTS = (
    "모델이 스스로 낸 호출은 세지 못한다 (과소)",
    "지시를 메인이 대신 처리하면 센 것이 실제로 안 일어난다 (과다)",
)


# 페이즈별 소요의 관측 단위. **`phase_enter` → `phase_pass` 짝이 아니다.**
# 되돌아간 페이즈에는 진입 이벤트가 안 찍히고(P8 의 01 재작업 3시간 26분이
# 02 로 귀속됐다) `08-report` 는 `phase_pass` 만 있어 짝으로는 못 잰다.
# 대신 이벤트를 seq 순으로 걸으며 인접한 두 `ts` 의 차를 **그때 활성인
# 페이즈**에 더한다 — `Σ 페이즈 소요 == 런 벽시계` 가 기계로 검산된다.
PHASE_DURATION_BASIS = "event-segment"

# 이 기준이 틀리는 세 방향. `BUDGET_BLIND_SPOTS` 와 같은 규율이다 —
# 값만 주고 어느 쪽으로 틀리는지 안 주면 사람이 그 숫자로 판단할 수 없다.
PHASE_DURATION_BLIND_SPOTS = (
    "이벤트는 대개 일을 끝낸 뒤 찍힌다 — 새 페이즈의 첫 이벤트가 "
    "phase_enter 가 아니면 그 준비 시간이 앞 페이즈에 붙는다 (앞이 과다)",
    "타임스탬프가 초 단위다 — 1초 미만 페이즈는 0 으로 보인다 (과소)",
    "버려진 런은 아무도 일하지 않은 날들이 그대로 벽시계에 들어간다 (과다)",
    "사람 판단 대기(exit 9)는 다음 이벤트까지다 — 이벤트를 안 남기는 명령"
    "(status 등)이 답이면 그 다음 이벤트까지가 대기로 잡힌다 (대기 과다)",
)


def _budget_node(s):
    node = s.setdefault("budget", {}).setdefault(
        "model_calls", {"total": 0, "max": None, "by_phase": {}})
    node["basis"] = BUDGET_BASIS
    node["blind_spots"] = list(BUDGET_BLIND_SPOTS)
    node.pop("approx", None)            # "근사" 는 무엇이 근사인지 말하지 못한다
    node.setdefault("counted", [])
    return node


def bump_model_calls(s, phase, n=1):
    """(total, max, exhausted). 예산이 없으면(max=None) 소진되지 않는다."""
    node = _budget_node(s)
    node["total"] = node.get("total", 0) + n
    by = node.setdefault("by_phase", {})
    by[phase] = by.get(phase, 0) + n
    max_ = node.get("max")
    return node["total"], max_, (max_ is not None and node["total"] >= max_)


def count_instructions(s, phase, keys):
    """봉투가 낸 **에이전트 기동 지시**를 센다. 반환: (total, max, exhausted).

    키가 필요한 이유는 `next` 가 같은 페이즈에서 여러 번 불릴 수 있기
    때문이다 — 같은 지시를 두 번 세면 계수가 왕복 횟수를 센다. 이미 센 키는
    다시 세지 않는다.

    **제출을 세지 않고 지시를 세는 이유** (M26): 제출 기준은 두 방향으로
    틀렸다. 02 교차검증·07 내장 리뷰·04 의 수리 배정은 `record --reviewer` 를
    남기지 않아 안 세지고(과소), 리뷰어 출력의 형식만 메인이 고쳐 재제출하면
    새 모델 호출 없이 세진다(과다). 지시 기준은 **한 방향으로만** 틀리고 그
    방향에 이름을 붙일 수 있다.
    """
    node = _budget_node(s)
    seen = node["counted"]
    fresh = [k for k in keys if k not in seen]
    if not fresh:
        max_ = node.get("max")
        return (node.get("total", 0), max_,
                (max_ is not None and node.get("total", 0) >= max_))
    seen.extend(fresh)
    return bump_model_calls(s, phase, len(fresh))


# ------------------------------------------------------------------ 지문

def _scope_signature(config):
    globs = []
    for role in config.get("roles") or []:
        owns, excludes = harness.effective_owner_globs(role)
        globs.append({"owns": sorted(owns), "excludes": sorted(excludes)})
    return hashlib.sha1(
        json.dumps(globs, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _in_scope(config, path):
    """소유 판정은 doctor 가 쓰는 함수 하나로만 한다."""
    return any(harness.owns_file(role, path) for role in (config.get("roles") or []))


def _nul_split(result):
    return [f for f in result.stdout.split("\0") if f] if result else []


def _candidate_files(root):
    """(경로 목록, algo). 지문에 들어갈 후보를 센다 — 소유 판정은 뒤에서 한다.

    추적 파일에 **미추적·비무시** 파일을 더한다. 03 이 방금 쓴 파일은 아직
    `git add` 전이고, 그것을 빼면 게이트가 보지 않은 코드가 영수증을 통과한다.
    `--exclude-standard` 가 없으면 `.gitignore` 가 뺀 `_workspace/` 가 들어와
    커맨드마다 지문이 바뀐다.
    """
    tracked = harness._git(root, "ls-files", "-z")
    if tracked is None or tracked.returncode != 0:
        # git 이 없다. 워크 탐색은 `.gitignore` 대신 WALK_SKIP 을 따르므로
        # **다른 모집단을 센다** — algo 를 갈라 둘이 우연히 같아도 안 맞게 한다.
        return harness.list_files(root), "walk-sha256"
    others = harness._git(root, "ls-files", "--others", "--exclude-standard", "-z")
    paths = set(_nul_split(tracked))
    if others is not None and others.returncode == 0:
        paths |= set(_nul_split(others))
    return sorted(paths), "tree-sha256"


def fingerprint(root, config, now=None):
    """소유 범위 파일 **내용**의 해시. HEAD 는 보지 않는다.

    - **커밋이 지문을 바꾸지 않는다.** 예전에는 `HEAD + 미커밋 파일 해시` 라
      06 이 PR diff 를 위해 요구하는 커밋이 04 영수증을 **반드시** 낡게 만들었다
      — 바이트가 하나도 안 바뀌었는데도. 영수증이 증언하는 것은 "게이트가
      무엇을 테스트했는가" 이고 그것은 커밋 여부가 아니라 내용이다 (M25).
    - **mtime 을 쓰지 않는다.** 되돌렸다가 같은 내용으로 다시 쓴 파일은
      지문이 같아야 한다 — 아니면 advance 가 늘 거부한다.
    - **없는 파일은 넣지 않는다.** `ABSENT` 표식을 쓰면 "삭제 미커밋"(인덱스에
      남아 표식이 붙는다)과 "삭제 커밋"(목록에서 사라진다)이 다른 값이 되어
      커밋 중립성에 삭제 모양의 구멍이 남는다. 생략하면 둘이 같고, 삭제 이전
      지문과는 여전히 다르다 — "삭제도 변경이다" 는 지켜진다.
    - scope_globs_sha1 은 roles[].owns 가 런 도중 바뀐 것을 잡는다 (M19 와 같은 규율).
    """
    root = Path(root)
    candidates, algo = _candidate_files(root)
    inputs = []
    for rel in candidates:
        if not _in_scope(config, rel):
            continue
        digest = _sha256_file(root / rel)
        if digest is None:                      # 삭제됐다 — 항목을 만들지 않는다
            continue
        inputs.append("C\t%s\t%s" % (rel, digest))

    value = hashlib.sha256("\n".join(sorted(inputs)).encode("utf-8")).hexdigest()
    return {"algo": algo, "value": value, "at": stamp(now),
            "scope_globs_sha1": _scope_signature(config),
            # **뜻이 바뀌었다** — 예전에는 HEAD 줄을 포함한 입력 줄 수(=변경
            # 파일 수 + 1)였고 지금은 해시한 소유 범위 파일 수다.
            "file_count": len(inputs)}


def _sha256_file(path):
    """없는 파일(삭제됨)은 None — 호출부가 항목을 생략한다."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def fingerprint_matches(saved, fresh):
    """**algo 가 같을 때만 비교가 성립한다.**

    다른 방법으로 잰 두 값이 우연히 같아 "안 바뀌었다"가 되는 것을 구조적으로
    막는다. algo 가 다르면 보수적으로 stale 이다.
    """
    if not saved or not fresh:
        return False
    if saved.get("algo") != fresh.get("algo"):
        return False
    if saved.get("scope_globs_sha1") != fresh.get("scope_globs_sha1"):
        return False
    return saved.get("value") == fresh.get("value")


# ------------------------------------------------------------------ 런 종료

def close_run(s, now=None, status=DONE, reason=None):
    """런을 닫는다. **`run_status` 를 종단으로 옮기는 유일한 자리다.**

    `demote` 가 등급의 단일 출처인 것과 같은 규율이다 — 대입이 여러 곳으로
    흩어지면 되돌리는 경로가 조용히 생긴다. `escalate` 의 대칭 짝이고, 둘 다
    런의 수명을 끝내는 쪽으로만 움직인다.

    `status` 를 파라미터로 둔 것은 종단이 둘이기 때문이다 — 08 이 닫는
    `done` 과 사람이 버리는 `abandoned`. **함수를 새로 만들면 대입이 둘로
    갈라져 위의 규율이 실제로 깨진다** (ADR-H016 이 지키려던 것은 "대입
    자리가 하나" 이지 "호출자가 하나" 가 아니다).
    """
    if status not in TERMINAL_STATUS:
        raise ValueError("종단 상태가 아니다: %r (%s)"
                         % (status, ", ".join(TERMINAL_STATUS)))
    s["run_status"] = status
    s["closed_at"] = stamp(now)
    if reason:
        s["closed_reason"] = reason
    return s


# ------------------------------------------------------------------ 에스컬레이션

def escalate(paths, s, reason, options=None, phase=None, now=None):
    """상태를 잠그고 ESCALATION.md 를 남긴다.

    대화에 묻히지 않게 파일로도 남기는 것이 요점이다 — 이후 모든 커맨드가
    에스컬레이션 패킷만 내고 exit 10 이다.
    """
    s["escalated"] = True
    s["run_status"] = "escalated"
    s["escalation"] = {"reason": reason, "phase": phase, "at": stamp(now),
                       "options": list(options or [])}
    if phase:
        set_phase_status(s, phase, "escalated", now=now)
    lines = ["# 에스컬레이션 — %s" % (phase or s.get("phase") or "?"),
             "", "run_id: `%s`" % paths.run_id, "", "## 왜 멈췄는가", "", reason, ""]
    if options:
        lines += ["## 선택지", ""]
        lines += ["%d. %s" % (i + 1, o) for i, o in enumerate(options)]
        lines += [""]
    lines += ["## 재개", "",
              "사람이 답을 정한 뒤:",
              "",
              "```",
              "python scripts/pipeline/cli.py resume --ack --answer-file <경로>",
              "```", ""]
    paths.escalation.write_text("\n".join(lines), encoding="utf-8")
    append_event(paths, "escalated", cmd="escalate", phase=phase, reason=reason)
    save(paths, s, now=now)


# ---------------------------------------------------------------------- 봉투

def envelope(cmd, ok, exit_, state, data, render, next_command):
    """stdout 에 나가는 단일 JSON.

    모델이 읽는 것은 `render` 와 `next_command` 둘뿐이다. 나머지는 사람과
    테스트를 위한 것이고, 모델이 다른 필드로 판단하기 시작하면 이 계약이 깨진다.
    """
    s = state or {}
    return {
        "schema": 1,
        "ok": bool(ok),
        "cmd": cmd,
        "exit": exit_,
        "run_id": s.get("run_id"),
        "phase": s.get("phase"),
        "state_summary": {
            "counters": s.get("counters") or {},
            "escalated": bool(s.get("escalated")),
            "grade": s.get("grade"),
            "gaps": s.get("gaps") or [],
            # 06~08. 승인과 PR 은 모델이 다음 행동을 고르는 데 필요한 사실이고,
            # 봉투에 없으면 모델이 state.json 을 직접 열어 읽게 된다 — 그것이
            # "봉투에서 읽는 것은 render 와 next_command 둘뿐" 이라는 계약을 깬다.
            "approval": s.get("approval") or {},
            "pr": s.get("pr") or {},
        },
        "data": data or {},
        "render": render or "",
        "next_command": next_command,
    }


def emit(env):
    """봉투 하나를 stdout 에 쓰고 종료 코드를 돌려준다."""
    sys.stdout.write(json.dumps(env, ensure_ascii=False, indent=None) + "\n")
    sys.stdout.flush()
    return env["exit"]
