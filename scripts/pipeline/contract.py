#!/usr/bin/env python3
"""계약 파싱과 스코프 선택자 조립.

절 제목은 **코어의 것이 아니다.** `config.contract.sections` 가 단일 출처이고,
언어를 바꾸려면 그 값과 템플릿만 고치면 된다 — 이 파일에는 어느 언어의 제목도
박혀 있지 않다.

선택자는 **테스트 이름이 아니라 파일 경로**로 조립한다. 이름 필터는 러너가 모든
파일을 수집·변환하고 환경을 세운 *뒤에* 거르므로 비용을 줄이지 못한다. 이 리포의
실측에서 이름 필터는 전체 대비 100.2%, 경로 필터는 14.6%(6.8배 싸다)였다.
"""

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402

_BACKTICK = re.compile(r"`([^`]+)`")
_SEPARATORS = ("·", "::", "#", " > ")
_SYMBOL = re.compile(r"[A-Za-z_][\w$]*")

# 「데이터 형태」 절이 이름 붙이는 것의 형태 — **타입 아니면 상수다** (M57).
#
# 이 절은 산문이 섞여 있어 백틱 안에 필드명(`retryAfterSeconds`)·내장
# (`map`·`any`·`globalThis`)·경로(`src/lib/env.ts`)가 함께 온다. 형태로 거르지
# 않고 다 모으면 `symbols()` 가 넓어져 **오탐 대신 미탐**이 생긴다 — 흔한 낱말이
# 계약 산문에 있다는 이유로 진짜 위반이 조용히 통과한다.
#
# `_errors` 가 `.isupper()` 로 하는 것과 같은 관용구이고, 다만 이 절은 타입도
# 담으므로 PascalCase 를 함께 받는다.
_DATA_SHAPE_NAME = re.compile(r"^(?:[A-Z][A-Za-z0-9]*|[A-Z][A-Z0-9_]*)$")
_METHOD_PATH = re.compile(r"^\s*(?P<method>[A-Z]+)\s+(?P<path>/\S*)")


def section(text, heading):
    """헤딩 줄부터 다음 `## ` 까지. 없으면 빈 문자열."""
    if not heading:
        return ""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == heading)
    except StopIteration:
        return ""
    out = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out)


def parse(text, config):
    """{units: [...], entrypoints: [...], errors: [...]}.

    각 유닛은 `{container, symbol, raw}` 다. **컨테이너와 심볼을 함께** 본다 —
    심볼명만 보면 흔한 이름이 다른 파일에서 거짓 통과한다.
    """
    sections = (config.get("contract") or {}).get("sections") or {}
    units, dropped = _units(section(text, sections.get("units")))
    return {
        "units": units,
        "dropped": dropped,
        "entrypoints": _entrypoints(section(text, sections.get("entrypoints"))),
        "errors": _errors(section(text, sections.get("errors"))),
        "data_shapes": _data_shapes(section(text, sections.get("data_shapes"))),
    }


def symbols(parsed):
    """계약이 이름 붙인 것 전부. 귀속의 `in_contract` 판정이 쓴다.

    **「데이터 형태」도 여기 들어온다** (M57). 그 절이 빠져 있어서 계약이 이름
    붙인 타입·상수가 전부 `out_of_contract` 로 잡혔고, P8 의 지적 6/6 이 그
    구조적 오탐이었다. 한 곳만 고치면 소비자 둘(`trace_contract` 의
    `out_of_contract` · `gate` 의 실패 귀속)이 함께 낫는다.
    """
    out = set()
    for u in parsed.get("units") or []:
        if u.get("symbol"):
            out.add(u["symbol"])
    for e in parsed.get("errors") or []:
        out.add(e)
    for d in parsed.get("data_shapes") or []:
        out.add(d)
    return out


def _units(block):
    """(유닛, 버려진 것). **들여쓴 불릿은 유닛이 아니다.**

    유닛은 최상위 불릿 하나에 하나다. 들여쓴 줄은 그 유닛의 정상·예외 서술이고,
    거기 백틱이 있다고 유닛으로 세면 설명이 많은 계약일수록 유닛이 부풀어
    프로파일 판정과 스코프 선택이 함께 오염된다.

    컨테이너와 심볼이 **둘 다** 없는 것도 유닛이 아니다 — 이 파서의 전제가
    컨테이너명+심볼명 쌍이기 때문이다. 다만 **조용히 버리지 않는다.** 무엇이 왜
    빠졌는지 둘째 반환값에 남는다.
    """
    out, dropped = [], []
    for line in block.splitlines():
        if not line.startswith("-"):
            continue
        spans = _BACKTICK.findall(line)
        if not spans:
            continue
        container, symbol = _split(spans[0])
        item = {"container": container, "symbol": symbol, "raw": spans[0]}
        if container and symbol:
            out.append(item)
        else:
            dropped.append(dict(item, reason="컨테이너명과 심볼명 쌍이 아니다"))
    return out, dropped


def _split(span):
    for sep in _SEPARATORS:
        if sep in span:
            left, right = span.split(sep, 1)
            return left.strip(), _first_symbol(right)
    return None, _first_symbol(span)


def _first_symbol(text):
    m = _SYMBOL.search(text or "")
    return m.group(0) if m else None


def _entrypoints(block):
    out = []
    for line in block.splitlines():
        if not line.strip().startswith("-"):
            continue
        for span in _BACKTICK.findall(line) or [line.strip("- ").strip()]:
            m = _METHOD_PATH.match(span)
            if m:
                out.append({"method": m.group("method"), "path": m.group("path"),
                            "raw": span.strip()})
                break
    return out


def _errors(block):
    out = []
    for line in block.splitlines():
        if not line.strip().startswith("-"):
            continue
        for span in _BACKTICK.findall(line):
            name = _first_symbol(span)
            if name and name.isupper():
                out.append(name)
                break
    return out


def _data_shapes(block):
    """계약이 「데이터 형태」에 이름 붙인 타입·상수 (M57).

    **`_errors` 와 달리 최상위 불릿만 보지 않는다.** 이 절은 상수를 불릿의
    **연속 줄**에 나열하는 것이 실물의 모양이고(P8 의 계약이 그랬다), 최상위
    불릿만 보면 여섯 중 둘밖에 못 잡는다. 대신 형태로 좁힌다 —
    `_DATA_SHAPE_NAME` 이 무엇을 왜 거르는지 적는다.

    한 줄에 이름이 여럿 올 수 있으므로 `_errors` 처럼 첫 스팬에서 `break`
    하지 않는다. P8 의 계약은 한 줄에 상수 둘·셋을 적었다.
    """
    out = []
    for line in block.splitlines():
        for span in _BACKTICK.findall(line):
            name = _first_symbol(span)
            if name and _DATA_SHAPE_NAME.match(name):
                out.append(name)
    return out


# --------------------------------------------------------------- 선택자 조립

def test_selectors(root, config, adapter, parsed, repo_files=None):
    """계약 심볼 → 소스 경로 → **테스트 파일 경로**.

    반환: {"paths": [...], "unmatched": [...], "entrypoint_resolver": "..."}

    스택 지식은 전부 `adapter.attribution.test_file_globs` 라는 **데이터**에
    있다. 코어에 확장자도 네이밍 규칙도 없다.
    """
    files = (repo_files if repo_files is not None
             else harness.list_files_with_untracked(root))
    tests = [f for f in files
             if harness.glob_any(
                 (adapter.get("attribution") or {}).get("test_file_globs") or [], f)]

    sources, unmatched = [], []
    for unit in parsed.get("units") or []:
        src = _source_for_container(unit.get("container"), files)
        if src:
            sources.append(src)
        else:
            unmatched.append({"kind": "unit", "raw": unit.get("raw"),
                              "why": "컨테이너를 리포의 파일과 맞추지 못했다"})

    resolver = (adapter.get("entrypoint_resolver") or {}).get("kind") or "none"
    for ep in parsed.get("entrypoints") or []:
        if resolver == "none":
            unmatched.append({"kind": "entrypoint", "raw": ep.get("raw"),
                              "why": "진입점 해석기가 없다 — 이 검사만 건너뛴다"})
            continue
        src = _source_for_entrypoint(adapter, ep, files)
        if src:
            sources.append(src)
        else:
            unmatched.append({"kind": "entrypoint", "raw": ep.get("raw"),
                              "why": "진입점을 파일로 해석하지 못했다"})

    paths = []
    for src in sources:
        got = _tests_for_source(root, src, tests, parsed)
        if not got:
            # **조용히 0경로를 기여하지 않는다.** 컨테이너는 풀렸는데 대응
            # 테스트가 없는 소스가 지금까지 `unmatched` 에 남지 않아, scoped 가
            # 실제보다 좁게 돌고 아무도 몰랐다 (M28).
            unmatched.append({"kind": "source", "raw": src,
                              "why": "대응 테스트를 찾지 못했다"})
        for t in got:
            if t not in paths:
                paths.append(t)

    # **넓히면 퇴화를 재야 한다.** 선택 경로가 전체 테스트에 가까우면 그것은
    # scoped 가 아니라 full 이고, "scoped 통과" 라고 적는 것이 새 자리의
    # 조용한 통과가 된다.
    ratio = (float(len(paths)) / len(tests)) if tests else 0.0
    return {"paths": sorted(paths), "unmatched": unmatched,
            "entrypoint_resolver": resolver,
            # 04 와 05 가 **같은 목록을 봤는지**가 사후에 보여야 한다. P6 에서
            # 04 는 추적분만, 05 는 미추적까지 봐서 같은 계약에 다른 말을 했다
            # (M50). 지금은 같은 함수를 쓰므로 두 수가 같아야 한다.
            "repo_files": len(files),
            "selected": len(paths), "test_files": len(tests),
            "selected_ratio": round(ratio, 3),
            "degenerate": bool(tests) and ratio >= DEGENERATE_RATIO}


def _source_for_container(container, files):
    """`lib/match.ts` → `src/lib/match.ts`. 접미사 일치로 실재를 확인한다."""
    if not container:
        return None
    needle = container.replace("\\", "/").lstrip("./")
    exact = [f for f in files if f == needle]
    if exact:
        return exact[0]
    suffix = [f for f in files if f.endswith("/" + needle)]
    return suffix[0] if len(suffix) == 1 else (suffix[0] if suffix else None)


_BRACE_PARAM = re.compile(r"\{(\w+)\}")


def _param_variants(captured, styles):
    """계약이 `{id}` 로 적은 경로 파라미터를 어댑터가 선언한 괄호 쌍(`[]` 등)으로
    바꾼 후보들. 원문이 먼저다 — 계약이 이미 실제 폴더명을 썼으면 그대로 맞는다.

    파일럿 ad59(FR-007) 는 계약에 API_SPEC 표기 `{id}` 를 그대로 써 resolver 가
    `src/app/api/contents/{id}/…/route.ts` 를 찾다 `missing_entrypoint` critical
    을 냈다 (ADR-H049). 변환 규칙은 스택 관례라 어댑터의 `param_styles` 가
    선언하고 여기는 읽기만 한다 (ADR-H031).
    """
    out = [captured]
    for style in styles or []:
        if not isinstance(style, str) or len(style) != 2:
            continue
        alt = _BRACE_PARAM.sub(lambda m: style[0] + m.group(1) + style[1], captured)
        if alt not in out:
            out.append(alt)
    return out


def _source_for_entrypoint(adapter, ep, files):
    resolver = adapter.get("entrypoint_resolver") or {}
    styles = resolver.get("param_styles") or []
    for entry in resolver.get("map") or []:
        route, target = entry.get("route"), entry.get("file")
        if not route or not target:
            continue
        rx = re.compile("^" + re.escape(route).replace(r"\{p\}", "(?P<p>.+)") + "$")
        m = rx.match(ep.get("path") or "")
        if not m:
            continue
        for captured in _param_variants(m.group("p"), styles):
            candidate = target.replace("{p}", captured)
            if candidate in files:
                return candidate
    return None


def is_entrypoint_file(adapter, rel):
    """이 파일이 어댑터 진입점 관례(`entrypoint_resolver.map[].file`)에 맞는가."""
    resolver = adapter.get("entrypoint_resolver") or {}
    for entry in resolver.get("map") or []:
        target = entry.get("file")
        if not target:
            continue
        rx = re.compile("^" + re.escape(target).replace(r"\{p\}", ".+") + "$")
        if rx.match(rel or ""):
            return True
    return False


# scoped 가 사실상 full 이 되는 지점. 넘으면 퇴화로 표기한다.
DEGENERATE_RATIO = 0.9


def _tests_for_source(root, src, tests, parsed=None):
    """이 소스를 검증하는 테스트 파일. **stem 일치 ∪ 내용 참조.**

    stem 일치만 보면 `page.tsx` → `edge-cases.test.tsx` 같은 통합 테스트가
    빠져 **수리 루프 안에서 한 번도 안 돌고** `full` 이 뒤에서 잡는다 (M28).

    내용 참조는 소스의 stem 을 **경로 형태로** 찾는다(`./match`·`/match'`).
    stem 을 그냥 포함으로 보면 `match` 가 `mismatch.test.ts` 를 끌고 오고,
    `id`·`api` 같은 짧은 stem 은 사실상 전체를 고른다.

    스택 지식은 넣지 않는다 — 확장자도 네이밍 규칙도 보지 않고 문자열만 본다.
    """
    stem = Path(src).name.split(".")[0]
    out = [t for t in tests if Path(t).name.split(".")[0] == stem]
    needles = ["/%s'" % stem, '/%s"' % stem, "/%s.js" % stem, "/%s'" % stem]
    needles += ["'%s'" % stem, '"%s"' % stem]
    for t in tests:
        if t in out:
            continue
        try:
            text = (Path(root) / t).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(n in text for n in needles):
            out.append(t)
    return out
