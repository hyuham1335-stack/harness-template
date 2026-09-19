#!/usr/bin/env python3
"""8페이즈 feature-pipeline 의 진입점.

    python scripts/pipeline/cli.py <cmd> [옵션]

**stdout 은 언제나 단일 JSON 봉투 하나다.** 진단·러너 출력은 stderr 로 간다.
모델이 읽는 것은 봉투의 `render` 와 `next_command` 둘뿐이고, 다른 필드로
판단하기 시작하면 이 계약이 깨진다.

이 파일이 `scripts/pipeline/` 을 패키지로 만들지 않는 이유: 정본(team-spec)이
`next_command` 를 `python scripts/pipeline/cli.py …` 로 문자 그대로 적어 두었다.
봉투가 내는 명령 전문이 스펙이므로 직접 스크립트 실행이 계약이다.

종료 코드는 team-spec 2.3 을 따른다:
    0 성공 · 1 내부 오류 · 2 사용법/미해결 플레이스홀더/doctor 미통과
    3 선행조건 미충족 · 4 기계 판정 실패(예산 남음) · 5 예산 소진
    6 advance 거부(지문 stale) · 7 반복 한계·stuck · 8 제출물 위반
    9 사용자 판단 대기(01~04 에는 없다) · 10 에스컬레이션(상태를 잠근다)
    11 런 완료
"""

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))            # 형제 모듈
sys.path.insert(0, str(_HERE.parent))     # scripts/harness.py · runtime.py

import harness  # noqa: E402  — 소유 판정·스키마 검증·doctor 의 단일 출처
import state as st  # noqa: E402
import adapters  # noqa: E402
import verdict  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
PHASES_REL = "harness/phases"
TAXONOMY_REL = "docs/harness/pipeline/ledger/taxonomy.json"

# 프론트매터 어휘. 늘리려면 여기와 team-spec 을 함께 고친다.
REQUIRES_KINDS = ("file", "state", "clean_ownership", "adapter_stage")
PRODUCES_KINDS = ("json", "markdown")
FRONT_KEYS = ("id", "index", "owner", "approval", "docs", "requires", "produces",
              "review", "converge", "submit_checks", "skip_when", "on_skip",
              "skip_policy", "gate", "loop", "allow", "on_success")
REQUIRED_SECTIONS = ("## 목적", "## 진입 조건", "## 절차",
                     "## 제출 형식", "## 금지", "## 실패 시")
ROLE_TEMPLATE_SECTION = "## 역할 프롬프트 템플릿"

_PLACEHOLDER = re.compile(r"\$\{([a-zA-Z0-9_.\[\]]+)\}")
_NAMESPACES = ("config", "calibration", "run")

# lint 는 런 없이 돈다. 경로 길이를 최악으로 재기 위한 자리표시자다 —
# run_id 18자 + slug 40자로 채운 값이 240자 상한을 넘지 않아야 한다.
LINT_RUN_ID = "20260101-0000-0000"
LINT_SLUG = "s" * 40

# 루프 선언의 어휘. **늘리려면 그 동작을 먼저 만든다** — 없는 기계를 어휘로
# 예고하는 것이 M36 이 이름한 결함 그 자체다.
LOOP_ON_EXCEED = ("escalate",)


class ConfigDeclarationError(ValueError):
    """루프 선언이 없거나 어휘 밖이다. **기본값으로 낙하하지 않는다.**

    M36: `on_exceed` · `on_fail_return_to` · `xverify_return` 의 상한이 전부
    프론트매터에만 있고 코드는 하드코딩된 값을 썼다. **지금 동작이 선언값과
    우연히 일치해서** 다섯 런 동안 아무도 눈치채지 못했고, 선언을 고치면
    조용히 무시됐다. 읽되, 읽을 것이 없으면 멈춘다 — `or` 폴백을 두면 그
    폴백이 곧 새 하드코딩이다.
    """

    def __init__(self, phase_id, key, detail, scope="loop"):
        self.phase_id, self.key, self.scope = phase_id, key, scope
        super(ConfigDeclarationError, self).__init__(
            "`%s` 의 `%s.%s` %s" % (phase_id, scope, key, detail))


def _loop_counter(front):
    """`loop.counter`. 어휘는 `state.COUNTERS` 다."""
    got = (front.get("loop") or {}).get("counter")
    if not got:
        raise ConfigDeclarationError(front.get("id"), "counter", "가 없다")
    if got not in st.COUNTERS:
        raise ConfigDeclarationError(
            front.get("id"), "counter",
            "가 어휘 밖이다: %r (%s)" % (got, ", ".join(st.COUNTERS)))
    return got


def _loop_max(front, profile=None):
    """`loop.max`, 또는 프로파일별이면 `loop.max_by_profile[profile]`."""
    loop = front.get("loop") or {}
    by = loop.get("max_by_profile")
    if by:
        got = by.get(profile) or by.get("normal")
        if not got:
            raise ConfigDeclarationError(
                front.get("id"), "max_by_profile",
                "에 %r 도 `normal` 도 없다" % (profile,))
        return got
    got = loop.get("max")
    if not got:
        raise ConfigDeclarationError(front.get("id"), "max",
                                     "도 `max_by_profile` 도 없다")
    return got


def _loop_return_to(front):
    """`loop.on_fail_return_to`. 없으면 기본 페이즈로 낙하하지 않는다."""
    got = (front.get("loop") or {}).get("on_fail_return_to")
    if not got:
        raise ConfigDeclarationError(front.get("id"), "on_fail_return_to",
                                     "가 없다")
    return got


def _loop_on_exceed(front):
    """`loop.on_exceed`. **어휘가 하나뿐인 것은 사실이다** — 둘째 동작이 없다.

    값을 늘리는 것은 그 동작을 구현한 뒤의 일이다. 지금 늘리면 선언이 다시
    기계 사실을 참칭한다.
    """
    got = (front.get("loop") or {}).get("on_exceed")
    if got not in LOOP_ON_EXCEED:
        raise ConfigDeclarationError(
            front.get("id"), "on_exceed",
            "가 어휘 밖이다: %r (%s)" % (got, ", ".join(LOOP_ON_EXCEED)))
    return got


def _converge_blocking(front):
    """`converge.blocking_severities` — 라운드를 강제하는 심각도 (ADR-H041).

    코드에 박지 않는다. 02 가 Critical 만 되돌리는 것과 같은 문턱을 01 이
    쓰는지는 선언이 말하고, 선언이 없으면 exit 2 다.
    """
    got = (front.get("converge") or {}).get("blocking_severities")
    if not got or not isinstance(got, list):
        raise ConfigDeclarationError(front.get("id"), "blocking_severities",
                                     "가 없다", scope="converge")
    bad = [s for s in got if s not in verdict.SEVERITIES]
    if bad:
        raise ConfigDeclarationError(
            front.get("id"), "blocking_severities",
            "가 어휘 밖이다: %r (%s)" % (bad, ", ".join(verdict.SEVERITIES)),
            scope="converge")
    return tuple(got)


def _loop_stuck_after(front):
    """`loop.stuck_after_identical`. 01 도 이제 읽는다 (ADR-H041)."""
    got = (front.get("loop") or {}).get("stuck_after_identical")
    if not got:
        raise ConfigDeclarationError(front.get("id"), "stuck_after_identical",
                                     "가 없다")
    return int(got)


DECLARATION_RENDER = """## 페이즈 선언을 읽을 수 없다

%s

**기본값으로 낙하시키지 않는다.** 낙하시키면 프론트매터를 고쳐도 조용히
무시되고, 그것이 M36 이다."""


def _declaration_envelope(cmd, s, exc):
    """선언 결함은 **제출물 결함이 아니다** — 코드가 아니라 설정이라 exit 2 다."""
    return st.envelope(
        cmd, False, 2, s,
        {"phase": exc.phase_id, "key": "%s.%s" % (exc.scope, exc.key)},
        DECLARATION_RENDER % exc, None)


class PlaceholderError(ValueError):
    """`${...}` 를 해결하지 못했다. lint-phases 가 exit 2 로 거부한다."""


# ------------------------------------------------------------------ 진단 출력

def warn(text):
    """사람이 읽는 줄은 전부 stderr 로. stdout 은 봉투 전용이다."""
    sys.stderr.write(text + "\n")


# ------------------------------------------------------------- 페이즈 파서

def _headings(lines):
    """(index, line) — **펜스 밖의** `## ` 헤딩만.

    페이즈 파일의 역할 프롬프트 템플릿·제출 형식은 코드 블록 안에 `## 네 소유
    경계` 같은 줄을 담는다. 그것을 헤딩으로 세면 두 곳이 동시에 망가진다 (M45):

    - `_section` 이 **여는 펜스 직후에서 절을 자른다.** 봉투가 소유권 표도
      제출 규약도 없이, 게다가 **닫히지 않은 펜스**를 실어 보낸다. 비는 것보다
      나쁘다 — 뒤따르는 절의 렌더가 그 안으로 빨려 들어간다.
    - `parse_phase_file` 의 `sections` 에 유령 절이 들어가, `lint-phases` 의
      "필수 절이 있는가" 가 **코드 블록 안의 글자로 통과할 수 있다.**

    한 곳에서 판정해 둘이 갈라지지 않게 한다.
    """
    out, in_fence = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("## "):
            out.append((i, line))
    return out


def parse_phase_file(path):
    """(front, body, sections). 프론트매터는 `---` 로 감싼 **JSON** 이다.

    서드파티 파서를 쓰지 않기 위한 결정이고(런타임 전제), 잘못 쓰면 즉시
    예외가 나므로 조용히 반쯤 읽히는 일이 없다.
    """
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError("프론트매터 구분자(---)로 시작하지 않는다")
    rest = text[3:].lstrip("\r\n")
    end = rest.find("\n---")
    if end < 0:
        raise ValueError("프론트매터를 닫는 --- 가 없다")
    raw, body = rest[:end], rest[end + 4:]
    try:
        front = json.loads(raw)
    except ValueError as exc:
        raise ValueError("프론트매터 JSON 파싱 실패: %s" % exc)
    if not isinstance(front, dict):
        raise ValueError("프론트매터가 객체가 아니다")
    sections = [line.strip() for _i, line in _headings(body.splitlines())]
    return front, body.lstrip("\r\n"), sections


def load_phases(root, phases_dir=None):
    """{id: {path, front, body, sections}} 또는 파싱 실패 목록."""
    d = Path(phases_dir or (Path(root) / PHASES_REL))
    loaded, broken = {}, []
    for p in sorted(d.glob("*.md")):
        try:
            front, body, sections = parse_phase_file(p)
        except ValueError as exc:
            broken.append((p, str(exc)))
            continue
        loaded[front.get("id") or p.stem] = {
            "path": p, "front": front, "body": body, "sections": sections}
    return loaded, broken


# ------------------------------------------------------------ 플레이스홀더

def build_context(root, paths=None, state=None, config=None, calibration=None):
    """`${config|calibration|run}` 세 네임스페이스. **state 는 여기에 없다.**

    state 는 `${}` 보간 대상이 아니라 `pointer`·`unless` 의 조건식 전용이다.
    보간 대상으로 만들면 페이즈 파일이 런 중 상태를 문자열로 끌어다 쓰기 시작하고,
    그러면 같은 페이즈 파일이 런마다 다른 것을 뜻하게 된다.
    """
    root = Path(root)
    if config is None:
        config = harness._read_json(root / harness.CONFIG_REL)
    if calibration is None:
        cal_rel = config.get("calibration_file")
        if cal_rel and (root / cal_rel).exists():
            try:
                calibration = harness._read_json(root / cal_rel)
            except (OSError, ValueError):
                calibration = {}
        else:
            calibration = {}

    if paths is not None:
        run_id, run_dir = paths.run_id, paths.run_dir.as_posix()
        slug = (state or {}).get("slug") or LINT_SLUG
    else:
        run_id, slug = LINT_RUN_ID, LINT_SLUG
        run_dir = (root / st.RUNS_REL / run_id).as_posix()

    template = ((config.get("contract") or {}).get("path_template")
                or "_workspace/contract_{slug}.md")
    return {
        "config": config,
        "calibration": calibration or {},
        "run": {"id": run_id, "dir": run_dir, "slug": slug,
                "contract_file": template.replace("{slug}", slug)},
    }


def _lookup(ctx, dotted):
    ns = dotted.split(".")[0]
    if ns not in _NAMESPACES:
        raise PlaceholderError(
            "`%s` 는 참조할 수 없는 네임스페이스다 — %s 셋만 쓴다"
            % (ns, "·".join(_NAMESPACES)))
    node = ctx
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise PlaceholderError("`${%s}` 를 해결하지 못했다" % dotted)
        node = node[part]
    return node


def resolve(value, ctx):
    """문자열 안의 `${...}` 를 해결한다.

    전체가 플레이스홀더 하나면 **원래 타입을 유지한다** — 숫자 비교가 문자열
    비교로 조용히 바뀌지 않게.
    """
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    if not isinstance(value, str):
        return value
    whole = _PLACEHOLDER.fullmatch(value.strip())
    if whole:
        return _lookup(ctx, whole.group(1))
    return _PLACEHOLDER.sub(lambda m: str(_lookup(ctx, m.group(1))), value)


# --------------------------------------------------------- 조건식 (unless 등)

# 포인터는 페이즈 id(`01-plan`)를 지날 수 있으므로 `-` 를 허용한다 (ADR-H042).
_CONDITION = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(==|!=)\s*(.+?)\s*$")


def eval_condition(expr, state):
    """`state.<pointer> == <JSON 리터럴>` 만 받는다. eval 을 쓰지 않는다.

    페이즈 파일이 임의 코드 실행 벡터가 되지 않게 하는 원칙이 명령에만
    적용되고 조건식에는 안 적용될 이유가 없다.
    """
    m = _CONDITION.match(expr or "")
    if not m:
        raise ValueError("해석할 수 없는 조건식: %r" % expr)
    pointer, op, literal = m.group(1), m.group(2), m.group(3)
    if not pointer.startswith("state."):
        raise ValueError("조건식은 state. 로 시작해야 한다: %r" % expr)
    try:
        want = json.loads(literal)
    except ValueError:
        want = literal.strip('"\'')
    got = harness._json_pointer(state or {}, pointer[len("state."):])
    return (got == want) if op == "==" else (got != want)


# --------------------------------------------------------------- requires

def check_requires(root, requires, ctx, state):
    """[{kind, ok, skipped, message}]. 실패해도 예외를 내지 않는다."""
    out = []
    for req in requires or []:
        kind = req.get("kind")
        if req.get("unless"):
            try:
                if eval_condition(req["unless"], state):
                    out.append({"kind": kind, "ok": True, "skipped": True,
                                "message": "unless 가 참이다: %s" % req["unless"]})
                    continue
            except ValueError as exc:
                out.append({"kind": kind, "ok": False, "skipped": False,
                            "message": str(exc)})
                continue
        handler = _REQUIRE_HANDLERS.get(kind)
        if handler is None:
            out.append({"kind": kind, "ok": False, "skipped": False,
                        "message": "알 수 없는 requires kind: %r" % kind})
            continue
        out.append(handler(Path(root), req, ctx, state))
    return out


def _req_file(root, req, ctx, state):
    path = root / resolve(req["path"], ctx)
    if not path.exists():
        return _bad("file", "없다: %s" % req["path"])
    raw = path.read_bytes()
    if req.get("min_bytes") and len(raw) < req["min_bytes"]:
        return _bad("file", "%s 가 %d바이트로 최소 %d 에 못 미친다"
                    % (req["path"], len(raw), req["min_bytes"]))
    needles = req.get("must_contain")
    if needles:
        text = raw.decode("utf-8", "replace")
        for needle in ([needles] if isinstance(needles, str) else needles):
            resolved = resolve(needle, ctx)
            if resolved not in text:
                return _bad("file", "%s 에 `%s` 가 없다" % (req["path"], resolved))
    pointer = req.get("sha256_pointer")
    if pointer:
        import hashlib
        want = harness._json_pointer(state or {}, pointer)
        if want and hashlib.sha256(raw).hexdigest() != want:
            return _bad("file", "%s 의 sha256 이 상태의 것과 다르다 — 동결이 깨졌다"
                        % req["path"])
    return _ok("file")


def _req_state(root, req, ctx, state):
    got = harness._json_pointer(state or {}, req["pointer"])
    if "in" in req:
        return (_ok("state") if got in req["in"] else
                _bad("state", "%s 가 %r 인데 %r 중 하나여야 한다"
                     % (req["pointer"], got, req["in"])))
    want = req.get("equals")
    return (_ok("state") if got == want else
            _bad("state", "%s 가 %r 인데 %r 이어야 한다" % (req["pointer"], got, want)))


def _req_adapter_stage(root, req, ctx, state):
    try:
        _config, adapter, _cal = adapters.load(root)
    except (OSError, ValueError, KeyError) as exc:
        return _bad("adapter_stage", "어댑터를 읽지 못했다: %s" % exc)
    absent = [n for n in req.get("steps") or []
              if adapters.stage_state(adapter, n) != "present"]
    if not absent:
        return _ok("adapter_stage")
    msg = "이 스택에 없는 스테이지: %s — 없는 것이지 통과한 것이 아니다" % ", ".join(absent)
    if req.get("mode") == "warn":
        return {"kind": "adapter_stage", "ok": True, "skipped": False,
                "warn": True, "message": msg}
    return _bad("adapter_stage", msg)


def _req_clean_ownership(root, req, ctx, state):
    import attribution
    verdict = attribution.clean_ownership(root, _config_of(ctx), _claims_of(root, req, ctx))
    if verdict["ok"]:
        return _ok("clean_ownership")
    return dict(_bad("clean_ownership", verdict["message"]), detail=verdict)


def _config_of(ctx):
    return ctx["config"]


def _claims_of(root, req, ctx):
    path = req.get("claims")
    if not path:
        return None
    p = Path(root) / resolve(path, ctx)
    if not p.exists():
        return None
    try:
        return harness._read_json(p)
    except (OSError, ValueError):
        return None


_REQUIRE_HANDLERS = {
    "file": _req_file,
    "state": _req_state,
    "adapter_stage": _req_adapter_stage,
    "clean_ownership": _req_clean_ownership,
}


def _ok(kind, message=""):
    return {"kind": kind, "ok": True, "skipped": False, "message": message}


def _bad(kind, message):
    return {"kind": kind, "ok": False, "skipped": False, "message": message}


# ---------------------------------------------------------------------- doctor

def cmd_doctor(root, args):
    """계약 계층 doctor 에 위임하고 파이프라인 검사 셋을 더한다.

    같은 검사를 두 번 구현하지 않는다 — 소유 판정·스키마 검증이 두 곳에서
    갈라지는 것이 이 리포가 이미 기록한 실패다.
    """
    report = harness.run_doctor(root)
    harness_data = {
        "checks": [{"name": c.name, "status": c.status, "message": c.message}
                   for c in report.checks],
        "failures": len(report.failures),
        "warnings": len(report.warnings),
    }
    pipeline = _pipeline_checks(root)
    warn(report.text())
    for c in pipeline:
        warn("  %-4s %s" % (c["status"], c["name"]))
        if c.get("message"):
            warn("         %s" % c["message"])

    failed = report.failures or [c for c in pipeline if c["status"] == "FAIL"]
    exit_ = 2 if failed else 0
    render = _doctor_render(harness_data, pipeline, exit_)
    return st.emit(st.envelope(
        "doctor", exit_ == 0, exit_, None,
        {"harness": harness_data, "pipeline": pipeline},
        render,
        None if exit_ else "python scripts/pipeline/cli.py init --feature <slug> "
                           "--request-file <경로>"))


def _pipeline_checks(root):
    """계약 계층이 보지 않는 것 셋. 전부 /feature 진입 **전에** 값싸게 잡힌다."""
    out = []

    # ① 작업 공간이 무시되는가. 아니면 03 의 clean_ownership 이 계약 파일을
    #    orphan 으로 잡아 exit 8 로 죽는다.
    r = harness._git(root, "check-ignore", "-q", "%s/probe" % st.WORKSPACE_REL)
    if r is None:
        out.append({"name": "작업 공간 무시", "status": "SKIP",
                    "message": "git 을 부를 수 없다"})
    elif r.returncode == 0:
        out.append({"name": "작업 공간 무시", "status": "PASS"})
    else:
        out.append({"name": "작업 공간 무시", "status": "FAIL",
                    "message": "%s/ 가 VCS 무시 목록에 없다. 계약 파일이 추적되는 "
                               "orphan 이 되어 03 이 exit 8 로 죽는다."
                               % st.WORKSPACE_REL})

    # ② 역할 에이전트 정의. 계약 계층에서는 경고지만 여기서는 차단이다 —
    #    03 이 그 파일 없이 돌 수 없다.
    try:
        config = harness._read_json(root / harness.CONFIG_REL)
    except (OSError, ValueError):
        config = {}
    #    폴백 교차검증기도 같이 본다 — config 가 이름을 부르는데 파일이 없으면
    #    02 가 그 에이전트를 못 찾는다. 계약 계층은 roles 만 훑으므로
    #    이 구멍은 여기서만 닫힌다.
    wanted = [(r_.get("agent"), "03-implement 가 호출할 대상이다")
              for r_ in (config.get("roles") or [])]
    fb = (config.get("cross_verify") or {}).get("fallback")
    if fb:
        wanted.append((fb, "02 의 폴백 교차검증기다"))
    missing = ["%s (%s)" % (a, why) for a, why in wanted
               if not (root / ".claude" / "agents" / ("%s.md" % a)).exists()]
    if not config.get("roles"):
        out.append({"name": "역할 에이전트 정의", "status": "SKIP",
                    "message": "config 를 읽지 못했다"})
    elif missing:
        out.append({"name": "역할 에이전트 정의", "status": "FAIL",
                    "message": "없다: %s" % ", ".join(missing)})
    else:
        out.append({"name": "역할 에이전트 정의", "status": "PASS"})

    # ③ 페이즈 파일. 깨진 채로 /feature 가 시작하면 런 중간에 알게 된다 (P5 의 일반형).
    phases_dir = root / PHASES_REL
    if not phases_dir.is_dir():
        out.append({"name": "페이즈 파일", "status": "FAIL",
                    "message": "%s/ 가 없다" % PHASES_REL})
    else:
        findings = lint_phases(root)
        bad = [f for f in findings if f["status"] == "FAIL"]
        out.append({"name": "페이즈 파일",
                    "status": "FAIL" if bad else "PASS",
                    "message": "; ".join("%s: %s" % (f["file"], f["message"])
                                         for f in bad[:3])})

    # ⑤ 00 이 읽을 임계값. 선언이 없으면 폴백이 곧 새 하드코딩이다 (M36).
    if config and not config.get("triage"):
        out.append({"name": "트리아지 선언", "status": "FAIL",
                    "message": "config.triage 가 없다 — 00 이 읽을 임계값이 없다. "
                               "harness/profiles/*/config.json 의 triage 블록을 본다"})
    elif config:
        out.append({"name": "트리아지 선언", "status": "PASS"})

    # ④ 계약 템플릿이 **자기 파서를 통과하는가.** 계약 계층의 "계약 절 ↔ 템플릿"
    #    은 절 제목 일치만 본다 — 템플릿이 시범 보이는 **형태**가 파서를 속이면
    #    첫 계약이 그 형태를 베끼고, 유닛이 부풀어 스코프 선택이 조용히 빗나간다.
    out.append(_check_template_parses(root, config))

    # ⑤ 리뷰어. 05 가 없는 스킬을 부르면 라운드마다 헛돌고, 그것을 알게 되는
    #    시점은 리뷰어를 이미 띄운 뒤다. **기동 전에, 무료로 잡는다** (§E10).
    out.append(_check_reviewers(root, config))

    # ⑥ 원격과 base. 06 이 진입할 때 exit 9(3지선다)로 멈추는 것을 **기동 전에,
    #    무료로** 알려 준다 (§P3). 여기서 막지는 않는다 — 원격 없이 로컬까지만
    #    가는 것도 정당한 선택이고, 그 선택은 사람의 것이다.
    out.append(_check_remote(root, config))

    # ⑦ 외부 리뷰 봇. 켜 놓고 대상을 안 적으면 07 이 아무도 아닌 것을 기다린다.
    out.append(_check_external_bot(config))
    return out


def _check_remote(root, config):
    name = "원격과 base 브랜치"
    if not config:
        return {"name": name, "status": "SKIP", "message": "config 를 읽지 못했다"}
    vcs = config.get("vcs") or {}
    remote = vcs.get("remote") or "origin"
    base = vcs.get("base_branch")
    r = harness._git(root, "remote")
    names = (r.stdout.split() if r is not None and r.returncode == 0 else [])
    if remote not in names:
        return {"name": name, "status": "WARN",
                "message": "원격 %r 이 없다 — 06 이 exit 9 3지선다로 멈춘다. "
                           "**자동으로 만들지 않는다** (§P3)." % remote}
    v = harness._git(root, "rev-parse", "--verify", "-q",
                     "refs/remotes/%s/%s" % (remote, base))
    if v is None or v.returncode != 0:
        return {"name": name, "status": "WARN",
                "message": "base %r 이 원격 %r 에 없다 — 06 이 exit 9 로 "
                           "멈춘다." % (base, remote)}
    return {"name": name, "status": "PASS",
            "message": "%s/%s 확인" % (remote, base)}


def _check_external_bot(config):
    name = "외부 리뷰 봇"
    ext = (config or {}).get("external_pr_review") or {}
    if not ext.get("enabled"):
        return {"name": name, "status": "PASS",
                "message": "꺼져 있다 — gap 이 아니다. 07 의 내장 리뷰는 05 가 "
                           "ok 가 아니거나 트리아지가 빗나갔거나 05 지적이 0건이거나 "
                           "감사 런일 때 돌고, 그 밖은 생략한다 "
                           "(ADR-H043 · ADR-H059)."}
    if not (ext.get("bot_logins") or []):
        return {"name": name, "status": "FAIL",
                "message": "켜져 있는데 bot_logins 가 비었다 — 07 이 아무도 "
                           "아닌 것을 timeout_sec 동안 기다린다."}
    return {"name": name, "status": "PASS",
            "message": "%s — 폴링 %ss / 상한 %ss (**미검증 상속값**)"
                       % (", ".join(ext["bot_logins"]), ext.get("poll_sec"),
                          ext.get("timeout_sec"))}


def _check_reviewers(root, config):
    import review as review_mod

    name = "리뷰어 스킬"
    if not config:
        return {"name": name, "status": "SKIP", "message": "config 를 읽지 못했다"}
    reviewers = config.get("reviewers") or []
    if not reviewers:
        return {"name": name, "status": "WARN",
                "message": "config.reviewers 가 비어 있다 — 05 의 "
                           "review05.status 가 늘 failed 이고 등급이 "
                           "PASS_WITH_GAPS 로 떨어진다. 통과가 아니라 미수행이다."}
    errors = review_mod.validate(root, config)
    if errors:
        return {"name": name, "status": "FAIL", "message": "; ".join(errors[:3])}
    return {"name": name, "status": "PASS",
            "message": "%d종 — %s" % (len(reviewers),
                                     ", ".join(r["code"] for r in reviewers))}


def _check_template_parses(root, config):
    import contract as contract_mod
    name = "계약 템플릿 파싱"
    path = Path(root) / harness.CONTRACT_TEMPLATE_REL
    if not path.is_file() or not config:
        return {"name": name, "status": "SKIP",
                "message": "템플릿이나 config 를 읽지 못했다"}
    try:
        parsed = contract_mod.parse(path.read_text(encoding="utf-8"), config)
    except (OSError, ValueError) as exc:
        return {"name": name, "status": "FAIL", "message": "파싱 실패: %s" % exc}
    dropped = parsed.get("dropped") or []
    if dropped:
        return {"name": name, "status": "FAIL",
                "message": "템플릿이 자기 파서에 유닛 아닌 것 %d개를 낸다: %s — "
                           "첫 계약이 이 형태를 베끼면 유닛 수가 부풀어 프로파일 "
                           "판정과 스코프 선택이 함께 빗나간다."
                           % (len(dropped),
                              ", ".join(repr(d["raw"]) for d in dropped[:3]))}
    if not parsed.get("units"):
        return {"name": name, "status": "FAIL",
                "message": "템플릿의 유닛 절이 파서에 아무것도 내지 않는다 — "
                           "시범이 시범 노릇을 못 한다."}
    return {"name": name, "status": "PASS",
            "message": "유닛 %d개 · 버려진 것 0개" % len(parsed["units"])}


def _doctor_render(harness_data, pipeline, exit_):
    if exit_ == 0:
        return ("doctor 통과. 경고 %d건은 진행을 막지 않지만 전부 stderr 에 드러나 있다.\n"
                "`init --feature <slug> --request-file <경로>` 로 런을 시작한다."
                % harness_data["warnings"])
    lines = ["## doctor 미통과 — /feature 를 시작하지 않는다", ""]
    for c in harness_data["checks"]:
        if c["status"] == "FAIL":
            lines.append("- %s: %s" % (c["name"], c["message"]))
    for c in pipeline:
        if c["status"] == "FAIL":
            lines.append("- %s: %s" % (c["name"], c.get("message", "")))
    lines += ["", "설정과 실물이 어긋난 채로 시작하면 게이트가 한참 돌고 나서 드러난다.",
              "위 항목을 고친 뒤 다시 실행한다."]
    return "\n".join(lines)


# ----------------------------------------------------------------- lint-phases

def lint_phases(root, phases_dir=None):
    """페이즈 파일을 검증해 findings 를 낸다.

    이것이 CI 없이도 도는 유일한 검증 장치다. 여기서 못 잡으면 런 중간에
    알게 되고, 그때는 앞 페이즈에 쓴 시간이 이미 낭비된 뒤다.
    """
    root = Path(root)
    d = Path(phases_dir or (root / PHASES_REL))
    out = []

    def add(file, rule, status, message=""):
        out.append({"file": str(file), "rule": rule,
                    "status": status, "message": message})

    if not d.is_dir():
        add(PHASES_REL, "phases_dir", "FAIL", "페이즈 디렉터리가 없다")
        return out

    loaded, broken = load_phases(root, d)
    for path, msg in broken:
        add(path.name, "frontmatter", "FAIL", msg)
    if not loaded:
        if not broken:
            add(PHASES_REL, "phases_dir", "FAIL", "페이즈 파일이 없다")
        return out

    try:
        config, adapter, calibration = adapters.load(root)
    except (OSError, ValueError, KeyError) as exc:
        add(harness.CONFIG_REL, "config", "FAIL", "설정·어댑터를 읽지 못했다: %s" % exc)
        return out
    ctx = build_context(root, config=config, calibration=calibration)

    _lint_runner_bin(root, adapter, config, add)
    _lint_infra_preflight(adapter, config, add)

    seen_index, seen_keys, terminals = {}, {}, []
    max_index = max((p["front"].get("index") or 0) for p in loaded.values())

    for pid, item in sorted(loaded.items()):
        front, path, sections = item["front"], item["path"], item["sections"]
        name = path.name

        # ── 신원
        if pid != path.stem:
            add(name, "id", "FAIL", "id(%r) 가 파일명(%r) 과 다르다" % (pid, path.stem))
        idx = front.get("index")
        if idx in seen_index:
            add(name, "index", "FAIL", "index %r 가 %s 와 겹친다" % (idx, seen_index[idx]))
        else:
            seen_index[idx] = pid
        unknown = [k for k in front if k not in FRONT_KEYS]
        if unknown:
            add(name, "front_keys", "FAIL", "알 수 없는 최상위 키: %s" % ", ".join(unknown))

        # ── 본문 섹션
        missing = [s for s in REQUIRED_SECTIONS if s not in sections]
        if front.get("allow", {}).get("agents") and ROLE_TEMPLATE_SECTION not in sections:
            missing.append(ROLE_TEMPLATE_SECTION)
        if missing:
            add(name, "sections", "FAIL", "필수 절이 없다: %s" % ", ".join(missing))

        # 펜스가 안 닫히면 `_headings` 가 그 뒤의 헤딩을 못 보고, 절 하나가
        # 파일 끝까지 삼킨다. 봉투에서 알게 되면 이미 그 페이즈의 지시가
        # 틀린 채로 나간 뒤다 — 런 전에 잡는 것이 싸다 (M45).
        if item["body"].count("```") % 2:
            add(name, "fences", "FAIL",
                "코드 펜스(```)가 홀수다 — 안 닫힌 블록이 절 경계를 삼킨다")

        # ── 게이트
        gate = front.get("gate") or {}
        runner = gate.get("runner")
        if runner not in ("adapter", "none"):
            add(name, "runner", "FAIL",
                "gate.runner 는 adapter 또는 none 이다 (받은 값: %r). "
                "페이즈 파일이 임의 명령 실행 벡터가 되지 않게 한다" % runner)
        for step in gate.get("steps") or []:
            sid = step.get("id")
            if sid not in (adapter.get("stages") or {}):
                add(name, "stage", "FAIL", "어댑터에 없는 스테이지 이름: %r" % sid)
            if "background" in step:
                _lint_background(name, step, ctx, add)

        # ── 어휘
        for req in front.get("requires") or []:
            if req.get("kind") not in REQUIRES_KINDS:
                add(name, "requires_kind", "FAIL",
                    "알 수 없는 requires kind: %r (%s)"
                    % (req.get("kind"), ", ".join(REQUIRES_KINDS)))
        for prod in front.get("produces") or []:
            if prod.get("kind") not in PRODUCES_KINDS:
                add(name, "produces_kind", "FAIL",
                    "알 수 없는 produces kind: %r (%s)"
                    % (prod.get("kind"), ", ".join(PRODUCES_KINDS)))
            key = prod.get("key")
            if key in seen_keys:
                add(name, "produces_key", "FAIL",
                    "produces.key %r 가 %s 와 겹친다" % (key, seen_keys[key]))
            else:
                seen_keys[key] = pid
        _lint_loop(name, pid, front, loaded, add)
        _lint_converge(name, front, add)
        _lint_skip_policy(name, front, add)
        _lint_conditions(name, front, add)

        # ── 플레이스홀더와 경로
        _lint_placeholders(name, front, ctx, add)
        _lint_paths(name, front, ctx, add)

        # ── 역할 에이전트 정의
        _lint_agents(root, name, front, config, add)

        # ── 전이
        nxt = front.get("on_success")
        if nxt == st.DONE:
            # 종단이다. 전이 대상을 찾지 않는다.
            terminals.append(pid)
        elif not nxt:
            add(name, "on_success", "FAIL",
                "on_success 가 없다 — 마지막 페이즈는 `%s` 를 명시한다. "
                "적지 않으면 런이 닫히는 자리가 코드 어디에도 생기지 않는다 (M24)"
                % st.DONE)
        elif nxt not in loaded:
            nxt_idx = _index_prefix(nxt)
            if nxt_idx is not None and nxt_idx > max_index:
                add(name, "on_success", "WARN",
                    "%r 는 아직 없다 (FUTURE) — 그 페이즈가 생기면 이어진다" % nxt)
            else:
                add(name, "on_success", "FAIL", "전이 대상이 없다: %r" % nxt)

    # **종단이 없는 것은 FAIL 이 아니다** — 확장 중인 실행기는 마지막 페이즈가
    # 아직 없는 다음을 가리키는 상태가 정상이고, 그 자리는 이미 WARN 이 본다.
    # 둘 이상인 것만 막는다: 런이 닫히는 자리는 하나여야 한다.
    if len(terminals) > 1:
        add("(전체)", "terminal", "FAIL",
            "종단이 둘 이상이다: %s — 런이 닫히는 자리는 하나다"
            % ", ".join(sorted(terminals)))

    _lint_cycle(loaded, add)
    _lint_taxonomy(root, add)
    _lint_reviewers(root, config, add)
    return out


def _lint_converge(name, front, add):
    """`converge.blocking_severities` 가 **읽히는 값**인가 (ADR-H041)."""
    conv = front.get("converge")
    if not conv:
        return
    try:
        _converge_blocking(front)
    except ConfigDeclarationError as exc:
        add(name, "blocking_severities", "FAIL", str(exc))


def _lint_skip_policy(name, front, add):
    """`skip_policy[].when` 이 `eval_condition` 문법인가 (ADR-H042 · ADR-H044).

    리스트다 — 02 는 `docs_profile`(00 이 docs 레인으로 예측했다)과
    `plan_unedited`(01 이 1라운드에 수렴했다) 둘을 갖고, 첫 일치가 이긴다.
    사유가 다른 두 스킵을 하나의 선언에 뭉치면 보고서가 거짓 사유를 적는다.
    """
    node = front.get("skip_policy")
    if node is None:
        return
    if not isinstance(node, list):
        add(name, "skip_policy", "FAIL", "skip_policy 는 배열이어야 한다")
        return
    for i, item in enumerate(node):
        try:
            eval_condition((item or {}).get("when"), {})
        except ValueError as exc:
            add(name, "skip_policy", "FAIL", "skip_policy[%d].when: %s" % (i, exc))
        if (item or {}).get("status") not in st.PHASE_STATUS:
            add(name, "skip_policy", "FAIL",
                "skip_policy[%d].status 가 어휘 밖이다: %r"
                % (i, (item or {}).get("status")))
        if not (item or {}).get("reason"):
            add(name, "skip_policy", "FAIL",
                "skip_policy[%d].reason 이 없다 — 사유 없는 스킵은 보고서가 "
                "설명하지 못한다" % i)


def _lint_conditions(name, front, add):
    """`review.unless` · `allow.unless` 가 조건식 문법인가 (ADR-H044).

    01 의 리뷰어와 03 의 역할을 레인별로 끄는 선언이다. 코드가 읽는 선언이라
    문법이 틀리면 런 한복판에서 exit 2 를 만난다 — 여기서 먼저 잡는다.
    """
    for key in ("review", "allow"):
        expr = (front.get(key) or {}).get("unless")
        if expr is None:
            continue
        try:
            eval_condition(expr, {})
        except ValueError as exc:
            add(name, "%s_unless" % key, "FAIL", "%s.unless: %s" % (key, exc))


def _lint_loop(name, pid, front, loaded, add):
    """루프 선언이 **읽히는 값**인가.

    M36: `on_exceed` · `on_fail_return_to` · 상한이 프론트매터에만 있고 코드는
    하드코딩을 썼다. 이제 코드가 읽으므로, 선언이 어휘 밖이면 런 중간이 아니라
    **여기서** 안다. 검사하지 않으면 exit 2 를 런 한복판에서 만난다.
    """
    loop = front.get("loop") or {}
    if not loop:
        return
    counter = loop.get("counter")
    if not counter:
        add(name, "counter", "FAIL", "loop.counter 가 없다 — 무엇을 세는지 "
                                     "코드가 읽을 자리가 없다")
    elif counter not in st.COUNTERS:
        add(name, "counter", "FAIL",
            "알 수 없는 카운터: %r (%s)" % (counter, ", ".join(st.COUNTERS)))

    if not loop.get("max") and not loop.get("max_by_profile"):
        add(name, "loop_max", "FAIL",
            "loop.max 도 loop.max_by_profile 도 없다 — 상한이 코드에만 남는다")

    on_exceed = loop.get("on_exceed")
    if on_exceed is not None and on_exceed not in LOOP_ON_EXCEED:
        add(name, "on_exceed", "FAIL",
            "loop.on_exceed 가 어휘 밖이다: %r (%s) — **없는 동작을 어휘로 "
            "예고하지 않는다.** 늘리려면 그 동작을 먼저 만든다"
            % (on_exceed, ", ".join(LOOP_ON_EXCEED)))

    # 01 은 `converge` 와 `loop` 가 같은 초과 동작을 선언한다. 코드는 `loop` 를
    # 읽으므로 둘이 갈리면 `converge` 쪽이 조용히 무시된다.
    conv_exceed = (front.get("converge") or {}).get("on_exceed")
    if conv_exceed is not None and conv_exceed != on_exceed:
        add(name, "on_exceed", "FAIL",
            "converge.on_exceed(%r) 와 loop.on_exceed(%r) 가 다르다 — "
            "코드는 loop 를 읽는다" % (conv_exceed, on_exceed))

    back = loop.get("on_fail_return_to")
    if back is not None:
        if back not in loaded:
            add(name, "on_fail_return_to", "FAIL", "되돌아갈 페이즈가 없다: %r" % back)
        else:
            here = front.get("index") or 0
            there = (loaded[back]["front"].get("index") or 0)
            if there >= here:
                add(name, "on_fail_return_to", "FAIL",
                    "%r 는 자기(index %s)보다 뒤다(index %s) — 되돌림은 뒤로만 "
                    "간다" % (back, here, there))


def _index_prefix(phase_id):
    head = (phase_id or "").split("-")[0]
    return int(head) if head.isdigit() else None


def _lint_runner_bin(root, adapter, config, add):
    """화이트리스트는 스키마의 enum 이다 — 실행기 코드에 목록을 두지 않는다."""
    try:
        schema = harness._read_json(root / harness.ADAPTER_SCHEMA_REL)
    except (OSError, ValueError):
        return
    allowed = (((schema.get("properties") or {}).get("runner") or {})
               .get("properties", {}).get("bin", {}).get("enum"))
    got = (adapter.get("runner") or {}).get("bin")
    if allowed and got not in allowed:
        add("harness/adapters/%s.json" % config.get("adapter"), "runner_bin", "FAIL",
            "runner.bin %r 이 화이트리스트 밖이다" % got)


def _lint_infra_preflight(adapter, config, add):
    """면제에는 이유가 있어야 한다 (M44).

    `on_missing: "warn"` 은 "키가 없어도 회귀가 돈다" 는 주장이다. 그 주장의
    근거가 없으면 다음 사람이 검증할 수 없고, 검증할 수 없는 면제는 면제가
    아니라 구멍이다.
    """
    where = "harness/adapters/%s.json" % config.get("adapter")
    for probe in adapter.get("infra_preflight") or []:
        policy = probe.get("on_missing") or "fail"
        if policy not in ("fail", "warn"):
            add(where, "infra_preflight", "FAIL",
                "%s 의 on_missing 이 어휘 밖이다: %r (fail, warn)"
                % (probe.get("name"), policy))
        elif policy == "warn" and not (probe.get("why") or "").strip():
            add(where, "infra_preflight", "FAIL",
                "%s 는 on_missing=warn 인데 why 가 없다 — 왜 그 프로브 없이도 "
                "회귀가 도는지를 적지 않으면 면제가 아니라 구멍이다"
                % probe.get("name"))


def _lint_background(name, step, ctx, add):
    """참으로 해석되면 거부한다 — 조용히 동기로 낙하시키지 않는다.

    이 스켈레톤은 백그라운드 경로를 만들지 않았다. 없는 기계를 있는 척
    통과시키는 것이 이 문서군이 막으려는 실패다.
    """
    try:
        value = resolve(step["background"], ctx)
    except PlaceholderError as exc:
        add(name, "placeholder", "FAIL", str(exc))
        return
    if value:
        add(name, "background", "FAIL",
            "background 가 참으로 해석된다. 이 실행기는 동기 실행만 지원하므로 "
            "거부한다 — 조용히 동기로 돌리면 없는 기계를 통과시키는 것이다")


def _lint_placeholders(name, front, ctx, add):
    try:
        resolve({k: v for k, v in front.items() if k != "gate"}, ctx)
        for step in (front.get("gate") or {}).get("steps") or []:
            resolve({k: v for k, v in step.items() if k != "background"}, ctx)
    except PlaceholderError as exc:
        add(name, "placeholder", "FAIL", str(exc))


def _lint_paths(name, front, ctx, add):
    for prod in front.get("produces") or []:
        try:
            path = resolve(prod.get("path", ""), ctx)
        except PlaceholderError:
            continue                      # 위에서 이미 잡았다
        if len(str(path)) > harness.PATH_LIMIT:
            add(name, "path_length", "FAIL",
                "산출물 경로가 %d자로 상한 %d 를 넘는다: %s"
                % (len(str(path)), harness.PATH_LIMIT, path))


def _lint_agents(root, name, front, config, add):
    allow = (front.get("allow") or {}).get("agents")
    if allow != "config.roles[].agent":
        return
    for role in config.get("roles") or []:
        agent = role.get("agent")
        path = root / ".claude" / "agents" / ("%s.md" % agent)
        if not path.exists():
            add(name, "agent_file", "FAIL",
                ".claude/agents/%s.md 가 없다 — 기동 전에 잡는다" % agent)
            continue
        # 모델 등급은 봉투가 레인별로 정한다 (`config.models`, ADR-H044).
        # 프론트매터에 박으면 두 출처가 되고, 갈라진 날 어느 쪽이 이겼는지
        # 실행기가 볼 수 없다.
        if _agent_declares_model(path):
            add(name, "agent_model", "WARN",
                ".claude/agents/%s.md 프론트매터에 `model:` 이 있다 — 등급은 "
                "봉투가 `config.models` 로 지시한다. 두 출처가 갈라지면 어느 "
                "쪽이 이겼는지 실행기가 보지 못한다" % agent)


def _agent_declares_model(path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    head = text.split("---", 2)
    front = head[1] if len(head) > 2 else ""
    return any(line.strip().startswith("model:") for line in front.splitlines())


def _lint_cycle(loaded, add):
    for pid in loaded:
        seen, cur = [], pid
        while cur in loaded:
            if cur in seen:
                add(loaded[pid]["path"].name, "cycle", "FAIL",
                    "전이가 순환한다: %s" % " → ".join(seen + [cur]))
                return
            seen.append(cur)
            cur = loaded[cur]["front"].get("on_success")


def _lint_taxonomy(root, add):
    """원장 어휘 · 승격 목적지 · 리뷰 범위 셋의 단일 출처를 검사한다.

    이 파일이 손상되면 셋이 **동시에** 조용히 틀어진다 — 05 가 검토 제외 목록을
    잘못 만들고, 승격이 갈 곳을 잃고, 원장이 어휘 밖의 코드를 받는다.
    """
    import ledger

    path = Path(root) / TAXONOMY_REL
    if not path.exists():
        add(TAXONOMY_REL, "taxonomy", "SKIP",
            "원장이 아직 없다 — `ledger.seed(root)` 가 시드를 만든다")
        return
    try:
        data = harness._read_json(path)
    except (OSError, ValueError) as exc:
        add(TAXONOMY_REL, "taxonomy", "FAIL", "읽지 못했다: %s" % exc)
        return
    for err in ledger.validate_taxonomy(data):
        add(TAXONOMY_REL, "taxonomy", "FAIL", err)


def _lint_reviewers(root, config, add):
    """스킬 파일 실재 · 작성자 격리 · code 유니크.

    **기동 전에, 무료로 잡는다** (§E10 첫 행). 05 가 없는 스킬을 부르면 라운드
    마다 헛돌고, 그것을 알게 되는 시점은 리뷰어를 이미 띄운 뒤다.
    """
    import review as review_mod

    for err in review_mod.validate(root, config):
        add(harness.CONFIG_REL, "reviewers", "FAIL", err)


def cmd_lint_phases(root, args):
    findings = lint_phases(root, args.dir)
    bad = [f for f in findings if f["status"] == "FAIL"]
    exit_ = 2 if bad else 0
    for f in findings:
        warn("  %-4s %s — %s" % (f["status"], f["file"], f["message"]))
    render = ("페이즈 파일 검증 통과." if not bad else
              "## 페이즈 파일 거부\n\n" +
              "\n".join("- `%s` %s: %s" % (f["file"], f["rule"], f["message"])
                        for f in bad))
    return st.emit(st.envelope("lint-phases", exit_ == 0, exit_, None,
                               {"findings": findings}, render, None))


# ------------------------------------------------------------------------ init

def cmd_init(root, args):
    return st.emit(run_init(root, args.feature, args.request_file, args.profile))


def run_init(root, slug, request_file, profile=None):
    root = Path(root)
    if not slug or not harness.NAME_RE.match(slug):
        return st.envelope("init", False, 2, None, {"slug": slug},
                           "슬러그는 `^[a-z0-9][a-z0-9-]*$` 여야 한다: %r" % slug, None)
    req = Path(request_file)
    if not req.is_absolute():
        req = root / req
    if not req.exists():
        return st.envelope("init", False, 2, None, {"request_file": str(req)},
                           "요청 파일이 없다: %s" % request_file, None)
    paths, s = st.create_run(root, slug, req, profile=profile)
    data = {"run_id": s["run_id"], "run_dir": str(paths.run_dir),
            "request_sha256": s["request"]["sha256"],
            "profile": s["profile"]}
    render = ("런 `%s` 을 만들었다. 요청은 바이트 그대로 동결됐고 sha256 이 박혔다.\n"
              "`next` 로 첫 페이즈 지시문을 받는다." % s["run_id"])
    return st.envelope("init", True, 0, s, data, render,
                       "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])


# ------------------------------------------------------------------------ next

def cmd_next(root, args):
    return st.emit(run_next(root, args.run_id))


def run_next(root, run_id=None):
    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("next", False, 3, None, {},
                           "런이 없다. `init --feature <slug> --request-file <경로>` 로 시작한다.",
                           None)
    if s.get("escalated"):
        return _escalation_envelope("next", paths, s)

    pid = s.get("phase")
    loaded, _broken = load_phases(root)
    phase = loaded.get(pid)
    if phase is None:
        return st.envelope("next", True, 11, s, {"phase": pid},
                           _horizon_render(pid, loaded), None)

    ctx = build_context(root, paths, s)
    checks = check_requires(root, phase["front"].get("requires"), ctx, s)
    failed = [c for c in checks if not c["ok"]]
    if failed:
        return st.envelope(
            "next", False, 3, s, {"requires_report": checks},
            "## %s 진입 거부\n\n선행 조건이 채워지지 않았다.\n\n%s%s"
            % (pid, "\n".join("- %s" % c["message"] for c in failed),
               _journey_hint(root, s) if pid == "03-implement" else ""),
            None)

    st.set_phase_status(s, pid, "running")
    st.append_event(paths, "phase_enter", cmd="next", phase=pid)
    skipped = _skip_policy(root, paths, s, phase, ctx, "next")
    if skipped is not None:
        return skipped
    if pid == "00-triage":
        # 기계 신호로 확정되면 **모델을 부르지 않고** 같은 봉투에서 01 지시문을
        # 낸다 (ADR-H044). 미확정이면 아래로 내려가 00 패킷(저가 모델 1회)이다.
        decided = _plan_00_triage(root, paths, s, phase, ctx)
        if decided is not None:
            decided.setdefault("data", {})["prescan"] = _prescan(root, loaded, ctx, s)
            return decided
    if pid == "05-code-review":
        node05 = _plan_05_review(root, paths, s, ctx)
        refused = node05.get("routing_refused")
        if refused:
            node05.pop("routing_refused", None)
            files = refused["pr_scope_changed"]
            return st.envelope(
                "next", False, 3, s,
                {"pr_scope_changed": files},
                "## 05 라우팅 대상이 워킹트리에 없다\n\n"
                "리뷰어 라우팅은 **미커밋 diff** 를 본다(ADR-H028). 지금 워킹트리는 "
                "깨끗한데 base 이후 커밋에는 변경이 %d개 파일 있다 — 05 통과 **전에** "
                "커밋했다.\n\n커밋 자리는 `record --phase 05` 통과 직후 · "
                "`precheck --phase 06` 이전 한 곳뿐이다. 아직 push 전이면 "
                "`git reset --mixed HEAD~1` 로 커밋만 되돌리고(브랜치는 유지된다) "
                "`next` 를 다시 친다. `review05` 는 쓰지 않았다 — 되돌리면 gap 없이 "
                "정상 라우팅된다 (ADR-H046).\n\n"
                "커밋된 변경:\n%s"
                % (len(files), "\n".join("- `%s`" % f for f in files[:20])),
                "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])
    if pid == "03-implement":
        refused = _contract_precheck_refuse(root, paths, s, ctx, "next")
        if refused is not None:
            return refused
        _store_dispatch(root, ctx, s, phase["front"])
    # **지시를 낸 자리에서 센다** (M26). `next` 는 같은 페이즈에서 여러 번
    # 불릴 수 있으므로 키로 멱등을 만든다.
    _t, _m, exhausted = _instruct(
        s, pid, _instruction_keys(s, pid, ctx, phase["front"]), ctx)
    st.save(paths, s)

    render, next_cmd = render_packet(root, phase, ctx, s, checks)
    env = st.envelope("next", True, 0, s,
                      {"produces": [resolve(p.get("path"), ctx)
                                    for p in phase["front"].get("produces") or []],
                       "requires_report": checks,
                       # 런 전체의 사전 검사는 **첫 페이즈**에서 한 번 — 01 이
                       # 전이로 나오는 런(00 기계 확정)에서도 빠지지 않는다.
                       "prescan": _prescan(root, loaded, ctx, s) if pid == "00-triage" else []},
                      render, next_cmd)
    return _budget_stop(paths, env) if exhausted else env


# ------------------------------------------------------------- 00 트리아지

def _plan_00_triage(root, paths, s, phase_item, ctx):
    """00 진입 — 기계가 먼저 본다. 확정되면 01 패킷을 든 봉투, 아니면 None.

    None 은 "모델 1회가 필요하다" 다. `phases.00-triage.needs_model` 이 그
    사실을 들고, `_instruction_keys` 가 그것을 읽어 `00:triage` 를 센다.
    """
    import triage

    config = ctx["config"]
    node = s.setdefault("phases", {}).setdefault("00-triage", {})
    text = paths.request.read_text(encoding="utf-8")
    sig = triage.signals(text, config)
    node["signals"] = sig
    # 원장의 열린 `deferred` 중 요청 경로와 겹치는 것 — 이월은 여기서 다음
    # 런에 닿는다 (ADR-H051 결정 3). 원장이 없거나 깨졌으면 0 이 아니라 미계측.
    import ledger
    try:
        node["deferred_overlap"] = ledger.deferred_overlap(
            root, (sig.get("paths_role_owned") or []) + (sig.get("paths_docs") or [])
            + (sig.get("paths_unresolved") or []))
    except (OSError, ValueError):
        node["deferred_overlap"] = None
    prof = s.get("profile") or {}
    if prof.get("source") == "user":
        # 사람이 `init --profile` 로 정했다. 신호만 남기고 판정을 덮지 않는다.
        decision = {"profile": prof.get("name"), "expected_paths": [],
                    "touches_source": None, "reasons": ["user_profile"],
                    "decided_by": "user"}
    else:
        try:
            decision = triage.decide(sig, config)
        except ValueError as exc:
            return _declaration_envelope("next", s, ConfigDeclarationError(str(exc)))
        if decision is None:
            if (config.get("triage") or {}).get("model_call_when_undecided", True):
                node["needs_model"] = True
                st.save(paths, s)
                return None
            decision = triage.fallback_when_no_model(sig)
    _apply_triage(paths, s, decision, sig, cmd="next")
    _write_triage_file(paths, decision, sig)
    st.save(paths, s)
    return _advance_to_next(root, paths, s, phase_item, ctx, cmd="next")


def _apply_triage(paths, s, decision, sig, cmd):
    """예측을 `state.profile` 로 굳힌다. docs 면 계약이 없는 런이 된다."""
    prof = s.get("profile") or {}
    name = decision["profile"]
    decided_by = decision.get("decided_by") or "model"
    # 사람이 정한 것(`init --profile` · exit 9 의 답)은 자동 재판정에 안 밀린다.
    # 기계·모델의 예측은 밀린다 — 그것이 `triage` 출처의 뜻이다.
    source = ("user" if (prof.get("source") == "user" or decided_by == "user")
              else "triage")
    s["profile"] = {
        "name": name, "source": source,
        "predicted": {k: decision.get(k) for k in
                      ("profile", "expected_paths", "touches_source",
                       "reasons", "decided_by")},
        "signals": sig, "applied": []}
    if name == "docs":
        # **여기가 `no_contract` 의 첫 쓰기 주체다.** 04·05·06 은 읽기만 한다.
        s["contract"] = {"mode": "no_contract", "present": False,
                         "reason": "docs_profile"}
    node = s.setdefault("phases", {}).setdefault("00-triage", {})
    node["decided_by"] = decided_by
    node["profile"] = name
    st.append_event(paths, "triage_decided", cmd=cmd, phase="00-triage",
                    profile=name, decided_by=decided_by, source=source)


def _write_triage_file(paths, decision, sig):
    """기계 확정 경로에서는 실행기가 산출물을 쓴다 (04 의 게이트 보고서와 같다)."""
    out = dict(decision, signals=sig)
    (paths.run_dir / "00_triage.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _note_applied(s, tag):
    """프로파일이 실제로 적용한 양보를 적는다. miss 의 gap 이름이 이것으로 만들어진다."""
    prof = s.setdefault("profile", {})
    applied = prof.setdefault("applied", [])
    if tag not in applied:
        applied.append(tag)


def _note_triage_miss(paths, s, before, after, where, cmd="record"):
    """예측이 **상향**으로 빗나갔다 — 앞 페이즈가 양보를 적용한 채 지나갔다.

    gap 이름에 실제로 적용된 양보만 넣는다 (재지 않은 것을 적지 않는다).
    반환: `profile.triage_miss` 에 넣을 dict.
    """
    applied = list(before.get("applied") or [])
    gap = "triage_miss:" + (";".join(applied) if applied else "none")
    st.demote(s, st.GRADES[1], gap)
    miss = {"at": where, "was": before.get("name"), "became": after.get("name"),
            "applied": applied}
    st.append_event(paths, "triage_miss", cmd=cmd, phase=where,
                    was=before.get("name"), became=after.get("name"),
                    applied=applied)
    return miss


def _docs_lane_source_check(root, paths, s, ctx, where, source_changed=None,
                            cmd="record"):
    """docs 레인인데 소스가 바뀌었는가. 반환: miss 가 났으면 True.

    docs 레인은 계약이 없어 `_refresh_profile` 이 조기 반환한다 — 그래서
    03(claims 제출)과 05(라우팅) 두 자리가 따로 묻는다. 술어는 05 의 라우팅과
    같은 `review._source_changed` 다.
    """
    import attribution
    import review as review_mod

    prof = s.get("profile") or {}
    if prof.get("name") != "docs":
        return False
    if source_changed is None:
        changed = attribution._changed_paths(root)
        source_changed = bool(changed) and review_mod._source_changed(
            ctx["config"], changed)
    if not source_changed:
        return False
    before = dict(prof)
    after = {"name": "normal", "source": "auto", "units": None,
             "entrypoints": None,
             "reason": "docs 예측이 빗나갔다 — 역할 소유 경로가 바뀌었다",
             "previous": {k: v for k, v in before.items()
                          if k not in ("previous", "triage_miss")},
             "reconfirmed_at": st.stamp()}
    for k in ("predicted", "signals", "applied"):
        if k in before:
            after[k] = before[k]
    after["triage_miss"] = _note_triage_miss(paths, s, before, after, where, cmd)
    s["profile"] = after
    s["contract"] = dict(s.get("contract") or {}, mode="contract",
                         reason="triage_miss")
    st.append_event(paths, "profile_reconfirmed", cmd=cmd, was="docs",
                    became="normal", units=None)
    st.save(paths, s)
    return True


def _reviewers_for(front, s):
    """이 페이즈가 이 런에서 부르는 리뷰어 코드. `review.unless` 가 끈다 (ADR-H044)."""
    review = (front or {}).get("review") or {}
    unless = review.get("unless")
    if unless and eval_condition(unless, s):
        return []
    return [r["code"] for r in review.get("reviewers") or []]


def _roles_for(front, ctx, s):
    """이 페이즈가 이 런에서 부르는 역할. `allow.unless` 가 끈다 (ADR-H044)."""
    allow = (front or {}).get("allow") or {}
    unless = allow.get("unless")
    if unless and eval_condition(unless, s):
        return []
    return list((ctx["config"].get("roles") or []) if ctx else [])


def _dispatched_roles(root, ctx, s, front):
    """03 이 이 런에 **실제로 부르는** 역할 id 와 걸러진 역할 (ADR-H057).

    `when_contract_section` 이 있는 역할은 계약의 그 절에 항목이 있을 때만 부른다.
    자진신고가 아니라 계약 파서가 정한다 — 03 의 `requires` 가 계약 파일을
    요구하므로 패킷을 낼 때 계약은 있고, 03 제출이 같은 계산을 다시 해 대조한다.
    반환: (ids, [{"id", "heading"}])
    """
    import contract as contract_mod

    roles = _roles_for(front, ctx, s)
    if not any(r.get("when_contract_section") for r in roles):
        return [r["id"] for r in roles], []
    parsed = {}
    full = Path(root) / resolve("${run.contract_file}", ctx)
    if ((s.get("contract") or {}).get("mode")) != "no_contract" and full.exists():
        parsed = contract_mod.parse(full.read_text(encoding="utf-8"), ctx["config"])
    sections = (ctx["config"].get("contract") or {}).get("sections") or {}
    ids, omitted = [], []
    for r in roles:
        key = r.get("when_contract_section")
        if key and not parsed.get(key):
            omitted.append({"id": r["id"], "heading": sections.get(key) or key})
        else:
            ids.append(r["id"])
    return ids, omitted


def _store_dispatch(root, ctx, s, front):
    """03 패킷을 내는 두 자리(`next`·전이)가 부른다. 귀속 사다리와 03 제출이 읽는다."""
    ids, _omitted = _dispatched_roles(root, ctx, s, front)
    s.setdefault("phases", {}).setdefault("03-implement", {})["dispatched_roles"] = ids


def _dispatch_render(root, ctx, s, front):
    """03 패킷 — 이 런에 부르는 역할과 걸러진 역할. 조건부 역할이 없으면 빈 문자열."""
    ids, omitted = _dispatched_roles(root, ctx, s, front)
    if not omitted and not any(r.get("when_contract_section")
                               for r in _roles_for(front, ctx, s)):
        return ""
    lines = ["## 이 런에 부르는 역할", "",
             "계약이 정한다 — 이 목록 전부를 한 메시지에서 부르고, `03_claims.json` 의 "
             "역할도 정확히 이 목록이어야 한다.", "",
             "- " + " · ".join("`%s`" % i for i in ids)]
    lines += ["- `%s` — 계약에 `%s` 항목이 없다, 미호출" % (o["id"], o["heading"])
              for o in omitted]
    return "\n".join(lines)


# ------------------------------------------------------------- 모델 등급

def _slot_of(key):
    """지시 키 → `config.models` 슬롯. 슬롯이 없는 지시(07 의 /code-review ·
    승격 판정)는 None — 그것은 Agent 기동이 아니라 등급을 줄 자리가 없다."""
    parts = (key or "").split(":")
    head = parts[0]
    if head == "00":
        return "triage"
    if head == "01":
        return "xv" if parts[-1] == "xv" else "plan"
    if head == "02":
        return "xv"
    if head in ("03", "04"):
        return "roles"
    if head == "05":
        # 05 수리 작성자는 04 수리와 같은 작성자다 (ADR-H064).
        return "roles" if len(parts) > 2 and parts[2] == "repair" else "reviewers"
    return None


def _model_for(config, key, profile):
    """`config.models[slot][profile]` 또는 `default`. 미선언이면 None."""
    models = config.get("models")
    slot = _slot_of(key)
    if not models or not slot:
        return None
    node = models.get(slot) or {}
    return node.get(profile) or node.get("default")


def _instruct(s, pid, keys, ctx):
    """계수(`count_instructions`)와 등급 기록을 **같은 자리**에서 한다.

    둘을 떼어 놓으면 세지 않은 지시에 등급이 붙거나 그 반대가 된다. 반환은
    `count_instructions` 와 같다.
    """
    config = ctx["config"] if ctx else {}
    profile = (s.get("profile") or {}).get("name") or "normal"
    for k in keys:
        if _slot_of(k):
            st.note_model_instruction(s, k, _model_for(config, k, profile))
    return st.count_instructions(s, pid, keys)


def _model_tiers_render(ctx, s, keys):
    """봉투가 지시 키마다 등급을 말한다 — 메인이 고르지 않는다."""
    keys = [k for k in keys or [] if _slot_of(k)]
    if not keys:
        return ""
    config = ctx["config"]
    if not config.get("models"):
        return ("## 모델 등급\n\n`config.models` 가 없다 — 등급을 지시하지 "
                "않는다. 메인 세션의 모델을 상속한다.")
    profile = (s.get("profile") or {}).get("name") or "normal"
    lines = ["## 모델 등급 (봉투가 정한다 — 네가 고르지 마라)", ""]
    for k in keys:
        tier = _model_for(config, k, profile)
        lines.append("- `%s` → model: `%s`" % (k, tier or "inherit"))
    lines += ["", "Agent 호출의 `model` 인자로 **그대로** 넘긴다. `inherit` 는 "
                  "인자를 주지 않는다. 실행기는 실제 모델을 검증하지 못한다 — "
                  "지시로만 남고, 그 사실이 `state.models.blind_spots` 에 있다."]
    return "\n".join(lines)


def _plan_05_review(root, paths, s, ctx):
    """05 진입 시 **누가 리뷰할지를 여기서 확정한다.**

    모델이 정하지 않는다. `when` glob 이 정하는 결정론이고, 모델이 정하면 같은
    diff 가 런마다 다른 리뷰를 받아 `escaped_05` 를 세는 것이 의미를 잃는다.
    """
    import precheck as pc
    import review as review_mod

    # **라우팅 전에 프로파일을 다시 센다.** 04 수리 중 계약 델타가 적용됐으면
    # 여기 오는 `profile` 이 낡은 값이고, 그 값이 곧 리뷰어 상한이다 (M34).
    refreshed = _refresh_profile(root, paths, s, ctx)
    # **라우팅은 `worktree` 다** (M40 · ADR-H028). 예산은 PR 전체를 재지만
    # 라우팅까지 넓히면 05 가 브랜치의 앞선 커밋(캘리브레이션·문서 등)까지
    # 리뷰어 매칭에 넣는다. 그것은 근거가 따로 필요한 별개 결정이다.
    changed = pc.changed_files(root, "worktree", ctx["config"])
    profile = (s.get("profile") or {}).get("name") or "normal"
    routed = review_mod.route(ctx["config"], changed, profile)
    if routed["source_changed"] and profile == "docs":
        # docs 예측인데 소스가 바뀌었다. 03 이 못 잡은 경로(예: 04 수리 중
        # 메인이 소스를 고쳤다)를 라우팅 직전에 한 번 더 묻는다 (ADR-H044).
        if _docs_lane_source_check(root, paths, s, ctx, "05-code-review",
                                   source_changed=True, cmd="next"):
            profile = "normal"
            routed = review_mod.route(ctx["config"], changed, profile)
    node = s.setdefault("phases", {}).setdefault("05-code-review", {})
    # **계획된 리뷰어는 줄지 않는다.** `next --phase 05` 는 여러 번 불릴 수
    # 있고 그때마다 변경 집합을 다시 읽는다. 줄어든 집합으로 덮으면 계획이
    # 조용히 작아지고 `escaped_05` 를 세는 것이 뜻을 잃는다 — `review05` 가
    # "런 안에서 좋아지지 않는다" 를 지키는 것과 같은 규율이다.
    kept = [c for c in (node.get("planned") or [])
            if c not in [r["code"] for r in routed["reviewers"]]]
    node["planned"] = [r["code"] for r in routed["reviewers"]] + kept
    node["routing"] = routed
    # **자진신고한 위험과 라우팅을 대조한다 — 관측만** (ADR-H067). `next` 가
    # 여러 번 불려도 원장에는 한 번만 남긴다. 등급은 치르지 않는다.
    if "risk_undeclared" not in node:
        risk = ((s.get("phases") or {}).get("01-plan") or {}).get("risk")
        node["risk_undeclared"] = review_mod.undeclared_risk(
            ctx["config"], routed, risk)
        if node["risk_undeclared"]:
            st.append_event(paths, "risk_undeclared", cmd="next",
                            phase="05-code-review",
                            reviewers=node["risk_undeclared"], declared=risk)
    node["profile_reconfirmed"] = refreshed if refreshed.get("changed") else None
    node["contract_dropped"] = refreshed.get("dropped") or []
    node["mode"] = review_mod.mode(ctx["config"],
                                   pc._changed_lines(root, changed))
    # **리뷰 범위도 레인이 정한다** (ADR-H059). FR-007 의 동시성 결함은 05 가
    # diff 만 봐서 놓쳤다 — 기존 `transition()` 과의 상호작용은 누구의
    # 체크리스트에도 없었다. `normal` 은 계약이 참조하는 기존 파일까지 본다.
    node["depth"] = (((ctx["config"].get("review") or {}).get("depth") or {})
                     .get(profile) or "diff")
    # **인라인 상한은 기계가 정한다** (ADR-H042). `review.inline_max` 는
    # 정의만 있고 아무도 안 읽어 큰 diff 가 리뷰어 수만큼 인라인됐다.
    node["inline"] = review_mod.inline_budget(ctx["config"],
                                              _diff_text(root, changed))
    if not node["planned"]:
        # **커밋만 있고 워킹트리가 깨끗하면 라우팅 실패가 아니라 절차 오류다**
        # (ADR-H046). 파일럿 40dc 가 05 통과 전에 커밋해 여기서 0명이 되고
        # `review05:failed` 가 append-only 로 박혔다 — 되돌려 4/4 리뷰를
        # 정상 수행했는데도 gap 은 남았다. 라우팅 scope 는 그대로 worktree 다
        # (ADR-H028); 다만 `pr` scope 에 변경이 있으면 failed 를 쓰지 않고
        # 호출자(`run_next`)가 exit 3 을 낸다.
        # **커밋에만 있는 변경** = pr scope − worktree scope. 워킹트리의 미커밋
        # 파일(리뷰어 glob 밖이라 라우팅 0 이 된 것)은 여기서 상쇄된다.
        committed = sorted(set(pc.changed_files(root, "pr", ctx["config"]))
                           - set(changed))
        if committed:
            node["routing_refused"] = {"pr_scope_changed": committed}
            return node
        # **여기서 확정하지 않으면 아무도 확정하지 않는다.** 리뷰어가 0명이면
        # 제출도 0건이고 `_judge_05` 가 아예 안 불린다 — 05 가 조용히 지나간다.
        # 봉투는 이 사실을 이미 산문으로 말하고 있었고, 그것을 쓰는 코드가
        # 없다는 것이 G-4 의 절반이었다.
        _write_review05(s, node, planned=[], ok=0, merged=[], slot={})
    node.pop("routing_refused", None)
    # **지시 시점의 지문을 라운드에 남긴다** (ADR-H046). `record` 가 이것과
    # 현재 지문을 대조해 "리뷰 뒤에 코드를 고치고 나서 record" 하는 순서를
    # 막는다 — 그 순서는 `review_repair` 를 헛되이 태워 이미 해소된 지적으로
    # 에스컬레이션을 냈다 (파일럿 세션 a3decd93).
    r = ((s.get("counters") or {}).get("review_repair") or {}).get("used", 0) + 1
    node.setdefault("dispatched_fp", {})[str(r)] = st.fingerprint(root, ctx["config"])
    return node


def _diff_text(root, changed):
    """워크트리 diff 원문. 추적분은 `git diff HEAD`, 새 파일은 내용 그대로."""
    r = harness._git(root, "diff", "HEAD", "--", *changed) if changed else None
    text = r.stdout if (r is not None and r.returncode == 0) else ""
    tracked = harness._git(root, "ls-files", "--", *changed) if changed else None
    known = set()
    if tracked is not None and tracked.returncode == 0:
        known = {l.strip().replace("\\", "/") for l in tracked.stdout.splitlines()}
    for rel in changed:
        if rel in known:
            continue
        try:
            text += (Path(root) / rel).read_text(encoding="utf-8",
                                                 errors="replace")
        except OSError:
            continue
    return text


def _dedup_ordered(items):
    """문자열 **정확 일치**로 접고 **첫 등장 순서를 지킨다** (M53).

    `reviewers_failed` 의 집합 합집합과 같은 규율인데(M43) 정렬하지 않는다 —
    리뷰어 코드는 이름이라 정렬해도 뜻이 안 바뀌지만 이것은 사람이 읽는
    문장이고, 순서가 "누가 먼저 무엇을 못 봤나" 를 담는다. 다듬지도 않는다
    (strip·casefold 없음): 정규화는 서로 다른 요청을 조용히 합치는
    휴리스틱이고, `finding_key` 가 제목을 정확히 보는 것과 같은 보수성이다.
    """
    out = []
    for it in items:
        if it not in out:
            out.append(it)
    return out


def _write_review05(s, node, planned, ok, merged, slot, round_=None):
    """`review05` 의 단일 출처. **status 는 런 안에서 좋아지지 않는다.**

    `st.demote` 가 등급의 단일 출처인 것과 같은 규율이다 — 대입이 흩어지면
    되돌리는 경로가 조용히 생긴다 (ADR-H015).
    """
    import review as review_mod

    round_status = node.setdefault("round_status", {})
    this = review_mod.status(len(planned), ok)
    if round_ is not None:
        round_status[str(round_)] = this
    status = review_mod.worst_status(list(round_status.values()) or [this])

    # **실적도 라운드를 가로질러 보존한다** (M43). 예전에는 `status` 만
    # `round_status` 로 최악을 지키고 `planned`/`ok` 는 매 라운드 덮였다.
    # 그래서 1회차에 셋이 돌아도 델타 라운드(1명)가 끝나면 `1/1` 로 적혀
    # 보고서와 승인 프롬프트가 리뷰 실적을 축소했다. 그 필드는 "리뷰가
    # 수행됐는가" 를 findings 개수와 분리하려고 만든 신호인데, 분모가
    # 마지막 라운드로 줄면 그 뜻을 잃는다.
    failed_now = sorted(c for c in planned
                        if (slot.get(c) or {}).get("keys") is None
                        and c in slot)
    rounds = node.setdefault("round_reviewers", {})
    if round_ is not None:
        rounds[str(round_)] = {"planned": len(planned), "ok": ok,
                               "failed": failed_now}
    seen = list(rounds.values()) or [{"planned": len(planned), "ok": ok,
                                      "failed": failed_now}]

    # **리뷰어가 남긴 신호도 라운드를 가로질러 보존한다** (M53). 바로 위와
    # 같은 이유이고 **원인도 같은 블록에 있었다** — 아래 셋이 `slot`(현재
    # 라운드 하나)만 읽어 델타 라운드의 1명이 덮었다. P7 에서 1회차 리뷰어
    # 셋이 쌓은 `need_more_context` 5건이 2회차 `arch` 의 빈 배열에 **0** 이
    # 됐다. **리뷰어가 "확인 못 했다"고 말한 것이 증발한다** — 단조성 검사가
    # findings 에는 걸리는데 이 셋에는 안 걸린다.
    #
    # 접는 원천은 `node["rounds"]` 다. **파생 사본을 새로 쌓지 않는다**
    # (M31 · ADR-H022). `round_reviewers` 를 따로 만든 것은 `planned` 가
    # 인자라 슬롯에서 유도할 수 없었기 때문이고, 이 셋은 제출 자체에 있어
    # 원본에서 그대로 나온다. `slot` 은 `node["rounds"][str(round_)]` 와
    # **같은 객체**이므로 이중 계수가 아니고, `rounds` 가 없을 때만(리뷰어
    # 0명 경로, cli.py 의 `_write_review05(..., slot={})`) `slot` 으로
    # 낙하한다.
    #
    # 접는 방식이 셋 다 다르다 — 근거는 team-spec §3.5 의 표에 있다.
    subs = [v for r in (node.get("rounds") or {}).values() for v in r.values()]
    subs = subs or list(slot.values())

    prev = s.get("review05") or {}
    s["review05"] = {
        "status": status,
        "round_status": dict(round_status),
        # `max` 다. `status` 가 "런 안에서 좋아지지 않는다" 이므로 실적은
        # 대칭으로 "런 안에서 줄지 않는다" 여야 한다. 그리고 **파생 수 하나로
        # 덮지 않고 `rounds` 를 통째로 남긴다** — M31 이 회차 기록을 정수로
        # 덮은 손실이었다 (ADR-H022).
        "rounds": dict(rounds),
        "reviewers_planned": max(r["planned"] for r in seen),
        "reviewers_ok": max(r["ok"] for r in seen),
        "reviewers_failed": sorted({c for r in seen for c in r["failed"]}),
        "mode": node.get("mode") or "fanout",
        # 지시된 범위다 — 리뷰어가 실제로 참조 파일을 읽었는지는 실행기가 못 본다.
        "depth": node.get("depth"),
        "major": sum(1 for f in merged if f.get("severity") in verdict.BLOCKING),
        # **0 은 신호다** (ADR-H050). 07 이 "05 가 ok 이고 Major 가 없다" 만 보고
        # 생략하면 리뷰어 넷이 전부 0건을 낸 런(파일럿 9729 · 3305)이 자동
        # 게이트 말고는 아무 눈도 안 받는다. 그래서 총계를 따로 남긴다.
        "findings_total": len(merged),
        "need_more_context": _dedup_ordered(
            n for v in subs for n in (v.get("need_more_context") or [])),
        "dropped_by_enforcement": sum(v.get("dropped_by_enforcement") or 0
                                      for v in subs),
        "truncated": any(v.get("truncated") for v in subs),
    }
    if status != "ok":
        st.demote(s, st.GRADES[1], "review05:%s" % status)
    return prev


def _excluded_render(root):
    """"검토 제외" 목록. **기계 강제 규칙이 늘수록 05 가 자동으로 싸지고 좁아진다** —

    규칙 승격의 복리가 실현되는 지점이고, 그래서 이 목록이 길어지는 것이 좋은
    신호다. 드롭한 건수는 `dropped_by_enforcement` 로 센다 (조용히 버리지 않는다).
    """
    import ledger

    codes = ledger.excluded_categories(root)
    if not codes:
        return ("## 검토 제외\n\n(없다) — 아직 기계로 막는 규칙이 없다. "
                "원장이 쌓이면 여기가 채워지고 05 가 그만큼 좁아진다.")
    return ("## 검토 제외 — 리뷰어 프롬프트에 그대로 싣는다\n\n"
            "아래는 이미 기계가 막는다. 리뷰어가 지적하면 `record` 가 드롭하되 "
            "`dropped_by_enforcement` 로 **센다** — 조용히 버리지 않는다.\n\n"
            + "\n".join("- `%s`" % c for c in codes))


def _vocabulary_render(root):
    """쓸 수 있는 `category` 전부. **봉투가 규약을 먼저 말한다** (M46 · M20).

    이 절이 없으면 리뷰어는 어휘를 모른 채 제출하고, 틀리면 exit 8 을 받는다 —
    "리뷰어가 모르면 exit 8 이고 메인이 사후에 맞추는 것이 유일한 길이 된다"
    가 M20 이 고친 바로 그 모양이다.
    """
    import ledger

    cats = ledger.categories(root)
    if not cats:
        return ("## 원장 어휘\n\n**어휘를 읽지 못했다** (`%s`). 이 상태에서는 "
                "어떤 `category` 도 원장에 들어가지 못한다 — 리뷰어의 문제가 "
                "아니라 설정의 문제다. `doctor` 를 먼저 돌린다."
                % ledger.TAXONOMY_REL)
    usable = sorted(c for c, v in cats.items()
                    if (v.get("status") or "") != "retired")
    lines = ["## 원장 어휘 — `category` 는 이 안에서 고른다", ""]
    lines += ["- `%s`" % c for c in usable]
    lines += ["",
              "밖의 코드를 **지어내지 마라** — 제출이 exit 8 로 되돌아온다. "
              "맞는 것이 없으면 `OTHER` 로 내고 무엇이 없는지를 evidence 에 적는다. "
              "어휘를 늘리는 것은 승격의 일이지 제출의 일이 아니다."]
    lines += _slug_vocabulary_lines(cats, usable)
    return "\n".join(lines)


def _slug_vocabulary_lines(cats, usable):
    """`rule_slug` 어휘 (ADR-H035). **어휘를 선언한 카테고리만 필수다.**

    승격은 "무엇이 반복되는 유형인가" 를 묻는데 자유 서술 제목은 매번 달라
    규칙이 아니다. 그래서 축의 값을 통제 어휘에서 고르게 한다.

    `note` 를 함께 싣는 것이 이 절의 요점이다 — 한 카테고리를 여러 스킬이
    가로질러 내므로(원장 실측: `DOC_CODE_DRIFT` 는 arch·data·sec 가 냈고
    docs 는 0건), 이름만 나열하면 리뷰어가 뜻을 모른 채 고른다.

    **어휘가 없는 카테고리는 침묵으로 두지 않는다** — 안 적으면 "여기도
    필수인가" 가 리뷰어의 추측이 되고, 추측은 exit 8 아니면 억지 슬러그다.
    """
    with_vocab = [(c, cats[c]["slugs"]) for c in usable if cats[c].get("slugs")]
    if not with_vocab:
        return []
    out = ["", "## 규칙 슬러그 — 승격의 축이다", "",
           "아래 카테고리로 낼 때는 `rule_slug` 를 **함께** 적는다. 안 적거나 "
           "어휘 밖을 적으면 제출이 exit 8 로 되돌아온다. 맞는 것이 없으면 "
           "`category: OTHER` 로 내고 무엇이 없는지를 evidence 에 적어라 — "
           "**어휘를 늘리는 것은 승격의 일이지 제출의 일이 아니다.**", ""]
    for code, slugs in with_vocab:
        out.append("- `%s`" % code)
        out += ["  - `%s` — %s" % (s.get("slug"), s.get("note"))
                for s in slugs]
    bare = [c for c in usable if not cats[c].get("slugs")]
    if bare:
        out += ["",
                "나머지(%s)는 슬러그를 **요구하지 않는다** — 아직 어휘가 "
                "선언되지 않은 카테고리이고, 없는 것을 지어내면 무관한 지적이 "
                "한 버킷에 뭉친다." % " · ".join("`%s`" % c for c in bare)]
    return out

def _contract_drift_lines(node, s):
    """계약이 바뀌어 프로파일이 다시 정해졌다는 것과, 파서가 흘린 줄.

    둘 다 **조용하면 안 되는 사실**이다. 프로파일은 리뷰어 상한을 정하고,
    흘린 줄은 그 프로파일과 스코프 선택을 동시에 빗나가게 한다 (M34 · D-2).
    """
    out = []
    re_ = node.get("profile_reconfirmed")
    if re_:
        prev = re_.get("previous") or {}
        prof = s.get("profile") or {}
        out += ["**계약이 바뀌어 프로파일을 다시 셌다** — `%s`(유닛 %s) → "
                "`%s`(유닛 %s). 리뷰어 상한이 그만큼 달라진다."
                % (prev.get("name"), prev.get("units"),
                   prof.get("name"), prof.get("units")), ""]
    dropped = node.get("contract_dropped") or []
    if dropped:
        out += ["**계약의 %d줄이 유닛으로 세어지지 않았다** — 파서는 "
                "`컨테이너 · 심볼` 쌍을 요구한다. 이 줄들은 프로파일 판정에도 "
                "스코프 선택에도 들어가지 않는다:" % len(dropped), ""]
        out += ["- `%s` — %s" % (d.get("raw"), d.get("reason"))
                for d in dropped[:5]]
        out += [""]
    return out


def _review_render(s):
    """봉투가 **누가 리뷰하는지와 무엇이 빠졌는지**를 말한다."""
    node = (s.get("phases") or {}).get("05-code-review") or {}
    routed = node.get("routing")
    if not routed:
        return ""
    lines = ["## 리뷰어 라우팅 (결정론 — 네가 정하지 않는다)", ""]
    lines += _contract_drift_lines(node, s)
    if not routed["reviewers"]:
        lines += ["**매칭된 리뷰어가 0개다.** 그러면 `review05.status` 는 "
                  "`failed` 이고 등급이 `PASS_WITH_GAPS` 로 떨어진다 — "
                  "아무도 안 부른 것은 통과가 아니라 미수행이다.",
                  "",
                  "변경 경로가 `config.reviewers[].when` 어디에도 걸리지 않았다. "
                  "라우팅 결함일 수 있으니 보고서에 남긴다."]
        return "\n".join(lines)
    lines.append("모드: **%s** (%s)"
                 % (node.get("mode"),
                    "단일 에이전트가 체크리스트를 순차 적용한다"
                    if node.get("mode") == "merged" else
                    "관점별 병렬 fan-out"))
    depth = node.get("depth") or "diff"
    if depth == "diff+refs":
        lines.append("리뷰 범위: **diff+refs** — 계약 `## 유닛` 이 참조하는 **기존** "
                     "파일을 리뷰어 패킷에 경로로 넣어라. diff 밖 상호작용(낙관적 "
                     "잠금 · 상태 가드 · 기존 전이 함수)을 보는 것이 이 범위의 "
                     "목적이다 — 05 가 놓치고 07 이 잡은 것이 그 자리였다 (FR-007).")
    else:
        lines.append("리뷰 범위: **%s** — 인라인 diff · 계약 · `05_trace.json` 만. "
                     "그 밖의 파일은 패킷에 넣지 않는다." % depth)
    lines.append("")
    if node.get("mode") == "merged":
        # **M37.** 봉투가 `merged` 만 적으면 "제출도 하나" 로 읽힌다. 기계는
        # 그렇지 않다 — `_planned_guard` 가 라우팅에 없는 제출자를 exit 8 로
        # 되돌리고, `merged` 는 라우팅된 코드가 아니다. P5 가 제출 1회를
        # 여기서 잃었다.
        lines += ["**`merged` 는 실행 방식이지 제출 형태가 아니다.** 한 "
                  "에이전트가 관점을 순차로 적용하되 **제출은 라우팅된 코드 "
                  "수만큼 그대로 갈라진다** — `05_review_{code}.json` 과 "
                  "`.raw.md` 한 쌍씩이다. `record` 는 `--reviewer merged` 를 "
                  "받지 않는다:", ""]
        lines += ["```"]
        lines += ["python scripts/pipeline/cli.py record --phase 05 "
                  "--file <...>/05_review_%s.json --reviewer %s --round 1"
                  % (r["code"], r["code"]) for r in routed["reviewers"]]
        lines += ["```", ""]
    for r in routed["reviewers"]:
        lines.append("- `%s` → `.claude/skills/%s/SKILL.md` (매칭 %d개)"
                     % (r["code"], r["skill"], r.get("matched_count", 0)))
    if routed.get("dropped"):
        lines += ["", "**상한으로 빠진 리뷰어**: %s — 조용히 사라진 것이 아니라 "
                      "예산 때문이고, 보고서에 남는다."
                  % ", ".join("`%s`" % d["code"] for d in routed["dropped"])]
    lines += ["", "프롬프트 첫 줄은 **스킬 파일을 읽으라는 지시**다. "
                  "본문을 복사하지 마라 — 리뷰어 수만큼 고정비가 곱해진다."]
    inline = node.get("inline") or {}
    if inline and not inline.get("inline"):
        lines += ["", "**diff 를 인라인하지 마라 — 경로로 전달한다.** 인라인 "
                      "상한(`review.inline_max`)을 넘었다: %s. 리뷰어 패킷의 "
                      "`## 변경` 절에 diff 대신 변경 파일 경로 목록을 싣고, "
                      "리뷰어가 그 파일만 읽게 한다. 이 사실은 원장에 남는다."
                  % " · ".join(inline.get("over") or [])]
    return "\n".join(lines)


def render_packet(root, phase, ctx, s, checks=None):
    front, body = phase["front"], phase["body"]
    pid = front["id"]
    parts = [render_header(ctx["config"], s), ""]
    parts.append(_section(body, "## 목적"))
    parts.append(_section(body, "## 절차"))
    if pid == "00-triage":
        parts.append(_triage_render(ctx, s))
    if pid in ("00-triage", "01-plan"):
        parts.append(_deferred_render(s))
    if pid == "01-plan" and not _reviewers_for(front, s):
        parts.append(
            "## 리뷰어 — 0명 (%s 레인)\n\n이 런은 플랜 리뷰어를 부르지 않는다. "
            "인용 검증·커버리지·드리프트의 기계 검사가 이 페이즈의 전부이고, "
            "플랜 제출이 통과하면 1라운드에 닫힌다. 리뷰어를 부르지 마라 — "
            "라우팅 밖의 제출은 받지 않는다."
            % ((s.get("profile") or {}).get("name")))
    role_tpl = _section(body, "## 역할 프롬프트 템플릿")
    if role_tpl and pid == "03-implement" and not _roles_for(front, ctx, s):
        parts.append(
            "## 역할 — 0명 (%s 레인)\n\n이 런은 역할 에이전트를 부르지 않고 "
            "계약도 쓰지 않는다 (`no_contract`). **네가 직접** 문서를 고치고 "
            "`03_claims.json` 을 `{\"schema\":1,\"roles\":[]}` 로 낸다. 역할 소유 "
            "경로(소스)를 건드리면 제출이 exit 3 으로 되돌아온다 — 그때는 "
            "예측이 빗나간 것이고 계약을 쓰고 역할 패킷을 받는다."
            % ((s.get("profile") or {}).get("name")))
    elif role_tpl:
        parts.append(role_tpl)
        if pid == "03-implement":
            parts.append(_dispatch_render(root, ctx, s, front))
            parts.append(_tests_required_render(root, ctx, s))
    parts.append(_section(body, "## 제출 형식"))
    parts.append(_section(body, "## 금지"))

    produces = [resolve(p.get("path"), ctx) for p in front.get("produces") or []]
    if produces:
        parts.append("## 쓸 파일\n\n" +
                     "\n".join("- `%s`" % p for p in produces))
    xv = _cross_verify_render(ctx["config"], s, front)
    if xv:
        parts.append(xv)
    if pid == "05-code-review":
        rv_render = _review_render(s)
        if rv_render:
            parts.append(rv_render)
        parts.append(_vocabulary_render(root))
        parts.append(_excluded_render(root))
    if pid == "07-pr-review":
        # 07 이 05 와 같은 결함에 다른 이름을 붙이면 새 것으로 세어진다.
        # 목록을 봉투가 직접 준다 — 모델이 재구성하면 그 재구성이 곧 결함이다 (M48).
        parts.append(_open_from_05_render(_open_from_05(s)))
        # 07 도 어휘를 대조받는 생산자다 (ADR-H035). 봉투가 먼저 말하지
        # 않으면 필수를 모른 채 제출하고 exit 8 을 받는다 — M20 이 고친 모양.
        parts.append(_vocabulary_render(root))
    tiers = _model_tiers_render(ctx, s, _instruction_keys(s, pid, ctx, front))
    if tiers:
        parts.append(tiers)
    warns = [c for c in (checks or []) if c.get("warn")]
    if warns:
        parts.append("## 경고\n\n" + "\n".join("- %s" % c["message"] for c in warns))

    if (front.get("gate") or {}).get("runner") == "adapter" and pid == "04-gate":
        cmd = "python scripts/pipeline/cli.py gate --phase 04 --run-id %s" % s["run_id"]
    else:
        cmd = ("python scripts/pipeline/cli.py record --phase %s --file <산출물> "
               "--run-id %s" % (pid.split("-")[0], s["run_id"]))
    return "\n\n".join(p for p in parts if p), cmd


def _tests_required_render(root, ctx, s):
    """03 패킷 — 계약에서 **기계로** 뽑은, 게이트가 세는 테스트 목록 (ADR-H058).

    게이트가 계약 파서로 세는데 워커는 산문 지시문만 받으면, 워커가 같은 목록을
    스스로 유도해야 한다. 같은 파서의 결과를 그대로 준다. 03 의 `requires` 가
    계약 파일을 요구하므로 이 패킷이 렌더될 때 계약은 이미 있다.
    """
    import contract as contract_mod
    import trace_contract

    if ((s.get("contract") or {}).get("mode")) == "no_contract":
        return ""
    full = Path(root) / resolve("${run.contract_file}", ctx)
    if not full.exists():
        return ""
    parsed = contract_mod.parse(full.read_text(encoding="utf-8"), ctx["config"])
    eps, errors = parsed.get("entrypoints") or [], parsed.get("errors") or []
    journeys = parsed.get("journeys") or []
    if not eps and not errors and not journeys:
        return ""
    test_role = trace_contract._test_role(ctx["config"])
    lines = ["## 게이트가 세는 테스트 — `%s` 역할" % test_role, "",
             "계약에서 기계로 뽑은 목록이다. 03 제출과 05 계약 대조가 **같은 목록**을 "
             "센다 — 빠지면 03 제출이 거부된다.", ""]
    for ep in eps:
        need = "성공 경로"
        if ep.get("tags"):
            need += " + **거부 경로**(%s)" % ", ".join("`%s`" % t for t in ep["tags"])
        lines.append("- 진입점 `%s` — 그 진입점 파일 옆의 같은 이름 테스트, 또는 그 "
                     "파일을 import 하는 테스트에 %s" % (ep.get("raw"), need))
    for name in errors:
        lines.append("- 오류 어휘 `%s` — 이 상수를 단언하는 테스트" % name)
    for j in journeys:
        lines.append("- 여정 `%s` — `%s` 에 이 슬러그를 최상위 describe 문자열이나 "
                     "`export const` 이름으로" % (j["symbol"], j["container"]))
    return "\n".join(lines)


def _e2e_absent_reason(adapter):
    """어댑터에 e2e 가 없으면 그 이유 문장, 있으면 None. 거부와 힌트가 같이 쓴다."""
    state = adapters.stage_state(adapter, "e2e")
    if state == "present":
        return None
    if state == "na":
        return "이 스택은 e2e 가 해당 없음이라 여정을 적을 수 없다"
    return "어댑터에 e2e 가 없다 — 도입은 ADR 로 한다"


def _journey_hint(root, s):
    """계약을 쓰라는 봉투에 붙는 한 줄 (ADR-H058 추기). 해당 없으면 빈 문자열.

    메인은 03 패킷보다 **먼저** 계약을 쓴다 — 03 `requires` 가 계약 파일이다.
    그 시점에 어댑터의 e2e 여부를 봉투가 말하지 않으면 메인이 추론해야 하고,
    틀리면 `_contract_precheck_03` 에 튕긴다. e2e 가 있으면 말하지 않는다 —
    절차 문장이 이미 조건을 말하고, 되풀이하면 여정을 과하게 쓰게 부추긴다.
    """
    if ((s.get("contract") or {}).get("mode")) == "no_contract":
        return ""
    _config, adapter, _cal = adapters.load(root)
    why = _e2e_absent_reason(adapter)
    if why is None:
        return ""
    return "\n\n%s. 계약의 `## 여정` 은 \"없음\" 으로 둔다." % why


def _contract_precheck_refuse(root, paths, s, ctx, cmd):
    """`next`·전이가 03 패킷을 내기 전의 거부 봉투. 통과면 None — 지시를 세기 전이다."""
    refused = _contract_precheck_03(root, ctx, s)
    if refused is None:
        return None
    st.append_event(paths, "check_fail", cmd=cmd, phase="03-implement",
                    **refused["data"])
    st.save(paths, s)
    return st.envelope(cmd, False, 8, s, refused["data"], refused["render"],
                       "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])


def _contract_precheck_03(root, ctx, s):
    """03 패킷을 내기 **전에** 계약을 본다 (ADR-H058 추기). 문제가 없으면 None.

    러너 없는 여정을 워커가 스펙까지 쓴 뒤에 거부하면 그 스펙이 test 소유·claimed
    로 `clean_ownership` 을 지나 PR 에 조용히 실린다. 그래서 디스패치 전에 막는다.
    03 의 `requires` 가 계약 파일을 요구하므로 여기 올 때 계약은 있다.
    반환: {"render": str, "data": dict}
    """
    import contract as contract_mod

    if ((s.get("contract") or {}).get("mode")) == "no_contract":
        return None
    full = Path(root) / resolve("${run.contract_file}", ctx)
    if not full.exists():
        return None
    parsed = contract_mod.parse(full.read_text(encoding="utf-8"), ctx["config"])
    if not parsed.get("journeys") and not parsed.get("journeys_dropped"):
        return None
    _config, adapter, _cal = adapters.load(root)
    state = adapters.stage_state(adapter, "e2e")
    why = _e2e_absent_reason(adapter)
    if why is not None:
        files = harness.list_files_with_untracked(root)
        written = [src for src in (contract_mod._source_for_container(
                       j.get("container"), files) for j in parsed["journeys"]) if src]
        tail = ("\n\n이미 쓴 스펙은 지운다 — 러너 없는 스펙은 test 소유라 소유 "
                "검사를 지나 PR 에 조용히 실린다:\n%s"
                % "\n".join("- `%s`" % w for w in written)) if written else ""
        return {"data": {"journeys": "e2e_" + state, "written": written},
                "render": "## 러너 없는 여정\n\n%s. 계약의 `## 여정` 을 \"없음\" 으로 "
                          "되돌리고 `next` 를 다시 친다.%s" % (why, tail)}
    problems = contract_mod.journey_problems(parsed)
    if problems:
        return {"data": {"journeys": "invalid", "problems": problems},
                "render": "## 여정을 디스패치할 수 없다\n\n%s\n\n단계는 「진입점」 절의 "
                          "`METHOD /path` 를 글자 그대로 `→` 로 잇는다."
                          % "\n".join("- %s" % p for p in problems)}
    return None


def _deferred_render(s):
    """00·01 패킷 — 요청 경로와 겹치는 이월 미해결 (ADR-H051). 없으면 그렇게 말한다."""
    node = (s.get("phases") or {}).get("00-triage") or {}
    ov = node.get("deferred_overlap")
    if ov is None:
        return ""
    if not ov.get("count"):
        return ("## 이월 미해결 — 요청 경로와 겹치는 deferred 0건\n\n"
                "원장의 열린 `deferred` 중 이 요청의 경로에 걸린 것이 없다.")
    return ("## 이월 미해결 — 요청 경로와 겹치는 deferred %d건\n\n"
            "경로: %s\n\n`docs/harness/pipeline/ledger/deferred.md` 의 해당 절을 "
            "플랜의 입력으로 읽는다. 앞선 런이 미룬 것이고, 이번 런이 같은 자리를 "
            "건드린다면 고치거나 왜 또 미루는지 적는다."
            % (ov["count"], ", ".join("`%s`" % p for p in ov.get("paths") or [])))


def _triage_render(ctx, s):
    """00 패킷 — 기계 신호가 무엇을 봤고 왜 못 정했는지, 원문이 어디 있는지."""
    node = (s.get("phases") or {}).get("00-triage") or {}
    sig = node.get("signals") or {}
    req = resolve("${run.dir}/00_original_request.md", ctx)
    lines = ["## 기계 신호 (판정에 못 미쳤다 — 저가 모델 1회가 예측한다)", ""]
    lines.append("- 요청 글자 수: %s" % sig.get("request_chars"))
    lines.append("- 역할 소유 경로: %s" % (", ".join(
        "`%s`" % p for p in sig.get("paths_role_owned") or []) or "(없음)"))
    lines.append("- docs 경로: %s" % (", ".join(
        "`%s`" % p for p in sig.get("paths_docs") or []) or "(없음)"))
    lines.append("- 미해결 경로: %s" % (", ".join(
        "`%s`" % p for p in sig.get("paths_unresolved") or []) or "(없음)"))
    lines += ["", "트리아지 에이전트에게 **요청 원문만** 준다: `%s`. 리포 탐색을 "
                  "허용하지 마라. 위 신호를 함께 실어도 된다 — 원문 밖의 것은 "
                  "그것뿐이다." % req]
    return "\n".join(lines)


def _cross_verify_reviewer(front):
    """페이즈의 리뷰어 중 교차검증기의 `code`. 없으면 None.

    "누가 교차검증기인가" 를 판정하는 자리는 **여기 하나**다. 두 곳에서 따로
    판정하면 갈라지는 날이 오고, 그날 폴백 기록이 조용히 빠진다.
    """
    for r in ((front.get("review") or {}).get("reviewers") or []):
        if r.get("kind") == "cross_verify":
            return r.get("code")
    return None


def _is_cross_verifier(phase_item, reviewer):
    return _cross_verify_reviewer(phase_item["front"]) == reviewer


def _cross_verify_render(config, s, front):
    """교차검증기가 누구인지 봉투가 말한다.

    페이즈 파일 본문은 `${...}` 가 풀리지 않으므로(`_section` 이 원문을 그대로
    싣는다) 이 이름은 여기서만 나올 수 있다. **코어에 도구 이름을 박지 않는다** —
    config 를 읽을 뿐이고, 그래서 스택·도구를 바꿔도 코어는 그대로다.
    """
    if _cross_verify_reviewer(front) is None or not _reviewers_for(front, s):
        return ""
    cv = config.get("cross_verify") or {}
    node = s.get("cross_verify") or {}
    mode = node.get("mode") or "skipped"

    # **일시 실패는 부재가 아니다.** primary 가 선언돼 있는데 직전 회차가
    # 실패로 폴백했다면 이번 회차는 다시 시도한다 — 상류 과부하는 대개 한
    # 라운드보다 먼저 끝난다. 예전에는 이 분기가 없어 한 번 폴백하면 그 런
    # 내내 폴백이 굳었다 (P3 의 다섯 라운드).
    if node.get("last_primary_error") and cv.get("primary"):
        return ("## 교차검증\n\n직전 회차는 외부 관측기 `%s` 가 **실패**해 "
                "폴백 `%s` 로 돌았다 — %s\n\n**이번 회차는 primary 를 다시 "
                "시도한다.** 일시 실패는 부재가 아니고, 상류 과부하는 대개 한 "
                "라운드보다 먼저 끝난다. 또 실패하면 폴백으로 가되 제출에 "
                "`primary_error` 를 다시 싣는다 — 그래야 다음 회차가 같은 "
                "판단을 할 수 있다."
                % (cv.get("primary"), cv.get("fallback"),
                   node["last_primary_error"]))

    if mode == "primary":
        return ("## 교차검증\n\n외부 관측기 `%s` 를 쓴다. 이것이 있으면 "
                "**1라운드 수렴이 열린다** — 둘 다 폴백이 아니고 차단 심각도"
                "(`converge.blocking_severities`)가 0건이면 그 회차에서 끝난다. "
                "그 아래 심각도는 기록되되 라운드를 강제하지 않는다."
                % cv.get("primary"))
    if mode == "fallback":
        return ("## 교차검증\n\n외부 관측기가 없어 폴백 `%s` 를 쓴다. "
                "**폴백이 섞이면 1라운드 수렴을 허용하지 않는다** — 독립 관측 "
                "둘이라는 전제가 약해지기 때문이고, 최소 2라운드를 돈다."
                % cv.get("fallback"))
    return ("## 교차검증\n\n교차검증기가 없다. 02 는 스킵되고 등급이 "
            "`PASS_WITH_GAPS` 로 강등된다 — 조용히 통과가 아니다.")


def render_header(config, s):
    """300자 이내. 넘으면 INV → 소유권 → 예산 → 금지 순으로 자른다."""
    bits = []
    counters = s.get("counters") or {}
    used = ", ".join("%s %d/%s" % (k, v.get("used", 0), v.get("max"))
                     for k, v in sorted(counters.items()))
    bits.append("런 `%s` · 페이즈 **%s**" % (s.get("run_id"), s.get("phase")))
    roles = ", ".join("%s→%s" % (r["id"], r["agent"]) for r in config.get("roles") or [])
    if roles:
        bits.append("역할: %s" % roles)
    if used:
        bits.append("카운터: %s" % used)
    budget = (s.get("budget") or {}).get("model_calls") or {}
    if budget.get("max"):
        bits.append("모델 호출 %s/%s(지시 기준)"
                    % (budget.get("total"), budget["max"]))
    out = " · ".join(bits)
    while len(out) > 300 and len(bits) > 1:
        bits.pop()
        out = " · ".join(bits)
    return out


def _section(body, heading):
    """절 하나를 통째로. **경계 판정은 `_headings` 하나뿐이다** (M45)."""
    lines = body.splitlines()
    heads = _headings(lines)
    start = next((i for i, l in heads if l.strip() == heading), None)
    if start is None:
        return ""
    end = next((i for i, _l in heads if i > start), len(lines))
    return "\n".join(lines[start:end]).rstrip()


def _prescan(root, loaded, ctx, s):
    """런 전체의 requires 를 미리 훑는다 — 뒤 페이즈에서 막히면 앞이 낭비다.

    **예측이지 보장이 아니다.** 앞선 페이즈가 파일을 고치므로 여기서 통과한
    것이 나중에 실패할 수 있다. 그래서 경고로만 내고, 각 페이즈 진입 시 다시
    강제한다 — 둘 다 필요하다.
    """
    out = []
    for pid, item in sorted(loaded.items()):
        if pid == s.get("phase"):
            continue
        for req in item["front"].get("requires") or []:
            if req.get("kind") != "adapter_stage":
                continue          # 파일·상태는 아직 없는 것이 정상이다
            got = check_requires(root, [req], ctx, s)[0]
            if not got["ok"] or got.get("warn"):
                out.append({"phase": pid, "message": got["message"]})
    return out


def _horizon_render(pid, loaded=None):
    """지평선 — 다음 페이즈가 없다. 둘을 가른다.

    `pid` 가 없으면 **런이 끝난 것**이고, 있으면 그 페이즈가 아직 없는 것이다.
    전에는 이 함수가 "01~04 까지가 범위다"라고 **범위를 문자열로 적고 있었고**,
    05 가 생긴 뒤에도 그대로 남아 `feature.md` 와 어긋나 있었다. 이제 범위는
    실재하는 페이즈 파일에서 유도한다 — 문자열을 두 곳에 두지 않는다.
    """
    if not pid or pid == st.DONE:
        return ("## 런 완료\n\n"
                "마지막 페이즈까지 끝났다. `status` 로 등급과 남은 gap 을 본다.")
    scope = ""
    if loaded:
        ids = sorted(loaded)
        scope = "이 실행기의 범위는 `%s` ~ `%s` 다.\n" % (ids[0], ids[-1])
    return ("## 여기까지다\n\n"
            "%s다음 페이즈 `%s` 는 아직 구현되지 않았다.\n"
            "`status` 로 이 런의 등급과 남은 gap 을 본다." % (scope, pid))


def _escalation_envelope(cmd, paths, s):
    esc = s.get("escalation") or {}
    lines = ["## 에스컬레이션 — 사람의 판단이 필요하다", "", esc.get("reason", "")]
    if esc.get("options"):
        lines += [""] + ["%d. %s" % (i + 1, o) for i, o in enumerate(esc["options"])]
    lines += ["", "`%s` 에 전문이 있다." % paths.escalation.name]
    return st.envelope(cmd, False, 10, s, {"escalation": esc}, "\n".join(lines),
                       "python scripts/pipeline/cli.py resume --ack --answer-file <경로>")


# ---------------------------------------------------------------------- record

def cmd_record(root, args):
    return st.emit(run_record(root, args.phase, args.file, args.reviewer,
                              args.round, args.run_id, failed=args.failed,
                              reason=args.reason))


def run_record(root, phase, file, reviewer=None, round_=None, run_id=None,
               failed=False, reason=None):
    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("record", False, 3, None, {}, "런이 없다.", None)
    if s.get("escalated"):
        return _escalation_envelope("record", paths, s)

    loaded, _broken = load_phases(root)
    pid = _normalize_phase(phase, loaded)
    if pid is None:
        return st.envelope("record", False, 2, s, {"phase": phase},
                           "알 수 없는 페이즈: %r" % phase, None)

    if st.phase_status(s, pid) == "passed":
        return st.envelope(
            "record", False, 3, s, {"phase": pid},
            "`%s` 는 이미 통과했다. **record 는 멱등이 아니다** — 재작업은 "
            "`retry --phase %s --counter <이름> --reason <사유>` 로만 한다."
            % (pid, pid.split("-")[0]), None)

    phase_item = loaded[pid]
    ctx = build_context(root, paths, s)
    checks = check_requires(root, phase_item["front"].get("requires"), ctx, s)
    # 이름이 `failed` 이면 `--failed` 파라미터를 덮는다. 하나는 "선행 조건이
    # 안 맞았다", 하나는 "리뷰어 호출이 실패했다" 이고 뜻이 전혀 다르다.
    unmet = [c for c in checks if not c["ok"]]
    if unmet:
        return st.envelope("record", False, 3, s, {"requires_report": checks},
                           "## 선행 조건 미충족\n\n" +
                           "\n".join("- %s" % c["message"] for c in unmet), None)

    if failed and pid != "05-code-review":
        return st.envelope(
            "record", False, 2, s, {"phase": pid},
            "`--failed` 는 05 의 리뷰어 실패 신고 전용이다. 다른 페이즈의 실패는 "
            "제출을 내지 않는 것으로 드러나고 선행 조건이 그것을 막는다.", None)
    if not failed and not file:
        return st.envelope("record", False, 2, s, {"phase": pid},
                           "`--file` 이 필요하다.", None)

    if pid in _CLOSED_BY:
        return st.envelope(
            "record", False, 2, s, {"phase": pid, "closed_by": _CLOSED_BY[pid]},
            "`%s` 는 제출로 닫지 않는다 — `%s` 가 닫는다.\n\n"
            "페이즈는 자기 동사로 닫힌다: 04 는 `gate`, 08 은 `report` 다."
            % (pid, _CLOSED_BY[pid]), None)

    if failed:
        st.append_event(paths, "reviewer_failed", cmd="record", phase=pid,
                        reviewer=reviewer, reason=reason)
        try:
            return _record_05_failed(root, paths, s, phase_item, ctx, reviewer,
                                     round_, reason)
        except ConfigDeclarationError as exc:
            return _declaration_envelope("record", s, exc)

    st.append_event(paths, "submit_received", cmd="record", phase=pid,
                    file=paths.rel(file), reviewer=reviewer)
    handler = _RECORD_HANDLERS.get(pid)
    if handler is None:
        return st.envelope("record", False, 2, s, {"phase": pid},
                           "`%s` 의 제출 처리는 아직 구현되지 않았다." % pid, None)
    # **제출을 세지 않는다** (M26). 계수는 `next`·`review07`·`gate` 가 기동을
    # 지시하는 자리에서 일어난다 — `_instruction_keys` 를 보라.
    try:
        env = handler(root, paths, s, phase_item, ctx, Path(file), reviewer,
                      round_)
    except ConfigDeclarationError as exc:
        return _declaration_envelope("record", s, exc)
    # **형식 반려는 여기 한 곳에서 센다** (ADR-H052 결정 3). exit 8 을 내는
    # 자리는 핸들러 안에 열다섯 곳이고 절반은 `check_fail` 도 없다 — 핸들러가
    # 무엇을 돌려주든 8 이면 제출이 규약을 어겨 되돌아온 것이다.
    if env.get("exit") == 8:
        st.append_event(paths, "format_reject", cmd="record", phase=pid,
                        reviewer=reviewer, round=round_)
    return env


def _instruction_keys(s, pid, ctx, front=None):
    """이 페이즈 진입이 **기동을 지시하는** 에이전트들. 키는 라운드까지 담는다.

    실행기가 볼 수 있는 것은 자기가 낸 지시뿐이다. 여기 없는 것(모델이
    스스로 부르는 호출)은 세지지 않고, 그 사실이 `budget.blind_spots` 에
    이름으로 남는다.

    `front` 는 그 페이즈의 프론트매터다 — 01 의 `review.unless` 와 03 의
    `allow.unless` 가 레인별로 리뷰어·역할을 끄므로(ADR-H044) 선언 없이는
    누구를 지시하는지 알 수 없다.
    """
    counters = s.get("counters") or {}

    def used(name):
        return (counters.get(name) or {}).get("used", 0)

    if pid == "00-triage":
        node = (s.get("phases") or {}).get("00-triage") or {}
        return ["00:triage"] if node.get("needs_model") else []
    if pid == "01-plan":
        r = used("round")
        return ["01:r%d:%s" % (r, code) for code in _reviewers_for(front, s)]
    if pid == "02-cross-verify":
        return ["02:r%d:xv" % used("xverify_return")]
    if pid == "03-implement":
        r = used("repair")
        # 디스패치는 계약이 정하고 03 패킷을 내는 자리가 저장한다 (ADR-H057).
        # 기록이 없으면(옛 런) 조건부 역할을 빼고 센다.
        ids = ((s.get("phases") or {}).get("03-implement") or {}).get("dispatched_roles")
        if ids is None:
            ids = [role.get("id") for role in _roles_for(front, ctx, s)
                   if not role.get("when_contract_section")]
        return ["03:r%d:%s" % (r, i) for i in ids]
    if pid == "05-code-review":
        node = (s.get("phases") or {}).get("05-code-review") or {}
        r = used("review_repair") + 1
        planned = _planned_for_round(node, r)
        # `merged` 는 한 에이전트가 관점을 순차 적용한다 — 기동 지시도 하나다.
        # 제출은 M37 대로 리뷰어 수만큼 갈라지지만 그것은 계수가 아니다.
        if r == 1 and node.get("mode") == "merged" and len(planned) > 1:
            return ["05:r1:merged"]
        return ["05:r%d:%s" % (r, c) for c in planned]
    return []


def _budget_stop(paths, env):
    """제출은 살리고 다음 호출만 막는다 — exit 5 는 소진이지 거부가 아니다."""
    if not env.get("ok") or env.get("exit") not in (0, 11):
        return env
    env["ok"] = False
    env["exit"] = 5
    env["next_command"] = None
    env["render"] = (
        "## 모델 호출 예산이 소진됐다\n\n"
        "이번 제출은 기록됐다. 다음 호출을 요구하지 않고 여기서 멈춘다.\n"
        "계속하려면 사람이 `budget.model_calls_max` 를 올리거나 범위를 줄인다.\n\n"
        "직전 지시문:\n\n%s" % env.get("render", ""))
    st.append_event(paths, "check_fail", cmd="record", exit=5,
                    reason="model_call_budget")
    return env


def _normalize_phase(phase, loaded):
    """`04` 와 `04-gate` 를 둘 다 받는다."""
    if phase in loaded:
        return phase
    for pid in loaded:
        if pid.split("-")[0] == str(phase).zfill(2):
            return pid
    return None


def cmd_abandon(root, args):
    return st.emit(run_abandon(root, run_id=args.run_id, reason=args.reason))


def run_abandon(root, run_id=None, reason=None):
    """이어질 일이 없는 런을 **명시적으로** 닫는다. 종료 코드 0 / 2 / 3.

    버려진 런이 `active` 로 남아 있으면 `latest_run_id` 가 그것을 집고,
    `status` 화면이 이어질 것처럼 말한다 — **이어지지 않을 런이 이어질 것처럼
    보이는 것 자체가 거짓이다.** 지우지 않고 사실로 남긴다.

    `--reason` 을 강제하는 이유는 원장에서 "설계가 바뀌어 버렸다" 와 "인프라가
    깨져 못 이었다" 가 갈려야 하기 때문이다.
    """
    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("abandon", False, 3, None, {}, "런이 없다.", None)
    if not (reason or "").strip():
        return st.envelope("abandon", False, 2, s, {},
                           "`--reason` 이 필요하다. 사유 없이 닫으면 원장에서 "
                           "포기와 장애가 같아 보인다.", None)
    if s.get("run_status") in st.TERMINAL_STATUS:
        return st.envelope("abandon", False, 3, s,
                           {"run_status": s.get("run_status")},
                           "이미 닫힌 런이다 (`%s`). 종단은 되돌리지 않는다."
                           % s.get("run_status"), None)

    st.close_run(s, status="abandoned", reason=reason.strip())
    st.append_event(paths, "run_closed", cmd="abandon", phase=s.get("phase"),
                    grade=s.get("grade"), gaps=s.get("gaps") or [])
    st.save(paths, s)
    return st.envelope("abandon", True, 0, s,
                       {"run_status": "abandoned", "reason": reason.strip()},
                       "런 `%s` 을 **버린 것으로** 닫았다 — %s\n\n"
                       "산출물은 그대로 남는다. `--run-id` 없이 부르는 커맨드가 "
                       "이제 이 런을 집지 않는다." % (s["run_id"], reason.strip()),
                       None)


def _close_run(root, paths, s, phase_item, ctx, cmd):
    """마지막 페이즈 통과 → 런 종료. **`done` 으로 옮기는 자리는 여기 하나다.**

    `st.close_run` 이 `run_status` 의 단일 출처이고, 종단 상태를 인자로 받는다 —
    등급이 세 곳에서 대입되던 것을 `st.demote` 로 모은 것과 같은 규율이다
    (ADR-H015). `abandon` 도 그 함수를 부르지 자기 대입을 만들지 않는다.
    """
    pid = phase_item["front"]["id"]
    st.set_phase_status(s, pid, "passed")
    st.append_event(paths, "phase_pass", cmd=cmd, phase=pid)
    s["phase"] = st.DONE
    st.close_run(s)
    st.append_event(paths, "run_closed", cmd=cmd, phase=pid,
                    grade=s.get("grade"), gaps=s.get("gaps") or [])
    st.save(paths, s)
    return st.envelope(cmd, True, 11, s,
                       {"next_phase": st.DONE, "closed": True,
                        "grade": s.get("grade"), "gaps": s.get("gaps") or []},
                       _horizon_render(None), None)


def _advance_to_next(root, paths, s, phase_item, ctx, cmd="record",
                     status="passed"):
    """통과 시 전이하고 **다음 페이즈 지시문을 바로 낸다** (왕복 절약).

    `status` 는 떠나는 페이즈에 남길 상태다 — 통과면 `passed`, `skip_policy`
    로 건너뛰면 `skipped` 다. 건너뛴 것을 `passed` 로 덮으면 보고서가 "관측이
    있었다" 고 적는다.
    """
    pid = phase_item["front"]["id"]
    nxt = phase_item["front"].get("on_success")
    if nxt == st.DONE:
        return _close_run(root, paths, s, phase_item, ctx, cmd)

    st.set_phase_status(s, pid, status)
    if status == "passed":
        st.append_event(paths, "phase_pass", cmd=cmd, phase=pid)
    s["phase"] = nxt
    st.save(paths, s)

    loaded, _ = load_phases(root)
    if nxt not in loaded:
        st.append_event(paths, "horizon", cmd=cmd, phase=nxt)
        return st.envelope(cmd, True, 11, s, {"next_phase": nxt},
                           _horizon_render(nxt, loaded), None)
    nxt_item = loaded[nxt]
    nxt_checks = check_requires(root, nxt_item["front"].get("requires"), ctx, s)
    if [c for c in nxt_checks if not c["ok"]]:
        return st.envelope(cmd, True, 0, s, {"next_phase": nxt,
                                             "requires_report": nxt_checks},
                           "`%s` 통과. 다음은 `%s` 이고 아직 선행 조건이 남았다:\n\n%s%s"
                           % (pid, nxt,
                              "\n".join("- %s" % c["message"]
                                        for c in nxt_checks if not c["ok"]),
                              _journey_hint(root, s) if nxt == "03-implement" else ""),
                           "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])
    st.set_phase_status(s, nxt, "running")
    st.append_event(paths, "phase_enter", cmd=cmd, phase=nxt)
    skipped = _skip_policy(root, paths, s, nxt_item, ctx, cmd)
    if skipped is not None:
        return skipped
    if nxt == "03-implement":
        refused = _contract_precheck_refuse(root, paths, s, ctx, cmd)
        if refused is not None:
            return refused
        _store_dispatch(root, ctx, s, nxt_item["front"])
    # **지시를 낸 자리에서 센다.** 전이가 다음 패킷을 바로 내므로 `next` 의
    # 계수를 지나친다 — 02 의 교차검증기가 그렇게 예산 밖에 있었다 (ADR-H042).
    _t, _m, exhausted = _instruct(
        s, nxt, _instruction_keys(s, nxt, ctx, nxt_item["front"]), ctx)
    st.save(paths, s)
    render, next_cmd = render_packet(root, nxt_item, ctx, s, nxt_checks)
    env = st.envelope(cmd, True, 0, s, {"next_phase": nxt}, render, next_cmd)
    return _budget_stop(paths, env) if exhausted else env


def _skip_policy(root, paths, s, phase_item, ctx, cmd):
    """`skip_policy[]` 의 첫 일치가 참이면 이 페이즈를 **등급 강등 없이** 건너뛴다.

    02 의 존재 이유는 "부분 편집으로 고친 전문의 모순" 이다. 01 이 1라운드에
    수렴했으면 편집이 없었고, 그때 교차검증기가 본 것이 곧 전문이다 — 같은
    관측기를 같은 텍스트에 한 번 더 부르는 것이다 (`plan_unedited`,
    ADR-H042). 00 이 docs 레인으로 예측했으면 01 에 리뷰어가 없었으므로
    "본 텍스트가 곧 전문" 이라는 사유는 거짓이다 — 그래서 `docs_profile` 이
    앞에 따로 있다 (ADR-H044). `skip_when`(관측기 부재)과 다르다: 그쪽은
    관측이 없었던 것이라 등급이 내려간다.
    """
    front = phase_item["front"]
    for node in front.get("skip_policy") or []:
        if not eval_condition(node.get("when"), s):
            continue
        pid = front["id"]
        status = node.get("status") or "skipped"
        reason = node.get("reason")
        st.set_phase_status(s, pid, status, skip_reason=reason)
        if pid == "02-cross-verify":
            s.setdefault("cross_verify", {})["skip_reason"] = reason
        if reason in ("docs_profile", "fix_profile"):
            # 레인의 양보다 — 예측이 빗나가면 `triage_miss` gap 이름에 들어간다.
            _note_applied(s, "%s:skipped" % pid.split("-")[0])
        st.append_event(paths, "phase_skip", cmd=cmd, phase=pid, reason=reason)
        st.save(paths, s)
        return _advance_to_next(root, paths, s, phase_item, ctx, cmd,
                                status=status)
    return None


# ------------------------------------------------------- 00 제출 처리

def _record_00(root, paths, s, phase_item, ctx, file, reviewer, round_):
    """트리아지 에이전트의 예측을 받는다. 기계가 확인할 수 있는 것만 검사한다."""
    import triage

    if not file.exists():
        return st.envelope("record", False, 3, s, {}, "산출물이 없다: %s" % file, None)
    try:
        payload = harness._read_json(file)
    except (OSError, ValueError) as exc:
        return st.envelope("record", False, 8, s, {}, "JSON 을 읽지 못했다: %s" % exc, None)

    text = paths.request.read_text(encoding="utf-8")
    errors = triage.check_submission(payload, text, ctx["config"])
    if errors:
        st.append_event(paths, "check_fail", cmd="record", phase="00-triage",
                        errors=len(errors))
        st.save(paths, s)
        return st.envelope("record", False, 8, s, {"errors": errors},
                           "## 트리아지 제출 거부\n\n" +
                           "\n".join("- %s" % e for e in errors),
                           _same_command(s, "00"))

    node = s.setdefault("phases", {}).setdefault("00-triage", {})
    if payload.get("profile") == "unclear":
        options = ["docs — 문서·설정만 바뀐다 (리뷰어·역할 없이 메인이 직접 고친다)",
                   "fix — 재현 가능한 버그 하나의 수리 (01 1라운드 · 02 생략 · "
                   "리뷰어 1명)",
                   "small — 역할 소유 경로 셋 이하의 작은 변경",
                   "normal — 그 밖 전부"]
        st.save(paths, s)
        # 여기서부터 사람을 기다린다 — `40dc` 의 36분이 이 자리였다 (ADR-H052).
        st.append_event(paths, "waiting_human", cmd="record", phase="00-triage",
                        reason="triage_unclear")
        return st.envelope(
            "record", False, 9, s,
            {"options": options, "signals": node.get("signals"),
             "reasons": payload.get("reasons") or []},
            "## 트리아지가 `unclear` 다 — 사람의 판단\n\n기계 신호도 모델도 레인을 "
            "정하지 못했다. 아래 셋 중 하나를 사용자에게 **그대로** 제시하고, 고른 "
            "값을 `profile` 에 넣고 `decided_by: \"user\"` 로 같은 파일을 다시 "
            "낸다. **네가 고르지 마라.**\n\n%s"
            % "\n".join("%d. %s" % (i + 1, o) for i, o in enumerate(options)),
            _same_command(s, "00"))

    decided_by = payload.get("decided_by")
    if decided_by not in triage.DECIDED_BY or decided_by == "machine":
        decided_by = "model"
    decision = {"profile": payload["profile"],
                "expected_paths": payload.get("expected_paths") or [],
                "touches_source": payload.get("touches_source"),
                "reasons": payload.get("reasons") or [],
                "decided_by": decided_by}
    sig = node.get("signals")
    if sig is None:
        sig = triage.signals(text, ctx["config"])
        node["signals"] = sig
    _apply_triage(paths, s, decision, sig, cmd="record")
    st.save(paths, s)
    return _advance_to_next(root, paths, s, phase_item, ctx)


# ------------------------------------------------------- 01 제출 처리

def _record_01(root, paths, s, phase_item, ctx, file, reviewer, round_):
    if reviewer:
        return _record_01_review(root, paths, s, phase_item, ctx, file,
                                 reviewer, round_ or 1)
    return _record_01_plan(root, paths, s, phase_item, ctx, file)


def _record_01_plan(root, paths, s, phase_item, ctx, file):
    if not file.exists():
        return st.envelope("record", False, 3, s, {}, "산출물이 없다: %s" % file, None)
    text = file.read_text(encoding="utf-8")
    request_text = paths.request.read_text(encoding="utf-8")
    limit = ((ctx["config"].get("profile") or {}).get("inv_skip_below_chars") or 0)

    got = verdict.check_plan(text, request_text, limit)
    node = s.setdefault("phases", {}).setdefault("01-plan", {})
    node["drift_score"] = got["drift_score"]
    # INV 생략(짧은 요청)이면 `None` — 02 는 그때 보수적으로 돈다 (ADR-H060).
    node["risk"] = got.get("risk")
    if got.get("inv_skipped"):
        s.setdefault("profile", {})["inv_skipped"] = True

    if not got["ok"]:
        st.set_phase_status(s, "01-plan", "failed")
        st.append_event(paths, "check_fail", cmd="record", phase="01-plan",
                        exit=got["exit"], errors=len(got["errors"]))
        st.save(paths, s)
        return st.envelope("record", False, got["exit"], s,
                           {"errors": got["errors"], "drift_score": got["drift_score"],
                            "drift": got["drift"]},
                           _plan_fail_render(got), _same_command(s, "01"))

    node["plan_accepted"] = True
    st.set_phase_status(s, "01-plan", "running")   # node 는 상태 안의 같은 dict 다
    st.save(paths, s)
    codes = _reviewers_for(phase_item["front"], s)
    if not codes:
        # docs 레인 — 리뷰어 0명 (ADR-H044). 기계 검사(인용·커버리지·드리프트)가
        # 이 페이즈의 전부이고 1라운드에 닫는다. 정책 스킵이라 등급은 안
        # 내려가지만 **`applied` 에 남아** 예측이 빗나가면 gap 이름이 된다.
        _note_applied(s, "01:reviewers=0")
        rounds = node.setdefault("rounds", {})
        st.save(paths, s)
        return _judge_round(root, paths, s, phase_item, ctx, 1, {}, rounds)
    tiers = _model_tiers_render(
        ctx, s, _instruction_keys(s, "01-plan", ctx, phase_item["front"]))
    return st.envelope(
        "record", True, 0, s,
        {"drift_score": 0, "round": _round_no(s), "reviewers": codes},
        "## 플랜이 받아들여졌다\n\n인용 검증과 커버리지가 통과했고 드리프트가 0 이다.\n"
        "이제 **리뷰어 둘을 병렬로** 돌린다 (`%s`). 회차마다 원문 `.raw.md` 와 "
        "구조화 `.json` 을 함께 낸다.%s"
        % ("`, `".join(codes), ("\n\n" + tiers) if tiers else ""),
        "python scripts/pipeline/cli.py record --phase 01 --file <리뷰 json> "
        "--reviewer <code> --round %d --run-id %s" % (_round_no(s), s["run_id"]))


def _plan_fail_render(got):
    if got["exit"] == 4:
        lines = ["## 드리프트 — 의도가 새어 나갔다", ""]
        for d in got["drift"]:
            lines.append("- `%s` (%s): %s — 사유: %s"
                         % (d["id"], d.get("kind"), d["status"], d["reason"]))
        lines += ["", "덮거나, 사용자 승인을 받아야 넘어간다. 예산은 남아 있다."]
        return "\n".join(lines)
    return ("## 제출물 거부\n\n" +
            "\n".join("- %s" % e for e in got["errors"]))


def _round_no(s):
    return ((s.get("counters") or {}).get("round") or {}).get("used", 0) + 1


def _record_01_review(root, paths, s, phase_item, ctx, file, reviewer, round_):
    node = s.setdefault("phases", {}).setdefault("01-plan", {})
    if not node.get("plan_accepted"):
        return st.envelope("record", False, 3, s, {},
                           "플랜이 먼저다. `record --phase 01 --file <01_plan.md>`", None)
    if not file.exists():
        return st.envelope("record", False, 3, s, {}, "산출물이 없다: %s" % file, None)
    try:
        payload = harness._read_json(file)
    except (OSError, ValueError) as exc:
        return st.envelope("record", False, 8, s, {}, "JSON 을 읽지 못했다: %s" % exc, None)

    raw_path = file.with_name(file.name.replace(".json", ".raw.md"))
    if not raw_path.exists():
        return st.envelope("record", False, 8, s, {},
                           "리뷰어 원문이 없다: %s — 구조화 JSON 만으로는 "
                           "quote 를 검증할 수 없다" % raw_path.name, None)
    raw_text = raw_path.read_text(encoding="utf-8")

    rounds = node.setdefault("rounds", {})
    prev_open = _previous_open(rounds, round_, reviewer)
    got = verdict.check_review(payload, raw_text, prev_open,
                               _converge_blocking(phase_item["front"]))
    if not got["ok"]:
        st.append_event(paths, "check_fail", cmd="record", phase="01-plan",
                        reviewer=reviewer, errors=len(got["errors"]))
        st.save(paths, s)
        return st.envelope("record", False, 8, s, {"errors": got["errors"]},
                           "## 리뷰 제출 거부\n\n" +
                           "\n".join("- %s" % e for e in got["errors"]),
                           _same_command(s, "01"))

    slot = rounds.setdefault(str(round_), {})
    slot[reviewer] = {"mode": payload.get("mode") or "primary",
                      "keys": got["keys"], "blocking": got["blocking"],
                      "closed": got["closed"]}
    # **교차검증기의 회차 기록은 런 요약에도 접힌다.** 예전에는 여기 slot 에만
    # 들어가 `state.cross_verify` 는 config 가 찍은 값을 그대로 들고 있었다 —
    # 다섯 라운드가 전부 폴백인데 상태는 `primary` 라고 적었고, 보고서는 그
    # 사실을 한 글자도 말하지 않았다 (P3).
    if _is_cross_verifier(phase_item, reviewer):
        st.note_cross_verify_round(s, round_, slot[reviewer]["mode"],
                                   payload.get("primary_error"))
    st.save(paths, s)

    # 2라운드부터는 **열린 차단 지적을 낸 리뷰어만** 다시 온다 (ADR-H041) —
    # 05 의 델타 재리뷰와 같은 형태다. 1라운드는 전원이다.
    expected = ((node.get("rounds_planned") or {}).get(str(round_))
                or _reviewers_for(phase_item["front"], s))
    missing = [c for c in expected if c not in slot]
    if missing:
        return st.envelope("record", True, 0, s,
                           {"round": round_, "waiting_for": missing},
                           "`%s` 리뷰를 받았다. 아직 `%s` 가 남았다."
                           % (reviewer, "`, `".join(missing)),
                           "python scripts/pipeline/cli.py record --phase 01 "
                           "--file <리뷰 json> --reviewer %s --round %d --run-id %s"
                           % (missing[0], round_, s["run_id"]))

    return _judge_round(root, paths, s, phase_item, ctx, round_, slot, rounds)


def _previous_open(rounds, round_, reviewer=None):
    """**아직 열려 있는** 이전 회차의 지적. 사라지면 단조성 검사가 잡는다.

    닫힌 것은 뺀다. 안 빼면 3라운드 제출이 1라운드에서 이미 해소된 지적까지
    다시 적어야 통과하고, 그 목록이 리뷰어 프롬프트에 실리므로 **접두부가
    라운드마다 자란다** (M21 ②).

    `reviewer` 를 주면 그 리뷰어가 낸 것만 돌려준다. 두 리뷰어가 모두 `F-1` 을
    쓰므로 id 대조를 전역으로 하면 한 줄이 서로 다른 두 지적을 동시에
    해소로 계수한다 (M21 ③).
    """
    open_, closed = {}, set()
    for rn in sorted(rounds, key=int):
        if int(rn) >= round_:
            continue
        for code, sub in rounds[rn].items():
            for k in sub.get("keys") or []:
                open_.setdefault(k["key"], dict(k, reviewer=code))
            closed |= set(sub.get("closed") or [])
    out = [k for key, k in open_.items() if key not in closed]
    if reviewer is not None:
        out = [k for k in out if k.get("reviewer") == reviewer]
    return out


def _open_from_05(s):
    """05 가 열어 둔 채 07 에 넘긴 지적. 07 의 dedup 선언이 가리킬 대상이다.

    라운드 번호를 마지막보다 크게 잡아 **모든 회차**를 훑는다 — 05 는 이미
    끝났고, 남은 물음은 "무엇이 열린 채로 왔나" 하나다 (M48).
    """
    node = (s.get("phases") or {}).get("05-code-review") or {}
    rounds = node.get("rounds") or {}
    if not rounds:
        return []
    return _previous_open(rounds, max(int(r) for r in rounds) + 1)


def _open_from_05_render(open_):
    """봉투가 목록을 직접 준다 — 모델이 재구성하면 그 재구성이 곧 결함이다."""
    if not open_:
        return ("## 05 가 이미 낸 지적\n\n(없다) — 05 가 연 채로 넘긴 것이 없다. "
                "여기서 잡는 것은 전부 새 것이다.")
    lines = ["## 05 가 이미 낸 지적 — 같은 것이면 가리켜라", "",
             "아래는 05 가 **열어 둔 채** 넘긴 것이다. 같은 결함에 다른 이름을 "
             "붙이면 기계는 새 것으로 세고, 그러면 `escaped_05` 가 05 를 실제보다 "
             "나쁘게 적는다 (M48). 같은 것이면 그 finding 에 "
             '`"reraised_from_previous": "<키>"` 를 단다.', ""]
    for k in open_:
        lines.append("- `%s` (`%s`, `%s`) — %s"
                     % (k.get("key"), k.get("severity"), k.get("reviewer"),
                        k.get("title_norm") or k.get("title") or "제목 없음"))
    lines += ["", "**목록에 없는 키를 가리키면 exit 8 이다.** 새 것이면 아무것도 "
                  "달지 않는다 — 안 다는 것이 기본이고, 다는 것이 주장이다."]
    return "\n".join(lines)


def _plan_has_risk(node, rounds):
    """02 를 돌릴 근거가 있는가 (ADR-H060).

    셋 중 하나면 참이다 — INTENT 의 `risk` 가 비어 있지 않다 / INV 블록이
    생략돼 `risk` 자체가 없다(짧은 요청이라도 관측을 빼지 않는다) / 01 의
    어느 회차든 리뷰어가 Critical 을 냈다(플랜이 한 번 뒤집혔으면 위험 절이
    없다는 자진신고를 그대로 믿지 않는다). `risk` 는 01 의 자진신고이고
    03·05 가 검증하지 않는다 — 대조 장치는 계약에 스키마 절이 생기면 뒤에 둔다.
    """
    risk = node.get("risk")
    if risk is None or risk:
        return True
    return any(k.get("severity") == "critical"
               for r in (rounds or {}).values() for sub in r.values()
               for k in sub.get("keys") or [])


def _note_cross_verify_gap(s):
    """폴백으로 돈 회차가 있으면 등급이 그것을 말한다.

    **`external:disabled` 와 같은 형태다** — 리뷰가 약해진 것은 통과가 아니고,
    gap 에 이름이 박혀야 보고서가 그것을 적을 수 있다. 예전에는 폴백이 gap 이
    아니라 `PASS` 로 끝났고, P3 는 다섯 라운드가 전부 폴백인데 보고서에 그
    낱말이 한 번도 안 나왔다.

    등급은 `demote` 가 나쁜 쪽으로만 움직이므로 여기서 되돌아가지 않는다.
    """
    node = s.get("cross_verify") or {}
    if node.get("degraded_rounds"):
        st.demote(s, "PASS_WITH_GAPS", gap="cross_verify:fallback")


def _open_blocking_keys(rounds, upto_round, blocking):
    """`upto_round` 까지 제출된 것 중 **아직 열린 차단 키** 집합."""
    return {k["key"] for k in _previous_open(rounds, upto_round + 1)
            if k.get("severity") in blocking}


def _judge_round(root, paths, s, phase_item, ctx, round_, slot, rounds):
    front = phase_item["front"]
    blocking = _converge_blocking(front)
    subs = [dict(v, code=k) for k, v in slot.items()]
    prev_keys = {k["key"] for r in rounds for sub in rounds[r].values()
                 for k in sub.get("keys") or [] if int(r) < round_}
    drift = (s["phases"]["01-plan"] or {}).get("drift_score") or 0
    ok, reason = verdict.converged(round_, subs, prev_keys, drift, blocking)

    conv = front.get("converge") or {}
    profile = (s.get("profile") or {}).get("name") or "normal"
    max_rounds = (conv.get("max_by_profile") or {}).get(profile) or 5
    if profile != "normal" and (conv.get("max_by_profile") or {}).get(profile):
        # 라운드 상한이 레인의 양보다 — 예측이 빗나가면 gap 이름에 들어간다.
        _note_applied(s, "01:max_rounds=%d" % max_rounds)

    if ok:
        # **`rounds` 를 덮지 않는다.** 예전에는 여기서 수렴 회차(정수)를
        # 그 자리에 대입해 라운드별 제출 기록을 통째로 날렸다. 01 이 다시
        # 돌지 않으면 무해했지만, 02 의 Critical 이 01 로 되돌리는 경로가
        # 처음 돌자 `_previous_open` 이 정수를 순회하려다 죽었고 단조성
        # 검사가 근거로 삼는 이전 회차 지적이 사라졌다. 정수를 읽는
        # 소비자는 어디에도 없었다 — 순수한 손실이다 (P3).
        s["phases"]["01-plan"]["converged_at_round"] = round_
        # **02 가 돌지를 여기서 정한다** (ADR-H060). 02 의 `skip_policy` 가 이
        # 값을 읽는다 — 단일 비교만 받으므로 합성은 여기서 한다.
        s["phases"]["01-plan"]["has_risk"] = _plan_has_risk(
            s["phases"]["01-plan"], rounds)
        # exceeded 무시 — 수렴이 라운드를 닫았다. 마지막 라운드에서 수렴한
        # 것은 상한 초과가 아니고, 여기서 멈출 다음 라운드도 없다 (ADR-H048).
        st.counter_inc(s, _loop_counter(phase_item["front"]), max_rounds,
                       "converged", paths=paths)
        _note_cross_verify_gap(s)
        return _advance_to_next(root, paths, s, phase_item, ctx)

    # **봉투는 실효 상한을 말해야 한다** (M56). `max_rounds` 는 선언값이라
    # 왕복 뒤 지급을 받은 런에서 "5라운드 안에" 라고 적으면서 실제로는 10 을
    # 다 쓰고 멈춘다 — 사람이 그 숫자로 판단할 수 없다.
    used, max_eff, exceeded = st.counter_inc(
        s, _loop_counter(front), max_rounds, "not_converged", paths=paths)
    options = ["이대로 진행한다(미해결 지적을 안고 간다)",
               "범위를 줄여 플랜을 다시 쓴다", "중단한다"]
    if exceeded:
        _loop_on_exceed(front)
        st.escalate(paths, s,
                    "01 이 %d라운드 안에 수렴하지 않았다: %s" % (max_eff, reason),
                    options, phase="01-plan")
        return _escalation_envelope("record", paths, s)

    # **같은 차단 지적이 그대로 반복되면 상한 전에 멈춘다** (ADR-H041).
    # `stuck_after_identical` 은 01 에 선언돼 있었지만 04 만 읽었다 — 01 은
    # 같은 Critical 이 다섯 번 반복돼도 상한까지 태웠다.
    stuck_after = _loop_stuck_after(front)
    open_now = _open_blocking_keys(rounds, round_, blocking)
    identical = 1
    for r in range(round_ - 1, 0, -1):
        if open_now and _open_blocking_keys(rounds, r, blocking) == open_now:
            identical += 1
        else:
            break
    if open_now and identical >= stuck_after:
        _loop_on_exceed(front)
        st.escalate(paths, s,
                    "01 의 같은 %s 지적 %d건이 %d라운드 연속 반복됐다 — 플랜 "
                    "수정이 지적을 닫지 못한다: %s"
                    % ("·".join(blocking), len(open_now), identical, reason),
                    options, phase="01-plan")
        return _escalation_envelope("record", paths, s)

    # **다음 라운드는 열린 차단 지적을 낸 리뷰어만 온다** (ADR-H041). 05 의
    # 델타 재리뷰(`_delta_reviewer`)와 같은 규율이고, 결정론이다.
    planned = [code for code in slot
               if any(k["key"] in open_now for k in slot[code].get("keys") or [])]
    if not planned:
        # 차단 키가 없는데 미수렴 — 폴백 1라운드다. 전원이 다시 온다.
        planned = list(slot)
    s["phases"]["01-plan"].setdefault("rounds_planned", {})[str(used + 1)] = planned
    # **지시를 낸 자리에서 센다.** 01 의 루프는 `record → record` 라 `next`
    # 의 계수를 지나쳤고, 다섯 라운드 열 번을 불러도 예산은 2 였다 (ADR-H042).
    next_keys = ["01:r%d:%s" % (used, code) for code in planned]
    _t, _m, exhausted = _instruct(s, "01-plan", next_keys, ctx)
    st.save(paths, s)
    focus = conv.get("focus_round_2") or ""
    cv_note = _cross_verify_render(ctx["config"], s, front)
    tiers = _model_tiers_render(ctx, s, next_keys)
    if tiers:
        cv_note = (cv_note + "\n\n" + tiers) if cv_note else tiers
    env = st.envelope(
        "record", True, 0, s,
        {"round": used + 1, "reason": reason, "planned": planned},
        "## %d라운드가 필요하다\n\n%s\n\n다음 회차의 강제 초점: %s\n\n"
        "**다시 부를 리뷰어는 `%s` 다** — 열린 차단 지적을 낸 쪽만 온다. "
        "다른 리뷰어는 이번 회차에 부르지 않는다.\n\n"
        "플랜은 **부분 편집**으로 고친다 — 전체를 다시 쓰면 접두부가 라운드마다 "
        "쌓인다.%s"
        # **라운드마다 교차검증기를 다시 말한다.** 이 절은 `render_packet`
        # 에서만 나왔고 그건 `next` 에서만 불리는데, 01 의 루프는
        # `record → record` 라 봉투가 그 말을 다시 할 경로가 물리적으로
        # 없었다 — 그래서 한 번 폴백하면 그 런 내내 굳었다 (P3).
        % (used + 1, reason, focus or "(없음)", "`, `".join(planned),
           ("\n\n" + cv_note) if cv_note else ""),
        "python scripts/pipeline/cli.py record --phase 01 --file <리뷰 json> "
        "--reviewer %s --round %d --run-id %s"
        % (planned[0], used + 1, s["run_id"]))
    return _budget_stop(paths, env) if exhausted else env


def _same_command(s, phase):
    return ("python scripts/pipeline/cli.py record --phase %s --file <산출물> "
            "--run-id %s" % (phase, s["run_id"]))


# ------------------------------------------------------- 02 제출 처리

def _record_02(root, paths, s, phase_item, ctx, file, reviewer, round_):
    front = phase_item["front"]
    skipped = _skip_policy(root, paths, s, phase_item, ctx, "record")
    if skipped is not None:
        return skipped
    if front.get("skip_when") and verdict and eval_condition(front["skip_when"], s):
        on_skip = front.get("on_skip") or {}
        st.set_phase_status(s, "02-cross-verify", on_skip.get("status") or "skipped")
        st.demote(s, on_skip.get("grade") or st.GRADES[1], on_skip.get("gap"))
        gap = on_skip.get("gap")
        st.append_event(paths, "phase_skip", cmd="record", phase="02-cross-verify",
                        gap=gap)
        st.save(paths, s)
        return _advance_to_next(root, paths, s, phase_item, ctx)

    if not file.exists():
        return st.envelope("record", False, 3, s, {}, "산출물이 없다: %s" % file, None)
    try:
        payload = harness._read_json(file)
    except (OSError, ValueError) as exc:
        return st.envelope("record", False, 8, s, {}, "JSON 을 읽지 못했다: %s" % exc, None)

    errors = []
    if payload.get("reviewer") == "main":
        errors.append("reviewer 가 main 이다 — 독립 관측이 아니다")
    plan_text = (paths.run_dir / "01_plan.md").read_text(encoding="utf-8")
    for f in payload.get("findings") or []:
        q = f.get("quote")
        if q and verdict.normalize_ws(q) not in verdict.normalize_ws(plan_text):
            errors.append("%s 의 quote 가 플랜 원문에 없다" % f.get("id"))
        if f.get("severity") in verdict.BLOCKING and not _has_adoption(payload, f):
            errors.append("%s 에 대한 채택 판정(adopted)이 없다" % f.get("id"))
    if errors:
        st.append_event(paths, "check_fail", cmd="record", phase="02-cross-verify",
                        errors=len(errors))
        st.save(paths, s)
        return st.envelope("record", False, 8, s, {"errors": errors},
                           "## 제출물 거부\n\n" + "\n".join("- %s" % e for e in errors),
                           _same_command(s, "02"))

    critical = [f for f in payload.get("findings") or []
                if f.get("severity") == "critical" and _accepted(payload, f)]
    # **병합이지 덮어쓰기가 아니다.** 예전에는 02 의 `mode` 로 통째로 덮어
    # 01 의 회차 기록이 사라졌다. 02 가 primary 로 돌았다고 해서 01 이 폴백
    # 이었다는 사실이 없던 일이 되지 않는다.
    st.note_cross_verify_round(s, "02", payload.get("mode") or "primary",
                               payload.get("primary_error"))
    # **폴백은 여기서도 드러나야 한다** (ADR-H045). 01 은 더는 xv 를 부르지
    # 않으므로 `_judge_round`(01 자신의 수렴 체크)에서만 돌던 이 데모션이
    # 02 자신의 폴백은 영영 보지 못하게 된다 — 02 가 이제 유일한 xv 호출처라
    # 01 쪽 호출과 대칭으로 여기서도 불러야 한다.
    _note_cross_verify_gap(s)
    if critical:
        front = phase_item["front"]
        # **`loop.max` 는 Critical 제출 상한이다** — `max: 2` 면 두 번째 Critical
        # 에서 멈춘다(되돌림은 1회). 판정은 `counter_inc` 의 `exceeded` 하나가
        # 한다. 예전에는 이 반환값을 버리고 `used > max_` 를 따로 셌고, 그래서
        # `3b43` 의 상태가 `used 2 / max 1` 로 남았다 (ADR-H048).
        max_decl = _loop_max(front)
        return_to = _loop_return_to(front)
        used, max_, exceeded = st.counter_inc(s, _loop_counter(front), max_decl,
                                             "xverify_critical", paths=paths)
        if exceeded:
            _loop_on_exceed(front)
            st.escalate(paths, s, "02 가 Critical 을 %d회 냈다 — 상한이다" % max_,
                        ["이대로 진행한다", "범위를 줄인다", "중단한다"],
                        phase=front["id"])
            return _escalation_envelope("record", paths, s)
        st.set_phase_status(s, return_to, "failed")
        st.set_phase_status(s, front["id"], "failed")
        s["phase"] = return_to
        # **바뀐 설계는 새 설계다.** 예전에는 `phase` 만 되돌리고 `round` 카운터를
        # 그대로 뒀다. P3 에서 1~4회차가 수렴한 뒤 02 가 설계를 뒤집었는데 남은
        # 라운드가 한 번이었고, 그 한 번이 진짜 결함 셋을 찾았다 (M32).
        granted = _grant_rounds(root, s, critical)
        st.append_event(paths, "counter_grant", cmd="record",
                        phase="02-cross-verify", counter="round", extra=granted)
        st.save(paths, s)
        return st.envelope(
            "record", False, 4, s,
            {"critical": len(critical), "granted_rounds": granted},
            "## Critical 이 남았다 — `%s` 로 되돌린다\n\n%s\n\n"
            "Critical 은 %d회까지다(`loop.max`) — 다음 Critical 에서 멈춘다. "
            "바뀐 설계에 리뷰 라운드 **%d 를 새로 지급했다** — "
            "새 설계가 한 라운드로 수렴할 이유가 없다.\n"
            "쓴 회차는 지워지지 않는다: %d / %d."
            % (return_to,
               "\n".join("- %s: %s" % (f.get("id"), f.get("title"))
                         for f in critical),
               max_, granted, s["counters"]["round"]["used"],
               s["counters"]["round"]["max"]),
            "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])

    return _advance_to_next(root, paths, s, phase_item, ctx)


def _grant_rounds(root, s, critical):
    """왕복 뒤 01 에 줄 라운드 수. 프로파일 기준 예산 한 벌이다.

    01 의 라운드 상한과 **같은 출처**(`01-plan.md` 의 `converge.max_by_profile`)
    에서 읽는다. 두 곳이 갈라지면 "왕복 뒤 예산" 이 상한과 다른 뜻을 갖는다.
    `_judge_round` 의 `or 5` 폴백도 그대로 따라간다. 선언값은 normal 3 이다
    (ADR-H041) — 왕복 한 번이면 실효 상한 6.
    """
    loaded, _broken = load_phases(root)
    conv = ((loaded.get("01-plan") or {}).get("front") or {}).get("converge") or {}
    profile = (s.get("profile") or {}).get("name") or "normal"
    extra = (conv.get("max_by_profile") or {}).get(profile) or 5
    st.counter_grant(
        s, "round", extra,
        "02 의 Critical %d건이 설계를 뒤집었다 — 새 설계에 리뷰 라운드를 준다"
        % len(critical))
    return extra


def _has_adoption(payload, finding):
    return any(a.get("id") == finding.get("id")
               for a in payload.get("adopted") or [])


def _accepted(payload, finding):
    for a in payload.get("adopted") or []:
        if a.get("id") == finding.get("id"):
            return a.get("verdict") != "reject"
    return True


# ------------------------------------------------------- 03 제출 처리

def _record_03(root, paths, s, phase_item, ctx, file, reviewer, round_):
    import attribution
    import contract as contract_mod

    if not file.exists():
        return st.envelope("record", False, 3, s, {}, "산출물이 없다: %s" % file, None)
    try:
        claims = harness._read_json(file)
    except (OSError, ValueError) as exc:
        return st.envelope("record", False, 8, s, {}, "JSON 을 읽지 못했다: %s" % exc, None)

    _refresh_profile(root, paths, s, ctx)
    zero = _contract_units_zero(root, s, ctx)
    if zero is not None:
        # **계약 파일이 있는데 유닛이 0 이면 형식 문제다** (ADR-H049). `requires`
        # 는 크기와 절 제목만 본다 — 파일럿 40dc 의 계약이 `## 유닛` 을 `### `
        # 헤딩으로 적어 units=0 으로 게이트를 지났고, 그 결과 계약에 서술된
        # 심볼이 전부 `out_of_contract` 로 잡히고 scoped 는 `no_selector` 로
        # 스킵됐다. 여기서 막으면 그 둘이 뒤에서 안 난다.
        st.set_phase_status(s, "03-implement", "failed")
        st.append_event(paths, "check_fail", cmd="record", phase="03-implement",
                        contract_units=0)
        st.save(paths, s)
        return st.envelope(
            "record", False, 8, s, {"contract": zero},
            "## 계약의 유닛이 0 이다\n\n계약 파일은 있는데 `%s` 절에서 파서가 "
            "유닛을 하나도 못 읽었다. 유닛은 **최상위 `- ` 불릿 하나에 하나**이고 "
            "형식은 `harness/templates/contract.md` 의 예시 그대로다 — "
            "`- \\`컨테이너 · 심볼(...)\\``. `### ` 헤딩·들여쓴 불릿·산문은 유닛이 "
            "아니다. 진입점 경로는 실제 디렉터리명으로 적는다(어댑터가 "
            "`param_styles` 를 선언하면 `{id}` 도 받는다).\n\n버려진 줄 %d개:\n%s"
            % (zero["section"], len(zero["dropped"]),
               "\n".join("- `%s` — %s" % (d.get("raw"), d.get("reason"))
                         for d in zero["dropped"][:10]) or "- (없음 — 불릿이 한 줄도 없다)"),
            _same_command(s, "03"))
    refused = _contract_precheck_03(root, ctx, s)
    if refused is not None:
        # 백스톱이다 — 패킷 뒤에 메인이 계약에 여정을 넣은 경우 (ADR-H058 추기).
        st.set_phase_status(s, "03-implement", "failed")
        st.append_event(paths, "check_fail", cmd="record", phase="03-implement",
                        **refused["data"])
        st.save(paths, s)
        return st.envelope("record", False, 8, s, refused["data"], refused["render"],
                           _same_command(s, "03"))
    bad_rules = _check_rules_read(claims, _rules_read_expected(root, ctx["config"]))
    if bad_rules:
        # **워커의 규칙 읽기를 게이트가 묻는다** (ADR-H055). `CLAUDE.md` 는
        # 자동 주입되지 않고 「읽을 곳」이 가리키기만 한다 — 열었는지는 아무
        # 기록도 없었다. 해시 일치는 "읽었다" 의 증명이 아니지만 "열어 보지도
        # 않고 지켰다고 보고" 는 여기서 막힌다. 봉투에는 **경로와 상태만**
        # 싣는다 — 해시를 주면 안 열고도 맞춘다.
        st.set_phase_status(s, "03-implement", "failed")
        st.append_event(paths, "check_fail", cmd="record", phase="03-implement",
                        rules_read=[{"role": r, "path": p, "status": why}
                                    for r, p, why in bad_rules])
        st.save(paths, s)
        return st.envelope(
            "record", False, 8, s,
            {"rules_read": [{"role": r, "path": p, "status": why}
                            for r, p, why in bad_rules]},
            "## 규칙 읽기 증명이 없다 — `rules_read`\n\n각 역할의 제출에 "
            "`rules_read: [{path, sha256}]` 가 있어야 하고, 아래 파일 전부의 "
            "**현재** sha256 과 같아야 한다 (ADR-H055). 해시는 워커가 파일을 읽어 "
            "직접 계산한다 — 봉투는 답을 주지 않는다.\n\n%s\n\n규칙 파일이 런 "
            "중에 바뀌었으면 바뀐 것을 안 본 제출이다 — 다시 읽고 다시 낸다."
            % "\n".join("- `%s` · `%s` — %s" % (r, p, why) for r, p, why in bad_rules),
            _same_command(s, "03"))
    if not _roles_for(phase_item["front"], ctx, s):
        # 역할 0명은 이 페이즈가 적용한 양보다 — miss 검사보다 **먼저** 적어야
        # 빗나갔을 때 gap 이름에 들어간다.
        _note_applied(s, "03:roles=0")
    # docs 레인인데 역할 소유 경로가 바뀌었다 — 계약이 없어 위 재판정이 못
    # 잡는다. 여기서 안 잡으면 `clean_ownership` 이 orphan exit 8 을 내고
    # 메인은 역할 없이 고칠 길이 없다 (ADR-H044).
    if _docs_lane_source_check(root, paths, s, ctx, "03-implement"):
        st.set_phase_status(s, "03-implement", "running")
        st.save(paths, s)
        return st.envelope(
            "record", False, 3, s,
            {"profile": s.get("profile"), "contract": s.get("contract")},
            "## docs 예측이 빗나갔다\n\n00 은 문서만 바뀐다고 예측했는데 역할 "
            "소유 경로가 바뀌었다. 프로파일을 `normal` 로 올렸고 `triage_miss` "
            "가 gap 으로 남았다 — 01 리뷰어·02·역할을 건너뛴 채 여기까지 왔기 "
            "때문이다.\n\n계약 파일을 쓰고 `next` 로 역할 패킷을 받는다. "
            "이미 고친 소스는 그 역할이 claim 한다." + _journey_hint(root, s),
            "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])

    bad = _dispatch_problem(root, ctx, s, phase_item["front"], claims)
    if bad is not None:
        st.set_phase_status(s, "03-implement", "failed")
        st.append_event(paths, "check_fail", cmd="record", phase="03-implement",
                        **bad["data"])
        st.save(paths, s)
        return st.envelope("record", False, 8, s, bad["data"], bad["render"],
                           bad["next"] or _same_command(s, "03"))

    got = attribution.clean_ownership(root, ctx["config"], claims)
    if not got["ok"]:
        st.set_phase_status(s, "03-implement", "failed")
        st.append_event(paths, "check_fail", cmd="record", phase="03-implement",
                        findings=len(got["findings"]))
        st.save(paths, s)
        return st.envelope(
            "record", False, 8, s,
            {"findings": got["findings"], "rollback": got["rollback"]},
            "## 소유 경계 위반\n\n%s\n\n되돌릴 것:\n%s"
            % ("\n".join("- `%s` — %s" % (f["path"], f["message"])
                         for f in got["findings"]),
               "\n".join("- `%s` → %s" % (r["path"], r["by"])
                         for r in got["rollback"]) or "- (없음)"),
            _same_command(s, "03"))

    _config, adapter, calibration = adapters.load(root)
    if adapters.stage_state(adapter, "compile") == "present":
        log = paths.gates / "03_compile.log"
        result = adapters.run_stage(root, adapter, "compile", log_path=log,
                                    calibration=calibration)
        st.append_event(paths, "stage_done", cmd="record", phase="03-implement",
                        stage="compile", exit=result.get("exit"))
        if result.get("exit"):
            st.set_phase_status(s, "03-implement", "failed")
            st.save(paths, s)
            return st.envelope(
                "record", False, 4, s, {"stage": result},
                "## 컴파일 실패\n\n```\n%s\n```" % (result.get("output") or "")[:2000],
                _same_command(s, "03"))

    req = _tests_required(root, s, ctx, adapter)
    if req and req["findings"]:
        # **05 는 이것을 고치게 하지 못한다** (ADR-H058 결정 7). 계약 대조의
        # Major 는 원장에 `deferred` 로 쌓일 뿐 수리 루프를 돌리지 않는다 —
        # 워커 맥락이 살아 있는 여기서 요구한다.
        st.set_phase_status(s, "03-implement", "failed")
        st.append_event(paths, "check_fail", cmd="record", phase="03-implement",
                        tests_required=[f["code"] for f in req["findings"]])
        st.save(paths, s)
        return st.envelope(
            "record", False, 8, s, {"tests_required": req["findings"]},
            "## 게이트가 세는 테스트가 없다\n\n%s\n\n패킷의 「게이트가 세는 "
            "테스트」 목록이다. 해당 역할이 테스트를 더하고 같은 명령을 다시 친다. "
            "계약이 틀렸다고 판단되면 `CONTRACT_DEFECT` 로 보고한다.\n\n"
            "**테스트는 있는데 못 찾은 것이면 틀린 지적이다** — 어댑터 "
            "`attribution.import_aliases`(import 경로 별칭) · `authz_denied_pattern`"
            "(거부 단언 모양)을 고친다. 테스트에 이름만 적어 통과시키지 마라."
            % _findings_lines(req["findings"]),
            _same_command(s, "03"))

    st.set_phase_status(s, "03-implement", "passed",
                        claims=file.name)
    return _advance_to_next(root, paths, s, phase_item, ctx)


def _dispatch_problem(root, ctx, s, front, claims):
    """03 제출이 **디스패치된 역할 전부의** 것인가 (ADR-H057). 문제가 없으면 None.

    필터를 다시 계산해 패킷을 낼 때 저장한 값과 대조한다 — 다르면 패킷 뒤에
    메인이 계약의 조건부 절을 고친 것이고, 그 제출은 옛 패킷을 따른 것이다.
    저장값이 없는 옛 런은 다시 계산한 값을 쓰고 그 사실을 state 에 남긴다.
    반환: {"data", "render", "next"}
    """
    ids, _omitted = _dispatched_roles(root, ctx, s, front)
    node = s.setdefault("phases", {}).setdefault("03-implement", {})
    stored = node.get("dispatched_roles")
    if stored is None:
        node["dispatched_roles"] = ids
        node["dispatch_record"] = "recomputed_at_record"
    elif stored != ids:
        return {"data": {"dispatch": "contract_changed", "packet": stored,
                         "contract": ids},
                "render": "## 계약이 바뀌었다 — `next` 로 패킷을 다시 받아라\n\n패킷은 "
                          "%s 를 불렀는데 지금 계약으로는 %s 다. 패킷을 낸 뒤 계약의 "
                          "조건부 절(예: 화면)을 고쳤다 — 그 제출은 옛 패킷을 따른 것이다."
                          % (" · ".join("`%s`" % i for i in stored) or "(없음)",
                             " · ".join("`%s`" % i for i in ids) or "(없음)"),
                "next": "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"]}
    got = [r.get("role") for r in (claims or {}).get("roles") or []]
    missing = [i for i in ids if i not in got]
    extra = [g for g in got if g not in ids]
    if not missing and not extra:
        return None
    lines = ["- `%s` — 디스패치됐는데 claims 에 없다" % i for i in missing]
    lines += ["- `%s` — 이 런에 부르지 않은 역할인데 claims 에 있다" % g for g in extra]
    return {"data": {"dispatch": "claims_mismatch", "missing": missing, "extra": extra},
            "render": "## claims 의 역할이 디스패치 목록과 다르다\n\n%s\n\n`03_claims.json` "
                      "의 역할은 패킷의 「이 런에 부르는 역할」과 정확히 같아야 한다 "
                      "(ADR-H057). 빠진 역할을 불러 제출을 합치거나, 부르지 않은 역할의 "
                      "변경을 되돌린다." % "\n".join(lines),
            "next": None}


def _tests_required(root, s, ctx, adapter):
    """03 제출의 테스트 존재 검사. 계약이 없는 런은 None."""
    import trace_contract

    if ((s.get("contract") or {}).get("mode")) == "no_contract":
        return None
    full = Path(root) / resolve("${run.contract_file}", ctx)
    if not full.exists():
        return None
    return trace_contract.required_tests(root, ctx["config"], adapter, full)


def _findings_lines(findings):
    return "\n".join("- `%s` → **%s**: %s" % (f["code"], f["target_role"], f["title"])
                     for f in findings)


def _rules_read_expected(root, config):
    """워커가 읽었어야 할 규칙 파일 → 현재 sha256 (ADR-H055).

    `config.project.instruction_file` 과 `rules_dir` **직속** `*.md` 다. 재귀가
    아니다 — `docs/harness/**` 는 ADR 2600줄·원장·런 보고서이고 그것을 읽으라는
    뜻이 아니다. 없는 파일은 항목을 만들지 않는다.
    """
    root = Path(root)
    proj = config.get("project") or {}
    out = {}
    inst = proj.get("instruction_file")
    if inst:
        sha = st._sha256_file(root / inst)
        if sha:
            out[Path(inst).as_posix()] = sha
    rules_dir = proj.get("rules_dir")
    if rules_dir and (root / rules_dir).is_dir():
        for p in sorted((root / rules_dir).glob("*.md")):
            sha = st._sha256_file(p)
            if sha:
                out[p.relative_to(root).as_posix()] = sha
    return out


def _check_rules_read(claims, expected):
    """[(role, path, 상태)] — 비어 있으면 통과. 역할 0명(docs 레인)은 대상이 없다."""
    bad = []
    for role in (claims or {}).get("roles") or []:
        name = role.get("role") or role.get("agent") or "?"
        got = role.get("rules_read")
        if not isinstance(got, list):
            for p in sorted(expected):
                bad.append((name, p, "rules_read 없음"))
            continue
        seen = {}
        for item in got:
            if isinstance(item, dict) and item.get("path"):
                seen[str(item["path"]).replace("\\", "/")] = item.get("sha256")
        for p in sorted(expected):
            if p not in seen:
                bad.append((name, p, "누락"))
            elif seen[p] != expected[p]:
                bad.append((name, p, "불일치 — 지금 파일과 해시가 다르다"))
    return bad


def _contract_units_zero(root, s, ctx):
    """계약 파일이 있고 `no_contract` 가 아닌데 유닛이 0 이면 그 사실. 아니면 None."""
    import contract as contract_mod

    if ((s.get("contract") or {}).get("mode")) == "no_contract":
        return None
    rel = resolve("${run.contract_file}", ctx)
    full = Path(root) / rel
    if not full.exists():
        return None
    parsed = contract_mod.parse(full.read_text(encoding="utf-8"), ctx["config"])
    if parsed.get("units"):
        return None
    sections = (ctx["config"].get("contract") or {}).get("sections") or {}
    return {"units": 0, "path": rel, "section": sections.get("units"),
            "dropped": parsed.get("dropped") or []}


def _refresh_profile(root, paths, s, ctx):
    """계약이 바뀌었으면 프로파일을 **다시** 센다. 반환: 바뀐 내용 dict.

    예전에는 `_confirm_profile` 을 부르는 자리가 `_record_03` 하나뿐이었다.
    `run_record` 의 멱등 가드가 통과한 03 의 재제출을 막으므로, 04 수리 중
    메인이 계약 델타를 적용해도 프로파일은 그대로 굳었다 — P3 에서 유닛이
    2 → 5 가 됐는데 `small` 이 남아 05 의 리뷰어가 1명이 됐다 (M34).

    변화를 감지할 재료는 이미 있었다. `contract.sha256` 을 적어 두고 **읽는
    쪽이 없었다.** 여기서 그것을 읽는다.

    `dropped` 도 함께 드러낸다. 계약 파서가 `컨테이너 · 심볼` 쌍이 아닌 줄을
    유닛으로 안 세는데, 그 사실을 읽는 곳이 doctor 의 **템플릿** 검사뿐이라
    실제 계약이 유닛 셋을 흘렸을 때 아무도 말하지 않았다 (P3 의 델타 D-2).
    """
    import contract as contract_mod

    contract_path = resolve("${run.contract_file}", ctx)
    full = root / contract_path
    if not full.exists():
        return {"changed": False, "dropped": []}

    sha = _sha256(full)
    node = dict(s.get("contract") or {}, present=True, path=contract_path)
    same = node.get("sha256") == sha
    node["sha256"] = sha

    parsed = contract_mod.parse(full.read_text(encoding="utf-8"), ctx["config"])
    node["dropped"] = parsed.get("dropped") or []
    s["contract"] = node
    if same:
        # **매번 다시 세지 않는다.** 같은 계약을 재판정하면 판정이 흔들리고
        # `previous` 가 뜻 없는 값으로 채워진다.
        return {"changed": False, "dropped": node["dropped"]}

    before = dict(s.get("profile") or {})
    after = _confirm_profile(s, ctx["config"], parsed)
    changed = after.get("name") != before.get("name")
    if after is not s.get("profile"):
        # 00 의 예측·신호·적용 양보는 재판정 뒤에도 남는다 — 그것이 임계값을
        # 고칠 근거다. 이름이 같으면 예측이 맞은 것이고 출처도 그대로다.
        for k in ("predicted", "signals", "applied", "triage_miss"):
            if k in before and k not in after:
                after[k] = before[k]
        if not changed and before.get("source") == "triage":
            after["source"] = "triage"
            after["confirmed_at"] = s.get("phase")
    if changed:
        after = dict(after, previous={k: v for k, v in before.items()
                                      if k not in ("previous", "triage_miss")},
                     reconfirmed_at=st.stamp())
        if paths is not None:
            # `now` 는 `append_event` 의 타임스탬프 인자다 — 겹치면 안 된다.
            st.append_event(paths, "profile_reconfirmed", cmd="next",
                            was=before.get("name"), became=after.get("name"),
                            units=after.get("units"))
        if before.get("source") == "triage" and paths is not None:
            import triage
            if (triage.RANK.get(after.get("name"), 9)
                    > triage.RANK.get(before.get("name"), 9)):
                # **상향만 miss 다.** 하향은 관측을 더 한 것이라 비용만 더
                # 쓴 것이고, `previous` 가 그것을 말한다.
                after["triage_miss"] = _note_triage_miss(
                    paths, s, before, after, s.get("phase"))
    s["profile"] = after
    return {"changed": changed, "previous": before if changed else None,
            "dropped": node["dropped"]}


def _confirm_profile(s, config, parsed):
    """계약이 생겼으니 프로파일을 실제로 센다. 화면도 작업량이다 (ADR-H057)."""
    n = (len(parsed.get("units") or []) + len(parsed.get("entrypoints") or [])
         + len(parsed.get("screens") or []))
    limit = (config.get("profile") or {}).get("small_max_units") or 3
    if (s.get("profile") or {}).get("source") == "user":
        return s["profile"]
    return {"name": "small" if n <= limit else "normal", "source": "auto",
            "units": len(parsed.get("units") or []),
            "entrypoints": len(parsed.get("entrypoints") or [])}


def _sha256(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------- 05 제출 처리

def _record_05(root, paths, s, phase_item, ctx, file, reviewer, round_):
    """리뷰어 제출 하나를 받는다. 전원이 모이면 병합하고 원장에 쌓는다.

    **findings 개수와 "리뷰가 수행됐는가"를 분리한다** — 리뷰어가 전부 실패해도
    findings 는 0건이고, 그 0을 "지적이 없다"로 읽으면 아무도 보지 않은 코드가
    통과한다 (§E1).
    """
    import ledger
    import review as review_mod

    if not reviewer:
        return st.envelope(
            "record", False, 2, s, {},
            "05 는 리뷰어별 제출이다. `--reviewer <code>` 를 붙인다.\n"
            "계약 대조는 `contract-trace` 가 따로 낸다.", None)
    if not file.exists():
        return st.envelope("record", False, 3, s, {}, "산출물이 없다: %s" % file, None)
    try:
        payload = harness._read_json(file)
    except (OSError, ValueError) as exc:
        return st.envelope("record", False, 8, s, {},
                           "JSON 을 읽지 못했다: %s" % exc, None)

    raw_path = file.with_name(file.name.replace(".json", ".raw.md"))
    if not raw_path.exists():
        return st.envelope("record", False, 8, s, {},
                           "리뷰어 원문이 없다: %s — 구조화 JSON 만으로는 "
                           "quote 를 검증할 수 없다" % raw_path.name, None)
    raw_text = raw_path.read_text(encoding="utf-8")

    node = s.setdefault("phases", {}).setdefault("05-code-review", {})
    if not (node.get("trace") or {}):
        return st.envelope(
            "record", False, 3, s, {},
            "계약 대조가 먼저다. `contract-trace --run-id %s` 를 돌린다 — "
            "무료이고, 여기서 잡히는 것을 리뷰어에게 보내면 리뷰어가 같은 것을 "
            "다시 발견하는 데 돈을 쓴다." % s["run_id"],
            "python scripts/pipeline/cli.py contract-trace --run-id %s" % s["run_id"])

    round_ = round_ or 1
    guard = _planned_guard(s, node, reviewer, round_)
    if guard is not None:
        return guard
    stale = _dispatch_fingerprint_stale(root, ctx, node, round_)
    if stale is not None:
        return st.envelope(
            "record", False, 3, s, {"round": round_, "dispatched_fp": stale},
            "## 리뷰 대상 코드가 리뷰 뒤에 바뀌었다\n\n"
            "라운드 %d 의 리뷰어를 지시한 시점(`next`)의 지문과 지금 소유 범위 "
            "파일의 지문이 다르다. 이 제출은 **바뀌기 전 코드**를 본 리뷰다.\n\n"
            "- 지적을 이미 고쳤다면: 순서가 틀렸다. `review_repair` 는 record "
            "시점에 소모되므로, 코드를 먼저 고치고 record 하면 이미 해소된 지적에 "
            "카운터가 탄다 (ADR-H046). 고친 것을 되돌릴 필요는 없다 — 리뷰 결과를 "
            "그대로 두고 **다음 라운드**로 델타 재리뷰를 받는다: 이 파일을 지우지 "
            "말고, `next --run-id %s` 를 다시 쳐 라운드 %d 의 지시(지문 포함)를 "
            "갱신한 뒤 record 한다.\n"
            "- 리뷰 전에 선수리(contract-trace Critical)를 했다면: `next` 를 다시 "
            "쳐 리뷰 입력(인라인 diff·지문)을 갱신하고 그 diff 로 리뷰어를 부른다."
            % (round_, s["run_id"], round_),
            "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])
    planned = _planned_for_round(node, round_)

    rounds = node.setdefault("rounds", {})
    prev_open = _previous_open(rounds, round_, reviewer)
    excluded = ledger.excluded_categories(root)
    got = review_mod.check(root, ctx["config"], payload, raw_text, prev_open,
                           excluded=excluded, known=ledger.categories(root))
    if not got["ok"]:
        st.append_event(paths, "check_fail", cmd="record", phase="05-code-review",
                        reviewer=reviewer, errors=len(got["errors"]))
        tries = node.setdefault("attempts", {}).setdefault(str(round_), {})
        tries[reviewer] = (tries.get(reviewer) or 0) + 1
        st.save(paths, s)
        if tries[reviewer] < REVIEW_SUBMIT_TRIES:
            return st.envelope("record", False, 8, s,
                               {"errors": got["errors"], "attempt": tries[reviewer]},
                               "## 리뷰 제출 거부 (%d/%d)\n\n"
                               % (tries[reviewer], REVIEW_SUBMIT_TRIES) +
                               "\n".join("- %s" % e for e in got["errors"]),
                               _same_command(s, "05"))
        # **2회 실패는 오류가 아니라 데이터다** (페이즈 파일 "재제출 1회 →
        # 2회 실패 시 스킵 + degrade"). exit 8 로 계속 튕기면 그 리뷰어가
        # 영원히 슬롯에 못 들어가고, 결손이 등급에 드러날 자리가 없어진다.
        return _record_05_failure_slot(
            root, paths, s, phase_item, ctx, node, reviewer, round_,
            "제출이 %d회 규약을 어겼다: %s"
            % (tries[reviewer], "; ".join(got["errors"][:3])),
            errors=got["errors"])

    slot = rounds.setdefault(str(round_), {})
    slot[reviewer] = {"mode": payload.get("mode") or "primary",
                      "keys": got["keys"], "blocking": got["blocking"],
                      "closed": got["closed"], "findings": got["findings"],
                      "dropped_by_enforcement": got["dropped_by_enforcement"],
                      "truncated": got["truncated"],
                      "need_more_context": payload.get("need_more_context") or []}
    # 자진신고(선택). 지시 키와 같은 모양으로 남겨 08 이 나란히 놓는다 (ADR-H052).
    if payload.get("model_used"):
        slot[reviewer]["model_used"] = payload["model_used"]
        st.note_model_reported(s, "05:r%d:%s" % (round_, reviewer),
                               payload["model_used"])
    st.save(paths, s)

    missing = [c for c in planned if c not in slot]
    if missing:
        return st.envelope("record", True, 0, s,
                           {"round": round_, "waiting_for": missing},
                           "`%s` 리뷰를 받았다. 아직 `%s` 가 남았다."
                           % (reviewer, "`, `".join(missing)),
                           "python scripts/pipeline/cli.py record --phase 05 "
                           "--file <리뷰 json> --reviewer %s --round %d --run-id %s"
                           % (missing[0], round_, s["run_id"]))

    return _judge_05(root, paths, s, phase_item, ctx, round_, slot, node)


# 규약 위반 제출을 몇 번까지 되돌려 보내는가. 페이즈 파일의 "재제출 1회 →
# 2회 실패 시 스킵 + degrade" 를 숫자로 옮긴 것이다.
REVIEW_SUBMIT_TRIES = 2


def _dispatch_fingerprint_stale(root, ctx, node, round_):
    """지시 시점 지문 vs 지금. 다르면 저장된 지문을, 같거나 없으면 None.

    옛 상태(지문을 안 남긴 런)는 검사하지 않는다 — 없는 것을 stale 로 읽으면
    이 변경 전 런이 전부 거부된다.
    """
    saved = (node.get("dispatched_fp") or {}).get(str(round_))
    if not saved:
        return None
    fresh = st.fingerprint(root, ctx["config"])
    if st.fingerprint_matches(saved, fresh):
        return None
    return saved


def _planned_for_round(node, round_):
    """이 라운드가 기다리는 리뷰어. 1라운드는 전원, 델타는 지목된 1명이다.

    **`or` 로 낙하하지 않는다** — 빈 리스트는 "모른다" 가 아니라 "라우팅이
    아무도 안 골랐다" 는 관측된 사실이고, 그것이 기본값에 삼켜지는 것이 G-4 다.
    """
    by_round = node.get("rounds_planned") or {}
    if str(round_) in by_round:
        return list(by_round[str(round_)])
    return list(node.get("planned") or [])


def _planned_guard(s, node, reviewer, round_):
    """제출자가 라우팅에 있는가. 없으면 봉투, 있으면 None."""
    if "planned" not in node:
        return st.envelope(
            "record", False, 3, s, {},
            "05 에 진입하지 않았다. `next` 가 리뷰어 라우팅을 확정한 뒤에만 "
            "제출을 받는다 — 그러지 않으면 **분모가 제출자에서 유도되어** "
            "누가 리뷰했는지가 리뷰한 사람의 주장이 된다.",
            "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])
    planned = _planned_for_round(node, round_)
    if reviewer not in planned:
        return st.envelope(
            "record", False, 8, s,
            {"planned": planned, "reviewer": reviewer, "round": round_},
            "## 라우팅이 부르지 않은 리뷰어다\n\n"
            "`%s` 는 이 라운드의 계획(%s)에 없다. 라우팅은 결정론이고 "
            "모델이 정하지 않는다 — 부르지 않은 리뷰어의 제출을 받으면 "
            "`escaped_05` 를 세는 것이 의미를 잃는다."
            % (reviewer, ", ".join("`%s`" % c for c in planned) or "없음"),
            None)
    return None


def _record_05_failed(root, paths, s, phase_item, ctx, reviewer, round_, reason):
    """`record --phase 05 --reviewer <code> --failed` — 호출 자체가 실패했다.

    **이 verb 는 자진 신고다.** 그래서 기계로 확인 가능한 만큼만 받는다 —
    그 라운드의 유효한 제출 파일이 실재하면 신고를 거부한다. verb 가 아예
    없으면 모델이 쓸 수 있는 유일한 표현이 "빈 유효 JSON" 이고, 그것은
    깨끗한 리뷰와 기계적으로 구분되지 않는다.
    """
    if not reviewer:
        return st.envelope("record", False, 2, s, {},
                           "`--failed` 는 어느 리뷰어인지 필요하다: `--reviewer`",
                           None)
    if not (reason or "").strip():
        return st.envelope("record", False, 2, s, {},
                           "`--reason` 이 필요하다. 사유 없는 실패는 원장에서 "
                           "인프라 실패와 미수행을 구분하지 못한다.", None)
    node = s.setdefault("phases", {}).setdefault("05-code-review", {})
    round_ = round_ or 1
    guard = _planned_guard(s, node, reviewer, round_)
    if guard is not None:
        return guard

    f = paths.run_dir / ("05_review_%s.json" % reviewer)
    if round_ > 1:
        f = paths.run_dir / ("05_review_%s_r%d.json" % (reviewer, round_))
    if f.exists():
        return st.envelope(
            "record", False, 8, s, {"submission": paths.rel(f)},
            "## 실패 신고를 받지 않는다\n\n"
            "`%s` 의 제출 파일이 실재한다(`%s`). 신고 대신 그 파일을 "
            "`record` 로 낸다 — **자진 신고 중 기계로 확인 가능한 것은 기계로 "
            "확인한다.**" % (reviewer, paths.rel(f)), None)

    return _record_05_failure_slot(root, paths, s, phase_item, ctx, node,
                                   reviewer, round_, reason)


def _record_05_failure_slot(root, paths, s, phase_item, ctx, node, reviewer,
                            round_, reason, errors=None):
    """실패를 슬롯에 **데이터로** 남기고 대기·판정 흐름을 잇는다."""
    rounds = node.setdefault("rounds", {})
    slot = rounds.setdefault(str(round_), {})
    slot[reviewer] = {"mode": "primary", "keys": None, "blocking": 0,
                      "closed": [], "findings": [], "status": "failed",
                      "reason": reason, "errors": list(errors or []),
                      "dropped_by_enforcement": 0, "truncated": False,
                      "need_more_context": []}
    st.save(paths, s)

    planned = _planned_for_round(node, round_)
    missing = [c for c in planned if c not in slot]
    if missing:
        return st.envelope(
            "record", True, 0, s,
            {"round": round_, "waiting_for": missing, "failed": reviewer},
            "`%s` 를 **실패**로 기록했다 (%s). 아직 `%s` 가 남았다.\n\n"
            "실패는 없던 일이 되지 않는다 — 등급과 보고서에 남는다."
            % (reviewer, reason, "`, `".join(missing)),
            "python scripts/pipeline/cli.py record --phase 05 "
            "--file <리뷰 json> --reviewer %s --round %d --run-id %s"
            % (missing[0], round_, s["run_id"]))
    return _judge_05(root, paths, s, phase_item, ctx, round_, slot, node)


def _judge_05(root, paths, s, phase_item, ctx, round_, slot, node):
    """전원이 모였다. 병합 → 원장 → 수리 판정."""
    import ledger
    import review as review_mod

    subs = [dict(v, reviewer=code) for code, v in slot.items()
            if v.get("keys") is not None]
    merged = review_mod.merge(subs)

    # **분모는 라우팅이고 분자는 그 분모를 순회해 센다.** `slot` 을 순회하면
    # 계획됐지만 아무 흔적도 남기지 않은 리뷰어가 분자에서 빠지지 않는다.
    planned = _planned_for_round(node, round_)
    ok_count = sum(1 for c in planned
                   if (slot.get(c) or {}).get("keys") is not None)
    _write_review05(s, node, planned, ok_count, merged, slot, round_=round_)

    # 원장에 쌓는다. **계약 대조의 결과도 함께 쌓는다** — 기계가 찾은 것과
    # 리뷰어가 찾은 것이 같은 눈금 위에 있어야 승격 집계가 성립한다.
    #
    # **한 런 안에서 같은 키는 한 번만 새 발생이다** (M30). 라운드마다 쌓으면
    # "몇 런이 이것을 봤나" 여야 할 `count` 가 "고치는 데 몇 라운드 걸렸나"로
    # 조용히 바뀌고, 한 런의 3라운드가 major 임계를 혼자 채운다.
    seen = node.setdefault("ledgered_keys", [])
    rows = []
    for f in merged:
        key = ledger.finding_key(f)
        if key in seen:
            continue
        seen.append(key)
        rows.append(dict(f, resolution=f.get("resolution") or "deferred",
                         source=f.get("source") or "reviewer"))
    trace_path = paths.run_dir / "05_trace.json"
    if trace_path.exists() and not node.get("trace_ledgered"):
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        for f in trace.get("findings") or []:
            key = ledger.finding_key(f)
            if key in seen:
                continue
            seen.append(key)
            rows.append(dict(f, resolution=f.get("resolution") or "deferred"))
        node["trace_ledgered"] = True
    try:
        ledger.append(root, s["run_id"], "05", rows)
    except ValueError as exc:
        # 어휘 밖의 category 는 조용히 버리지 않는다. 제출을 되돌린다.
        return st.envelope("record", False, 8, s, {"error": str(exc)},
                           "## 원장 어휘 밖\n\n%s" % exc, _same_command(s, "05"))

    # **닫힌 지적은 `repaired` 로 승계한다** (M29). 근거는 모델의 "고쳤다" 가
    # 아니라 `review.check` 의 단조성 검사가 이미 검증한 `closed` 다 — 이전
    # 라운드에 열려 있던 키가 이번에 `resolved_from_previous` 로 닫힌 것만
    # 여기 들어온다. 자진 신고를 받지 않고 기계가 확인한 것에서 유도한다.
    #
    # `repaired_by` 는 `state.repair`(§2.4 의 `by_main`/`by_agent`)에서 와야
    # 하는데 **실행기가 아직 그것을 쓰지 않는다.** 그래서 지금은 `null` 이다 —
    # 지어내지 않는다.
    closed = sorted({k for v in slot.values() for k in (v.get("closed") or [])})
    if closed:
        ledger.supersede(root, s["run_id"], "05", closed,
                         resolution="repaired",
                         repaired_by=(s.get("repair") or {}).get("by"))

    staged = ledger.stage_promotions(root)
    (paths.run_dir / "05_promo_staged.json").write_text(
        json.dumps(staged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (paths.run_dir / "05_review.json").write_text(
        json.dumps({"round": round_, "review05": s["review05"],
                    "findings": merged}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    st.save(paths, s)

    blocking = [f for f in merged if f.get("severity") in verdict.BLOCKING]
    if blocking:
        front = phase_item["front"]
        max_decl = _loop_max(front)
        # **재상정 승격은 지급이다** (ADR-H048). 같은 `finding_key` 가 이전
        # 라운드보다 높은 심각도로 다시 오면 리뷰어가 처음에 낮게 본 것이지
        # 수리자의 잘못이 아니다 — 그 비용을 수리자의 예산에서 빼지 않는다.
        # 런당 1회다. 새 키가 major 로 나는 것은 새 지적이라 지급이 아니다.
        raised = _severity_raised(node.get("rounds") or {}, round_, blocking)
        if raised and "severity_raised_grant" not in node:
            st.counter_grant(s, _loop_counter(front), 1, "severity_raised")
            st.append_event(paths, "counter_grant", cmd="record",
                            phase="05-code-review", counter=_loop_counter(front),
                            extra=1, reason="severity_raised")
            node["severity_raised_grant"] = {"round": round_, "keys": raised}
        used, _max, exceeded = st.counter_inc(s, _loop_counter(front), max_decl,
                                              "review_blocking", paths=paths)
        if exceeded:
            _loop_on_exceed(front)
            st.escalate(paths, s,
                        "05 의 Critical/Major %d건이 %d회 안에 해소되지 않았다"
                        % (len(blocking), max_decl),
                        ["계약 결함을 먼저 의심한다 — 같은 지적이 반복되면 "
                         "코드가 아니라 계약이 틀렸을 수 있다",
                         "이대로 진행한다(미해결 지적을 안고 간다)", "중단한다"],
                        phase="05-code-review")
            return _escalation_envelope("record", paths, s)
        delta = _delta_reviewer(blocking, planned, slot)
        node.setdefault("rounds_planned", {})[str(round_ + 1)] = [delta]
        # 다음 회차에 델타가 회계해야 할 목록이다. `record` 가 같은 인자로
        # 부르는 함수이므로 봉투와 검사가 같은 것을 본다 (M38).
        prev_open = _previous_open(node.get("rounds") or {}, round_ + 1, delta)
        # 수리 배정도 기동 지시다 — 04 와 대칭으로 작성자마다 센다 (ADR-H064).
        repair_keys = ["05:r%d:repair:%s" % (used, r) for r in
                       sorted({f.get("target_role") for f in blocking
                               if f.get("target_role")})]
        _instruct(s, "05-code-review", repair_keys, ctx)
        tiers = _model_tiers_render(ctx, s, repair_keys)
        st.save(paths, s)
        return st.envelope(
            "record", False, 4, s,
            {"blocking": len(blocking), "findings": blocking,
             "review05": s["review05"], "delta_reviewer": delta},
            _review_repair_render(blocking, used + 1, delta, prev_open,
                                  raised=raised if node.get(
                                      "severity_raised_grant", {}).get(
                                      "round") == round_ else None)
            + (("\n\n" + tiers) if tiers else ""),
            "python scripts/pipeline/cli.py gate --phase 04 --stage scoped "
            "--run-id %s" % s["run_id"])

    return _advance_to_next(root, paths, s, phase_item, ctx)


def _severity_raised(rounds, round_, blocking):
    """이전 라운드보다 심각도가 오른 blocking 지적의 `finding_key` 목록.

    비교 재료는 `rounds[r][code].keys[] = {key, severity}` 다 — 같은 라운드
    안의 2인 합치 상승(`review.merge` 의 `severity_raised_from`)은 대상이
    아니다. 그것은 라운드를 가로지른 재상정이 아니다.
    """
    import ledger
    best = {}
    for rn, subs in rounds.items():
        if int(rn) >= round_:
            continue
        for sub in subs.values():
            for k in sub.get("keys") or []:
                rank = ledger._SEVERITY_RANK.get(k.get("severity"), -1)
                if rank > best.get(k["key"], -1):
                    best[k["key"]] = rank
    out = []
    for f in blocking:
        key = ledger.finding_key(f)
        if key in best and \
                ledger._SEVERITY_RANK.get(f.get("severity"), -1) > best[key]:
            out.append(key)
    return out


def _delta_reviewer(blocking, planned, slot):
    """델타 재리뷰를 맡을 **한 명**. 결정론이다 — 모델이 고르지 않는다.

    막은 지적을 가장 많이 낸 리뷰어이고, 동률이면 `planned` 순서다. 모델이
    고르면 라우팅 결정론(§3.5)이 델타 라운드에서만 무너지고, 그러면
    `escaped_05` 가 라운드마다 다른 것을 센다.

    성공한 리뷰어만 후보다 — 실패한 리뷰어를 다시 지목하면 그 라운드가
    구조적으로 또 실패한다.
    """
    alive = [c for c in planned if (slot.get(c) or {}).get("keys") is not None]
    if not alive:
        alive = list(planned)
    scores = {}
    for f in blocking:
        for c in f.get("reported_by") or []:
            if c in alive:
                scores[c] = scores.get(c, 0) + 1
    return max(alive, key=lambda c: (scores.get(c, 0), -alive.index(c)))


def _review_repair_render(blocking, round_no, delta=None, previous_open=None,
                          raised=None):
    lines = ["## 수리가 필요하다 (%d회차)" % round_no, "",
             "Critical/Major %d건. **Minor 는 고치지 않는다** — 원장에 쌓이고 "
             "보고서로 간다." % len(blocking), ""]
    if raised:
        lines += ["이전 라운드의 지적 %d건이 더 높은 심각도로 재상정됐다 — "
                  "`review_repair` 를 **1 지급했다** (`severity_raised`, 런당 "
                  "1회). 리뷰어가 처음에 낮게 본 비용을 수리자의 예산에서 빼지 "
                  "않는다 (ADR-H048)." % len(raised), ""]
    if delta:
        lines += ["수리 뒤 **델타 재리뷰는 `%s` 한 명**이다. 전원을 다시 "
                  "부르지 않는다 — 그리고 그 한 명이 깨끗해도 앞선 라운드의 "
                  "`degraded`·`failed` 는 지워지지 않는다." % delta, ""]
    # **M38.** 수리 면제와 회계 면제는 다르다. `verdict.check_review` 는
    # 심각도를 가리지 않고 열린 지적 전부를 회계하라 요구하고, 하나라도 빠지면
    # "조용히 증발했다" 로 exit 8 을 낸다. 봉투가 그 의무를 안 적어 P5 가
    # 제출 1회를 여기서 잃었다.
    lines += ["**회계는 심각도와 무관하다.** 이전 회차에 열려 있던 지적은 "
              "**Minor 를 포함해 전부** 이번 제출에서 회계된다 — 같은 지적을 "
              "다시 내거나, `resolved_from_previous` 로 닫거나, "
              "`reraised_from_previous` 로 다시 올린다. "
              "**\"Minor 를 고치려 들지 마라\" 는 수리 금지이지 회계 면제가 "
              "아니다.** 빠지면 exit 8 이다.", ""]
    if previous_open:
        # 목록을 봉투가 직접 준다 — 모델이 재구성하면 그 재구성이 곧 결함이다.
        lines += ["열려 있는 이전 회차 지적 %d건:" % len(previous_open), ""]
        lines += ["- `%s` (`%s`, `%s`) — %s"
                  % (f.get("id"), f.get("severity"), f.get("reviewer"),
                     f.get("title_norm") or f.get("title") or "제목 없음")
                  for f in previous_open]
        lines += [""]
    for f in blocking:
        raised = (" *(2인 합치로 %s → %s)*"
                  % (f["severity_raised_from"], f["severity"])
                  if f.get("severity_raised_from") else "")
        lines.append("- **%s** → `%s`: %s%s"
                     % (f.get("severity"), f.get("target_role"),
                        f.get("title"), raised))
    contract_defect = [f for f in blocking
                       if f.get("category") == "CONTRACT_DEFECT"]
    if contract_defect:
        lines += ["", "**`CONTRACT_DEFECT` 가 있다.** 이것은 수리 대상이 아니라 "
                      "에스컬레이션이다 — 계약은 메인 단독 소유다."]
    lines += ["", "제출이 **내용은 그대로이고 회계 필드만** 틀려 exit 8 로 되돌아오면 "
                  "(`resolved_from_previous` · `reraised_from_previous`) 메인이 "
                  "`local_repair` 로 그 필드를 고쳐 재제출해도 된다 — quote·헤딩 수·"
                  "severity 는 여전히 손대지 않는다 (ADR-H052).",
              "", "고친 뒤 `gate --phase 04 --stage scoped` 로 재게이트하고, "
                  "델타 재리뷰 1명을 돌린 다음 다시 제출한다.",
              "**수리하면 지문이 바뀌어 영수증이 낡는다** — 06 이 자동으로 막으므로 "
              "재게이트를 잊을 수 없다."]
    return "\n".join(lines)


PR_CLOSED_STATES = ("closed", "merged")


def _record_07(root, paths, s, phase_item, ctx, file, reviewer, round_):
    """07 제출 — 외부·내장 리뷰를 받고 `escaped_05` 를 센다.

    **PR 이 닫혔거나 머지됐으면 아무것도 하지 않고 정상 종료한다** (§E8).
    이미 끝난 것을 수리하거나 머지된 코드에 코멘트를 다는 것은 소음이다.
    """
    import ledger
    import review as review_mod
    import review07 as rv7

    file = Path(file)
    if not file.exists():
        return st.envelope("record", False, 3, s, {},
                           "산출물이 없다: %s" % file, None)
    try:
        payload = harness._read_json(file)
    except (OSError, ValueError) as exc:
        return st.envelope("record", False, 8, s, {},
                           "JSON 을 읽지 못했다: %s" % exc, None)

    pr_state = (s.get("pr") or {}).get("state")
    if pr_state in PR_CLOSED_STATES:
        st.demote(s, st.GRADES[1], "pr_%s" % pr_state)
        s.setdefault("review07", {}).update(
            {"external": {"status": "skipped_pr_%s" % pr_state, "major": 0},
             "code_review": "skipped", "escaped_05": 0})
        st.save(paths, s)
        return _advance_to_next(root, paths, s, phase_item, ctx)

    # **외부 계수의 권위는 `review07` 의 재계수에 있다** — 봇이 요약하지 않은
    # 원본을 본 것은 그쪽뿐이다. 여기서 제출의 값을 쓰면 `normalize_external`
    # 이 불변식 8 을 지키려고 다시 센 것을 자진 신고가 이긴다 (G-6).
    decided = (s.get("review07") or {}).get("external")
    if not decided:
        return st.envelope(
            "record", False, 3, s, {},
            "`review07` 을 먼저 돌린다. 외부 리뷰의 상태와 Major 수는 **거기서 "
            "기계가 세고**, 여기서는 그 값을 쓴다 — 제출자가 신고한 숫자를 "
            "저장하면 확인 가능한 것을 확인하지 않은 것이 된다.",
            "python scripts/pipeline/cli.py review07 --run-id %s" % s["run_id"])

    errors = []
    ext = payload.get("external") or {}
    if ext:
        # 실으면 대조한다. 안 실으면 그냥 결정된 값을 쓴다.
        for key in ("status", "major"):
            if key in ext and ext[key] != decided.get(key):
                errors.append(
                    "`external.%s` 가 기계 계수와 다르다 — 제출 %r vs "
                    "`review07` %r. 이 값은 신고하는 것이 아니라 대조되는 것이다."
                    % (key, ext[key], decided.get(key)))
    if payload.get("code_review") not in rv7.EFFORTS:
        errors.append("`code_review` 는 %s 중 하나다 (받은 값: %r)"
                      % (" · ".join(rv7.EFFORTS), payload.get("code_review")))

    findings = payload.get("findings") or []
    known = ledger.categories(root)
    for f in findings:
        if f.get("source") not in ledger.SOURCES:
            errors.append("finding %s: `source` 가 어휘 밖이다 (%r) — %s"
                          % (f.get("id"), f.get("source"),
                             " · ".join(ledger.SOURCES)))
        if f.get("category") not in known:
            errors.append("finding %s: taxonomy 에 없는 category 다 (%r)"
                          % (f.get("id"), f.get("category")))
        else:
            # 05 와 **같은 층**에서 같은 대조를 한다 (ADR-H035). 07 을 빼면
            # 반사실의 `doc_contradicts_code` 후보 4건 중 2건이 사라진다.
            errors += review_mod.slug_errors(f, known[f["category"]])
        if f.get("severity") not in verdict.SEVERITIES:
            errors.append("finding %s: severity 가 어휘 밖이다 (%r)"
                          % (f.get("id"), f.get("severity")))

    # 가리킨 대상이 실재해야 선언이 대조 가능한 사실이 된다 (M48).
    errors += rv7.check_reraise(findings, _open_from_05(s))

    # "고쳤다" 는 git 으로 확인 가능하므로 확인한다 (M49 · 불변식 8).
    head_sha = (s.get("pr") or {}).get("head_sha")
    res_errors, res_unverified = rv7.check_resolution(root, findings, head_sha)
    errors += res_errors

    if payload.get("change_requested") and not findings:
        errors.append("`change_requested` 가 참인데 findings 가 비었다 — "
                      "무엇을 고치라는 것인지 없이 차단만 하는 제출이다.")
    if errors:
        return st.envelope("record", False, 8, s, {"errors": errors},
                           "\n".join(["## 제출이 규약을 어겼다", ""]
                                     + ["- %s" % e for e in errors]),
                           None)

    open05 = _open_from_05(s)
    got = rv7.escaped(root, findings, s["run_id"], previous_open=open05)
    # **dedup 은 버리는 것이 아니라 세는 것이다** — 여기서 처음 잡힌
    # Critical/Major 가 05 라우팅이 놓친 것이다.
    # **하드코딩된 `deferred` 를 걷었다** (M49). 고쳐진 지적이 `deferred` 로
    # 굳으면 `EXCLUDED_FROM_COUNT` 밖이라 "반복되는 미해결" 로 승격 집계에
    # 학습된다. 다만 대조가 불가능하면 주장을 받지 않고 갭으로 드러낸다 —
    # 확인할 수 없는 것을 확인한 것처럼 적지 않는다.
    if res_unverified:
        gap = "repair_unverified"
        if gap not in s.setdefault("gaps", []):
            s["gaps"].append(gap)
    rows = []
    for f in got["findings"]:
        res = f.get("resolution") or "deferred"
        if res == "repaired" and res_unverified:
            res = "deferred"
        rows.append(dict(f, resolution=res))
    ledger.append(root, s["run_id"], "07", rows)

    s.setdefault("review07", {}).update(
        {"external": dict(decided),
         "code_review": payload.get("code_review"),
         "escaped_05": got["escaped_05"],
         "deduped": got["deduped"],
         "human_comments": len(payload.get("human_comments") or [])})

    if payload.get("change_requested"):
        used, max_, exceeded = st.counter_inc(s,
                                              _loop_counter(phase_item["front"]),
                                              _loop_max(phase_item["front"]),
                                              "external_change_requested",
                                              paths=paths)
        st.save(paths, s)
        if exceeded:
            _loop_on_exceed(phase_item["front"])
            st.escalate(paths, s, "외부 변경 요청이 수리 예산 안에서 안 닫혔다",
                        options=["사람이 직접 수리한 뒤 재개", "PR 을 닫는다",
                                 "중단"], phase="07-pr-review")
            st.save(paths, s)
            return _escalation_envelope("record", paths, s)
        return st.envelope("record", False, 10, s,
                           {"findings": findings, "repair": used},
                           "\n".join([
                               "## 변경 요청이 열려 있다", "",
                               "**미해결로 07 을 끝낼 수 없다.** PR 체크가 "
                               "빨간불인데 파이프라인이 초록불인 척하게 된다.",
                               "", "수리 %d/%d 회차다." % (used, max_)]),
                           None)

    st.save(paths, s)
    return _advance_to_next(root, paths, s, phase_item, ctx)


PR_STATES = ("open", "closed", "merged")


def _record_06(root, paths, s, phase_item, ctx, file, reviewer, round_):
    """06 제출 — 메인 세션이 forge 도구로 만든 PR 의 결과를 받는다.

    검사 셋이 전부 "번호가 갈라지는 것" 을 막는다. 07 이 PR 하나를 보고
    수리·코멘트·등급을 정하므로, 어느 PR 인지 흔들리면 그 뒤가 전부 흔들린다.
    """
    if not (s.get("pr") or {}).get("pushed"):
        return st.envelope("record", False, 3, s, {},
                           "\n".join([
                               "## 아직 push 하지 않았다", "",
                               "`pr` 을 먼저 돌려 push 와 요청서를 만든다.",
                               "",
                               "python scripts/pipeline/cli.py pr --run-id %s"
                               % s["run_id"]]),
                           None)

    file = Path(file)
    if not file.exists():
        return st.envelope("record", False, 3, s, {},
                           "산출물이 없다: %s" % file, None)
    try:
        payload = harness._read_json(file)
    except (OSError, ValueError) as exc:
        return st.envelope("record", False, 8, s, {},
                           "JSON 을 읽지 못했다: %s" % exc, None)

    errors = []
    number = payload.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        errors.append("`number` 는 정수여야 한다 (받은 값: %r). 문자열 번호는 "
                      "비교가 문자열 비교가 되어 갱신 판정이 흔들린다." % (number,))
    state = payload.get("state")
    if state not in PR_STATES:
        errors.append("`state` 는 %s 중 하나다 (받은 값: %r)"
                      % (" · ".join(PR_STATES), state))
    prev = (s.get("pr") or {}).get("number")
    if prev is not None and number != prev:
        errors.append("PR 번호가 갈라졌다: 기록은 #%s 인데 제출은 #%s 다. "
                      "**갱신이어야 할 것을 새로 만들었다** — 07 이 어느 PR 을 "
                      "볼지 모르게 된다." % (prev, number))
    if errors:
        return st.envelope("record", False, 8, s, {"errors": errors},
                           "\n".join(["## 제출이 규약을 어겼다", ""]
                                     + ["- %s" % e for e in errors]),
                           None)

    s.setdefault("pr", {}).update({
        "number": number, "state": state,
        "url": payload.get("url"),
        "created_at": payload.get("created_at") or st.stamp(),
    })
    st.append_event(paths, "pr_opened", cmd="record", phase="06-pr",
                    number=number, state=state)
    st.save(paths, s)
    return _advance_to_next(root, paths, s, phase_item, ctx)


# 제출로 닫지 않는 페이즈. **각자의 동사가 닫는다** — "미구현" 이라고 말하면
# 실제로는 구현돼 있는데 없는 것처럼 읽힌다 (M24).
_CLOSED_BY = {"04-gate": "gate --phase 04",
              "08-report": "report --out <경로>"}

_RECORD_HANDLERS = {"00-triage": _record_00,
                    "01-plan": _record_01, "02-cross-verify": _record_02,
                    "03-implement": _record_03, "05-code-review": _record_05,
                    "06-pr": _record_06,
                    "07-pr-review": _record_07}


# ------------------------------------------------------------------------ gate

def cmd_gate(root, args):
    return st.emit(run_gate_cmd(root, args.phase, args.stage, args.replay,
                                args.run_id))


def run_gate_cmd(root, phase="04", only_stage=None, replay=None, run_id=None):
    try:
        return _run_gate_cmd(root, phase, only_stage, replay, run_id)
    except ConfigDeclarationError as exc:
        _paths, s = st.load(Path(root), run_id)
        return _declaration_envelope("gate", s, exc)


def _run_gate_cmd(root, phase="04", only_stage=None, replay=None, run_id=None):
    import gate as gate_mod

    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("gate", False, 3, None, {}, "런이 없다.", None)
    if s.get("escalated"):
        return _escalation_envelope("gate", paths, s)

    loaded, _broken = load_phases(root)
    pid = _normalize_phase(phase, loaded)
    if pid is None:
        return st.envelope("gate", False, 2, s, {}, "알 수 없는 페이즈: %r" % phase, None)
    phase_item = loaded[pid]
    ctx = build_context(root, paths, s)

    if not only_stage:
        checks = check_requires(root, phase_item["front"].get("requires"), ctx, s)
        failed = [c for c in checks if not c["ok"]]
        if failed:
            return st.envelope("gate", False, 3, s, {"requires_report": checks},
                               "## 게이트 진입 거부\n\n" +
                               "\n".join("- %s" % c["message"] for c in failed), None)

    config, adapter, calibration = adapters.load(root)
    # **수리 라운드마다 계약을 다시 읽는다.** 메인이 여기서 계약 델타를 적용하고,
    # 그 델타가 스코프 선택과 프로파일을 함께 바꾼다. 예전에는 스코프만 새 계약을
    # 보고 프로파일은 03 이 정한 값으로 굳어 있었다 (M34).
    if not only_stage:
        _refresh_profile(root, paths, s, ctx)
    round_no = ((s.get("counters") or {}).get("repair") or {}).get("used", 0) + 1
    log_path = paths.gates / ("gr-%d.stdout.log" % round_no)

    st.append_event(paths, "stage_start", cmd="gate", phase=pid, round=round_no)
    report = gate_mod.run_gate(root, config, adapter, calibration, s,
                               phase_item["front"], paths.run_dir,
                               only_stage=only_stage, replay=replay,
                               log_path=log_path)
    report["at"] = st.stamp()
    report["run_id"] = s["run_id"]
    report["round"] = round_no
    report["log"] = paths.rel(log_path)

    if only_stage:
        # 단일 스테이지는 카운터를 소모하지 않고 리포트를 덮어쓰지 않는다.
        # **`tests` 는 예외다** (M55). 수리 뒤 `--stage scoped` → 전체 회귀
        # 1회는 정본이 선언한 정상 경로인데(§3.5), 그 회귀의 값이 상태에 안
        # 실려 08 보고서·PR 체크리스트·세션 원장 셋이 **마지막 코드 상태가
        # 아닌 수**를 증언했다. 그 셋은 전부 `state.tests` 를 읽는다.
        #
        # `gaps` 는 여기서 안 싣는다 — 등급을 낮추는 입력이고, 이 경로는
        # 설계상 런을 판정하지 않는다. `tests` 는 등급이 아니라 "몇 개가
        # 돌았나" 라는 사실이라 성격이 다르다.
        #
        # `--stage scoped` 는 `_tests_signal` 이 `full` 미실행에 `None` 을
        # 내므로 아래 가드에 걸려 안 실린다 — scoped 의 수를 전체 회귀의
        # 수로 적으면 다음 런의 하한 대조가 무의미해진다.
        if report.get("tests"):
            s["tests"] = report["tests"]
            st.save(paths, s)
        stages = report["stages"]
        if not stages:
            # 예전에는 `{}` 가 "ran 이 아니다" 로 읽혀 **exit 0** 이었다 —
            # 오타 난 스테이지 이름이 초록불로 지나가는 경로다.
            return st.envelope("gate", False, 2, s, {"stage": {}, "stages": []},
                               "`--stage %s` 에 해당하는 스테이지가 %s 의 선언에 "
                               "없다. `loop` · 쉼표 목록 · 단일 id 중 하나다."
                               % (only_stage, pid), None)
        failed = [x for x in stages
                  if x.get("state") == "ran" and x.get("exit") != 0]
        ok = not failed
        # `stage` 는 옛 소비자용 단수 키 — 실패한 첫 스테이지, 없으면 마지막.
        stage = failed[0] if failed else stages[-1]
        return st.envelope("gate", ok, 0 if ok else 4, s,
                           {"stage": stage, "stages": stages},
                           "\n".join(_stage_render(x) for x in stages), None)

    _write_json(paths.run_dir / "04_gate_report.json", report)

    log_text = ""
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    dispatch = gate_mod.attribute(
        root, config, adapter, report, s, replay=replay, log_text=log_text,
        stuck_after=((phase_item["front"].get("loop") or {})
                     .get("stuck_after_identical") or 2))

    if report.get("tests"):
        s["tests"] = report["tests"]
    for gap in report.get("gaps") or []:
        if gap not in s.setdefault("gaps", []):
            s["gaps"].append(gap)

    if dispatch is None:
        shrank = (report.get("tests") or {}).get("status") == "shrank"
        if shrank:
            return _gate_fail(root, paths, s, phase_item, ctx, report,
                              {"owner": None, "stuck": False,
                               "reason": "테스트 수가 하한 아래로 떨어졌다"},
                              round_no)
        st.demote(s, report.get("grade") or st.GRADES[1])
        s["fingerprint"] = st.fingerprint(root, config)
        # 실패가 없어도 회차를 남긴다 — "귀속을 안 했다"와 "귀속할 실패가
        # 없었다"는 다른 사실이고, 빈 파일이 후자를 말한다.
        _write_attribution(paths, report,
                           {"by_owner": {}, "failures": [], "deferred": [],
                            "owner": None, "stuck": False}, round_no)
        st.append_event(paths, "stage_done", cmd="gate", phase=pid,
                        grade=s["grade"])
        st.save(paths, s)
        return _advance_to_next(root, paths, s, phase_item, ctx, cmd="gate")

    return _gate_fail(root, paths, s, phase_item, ctx, report, dispatch, round_no)


def _gate_fail(root, paths, s, phase_item, ctx, report, dispatch, round_no):
    import gate as gate_mod   # noqa: F401  — 대칭을 위해 남긴다

    _write_attribution(paths, report, dispatch, round_no)
    st.append_event(paths, "attribution", cmd="gate", phase="04-gate",
                    owner=dispatch.get("owner"), stuck=dispatch.get("stuck"))

    if dispatch.get("owner") == "infra":
        # **카운터를 소모하지 않는다.** 외부 의존 미기동이 구현 역할의 실패로
        # 오분류되면 예산을 태운다.
        st.escalate(paths, s,
                    "외부 의존 실패로 보인다 (패턴: %s)" % dispatch.get("infra"),
                    ["의존을 띄우고 `gate` 를 다시 돌린다",
                     "이 스테이지를 건너뛰고 진행한다(등급에 남는다)", "중단한다"],
                    phase="04-gate")
        return _escalation_envelope("gate", paths, s)

    if dispatch.get("stuck"):
        # **소유자를 이름으로 적는다.** "같은 실패가 두 번" 만으로는 누구에게
        # 두 번 보냈는지가 안 보이고, 그것이 다음 판단(계약을 고칠 것인가
        # 범위를 줄일 것인가)에 필요한 사실이다.
        st.escalate(paths, s,
                    "같은 실패를 같은 소유자(%s)에게 되풀이해 보냈다 — "
                    "예산이 남아도 멈춘다 (%s)"
                    % (dispatch.get("owner") or "?",
                       ", ".join(dispatch.get("pairs") or [])[:200]),
                    ["계약을 고쳐 다시 돌린다", "범위를 줄인다", "중단한다"],
                    phase="04-gate")
        return _escalation_envelope("gate", paths, s)

    used, max_, exceeded = st.counter_inc(s,
                                          _loop_counter(phase_item["front"]),
                                          _loop_max(phase_item["front"]),
                                          "gate_failure", paths=paths)
    # **쌍을 쌓는다** — `owner|sig`. 시그니처만 쌓으면 flip 이 배정한 다음 역할이
    # 지시를 받기 전에 정체 감지가 먼저 멈춘다 (M33).
    s.setdefault("sig_chain", []).extend(dispatch.get("pairs") or [])
    st.set_phase_status(s, "04-gate", "failed")
    st.save(paths, s)

    if exceeded:
        _loop_on_exceed(phase_item["front"])
        st.escalate(paths, s, "수리 예산 %d회를 소진했다" % max_,
                    ["계약을 고쳐 다시 돌린다", "범위를 줄인다", "중단한다"],
                    phase="04-gate")
        return st.envelope("gate", False, 5, s, {"report": report["gaps"]},
                           "## 예산 소진 — 에스컬레이션\n\n`ESCALATION.md` 를 본다.",
                           "python scripts/pipeline/cli.py resume --ack "
                           "--answer-file <경로>")

    brief = _dispatch_brief(dispatch, paths, round_no)
    # 수리 배정도 기동 지시다. 제출 기준에서는 03 의 재제출로만 잡혀
    # **어느 페이즈가 태웠는지가 04 에서 03 으로 옮겨 보였다.**
    repair_keys = ["04:r%d:%s" % (used, dispatch.get("owner"))]
    _instruct(s, "04-gate", repair_keys, ctx)
    st.append_event(paths, "dispatch", cmd="gate", phase="04-gate",
                    owner=dispatch.get("owner"))
    tiers = _model_tiers_render(ctx, s, repair_keys)
    return st.envelope("gate", False, 4, s,
                       {"repair_dispatch": brief, "gaps": report.get("gaps")},
                       _repair_render(dispatch, brief)
                       + (("\n\n" + tiers) if tiers else ""),
                       "python scripts/pipeline/cli.py gate --phase 04 --run-id %s"
                       % s["run_id"])


def _write_attribution(paths, report, dispatch, round_no):
    path = paths.run_dir / "attribution.json"
    data = {"schema": 1, "run_id": report.get("run_id"), "rounds": []}
    if path.exists():
        try:
            data = harness._read_json(path)
        except (OSError, ValueError):
            pass
    data.setdefault("rounds", []).append({
        "round": round_no,
        "stage": (report.get("failed") or {}).get("id"),
        "stage_exit": (report.get("failed") or {}).get("exit"),
        "failures": dispatch.get("failures") or [],
        "by_owner": {k: [f["id"] for f in v]
                     for k, v in (dispatch.get("by_owner") or {}).items()},
        "infra": dispatch.get("owner") == "infra",
        "deferred": dispatch.get("deferred") or [],
        "rules_inactive": report.get("rules_inactive") or [],
    })
    _write_json(path, data)
    _write_json(paths.gates / ("gr-%d.dispatch.json" % round_no), dispatch)


def _dispatch_brief(dispatch, paths, round_no):
    """봉투에는 **실패당 60줄 상한**의 브리프만. 전문은 파일에 있다."""
    owner = dispatch.get("owner")
    items = (dispatch.get("by_owner") or {}).get(owner) or []
    brief = []
    for f in items:
        lines = (f.get("message") or "").splitlines()[:60]
        brief.append({"id": f.get("id"), "unit": f.get("unit"),
                      "file": f.get("file"), "frames": f.get("frames"),
                      "lines": lines})
    return {"round": round_no, "owner": owner,
            "reason": (items[0].get("owner_reason") if items else None),
            "failure_count": len(items),
            "file": "gates/gr-%d.dispatch.json" % round_no,
            "brief": brief, "deferred": dispatch.get("deferred") or []}


def _repair_render(dispatch, brief):
    lines = ["## 04 게이트 실패 — 수리 지시", "",
             "**`%s` 에게만** 보낸다. 배정 근거: %s"
             % (brief["owner"], brief.get("reason") or "-"), ""]
    for f in brief["brief"]:
        lines.append("- `%s` %s" % (f["id"], f.get("unit") or ""))
        if f.get("file"):
            lines.append("  - 파일: `%s`" % f["file"])
        for l in (f.get("lines") or [])[:6]:
            lines.append("  - %s" % l)
    if brief.get("deferred"):
        lines += ["", "미룬 것 (동시 배정 금지):"]
        lines += ["- %s (%d건) — %s" % (d["owner"], d["failure_count"], d["reason"])
                  for d in brief["deferred"]]
    lines += ["", "**실패를 다시 분류하지 마라.** 배정은 끝났다.",
              "고친 뒤 `gate` 를 다시 돌린다."]
    return "\n".join(lines)


def _stage_render(stage):
    if stage.get("state") == "skipped":
        return "`%s` 는 %s — 없는 것이지 통과한 것이 아니다." % (stage["id"],
                                                                stage["reason"])
    return "`%s` exit %s (%ss)" % (stage["id"], stage.get("exit"), stage.get("sec"))


def _write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")


# ---------------------------------------------- advance · retry · escalate · resume

# -------------------------------------------------------------------- precheck

def cmd_precheck(root, args):
    return st.emit(run_precheck(root, args.scope, args.run_id,
                               getattr(args, "phase", "05")))


def run_precheck(root, scope="pr", run_id=None, phase="05"):
    """05 진입과 06 에서 각 1회, 그리고 **재개마다** 다시 돈다 (§E13).

    런 없이도 돈다 — 무료 검사의 요점이 "시작하기 전에 안다"이므로 런을
    만들어야만 부를 수 있으면 그 값이 절반이 된다.
    """
    import precheck as pc

    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is not None and s.get("escalated"):
        return _escalation_envelope("precheck", paths, s)

    pid = _PRECHECK_PHASE.get(str(phase), "05-code-review")
    got = pc.run(root, scope=scope)

    if s is not None:
        # 명세의 state 스키마가 `precheck.at_05` 와 `at_06` 을 나란히 둔다 —
        # 같은 검사가 두 시점에 돌고 **그 사이에 값이 변하기 때문**이다 (§E13).
        # 한 칸에 덮어쓰면 06 이 05 의 예산을 지우고, 무엇이 언제 참이었는지
        # 보고서가 말할 수 없게 된다.
        slot = {"files": got["budget"]["files"], "lines": got["budget"]["lines"],
                "base_behind": _base_behind(got), "infra": got["infra_failures"],
                "exit": got["exit"], "classification": got["classification"]}
        s.setdefault("precheck", {})["at_%s" % str(phase).zfill(2)[:2]] = slot
        s.setdefault("phases", {}).setdefault(pid, {})["precheck"] = {
            "exit": got["exit"], "classification": got["classification"],
            "budget": got["budget"]}
        # **면제된 프로브는 등급이 치른다** (M44 · §E9). 어휘는 이미 있었고
        # 소비자(`pr.build_body`·`report.GAP_REASONS`)도 있었는데 **쓰는 코드가
        # 없었다** — 선언만 있고 코드가 안 읽는 M36 과 같은 모양이다.
        # `NON_DEMOTING_GAPS`(calibration_stale) 는 이름만 남기고 등급은 그대로다
        # — 사람이 할 일이 밀렸다는 표시이지 이 런의 관측 결손이 아니다 (ADR-H047).
        import report as rep
        for gap in got.get("gaps") or []:
            st.demote(s, None if rep.is_non_demoting(gap) else st.GRADES[1], gap)
        st.append_event(paths, "check_fail" if got["exit"] else "stage_done",
                        cmd="precheck", phase=pid, exit=got["exit"])
        if got["exit"] == 9:
            # 예산·브랜치·divergence 는 사람이 판단한다 — 그 대기가 여기서
            # 시작된다 (ADR-H052). `check_fail` 은 "무엇이" 이고 이것은 "언제부터" 다.
            st.append_event(paths, "waiting_human", cmd="precheck", phase=pid,
                            reason="precheck_policy")
        st.save(paths, s)

        if got["exit"] == 10:
            # exit 10 은 **상태를 잠근다** (§2.3). 전에는 잠근다고 적어 두고
            # 실제로는 잠그지 않아, 인프라가 깨진 채로 다음 명령이 그냥 돌았다.
            st.escalate(paths, s, "인프라 선행 조건 실패 — precheck",
                        options=[c["message"] for c in got["checks"]
                                 if not c["ok"] and c["kind"] == "infra"],
                        phase=pid)
            st.save(paths, s)
            return _escalation_envelope("precheck", paths, s)

    return st.envelope("precheck", got["exit"] == 0, got["exit"], s, got,
                       _precheck_render(got),
                       None if got["exit"] else _precheck_next(pid, s))


_PRECHECK_PHASE = {"05": "05-code-review", "06": "06-pr"}


def _base_behind(got):
    """divergence 검사가 센 behind 수. 검사가 안 돌았으면 0 이 아니라 None 이다."""
    for c in got["checks"]:
        if c["name"] == "base":
            return c.get("behind")
    return None


def _precheck_next(pid, s):
    rid = " --run-id %s" % s["run_id"] if s else ""
    if pid == "06-pr":
        return None        # 06 의 exit 9 는 사람의 판단이다 — 다음 명령이 없다
    return "python scripts/pipeline/cli.py contract-trace" + rid


def _precheck_render(got):
    if got["exit"] == 0:
        lines = ["`precheck` 통과. 파일 %d · 줄 %d 로 예산 안이고 브랜치·base·"
                 "인프라가 전부 맞다."
                 % (got["budget"]["files"], got["budget"]["lines"])]
        # **면제를 조용히 넘기지 않는다** (M44). "전부 맞다" 로만 적으면
        # 면제가 통과와 구분되지 않는다.
        for gap in got.get("gaps") or []:
            if gap == "calibration_stale":
                stale = [c for c in got["checks"] if c["name"] == "캘리브레이션"]
                lines += ["", "**캘리브레이션이 낡았다** — %s. 등급은 그대로이고 "
                              "`calibration_stale` 로 보고서에 남는다. 다음 런 전에 "
                              "`python scripts/harness.py calibrate` 를 돌린다."
                          % (stale[0]["message"] if stale else gap)]
                continue
            lines += ["", "**면제된 프로브가 있다: `%s`.** 통과가 아니라 "
                          "미검증이다 — 등급이 `PASS_WITH_GAPS` 로 내려가고 "
                          "보고서·PR 본문에 이름으로 남는다." % gap]
        lines += ["계약 대조로 넘어간다."]
        return "\n".join(lines)
    bad = [c for c in got["checks"] if not c["ok"]]
    head = ("## 인프라 선행 조건이 안 맞는다" if got["exit"] == 10 else
            "## 사람의 판단이 필요하다")
    lines = [head, ""]
    lines += ["- **%s**: %s" % (c["name"], c["message"]) for c in bad]
    if got["exit"] == 10:
        lines += ["", "**카운터를 소모하지 않았다.** 코드가 아니라 환경의 문제이므로 "
                      "재시도 예산을 태우지 않는다.",
                  "이대로 회귀를 돌리면 전부 빨간불이 되고, 그것을 코드 문제로 "
                  "읽게 된다."]
    else:
        lines += ["", "**자동으로 쪼개거나 리베이스하지 않는다.** 무엇을 할지 "
                      "정하고 다시 부른다."]
    return "\n".join(lines)


# --------------------------------------------------------------------- approve

def cmd_approve(root, args):
    return st.emit(run_approve(root, args.phase, revoke=args.revoke,
                               auto=args.auto, run_id=args.run_id))


def run_approve(root, phase="06", revoke=False, auto=False, run_id=None):
    """승인을 **이벤트로 못박는다.** 종료 코드 **0 / 3**.

    승인 시점의 **지문과 등급을 함께** 기록하는 것이 요점이다. 지문이 없으면
    "무엇을 승인했는가"가 남지 않아, 승인 뒤에 코드가 바뀌어도 그 승인이
    계속 유효해 보인다. 06 이 push 직전에 이 지문을 다시 보고 어긋나면
    exit 6 으로 재승인을 요구한다.

    `--auto` 의 범위는 **push + PR 생성까지**다 (§3.6). 06 시점의 등급은 외부
    리뷰를 아직 못 본 "예상" 이므로, 07 의 코멘트 게시는 등급을 재확인한 뒤다.
    """
    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("approve", False, 3, None, {},
                           "런이 없다. `init --feature` 로 시작한다.", None)
    if s.get("escalated"):
        return _escalation_envelope("approve", paths, s)

    pid = _PRECHECK_PHASE.get(str(phase), "06-pr")
    prev = _APPROVE_REQUIRES.get(pid)
    if prev and st.phase_status(s, prev) != "passed":
        return st.envelope("approve", False, 3, s, {"requires": prev},
                           "\n".join(["## 아직 승인할 수 없다", "",
                                      "`%s` 가 통과하지 않았다." % prev]),
                           None)

    node = s.setdefault("approval", {}).setdefault(str(phase).zfill(2)[:2], {})
    if revoke:
        node.update({"granted": False, "revoked_at": st.stamp(),
                     "reason": "사용자 철회"})
        st.append_event(paths, "approval_revoked", cmd="approve", phase=pid)
        st.save(paths, s)
        return st.envelope("approve", True, 0, s, {"approval": node},
                           "승인을 철회했다. 다시 승인하기 전에는 push 하지 않는다.",
                           None)

    config, _adapter, _cal = adapters.load(root)
    node.update({
        "granted": True,
        "mode": "auto" if auto else "user",
        "fingerprint": st.fingerprint(root, config),
        # **범위를 좁게 못박는다.** 07 의 코멘트 게시도, 머지도 여기 없다.
        "scope": "push+pr",
        "grade_at_grant": s.get("grade"),
        "at": st.stamp(),
    })
    st.append_event(paths, "approved", cmd="approve", phase=pid,
                    mode=node["mode"], grade=node["grade_at_grant"])
    st.save(paths, s)
    return st.envelope("approve", True, 0, s, {"approval": node},
                       _approve_render(node, pid),
                       "python scripts/pipeline/cli.py next --run-id %s"
                       % s["run_id"])


# 승인은 그 앞 페이즈가 끝난 뒤에만 뜻이 있다. 07 은 `inherited:06` 이라
# 자기 승인을 따로 받지 않는다 — 그래서 여기 없다.
_APPROVE_REQUIRES = {"06-pr": "05-code-review"}


def _approve_render(node, pid):
    return "\n".join([
        "## 승인 기록됨 — %s" % pid,
        "",
        "- 방식: **%s**" % node["mode"],
        "- 범위: **%s** (07 코멘트 게시와 머지는 포함하지 않는다)" % node["scope"],
        "- 승인 시점 등급: %s" % (node["grade_at_grant"] or "미정"),
        "",
        "이 뒤에 소스가 바뀌면 지문이 어긋나 **승인이 자동으로 무효**가 된다.",
    ])


# ------------------------------------------------------------------------ cost

# 런 비용 귀속의 어휘. 원장 줄은 `run.run_id` 를 갖지만 그것만으로 합산하면
# 안 된다 — `session_log._latest_run` 이 **가장 최근 런 디렉터리를 무조건**
# 집으므로 런이 닫힌 뒤 시작한 세션도 그 id 를 단다 (M59).
COST_BASIS = ("touched", "latest_only")

# 이 기준이 틀리는 세 방향. `BUDGET_BLIND_SPOTS` 와 같은 자리다 —
# **양방향으로 틀리므로 "하한" 이라고 부르지 않는다.**
COST_BLIND_SPOTS = (
    "원장에 이 run_id 를 안 단 세션은 애초에 목록에 없다 — 그 런을 실제로 "
    "돌린 세션이라도 그렇다 (과소). unread 는 목록에 있는데 못 읽은 수뿐이다",
    "touched 세션도 런 밖 작업을 섞을 수 있다 (과다)",
    "cost-state 가 없는 세션은 합계에서 빠진다 (과소)",
    "hasUnknownModelCost 면 그 세션은 비용 대신 플래그만 적는다 (과소)",
)


def cmd_cost(root, args):
    return st.emit(run_cost(root, run_id=args.run_id))


def run_cost(root, run_id=None, transcript_root=None):
    """런 비용을 원장 + 트랜스크립트에서 **읽는 시점에** 집계한다. 0 / 3.

    **보고서에 넣지 않는다.** 08 은 그 세션 안에서 돌고 그 세션의 비용이
    보통 그 런에서 제일 큰데, `cost-state` 는 트랜스크립트의 마지막 줄로
    써져 그 시점에 아직 없다. 찍는 순간 **구조적으로 미완인 숫자가 영구
    기록에 굳는다.** 사람이 세션이 끝난 뒤 이 명령을 부른다.

    귀속 기준(`COST_BASIS`)은 **세션 창과 런 구간이 겹치는가**다. 세션 창은
    `[직전 원장 줄의 ts, 이 줄의 ts]`(앞이 없으면 열려 있다)이고 런 구간은
    `[created_at, updated_at]` 이다. **한 시점으로 보면 안 된다** — P8 은
    17:20 에 시작해 다음날 01:08 에 닫혔고 세션 둘이 걸쳐 있어서, `updated_at`
    만 보면 앞 세션이 통째로 빠진다.

    판정할 수 없으면 `basis` 키를 만들지 않는다 — `latest_only` 로 단정하면
    못 잰 것이 "무관하다" 는 주장으로 바뀐다.
    `commits_since.kind` 가 기준을 같은 줄에 적는 것과 같은 규율이다.
    """
    import runtime            # run_report 가 report 를 부르는 것과 같은 자리다

    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("cost", False, 3, None, {},
                           "런이 없다. `--run-id` 를 확인한다.", None)
    rid = s["run_id"]
    # 런의 **구간**이다. 한 시점(`updated_at`)만 보면 여러 세션에 걸친 런의
    # 앞 세션들이 전부 빠진다 — P8 은 17:20 에 시작해 다음날 01:08 에 닫혔고
    # 세션 둘이 걸쳐 있다.
    born = st._parse_stamp(s.get("created_at"))
    updated = st._parse_stamp(s.get("updated_at"))

    ledger_path = root / "docs" / "pipeline-ledger.jsonl"
    rows = []
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue

    sessions = []
    for idx, row in enumerate(rows):
        if ((row.get("run") or {}).get("run_id")) != rid:
            continue
        start = st._parse_stamp(rows[idx - 1].get("ts")) if idx else None
        end = st._parse_stamp(row.get("ts"))
        # 두 구간이 겹치면 그 세션은 런이 살아 있는 동안 돌았다. 판정은
        # `session_log` 가 쓰는 시점에 부르는 것과 **같은 함수**다 (M59).
        touched = st.session_touched_run(born, updated, start, end)
        basis = None if touched is None else (
            "touched" if touched else "latest_only")
        cell = {"session_id": row.get("session_id"), "ts": row.get("ts")}
        if basis:
            # 판정할 수 없으면 키를 만들지 않는다 — latest_only 로 단정하면
            # 못 잰 것이 "무관하다" 는 주장으로 바뀐다.
            cell["basis"] = basis
        sessions.append(cell)

    if not sessions:
        return st.envelope("cost", False, 3, s, {"run_id": rid},
                           "원장에 이 런의 세션이 없다.", None)

    totals = {}
    unread = 0
    for cell in sessions:
        if cell.get("basis") != "touched":
            continue
        got = runtime.read_cost_state(
            cell["session_id"], transcript_root=transcript_root)
        if not got:
            unread += 1
            continue
        cell["cost_usd"] = got.get("session_cost_usd")
        # 세션 누적 이름을 런 합계 이름으로 옮긴다. 둘은 다른 것이고,
        # 읽는 쪽이 `session_` 을 그대로 보면 런 총액을 한 세션의 값으로 읽는다.
        for src_key, dst_key in (("session_cost_usd", "cost_usd"),
                                 ("session_input_tokens", "input_tokens"),
                                 ("session_output_tokens", "output_tokens"),
                                 ("session_thinking_tokens", "thinking_tokens"),
                                 ("session_cache_read", "cache_read"),
                                 ("session_cache_write", "cache_write")):
            val = got.get(src_key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                totals[dst_key] = round(totals.get(dst_key, 0) + val, 4)
        if got.get("unknown_model_cost"):
            cell["unknown_model_cost"] = True

    data = {"run_id": rid, "basis": list(COST_BASIS),
            "blind_spots": list(COST_BLIND_SPOTS),
            "sessions": sessions, "unread_sessions": unread}
    data.update(totals)

    counted = [c for c in sessions if c.get("basis") == "touched"]
    excluded = [c for c in sessions if c.get("basis") == "latest_only"]
    lines = ["## 런 비용 — %s" % rid, ""]
    lines.append("합산 대상 세션 **%d** · 읽지 못한 세션 **%d**"
                 % (len(counted), unread))
    if "cost_usd" in data:
        lines.append("")
        lines.append("**$%.2f**" % data["cost_usd"])
    else:
        lines.append("")
        lines.append("**비용을 재지 못했다** — 읽은 세션이 없다. "
                     "0 으로 적지 않는다.")
    if excluded:
        lines += ["", "합산에서 뺀 세션 (`latest_only` — 이 런을 만진 적이 "
                      "없는데 원장이 최신 런 id 를 달았다):"]
        lines += ["- `%s` (%s)" % (c.get("session_id"), c.get("ts"))
                  for c in excluded]
    lines += ["", "기준: **touched** — 세션 창 `[직전 원장 줄의 ts, 이 줄의 "
                  "ts]` 과 런 구간 `[created_at, updated_at]` 이 겹치는 세션만 "
                  "센다. 한 시점으로 보면 여러 세션에 걸친 런의 앞 세션이 빠진다."]
    lines += ["- %s" % x for x in COST_BLIND_SPOTS]
    return st.envelope("cost", True, 0, s, data, "\n".join(lines), None)


# ---------------------------------------------------------------------- report

def cmd_report(root, args):
    return st.emit(run_report(root, args.out, args.run_id))


def _report_cost(root, s):
    """08 의 「비용(있으면)」 입력. **`run_cost` 를 부르는 유일한 자리다.**

    `run_cost` 가 값을 못 내면(exit ≠ 0 · `cost_usd` 없음 · 예외) None 이고
    보고서는 `미계측` 을 적는다. 세션 원장·`cmd_cost` 를 걷어낸 클론은 이
    함수 본문을 `return None` 으로 두면 된다 — 그것이 델타 병합의 전부다.
    """
    try:
        env = run_cost(root, run_id=s.get("run_id"))
    except (OSError, ValueError, KeyError, ImportError):
        return None
    if not env or env.get("exit") != 0:
        return None
    data = env.get("data") or {}
    if "cost_usd" not in data:
        return None
    return data


def run_report(root, out=None, run_id=None):
    """08 — 결정론 표 조립 + 필수 섹션 검사. 종료 코드 **0 / 3 / 6**.

    **보고서는 파이프라인을 실패시키지 않는다.** 섹션이 빠져도 산출하고
    원장에 기록만 한다 — 보고서가 런을 실패시키면 안 쓰는 것이 이득이 된다.
    """
    import report as rep

    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("report", False, 3, None, {},
                           "런이 없다. `init --feature` 로 시작한다.", None)

    if s.get("grade") == st.GRADES[2]:
        # 에스컬레이션으로 멈춘 런은 ESCALATION.md 가 보고서다. 그 위에
        # 성공한 것 같은 문서를 얹지 않는다 (§E12).
        return st.envelope("report", False, 3, s, {"grade": s.get("grade")},
                           "\n".join([
                               "## 08 을 돌리지 않는다", "",
                               "등급이 `INCOMPLETE` 다 — `ESCALATION.md` 가 "
                               "보고서를 겸한다.",
                               "", "`%s`" % paths.rel(paths.escalation)]),
                           None)

    staged = [p for p in (s.get("promotions") or [])
              if p.get("status") == "staged"]
    if staged:
        return st.envelope("report", False, 6, s, {"staged": staged},
                           "\n".join([
                               "## 승격이 종결되지 않았다", "",
                               "`staged` 가 %d 건 남아 있다. 08 은 미종결을 "
                               "통과시키지 않는다." % len(staged), "",
                               "python scripts/pipeline/cli.py promote --flush "
                               "--run-id %s" % s["run_id"]]),
                           "python scripts/pipeline/cli.py promote --flush "
                           "--run-id %s" % s["run_id"])

    # **지시문 검토는 보고서보다 먼저다** (ADR-H056). 열린 런은 검토 파일이
    # 없거나 자진신고가 원장·diff 와 어긋나면 보고서를 쓰지 않고 되묻는다 —
    # 80자 되묻기와 같이 파일을 고쳐 같은 명령을 치면 닫힌다. 닫힌 런의
    # 재작성은 종전 계약대로 파일을 요구하지 않는다(있으면 멱등 은퇴만).
    import ledger as ledger_mod
    _config, _adapter, cal = adapters.load(root)
    review, errors = _instruction_review(root, paths, s, _config)
    closed = s.get("run_status") == st.DONE
    if errors and not closed:
        st.append_event(paths, "check_fail", cmd="report", phase="08-report",
                        instruction_review=errors)
        return st.envelope(
            "report", False, 8, s, {"instruction_review_errors": errors},
            "\n".join(["## 지시문 검토가 없거나 맞지 않는다", ""]
                      + ["- %s" % e for e in errors]
                      + ["", "`%s` 를 쓴다 — 스킬 `%s` 로 prose 후보와 이 런의 "
                             "「배운 점」을 검토한 뒤 결과를 옮겨 적는다. 바꾼 것 "
                             "없음도 유효하다(`changes: []`). 보고서는 아직 안 "
                             "썼고 런도 닫지 않았다. 같은 명령으로 다시 낸다."
                         % (paths.rel(paths.run_dir / INSTRUCTION_REVIEW_FILE),
                            _review_skill(_config))]),
            "python scripts/pipeline/cli.py report --run-id %s" % s["run_id"])
    if review is not None and not errors:
        for key in review["absorbed"]:
            files = [c["file"] for c in review["changes"]
                     if key in (c.get("rule_keys") or [])]
            ledger_mod.retire(root, s["run_id"], key,
                              "absorbed:" + ",".join(files))
        if review.get("skill") is None:
            st.demote(s, None, "instruction_review_manual")
        s["instruction_review"] = {
            "skill": review.get("skill"), "absorbed": review["absorbed"],
            "declined": review["declined"], "changes": review["changes"]}
    slots = _instruction_slots(root, _config)
    if slots is not None:
        s["instruction_slots"] = slots
        if slots["used"] > slots["budget"]:
            st.demote(s, None, "instruction_slot_over_budget")
        elif slots["used"] == 0 and slots["prose_lines"]:
            st.demote(s, None, "instruction_slot_unmeasured")

    src = paths.run_dir / "08_report_data.json"
    if not src.exists():
        return st.envelope("report", False, 3, s, {},
                           "`08_report_data.json` 이 없다 — 08 의 입력은 이 "
                           "파일 하나뿐이다 (20KB 이하).", None)
    try:
        data = harness._read_json(src)
    except (OSError, ValueError) as exc:
        data = {}
        st.append_event(paths, "check_fail", cmd="report", phase="08-report",
                        error=str(exc))

    # **원장 축은 모델이 쓰는 서술이 아니라 기계 사실이다.** 08 의 입력 파일은
    # 서술 전용이므로 실행기가 여기서 붙인다 — "승격 목록이 원장에서 자동으로
    # 나온다, 네가 빠뜨릴 수 없다" 와 같은 규율이다 (M39).
    try:
        got = ledger_mod.stage_promotions(root)
        data["ledger"] = {"by_category": got["by_category"],
                          "verdict_deadline": got["verdict_deadline"],
                          "prose_candidates": got["prose_candidates"],
                          "trace_repeats": got["trace_repeats"],
                          "by_reporter": ledger_mod.by_reporter(root)}
    except (OSError, ValueError, KeyError):
        pass
    # `verify-adapter` 기준 충족도 기계 사실이다 — 보고서가 말하지 않으면
    # 기준을 넘은 뒤에도 `adapter_unverified` 가 영구 gap 으로 남는다.
    if not _adapter.get("verified"):
        data["adapter_verify"] = {
            "qualified": len(harness.qualified_runs(root, _config.get("adapter"))),
            "min_runs": harness.ADAPTER_VERIFY_MIN_RUNS}
    # 소요는 `events.jsonl` 의 유도값이고, 08 시점에 그 파일은 이미 완결이다
    # — 미완 구간이 없다. 비용은 그 반대라 **있으면** 적고 아니면 `미계측` 이다
    # (ADR-H032 · ADR-H052 결정 2).
    timing = st.phase_durations(paths)
    text, missing = rep.build(s, data, cal or {}, s.get("promotions") or [],
                              timing, cost=_report_cost(root, s))

    target = Path(out) if out else (
        root / "docs" / "harness" / "pipeline" / "runs"
        / ("%s.md" % s["run_id"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    # 같은 run_id 로 다시 쓰면 **덮어쓴다** — 최종본이 맞다.
    target.write_text(text, encoding="utf-8")

    if missing:
        st.append_event(paths, "check_fail", cmd="report", phase="08-report",
                        missing_sections=missing)
    st.save(paths, s)
    rel = str(target.relative_to(root)) if str(target).startswith(str(root)) \
        else str(target)
    short = rep.short_narrative(data)
    data = {"out": rel, "missing_sections": missing}

    # 이월 뷰와 파일럿 기록은 **보고서와 같은 시점**에 쓴다 (ADR-H051 · H052).
    # 둘 다 파생 파일이고 실패해도 보고서를 막지 않는다 — 못 쓴 사실만 적는다.
    try:
        data["deferred_md"] = ledger_mod.write_deferred(root).relative_to(
            root).as_posix()
    except (OSError, ValueError):
        data["deferred_md"] = None
    data["pilot_log"] = (rep.PILOT_LOG_REL
                         if rep.append_pilot_log(root, s, timing, rel) else None)

    # 이미 닫힌 런 — 파일만 다시 쓰고 **전이하지 않는다.** exit 11 은 전이의
    # 순간이므로 두 번 내면 재작성과 첫 종료가 원장에서 구분되지 않는다.
    if s.get("run_status") == st.DONE:
        return st.envelope("report", True, 0, s, dict(data, closed=True),
                           _report_render(rel, missing, s, data)
                           + "\n\n이 런은 이미 닫혀 있다. 보고서만 다시 썼다.",
                           None)

    # **서술이 짧으면 되묻는다** (ADR-H052 결정 5). 보고서 파일은 이미 썼다 —
    # 표는 실행기가 조립했으므로 사실은 남는다. 다만 런은 닫지 않는다: 「왜
    # 그랬는가」 없이 닫힌 런이 `5568` 이었다. 등급은 건드리지 않는다.
    if short:
        st.append_event(paths, "check_fail", cmd="report", phase="08-report",
                        short_narrative=[{"field": k, "chars": n} for k, n in short])
        return st.envelope(
            "report", False, 8, s, dict(data, closed=False, short_narrative=short),
            _report_render(rel, missing, s, data) + "\n\n" + "\n".join(
                ["## 서술이 짧다 — 다시 낸다", ""]
                + ["- `%s`: %d자 (하한 %d)" % (k, n, rep.NARRATIVE_MIN_CHARS)
                   for k, n in short]
                + ["", "보고서는 썼지만 **런을 닫지 않았다.** 「배운 점」과 "
                       "「다음 런에서 바꿀 것」은 다음 런의 입력이다 — 표가 말하지 "
                       "못하는 「왜」를 적고 같은 명령으로 다시 낸다. 등급은 그대로다."]),
            "python scripts/pipeline/cli.py report --run-id %s" % s["run_id"])

    # **전이 조건은 08 자신의 `requires` 다.** 여기 따로 적으면 페이즈 파일과
    # 갈라지고, 안 두면 03 에서 부른 report 가 런을 닫아 버린다.
    loaded, _ = load_phases(root)
    item = loaded.get("08-report")
    ctx = build_context(root, paths, s)
    checks = check_requires(root, (item or {}).get("front", {}).get("requires"),
                            ctx, s) if item else []
    if item is None or [c for c in checks if not c["ok"]]:
        # **보고서는 파이프라인을 실패시키지 않는다** — 썼다고 말하고 exit 0 이다.
        return st.envelope(
            "report", True, 0, s,
            dict(data, closed=False, requires_report=checks),
            _report_render(rel, missing, s)
            + "\n\n**런을 닫지 않았다** — 08 의 선행 조건이 아직 안 맞는다."
              " 보고서만 썼다.", None)

    env = _close_run(root, paths, s, item, ctx, cmd="report")
    env["data"].update(data)
    env["render"] = _report_render(rel, missing, s, data) + "\n\n" + env["render"]
    return env


def _report_render(rel, missing, s, data=None):
    lines = ["## 보고서를 썼다", "", "`%s`" % rel, "",
             "완료 등급 **%s**%s" % (s.get("grade") or "미정",
                                    (" — " + ", ".join(s.get("gaps") or []))
                                    if s.get("gaps") else "")]
    if missing:
        lines += ["", "**필수 섹션이 빠졌다: %s**" % ", ".join(missing),
                  "원장에 기록했다. 다만 **보고서는 파이프라인을 실패시키지 "
                  "않는다.**"]
    data = data or {}
    if data.get("pilot_log"):
        lines += ["", "`%s` 의 「런 기록」에 이 런의 절을 붙였다." % data["pilot_log"]]
    if data.get("deferred_md"):
        lines += ["`%s` 를 원장에서 다시 만들었다 (이월 미해결)." % data["deferred_md"]]
    lines += ["", "**런 기록·PILOT-LOG·deferred.md 를 커밋하고 `pr --run-id %s` 를 "
                  "다시 돌려 PR 을 갱신한다.** 닫힌 런의 PR 갱신에 런 기록이 diff 에 "
                  "없으면 gap `run_record_missing` 이다 (ADR-H052)."
              % s.get("run_id")]
    return "\n".join(lines)


INSTRUCTION_REVIEW_FILE = "08_instruction_review.json"
_SLOT_BULLET = re.compile(r"^[-*+]\s+")


def _review_skill(config):
    return ((config.get("project") or {}).get("instruction_review")
            or {}).get("skill")


def _instruction_destination(config, rel):
    """지시문 목적지인가 — `_rules_read_expected` 의 집합 ∪ `.claude/agent-memory/**`.

    존재가 아니라 경로로 가른다 — 검토가 `rules_dir` 에 새 파일을 만들 수 있다.
    """
    rel = Path(rel).as_posix()
    proj = config.get("project") or {}
    inst = proj.get("instruction_file")
    if inst and rel == Path(inst).as_posix():
        return True
    rules_dir = proj.get("rules_dir")
    if (rules_dir and rel.endswith(".md")
            and Path(rel).parent.as_posix() == Path(rules_dir).as_posix()):
        return True
    return rel.startswith(".claude/agent-memory/")


def _changed_since(root, ref):
    """`ref` 이후 바뀐 파일(워크트리 포함) ∪ 미커밋·새 파일. `ref` 가 없으면 뒤엣것만."""
    out = set()
    if ref:
        r = harness._git(root, "diff", "--name-only", ref)
        if r is not None and r.returncode == 0:
            out |= {ln.strip().strip('"') for ln in r.stdout.splitlines()
                    if ln.strip()}
    r = harness._git(root, "status", "--porcelain", "-uall")
    if r is not None and r.returncode == 0:
        out |= {ln[3:].strip().strip('"') for ln in r.stdout.splitlines()
                if len(ln) > 3}
    return {p.replace("\\", "/") for p in out}


def _instruction_review(root, paths, s, config):
    """08 지시문 검토 파일을 읽고 **자진신고를 기계로 대조한다** (ADR-H056).

    반환: (검토 dict | None, 오류 목록). 오류가 없어야 흡수 키를 은퇴시킨다.

    ① prose 후보는 흡수·기각 중 정확히 한쪽이다 — 어느 쪽도 아닌 경로가 없다
    ② 흡수했다면 지시문 목적지가 **06 push 이후** 실제로 바뀌었어야 한다 —
       base 이후 전체를 보면 기능 런마다 바뀌는 `rules_dir` 문서가 증거로 통과한다
    ③ 후보도 이 런이 이미 은퇴시킨 키(재실행)도 아닌 키는 받지 않는다 — lint
       후보를 여기서 은퇴시키는 것은 07 판정을 우회하는 길이다
    """
    import ledger as ledger_mod

    p = paths.run_dir / INSTRUCTION_REVIEW_FILE
    if not p.exists():
        return None, ["`%s` 이 없다" % INSTRUCTION_REVIEW_FILE]
    try:
        d = harness._read_json(p)
    except (OSError, ValueError) as exc:
        return None, ["`%s` 를 읽지 못했다: %s" % (INSTRUCTION_REVIEW_FILE, exc)]
    if not isinstance(d, dict):
        return None, ["`%s` 는 객체다" % INSTRUCTION_REVIEW_FILE]

    errors = []
    if d.get("schema") != 1:
        errors.append("`schema` 는 1 이다")
    if d.get("reviewed") is not True:
        errors.append("`reviewed` 는 true 다 — 검토하지 않았으면 파일을 쓰지 않는다")
    want = _review_skill(config)
    if d.get("skill") != want:
        errors.append("`skill` 이 config 와 다르다: %r (config: %r)"
                      % (d.get("skill"), want))

    absorbed = d.get("absorbed")
    declined = d.get("declined")
    changes = d.get("changes")
    if not (isinstance(absorbed, list)
            and all(isinstance(k, str) and k for k in absorbed)):
        errors.append("`absorbed` 는 rule_key 문자열 목록이다")
        absorbed = []
    if not (isinstance(declined, list) and all(isinstance(x, dict) for x in declined)):
        errors.append("`declined` 는 {rule_key, reason} 목록이다")
        declined = []
    if not (isinstance(changes, list) and all(isinstance(x, dict) for x in changes)):
        errors.append("`changes` 는 {file, summary, rule_keys} 목록이다")
        changes = []
    for x in declined:
        if not str(x.get("reason") or "").strip():
            errors.append("기각 `%s` 에 사유가 없다 — skip 에는 rationale 이 "
                          "있다 (ADR-H051)" % x.get("rule_key"))
    declined_keys = [x.get("rule_key") for x in declined]

    try:
        prose = [c["rule_key"] for c in
                 ledger_mod.stage_promotions(root)["prose_candidates"]]
    except (OSError, ValueError, KeyError):
        prose = []
    own = {(r.get("retire") or {}).get("rule_key")
           for r in ledger_mod.read_all(root)
           if (r.get("retire") or {}).get("run_id") == s.get("run_id")}
    for key in prose:
        n = (key in absorbed) + (key in declined_keys)
        if n != 1:
            errors.append("prose 후보 `%s` 는 absorbed·declined 중 정확히 한쪽에 "
                          "있어야 한다 (지금 %d곳)" % (key, n))
    for key in absorbed + declined_keys:
        if key not in prose and key not in own:
            errors.append("`%s` 는 이 런의 prose 후보가 아니다 — 기계 강제 후보의 "
                          "은퇴는 07 의 `retire` 판정이다" % key)

    if absorbed and not changes:
        errors.append("흡수했는데 `changes` 가 비었다 — 지시문을 바꾸지 않고 "
                      "후보를 은퇴시키지 않는다")
    evidence = _changed_since(root, (s.get("pr") or {}).get("head_sha"))
    covered = set()
    for c in changes:
        f = Path(str(c.get("file") or "")).as_posix()
        if not c.get("file") or not str(c.get("summary") or "").strip():
            errors.append("`changes` 항목에는 `file` 과 `summary` 가 있다: %r" % c)
            continue
        if not _instruction_destination(config, f):
            errors.append("`%s` 는 지시문 목적지가 아니다 (instruction_file · "
                          "rules_dir 직속 *.md · .claude/agent-memory/**)" % f)
        elif f not in evidence:
            errors.append("`%s` 가 06 push 이후 바뀌지 않았다" % f)
        covered |= set(c.get("rule_keys") or [])
    for key in absorbed:
        if key not in covered:
            errors.append("흡수 `%s` 에 대응하는 변경이 없다 — `changes[].rule_keys` "
                          "에 적는다" % key)
    d.update(absorbed=absorbed, declined=declined, changes=changes)
    return d, errors


def _instruction_slots(root, config):
    """`instruction_slot_budget` 의 소비자 (ADR-H056). 재지 못하면 None.

    `CRITICAL:` 라벨은 자기 선언이라 세지 않는다 — **0열에서 시작하는 불릿**
    이 한 칸이다. 들여쓴 줄은 하위 항목이고 펜스·표 안은 규칙이 아니다.
    `prose_lines` 는 제목·불릿이 아닌 본문 줄 수 — 규칙이 산문뿐이면 0칸이
    "규칙 없음" 이 아니라 "못 잼" 이다.
    """
    proj = config.get("project") or {}
    inst, budget = proj.get("instruction_file"), proj.get("instruction_slot_budget")
    if not inst or budget is None:
        return None
    try:
        text = (Path(root) / inst).read_text(encoding="utf-8")
    except OSError:
        return None
    used = prose = 0
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence or not line.strip() or line.startswith("|"):
            continue
        if _SLOT_BULLET.match(line):
            used += 1
        elif not line.startswith("#"):
            prose += 1
    return {"used": used, "budget": budget, "prose_lines": prose}


# ------------------------------------------------------------------- review07

def cmd_review07(root, args):
    return st.emit(run_review07(root, args.external, args.run_id))


def run_review07(root, external=None, run_id=None):
    """생략 조건과 effort 를 **결정론으로** 정한다. 종료 코드 0 / 3 / 8.

    모델이 effort 를 고르면 같은 상황이 런마다 다른 리뷰를 받고, 그러면
    `escaped_05` 가 생략 정책의 근거가 되지 못한다.
    """
    import review07 as rv7

    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("review07", False, 3, None, {},
                           "런이 없다. `init --feature` 로 시작한다.", None)
    if s.get("escalated"):
        return _escalation_envelope("review07", paths, s)

    config, _adapter, _cal = adapters.load(root)
    ext_cfg = config.get("external_pr_review") or {}

    if not ext_cfg.get("enabled"):
        # **봇이 꺼져 있으면 소스 자체가 없다.** `disabled` 는 gap 이 아니다 —
        # config 로 뺀 관측기이고, 내장 리뷰를 부를지는 05 의 결과와 수리
        # 흔적이 정한다 (ADR-H043 · `review07.decide`).
        norm = {"status": "disabled", "major": 0, "findings": [],
                "human_comments": [], "change_requested": False,
                "note": "config.external_pr_review.enabled 가 false 다."}
    elif not external:
        norm = {"status": "timeout", "major": 0, "findings": [],
                "human_comments": [], "change_requested": False,
                "note": "외부 리뷰 파일이 주어지지 않았다 — 무응답으로 본다."}
    else:
        f = Path(external)
        if not f.exists():
            return st.envelope("review07", False, 3, s, {},
                               "외부 리뷰 파일이 없다: %s" % f, None)
        try:
            payload = harness._read_json(f)
        except (OSError, ValueError) as exc:
            return st.envelope("review07", False, 8, s, {},
                               "JSON 을 읽지 못했다: %s" % exc, None)
        if (payload.get("status") is not None
                and payload["status"] not in rv7.EXTERNAL_STATUS):
            return st.envelope("review07", False, 8, s,
                               {"status": payload.get("status")},
                               "`status` 는 %s 중 하나다 (받은 값: %r)"
                               % (" · ".join(rv7.EXTERNAL_STATUS),
                                  payload.get("status")), None)
        norm = rv7.normalize_external(payload)

    audit = rv7.audit_due(root)
    got = rv7.decide(s, norm, config, audit=audit)

    s.setdefault("audit", {}).update(
        {"is_audit_run": bool(audit),
         "reason": ("5런 주기" if audit else None)})
    s.setdefault("review07", {}).update(
        {"external": {"status": norm["status"], "major": norm["major"]},
         "code_review": got["effort"], "escaped_05": None,
         # 정책 생략의 사유. 보고서가 "생략이라 0" 과 "봤는데 0" 을 가른다.
         "skip_reason": got.get("skip_reason")})
    for gap in got["gaps"]:
        st.demote(s, st.GRADES[1], gap)
    if not got["skip"]:
        # 내장 `/code-review` 는 `record --reviewer` 를 남기지 않아 제출
        # 기준에서 통째로 빠져 있었다 — ADR-H014 가 지목한 그 1회다.
        st.count_instructions(s, "07-pr-review", ["07:code-review"])
    st.save(paths, s)

    data = dict(got, external=norm)
    if got["skip"]:
        # 스킵되는 것은 `/code-review` 호출뿐이다. `record --phase 07` 과
        # `promote` 는 그대로 돈다 — 승격 쓰기가 07 안에 있다.
        next_cmd = ("python scripts/pipeline/cli.py record --phase 07 "
                    "--file {07_pr_review.json} --run-id %s" % s["run_id"])
    else:
        next_cmd = "/code-review --effort %s" % got["effort"]
    return st.envelope("review07", True, 0, s, data, _review07_render(got, norm),
                       next_cmd)


def _review07_render(got, norm):
    lines = ["## 07 리뷰 판정", ""]
    lines.append("- 외부: **%s** (Major %d)" % (norm["status"], norm["major"]))
    lines.append("- 내장 리뷰: **%s**" % got["effort"])
    if got["audit_run"]:
        lines.append("- **감사 런이다**")
    lines += [""] + ["- %s" % r for r in got["reasons"]]
    if not got["skip"]:
        lines += ["", "`/code-review --effort %s` 를 그대로 부른다. "
                      "**effort 를 네가 고르지 마라.**" % got["effort"]]
    else:
        lines += ["", "**내장 리뷰를 부르지 않는다** (사유: `%s`). "
                      "`07_pr_review.json` 을 `code_review: \"skipped\"` · "
                      "findings 빈 배열로 내고 바로 `record --phase 07` 로 간다 — "
                      "`promote` 는 그 뒤에 그대로 돈다." % got.get("skip_reason")]
    if norm.get("human_comments"):
        lines += ["", "사람 코멘트 %d 건은 **수리 대상이 아니라 보고 대상**이다."
                  % len(norm["human_comments"])]
    return "\n".join(lines)


# --------------------------------------------------------------------- promote

def cmd_promote(root, args):
    return st.emit(run_promote(root, scan=args.scan, stage=args.stage,
                               apply=args.apply, flush=args.flush,
                               verdict_file=args.verdict_file,
                               run_id=args.run_id))


def run_promote(root, scan=False, stage=False, apply=False, flush=False,
                verdict_file=None, run_id=None, runner=None):
    """승격. 종료 코드 **0 / 4 / 6 / 8** (네 플래그가 한 행을 공유한다).

    명세는 `--apply` 단독의 종료 코드를 따로 적지 않았다 — **명세 미규정이고,
    여기서는 판정 위반을 8, 자동 쓰기 차단을 10 으로 쓴다.**

    `runner` 는 베이스라인 측정용 주입점이다 (테스트).
    """
    import promote as pm

    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("promote", False, 3, None, {},
                           "런이 없다. `init --feature` 로 시작한다.", None)
    if s.get("escalated"):
        return _escalation_envelope("promote", paths, s)

    if not (scan or stage or apply or flush):
        return st.envelope("promote", False, 2, s, {},
                           "플래그가 필요하다: --scan / --stage / --apply / --flush",
                           None)

    scanned = pm.scan(root)

    if flush:
        import ledger as ledger_mod

        promos = s.setdefault("promotions", [])
        n = pm.flush(promos)
        # **시한이 지난 뒤의 미룸은 등급이 치른다** (ADR-H051). 시한은 표시로만
        # 있었고(ADR-H033) 파일럿 40dc 는 「12 / 9 — 판정할 때다」 를 찍은 채
        # 후보 셋을 전부 skip 했다. 여기는 런당 한 번, 승격이 종결되는 자리다.
        # prose 후보는 staged 되지 않으므로(ADR-H056) 여기 들 수 없다.
        overdue = bool(ledger_mod.verdict_deadline(root).get("due")) and any(
            p.get("status") == "skipped" for p in promos)
        if overdue:
            st.demote(s, st.GRADES[1], "promotion_overdue")
        # 런당 한 번 도는 자리 — 테스트 수 하한을 이 런의 전체 회귀로 올린다
        # (ADR-H047). `state.tests.ran` 은 04/05 의 `full` 이 테스트 리포트에서 셌다.
        config, _adapter, _cal = adapters.load(root)
        floor = adapters.raise_tests_floor(
            root, config, (s.get("tests") or {}).get("ran"), s["run_id"])
        st.save(paths, s)
        note = ""
        if floor:
            note = ("\n\n테스트 수 하한을 %s → %d 로 올렸다 (`calibration.derived."
                    "tests_ran_floor`, 이 런의 전체 회귀 %d개 × %.1f)."
                    % (floor["from"], floor["to"], s["tests"]["ran"],
                       harness.TESTS_FLOOR_RATIO))
        return st.envelope("promote", True, 0, s,
                           {"flushed": n, "promotions": promos,
                            "tests_ran_floor": floor,
                            "promotion_overdue": overdue},
                           "잔여 승격 %d 건을 `skipped` 로 종결했다 — 임계가 다시 "
                           "충족되면 다음 런에서 재승격 후보가 된다." % n
                           + ("\n\n**승격 판정 시한이 지났는데 후보를 미뤘다** — gap "
                              "`promotion_overdue` 로 등급이 내려간다 (ADR-H051). "
                              "다음 런에서는 판정(`create`/`amend`)하거나 임계를 "
                              "고친다." if overdue else "")
                           + note, None)

    if scan or stage:
        # **덮어쓰지 않고 병합한다** (G-3). 07 의 절차는 `--scan` → 판정 →
        # `--apply` 이고 `--scan` 이 적재를 겸한다. 그런데 대입이면 `--apply`
        # 뒤에 다시 훑는 것만으로 `applied` 가 `staged` 로 되돌아간다 —
        # `report` 가 exit 6 을 내고 두 번째 `--apply` 에서 changelog 가
        # 중복된다. **한 번 일어난 일이 재집계로 없던 일이 되면 안 된다.**
        promos = s.setdefault("promotions", [])
        pm.merge_staged(promos, pm.stage(scanned["candidates"]))
        if scanned["needs_model"]:
            # 승격 판정은 모델 호출이다 — 지시하는 자리에서 센다 (ADR-H042).
            st.count_instructions(s, "07-pr-review", ["07:promote"])
        st.save(paths, s)
        data = dict(scanned, promotions=promos)
        return st.envelope("promote", True, 0, s, data,
                           _promote_scan_render(scanned),
                           None if not scanned["needs_model"] else
                           "python scripts/pipeline/cli.py promote --apply "
                           "--verdict-file {07_promo_verdict.json} --run-id %s"
                           % s["run_id"])

    # --- apply
    if not verdict_file:
        return st.envelope("promote", False, 2, s, {},
                           "`--apply` 는 판정 파일이 필요하다: --verdict-file",
                           None)
    vf = Path(verdict_file)
    if not vf.exists():
        return st.envelope("promote", False, 3, s, {},
                           "판정 파일이 없다: %s" % vf, None)
    try:
        payload = harness._read_json(vf)
    except (OSError, ValueError) as exc:
        return st.envelope("promote", False, 8, s, {},
                           "JSON 을 읽지 못했다: %s" % exc, None)
    verdicts = payload.get("verdicts") or []

    # `or` 가 적법하게 빈 `[]` 를 거짓으로 읽으면, 아무것도 staged 되지 않은
    # 런에서 판정만으로 후보가 되살아나 `--stage` 를 건너뛴 승격이 성립한다.
    # G-4 의 `planned or [reviewer]` 와 같은 모양의 결함이다.
    staged_now = s.get("promotions")
    if staged_now is None:
        staged_now = pm.stage(scanned["candidates"])
    errors, blocked = pm.check_verdicts(root, verdicts, staged_now)
    if blocked:
        st.escalate(paths, s,
                    "승격 판정이 `contradicts` 다 — 기존 규칙과 싸운다",
                    options=["규칙을 사람이 조정한 뒤 재개",
                             "이 승격을 포기하고 진행", "중단"],
                    phase="07-pr-review")
        st.save(paths, s)
        return _escalation_envelope("promote", paths, s)
    if errors:
        return st.envelope("promote", False, 8, s, {"errors": errors},
                           "\n".join(["## 승격 판정이 규약을 어겼다", ""]
                                     + ["- %s" % e for e in errors]),
                           None)

    # 베이스라인은 **기계가 잰다.** lint 승격이 하나도 없으면 재지 않는다.
    baseline = None
    if pm.wants_baseline(verdicts):
        config, adapter, _cal = adapters.load(root)
        baseline = pm.measure_baseline(root, adapter, runner=runner)
        if baseline["state"] == "infra":
            # 시스템 문제다. **아무것도 쓰지 않고** 카운터도 태우지 않는다 —
            # 이것을 rejected 로 적으면 "규칙이 아무것도 안 막는다" 가 된다.
            return st.envelope("promote", False, 10, s, {"baseline": baseline},
                               "\n".join(["## 베이스라인을 재지 못했다", "",
                                          baseline["reason"], "",
                                          "**인프라 실패다.** 승격은 하나도 "
                                          "쓰이지 않았고 다시 치면 된다."]),
                               None)
        mismatch = [v for v in verdicts
                    if v.get("enforceable") == "lint"
                    and pm.baseline_mismatch(v.get("baseline_diff"),
                                             baseline["diff"])]
        if mismatch:
            return st.envelope(
                "promote", False, 8, s,
                {"baseline": baseline,
                 "reported": [v.get("baseline_diff") for v in mismatch]},
                "\n".join(
                    ["## 신고한 베이스라인이 기계가 잰 것과 다르다", "",
                     "**`baseline_diff` 를 신고하지 마라** — 재는 것은 기계의 "
                     "일이다. 실었으면 버리지 않고 대조한다.", "",
                     "**신고:**", "```", "\n".join(
                         str(v.get("baseline_diff")) for v in mismatch), "```",
                     "**기계가 잰 값:**", "```", baseline["diff"] or "(없음)",
                     "```"]),
                None)

    promos = staged_now
    promos, rows = pm.apply(root, s["run_id"], promos, verdicts, baseline)
    # 자체 게이트는 **실행기가 돌린다** (ADR-H065). changelog 를 쓰기 전이라
    # infra 면 아무것도 남지 않는다 — 베이스라인과 같은 규율이다.
    gate = None
    if pm.wants_self_gate(promos):
        _config, adapter, _cal = adapters.load(root)
        gate = pm.self_gate(root, adapter, runner=runner)
        if gate["state"] == "infra":
            return st.envelope("promote", False, 10, s, {"self_gate": gate},
                               "\n".join(["## 승격 자체 게이트를 돌리지 못했다", "",
                                          gate["reason"], "",
                                          "**인프라 실패다.** 승격은 하나도 "
                                          "쓰이지 않았고 다시 치면 된다."]),
                               None)
        if gate["state"] == "failed":
            promos, rows = pm.reject_applied(promos, rows, gate["reason"])
        elif gate["state"] == "unverified":
            st.demote(s, "PASS_WITH_GAPS", "promotion_selfgate_unverified")
    s["promotions"] = promos
    if baseline is not None and baseline["state"] == "unavailable":
        # 재지 못한 것을 잰 것처럼 적지 않는다 (§E12 가 나열을 요구한다).
        st.demote(s, "PASS_WITH_GAPS", "promotion_baseline_unverified")
    written = pm.changelog_append(root, rows)

    out = paths.run_dir / "07_promo_applied.json"
    _write_json(out, pm.applied_payload(s["run_id"], promos, rows, scanned))

    st.append_event(paths, "promoted", cmd="promote", phase="07-pr-review",
                    applied=sum(1 for p in promos if p["status"] == "applied"),
                    rejected=sum(1 for p in promos if p["status"] == "rejected"))
    st.save(paths, s)
    return st.envelope("promote", True, 0, s,
                       {"promotions": promos, "changelog_rows": written},
                       _promote_apply_render(promos, written), None)


def _verdict_deadline_lines(dl):
    """후보 0 을 보는 사람이 **그 자리에서** 시한을 본다 (ADR-H033).

    이 분기가 초기 런의 최빈 경로다. "표본이 아직 없다" 만 적으면 그 말이
    몇 런까지 유효한지를 아무도 모른다 — 그것이 `THRESHOLDS` 의 옛 약속이
    두 배 지나도록 아무도 판정하지 않은 이유다. **못 읽으면 안 적는다.**
    """
    if not dl:
        return []
    tail = ("**시한이 지났다 — 판정할 때다.**" if dl.get("due")
            else "남은 런 %d." % dl.get("remaining"))
    return ["", "**승격 판정 시한** — 원장이 본 런 %s / %s. %s 이 셈의 "
                "단위는 `distinct_runs` 라 달력의 런 수와 다를 수 있다. "
                "그때 무엇을 보고 어떻게 가를지는 **ADR-H033** 에 미리 "
                "적혀 있다."
            % (dl.get("seen"), dl.get("at"), tail)]


def _rule_label(b):
    """버킷의 규칙 슬러그를 사람이 읽는 꼬리표로. 없으면 빈 문자열.

    `CATEGORY` 가 다대일이라 category 만 찍으면 `BOUNDARY_VIOLATION` 셋이
    구별되지 않는다 — 무엇을 승격하는지 모르는 채 판정하게 된다 (ADR-H034).
    """
    slug = b.get("rule_slug")
    return " / `%s`" % slug if slug else ""


def _recur_label(b):
    """은퇴 이후 다시 쌓인 버킷이면 그렇게 말한다 — 재발은 0 부터 센 값이다."""
    at = b.get("retired_at")
    return (" · 재발(은퇴 %s 이후 %d회)" % (at, b["count"])) if at else ""


def _promote_side_lines(got):
    """원장 승격이 아닌 두 갈래 (ADR-H056). 없으면 빈 목록."""
    lines = []
    prose = got.get("prose_candidates") or []
    if prose:
        lines += ["", "## 지시문 검토 후보 %d 건 — 08 로 간다" % len(prose), "",
                  "목적지가 prose 라 여기서 판정하지 않는다. 08 지시문 검토의 "
                  "입력이다 (ADR-H056)."]
        lines += ["- %s%s (%s) — %d회 / %d런 · 신원 `%s`%s"
                  % (c["category"], _rule_label(c), c["severity"], c["count"],
                     c["distinct_runs"], c.get("rule_key") or c.get("finding_key"),
                     _recur_label(c))
                  for c in prose]
    trace = got.get("trace_repeats") or []
    if trace:
        lines += ["", "## 검사 반복 검출 %d 건" % len(trace), "",
                  "`contract-trace` 가 이미 막는 규칙의 반복이라 승격 후보가 "
                  "아니다."]
        lines += ["- %s%s — %d회 / %d런%s"
                  % (c["category"], _rule_label(c), c["count"],
                     c["distinct_runs"], _recur_label(c))
                  for c in trace]
    if prose or trace:
        lines += ["", "근본 원인을 하네스에서 고친 규칙은 `action: retire` + "
                      "`retired_reason` 으로 관측을 끊는다 — 이후 관측은 0 부터 "
                      "센다."]
    return lines


def _promote_scan_render(got):
    if not got["candidates"]:
        lines = ["승격 후보가 없다 — **모델을 부르지 않고 종결한다.**", ""]
        if got["held"]:
            lines.append("다만 누적은 넘었는데 `distinct_runs` 에서 막힌 것이 "
                         "%d 건 있다:" % len(got["held"]))
            lines += ["- %s%s (%s)"
                      % (h["category"], _rule_label(h), h["held_because"])
                      for h in got["held"]]
        else:
            lines.append("원장이 본 런은 %d 개다. 임계에 닿을 표본이 아직 "
                         "없다는 뜻이지 지적이 없었다는 뜻이 아니다."
                         % got["distinct_runs"])
        lines += _verdict_deadline_lines(got.get("verdict_deadline"))
        lines += _promote_side_lines(got)
        return "\n".join(lines)
    lines = ["## 승격 후보 %d 건" % len(got["candidates"]), ""]
    for c in got["candidates"]:
        lines.append("- **%s**%s (%s) — %d회 / %d런 · 신원 `%s` · 목적지 `%s`%s"
                     % (c["category"], _rule_label(c), c["severity"],
                        c["count"], c["distinct_runs"],
                        c.get("rule_key") or c.get("finding_key"),
                        c.get("enforceable"), _recur_label(c)))
        keys = c.get("finding_keys") or []
        if len(keys) > 1:
            lines.append("  - 접은 인스턴스 %d 건: %s"
                         % (len(keys), " · ".join("`%s`" % k for k in keys[:6])
                            + (" …" if len(keys) > 6 else "")))
    lines += ["", "각각에 `create` / `amend` / `skip` / `retire` 판정을 내고 "
                  "근거를 적는다.",
              "**판정은 위 `신원` 을 `rule_key` 로 그대로 돌려준다** — 이름을 "
              "부른 것과 다른 지적이 승격되면 그 사실이 어디에도 안 드러난다.",
              "**`duplicate` 면 `create` 가 금지되고, `contradicts` 면 자동 "
              "쓰기가 차단된다.**"]
    lines += _promote_side_lines(got)
    return "\n".join(lines)


def _promote_apply_render(promos, written):
    by = {}
    for p in promos:
        by[p["status"]] = by.get(p["status"], 0) + 1
    lines = ["## 승격 적용", "",
             " · ".join("%s %d" % (k, v) for k, v in sorted(by.items())),
             "", "`rules_changelog.md` 에 %d 줄을 남겼다." % written]
    rejected = [p for p in promos if p["status"] == "rejected"]
    if rejected:
        lines += ["", "**거절된 것:**"]
        lines += ["- %s — %s" % (p["rule_id"], p["reason"]) for p in rejected]
    lines += ["", "승격은 **별도 브랜치**로 간다. 기능 PR 에 섞지 않는다."]
    return "\n".join(lines)


# -------------------------------------------------------------------------- pr

def cmd_pr(root, args):
    return st.emit(run_pr(root, args.run_id))


def run_pr(root, run_id=None):
    """06 의 6단계. **비용 오름차순이고 첫 실패에서 멈춘다.**

    명세는 06 의 절차를 페이즈로만 기술하고 어느 커맨드가 그것을 집행하는지
    정하지 않았다 (CLI 표에 `pr` 행이 없다). `approve` 가 별도 커맨드인 것과
    같은 형태로 여기 둔다 — **명세 미규정이고, 그렇게 표기한다.**
    """
    import pr as pr_mod

    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("pr", False, 3, None, {},
                           "런이 없다. `init --feature` 로 시작한다.", None)
    if s.get("escalated"):
        return _escalation_envelope("pr", paths, s)

    config, adapter, _cal = adapters.load(root)
    data = {}

    # 1. 브랜치 — 규약과 보호. **자동 생성하지 않는다.**
    ok, branch, msg = pr_mod.check_branch(root, config)
    data["branch"] = {"ok": ok, "name": branch, "message": msg}
    if not ok:
        return st.envelope("pr", False, 3, s, data,
                           "\n".join(["## 브랜치가 맞지 않는다", "", msg, "",
                                      "**브랜치를 자동으로 만들지 않는다.**"]),
                           None)

    # 2. 백그라운드 전체 회귀 조인 (04 의 join_before: 06-pr)
    join = pr_mod.join_pending(s)
    data["join"] = join
    if join.get("blocked"):
        return st.envelope("pr", False, 3, s, data,
                           "\n".join(["## 회귀가 아직 안 끝났다", "",
                                      join["reason"], "",
                                      "끝나기 전에 push 하지 않는다."]), None)

    # 2-b. **닫힌 런의 PR 갱신**에는 런 기록이 실려야 한다 (ADR-H052 결정 4).
    # 08 이 쓴 `runs/{run_id}.md` 가 base 이후 diff 에 없으면 gap — 06 의 첫
    # push 때는 기록이 있을 수 없고 07 수리 뒤 재push(아직 done 아님)는 대상이
    # 아니다. 그래서 `run_status: done` 일 때만 본다.
    closed_run = s.get("run_status") == st.DONE
    # 2-a. 흐름 노트 (ADR-H058 추기). PR 본문의 「핵심 흐름」은 모델이 쓰고 `refs`
    # 로 계약에 묶인다 — 열린 런은 첫 `pr` 부터 요구한다. 닫힌 런의 재실행은
    # 이미 통과한 파일을 렌더만 한다(런 기록 갱신 경로에 새 exit 8 을 두지 않는다).
    if not closed_run and (s.get("contract") or {}).get("mode") != "no_contract":
        _notes, problems = pr_mod.check_notes(root, paths, s, config)
        if problems:
            data["pr_notes"] = problems
            return st.envelope(
                "pr", False, 8, s, data,
                "## 흐름 노트 `%s` 가 없거나 틀렸다\n\n%s\n\n`pr` 전에 네가 쓴다. "
                "형식:\n\n```json\n%s\n```\n\n`refs` 는 계약이 이름 붙인 것만 받는다 — "
                "유닛·화면 심볼, 오류 어휘, 데이터 형태, 진입점(`METHOD /path`), 컨테이너 "
                "경로. `step` 산문은 검사하지 않는다 (ADR-H058)."
                % (pr_mod.NOTES_FILE, "\n".join("- %s" % p for p in problems),
                   pr_mod.NOTES_EXAMPLE),
                "python scripts/pipeline/cli.py pr --run-id %s" % s["run_id"])
    if closed_run:
        import precheck as pc
        rec_rel = "docs/harness/pipeline/runs/%s.md" % s["run_id"]
        if rec_rel not in pc.changed_files(root, "pr", config):
            st.demote(s, st.GRADES[1], "run_record_missing")
            st.save(paths, s)
            data["run_record_missing"] = rec_rel
        # 08 지시문 검토가 바꾼 파일도 이 PR 에 실린다 (ADR-H056 추기). 지시문은
        # 06 승인 지문 밖이라 리뷰어가 못 봤다 — 실렸으면 이름으로 드러내고,
        # 안 실렸으면 런 기록과 같은 급이다. retire 는 08 에서 이미 원장에
        # 굳었으므로 **커밋분**만 센다(미커밋은 push 되지 않는다).
        changed = pr_mod._rule_changes(paths)
        if changed:
            base = (config.get("vcs") or {}).get("base_branch") or "main"
            r = harness._git(root, "diff", "--name-only", base, "HEAD")
            committed = ({ln.strip().strip('"') for ln in r.stdout.splitlines()}
                         if r is not None and r.returncode == 0 else set())
            missing = sorted({Path(str(c["file"])).as_posix() for c in changed}
                             - committed)
            if missing:
                st.demote(s, st.GRADES[1], "instruction_change_missing")
                data["instruction_change_missing"] = missing
            else:
                st.demote(s, None, "instruction_changed")
            st.save(paths, s)

    # 3. 원격 상태 — 없으면 §P3 3지선다, non-FF 면 에스컬레이션
    rs = pr_mod.remote_state(root, config, branch)
    data["remote"] = rs
    if not rs["has_remote"]:
        st.append_event(paths, "waiting_human", cmd="pr", phase="06-pr",
                        reason="no_remote")
        return st.envelope("pr", False, 9, s, data, _no_remote_render(rs), None)
    if rs["non_ff"]:
        st.escalate(paths, s,
                    "원격 브랜치가 non-fast-forward 다 (%d 커밋 앞섬)"
                    % rs.get("behind", 0),
                    options=["원격을 로컬로 가져와 머지한 뒤 재개",
                             "원격 브랜치를 사람이 정리한 뒤 재개",
                             "중단"],
                    phase="06-pr")
        st.save(paths, s)
        return _escalation_envelope("pr", paths, s)

    # 4. 승인 — 없거나 철회됐으면 exit 9, 지문이 어긋나면 exit 6
    node = (s.get("approval") or {}).get("06") or {}
    if not node.get("granted"):
        # 승인 대기 — `728c` 의 1h09m 같은 시간이 06 의 벽시계에 섞이지 않게
        # 시작점을 남긴다 (ADR-H052).
        st.append_event(paths, "waiting_human", cmd="pr", phase="06-pr",
                        reason="approval")
        return st.envelope("pr", False, 9, s, data,
                           _approval_prompt(root, s, rs, branch, config), None)
    fresh = st.fingerprint(root, config)
    if not st.fingerprint_matches(node.get("fingerprint") or {}, fresh):
        return st.envelope("pr", False, 6, s,
                           dict(data, saved=node.get("fingerprint"),
                                fresh=fresh),
                           "\n".join([
                               "## 승인이 무효가 됐다", "",
                               "승인 뒤에 소스가 바뀌었다. 그 승인은 **다른 "
                               "코드에 대한 것**이므로 재승인이 필요하다.", "",
                               "재승인: `python scripts/pipeline/cli.py "
                               "approve --phase 06 --run-id %s`" % s["run_id"]]),
                           None)

    # 5. 본문 조립 + 마스킹
    body = pr_mod.build_body(root, paths, s, config)
    body_path = paths.run_dir / "06_pr_body.md"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    # §E4 — 산출물은 UTF-8 을 명시한다. 한글 식별자가 흔한 리포다.
    body_path.write_text(body, encoding="utf-8")
    data["body_file"] = paths.rel(body_path)

    # 6. push → 계약 삭제 → 요청서.
    # **삭제는 push 가 성공한 뒤다** (G-7). 04 귀속과 05 대조가 계약을 계속
    # 읽으므로 최대한 늦추는 것이 §E13 의 근거인데, push 앞은 충분히 늦지
    # 않다 — push 는 실패할 수 있고, 실패하면 05 의 `requires` 가 안 채워져
    # **재개가 불가능해진다.** 계약은 `_workspace/` 아래 untracked 라 삭제
    # 시점이 커밋 diff 에 영향을 주지 않는다.
    pushed = pr_mod.push(root, config, branch)
    data["push"] = pushed
    if not pushed["ok"]:
        st.escalate(paths, s, "push 가 실패했다 — 원격 ref 조회로 확인했다",
                    options=[pushed.get("detail") or "상세 없음"], phase="06-pr")
        st.save(paths, s)
        return _escalation_envelope("pr", paths, s)

    removed = _drop_contract(root, paths, s, build_context(root, paths, s))
    data["contract_removed"] = removed

    # **07 이 대조할 기준점이다** (M49). 07 에서 메인이 "고쳤다"고 신고하면
    # 그 주장은 `<head_sha>..HEAD` 에 그 파일을 건드린 변경이 실재해야 사실이
    # 된다. 기준점이 없으면 확인할 수 없고, 확인할 수 없는 것을 확인한 것처럼
    # 적지 않는다.
    head_sha = harness._git(root, "rev-parse", "HEAD")
    s.setdefault("pr", {}).update({
        "head": branch, "pushed": True, "pushed_at": st.stamp(),
        "remote": rs["remote"],
        "head_sha": (head_sha.stdout.strip()
                     if head_sha is not None and head_sha.returncode == 0
                     else None),
    })
    req = pr_mod.build_request(s, config, branch, paths.rel(body_path),
                              rs["remote"])
    req_path = paths.run_dir / "06_pr_req.json"
    _write_json(req_path, req)
    data["pr_req"] = paths.rel(req_path)

    st.append_event(paths, "pr_pushed", cmd="pr", phase="06-pr",
                    branch=branch, remote=rs["remote"])
    st.save(paths, s)
    if closed_run:
        # 닫힌 런의 갱신은 06 record 로 이어지지 않는다 — 전이는 이미 끝났다.
        note = ["", "**닫힌 런의 PR 갱신이다.** 06 record 로 이어지지 않는다."]
        if data.get("run_record_missing"):
            note += ["", "**런 기록 `%s` 가 diff 에 없다** — gap "
                         "`run_record_missing` 으로 등급이 내려갔다. 08 이 쓴 "
                         "기록을 커밋하고 다시 돌린다 (ADR-H052)."
                     % data["run_record_missing"]]
        if data.get("instruction_change_missing"):
            note += ["", "**08 지시문 검토가 바꾼 파일이 커밋돼 있지 않다**: %s — "
                         "gap `instruction_change_missing` 으로 등급이 내려갔다. "
                         "커밋하고 다시 돌린다 (ADR-H056)."
                     % ", ".join("`%s`" % f
                                 for f in data["instruction_change_missing"])]
        return st.envelope("pr", True, 0, s, data,
                           _pr_render(req, paths, req_path) + "\n".join(note), None)
    return st.envelope("pr", True, 0, s, data, _pr_render(req, paths, req_path),
                       "python scripts/pipeline/cli.py record --phase 06 "
                       "--file {06_pr_result.json} --run-id %s" % s["run_id"])


def _drop_contract(root, paths, s, ctx):
    """계약 파일을 지운다. 없으면 없는 대로 사실을 남긴다.

    경로는 `state.contract.path` 를 먼저 보고, 없으면 `${run.contract_file}`
    로 낙하한다 — **계약 경로의 단일 출처는 후자**이고, 상태에 그 값이 안 실린
    런에서도 계약이 남지 않아야 한다. 남으면 다음 런이 남의 계약을 읽는다.
    """
    if (s.get("contract") or {}).get("mode") == "no_contract":
        return {"removed": False, "reason": "계약이 없다 (no_contract)"}
    rel = (s.get("contract") or {}).get("path") or resolve(
        "${run.contract_file}", ctx)
    if not rel:
        return {"removed": False, "reason": "계약 경로를 알 수 없다"}
    p = Path(root) / rel
    if not p.exists():
        return {"removed": False, "reason": "이미 없다", "path": rel}
    # **지우기 전에 런 디렉터리로 옮겨 둔다.** §E13 의 sha256 재대조가 06
    # 이후 재개에서도 돌 수 있어야 하고, 계약이 무엇이었는지는 런의 기록이다.
    snap = paths.run_dir / "06_contract_snapshot.md"
    snap.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    p.unlink()
    # **어디로 옮겼는지를 상태에 남긴다** (M54). 06 본문은 계약의 유닛·진입점
    # 절을 실어야 하는데(`team-spec.md` PR 본문 매핑표), 07 수리 뒤 `pr` 을
    # 다시 돌리는 정상 경로에서는 원본이 이미 없다. 읽는 쪽이 파일 이름을
    # 짐작하지 않게 출처를 상태로 준다 — 새 사본은 만들지 않는다.
    # **`paths.rel` 이 아니라 리포 루트 기준이다** — 같은 노드의 `path` 와
    # 기준이 갈리면 읽는 쪽이 둘을 다르게 조립해야 한다.
    s.setdefault("contract", {})["snapshot"] = snap.relative_to(
        paths.root).as_posix()
    return {"removed": True, "path": rel, "snapshot": paths.rel(snap)}


def _no_remote_render(rs):
    return "\n".join([
        "## 원격이 없다 — 사람이 정한다",
        "",
        "`%s` 원격을 찾지 못했다. **자동으로 원격을 만들거나 브랜치를 만들지 "
        "않는다.**" % rs["remote"],
        "",
        "① 원격을 붙이고 재개",
        "② 로컬 커밋까지만 하고 종료 (등급 `PASS_WITH_GAPS`)",
        "③ 중단",
    ])


def _approval_prompt(root, s, rs, branch, config):
    """§3.6 의 승인 프로토콜. 마지막 줄이 범위를 못박는다."""
    import precheck as pc

    got = pc.run(root, scope="pr")
    b = got["budget"]
    r05 = s.get("review05") or {}
    gaps = s.get("gaps") or []
    skipped = [g for g in gaps
               if g.startswith(("stage_absent:", "stage_na:"))] or ["없음"]
    return "\n".join([
        "## PR 생성 승인 요청",
        "",
        "%s  %s → %s      %d파일 / %d라인 (%s)"
        % (rs["remote"], branch,
           (config.get("vcs") or {}).get("base_branch") or "main",
           b["files"], b["lines"],
           "예산 내" if got["exit"] == 0 else "**예산 밖**"),
        "게이트: 스킵된 스테이지 %s" % ", ".join(skipped),
        "05: 리뷰어 %s/%s · Major %s"
        % (r05.get("reviewers_ok", "?"), r05.get("reviewers_planned", "?"),
           r05.get("major", "?")),
        "완료 등급 예상: %s%s"
        % (s.get("grade") or "미정",
           (" (" + ", ".join(gaps) + ")") if gaps else ""),
        "승인 범위: push + PR 생성.  07 코멘트 게시는 등급 재확인 후.  "
        "머지는 포함하지 않습니다.",
        "",
        "→ python scripts/pipeline/cli.py approve --phase 06 --run-id %s"
        % s["run_id"],
    ])


def _pr_render(req, paths, req_path):
    return "\n".join([
        "## push 완료 — 이제 PR 은 네가 만든다",
        "",
        "`%s` 를 읽고 **forge 도구로** PR 을 %s 한다."
        % (paths.rel(req_path),
           "갱신" if req["action"] == "update" else "생성"),
        "본문은 `%s` 다 — **이미 마스킹을 거쳤으니 다시 조립하지 마라.**"
        % req["body_file"],
        "",
        "- head: `%s` → base: `%s`" % (req["head"], req["base"]),
        "- 이미 있는 PR 이면 **생성하지 말고 갱신한다** (번호가 갈라진다)",
        "- **머지하지 마라.** 이 파이프라인의 범위는 PR 까지다",
        "",
        "끝나면 PR 번호와 상태를 `06_pr_result.json` 으로 내고 "
        "`record --phase 06` 을 부른다.",
    ])


# ------------------------------------------------------------------------ mask

def cmd_mask(root, args):
    return st.emit(run_mask(root, args.file, args.out, args.run_id))


def run_mask(root, file, out, run_id=None):
    """외부로 나가는 페이로드를 마스킹한다. 종료 코드 **0 / 1**.

    런이 없어도 돈다 — 06 이전에 본문 초안을 확인할 수 있어야 한다.
    실패가 exit 1(내부 오류)인 것은 명세의 CLI 표가 그렇게 정한다: 가리지
    못한 채로 내보내느니 멈추는 쪽이다.
    """
    import mask as mask_mod

    root = Path(root)
    paths, s = st.load(root, run_id)
    got = mask_mod.mask_file(root, file, out)
    ok = bool(got.get("ok"))
    if s is not None and ok:
        # 비밀 파일 부재는 **원장에 기록한다** — 경고이지 실패가 아니지만
        # "그때 패턴만 걸렸다"를 나중에 알 수 있어야 한다 (§8.2 06 마지막 행).
        st.append_event(paths, "stage_done", cmd="mask", phase=s.get("phase"),
                        hits=got["hits"],
                        secret_files_missing=got["secret_files_missing"])
        st.save(paths, s)
    return st.envelope("mask", ok, 0 if ok else 1, s, got,
                       _mask_render(got), None)


def _mask_render(got):
    if not got.get("ok"):
        return "\n".join([
            "## 마스킹 실패", "",
            got.get("error") or "알 수 없는 오류", "",
            "가리지 못한 채로 내보내지 않는다."])
    lines = ["`mask` 완료 — %d 곳을 가렸다." % got["hits"]]
    by = got.get("by_source") or {}
    if by:
        lines.append("출처별: " + " · ".join(
            "%s %d" % (k, v) for k, v in sorted(by.items()) if v))
    if got.get("secret_files_missing"):
        lines.append("")
        lines.append("**비밀 파일이 없어 패턴만 적용했다** (%s) — 경고이지 "
                     "실패가 아니다. 원장에 남겼다."
                     % ", ".join(got["secret_files_missing"]))
    return "\n".join(lines)


# --------------------------------------------------------------- contract-trace

def cmd_contract_trace(root, args):
    return st.emit(run_contract_trace(root, args.contract, args.run_id))


def run_contract_trace(root, contract=None, run_id=None):
    """계약 ↔ 코드 대조. **05 에서 두 번째로 도는 무료 검사다.**

    모델을 한 번도 부르지 않는다. 여기서 잡히는 것을 리뷰어에게 보내면 리뷰어가
    같은 것을 다시 발견하는 데 돈을 쓴다.

    exit 8 은 **Critical 이 남았다**는 뜻이고 "리뷰어를 부르기 전에 고쳐라"다.
    """
    import trace_contract

    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("contract-trace", False, 3, None, {}, "런이 없다.", None)
    if s.get("escalated"):
        return _escalation_envelope("contract-trace", paths, s)

    config, adapter, _cal = adapters.load(root)
    ctx = build_context(root, paths, s)
    no_contract = (s.get("contract") or {}).get("mode") == "no_contract"
    rel = contract or resolve("${run.contract_file}", ctx)
    full = root / rel
    if not no_contract and not full.exists():
        return st.envelope("contract-trace", False, 3, s, {"contract": str(rel)},
                           "계약 파일이 없다: %s — `no_contract` 런이면 "
                           "state.contract.mode 가 그렇게 적혀 있어야 한다" % rel,
                           None)

    got = trace_contract.run(root, config, adapter,
                             None if no_contract else full,
                             no_contract=no_contract)
    out = paths.run_dir / "05_trace.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(got, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")

    s.setdefault("phases", {}).setdefault("05-code-review", {})["trace"] = {
        "status": got["status"], "blocking": got.get("blocking", 0),
        "skipped": got.get("skipped") or []}
    st.append_event(paths, "submit_received", cmd="contract-trace",
                    phase="05-code-review", file=paths.rel(out))
    st.save(paths, s)

    exit_ = 8 if got.get("blocking") else 0
    return st.envelope("contract-trace", exit_ == 0, exit_, s, got,
                       _trace_render(got, rel),
                       None if exit_ else
                       "python scripts/pipeline/cli.py record --phase 05 "
                       "--file <리뷰 json> --reviewer <code> --run-id %s" % s["run_id"])


def _trace_render(got, rel):
    if got["status"] == "skipped_no_contract":
        return ("## 계약 대조 — 수행하지 않았다\n\n"
                "`no_contract` 런이다. 계약이 없으므로 대조할 것이 없고, "
                "**이것은 통과가 아니라 미수행이다** — 보고서가 그렇게 적는다.")
    lines = ["## 계약 대조 (`%s`)" % rel, ""]
    lines.append("검사 %d종 수행 · 지적 %d건"
                 % (len(got["checks_run"]), len(got["findings"])))
    if got.get("skipped"):
        reasons = got.get("skip_reasons") or {}
        lines.append("**건너뛴 검사** — 통과가 아니라 미수행이다: %s"
                     % " · ".join("`%s` (%s)" % (s, reasons.get(s, "사유 미기재"))
                                  for s in got["skipped"]))
    warn_only = [f for f in got["findings"] if f.get("resolution") == "warn_only"]
    if warn_only:
        lines.append("`warn_only` %d건 — baseline 기간이라 지적으로 올리지 않는다. "
                     "보고서에는 남는다." % len(warn_only))
    blocking = [f for f in got["findings"]
                if f["severity"] == "critical" and f.get("resolution") != "warn_only"]
    if blocking:
        lines += ["", "### 리뷰어를 부르기 전에 고칠 것 (Critical %d건)" % len(blocking)]
        for f in blocking:
            lines.append("- `%s` → **%s**: %s"
                         % (f["code"], f["target_role"], f["title"]))
        lines += ["", "고친 뒤 `gate --phase 04 --stage scoped` 로 재게이트하고 "
                      "이 명령을 다시 친다."]
    else:
        lines += ["", "Critical 0건. 리뷰어 라우팅으로 넘어간다."]
    return "\n".join(lines)


def cmd_advance(root, args):
    return st.emit(run_advance(root, args.phase, args.run_id))


def run_advance(root, phase, run_id=None):
    """명시 전이. **산출물 신선도 + 워크트리 지문**을 대조한다.

    게이트 통과 후 소스가 바뀌면 영수증이 stale 이다 — "통과한 셈 치고 넘어가기"의
    구조적 차단이고, 막히는 것이 정상 동작이다.
    """
    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("advance", False, 3, None, {}, "런이 없다.", None)
    if s.get("run_status") == st.DONE:
        return st.envelope("advance", True, 0, s, {"closed": True},
                           "이 런은 이미 닫혔다 (`run_status: done`). "
                           "전이할 것이 없다.", None)
    loaded, _ = load_phases(root)
    pid = _normalize_phase(phase, loaded)
    if pid is None:
        return st.envelope("advance", False, 2, s, {}, "알 수 없는 페이즈: %r" % phase, None)

    ctx = build_context(root, paths, s)
    missing = []
    for prod in loaded[pid]["front"].get("produces") or []:
        if prod.get("unless") and eval_condition(prod["unless"], s):
            continue
        target = root / resolve(prod["path"], ctx)
        if not target.exists():
            missing.append(prod["path"])
    if missing:
        return st.envelope("advance", False, 6, s, {"missing": missing},
                           "## 전이 거부 — 산출물이 없다\n\n" +
                           "\n".join("- `%s`" % m for m in missing), None)

    saved = s.get("fingerprint")
    fresh = st.fingerprint(root, ctx["config"])
    if saved and not st.fingerprint_matches(saved, fresh):
        return st.envelope(
            "advance", False, 6, s, {"saved": saved, "fresh": fresh},
            "## 전이 거부 — 영수증이 낡았다\n\n게이트 통과 뒤 소유 범위의 소스가 "
            "바뀌었다. 게이트를 다시 돌려야 한다.\n\n"
            "`python scripts/pipeline/cli.py gate --phase 04 --run-id %s`" % s["run_id"],
            "python scripts/pipeline/cli.py gate --phase 04 --run-id %s" % s["run_id"])

    return _advance_to_next(root, paths, s, loaded[pid], ctx, cmd="advance")


def cmd_retry(root, args):
    return st.emit(run_retry(root, args.phase, args.counter, args.reason, args.run_id))


def run_retry(root, phase, counter, reason, run_id=None):
    """`failed` → `running`. **record 로는 못 한다** — 재작업의 유일한 문이다."""
    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("retry", False, 3, None, {}, "런이 없다.", None)
    loaded, _ = load_phases(root)
    pid = _normalize_phase(phase, loaded)
    if pid is None:
        return st.envelope("retry", False, 2, s, {}, "알 수 없는 페이즈: %r" % phase, None)
    if counter not in st.COUNTERS:
        return st.envelope("retry", False, 2, s, {},
                           "알 수 없는 카운터: %r (%s)"
                           % (counter, ", ".join(st.COUNTERS)), None)

    profile = (s.get("profile") or {}).get("name") or "normal"
    try:
        max_ = _loop_max(loaded[pid]["front"], profile)
    except ConfigDeclarationError as exc:
        return _declaration_envelope("retry", s, exc)
    # `max_` 는 선언값이고 봉투가 말해야 하는 것은 실효 상한이다 (M56).
    used, max_eff, exceeded = st.counter_inc(s, counter, max_, "manual",
                                             paths=paths, note=reason)
    if exceeded:
        try:
            _loop_on_exceed(loaded[pid]["front"])
        except ConfigDeclarationError as exc:
            return _declaration_envelope("retry", s, exc)
        st.escalate(paths, s, "`%s` 카운터가 상한 %d 에 닿았다: %s" % (counter, max_eff, reason),
                    ["범위를 줄인다", "계약을 고친다", "중단한다"], phase=pid)
        return st.envelope("retry", False, 7, s, {"counter": counter, "used": used},
                           "## 반복 한계 — 에스컬레이션\n\n`ESCALATION.md` 를 본다.",
                           "python scripts/pipeline/cli.py resume --ack "
                           "--answer-file <경로>")
    st.set_phase_status(s, pid, "running", retry_reason=reason)
    s["phase"] = pid
    st.save(paths, s)
    return st.envelope("retry", True, 0, s,
                       {"counter": counter, "used": used, "max": max_eff},
                       "`%s` 를 다시 연다 (%s %d/%d). 사유: %s"
                       % (pid, counter, used, max_eff, reason),
                       "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])


def cmd_escalate(root, args):
    return st.emit(run_escalate(root, args.reason, args.run_id))


def run_escalate(root, reason=None, run_id=None):
    paths, s = st.load(Path(root), run_id)
    if s is None:
        return st.envelope("escalate", False, 3, None, {}, "런이 없다.", None)
    st.escalate(paths, s, reason or "사람이 판단을 요청했다",
                ["이대로 진행한다", "범위를 줄인다", "중단한다"],
                phase=s.get("phase"))
    return _escalation_envelope("escalate", paths, s)


def cmd_resume(root, args):
    return st.emit(run_resume(root, args.ack, args.answer_file, args.run_id))


def run_resume(root, ack=False, answer_file=None, run_id=None):
    """에스컬레이션 잠금 해제 **전용**.

    세션 복구는 `next --run-id` 가 한다 — 두 가지를 한 커맨드에 넣으면
    "재개했다"가 "판단했다"를 조용히 대신하게 된다.
    """
    root = Path(root)
    paths, s = st.load(root, run_id)
    if s is None:
        return st.envelope("resume", False, 3, None, {}, "런이 없다.", None)
    if not s.get("escalated"):
        return st.envelope("resume", True, 0, s, {},
                           "잠긴 런이 아니다. 세션을 이어가려면 `next --run-id` 를 쓴다.",
                           "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])
    if not ack:
        return st.envelope("resume", False, 2, s, {},
                           "`--ack` 없이는 잠금을 풀지 않는다. 사람이 답을 정했다는 "
                           "표시다.", None)
    answer = None
    if answer_file:
        p = Path(answer_file)
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            return st.envelope("resume", False, 2, s, {}, "답변 파일이 없다: %s"
                               % answer_file, None)
        answer = p.read_text(encoding="utf-8")
    s["escalated"] = False
    s["run_status"] = "active"
    s["escalation"] = dict(s.get("escalation") or {}, answered_at=st.stamp(),
                           answer=answer)
    pid = s.get("phase")
    if pid and st.phase_status(s, pid) == "escalated":
        st.set_phase_status(s, pid, "running")
    st.append_event(paths, "resumed", cmd="resume", phase=pid)
    st.save(paths, s)
    return st.envelope("resume", True, 0, s, {"phase": pid},
                       "잠금을 풀었다. 사람의 답이 원장에 남았다.",
                       "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])


# ---------------------------------------------------------------------- status

def cmd_status(root, args):
    """현황. **항상 exit 0** — 상태를 묻는 것이 실패일 수는 없다."""
    paths, s = st.load(root, args.run_id)
    if s is None:
        return st.emit(st.envelope(
            "status", True, 0, None, {"runs": 0},
            "진행 중인 런이 없다. `init --feature <slug> --request-file <경로>` 로 시작한다.",
            None))
    data = {
        "run_id": s["run_id"], "slug": s.get("slug"),
        "run_status": s.get("run_status"),
        "phase": s.get("phase"), "phases": s.get("phases") or {},
        "run_dir": str(paths.run_dir),
        "contract": s.get("contract"), "grade": s.get("grade"),
        "gaps": s.get("gaps") or [],
    }
    # 상태를 함께 적는다 — 닫힌 뒤에는 페이즈가 `done` 이라 그것만 보면
    # 끝난 런과 망가진 런이 화면에서 같아 보인다.
    lines = ["## 런 `%s` (%s)" % (s["run_id"], s.get("slug")), "",
             "상태: **%s**" % (s.get("run_status") or "?"),
             "현재 페이즈: **%s**" % s.get("phase"), ""]
    for pid in sorted((s.get("phases") or {})):
        lines.append("- `%s` — %s" % (pid, s["phases"][pid].get("status")))
    if s.get("gaps"):
        lines += ["", "gaps: " + ", ".join(s["gaps"])]
    return st.emit(st.envelope("status", True, 0, s, data, "\n".join(lines), None))


# ------------------------------------------------------------------------ CLI

def build_parser():
    # add_help=False — argparse 의 도움말은 stdout 으로 나가 봉투를 오염시킨다.
    p = argparse.ArgumentParser(prog="cli.py", add_help=False,
                                description="8페이즈 feature-pipeline")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("doctor", add_help=False)

    sp = sub.add_parser("abandon", add_help=False)
    sp.add_argument("--reason", dest="reason", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("status", add_help=False)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("lint-phases", add_help=False)
    sp.add_argument("--dir", dest="dir", default=None)

    sp = sub.add_parser("init", add_help=False)
    sp.add_argument("--feature", dest="feature", default=None)
    sp.add_argument("--request-file", dest="request_file", default=None)
    sp.add_argument("--profile", dest="profile", default=None,
                    choices=["docs", "fix", "small", "normal"])

    sp = sub.add_parser("next", add_help=False)
    sp.add_argument("--run-id", dest="run_id", default=None)
    sp.add_argument("--phase", dest="phase", default=None)

    sp = sub.add_parser("advance", add_help=False)
    sp.add_argument("--phase", dest="phase", required=True)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("retry", add_help=False)
    sp.add_argument("--phase", dest="phase", required=True)
    sp.add_argument("--counter", dest="counter", required=True)
    sp.add_argument("--reason", dest="reason", required=True)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("escalate", add_help=False)
    sp.add_argument("--reason", dest="reason", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("resume", add_help=False)
    sp.add_argument("--ack", dest="ack", action="store_true")
    sp.add_argument("--answer-file", dest="answer_file", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("gate", add_help=False)
    sp.add_argument("--phase", dest="phase", default="04")
    sp.add_argument("--stage", dest="stage", default=None)
    sp.add_argument("--replay", dest="replay", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("report", add_help=False)
    sp.add_argument("--out", dest="out", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("cost", add_help=False)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("review07", add_help=False)
    sp.add_argument("--external", dest="external", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("promote", add_help=False)
    sp.add_argument("--scan", dest="scan", action="store_true")
    sp.add_argument("--stage", dest="stage", action="store_true")
    sp.add_argument("--apply", dest="apply", action="store_true")
    sp.add_argument("--flush", dest="flush", action="store_true")
    sp.add_argument("--verdict-file", dest="verdict_file", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("pr", add_help=False)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("approve", add_help=False)
    sp.add_argument("--phase", dest="phase", default="06", choices=["06"])
    sp.add_argument("--revoke", dest="revoke", action="store_true")
    sp.add_argument("--auto", dest="auto", action="store_true")
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("mask", add_help=False)
    sp.add_argument("--file", dest="file", required=True)
    sp.add_argument("--out", dest="out", required=True)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("precheck", add_help=False)
    sp.add_argument("--scope", dest="scope", default="pr",
                    choices=["pr", "worktree"])
    sp.add_argument("--phase", dest="phase", default="05", choices=["05", "06"])
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("contract-trace", add_help=False)
    sp.add_argument("--contract", dest="contract", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("record", add_help=False)
    sp.add_argument("--phase", dest="phase", required=True)
    # `--failed` 는 산출물이 없는 신고라 `--file` 을 요구할 수 없다. 대신
    # `run_record` 가 "둘 중 하나는 있어야 한다" 를 강제한다.
    sp.add_argument("--file", dest="file", default=None)
    sp.add_argument("--reviewer", dest="reviewer", default=None)
    sp.add_argument("--round", dest="round", type=int, default=None)
    sp.add_argument("--failed", dest="failed", action="store_true")
    sp.add_argument("--reason", dest="reason", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    return p


HANDLERS = {
    "doctor": cmd_doctor,
    "init": cmd_init,
    "next": cmd_next,
    "record": cmd_record,
    "gate": cmd_gate,
    "advance": cmd_advance,
    "retry": cmd_retry,
    "escalate": cmd_escalate,
    "resume": cmd_resume,
    "status": cmd_status,
    "abandon": cmd_abandon,
    "lint-phases": cmd_lint_phases,
    "contract-trace": cmd_contract_trace,
    "precheck": cmd_precheck,
    "mask": cmd_mask,
    "approve": cmd_approve,
    "pr": cmd_pr,
    "promote": cmd_promote,
    "review07": cmd_review07,
    "report": cmd_report,
    "cost": cmd_cost,
}


def main(argv=None):
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        return st.emit(st.envelope(
            "usage", False, 2, None, {"unknown": unknown},
            "알 수 없는 인자: %s" % " ".join(unknown), None))
    handler = HANDLERS.get(args.cmd)
    if handler is None:
        return st.emit(st.envelope(
            "usage", False, 2, None, {"commands": sorted(HANDLERS)},
            "커맨드를 지정한다: %s" % ", ".join(sorted(HANDLERS)), None))
    return handler(resolve_root(), args)


def resolve_root():
    """설정이 있는 리포 루트를 찾는다.

    cwd 우선인 것이 요점이다 — 테스트가 임시 리포를 cwd 로 주고 부르므로,
    모듈 위치를 먼저 보면 실물 `_workspace/` 를 건드리게 된다.
    """
    cwd = Path.cwd()
    if (cwd / harness.CONFIG_REL).exists():
        return cwd
    r = harness._git(cwd, "rev-parse", "--show-toplevel")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        top = Path(r.stdout.strip())
        if (top / harness.CONFIG_REL).exists():
            return top
    return ROOT


if __name__ == "__main__":
    sys.exit(main())
