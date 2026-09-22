#!/usr/bin/env python3
"""제출물 판정 — 01 리뷰어 제출의 형식과 수렴, 05 가 같이 쓰는 지적 신원.

**순수 함수에 가깝게 쓴다.** 입력은 텍스트와 dict, 출력은 dict다. 파일을 읽지
않는다 — 리뷰어 원문도 경로가 아니라 텍스트로 받는다.

여기가 막는 것은 하나다 — **모델의 자진 신고 중 기계로 확인 가능한 것을
기계로 확인하지 않고 넘어가는 것.** "리뷰했다"는 원문 대조로, "고쳤다"는
(05 에서) 단조성으로 확인한다.
"""

import hashlib
import re

_WS = re.compile(r"\s+")

SEVERITIES = ("critical", "major", "minor")
BLOCKING = ("critical", "major")


def normalize_ws(text):
    """공백만 정규화한다. 그 밖은 건드리지 않는다 — 다듬기와 위조를 구분해야 한다."""
    return _WS.sub(" ", (text or "")).strip()


# ----------------------------------------------------------------- 리뷰 판정

def finding_key(f):
    """같은 지적을 라운드를 가로질러 같은 것으로 센다."""
    raw = "|".join([str(f.get("category") or ""),
                    str(f.get("target_role") or ""),
                    normalize_ws(f.get("title") or "").lower()])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def check_vocabulary(payload):
    """`reviewer` · `severity` 의 닫힌 어휘. 반환: [오류]"""
    errors = []
    reviewer = payload.get("reviewer")
    if not reviewer:
        errors.append("reviewer 가 없다")
    elif reviewer == "main":
        errors.append("reviewer 가 main 이다 — 작성자가 자기 글을 리뷰한 것은 "
                      "독립 관측이 아니다")
    for f in payload.get("findings") or []:
        if f.get("severity") not in SEVERITIES:
            errors.append("%s 의 severity 가 어휘 밖이다: %r"
                          % (f.get("id"), f.get("severity")))
    return errors


def check_review(payload, raw_text, previous_open, blocking=BLOCKING):
    """리뷰어 제출의 판정. 반환: {"ok","exit","errors","keys","closed","blocking"}

    `blocking` 은 **라운드를 강제하는 심각도**다. 05 는 기본값(critical·major)
    이고, 01 은 페이즈 선언 `converge.blocking_severities` 에서 읽어 넘긴다
    (ADR-H041). `previous_open` 이 비어 있으면 단조성 검사는 할 일이 없다 —
    01 이 그렇다(다음 회차에 그 지적이 안 나오면 닫힌 것이다).
    """
    errors = check_vocabulary(payload)

    findings = payload.get("findings") or []
    for f in findings:
        quote = f.get("quote")
        if quote and normalize_ws(quote) not in normalize_ws(raw_text):
            errors.append("%s 의 quote 가 리뷰어 원문에 없다 — 옮겨 적는 쪽이 "
                          "지어냈거나 바꿨다" % f.get("id"))

    # 원문의 심각도 헤딩 개수와 findings 개수가 맞아야 한다. 1라운드 수렴을
    # 허용하는 만큼 이 검사가 더 중요해진다.
    headings = sum(len(re.findall(r"(?mi)^#{1,6}\s*%s\b" % s, raw_text or ""))
                   for s in SEVERITIES)
    if headings != len(findings):
        errors.append("원문의 심각도 헤딩 %d개와 findings %d개가 맞지 않는다"
                      % (headings, len(findings)))

    keys = {finding_key(f) for f in findings}
    resolved = {r.get("id") for r in payload.get("resolved_from_previous") or []}
    resolved_keys = {r.get("key") for r in payload.get("resolved_from_previous") or []}

    # 재제기는 1급 어휘다. 제목이 지적의 신원이라 다듬은 제목으로 다시 올리면
    # 같은 지적이 "신규" 이자 동시에 "증발" 이 되어 한 번의 재제기가 두 곳에서
    # 오탐을 낸다. 리뷰어가 무엇을 다시 올리는지 말할 수 있게 한다.
    prev_by_id = {p["id"]: p for p in previous_open or []}
    reraised, carried = {}, set()
    for f in findings:
        src = f.get("reraised_from_previous")
        if not src:
            continue
        if src not in prev_by_id:
            errors.append(
                "%s 의 reraised_from_previous 가 열려 있는 이전 지적을 "
                "가리키지 않는다: %r — 없는 것을 가리키면 단조성 검사를 "
                "우회하는 구멍이 된다" % (f.get("id"), src))
            continue
        reraised[finding_key(f)] = src
        carried.add(src)

    for prev in previous_open or []:
        if (prev["key"] in keys or prev["key"] in resolved_keys
                or prev["id"] in resolved or prev["id"] in carried):
            continue
        errors.append(
            "이전 회차의 %s 가 이번 findings 에도 resolved_from_previous 에도 "
            "없다 — 지적이 조용히 증발했다" % prev["id"])

    if errors:
        return {"ok": False, "exit": 8, "errors": errors, "keys": [],
                "closed": [], "blocking": 0}
    blocking_n = sum(1 for f in findings if f.get("severity") in blocking)
    closed = sorted({p["key"] for p in previous_open or []
                     if p["id"] in resolved or p["key"] in resolved_keys})
    return {"ok": True, "exit": 0, "errors": [],
            "keys": [{"key": finding_key(f), "id": f.get("id"),
                      "severity": f.get("severity"),
                      "reraised_from": reraised.get(finding_key(f))}
                     for f in findings],
            "closed": closed,
            "blocking": blocking_n}


def converged(submissions, blocking=BLOCKING):
    """(수렴했는가, 사유).

    라운드를 강제하는 것은 **열린 차단 심각도**(`blocking`, 01 은 선언에서
    읽는다)뿐이다 (ADR-H041). 그 아래 심각도는 기록되고 보고서로 가되 라운드를
    강제하지 않는다 — 05 의 "Minor 는 고치지 않는다" 와 같은 형태다. 메인이
    코드 근거로 기각한 지적(`false_positive`)은 제출의 `blocking` 계수에서
    이미 빠져 있다.
    """
    label = "·".join(blocking)
    open_n = sum(s.get("blocking") or 0 for s in submissions)
    if open_n:
        return False, "%s %d건이 열려 있다" % (label, open_n)
    if not submissions:
        # docs 레인 — 리뷰어 0명은 정책 생략이다 (ADR-H044). "0건" 이라고
        # 적으면 관측이 있었던 것처럼 읽힌다.
        return True, "리뷰어 0명 — 정책으로 생략했다"
    return True, "열린 %s 0건" % label
