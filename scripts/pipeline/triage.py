# -*- coding: utf-8 -*-
"""`00-triage` 의 판정 — 요청 원문의 **구조 신호**만 읽는다.

**이 모듈이 막는 실패는 하나다** — 문서 한 줄 고치는 요청이 플랜 리뷰어 둘 ·
교차검증 · 역할 둘 · 리뷰어 넷을 다 내는 것. 요청을 파이프라인에 넣기 **전에**
레인(`docs` · `small` · `normal`)을 정하고, 그 값이 01 의 라운드 상한 · 02 의
생략 · 03 의 역할 · 05 의 리뷰어 상한 · 07 의 생략을 결정론적으로 줄인다.

판정은 **예측**이다. 실행기는 요청이 실제로 무엇을 바꿀지 볼 수 없다 — 그것은
03 의 계약(유닛 수)과 05 의 변경 파일이 말한다. 그래서 여기서 정한 값은
`state.profile.source == "triage"` 로 남고, 그 두 자리의 재판정에 **밀린다.**
상향(docs → small · normal)이면 앞 페이즈가 양보를 적용한 채 지나간 것이라
`triage_miss` 가 gap 으로 남는다.

신호는 **언어 비의존**이다. 한국어·영어 키워드 휴리스틱을 쓰지 않는다 —
경로 토큰 · 글자 수 · 개수만 본다. 같은 경로를 어느 언어의 산문에 넣어도
판정이 같아야 하고, 테스트가 그것을 잠근다.

기계 신호로 못 정하면 저가 모델 1회가 같은 어휘로 예측한다. 그 제출에서
기계가 검사할 수 있는 것은 셋뿐이다 — 어휘 · `expected_paths` 가 요청 원문의
부분문자열인가(`source_quote` 와 같은 손잡이) · `docs` 라면 그 경로가 전부
docs glob 안인가. 나머지(`touches_source`)는 자진신고이고 03·05 가 검증한다.
"""

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402
import verdict  # noqa: E402

# 레인 어휘. `unclear` 는 모델 제출에서만 온다 — 기계 판정은 `unclear` 를
# 내지 않고 "미확정" 으로 모델에 넘긴다.
PROFILES = ("docs", "small", "normal")
VOCAB = PROFILES + ("unclear",)

# 상향·하향의 순위. 재판정이 이 순위에서 **올라가면** 앞 페이즈가 양보를 적용한
# 채 지나간 것이라 miss 다. 내려가면 관측을 더 한 것이라 비용만 더 쓴 것이다.
RANK = {"docs": 0, "small": 1, "normal": 2}

# 누가 정했나. 보고서와 원장이 이것으로 "기계가 맞췄나 · 모델이 맞췄나 ·
# 사람이 골랐나" 를 가른다 — 임계값을 고칠 근거가 여기서 나온다.
DECIDED_BY = ("machine", "model", "user")

# 경로 토큰 — ASCII 경로 문자의 최대 연속. 한국어 조사가 공백 없이 붙는
# 경우(`docs/PRD.md를`)를 이 문자 집합이 자연히 자른다.
_PATH_CHARS = re.compile(r"[A-Za-z0-9_./\-*~]+")


def docs_globs(config):
    """docs 레인의 정의 — **docs 리뷰어의 `when` glob** 이 단일 출처다.

    `main_owned_paths` 전체를 docs 로 보지 않는 이유: 이 리포에서 `scripts/**`
    · `.claude/**` 는 main_owned 이지만 하네스 소스다. "docs 레인 = docs
    리뷰어가 볼 것" 으로 정의하면 05 의 라우팅과 같은 술어를 쓰게 된다.
    """
    out = []
    for r in config.get("reviewers") or []:
        if r.get("only_when_no_source_change"):
            out += r.get("when") or []
    return out


def _tokens(text):
    seen, out = set(), []
    for m in _PATH_CHARS.finditer(text or ""):
        tok = m.group(0).strip("./-~*")
        if not tok or tok in seen:
            continue
        # `//host/x` 는 URL 의 꼬리다 — 경로가 아니다.
        if m.group(0).startswith("//"):
            continue
        seen.add(tok)
        out.append(tok)
    return out


def signals(text, config):
    """요청 원문 → 구조 신호. **결정론이고 git 을 부르지 않는다.**

    반환: {"request_chars", "paths_role_owned", "paths_docs",
           "paths_unresolved"}

    분류는 기존 소유 판정(`harness.owns_file` · `harness.glob_any`)으로만
    한다 — 03 의 소유 검사 · 05 의 라우팅과 같은 술어다. 여기서 술어가
    갈라지면 00 이 docs 라 한 것을 05 가 소스라 읽는다.
    """
    roles = config.get("roles") or []
    dglobs = docs_globs(config)
    role_owned, docs, unresolved = [], [], []
    for tok in _tokens(text):
        if any(harness.owns_file(role, tok) for role in roles):
            role_owned.append(tok)
        elif harness.glob_any(dglobs, tok):
            docs.append(tok)
        elif "/" in tok:
            # 경로 모양인데 어느 소유에도 안 걸린다. 03 에서 바뀌면
            # `clean_ownership` 이 orphan 으로 잡는 종류다 — 여기서는
            # 모델에게 넘길 근거로만 쓴다.
            unresolved.append(tok)
        # `/` 도 없고 어느 glob 에도 안 걸리는 토큰(`0.5` · `v1.2`)은 경로가
        # 아니다.
    return {"request_chars": len(text or ""),
            "paths_role_owned": role_owned,
            "paths_docs": docs,
            "paths_unresolved": unresolved}


def decide(sig, config):
    """구조 신호 → 예측. **못 정하면 None** — 모델 또는 normal 낙하는 호출자가.

    순서대로 첫 일치:
    1. 역할 소유 경로가 있다 → 소스를 건드린다. 경로 수 · 글자 수가 임계
       아래면 `small`, 아니면 `normal`
    2. 역할 소유 경로가 없고 docs 경로만 있다(미해결 경로 0) → `docs`
    3. 그 밖 → 미확정
    """
    tri = config.get("triage") or {}
    max_paths = tri.get("small_max_paths")
    min_chars = tri.get("normal_min_chars")
    if not max_paths or not min_chars:
        # 선언이 없으면 폴백이 곧 새 하드코딩이다 (M36). doctor 가 먼저 잡고,
        # 여기까지 오면 exit 2 다.
        raise ValueError("config.triage 에 small_max_paths · normal_min_chars 가 "
                         "없다 — 00 이 읽을 임계값이 없다")
    role_owned = sig.get("paths_role_owned") or []
    docs = sig.get("paths_docs") or []
    unresolved = sig.get("paths_unresolved") or []
    chars = sig.get("request_chars") or 0

    if role_owned:
        small = len(role_owned) <= max_paths and chars < min_chars
        return {"profile": "small" if small else "normal",
                "expected_paths": role_owned + docs,
                "touches_source": True,
                "reasons": ["role_owned_paths=%d" % len(role_owned),
                            "request_chars=%d" % chars,
                            "small" if small else
                            "over_small_max_paths_or_normal_min_chars"],
                "decided_by": "machine"}
    if docs and not unresolved:
        return {"profile": "docs",
                "expected_paths": docs,
                "touches_source": False,
                "reasons": ["all_paths_docs"],
                "decided_by": "machine"}
    return None


def fallback_when_no_model(sig):
    """`model_call_when_undecided: false` 일 때의 낙하 — 보수적으로 normal."""
    return {"profile": "normal", "expected_paths": [],
            "touches_source": None,
            "reasons": ["undecided_no_model"], "decided_by": "machine"}


def check_submission(payload, request_text, config):
    """모델 제출을 검사한다. 반환: 오류 문자열 목록 (비면 통과).

    기계가 확인할 수 있는 것만 본다 — 어휘 · 부분문자열 · docs glob.
    `touches_source` 는 자진신고라 형만 본다.
    """
    errors = []
    if not isinstance(payload, dict):
        return ["제출이 JSON 객체가 아니다"]
    prof = payload.get("profile")
    if prof not in VOCAB:
        errors.append("profile 이 어휘 밖이다: %r (%s)" % (prof, " | ".join(VOCAB)))
    paths = payload.get("expected_paths")
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        errors.append("expected_paths 는 문자열 배열이어야 한다")
        paths = []
    hay = verdict.normalize_ws(request_text or "")
    for p in paths:
        if verdict.normalize_ws(p) not in hay:
            errors.append("expected_paths %r 가 요청 원문에 없다 — 요청에 없는 "
                          "경로를 지어내지 마라" % p)
    if prof == "docs":
        dglobs = docs_globs(config)
        bad = [p for p in paths if not harness.glob_any(dglobs, p)]
        if bad:
            errors.append("docs 로 예측했는데 docs glob(%s) 밖의 경로가 있다: %s"
                          % (", ".join(dglobs) or "(없음)", ", ".join(bad)))
        if not paths:
            errors.append("docs 로 예측하려면 expected_paths 가 최소 하나 있어야 "
                          "한다 — 무엇이 문서인지 기계가 대조할 대상이 없다")
    if "touches_source" in payload and not isinstance(
            payload.get("touches_source"), bool):
        errors.append("touches_source 는 bool 이어야 한다")
    if "reasons" in payload and not isinstance(payload.get("reasons"), list):
        errors.append("reasons 는 배열이어야 한다")
    return errors
