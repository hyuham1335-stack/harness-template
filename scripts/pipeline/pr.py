# -*- coding: utf-8 -*-
"""`06-pr` 의 본체 — 승인 · push · PR 요청서.

**이 모듈은 forge 를 부르지 않는다.** git 까지가 실행기의 손이고, PR 조회와
생성은 메인 세션이 forge 도구로 한다 — 이 실행기가 `subprocess` 로 부르는
것은 어댑터가 선언한 러너와 `git` 뿐이라는 기존 경계를 06 에서도 지킨다. 명세가 06 의
산출물을 `06_pr_req.json`(**요청서**)으로 둔 것이 이 분업을 이미 가리킨다.

절차는 비용 오름차순이고 첫 실패에서 멈춘다. 종료 코드가 갈리는 자리가
이 페이즈의 설계 전부다:

- **exit 3** — 브랜치가 규약과 안 맞거나 보호 브랜치 위다. **브랜치를 자동
  생성하지 않는다.** 어디에 커밋할지는 사람이 정한다.
- **exit 6** — 승인 뒤에 소스가 바뀌었다. 승인은 지문에 묶여 있고, 지문이
  어긋나면 그 승인은 다른 코드에 대한 것이다 → 재승인.
- **exit 9** — 사람의 판단 대기. 승인 미응답 · 원격 부재(§P3 3지선다).
  **상태를 잠그지 않는다.** stdin 을 붙잡지 않고 정상 종료한 뒤 `--resume`.
- **exit 10** — non-fast-forward. **force-push 가 금지이므로 자동 해결이
  없다** (§E8). 외부 리뷰 스레드와 승인이 깨지기 때문이다.

**계약 파일 삭제는 push 직전이다.** 04 의 귀속과 05 의 대조가 계약을 계속
읽으므로 시점을 여기까지 미룬다 (§E13 — 지우는 것은 계약 파일뿐이고 그것도
06 에서 한 번이다).
"""

import io
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import harness  # noqa: E402
import mask as mask_mod  # noqa: E402
import report as report_mod  # noqa: E402
import review as review_mod  # noqa: E402


# --------------------------------------------------------------------- 브랜치

def check_branch(root, config):
    """반환: (ok, branch, message). 실패는 **exit 3** 이고 자동 생성은 없다."""
    vcs = config.get("vcs") or {}
    r = harness._git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if r is None or r.returncode != 0:
        return False, None, "git 이 현재 브랜치를 답하지 못한다."
    branch = r.stdout.strip()
    if branch in (vcs.get("protected") or []):
        return (False, branch,
                "HEAD 가 보호 브랜치 %r 위에 있다. **브랜치를 자동으로 만들지 "
                "않는다** — 어디에 커밋할지는 사람이 정한다." % branch)
    pattern = vcs.get("branch_pattern")
    if pattern and not re.match(pattern, branch):
        return (False, branch,
                "브랜치 %r 이 규약 %r 과 맞지 않는다." % (branch, pattern))
    return True, branch, branch


# ----------------------------------------------------------------------- 원격

def remote_state(root, config, branch):
    """원격과 브랜치의 상태. 반환: {"has_remote","remote_has_branch","non_ff"}.

    `non_ff` 는 **원격이 로컬에 없는 커밋을 갖고 있는가**다. 그러면 우리의
    push 는 fast-forward 가 아니고, force 는 금지다.
    """
    vcs = config.get("vcs") or {}
    remote = vcs.get("remote") or "origin"
    r = harness._git(root, "remote")
    names = (r.stdout.split() if r is not None and r.returncode == 0 else [])
    if remote not in names:
        return {"has_remote": False, "remote": remote,
                "remote_has_branch": False, "non_ff": False, "behind": 0}

    harness._git(root, "fetch", "-q", remote, branch)
    ref = "refs/remotes/%s/%s" % (remote, branch)
    r = harness._git(root, "rev-parse", "--verify", "-q", ref)
    if r is None or r.returncode != 0:
        # 원격에 이 브랜치가 아직 없다 — 첫 push 다. non-FF 가 아니다.
        return {"has_remote": True, "remote": remote,
                "remote_has_branch": False, "non_ff": False, "behind": 0}

    r = harness._git(root, "rev-list", "--count", "HEAD..%s" % ref)
    behind = 0
    if r is not None and r.returncode == 0 and r.stdout.strip().isdigit():
        behind = int(r.stdout.strip())
    return {"has_remote": True, "remote": remote, "remote_has_branch": True,
            "non_ff": behind > 0, "behind": behind}


def push(root, config, branch):
    """`git push`. **force 를 쓰지 않는다.** 성공을 원격 ref 조회로 확인한다."""
    vcs = config.get("vcs") or {}
    remote = vcs.get("remote") or "origin"
    r = harness._git(root, "push", "-q", "-u", remote, branch)
    ok = r is not None and r.returncode == 0
    detail = "" if r is None else ((r.stderr or "") + (r.stdout or "")).strip()
    if ok:
        # push 가 0 을 냈다고 원격에 있다고 믿지 않는다 — 도구가 불통일 때
        # push 는 이미 됐을 수도, 안 됐을 수도 있다 (§E8).
        v = harness._git(root, "rev-parse", "--verify", "-q",
                         "refs/remotes/%s/%s" % (remote, branch))
        ok = v is not None and v.returncode == 0
    return {"ok": ok, "remote": remote, "branch": branch, "detail": detail}


# ------------------------------------------------------------ 백그라운드 조인

def join_pending(state):
    """04 의 백그라운드 전체 회귀를 06 진입 전에 조인한다.

    `04-gate.md` 가 `join_before: "06-pr"` 를 적어 두고도 06 이 없어 아무 데도
    걸리지 않던 값이다. 이 스택은 지금 `background_full_regression: false` 라
    조인할 것이 없지만, **없다는 것과 안 본다는 것은 다르다** — 안 보면 다른
    스택에서 회귀가 안 끝난 채로 push 한다.
    """
    node = (state.get("phases") or {}).get("04-gate") or {}
    pending = node.get("background")
    if not pending:
        return {"joined": False, "blocked": False,
                "reason": "백그라운드 회귀가 없었다"}
    if pending.get("status") == "passed":
        return {"joined": True, "blocked": False, "reason": "이미 끝나 있었다"}
    return {"joined": False, "blocked": True,
            "reason": "백그라운드 전체 회귀가 아직 끝나지 않았다"}


# --------------------------------------------------------------------- PR 본문

def _read(p, limit=None):
    try:
        t = io.open(p, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return ""
    return t[:limit] if limit else t


def _diff_stat(root, config):
    base = (config.get("vcs") or {}).get("base_branch") or "main"
    r = harness._git(root, "diff", "--shortstat", "%s...HEAD" % base)
    if r is None or r.returncode != 0 or not r.stdout.strip():
        return "diff 통계를 읽지 못했다"
    return r.stdout.strip()


_INTENT = re.compile(r"(?s)<!--\s*INTENT\s*(\{.*?\})\s*-->")


def _inv_block(plan_text):
    """01 의 INV(불변) 목록. 없으면 없다고 적는다 — 지어내지 않는다.

    **INV 는 헤딩이 아니라 `<!-- INTENT {...} -->` JSON 주석 안에 있다**
    (`01-plan.md` 의 산출물 형태). 헤딩만 찾던 예전 구현은 이 리포의 모든
    플랜에서 빈 문자열을 돌려줬고, 본문이 "01 의 INV 블록이 없다" 고
    **없는 결손을 보고**했다 (M42).

    헤딩 폴백을 남긴다 — 다른 스택이 헤딩을 쓸 수 있고, `01-plan.md` 는 짧은
    요청에서 INV 블록을 생략한다고도 적는다. 둘 다 없으면 빈 문자열이고,
    **그때는 "없다" 가 참이다.**
    """
    m = _INTENT.search(plan_text or "")
    if m:
        try:
            inv = (json.loads(m.group(1)) or {}).get("invariants") or []
        except ValueError:
            inv = []
        rows = ["- **%s** `%s` — %s" % (i.get("id"), i.get("kind"),
                                        i.get("text"))
                for i in inv if isinstance(i, dict) and i.get("id")]
        if rows:
            return "\n".join(rows)
    m = re.search(r"(?ms)^#{2,}\s*INV.*?(?=^##\s|\Z)", plan_text or "")
    return m.group(0).strip() if m else ""


def _contract_sections(root, state, config):
    """계약의 유닛·진입점 절. **살아 있는 파일이 없으면 스냅샷을 읽는다** (M54).

    06 push 가 성공하면 계약 파일을 지운다. 07 수리 뒤 재승인하고 `pr` 을 다시
    돌리는 것은 정본이 선언한 정상 경로인데(`team-spec.md` §3.5), 그때 원본이
    없어 이 절이 통째로 비었고 본문이 **자기를 `no_contract` 런이라고 잘못
    보고했다.** 지우기 직전에 남긴 `06_contract_snapshot.md` 가 그 원본이다.

    **살아 있으면 스냅샷을 보지 않는다** — 스냅샷은 삭제 시점에 굳은 값이라
    그 뒤의 계약 델타를 모른다.
    """
    node = state.get("contract") or {}
    text = ""
    for rel in (node.get("path"), node.get("snapshot")):
        if rel:
            text = _read(Path(root) / rel)
        if text:
            break
    if not text:
        return ""
    sections = (config.get("contract") or {}).get("sections") or {}
    out = []
    for head in [sections.get("units"), sections.get("entrypoints")]:
        if not head:
            continue
        m = re.search(r"(?ms)^%s\s*$.*?(?=^##\s|\Z)" % re.escape(head), text)
        if m:
            out.append(m.group(0).strip())
    return "\n\n".join(out)


def _gap_display(gap):
    """gap 코드 뒤에 한글 설명을 병기한다. **모르는 코드는 그대로 보여준다** —

    `report.py` 의 `GAP_REASONS` 를 그대로 쓴다 — 08 보고서와 06 PR 본문이
    같은 gap 코드에 다른 설명을 달면 두 문서가 서로 다른 말을 하게 된다.
    이 절은 08 처럼 "어휘에 없다" 고 적지 않는다 — 완료 등급 옆 한 줄은
    빠르게 훑는 자리라 라벨이 없으면 코드만 남기는 쪽이 더 읽기 좋다.
    """
    head = str(gap).split(":")[0]
    label = report_mod.GAP_REASONS.get(head)
    return "%s (%s)" % (gap, label) if label else gap


def _summary_block(plan_text):
    """01 의 짧은 총평. **감사 대상이 아니다 — 없다고 적지 않는다.**

    INV·계약 절은 없으면 결손이라 "없다" 고 적어야 완료 등급의 근거가
    맞는다. 이 필드는 그런 사실이 아니라 순수 가독성 보조라, 없다고 적으면
    이 필드가 없던 과거 모든 런의 본문에 결손처럼 보이는 줄이 하나 늘어난다.
    """
    m = _INTENT.search(plan_text or "")
    if not m:
        return ""
    try:
        summary = (json.loads(m.group(1)) or {}).get("summary")
    except ValueError:
        return ""
    return summary.strip() if isinstance(summary, str) else ""


def _no_units(state):
    """유닛 절이 비었을 때의 문구. **실패와 데이터 없음을 뭉개지 않는다.**

    계약이 없는 런과 계약이 있는데 지금 못 읽는 것은 다른 사실이다. 뒤엣것을
    `no_contract` 라 적으면 같은 본문의 체크리스트(`state.contract.mode` 를
    본다)와 모순되고, PR 이 스스로를 잘못 보고한다 (M54).
    """
    if (state.get("contract") or {}).get("mode") == "no_contract":
        return "_계약의 유닛·진입점 절이 없다 (no_contract)._"
    return ("_계약의 유닛·진입점 절을 읽지 못했다 — 계약 파일도 스냅샷도 "
            "없다._")


_VERDICT_LABELS = {
    "accept": "반영함",
    "reject": "반영 안 함",
    "modify": "수정해서 반영함",
}


def _verdict_label(v):
    """판정 코드 → 한글 동사. **모르는 코드는 코드 그대로 보여준다** — 표가
    새 어휘를 못 따라가도 정보가 사라지지 않는 안전 폴백이다."""
    return _VERDICT_LABELS.get(v, v or "판정 없음")


def _adopted(paths):
    """02 의 **채택 판정**. `reject` 도 뺴지 않는다.

    `adopted[]` 는 "채택된 것" 이 아니라 판정이고(`02-cross-verify.md` 절차 4),
    거부를 감추면 본문이 거짓말을 한다. 이유도 자르지 않는다 — 자르면 거부
    근거가 사라지고 그것이 이 절의 유일한 값이다.

    원소가 dict 가 아니면 `str` 로 떨어뜨린다 (M41). 스키마가 문자열을 금하지
    않고, 본문 조립 중에 예외를 던지면 06 이 죽는다.
    """
    try:
        d = json.loads(_read(paths.run_dir / "02_verdict.json") or "{}")
    except ValueError:
        return []
    out = []
    for a in d.get("adopted") or []:
        if isinstance(a, dict) and a.get("id"):
            head = "**%s** — %s" % (a.get("id"), _verdict_label(a.get("verdict")))
            reason = (a.get("reason") or "").strip()
            out.append("%s: %s" % (head, reason) if reason else head)
        else:
            out.append(str(a))
    return out


def _minor_open(paths, state):
    """미해결 Minor. **런 전체이지 마지막 라운드가 아니다** (M52).

    원천은 `phases.05-code-review.rounds` 다 — 디스크에 파생 사본을 만들지
    않는다 (M31 · ADR-H022). 예전에는 `05_review.json` 의 `findings` 를 읽었는데
    그것은 **그 라운드의** 병합 결과이고, 델타 라운드는 설계상 한 명이므로 다른
    리뷰어의 열린 Minor 가 **원장에는 남은 채 사람이 읽는 자리에서만** 사라졌다.
    "Minor 는 수리 면제이지 회계 면제가 아니다"(M38)는 보고 표면에서도 성립한다.

    `rounds` 가 아예 없을 때만 옛 경로로 낙하한다 — 리뷰어 0명 경로와 옛 런
    디렉터리다. **빈 목록으로는 낙하하지 않는다**: 전부 닫혀 0건인 것과 원천이
    없는 것은 다르고, 앞을 뒤로 읽으면 이미 닫힌 Minor 가 미해결로 되살아난다.
    """
    node = ((state or {}).get("phases") or {}).get("05-code-review") or {}
    rounds = node.get("rounds")
    if rounds:
        return [f.get("title") or f.get("finding_key") or "제목 없음"
                for f in review_mod.open_findings(rounds)
                if f.get("severity") == "minor"]
    try:
        d = json.loads(_read(paths.run_dir / "05_review.json") or "{}")
    except ValueError:
        return []
    out = []
    for f in d.get("findings") or []:
        if f.get("severity") == "minor":
            out.append(f.get("title") or f.get("finding_key") or "제목 없음")
    return out


REQUEST_QUOTE_LIMIT = 1200


def _quoted_request(path):
    """(인용문, 절단 안내). **줄 경계에서 자르고 잘랐다고 적는다** (M42).

    예전에는 문자 수로 잘라 문장 중간에서 끊었고 **끊었다는 말을 안 했다.**
    상한 자체는 유지한다 — 포지가 본문 크기를 제한하므로 무한정 실을 수
    없다. 경계 절단 + 명시가 상한값과 무관하게 정직하다.
    """
    full = _read(path) or ""
    if len(full) <= REQUEST_QUOTE_LIMIT:
        return full.strip(), ""
    cut = full[:REQUEST_QUOTE_LIMIT]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return (cut.strip(),
            "_(요청 원문 %d자 중 앞 %d자다. 전문은 `%s` 에 있다.)_"
            % (len(full), len(cut.strip()), path.name))


def _check_mark(v):
    """체크리스트 마크. **확인 불가는 체크하지 않는다** (명세 템플릿 매핑표)."""
    return "x" if v else " "


def build_body(root, paths, state, config):
    """PR 본문을 조립한다. **완료 등급이 최상단 한 줄이다.**

    다섯 절의 출처는 명세의 템플릿 매핑표가 정한다. 없는 것은 지어내지 않고
    "없다"고 적는다 — 빈 절과 채운 절이 같아 보이면 본문이 거짓말을 한다.
    """
    grade = state.get("grade") or "미정"
    gaps = state.get("gaps") or []
    head = "**완료 등급: %s**%s" % (
        grade,
        (" (" + ", ".join(_gap_display(g) for g in gaps) + ")" if gaps else ""))

    plan_text = _read(paths.run_dir / "01_plan.md")
    inv = _inv_block(plan_text)
    summary = _summary_block(plan_text)
    request, request_note = _quoted_request(paths.request)
    units = _contract_sections(root, state, config)
    stat = _diff_stat(root, config)
    adopted = _adopted(paths)
    minors = _minor_open(paths, state)
    skipped = [g for g in gaps if g.startswith(("stage_absent:",
                                                "stage_not_touched:",
                                                "infra_skipped:"))]

    tests = state.get("tests") or {}
    review05 = state.get("review05") or {}
    phases = state.get("phases") or {}
    checks = [
        ("게이트 통과", (phases.get("04-gate") or {}).get("status") == "passed"),
        ("전체 회귀 실행됨", bool(tests.get("ran"))),
        ("코드리뷰 수행됨", review05.get("status") == "ok"),
        ("계약 대조됨",
         (state.get("contract") or {}).get("mode") != "no_contract"),
    ]

    lines = [head, ""]
    lines += ["## 개요", ""]
    if summary:
        lines += [summary, ""]
    lines += [inv or "_01 의 INV 블록이 없다._", ""]
    lines += ["**원본 요청**", ""]
    lines += ["> " + request.replace("\n", "\n> ") if request
              else "_요청 원문이 없다._", ""]
    if request_note:
        lines += [request_note, ""]
    lines += ["## 작업 내용", ""]
    if units:
        lines += ["<details><summary>계약 상세 (유닛·진입점)</summary>", ""]
        lines += [units, ""]
        lines += ["</details>", ""]
    else:
        lines += [_no_units(state), ""]
    lines += ["- 변경 규모: %s" % stat, ""]
    lines += ["## 기술적 고려사항", ""]
    lines += (["- %s" % a for a in adopted] if adopted
              else ["_02 교차검증에서 채택된 판정이 없다._"]) + [""]
    lines += ["## 참고사항", ""]
    lines += ["**미해결 Minor**", ""]
    lines += (["- %s" % m for m in minors] if minors else ["- 없다"]) + [""]
    lines += ["**건너뛴 비차단 게이트**", ""]
    lines += (["- %s" % g for g in skipped] if skipped else ["- 없다"]) + [""]
    lines += ["_이슈 자동 종결 링크는 비워 둔다._", ""]
    lines += ["## 체크리스트", ""]
    lines += ["- [%s] %s" % (_check_mark(v), name) for name, v in checks]
    lines += [""]

    # **외부로 나가는 페이로드다.** 원장과 런 디렉터리의 원문은 건드리지 않는다.
    return mask_mod.mask_text(root, "\n".join(lines), config)["text"]


# ------------------------------------------------------------------ PR 요청서

def build_request(state, config, branch, body_rel, remote):
    """메인 세션이 forge 도구로 집행할 요청서.

    `existing_number` 가 있으면 **생성이 아니라 갱신**이다 — 재개했을 때 PR 이
    둘로 갈라지는 것을 막는다 (§8.2 의 "생성 금지, 갱신만").
    """
    vcs = config.get("vcs") or {}
    existing = (state.get("pr") or {}).get("number")
    return {
        "schema": 1,
        "forge": vcs.get("forge") or "github",
        "remote": remote,
        "head": branch,
        "base": vcs.get("base_branch") or "main",
        "title": _title(state),
        "body_file": body_rel,
        "action": "update" if existing else "create",
        "existing_number": existing,
        "grade": state.get("grade"),
        "gaps": state.get("gaps") or [],
        "note": ("실행기는 여기까지다. PR 조회·생성·갱신은 메인 세션이 forge "
                 "도구로 하고, 결과를 `record --phase 06` 으로 되돌려 준다. "
                 "**머지는 이 파이프라인의 범위가 아니다.**"),
    }


def _title(state):
    slug = state.get("slug") or "change"
    return "feat(%s): %s" % (slug, slug)
