#!/usr/bin/env python3
"""`contract-trace` — 계약이 말한 것이 코드에 실제로 있는가.

**05 에서 두 번째로 도는 검사이고 무료다.** 리뷰어를 부르기 전에 여기서 잡으면
뒤에서 되돌릴 일이 없다. 모델을 한 번도 부르지 않는다.

검사는 열이다:

| 코드 | 무엇 | 심각도 |
|---|---|---|
| `missing_impl`            | 계약의 유닛이 소스에 있는가        | critical |
| `missing_screen`          | 계약의 화면이 소스에 있는가 (ui 역할) | critical |
| `missing_error_symbol`    | 오류 어휘 상수가 실재하는가        | critical |
| `missing_entrypoint`      | 진입점이 실재하는가                | critical |
| `untested_contract_item`  | 그 유닛을 참조하는 테스트가 있는가 | major (첫 3런 warn_only) |
| `untested_entrypoint`     | 진입점마다 그 진입점의 테스트 파일이 있는가 | major (유예 없음, 03 이 먼저 거부) |
| `untested_error_symbol`   | 오류 어휘 상수를 테스트가 한 번이라도 쓰는가 | major (유예 없음, 03 이 먼저 거부) |
| `authz_untested`          | `[역할]` 태그 진입점의 테스트에 거부 단언이 있는가 | major (유예 없음, 03 이 먼저 거부) |
| `missing_journey_spec`    | 계약 `## 여정` 의 스펙 파일에 그 슬러그가 선언돼 있는가 | critical (유예 없음, 03 이 먼저 거부) |
| `out_of_contract`         | 계약에 없는 신규 public 심볼      | major (첫 3런 warn_only) |

"첫 3런" 은 **그 검사가 지적을 낸 런**으로 센다 (ADR-H058 · `ledger.in_baseline_for`).

테스트 셋(`untested_*`·`authz_untested`)은 **존재 검사이지 의미 검사가 아니다.**
커버리지 도구가 없는 stdlib 실행기라 "그 이름·그 패턴이 테스트 본문에 있는가" 까지만
본다 — 단언이 맞는지는 test-quality 리뷰어의 몫이다.

**파일명이 `trace.py` 가 아닌 이유**: 이 패키지는 `sys.path` 에 자기 디렉터리를
넣으므로 모듈 이름이 프로세스 전역 최상위가 된다. `trace` 는 stdlib 모듈이고,
그 이름을 쓰면 stdlib 을 가린다.

**스킵을 통과로 적지 않는다.** `entrypoint_resolver` 가 없으면 진입점을 푸는 검사만 빠지고
그 사실과 사유가 `skipped`·`skip_reasons` 에 남는다. `no_contract` 런은 `skipped_no_contract` 다.
화면의 테스트는 묻지 않는다 — 계약에 화면이 있으면 `untested_screen` 이 `skipped` 에 남는다 (ADR-H057).
"""

import posixpath
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402
import contract as contract_mod  # noqa: E402
import ledger  # noqa: E402

CHECKS = ("missing_impl", "missing_screen", "missing_error_symbol", "missing_entrypoint",
          "untested_contract_item", "untested_entrypoint", "untested_error_symbol",
          "authz_untested", "missing_journey_spec", "out_of_contract")

_NO_RESOLVER = "어댑터에 `entrypoint_resolver` 가 없다"

# 오탐이 잦은 둘. 상위 계층 테스트로만 커버되거나 테스트가 심볼명을 직접 쓰지
# 않는 스타일일 수 있고, 생성 코드가 `out_of_contract` 오탐을 만든다.
#
# **§E6 이 재려던 값이 나왔다** — P2 46 + P3 32 = 78/78 이 `out_of_contract` 의
# 구조적 오탐이었고 `untested_contract_item` 도 6/6 이었다. 그중 `out_of_contract`
# 는 원인이 규명돼 고쳤다(변경 파일 전체 → 추가된 줄). 다만 **고친 구현의
# 오탐률은 아직 0런이다** — 78/78 은 고치기 전 값이고, 그것을 근거로 승격하면
# 재지 않은 것을 잰 것처럼 쓰는 셈이다. 여기 남겨 두고 P4·P5 가 새 값을 만든다.
#
# **테스트 존재 검사 셋(`_test_checks` — 03 제출과 05 가 같이 쓴다)은 여기 두지
# 않는다** (ADR-H058 결정 8). 존재
# 검사의 오탐은 구조적(재수출 import · 다른 거부 단언 모양)이라 매 런 똑같이 나고,
# 런 수 유예는 그것을 고치지 못하고 거부만 미룬다. banana 실측(과거 계약 15개 ×
# 현재 테스트 트리)에서 진입점 14 · 오류 상수 10 오탐 0, 자기 테스트를 빼면 14/14
# 지적이었다. 위 둘은 78/78 · 6/6 오탐 이력이 있어 유예를 유지한다.
BASELINE_CHECKS = ("untested_contract_item", "out_of_contract")

DEFAULT_BASELINE_RUNS = 3

# 계약 대조에서 나온 지적이 원장에 들어갈 때의 category.
# 계약과 코드가 어긋난 것은 "계약 결함"과 다르다 — 여기서는 코드가 계약을
# 아직 안 지킨 것이고, 계약 자체가 틀렸다는 판정은 리뷰어·사람의 몫이다.
CATEGORY = {
    "missing_impl": "BOUNDARY_VIOLATION",
    "missing_screen": "BOUNDARY_VIOLATION",
    "missing_error_symbol": "BOUNDARY_VIOLATION",
    "missing_entrypoint": "BOUNDARY_VIOLATION",
    "untested_contract_item": "TEST_MISSING_FAILURE_PATH",
    "untested_entrypoint": "TEST_MISSING_FAILURE_PATH",
    "untested_error_symbol": "TEST_MISSING_FAILURE_PATH",
    "authz_untested": "AUTHZ_MISSING_RULE",
    "missing_journey_spec": "BOUNDARY_VIOLATION",
    "out_of_contract": "NAMING",
}

# 소스에서 밖으로 나가는 이름. 스택 지식이 아니라 **표기 관습**이라 코어에 둔다 —
# 어댑터가 이것을 덮고 싶으면 `attribution.public_symbol_regex` 로 준다.
#
# 첫 글자를 `[A-Za-z_$]` 로 쓰지 않는다. 그러면 **한글 식별자를 통째로 놓치고**,
# 이 리포처럼 비ASCII 식별자가 흔한 곳에서 검사가 조용히 아무것도 안 잡는다
# (§E4 가 인코딩에서 경고하는 것과 같은 자리다). `[^\W\d]` 는 유니코드 word
# 문자 중 숫자가 아닌 것이다.
_DEFAULT_PUBLIC = (r"^\s*export\s+(?:async\s+)?(?:function|const|class|type|"
                   r"interface|enum)\s+(?P<name>[^\W\d][\w$]*)")


def run(root, config, adapter, contract_path, no_contract=False, changed=None,
        baseline_runs=None):
    """열 검사를 돌린다. 반환은 그대로 `05_trace.json` 이 된다."""
    root = Path(root)
    if no_contract or not contract_path:
        # §E3. 계약이 없는 런은 정상 경로다. 다만 **통과가 아니다** —
        # 보고서가 "계약 대조가 없었다"고 말해야 한다.
        return {"status": "skipped_no_contract", "findings": [],
                "checks_run": [], "skipped": list(CHECKS),
                "entrypoint_resolver": None,
                "note": "계약이 없는 런이다. 계약 대조를 수행하지 않았다 — "
                        "통과가 아니라 미수행이다."}

    text = Path(contract_path).read_text(encoding="utf-8")
    parsed = contract_mod.parse(text, config)
    files = repo_files(root)

    resolver = (adapter.get("entrypoint_resolver") or {}).get("kind") or "none"
    baseline_runs = (baseline_runs if baseline_runs is not None
                     else _baseline_runs(config))
    in_baseline = {code: ledger.in_baseline_for(root, code, baseline_runs)
                   for code in BASELINE_CHECKS}

    primary = config.get("primary_role") or "impl"
    test_role = _test_role(config)

    findings, checks_run, skipped, skip_reasons = [], [], [], {}

    checks_run.append("missing_impl")
    findings += _missing_impl(root, parsed, files, primary)

    checks_run.append("missing_screen")
    findings += _missing_impl(root, parsed, files, _screen_role(config),
                              key="screens", code="missing_screen", noun="화면")
    if parsed.get("screens"):
        skipped.append("untested_screen")
        skip_reasons["untested_screen"] = (
            "화면의 단위테스트는 두지 않는다 — 통과가 아니라 미수행이다 (ADR-H057)")

    checks_run.append("missing_error_symbol")
    findings += _missing_error_symbol(root, parsed, config, files, primary)

    if resolver == "none":
        skipped.append("missing_entrypoint")
        skip_reasons["missing_entrypoint"] = _NO_RESOLVER
    else:
        checks_run.append("missing_entrypoint")
        findings += _missing_entrypoint(adapter, parsed, files, primary)

    checks_run.append("untested_contract_item")
    findings += _untested(root, config, adapter, parsed, files, test_role)

    tc = _test_checks(root, adapter, parsed, files, test_role)
    findings += tc["findings"]
    checks_run += tc["checks_run"]
    skipped += tc["skipped"]
    skip_reasons.update(tc["skip_reasons"])

    checks_run.append("out_of_contract")
    findings += _out_of_contract(root, adapter, parsed, changed, primary)

    _apply_baseline(root, findings, in_baseline, baseline_runs)

    blocking = [f for f in findings
                if f["severity"] == "critical" and f["resolution"] != "warn_only"]
    return {
        "status": "ok",
        "findings": findings,
        "checks_run": checks_run,
        "skipped": skipped,
        "skip_reasons": skip_reasons,
        "entrypoint_resolver": resolver,
        # 진입점을 파일로 풀지 못해 테스트 존재를 묻지 않은 것. 지적이 아니다 —
        # 진입점 부재는 `missing_entrypoint` 의 몫이다.
        "entrypoints_unresolved": tc["unresolved"],
        "baseline": {"in_baseline": in_baseline,
                     "distinct_runs": ledger.distinct_runs(root),
                     "baseline_runs": baseline_runs},
        "blocking": len(blocking),
        # 04 의 `contract.scope.repo_files` 와 같아야 한다 (M50).
        "repo_files": len(files),
        "contract": {"units": len(parsed.get("units") or []),
                     "screens": len(parsed.get("screens") or []),
                     "entrypoints": len(parsed.get("entrypoints") or []),
                     "errors": len(parsed.get("errors") or []),
                     "journeys": len(parsed.get("journeys") or []),
                     "dropped": parsed.get("dropped") or []},
        "note": ("Critical 은 리뷰어를 부르기 전에 선수리한다 — 계약과 코드가 "
                 "어긋난 채로 리뷰하면 리뷰어가 그것을 다시 발견하는 데 돈을 쓴다."),
    }


def required_tests(root, config, adapter, contract_path):
    """테스트 존재 검사 셋만 — **03 제출이 부른다** (ADR-H058 결정 7·8).

    05 의 `run()` 과 **같은 `_test_checks`** 를 쓴다. 두 자리가 다른 목록을 보면
    03 통과가 05 지적을 예고하지 못한다. 05 의 Major 는 원장에 `deferred` 로
    쌓일 뿐 수리 루프를 돌리지 않으므로, 워커 맥락이 살아 있는 03 에서 요구해야
    실제로 고쳐진다. 셋은 유예가 없어(`BASELINE_CHECKS` 밖) 지적이 곧 거부다.

    반환: {"findings": [...], "skipped": [...], "skip_reasons": {...}}
    """
    root = Path(root)
    parsed = contract_mod.parse(Path(contract_path).read_text(encoding="utf-8"),
                                config)
    tc = _test_checks(root, adapter, parsed, repo_files(root), _test_role(config))
    return {"findings": tc["findings"], "skipped": tc["skipped"],
            "skip_reasons": tc["skip_reasons"]}


def _test_checks(root, adapter, parsed, files, test_role):
    """`untested_entrypoint` · `untested_error_symbol` · `authz_untested` ·
    `missing_journey_spec`."""
    resolver = (adapter.get("entrypoint_resolver") or {}).get("kind") or "none"
    authz_rx = (adapter.get("attribution") or {}).get("authz_denied_pattern")
    tests = _unit_test_files(adapter, files, parsed)
    out = {"findings": [], "checks_run": [], "skipped": [], "skip_reasons": {},
           "unresolved": []}

    if resolver == "none":
        out["skipped"].append("untested_entrypoint")
        out["skip_reasons"]["untested_entrypoint"] = _NO_RESOLVER
    else:
        out["checks_run"].append("untested_entrypoint")
        got, out["unresolved"] = _untested_entrypoint(root, adapter, parsed, files,
                                                      tests, test_role)
        out["findings"] += got

    out["checks_run"].append("untested_error_symbol")
    out["findings"] += _untested_error_symbol(root, parsed, tests, test_role)

    if resolver == "none":
        out["skipped"].append("authz_untested")
        out["skip_reasons"]["authz_untested"] = _NO_RESOLVER
    elif not authz_rx:
        out["skipped"].append("authz_untested")
        out["skip_reasons"]["authz_untested"] = (
            "어댑터에 `attribution.authz_denied_pattern` 이 없다")
    else:
        out["checks_run"].append("authz_untested")
        out["findings"] += _authz_untested(root, adapter, parsed, files, tests,
                                           re.compile(authz_rx), test_role)

    out["checks_run"].append("missing_journey_spec")
    out["findings"] += _missing_journey_spec(root, parsed, files, test_role)
    return out


def _apply_baseline(root, findings, in_baseline, baseline_runs):
    for f in findings:
        if in_baseline.get(f["code"]):
            f["resolution"] = "warn_only"
            f["why_warn_only"] = (
                "baseline 기간이다 — 이 검사가 지적을 낸 런이 %d 로 %d 에 못 "
                "미친다. 오탐률을 보고 나서 승격한다 (미검증 상속값)."
                % (ledger.trace_runs(root, f["code"]), baseline_runs))
        else:
            f.setdefault("resolution", "deferred")


def repo_files(root):
    """추적 파일 **+ 아직 커밋되지 않은 새 파일.**

    정본은 `harness.list_files_with_untracked` 다. 04 게이트도 같은 함수를
    쓴다 — 두 페이즈가 같은 계약을 두고 다른 파일 목록을 보면 04 는
    `unmatched: 4` 를, 05 는 `dropped: []` 를 적는다 (M50).
    """
    return harness.list_files_with_untracked(root)


def _baseline_runs(config):
    return ((config.get("review") or {}).get("baseline_runs")
            or DEFAULT_BASELINE_RUNS)


def _test_role(config):
    """테스트 파일을 소유한 역할. 없으면 primary 로 낙하한다."""
    for role in config.get("roles") or []:
        owns = role.get("owns") or []
        if any("test" in o or "spec" in o for o in owns):
            return role.get("id")
    return config.get("primary_role") or "impl"


def _screen_role(config):
    """화면을 소유하는 역할 — `when_contract_section` 이 `screens` 인 역할. 없으면 primary."""
    for role in config.get("roles") or []:
        if role.get("when_contract_section") == "screens":
            return role.get("id")
    return config.get("primary_role") or "impl"


def _finding(code, severity, role, title, **kw):
    """`rule_slug` 를 여기서 단다 — **승격 집계의 축**이다 (ADR-H034).

    `code` 를 그대로 쓰지 않고 이름을 달리한 것은 이 리포에서 `code` 가 이미
    **리뷰어 코드**(`cli.py` 라우팅)와 **taxonomy 카테고리 코드**(`ledger.py`)
    두 뜻으로 쓰이기 때문이다. 세 번째 뜻을 얹지 않는다.

    `CATEGORY` 가 다대일이라(코드 8 → category 4) category 만으로는
    `missing_impl` 과 `missing_entrypoint` 를 못 가른다. 슬러그가 그것을
    가르고, 동시에 제목에 박힌 심볼 이름을 축에서 뺀다.
    """
    out = {"code": code, "rule_slug": code, "severity": severity,
           "target_role": role, "title": title, "category": CATEGORY[code],
           "source": "contract-trace"}
    out.update(kw)
    return out


# ------------------------------------------------------------- missing_impl

def _missing_impl(root, parsed, files, primary, key="units", code="missing_impl",
                  noun="유닛"):
    """**컨테이너명 + 심볼명 쌍**으로 찾는다.

    심볼명만 보면 흔한 이름이 다른 파일에 있어 거짓 통과한다. 컨테이너를 리포의
    파일과 맞추지 못하면 통과가 아니라 `container_resolved: false` 로 낙하한다 —
    "못 찾았다"가 "없다"보다 약한 판정이지만 **침묵보다는 강하다.**

    계약 `## 화면`(`missing_screen`)도 같은 형식이라 같은 본체를 쓴다 (ADR-H057).
    """
    out = []
    for unit in parsed.get(key) or []:
        symbol = unit.get("symbol")
        src = contract_mod._source_for_container(unit.get("container"), files)
        if src is None:
            out.append(_finding(
                code, "critical", primary,
                "계약의 %s %s 를 담을 파일을 찾지 못했다" % (noun, unit.get("raw")),
                container=unit.get("container"), symbol=symbol,
                container_resolved=False,
                evidence="컨테이너 %r 이 리포의 어느 파일과도 맞지 않는다"
                         % unit.get("container")))
            continue
        if not _has_symbol(root / src, symbol):
            out.append(_finding(
                code, "critical", primary,
                "계약의 %s %s 가 소스에 없다" % (noun, unit.get("raw")),
                container=unit.get("container"), symbol=symbol, path=src,
                container_resolved=True,
                evidence="%s 에 %r 이 없다" % (src, symbol)))
    return out


def _has_symbol(path, symbol):
    if not symbol:
        return False
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"\b%s\b" % re.escape(symbol), text) is not None


# ------------------------------------------------------ missing_error_symbol

def _missing_error_symbol(root, parsed, config, files, primary):
    """오류 어휘 상수는 **리포 어디에든** 있으면 된다.

    유닛과 달리 계약이 그 상수가 어느 파일에 사는지 말하지 않는다. 그래서 여기만
    전역 검색이고, 그 대가로 흔한 이름에 약하다 — 다만 오류 상수는 대문자
    스네이크라 충돌 위험이 낮다.
    """
    out = []
    for name in parsed.get("errors") or []:
        if not _found_anywhere(root, files, name):
            out.append(_finding(
                "missing_error_symbol", "critical", primary,
                "계약의 오류 어휘 %s 가 소스에 없다" % name,
                symbol=name,
                evidence="리포의 어느 소스에도 %r 이 없다" % name))
    return out


def _found_anywhere(root, files, needle):
    rx = re.compile(r"\b%s\b" % re.escape(needle))
    for rel in files:
        p = Path(root) / rel
        if p.suffix not in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".py",
                            ".java", ".kt", ".go", ".rs"):
            continue
        try:
            if rx.search(p.read_text(encoding="utf-8")):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


# --------------------------------------------------------- missing_entrypoint

def _missing_entrypoint(adapter, parsed, files, primary):
    out = []
    for ep in parsed.get("entrypoints") or []:
        src = contract_mod._source_for_entrypoint(adapter, ep, files)
        if src is None:
            out.append(_finding(
                "missing_entrypoint", "critical", primary,
                "계약의 진입점 %s 가 실재하지 않는다" % ep.get("raw"),
                method=ep.get("method"), route=ep.get("path"),
                evidence="어댑터의 entrypoint_resolver 가 %r 을 파일로 해석하지 "
                         "못했다" % ep.get("path")))
    return out


# ---------------------------------------------------- untested_contract_item

def _untested(root, config, adapter, parsed, files, test_role):
    """심볼 문자열 **또는** 진입점 경로 — 둘 다 실패할 때만 지적한다 (§E6).

    커버리지 도구가 없는 상태에서 이 검사가 "테스트 약화" 탐지를 대신한다.
    """
    tests = _unit_test_files(adapter, files, parsed)
    blob = _concat(root, tests)
    out = []
    for unit in parsed.get("units") or []:
        symbol = unit.get("symbol")
        if not symbol:
            continue
        if re.search(r"\b%s\b" % re.escape(symbol), blob):
            continue
        link = _entrypoint_link(root, adapter, unit, parsed, files, blob)
        if link["covered"]:
            continue
        out.append(_finding(
            "untested_contract_item", "major", test_role,
            "계약의 유닛 %s 를 참조하는 테스트가 없다" % unit.get("raw"),
            symbol=symbol, container=unit.get("container"),
            entrypoint_link=link["state"],
            evidence="테스트 파일 %d개 어디에도 %r 이 없고, 이 유닛과 연결된 "
                     "진입점 경로도 없다 (진입점 연결: %s)"
                     % (len(tests), symbol, link["state"])))
    return out


def _entrypoint_link(root, adapter, unit, parsed, files, blob):
    """이 유닛이 **자기 진입점**의 경로 문자열로 커버되는가.

    반환: {"covered": bool, "state": "linked|unlinked|unresolved"}

    예전에는 `unit` 을 아예 안 읽고 "**아무** 진입점 경로가 blob 에 있는가"를
    답했다. 그래서 진입점 문자열 하나가 테스트 어딘가에 있으면 **전 유닛의
    지적이 억제됐다** — 사실상 이 검사가 꺼져 있었다 (G-2).

    연결은 한 홉까지 본다. 진입점이 해석한 파일이 ① 유닛의 컨테이너 파일이거나
    ② 그 파일 본문이 유닛의 심볼을 참조하면 연결이다. ②가 없으면 §E6 이
    진입점 폴백을 둔 이유(유닛이 라우트 핸들러가 **호출하는** 헬퍼인 경우)가
    사라진다.

    **해석에 실패하면 예전처럼 관대하게 낙하하되 그 사실을 남긴다.** 억제가
    침묵으로 일어나면 이 검사가 왜 조용한지 아무도 모른다.
    """
    symbol = unit.get("symbol") or ""
    container = (unit.get("container") or "").replace("\\", "/").lstrip("./")
    hit = None
    unresolved = False
    for ep in parsed.get("entrypoints") or []:
        path = ep.get("path")
        if not path or path not in blob:
            continue
        src = contract_mod._source_for_entrypoint(adapter, ep, files)
        if src is None:
            unresolved = True
            continue
        if container and (src == container or src.endswith("/" + container)):
            hit = src
            break
        try:
            text = (Path(root) / src).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unresolved = True
            continue
        if symbol and re.search(r"\b%s\b" % re.escape(symbol), text):
            hit = src
            break
    if hit is not None:
        return {"covered": True, "state": "linked"}
    if unresolved:
        # 해석 실패는 "커버됐다" 가 아니다. 다만 왜 억제되지 않았는지 남긴다.
        return {"covered": False, "state": "unresolved"}
    return {"covered": False, "state": "unlinked"}


def _concat(root, rels):
    parts = []
    for rel in rels:
        try:
            parts.append((Path(root) / rel).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(parts)


def _test_files(adapter, files):
    globs = (adapter.get("attribution") or {}).get("test_file_globs") or []
    return [f for f in files if harness.glob_any(globs, f)]


def _unit_test_files(adapter, files, parsed):
    """유닛·오류 어휘·진입점 테스트 검사가 세는 파일 — **e2e 는 빼고.**

    e2e 스펙이 오류 상수를 화면 문구로 단언하거나 심볼명을 담으면 유닛 테스트
    부재를 가린다. 빼는 것은 어댑터 `attribution.e2e_file_globs` 와 이 계약의 여정
    스펙 파일이다 — 코어는 e2e 가 어디 사는지 모른다 (ADR-H031).
    """
    e2e = (adapter.get("attribution") or {}).get("e2e_file_globs") or []
    specs = {contract_mod._source_for_container(j.get("container"), files)
             for j in parsed.get("journeys") or []}
    return [f for f in _test_files(adapter, files)
            if f not in specs and not harness.glob_any(e2e, f)]


# ------------------------------------------------ 진입점·오류 어휘·인가 테스트

# `from "…"` · `require("…")` · `import("…")`. 여러 줄에 걸친 동적 import 도 받는다.
_IMPORT_SPEC = re.compile(
    r"""(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*)["']([^"']+)["']""")

_RULES_TRIED = "같은 디렉터리의 스템 일치 · import 지정자 해석"


def _tests_for_entrypoint(src, tests, root, adapter):
    """이 진입점 파일을 검증하는 테스트 파일. **존재 검사 전용이다.**

    `contract._tests_for_source` 를 쓰지 않는 이유 (N1): 그 함수의 스템 일치는
    디렉터리를 보지 않아, 진입점 파일이 전부 `route.ts` 인 스택에서 리포의 모든
    `route.test.ts` 를 고른다. 스코프 선택에서 과선택은 비용이지만 **존재 검사에서는
    거짓 통과**다 — 검사가 항상 통과한다.

    규칙은 둘이다. (a) 같은 부모 디렉터리에서 스템이 같다 (b) 테스트 본문의 import
    지정자를 풀면 확장자 없이 `src` 와 같다. 상대 경로는 테스트의 디렉터리 기준,
    별칭은 어댑터 `attribution.import_aliases` 의 접두 치환이다. AST 는 쓰지 않는다.
    """
    parent = posixpath.dirname(src)
    stem = posixpath.basename(src).split(".")[0]
    target = posixpath.splitext(src)[0]
    aliases = (adapter.get("attribution") or {}).get("import_aliases") or {}
    out = []
    for t in tests:
        if (posixpath.dirname(t) == parent
                and posixpath.basename(t).split(".")[0] == stem):
            out.append(t)
            continue
        try:
            text = (Path(root) / t).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for spec in _IMPORT_SPEC.findall(text):
            if _resolve_spec(spec, t, aliases) == target:
                out.append(t)
                break
    return out


def _resolve_spec(spec, test_rel, aliases):
    """import 지정자 → 확장자 없는 리포 상대 경로. 풀 수 없으면 None."""
    if spec.startswith("."):
        path = posixpath.normpath(posixpath.join(posixpath.dirname(test_rel), spec))
    else:
        prefix = next((a for a in aliases if spec.startswith(a)), None)
        if prefix is None:
            return None               # 패키지 import — 리포 파일이 아니다
        path = posixpath.normpath(aliases[prefix] + spec[len(prefix):])
    base, ext = posixpath.splitext(path)
    return base if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs") else path


def _untested_entrypoint(root, adapter, parsed, files, tests, test_role):
    """진입점마다 **그 진입점의** 테스트 파일이 하나라도 있는가.

    반환: (지적, 해석하지 못한 진입점의 raw 목록). 경로 문자열 검색은 쓰지 않는다 —
    라우트 테스트는 핸들러를 직접 호출해 경로가 본문에 안 나온다.
    """
    out, unresolved = [], []
    for ep in parsed.get("entrypoints") or []:
        src = contract_mod._source_for_entrypoint(adapter, ep, files)
        if src is None:
            unresolved.append(ep.get("raw"))
            continue
        if _tests_for_entrypoint(src, tests, root, adapter):
            continue
        out.append(_finding(
            "untested_entrypoint", "major", test_role,
            "계약의 진입점 %s 를 검증하는 테스트가 없다" % ep.get("raw"),
            method=ep.get("method"), route=ep.get("path"), path=src,
            evidence="%s 를 가리키는 테스트 파일이 없다 (시도한 규칙: %s)"
                     % (src, _RULES_TRIED)))
    return out, unresolved


def _untested_error_symbol(root, parsed, tests, test_role):
    """오류 어휘 상수가 테스트 본문에 한 번도 안 나오는가. 문자열 존재 검사다."""
    blob = _concat(root, tests)
    out = []
    for name in parsed.get("errors") or []:
        if re.search(r"\b%s\b" % re.escape(name), blob):
            continue
        out.append(_finding(
            "untested_error_symbol", "major", test_role,
            "계약의 오류 어휘 %s 를 단언하는 테스트가 없다" % name,
            symbol=name,
            evidence="테스트 파일 %d개 어디에도 %r 이 없다" % (len(tests), name)))
    return out


def _authz_untested(root, adapter, parsed, files, tests, rx, test_role):
    """`[역할]` 태그 진입점은 **그 진입점의** 테스트에 거부 단언 패턴이 있는가.

    다른 라우트의 거부 테스트는 세지 않는다. 존재 검사이지 의미 검사가 아니다 —
    패턴이 본문에 있으면 통과이고, 그 단언이 맞는지는 리뷰어가 본다.
    """
    out = []
    for ep in parsed.get("entrypoints") or []:
        if not ep.get("tags"):
            continue
        src = contract_mod._source_for_entrypoint(adapter, ep, files)
        if src is None:
            continue                  # untested_entrypoint 가 unresolved 로 남긴다
        own = _tests_for_entrypoint(src, tests, root, adapter)
        if rx.search(_concat(root, own)):
            continue
        out.append(_finding(
            "authz_untested", "major", test_role,
            "역할 %s 가 걸린 진입점 %s 에 거부 테스트가 없다"
            % (", ".join(ep["tags"]), ep.get("raw")),
            method=ep.get("method"), route=ep.get("path"), path=src,
            tags=ep["tags"],
            evidence="이 진입점의 테스트 파일 %d개에 %r 이 없다"
                     % (len(own), rx.pattern)))
    return out


def _missing_journey_spec(root, parsed, files, test_role):
    """계약 `## 여정` 의 스펙 파일이 실재하고 슬러그가 **선언**으로 있는가.

    파일 전문의 `\b슬러그\b` 는 주석 한 줄로 통과하므로 쓰지 않는다 — 최상위
    그룹 이름(`describe`/`test.describe` 의 문자열 인자)이나 `export` 이름만 센다.
    """
    out = []
    for j in parsed.get("journeys") or []:
        src = contract_mod._source_for_container(j.get("container"), files)
        if src is not None and _has_symbol_declared(Path(root) / src, j["symbol"]):
            continue
        out.append(_finding(
            "missing_journey_spec", "critical", test_role,
            "계약의 여정 %s 의 스펙이 없다" % j.get("raw"),
            container=j.get("container"), symbol=j["symbol"], path=src,
            evidence=("%r 이 리포에 없다" % j.get("container") if src is None else
                      "%s 에 %r 을 이름으로 하는 describe 나 export 가 없다"
                      % (src, j["symbol"]))))
    return out


def _has_symbol_declared(path, symbol):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    s = re.escape(symbol)
    rx = re.compile(r"""\b(?:test\.)?describe(?:\.\w+)?\(\s*["'`]%s["'`]"""
                    r"|\bexport\s+(?:const|function|let)\s+%s\b" % (s, s))
    return rx.search(text) is not None


# ------------------------------------------------------------ out_of_contract

def _out_of_contract(root, adapter, parsed, changed, primary):
    """계약에 없는 **신규** public 심볼.

    변경된 파일만 보고, 그 안에서도 **추가된 줄만** 본다. 예전에는 변경된 파일의
    본문 전체를 정규식에 태워 "계약에 없는 모든 public 심볼" 을 셌고 — 새것인지
    묻는 줄이 없었다. docstring 은 처음부터 "신규" 라 적고 있었다.

    그 어긋남의 값이 실측됐다: P2 46 + P3 32 = **78/78 이 구조적 오탐**이었고,
    P3 의 32건 중 24건은 `env.ts` 의 `MAX_PHOTOS`·`DEFAULT_MODEL` 처럼 그 런이
    손도 안 댄 상수였다. 산문이 기계 사실을 참칭하는 이 리포의 반복 결함이
    검사 자신에게서 났다.

    `changed` 가 `None` 이면 VCS 에 묻는다.
    """
    if changed is None:
        changed = _changed_files(root)
    if not changed:
        return []
    rx = re.compile(
        (adapter.get("attribution") or {}).get("public_symbol_regex")
        or _DEFAULT_PUBLIC, re.M)
    globs = (adapter.get("attribution") or {}).get("test_file_globs") or []

    known = contract_mod.symbols(parsed)
    # **진입점 파일이 관례로 내보내는 이름은 계약 밖이 아니다** (ADR-H049).
    # 라우트 파일의 `POST` · `maxDuration` 은 스택이 정한 이름이라 계약의
    # `## 유닛` 에 다시 적을 것이 아닌데, 파일럿에서 9회/7런 반복 오탐이 났다
    # (`maxduration-route-config-not-out-of-contract`). 어댑터가 선언한다.
    implied = set((adapter.get("entrypoint_resolver") or {})
                  .get("implied_exports") or [])
    out = []
    for rel in changed:
        if harness.glob_any(globs, rel):
            continue              # 테스트의 헬퍼는 계약의 대상이 아니다
        text = _added_lines(root, rel)
        if text is None:
            continue
        at_entrypoint = bool(implied) and contract_mod.is_entrypoint_file(adapter, rel)
        for m in rx.finditer(text):
            name = m.group("name")
            if name in known:
                continue
            if at_entrypoint and name in implied:
                continue
            out.append(_finding(
                "out_of_contract", "major", primary,
                "계약에 없는 public 심볼 %s 가 생겼다" % name,
                symbol=name, path=rel,
                evidence="%s 가 %r 을 내보내는데 계약이 그것을 말하지 않는다"
                         % (rel, name)))
    return out


def _added_lines(root, rel):
    """이 파일에서 **추가된 줄**만. 읽지 못하면 `None`.

    추적분은 `git diff -U0 HEAD` 의 `+` 줄이고, 추적되지 않는 새 파일은 본문
    전체가 추가분이다 — 03 이 방금 쓴 코드가 정확히 그 상태다.

    지운 줄(`-`)은 들어오지 않는다. 삭제를 신규로 세면 심볼을 지우는 것이
    지적이 된다.

    `+` 를 뗀 줄을 그대로 잇는다. `public_symbol_regex` 가 `^\\s*export …` 라
    줄 단위로 물기 때문에 이어 붙여도 뜻이 안 바뀐다.
    """
    p = Path(root) / rel
    r = harness._git(root, "diff", "-U0", "HEAD", "--", rel)
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return "\n".join(line[1:] for line in r.stdout.splitlines()
                         if line.startswith("+") and not line.startswith("+++"))
    # diff 가 비었다 = 추적되지 않는 새 파일이거나, git 이 답하지 못했다.
    # 둘 다 본문 전체를 추가분으로 본다 — 신규 파일을 놓치면 03 이 만든 심볼이
    # 통째로 안 보인다.
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _changed_files(root):
    """미커밋 변경 + 새 파일. git 이 답하지 못하면 빈 목록이다.

    빈 목록은 "변경이 없다"가 아니라 **"모른다"** 이고, 그래서 이 검사가 조용히
    아무것도 못 잡는다. 호출부가 `changed` 를 명시적으로 주는 쪽이 정확하다.

    **base 대비 변경은 안 본다.** 예전 docstring 이 "base 대비 변경 + 미커밋"
    이라 적었지만 `git status` 는 미커밋만 답한다 — 산문을 구현에 맞춘다.
    """
    out = []
    # `-uall` — 새 디렉터리를 한 줄로 뭉치면 그 안의 새 심볼을 통째로 놓친다.
    r = harness._git(root, "status", "--porcelain", "-uall")
    if r is not None and r.returncode == 0:
        for line in r.stdout.splitlines():
            if len(line) > 3:
                out.append(line[3:].strip().replace("\\", "/"))
    return sorted(set(out))
