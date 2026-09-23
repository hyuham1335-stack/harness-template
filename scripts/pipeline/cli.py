#!/usr/bin/env python3
"""8페이즈 feature-pipeline 의 진입점.

    python scripts/pipeline/cli.py <cmd> [옵션]

**stdout 은 언제나 단일 JSON 봉투 하나다.** 진단·러너 출력은 stderr 로 간다.
모델이 읽는 것은 봉투의 `render` 와 `next_command` 둘뿐이고, 다른 필드로
판단하기 시작하면 이 계약이 깨진다.

이 파일이 `scripts/pipeline/` 을 패키지로 만들지 않는 이유: 페이즈 파일과 README 가
`next_command` 를 `python scripts/pipeline/cli.py …` 로 문자 그대로 적어 두었다.
봉투가 내는 명령 전문이 스펙이므로 직접 스크립트 실행이 계약이다.

종료 코드는 README 의 종료 코드표를 따른다:
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

# 프론트매터 어휘. 늘리려면 여기와 페이즈 파일을 함께 고친다.
REQUIRES_KINDS = ("file", "state", "adapter_stage")
PRODUCES_KINDS = ("json", "markdown")
# `produces[]` 의 키 집합도 닫는다 — `FRONT_KEYS` 가 최상위에 하는 일과 대칭이다.
# 한때 여기 `schema` 가 열 곳에 있었는데 읽는 코드도 그 이름의 아티팩트도
# 없었다 (ADR-H073). 집합을 닫아야 근거 없는 어휘가 되돌아오지 못한다.
PRODUCES_KEYS = ("key", "path", "kind", "owner", "min_bytes", "must_contain",
                 "unless")
# `produces[].owner` — 누가 쓰는가. `executor` 는 실행기 산출물이라 봉투의 「쓸 파일」에
# 싣지 않는다 (ADR-H076 B′). 없으면 `main` 이다.
PRODUCES_OWNERS = ("main", "executor")
FRONT_KEYS = ("id", "index", "requires", "produces", "review", "converge",
              "submit_checks", "gate", "loop", "trace_loop", "allow", "on_success")
# 하위 키도 닫는다 — 최상위만 닫혀 있어 `rerun_failed_once`·`assert_tests_ran` 같은
# 키가 네 페이즈에 살면서 한 번도 안 읽혔다 (ADR-H076 B).
REVIEW_KEYS = ("unless", "reviewers")
REVIEWER_KEYS = ("code", "agent")
GATE_KEYS = ("runner", "fail_fast", "steps")
GATE_STEP_KEYS = ("id", "tests_from", "loop_stage")
CONVERGE_KEYS = ("blocking_severities", "focus_round_2")
LOOP_KEYS = ("counter", "max", "max_by_profile", "on_exceed")
REQUIRED_SECTIONS = ("## 목적", "## 진입 조건", "## 절차",
                     "## 제출 형식", "## 금지", "## 실패 시")
ROLE_TEMPLATE_SECTION = "## 역할 프롬프트 템플릿"

_PLACEHOLDER = re.compile(r"\$\{([a-zA-Z0-9_.\[\]]+)\}")
_NAMESPACES = ("config", "run")

# lint 는 런 없이 돈다. 경로 길이를 최악으로 재기 위한 자리표시자다 —
# run_id 18자 + slug 40자로 채운 값이 240자 상한을 넘지 않아야 한다.
LINT_RUN_ID = "20260101-0000-0000"
LINT_SLUG = "s" * 40

# 루프 선언의 어휘. **늘리려면 그 동작을 먼저 만든다** — 없는 기계를 어휘로
# 예고하는 것이 M36 이 이름한 결함 그 자체다.
LOOP_ON_EXCEED = ("escalate",)

# 종료 코드의 어휘. 정본은 README 의 종료 코드표이고 여기는 그것을 코드로
# 내린 것이다 — 새 값을 여기서 만들지 않는다.
EXIT_CODES = tuple(range(12))

# 제출 검사의 어휘와 **그 검사를 실제로 내는 자리**. `LOOP_ON_EXCEED` 와 같은
# 규율이다 — **늘리려면 그 동작을 먼저 만든다** (M36 · ADR-H025).
#
# `impl` 은 `"모듈:이름"` 이고 `lint-phases` 가 `_resolve_submit_impl` 로
# **해석한다**(호출하지는 않는다). 주석이 아니라 기계가 대조하는 사실이라야
# 레지스트리가 기계의 소재에 대해 거짓말할 수 없다.
#
# **증명하는 것**: 그 검사의 구현 자리가 아직 있다.
# **증명하지 않는 것**: 그 선언이 그 런에서 실제로 돌았다. 그것은 기록자가
# 자기가 돌린 id 를 영수증으로 남겨야 말할 수 있고 미구현 백로그 31 이다.
# 반쪽짜리 보증을 온전한 것처럼 적지 않는다.
#
# `exit` 는 페이즈 파일의 `on_fail` 과 대조된다. **이 값이 두 번째 하드코딩이
# 되지 않게 하는 것은 lint 가 아니라 테스트다** — `test_the_registry_exit_is_
# the_exit_a_real_run_returns` 가 실제 런의 exit 와 여기를 잇는다.
#
# 인라인 검사는 그것을 품은 기록자를 가리킨다. **거칠다** — `_record_06` 은
# PR 번호 검사 말고도 여럿을 하므로, 이 포인터는 "이 함수 안에 있다" 이지
# "이 함수가 그 검사다" 가 아니다.
SUBMIT_CHECKS = {
    "reviewer_not_main": {
        "exit": 8, "impl": ("verdict:check_vocabulary",),
        "why": "작성자가 자기 글을 리뷰한 것은 독립 관측이 아니다"},
    "source_quote_substring": {
        "exit": 8, "impl": ("verdict:check_review",),
        "why": "리뷰어의 인용이 자기 원문 `.raw.md` 에 실재하는가 — 옮겨 적는 쪽이 "
               "지어내거나 바꾸지 않았는가"},
    "raw_json_severity_match": {
        "exit": 8, "impl": ("verdict:check_review",),
        "why": "원문의 심각도 헤딩 수와 findings 수가 맞는가 — 1라운드 수렴을 "
               "허용하는 만큼 \"Major 0건\" 이 진짜인지 묻는 것이 이것뿐이다"},
    "monotonicity": {
        "exit": 8, "impl": ("verdict:check_review",),
        "why": "이전 회차의 열린 지적이 조용히 증발하지 않았는가 (05 의 델타 라운드)"},
    "false_positive_evidence": {
        "exit": 8, "impl": ("cli:_record_01_review",),
        "why": "메인이 리뷰어의 지적을 기각하려면 findings 안의 id · 사유 · 리포에 실재하는 "
               "경로를 근거로 대야 한다 — 기록 없는 기각은 지적의 증발이다"},
    "tests_required": {
        "exit": 8, "impl": ("cli:_tests_required",),
        "why": "계약의 유닛·진입점·오류 어휘에 대응하는 테스트가 있는가"},
    "pr_number_is_int": {
        "exit": 8, "impl": ("cli:_record_06",),
        "why": "PR 번호가 정수인가 — 문자열 번호는 뒤에서 조용히 안 맞는다"},
    "pr_state_vocabulary": {
        "exit": 8, "impl": ("cli:_record_06",),
        "why": "`state` 가 `PR_STATES` 안인가"},
    "pr_number_stable": {
        "exit": 8, "impl": ("cli:_record_06",),
        "why": "기록된 PR 번호와 제출이 갈라지지 않았는가"},
}


def _resolve_submit_impl(ptr):
    """`"모듈:이름"` 을 콜러블로 해석한다. 없으면 `None`. **호출하지 않는다.**

    `cli` 는 `import_module` 로 다시 읽지 않는다 — 스크립트로 돌 때 이 모듈은
    `__main__` 이라, 이름으로 다시 읽으면 같은 파일이 **두 번째 모듈 객체**로
    올라온다.
    """
    import importlib

    mod_name, _sep, attr = ptr.partition(":")
    if mod_name == "cli":
        mod = sys.modules[__name__]
    else:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            return None
    return getattr(mod, attr, None)


class ConfigDeclarationError(ValueError):
    """루프 선언이 없거나 어휘 밖이다. **기본값으로 낙하하지 않는다.**

    M36: `on_exceed` 와 루프 상한이 전부
    프론트매터에만 있고 코드는 하드코딩된 값을 썼다. **지금 동작이 선언값과
    우연히 일치해서** 다섯 런 동안 아무도 눈치채지 못했고, 선언을 고치면
    조용히 무시됐다. 읽되, 읽을 것이 없으면 멈춘다 — `or` 폴백을 두면 그
    폴백이 곧 새 하드코딩이다.
    """

    def __init__(self, phase_id, key, detail, scope="loop"):
        self.phase_id, self.key, self.scope = phase_id, key, scope
        super(ConfigDeclarationError, self).__init__(
            "`%s` 의 `%s.%s` %s" % (phase_id, scope, key, detail))


def _loop_counter(front, key="loop"):
    """`loop.counter`. 어휘는 `state.COUNTERS` 다. `key` 는 05 의 `trace_loop` 처럼
    같은 모양의 둘째 루프 선언을 읽을 때 준다."""
    got = (front.get(key) or {}).get("counter")
    if not got:
        raise ConfigDeclarationError(front.get("id"), "counter", "가 없다", key)
    if got not in st.COUNTERS:
        raise ConfigDeclarationError(
            front.get("id"), "counter",
            "가 어휘 밖이다: %r (%s)" % (got, ", ".join(st.COUNTERS)), key)
    return got


def _loop_max(front, profile=None, key="loop"):
    """`loop.max`, 또는 프로파일별이면 `loop.max_by_profile[profile]`."""
    loop = front.get(key) or {}
    by = loop.get("max_by_profile")
    if by:
        got = by.get(profile) or by.get("normal")
        if not got:
            raise ConfigDeclarationError(
                front.get("id"), "max_by_profile",
                "에 %r 도 `normal` 도 없다" % (profile,), key)
        return got
    got = loop.get("max")
    if not got:
        raise ConfigDeclarationError(front.get("id"), "max",
                                     "도 `max_by_profile` 도 없다", key)
    return got


def _loop_on_exceed(front, key="loop"):
    """`loop.on_exceed`. **어휘가 하나뿐인 것은 사실이다** — 둘째 동작이 없다.

    값을 늘리는 것은 그 동작을 구현한 뒤의 일이다. 지금 늘리면 선언이 다시
    기계 사실을 참칭한다.
    """
    got = (front.get(key) or {}).get("on_exceed")
    if got not in LOOP_ON_EXCEED:
        raise ConfigDeclarationError(
            front.get("id"), "on_exceed",
            "가 어휘 밖이다: %r (%s)" % (got, ", ".join(LOOP_ON_EXCEED)), key)
    return got


def _converge_blocking(front):
    """`converge.blocking_severities` — 라운드를 강제하는 심각도 (ADR-H041).

    코드에 박지 않는다. 문턱은 선언이 말하고, 선언이 없으면 exit 2 다.
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

def build_context(root, paths=None, state=None, config=None):
    """`${config|run}` 두 네임스페이스. **state 는 여기에 없다.**

    state 는 `${}` 보간 대상이 아니라 `pointer`·`unless` 의 조건식 전용이다.
    보간 대상으로 만들면 페이즈 파일이 런 중 상태를 문자열로 끌어다 쓰기 시작하고,
    그러면 같은 페이즈 파일이 런마다 다른 것을 뜻하게 된다.
    """
    root = Path(root)
    if config is None:
        config = harness._read_json(root / harness.CONFIG_REL)

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
        "run": {"id": run_id, "dir": run_dir, "slug": slug,
                "contract_file": template.replace("{slug}", slug)},
    }


def _lookup(ctx, dotted):
    ns = dotted.split(".")[0]
    if ns not in _NAMESPACES:
        raise PlaceholderError(
            "`%s` 는 참조할 수 없는 네임스페이스다 — %s 둘만 쓴다"
            % (ns, "·".join(_NAMESPACES)))
    node = ctx
    for part in dotted.split("."):
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            # `config.reviewers.0.code` — 05 산출물 이름의 리뷰어 code (ADR-H076 B′)
            node = node[int(part)]
            continue
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
        _config, adapter = adapters.load(root)
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


_REQUIRE_HANDLERS = {
    "file": _req_file,
    "state": _req_state,
    "adapter_stage": _req_adapter_stage,
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

    # ① 작업 공간이 무시되는가. 아니면 런 폴더·계약 파일이 변경 집합과 지문에
    #    섞여 05 라우팅과 06 지문 대조가 어긋난다.
    r = harness._git(root, "check-ignore", "-q", "%s/probe" % st.WORKSPACE_REL)
    if r is None:
        out.append({"name": "작업 공간 무시", "status": "SKIP",
                    "message": "git 을 부를 수 없다"})
    elif r.returncode == 0:
        out.append({"name": "작업 공간 무시", "status": "PASS"})
    else:
        out.append({"name": "작업 공간 무시", "status": "FAIL",
                    "message": "%s/ 가 VCS 무시 목록에 없다. 런 폴더와 계약 파일이 "
                               "변경 집합·지문에 섞여 05 라우팅과 06 지문 대조가 어긋난다."
                               % st.WORKSPACE_REL})

    # ② 역할 에이전트 정의. 계약 계층에서는 경고지만 여기서는 차단이다 —
    #    03 이 그 파일 없이 돌 수 없다.
    try:
        config = harness._read_json(root / harness.CONFIG_REL)
    except (OSError, ValueError):
        config = {}
    wanted = [(r_.get("agent"), "03-implement 가 호출할 대상이다")
              for r_ in (config.get("roles") or [])]
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

    # ④ 계약 템플릿이 **자기 파서를 통과하는가.** 계약 계층의 "계약 절 ↔ 템플릿"
    #    은 절 제목 일치만 본다 — 템플릿이 시범 보이는 **형태**가 파서를 속이면
    #    첫 계약이 그 형태를 베끼고, 유닛이 부풀어 스코프 선택이 조용히 빗나간다.
    out.append(_check_template_parses(root, config))

    # ⑤ 리뷰어. 05 가 없는 에이전트를 부르면 라운드마다 헛돌고, 그것을 알게 되는
    #    시점은 리뷰어를 이미 띄운 뒤다. **기동 전에, 무료로 잡는다** (§E10).
    out.append(_check_reviewers(root, config))

    # ⑥ 원격과 base. 06 이 진입할 때 exit 9(3지선다)로 멈추는 것을 **기동 전에,
    #    무료로** 알려 준다 (§P3). 여기서 막지는 않는다 — 원격 없이 로컬까지만
    #    가는 것도 정당한 선택이고, 그 선택은 사람의 것이다.
    out.append(_check_remote(root, config))
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


def _check_reviewers(root, config):
    import review as review_mod

    name = "리뷰어 에이전트"
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
        config, adapter = adapters.load(root)
    except (OSError, ValueError, KeyError) as exc:
        add(harness.CONFIG_REL, "config", "FAIL", "설정·어댑터를 읽지 못했다: %s" % exc)
        return out
    ctx = build_context(root, config=config)

    _lint_runner_bin(root, adapter, config, add)
    _lint_infra_preflight(adapter, config, add)

    seen_index, seen_keys, terminals = {}, {}, []
    declared_checks = set()
    max_index = max((p["front"].get("index") or 0) for p in loaded.values())

    # 레지스트리 자신이 먼저 검사 대상이다 — 가리키는 자리가 사라졌으면
    # 페이즈 파일이 아니라 여기가 거짓말하고 있는 것이다.
    for cid, spec in sorted(SUBMIT_CHECKS.items()):
        for ptr in spec["impl"]:
            if not callable(_resolve_submit_impl(ptr)):
                add("cli.py", "submit_check_impl", "FAIL",
                    "SUBMIT_CHECKS[%r] 의 구현 포인터가 해석되지 않는다: %r"
                    % (cid, ptr))

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
            _lint_closed(name, "produces_keys", prod, PRODUCES_KEYS, add, "produces")
            owner = prod.get("owner")
            if owner is not None and owner not in PRODUCES_OWNERS:
                add(name, "produces_owner", "FAIL",
                    "produces.owner 가 어휘 밖이다: %r (%s)"
                    % (owner, ", ".join(PRODUCES_OWNERS)))
            key = prod.get("key")
            if key in seen_keys:
                add(name, "produces_key", "FAIL",
                    "produces.key %r 가 %s 와 겹친다" % (key, seen_keys[key]))
            else:
                seen_keys[key] = pid
        review = front.get("review") or {}
        _lint_closed(name, "review_keys", review, REVIEW_KEYS, add, "review")
        for r in review.get("reviewers") or []:
            _lint_closed(name, "reviewer_keys", r, REVIEWER_KEYS, add, "review.reviewers[]")
        gate = front.get("gate") or {}
        _lint_closed(name, "gate_keys", gate, GATE_KEYS, add, "gate")
        for step in gate.get("steps") or []:
            _lint_closed(name, "gate_step_keys", step, GATE_STEP_KEYS, add, "gate.steps[]")
        _lint_closed(name, "converge_keys", front.get("converge"), CONVERGE_KEYS, add,
                     "converge")
        _lint_closed(name, "loop_keys", front.get("loop"), LOOP_KEYS, add, "loop")
        _lint_closed(name, "loop_keys", front.get("trace_loop"), LOOP_KEYS, add, "trace_loop")
        _lint_submit_checks(name, front, declared_checks, add)
        _lint_loop(name, pid, front, loaded, add)
        _lint_loop(name, pid, front, loaded, add, key="trace_loop")
        _lint_converge(name, front, add)
        _lint_conditions(name, front, add)
        _lint_requires(name, front, loaded, add)

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

    # **WARN 이지 FAIL 이 아니다.** M36 이 금지한 것은 어휘가 기계를 앞서는 한
    # 방향이다 — 기계를 먼저 만들고 선언을 나중에 다는 것은 정당한 순서이고,
    # `impl` 해석이 통과한 이상 그 기계는 실재한다. 여기서 막으면 그 순서가
    # lint 에 걸린다.
    for cid in sorted(set(SUBMIT_CHECKS) - declared_checks):
        add("(전체)", "submit_check_unused", "WARN",
            "SUBMIT_CHECKS[%r] 를 선언하는 페이즈가 없다 — 구현은 있는데(%s) "
            "어느 페이즈도 그 검사를 약속하지 않는다"
            % (cid, " · ".join(SUBMIT_CHECKS[cid]["impl"])))

    _lint_cycle(loaded, add)
    _lint_reviewers(root, config, add)
    return out


# `init` 이 쓰는 런 파일. 어느 페이즈의 produces 에도 본문에도 없지만 requires 가
# 가리킬 수 있다 — 정본은 `state.RunPaths.request` 다.
INIT_WRITTEN = ("00_original_request.md",)


def _lint_requires(name, front, loaded, add):
    """`requires` 가 가리키는 것이 실재하는가.

    `state` 포인터 `phases.<id>.status` 의 `<id>` 는 로드된 페이즈여야 하고,
    `file` 의 `${run.dir}/<이름>` 은 어느 페이즈의 `produces` 나 본문, 또는
    실행기가 `init` 에서 쓰는 목록(`INIT_WRITTEN`)에 있어야 한다. 없으면
    `check_requires` 가 전이를 **조용히 보류**한다 — 파일을 만드는 기계를 지웠는데
    선언이 남은 `08_instruction_review.json` 이 그 모양이었다.
    """
    produced = {Path(p.get("path") or "").name
                for item in loaded.values()
                for p in (item["front"].get("produces") or [])
                if isinstance(p, dict)}
    bodies = "\n".join(item.get("body") or "" for item in loaded.values())
    for req in front.get("requires") or []:
        if not isinstance(req, dict):
            continue
        if req.get("kind") == "state":
            m = re.match(r"^phases\.([^.]+)\.status$", str(req.get("pointer") or ""))
            if m and m.group(1) not in loaded:
                add(name, "requires_phase", "FAIL",
                    "requires 가 없는 페이즈를 가리킨다: %r — 전이가 조용히 보류된다"
                    % req.get("pointer"))
        elif req.get("kind") == "file":
            path = str(req.get("path") or "")
            if not path.startswith("${run.dir}/"):
                continue
            fname = path[len("${run.dir}/"):]
            if fname in produced or fname in INIT_WRITTEN or fname in bodies:
                continue
            add(name, "requires_file", "FAIL",
                "requires 의 %r 를 만드는 자리가 없다 — 어느 페이즈의 produces 에도, "
                "본문에도, 실행기의 init 목록에도 없다. 전이가 조용히 보류된다" % fname)


def _lint_submit_checks(name, front, declared, add):
    """선언된 제출 검사가 어휘 안이고 종료 코드가 레지스트리와 같은가 (ADR-H073).

    `declared` 에 본 id 를 쌓는다 — 호출자가 전 페이즈를 돈 뒤 미선언 항목을
    WARN 으로 남긴다.
    """
    checks = front.get("submit_checks")
    if checks is None:
        return
    if not isinstance(checks, list):
        add(name, "submit_check_shape", "FAIL", "submit_checks 는 배열이어야 한다")
        return
    for c in checks:
        if not isinstance(c, dict) or "id" not in c or "on_fail" not in c:
            add(name, "submit_check_shape", "FAIL",
                "submit_checks 항목은 `id` 와 `on_fail` 을 갖는다: %r" % (c,))
            continue
        cid, got = c["id"], c["on_fail"]
        spec = SUBMIT_CHECKS.get(cid)
        if spec is None:
            add(name, "submit_check_id", "FAIL",
                "알 수 없는 제출 검사 id: %r — 어휘를 늘리려면 그 동작을 먼저 "
                "만들고 `SUBMIT_CHECKS` 에 구현 자리와 함께 올린다 (M36)" % cid)
            continue
        declared.add(cid)
        if got not in EXIT_CODES:
            add(name, "submit_check_exit", "FAIL",
                "%s 의 on_fail 이 종료 코드표 밖이다: %r (README 종료 코드표)"
                % (cid, got))
        elif got != spec["exit"]:
            add(name, "submit_check_exit", "FAIL",
                "%s 의 on_fail 이 %r 인데 구현은 %r 을 낸다 (%s) — 선언과 동작이 "
                "갈라진 채로 남는 것이 M36 이다"
                % (cid, got, spec["exit"], " · ".join(spec["impl"])))


def _lint_converge(name, front, add):
    """`converge.blocking_severities` 가 **읽히는 값**인가 (ADR-H041)."""
    conv = front.get("converge")
    if not conv:
        return
    try:
        _converge_blocking(front)
    except ConfigDeclarationError as exc:
        add(name, "blocking_severities", "FAIL", str(exc))


def _lint_closed(name, rule, obj, keys, add, label):
    """키 집합을 닫는다 — 읽는 코드가 없는 키는 선언이 아니라 장식이다 (ADR-H073)."""
    if not isinstance(obj, dict):
        return
    unknown = [k for k in obj if k not in keys]
    if unknown:
        add(name, rule, "FAIL",
            "%s 에 알 수 없는 키: %s (%s) — 읽는 코드가 없는 키는 선언이 아니라 "
            "장식이다 (ADR-H073)" % (label, ", ".join(sorted(unknown)), ", ".join(keys)))


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


def _lint_loop(name, pid, front, loaded, add, key="loop"):
    """루프 선언이 **읽히는 값**인가.

    M36: `on_exceed` · 상한이 프론트매터에만 있고 코드는
    하드코딩을 썼다. 이제 코드가 읽으므로, 선언이 어휘 밖이면 런 중간이 아니라
    **여기서** 안다. 검사하지 않으면 exit 2 를 런 한복판에서 만난다.
    """
    loop = front.get(key) or {}
    if not loop:
        return
    counter = loop.get("counter")
    if not counter:
        add(name, "counter", "FAIL", "%s.counter 가 없다 — 무엇을 세는지 "
                                     "코드가 읽을 자리가 없다" % key)
    elif counter not in st.COUNTERS:
        add(name, "counter", "FAIL",
            "알 수 없는 카운터: %r (%s)" % (counter, ", ".join(st.COUNTERS)))

    if not loop.get("max") and not loop.get("max_by_profile"):
        add(name, "loop_max", "FAIL",
            "%s.max 도 %s.max_by_profile 도 없다 — 상한이 코드에만 남는다"
            % (key, key))

    on_exceed = loop.get("on_exceed")
    if on_exceed is not None and on_exceed not in LOOP_ON_EXCEED:
        add(name, "on_exceed", "FAIL",
            "%s.on_exceed 가 어휘 밖이다: %r (%s) — **없는 동작을 어휘로 "
            "예고하지 않는다.** 늘리려면 그 동작을 먼저 만든다"
            % (key, on_exceed, ", ".join(LOOP_ON_EXCEED)))


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


def _lint_placeholders(name, front, ctx, add):
    try:
        resolve({k: v for k, v in front.items() if k != "gate"}, ctx)
        for step in (front.get("gate") or {}).get("steps") or []:
            resolve(step, ctx)
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
    """이 페이즈가 부르는 에이전트 파일의 실재 — `allow.agents` 가 가리키는 역할과
    `review.reviewers[].agent`. 후자는 아무도 검사하지 않았다 (ADR-H076 B′)."""
    wanted = []
    if (front.get("allow") or {}).get("agents") == "config.roles[].agent":
        wanted += [role.get("agent") for role in config.get("roles") or []]
    wanted += [r.get("agent") for r in (front.get("review") or {}).get("reviewers") or []]
    for agent in wanted:
        if not agent:
            add(name, "agent_file", "FAIL", "리뷰어에 agent 가 없다 — 누구를 부를지 없다")
            continue
        path = root / ".claude" / "agents" / ("%s.md" % agent)
        if not path.exists():
            add(name, "agent_file", "FAIL",
                ".claude/agents/%s.md 가 없다 — 기동 전에 잡는다" % agent)


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


def _lint_reviewers(root, config, add):
    """에이전트 파일 실재 · 작성자 격리 · code 유니크.

    **기동 전에, 무료로 잡는다** (§E10 첫 행). 05 가 없는 에이전트를 부르면 라운드
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
            % (pid, "\n".join("- %s" % c["message"] for c in failed), ""),
            None)

    if pid == "05-code-review":
        # 리뷰어는 모델 호출이다 — 게이트 안 된 코드에 보내면 그 호출이 낭비다.
        stale = _receipt_stale(root, ctx["config"], s)
        if stale:
            return _receipt_envelope("next", s, stale)

    st.set_phase_status(s, pid, "running")
    st.append_event(paths, "phase_enter", cmd="next", phase=pid)
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
    # **지시를 낸 자리에서 센다** (M26). `next` 는 같은 페이즈에서 여러 번
    # 불릴 수 있으므로 키로 멱등을 만든다.
    st.count_instructions(s, pid, _instruction_keys(s, pid, ctx, phase["front"]))
    st.save(paths, s)

    render, next_cmd = render_packet(root, phase, ctx, s, checks)
    env = st.envelope("next", True, 0, s,
                      {"produces": _model_produces(phase["front"], ctx),
                       "requires_report": checks,
                       # 런 전체의 사전 검사는 **첫 페이즈**에서 한 번.
                       "prescan": _prescan(root, loaded, ctx, s) if pid == "01-plan" else []},
                      render, next_cmd)
    return env


def _note_applied(s, tag):
    """레인이 실제로 적용한 양보를 적는다. `lane_miss` 의 gap 이름이 이것으로 만들어진다."""
    prof = s.setdefault("profile", {})
    applied = prof.setdefault("applied", [])
    if tag not in applied:
        applied.append(tag)


def _note_lane_miss(paths, s, before, after, where, cmd="record"):
    """선언한 레인이 **상향**으로 빗나갔다 — 앞 페이즈가 양보를 적용한 채 지나갔다.

    gap 이름에 실제로 적용된 양보만 넣는다 (재지 않은 것을 적지 않는다).
    반환: `profile.lane_miss` 에 넣을 dict.
    """
    applied = list(before.get("applied") or [])
    gap = "lane_miss:" + (";".join(applied) if applied else "none")
    st.demote(s, st.GRADES[1], gap)
    miss = {"at": where, "was": before.get("name"), "became": after.get("name"),
            "applied": applied}
    st.append_event(paths, "lane_miss", cmd=cmd, phase=where,
                    was=before.get("name"), became=after.get("name"),
                    applied=applied)
    return miss


def _docs_lane_source_check(root, paths, s, ctx, where, source_changed=None,
                            cmd="record"):
    """docs 레인을 선언했는데 소스가 바뀌었는가. 반환: miss 가 났으면 True.

    docs 레인은 계약이 없어 03(claims 제출)과 05(리뷰 계획) 두 자리가 따로 묻는다.
    술어는 05 의 리뷰 계획과 같은 `review._source_changed` 다.
    """
    import precheck as pc
    import review as review_mod

    prof = s.get("profile") or {}
    if prof.get("name") != "docs":
        return False
    if source_changed is None:
        changed = pc.changed_files(root, "worktree")
        source_changed = bool(changed) and review_mod._source_changed(
            ctx["config"], changed)
    if not source_changed:
        return False
    before = dict(prof)
    after = {"name": "normal", "source": "auto",
             "reason": "docs 레인 선언이 빗나갔다 — 역할 소유 경로가 바뀌었다",
             "previous": {k: v for k, v in before.items()
                          if k not in ("previous", "lane_miss")},
             "reconfirmed_at": st.stamp()}
    if "applied" in before:
        after["applied"] = before["applied"]
    after["lane_miss"] = _note_lane_miss(paths, s, before, after, where, cmd)
    s["profile"] = after
    s["contract"] = dict(s.get("contract") or {}, mode="contract",
                         reason="lane_miss")
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


def _plan_05_review(root, paths, s, ctx):
    """05 진입 시 **누가 리뷰할지를 여기서 확정한다.** 리뷰어는 하나다 (ADR-H075).

    모델이 정하지 않는다. `config.reviewers` 의 하나(`gen`)를 소스 변경이 있으면
    계획하고(docs 레인은 문서 변경만으로도), 없으면 0명이다 — 0명은 `review05.status`
    가 `failed` 다. 워킹트리에 소스 변경이 없는데 커밋에는 있으면 라우팅 실패가
    아니라 절차 오류라 exit 3 으로 되돌린다.
    """
    import precheck as pc
    import review as review_mod

    # 04 수리 중 계약 델타가 적용됐을 수 있다 — 해시와 버려진 줄을 다시 적는다.
    noted = _note_contract(root, s, ctx)
    # **변경 집합은 `worktree` 다** (M40 · ADR-H028). 예산은 PR 전체를 재지만
    # 여기까지 넓히면 05 가 브랜치의 앞선 커밋까지 리뷰 대상에 넣는다.
    changed = pc.changed_files(root, "worktree", ctx["config"])
    profile = (s.get("profile") or {}).get("name") or "normal"
    source_changed = review_mod._source_changed(ctx["config"], changed)
    if source_changed and profile == "docs":
        # docs 선언인데 소스가 바뀌었다. 03 이 못 잡은 경로(예: 04 수리 중
        # 메인이 소스를 고쳤다)를 계획 직전에 한 번 더 묻는다 (ADR-H044).
        if _docs_lane_source_check(root, paths, s, ctx, "05-code-review",
                                   source_changed=True, cmd="next"):
            profile = "normal"
    reviewers = [{"code": r["code"], "agent": r["agent"]}
                 for r in ctx["config"].get("reviewers") or []]
    # 메인 소유 파일(`harness/**`·`.claude/**`)만 더러운 워킹트리에서 gen 을
    # 계획하면 「커밋만 있으면 exit 3」 거부 경로가 죽는다 — 소스 변경으로 판정한다.
    planned = ([r["code"] for r in reviewers]
               if (source_changed or (profile == "docs" and changed)) else [])
    node = s.setdefault("phases", {}).setdefault("05-code-review", {})
    # **계획된 리뷰어는 줄지 않는다.** `next --phase 05` 는 여러 번 불릴 수
    # 있고 그때마다 변경 집합을 다시 읽는다. 줄어든 집합으로 덮으면 계획이
    # 조용히 작아진다 — `review05` 가 "런 안에서 좋아지지 않는다" 를 지키는 것과
    # 같은 규율이다.
    kept = [c for c in (node.get("planned") or []) if c not in planned]
    node["planned"] = planned + kept
    node["reviewers"] = reviewers
    node["contract_dropped"] = noted.get("dropped") or []
    # **리뷰 범위는 하나다** (ADR-H059 · ADR-H075). FR-007 의 동시성 결함은 05 가
    # diff 만 봐서 놓쳤다 — 계약이 참조하는 기존 파일까지 본다.
    node["depth"] = (ctx["config"].get("review") or {}).get("depth") or "diff+refs"
    # **인라인 상한은 기계가 정한다** (ADR-H042).
    node["inline"] = review_mod.inline_budget(ctx["config"],
                                              _diff_text(root, changed))
    if not node["planned"]:
        # **커밋만 있고 워킹트리가 깨끗하면 라우팅 실패가 아니라 절차 오류다**
        # (ADR-H046). 파일럿 40dc 가 05 통과 전에 커밋해 여기서 0명이 되고
        # `review05:failed` 가 append-only 로 박혔다. `pr` scope 에 변경이 있으면
        # failed 를 쓰지 않고 호출자(`run_next`)가 exit 3 을 낸다.
        committed = sorted(set(pc.changed_files(root, "pr", ctx["config"]))
                           - set(changed))
        if committed:
            node["routing_refused"] = {"pr_scope_changed": committed}
            return node
        # **여기서 확정하지 않으면 아무도 확정하지 않는다.** 리뷰어가 0명이면
        # 제출도 0건이고 `_judge_05` 가 아예 안 불린다 — 05 가 조용히 지나간다.
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
    # 접는 방식이 셋 다 다르다 — 근거는 M43·M53 (DECISIONS.md) 이다.
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
        # 지시된 범위다 — 리뷰어가 실제로 참조 파일을 읽었는지는 실행기가 못 본다.
        "depth": node.get("depth"),
        "major": sum(1 for f in merged if f.get("severity") in verdict.BLOCKING),
        # **0 은 신호다** (ADR-H050). 07 이 "05 가 ok 이고 Major 가 없다" 만 보고
        # 생략하면 리뷰어 넷이 전부 0건을 낸 런(파일럿 9729 · 3305)이 자동
        # 게이트 말고는 아무 눈도 안 받는다. 그래서 총계를 따로 남긴다.
        "findings_total": len(merged),
        "need_more_context": _dedup_ordered(
            n for v in subs for n in (v.get("need_more_context") or [])),
        "truncated": any(v.get("truncated") for v in subs),
    }
    if status != "ok":
        st.demote(s, st.GRADES[1], "review05:%s" % status)
    return prev


def _contract_drift_lines(node, s):
    """파서가 흘린 계약 줄 — **조용하면 안 되는 사실**이다. 흘린 줄은 스코프
    선택을 빗나가게 한다 (D-2)."""
    out = []
    dropped = node.get("contract_dropped") or []
    if dropped:
        out += ["**계약의 %d줄이 유닛으로 세어지지 않았다** — 파서는 "
                "`컨테이너 · 심볼` 쌍을 요구한다. 이 줄들은 스코프 선택에 "
                "들어가지 않는다:" % len(dropped), ""]
        out += ["- `%s` — %s" % (d.get("raw"), d.get("reason"))
                for d in dropped[:5]]
        out += [""]
    return out


def _review_render(s):
    """봉투가 **누가 리뷰하는지와 무엇이 빠졌는지**를 말한다.

    **이 라운드의 계획만 이름 짓는다** (백로그 20). 델타 재리뷰는 같은 한 명이다 —
    봉투가 부르는 사람과 `_planned_guard` 가 받는 사람이 같아야 한다.
    """
    node = (s.get("phases") or {}).get("05-code-review") or {}
    if "planned" not in node:
        return ""
    lines = ["## 리뷰어 (결정론 — 네가 정하지 않는다)", ""]
    lines += _contract_drift_lines(node, s)
    if not node.get("planned"):
        lines += ["**계획된 리뷰어가 0명이다.** 소스 변경이 없다 — `review05.status` 는 "
                  "`failed` 이고 등급이 `PASS_WITH_GAPS` 로 떨어진다. 아무도 안 부른 것은 "
                  "통과가 아니라 미수행이다."]
        return "\n".join(lines)
    round_ = ((s.get("counters") or {}).get("review_repair") or {}).get("used", 0) + 1
    planned = _planned_for_round(node, round_)
    by_code = {r.get("code"): r for r in node.get("reviewers") or []}
    # 값은 하나다 (ADR-H059 · ADR-H075) — `diff` 분기는 죽은 어휘였다 (ADR-H076 B).
    lines.append("리뷰 범위: **diff+refs** — 계약 `## 유닛` 이 참조하는 **기존** "
                 "파일을 리뷰어 패킷에 경로로 넣어라. diff 밖 상호작용(낙관적 "
                 "잠금 · 상태 가드 · 기존 전이 함수)을 보는 것이 이 범위의 "
                 "목적이다 — 05 가 놓치고 07 이 잡은 것이 그 자리였다 (FR-007).")
    lines.append("")
    for code in planned:
        agent = (by_code.get(code) or {}).get("agent") or code
        lines.append("- `%s` → Agent 호출 `subagent_type: %s` (`.claude/agents/%s.md`)"
                     % (code, agent, agent))
    lines += ["", "관점·제출 형식은 에이전트 정의가 든다 — 본문을 프롬프트에 복사하지 마라. "
                  "`model` 인자를 주지 마라 — 모델·effort 는 프론트매터가 정한다 (ADR-H061)."]
    inline = node.get("inline") or {}
    if inline and not inline.get("inline"):
        lines += ["", "**diff 를 인라인하지 마라 — 경로로 전달한다.** 인라인 "
                      "상한(`review.inline_max`)을 넘었다: %s. 리뷰어 패킷의 "
                      "`## 변경` 절에 diff 대신 변경 파일 경로 목록을 싣고, "
                      "리뷰어가 그 파일만 읽게 한다. 이 사실은 상태에 남는다."
                  % " · ".join(inline.get("over") or [])]
    return "\n".join(lines)


def _model_produces(front, ctx):
    """모델이 쓰는 산출물 경로 — `owner: executor`(실행기가 쓴다)는 뺀다 (ADR-H076 B′)."""
    return [resolve(p.get("path"), ctx) for p in front.get("produces") or []
            if p.get("owner") != "executor"]


def render_packet(root, phase, ctx, s, checks=None):
    front, body = phase["front"], phase["body"]
    pid = front["id"]
    parts = [render_header(ctx["config"], s), ""]
    parts.append(_section(body, "## 목적"))
    parts.append(_section(body, "## 절차"))
    if pid == "01-plan" and not _reviewers_for(front, s):
        parts.append(
            "## 리뷰어 — 0명 (%s 레인)\n\n이 런은 플랜 리뷰어를 부르지 않는다. "
            "플랜 제출이 이 페이즈의 전부이고 1라운드에 닫힌다. 리뷰어를 부르지 "
            "마라 — 라우팅 밖의 제출은 받지 않는다."
            % ((s.get("profile") or {}).get("name")))
    role_tpl = _section(body, "## 역할 프롬프트 템플릿")
    if role_tpl and pid == "03-implement" and not _roles_for(front, ctx, s):
        parts.append(
            "## 역할 — 0명 (%s 레인)\n\n이 런은 역할 에이전트를 부르지 않고 "
            "계약도 쓰지 않는다 (`no_contract`). **네가 직접** 문서를 고치고 "
            "`03_claims.json` 을 `{\"schema\":1,\"roles\":[]}` 로 낸다. 역할 소유 "
            "경로(소스)를 건드리면 제출이 exit 3 으로 되돌아온다 — 그때는 "
            "선언이 빗나간 것이고 계약을 쓰고 역할 패킷을 받는다."
            % ((s.get("profile") or {}).get("name")))
    elif role_tpl:
        parts.append(role_tpl)
        if pid == "03-implement":
            parts.append(_tests_required_render(root, ctx, s))
    parts.append(_section(body, "## 제출 형식"))
    parts.append(_section(body, "## 금지"))

    produces = _model_produces(front, ctx)
    if produces:
        parts.append("## 쓸 파일\n\n" +
                     "\n".join("- `%s`" % p for p in produces))
    if pid == "05-code-review":
        rv_render = _review_render(s)
        if rv_render:
            parts.append(rv_render)
    if pid == "07-pr-review":
        # 07 이 05 와 같은 결함에 다른 이름을 붙이면 새 것으로 세어진다.
        # 목록을 봉투가 직접 준다 — 모델이 재구성하면 그 재구성이 곧 결함이다 (M48).
        parts.append(_keys_from_05_render(_keys_from_05(s)))
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

    if ((s.get("contract") or {}).get("mode")) == "no_contract":
        return ""
    full = Path(root) / resolve("${run.contract_file}", ctx)
    if not full.exists():
        return ""
    parsed = contract_mod.parse(full.read_text(encoding="utf-8"), ctx["config"])
    eps, errors = parsed.get("entrypoints") or [], parsed.get("errors") or []
    if not eps and not errors:
        return ""
    role = ctx["config"].get("primary_role") or "impl"
    lines = ["## 게이트가 세는 테스트 — `%s` 역할" % role, "",
             "계약에서 기계로 뽑은 목록이다. 03 제출과 05 계약 대조가 **같은 목록**을 "
             "센다 — 빠지면 03 제출이 거부된다.", ""]
    for ep in eps:
        lines.append("- 진입점 `%s` — 그 진입점 파일 옆의 같은 이름 테스트, 또는 그 "
                     "파일을 import 하는 테스트" % ep.get("raw"))
    for name in errors:
        lines.append("- 오류 어휘 `%s` — 이 상수를 단언하는 테스트" % name)
    return "\n".join(lines)


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
            "`%s` 는 이미 통과했다. **record 는 멱등이 아니다** — "
            "`next --run-id %s` 로 현재 페이즈의 지시를 본다."
            % (pid, s["run_id"]),
            "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])

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
    # **제출을 세지 않는다** (M26). 계수는 `next`·`gate` 가 기동을
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

    if pid == "01-plan":
        r = used("round")
        return ["01:r%d:%s" % (r, code) for code in _reviewers_for(front, s)]
    if pid == "03-implement":
        r = used("repair")
        return ["03:r%d:%s" % (r, role.get("id")) for role in _roles_for(front, ctx, s)]
    if pid == "05-code-review":
        node = (s.get("phases") or {}).get("05-code-review") or {}
        r = used("review_repair") + 1
        planned = _planned_for_round(node, r)
        return ["05:r%d:%s" % (r, c) for c in planned]
    if pid == "07-pr-review":
        # `/code-review` 1회. 스킬 호출이라 `record --reviewer` 를 남기지 않는다 —
        # 지시 기준으로 세야 표본에 들어온다 (M26).
        return ["07:code-review"]
    return []


def _normalize_phase(phase, loaded):
    """`04` 와 `04-gate` 를 둘 다 받는다."""
    if phase in loaded:
        return phase
    for pid in loaded:
        if pid.split("-")[0] == str(phase).zfill(2):
            return pid
    return None


def _close_run(root, paths, s, phase_item, ctx, cmd):
    """마지막 페이즈 통과 → 런 종료. **`done` 으로 옮기는 자리는 여기 하나다.**

    `st.close_run` 이 `run_status` 의 단일 출처이고, 종단 상태를 인자로 받는다 —
    등급이 세 곳에서 대입되던 것을 `st.demote` 로 모은 것과 같은 규율이다
    (ADR-H015).
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


def _advance_to_next(root, paths, s, phase_item, ctx, cmd="record"):
    """통과 시 전이하고 **다음 페이즈 지시문을 바로 낸다** (왕복 절약)."""
    pid = phase_item["front"]["id"]
    nxt = phase_item["front"].get("on_success")
    if nxt == st.DONE:
        return _close_run(root, paths, s, phase_item, ctx, cmd)

    st.set_phase_status(s, pid, "passed")
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
                                        for c in nxt_checks if not c["ok"]), ""),
                           "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])
    st.set_phase_status(s, nxt, "running")
    st.append_event(paths, "phase_enter", cmd=cmd, phase=nxt)
    # **지시를 낸 자리에서 센다.** 전이가 다음 패킷을 바로 내므로 `next` 의
    # 계수를 지나친다 (ADR-H042).
    st.count_instructions(s, nxt, _instruction_keys(s, nxt, ctx, nxt_item["front"]))
    st.save(paths, s)
    render, next_cmd = render_packet(root, nxt_item, ctx, s, nxt_checks)
    env = st.envelope(cmd, True, 0, s, {"next_phase": nxt}, render, next_cmd)
    return env


# ------------------------------------------------------- 01 제출 처리

def _record_01(root, paths, s, phase_item, ctx, file, reviewer, round_):
    if reviewer:
        return _record_01_review(root, paths, s, phase_item, ctx, file,
                                 reviewer, round_ or 1)
    return _record_01_plan(root, paths, s, phase_item, ctx, file)


def _record_01_plan(root, paths, s, phase_item, ctx, file):
    """플랜 제출. 기계가 보는 것은 파일의 존재와 크기(`produces`)뿐이다 —
    내용은 plan-reviewer 가 리포를 읽으며 본다."""
    if not file.exists():
        return st.envelope("record", False, 3, s, {}, "산출물이 없다: %s" % file, None)
    node = s.setdefault("phases", {}).setdefault("01-plan", {})
    node["plan_accepted"] = True
    st.set_phase_status(s, "01-plan", "running")   # node 는 상태 안의 같은 dict 다
    st.save(paths, s)
    codes = _reviewers_for(phase_item["front"], s)
    if not codes:
        # docs 레인 — 리뷰어 0명 (ADR-H044). 플랜 제출이 이 페이즈의 전부이고
        # 1라운드에 닫는다. 정책 스킵이라 등급은 안 내려가지만 **`applied` 에
        # 남아** 선언이 빗나가면 gap 이름이 된다.
        _note_applied(s, "01:reviewers=0")
        rounds = node.setdefault("rounds", {})
        st.save(paths, s)
        return _judge_round(root, paths, s, phase_item, ctx, 1, {}, rounds)
    return st.envelope(
        "record", True, 0, s,
        {"round": _round_no(s), "reviewers": codes},
        "## 플랜을 받았다\n\n리뷰어 `%s` 를 돌린다. 회차마다 원문 `.raw.md` 와 "
        "구조화 `.json` 을 함께 낸다. 리뷰어는 **리포를 읽을 수 있다** — 플랜이 "
        "가리키는 파일을 열어 근거를 확인한다. 받은 Critical 이 틀렸다고 보면 "
        "`record` 전에 JSON 최상위에 `false_positive: [{id, reason, evidence}]` 를 "
        "달아 낸다 — `evidence` 는 리포에 실재하는 경로다."
        % "`, `".join(codes),
        "python scripts/pipeline/cli.py record --phase 01 --file <리뷰 json> "
        "--reviewer <code> --round %d --run-id %s" % (_round_no(s), s["run_id"]))


def _false_positive_errors(root, payload):
    """메인의 기각(`false_positive`)에 근거가 있는가. 반환: [오류].

    셋이 다 있어야 한다 — 이 회차 findings 안의 `id` · 비지 않은 `reason` ·
    리포에 실재하는 경로 `evidence`(`path[:줄]`). 사유의 진위는 기계가 못 본다 —
    기록이 억지력이고, 리뷰어가 리포를 읽을 수 있는 것이 가짜 Critical 의
    1차 방어다.
    """
    ids = {f.get("id") for f in payload.get("findings") or []}
    errors = []
    for fp in payload.get("false_positive") or []:
        if not isinstance(fp, dict):
            errors.append("false_positive 항목은 {id, reason, evidence} 객체다: %r" % (fp,))
            continue
        fid = fp.get("id")
        if fid not in ids:
            errors.append("false_positive %r 가 이 회차 findings 에 없다 — 기각은 "
                          "리뷰어가 낸 지적에만 한다" % (fid,))
        if not str(fp.get("reason") or "").strip():
            errors.append("false_positive %r 에 reason 이 없다" % (fid,))
        ev = str(fp.get("evidence") or "").strip()
        rel = re.sub(r":\d+$", "", ev)
        if not ev or not (Path(root) / rel).exists():
            errors.append("false_positive %r 의 evidence 가 리포에 없다: %r — "
                          "실재하는 경로(`path[:줄]`)를 근거로 댄다" % (fid, ev))
    return errors


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
    blocking = _converge_blocking(phase_item["front"])
    # `previous_open=[]` — 01 은 단조성을 끈다. 다음 회차에 그 지적이 안 나오면
    # 닫힌 것이고, 남았으면 리뷰어가 다시 낸다.
    got = verdict.check_review(payload, raw_text, [], blocking)
    errors = got["errors"] or _false_positive_errors(root, payload)
    if errors:
        st.append_event(paths, "check_fail", cmd="record", phase="01-plan",
                        reviewer=reviewer, errors=len(errors))
        st.save(paths, s)
        return st.envelope("record", False, 8, s, {"errors": errors},
                           "## 리뷰 제출 거부\n\n" +
                           "\n".join("- %s" % e for e in errors),
                           _same_command(s, "01"))

    # 기각된 지적은 `findings[]` 와 원문에 그대로 남는다(헤딩 대조가 전체를
    # 센다) — 차단 계수에서만 빠지고, 어느 것을 왜 기각했는지가 기록으로 남는다.
    dismissed = {fp["id"] for fp in payload.get("false_positive") or []}
    keys = [dict(k, false_positive=True) if k["id"] in dismissed else k
            for k in got["keys"]]
    slot = rounds.setdefault(str(round_), {})
    slot[reviewer] = {"keys": keys, "closed": got["closed"],
                      "blocking": sum(1 for k in keys if k["severity"] in blocking
                                      and not k.get("false_positive")),
                      "false_positive": [dict(fp) for fp in
                                         payload.get("false_positive") or []]}
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


def _keys_from_05(s):
    """05 가 낸 지적 전부 — 07 의 `dup_05` 가 가리킬 대상이다.

    원천은 `phases.05-code-review.rounds` 하나다. `05_review.json` 은 라운드마다
    덮어써져 델타 라운드 뒤엔 델타 리뷰어의 것만 남는다 — `pr._minor_open` 이
    같은 이유로 `review.open_findings` 를 쓴다. 같은 키는 첫 등장이 이기고,
    어느 라운드에서든 닫힌 키는 `closed` 로 표시한다. 실패 슬롯(`keys is None`)은
    읽지 않는다.
    """
    node = (s.get("phases") or {}).get("05-code-review") or {}
    rounds = node.get("rounds") or {}
    seen, closed = {}, set()
    for rn in sorted(rounds, key=int):
        for code, sub in (rounds[rn] or {}).items():
            if sub.get("keys") is None:
                continue
            for f in sub.get("findings") or []:
                key = verdict.finding_key(f)
                seen.setdefault(key, {"key": key, "severity": f.get("severity"),
                                      "reviewer": code,
                                      "title": f.get("title") or "제목 없음"})
            closed |= set(sub.get("closed") or [])
    return [dict(v, closed=v["key"] in closed) for v in seen.values()]


def _keys_from_05_render(keys):
    """봉투가 목록을 직접 준다 — 모델이 재구성하면 그 재구성이 곧 결함이다."""
    if not keys:
        return ("## 05 가 낸 지적\n\n(없다) — 여기서 잡는 것은 전부 새 것이다. "
                "`finding_key` 를 달지 마라.")
    lines = ["## 05 가 낸 지적 — 같은 것이면 그 키를 가리켜라", "",
             "`/code-review` 가 낸 것이 아래와 **같은 결함**이면 그 finding 에 "
             '`"finding_key": "<키>"` 를 단다. 05 가 이미 본 것은 escaped 가 아니다. '
             "같은 결함에 키를 안 달면 새 것으로 세고, 목록에 없는 키를 달면 exit 8 이다. "
             "안 다는 것이 기본이고, 다는 것이 주장이다.", ""]
    for k in keys:
        lines.append("- `%s` (`%s`, `%s`%s) — %s"
                     % (k["key"], k.get("severity"), k.get("reviewer"),
                        " · 닫힘" if k.get("closed") else "", k.get("title")))
    return "\n".join(lines)


def _judge_round(root, paths, s, phase_item, ctx, round_, slot, rounds):
    front = phase_item["front"]
    blocking = _converge_blocking(front)
    subs = [dict(v, code=k) for k, v in slot.items()]
    ok, reason = verdict.converged(subs, blocking)

    profile = (s.get("profile") or {}).get("name") or "normal"
    # 선언이 없으면 exit 2 다 — `or 5` 폴백은 곧 새 하드코딩이다 (M36).
    max_rounds = _loop_max(front, profile)
    if profile != "normal" and \
            ((front.get("loop") or {}).get("max_by_profile") or {}).get(profile):
        # 라운드 상한이 레인의 양보다 — 선언이 빗나가면 gap 이름에 들어간다.
        _note_applied(s, "01:max_rounds=%d" % max_rounds)

    if ok:
        # **`rounds` 를 덮지 않는다.** 예전에는 여기서 수렴 회차(정수)를
        # 그 자리에 대입해 라운드별 제출 기록을 통째로 날렸다 — 정수를 읽는
        # 소비자는 어디에도 없었다, 순수한 손실이다 (P3).
        s["phases"]["01-plan"]["converged_at_round"] = round_
        # exceeded 무시 — 수렴이 라운드를 닫았다. 마지막 라운드에서 수렴한
        # 것은 상한 초과가 아니고, 여기서 멈출 다음 라운드도 없다 (ADR-H048).
        st.counter_inc(s, _loop_counter(phase_item["front"]), max_rounds,
                       "converged", paths=paths)
        return _advance_to_next(root, paths, s, phase_item, ctx)

    # **봉투는 실효 상한을 말해야 한다** (M56) — 사람이 그 숫자로 판단한다.
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

    # 리뷰어는 `plan` 하나다 — 다음 라운드도 같은 리뷰어가 온다 (ADR-H045).
    planned = list(slot)
    s["phases"]["01-plan"].setdefault("rounds_planned", {})[str(used + 1)] = planned
    # **지시를 낸 자리에서 센다.** 01 의 루프는 `record → record` 라 `next`
    # 의 계수를 지나쳤고, 다섯 라운드 열 번을 불러도 예산은 2 였다 (ADR-H042).
    next_keys = ["01:r%d:%s" % (used, code) for code in planned]
    st.count_instructions(s, "01-plan", next_keys)
    st.save(paths, s)
    focus = (front.get("converge") or {}).get("focus_round_2") or ""
    env = st.envelope(
        "record", True, 0, s,
        {"round": used + 1, "reason": reason, "planned": planned},
        "## %d라운드가 필요하다\n\n%s\n\n다음 회차의 강제 초점: %s\n\n"
        "**다시 부를 리뷰어는 `%s` 다.** 플랜은 **부분 편집**으로 고친다 — 전체를 "
        "다시 쓰면 접두부가 라운드마다 쌓인다.\n\n"
        "열린 Critical 이 틀렸다고 보면 플랜을 억지로 맞추지 말고 **코드 근거로 "
        "기각한다** — 다음 회차 리뷰 JSON 을 `record` 하기 전에 최상위에 "
        "`false_positive: [{id, reason, evidence}]` 를 단다(`evidence` 는 리포에 "
        "실재하는 경로). 근거 없는 기각은 exit 8 이다."
        % (used + 1, reason, focus or "(없음)", "`, `".join(planned)),
        "python scripts/pipeline/cli.py record --phase 01 --file <리뷰 json> "
        "--reviewer %s --round %d --run-id %s"
        % (planned[0], used + 1, s["run_id"]))
    return env


def _same_command(s, phase):
    return ("python scripts/pipeline/cli.py record --phase %s --file <산출물> "
            "--run-id %s" % (phase, s["run_id"]))


# ------------------------------------------------------- 03 제출 처리

def _record_03(root, paths, s, phase_item, ctx, file, reviewer, round_):
    import contract as contract_mod

    if not file.exists():
        return st.envelope("record", False, 3, s, {}, "산출물이 없다: %s" % file, None)
    try:
        claims = harness._read_json(file)
    except (OSError, ValueError) as exc:
        return st.envelope("record", False, 8, s, {}, "JSON 을 읽지 못했다: %s" % exc, None)

    _note_contract(root, s, ctx)
    zero = _contract_units_zero(root, s, ctx)
    if zero is not None:
        # **계약 파일이 있는데 유닛이 0 이면 형식 문제다** (ADR-H049). `requires`
        # 는 크기와 절 제목만 본다 — 파일럿 40dc 의 계약이 `## 유닛` 을 `### `
        # 헤딩으로 적어 units=0 으로 게이트를 지났고, 그 결과 계약에 서술된
        # 심볼이 전부 대조 밖으로 빠지고 scoped 는 `no_selector` 로 스킵됐다.
        # 여기서 막으면 그 둘이 뒤에서 안 난다.
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
    if not _roles_for(phase_item["front"], ctx, s):
        # 역할 0명은 이 페이즈가 적용한 양보다 — miss 검사보다 **먼저** 적어야
        # 빗나갔을 때 gap 이름에 들어간다.
        _note_applied(s, "03:roles=0")
    # docs 레인인데 역할 소유 경로가 바뀌었다 — 계약이 없어 여기서 묻는다 (ADR-H044).
    if _docs_lane_source_check(root, paths, s, ctx, "03-implement"):
        st.set_phase_status(s, "03-implement", "running")
        st.save(paths, s)
        return st.envelope(
            "record", False, 3, s,
            {"profile": s.get("profile"), "contract": s.get("contract")},
            "## docs 레인 선언이 빗나갔다\n\n문서만 바뀐다고 선언했는데 역할 "
            "소유 경로가 바뀌었다. 프로파일을 `normal` 로 올렸고 `lane_miss` "
            "가 gap 으로 남았다 — 01 리뷰어·역할을 건너뛴 채 여기까지 왔기 "
            "때문이다.\n\n계약 파일을 쓰고 `next` 로 역할 패킷을 받는다. "
            "이미 고친 소스는 그 역할이 claim 한다.",
            "python scripts/pipeline/cli.py next --run-id %s" % s["run_id"])

    _config, adapter = adapters.load(root)
    if adapters.stage_state(adapter, "compile") == "present":
        log = paths.gates / "03_compile.log"
        result = adapters.run_stage(root, adapter, "compile", log_path=log)
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
            "`attribution.import_aliases`(import 경로 별칭)를 고친다. 테스트에 이름만 "
            "적어 통과시키지 마라."
            % _findings_lines(req["findings"]),
            _same_command(s, "03"))

    st.set_phase_status(s, "03-implement", "passed",
                        claims=file.name)
    return _advance_to_next(root, paths, s, phase_item, ctx)


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


def _note_contract(root, s, ctx):
    """계약 파일의 존재·해시·버려진 줄을 상태에 적는다. 반환: {"dropped": [...]}.

    `dropped` 는 파서가 `컨테이너 · 심볼` 쌍이 아니라서 유닛으로 안 센 줄이다 —
    그 사실을 읽는 곳이 없으면 실제 계약이 유닛 셋을 흘려도 아무도 말하지
    않는다 (P3 의 델타 D-2). 03 제출 · 04 수리 라운드 · 05 진입이 부른다.
    프로파일은 건드리지 않는다 — 레인은 `init` 의 선언이다.
    """
    import contract as contract_mod

    rel = resolve("${run.contract_file}", ctx)
    full = Path(root) / rel
    if not full.exists():
        return {"dropped": []}
    parsed = contract_mod.parse(full.read_text(encoding="utf-8"), ctx["config"])
    s["contract"] = dict(s.get("contract") or {}, present=True, path=rel,
                         sha256=_sha256(full), dropped=parsed.get("dropped") or [])
    return {"dropped": s["contract"]["dropped"]}


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
    # `next` 를 건너뛴 워커도 막는다 — 슬롯을 쓰기 전에 거부해야 재게이트 뒤
    # 같은 제출을 다시 낼 수 있다.
    stale = _receipt_stale(root, ctx["config"], s)
    if stale:
        return _receipt_envelope("record", s, stale)
    planned = _planned_for_round(node, round_)

    rounds = node.setdefault("rounds", {})
    prev_open = _previous_open(rounds, round_, reviewer)
    got = review_mod.check(root, ctx["config"], payload, raw_text, prev_open)
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
    slot[reviewer] = {"keys": got["keys"], "blocking": got["blocking"],
                      "closed": got["closed"], "findings": got["findings"],
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


def _receipt_stale(root, config, s, need_full=False):
    """게이트 영수증 vs 지금. `("loop", saved, fresh)` · `("full", …)` · None.

    영수증은 둘이다 — `s["fingerprint"]` 는 loop(compile·scoped)가, `s["tests"]
    ["fingerprint"]` 는 전체 회귀가 **이 코드에서** 돌았다는 증거다. 04 가 쓰기만
    하고 아무도 읽지 않아 「재게이트를 잊을 수 없다」가 산문이었다 (ADR-H076).
    **없는 영수증은 stale 이다** — 04 가 통과했으면 반드시 있다.
    """
    fresh = st.fingerprint(root, config)
    if not st.fingerprint_matches(s.get("fingerprint") or {}, fresh):
        return "loop", s.get("fingerprint"), fresh
    if need_full:
        saved = (s.get("tests") or {}).get("fingerprint") or {}
        if not st.fingerprint_matches(saved, fresh):
            return "full", saved, fresh
    return None


# loop 은 전이 거부(지문 stale)이고, full 은 선행 조건(전체 회귀)이다.
_RECEIPT_EXIT = {"loop": 6, "full": 3}


def _receipt_envelope(cmd, s, stale):
    kind, saved, fresh = stale
    if kind == "loop":
        text = ("## 소스가 게이트 뒤에 바뀌었다\n\n"
                "마지막 게이트 영수증의 지문과 지금 소유 범위 파일의 지문이 다르다 — "
                "수리한 코드가 compile·scoped 를 안 거쳤다. 재게이트 없이는 리뷰도 "
                "승인도 **게이트 안 된 코드**에 대한 것이 된다.")
    else:
        text = ("## 전체 회귀가 지금 코드에서 돌지 않았다\n\n"
                "회귀 영수증의 지문이 지금과 다르다 — 05 수리 뒤 loop 만 다시 돌았다. "
                "PR 본문의 「전체 회귀 실행됨」이 수리 전 코드를 증언하지 않도록 "
                "06 전에 한 번 돈다.")
    next_cmd = ("python scripts/pipeline/cli.py gate --phase 05 --stage %s "
                "--run-id %s" % (kind, s["run_id"]))
    return st.envelope(cmd, False, _RECEIPT_EXIT[kind], s,
                       {"receipt": kind, "saved": saved, "fresh": fresh},
                       "%s\n\n`gate --phase 05 --stage %s` 뒤에 이 명령을 다시 친다."
                       % (text, kind), next_cmd)


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
            "리뷰의 결정론이 무너진다."
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

    # 제출 파일은 라운드와 무관하게 하나다 — 페이즈 파일이 그렇게 말하고, 원문과
    # findings 는 `record` 가 슬롯에 옮기므로 다음 회차가 덮어써도 잃지 않는다.
    f = paths.run_dir / ("05_review_%s.json" % reviewer)
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
    slot[reviewer] = {"keys": None, "blocking": 0,
                      "closed": [], "findings": [], "status": "failed",
                      "reason": reason, "errors": list(errors or []),
                      "truncated": False,
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
    """제출이 모였다. 접기 → 영수증 → 수리 판정."""
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

    (paths.run_dir / "05_review.json").write_text(
        json.dumps({"round": round_, "review05": s["review05"],
                    "findings": merged}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    st.save(paths, s)

    blocking = [f for f in merged if f.get("severity") in verdict.BLOCKING]
    if blocking:
        front = phase_item["front"]
        max_decl = _loop_max(front)
        used, _max, exceeded = st.counter_inc(s, _loop_counter(front), max_decl,
                                              "review_blocking", paths=paths)
        # **다음 라운드의 델타는 에스컬레이션 여부보다 앞에서 정한다** (백로그 20).
        # 리뷰어가 하나라 델타도 그 하나다 — 재개된 라운드가 같은 한 명을 받는다.
        delta = planned[0] if planned else None
        node.setdefault("rounds_planned", {})[str(round_ + 1)] = [delta]
        if exceeded:
            _loop_on_exceed(front)
            st.escalate(paths, s,
                        "05 의 Critical/Major %d건이 %d회 안에 해소되지 않았다 — "
                        "같은 지적이 반복되면 코드가 아니라 계약이 틀렸을 수 있다"
                        % (len(blocking), max_decl),
                        phase="05-code-review")
            return _escalation_envelope("record", paths, s)
        # 다음 회차에 델타가 회계해야 할 목록이다. `record` 가 같은 인자로
        # 부르는 함수이므로 봉투와 검사가 같은 것을 본다 (M38).
        prev_open = _previous_open(node.get("rounds") or {}, round_ + 1, delta)
        # 수리 배정도 기동 지시다 — 04 와 대칭으로 작성자마다 센다 (ADR-H064).
        repair_keys = ["05:r%d:repair:%s" % (used, r) for r in
                       sorted({f.get("target_role") for f in blocking
                               if f.get("target_role")})]
        st.count_instructions(s, "05-code-review", repair_keys)
        st.save(paths, s)
        return st.envelope(
            "record", False, 4, s,
            {"blocking": len(blocking), "findings": blocking,
             "review05": s["review05"], "delta_reviewer": delta},
            _review_repair_render(blocking, used + 1, delta, prev_open),
            "python scripts/pipeline/cli.py gate --phase 05 --stage loop "
            "--run-id %s" % s["run_id"])

    return _advance_to_next(root, paths, s, phase_item, ctx)


def _review_repair_render(blocking, round_no, delta=None, previous_open=None):
    lines = ["## 수리가 필요하다 (%d회차)" % round_no, "",
             "Critical/Major %d건. **Minor 는 고치지 않는다** — 보고서로 간다."
             % len(blocking), ""]
    if delta:
        lines += ["수리 뒤 **델타 재리뷰는 `%s` 한 명**이다 — 그 한 명이 깨끗해도 "
                  "앞선 라운드의 `degraded`·`failed` 는 지워지지 않는다." % delta, ""]
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
        lines.append("- **%s** → `%s`: %s"
                     % (f.get("severity"), f.get("target_role"), f.get("title")))
    contract_defect = [f for f in blocking
                       if f.get("category") == "CONTRACT_DEFECT"]
    if contract_defect:
        lines += ["", "**`CONTRACT_DEFECT` 가 있다.** 이것은 수리 대상이 아니라 "
                      "에스컬레이션이다 — 계약은 메인 단독 소유다."]
    lines += ["", "제출이 **내용은 그대로이고 회계 필드만** 틀려 exit 8 로 되돌아오면 "
                  "(`resolved_from_previous` · `reraised_from_previous`) 메인이 "
                  "그 필드를 고쳐 재제출해도 된다 — quote·헤딩 수·"
                  "severity 는 여전히 손대지 않는다 (ADR-H052).",
              "", "고친 뒤 `gate --phase 05 --stage loop` 로 재게이트하고, "
                  "`next` 로 델타 지시를 받아 재리뷰 1명을 돌린 다음 다시 제출한다.",
              "**수리하면 지문이 바뀌어 영수증이 낡는다** — `next`·`record`·`approve` 가 "
              "자동으로 막으므로 재게이트를 잊을 수 없다."]
    return "\n".join(lines)


PR_CLOSED_STATES = ("closed", "merged")
CODE_REVIEW_STATES = ("done", "skipped")


def _record_07(root, paths, s, phase_item, ctx, file, reviewer, round_):
    """07 제출 — `/code-review` 1회의 결과를 받고 **05 가 놓친 것**을 센다.

    **PR 이 닫혔거나 머지됐으면 아무것도 하지 않고 정상 종료한다** (§E8).
    판정은 기록과 등급뿐이다 — Critical/Major 가 새로 나와도 수리 루프를
    돌리지 않고 사람에게 넘긴다. `dup_05` 는 메인의 선언이고, 기계는 그 키가
    05 의 목록(`_keys_from_05`)에 있는지만 본다.
    """
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
        s["review07"] = {"code_review": "skipped", "skip_reason": "pr_%s" % pr_state,
                         "findings": 0, "dup_05": 0, "escaped": []}
        st.save(paths, s)
        return _advance_to_next(root, paths, s, phase_item, ctx)

    errors = []
    mode = payload.get("code_review")
    if mode not in CODE_REVIEW_STATES:
        errors.append("`code_review` 는 %s 중 하나다 (받은 값: %r)"
                      % (" · ".join(CODE_REVIEW_STATES), mode))
    reason = payload.get("skip_reason")
    if mode == "skipped" and not (isinstance(reason, str) and reason.strip()):
        errors.append("`code_review: \"skipped\"` 에는 `skip_reason` 이 필수다 — "
                      "사유 없는 생략은 받지 않는다")
    findings = payload.get("findings") or []
    known = {k["key"] for k in _keys_from_05(s)}
    for f in findings:
        label = f.get("id") or f.get("title")
        if f.get("severity") not in verdict.SEVERITIES:
            errors.append("finding %s: severity 가 어휘 밖이다 (%r)"
                          % (label, f.get("severity")))
        if not str(f.get("title") or "").strip():
            errors.append("finding %s: `title` 이 없다" % label)
        key = f.get("finding_key")
        if key and key not in known:
            errors.append("finding %s: `finding_key` %r 가 05 의 목록에 없다 — 봉투의 "
                          "「05 가 낸 지적」 절에 있는 키만 가리킨다" % (label, key))
    if errors:
        return st.envelope("record", False, 8, s, {"errors": errors},
                           "\n".join(["## 제출이 규약을 어겼다", ""]
                                     + ["- %s" % e for e in errors]),
                           None)

    # **dedup 은 버리는 것이 아니라 세는 것이다** — 05 가 낸 키를 가리키지 않은
    # Critical/Major 가 05 가 놓친 것이다. 수리는 여기서 하지 않는다.
    dup = [f for f in findings if f.get("finding_key")]
    escaped = [{"severity": f.get("severity"), "title": f.get("title"),
                "path": f.get("path")}
               for f in findings
               if not f.get("finding_key") and f.get("severity") in verdict.BLOCKING]
    s["review07"] = {"code_review": mode,
                     "skip_reason": reason.strip() if mode == "skipped" else None,
                     "findings": len(findings), "dup_05": len(dup),
                     "escaped": escaped}
    if mode == "skipped":
        st.demote(s, st.GRADES[1], "pr_review_skipped")
    if escaped:
        st.demote(s, st.GRADES[1], "pr_review_open")
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

_RECORD_HANDLERS = {"01-plan": _record_01,
                    "03-implement": _record_03, "05-code-review": _record_05,
                    "06-pr": _record_06,
                    "07-pr-review": _record_07}


# ------------------------------------------------------------------------ gate

def cmd_gate(root, args):
    return st.emit(run_gate_cmd(root, args.phase, args.stage, args.run_id))


def run_gate_cmd(root, phase="04", only_stage=None, run_id=None, runner=None):
    """`runner` 는 테스트의 스텁 주입 통로다 — CLI 는 주지 않는다."""
    try:
        return _run_gate_cmd(root, phase, only_stage, run_id, runner)
    except ConfigDeclarationError as exc:
        _paths, s = st.load(Path(root), run_id)
        return _declaration_envelope("gate", s, exc)


def _run_gate_cmd(root, phase="04", only_stage=None, run_id=None, runner=None):
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

    # **페이즈 전이는 04 의 전체 게이트만 한다.** `gate --phase 05` 를 `--stage`
    # 없이 치면 05 체인이 돌고 04 리포트를 덮어쓴 뒤 05 가 passed 로 올라갔다 —
    # precheck·contract-trace·리뷰 없이 05 통과다. 04 도 04 에 있을 때만 전체를 돈다.
    if not only_stage and (pid != "04-gate" or s.get("phase") != pid):
        return st.envelope(
            "gate", False, 2, s, {"phase": pid, "current": s.get("phase")},
            "`gate --phase %s` 는 `--stage` 가 필요하다 — 전체 게이트로 페이즈를 "
            "닫는 것은 `04-gate` 에서 `gate --phase 04` 뿐이다(지금 페이즈: `%s`).\n\n"
            "- 수리 뒤 재게이트: `gate --phase 05 --stage loop`\n"
            "- 06 진입 전 전체 회귀: `gate --phase 05 --stage full`"
            % (pid.split("-")[0], s.get("phase")), None)

    if not only_stage:
        checks = check_requires(root, phase_item["front"].get("requires"), ctx, s)
        failed = [c for c in checks if not c["ok"]]
        if failed:
            return st.envelope("gate", False, 3, s, {"requires_report": checks},
                               "## 게이트 진입 거부\n\n" +
                               "\n".join("- %s" % c["message"] for c in failed), None)

    config, adapter = adapters.load(root)
    # **수리 라운드마다 계약을 다시 읽는다.** 메인이 여기서 계약 델타를 적용한다.
    if not only_stage:
        _note_contract(root, s, ctx)
    round_no = ((s.get("counters") or {}).get("repair") or {}).get("used", 0) + 1
    log_path = paths.gates / ("gr-%d.stdout.log" % round_no)

    st.append_event(paths, "stage_start", cmd="gate", phase=pid, round=round_no)
    report = gate_mod.run_gate(root, config, adapter, s,
                               phase_item["front"], paths.run_dir,
                               only_stage=only_stage, runner=runner,
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
        render = "\n".join(_stage_render(x) for x in stages)
        if ok and (report.get("tests") or {}).get("status") == "shrank":
            # 전체 게이트만 잡던 하한을 05 의 full 재실행이 우회하면 반만 닫힌다.
            ok = False
            render += ("\n\n테스트 수가 하한 아래로 떨어졌다(%s < %s) — 전체 회귀 "
                       "영수증을 남기지 않는다."
                       % (report["tests"].get("ran"), report["tests"].get("expected_min")))
        if ok:
            # **영수증은 둘이다.** loop 는 compile·scoped 가, full 은 전체 회귀가
            # **이 코드에서** 돌았다는 증거다. `next`·`record`·`approve` 가 읽는다.
            fresh = st.fingerprint(root, config)
            if only_stage == "loop":
                s["fingerprint"] = fresh
            elif only_stage == "full":
                s.setdefault("tests", {})["fingerprint"] = fresh
            st.save(paths, s)
        # `stage` 는 옛 소비자용 단수 키 — 실패한 첫 스테이지, 없으면 마지막.
        stage = failed[0] if failed else stages[-1]
        return st.envelope("gate", ok, 0 if ok else 4, s,
                           {"stage": stage, "stages": stages}, render, None)

    _write_json(paths.run_dir / "04_gate_report.json", report)

    log_text = ""
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")

    if report.get("tests"):
        s["tests"] = report["tests"]
    for gap in report.get("gaps") or []:
        if gap not in s.setdefault("gaps", []):
            s["gaps"].append(gap)

    if report.get("failed") is None:
        if (report.get("tests") or {}).get("status") == "shrank":
            return _gate_fail(root, paths, s, phase_item, ctx, adapter, report,
                              round_no, log_text, reason="테스트 수가 하한 아래로 떨어졌다")
        st.demote(s, report.get("grade") or st.GRADES[1])
        s["fingerprint"] = st.fingerprint(root, config)
        # 전체 게이트는 full 까지 돌았다 — 회귀 영수증도 같은 지문이다.
        s.setdefault("tests", {})["fingerprint"] = s["fingerprint"]
        st.append_event(paths, "stage_done", cmd="gate", phase=pid,
                        grade=s["grade"])
        st.save(paths, s)
        return _advance_to_next(root, paths, s, phase_item, ctx, cmd="gate")

    return _gate_fail(root, paths, s, phase_item, ctx, adapter, report, round_no,
                      log_text)


def _gate_fail(root, paths, s, phase_item, ctx, adapter, report, round_no, log_text,
               reason=None):
    """실패를 **작성자에게 그대로** 되돌린다 — 귀속하지 않는다 (ADR-H075).

    인프라 매칭이면 카운터를 소모하지 않고 에스컬레이션한다. 아니면 `repair` 를
    하나 쓰고, 상한이면 에스컬레이션, 아니면 실패 스테이지의 출력 브리프를 봉투에
    싣는다. 작성자가 하나라 누구의 실패인지 물을 것이 없다.
    """
    failed = report.get("failed") or {}
    text = log_text or failed.get("output") or ""
    infra = adapters.infra_match(adapter, failed.get("exit", 1), text) if failed else None
    if infra:
        # **카운터를 소모하지 않는다.** 외부 의존 미기동이 작성자의 실패로
        # 오분류되면 예산을 태운다.
        st.escalate(paths, s, "외부 의존 실패로 보인다 (패턴: %s)" % infra,
                    phase="04-gate")
        return _escalation_envelope("gate", paths, s)

    used, max_, exceeded = st.counter_inc(s,
                                          _loop_counter(phase_item["front"]),
                                          _loop_max(phase_item["front"]),
                                          "gate_failure", paths=paths)
    st.set_phase_status(s, "04-gate", "failed")
    st.save(paths, s)

    if exceeded:
        _loop_on_exceed(phase_item["front"])
        st.escalate(paths, s, "수리 예산 %d회를 소진했다" % max_, phase="04-gate")
        return st.envelope("gate", False, 5, s, {"report": report["gaps"]},
                           "## 예산 소진 — 에스컬레이션\n\n`ESCALATION.md` 를 본다.",
                           "python scripts/pipeline/cli.py resume --ack "
                           "--answer-file <경로>")

    owner = ctx["config"].get("primary_role") or "impl"
    brief = _gate_brief(report, round_no, owner, text, reason)
    # 수리 배정도 기동 지시다 (ADR-H064).
    st.count_instructions(s, "04-gate", ["04:r%d:%s" % (used, owner)])
    st.append_event(paths, "dispatch", cmd="gate", phase="04-gate", owner=owner)
    st.save(paths, s)
    return st.envelope("gate", False, 4, s,
                       {"repair_dispatch": brief, "gaps": report.get("gaps")},
                       _repair_render(brief),
                       "python scripts/pipeline/cli.py gate --phase 04 --run-id %s"
                       % s["run_id"])


def _gate_brief(report, round_no, owner, text, reason=None):
    """봉투에는 **출력 끝 60줄 · 4,000자**의 브리프만. 전문은 로그 파일에 있다."""
    failed = report.get("failed") or {}
    tail = [ln for ln in (text or "").splitlines() if ln.strip()][-60:]
    return {"round": round_no, "owner": owner,
            "stage": failed.get("id"), "exit": failed.get("exit"),
            "reason": reason, "output": "\n".join(tail)[-4000:],
            "log": report.get("log")}


def _repair_render(brief):
    head = ("스테이지 `%s` 가 exit %s 로 실패했다" % (brief["stage"], brief["exit"])
            if brief.get("stage") else (brief.get("reason") or "게이트 실패"))
    lines = ["## 04 게이트 실패 — 수리 지시 (%d회차)" % brief["round"], "",
             "**`%s` 에게** 되돌린다. %s." % (brief["owner"], head), ""]
    if brief.get("reason") and brief.get("stage"):
        lines += [brief["reason"], ""]
    if brief.get("output"):
        lines += ["```", brief["output"], "```", ""]
    lines += ["전문은 `%s` 에 있다." % (brief.get("log") or "gates/"), "",
              "**출력을 요약해 없애지 마라** — 작성자가 실패 출력을 그대로 받는다. "
              "계약이 틀렸다고 판단되면 고치지 말고 `CONTRACT_DEFECT` 로 보고한다.",
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
        # `NON_DEMOTING_GAPS` 는 이름만 남기고 등급은 그대로다
        # — 사람이 할 일이 밀렸다는 표시이지 이 런의 관측 결손이 아니다 (ADR-H047).
        import report as rep
        for gap in got.get("gaps") or []:
            st.demote(s, None if rep.is_non_demoting(gap) else st.GRADES[1], gap)
        st.append_event(paths, "check_fail" if got["exit"] else "stage_done",
                        cmd="precheck", phase=pid, exit=got["exit"])
        if got["exit"] == 9:
            # 브랜치·divergence 는 사람이 판단한다 — 그 대기가 여기서
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
        lines = ["`precheck` 통과. 파일 %d · 줄 %d — 브랜치·base·인프라가 전부 맞다."
                 % (got["budget"]["files"], got["budget"]["lines"])]
        # **면제를 조용히 넘기지 않는다** (M44). "전부 맞다" 로만 적으면
        # 면제가 통과와 구분되지 않는다.
        for gap in got.get("gaps") or []:
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
        lines += ["", "**자동으로 리베이스하지 않는다.** 브랜치를 옮기거나 "
                      "리베이스한 뒤 같은 명령을 다시 부른다."]
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

    config, _adapter = adapters.load(root)
    # 승인은 **게이트된 코드**에 대한 것이다 — loop 영수증(exit 6)과 전체 회귀
    # 영수증(exit 3)을 여기서 본다. `pr` 은 승인 지문을 대조하므로 전이적으로 덮인다.
    stale = _receipt_stale(root, config, s, need_full=True)
    if stale:
        return _receipt_envelope("approve", s, stale)
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


# 승인은 그 앞 페이즈가 끝난 뒤에만 뜻이 있다. 07 은 06 의 승인을 그대로 쓰고
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


# ---------------------------------------------------------------------- report

def cmd_report(root, args):
    return st.emit(run_report(root, args.out, args.run_id))


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

    # 소요는 `events.jsonl` 의 유도값이고, 08 시점에 그 파일은 이미 완결이다
    # — 미완 구간이 없다.
    timing = st.phase_durations(paths)
    text, missing = rep.build(s, data, timing)

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

    # 이미 닫힌 런 — 파일만 다시 쓰고 **전이하지 않는다.** exit 11 은 전이의
    # 순간이므로 두 번 내면 재작성과 첫 종료가 원장에서 구분되지 않는다.
    if s.get("run_status") == st.DONE:
        return st.envelope("report", True, 0, s, dict(data, closed=True),
                           _report_render(rel, missing, s)
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
            _report_render(rel, missing, s) + "\n\n" + "\n".join(
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
    env["render"] = _report_render(rel, missing, s) + "\n\n" + env["render"]
    return env


def _report_render(rel, missing, s):
    lines = ["## 보고서를 썼다", "", "`%s`" % rel, "",
             "완료 등급 **%s**%s" % (s.get("grade") or "미정",
                                    (" — " + ", ".join(s.get("gaps") or []))
                                    if s.get("gaps") else "")]
    if missing:
        lines += ["", "**필수 섹션이 빠졌다: %s**" % ", ".join(missing),
                  "원장에 기록했다. 다만 **보고서는 파이프라인을 실패시키지 "
                  "않는다.**"]
    lines += ["", "**런 기록을 커밋하고 `pr --run-id %s` 를 다시 돌려 PR 을 갱신한다.**"
              % s.get("run_id")]
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

    config, adapter = adapters.load(root)
    data = {}

    # 1. 브랜치 — 규약과 보호. **자동 생성하지 않는다.**
    ok, branch, msg = pr_mod.check_branch(root, config)
    data["branch"] = {"ok": ok, "name": branch, "message": msg}
    if not ok:
        return st.envelope("pr", False, 3, s, data,
                           "\n".join(["## 브랜치가 맞지 않는다", "", msg, "",
                                      "**브랜치를 자동으로 만들지 않는다.**"]),
                           None)

    # 2-b. 닫힌 런(`done`)의 `pr` 는 08 이 쓴 런 기록을 기능 PR 에 싣는 재push 다 —
    # 흐름 노트를 다시 묻지 않고 06 record 로 이어지지 않는다 (ADR-H052).
    closed_run = s.get("run_status") == st.DONE
    # 2-a. 흐름 노트 (ADR-H058 추기). PR 본문의 「핵심 흐름」은 모델이 쓴다 —
    # 열린 런은 첫 `pr` 부터 요구한다. 닫힌 런의 재실행은 이미 통과한 파일을
    # 렌더만 한다(런 기록 갱신 경로에 새 exit 8 을 두지 않는다).
    if not closed_run and (s.get("contract") or {}).get("mode") != "no_contract":
        _notes, problems = pr_mod.check_notes(root, paths, s, config)
        if problems:
            data["pr_notes"] = problems
            return st.envelope(
                "pr", False, 8, s, data,
                "## 흐름 노트 `%s` 가 없거나 틀렸다\n\n%s\n\n`pr` 전에 네가 쓴다. "
                "형식:\n\n```json\n%s\n```\n\n`step` 은 한 줄 이상, `verify` 는 하나 "
                "이상이다. `refs` 는 선택이고 검사하지 않는다 — 있으면 본문에 그대로 싣는다."
                % (pr_mod.NOTES_FILE, "\n".join("- %s" % p for p in problems),
                   pr_mod.NOTES_EXAMPLE),
                "python scripts/pipeline/cli.py pr --run-id %s" % s["run_id"])
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
    # **삭제는 push 가 성공한 뒤다** (G-7). 04 의 선택자와 05 의 대조가 계약을 계속
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
        return st.envelope("pr", True, 0, s, data,
                           _pr_render(req, paths, req_path)
                           + "\n\n**닫힌 런의 PR 갱신이다.** 06 record 로 이어지지 않는다.",
                           None)
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
    # 절을 실어야 하는데(06 페이즈 파일의 PR 본문 절 목록), 07 수리 뒤 `pr` 을
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
           "precheck 통과" if got["exit"] == 0 else "**precheck 미통과**"),
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

    config, adapter = adapters.load(root)
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
    if exit_:
        # **선수리 루프에도 천장이 있다.** 05 의 `trace_loop` 가 선언하고 여기서
        # 읽는다 — 반복마다 작성자 호출 1 + 재게이트 1 이고, 같은 Critical 이
        # 반복되면 코드가 아니라 계약이 틀렸을 수 있다 (ADR-H076).
        front = (load_phases(root)[0].get("05-code-review") or {}).get("front") or {}
        try:
            used, max_, exceeded = st.counter_inc(
                s, _loop_counter(front, "trace_loop"),
                _loop_max(front, key="trace_loop"), "trace_blocking", paths=paths)
            if exceeded:
                _loop_on_exceed(front, "trace_loop")
        except ConfigDeclarationError as exc:
            return _declaration_envelope("contract-trace", s, exc)
        st.save(paths, s)
        if exceeded:
            st.escalate(paths, s,
                        "계약 대조의 Critical %d건이 %d회 안에 해소되지 않았다 — "
                        "같은 지적이 반복되면 코드가 아니라 계약이 틀렸을 수 있다"
                        % (got["blocking"], max_),
                        phase="05-code-review")
            return _escalation_envelope("contract-trace", paths, s)
    return st.envelope("contract-trace", exit_ == 0, exit_, s, got,
                       _trace_render(got, rel),
                       "python scripts/pipeline/cli.py gate --phase 05 --stage loop "
                       "--run-id %s" % s["run_id"] if exit_ else
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
    blocking = [f for f in got["findings"] if f["severity"] == "critical"]
    if blocking:
        lines += ["", "### 리뷰어를 부르기 전에 고칠 것 (Critical %d건)" % len(blocking)]
        for f in blocking:
            lines.append("- `%s` → **%s**: %s"
                         % (f["code"], f["target_role"], f["title"]))
        lines += ["", "고친 뒤 `gate --phase 05 --stage loop` 로 재게이트하고 "
                      "이 명령을 다시 친다."]
    else:
        lines += ["", "Critical 0건. 리뷰어 라우팅으로 넘어간다."]
    return "\n".join(lines)


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

    sp = sub.add_parser("status", add_help=False)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("lint-phases", add_help=False)
    sp.add_argument("--dir", dest="dir", default=None)

    sp = sub.add_parser("init", add_help=False)
    sp.add_argument("--feature", dest="feature", default=None)
    sp.add_argument("--request-file", dest="request_file", default=None)
    sp.add_argument("--profile", dest="profile", default=None,
                    choices=["docs", "fix", "normal"])

    sp = sub.add_parser("next", add_help=False)
    sp.add_argument("--run-id", dest="run_id", default=None)
    sp.add_argument("--phase", dest="phase", default=None)

    sp = sub.add_parser("resume", add_help=False)
    sp.add_argument("--ack", dest="ack", action="store_true")
    sp.add_argument("--answer-file", dest="answer_file", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("gate", add_help=False)
    sp.add_argument("--phase", dest="phase", default="04")
    sp.add_argument("--stage", dest="stage", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("report", add_help=False)
    sp.add_argument("--out", dest="out", default=None)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("pr", add_help=False)
    sp.add_argument("--run-id", dest="run_id", default=None)

    sp = sub.add_parser("approve", add_help=False)
    sp.add_argument("--phase", dest="phase", default="06", choices=["06"])
    sp.add_argument("--revoke", dest="revoke", action="store_true")
    sp.add_argument("--auto", dest="auto", action="store_true")
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
    "resume": cmd_resume,
    "status": cmd_status,
    "lint-phases": cmd_lint_phases,
    "contract-trace": cmd_contract_trace,
    "precheck": cmd_precheck,
    "approve": cmd_approve,
    "pr": cmd_pr,
    "report": cmd_report,
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
