#!/usr/bin/env python3
"""`contract-trace` — 계약이 말한 것이 코드에 실제로 있는가.

**05 에서 두 번째로 도는 검사이고 무료다.** 리뷰어를 부르기 전에 여기서 잡으면
뒤에서 되돌릴 일이 없다. 모델을 한 번도 부르지 않는다.

검사는 다섯이다 (덜어내기 Wave 4 · ADR-H075 — 오탐 이력의 둘(유닛 참조·계약 밖
심볼), 재본 적 없는 인가 검사, 절이 사라진 화면·여정 검사를 지웠다):

| 코드 | 무엇 | 심각도 |
|---|---|---|
| `missing_impl`            | 계약의 유닛이 소스에 있는가        | critical |
| `missing_error_symbol`    | 오류 어휘 상수가 실재하는가        | critical |
| `missing_entrypoint`      | 진입점이 실재하는가                | critical |
| `untested_entrypoint`     | 진입점마다 그 진입점의 테스트 파일이 있는가 | major (유예 없음, 03 이 먼저 거부) |
| `untested_error_symbol`   | 오류 어휘 상수를 테스트가 한 번이라도 쓰는가 | major (유예 없음, 03 이 먼저 거부) |

테스트 둘(`untested_*`)은 **존재 검사이지 의미 검사가 아니다.** 커버리지 도구가
없는 stdlib 실행기라 "그 이름이 테스트 본문에 있는가" 까지만 본다 — 단언이 맞는지는
05 리뷰어의 몫이다. 뒤 둘은 banana 실측(계약 15개 × 현재 테스트 트리)에서 진입점 14 ·
오류 상수 10 오탐 0 이었다 (ADR-H058).

**파일명이 `trace.py` 가 아닌 이유**: 이 패키지는 `sys.path` 에 자기 디렉터리를
넣으므로 모듈 이름이 프로세스 전역 최상위가 된다. `trace` 는 stdlib 모듈이고,
그 이름을 쓰면 stdlib 을 가린다.

**스킵을 통과로 적지 않는다.** `entrypoint_resolver` 가 없으면 진입점을 푸는 검사만 빠지고
그 사실과 사유가 `skipped`·`skip_reasons` 에 남는다. `no_contract` 런은 `skipped_no_contract` 다.
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

CHECKS = ("missing_impl", "missing_error_symbol", "missing_entrypoint",
          "untested_entrypoint", "untested_error_symbol")

_NO_RESOLVER = "어댑터에 `entrypoint_resolver` 가 없다"


# 계약 대조에서 나온 지적의 category.
# 계약과 코드가 어긋난 것은 "계약 결함"과 다르다 — 여기서는 코드가 계약을
# 아직 안 지킨 것이고, 계약 자체가 틀렸다는 판정은 리뷰어·사람의 몫이다.
CATEGORY = {
    "missing_impl": "BOUNDARY_VIOLATION",
    "missing_error_symbol": "BOUNDARY_VIOLATION",
    "missing_entrypoint": "BOUNDARY_VIOLATION",
    "untested_entrypoint": "TEST_MISSING_FAILURE_PATH",
    "untested_error_symbol": "TEST_MISSING_FAILURE_PATH",
}


def run(root, config, adapter, contract_path, no_contract=False):
    """다섯 검사를 돌린다. 반환은 그대로 `05_trace.json` 이 된다."""
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

    primary = config.get("primary_role") or "impl"

    findings, checks_run, skipped, skip_reasons = [], [], [], {}

    checks_run.append("missing_impl")
    findings += _missing_impl(root, parsed, files, primary)

    checks_run.append("missing_error_symbol")
    findings += _missing_error_symbol(root, parsed, config, files, primary)

    if resolver == "none":
        skipped.append("missing_entrypoint")
        skip_reasons["missing_entrypoint"] = _NO_RESOLVER
    else:
        checks_run.append("missing_entrypoint")
        findings += _missing_entrypoint(adapter, parsed, files, primary)

    tc = _test_checks(root, adapter, parsed, files, primary)
    findings += tc["findings"]
    checks_run += tc["checks_run"]
    skipped += tc["skipped"]
    skip_reasons.update(tc["skip_reasons"])

    blocking = [f for f in findings if f["severity"] == "critical"]
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
        "blocking": len(blocking),
        # 04 의 `contract.scope.repo_files` 와 같아야 한다 (M50).
        "repo_files": len(files),
        "contract": {"units": len(parsed.get("units") or []),
                     "entrypoints": len(parsed.get("entrypoints") or []),
                     "errors": len(parsed.get("errors") or []),
                     "dropped": parsed.get("dropped") or []},
        "note": ("Critical 은 리뷰어를 부르기 전에 선수리한다 — 계약과 코드가 "
                 "어긋난 채로 리뷰하면 리뷰어가 그것을 다시 발견하는 데 돈을 쓴다."),
    }


def required_tests(root, config, adapter, contract_path):
    """테스트 존재 검사 둘만 — **03 제출이 부른다** (ADR-H058 결정 7·8).

    05 의 `run()` 과 **같은 `_test_checks`** 를 쓴다. 두 자리가 다른 목록을 보면
    03 통과가 05 지적을 예고하지 못한다. 05 의 Major 는 `deferred` 로
    남을 뿐 수리 루프를 돌리지 않으므로, 워커 맥락이 살아 있는 03 에서 요구해야
    실제로 고쳐진다. 유예가 없어 지적이 곧 거부다.

    반환: {"findings": [...], "skipped": [...], "skip_reasons": {...}}
    """
    root = Path(root)
    parsed = contract_mod.parse(Path(contract_path).read_text(encoding="utf-8"),
                                config)
    tc = _test_checks(root, adapter, parsed, repo_files(root),
                      config.get("primary_role") or "impl")
    return {"findings": tc["findings"], "skipped": tc["skipped"],
            "skip_reasons": tc["skip_reasons"]}


def _test_checks(root, adapter, parsed, files, test_role):
    """`untested_entrypoint` · `untested_error_symbol`."""
    resolver = (adapter.get("entrypoint_resolver") or {}).get("kind") or "none"
    tests = _unit_test_files(adapter, files)
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

    return out


def repo_files(root):
    """추적 파일 **+ 아직 커밋되지 않은 새 파일.**

    정본은 `harness.list_files_with_untracked` 다. 04 게이트도 같은 함수를
    쓴다 — 두 페이즈가 같은 계약을 두고 다른 파일 목록을 보면 04 는
    `unmatched: 4` 를, 05 는 `dropped: []` 를 적는다 (M50).
    """
    return harness.list_files_with_untracked(root)


def _finding(code, severity, role, title, **kw):
    """`rule_slug` 를 여기서 단다 — 검사 코드를 category 옆에 남긴다 (ADR-H034).

    `code` 를 그대로 쓰지 않고 이름을 달리한 것은 이 리포에서 `code` 가 이미
    **리뷰어 코드**(`cli.py` 라우팅)와 **카테고리 코드**(`CATEGORY`)
    두 뜻으로 쓰이기 때문이다. 세 번째 뜻을 얹지 않는다.

    `CATEGORY` 가 다대일이라(코드 5 → category 2) category 만으로는
    `missing_impl` 과 `missing_entrypoint` 를 못 가른다. 슬러그가 그것을
    가르고, 동시에 제목에 박힌 심볼 이름을 축에서 뺀다.
    """
    out = {"code": code, "rule_slug": code, "severity": severity,
           "target_role": role, "title": title, "category": CATEGORY[code],
           "source": "contract-trace", "resolution": "deferred"}
    out.update(kw)
    return out


# ------------------------------------------------------------- missing_impl

def _missing_impl(root, parsed, files, primary, key="units", code="missing_impl",
                  noun="유닛"):
    """**컨테이너명 + 심볼명 쌍**으로 찾는다.

    심볼명만 보면 흔한 이름이 다른 파일에 있어 거짓 통과한다. 컨테이너를 리포의
    파일과 맞추지 못하면 통과가 아니라 `container_resolved: false` 로 낙하한다 —
    "못 찾았다"가 "없다"보다 약한 판정이지만 **침묵보다는 강하다.**

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


def _unit_test_files(adapter, files):
    """오류 어휘·진입점 테스트 검사가 세는 파일 — **e2e 는 빼고.**

    e2e 스펙이 오류 상수를 화면 문구로 단언하거나 심볼명을 담으면 유닛 테스트
    부재를 가린다. 빼는 것은 어댑터 `attribution.e2e_file_globs` 다 — 코어는
    e2e 가 어디 사는지 모른다 (ADR-H031).
    """
    e2e = (adapter.get("attribution") or {}).get("e2e_file_globs") or []
    return [f for f in _test_files(adapter, files) if not harness.glob_any(e2e, f)]


# ----------------------------------------------------- 진입점·오류 어휘 테스트

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



