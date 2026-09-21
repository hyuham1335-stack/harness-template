# -*- coding: utf-8 -*-
"""`mask` — 외부로 나가는 페이로드에서 비밀값을 가린다.

**적용 범위가 규칙의 절반이다.** 마스킹은 리포 밖으로 나가는 것에만 건다 —
PR 본문(06)과 PR 코멘트(07). 원장·내부 보고서·런 디렉터리는 **원문을
보존한다**. 내부 기록까지 가리면 나중에 무엇이 문제였는지 되짚을 수 없다.

가리는 것은 둘이다:

1. `config.project.secret_files[]` 에 **실재하는 값**과의 문자열 일치.
   키 이름은 남기고 값만 `[MASKED]` 로 바꾼다 — `FOO=[MASKED]` 는
   "FOO 가 있었다"를 말하지만 `[MASKED]` 하나는 아무것도 말하지 않는다.
2. 명시 패턴 셋 — 커넥션 문자열의 비밀번호 · 베어러 토큰 · 클라우드 액세스 키.

**하지 않는 것 하나가 이 모듈에서 가장 중요하다.** "32자 난수처럼 보이는
것"을 엔트로피로 판정해 통째로 가리지 않는다. 커밋 sha · 계약 sha256 ·
run_id · 스택트레이스의 주소가 전부 그 모양이고, 그것들이 지워지면 PR 본문이
읽는 사람에게 아무 쓸모가 없어진다. **가릴 대상을 아는 것만 가린다.**

비밀 파일이 없으면 2번만 적용하고 그 사실을 결과에 남긴다 — **경고이지
실패가 아니다** (§8.2 의 06 마지막 행).
"""

import io
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import adapters  # noqa: E402

PLACEHOLDER = "[MASKED]"

# 이보다 짧은 값은 비밀로 보지 않는다. `DEBUG=1` · `NODE_ENV=test` 를 가리면
# 본문이 걸레가 되고, 그런 값은 애초에 비밀이 아니다.
MIN_SECRET_LEN = 8

# 커넥션 문자열 — 비밀번호 자리만 가린다. 호스트와 사용자 이름은 남긴다.
# 그 둘이 지워지면 "어디에 무엇으로 붙으려 했는가"를 알 수 없다.
_CONNECTION = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)"
                         r"(?P<user>[^:/?#\[\]@\s]+):"
                         r"(?P<pw>[^/?#\[\]@\s]+)@")

# 베어러 토큰 — `Bearer` 는 남기고 토큰만 가린다.
_BEARER = re.compile(r"(?i)(?P<kw>\bbearer\s+)(?P<tok>[A-Za-z0-9\-._~+/]{8,}=*)")

# 클라우드 액세스 키 — 접두가 고정된 것만. 접두 없이 길이로 잡으면 위 원칙을
# 어기게 된다.
_CLOUD = re.compile(r"\b(?:AKIA|ASIA|AIza|ghp_|gho_|ghs_|github_pat_)"
                    r"[A-Za-z0-9_\-]{10,}\b")

_PATTERNS = (("connection", _CONNECTION), ("bearer", _BEARER),
             ("cloud_key", _CLOUD))


def secret_values(root, config=None):
    """비밀 파일에서 값만 모은다. 반환: (값 집합, 없는 파일 목록).

    파일 형식은 `KEY=VALUE` 를 가정한다. 파싱에 실패한 줄은 조용히 버리지 않고
    **그 줄 전체를 값 후보로도 쓰지 않는다** — 형식을 모르는 줄에서 값을
    추측하면 엉뚱한 것을 가린다.
    """
    root = Path(root)
    if config is None:
        config, _adapter = adapters.load(root)
    files = list((config.get("project") or {}).get("secret_files") or [])
    values, missing = set(), []
    for rel in files:
        p = root / rel
        if not p.exists():
            missing.append(rel)
            continue
        try:
            text = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            missing.append(rel)
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            _k, _sep, v = line.partition("=")
            v = v.strip().strip("'").strip('"')
            if len(v) >= MIN_SECRET_LEN:
                values.add(v)
    return values, missing


def mask_text(root, text, config=None):
    """반환: {"ok", "text", "hits", "secret_files_missing", "by_source"}.

    `hits` 가 0 이어도 실패가 아니다 — 가릴 것이 없었다는 사실이다.
    """
    values, missing = secret_values(root, config)
    by_source = {"secret_file": 0, "connection": 0, "bearer": 0, "cloud_key": 0}

    # 긴 값부터 바꾼다 — 짧은 값이 긴 값의 부분문자열이면 먼저 잘라 놓고
    # 긴 값이 더는 일치하지 않게 된다.
    for v in sorted(values, key=len, reverse=True):
        if v and v in text:
            by_source["secret_file"] += text.count(v)
            text = text.replace(v, PLACEHOLDER)

    for name, rx in _PATTERNS:
        if name == "connection":
            text, n = rx.subn(lambda m: "%s%s:%s@" % (m.group("scheme"),
                                                      m.group("user"),
                                                      PLACEHOLDER), text)
        elif name == "bearer":
            text, n = rx.subn(lambda m: "%s%s" % (m.group("kw"), PLACEHOLDER),
                              text)
        else:
            text, n = rx.subn(PLACEHOLDER, text)
        by_source[name] += n

    return {"ok": True, "text": text, "hits": sum(by_source.values()),
            "secret_files_missing": missing, "by_source": by_source,
            "note": ("비밀 파일이 없어 패턴만 적용했다 — 경고이지 실패가 아니다."
                     if missing else
                     "비밀 파일의 값과 패턴 셋을 적용했다.")}

