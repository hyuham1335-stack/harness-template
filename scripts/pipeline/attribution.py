#!/usr/bin/env python3
"""실패 귀속 — 순수 함수만.

입력이 dict, 출력이 dict다. 파일 I/O·서브프로세스·상태 쓰기·출력이 없다.
**직렬화 가능한 것이 곧 replay 픽스처다** — 실물 러너 없이 수리 루프를
시뮬레이션할 수 있는 것이 이 경계에서 나온다.

소유 판정은 계약 계층의 함수 하나로만 한다. 같은 규칙이 두 곳에서 갈라지는
것이 이 리포가 이미 기록한 실패다.
"""

import hashlib
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402

# 단언 판정의 **폴백**이다. 1차 신호는 리포트의 실패 유형 속성이고, 여기에는
# 러너 고유 매처 이름을 넣지 않는다 — 코어에 스택이 박히면 안 된다.
ASSERTION_TOKENS = ("assertionerror", "assert", "expect")

# 스키마·데이터 계열. 이 분기는 어댑터가 마이그레이션을 선언했을 때만 활성이다.
SCHEMA_TOKENS = ("migration", "schema", "column", "constraint", "relation")

_PATH_TOKEN = re.compile(r"[\w.\-/\\]+\.[A-Za-z0-9]+")
_MASK_PATH = re.compile(r"[A-Za-z]:?[\\/][\w.\-\\/]+")
_MASK_HASH = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_MASK_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+")
_MASK_NUM = re.compile(r"\d+")


# ------------------------------------------------------------------ 소유 판정

def owner_for_path(config, path):
    """어느 역할의 것인가. 아니면 None.

    `harness.owns_file` 외의 경로가 없다 — doctor·실행기와 세 번째 호출자가
    되면서 단일 출처가 유지된다.
    """
    for role in config.get("roles") or []:
        if harness.owns_file(role, path):
            return role["id"]
    return None


def is_test_file(adapter, path):
    return harness.glob_any(
        (adapter.get("attribution") or {}).get("test_file_globs") or [], path)


def is_app_frame(adapter, path):
    prefixes = (adapter.get("attribution") or {}).get("app_frame_prefixes") or []
    return harness.glob_any([p.rstrip("/") + "/**" for p in prefixes], path)


def frames_from(detail, repo_files):
    """스택에서 프레임을 뽑는다. **리포에 실재하는 파일만 인정한다.**

    이 한 규칙이 스택별 트레이스 문법 차이를 흡수하고 오탐을 없앤다.
    어댑터에 프레임 정규식 필드를 늘리지 않는 이유이기도 하다.
    """
    known = set(repo_files or [])
    out = []
    for token in _PATH_TOKEN.findall(detail or ""):
        rel = token.replace("\\", "/").lstrip("./")
        for cand in (rel, rel.split(":")[0]):
            if cand in known and cand not in out:
                out.append(cand)
                break
    return out


def first_app_frame(adapter, frames):
    for f in frames or []:
        if is_app_frame(adapter, f) and not is_test_file(adapter, f):
            return f
    return None


# ------------------------------------------------------------------ 시그니처

def normalize_message(msg):
    text = _MASK_TS.sub("<ts>", msg or "")
    text = _MASK_PATH.sub("<path>", text)
    text = _MASK_HASH.sub("<hash>", text)
    text = _MASK_NUM.sub("<n>", text)
    return re.sub(r"\s+", " ", text).strip()


def signature(owner, unit, ftype, msg):
    raw = "|".join([str(owner), str(unit), str(ftype), normalize_message(msg)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ 0단계

def classify_infra(adapter, exit_code, log_text):
    """외부 의존 실패인가. 종료 코드가 0 이 아닐 때만 본다."""
    if exit_code == 0:
        return None
    for pat in adapter.get("infra_failure_patterns") or []:
        try:
            if re.search(pat, log_text or ""):
                return pat
        except re.error:
            continue
    return None


# ------------------------------------------------------------------ 1단계

def attribute_compile(adapter, config, contract_symbols, log_text):
    """컴파일·타입 실패의 소유자."""
    att = adapter.get("attribution") or {}
    pattern = att.get("compile_error_regex")
    if not pattern:
        return []
    try:
        rx = re.compile(pattern)
    except re.error:
        return []

    out = []
    for line in (log_text or "").splitlines():
        m = rx.search(line)
        if not m:
            continue
        path = (m.groupdict().get("path") or "").replace("\\", "/")
        owner = owner_for_path(config, path)
        reason = "compile_error_regex → owns"

        # 테스트 쪽 컴파일 실패의 상당수는 구현 역할이 계약 시그니처를 안 지켜서
        # 생긴다. 경로만 보고 테스트 역할에 보내면 매번 오귀속된다.
        symbol = _symbol_in_line(line, att.get("symbol_not_found_patterns") or [],
                                 contract_symbols)
        if symbol:
            owner = config.get("primary_role")
            reason = ("계약 심볼 %r 이 없다는 실패 — primary_role 로 강제한다" % symbol)
        elif owner is None:
            owner = "ambiguous"
            reason = "경로에서 소유자를 정하지 못했다"

        out.append({"id": "C-%d" % (len(out) + 1), "kind": "compile",
                    "unit": path, "file": path, "ftype": "compile",
                    "message": line.strip(), "frames": [path] if path else [],
                    "owner": owner, "owner_reason": reason,
                    "in_contract": bool(symbol),
                    "sig": signature(owner, path, "compile", line)})
    return out


def _symbol_in_line(line, patterns, contract_symbols):
    """심볼 부재 패턴에 걸리고 그 심볼이 계약에 있으면 그 이름을 돌려준다."""
    low = line.lower()
    if not any(p.lower() in low for p in patterns):
        return None
    quoted = re.findall(r"['\"`]([A-Za-z_][\w$]*)['\"`]", line)
    for name in quoted:
        if name in (contract_symbols or set()):
            return name
    for token in re.findall(r"[A-Za-z_][\w$]*", line):
        if token in (contract_symbols or set()):
            return token
    return None


# ------------------------------------------------------------------ 2단계

def attribute_tests(adapter, config, contract_symbols, failed_units, repo_files=None):
    """테스트 실패의 소유자."""
    out = []
    has_migration = bool(adapter.get("migration"))
    test_role = _test_role_id(adapter, config)

    for i, unit in enumerate(failed_units or []):
        detail = "%s\n%s" % (unit.get("message") or "", unit.get("detail") or "")
        frames = unit.get("frames") or frames_from(detail, repo_files)
        app = first_app_frame(adapter, frames)
        in_contract = _mentions_contract_symbol(unit, contract_symbols)
        assertion = _is_assertion(unit)

        if not assertion and app:
            owner = owner_for_path(config, app) or "ambiguous"
            reason = "단언이 아닌 예외 · 최상단 앱 프레임 %s" % app
        elif has_migration and _looks_schema(unit):
            owner = config.get("primary_role")
            reason = "스키마·데이터 계열 메시지"
        elif assertion and not in_contract:
            owner = test_role or "ambiguous"
            reason = "단언 실패 · 계약에 심볼이 없다 [out_of_contract]"
        elif assertion and in_contract:
            owner = "ambiguous"
            reason = "단언 실패 · 계약에 심볼이 있다"
        else:
            owner = "ambiguous"
            reason = "판정 근거가 없다 — 틀린 확신보다 모른다가 낫다"

        out.append({"id": "T-%d" % (i + 1), "kind": "test",
                    "unit": unit.get("unit"), "file": unit.get("file"),
                    "ftype": unit.get("ftype") or "", "message": unit.get("message"),
                    "frames": frames, "owner": owner, "owner_reason": reason,
                    "in_contract": in_contract,
                    "sig": signature(owner, unit.get("unit"),
                                     unit.get("ftype"), unit.get("message"))})
    return out


def rules_inactive(adapter):
    """없는 것과 조용히 안 도는 것을 구분한다."""
    out = []
    if not adapter.get("migration"):
        out.append("스키마·데이터 분기 (adapter.migration == null)")
    if not (adapter.get("attribution") or {}).get("compile_error_regex"):
        out.append("컴파일 귀속 (compile_error_regex 없음)")
    return out


def _test_role_id(adapter, config):
    """테스트 파일 glob 과 겹치는 역할. 없으면 None."""
    globs = (adapter.get("attribution") or {}).get("test_file_globs") or []
    for role in config.get("roles") or []:
        owns = role.get("owns") or []
        if any(g in owns for g in globs) or any(
                harness.glob_any(owns, g.replace("**/", "").replace("*", "x"))
                for g in globs):
            return role["id"]
    return None


def _is_assertion(unit):
    ftype = (unit.get("ftype") or "").lower()
    if ftype:
        return any(tok in ftype for tok in ASSERTION_TOKENS)
    blob = ("%s %s" % (unit.get("message") or "", unit.get("detail") or "")).lower()
    return any(tok in blob for tok in ASSERTION_TOKENS)


def _looks_schema(unit):
    blob = ("%s %s" % (unit.get("message") or "", unit.get("detail") or "")).lower()
    return any(tok in blob for tok in SCHEMA_TOKENS)


def _mentions_contract_symbol(unit, contract_symbols):
    blob = " ".join(str(unit.get(k) or "") for k in ("unit", "message", "detail"))
    return any(sym and sym in blob for sym in (contract_symbols or set()))


# ------------------------------------------------------------------ 3단계

def resolve_ambiguous(failures, config, flip_state, roles=None):
    """ambiguous → primary_role → 동일 시그니처 재발 시 다음 역할 → 계약 결함.

    **동시 배정을 하지 않는다.** 같은 하나의 동작을 두고 둘에게 동시에 보내면
    구현은 테스트에 맞춰, 테스트는 구현에 맞춰 서로를 좇는 핑퐁이 나고 둘 다
    계약에서 멀어진다.

    사다리는 **이 런에 디스패치된 역할**(`roles`, 03 이 저장한 순서)로만 만든다
    (ADR-H057). 기록이 없으면 조건부 역할(`when_contract_section`)을 뺀다 — 불린
    적 없는 역할에게 수리를 보내지 않는다. 단언 실패(`kind: test`)는 조건부
    역할을 건너뛴다 — 그 역할은 테스트를 소유하지 않는다.
    """
    by_id = {r["id"]: r for r in config.get("roles") or []}
    if roles is None:
        order = [i for i, r in by_id.items() if not r.get("when_contract_section")]
    else:
        order = [i for i in roles if i in by_id]
    primary = config.get("primary_role")
    if primary in order:
        order = [primary] + [r for r in order if r != primary]

    out = []
    for f in failures:
        f = dict(f)
        if f.get("owner") != "ambiguous":
            out.append(f)
            continue
        ladder = ([i for i in order if not by_id[i].get("when_contract_section")]
                  if f.get("kind") == "test" else order)
        node = flip_state.setdefault(f["sig"], {"assigned": [], "count": 0})
        node["count"] += 1
        idx = len(node["assigned"])
        if idx < len(ladder):
            owner = ladder[idx]
            node["assigned"].append(owner)
            f["owner"] = owner
            f["owner_reason"] = ("ambiguous → %s" %
                                 ("primary_role" if idx == 0 else
                                  "동일 시그니처 재발 · 다음 역할로 넘긴다"))
            if idx > 0:
                f["carry_contract"] = True   # 계약 원문과 현재 시그니처를 함께 준다
        else:
            f["owner"] = "contract"
            f["owner_reason"] = "역할을 다 돌았는데 같은 실패다 — 계약 결함으로 재분류"
        out.append(f)
    return out


def owner_sig(failure):
    """정체 감지가 세는 단위. **시그니처가 아니라 (소유자, 시그니처) 다.**

    `signature()` 는 소유자를 해시 입력에 넣지만 그 소유자는 `resolve_ambiguous`
    가 돌기 **전** 값이라, ambiguous 실패의 sig 는 문자열 `"ambiguous"` 로 굳어
    라운드를 넘어 똑같이 유지된다. 그래서 sig 만 세면 flip 이 다음 역할을
    배정한 바로 그 라운드에 정체 감지가 먼저 멈추고, **배정이 지시로 나가지
    못한 채 버려진다** — ambiguous 실패는 두 역할 중 한쪽만 시도해 보게 된다
    (M33 · P3 가 실물에서 밟았다).

    배정 **뒤의** 소유자를 붙이면 그 둘이 더 이상 같은 조건이 아니다.
    """
    return "%s|%s" % (failure.get("owner"), failure.get("sig"))


def dispatch(failures, config, prev_sigs, flip_state, stuck_after=2, roles=None):
    """소유자별 배정. 같은 대상을 공유하면 하나만 보낸다.

    `prev_sigs` 는 이전 라운드들의 **쌍** 목록이다(`owner_sig`). `stuck_after`
    는 04 프론트매터의 `loop.stuck_after_identical` 이고, 그 값이 코드에 닿는
    유일한 경로다 — 예전에는 선언만 있고 `2` 가 여기 박혀 있어 값을 3 으로
    바꿔도 동작이 안 변했다.
    """
    resolved = resolve_ambiguous(failures, config, flip_state, roles=roles)
    sigs = [f["sig"] for f in resolved]
    pairs = [owner_sig(f) for f in resolved]
    prior = list(prev_sigs or [])
    threshold = max(int(stuck_after or 2), 1) - 1
    stuck = any(prior.count(p) >= threshold for p in pairs) if threshold else True

    by_owner = {}
    for f in resolved:
        by_owner.setdefault(f["owner"], []).append(f)

    targets = {owner: {t for f in items for t in (f.get("frames") or [f.get("file")])
                       if t}
               for owner, items in by_owner.items()}
    owners = list(by_owner)
    disjoint = True
    for i, a in enumerate(owners):
        for b in owners[i + 1:]:
            if targets.get(a, set()) & targets.get(b, set()):
                disjoint = False

    if len(owners) <= 1 or disjoint:
        return {"by_owner": by_owner, "owner": owners[0] if owners else None,
                "parallel": len(owners) > 1, "deferred": [], "stuck": stuck,
                "sigs": sigs, "pairs": pairs, "failures": resolved}

    chosen = max(owners, key=lambda o: len(by_owner[o]))
    deferred = [{"owner": o, "failure_count": len(by_owner[o]),
                 "reason": "동시 배정 금지 — 대상을 공유한다"}
                for o in owners if o != chosen]
    # **미룬 배정은 없던 일이다.** `resolve_ambiguous` 가 올린 flip 인덱스를
    # 그대로 두면 다음 라운드에 그 실패가 한 역할을 건너뛴다. 정체 체인도
    # 같다 — 나가지 않은 쌍을 쌓으면 다음 라운드의 첫 시도가 곧 정체다.
    for o in (d["owner"] for d in deferred):
        for f in by_owner[o]:
            if (f.get("owner_reason") or "").startswith("ambiguous"):
                node = flip_state.get(f["sig"])
                if node and node["assigned"] and node["assigned"][-1] == o:
                    node["assigned"].pop()
    pairs = [owner_sig(f) for f in by_owner[chosen]]
    return {"by_owner": {chosen: by_owner[chosen]}, "owner": chosen,
            "parallel": False, "deferred": deferred, "stuck": stuck,
            "sigs": sigs, "pairs": pairs, "failures": resolved}


# ------------------------------------------------------------- clean_ownership

def clean_ownership(root, config, claims):
    """변경 집합과 각 역할의 claim 을 대조한다.

    잡는 것 둘: (a) 소유 경로 위반 (b) 아무도 claim 하지 않은 orphan.
    소유 계산은 `harness.owns_file` 하나로만 한다.
    """
    changed = _changed_paths(root)
    if changed is None:
        return {"ok": True, "findings": [], "rollback": [],
                "message": "변경 집합을 읽지 못해 검사를 건너뛴다"}

    main_globs = config.get("main_owned_paths") or []
    claimed = {}
    for role in (claims or {}).get("roles") or []:
        for f in role.get("claimed_files") or []:
            claimed[f.replace("\\", "/")] = role.get("role")

    findings = []
    for path in changed:
        if harness.glob_any(main_globs, path):
            continue                       # 메인 소유는 위반이 아니다
        owner = owner_for_path(config, path)
        if owner is None:
            findings.append({"kind": "orphan", "path": path,
                             "message": "아무 역할도 소유하지 않는 경로가 바뀌었다"})
            continue
        who = claimed.get(path)
        if who is None:
            findings.append({"kind": "orphan", "path": path, "owner": owner,
                             "message": "%s 소유인데 아무도 claim 하지 않았다" % owner})
        elif who != owner:
            findings.append({"kind": "violation", "path": path, "owner": owner,
                             "claimed_by": who,
                             "message": "%s 가 claim 했지만 %s 소유다"
                                        % (who, owner)})

    for path, who in claimed.items():
        role = next((r for r in config.get("roles") or [] if r["id"] == who), None)
        if role and not harness.owns_file(role, path):
            if not any(f["path"] == path for f in findings):
                findings.append({"kind": "violation", "path": path,
                                 "claimed_by": who,
                                 "message": "%s 의 소유 경계 밖이다" % who})

    rollback = [{"path": f["path"], "by": f.get("claimed_by") or f.get("owner")}
                for f in findings if f["kind"] == "violation"]
    if not findings:
        return {"ok": True, "findings": [], "rollback": [], "message": ""}
    return {"ok": False, "findings": findings, "rollback": rollback,
            "message": "; ".join("%s: %s" % (f["path"], f["message"])
                                 for f in findings[:5])}


def _changed_paths(root):
    r = harness._git(root, "status", "--porcelain", "-z", "--untracked-files=all")
    if r is None or r.returncode != 0:
        return None
    out, fields = [], [f for f in r.stdout.split("\0") if f]
    i = 0
    while i < len(fields):
        entry = fields[i]
        if len(entry) < 4:
            i += 1
            continue
        code, path = entry[:2], entry[3:]
        out.append(path)
        if code[0] in ("R", "C") and i + 1 < len(fields):
            out.append(fields[i + 1])
            i += 1
        i += 1
    return out
