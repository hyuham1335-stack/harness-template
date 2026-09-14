"""8페이즈 feature-pipeline (scripts/pipeline/) 의 테스트.

test_execute.py 의 관용구를 따른다 — pytest · tmp_path · 인스턴스 속성 직접 주입.
test_harness.py 가 unittest 인 것은 더 오래된 층이라 그렇고, 새 파일은 pytest 다.

그룹:
    A  봉투              — stdout 은 항상 단일 JSON 하나
    B  lint-phases       — 페이즈 파일이 깨진 채로 /feature 가 시작하지 않는다
    C  state             — 런 디렉터리 · 지문 · 이벤트 · 카운터
    D  페이즈 파서       — requires 4종 · 플레이스홀더
    E  01 판정           — quote · 커버리지 · 드리프트 · 단조성
    F  clean_ownership   — 소유 경계 · orphan
    G  게이트 · 귀속     — replay 픽스처
    H  adapters          — 스테이지 상태 · 타임아웃 · 선택자
    I  3단계 게이트 잠금 — 고유명사 0건 · 스택/언어 교체 무변경
"""

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS / "pipeline"))
sys.path.insert(0, str(_SCRIPTS))

import harness  # noqa: E402
import state as st  # noqa: E402
import cli  # noqa: E402
import adapters  # noqa: E402
import attribution as attr  # noqa: E402
import contract as contract_mod  # noqa: E402

ROOT = _SCRIPTS.parent


# ---------------------------------------------------------------------------
# 공용 픽스처
# ---------------------------------------------------------------------------

# 실물을 복사한다 — 실물이 바뀌면 이 테스트가 먼저 깨진다 (test_harness.py 와 같은 규율).
COPIED = [
    "harness/config.json",
    "harness/config.schema.json",
    "harness/adapters/adapter.schema.json",
    "harness/adapters/nextjs-ts.json",
    "harness/calibration.json",
    "harness/templates/contract.md",
    # 05 는 기동 전에 리뷰어 스킬의 실재를 확인한다. 실물을 복사해 두므로
    # 스킬 하나를 지우거나 이름을 바꾸면 이 테스트가 먼저 깨진다.
    ".claude/skills/data-layer-reviewer/SKILL.md",
    ".claude/skills/security-reviewer/SKILL.md",
    ".claude/skills/architecture-reviewer/SKILL.md",
    ".claude/skills/test-quality-reviewer/SKILL.md",
    ".claude/skills/docs-reviewer/SKILL.md",
    ".claude/skills/general-reviewer/SKILL.md",
]

# 픽스처의 실측은 **리포 자신의 실측과 별개 사실이다** — 어댑터에서 같은 판단을
# 한 자리가 ADR-H038 결정 2 이고, 이쪽은 캘리브레이션이다.
#
# 템플릿의 `harness/calibration.json` 은 **영구히 미측정**이다(ADR-H039 결정 2 —
# 남의 실측을 상속하지 않는다). 그래서 그것을 복사하면 픽스처는 `derived` 가 전부
# null 인 리포가 되고, 타임아웃·테스트 수 하한을 소비하는 검사들이 *"값이 틀렸다"*
# 가 아니라 *"값이 없다"* 로 깨진다 — **재지 않은 것을 잰 것처럼 쓰는 것을 막느라
# 잰 것을 쓰는 테스트까지 막은 것**이다.
#
# 픽스처가 자기 실측을 선언한다. 아래 숫자는 Next.js 모양 리포에서 나올 법한
# 값이고, **어느 실물의 측정도 아니다** — 이 검사들이 묻는 것은 값이 얼마인가가
# 아니라 그 값이 소비자까지 도달하는가다.
FIXTURE_CALIBRATION = {
    "measured_at": "2026-01-01T00:00:00+0900",
    "adapter": "nextjs-ts",
    "adapter_verified": False,
    "partial": False,
    "stages": {
        "compile": {"sec": 5.8, "ok": True, "exit_code": 0, "state": "measured"},
        "lint": {"sec": 40.58, "ok": True, "exit_code": 0, "state": "measured"},
        "check": {"sec": 8.06, "ok": True, "exit_code": 0, "state": "measured"},
        "scoped": {"sec": 4.33, "ok": True, "exit_code": 0, "state": "measured"},
        "full": {"sec": 35.36, "ok": True, "exit_code": 0, "state": "measured",
                 "tests_ran": 1403, "suites": 47, "failures": 0},
        "e2e": {"sec": None, "skipped": True, "state": "absent",
                "reason": "cmd:null — 이 스택에 없는 스테이지"},
        "build": {"sec": 20.66, "ok": True, "exit_code": 0, "state": "measured"},
        "docs": {"sec": None, "skipped": True, "state": "absent",
                 "reason": "cmd:null — 이 스택에 없는 스테이지"},
    },
    "retry": {"steps_recorded": 29, "steps_unrecorded": 20,
              "steps_retried": 0, "max_attempts_observed": 1},
    "report_glob_matched": True,
    "infra": {},
    "derived": {
        "background_threshold_sec": 180,
        "background_full_regression": False,
        "full_timeout_sec": 300,
        "tests_ran_floor": 1262,
        "retry_budget": 2,
    },
}


def _git(root, *args):
    return subprocess.run(["git"] + list(args), cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """실물 설정을 복사한 빈 git 리포. 실물 _workspace/ 를 건드리지 않는다."""
    for rel in COPIED:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")

    # 픽스처는 **Next.js 모양의 리포**다 (아래 src/lib/match.ts · src/services/**).
    # 이 리포 자신은 파이썬이라 실물 config 의 adapter 가 self-python 이고, 둘은
    # 다른 사실이다. 실물을 복사하는 값(스키마·역할·리뷰어 라우팅이 실물과 같이
    # 움직인다)은 지키되 어댑터만 픽스처의 스택으로 되돌린다 (ADR-H038).
    cfg_path = tmp_path / "harness/config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["adapter"] = "nextjs-ts"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + chr(10),
                        encoding="utf-8")

    # 캘리브레이션도 같다 — 실물은 영구히 미측정이고 픽스처는 잰 것이 있어야 한다.
    # 위 FIXTURE_CALIBRATION 주석을 본다 (ADR-H039 결정 2).
    (tmp_path / "harness" / "calibration.json").write_text(
        json.dumps(FIXTURE_CALIBRATION, ensure_ascii=False, indent=2) + chr(10),
        encoding="utf-8")

    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "lib" / "match.ts").write_text(
        "export function matchTitle(a: string, b: string): number { return 0 }\n",
        encoding="utf-8")
    (tmp_path / "src" / "lib" / "match.test.ts").write_text(
        "import { matchTitle } from './match'\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# 가드레일\n", encoding="utf-8")

    # 실물과 같게 _workspace/ 를 무시한다 — 계약 파일이 추적되는 orphan 이 되면
    # clean_ownership 이 잡는다.
    (tmp_path / ".gitignore").write_text("_workspace/\n", encoding="utf-8")

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


@pytest.fixture
def request_file(repo):
    """실물 /feature 흐름과 같은 자리에 둔다 — _workspace/ 는 추적되지 않는다."""
    p = repo / "_workspace" / "requests" / "req.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("책 제목 유사도를 재는 함수를 만들어 줘 — 한글 포함\n", encoding="utf-8")
    return p


def _run_cli(root, *args):
    """실물 CLI 를 서브프로세스로 부른다 — 종료 코드와 stdout 오염을 함께 본다."""
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "pipeline" / "cli.py"), *args],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8")


# ---------------------------------------------------------------------------
# A. 봉투 — stdout 은 항상 단일 JSON 하나
# ---------------------------------------------------------------------------

class TestEnvelope:
    """모델이 읽는 것은 render 와 next_command 둘뿐이다.

    그러려면 stdout 이 파싱 가능한 JSON 하나여야 한다. 진단 한 줄이 섞이면
    모델이 그 줄을 지시로 읽거나 파싱에 실패한다.
    """

    def test_envelope_has_every_required_key(self):
        env = st.envelope("next", ok=True, exit_=0, state=None,
                          data={}, render="...", next_command=None)
        for key in ("schema", "ok", "cmd", "exit", "run_id", "phase",
                    "state_summary", "data", "render", "next_command"):
            assert key in env, key

    def test_ok_agrees_with_exit_zero(self):
        assert st.envelope("next", True, 0, None, {}, "", None)["ok"] is True
        assert st.envelope("next", False, 4, None, {}, "", None)["ok"] is False

    def test_emit_returns_the_exit_code(self, capsys):
        code = st.emit(st.envelope("gate", False, 4, None, {}, "r", "c"))
        assert code == 4
        assert json.loads(capsys.readouterr().out)["exit"] == 4

    def test_emit_writes_exactly_one_json_object(self, capsys):
        st.emit(st.envelope("status", True, 0, None, {"a": 1}, "r", None))
        out = capsys.readouterr().out
        assert out.endswith("\n") and out.count("\n") == 1
        json.loads(out)          # 파싱되면 단일 객체다

    def test_non_ascii_survives(self, capsys):
        st.emit(st.envelope("next", True, 0, None, {}, "## 계약을 쓴다", None))
        out = capsys.readouterr().out
        assert "계약" in out, "ensure_ascii=False 여야 한다 — 이스케이프되면 render 가 안 읽힌다"
        assert json.loads(out)["render"] == "## 계약을 쓴다"

    def test_state_summary_carries_counters(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        st.counter_inc(s, "repair", 3, "gate_failure")
        env = st.envelope("gate", False, 4, s, {}, "", None)
        got = env["state_summary"]["counters"]["repair"]
        assert (got["used"], got["max"]) == (1, 3), got
        # 소모 사유가 상태에 함께 있다 (M47) — 봉투가 그것을 지우지 않는다.
        assert [x["reason"] for x in got["spent"]] == ["gate_failure"], got
        assert env["state_summary"]["escalated"] is False
        assert env["run_id"] == s["run_id"]

    def test_cli_doctor_emits_a_parsable_envelope(self, repo):
        out = _run_cli(repo, "doctor")
        env = json.loads(out.stdout)
        assert env["cmd"] == "doctor"
        assert env["exit"] == out.returncode

    def test_cli_keeps_diagnostics_off_stdout(self, repo):
        """stdout 을 통째로 파싱할 수 있어야 한다. 진단은 stderr 로 간다."""
        out = _run_cli(repo, "status")
        json.loads(out.stdout)


# ---------------------------------------------------------------------------
# C. state — 런 디렉터리 · 지문 · 이벤트 · 카운터
# ---------------------------------------------------------------------------

class TestCreateRun:

    def test_request_is_frozen_byte_for_byte(self, repo, request_file):
        paths, s = st.create_run(repo, "demo", request_file)
        copied = (paths.run_dir / "00_original_request.md").read_bytes()
        assert copied == request_file.read_bytes(), "개행·인코딩 변환 없이 그대로"
        assert s["request"]["bytes"] == len(copied)
        assert s["request"]["sha256"] == hashlib.sha256(copied).hexdigest()

    def test_run_id_shape_and_length(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        rid = s["run_id"]
        assert len(rid) == 18, "경로 240자 상한 때문에 짧게 유지한다"
        date, time_, tail = rid.split("-")
        assert len(date) == 8 and len(time_) == 4 and len(tail) == 4

    def test_run_dir_lives_under_workspace(self, repo, request_file):
        paths, _ = st.create_run(repo, "demo", request_file)
        assert paths.run_dir.parent == repo / "_workspace" / "runs"

    def test_vcs_is_untouched(self, repo, request_file):
        before_head = _git(repo, "rev-parse", "HEAD").stdout
        before_branch = _git(repo, "branch", "--show-current").stdout
        st.create_run(repo, "demo", request_file)
        assert _git(repo, "rev-parse", "HEAD").stdout == before_head
        assert _git(repo, "branch", "--show-current").stdout == before_branch

    def test_vcs_baseline_is_recorded(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        assert s["vcs"]["baseline"]["head"]
        assert s["vcs"]["baseline"]["dirty"] is False

    def test_load_finds_runs_by_id_and_latest(self, repo, request_file):
        _, first = st.create_run(repo, "a", request_file, seed_bytes=b"1")
        _, second = st.create_run(repo, "b", request_file, seed_bytes=b"2")
        _, loaded = st.load(repo)
        assert loaded["run_id"] in (first["run_id"], second["run_id"])
        _, by_id = st.load(repo, first["run_id"])
        assert by_id["slug"] == "a"

    def test_save_load_roundtrip_keeps_hangul(self, repo, request_file):
        paths, s = st.create_run(repo, "demo", request_file)
        s["gaps"] = ["스테이지 없음"]
        st.save(paths, s)
        assert "스테이지" in paths.state.read_text(encoding="utf-8")
        _, again = st.load(repo, s["run_id"])
        assert again["gaps"] == ["스테이지 없음"]

    def test_future_phase_keys_are_not_pre_created(self, repo, request_file):
        """05~08 의 키를 null 로 파 두면 '안 돌렸다'와 '0 이었다'가 같은 칸에 든다."""
        _, s = st.create_run(repo, "demo", request_file)
        for key in ("review05", "precheck", "approval", "pr", "review07", "tests"):
            assert key not in s, key


class TestPhaseStatus:

    def test_absent_key_means_never_entered(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        assert st.phase_status(s, "03-implement") is None, \
            "pending 을 만들지 않는다 — 키 부재가 그것이다"

    def test_unknown_status_is_rejected(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        with pytest.raises(ValueError):
            st.set_phase_status(s, "01-plan", "submitted")

    def test_status_transitions_record_a_timestamp(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        st.set_phase_status(s, "01-plan", "running")
        st.set_phase_status(s, "01-plan", "passed", rounds=2)
        assert st.phase_status(s, "01-plan") == "passed"
        assert s["phases"]["01-plan"]["rounds"] == 2
        assert "at" in s["phases"]["01-plan"]


class TestEvents:

    def test_kind_vocabulary_is_closed(self, repo, request_file):
        """budget.model_calls 가 이벤트 수에서 유도되므로 어휘가 열리면 정의가 흔들린다."""
        paths, _ = st.create_run(repo, "demo", request_file)
        with pytest.raises(ValueError):
            st.append_event(paths, "made_up_kind", cmd="next", phase="01-plan")

    def test_seq_increments_and_lines_parse(self, repo, request_file):
        paths, _ = st.create_run(repo, "demo", request_file)
        st.append_event(paths, "phase_enter", cmd="next", phase="01-plan")
        st.append_event(paths, "phase_pass", cmd="record", phase="01-plan")
        lines = [json.loads(x) for x in
                 paths.events.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert [e["seq"] for e in lines] == list(range(1, len(lines) + 1))
        assert lines[-1]["kind"] == "phase_pass"


class TestPhaseDurations:
    """8페이즈가 자기 소요를 잰다 — 새 계측이 아니라 `events.jsonl` 의 유도값이다.

    `report.py` 가 여섯 런에 걸쳐 "소요 시간은 미측정이다" 를 적었는데,
    `team-spec.md` 의 08 결정론 칸은 페이즈별 소요를 **이미 요구한다.**
    M56 과 같은 모양이다 — 선언이 있는데 코드가 안 하는 자리다.

    **기준은 `phase_enter` → `phase_pass` 짝이 아니라 이벤트 구간 분할이다.**
    P8 실측이 그 이유다: 02 의 Critical 이 01 로 되돌렸을 때 되돌아간 01 에
    `phase_enter` 가 안 찍혔고, 짝 맞추기는 그 3시간 26분을 **02 의 소요로**
    적는다. `08-report` 는 `phase_pass` 만 있어 짝 맞추기로는 영영 못 잰다.
    """

    def _at(self, h, m=0, s=0):
        return datetime(2026, 3, 1, h, m, s, tzinfo=st.TZ)

    def test_구간_합이_런_벽시계와_같다(self, repo, request_file):
        """불변식. 깨지면 어딘가를 이중계상했거나 흘렸다는 뜻이다."""
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "run_created", phase="01-plan", now=self._at(10))
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(10, 30))
        st.append_event(paths, "phase_enter", phase="02-cross-verify",
                        now=self._at(10, 30))
        st.append_event(paths, "run_closed", phase="02-cross-verify",
                        now=self._at(11))

        t = st.phase_durations(paths)
        assert t["wall_sec"] == 3600
        assert sum(p["wall_sec"] for p in t["phases"].values()) == t["wall_sec"]

    def test_재진입한_페이즈의_두_구간이_합산된다(self, repo, request_file):
        """01 → 02 → 01 → pass. **짝 맞추기 기준이면 여기서 깨진다.**"""
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="01-plan", now=self._at(10))
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(10, 10))
        st.append_event(paths, "phase_enter", phase="02-cross-verify",
                        now=self._at(10, 10))
        # 02 가 되돌린다. 되돌아간 01 에 phase_enter 가 안 찍히는 것이 실물이다.
        st.append_event(paths, "submit_received", phase="01-plan",
                        now=self._at(10, 20))
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(10, 50))

        t = st.phase_durations(paths)
        assert t["phases"]["01-plan"]["wall_sec"] == 600 + 1800
        assert t["phases"]["01-plan"]["segments"] == 2
        assert t["phases"]["02-cross-verify"]["wall_sec"] == 600

    def test_phase_enter_없이_pass_만_있는_페이즈도_잰다(self, repo, request_file):
        """`08-report` 의 실물 형태다 — P8 은 seq 107 이 pass 뿐이다."""
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_pass", phase="07-pr-review", now=self._at(10))
        st.append_event(paths, "phase_pass", phase="08-report", now=self._at(10, 5))
        st.append_event(paths, "run_closed", phase="08-report", now=self._at(10, 5))

        t = st.phase_durations(paths)
        assert "08-report" in t["phases"]
        assert t["phases"]["08-report"]["wall_sec"] == 0

    def test_phase_가_없는_이벤트는_직전_페이즈를_잇는다(self, repo, request_file):
        """`counter_inc` 은 phase 를 안 받는다 — P8 에서 11건이다.

        새 구간을 열면 그 시간이 어느 페이즈에도 안 들어가 벽시계가 샌다.
        """
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="01-plan", now=self._at(10))
        st.append_event(paths, "counter_inc", now=self._at(10, 20))
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(10, 40))

        t = st.phase_durations(paths)
        assert list(t["phases"]) == ["01-plan"]
        assert t["phases"]["01-plan"]["wall_sec"] == 2400

    def test_에스컬레이션_대기가_페이즈별로_따로_나온다(self, repo, request_file):
        """벽시계에서 **사람을 기다린 시간**을 뺄 수 있어야 한다.

        P8 은 벽시계 7:48:14 중 4:42:04(60.2%)가 이것이었고, 다섯 건이
        전부 01-plan 이었다. 총계 한 줄로는 그 사실이 안 보인다.
        """
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="01-plan", now=self._at(10))
        st.append_event(paths, "escalated", phase="01-plan", now=self._at(10, 10))
        st.append_event(paths, "resumed", phase="01-plan", now=self._at(11, 10))
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(11, 20))

        t = st.phase_durations(paths)
        assert t["phases"]["01-plan"]["wall_sec"] == 4800
        assert t["phases"]["01-plan"]["escalation_wait_sec"] == 3600
        assert t["phases"]["01-plan"]["escalations"] == 1
        assert t["escalation_wait_sec"] == 3600

    def test_재개되지_않은_에스컬레이션은_대기_키를_만들지_않는다(
            self, repo, request_file):
        """값을 지어내지 않는다. 아직 안 끝난 대기는 길이가 없다 (ADR-H007)."""
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="01-plan", now=self._at(10))
        st.append_event(paths, "escalated", phase="01-plan", now=self._at(10, 10))

        t = st.phase_durations(paths)
        assert "escalation_wait_sec" not in t
        assert "escalation_wait_sec" not in t["phases"]["01-plan"]
        assert t["unresumed_escalations"] == 1

    def test_이벤트가_한_줄이면_소요를_주장하지_않는다(self, repo, request_file):
        """구간이 없으면 잰 것이 없다 — 0 으로 채우면 '안 쟀다'와 같아진다."""
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "run_created", phase="01-plan", now=self._at(10))
        assert st.phase_durations(paths) == {}

    def test_깨진_줄이_나머지를_버리지_않는다(self, repo, request_file):
        """`_read_session_metrics` 와 같은 규율이다."""
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="01-plan", now=self._at(10))
        with paths.events.open("a", encoding="utf-8", newline="") as fh:
            fh.write("{ broken line\n")
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(10, 30))

        t = st.phase_durations(paths)
        assert t["phases"]["01-plan"]["wall_sec"] == 1800

    def test_기준과_사각을_함께_돌려준다(self, repo, request_file):
        """`BUDGET_BASIS`·`BUDGET_BLIND_SPOTS` 와 같은 자리다 — 값만 주고
        그 값이 어느 방향으로 틀리는지 안 주면 사람이 읽을 수 없다."""
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="01-plan", now=self._at(10))
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(10, 1))

        t = st.phase_durations(paths)
        assert t["basis"] == st.PHASE_DURATION_BASIS
        assert t["blind_spots"] == list(st.PHASE_DURATION_BLIND_SPOTS)

    @pytest.mark.skipif(
        not (ROOT / "_workspace/runs/20260908-1720-dca1/events.jsonl").exists(),
        reason="P8 런 디렉터리가 없다")
    def test_P8_실물_events_가_같은_값을_낸다(self):
        """실물 앵커. 합성 픽스처만으로는 기준이 실물에서 성립하는지 모른다.

        P8 은 `phase_enter` 16 · `phase_pass` 9 이고 되돌아간 01 에 진입
        이벤트가 없다 — 이 런이 기준을 고른 근거 자체다.
        """
        t = st.phase_durations(st.RunPaths(ROOT, "20260908-1720-dca1"))
        assert t["wall_sec"] == 28094
        assert t["phases"]["01-plan"]["wall_sec"] == 20977
        assert t["phases"]["01-plan"]["escalation_wait_sec"] == 16924
        assert t["escalation_wait_sec"] == 16924
        assert sum(p["wall_sec"] for p in t["phases"].values()) == 28094
        assert sum(p["entries"] for p in t["phases"].values()) == 16
        assert sum(p["passes"] for p in t["phases"].values()) == 9


class TestFingerprint:
    """게이트 통과 후 소스가 바뀌면 영수증이 stale 이어야 한다."""

    def test_same_content_same_value(self, repo):
        config = harness._read_json(repo / "harness/config.json")
        assert st.fingerprint(repo, config)["value"] == \
            st.fingerprint(repo, config)["value"]

    def test_edit_changes_the_value(self, repo):
        config = harness._read_json(repo / "harness/config.json")
        before = st.fingerprint(repo, config)
        (repo / "src" / "lib" / "match.ts").write_text("// 바뀜\n", encoding="utf-8")
        assert st.fingerprint(repo, config)["value"] != before["value"]

    def test_revert_restores_the_value(self, repo):
        """mtime 이 아니라 내용을 해시한다 — 되돌리면 같은 지문이어야 한다."""
        config = harness._read_json(repo / "harness/config.json")
        target = repo / "src" / "lib" / "match.ts"
        original = target.read_text(encoding="utf-8")
        before = st.fingerprint(repo, config)
        target.write_text("// 바뀜\n", encoding="utf-8")
        target.write_text(original, encoding="utf-8")
        assert st.fingerprint(repo, config)["value"] == before["value"]

    def test_change_outside_role_scope_is_ignored(self, repo):
        """소유 범위 밖(문서 등)의 변경은 게이트 영수증을 무효로 만들지 않는다."""
        config = harness._read_json(repo / "harness/config.json")
        before = st.fingerprint(repo, config)
        (repo / "CLAUDE.md").write_text("# 가드레일\n추가 줄\n", encoding="utf-8")
        assert st.fingerprint(repo, config)["value"] == before["value"]

    def test_different_algo_never_matches(self):
        a = {"algo": "tree-sha256", "value": "x"}
        b = {"algo": "walk-sha256", "value": "x"}
        assert st.fingerprint_matches(a, dict(a)) is True
        assert st.fingerprint_matches(a, b) is False, \
            "다른 방법으로 잰 값이 우연히 같아 '안 바뀌었다'가 되면 안 된다"

    # ── M25. 지문은 커밋이 아니라 내용에 매달린다.

    def test_커밋해도_지문이_같다(self, repo):
        """**M25 의 본체다.** 06 은 PR diff 를 위해 커밋을 요구한다. HEAD 를
        해시에 넣으면 그 커밋이 04 영수증을 반드시 낡게 만든다 — 바이트가
        하나도 안 바뀌었는데도."""
        config = harness._read_json(repo / "harness/config.json")
        (repo / "src" / "lib" / "match.ts").write_text("// 고침\n", encoding="utf-8")
        before = st.fingerprint(repo, config)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "06 이 요구하는 커밋")
        after = st.fingerprint(repo, config)
        assert after["value"] == before["value"]
        assert st.fingerprint_matches(before, after) is True

    def test_커밋한_뒤_고치면_지문이_달라진다(self, repo):
        """커밋 중립이 permissive 가 되면 안 된다 — 내용이 바뀌면 여전히 stale."""
        config = harness._read_json(repo / "harness/config.json")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "빈 커밋")
        before = st.fingerprint(repo, config)
        (repo / "src" / "lib" / "match.ts").write_text("// 한 글자\n", encoding="utf-8")
        assert st.fingerprint_matches(before, st.fingerprint(repo, config)) is False

    def test_추적되지_않은_소유_파일이_지문에_들어간다(self, repo):
        """03 이 새로 쓴 파일은 아직 git add 전이다. 놓치면 게이트가 보지
        않은 코드가 영수증을 통과한다."""
        config = harness._read_json(repo / "harness/config.json")
        before = st.fingerprint(repo, config)
        (repo / "src" / "lib" / "new.ts").write_text("export const x = 1\n",
                                                     encoding="utf-8")
        assert st.fingerprint(repo, config)["value"] != before["value"]

    def test_무시된_파일은_지문에_들어가지_않는다(self, repo):
        """`_workspace/` 는 커맨드마다 커진다 — 들어가면 지문이 매번 바뀐다."""
        config = harness._read_json(repo / "harness/config.json")
        before = st.fingerprint(repo, config)
        d = repo / "_workspace" / "runs" / "x"
        d.mkdir(parents=True, exist_ok=True)
        (d / "events.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
        assert st.fingerprint(repo, config)["value"] == before["value"]

    def test_삭제도_지문을_바꾸고_커밋_전후가_같다(self, repo):
        """삭제는 변경이다. 그리고 그 삭제를 커밋해도 값은 그대로여야 한다 —
        아니면 M25 가 삭제라는 형태로 되살아난다."""
        config = harness._read_json(repo / "harness/config.json")
        before = st.fingerprint(repo, config)
        (repo / "src" / "lib" / "match.ts").unlink()
        uncommitted = st.fingerprint(repo, config)
        assert uncommitted["value"] != before["value"]
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "삭제를 커밋")
        assert st.fingerprint(repo, config)["value"] == uncommitted["value"]

    def test_소유_범위_밖의_커밋은_지문을_바꾸지_않는다(self, repo):
        """`test_change_outside_role_scope_is_ignored` 의 커밋 판이다.
        워크트리 편집은 무시하면서 그 편집을 커밋하면 무효가 되던 것이 M25."""
        config = harness._read_json(repo / "harness/config.json")
        before = st.fingerprint(repo, config)
        (repo / "CLAUDE.md").write_text("# 가드레일\n추가 줄\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "문서만 고침")
        assert st.fingerprint(repo, config)["value"] == before["value"]

    def test_옛_지문은_보수적으로_stale_이다(self, repo):
        """`git-sha256` 로 잰 P2 시절 값은 algo 가 달라 영원히 안 맞는다.
        그래서 P2 는 `advance` 가 아니라 `report` 로만 닫힌다."""
        config = harness._read_json(repo / "harness/config.json")
        fresh = st.fingerprint(repo, config)
        old = dict(fresh, algo="git-sha256")
        assert st.fingerprint_matches(old, fresh) is False

    def test_file_count_는_해시한_파일_수다(self, repo):
        """뜻이 바뀌었다 — 예전에는 HEAD 줄을 포함한 입력 줄 수였다."""
        config = harness._read_json(repo / "harness/config.json")
        fp = st.fingerprint(repo, config)
        assert fp["file_count"] == 2, "match.ts 와 match.test.ts 둘"


class TestCounters:

    def test_inc_reports_exceeded_at_the_limit(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        assert st.counter_inc(s, "repair", 2, "gate_failure") == (1, 2, False)
        assert st.counter_inc(s, "repair", 2, "gate_failure") == (2, 2, True)

    def test_지급한_상한을_다음_소모가_지우지_않는다(self, repo, request_file):
        """M56 — `counter_grant` 가 올린 상한을 `counter_inc` 한 번이 되돌렸다.

        `20260908-1720-dca1` 의 `counters.round` 는 `used 9 / max 5` 인데
        `grants[0].extra` 가 5 다. 실효 상한 10 인 예산에서 라운드 7·8·9 가
        `used >= max` 로 잘못 에스컬레이션했고 **사람이 답변 셋을 손으로 써서
        그 대역을 했다** — [[ADR-H024]] 가 만든 지급 경로가 실질적으로 없었다.

        기존 지급 테스트 다섯(`TestRoundBudgetAfterRoundTrip`)은 전부 지급
        **직후** 상태만 봐서 이 결함을 초록불로 통과시켰다.
        """
        _, s = st.create_run(repo, "demo", request_file)
        assert st.counter_inc(s, "repair", 2, "gate_failure") == (1, 2, False)
        assert st.counter_grant(s, "repair", 2, "왕복이 설계를 뒤집었다") == (1, 4)
        # 되돌리면 여기가 `(2, 2, True)` 다 — 상한도 판정도 선언값으로 돌아간다.
        assert st.counter_inc(s, "repair", 2, "gate_failure") == (2, 4, False)
        assert s["counters"]["repair"]["max"] == 4, s["counters"]["repair"]

    def test_지급받은_예산도_결국_소진된다(self, repo, request_file):
        """지급은 상한을 올릴 뿐 무한 연장이 아니다 (ADR-H024 의 트레이드오프).

        M56 을 고치면서 `exceeded` 를 영영 False 로 만들면 상한이 사라진다.
        """
        _, s = st.create_run(repo, "demo", request_file)
        st.counter_inc(s, "repair", 1, "gate_failure")
        st.counter_grant(s, "repair", 1, "왕복 지급")
        assert st.counter_inc(s, "repair", 1, "gate_failure") == (2, 2, True)

    def test_두_번_지급해도_한_번씩만_더해진다(self, repo, request_file):
        """실효 상한은 **선언값 + `grants` 합**이고 재계산은 멱등이다.

        `counter_grant` 도 `node["max"]` 를 직접 올리므로, 재계산이 그것과
        어긋나면 지급~다음 소모 사이 구간에서 봉투와 보고서가 다른 값을 말한다.
        실물에서 두 번 지급은 `loop.max` 를 올린 변이 테스트에서만 난다
        (`xverify_return` 상한 1 이 런당 한 번으로 묶는다 · M32).
        """
        _, s = st.create_run(repo, "demo", request_file)
        st.counter_inc(s, "repair", 2, "gate_failure")
        st.counter_grant(s, "repair", 2, "1차 지급")
        st.counter_inc(s, "repair", 2, "gate_failure")
        st.counter_grant(s, "repair", 2, "2차 지급")
        assert st.counter_inc(s, "repair", 2, "gate_failure") == (3, 6, False)

    def test_소모_전_첫_지급이_상한을_두_배로_만들지_않는다(self, repo, request_file):
        """M58 — `counter_grant` 가 카운터 노드를 만들 때 `extra` 를 두 번 센다.

        `setdefault(name, {"used": 0, "max": extra})` 로 노드를 만든 **직후**
        `node["max"] = (node.get("max") or 0) + extra` 를 하므로, 소모가 한 번도
        없던 카운터의 첫 지급이 상한을 `2 * extra` 로 만든다.

        **오늘 실물 경로로는 도달하지 않는다** — 지급은 `_grant_rounds` 한 곳에서
        `round` 에만 일어나고 01 이 그 전에 반드시 라운드를 소모해 노드가 이미
        있다. 재현이 라이브러리 직접 호출뿐이라는 것이 이 결함이 여섯 런을 조용히
        지나온 이유다. **드러나지 않는다는 것이 안전하다는 뜻은 아니다** (M11 이
        다섯 런 동안 같은 자리를 지났다).

        `max` 는 `grants` 의 파생값이므로(`counter_grant` docstring) 소모가 없는
        상태의 상한은 **지급 합계**여야 한다.
        """
        _, s = st.create_run(repo, "demo", request_file)
        assert "repair" not in (s.get("counters") or {})
        # 되돌리면 `(0, 10)` 이다 — 초기값 `extra` 에 `extra` 를 또 더한다.
        assert st.counter_grant(s, "repair", 5, "소모 전 지급") == (0, 5)
        assert s["counters"]["repair"]["max"] == 5, s["counters"]["repair"]

    def test_소모_전_지급도_선언값_위에서_다시_계산된다(self, repo, request_file):
        """M58 수정이 M56 의 재계산을 되돌리지 않는지 잠근다.

        지급 직후의 `max` 는 지급 합계뿐이고 **선언값을 모른다** — 선언값은
        `counter_inc` 이 인자로 받는다. 그러므로 그 첫 소모가 내는 실효 상한은
        `선언값 + 지급 합` 이어야 한다. 초기값을 0 으로 내린 것이 이 경로를
        깨뜨리면 여기가 먼저 빨간불이 된다.
        """
        _, s = st.create_run(repo, "demo", request_file)
        st.counter_grant(s, "repair", 3, "소모 전 지급")
        assert st.counter_inc(s, "repair", 2, "gate_failure") == (1, 5, False)


class TestCounterSpendReason:
    """예산을 **무엇에 썼는지**가 원장에 남는가 (M47).

    `counter_inc` 이벤트 어휘는 `state.EVENT_KINDS` 에 처음부터 있었고, 바로
    아래 주석이 그 취지를 적는다 — "뭉치면 원장에서 다섯 라운드를 쓴 런과 세
    라운드를 쓰고 둘을 더 받은 런이 같아 보인다". **그런데 일곱 호출처 중
    `retry` 한 곳만 이벤트를 냈다.**

    P6 의 `events.jsonl` 에 `counter_inc` 가 **0건**인데 `review_repair` 는
    3/2 였다. 그 셋이 형식 반려로 탄 것인지 수리 실패로 탄 것인지 원장에서
    갈리지 않는다 — 실제로는 M46 의 교착 때문에 **수리를 한 번도 시도하기
    전에** 05 에스컬레이션에 닿았다.

    예산 자체는 가르지 않는다. 상한을 새로 정하려면 실측이 있어야 하고
    아직 없다 — 재지 않은 상수를 상속하지 않는다.
    """

    def test_사유_없이는_예산을_못_쓴다(self, repo, request_file):
        """폴백을 두지 않는다 — 폴백이 곧 새 하드코딩이다 ([[ADR-H025]])."""
        _, s = st.create_run(repo, "demo", request_file)
        with pytest.raises(ValueError):
            st.counter_inc(s, "repair", 2, None)

    def test_어휘_밖_사유는_거부된다(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        with pytest.raises(ValueError):
            st.counter_inc(s, "repair", 2, "그때그때 지어낸 말")

    def test_소모_사유가_상태에_쌓인다(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        st.counter_inc(s, "review_repair", 3, "format_reject")
        st.counter_inc(s, "review_repair", 3, "review_blocking")
        spent = s["counters"]["review_repair"]["spent"]
        assert [x["reason"] for x in spent] == ["format_reject", "review_blocking"]
        assert [x["n"] for x in spent] == [1, 2]

    def test_소모가_이벤트로도_남는다(self, repo, request_file):
        """상태는 마지막 모습이고 이벤트는 순서다 — 둘 다 필요하다."""
        paths, s = st.create_run(repo, "demo", request_file)
        st.counter_inc(s, "review_repair", 3, "format_reject", paths=paths)
        kinds = [json.loads(l) for l in
                 paths.events.read_text(encoding="utf-8").splitlines() if l.strip()]
        got = [e for e in kinds if e["kind"] == "counter_inc"]
        assert got and got[-1]["data"]["reason"] == "format_reject", got

    def test_이벤트가_실효_상한을_적는다(self, repo, request_file):
        """M56 — P8 의 `events.jsonl` 은 지급(seq 44) 뒤에도 `max: 5` 를 적었다.

        상태는 마지막 모습이고 이벤트는 순서다. 이벤트가 선언값을 적으면
        **"라운드 7 이 어느 예산으로 돌았는가"가 원장에서 사라진다** — 그 런이
        왜 세 번 멈췄는지 원장만 봐서는 설명되지 않는 것이 그래서다.
        """
        paths, s = st.create_run(repo, "demo", request_file)
        st.counter_inc(s, "review_repair", 2, "format_reject", paths=paths)
        st.counter_grant(s, "review_repair", 2, "왕복 지급")
        st.counter_inc(s, "review_repair", 2, "review_blocking", paths=paths)
        kinds = [json.loads(l) for l in
                 paths.events.read_text(encoding="utf-8").splitlines() if l.strip()]
        got = [e for e in kinds if e["kind"] == "counter_inc"]
        assert [e["data"]["max"] for e in got] == [2, 4], got

    def test_보고서가_예산을_무엇에_썼는지_적는다(self, repo, request_file):
        """원장에 있어도 보고서가 안 말하면 사람이 그 런을 못 읽는다."""
        _, s = st.create_run(repo, "demo", request_file)
        st.counter_inc(s, "review_repair", 2, "format_reject")
        st.counter_inc(s, "review_repair", 2, "format_reject")
        cell = rep_mod._counter_cell(s["counters"]["review_repair"])
        assert "format_reject" in cell, cell
        assert "2" in cell, cell

    def test_사유가_섞이면_둘_다_적는다(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        st.counter_inc(s, "review_repair", 3, "format_reject")
        st.counter_inc(s, "review_repair", 3, "review_blocking")
        cell = rep_mod._counter_cell(s["counters"]["review_repair"])
        assert "format_reject" in cell and "review_blocking" in cell, cell

    def test_모든_호출처가_사유를_준다(self, repo):
        """어휘가 있는데 코드가 안 쓰는 것이 [[ADR-H025]](M36) 의 모양이다.

        P6 의 `events.jsonl` 은 `counter_inc` 0건이었다 — 일곱 호출처 중
        하나만 이벤트를 냈기 때문이다.
        """
        text = (ROOT / "scripts" / "pipeline" / "cli.py").read_text(encoding="utf-8")
        spots = [m.start() for m in re.finditer(r"st\.counter_inc\(", text)]
        assert len(spots) >= 7, "호출처를 못 찾았다 — 이 검사가 무의미해졌다"
        for i in spots:
            window = text[i:i + 320]
            assert any('"%s"' % r in window for r in st.COUNTER_REASONS), window
            assert "paths=paths" in window, window


class TestModelCallBudget:
    """M22 — 선언만 되고 아무도 세지 않던 예산.

    P1 은 서브에이전트 10회를 태우고도 봉투에 "0/24" 를 찍었다. 재지 않는 예산은
    소진되지 않으므로 exit 5 가 영원히 발화하지 않는다.
    """

    def test_a_new_run_starts_at_zero_with_the_configured_max(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        mc = s["budget"]["model_calls"]
        assert mc["total"] == 0
        assert mc["max"] == 24
        assert mc["basis"] == "instructed"
        assert mc["blind_spots"], "두 오차 방향이 이름으로 남아야 한다"

    def test_bump_raises_total_and_the_phase_bucket_together(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        st.bump_model_calls(s, "01-plan")
        st.bump_model_calls(s, "01-plan")
        st.bump_model_calls(s, "03-implement", 2)
        mc = s["budget"]["model_calls"]
        assert mc["total"] == 4
        assert mc["by_phase"] == {"01-plan": 2, "03-implement": 2}

    def test_bump_reports_exhaustion_at_the_max(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        s["budget"]["model_calls"]["max"] = 2
        assert st.bump_model_calls(s, "01-plan") == (1, 2, False)
        assert st.bump_model_calls(s, "01-plan") == (2, 2, True)

    def test_no_max_never_exhausts(self, repo, request_file):
        """max 가 null 이면 예산이 없는 것이지 0 인 것이 아니다."""
        _, s = st.create_run(repo, "demo", request_file)
        s["budget"]["model_calls"]["max"] = None
        assert st.bump_model_calls(s, "01-plan") == (1, None, False)

    def test_01_진입이_리뷰어_둘을_지시로_센다(self, run01):
        """01 은 리뷰어 둘을 병렬로 부른다 — 봉투가 그것을 지시한다."""
        repo, paths, s = run01
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        mc = after["budget"]["model_calls"]
        assert mc["total"] == 2
        assert mc["by_phase"] == {"01-plan": 2}

    def test_같은_지시를_두_번_세지_않는다(self, run01):
        """`next` 는 같은 페이즈에서 여러 번 불린다 — 왕복 횟수를 세면 안 된다."""
        repo, paths, s = run01
        cli.run_next(repo, run_id=paths.run_id)
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        assert after["budget"]["model_calls"]["total"] == 2

    def test_제출은_세지_않는다(self, run01):
        """제출 기준은 두 방향으로 틀렸다 (M26) — 이제 지시만 센다."""
        repo, paths, s = run01
        before = st.load(repo, paths.run_id)[1]["budget"]["model_calls"]["total"]
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        _, after = st.load(repo, paths.run_id)
        assert after["budget"]["model_calls"]["total"] == before

    def test_형식만_고쳐_재제출해도_계수가_오르지_않는다(self, run01):
        """메인이 형식만 고친 재제출은 새 모델 호출이 아니다 — 옛 과다 계수."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        bad = _review("plan", findings=[
            {"id": "F-1", "severity": "major", "title": "x", "quote": "원문에 없다"}])
        j = paths.run_dir / "01_review_r1.json"
        j.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        (paths.run_dir / "01_review_r1.raw.md").write_text(
            _raw([{"severity": "major", "quote": "다른 말"}]),
            encoding="utf-8")
        before = st.load(repo, paths.run_id)[1]["budget"]["model_calls"]["total"]
        assert cli.run_record(repo, phase="01", file=str(j),
                              reviewer="plan", round_=1)["exit"] == 8
        _, after = st.load(repo, paths.run_id)
        assert after["budget"]["model_calls"]["total"] == before

    def test_exhausted_budget_stops_the_run_with_exit_5(self, run01):
        """예산이 소진되면 다음 모델 호출을 요구하지 않고 멈춘다."""
        repo, paths, s = run01
        s["budget"]["model_calls"]["max"] = 2
        st.save(paths, s)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 5, env["render"]
        assert "예산" in env["render"]

    def test_the_packet_header_names_what_it_counts(self, run01):
        """무엇을 세는지 이름으로 말한다 — "근사" 는 그것을 말하지 못한다."""
        repo, paths, s = run01
        env = cli.run_next(repo, run_id=paths.run_id)
        assert "모델 호출 2/24" in env["render"]
        assert "지시 기준" in env["render"]


# ---------------------------------------------------------------------------
# B. lint-phases — 페이즈 파일이 깨진 채로 /feature 가 시작하지 않는다
# ---------------------------------------------------------------------------

PHASE_IDS = ["00-triage", "01-plan", "02-cross-verify", "03-implement", "04-gate",
             "05-code-review", "06-pr", "07-pr-review", "08-report"]


@pytest.fixture
def phases(repo):
    """실물 페이즈 파일을 복사한다 — 실물이 깨지면 이 테스트가 먼저 깨진다."""
    d = repo / "harness" / "phases"
    d.mkdir(parents=True, exist_ok=True)
    for pid in PHASE_IDS:
        (d / ("%s.md" % pid)).write_text(
            (ROOT / "harness" / "phases" / ("%s.md" % pid)).read_text(encoding="utf-8"),
            encoding="utf-8")
    for role in ("impl-writer", "test-writer"):
        agent = repo / ".claude" / "agents" / ("%s.md" % role)
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text("# %s\n" % role, encoding="utf-8")
    return d


def _front(path):
    front, _body, _sections = cli.parse_phase_file(path)
    return front


def _rewrite(path, mutate):
    """프론트매터만 고쳐 다시 쓴다. 본문은 그대로 둔다."""
    front, body, _ = cli.parse_phase_file(path)
    mutate(front)
    path.write_text("---\n%s\n---\n%s" % (
        json.dumps(front, ensure_ascii=False, indent=2), body), encoding="utf-8")


def _lint(repo):
    return cli.lint_phases(repo)


def _fails(findings, rule=None):
    out = [f for f in findings if f["status"] == "FAIL"]
    return [f for f in out if f["rule"] == rule] if rule else out


class TestLintPhases:
    """lint-phases 가 CI 없이 검증하는 유일한 장치다 — 못 잡으면 런 중간에 안다."""

    def test_shipped_phase_files_pass(self, repo, phases):
        assert _fails(_lint(repo)) == []

    def test_missing_frontmatter_fence(self, repo, phases):
        (phases / "01-plan.md").write_text("# 본문만 있다\n", encoding="utf-8")
        assert _fails(_lint(repo), "frontmatter")

    def test_broken_json(self, repo, phases):
        (phases / "01-plan.md").write_text("---\n{not json}\n---\n# x\n", encoding="utf-8")
        assert _fails(_lint(repo), "frontmatter")

    def test_unknown_placeholder_namespace(self, repo, phases):
        _rewrite(phases / "01-plan.md",
                 lambda f: f["produces"][0].__setitem__("path", "${secrets.token}/x.md"))
        assert _fails(_lint(repo), "placeholder")

    def test_placeholder_resolves_to_nothing(self, repo, phases):
        _rewrite(phases / "01-plan.md",
                 lambda f: f["produces"][0].__setitem__(
                     "path", "${config.project.no_such_key}/x.md"))
        assert _fails(_lint(repo), "placeholder")

    def test_every_transition_lands_on_a_real_phase(self, repo, phases):
        """**여덟이 다 서면서 이 단언의 성질이 바뀌었다.**

        05 까지는 "FUTURE 한 줄이 다음 진입점을 가리킨다"였고, 그 대상이
        04 → 05 → 06 으로 옮겨 다녔다. 08 이 마지막이라 **옮길 곳이 없다** —
        이제 잠글 것은 "모든 전이가 실재하는 페이즈로 떨어지고, 마지막은
        전이하지 않는다"다. FUTURE 가 0건인 것이 이제 정상이다.

        FUTURE 판정 자체는 지우지 않았다 — 09 를 가리키는 페이즈가 생기면
        그때 다시 한 줄이 뜬다. `test_future_transition_still_warns` 가 그
        기제를 따로 잠근다.
        """
        findings = _lint(repo)
        future = [f for f in findings
                  if f["rule"] == "on_success" and f["status"] == "WARN"]
        assert future == [], "여덟이 다 섰으므로 FUTURE 는 0건이다"
        assert _fails(findings, "on_success") == []

    def test_future_transition_still_warns(self, repo, phases):
        """기제는 살아 있다 — 없는 다음을 가리키면 FAIL 이 아니라 WARN 이다."""
        _rewrite(phases / "08-report.md",
                 lambda f: f.__setitem__("on_success", "09-nope"))
        findings = _lint(repo)
        future = [f for f in findings
                  if f["rule"] == "on_success" and f["status"] == "WARN"]
        assert len(future) == 1
        assert "09-nope" in future[0]["message"]
        assert _fails(findings, "on_success") == []

    def test_backward_orphan_on_success_fails(self, repo, phases):
        _rewrite(phases / "01-plan.md", lambda f: f.__setitem__("on_success", "00-nope"))
        assert _fails(_lint(repo), "on_success")

    # ── M24. `done` 은 깨진 포인터가 아니라 종단이다.

    def test_done_은_전이_대상이_없어도_FAIL_이_아니다(self, repo, phases):
        findings = _lint(repo)
        assert _fails(findings, "on_success") == []
        assert [f for f in findings
                if f["rule"] == "on_success" and f["status"] == "WARN"] == []

    def test_on_success_가_없으면_FAIL(self, repo, phases):
        """**M24 의 lint 층 회귀.** 마지막 페이즈가 아무것도 안 가리키면
        런을 닫는 자리가 코드 어디에도 생기지 않는다."""
        _rewrite(phases / "08-report.md", lambda f: f.pop("on_success", None))
        assert _fails(_lint(repo), "on_success")

    def test_종단이_둘이면_FAIL(self, repo, phases):
        """런이 닫히는 자리는 하나다."""
        _rewrite(phases / "07-pr-review.md",
                 lambda f: f.__setitem__("on_success", st.DONE))
        assert _fails(_lint(repo), "terminal")

    def test_done_은_순환_검사를_멈추지_않는다(self, repo, phases):
        assert _fails(_lint(repo), "cycle") == []

    def test_cycle_is_rejected(self, repo, phases):
        _rewrite(phases / "02-cross-verify.md",
                 lambda f: f.__setitem__("on_success", "01-plan"))
        assert _fails(_lint(repo), "cycle")

    def test_unknown_stage_name(self, repo, phases):
        _rewrite(phases / "04-gate.md",
                 lambda f: f["gate"]["steps"].append({"id": "deploy"}))
        assert _fails(_lint(repo), "stage")

    def test_raw_shell_runner_is_rejected(self, repo, phases):
        """페이즈 파일이 임의 명령 실행 벡터가 되지 않게 한다."""
        _rewrite(phases / "04-gate.md",
                 lambda f: f["gate"].__setitem__("runner", "shell"))
        assert _fails(_lint(repo), "runner")

    def test_runner_bin_outside_whitelist(self, repo, phases):
        a = repo / "harness" / "adapters" / "nextjs-ts.json"
        data = json.loads(a.read_text(encoding="utf-8"))
        data["runner"]["bin"] = "curl"
        a.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        assert _fails(_lint(repo), "runner_bin")

    def test_missing_required_h2_section(self, repo, phases):
        p = phases / "02-cross-verify.md"
        p.write_text(p.read_text(encoding="utf-8").replace("## 금지", "## 하지 말 것"),
                     encoding="utf-8")
        assert _fails(_lint(repo), "sections")

    def test_unbalanced_code_fence_is_rejected(self, repo, phases):
        """안 닫힌 펜스는 절 하나가 파일 끝까지 삼키게 한다 (M45)."""
        p = phases / "03-implement.md"
        tail = "\n```\n열고 안 닫는다\n"
        p.write_text(p.read_text(encoding="utf-8") + tail, encoding="utf-8")
        assert _fails(_lint(repo), "fences")

    def test_role_template_required_when_agents_allowed(self, repo, phases):
        p = phases / "03-implement.md"
        p.write_text(p.read_text(encoding="utf-8")
                     .replace("## 역할 프롬프트 템플릿", "## 참고"), encoding="utf-8")
        assert _fails(_lint(repo), "sections")

    def test_path_over_the_limit(self, repo, phases):
        _rewrite(phases / "01-plan.md",
                 lambda f: f["produces"][0].__setitem__(
                     "path", "${run.dir}/" + "x" * 240 + ".md"))
        assert _fails(_lint(repo), "path_length")

    def test_missing_role_agent_definition(self, repo, phases):
        """기동 전에 무료로 잡는다 — 03 이 그 파일 없이 돌 수 없다."""
        (repo / ".claude" / "agents" / "impl-writer.md").unlink()
        assert _fails(_lint(repo), "agent_file")

    def test_unknown_requires_kind(self, repo, phases):
        _rewrite(phases / "02-cross-verify.md",
                 lambda f: f["requires"].append({"kind": "vibes"}))
        assert _fails(_lint(repo), "requires_kind")

    def test_unknown_produces_kind(self, repo, phases):
        _rewrite(phases / "02-cross-verify.md",
                 lambda f: f["produces"][0].__setitem__("kind", "yaml"))
        assert _fails(_lint(repo), "produces_kind")

    def test_id_must_match_filename(self, repo, phases):
        _rewrite(phases / "02-cross-verify.md", lambda f: f.__setitem__("id", "02-other"))
        assert _fails(_lint(repo), "id")

    def test_duplicate_index(self, repo, phases):
        _rewrite(phases / "02-cross-verify.md", lambda f: f.__setitem__("index", 1))
        assert _fails(_lint(repo), "index")

    def test_unknown_loop_counter(self, repo, phases):
        _rewrite(phases / "04-gate.md",
                 lambda f: f["loop"].__setitem__("counter", "made_up"))
        assert _fails(_lint(repo), "counter")

    def test_background_true_is_refused_not_silently_downgraded(self, repo, phases):
        """켜지지 않는 기계를 조용히 동기로 낙하시키지 않는다."""
        cal = repo / "harness" / "calibration.json"
        data = json.loads(cal.read_text(encoding="utf-8"))
        data["derived"]["background_full_regression"] = True
        cal.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        assert _fails(_lint(repo), "background")

    def test_duplicate_produces_key(self, repo, phases):
        _rewrite(phases / "04-gate.md",
                 lambda f: f["produces"][1].__setitem__("key", "gate_report"))
        assert _fails(_lint(repo), "produces_key")

    def test_taxonomy_absent_is_skip_not_pass(self, repo, phases):
        """01~04 에 소비자가 없다. 없는 검사를 통과로 세지 않는다."""
        findings = _lint(repo)
        tax = [f for f in findings if f["rule"] == "taxonomy"]
        assert tax and tax[0]["status"] == "SKIP"

    def test_cli_lint_phases_exits_two_on_failure(self, repo, phases):
        (phases / "01-plan.md").write_text("# 깨짐\n", encoding="utf-8")
        out = _run_cli(repo, "lint-phases")
        assert out.returncode == 2
        assert json.loads(out.stdout)["exit"] == 2


# ---------------------------------------------------------------------------
# D. 페이즈 파서 · requires
# ---------------------------------------------------------------------------

UNITS_DOC = """## 유닛

- `lib/match.ts · matchTitle(a: string, b: string): number`
  - 정상: 0~1 유사도 반환 / 부수효과: 없음
  - 예외: 빈 문자열 → `0`
"""


class TestContractUnits:
    """M23 — 계약 파서가 중첩 불릿을 유닛으로 세고, 템플릿 자신이 그 형태다.

    P1 에서 이것이 유닛 19개·unmatched 15건을 만들었고 화면 층 테스트가 스코프
    선택에서 빠졌다. 프로파일 판정(`small_max_units`)까지 함께 오염된다.
    """

    def _parse(self, repo, text):
        cfg = json.loads((repo / "harness" / "config.json").read_text(encoding="utf-8"))
        return contract_mod.parse(text, cfg)

    def test_top_level_bullet_is_a_unit(self, repo):
        p = self._parse(repo, UNITS_DOC)
        assert [u["symbol"] for u in p["units"]] == ["matchTitle"]
        assert p["units"][0]["container"] == "lib/match.ts"

    def test_nested_bullet_is_not_a_unit(self, repo):
        """들여쓴 줄은 그 유닛의 설명이지 또 하나의 유닛이 아니다."""
        p = self._parse(repo, UNITS_DOC)
        assert len(p["units"]) == 1, p["units"]

    def test_a_description_line_is_neither_a_unit_nor_a_drop(self, repo):
        """들여쓴 서술은 버려진 것이 아니다 — 애초에 유닛 자리가 아니다."""
        p = self._parse(repo, UNITS_DOC)
        assert p["dropped"] == [], p["dropped"]

    def test_a_symbolless_top_level_bullet_is_dropped_not_silently_lost(self, repo):
        """컨테이너도 심볼도 없으면 유닛이 아니다 — 그러나 조용히 버리지 않는다."""
        p = self._parse(repo, "## 유닛\n\n- `0`\n")
        assert p["units"] == []
        assert p["dropped"] and p["dropped"][0]["reason"]

    def test_a_unit_needs_both_a_container_and_a_symbol(self, repo):
        """심볼명만 보면 흔한 이름이 다른 파일에서 거짓 통과한다."""
        p = self._parse(repo, "## 유닛\n\n- `matchTitle`\n")
        assert p["units"] == []
        assert len(p["dropped"]) == 1

    def test_shipped_template_parses_without_drops(self, repo):
        """**템플릿이 시범 보이는 형태가 자기 파서를 속이면 안 된다.**"""
        text = (ROOT / "harness" / "templates" / "contract.md").read_text(
            encoding="utf-8")
        p = self._parse(repo, text)
        assert p["dropped"] == [], p["dropped"]
        assert [u["symbol"] for u in p["units"]] == ["matchTitle"]

    def test_doctor_rejects_a_template_that_fools_its_own_parser(self, repo, phases):
        """지금은 절 제목 일치만 본다 — 본문이 파서를 통과하는지는 아무도 안 봤다."""
        tpl = repo / "harness" / "templates" / "contract.md"
        text = tpl.read_text(encoding="utf-8")
        tpl.write_text(
            text.replace("## 진입점", "- 예외: 빈 문자열 → `0`\n\n## 진입점", 1),
            encoding="utf-8")
        bad = [c for c in cli._pipeline_checks(repo) if c["status"] == "FAIL"]
        assert bad, "템플릿이 자기 파서를 속이는데 doctor 가 통과시켰다"
        assert any("템플릿" in c["name"] for c in bad), [c["name"] for c in bad]


class TestFencedHeadingsAreNotSectionBoundaries:
    """코드 블록 안의 `## ` 가 절을 자르지 않는가 (M45).

    페이즈 파일의 역할 프롬프트 템플릿은 ` ``` ` 블록 안에 `## 네 소유 경계`
    같은 줄을 담는다. `_section` 이 그것을 다음 절의 시작으로 보고 **여는 펜스
    직후에서 잘랐다** — 봉투가 소유권 표도 계약도 제출 지시도 없이, 게다가
    **닫히지 않은 펜스**를 실어 보냈다.

    같은 원인이 `parse_phase_file` 의 `sections` 에도 있었다. 유령 절이 목록에
    들어가 `lint-phases` 의 "필수 절이 있는가" 검사가 **코드 블록 안의 글자로
    통과할 수 있었다.**

    실측(수정 전): 05 「제출 형식」 78줄 중 22줄 · 01 「제출 형식」 60줄 중
    16줄이 잘렸다. 「제출 형식」은 리뷰어에게 제출 규약을 알려 주는 절이고,
    M20·M37·M38 이 전부 "봉투가 기계 검사를 다 말하지 않아 제출이 반려됐다"는
    같은 계열이었다.
    """

    SAMPLE = """## 첫째

본문.

```
## 펜스 안 — 절이 아니다
| a | b |
```

꼬리 문장.

## 둘째

다음 절.
"""

    def test_펜스_안의_헤딩에서_자르지_않는다(self):
        got = cli._section(self.SAMPLE, "## 첫째")
        assert "| a | b |" in got, got
        assert "꼬리 문장." in got, got
        assert "## 둘째" not in got, got

    def test_잘린_절은_펜스가_짝수로_닫힌다(self):
        got = cli._section(self.SAMPLE, "## 첫째")
        assert got.count("```") % 2 == 0, got

    def test_펜스_안의_헤딩은_절_목록에_안_들어간다(self, repo, phases):
        _f, _b, sections = cli.parse_phase_file(phases / "03-implement.md")
        assert "## 네 소유 경계" not in sections, sections
        assert "## 역할 프롬프트 템플릿" in sections

    def test_모든_페이즈_절이_펜스를_닫은_채_나온다(self, repo, phases):
        """봉투에 실리는 모든 절이 온전해야 한다 — 하나라도 홀수면 렌더가 샌다."""
        bad = []
        for p in sorted(phases.glob("*.md")):
            _f, body, sections = cli.parse_phase_file(p)
            for h in sections:
                s = cli._section(body, h)
                if s.count("```") % 2:
                    bad.append((p.name, h))
        assert bad == [], bad

    def test_역할_템플릿_봉투가_소유권_표를_싣는다(self, repo, phases):
        """비는 것보다 나쁜 것은 역할이 문서와 다른 지시를 받는 것이다."""
        for name in ("03-implement.md", "04-gate.md", "05-code-review.md"):
            _f, body, _s = cli.parse_phase_file(phases / name)
            got = cli._section(body, "## 역할 프롬프트 템플릿")
            assert got.count("```") % 2 == 0, (name, got)
            assert len(got.splitlines()) > 7, (name, got)


class TestPhaseParser:

    def test_splits_frontmatter_body_and_sections(self, repo, phases):
        front, body, sections = cli.parse_phase_file(phases / "01-plan.md")
        assert front["id"] == "01-plan"
        assert body.lstrip().startswith("## 목적")
        assert "## 절차" in sections and "## 금지" in sections

    def test_resolves_the_three_namespaces(self, repo, phases, request_file):
        paths, s = st.create_run(repo, "demo", request_file)
        ctx = cli.build_context(repo, paths, s)
        # 기대값을 리터럴로 박지 않는다 — 이 검사가 묻는 것은 이름이 무엇인가가
        # 아니라 `config` 네임스페이스가 실물 설정까지 도달하는가다.
        expected = json.loads(
            (repo / "harness/config.json").read_text(encoding="utf-8"))["project"]["name"]
        assert cli.resolve("${config.project.name}", ctx) == expected
        assert cli.resolve("${run.dir}/x.md", ctx).endswith("x.md")

        # 기대값을 리터럴로 박지 않는다 — 이 칸은 `calibrate` 가 다시 잴 때마다
        # 바뀌는 **실측값**이고, 숫자를 여기 적으면 재측정이 이 테스트를 깬다.
        # 이 검사가 묻는 것은 값이 얼마인가가 아니라 `calibration` 네임스페이스가
        # 파일까지 도달하는가다.
        floor = json.loads(
            (repo / "harness" / "calibration.json").read_text(encoding="utf-8")
        )["derived"]["tests_ran_floor"]
        assert cli.resolve("${calibration.derived.tests_ran_floor}", ctx) == floor

    def test_unresolved_placeholder_raises(self, repo, phases, request_file):
        paths, s = st.create_run(repo, "demo", request_file)
        ctx = cli.build_context(repo, paths, s)
        with pytest.raises(cli.PlaceholderError):
            cli.resolve("${secrets.token}", ctx)


class TestRequires:

    @pytest.fixture
    def ctx(self, repo, phases, request_file):
        paths, s = st.create_run(repo, "demo", request_file)
        return repo, paths, s, cli.build_context(repo, paths, s)

    def test_file_kind_missing(self, ctx):
        repo, paths, s, c = ctx
        checks = cli.check_requires(
            repo, [{"kind": "file", "path": "${run.dir}/nope.md"}], c, s)
        assert not checks[0]["ok"]

    def test_file_kind_min_bytes_and_must_contain(self, ctx):
        repo, paths, s, c = ctx
        target = paths.run_dir / "01_plan.md"
        target.write_text("짧다", encoding="utf-8")
        req = [{"kind": "file", "path": "${run.dir}/01_plan.md", "min_bytes": 200}]
        assert not cli.check_requires(repo, req, c, s)[0]["ok"]
        target.write_text("가" * 300, encoding="utf-8")
        assert cli.check_requires(repo, req, c, s)[0]["ok"]
        req2 = [{"kind": "file", "path": "${run.dir}/01_plan.md",
                 "must_contain": "<!-- INTENT"}]
        assert not cli.check_requires(repo, req2, c, s)[0]["ok"]

    def test_file_kind_sha256_pointer(self, ctx):
        """의도 동결의 앵커를 진입 조건으로 건다."""
        repo, paths, s, c = ctx
        req = [{"kind": "file", "path": "${run.dir}/00_original_request.md",
                "min_bytes": 1, "sha256_pointer": "request.sha256"}]
        assert cli.check_requires(repo, req, c, s)[0]["ok"]
        paths.request.write_bytes(b"tampered")
        assert not cli.check_requires(repo, req, c, s)[0]["ok"]

    def test_state_kind_equals_and_in(self, ctx):
        repo, paths, s, c = ctx
        req = [{"kind": "state", "pointer": "phases.01-plan.status", "equals": "passed"}]
        assert not cli.check_requires(repo, req, c, s)[0]["ok"], "키 부재 = 미진입"
        st.set_phase_status(s, "01-plan", "passed")
        assert cli.check_requires(repo, req, c, s)[0]["ok"]
        req_in = [{"kind": "state", "pointer": "phases.01-plan.status",
                   "in": ["passed", "skipped"]}]
        assert cli.check_requires(repo, req_in, c, s)[0]["ok"]
        st.set_phase_status(s, "01-plan", "skipped")
        assert cli.check_requires(repo, req_in, c, s)[0]["ok"]
        assert not cli.check_requires(repo, req, c, s)[0]["ok"]

    def test_unless_skips_the_requirement(self, ctx):
        repo, paths, s, c = ctx
        s["contract"] = {"mode": "no_contract"}
        req = [{"kind": "file", "path": "${run.dir}/nope.md",
                "unless": "state.contract.mode == \"no_contract\""}]
        check = cli.check_requires(repo, req, c, s)[0]
        assert check["ok"] and check["skipped"]

    def test_adapter_stage_warn_versus_fail(self, ctx):
        repo, paths, s, c = ctx
        req_warn = [{"kind": "adapter_stage", "steps": ["e2e"], "mode": "warn"}]
        req_fail = [{"kind": "adapter_stage", "steps": ["e2e"], "mode": "fail"}]
        assert cli.check_requires(repo, req_warn, c, s)[0]["ok"], "warn 은 막지 않는다"
        assert not cli.check_requires(repo, req_fail, c, s)[0]["ok"], \
            "cmd:null 스테이지는 '없는 것'이다"

    def test_adapter_stage_present_passes(self, ctx):
        repo, paths, s, c = ctx
        req = [{"kind": "adapter_stage", "steps": ["compile", "full"], "mode": "fail"}]
        assert cli.check_requires(repo, req, c, s)[0]["ok"]


# ---------------------------------------------------------------------------
# E. 01 판정 — quote · 커버리지 · 드리프트 · 단조성
# ---------------------------------------------------------------------------

REQUEST_TEXT = (
    "책 제목 유사도를 재는 함수를 만들어 줘. 빈 문자열은 0 을 돌려주고, "
    "외부 서비스를 새로 부르지는 마.\n")


def _plan(intent=None, coverage=None, body=None):
    """정상 플랜 하나. 인자로 한 군데씩 망가뜨린다."""
    intent = intent if intent is not None else {
        "invariants": [
            {"id": "INV-1", "kind": "must", "text": "빈 문자열은 0",
             "source_quote": "빈 문자열은 0 을 돌려주고"},
            {"id": "INV-2", "kind": "must_not", "text": "외부 호출 금지",
             "source_quote": "외부 서비스를 새로 부르지는 마"},
        ],
        "out_of_scope": [],
        "acceptance": [{"id": "AC-1", "text": "0~1 유사도",
                        "source_quote": "책 제목 유사도를 재는 함수"}],
    }
    coverage = coverage if coverage is not None else {
        "covers": [{"id": "INV-1", "status": "covered", "plan_section": "## 경계값"},
                   {"id": "INV-2", "status": "covered", "plan_section": "## 외부 경계"}],
        "added_scope": [],
    }
    body = body if body is not None else (
        "# 플랜\n\n## 경계값\n빈 문자열을 먼저 거른다.\n\n"
        "## 외부 경계\n순수 함수다. 아무것도 부르지 않는다.\n" + "여백 " * 40)
    return ("<!-- INTENT\n%s\n-->\n\n%s\n\n<!-- COVERAGE\n%s\n-->\n"
            % (json.dumps(intent, ensure_ascii=False),
               body,
               json.dumps(coverage, ensure_ascii=False)))


def _review(reviewer="plan", round_=1, findings=None, mode="primary",
            resolved=None):
    return {"reviewer": reviewer, "round": round_, "mode": mode,
            "findings": findings if findings is not None else [],
            "resolved_from_previous": resolved or []}


def _raw(findings):
    """리뷰어 원문. quote 와 심각도 헤딩 개수가 json 과 맞아야 한다."""
    lines = ["# 리뷰"]
    for f in findings:
        lines.append("## %s" % f["severity"])
        lines.append(f.get("quote", ""))
    return "\n".join(lines) + "\n"


def _past_00(paths, s):
    """00 을 지나 01 앞에 세운다 — 00 의 판정은 `TestPhase00Wiring` 이 따로 묻는다."""
    st.set_phase_status(s, "00-triage", "passed")
    s["phase"] = "01-plan"
    st.save(paths, s)


@pytest.fixture
def run01(repo, phases):
    """01 지시문까지 진행된 런."""
    req = repo / "_workspace" / "requests" / "sim.md"
    req.parent.mkdir(parents=True, exist_ok=True)
    req.write_text(REQUEST_TEXT, encoding="utf-8")
    paths, s = st.create_run(repo, "sim", req)
    _past_00(paths, s)
    st.set_phase_status(s, "01-plan", "running")
    st.save(paths, s)
    return repo, paths, s


def _submit_plan(repo, paths, text):
    p = paths.run_dir / "01_plan.md"
    p.write_text(text, encoding="utf-8")
    return cli.run_record(repo, phase="01", file=str(p), reviewer=None, round_=None)


def _submit_review(repo, paths, payload, round_=1):
    code = payload["reviewer"]
    j = paths.run_dir / ("01_review_r%d.json" % round_ if code == "plan"
                         else "01_xverify_r%d.json" % round_)
    r = paths.run_dir / (j.name.replace(".json", ".raw.md"))
    j.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    r.write_text(_raw(payload["findings"]), encoding="utf-8")
    return cli.run_record(repo, phase="01", file=str(j), reviewer=code, round_=round_)


class TestPlanSubmission:

    def test_clean_plan_is_accepted(self, run01):
        repo, paths, s = run01
        env = _submit_plan(repo, paths, _plan())
        assert env["exit"] == 0, env["render"]
        assert env["data"]["drift_score"] == 0

    def test_forged_quote_is_rejected(self, run01):
        """없는 요구를 지어내는 것을 막는 유일한 기계적 손잡이다."""
        repo, paths, s = run01
        intent = json.loads(_plan().split("<!-- INTENT\n")[1].split("\n-->")[0])
        intent["invariants"][0]["source_quote"] = "요청에 없는 문장이다"
        env = _submit_plan(repo, paths, _plan(intent=intent))
        assert env["exit"] == 8
        assert "INV-1" in json.dumps(env["data"], ensure_ascii=False)

    def test_whitespace_only_differences_are_tolerated(self, run01):
        repo, paths, s = run01
        intent = json.loads(_plan().split("<!-- INTENT\n")[1].split("\n-->")[0])
        intent["invariants"][0]["source_quote"] = "빈 문자열은   0 을\n돌려주고"
        assert _submit_plan(repo, paths, _plan(intent=intent))["exit"] == 0

    def test_coverage_must_hit_each_invariant_exactly_once(self, run01):
        repo, paths, s = run01
        cov = {"covers": [{"id": "INV-1", "status": "covered",
                           "plan_section": "## 경계값"}], "added_scope": []}
        assert _submit_plan(repo, paths, _plan(coverage=cov))["exit"] == 8

    def test_duplicate_coverage_is_rejected(self, run01):
        repo, paths, s = run01
        cov = {"covers": [{"id": "INV-1", "status": "covered", "plan_section": "## 경계값"},
                          {"id": "INV-1", "status": "covered", "plan_section": "## 경계값"},
                          {"id": "INV-2", "status": "covered", "plan_section": "## 외부 경계"}],
               "added_scope": []}
        assert _submit_plan(repo, paths, _plan(coverage=cov))["exit"] == 8

    def test_plan_section_must_exist_in_the_body(self, run01):
        repo, paths, s = run01
        cov = {"covers": [{"id": "INV-1", "status": "covered", "plan_section": "## 없는 절"},
                          {"id": "INV-2", "status": "covered", "plan_section": "## 외부 경계"}],
               "added_scope": []}
        assert _submit_plan(repo, paths, _plan(coverage=cov))["exit"] == 8

    def test_uncovered_needs_a_reason(self, run01):
        repo, paths, s = run01
        cov = {"covers": [{"id": "INV-1", "status": "covered", "plan_section": "## 경계값"},
                          {"id": "INV-2", "status": "dropped"}], "added_scope": []}
        env = _submit_plan(repo, paths, _plan(coverage=cov))
        assert env["exit"] == 8

    def test_dropped_with_reason_is_drift_not_schema_error(self, run01):
        """드리프트는 exit 4 — 기계 판정 실패이고 예산이 남아 있다."""
        repo, paths, s = run01
        cov = {"covers": [{"id": "INV-1", "status": "covered", "plan_section": "## 경계값"},
                          {"id": "INV-2", "status": "dropped", "reason": "범위 밖으로 뺐다"}],
               "added_scope": []}
        env = _submit_plan(repo, paths, _plan(coverage=cov))
        assert env["exit"] == 4
        assert env["data"]["drift_score"] > 0

    def test_short_request_may_skip_the_intent_block(self, repo, phases):
        req = repo / "_workspace" / "requests" / "tiny.md"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text("오타 하나 고쳐 줘\n", encoding="utf-8")
        paths, s = st.create_run(repo, "tiny", req)
        st.set_phase_status(s, "01-plan", "running")
        st.save(paths, s)
        p = paths.run_dir / "01_plan.md"
        p.write_text("# 플랜\n\n## 수정\n" + "가" * 300, encoding="utf-8")
        env = cli.run_record(repo, phase="01", file=str(p), reviewer=None, round_=None)
        assert env["exit"] == 0, env["render"]


class TestReviewConvergence:

    def test_reviewer_main_is_rejected(self, run01):
        """작성자가 자기 글을 리뷰한 것은 독립 관측이 아니다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review(reviewer="main"))
        assert env["exit"] == 8

    def test_one_round_converges_when_both_are_primary_and_clean(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        assert _submit_review(repo, paths, _review("plan"))["exit"] == 0
        env = _submit_review(repo, paths, _review("xv"))
        assert env["exit"] == 0
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"

    def test_fallback_forbids_one_round_convergence(self, run01):
        """폴백이 섞이면 독립 관측 두 개라는 전제가 약해진다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        env = _submit_review(repo, paths, _review("xv", mode="fallback"))
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") != "passed"
        assert "2라운드" in env["render"] or env["data"].get("round") == 2

    def test_critical_forces_a_second_round(self, run01):
        """ADR-H041 — 라운드를 강제하는 것은 Critical 뿐이다 (Major 는 아니다)."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        finding = {"id": "F-1", "severity": "critical", "category": "scope",
                   "title": "범위가 넓다", "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[finding]))
        _submit_review(repo, paths, _review("xv"))
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") != "passed"

    def test_quote_not_in_raw_is_rejected(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        payload = _review("plan", findings=[
            {"id": "F-1", "severity": "major", "title": "x", "quote": "원문에 없다"}])
        j = paths.run_dir / "01_review_r1.json"
        j.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        (paths.run_dir / "01_review_r1.raw.md").write_text(
            "# 리뷰\n## major\n다른 말\n", encoding="utf-8")
        env = cli.run_record(repo, phase="01", file=str(j),
                             reviewer="plan", round_=1)
        assert env["exit"] == 8

    def test_finding_may_not_vanish_between_rounds(self, run01):
        """지적이 조용히 증발하는 것을 막는다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        finding = {"id": "F-1", "severity": "critical", "title": "범위",
                   "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[finding]))
        _submit_review(repo, paths, _review("xv"))
        env = _submit_review(repo, paths, _review("plan", round_=2), round_=2)
        assert env["exit"] == 8, "이전 open 지적이 findings 에도 resolved 에도 없다"

    # ── M21: 단조성 검사가 세 방향으로 샜다

    def test_a_reraised_finding_is_neither_new_nor_vanished(self, run01):
        """① 제목이 지적의 신원이라 다듬은 제목이 오탐을 두 번 낸다.

        `finding_key = sha1(category|target_role|title)` 이므로 리뷰어가 같은
        지적을 다른 제목으로 다시 올리면 '신규 지적' 이자 동시에 '증발한 지적'
        이 된다. 재제기를 1급 어휘로 두어 둘 다 아니게 한다.
        """
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        first = {"id": "F-1", "severity": "critical", "title": "범위가 넓다",
                 "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[first]))
        _submit_review(repo, paths, _review("xv"))

        reraised = {"id": "F-1", "severity": "critical",
                    "title": "범위가 여전히 넓다", "quote": "범위가 넓다",
                    "reraised_from_previous": "F-1"}
        env = _submit_review(
            repo, paths, _review("plan", round_=2, findings=[reraised]), round_=2)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") != "passed", "재제기는 미해결이다"

    def test_a_reraise_must_point_at_something_open(self, run01):
        """없는 지적을 가리키는 재제기는 단조성을 우회하는 구멍이 된다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        f1 = {"id": "F-1", "severity": "critical", "title": "범위", "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[f1]))
        _submit_review(repo, paths, _review("xv"))
        ghost = {"id": "F-9", "severity": "critical", "title": "x", "quote": "범위가 넓다",
                 "reraised_from_previous": "F-404"}
        env = _submit_review(
            repo, paths, _review("plan", round_=2, findings=[ghost]), round_=2)
        assert env["exit"] == 8, env["render"]
        assert "reraised_from_previous" in " ".join(env["data"]["errors"])

    def test_previous_open_drops_what_an_earlier_round_closed(self, run01):
        """② 해소가 누적되지 않아 3라운드가 1라운드에 닫힌 지적까지 또 적어야 했다.

        그 목록이 리뷰어 프롬프트에 실리므로 접두부가 라운드마다 자란다.
        """
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        f1 = {"id": "F-1", "severity": "critical", "title": "범위", "quote": "범위가 넓다"}
        g1 = {"id": "G-1", "severity": "critical", "title": "xv 의 것",
              "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[f1]))
        _submit_review(repo, paths, _review("xv", findings=[g1]))

        _submit_review(repo, paths,
                       _review("plan", round_=2,
                               resolved=[{"id": "F-1", "resolved_by": "좁혔다"}]),
                       round_=2)
        f2 = {"id": "F-2", "severity": "critical", "title": "다른 것",
              "quote": "범위가 넓다"}
        _submit_review(repo, paths,
                       _review("xv", round_=2, findings=[f2],
                               resolved=[{"id": "G-1", "resolved_by": "고쳤다"}]),
                       round_=2)

        # 3라운드: F-1 은 1라운드에서 닫혔으므로 다시 적지 않아도 통과해야 한다.
        env = _submit_review(
            repo, paths,
            _review("plan", round_=3,
                    resolved=[{"id": "F-2", "resolved_by": "고쳤다"}]),
            round_=3)
        assert env["exit"] == 0, env["render"]

    def test_an_id_from_one_reviewer_does_not_close_anothers_finding(self, run01):
        """③ 두 리뷰어가 모두 F-1·F-2… 를 쓰므로 id 한 줄이 둘을 동시에 닫았다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        mine = {"id": "F-1", "severity": "critical", "title": "내 지적",
                "quote": "범위가 넓다"}
        theirs = {"id": "F-1", "severity": "critical", "title": "남의 지적",
                  "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[mine]))
        _submit_review(repo, paths, _review("xv", findings=[theirs]))

        # plan 이 자기 F-1 을 닫는 것은 정당하다.
        assert _submit_review(
            repo, paths,
            _review("plan", round_=2,
                    resolved=[{"id": "F-1", "resolved_by": "내 것을 고쳤다"}]),
            round_=2)["exit"] == 0

        # 그러나 xv 의 F-1 은 여전히 열려 있다. 아무 말 없이 넘어갈 수 없다.
        env = _submit_review(repo, paths, _review("xv", round_=2), round_=2)
        assert env["exit"] == 8, "남의 F-1 이 다른 리뷰어의 id 한 줄로 닫혔다"

    def test_raw_without_severity_headings_is_rejected(self, run01):
        """M20 — 이 규칙이 코드에만 있고 문서에 없어서 P1 의 제출 6건 전부에
        메인이 사후에 헤딩을 붙였다. 원문 대조라는 검사의 취지와 어긋난다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        payload = _review("plan", findings=[
            {"id": "F-1", "severity": "major", "title": "범위", "quote": "범위가 넓다"}])
        j = paths.run_dir / "01_review_r1.json"
        j.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        (paths.run_dir / "01_review_r1.raw.md").write_text(
            "# 리뷰\n\n범위가 넓다\n", encoding="utf-8")
        env = cli.run_record(repo, phase="01", file=str(j),
                             reviewer="plan", round_=1)
        assert env["exit"] == 8
        assert "헤딩" in env["render"]

    def test_the_phase_file_documents_the_raw_format(self, repo):
        """검사가 요구하는 것을 페이즈 파일이 적지 않으면 리뷰어가 알 길이 없다."""
        for name in ("01-plan.md", "02-cross-verify.md"):
            body = (ROOT / "harness" / "phases" / name).read_text(encoding="utf-8")
            submit = body.split("## 제출 형식", 1)[1].split("\n## ", 1)[0]
            assert ".raw.md" in submit, name
            for sev in ("critical", "major", "minor"):
                assert sev in submit, "%s 가 %s 를 적지 않는다" % (name, sev)

    def test_resolved_from_previous_closes_it(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        finding = {"id": "F-1", "severity": "critical", "title": "범위",
                   "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[finding]))
        _submit_review(repo, paths, _review("xv"))
        env = _submit_review(
            repo, paths,
            _review("plan", round_=2,
                    resolved=[{"id": "F-1", "resolved_by": "범위를 좁혔다"}]),
            round_=2)
        assert env["exit"] == 0, env["render"]


    def test_convergence_keeps_the_round_record(self, run01):
        """수렴이 라운드 기록을 지우지 않는다 — 02 가 01 로 되돌릴 수 있다.

        예전에는 수렴 경로가 `phases["01-plan"]["rounds"]` 에 **수렴 회차(정수)**
        를 대입해 회차별 제출 기록을 통째로 날렸다. 01 이 다시 돌지 않으면
        무해했지만, 02 의 Critical 이 01 로 되돌리는 경로가 처음 돌자
        `_previous_open` 이 정수를 순회하려다 죽었다. 그리고 그 기록은
        **단조성 검사가 근거로 삼는 것**이라, 죽지 않았더라도 이전 회차 지적이
        조용히 사라지는 것을 더는 잡지 못했을 것이다.

        그 정수를 읽는 소비자는 어디에도 없었다 — 순수한 손실이었다 (P3).
        """
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        assert _submit_review(repo, paths, _review("plan"))["exit"] == 0
        assert _submit_review(repo, paths, _review("xv"))["exit"] == 0

        _, after = st.load(repo, paths.run_id)
        node = after["phases"]["01-plan"]
        assert st.phase_status(after, "01-plan") == "passed"
        assert isinstance(node["rounds"], dict), node["rounds"]
        assert set(node["rounds"]["1"]) == {"plan", "xv"}
        assert node["converged_at_round"] == 1


class TestCrossVerifySource:
    """폴백이 섞이면 1라운드 수렴이 막힌다 — 이 리포의 모든 런이 그랬다."""

    def _set_primary(self, repo, value):
        p = repo / "harness" / "config.json"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        cfg["cross_verify"]["primary"] = value
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_the_shipped_config_declares_a_primary(self, repo):
        """MCP 가 실제로 붙어 있는데 config 가 null 이면 폴백만 돈다."""
        cfg = json.loads(
            (ROOT / "harness" / "config.json").read_text(encoding="utf-8"))
        assert cfg["cross_verify"]["primary"], "primary 가 비어 있다"

    def test_primary_makes_the_mode_primary(self, repo, request_file):
        self._set_primary(repo, "some-external-reviewer")
        _, s = st.create_run(repo, "demo", request_file)
        assert s["cross_verify"]["mode"] == "primary"

    def test_no_primary_falls_back(self, repo, request_file):
        self._set_primary(repo, None)
        _, s = st.create_run(repo, "demo", request_file)
        assert s["cross_verify"]["mode"] == "fallback"

    def test_the_packet_names_the_cross_verifier(self, repo, phases, request_file):
        """페이즈 파일 본문은 플레이스홀더가 풀리지 않는다 — 봉투가 알려줘야 한다."""
        self._set_primary(repo, "some-external-reviewer")
        paths, s = st.create_run(repo, "demo", request_file)
        _past_00(paths, s)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert "## 교차검증" in env["render"]
        assert "some-external-reviewer" in env["render"]

    def test_the_packet_says_when_it_is_only_a_fallback(self, repo, phases,
                                                        request_file):
        """폴백이라는 사실이 드러나야 1라운드 수렴이 막히는 이유를 안다."""
        self._set_primary(repo, None)
        paths, s = st.create_run(repo, "demo", request_file)
        _past_00(paths, s)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert "폴백" in env["render"]
        assert "plan-reviewer" in env["render"]

    def test_no_stack_proper_noun_reaches_the_core(self):
        """도구 이름은 config 에만 둔다 — 코어는 읽기만 한다."""
        name = json.loads(
            (ROOT / "harness" / "config.json").read_text(encoding="utf-8")
        )["cross_verify"]["primary"]
        for rel in ("scripts/pipeline/cli.py", "harness/phases/01-plan.md",
                    "harness/phases/02-cross-verify.md"):
            assert name not in (ROOT / rel).read_text(encoding="utf-8"), rel

    def test_doctor_flags_a_missing_fallback_agent_file(self, repo, phases):
        """P1 직전에 plan-reviewer.md 부재를 doctor 가 못 잡았다."""
        (repo / ".claude" / "agents" / "plan-reviewer.md").unlink(missing_ok=True)
        bad = [c for c in cli._pipeline_checks(repo) if c["status"] == "FAIL"]
        assert any("plan-reviewer" in (c.get("message") or "") for c in bad), bad


class TestCrossVerifyTransientFailure:
    """primary 가 **있는데 지금 응답을 못 하는 것**은 부재가 아니다.

    P3 는 상류가 503 을 내자 폴백으로 갈아탔고 **다섯 라운드 내내 폴백이
    굳었다.** 같은 도구가 02 에서는 성공했다. 그런데 그 사실이 상태에도
    보고서에도 남지 않았고, 폴백 xv 가 민 설계를 02 의 primary 가 Critical 로
    반려하면서 왕복 예산 1회와 라운드 예산 다섯을 다 썼다.

    앱 코드에 `lookup_failed` != `no_match` 를 요구하면서(ADR-005) 하네스가
    "지금 못 함" 과 "없음" 을 한 어휘로 뭉개고 있었다.
    """

    def _fallback(self, round_=1, err=None):
        r = _review("xv", round_=round_, mode="fallback")
        if err:
            r["primary_error"] = err
        return r

    def test_a_fallback_round_lands_in_the_run_summary(self, run01):
        """라운드 제출이 런 요약에 접힌다 — 예전에는 slot 에만 들어갔다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        _submit_review(repo, paths, self._fallback())

        _, after = st.load(repo, paths.run_id)
        cv = after["cross_verify"]
        assert cv["rounds"]["1"] == "fallback", cv
        assert cv["degraded_rounds"] == 1, cv
        # config 가 선언한 것과 실제로 관측한 것은 다른 값이다
        assert cv["configured"] == "primary", cv
        assert cv["mode"] == "fallback", cv

    def test_a_transient_failure_is_not_an_absence(self, run01):
        """`primary_error` 의 유무가 일시 실패와 부재를 가른다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        _submit_review(repo, paths, self._fallback(err="HTTP 503 high demand"))

        _, after = st.load(repo, paths.run_id)
        assert after["cross_verify"]["last_primary_error"] == "HTTP 503 high demand"

    def test_an_absence_leaves_no_error(self, run01):
        """primary 가 애초에 없던 런은 `last_primary_error` 가 비어 있다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        _submit_review(repo, paths, self._fallback())

        _, after = st.load(repo, paths.run_id)
        assert after["cross_verify"]["last_primary_error"] is None

    def test_the_next_round_packet_asks_to_retry_primary(self, run01):
        """봉투가 라운드마다 교차검증기를 다시 말한다.

        예전에는 `## 교차검증` 절이 `render_packet` 에서만 나왔고 그건 `next`
        에서만 불렸다. 01 의 루프는 `record -> record` 라 그 말을 다시 할
        경로가 **물리적으로 없었다.**
        """
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        finding = {"id": "F-1", "severity": "major", "title": "범위",
                   "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[finding]))
        env = _submit_review(repo, paths,
                             self._fallback(err="HTTP 503 high demand"))

        assert env["exit"] == 0, env["render"]
        assert "## 교차검증" in env["render"], env["render"]
        assert "다시 시도" in env["render"], env["render"]
        # 도구 이름은 config 에서 온다 — 코어에 박지 않는다
        primary = json.loads(
            (repo / "harness" / "config.json").read_text(encoding="utf-8")
        )["cross_verify"]["primary"]
        assert primary in env["render"], env["render"]

    def test_a_fallback_round_degrades_the_grade(self, run01):
        """폴백은 통과가 아니다 — `external:disabled` 와 같은 형태다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        _submit_review(repo, paths, self._fallback())
        # 폴백이 섞이면 1라운드 수렴이 막히므로 2라운드를 돌려 수렴시킨다
        _submit_review(repo, paths, _review("plan", round_=2), round_=2)
        _submit_review(repo, paths, _review("xv", round_=2), round_=2)

        _, after = st.load(repo, paths.run_id)
        assert "cross_verify:fallback" in (after.get("gaps") or []), after.get("gaps")
        assert after["grade"] == "PASS_WITH_GAPS", after["grade"]

    def test_02_does_not_erase_the_round_history(self, run01):
        """02 가 primary 로 돌았다고 01 의 폴백이 없던 일이 되지 않는다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        _submit_review(repo, paths, self._fallback(err="HTTP 503 high demand"))
        _submit_review(repo, paths, _review("plan", round_=2), round_=2)
        _submit_review(repo, paths, _review("xv", round_=2), round_=2)

        v = paths.run_dir / "02_verdict.json"
        v.write_text(json.dumps({"reviewer": "xv", "mode": "primary",
                                 "status": "ok", "findings": [],
                                 "adopted": [], "resolved_from_previous": []},
                                ensure_ascii=False), encoding="utf-8")
        (paths.run_dir / "02_verdict.raw.md").write_text("# 판정\n", encoding="utf-8")
        cli.run_record(repo, phase="02", file=str(v), reviewer=None, round_=None)

        _, after = st.load(repo, paths.run_id)
        cv = after["cross_verify"]
        assert cv["rounds"]["1"] == "fallback", cv
        assert cv["degraded_rounds"] >= 1, cv
        assert cv["mode"] == "fallback", cv

    def test_mode_vocabulary_is_locked(self, run01):
        """`mode` 어휘를 늘리지 않는다 — `converged` 가 새 값을 놓치면
        조용히 1라운드 수렴이 열린다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths,
                             _review("xv", mode="fallback_after_failure"))
        assert env["exit"] == 8, env["render"]
        assert "mode" in env["render"]

    def test_primary_error_on_a_primary_submission_is_rejected(self, run01):
        """그 필드는 **폴백으로 갈아탄 이유**이지 성공한 런의 기록이 아니다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        payload = _review("xv", mode="primary")
        payload["primary_error"] = "HTTP 503"
        env = _submit_review(repo, paths, payload)
        assert env["exit"] == 8, env["render"]


FIVE_UNIT_CONTRACT = """# 계약: 제목 유사도

## 스키마·데이터 변경

없음.

## 외부 경계

없음.

## 유닛

- `lib/match.ts · matchTitle(a: string, b: string): number`
  - 정상: 0~1 유사도 / 예외: 빈 문자열 → `0`
- `lib/match.ts · normalizeTitle(a: string): string`
- `lib/match.ts · stripPunctuation(a: string): string`
- `lib/match.ts · tokenize(a: string): string[]`
- `lib/match.ts · scorePair(a: string, b: string): number`

## 진입점

없음.

## 오류 어휘

- `MATCH_EMPTY` (400)
"""


class TestProfileReconfirmation:
    """M34 — 계약이 바뀌면 프로파일을 다시 센다.

    P3 의 계약 델타 D-2 는 04 게이트 수리 중에 적용됐고, 유닛이 2 → 5 가 됐는데
    프로파일은 `small` 로 남아 05 의 리뷰어가 1명이 됐다. `_confirm_profile` 을
    부르는 자리가 `_record_03` 하나뿐이었고, `run_record` 의 멱등 가드가 통과한
    03 의 재제출을 막으므로 그 뒤에는 재판정 경로가 없었다.

    변화를 감지할 재료(`contract.sha256`)는 이미 상태에 있었고 읽는 쪽이 없었다.
    """

    def _at_05(self, repo, paths, s, profile):
        s["profile"] = dict(profile)
        st.set_phase_status(s, "04-gate", "passed")
        s["phase"] = "05-code-review"
        st.save(paths, s)

    def test_a_contract_delta_reconfirms_the_profile(self, gated, phases):
        repo, paths, s = gated
        # 03 이 세었을 때의 계약(유닛 1개) — small
        s["contract"]["sha256"] = hashlib.sha256(
            (repo / "_workspace" / "contract_sim.md").read_bytes()).hexdigest()
        self._at_05(repo, paths, s,
                    {"name": "small", "source": "auto", "units": 1,
                     "entrypoints": 0})

        # 04 수리 중 메인이 델타를 적용한다 — 유닛이 1 → 5 가 된다
        (repo / "_workspace" / "contract_sim.md").write_text(
            FIVE_UNIT_CONTRACT, encoding="utf-8")

        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        assert after["profile"]["name"] == "normal", after["profile"]
        assert after["profile"]["units"] == 5, after["profile"]
        assert after["profile"]["previous"]["name"] == "small", after["profile"]
        assert after["profile"].get("reconfirmed_at"), after["profile"]

    def test_the_reviewer_cap_follows_the_new_profile(self, gated, phases):
        """재판정이 값을 내는 자리는 05 의 리뷰어 수다."""
        repo, paths, s = gated
        s["contract"]["sha256"] = hashlib.sha256(
            (repo / "_workspace" / "contract_sim.md").read_bytes()).hexdigest()
        self._at_05(repo, paths, s,
                    {"name": "small", "source": "auto", "units": 1,
                     "entrypoints": 0})
        (repo / "_workspace" / "contract_sim.md").write_text(
            FIVE_UNIT_CONTRACT, encoding="utf-8")

        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        routed = after["phases"]["05-code-review"]["routing"]
        assert routed["profile"] == "normal", routed
        assert routed["cap"] > 1, routed

    def test_an_unchanged_contract_reconfirms_nothing(self, gated, phases):
        """sha 가 같으면 재판정하지 않는다 — 매번 다시 세면 판정이 흔들린다."""
        repo, paths, s = gated
        s["contract"]["sha256"] = hashlib.sha256(
            (repo / "_workspace" / "contract_sim.md").read_bytes()).hexdigest()
        self._at_05(repo, paths, s,
                    {"name": "small", "source": "auto", "units": 1,
                     "entrypoints": 0})

        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        assert after["profile"]["name"] == "small", after["profile"]
        assert "previous" not in after["profile"], after["profile"]

    def test_a_user_profile_survives_the_refresh(self, gated, phases):
        """`--profile` 로 준 값은 사람이 정한 것이다 — 자동 판정이 못 이긴다."""
        repo, paths, s = gated
        s["contract"]["sha256"] = "낡은값"
        self._at_05(repo, paths, s, {"name": "small", "source": "user"})
        (repo / "_workspace" / "contract_sim.md").write_text(
            FIVE_UNIT_CONTRACT, encoding="utf-8")

        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        assert after["profile"]["name"] == "small", after["profile"]
        assert after["profile"]["source"] == "user", after["profile"]

    def test_dropped_units_are_named_not_silent(self, gated, phases):
        """D-2 의 침묵. 계약이 `경로` — 서술 형태로 쓴 줄은 유닛으로 안 세어졌고
        `dropped` 를 읽는 곳이 doctor 의 **템플릿** 검사뿐이라 아무도 말하지
        않았다. 유닛이 5 가 아니라 2 로 세어져 프로파일과 스코프가 함께 빗나갔다.
        """
        repo, paths, s = gated
        s["contract"]["sha256"] = "낡은값"
        self._at_05(repo, paths, s,
                    {"name": "small", "source": "auto", "units": 1,
                     "entrypoints": 0})
        (repo / "_workspace" / "contract_sim.md").write_text(
            CONTRACT_MD.replace(
                "- `lib/match.ts · matchTitle(a: string, b: string): number`",
                "- `lib/match.ts · matchTitle(a: string, b: string): number`\n"
                "- `lib/normalize.ts` — 제목을 정규화한다"),
            encoding="utf-8")

        env = cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        dropped = after["contract"]["dropped"]
        assert dropped, after["contract"]
        assert "lib/normalize.ts" in json.dumps(dropped, ensure_ascii=False)
        assert "유닛으로 세어지지 않" in env["render"], env["render"]

    def test_the_report_names_the_profile_and_its_source(self, gated, phases):
        """`리뷰어 1/1` 이 계획인지 결함인지는 프로파일이 갈라 준다."""
        repo, paths, s = gated
        s["contract"]["sha256"] = "낡은값"
        self._at_05(repo, paths, s,
                    {"name": "small", "source": "auto", "units": 1,
                     "entrypoints": 0})
        (repo / "_workspace" / "contract_sim.md").write_text(
            FIVE_UNIT_CONTRACT, encoding="utf-8")
        cli.run_next(repo, run_id=paths.run_id)

        _, after = st.load(repo, paths.run_id)
        text, _missing = rep_mod.build(after, {}, {}, [])
        line = next(l for l in text.splitlines() if l.startswith("| 프로파일"))
        assert "normal" in line and "small" in line, line
        assert "유닛 5" in line, line

    def test_replanning_05_does_not_shrink_the_planned_reviewers(self, gated,
                                                                 phases):
        """재판정을 넣으면 이 자리를 더 자주 지난다. 변경 집합이 줄었다고
        계획된 리뷰어가 조용히 줄면 `escaped_05` 를 세는 것이 뜻을 잃는다.
        """
        repo, paths, s = gated
        (repo / "src" / "lib" / "match.ts").write_text("// 고침\n",
                                                       encoding="utf-8")
        self._at_05(repo, paths, s,
                    {"name": "normal", "source": "auto", "units": 5,
                     "entrypoints": 0})
        cli.run_next(repo, run_id=paths.run_id)
        _, mid = st.load(repo, paths.run_id)
        first = mid["phases"]["05-code-review"]["planned"]
        assert first, mid["phases"]["05-code-review"]

        # 변경 집합이 사라진다 (커밋됐다고 치자)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "wip", "--no-verify")

        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        assert set(after["phases"]["05-code-review"]["planned"]) >= set(first), \
            "계획된 리뷰어는 줄지 않는다"


class TestRoundBudgetAfterRoundTrip:
    """M32 — 바뀐 설계는 새 설계다. 한 라운드로 수렴할 이유가 없다.

    P3 에서 1~4회차가 수렴한 뒤 02 의 Critical 이 설계를 뒤집었는데, 되돌아간
    01 에 남은 라운드가 **한 번**이었다. 그 한 번이 진짜 결함 셋을 찾았다.
    왕복을 "최대 1회" 로 제한하면서 **그 뒤에 필요한 리뷰 라운드를 예산에 넣지
    않았다** — `phase` 만 되돌리고 `round` 카운터는 그대로였다.
    """

    CRITICAL = {"id": "F-1", "severity": "critical", "title": "설계를 뒤집는다",
                "quote": "빈 문자열을 먼저 거른다."}

    def _converge_01(self, repo, paths):
        """두 라운드로 수렴시킨다 — 1라운드 수렴은 02 를 건너뛴다 (ADR-H042)."""
        _submit_plan(repo, paths, _plan())
        crit = {"id": "F-0", "severity": "critical", "title": "1회차 지적",
                "quote": "빈 문자열을 먼저 거른다."}
        _submit_review(repo, paths, _review("plan", findings=[crit]))
        _submit_review(repo, paths, _review("xv"))
        return _submit_review(
            repo, paths,
            _review("plan", round_=2,
                    resolved=[{"id": "F-0", "resolved_by": "고쳤다"}]),
            round_=2)

    def _verdict(self, repo, paths, findings):
        v = paths.run_dir / "02_verdict.json"
        v.write_text(json.dumps(
            {"reviewer": "xv", "mode": "primary", "status": "ok",
             "findings": findings,
             "adopted": [{"id": f["id"], "verdict": "accept"} for f in findings],
             "resolved_from_previous": []}, ensure_ascii=False), encoding="utf-8")
        (paths.run_dir / "02_verdict.raw.md").write_text("# 판정\n", encoding="utf-8")
        return cli.run_record(repo, phase="02", file=str(v), reviewer=None,
                              round_=None)

    def test_a_round_trip_grants_round_budget(self, run01):
        repo, paths, s = run01
        self._converge_01(repo, paths)
        _, mid = st.load(repo, paths.run_id)
        before = mid["counters"]["round"]["max"]

        env = self._verdict(repo, paths, [dict(self.CRITICAL)])
        assert env["exit"] == 4, env["render"]

        _, after = st.load(repo, paths.run_id)
        node = after["counters"]["round"]
        assert node["max"] > before, node
        assert node["grants"], "지급 사실이 남아야 한다"
        assert node["grants"][0]["reason"], node["grants"][0]

    def test_a_grant_does_not_rewind_used(self, run01):
        """리셋이 아니라 지급이다 — M31 이 회차 기록을 지운 손실이었다."""
        repo, paths, s = run01
        self._converge_01(repo, paths)
        _, mid = st.load(repo, paths.run_id)
        used = mid["counters"]["round"]["used"]
        assert used > 0

        self._verdict(repo, paths, [dict(self.CRITICAL)])
        _, after = st.load(repo, paths.run_id)
        assert after["counters"]["round"]["used"] == used, \
            "몇 라운드를 썼는가는 지워지지 않는다"

    def test_the_envelope_names_the_grant(self, run01):
        repo, paths, s = run01
        self._converge_01(repo, paths)
        env = self._verdict(repo, paths, [dict(self.CRITICAL)])
        _, after = st.load(repo, paths.run_id)
        extra = after["counters"]["round"]["grants"][0]["extra"]
        assert extra > 0
        # 문구가 아니라 **실제 지급량**을 말해야 한다
        assert "**%d 를 새로 지급했다**" % extra in env["render"], env["render"]
        assert env["data"]["granted_rounds"] == extra, env["data"]

    def test_a_clean_verdict_grants_nothing(self, run01):
        """되돌리지 않는 판정은 예산을 늘리지 않는다."""
        repo, paths, s = run01
        self._converge_01(repo, paths)
        _, mid = st.load(repo, paths.run_id)
        before = mid["counters"]["round"]["max"]
        self._verdict(repo, paths, [])
        _, after = st.load(repo, paths.run_id)
        assert after["counters"]["round"]["max"] == before
        assert not (after["counters"]["round"].get("grants") or [])

    def test_the_report_names_the_granted_rounds(self, run01):
        """보고서의 `라운드` 행이 `used` 만 적으면 지급이 안 드러난다."""
        repo, paths, s = run01
        self._converge_01(repo, paths)
        self._verdict(repo, paths, [dict(self.CRITICAL)])
        _, after = st.load(repo, paths.run_id)
        text, _missing = rep_mod.build(after, {}, {}, [])
        line = next(l for l in text.splitlines() if l.startswith("| 라운드"))
        assert "지급" in line, line

    def test_지급받은_라운드가_다음_라운드에서_살아남는다(self, run01):
        """M56 — 되돌아간 01 이 한 바퀴 더 돌면 상한이 선언값으로 되돌아갔다.

        **위 다섯은 전부 `counter_grant` 직후만 봤다.** 되돌린 01 이 실제로
        라운드를 더 도는 것을 아무도 묻지 않아, 지급 경로가 실질적으로 없는
        채로 P8 까지 왔다 — 그 런에서 라운드 7·8·9 가 오탐 에스컬레이션이었다.
        """
        repo, paths, s = run01
        self._converge_01(repo, paths)
        self._verdict(repo, paths, [dict(self.CRITICAL)])
        _, granted = st.load(repo, paths.run_id)
        eff = granted["counters"]["round"]["max"]
        assert eff == 6, granted["counters"]["round"]    # 선언 3 + 지급 3

        self._converge_01(repo, paths)                   # 되돌아간 01 이 한 바퀴 더
        _, after = st.load(repo, paths.run_id)
        assert after["counters"]["round"]["max"] == eff, after["counters"]["round"]

    def test_에스컬레이션은_선언_상한이_아니라_실효_상한에서_난다(self, run01):
        """`_judge_round`(cli.py) 의 `exceeded` 가 실효값을 받는가.

        M56 의 피해가 실제로 난 자리다 — 지급으로 상한이 10 이 됐는데 6회차가
        `used >= 5` 로 판정돼 멈췄다. `used` 직접 대입은 이 스위트의 기존
        패턴이고, 선언 상한 5 의 코앞에서만 이 갈림이 보이기 때문에 쓴다.
        """
        MINOR = {"id": "F-9", "severity": "minor", "title": "이름이 모호하다",
                 "quote": "빈 문자열을 먼저 거른다."}
        repo, paths, s = run01
        self._converge_01(repo, paths)
        self._verdict(repo, paths, [dict(self.CRITICAL)])
        _, mid = st.load(repo, paths.run_id)
        assert mid["counters"]["round"]["used"] == 2   # 선언 상한 3 의 코앞
        st.save(paths, mid)

        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", round_=3, findings=[MINOR]),
                       round_=3)
        env = _submit_review(repo, paths, _review("xv", round_=3, findings=[MINOR]),
                             round_=3)
        _, after = st.load(repo, paths.run_id)
        assert after["counters"]["round"]["used"] == 3, after["counters"]["round"]
        # 실효 상한은 6 이다. 3 에서 멈추면 그것이 M56 이다.
        assert not after.get("escalated"), env["render"]


class TestLoopDeclarationsAreRead:
    """M36 — 선언만 있고 코드가 안 읽는 설정을 잡는다.

    **지금 동작이 선언값과 우연히 일치했다.** 그래서 "읽는지" 만 보는 단언은
    되돌려도 초록이다. 여기 있는 것은 전부 **값을 바꾸는 변이 테스트**다 —
    선언을 고치면 동작이 따라 바뀌는가를 묻는다. ADR-H023 이 
    `stuck_after_identical` 에서 겪은 함정이 그것이다.
    """

    CRITICAL = {"id": "F-1", "severity": "critical", "title": "설계를 뒤집는다",
                "quote": "빈 문자열을 먼저 거른다."}

    def _xv(self, repo):
        return repo / "harness" / "phases" / "02-cross-verify.md"

    def _round_trip(self, repo, paths):
        """01 을 두 라운드로 수렴시키고 02 에 Critical 판정을 낸다. 반환: 02 의 봉투.

        1라운드 수렴은 02 를 건너뛰므로(ADR-H042) 2라운드로 간다.
        """
        _submit_plan(repo, paths, _plan())
        crit = {"id": "F-0", "severity": "critical", "title": "1회차 지적",
                "quote": "빈 문자열을 먼저 거른다."}
        _submit_review(repo, paths, _review("plan", findings=[crit]))
        _submit_review(repo, paths, _review("xv"))
        _submit_review(repo, paths,
                       _review("plan", round_=2,
                               resolved=[{"id": "F-0", "resolved_by": "고쳤다"}]),
                       round_=2)
        v = paths.run_dir / "02_verdict.json"
        v.write_text(json.dumps(
            {"reviewer": "xv", "mode": "primary", "status": "ok",
             "findings": [dict(self.CRITICAL)],
             "adopted": [{"id": "F-1", "verdict": "accept"}],
             "resolved_from_previous": []}, ensure_ascii=False), encoding="utf-8")
        (paths.run_dir / "02_verdict.raw.md").write_text(
            "# " + "판정", encoding="utf-8")
        return cli.run_record(repo, phase="02", file=str(v), reviewer=None,
                              round_=None)

    def test_왕복_상한을_프론트매터에서_읽는다(self, run01):
        """상한 3 이면 두 번째 왕복이 허용된다. 하드코딩 1 이면 에스컬레이션이다."""
        repo, paths, s = run01
        _rewrite(self._xv(repo), lambda f: f["loop"].__setitem__("max", 3))

        first = self._round_trip(repo, paths)
        assert first["exit"] == 4, first["render"]
        second = self._round_trip(repo, paths)
        assert second["exit"] == 4, second["render"]

        _, after = st.load(repo, paths.run_id)
        assert after["counters"]["xverify_return"]["max"] == 3, after["counters"]
        assert not after.get("escalated"), "상한 3 인데 두 번째에서 멈췄다"

    def test_상한을_안_올리면_두_번째_왕복이_멈춘다(self, run01):
        """실물 선언(max 1)의 동작이 안 바뀌었음을 잠근다."""
        repo, paths, s = run01
        assert self._round_trip(repo, paths)["exit"] == 4
        second = self._round_trip(repo, paths)
        _, after = st.load(repo, paths.run_id)
        assert after.get("escalated"), second["render"]

    def test_왕복_상한을_지우면_거부한다(self, run01):
        repo, paths, s = run01
        _rewrite(self._xv(repo), lambda f: f["loop"].pop("max"))
        env = self._round_trip(repo, paths)
        assert env["exit"] == 2, env["render"]
        assert env["data"]["key"] == "loop.max", env["data"]
        assert "02-cross-verify" in env["render"], env["render"]

    def test_되돌아갈_페이즈를_지우면_거부한다(self, run01):
        repo, paths, s = run01
        _rewrite(self._xv(repo), lambda f: f["loop"].pop("on_fail_return_to"))
        env = self._round_trip(repo, paths)
        assert env["exit"] == 2, env["render"]
        assert env["data"]["key"] == "loop.on_fail_return_to", env["data"]
        _, after = st.load(repo, paths.run_id)
        assert after["phase"] == "02-cross-verify", "되돌아가지 않았어야 한다"

    def test_카운터를_지우면_거부한다(self, run01):
        repo, paths, s = run01
        _rewrite(self._xv(repo), lambda f: f["loop"].pop("counter"))
        env = self._round_trip(repo, paths)
        assert env["exit"] == 2, env["render"]
        assert env["data"]["key"] == "loop.counter", env["data"]

    def test_on_exceed_어휘_밖은_런타임이_거부한다(self, run01):
        repo, paths, s = run01
        _rewrite(self._xv(repo),
                 lambda f: f["loop"].__setitem__("on_exceed", "continue"))
        assert self._round_trip(repo, paths)["exit"] == 4
        env = self._round_trip(repo, paths)
        assert env["exit"] == 2, env["render"]
        assert env["data"]["key"] == "loop.on_exceed", env["data"]
        _, after = st.load(repo, paths.run_id)
        assert not after.get("escalated"), "어휘 밖인데 escalate 로 낙하했다"

    def test_on_exceed_어휘_밖은_lint_가_거부한다(self, repo, phases):
        _rewrite(phases / "04-gate.md",
                 lambda f: f["loop"].__setitem__("on_exceed", "continue"))
        assert _fails(_lint(repo), "on_exceed"), _lint(repo)

    def test_converge_와_loop_의_on_exceed_가_어긋나면_거부한다(self, repo, phases):
        _rewrite(phases / "01-plan.md",
                 lambda f: f["converge"].__setitem__("on_exceed", "continue"))
        assert _fails(_lint(repo), "on_exceed"), _lint(repo)

    def test_되돌아갈_페이즈는_자기보다_앞이어야_한다(self, repo, phases):
        _rewrite(phases / "02-cross-verify.md",
                 lambda f: f["loop"].__setitem__("on_fail_return_to", "03-implement"))
        assert _fails(_lint(repo), "on_fail_return_to"), _lint(repo)

    def test_상한이_없으면_lint_가_거부한다(self, repo, phases):
        _rewrite(phases / "04-gate.md", lambda f: f["loop"].pop("max"))
        assert _fails(_lint(repo), "loop_max"), _lint(repo)


class TestInitAndNext:

    def test_init_creates_a_run_and_next_renders_the_first_packet(self, repo, phases):
        req = repo / "_workspace" / "requests" / "x.md"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text(REQUEST_TEXT, encoding="utf-8")
        out = _run_cli(repo, "init", "--feature", "demo", "--request-file", str(req))
        assert out.returncode == 0, out.stderr
        env = json.loads(out.stdout)
        assert env["run_id"]

        out2 = _run_cli(repo, "next")
        env2 = json.loads(out2.stdout)
        assert env2["exit"] == 0, env2["render"]
        # 경로 언급이 없는 요청은 기계가 못 정한다 — 첫 패킷은 00 이고 저가
        # 모델 1회를 지시한다 (ADR-H044). docs 요청의 첫 패킷이 01 인 것은
        # `TestPhase00Wiring` 이 묻는다.
        assert env2["phase"] == "00-triage"
        assert "00_triage.json" in env2["render"]
        assert "record --phase 00" in (env2["next_command"] or "")

    def test_init_rejects_a_bad_slug(self, repo, phases, request_file):
        out = _run_cli(repo, "init", "--feature", "Bad Slug",
                       "--request-file", str(request_file))
        assert out.returncode == 2

    def test_next_refuses_when_requires_fail(self, repo, phases, request_file):
        paths, s = st.create_run(repo, "demo", request_file)
        paths.request.unlink()
        out = _run_cli(repo, "next")
        assert out.returncode == 3
        assert json.loads(out.stdout)["data"]["requires_report"]

    def test_record_on_a_passed_phase_is_refused(self, run01):
        """record 는 멱등이 아니다. 재작업은 retry 로만."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        _submit_review(repo, paths, _review("xv"))
        env = _submit_plan(repo, paths, _plan())
        assert env["exit"] == 3


# ---------------------------------------------------------------------------
# F. clean_ownership — 소유 경계 · orphan
# ---------------------------------------------------------------------------

def _claims(impl=None, test=None):
    return {"schema": 1, "roles": [
        {"role": "impl", "agent": "impl-writer", "status": "ok",
         "claimed_files": impl or [], "contract_symbols_implemented": []},
        {"role": "test", "agent": "test-writer", "status": "ok",
         "claimed_files": test or [], "contract_symbols_covered": []}]}


@pytest.fixture
def config(repo):
    return harness._read_json(repo / "harness/config.json")


class TestCleanOwnership:

    def test_clean_run_passes(self, repo, config):
        (repo / "src" / "lib" / "match.ts").write_text("// 고침\n", encoding="utf-8")
        got = attr.clean_ownership(repo, config, _claims(impl=["src/lib/match.ts"]))
        assert got["ok"], got["message"]

    def test_role_touching_another_roles_file(self, repo, config):
        """구현 역할이 테스트 파일을 고치면 둘이 서로를 덮는다."""
        (repo / "src" / "lib" / "match.test.ts").write_text("// 고침\n", encoding="utf-8")
        got = attr.clean_ownership(repo, config, _claims(impl=["src/lib/match.test.ts"]))
        assert not got["ok"]
        assert any(v["kind"] == "violation" for v in got["findings"])
        assert got["rollback"]

    def test_orphan_change_is_caught(self, repo, config):
        (repo / "src" / "lib" / "match.ts").write_text("// 고침\n", encoding="utf-8")
        got = attr.clean_ownership(repo, config, _claims())
        assert not got["ok"]
        assert any(v["kind"] == "orphan" for v in got["findings"])

    def test_main_owned_change_is_not_a_violation(self, repo, config):
        (repo / "harness" / "config.json").write_text(
            (repo / "harness" / "config.json").read_text(encoding="utf-8"),
            encoding="utf-8")
        (repo / "CLAUDE.md").write_text("# 가드레일\n한 줄 더\n", encoding="utf-8")
        got = attr.clean_ownership(repo, config, _claims())
        assert got["ok"], got["message"]

    def test_excludes_beats_owns(self, repo, config):
        """구현 역할의 owns 가 src/lib/** 이지만 excludes 가 테스트 파일을 뺀다."""
        impl = next(r for r in config["roles"] if r["id"] == "impl")
        assert not harness.owns_file(impl, "src/lib/match.test.ts")
        assert harness.owns_file(impl, "src/lib/match.ts")

    def test_verdict_agrees_with_the_doctor_glob(self, repo, config):
        """소유 판정이 두 곳에서 갈라지면 안 된다 — 같은 함수를 쓴다."""
        samples = ["src/lib/match.ts", "src/lib/match.test.ts", "src/app/page.tsx",
                   "docs/TRD.md", "harness/config.json", "README.md",
                   "src/components/x.tsx", "src/services/y.ts"]
        for path in samples:
            mine = attr.owner_for_path(config, path)
            theirs = next((r["id"] for r in config["roles"]
                           if harness.owns_file(r, path)), None)
            assert mine == theirs, path


# ---------------------------------------------------------------------------
# H. adapters — 스테이지 상태 · 타임아웃 · 선택자
# ---------------------------------------------------------------------------

class TestAdapters:

    def test_null_cmd_is_absent_and_never_runs(self, repo):
        _config, adapter, cal = adapters.load(repo)
        assert adapters.stage_state(adapter, "e2e") == "absent"
        called = []
        got = adapters.run_stage(repo, adapter, "e2e",
                                 runner=lambda *a: called.append(a) or (0, ""))
        assert got == {"id": "e2e", "state": "skipped", "reason": "absent"}
        assert not called, "없는 스테이지를 실행하지 않는다"
        assert "sec" not in got, "못 잰 값에 0 을 넣지 않는다"

    def test_when_touched_miss(self, repo):
        _config, adapter, _cal = adapters.load(repo)
        assert adapters.when_touched_hit(adapter, "build", ["docs/TRD.md"]) is False
        assert adapters.when_touched_hit(adapter, "build", ["src/app/page.tsx"]) is True
        assert adapters.when_touched_hit(adapter, "compile", ["x"]) is None

    def test_full_timeout_comes_from_calibration_not_the_adapter(self, repo):
        """실측이 어댑터 선언을 이긴다 — 상수가 아니라 함수다."""
        _config, adapter, cal = adapters.load(repo)
        assert adapter["stages"]["full"]["timeout_sec"] == 1800
        assert adapters.stage_timeout(adapter, cal, "full") == (300, "calibration")
        assert adapters.stage_timeout(adapter, None, "full") == (1800, "adapter")

    def test_derived_is_none_not_zero_when_absent(self, repo):
        assert adapters.derived(None, "tests_ran_floor") is None
        assert adapters.derived({}, "tests_ran_floor") is None

    def test_multi_selector_becomes_path_arguments(self, repo):
        """이 어댑터는 select 를 두지 않는다 — 선택자가 경로 필터로 붙는다."""
        _config, adapter, _cal = adapters.load(repo)
        argv = adapters.stage_argv(repo, adapter, "scoped",
                                   ["src/lib/a.test.ts", "src/lib/b.test.ts"])
        assert argv[-2:] == ["src/lib/a.test.ts", "src/lib/b.test.ts"]

    def test_parse_report_agrees_with_the_contract_layer(self, repo):
        _config, adapter, _cal = adapters.load(repo)
        _write_report(repo, tests=7, failures=2)
        mine = adapters.parse_report(repo, adapter)
        theirs = harness._parse_junit(repo, adapter)
        assert (mine["ran"], mine["suites"], mine["failures"], mine["matched"]) == theirs

    def test_infra_pattern_ignored_when_exit_is_zero(self, repo):
        _config, adapter, _cal = adapters.load(repo)
        assert adapters.infra_match(adapter, 0, "ECONNREFUSED 가 로그에 스쳤다") is None
        assert adapters.infra_match(adapter, 1, "ECONNREFUSED") == "ECONNREFUSED"


def _write_report(root, tests=1, failures=0, cases=None):
    """어댑터의 glob 과 같은 구조로 리포트를 만든다."""
    d = Path(root) / "reports" / "junit"
    d.mkdir(parents=True, exist_ok=True)
    body = cases or ""
    d.joinpath("report.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        '<testsuites name="t" tests="%d" failures="%d" errors="0" time="1">\n'
        '  <testsuite name="s" tests="%d" failures="%d" errors="0" skipped="0">\n'
        '%s'
        '  </testsuite>\n</testsuites>\n' % (tests, failures, tests, failures, body),
        encoding="utf-8")


# ---------------------------------------------------------------------------
# G(순수 함수). 귀속 — 소유자 · 시그니처 · flip
# ---------------------------------------------------------------------------

class TestAttribution:

    def test_signature_masks_volatile_parts(self):
        a = attr.signature("impl", "u", "assertion",
                           "expected 3 at C:/x/y.ts:12 (deadbeef1234)")
        b = attr.signature("impl", "u", "assertion",
                           "expected 9 at C:/other/z.ts:44 (cafebabe9999)")
        assert a == b, "경로·숫자·해시를 마스킹해야 같은 실패가 같은 시그니처가 된다"
        c = attr.signature("test", "u", "assertion", "expected 3")
        assert c != a

    def test_symbol_not_found_in_contract_forces_the_primary_role(self, repo, config):
        """경로만 보고 테스트 역할에 보내면 매번 오귀속된다."""
        _c, adapter, _cal = adapters.load(repo)
        log = ("src/lib/match.test.ts(3,10): error TS2305: "
               "Module './match' has no exported member 'matchTitle'.")
        got = attr.attribute_compile(adapter, config, {"matchTitle"}, log)
        assert got and got[0]["owner"] == config["primary_role"]
        assert "primary_role" in got[0]["owner_reason"]

    def test_plain_compile_error_uses_the_path(self, repo, config):
        _c, adapter, _cal = adapters.load(repo)
        log = "src/lib/match.ts(9,3): error TS2322: Type 'string' is not assignable."
        got = attr.attribute_compile(adapter, config, set(), log)
        assert got and got[0]["owner"] == "impl"

    def test_assertion_in_contract_is_ambiguous(self, repo, config):
        _c, adapter, _cal = adapters.load(repo)
        units = [{"unit": "matchTitle 는 0 을 돌려준다", "file": "src/lib/match.test.ts",
                  "ftype": "AssertionError", "message": "expected 1 to be 0",
                  "detail": "at src/lib/match.ts:4"}]
        got = attr.attribute_tests(adapter, config, {"matchTitle"}, units,
                                   repo_files=["src/lib/match.ts", "src/lib/match.test.ts"])
        assert got[0]["owner"] == "ambiguous"

    def test_assertion_outside_contract_goes_to_the_test_role(self, repo, config):
        _c, adapter, _cal = adapters.load(repo)
        units = [{"unit": "지어낸 심볼", "file": "src/lib/match.test.ts",
                  "ftype": "AssertionError", "message": "expected", "detail": ""}]
        got = attr.attribute_tests(adapter, config, {"matchTitle"}, units,
                                   repo_files=["src/lib/match.test.ts"])
        assert got[0]["owner"] == "test"
        assert "out_of_contract" in got[0]["owner_reason"]

    def test_frames_only_count_files_that_exist(self, repo, config):
        """스택 문법에 의존하지 않는다 — 리포에 실재하는 파일만 프레임이다."""
        _c, adapter, _cal = adapters.load(repo)
        frames = attr.frames_from(
            "at wonder (src/lib/match.ts:4)\nat nowhere (vendor/ghost.ts:9)",
            ["src/lib/match.ts", "src/lib/match.test.ts"])
        assert frames == ["src/lib/match.ts"]

    def test_ambiguous_goes_to_primary_then_flips(self, repo, config):
        failures = [{"id": "F-1", "owner": "ambiguous", "sig": "abc",
                     "file": "src/lib/match.test.ts"}]
        flip = {}
        first = attr.resolve_ambiguous(failures, config, flip)
        assert first[0]["owner"] == "impl"
        second = attr.resolve_ambiguous(
            [dict(failures[0])], config, flip)
        assert second[0]["owner"] == "test", "동일 시그니처 재발이면 다음 역할로 넘긴다"
        third = attr.resolve_ambiguous([dict(failures[0])], config, flip)
        assert third[0]["owner"] == "contract", "또 재발하면 계약 결함으로 재분류한다"

    def test_dispatch_never_assigns_two_owners_that_share_a_target(self, repo, config):
        """핑퐁 방지 — 같은 대상을 두고 둘에게 동시에 보내지 않는다."""
        failures = [
            {"id": "F-1", "owner": "impl", "sig": "a", "frames": ["src/lib/match.ts"]},
            {"id": "F-2", "owner": "test", "sig": "b", "frames": ["src/lib/match.ts"]},
        ]
        got = attr.dispatch(failures, config, prev_sigs=[], flip_state={})
        assert got["owner"] in ("impl", "test")
        assert got["deferred"], "나머지는 미룬 것으로 드러난다"

    def test_disjoint_failures_may_go_out_together(self, repo, config):
        failures = [
            {"id": "F-1", "owner": "impl", "sig": "a", "frames": ["src/lib/match.ts"]},
            {"id": "F-2", "owner": "test", "sig": "b", "frames": ["src/lib/other.test.ts"]},
        ]
        got = attr.dispatch(failures, config, prev_sigs=[], flip_state={})
        assert got["parallel"] is True and not got["deferred"]

    def test_same_signature_twice_is_stuck(self, repo, config):
        failures = [{"id": "F-1", "owner": "impl", "sig": "a", "frames": []}]
        got = attr.dispatch(failures, config, prev_sigs=["impl|a"], flip_state={})
        assert got["stuck"] is True, "예산이 남아도 즉시 에스컬레이션이다"

    # --- M33. 정체 감지는 시그니처가 아니라 (소유자, 시그니처) 를 센다 -------

    def test_a_flip_gets_its_turn_before_stuck(self, repo, config):
        """**P3 가 밟은 경로다.** flip 이 다음 역할을 배정한 바로 그 라운드에
        정체 감지가 먼저 멈추면, 그 배정은 지시로 나가지 못하고 버려진다.
        ambiguous 실패는 구조적으로 두 역할 중 한쪽만 시도해 보게 된다.
        """
        failure = {"id": "F-1", "owner": "ambiguous", "sig": "a", "frames": []}
        flip, chain = {}, []

        # 예전 코드가 체인에 쌓던 것은 **순수 시그니처**였고, ambiguous 실패의
        # 그 값은 라운드를 넘어 안 바뀌므로 2회차를 반드시 멈춰 세웠다.
        assert attr.dispatch([dict(failure)], config, ["a"], {})["stuck"] is False, \
            "시그니처만으로 정체를 세면 flip 이 값을 낼 기회가 없다"

        first = attr.dispatch([dict(failure)], config, chain, flip)
        assert first["owner"] == "impl"
        assert first["stuck"] is False
        chain.extend(first["pairs"])

        second = attr.dispatch([dict(failure)], config, chain, flip)
        assert second["owner"] == "test", "flip 이 다음 역할로 넘겼다"
        assert second["stuck"] is False, "그 배정은 지시로 나가야 한다"
        chain.extend(second["pairs"])

        third = attr.dispatch([dict(failure)], config, chain, flip)
        assert third["owner"] == "contract", "역할을 다 돌면 계약 결함이다"
        assert third["stuck"] is False

    def test_the_same_owner_twice_is_still_stuck(self, repo, config):
        """경로에서 소유자가 정해진 실패는 쌍이 1회차부터 고정이다."""
        failure = {"id": "F-1", "owner": "impl", "sig": "a", "frames": []}
        chain = []
        first = attr.dispatch([dict(failure)], config, chain, {})
        assert first["stuck"] is False
        chain.extend(first["pairs"])
        second = attr.dispatch([dict(failure)], config, chain, {})
        assert second["stuck"] is True, "같은 소유자에게 같은 실패를 두 번 보냈다"

    def test_stuck_after_identical_is_read_not_hardcoded(self, repo, config):
        """`stuck_after_identical` 은 프론트매터에만 있고 코드가 안 읽었다 —
        값을 3 으로 바꿔도 2회차에 멈췄다.
        """
        failure = {"id": "F-1", "owner": "impl", "sig": "a", "frames": []}
        chain = ["impl|a"]
        got = attr.dispatch([dict(failure)], config, chain, {}, stuck_after=3)
        assert got["stuck"] is False, "3회 설정이면 2회차에 안 멈춘다"
        chain.extend(got["pairs"])
        again = attr.dispatch([dict(failure)], config, chain, {}, stuck_after=3)
        assert again["stuck"] is True, "3회차에 멈춘다"

    def test_dispatch_reports_pairs_and_sigs_separately(self, repo, config):
        """`sigs` 는 `attribution.json` 기록용으로 남는다 — 쌍이 그것을 대체하지
        않는다. 무엇으로 셌는지와 무엇이 실패했는지는 다른 사실이다.
        """
        failure = {"id": "F-1", "owner": "ambiguous", "sig": "a", "frames": []}
        got = attr.dispatch([dict(failure)], config, [], {})
        assert got["sigs"] == ["a"]
        assert got["pairs"] == ["impl|a"], "쌍은 배정된 소유자를 담는다"


# ---------------------------------------------------------------------------
# G. 게이트 · 귀속 — replay 픽스처
# ---------------------------------------------------------------------------

CONTRACT_MD = """# 계약: 제목 유사도

## 스키마·데이터 변경

없음.

## 외부 경계

없음.

## 유닛

- `lib/match.ts · matchTitle(a: string, b: string): number`
  - 정상: 0~1 유사도 / 예외: 빈 문자열 → `0`

## 진입점

없음.

## 오류 어휘

- `MATCH_EMPTY` (400)
"""

FIXTURES = ROOT / "scripts" / "fixtures" / "gate"


def make_fixture(base, case, stages, *, tests=None, failures=0, cases="",
                 with_report=True, with_contract=True, changed=None,
                 stdouts=None):
    """replay 픽스처 하나. 어댑터 glob 과 같은 구조로 리포트를 놓는다."""
    d = Path(base) / case
    (d / "reports" / "junit").mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 1, "case": case, "adapter": "nextjs-ts",
                "stages": stages,
                "changed_paths": changed or ["src/lib/match.ts",
                                             "src/lib/match.test.ts"],
                "repo_files": ["src/lib/match.ts", "src/lib/match.test.ts",
                               "package.json"]}
    for name, text in (stdouts or {}).items():
        (d / ("%s.stdout.txt" % name)).write_text(text, encoding="utf-8")
        manifest["stages"].setdefault(name, {})["stdout"] = "%s.stdout.txt" % name
    (d / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if with_contract:
        (d / "contract.md").write_text(CONTRACT_MD, encoding="utf-8")
    if with_report:
        _write_report_at(d / "reports" / "junit", tests if tests is not None else 1300,
                         failures, cases)
    return d


def _write_report_at(d, tests, failures, cases=""):
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("report.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        '<testsuites name="t" tests="%d" failures="%d" errors="0" time="1">\n'
        '  <testsuite name="s" tests="%d" failures="%d" errors="0" skipped="0">\n'
        '%s'
        '  </testsuite>\n</testsuites>\n' % (tests, failures, tests, failures, cases),
        encoding="utf-8")


ALL_PASS = {"compile": {"exit": 0}, "lint": {"exit": 0}, "check": {"exit": 0},
            "scoped": {"exit": 0}, "full": {"exit": 0}, "build": {"exit": 0}}


@pytest.fixture
def fxdir(tmp_path_factory):
    """픽스처는 리포 **밖**에 만든다.

    리포 안에 두면 그 파일들이 변경 집합에 들어가 clean_ownership 이 orphan 으로
    잡는다 — 픽스처가 검사 대상이 되어 버린다.
    """
    return tmp_path_factory.mktemp("gatefx")


@pytest.fixture
def gated(repo, phases, request_file):
    """04-gate 진입 직전까지 세팅된 런."""
    paths, s = st.create_run(repo, "sim", request_file)
    contract_path = repo / "_workspace" / "contract_sim.md"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(CONTRACT_MD, encoding="utf-8")
    s["contract"] = {"mode": "contract", "present": True,
                     "path": "_workspace/contract_sim.md"}
    st.set_phase_status(s, "01-plan", "passed")
    st.set_phase_status(s, "02-cross-verify", "passed")
    st.set_phase_status(s, "03-implement", "passed")
    s["phase"] = "04-gate"
    st.save(paths, s)
    return repo, paths, s


def _gate(repo, fixture, **kw):
    return cli.run_gate_cmd(repo, phase="04", replay=str(fixture), **kw)


class TestGateReplay:

    def test_replay_never_runs_a_stage_for_real(self, gated, fxdir, monkeypatch):
        """픽스처가 실물 러너를 부르면 replay 의 값이 사라진다.

        git 은 부른다(변경 집합·지문) — 막는 것은 **스테이지 실행**이다.
        """
        repo, paths, s = gated
        fx = make_fixture(fxdir, "all-pass", dict(ALL_PASS))
        monkeypatch.setattr(adapters, "_default_runner",
                            lambda *a, **k: pytest.fail("실물 러너를 불렀다"))
        env = _gate(repo, fx)
        assert env["exit"] in (0, 11), env["render"]

    def test_all_pass_grades_pass_with_gaps_for_absent_stages(self, gated, fxdir):
        """cmd:null 스테이지는 스킵으로 기록되고 등급에 반영된다."""
        repo, paths, s = gated
        fx = make_fixture(fxdir, "all-pass", dict(ALL_PASS))
        env = _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert "stage_absent:e2e" in report["gaps"]
        assert "stage_absent:docs" in report["gaps"]
        assert report["grade"] == "PASS_WITH_GAPS"

    def test_greenfield_zero_tests_is_not_a_green_light(self, gated, fxdir):
        """3단계 게이트 4번 — 빈 스위트는 통과하고, 통과는 초록불로 보인다."""
        repo, paths, s = gated
        fx = make_fixture(fxdir, "greenfield-zero-tests", dict(ALL_PASS), tests=0)
        env = _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert report["tests"]["ran"] == 0
        assert report["tests"]["status"] == "none"
        assert "tests_ran_zero" in report["gaps"]
        assert report["grade"] == "PASS_WITH_GAPS", "PASS 가 아니다"
        assert env["exit"] in (0, 11), "비차단이다 — 진행은 한다"

    def test_missing_report_is_infra_and_spends_no_counter(self, gated, fxdir):
        """리포트 경로 설정 오류일 수 있다. 구현 역할의 실패로 세지 않는다."""
        repo, paths, s = gated
        fx = make_fixture(fxdir, "no-report", dict(ALL_PASS), with_report=False)
        env = _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert report["tests"]["status"] == "none"
        assert "test_report_missing" in report["gaps"]
        _, after = st.load(repo, paths.run_id)
        assert not (after.get("counters") or {}).get("repair")

    def test_shrank_tests_block(self, gated, fxdir):
        """테스트가 삭제·스킵된 것을 잡는다."""
        repo, paths, s = gated
        fx = make_fixture(fxdir, "tests-shrank", dict(ALL_PASS), tests=100)
        env = _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert report["tests"]["status"] == "shrank"
        assert env["exit"] in (4, 5, 10)

    def test_infra_pattern_escalates_without_spending_the_counter(self, gated, fxdir):
        repo, paths, s = gated
        stages = dict(ALL_PASS, scoped={"exit": 1})
        fx = make_fixture(fxdir, "infra", stages,
                          stdouts={"scoped": "Error: connect ECONNREFUSED 127.0.0.1:5432\n"})
        env = _gate(repo, fx)
        assert env["exit"] == 10
        _, after = st.load(repo, paths.run_id)
        assert not (after.get("counters") or {}).get("repair"), "카운터를 소모하지 않는다"
        assert after["escalated"] is True

    def test_compile_symbol_error_goes_to_the_primary_role(self, gated, fxdir):
        repo, paths, s = gated
        stages = dict(ALL_PASS, compile={"exit": 2})
        log = ("src/lib/match.test.ts(3,10): error TS2305: "
               "Module './match' has no exported member 'matchTitle'.\n")
        fx = make_fixture(fxdir, "compile-symbol", stages, stdouts={"compile": log})
        env = _gate(repo, fx)
        assert env["exit"] == 4
        assert env["data"]["repair_dispatch"]["owner"] == "impl"

    def test_scoped_selector_is_a_path_not_a_test_name(self, gated, fxdir):
        """M16 — 이름 필터는 파일 수집을 줄이지 못한다."""
        repo, paths, s = gated
        fx = make_fixture(fxdir, "selector", dict(ALL_PASS))
        _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        scoped = next(x for x in report["stages"] if x["id"] == "scoped")
        assert scoped["selector"] == ["src/lib/match.test.ts"]
        assert scoped["selector_kind"] == "path"
        assert "matchTitle" not in json.dumps(scoped["selector"]), \
            "테스트 이름이 아니라 파일 경로다"

    def test_no_selector_skips_scoped_and_does_not_fall_back_to_full(self, gated, fxdir):
        repo, paths, s = gated
        (repo / "_workspace" / "contract_sim.md").write_text(
            CONTRACT_MD.replace("`lib/match.ts · matchTitle(a: string, b: string): number`",
                                "`없는파일.ts · nothing()`"), encoding="utf-8")
        fx = make_fixture(fxdir, "no-selector", dict(ALL_PASS),
                          with_contract=False)
        (fx / "contract.md").write_text(
            CONTRACT_MD.replace("`lib/match.ts · matchTitle(a: string, b: string): number`",
                                "`없는파일.ts · nothing()`"), encoding="utf-8")
        _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        scoped = next(x for x in report["stages"] if x["id"] == "scoped")
        assert scoped == {"id": "scoped", "state": "skipped", "reason": "no_selector"}
        assert "stage_no_selector:scoped" in report["gaps"]

    def test_when_touched_miss_is_recorded_as_skipped(self, gated, fxdir):
        repo, paths, s = gated
        fx = make_fixture(fxdir, "untouched", dict(ALL_PASS),
                          changed=["docs/TRD.md"])
        _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        build = next(x for x in report["stages"] if x["id"] == "build")
        assert build["reason"] == "not_touched"

    def test_same_signature_twice_escalates_even_with_budget_left(self, gated, fxdir):
        repo, paths, s = gated
        stages = dict(ALL_PASS, compile={"exit": 2})
        log = "src/lib/match.ts(9,3): error TS2322: Type mismatch.\n"
        fx = make_fixture(fxdir, "same-sig", stages, stdouts={"compile": log})
        first = _gate(repo, fx)
        assert first["exit"] == 4
        second = _gate(repo, fx)
        assert second["exit"] == 10, "동일 시그니처 2회면 예산이 남아도 멈춘다"

    def test_the_sig_chain_carries_the_owner(self, gated, fxdir):
        """M33 — 원장에 쌓이는 것이 시그니처가 아니라 `owner|sig` 쌍이다."""
        repo, paths, s = gated
        stages = dict(ALL_PASS, compile={"exit": 2})
        log = "src/lib/match.ts(9,3): error TS2322: Type mismatch.\n"
        fx = make_fixture(fxdir, "chain-owner", stages, stdouts={"compile": log})
        _gate(repo, fx)
        _, after = st.load(repo, paths.run_id)
        chain = after.get("sig_chain") or []
        assert chain and all(c.startswith("impl|") for c in chain), chain

    def test_single_stage_run_spends_no_counter_and_keeps_the_report(self, gated, fxdir):
        repo, paths, s = gated
        fx = make_fixture(fxdir, "single", dict(ALL_PASS))
        env = cli.run_gate_cmd(repo, phase="04", only_stage="compile",
                               replay=str(fx))
        assert env["exit"] == 0
        assert not (paths.run_dir / "04_gate_report.json").exists()
        _, after = st.load(repo, paths.run_id)
        assert not (after.get("counters") or {}).get("repair")

    def test_단일_스테이지_full_재실행이_테스트_수를_상태에_남긴다(self, gated,
                                                                   fxdir):
        """M55 — 수리 뒤 재게이트는 정본이 선언한 정상 경로다.

        `team-spec.md` §3.5: *"수리 후: `gate --stage scoped` → 전체 회귀 1회
        → 승인 알림."* 그 전체 회귀의 값이 상태에 안 실리면 08 보고서·PR
        체크리스트·세션 원장 셋이 전부 **마지막 코드 상태가 아닌 수**를
        증언한다. P7 이 `1423` 을 적었고 마지막 `full` 은 `1424` 를 돌았다.

        **그 회차의 영수증과 카운터는 그대로다** — `only_stage` 는 런을
        판정하지 않는 경로이고, 그 성질은 바로 위 테스트가 잠근다.
        """
        repo, paths, s = gated
        _gate(repo, make_fixture(fxdir, "regate-first", dict(ALL_PASS),
                                 tests=1300))
        _, mid = st.load(repo, paths.run_id)
        assert (mid.get("tests") or {}).get("ran") == 1300, mid.get("tests")

        fx = make_fixture(fxdir, "regate-full", dict(ALL_PASS), tests=1305)
        env = cli.run_gate_cmd(repo, phase="04", only_stage="full",
                               replay=str(fx))
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert (after.get("tests") or {}).get("ran") == 1305, after.get("tests")

        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert report["tests"]["ran"] == 1300, "그 회차의 영수증은 안 덮는다"
        assert not (after.get("counters") or {}).get("repair")

    def test_단일_스테이지_scoped_는_테스트_수를_건드리지_않는다(self, gated,
                                                                 fxdir):
        """`scoped` 는 전체 회귀가 아니다.

        그 수를 「몇 개 돌았나」로 적으면 다음 런의 하한 대조가 무의미해진다.
        `_tests_signal` 이 `full` 미실행에 `None` 을 내는 것이 그 규율이고,
        여기서 그것이 상태까지 지켜지는지 본다.
        """
        repo, paths, s = gated
        _gate(repo, make_fixture(fxdir, "scoped-first", dict(ALL_PASS),
                                 tests=1300))
        fx = make_fixture(fxdir, "scoped-only", dict(ALL_PASS), tests=9999)
        env = cli.run_gate_cmd(repo, phase="04", only_stage="scoped",
                               replay=str(fx))
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert (after.get("tests") or {}).get("ran") == 1300, after.get("tests")

    def test_uncalibrated_and_unverified_show_up_in_gaps(self, gated, fxdir):
        """미캘리브레이션·verified:false 가 조용히 통과하지 않는다."""
        repo, paths, s = gated
        (repo / "harness" / "calibration.json").unlink()
        fx = make_fixture(fxdir, "uncal", dict(ALL_PASS))
        _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert "uncalibrated_run" in report["gaps"]
        assert "adapter_unverified" in report["gaps"]

    def test_inactive_rules_are_named(self, gated, fxdir):
        """없는 것과 조용히 안 도는 것을 구분한다."""
        repo, paths, s = gated
        fx = make_fixture(fxdir, "inactive", dict(ALL_PASS))
        _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert any("migration" in r for r in report["rules_inactive"])


class TestCommittedFixtures:
    """디스크에 커밋된 픽스처 — 3단계 게이트가 이것으로 재현된다."""

    def test_greenfield_fixture_exists_and_is_tracked(self):
        fx = FIXTURES / "greenfield-zero-tests"
        assert (fx / "manifest.json").exists()
        assert (fx / "reports" / "junit" / "report.xml").exists(), \
            ".gitignore 의 reports/ 앵커가 이 파일을 삼키면 안 된다"

    def test_greenfield_fixture_reproduces_pass_with_gaps(self, gated):
        repo, paths, s = gated
        env = _gate(repo, FIXTURES / "greenfield-zero-tests")
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert report["grade"] == "PASS_WITH_GAPS"
        assert "tests_ran_zero" in report["gaps"]


class TestFlowCommands:
    """advance · retry · escalate · resume"""

    def test_advance_refuses_when_the_receipt_is_stale(self, gated, fxdir):
        """게이트 통과 뒤 소스가 바뀌면 막힌다 — 막히는 것이 정상 동작이다."""
        repo, paths, s = gated
        _gate(repo, make_fixture(fxdir, "adv", dict(ALL_PASS)))
        _, after = st.load(repo, paths.run_id)
        assert after.get("fingerprint"), "게이트가 영수증을 남겼어야 한다"

        (repo / "src" / "lib" / "match.ts").write_text("// 한 글자\n", encoding="utf-8")
        env = cli.run_advance(repo, "04", run_id=paths.run_id)
        assert env["exit"] == 6
        assert "낡았다" in env["render"]

    def test_advance_refuses_when_a_product_is_missing(self, gated):
        repo, paths, s = gated
        env = cli.run_advance(repo, "04", run_id=paths.run_id)
        assert env["exit"] == 6

    def test_retry_reopens_a_failed_phase(self, gated):
        repo, paths, s = gated
        st.set_phase_status(s, "04-gate", "failed")
        st.save(paths, s)
        env = cli.run_retry(repo, "04", "repair", "수리한다", run_id=paths.run_id)
        assert env["exit"] == 0
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "04-gate") == "running"

    def test_retry_escalates_at_the_limit(self, gated):
        repo, paths, s = gated
        for _ in range(3):
            env = cli.run_retry(repo, "04", "repair", "또", run_id=paths.run_id)
        assert env["exit"] == 7
        _, after = st.load(repo, paths.run_id)
        assert after["escalated"] is True
        assert paths.escalation.exists()

    def test_escalated_state_locks_every_command(self, gated):
        repo, paths, s = gated
        cli.run_escalate(repo, "사람 판단", run_id=paths.run_id)
        for env in (cli.run_next(repo, paths.run_id),
                    cli.run_gate_cmd(repo, run_id=paths.run_id)):
            assert env["exit"] == 10, env["cmd"]

    def test_resume_needs_an_explicit_ack(self, gated):
        repo, paths, s = gated
        cli.run_escalate(repo, "사람 판단", run_id=paths.run_id)
        assert cli.run_resume(repo, ack=False, run_id=paths.run_id)["exit"] == 2
        env = cli.run_resume(repo, ack=True, run_id=paths.run_id)
        assert env["exit"] == 0
        _, after = st.load(repo, paths.run_id)
        assert after["escalated"] is False

    def test_resume_is_not_the_session_recovery_path(self, gated):
        """잠기지 않은 런에는 아무것도 하지 않고 next 를 가리킨다."""
        repo, paths, s = gated
        env = cli.run_resume(repo, ack=True, run_id=paths.run_id)
        assert env["exit"] == 0
        assert "next --run-id" in (env["next_command"] or "")


# ---------------------------------------------------------------------------
# I. 3단계 게이트 잠금 (ADR-H013)
# ---------------------------------------------------------------------------

CORE_GLOBS = [
    "scripts/pipeline/*.py",
    "harness/phases/*.md",
    "harness/templates/*.md",
    ".claude/commands/*.md",
    ".claude/agents/*.md",
]


def _banned_words():
    """금지어 목록을 **team-spec 에서 읽어 온다.**

    테스트에 복사하면 정본이 늘어날 때 이 검사가 조용히 뒤처진다.
    """
    text = (ROOT / "docs" / "harness" / "pipeline" / "team-spec.md").read_text(
        encoding="utf-8")
    m = re.search(r"검수 기준.*?```\n(.*?)```", text, re.S)
    assert m, "team-spec 0.2 의 금지어 블록을 찾지 못했다"
    return sorted(set(m.group(1).split()))


class TestPromotionGate:
    """ADR-H013 이 다섯 줄로 재정의한 3단계 게이트."""

    def test_gate_1_no_stack_proper_nouns_in_the_core(self):
        banned = _banned_words()
        hits = []
        for pattern in CORE_GLOBS:
            for path in sorted(ROOT.glob(pattern)):
                text = path.read_text(encoding="utf-8").lower()
                for word in banned:
                    if word in text:
                        hits.append("%s: %s" % (path.relative_to(ROOT), word))
        assert hits == [], "코어에 스택 고유명사가 박혔다: %s" % hits

    def test_gate_1_covers_every_core_file(self):
        """glob 이 실제로 파일을 잡는지 — 0건 통과가 '검사 안 함'이면 안 된다."""
        for pattern in CORE_GLOBS:
            assert list(ROOT.glob(pattern)), "이 glob 이 아무 파일도 안 잡는다: %s" % pattern

    def test_gate_2_swapping_the_adapter_leaves_the_core_untouched(self, repo, phases,
                                                                   fxdir, request_file):
        """어댑터만 바꿔 lint-phases 와 gate --replay 가 통과하는가."""
        second = repo / "harness" / "adapters" / "other.json"
        base = harness._read_json(repo / "harness" / "adapters" / "nextjs-ts.json")
        base["id"] = "other"
        base["runner"] = {"bin": "make", "common_args": [], "cwd": "."}
        base["stages"] = {k: ({"cmd": ["run", k]} if v.get("cmd") else {"cmd": None})
                          for k, v in base["stages"].items()}
        base["stages"]["scoped"]["loop_stage"] = True
        base["stages"]["full"]["once_after_loop"] = True
        second.write_text(json.dumps(base, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        cfg_path = repo / "harness" / "config.json"
        cfg = harness._read_json(cfg_path)
        cfg["adapter"] = "other"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")

        findings = cli.lint_phases(repo)
        assert [f for f in findings if f["status"] == "FAIL"] == []

    def test_gate_3_swapping_the_language_leaves_the_core_untouched(self, repo, phases):
        """절 제목과 언어를 바꿔도 코어·페이즈 파일을 손대지 않는다."""
        cfg_path = repo / "harness" / "config.json"
        cfg = harness._read_json(cfg_path)
        cfg["project"]["language"] = "en"
        cfg["contract"]["sections"] = {
            "units": "## Units", "entrypoints": "## Entrypoints",
            "errors": "## Errors", "schema": "## Schema",
            "boundaries": "## Boundaries"}
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        tpl = repo / "harness" / "templates" / "contract.md"
        text = tpl.read_text(encoding="utf-8")
        for ko, en in [("## 유닛", "## Units"), ("## 진입점", "## Entrypoints"),
                       ("## 오류 어휘", "## Errors"),
                       ("## 스키마·데이터 변경", "## Schema"),
                       ("## 외부 경계", "## Boundaries")]:
            text = text.replace(ko, en)
        tpl.write_text(text, encoding="utf-8")

        findings = cli.lint_phases(repo)
        assert [f for f in findings if f["status"] == "FAIL"] == []
        parsed = contract_mod.parse(text, harness._read_json(cfg_path))
        assert parsed["units"], "절 제목이 바뀌어도 파서가 찾는다"

    def test_the_adapter_stays_unverified_until_a_real_run(self):
        """픽스처는 내가 만든 출력이지 진짜 러너 출력이 아니다."""
        adapter = harness._read_json(ROOT / "harness/adapters/nextjs-ts.json")
        assert adapter["verified"] is False
        assert "_unconsumed" in adapter["attribution"]


# ---------------------------------------------------------------------------
# J  세션 원장 — SessionEnd 훅이 사실만 쌓는다
# ---------------------------------------------------------------------------

import io  # noqa: E402
import time  # noqa: E402

import session_log as sl  # noqa: E402

HOOK_IN = {"session_id": "sid-1", "transcript_path": "", "cwd": ".",
           "hook_event_name": "SessionEnd", "reason": "clear"}


def _ledger_lines(root):
    p = Path(root) / sl.LEDGER_REL
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestRunCost:
    """런 비용은 **읽는 시점**에 원장 + 트랜스크립트로 집계한다.

    훅에서 못 한다: `cost-state` 는 트랜스크립트의 **마지막 줄**로 써지고
    그것은 `SessionEnd` 훅보다 늦다 — 세션은 자기 비용을 영원히 못 적는다.
    반대로 읽는 시점에는 잘 된다: 실물 원장 46줄 중 트랜스크립트가 남은
    32줄은 **100%** 그 레코드를 갖고 있다.

    그리고 **`run_id` 만으로 합산하면 안 된다.** `session_log._latest_run` 이
    가장 최근 런 디렉터리를 무조건 집으므로, 런이 닫힌 뒤 시작한 세션도 그
    `run_id` 를 단다 (M59). 실물 원장에 그 두 줄이 나란히 있다.
    """

    def _ledger(self, repo, rows):
        p = repo / "docs" / "pipeline-ledger.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                             for r in rows), encoding="utf-8")
        return p

    def _transcript(self, root, sid, usd):
        d = root / "C--slug"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.jsonl" % sid)).write_text(
            json.dumps({"type": "cost-state", "totalCostUSD": usd,
                        "totalDuration": 1000, "hasUnknownModelCost": False,
                        "modelUsage": {"m": {"inputTokens": 1, "outputTokens": 2,
                                             "thinkingTokens": 0,
                                             "cacheReadInputTokens": 3,
                                             "cacheCreationInputTokens": 4,
                                             "costUSD": usd}}},
                       ensure_ascii=False) + "\n", encoding="utf-8")

    def _run(self, repo, request_file, updated_at,
             created_at="2026-09-08T17:20:01+0900"):
        """런은 **구간**을 갖는다. P8 은 17:20 에 시작해 다음날 01:08 에 닫혔다."""
        paths, s = st.create_run(repo, "demo", request_file)
        s["created_at"] = created_at
        s["updated_at"] = updated_at
        st._write_json(paths.state, s)
        return paths.run_id

    def test_런을_만진_세션만_합산한다(self, repo, request_file, tmp_path):
        """`touched` 는 세션 창 안에 런의 `updated_at` 이 있다는 뜻이다."""
        rid = self._run(repo, request_file, "2026-09-09T01:08:15+0900")
        self._ledger(repo, [
            {"ts": "2026-09-08T16:51:21+0900", "session_id": "s0"},
            {"ts": "2026-09-09T09:05:38+0900", "session_id": "s1",
             "run": {"run_id": rid}},
            {"ts": "2026-09-09T10:21:20+0900", "session_id": "s2",
             "run": {"run_id": rid}},
        ])
        troot = tmp_path / "projects"
        self._transcript(troot, "s1", 4.34)
        self._transcript(troot, "s2", 20.29)

        out = cli.run_cost(repo, run_id=rid, transcript_root=troot)
        assert out["ok"] is True
        d = out["data"]
        assert d["cost_usd"] == 4.34, "닫힌 뒤 시작한 s2 는 빠진다"
        assert [x["session_id"] for x in d["sessions"] if x["basis"] == "touched"] \
            == ["s1"]
        assert [x["session_id"] for x in d["sessions"]
                if x["basis"] == "latest_only"] == ["s2"]

    def test_여러_세션에_걸친_런은_앞_세션도_합산한다(self, repo, request_file,
                                                      tmp_path):
        """**P8 의 실제 모양이다** — 17:20 에 시작해 다음날 01:08 에 닫혔고
        세션 둘이 걸쳐 있다. `updated_at` 한 시점만 보면 앞 세션이 통째로 빠진다.
        """
        rid = self._run(repo, request_file, "2026-09-09T01:08:15+0900",
                        created_at="2026-09-08T17:20:01+0900")
        self._ledger(repo, [
            {"ts": "2026-09-08T16:51:21+0900", "session_id": "s0"},
            # 17:20~24:00 을 담당한 세션.
            {"ts": "2026-09-08T23:00:00+0900", "session_id": "early",
             "run": {"run_id": rid}},
            # 00:00~01:08 을 담당하고 런을 닫은 세션.
            {"ts": "2026-09-09T09:05:38+0900", "session_id": "late",
             "run": {"run_id": rid}},
        ])
        troot = tmp_path / "projects"
        self._transcript(troot, "early", 10.0)
        self._transcript(troot, "late", 4.0)
        out = cli.run_cost(repo, run_id=rid, transcript_root=troot)
        assert out["data"]["cost_usd"] == 14.0
        assert [x["basis"] for x in out["data"]["sessions"]] == \
            ["touched", "touched"]

    def test_latest_only_임을_봉투가_말한다(self, repo, request_file, tmp_path):
        """뺀 것을 조용히 빼지 않는다 — 왜 뺐는지가 화면에 남아야 한다."""
        rid = self._run(repo, request_file, "2026-09-09T01:08:15+0900")
        self._ledger(repo, [
            {"ts": "2026-09-09T09:05:38+0900", "session_id": "s1",
             "run": {"run_id": rid}},
            {"ts": "2026-09-09T10:21:20+0900", "session_id": "s2",
             "run": {"run_id": rid}},
        ])
        troot = tmp_path / "projects"
        self._transcript(troot, "s1", 1.0)
        self._transcript(troot, "s2", 2.0)
        out = cli.run_cost(repo, run_id=rid, transcript_root=troot)
        assert "latest_only" in out["render"]

    def test_훅이_적은_basis_와_읽는_시점_판정이_일치한다(self, repo, request_file,
                                                          tmp_path):
        """M59 — 쓰는 쪽(`session_log`)과 읽는 쪽(`cost-state`)이 **한 함수**를
        쓴다. 같은 식을 각자 쓰면 갈라지고, 그때 원장이 자기와 모순된다.
        """
        rid = self._run(repo, request_file, "2026-09-09T01:08:15+0900")
        sl.append(repo, {"ts": "2026-09-08T16:51:21+0900", "session_id": "s0"})
        for sid, ts in (("s1", "2026-09-09T09:05:38+0900"),
                        ("s2", "2026-09-09T10:21:20+0900")):
            rec = sl.collect(repo, dict(HOOK_IN, session_id=sid),
                             now=st._parse_stamp(ts))
            sl.append(repo, rec)
        troot = tmp_path / "projects"
        self._transcript(troot, "s1", 4.34)
        self._transcript(troot, "s2", 20.29)

        out = cli.run_cost(repo, run_id=rid, transcript_root=troot)
        written = {r["session_id"]: (r.get("run") or {}).get("basis")
                   for r in _ledger_lines(repo)
                   if r.get("session_id") in ("s1", "s2")}
        read = {x["session_id"]: x.get("basis")
                for x in out["data"]["sessions"]}
        assert written == {"s1": "touched", "s2": "latest_only"}
        assert read == written

    def test_트랜스크립트가_없는_세션은_수로_적힌다(self, repo, request_file,
                                                    tmp_path):
        """빠진 것을 세지 않으면 합계가 얼마나 모자란지 알 수 없다."""
        rid = self._run(repo, request_file, "2026-09-09T01:08:15+0900")
        self._ledger(repo, [
            # 둘 다 런 구간(17:20~01:08)과 겹친다. 뒤엣것만 트랜스크립트가 없다.
            {"ts": "2026-09-08T23:00:00+0900", "session_id": "gone",
             "run": {"run_id": rid}},
            {"ts": "2026-09-09T00:30:00+0900", "session_id": "s1",
             "run": {"run_id": rid}},
        ])
        troot = tmp_path / "projects"
        self._transcript(troot, "s1", 1.25)
        out = cli.run_cost(repo, run_id=rid, transcript_root=troot)
        assert out["data"]["cost_usd"] == 1.25
        assert out["data"]["unread_sessions"] == 1
        assert "1" in out["render"]

    def test_읽은_세션이_하나도_없으면_합계를_주장하지_않는다(
            self, repo, request_file, tmp_path):
        """0 달러와 '못 읽었다' 는 다른 것이다 (ADR-H007)."""
        rid = self._run(repo, request_file, "2026-09-09T01:08:15+0900")
        self._ledger(repo, [
            {"ts": "2026-09-09T09:05:38+0900", "session_id": "gone",
             "run": {"run_id": rid}},
        ])
        out = cli.run_cost(repo, run_id=rid, transcript_root=tmp_path / "없음")
        assert "cost_usd" not in out["data"]
        assert out["data"]["unread_sessions"] == 1

    def test_사각을_봉투가_그대로_인쇄한다(self, repo, request_file, tmp_path):
        """`BUDGET_BLIND_SPOTS` 와 같은 규율 — 양방향으로 틀리므로
        "하한" 이라고 부르지 않는다."""
        rid = self._run(repo, request_file, "2026-09-09T01:08:15+0900")
        self._ledger(repo, [
            {"ts": "2026-09-09T09:05:38+0900", "session_id": "s1",
             "run": {"run_id": rid}},
        ])
        troot = tmp_path / "projects"
        self._transcript(troot, "s1", 3.0)
        out = cli.run_cost(repo, run_id=rid, transcript_root=troot)
        for spot in cli.COST_BLIND_SPOTS:
            assert spot in out["render"], spot

    def test_원장에_그_런이_없으면_exit_3(self, repo, request_file, tmp_path):
        rid = self._run(repo, request_file, "2026-09-09T01:08:15+0900")
        self._ledger(repo, [{"ts": "2026-09-09T09:05:38+0900",
                             "session_id": "s1"}])
        out = cli.run_cost(repo, run_id=rid, transcript_root=tmp_path)
        assert out["exit"] == 3

    def test_토큰도_같이_나온다(self, repo, request_file, tmp_path):
        rid = self._run(repo, request_file, "2026-09-09T01:08:15+0900")
        self._ledger(repo, [
            {"ts": "2026-09-09T09:05:38+0900", "session_id": "s1",
             "run": {"run_id": rid}},
            {"ts": "2026-09-09T09:40:00+0900", "session_id": "s1b",
             "run": {"run_id": rid}},
        ])
        troot = tmp_path / "projects"
        self._transcript(troot, "s1", 1.0)
        self._transcript(troot, "s1b", 2.0)
        out = cli.run_cost(repo, run_id=rid, transcript_root=troot)
        # 창이 [09:05, 09:40] 인 s1b 는 런 구간(~01:08)과 안 겹친다.
        assert out["data"]["cost_usd"] == 1.0
        assert out["data"]["output_tokens"] == 2


class TestSessionLedger:
    """훅은 셸이라 해석을 못 쓴다. **그래서 사실만 쌓는다.**

    거짓말할 수 없는 부분(커밋·변경량·런 등급)과 해석을 갈라 두지 않으면
    검증하는 사람 없이 문서에 추측이 쌓인다.
    """

    def test_broken_stdin_still_exits_zero_and_records_error(self, repo):
        """세션 종료를 막으면 안 되고, 실패가 조용히 사라져도 안 된다."""
        code = sl.main(["--from-hook"], stdin=io.StringIO("이건 JSON 이 아니다"),
                       root=repo)
        assert code == 0
        rows = _ledger_lines(repo)
        assert len(rows) == 1
        assert "error" in rows[0]
        # 실패와 "기록할 게 없음"이 같은 모양이면 안 된다.
        assert "commits" not in rows[0]

    def test_missing_transcript_omits_session_key(self, repo):
        """0 으로 채우면 '안 쟀다'가 사라진다 (ADR-H007)."""
        rec = sl.collect(repo, HOOK_IN, transcript_root=repo / "없는곳")
        assert "session" not in rec

    def test_transcript_metrics_are_carried_when_present(self, repo, tmp_path):
        troot = tmp_path / "projects"
        (troot / "slug").mkdir(parents=True)
        (troot / "slug" / "sid-1.jsonl").write_text(
            json.dumps({"message": {"content": [
                {"type": "tool_result", "content": "가나다"}]}},
                ensure_ascii=False) + "\n", encoding="utf-8")
        rec = sl.collect(repo, HOOK_IN, transcript_root=troot)
        assert rec["session"]["tool_result_chars"] == 3

    def test_hangul_commit_subject_survives(self, repo):
        (repo / "src" / "lib" / "새파일.ts").write_text("export const a = 1\n",
                                                     encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "feat(파이프라인): 한글 제목 — em dash 포함")
        sl.main(["--from-hook"], stdin=io.StringIO(json.dumps(HOOK_IN)), root=repo)
        row = _ledger_lines(repo)[0]
        subjects = [c["subject"] for c in row["commits"]]
        assert any("한글 제목 — em dash" in s for s in subjects)

    def test_no_workspace_runs_omits_run_key(self, repo):
        """파이프라인을 안 돌린 세션이 정상 경로다."""
        rec = sl.collect(repo, HOOK_IN)
        assert "run" not in rec

    def test_latest_run_is_carried(self, repo):
        d = repo / "_workspace" / "runs" / "20260903-1220-d9c0"
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({
            "run_id": "20260903-1220-d9c0", "grade": "PASS_WITH_GAPS",
            "gaps": ["stage_absent:e2e"], "counters": {"round": {"used": 3}},
            "budget": {"model_calls": {"total": 10}},
            "tests": {"ran": 1333}}, ensure_ascii=False), encoding="utf-8")
        rec = sl.collect(repo, HOOK_IN)
        assert rec["run"]["grade"] == "PASS_WITH_GAPS"
        assert rec["run"]["rounds"] == 3
        assert rec["run"]["model_calls"] == 10
        assert rec["tests"]["app"] == 1333

    def _run_dir(self, repo, rid, created_at, updated_at):
        """런은 **구간**을 갖는다 — `[created_at, updated_at]`."""
        d = repo / "_workspace" / "runs" / rid
        d.mkdir(parents=True, exist_ok=True)
        body = {"run_id": rid, "grade": "PASS_WITH_GAPS",
                "gaps": ["stage_absent:e2e"],
                "counters": {"round": {"used": 9}},
                "budget": {"model_calls": {"total": 15}},
                "tests": {"ran": 1459}}
        if created_at is not None:
            body["created_at"] = created_at
        if updated_at is not None:
            body["updated_at"] = updated_at
        (d / "state.json").write_text(json.dumps(body, ensure_ascii=False),
                                      encoding="utf-8")
        return rid

    def test_안_만진_런을_자기_것이라_적지_않는다(self, repo):
        """M59 — `_latest_run` 이 가장 최근 런 디렉터리를 **무조건** 집는다.

        실물 원장에 그 줄이 있다: P8(`20260908-1720-dca1`)은 2026-09-09
        01:08 에 닫혔는데 09-10 13:12 에 끝난 C5 세션이 그 `run_id` 를 달았고,
        그 세션은 그 런을 만진 적이 없다.

        **침묵으로 지우지 않는다.** `run` 칸을 통째로 빼면 "파이프라인 런이
        아예 없던 세션" 과 원장에서 구분되지 않는다 — 두 사실을 같은 침묵으로
        뭉개는 것이 이 리포가 반복해서 잡아 온 모양이다. 그래서 `run_id` 와
        `basis` 만 남기고, 등급·라운드처럼 **그 런의 사실**은 안 싣는다.
        """
        self._run_dir(repo, "20260908-1720-dca1",
                      "2026-09-08T17:20:01+0900", "2026-09-09T01:08:15+0900")
        sl.append(repo, {"ts": "2026-09-10T00:39:15+0900", "session_id": "앞"})
        rec = sl.collect(repo, HOOK_IN,
                         now=st._parse_stamp("2026-09-10T13:12:07+0900"))
        assert rec["run"]["basis"] == "latest_only"
        assert rec["run"]["run_id"] == "20260908-1720-dca1"
        assert "grade" not in rec["run"], rec["run"]
        assert "rounds" not in rec["run"], rec["run"]

    def test_만진_런은_touched_로_그대로_실린다(self, repo):
        """겹침 판정이 참인 경로에서는 기존 칸이 한 개도 안 줄어야 한다."""
        self._run_dir(repo, "20260908-1720-dca1",
                      "2026-09-08T17:20:01+0900", "2026-09-09T01:08:15+0900")
        sl.append(repo, {"ts": "2026-09-08T16:51:21+0900", "session_id": "앞"})
        rec = sl.collect(repo, HOOK_IN,
                         now=st._parse_stamp("2026-09-09T09:05:38+0900"))
        assert rec["run"]["basis"] == "touched"
        assert rec["run"]["grade"] == "PASS_WITH_GAPS"
        assert rec["run"]["rounds"] == 9
        assert rec["run"]["model_calls"] == 15
        assert rec["tests"] == {"app": 1459, "source": "run_state"}

    def test_원장_첫_줄은_창이_열려_있다(self, repo):
        """직전 줄이 없는 것은 **판정 불가가 아니다** — 앞 경계가 없을 뿐이다.

        `cost-state` 의 `start is None` 분기와 같은 자리다.
        """
        self._run_dir(repo, "20260908-1720-dca1",
                      "2026-09-08T17:20:01+0900", "2026-09-09T01:08:15+0900")
        rec = sl.collect(repo, HOOK_IN,
                         now=st._parse_stamp("2026-09-09T09:05:38+0900"))
        assert rec["run"]["basis"] == "touched"

    def test_구간을_모르는_런은_단정하지_않는다(self, repo):
        """`created_at`·`updated_at` 이 없으면 `basis` 키를 **안 만든다.**

        `latest_only` 로 단정하면 못 잰 것이 "무관하다" 는 주장으로 바뀐다
        (`cli.py` 의 같은 자리가 같은 이유를 적고 있다 · ADR-H007).
        """
        self._run_dir(repo, "20260903-1220-d9c0", None, None)
        rec = sl.collect(repo, HOOK_IN)
        assert "basis" not in rec["run"], rec["run"]
        assert rec["run"]["grade"] == "PASS_WITH_GAPS"
        assert rec["tests"]["app"] == 1459

    def test_안_만진_런의_테스트_수를_자기_것이라_적지_않는다(self, repo):
        """`tests` 칸도 같이 오염됐다 — 실물 51번째 줄이 1459 를 실었는데
        그 세션(C5)의 실제 앱 테스트는 1,460 이다. `latest_only` 면
        `calibration.json` 으로 떨어지고 `source` 가 그 사실을 말한다."""
        self._run_dir(repo, "20260908-1720-dca1",
                      "2026-09-08T17:20:01+0900", "2026-09-09T01:08:15+0900")
        cal = repo / "harness" / "calibration.json"
        cal.parent.mkdir(parents=True, exist_ok=True)
        cal.write_text(json.dumps({"stages": {"full": {"tests_ran": 1460}}}),
                       encoding="utf-8")
        sl.append(repo, {"ts": "2026-09-10T00:39:15+0900", "session_id": "앞"})
        rec = sl.collect(repo, HOOK_IN,
                         now=st._parse_stamp("2026-09-10T13:12:07+0900"))
        assert rec["tests"] == {"app": 1460, "source": "calibration"}

    def test_append_only_keeps_existing_lines(self, repo):
        sl.append(repo, {"ts": "t1", "marker": "먼저"})
        sl.main(["--from-hook"], stdin=io.StringIO(json.dumps(HOOK_IN)), root=repo)
        rows = _ledger_lines(repo)
        assert len(rows) == 2
        assert rows[0]["marker"] == "먼저"      # 기존 줄은 손대지 않는다

    def test_finds_root_from_subdirectory(self, repo, monkeypatch):
        """훅은 하위 디렉터리에서 돌 수 있다."""
        sub = repo / "src" / "lib"
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        assert sl.find_root({"cwd": str(sub)}, env={}) == repo

    def test_env_project_dir_wins_over_cwd(self, repo, tmp_path):
        other = tmp_path / "다른곳"
        other.mkdir()
        found = sl.find_root({"cwd": str(other)},
                             env={"CLAUDE_PROJECT_DIR": str(repo)})
        assert found == repo

    def test_uncommitted_change_is_counted(self, repo):
        (repo / "src" / "lib" / "match.ts").write_text("export const b = 2\n",
                                                       encoding="utf-8")
        rec = sl.collect(repo, HOOK_IN)
        assert rec["uncommitted"]["files"] >= 1
        assert rec["dirty"] is True

    def test_untracked_file_is_not_lost(self, repo):
        """`git diff` 는 새 파일을 안 센다 — 세면 안 되는 게 아니라 못 보는 것이다.

        새 파일이 안 세지면 "파일 3개 바뀜"이 사실보다 작게 적히고, 그 숫자를
        나중에 근거로 쓴다.
        """
        (repo / "src" / "lib" / "새것.ts").write_text("export const c = 3\n",
                                                    encoding="utf-8")
        rec = sl.collect(repo, HOOK_IN)
        assert rec["uncommitted"]["untracked"] == 1

    def test_gitignored_file_is_not_counted_as_untracked(self, repo):
        (repo / "_workspace").mkdir()
        (repo / "_workspace" / "임시.txt").write_text("x", encoding="utf-8")
        rec = sl.collect(repo, HOOK_IN)
        assert "uncommitted" not in rec

    def test_collect_fits_the_sessionend_budget(self, repo):
        """SessionEnd 예산을 넘기면 기록이 통째로 버려진다."""
        started = time.time()
        sl.collect(repo, HOOK_IN)
        assert time.time() - started < 2.0


# ---------------------------------------------------------------------------
# J. 규칙 원장 — findings.jsonl · taxonomy.json · staged 승격
# ---------------------------------------------------------------------------

import ledger as ldg  # noqa: E402


def _finding(category="NAMING", severity="major", role="impl", title="제목",
             resolution="deferred", source="reviewer", reported_by=None,
             rule_slug=None):
    out = {"category": category, "severity": severity, "target_role": role,
           "title": title, "resolution": resolution, "source": source,
           "reported_by": reported_by or ["arch"]}
    if rule_slug is not None:
        out["rule_slug"] = rule_slug
    return out


class TestLedgerTaxonomy:
    """이 파일 하나가 원장 어휘 · 승격 목적지 · 리뷰 범위 셋의 단일 출처다.

    그래서 손상되면 셋이 동시에 조용히 틀어진다 — lint-phases 가 잡아야 한다.
    """

    def test_seed_creates_the_three_ledger_files(self, repo):
        ldg.seed(repo)
        assert (repo / ldg.TAXONOMY_REL).exists()
        assert (repo / ldg.FINDINGS_REL).exists()
        assert (repo / ldg.CHANGELOG_REL).exists()

    def test_seed_is_idempotent_and_never_overwrites(self, repo):
        ldg.seed(repo)
        ldg.append(repo, "r1", "05", [_finding()])
        ldg.seed(repo)
        assert len(ldg.read_all(repo)) == 1     # 시드가 원장을 지우지 않는다

    def test_seed_passes_its_own_validator(self, repo):
        """템플릿이 자기 파서를 통과해야 하는 것과 같은 규율이다."""
        ldg.seed(repo)
        assert ldg.validate_taxonomy(ldg.load_taxonomy(repo)) == []

    def test_excluded_categories_are_active_and_machine_enforced(self, repo):
        """active + enforceable != prose 인 것만 05 의 검토 제외 목록이다."""
        ldg.seed(repo)
        excluded = ldg.excluded_categories(repo)
        cats = ldg.categories(repo)
        assert excluded, "시드에 기계 강제 가능한 active 항목이 하나는 있어야 한다"
        for code in excluded:
            assert cats[code]["status"] == "active"
            assert cats[code]["enforceable"] in ("lint", "check")
        # prose 는 기계가 못 막으므로 리뷰 범위에서 빼면 안 된다.
        assert not any(cats[c]["enforceable"] == "prose" for c in excluded)

    def test_duplicate_code_is_rejected(self, repo):
        data = {"version": 1, "categories": [
            {"code": "NAMING", "enforceable": "lint", "rule": "r", "status": "active"},
            {"code": "NAMING", "enforceable": "prose", "status": "active"}]}
        errs = ldg.validate_taxonomy(data)
        assert any("유니크" in e for e in errs)

    def test_unknown_enforceable_vocabulary_is_rejected(self, repo):
        data = {"version": 1, "categories": [
            {"code": "X", "enforceable": "archunit", "status": "active"}]}
        assert any("enforceable" in e for e in ldg.validate_taxonomy(data))

    def test_unknown_status_vocabulary_is_rejected(self, repo):
        data = {"version": 1, "categories": [
            {"code": "X", "enforceable": "prose", "status": "켜짐"}]}
        assert any("status" in e for e in ldg.validate_taxonomy(data))

    def test_lint_or_check_category_without_rule_is_rejected(self, repo):
        """규칙 참조가 없으면 '어디에 승격할지'를 아무도 모른다."""
        data = {"version": 1, "categories": [
            {"code": "X", "enforceable": "lint", "status": "active"}]}
        assert any("rule" in e for e in ldg.validate_taxonomy(data))


class TestLedgerFindings:
    """append-only. 집계 파일을 두지 않고 매번 재계산한다."""

    def test_finding_key_ignores_path(self, repo):
        """'동일 유형'은 파일을 가로질러야 의미가 있다."""
        a = dict(_finding(), path="src/a.ts")
        b = dict(_finding(), path="src/b.ts")
        assert ldg.finding_key(a) == ldg.finding_key(b)

    def test_finding_key_separates_role(self, repo):
        assert ldg.finding_key(_finding(role="impl")) != ldg.finding_key(_finding(role="test"))

    def test_append_writes_one_line_per_finding(self, repo):
        ldg.seed(repo)
        ldg.append(repo, "r1", "05", [_finding(title="가"), _finding(title="나")])
        rows = ldg.read_all(repo)
        assert len(rows) == 2
        assert {r["run_id"] for r in rows} == {"r1"}
        assert all(r["phase"] == "05" for r in rows)

    def test_append_preserves_hangul_without_escaping(self, repo):
        ldg.seed(repo)
        ldg.append(repo, "r1", "05", [_finding(title="한글 제목 — em dash")])
        raw = (repo / ldg.FINDINGS_REL).read_text(encoding="utf-8")
        assert "한글 제목 — em dash" in raw     # ensure_ascii=False 여야 한다

    def test_append_is_append_only(self, repo):
        ldg.seed(repo)
        ldg.append(repo, "r1", "05", [_finding(title="먼저")])
        ldg.append(repo, "r2", "05", [_finding(title="나중")])
        rows = ldg.read_all(repo)
        assert [r["title_norm"] for r in rows] == ["먼저", "나중"]

    def test_unknown_category_is_rejected_not_silently_written(self, repo):
        ldg.seed(repo)
        with pytest.raises(ValueError):
            ldg.append(repo, "r1", "05", [_finding(category="아무거나")])

    def test_unknown_resolution_is_rejected(self, repo):
        ldg.seed(repo)
        with pytest.raises(ValueError):
            ldg.append(repo, "r1", "05", [_finding(resolution="고쳤음")])


class TestLedgerIdentity:
    """원장 행의 신원은 `(run_id, phase, finding_key)` 이고 갱신은 **승계**다.

    파일은 append-only 그대로다 — 그 성질이 tracked 파일의 머지를 자명한
    union 으로 만든다. 가변성은 쓰기가 아니라 **읽기**로 옮긴다.
    """

    def _row(self, **kw):
        d = {"category": "NAMING", "severity": "major", "target_role": "impl",
             "title": "이름이 규약을 벗어난다", "resolution": "deferred",
             "source": "reviewer"}
        d.update(kw)
        return d

    def test_같은_런_같은_페이즈의_같은_키는_한_번만_세어진다(self, repo):
        """M30 — 라운드마다 한 줄씩 쌓이면 `count` 축이 무력해진다."""
        ldg.seed(repo)
        for _ in range(3):
            ldg.append(repo, "R1", "05", [self._row()])
        obs = ldg.observations(repo)
        assert len(obs) == 1
        assert obs[0]["run_id"] == "R1"

    def test_05_와_07_은_같은_런에서도_따로_센다(self, repo):
        """신원에 phase 를 넣지 않으면 `count` 가 `distinct_runs` 를 흡수한다."""
        ldg.seed(repo)
        ldg.append(repo, "R1", "05", [self._row()])
        ldg.append(repo, "R1", "07", [self._row()])
        assert len(ldg.observations(repo)) == 2

    def test_마지막_행이_이긴다(self, repo):
        """M29 — 승계. 뒤에 온 `repaired` 가 앞의 `deferred` 를 대신한다."""
        ldg.seed(repo)
        ldg.append(repo, "R1", "05", [self._row()])
        ldg.append(repo, "R1", "05", [self._row(resolution="repaired",
                                                repaired_by="main")])
        obs = ldg.observations(repo)
        assert len(obs) == 1
        assert obs[0]["resolution"] == "repaired"
        assert obs[0]["repaired_by"] == "main"

    def test_severity_는_최대로_접힌다(self, repo):
        ldg.seed(repo)
        ldg.append(repo, "R1", "05", [self._row(severity="critical")])
        ldg.append(repo, "R1", "05", [self._row(severity="minor")])
        assert ldg.observations(repo)[0]["severity"] == "critical"

    def test_원장_파일은_다시_쓰이지_않는다(self, repo):
        """append-only 잠금 — 이전 바이트가 접두사로 남아야 한다."""
        ldg.seed(repo)
        p = repo / ldg.FINDINGS_REL
        ldg.append(repo, "R1", "05", [self._row()])
        before = p.read_bytes()
        ldg.append(repo, "R1", "05", [self._row(resolution="repaired")])
        assert p.read_bytes().startswith(before)

    def test_read_all_은_승계_행을_전부_보존한다(self, repo):
        """감사 이력이 사라지지 않는다 — 접기는 읽기에서만 일어난다."""
        ldg.seed(repo)
        ldg.append(repo, "R1", "05", [self._row()])
        ldg.append(repo, "R1", "05", [self._row(resolution="repaired")])
        assert len(ldg.read_all(repo)) == 2

    def test_승격_집계가_라운드_반복에_속지_않는다(self, repo):
        """major 임계는 3회/2런이다. 한 런의 3라운드로 채워지면 안 된다."""
        ldg.seed(repo)
        for _ in range(3):
            ldg.append(repo, "R1", "05", [self._row()])
        got = ldg.stage_promotions(repo)
        assert got["candidates"] == []
        keys = [b["count"] for b in got["held"]] or [0]
        assert max(keys) <= 1, "한 런은 한 번이다"


class TestLedgerBaseline:
    """untested_contract_item 의 baseline 기간 판정 (§E6)."""

    def test_empty_ledger_is_in_baseline(self, repo):
        ldg.seed(repo)
        assert ldg.distinct_runs(repo) == 0
        assert ldg.in_baseline(repo, 3) is True

    def test_baseline_closes_after_three_distinct_runs(self, repo):
        ldg.seed(repo)
        for rid in ("r1", "r2"):
            ldg.append(repo, rid, "05", [_finding()])
        assert ldg.in_baseline(repo, 3) is True
        ldg.append(repo, "r3", "05", [_finding()])
        assert ldg.distinct_runs(repo) == 3
        assert ldg.in_baseline(repo, 3) is False

    def test_same_run_many_findings_is_still_one_run(self, repo):
        ldg.seed(repo)
        ldg.append(repo, "r1", "05", [_finding(title=str(i)) for i in range(9)])
        assert ldg.distinct_runs(repo) == 1


class TestDocDriftAxis:
    """M39 — 문서 드리프트가 어휘 밖으로 새고 승격 경로를 못 가졌다.

    `other/*` 는 글롭처럼 생겼지만 `categories()` 가 만드는 dict 의 **문자열
    키**다. 그리고 `unpromotable` 이라, 이 리포에서 가장 자주 나는 결함이
    승격 후보가 되지 않았다 (원장 실측 17건 · 3런).
    """

    def test_문서_드리프트_코드가_어휘에_있다(self, repo):
        ldg.seed(repo)
        assert "DOC_CODE_DRIFT" in ldg.categories(repo)
        ldg.append(repo, "r1", "05",
                   [_finding(category="DOC_CODE_DRIFT")])
        assert len(ldg.read_all(repo)) == 1

    def test_seed_와_디스크_taxonomy_의_코드_집합이_같다(self, repo):
        """M39 는 두 곳을 동시에 고쳐야 한다. 갈라져도 아무도 몰랐다."""
        disk = harness._read_json(ROOT / ldg.TAXONOMY_REL)
        seeded = {c["code"] for c in ldg.SEED_TAXONOMY["categories"]}
        assert {c["code"] for c in disk["categories"]} == seeded

    def test_글롭처럼_생긴_새_코드는_거부된다(self, repo):
        data = {"version": 1, "categories": [
            {"code": "foo/*", "enforceable": "prose", "status": "active"}]}
        assert any("코드 형태" in e for e in ldg.validate_taxonomy(data)),             ldg.validate_taxonomy(data)

    def test_실물_taxonomy_가_형태_규칙을_통과한다(self, repo):
        """`other/*` 는 retired 라 면제된다 — 원장의 과거를 읽을 수 있어야 한다."""
        disk = harness._read_json(ROOT / ldg.TAXONOMY_REL)
        assert ldg.validate_taxonomy(disk) == []

    def test_other_글롭은_경로처럼_매칭되지_않는다(self, repo):
        """성격 규정 — 지금도 통과한다. 제출 1회를 무르게 한 그 오해를 잠근다."""
        ldg.seed(repo)
        with pytest.raises(ValueError):
            ldg.append(repo, "r1", "05", [_finding(category="other/무엇")])

    def test_제목이_매번_다르면_임계에_닿지_않는다(self, repo):
        """**M39 의 진실이다.** 어휘를 고쳐도 승격은 안 된다.

        C4 가 축을 `rule_key` 로 갈랐어도 **이 경로는 안 바뀐다.**
        `rule_slug` 가 없는 지적의 `rule_key` 는 `finding_key` 와 같고
        (ADR-H034 의 폴백), 리뷰어의 자유 서술에는 슬러그가 없다. 그래서
        카테고리가 아무리 잦아도 제목이 매번 다르면 임계에 영원히 못
        닿는다. 나중에 누가 "고쳐졌다" 고 착각하지 않게 단언으로 못박는다
        — **C4 가 접은 것은 통제 어휘를 쓰는 쪽뿐이다.**
        """
        ldg.seed(repo)
        for run in ("r1", "r2", "r3"):
            for n in (1, 2):
                ldg.append(repo, run, "05",
                           [_finding(category="DOC_CODE_DRIFT",
                                     title="%s 의 %d 번째 어긋남" % (run, n))])
        got = ldg.stage_promotions(repo)
        assert got["candidates"] == [], got["candidates"]
        assert got["held"] == [], got["held"]
        roll = {b["category"]: b for b in got["by_category"]}
        assert roll["DOC_CODE_DRIFT"]["count"] == 6, roll
        assert roll["DOC_CODE_DRIFT"]["distinct_runs"] == 3, roll
        assert roll["DOC_CODE_DRIFT"]["distinct_keys"] == 6, roll
        assert roll["DOC_CODE_DRIFT"]["promotable"] is True, roll

    def test_같은_제목은_표현이_흔들려도_접힌다(self, repo):
        """**축은 통제 어휘 위에서는 슬러그 없이도 이미 작동했다.**

        `finding_key` 의 정규화는 공백 접기와 소문자화가 전부다
        (`verdict.py:125-130`). 그래서 템플릿이 **같은 심볼을 두 번** 찍으면
        슬러그 없이도 접힌다 — 실물 원장에서 누적 2 를 넘긴 버킷 둘이
        정확히 그것이다. 이 경로도 폴백이라 C4 뒤에 안 바뀐다.

        **C4 가 고친 것은 여기가 아니라 심볼이 매번 다를 때다** —
        `TestRuleKeyAxis` 가 그쪽을 잠근다. 셋이 함께 있어야 "축이 틀렸다"
        와 "입력이 자유 서술이다" 와 "같은 규칙인데 인스턴스가 다르다" 를
        가를 수 있다.
        """
        ldg.seed(repo)
        tmpl = "계약에 없는 public 심볼 ErrorBanner 가 생겼다"
        ldg.append(repo, "r1", "05",
                   [_finding(severity="critical", title=tmpl)])
        ldg.append(repo, "r2", "05",
                   [_finding(severity="critical",
                             title="  계약에 없는 Public 심볼   ErrorBanner 가 생겼다 ")])
        got = ldg.stage_promotions(repo)
        assert len(got["candidates"]) == 1, got["candidates"]
        assert got["candidates"][0]["distinct_runs"] == 2, got["candidates"]

    def test_롤업이_승격을_바꾸지_않는다(self, repo):
        """같은 제목이 임계를 넘으면 후보가 되는 경로는 그대로다."""
        ldg.seed(repo)
        for run in ("r1", "r2"):
            ldg.append(repo, run, "05",
                       [_finding(category="NAMING", severity="critical",
                                 title="같은 이름")])
        got = ldg.stage_promotions(repo)
        assert len(got["candidates"]) == 1, got

    def test_승격_불가_카테고리도_롤업에는_보인다(self, repo):
        """드러내고 안 고치는 것이 이 리포의 기본 수다."""
        ldg.seed(repo)
        ldg.append(repo, "r1", "05", [_finding(category="OTHER")])
        roll = {b["category"]: b
                for b in ldg.stage_promotions(repo)["by_category"]}
        assert roll["OTHER"]["promotable"] is False, roll


class TestRuleKeyAxis:
    """승격의 축을 **규칙**으로 가른다 (ADR-H034). `finding_key` 는 안 바꾼다.

    두 질문이 원래 다르다 — "이 런에서 무엇을 고쳐야 하나"(인스턴스)와
    "무엇이 반복되는 유형인가"(규칙). 전자는 `finding_key` 가 답하고
    후자를 `rule_key` 가 맡는다. **키를 갈아치우지 않고 하나 더 두는 것**이
    `review.merge` 2인 합치 · `review07.escaped` · 05 단조성 셋을 통째로
    비켜 가는 방법이다 (`team-spec.md` 의 "finding_key 는 바꾸지 않는다").

    폴백이 안전장치다 — `rule_slug` 가 없으면 `rule_key == finding_key` 라
    슬러그 없는 과거 줄의 집계가 한 비트도 안 바뀐다.
    """

    def test_슬러그가_없으면_rule_key_는_finding_key_다(self, repo):
        """**폴백이 항등이다.** 소급 오염이 구조적으로 불가능한 이유."""
        f = _finding()
        assert ldg.rule_key(f) == ldg.finding_key(f)

    def test_같은_슬러그는_제목이_달라도_한_키다(self, repo):
        """실물에서 84버킷으로 흩어진 그 모양이다 — 심볼만 다르다."""
        a = _finding(source="contract-trace", rule_slug="out_of_contract",
                     title="계약에 없는 public 심볼 ErrorBanner 가 생겼다")
        b = _finding(source="contract-trace", rule_slug="out_of_contract",
                     title="계약에 없는 public 심볼 RATE_LIMIT_WINDOW_MS 가 생겼다")
        assert ldg.finding_key(a) != ldg.finding_key(b), "인스턴스는 갈린다"
        assert ldg.rule_key(a) == ldg.rule_key(b), "규칙은 접힌다"

    def test_카테고리가_같아도_슬러그가_다르면_안_뭉친다(self, repo):
        """`CATEGORY` 는 다대일이다 — BOUNDARY_VIOLATION 에 코드 셋이 몰린다.

        축을 카테고리로 접었다면 이 셋이 한 버킷이 됐고, 승격된 규칙이
        어느 검사에서 왔는지 아무도 몰랐다. 슬러그가 그것을 가른다.
        """
        a = _finding(category="BOUNDARY_VIOLATION", source="contract-trace",
                     rule_slug="missing_impl", title="가")
        b = _finding(category="BOUNDARY_VIOLATION", source="contract-trace",
                     rule_slug="missing_entrypoint", title="나")
        assert ldg.rule_key(a) != ldg.rule_key(b)

    def test_out_of_contract_여섯이_한_버킷에_쌓인다(self, repo):
        """P8 이 실제로 낸 모양이다. `observations` 는 여전히 6관측이다."""
        ldg.seed(repo)
        ldg.append(repo, "r1", "05",
                   [_finding(source="contract-trace",
                             rule_slug="out_of_contract",
                             title="계약에 없는 public 심볼 %s 가 생겼다" % n)
                    for n in ("A", "B", "C", "D", "E", "F")])
        assert len(ldg.observations(repo)) == 6, "관측은 안 접힌다"
        got = ldg.stage_promotions(repo)
        buckets = got["candidates"] + got["held"]
        assert len(buckets) == 1, buckets
        b = buckets[0]
        assert b["count"] == 6, b
        assert b["rule_slug"] == "out_of_contract", b
        assert len(b["finding_keys"]) == 6, b
        assert got["candidates"] == [], "한 런이라 distinct_runs 가 모자란다"
        assert len(got["held"]) == 1, got["held"]

    def test_두_런이면_후보가_된다(self, repo):
        """심볼이 런마다 달라도 규칙은 같다 — C4 가 겨눈 바로 그 자리."""
        ldg.seed(repo)
        for run, names in (("r1", ("A", "B", "C")), ("r2", ("D", "E", "F"))):
            ldg.append(repo, run, "05",
                       [_finding(source="contract-trace",
                                 rule_slug="out_of_contract",
                                 title="계약에 없는 public 심볼 %s 가 생겼다" % n)
                        for n in names])
        got = ldg.stage_promotions(repo)
        assert len(got["candidates"]) == 1, got["candidates"]
        c = got["candidates"][0]
        assert c["count"] == 6 and c["distinct_runs"] == 2, c
        assert c["rule_key"] and c["rule_slug"] == "out_of_contract", c

    def test_리뷰어가_준_슬러그를_이제_받는다(self, repo):
        """**C4 는 버렸고 C5 가 받는다** (ADR-H035). 신뢰 경계가 옮겨 갔다.

        C4 의 이유는 *"`contract-trace` 의 어휘는 코드 안의 닫힌 집합이라
        모델이 지어낼 수 없다"* 였다. C5 는 `taxonomy.json` 에 리뷰어 어휘를
        선언하고 **제출자 층이 대조**하게 만들었다 — 닫힘이 코드에서 데이터로
        내려왔고, 보증은 `category` 가 M46 이후 갖고 있던 것과 같아졌다
        (*"지어낼 수 없다"* 가 아니라 *"지어내면 exit 8"*).
        """
        ldg.seed(repo)
        ldg.append(repo, "r1", "05",
                   [_finding(category="DOC_CODE_DRIFT", source="reviewer",
                             rule_slug="doc_contradicts_code", title="가"),
                    _finding(category="DOC_CODE_DRIFT", source="reviewer",
                             rule_slug="doc_contradicts_code", title="나")])
        rows = ldg.read_all(repo)
        assert all(r["rule_slug"] == "doc_contradicts_code" for r in rows), rows
        assert all(r["rule_key"] != r["finding_key"] for r in rows), rows
        roll = {b["category"]: b
                for b in ldg.stage_promotions(repo)["by_category"]}
        assert roll["DOC_CODE_DRIFT"]["distinct_keys"] == 1, roll

    def test_형태가_어긋난_슬러그는_조용히_안_받는다(self, repo):
        """생산자 안의 닫힌 집합이라 어긋나면 **버그다.**

        어휘 밖 `category`·`resolution` 을 조용히 받지 않는 것과 같은 자리다.
        신뢰 경계(다른 source)와 다르다 — 저쪽은 예상된 입력이고 이쪽은
        `trace_contract` 가 스스로 깨진 것이다.
        """
        ldg.seed(repo)
        with pytest.raises(ValueError):
            ldg.append(repo, "r1", "05",
                       [_finding(source="contract-trace",
                                 rule_slug="Out Of Contract")])

    def test_슬러그가_없는_줄은_finding_key_축_그대로다(self, repo):
        """**소급 무오염의 단위 판.** 슬러그 없는 원장은 변경 전과 같다."""
        ldg.seed(repo)
        for run in ("r1", "r2"):
            ldg.append(repo, run, "05",
                       [_finding(category="DOC_CODE_DRIFT", severity="critical",
                                 title="%s 의 어긋남" % run)])
        got = ldg.stage_promotions(repo)
        assert got["candidates"] == [] and got["held"] == []
        roll = {b["category"]: b for b in got["by_category"]}
        assert roll["DOC_CODE_DRIFT"]["distinct_keys"] == 2, roll

    # 파일럿 원장의 앞 168줄에 슬러그가 없다는 것을 못박던 검사가 여기 있었다.
    # **추출이 그 168줄을 안 실었으므로 검사할 대상이 없다** (ADR-H039 결정 2).
    # 폴백이 항등이라는 사실 자체는 위 `test_슬러그가_없으면_...` 이 픽스처 위에서
    # 계속 든다 — 잃은 것은 *그 파일럿의 과거 데이터가 안 바뀌었다* 는 일회성
    # 마이그레이션 잠금이고, 그것은 이미 끝난 일이다.

    def test_실물_원장의_슬러그는_전부_어휘_안이다(self, repo):
        """지금은 0건이라 자명히 통과하고 **P9 부터 진짜 불변식**이 된다.

        `contract-trace` 는 면제한다 — 그쪽 어휘는 `trace_contract.CATEGORY`
        이지 taxonomy 가 아니다 (2층 규율에서 append 가 어휘를 안 보는 이유).
        """
        cats = ldg.categories(ROOT)
        for r in ldg.read_all(ROOT):
            slug = r.get("rule_slug")
            if not slug or r.get("source") == "contract-trace":
                continue
            vocab = {s["slug"]
                     for s in (cats.get(r["category"]) or {}).get("slugs") or []}
            assert slug in vocab, (r["category"], slug, sorted(vocab))

    def test_05_합치는_슬러그로_뭉개지지_않는다(self, repo):
        """**가장 중요한 회귀다.** `review.merge` 의 축은 `finding_key` 다.

        축을 접었다면 심볼 셋이 한 finding 으로 뭉개져 **수리하는 쪽이
        무엇을 고칠지 모르게** 되고, 2인 합치로 오인돼 severity 까지 올랐다.
        """
        subs = [{"reviewer": "arch", "findings": [
            _finding(source="contract-trace", rule_slug="out_of_contract",
                     title="계약에 없는 public 심볼 %s 가 생겼다" % n)
            for n in ("A", "B", "C")]}]
        got = rv.merge(subs)
        assert len(got) == 3, got
        assert len({f["finding_key"] for f in got}) == 3, got
        assert all(f["severity"] == "major" for f in got), got

    def test_code_review_의_슬러그도_받는다(self, repo):
        """07 도 봉투가 어휘를 실어 주는 생산자다. **빼면 안 된다** —

        반사실 계산에서 `doc_contradicts_code` 후보 4건 중 2건이 07 행이라,
        07 을 빼면 그 후보가 성립하지 않는다.
        """
        ldg.seed(repo)
        ldg.append(repo, "r1", "07",
                   [_finding(category="DOC_CODE_DRIFT", source="code-review",
                             rule_slug="same_fact_two_places")])
        assert ldg.read_all(repo)[0]["rule_slug"] == "same_fact_two_places"

    def test_external_과_human_의_슬러그는_여전히_폴백이다(self, repo):
        """신뢰 경계가 **완전히** 열리지는 않았다.

        `external` 은 봇 요약이라 봉투를 본 적이 없고 `human` 은 정의상
        `review.check` 를 안 탄다. 어휘를 아무도 안 찍어 준 값을 받는 것은
        C4 가 경고한 "무관한 지적을 한 버킷에 뭉친다" 로 곧장 간다.
        """
        ldg.seed(repo)
        for src in ("external", "human"):
            ldg.append(repo, "r1", "05",
                       [_finding(category="DOC_CODE_DRIFT", source=src,
                                 rule_slug="doc_contradicts_code",
                                 title="%s 의 지적" % src)])
        rows = ldg.read_all(repo)
        assert all("rule_slug" not in r for r in rows), rows
        assert all(r["rule_key"] == r["finding_key"] for r in rows), rows

    def test_append_는_어휘_대조를_하지_않는다(self, repo):
        """**2층 규율** (M46). 어휘는 제출자 층이 막고 여기는 신뢰 경계와 형태다.

        여기서 어휘를 강제하면 `contract-trace` 의 `out_of_contract` 를
        `NAMING.slugs` 에 적어야 하고 두 생산자의 어휘가 한 배열에서 섞인다.
        마지막 방어선은 **동작해야** 하므로 모르는 슬러그도 행으로는 쓴다.
        """
        ldg.seed(repo)
        ldg.append(repo, "r1", "05",
                   [_finding(category="DOC_CODE_DRIFT", source="reviewer",
                             rule_slug="unknown_slug")])
        assert ldg.read_all(repo)[0]["rule_slug"] == "unknown_slug"

    def test_형태가_어긋난_리뷰어_슬러그는_exit_8_이다(self, repo):
        """신뢰 경계(폴백)와 **형태**(버그)는 다른 거절이다.

        source 가 어휘 밖인 것은 예상된 입력이라 조용히 폴백하지만, 형태가
        어긋난 것은 생산자가 스스로 깨진 것이다 — `lookup_failed` 와
        `no_match` 를 안 뭉개는 규율이 승격 입력에도 그대로 선다.
        """
        ldg.seed(repo)
        with pytest.raises(ValueError):
            ldg.append(repo, "r1", "05",
                       [_finding(category="DOC_CODE_DRIFT", source="reviewer",
                                 rule_slug="Doc Contradicts Code")])

    def test_같은_슬러그_다른_제목이_리뷰어_경로에서도_접힌다(self, repo):
        """C4 가 `out_of_contract` 에서 본 모양의 **리뷰어 판**이다.

        원장 실측 — `nothing_locked` 7관측이 3런을 가로지르는데 제목은 매번
        달랐다. 접히지 않으면 그 일곱이 일곱 버킷이고 임계에 못 닿는다.
        """
        ldg.seed(repo)
        for run in ("r1", "r2"):
            ldg.append(repo, run, "05",
                       [_finding(category="TEST_MISSING_FAILURE_PATH",
                                 role="test", source="reviewer",
                                 rule_slug="nothing_locked",
                                 title="%s 에서 %s 를 아무도 안 잠근다" % (run, n))
                        for n in ("상한", "빈 값", "경계")])
        got = ldg.stage_promotions(repo)
        assert len(got["candidates"]) == 1, got["candidates"]
        c = got["candidates"][0]
        assert c["count"] == 6 and c["distinct_runs"] == 2, c
        assert c["rule_slug"] == "nothing_locked", c

    def test_슬러그가_있어도_finding_key_는_안_바뀐다(self, repo):
        """`team-spec` 이 못박은 불변이다. 두 키가 갈리는 자리를 잠근다."""
        base = _finding(category="DOC_CODE_DRIFT", source="reviewer")
        with_slug = dict(base, rule_slug="doc_contradicts_code")
        assert ldg.finding_key(base) == ldg.finding_key(with_slug)
        assert ldg.rule_key(with_slug) != ldg.finding_key(with_slug)

    def test_05_합치는_리뷰어_슬러그로도_뭉개지지_않는다(self, repo):
        """C4 의 회귀가 C5 에서도 선다 — `review.merge` 의 축은 `finding_key` 다.

        접었다면 서로 다른 세 지적이 합치로 오인돼 severity 가 부당하게
        오르고, 수리하는 쪽이 무엇을 고칠지 모르게 된다.
        """
        subs = [{"reviewer": "arch", "findings": [
            _finding(category="DOC_CODE_DRIFT", source="reviewer",
                     rule_slug="doc_contradicts_code",
                     title="%s 의 주석이 코드와 다르다" % n)
            for n in ("A", "B", "C")]}]
        got = rv.merge(subs)
        assert len(got) == 3, got
        assert len({f["finding_key"] for f in got}) == 3, got
        assert all(f["severity"] == "major" for f in got), got

    def test_두_리뷰어가_다른_슬러그를_주면_먼저_온_것이_굳는다(self, repo):
        """도착 순서 의존이지만 **새 비결정성이 아니다.**

        `merge` 는 `setdefault` 라 `path`·`quote`·`evidence` 가 이미 첫
        제출을 굳힌다. 슬러그가 그 성질을 하나 더 탈 뿐이라는 것을 못박고
        고치지 않는다 — 고치면 두 리뷰어의 한 지적이 두 규칙으로 갈린다.
        """
        def one(slug):
            return _finding(category="DOC_CODE_DRIFT", source="reviewer",
                            rule_slug=slug, title="같은 지적")
        got = rv.merge([{"reviewer": "arch",
                         "findings": [one("doc_contradicts_code")]},
                        {"reviewer": "sec",
                         "findings": [one("same_fact_two_places")]}])
        assert len(got) == 1, got
        assert got[0]["rule_slug"] == "doc_contradicts_code", got

class TestSlugVocabulary:
    """리뷰어·code-review 의 통제 어휘가 `taxonomy.json` 에 산다 (ADR-H035).

    **거처가 스킬 파일이 아닌 이유는 실측이다.** `DOC_CODE_DRIFT` 18건을 낸
    것은 arch 7 · data 5 · sec 3 이고 `docs` 는 0 이다 — `docs-reviewer` 는
    `only_when_no_source_change` 라 여섯 런에서 한 번도 안 켜졌다.
    **카테고리는 스킬을 가로지르므로** 어휘를 SKILL.md 에 두면 그것을 안 읽는
    리뷰어가 그 카테고리를 내고, 그것이 바로 `same_fact_two_places` 다.
    """

    def _cat(self, **kw):
        d = {"code": "DOC_CODE_DRIFT", "enforceable": "prose",
             "status": "active"}
        d.update(kw)
        return {"version": 1, "categories": [d]}

    def test_어휘가_카테고리_객체_안에_산다(self, repo):
        """최상위 별도 맵을 안 고른 이유 — `rule_key` 가 이미
        `category|target_role|rule_slug` 라 슬러그는 구조적으로 카테고리
        종속이다. 중첩이면 참조 무결성 위반이 **불가능**하다.
        """
        ldg.seed(repo)
        cats = ldg.categories(repo)
        for code in ("DOC_CODE_DRIFT", "TEST_MISSING_FAILURE_PATH"):
            slugs = cats[code].get("slugs")
            assert slugs, code
            assert all(set(s) >= {"slug", "note"} for s in slugs), slugs

    def test_형태가_어긋난_슬러그_선언은_거부된다(self, repo):
        data = self._cat(slugs=[{"slug": "Doc Contradicts", "note": "x"}])
        assert any("형태" in e for e in ldg.validate_taxonomy(data)), \
            ldg.validate_taxonomy(data)

    def test_선언이_받는_형태와_append_가_받는_형태가_같다(self, repo):
        """정규식을 새로 쓰면 **선언한 슬러그를 append 가 exit 8 로 튕긴다.**

        그리고 그 사실이 어디에도 안 드러난다 — 두 곳이 갈렸다는 것을
        아무도 못 보는 종류의 결함이다. **소스에 `_SLUG_SHAPE` 가 적혔는지가
        아니라 두 판정이 실제로 일치하는지**를 본다: 이름만 보면 정규식을
        복사해 두 번 쓴 것을 못 잡는다.
        """
        candidates = ["nothing_locked", "a", "a_1", "A_b", "a-b", "a b",
                      "1a", "_a", "a__b", "aB", "", "한글"]
        for slug in candidates:
            declared = ldg.validate_taxonomy(
                self._cat(slugs=[{"slug": slug, "note": "x"}])) == []
            accepted = bool(ldg._SLUG_SHAPE.match(slug))
            assert declared == accepted, (slug, declared, accepted)

    def test_한_카테고리_안에서_슬러그가_유니크하다(self, repo):
        data = self._cat(slugs=[{"slug": "a_b", "note": "x"},
                                {"slug": "a_b", "note": "y"}])
        assert any("유니크" in e for e in ldg.validate_taxonomy(data))

    def test_카테고리를_가로지르는_중복은_허용된다(self, repo):
        """`rule_key` 가 category 를 포함하므로 **다른 규칙이다.**

        금지하면 없는 제약을 만든다 — security 의 「신뢰 경계」와 data-layer 의
        「외부 응답의 신뢰 경계」처럼 같은 말이 두 관점에 실재한다.
        """
        data = {"version": 1, "categories": [
            {"code": "DOC_CODE_DRIFT", "enforceable": "prose",
             "status": "active", "slugs": [{"slug": "a_b", "note": "x"}]},
            {"code": "TX_BOUNDARY", "enforceable": "prose",
             "status": "active", "slugs": [{"slug": "a_b", "note": "y"}]}]}
        assert ldg.validate_taxonomy(data) == []

    def test_승격_불가_카테고리는_어휘를_선언할_수_없다(self, repo):
        """**이 한 줄이 「어휘가 선언된 곳에서만 필수」를 구조로 만든다.**

        면제 목록을 코드에 손으로 적으면 어휘가 늘 때 한쪽만 고쳐진다.
        스키마가 답하면 `OTHER`·`CONTRACT_DEFECT`·`other/*` 의 면제가
        **선언에서** 나온다.
        """
        for status in ldg.NEVER_PROMOTE:
            data = self._cat(code="OTHER", status=status,
                             slugs=[{"slug": "a_b", "note": "x"}])
            errs = ldg.validate_taxonomy(data)
            assert any("승격" in e for e in errs), (status, errs)

    def test_빈_배열_선언은_거부된다(self, repo):
        """"선언했는데 비었다" 와 "선언 안 했다" 가 같은 침묵이 되면 안 된다."""
        assert any("비었다" in e for e in ldg.validate_taxonomy(
            self._cat(slugs=[])))

    def test_note_없는_슬러그는_거부된다(self, repo):
        """`note` 가 봉투의 유일한 화물이다.

        없으면 arch·data·sec 가 `same_fact_two_places` 와
        `doc_contradicts_code` 를 언제 가르는지 모른 채 고른다.
        """
        assert any("note" in e for e in ldg.validate_taxonomy(
            self._cat(slugs=[{"slug": "a_b"}])))

    def test_배열이_아닌_slugs_는_거부된다(self, repo):
        assert ldg.validate_taxonomy(self._cat(slugs="a_b")) != []

    def test_실물_taxonomy_가_새_검증을_통과한다(self, repo):
        disk = harness._read_json(ROOT / ldg.TAXONOMY_REL)
        assert ldg.validate_taxonomy(disk) == []

    def test_seed_와_디스크의_슬러그_집합이_같다(self, repo):
        """기존 대조는 `code` 집합만 봤다 — **슬러그 발산이 조용했다.**

        `taxonomy.json` 은 시드가 만들고 사람이 늘리는 파일이라 둘이 갈리면
        시드를 물려받는 파생 프로젝트가 다른 어휘를 갖는다.
        """
        disk = harness._read_json(ROOT / ldg.TAXONOMY_REL)

        def vocab(data):
            return {(c["code"], s["slug"])
                    for c in data["categories"]
                    for s in c.get("slugs") or []}

        assert vocab(disk) == vocab(ldg.SEED_TAXONOMY)

    def test_slug_vocabulary_는_없으면_빈_목록이다(self, repo):
        """없는 것과 빈 것을 구분한다 — 호출부가 분기를 안 써도 되게."""
        ldg.seed(repo)
        assert ldg.slug_vocabulary(repo, "OTHER") == []
        assert ldg.slug_vocabulary(repo, "지어낸_코드") == []
        got = ldg.slug_vocabulary(repo, "DOC_CODE_DRIFT")
        assert {s["slug"] for s in got} >= {"doc_contradicts_code",
                                            "same_fact_two_places"}


class TestPromotionThresholds:
    """임계 여섯을 상수로 못박는다 — 축을 넓혔다고 임계가 맞다는 뜻이 아니다.

    **이 클래스에는 파일럿 원장의 집계를 고정 기대값으로 못박던 검사 둘이
    있었다** (`by_category` 아홉 줄과 `distinct_runs == 6`). 추출이 그
    168줄을 안 실었으므로 검사할 대상이 없다 (ADR-H039 결정 2).

    **잃은 것과 남은 것을 가른다.** 잃은 것은 *그 파일럿의 과거 데이터가 한
    비트도 안 바뀌었다* 는 **일회성 마이그레이션 잠금**이고 그 마이그레이션은
    이미 끝났다. 남은 것은 집계 **동작**을 픽스처 위에서 재는 검사들
    (`TestRuleKeyAxis` · `TestLedgerPromotion`)이고, 그쪽이 회귀를 든다.
    """

    def test_임계_여섯은_한_자리도_안_바뀐다(self, repo):
        """축을 넓혔다고 임계가 맞다는 뜻이 아니다 — 캘리브레이션은 P9~P11 이다."""
        assert ldg.THRESHOLDS == {
            "critical": (2, 2), "major": (3, 2), "minor": (5, 3)}

class TestLedgerPromotion:
    """임계값 여섯과 distinct_runs >= 2. 전부 미검증 상속값이다."""

    def _seed_key(self, repo, runs, severity="major", **kw):
        ldg.seed(repo)
        for rid in runs:
            ldg.append(repo, rid, "05", [_finding(severity=severity, **kw)])

    def test_major_needs_three_occurrences(self, repo):
        self._seed_key(repo, ["r1", "r2"])
        assert ldg.stage_promotions(repo)["candidates"] == []
        ldg.append(repo, "r3", "05", [_finding(severity="major")])
        assert len(ldg.stage_promotions(repo)["candidates"]) == 1

    def test_critical_needs_two(self, repo):
        self._seed_key(repo, ["r1"], severity="critical")
        assert ldg.stage_promotions(repo)["candidates"] == []
        ldg.append(repo, "r2", "05", [_finding(severity="critical")])
        assert len(ldg.stage_promotions(repo)["candidates"]) == 1

    def test_one_run_never_promotes_however_many_times(self, repo):
        """한 런에서 같은 지적이 다섯 번 와도 관측은 하나다.

        **신원이 `(run_id, phase, finding_key)` 이므로 라운드 반복이 임계를
        혼자 채우지 못한다** (M30). 예전에는 다섯 줄이 `count=5` 가 되어
        `distinct_runs` 하나에만 기대고 있었다.
        """
        ldg.seed(repo)
        ldg.append(repo, "r1", "05",
                   [_finding(severity="critical") for _ in range(5)])
        got = ldg.stage_promotions(repo)
        assert got["candidates"] == []
        assert got["held"] == [], "누적 자체가 임계에 못 닿는다"

    def test_held_is_still_reachable(self, repo):
        """`held` 가 도달 불가능해지면 두 침묵이 다시 하나가 된다.

        누적은 넘었는데 `distinct_runs` 에서 막힌 상태가 여전히 표현돼야
        "임계가 높다" 와 "런이 모자라다" 가 갈린다. minor 는 (5회, 3런) 이므로
        두 런 × 세 페이즈면 누적 6 · 런 2 로 그 자리에 선다.
        """
        ldg.seed(repo)
        for rid in ("r1", "r2"):
            for phase in ("05", "07", "05-trace"):
                ldg.append(repo, rid, phase, [_finding(severity="minor")])
        got = ldg.stage_promotions(repo)
        assert got["candidates"] == []
        assert len(got["held"]) == 1
        assert got["held"][0]["count"] >= 5
        assert got["held"][0]["distinct_runs"] == 2

    def test_minor_needs_five_over_three_runs(self, repo):
        """페이즈 축이 살아 있어 누적과 런 수가 여전히 다른 것을 센다."""
        ldg.seed(repo)
        for rid in ("r1", "r2"):
            for phase in ("05", "07"):
                ldg.append(repo, rid, phase, [_finding(severity="minor")])
        assert ldg.stage_promotions(repo)["candidates"] == [], "누적 4 · 런 2"
        ldg.append(repo, "r3", "05", [_finding(severity="minor")])
        assert len(ldg.stage_promotions(repo)["candidates"]) == 1

    def test_warn_only_is_excluded_from_the_count(self, repo):
        """baseline 기간의 관측은 승격 근거가 아니다."""
        ldg.seed(repo)
        for rid in ("r1", "r2", "r3"):
            ldg.append(repo, rid, "05",
                       [_finding(severity="critical", resolution="warn_only")])
        assert ldg.stage_promotions(repo)["candidates"] == []

    def test_escalate_only_category_never_promotes(self, repo):
        ldg.seed(repo)
        for rid in ("r1", "r2", "r3"):
            ldg.append(repo, rid, "05",
                       [_finding(category="CONTRACT_DEFECT", severity="critical")])
        assert ldg.stage_promotions(repo)["candidates"] == []

    def test_CONTRACT_MISMATCH_는_받되_승격하지_않는다(self, repo):
        """gen 의 기본 category 다 (ADR-H043). 인스턴스 결함이지 규칙이 아니다."""
        ldg.seed(repo)
        for rid in ("r1", "r2", "r3"):
            ldg.append(repo, rid, "05",
                       [_finding(category="CONTRACT_MISMATCH", severity="critical",
                                 reported_by=["gen"])])
        assert len(ldg.read_all(repo)) == 3
        assert ldg.stage_promotions(repo)["candidates"] == []
        assert "CONTRACT_MISMATCH" not in ldg.excluded_categories(repo)

    def test_candidate_carries_destination_from_taxonomy(self, repo):
        """enforceable 이 어디로 승격할지를 정한다 — 후보가 그것을 들고 나온다."""
        self._seed_key(repo, ["r1", "r2", "r3"], category="NAMING")
        cand = ldg.stage_promotions(repo)["candidates"][0]
        assert cand["enforceable"] == "lint"
        assert cand["distinct_runs"] >= 2
        assert cand["count"] == 3

    def test_prose_promotion_of_machine_enforceable_is_refused(self, repo):
        """기계로 막을 수 있는 규칙의 산문 승격은 exit 8 이다."""
        ldg.seed(repo)
        with pytest.raises(ValueError):
            ldg.check_destination(repo, "NAMING", "prose")
        assert ldg.check_destination(repo, "AUTHZ_MISSING_RULE", "prose") is None


class TestPromotionVerdictDeadline:
    """승격 임계·축을 **언제** 판정하는가 (ADR-H033).

    임계값 여섯이 미검증 상속값이라는 사실은 `ledger.py` 주석에 처음부터
    적혀 있었고 *"첫 세 런의 원장이 이 값을 검사한다"* 는 약속도 있었다.
    그런데 그 약속에 기계가 읽는 시한이 없어서 `distinct_runs` 가 6 이 될
    때까지 아무도 판정하지 않았다 — **지나간 것조차 몰랐다.** 이 클래스가
    지키는 것은 시한이 표시되는가이지 시한이 무엇을 강제하는가가 아니다.
    """

    def _seed_runs(self, repo, n):
        ldg.seed(repo)
        for i in range(n):
            ldg.append(repo, "r%d" % i, "05",
                       [_finding(title="런 %d 만의 제목" % i)])

    def test_시한이_원장이_본_런으로_남은_런을_낸다(self, repo):
        self._seed_runs(repo, 6)
        got = ldg.verdict_deadline(repo)
        assert got == {"at": 9, "seen": 6, "remaining": 3, "due": False}, got

    def test_시한에_닿으면_due_고_남은_런은_음수로_안_내려간다(self, repo):
        self._seed_runs(repo, 9)
        got = ldg.verdict_deadline(repo)
        assert got["due"] is True and got["remaining"] == 0, got
        self._seed_runs(repo, 12)
        got = ldg.verdict_deadline(repo)
        assert got["seen"] == 12, got
        assert got["remaining"] == 0, "지난 시한을 음수로 적지 않는다"

    def test_시한_셈이_승격_판정을_한_비트도_안_바꾼다(self, repo, monkeypatch):
        """ADR-H026 이 `by_category` 롤업을 넣을 때 쓴 것과 같은 확인이다.

        시한을 넘겼는지가 후보 판정에 되먹임되면, 「임계가 높다」 와
        「표본이 모자라다」 를 가르려고 만든 장치가 그 판정을 오염시킨다.
        원장은 그대로 두고 **시한 상수만** 흔들어 본다.
        """
        ldg.seed(repo)
        for rid in ("r1", "r2"):
            ldg.append(repo, rid, "05",
                       [_finding(severity="critical", title="같은 이름")])
        for rid in ("r1", "r2"):
            for phase in ("05", "07", "05-trace"):
                ldg.append(repo, rid, phase,
                           [_finding(severity="minor", title="막힌 이름")])
        keys = ("candidates", "held", "by_category", "distinct_runs")

        monkeypatch.setattr(ldg, "PROMOTION_VERDICT_AT_RUNS", 9)
        before = ldg.stage_promotions(repo)
        monkeypatch.setattr(ldg, "PROMOTION_VERDICT_AT_RUNS", 1)
        after = ldg.stage_promotions(repo)

        assert before["verdict_deadline"]["due"] is False
        assert after["verdict_deadline"]["due"] is True, "시한은 실제로 흔들렸다"
        for k in keys:
            assert before[k] == after[k], k
        assert len(before["candidates"]) == 1 and len(before["held"]) == 1,             "후보와 held 가 둘 다 살아 있는 표본이어야 확인에 값이 있다"


# ---------------------------------------------------------------------------
# K. contract-trace — 계약 ↔ 코드 대조 5종
# ---------------------------------------------------------------------------

import trace_contract as tr  # noqa: E402

CONTRACT = """# 계약: 유사도

## 유닛
- `lib/match.ts · matchTitle(a: string, b: string): number`
  - 정상: 0~1 을 돌려준다

## 진입점
- `POST /api/analyze` → 200

## 오류 어휘
- `MATCH_FAILED` (500)
"""


def _write_contract(repo, text=CONTRACT):
    p = repo / "_workspace" / "contract_x.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _load(repo):
    return adapters.load(repo)


def _trace(repo, contract_path, **kw):
    config, adapter, _cal = _load(repo)
    return tr.run(repo, config, adapter, contract_path, **kw)


# P8 의 계약이 「데이터 형태」 절을 쓴 그대로다 (M57).
#
# **상수 다섯이 불릿이 아니라 그 불릿의 연속 줄에 있다.** `_errors` 의
# "최상위 `-` 줄만" 규칙으로는 못 잡는 모양이고, 그것이 P8 의 오탐 6/6 이
# 나온 자리다. 픽스처를 다듬지 않고 실물 그대로 둔다 — 다듬으면 이 테스트가
# 실제로 났던 실패를 재현하지 않는다.
DATA_SHAPES_DOC = """# 계약: x

## 데이터 형태

- `RateLimitDecision { allowed: boolean; retryAfterSeconds: number }`
  - `retryAfterSeconds` 는 **1 이상의 정수**다. `Retry-After` 가 정수 초를 요구한다
- 전역 상태는 **컨테이너 하나**다:
  `RateLimitState { map: Map<string, number>; windowStart: number | undefined }`
  - **`windowStart` 는 `map` 의 속성이 아니라 컨테이너의 형제 필드다**
  - `globalThis` 에 건다. `declare global` 로 타입을 선언하고 `any` 로 얹지 않는다
- 상수는 전부 `src/lib/env.ts` 에서 온다:
  `RATE_LIMIT_MAX_REQUESTS`(20) · `RATE_LIMIT_WINDOW_MS`(60_000) ·
  `RATE_LIMIT_MAX_TRACKED_KEYS`(50_000) · `RATE_LIMIT_SHARED_MAX_REQUESTS`(200) ·
  `RATE_LIMIT_MAX_KEY_CHARS`(45)
  - `process.env` 를 읽지 않는다

## 유닛

- `lib/match.ts · matchTitle(a: string, b: string): number`
"""

# 「데이터 형태」 절이 없는 옛 계약. 그대로 돌아야 한다.
OLD_CONTRACT_DOC = """# 계약: x

## 유닛

- `lib/a.ts · f(): void`
"""

P8_FALSE_POSITIVES = (
    "RATE_LIMIT_MAX_REQUESTS", "RATE_LIMIT_WINDOW_MS",
    "RATE_LIMIT_MAX_TRACKED_KEYS", "RATE_LIMIT_SHARED_MAX_REQUESTS",
    "RATE_LIMIT_MAX_KEY_CHARS", "RateLimitDecision",
)


class TestContractDataShapes:
    """계약의 「데이터 형태」 절이 파서에 등록된 적이 없었다 (M57).

    `parse()` 는 `config.contract.sections` 가 이름 붙인 절만 읽는데 그 매핑에
    이 절이 없었다. 템플릿은 거기 타입·상수를 적게 하므로, 계약이 이름 붙인
    이름이 `symbols()` 에 안 들어오고 `out_of_contract` 가 전부 "계약에 없는
    심볼" 로 잡았다 — **P8 의 지적 6/6 이 그 구조적 오탐이다.**

    C4([[ADR-H034]])가 그 여섯을 한 버킷으로 접었으므로 고치지 않으면 이
    파이프라인의 **첫 승격 후보가 기계가 틀린 규칙 위에 선다.** 그래서 P9 전에
    닫는다.
    """

    def _parse(self, repo, text=DATA_SHAPES_DOC):
        cfg = json.loads((repo / "harness" / "config.json").read_text(encoding="utf-8"))
        return contract_mod.parse(text, cfg)

    def test_계약이_이름_붙인_타입과_상수가_심볼에_들어온다(self, repo):
        """**P8 오탐 여섯이 전부 여기서 회수된다.**"""
        got = contract_mod.symbols(self._parse(repo))
        missing = [n for n in P8_FALSE_POSITIVES if n not in got]
        assert missing == [], "P8 이 오탐으로 잡은 이름이 아직 안 들어온다: %s" % missing

    def test_불릿이_아닌_연속_줄도_읽는다(self, repo):
        """상수 다섯이 그 모양이다 — 최상위 불릿만 보면 2/6 밖에 못 잡는다."""
        got = contract_mod.symbols(self._parse(repo))
        consts = [n for n in P8_FALSE_POSITIVES if n.isupper()]
        assert all(n in got for n in consts), got

    def test_타입_상수_형태가_아닌_낱말은_안_들어온다(self, repo):
        """**미탐을 막는 회귀다.**

        절 전체의 백틱을 형태 없이 다 모으면 `map`·`any` 같은 흔한 낱말이
        계약에 있다는 이유로 **진짜 위반이 조용히 통과한다.** 오탐을 고치려다
        미탐을 만드는 것이 이 자리의 실패 방식이다.
        """
        got = contract_mod.symbols(self._parse(repo))
        for noise in ("retryAfterSeconds", "map", "windowStart", "globalThis",
                      "any", "src", "process", "declare"):
            assert noise not in got, "%r 가 심볼로 들어왔다" % noise

    def test_절이_없으면_빈_결과이고_예외가_아니다(self, repo):
        """그 절이 없는 옛 계약이 그대로 돌아야 한다."""
        p = self._parse(repo, OLD_CONTRACT_DOC)
        assert p["data_shapes"] == []
        assert [u["symbol"] for u in p["units"]] == ["f"]

    def test_기존_세_키가_안_바뀐다(self, repo):
        """`units`·`entrypoints`·`errors` 는 이 증분이 건드리지 않는다."""
        p = self._parse(repo)
        assert [u["symbol"] for u in p["units"]] == ["matchTitle"]
        assert p["entrypoints"] == [] and p["errors"] == []

    def test_게이트의_귀속도_같은_심볼_집합을_쓴다(self, repo):
        """`symbols()` 소비자는 둘이고 **둘 다 넓어진다** — 말없이 넓히지 않는다.

        `gate.py` 가 컴파일·테스트 실패를 역할에 배정할 때 같은 집합으로
        `in_contract` 를 판정한다. 여기서 잠그지 않으면 이 증분이 게이트 거동을
        바꾼 사실이 어디에도 안 드러난다.
        """
        got = contract_mod.symbols(self._parse(repo))
        assert "RateLimitDecision" in got and "matchTitle" in got, got

    def test_실물_P8_계약에서_여섯이_전부_회수된다(self, repo):
        """**픽스처가 아니라 그 런이 실제로 쓴 계약으로 확인한다.**

        `_workspace/runs/**` 는 그 런의 사실 기록이라 한 바이트도 안 고친다 —
        읽기만 한다. 스냅샷이 없는 환경에서는 건너뛴다: 없는 것을 실패로 적으면
        「파일이 없다」와 「고쳐지지 않았다」가 같은 빨간불이 된다.
        """
        snap = (ROOT / "_workspace" / "runs" / "20260908-1720-dca1"
                / "06_contract_snapshot.md")
        if not snap.exists():
            pytest.skip("P8 계약 스냅샷이 없다 — 판정할 표본이 없는 것이지 실패가 아니다")
        got = contract_mod.symbols(
            self._parse(repo, snap.read_text(encoding="utf-8")))
        missing = [n for n in P8_FALSE_POSITIVES if n not in got]
        assert missing == [], missing


class TestContractTraceMissingImpl:
    """컨테이너명 + 심볼명 **쌍**으로 본다. 심볼명만 보면 거짓 통과한다."""

    def test_present_symbol_produces_no_finding(self, repo):
        got = _trace(repo, _write_contract(repo))
        assert [f for f in got["findings"] if f["code"] == "missing_impl"] == []

    def test_absent_symbol_is_critical(self, repo):
        (repo / "src" / "lib" / "match.ts").write_text(
            "export function 다른것(): number { return 0 }\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo))
        miss = [f for f in got["findings"] if f["code"] == "missing_impl"]
        assert len(miss) == 1
        assert miss[0]["severity"] == "critical"
        assert miss[0]["target_role"] == "impl"      # primary_role

    def test_same_name_in_another_file_does_not_pass(self, repo):
        """이것이 컨테이너 쌍 검색의 존재 이유다."""
        (repo / "src" / "lib" / "match.ts").write_text("export const x = 1\n",
                                                       encoding="utf-8")
        (repo / "src" / "lib" / "다른.ts").write_text(
            "export function matchTitle(): number { return 0 }\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo))
        assert [f["code"] for f in got["findings"]].count("missing_impl") == 1

    def test_unresolvable_container_falls_to_unknown_not_pass(self, repo):
        """컨테이너를 못 찾으면 통과가 아니라 unknown 으로 낙하한다."""
        text = CONTRACT.replace("lib/match.ts", "lib/없는파일.ts")
        got = _trace(repo, _write_contract(repo, text))
        miss = [f for f in got["findings"] if f["code"] == "missing_impl"]
        assert len(miss) == 1
        assert miss[0]["container_resolved"] is False


class TestContractTraceErrorsAndEntrypoints:

    def test_missing_error_symbol_is_critical(self, repo):
        got = _trace(repo, _write_contract(repo))
        errs = [f for f in got["findings"] if f["code"] == "missing_error_symbol"]
        assert len(errs) == 1 and errs[0]["severity"] == "critical"

    def test_present_error_symbol_is_clean(self, repo):
        (repo / "src" / "lib" / "errors.ts").write_text(
            "export const MATCH_FAILED = 'MATCH_FAILED'\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo))
        assert [f for f in got["findings"] if f["code"] == "missing_error_symbol"] == []

    def test_missing_entrypoint_is_critical(self, repo):
        got = _trace(repo, _write_contract(repo))
        eps = [f for f in got["findings"] if f["code"] == "missing_entrypoint"]
        assert len(eps) == 1 and eps[0]["severity"] == "critical"

    def test_present_entrypoint_is_clean(self, repo):
        d = repo / "src" / "app" / "api" / "analyze"
        d.mkdir(parents=True)
        (d / "route.ts").write_text("export async function POST() {}\n",
                                    encoding="utf-8")
        got = _trace(repo, _write_contract(repo))
        assert [f for f in got["findings"] if f["code"] == "missing_entrypoint"] == []

    def test_no_resolver_skips_only_that_check(self, repo):
        """스킵을 통과로 적지 않는다. 나머지 4종은 수행한다."""
        config, adapter, _cal = _load(repo)
        adapter = dict(adapter)
        adapter.pop("entrypoint_resolver", None)
        got = tr.run(repo, config, adapter, _write_contract(repo))
        assert got["entrypoint_resolver"] == "none"
        assert "missing_entrypoint" in got["skipped"]
        assert [f for f in got["findings"] if f["code"] == "missing_entrypoint"] == []
        # 오류 어휘 검사는 그대로 돌아야 한다.
        assert any(f["code"] == "missing_error_symbol" for f in got["findings"])
        assert len(got["checks_run"]) == 4


class TestUntestedEntrypointLink:
    """진입점 폴백은 **그 유닛과 연결된** 진입점만 본다 (G-2).

    `_entrypoint_referenced(unit, ...)` 가 `unit` 을 안 써서, 아무 진입점
    경로 하나가 테스트 blob 에 있으면 **모든 유닛**의 지적이 억제됐다.
    사실상 이 검사가 꺼져 있었고, §E6 의 baseline 3런은 그동안 발화할 수
    없는 검사를 재고 있었다.
    """

    CONTRACT = """# 계약: x

## 유닛
- `lib/alpha.ts · doAlpha(x: string): void`
- `lib/beta.ts · doBeta(x: string): void`

## 진입점
- `POST /api/alpha` → 201
"""

    def _repo(self, repo):
        (repo / "src" / "lib").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "lib" / "alpha.ts").write_text(
            "export function doAlpha(x: string) {}\n", encoding="utf-8")
        (repo / "src" / "lib" / "beta.ts").write_text(
            "export function doBeta(x: string) {}\n", encoding="utf-8")
        d = repo / "src" / "app" / "api" / "alpha"
        d.mkdir(parents=True, exist_ok=True)
        (d / "route.ts").write_text(
            "import { doAlpha } from '../../../lib/alpha';\n"
            "export async function POST() { doAlpha('x'); }\n", encoding="utf-8")
        t = repo / "src" / "lib" / "alpha.test.ts"
        # 진입점 경로만 언급하고 어느 심볼도 부르지 않는다.
        t.write_text("it('routes', () => { fetch('/api/alpha'); });\n",
                     encoding="utf-8")

    def test_한_진입점이_다른_유닛의_지적을_덮지_않는다(self, repo):
        self._repo(repo)
        got = _trace(repo, _write_contract(repo, self.CONTRACT), changed=[])
        untested = [f for f in got["findings"]
                    if f["code"] == "untested_contract_item"]
        symbols = {f.get("symbol") for f in untested}
        assert "doBeta" in symbols, \
            "진입점 하나가 blob 에 있다고 모든 유닛이 커버로 처리되면 안 된다"

    def test_연결을_못_풀면_evidence_에_적는다(self, repo):
        self._repo(repo)
        got = _trace(repo, _write_contract(repo, self.CONTRACT), changed=[])
        untested = [f for f in got["findings"]
                    if f["code"] == "untested_contract_item"]
        assert untested, "억제가 침묵으로 일어나면 안 된다"
        assert all(f.get("evidence") for f in untested)


class TestScopeSelectorWidth:
    """`scoped` 가 통합 테스트를 고르는가 (M28).

    `_tests_for_source` 가 소스의 stem 과 **같은 stem** 인 테스트만 골라,
    이름이 다른 통합 테스트는 수리 루프에서 한 번도 안 돌고 `full` 이
    뒤에서 잡았다.
    """

    CONTRACT = """# 계약: x

## 유닛
- `lib/match.ts · matchBooks(x: string): void`
"""

    LONE = """# 계약: x

## 유닛
- `lib/lone.ts · doLone(x: string): void`
"""

    def _select(self, repo, text):
        # `list_files` 는 추적 파일만 본다 — 새로 쓴 것을 인덱스에 올린다.
        _git(repo, "add", "-A")
        config, adapter, _c = _load(repo)
        return contract_mod.test_selectors(
            repo, config, adapter, contract_mod.parse(text, config))

    def _repo(self, repo):
        (repo / "src" / "lib").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "lib" / "match.ts").write_text(
            "export function matchBooks(x: string) {}\n", encoding="utf-8")
        (repo / "src" / "lib" / "match.test.ts").write_text(
            "import { matchBooks } from './match';\n", encoding="utf-8")
        (repo / "src" / "lib" / "edge-cases.test.ts").write_text(
            "import { matchBooks } from './match';\n"
            "it('edge', () => matchBooks('x'));\n", encoding="utf-8")
        (repo / "src" / "lib" / "unrelated.test.ts").write_text(
            "it('nope', () => {});\n", encoding="utf-8")

    def test_통합_테스트가_스코프에_들어온다(self, repo):
        self._repo(repo)
        got = self._select(repo, self.CONTRACT)
        assert any("edge-cases" in p for p in got["paths"]), got["paths"]

    def test_무관한_테스트는_들어오지_않는다(self, repo):
        self._repo(repo)
        got = self._select(repo, self.CONTRACT)
        assert not any("unrelated" in p for p in got["paths"]), got["paths"]

    def test_대응_테스트가_없는_소스는_unmatched_에_남는다(self, repo):
        """지금까지 이 경우는 **조용히 0경로를 기여했다.**"""
        (repo / "src" / "lib").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "lib" / "lone.ts").write_text(
            "export function doLone(x: string) {}\n", encoding="utf-8")
        got = self._select(repo, self.LONE)
        assert not any("lone" in p for p in got["paths"]), got["paths"]
        kinds = {u["kind"] for u in got["unmatched"]}
        assert "source" in kinds, got["unmatched"]

    def test_스코프가_전체에_가까우면_퇴화로_드러난다(self, repo):
        """'scoped 라고 부르면서 full 을 도는 것'이 새 자리의 조용한 통과다."""
        (repo / "src" / "lib").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "lib" / "match.ts").write_text(
            "export function matchBooks(x: string) {}\n", encoding="utf-8")
        for t in list((repo / "src").rglob("*.test.*")):
            t.unlink()
        for i in range(3):
            (repo / "src" / "lib" / ("t%d.test.ts" % i)).write_text(
                "import { matchBooks } from './match';\n", encoding="utf-8")
        got = self._select(repo, self.CONTRACT)
        assert got["degenerate"] is True, got
        assert got["selected_ratio"] >= 0.9


class TestScopeSeesUntrackedFiles:
    """04 가 03 이 방금 만든 파일을 보는가 (M50).

    `harness.list_files` 는 `git ls-files` 라 **추적 파일만** 낸다. 04 가 도는
    시점은 03 이 방금 코드를 쓴 직후이고 그 파일들은 아직 추적되지 않는다.
    P6 은 계약 유닛 8 중 **4가 `unmatched`** 였고 넷 다 그 런이 새로 만든
    `src/lib/unidentified.ts` 의 것이었다 — `scoped` 가 그 런의 핵심 모듈
    테스트(450줄)를 **수리 루프 내내 한 번도 안 돌았다.**

    05 의 `contract-trace` 는 이미 미추적을 함께 본다(`trace_contract.repo_files`).
    같은 계약을 두고 04 가 `unmatched: 4` 를, 05 가 `dropped: []` 를 적던 것이
    이 결함의 표면이다.

    **이 클래스 위의 `TestScopeSelectorWidth._select` 가 `git add -A` 를 하는
    것 자체가 이 결함의 증거였다** — 테스트가 결함을 우회해서 통과했다.
    """

    CONTRACT = """# 계약: x

## 유닛
- `lib/fresh.ts · doFresh(x: string): void`
"""

    def _fresh(self, repo):
        """03 이 방금 쓴 모양 — 파일은 있고 인덱스에는 없다."""
        (repo / "src" / "lib").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "lib" / "fresh.ts").write_text(
            "export function doFresh(x: string) {}\n", encoding="utf-8")
        (repo / "src" / "lib" / "fresh.test.ts").write_text(
            "import { doFresh } from './fresh';\n", encoding="utf-8")

    def _select(self, repo):
        config, adapter, _c = _load(repo)
        return contract_mod.test_selectors(
            repo, config, adapter, contract_mod.parse(self.CONTRACT, config))

    def test_미커밋_새_파일이_스코프에_들어온다(self, repo):
        self._fresh(repo)
        got = self._select(repo)
        assert any("fresh.test" in p for p in got["paths"]), got

    def test_미커밋_새_파일이_unmatched_로_떨어지지_않는다(self, repo):
        self._fresh(repo)
        got = self._select(repo)
        assert got["unmatched"] == [], got["unmatched"]

    def test_무시된_경로는_소스로_세지_않는다(self, repo):
        """`_workspace/` 의 계약 파일이 소스로 세어지면 안 된다."""
        self._fresh(repo)
        p = repo / "_workspace" / "contract_x.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.CONTRACT, encoding="utf-8")
        got = self._select(repo)
        assert not any("_workspace" in x for x in got["paths"]), got["paths"]

    def test_04_와_05_가_같은_파일_목록을_본다(self, repo):
        """두 페이즈가 같은 계약을 두고 다른 말을 하면 초록불의 뜻이 갈린다."""
        self._fresh(repo)
        assert (sorted(harness.list_files_with_untracked(repo))
                == sorted(tr.repo_files(repo)))

    def test_두_페이즈가_센_파일_수가_영수증에_남는다(self, repo):
        """같으니까 안 적는 것이 아니라, 갈라지면 보이게 적는다."""
        self._fresh(repo)
        got = self._select(repo)
        config, adapter, _c = _load(repo)
        contract_path = _write_contract(repo, self.CONTRACT)
        trace = tr.run(repo, config, adapter, contract_path, changed=[])
        assert got["repo_files"] == trace["repo_files"], (got, trace["repo_files"])
        assert got["repo_files"] > 0


class TestContractTraceBaseline:
    """오탐이 잦은 둘은 첫 3런 동안 warn_only 다 (§E6)."""

    def _contract_untested(self, repo):
        (repo / "src" / "lib" / "match.test.ts").write_text("// 아무것도 안 부른다\n",
                                                            encoding="utf-8")
        return _write_contract(repo)

    def test_untested_is_warn_only_inside_baseline(self, repo):
        ldg.seed(repo)
        got = _trace(repo, self._contract_untested(repo))
        f = next(f for f in got["findings"] if f["code"] == "untested_contract_item")
        assert f["resolution"] == "warn_only"

    def test_untested_becomes_a_real_finding_after_baseline(self, repo):
        ldg.seed(repo)
        for rid in ("r1", "r2", "r3"):
            ldg.append(repo, rid, "05", [_finding()])
        got = _trace(repo, self._contract_untested(repo))
        f = next(f for f in got["findings"] if f["code"] == "untested_contract_item")
        assert f["resolution"] != "warn_only"
        assert f["severity"] == "major"
        assert f["target_role"] == "test"

    def test_symbol_referenced_by_test_is_clean(self, repo):
        """심볼 문자열 **또는** 진입점 경로 — 둘 다 실패할 때만 지적한다."""
        ldg.seed(repo)
        got = _trace(repo, _write_contract(repo))   # match.test.ts 가 matchTitle 을 import 한다
        assert [f for f in got["findings"] if f["code"] == "untested_contract_item"] == []

    def test_out_of_contract_is_warn_only_inside_baseline(self, repo):
        ldg.seed(repo)
        (repo / "src" / "lib" / "match.ts").write_text(
            "export function matchTitle(): number { return 0 }\n"
            "export function 계약에없는함수(): void {}\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo), changed=["src/lib/match.ts"])
        f = next(f for f in got["findings"] if f["code"] == "out_of_contract")
        assert f["resolution"] == "warn_only"

    def test_out_of_contract_only_looks_at_changed_files(self, repo):
        """안 건드린 파일의 기존 심볼을 신규로 세면 온 리포가 지적이 된다."""
        (repo / "src" / "lib" / "기존.ts").write_text(
            "export function 아주오래된함수(): void {}\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo), changed=[])
        assert [f for f in got["findings"] if f["code"] == "out_of_contract"] == []

    # --- 변경된 파일이 아니라 **추가된 줄**을 본다 ----------------------------

    def _ooc(self, got):
        return sorted(f["symbol"] for f in got["findings"]
                      if f["code"] == "out_of_contract")

    def test_an_untouched_export_in_a_changed_file_is_not_new(self, repo):
        """**P3 의 24/32 가 이 자리다.**

        docstring 은 "신규 public 심볼" 이라 적는데 구현은 변경된 파일의 계약에
        없는 **모든** public 심볼을 셌다 — 새것인지 묻지 않았다. P2 46 + P3 32
        = 78/78 이 구조적 오탐이었고, 그중 24건이 `env.ts` 의 상수처럼 그 런이
        손도 안 댄 이름이었다.
        """
        f = repo / "src" / "lib" / "match.ts"
        f.write_text(f.read_text(encoding="utf-8")
                     + "export function 새로생긴함수(): void {}\n",
                     encoding="utf-8")
        # 같은 변경 집합에 계약 밖 심볼이 하나 더 있다 — 다만 **원래 있던 것**이다
        old = repo / "src" / "lib" / "오래된.ts"
        old.write_text("export const 오래된상수 = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "기존 심볼을 커밋한다")
        old.write_text("export const 오래된상수 = 1\n// 주석만 더한다\n",
                       encoding="utf-8")

        got = _trace(repo, _write_contract(repo),
                     changed=["src/lib/match.ts", "src/lib/오래된.ts"])
        assert self._ooc(got) == ["새로생긴함수"], got["findings"]

    def test_a_brand_new_file_is_all_new(self, repo):
        """추적되지 않는 파일은 본문 전체가 추가분이다 — 03 이 방금 쓴 코드다.

        diff 만 보고 폴백을 안 두면 03 이 만든 심볼이 통째로 안 보인다.
        """
        (repo / "src" / "lib" / "새파일.ts").write_text(
            "export function 갓태어난함수(): void {}\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo), changed=["src/lib/새파일.ts"])
        assert self._ooc(got) == ["갓태어난함수"], got["findings"]

    def test_a_removed_export_is_not_a_new_symbol(self, repo):
        """심볼을 **지우는 것**이 지적이 되면 안 된다."""
        f = repo / "src" / "lib" / "match.ts"
        f.write_text(f.read_text(encoding="utf-8")
                     + "export function 곧지울함수(): void {}\n",
                     encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "지울 함수를 커밋한다")
        f.write_text(f.read_text(encoding="utf-8")
                     .replace("export function 곧지울함수(): void {}\n", ""),
                     encoding="utf-8")

        got = _trace(repo, _write_contract(repo), changed=["src/lib/match.ts"])
        assert self._ooc(got) == [], got["findings"]


class TestOutOfContractReadsDataShapes:
    """P8 의 오탐 6/6 이 실제로 사라지는가 (M57). **이 증분의 성공 정의다.**

    앞의 `TestContractDataShapes` 는 파서가 이름을 모으는지를 묻고, 여기서는
    그 결과가 `out_of_contract` 까지 도달하는지를 묻는다. 둘이 갈라져 있어야
    "모으긴 하는데 검사가 안 쓴다" 를 잡을 수 있다.

    P8 의 여섯은 `env.ts` 의 상수 다섯과 `rate-limit.ts` 의 타입 하나였다.
    여기서는 같은 **모양**을 최소로 재현한다 — 실물 파일 내용을 복사하면
    이 테스트가 그 런의 코드에 묶인다.
    """

    CONTRACT = """# 계약: x

## 데이터 형태

- `RateLimitDecision { allowed: boolean; retryAfterSeconds: number }`
- 상수는 전부 `src/lib/env.ts` 에서 온다:
  `RATE_LIMIT_MAX_REQUESTS`(20) · `RATE_LIMIT_WINDOW_MS`(60_000)

## 유닛

- `lib/match.ts · matchTitle(a: string, b: string): number`
"""

    ENV_TS = """
export const RATE_LIMIT_MAX_REQUESTS = 20
export const RATE_LIMIT_WINDOW_MS = 60_000
"""
    RATE_LIMIT_TS = """
export type RateLimitDecision = { allowed: boolean }
"""
    EXTRA_TS = """
export const 계약에없는상수 = 3
"""

    def _write(self, repo):
        (repo / "src" / "lib" / "env.ts").write_text(
            self.ENV_TS, encoding="utf-8")
        (repo / "src" / "lib" / "rate-limit.ts").write_text(
            self.RATE_LIMIT_TS, encoding="utf-8")
        return ["src/lib/env.ts", "src/lib/rate-limit.ts"]

    def _ooc(self, got):
        return sorted(f["symbol"] for f in got["findings"]
                      if f["code"] == "out_of_contract")

    def test_데이터_형태에_적힌_이름은_계약_밖이_아니다(self, repo):
        """P8 이 여섯을 잡은 그 경로다. 이제 0 이어야 한다."""
        changed = self._write(repo)
        got = _trace(repo, _write_contract(repo, self.CONTRACT), changed=changed)
        assert self._ooc(got) == [], got["findings"]

    def test_그래도_계약에_없는_것은_여전히_잡는다(self, repo):
        """**검사를 무력화한 것이 아니다.**

        오탐을 없애려고 판정을 넓히면 진짜 위반이 함께 사라진다 — 그러면
        고친 것이 아니라 끈 것이다.
        """
        changed = self._write(repo)
        (repo / "src" / "lib" / "env.ts").write_text(
            (repo / "src" / "lib" / "env.ts").read_text(encoding="utf-8")
            + self.EXTRA_TS, encoding="utf-8")
        got = _trace(repo, _write_contract(repo, self.CONTRACT), changed=changed)
        assert self._ooc(got) == ["계약에없는상수"], got["findings"]


class TestContractTraceNoContract:

    def test_no_contract_mode_is_recorded_not_passed(self, repo):
        config, adapter, _cal = _load(repo)
        got = tr.run(repo, config, adapter, None, no_contract=True)
        assert got["status"] == "skipped_no_contract"
        assert got["findings"] == []
        assert got["checks_run"] == []


class TestContractTraceCli:

    def test_cli_emits_a_single_envelope_and_writes_the_file(self, repo, request_file):
        ldg.seed(repo)
        _write_contract(repo)
        init = cli.run_init(repo, "x", request_file)
        run_id = init["run_id"]
        env = cli.run_contract_trace(repo, contract="_workspace/contract_x.md",
                                     run_id=run_id)
        assert env["cmd"] == "contract-trace"
        assert env["exit"] in (0, 8)
        out = repo / "_workspace" / "runs" / run_id / "05_trace.json"
        assert out.exists()
        assert json.loads(out.read_text(encoding="utf-8"))["checks_run"]

    def test_cli_reports_utf8_without_escaping(self, repo, request_file):
        ldg.seed(repo)
        _write_contract(repo, CONTRACT.replace("matchTitle", "제목맞추기"))
        init = cli.run_init(repo, "x", request_file)
        run_id = init["run_id"]
        cli.run_contract_trace(repo, contract="_workspace/contract_x.md",
                               run_id=run_id)
        raw = (repo / "_workspace" / "runs" / run_id / "05_trace.json").read_text(
            encoding="utf-8")
        assert "제목맞추기" in raw


# ---------------------------------------------------------------------------
# L. precheck — 05 의 첫 검사. 무료이고, 뒤에서 되돌릴 일을 먼저 잡는다
# ---------------------------------------------------------------------------

import precheck as pc  # noqa: E402


def _branch(repo, name):
    _git(repo, "checkout", "-q", "-b", name)


def _bulk_change(repo, files, lines=1):
    for i in range(files):
        p = repo / "src" / "lib" / ("f%d.ts" % i)
        p.write_text("\n".join("export const v%d_%d = %d" % (i, j, j)
                               for j in range(lines)) + "\n", encoding="utf-8")


def _commit_all(repo, msg="wip"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


class TestPrecheckScope:
    """M40 — `scope` 를 받고 한 번도 쓰지 않았다.

    변경 집합이 늘 미커밋 diff 라, 06 에서 커밋 뒤에 부르면 `at_06` 이 항상
    0파일/0줄이었다 (P5 실측: `at_05` 8/91 · `at_06` 0/0). 같은 페이즈의 PR
    본문은 `main...HEAD` 로 11파일을 옳게 적었다 — 한 페이즈가 두 방법으로
    재고 다른 답을 냈다.
    """

    def test_커밋된_변경이_scope_pr_에_잡힌다(self, repo):
        """P5 의 증상 그 자체다."""
        _branch(repo, "feat-x")
        _bulk_change(repo, 3)
        _commit_all(repo)
        got = pc.run(repo, scope="pr")
        assert got["budget"]["files"] == 3, got["budget"]
        assert got["budget"]["lines"] > 0, got["budget"]

    def test_scope_worktree_는_미커밋만_본다(self, repo):
        _branch(repo, "feat-x")
        _bulk_change(repo, 3)
        _commit_all(repo)
        got = pc.run(repo, scope="worktree")
        assert got["budget"]["files"] == 0, got["budget"]

    def test_커밋과_미커밋이_이중계수되지_않는다(self, repo):
        _branch(repo, "feat-x")
        _bulk_change(repo, 1, lines=10)
        _commit_all(repo)
        _bulk_change(repo, 1, lines=11)      # 같은 파일을 한 줄 더 더럽힌다
        got = pc.run(repo, scope="pr")
        assert got["budget"]["files"] == 1, got["budget"]

    def test_미커밋만_있어도_scope_pr_이_본다(self, repo):
        """05 시점의 동작이다 — 03 이 방금 쓴 것은 아직 커밋 전이다."""
        _branch(repo, "feat-x")
        _bulk_change(repo, 2)
        got = pc.run(repo, scope="pr")
        assert got["budget"]["files"] == 2, got["budget"]

    def test_알_수_없는_scope_는_거부된다(self, repo):
        _branch(repo, "feat-x")
        with pytest.raises(ValueError):
            pc.run(repo, scope="staged")


def _probe_policy(repo, name, value):
    """실물 어댑터의 프로브 정책을 바꾼다.

예시 어댑터의 `api_key` 는 `on_missing: warn` 이다(키가 없으면 목업으로
    떨어지는 경로를 가정한 값이다). **exit 10 기전 자체를 보는 테스트는 그 정책에
    기대면 안 된다** — 기전과 어댑터의 정책은 다른 사실이다.
    """
    ap = repo / "harness" / "adapters" / "nextjs-ts.json"
    d = harness._read_json(ap)
    for probe in d["infra_preflight"]:
        if probe["name"] == name:
            probe["on_missing"] = value
    ap.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                  encoding="utf-8")


class TestPrecheckBudget:
    """예산 초과는 exit 9 다 — **자동 분할하지 않는다.** 범위 판단은 사람의 것이다."""

    def test_clean_small_change_passes(self, repo):
        _branch(repo, "feat-x")
        _bulk_change(repo, 1)
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 0, got["checks"]

    def test_too_many_files_is_exit_9(self, repo):
        _branch(repo, "feat-x")
        _bulk_change(repo, 12)          # budget.files_max 는 10 이다
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 9
        assert any(c["name"] == "예산" and not c["ok"] for c in got["checks"])

    def test_too_many_lines_is_exit_9(self, repo):
        _branch(repo, "feat-x")
        _bulk_change(repo, 1, lines=1200)   # budget.lines_max 는 1000 이다
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 9

    def test_untracked_file_counts_toward_the_budget(self, repo):
        """git diff 는 새 파일을 못 본다. 안 세면 예산이 사실보다 작게 잡힌다."""
        _branch(repo, "feat-x")
        _bulk_change(repo, 12)
        got = pc.run(repo, scope="pr")
        assert got["budget"]["files"] >= 12


class TestPrecheckBranch:

    def test_protected_branch_is_refused(self, repo):
        _bulk_change(repo, 1)           # main 위다
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 9
        assert any("보호" in c["message"] for c in got["checks"] if not c["ok"])

    def test_branch_pattern_mismatch_is_refused(self, repo):
        _branch(repo, "wip/아무거나")
        _bulk_change(repo, 1)
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 9
        assert any(c["name"] == "브랜치" and not c["ok"] for c in got["checks"])


class TestPrecheckInfra:
    """인프라 실패는 정책 실패와 다르다 — **카운터를 소모하지 않는다** (§E9)."""

    def test_env_probe_fires_only_when_the_path_is_touched(self, repo, monkeypatch):
        monkeypatch.delenv("EXAMPLE_API_KEY", raising=False)
        _branch(repo, "feat-x")
        _bulk_change(repo, 1)           # services/ 를 안 건드렸다
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 0

    def test_env_probe_failure_is_exit_10(self, repo, monkeypatch):
        monkeypatch.delenv("EXAMPLE_API_KEY", raising=False)
        _branch(repo, "feat-x")
        p = repo / "src" / "services"
        p.mkdir(parents=True)
        (p / "api-client.ts").write_text("export const a = 1\n", encoding="utf-8")
        _probe_policy(repo, "api_key", "fail")
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 10
        assert got["classification"] == "infra"
        assert got["counter_consumed"] is False

    def _touch_services(self, repo):
        p = repo / "src" / "services"
        p.mkdir(parents=True, exist_ok=True)
        (p / "api-client.ts").write_text("export const a = 1" + "\n",
                                        encoding="utf-8")

    def _on_missing(self, repo, value, why="목업으로 떨어진다"):
        ap = repo / "harness" / "adapters" / "nextjs-ts.json"
        d = harness._read_json(ap)
        for probe in d["infra_preflight"]:
            if probe["name"] == "api_key":
                if value is None:
                    probe.pop("on_missing", None)
                    probe.pop("why", None)
                else:
                    probe["on_missing"] = value
                    if why is not None:
                        probe["why"] = why
        ap.write_text(json.dumps(d, ensure_ascii=False, indent=2)+ "\n",
                      encoding="utf-8")

    def test_on_missing_warn_은_exit_10_을_내지_않는다(self, repo, monkeypatch):
        """M44 — P4 를 죽인 기전. 키가 없어도 목업이 돌면 회귀가 안 깨진다."""
        monkeypatch.delenv("EXAMPLE_API_KEY", raising=False)
        _branch(repo, "feat-x")
        self._touch_services(repo)
        self._on_missing(repo, "warn")
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 0, got["checks"]

    def test_면제는_통과가_아니라_gap_이다(self, repo, monkeypatch):
        """면제가 조용하면 그것은 면제가 아니라 구멍이다."""
        monkeypatch.delenv("EXAMPLE_API_KEY", raising=False)
        _branch(repo, "feat-x")
        self._touch_services(repo)
        self._on_missing(repo, "warn")
        got = pc.run(repo, scope="pr")
        assert got["gaps"] == ["infra_skipped:api_key"], got
        waived = [c for c in got["checks"] if c.get("waived")]
        assert len(waived) == 1, got["checks"]
        assert "목업으로 떨어진다" in waived[0]["message"], waived[0]

    def test_기본값은_여전히_fail_이다(self, repo, monkeypatch):
        monkeypatch.delenv("EXAMPLE_API_KEY", raising=False)
        _branch(repo, "feat-x")
        self._touch_services(repo)
        self._on_missing(repo, None)
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 10, got["checks"]
        assert got["classification"] == "infra"
        assert not got["gaps"], got["gaps"]

    def test_why_없는_warn_은_lint_가_거부한다(self, repo, phases, monkeypatch):
        self._on_missing(repo, "warn", why=None)
        ap = repo / "harness" / "adapters" / "nextjs-ts.json"
        d = harness._read_json(ap)
        for probe in d["infra_preflight"]:
            probe.pop("why", None)
        ap.write_text(json.dumps(d, ensure_ascii=False, indent=2)+ "\n",
                      encoding="utf-8")
        rows = [r for r in cli.lint_phases(repo)
                if r["status"] == "FAIL" and r["rule"] == "infra_preflight"]
        assert rows, cli.lint_phases(repo)

    def test_실물_어댑터가_스키마를_만족한다(self, repo, phases):
        assert _fails(_lint(repo), "infra_preflight") == []

    def test_present_env_probe_passes(self, repo, monkeypatch):
        monkeypatch.setenv("EXAMPLE_API_KEY", "sk-테스트")
        _branch(repo, "feat-x")
        p = repo / "src" / "services"
        p.mkdir(parents=True)
        (p / "api-client.ts").write_text("export const a = 1\n", encoding="utf-8")
        _probe_policy(repo, "api_key", "fail")
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 0

    def test_secret_value_never_appears_in_the_report(self, repo, monkeypatch):
        """precheck 결과는 원장·보고서로 간다. 값이 실리면 리포로 샌다."""
        monkeypatch.setenv("EXAMPLE_API_KEY", "sk-비밀값-12345")
        _branch(repo, "feat-x")
        p = repo / "src" / "services"
        p.mkdir(parents=True)
        (p / "api-client.ts").write_text("export const a = 1\n", encoding="utf-8")
        _probe_policy(repo, "api_key", "fail")
        got = pc.run(repo, scope="pr")
        assert "sk-비밀값-12345" not in json.dumps(got, ensure_ascii=False)


class TestPrecheckCli:

    def test_cli_emits_one_envelope(self, repo, request_file):
        _branch(repo, "feat-x")
        _bulk_change(repo, 1)
        cli.run_init(repo, "x", request_file)
        env = cli.run_precheck(repo, scope="pr")
        assert env["cmd"] == "precheck"
        assert env["exit"] in (0, 9, 10)

    def test_cli_runs_without_a_run(self, repo):
        """05 진입 전에도 부를 수 있어야 한다 — 무료 검사의 요점이다."""
        _branch(repo, "feat-x")
        _bulk_change(repo, 1)
        env = cli.run_precheck(repo, scope="pr")
        assert env["exit"] == 0


# ---------------------------------------------------------------------------
# M. 리뷰어 라우팅 — 결정론. when glob 이 정하고 우선순위는 배열 순서다
# ---------------------------------------------------------------------------

import review as rv  # noqa: E402


def _config(repo):
    return harness._read_json(repo / harness.CONFIG_REL)


class TestReviewerRouting:

    def test_api_change_wakes_the_security_reviewer(self, repo):
        got = rv.route(_config(repo), ["src/app/api/analyze/route.ts"], "normal")
        assert "sec" in [r["code"] for r in got["reviewers"]]

    def test_priority_order_is_array_order(self, repo):
        got = rv.route(_config(repo),
                       ["src/lib/schemas.ts", "src/app/api/x/route.ts",
                        "src/lib/a.ts"], "normal")
        codes = [r["code"] for r in got["reviewers"]]
        assert codes == sorted(codes, key=lambda c: _priority(repo, c))

    def test_small_profile_takes_gen_and_the_top_one(self, repo):
        """small 의 상한 2 는 gen 자리 하나 + 특화 리뷰어 하나다 (ADR-H043)."""
        got = rv.route(_config(repo),
                       ["src/lib/schemas.ts", "src/app/api/x/route.ts"], "small")
        assert [r["code"] for r in got["reviewers"]] == ["gen", "data"]
        assert got["capped"] is True

    def test_normal_profile_respects_the_cap(self, repo):
        changed = ["src/lib/schemas.ts", "src/app/api/x/route.ts",
                   "src/lib/a.ts", "src/lib/a.test.ts"]
        got = rv.route(_config(repo), changed, "normal")
        assert len(got["reviewers"]) <= 4

    def test_dropped_reviewers_are_named_not_silently_lost(self, repo):
        """상한에 걸려 빠진 리뷰어가 누구인지 드러나야 한다."""
        changed = ["src/lib/schemas.ts", "src/app/api/x/route.ts",
                   "src/lib/a.ts", "src/lib/a.test.ts"]
        got = rv.route(_config(repo), changed, "normal")
        assert got["dropped"], "상한으로 빠진 리뷰어가 이름으로 남아야 한다"

    def test_docs_reviewer_only_when_no_source_change(self, repo):
        with_src = rv.route(_config(repo), ["docs/x.md", "src/lib/a.ts"], "normal")
        assert "docs" not in [r["code"] for r in with_src["reviewers"]]
        docs_only = rv.route(_config(repo), ["docs/x.md"], "normal")
        assert "docs" in [r["code"] for r in docs_only["reviewers"]]

    def test_no_match_yields_zero_reviewers_and_that_is_a_failed_review(self, repo):
        """계획된 리뷰어가 0개면 review05.status 는 failed 다 (§E1)."""
        got = rv.route(_config(repo), ["아무데도/안걸리는.txt"], "normal")
        assert got["reviewers"] == []
        assert rv.status(planned=0, ok=0) == "failed"


class TestGeneralReviewerRouting:
    """gen 은 glob 이 아니라 **역할 소유**로 켜진다 (ADR-H043).

    07 이 깨끗한 런의 내장 리뷰를 생략하는 근거가 "05 의 gen 이 봤다" 이므로,
    소스 변경이 있는데 gen 이 안 켜지는 경로가 있으면 그 근거가 무너진다.
    프로젝트가 `roles[].owns` 를 다른 레이아웃으로 바꿔도 따라가야 한다.
    """

    def test_source_change_wakes_gen_first(self, repo):
        got = rv.route(_config(repo), ["src/lib/a.ts"], "normal")
        assert [r["code"] for r in got["reviewers"]][0] == "gen"

    def test_gen_follows_roles_owns_not_a_glob(self, repo):
        cfg = _config(repo)
        cfg["roles"][0]["owns"] = ["lib/**"]
        got = rv.route(cfg, ["lib/a.ts"], "normal")
        # 다른 리뷰어의 glob 은 src/** 라 아무도 안 걸린다. gen 만 걸린다.
        assert [r["code"] for r in got["reviewers"]] == ["gen"]

    def test_docs_only_does_not_wake_gen(self, repo):
        got = rv.route(_config(repo), ["docs/x.md"], "normal")
        assert "gen" not in [r["code"] for r in got["reviewers"]]

    def test_excluded_test_files_do_not_wake_gen_alone(self, repo):
        """impl 이 excludes 한 테스트 파일은 test 역할이 소유한다 — 그래도 소유다."""
        got = rv.route(_config(repo), ["src/lib/a.test.ts"], "normal")
        assert "gen" in [r["code"] for r in got["reviewers"]]

    def test_validate_accepts_when_role_owned_without_glob(self, repo):
        cfg = _config(repo)
        assert rv.validate(repo, cfg) == []
        gen = [r for r in cfg["reviewers"] if r["code"] == "gen"][0]
        assert not gen.get("when"), "gen 은 glob 을 갖지 않는다"
        gen.pop("when_role_owned")
        errs = rv.validate(repo, cfg)
        assert any("'gen'" in e and "켜지지 않는다" in e for e in errs), errs


class TestReviewMode:
    """작은 diff 는 통합 모드다 — 같은 diff 를 여러 번 보내지 않는다."""

    def test_small_diff_is_merged_mode(self, repo):
        assert rv.mode(_config(repo), diff_lines=20) == "merged"

    def test_large_diff_is_fanout(self, repo):
        assert rv.mode(_config(repo), diff_lines=900) == "fanout"


class TestReviewerIsolation:
    """작성자는 리뷰어가 될 수 없다. 자기 글을 리뷰한 것은 독립 관측이 아니다."""

    def test_author_agent_as_reviewer_is_rejected(self, repo):
        cfg = _config(repo)
        cfg["reviewers"] = [{"code": "x", "skill": "impl-writer",
                             "priority": 1, "when": ["src/**"]}]
        errs = rv.validate(repo, cfg)
        assert any("격리" in e or "작성자" in e for e in errs)

    def test_missing_skill_file_is_caught_before_launch(self, repo):
        cfg = _config(repo)
        cfg["reviewers"] = [{"code": "x", "skill": "없는-리뷰어",
                             "priority": 1, "when": ["src/**"]}]
        errs = rv.validate(repo, cfg)
        assert any("없는-리뷰어" in e for e in errs)

    def test_duplicate_code_is_rejected(self, repo):
        cfg = _config(repo)
        cfg["reviewers"] = [
            {"code": "a", "skill": "architecture-reviewer", "priority": 1,
             "when": ["src/**"]},
            {"code": "a", "skill": "security-reviewer", "priority": 2,
             "when": ["src/**"]}]
        assert any("code" in e for e in rv.validate(repo, cfg))

    def test_real_config_passes_validation(self, repo):
        """실물이 자기 검사를 통과해야 한다."""
        assert rv.validate(repo, _config(repo)) == []


class TestReviewerSkillDocs:
    """스킬 문서와 기계가 `quote` 의 대조 대상을 같게 말해야 한다 (M51).

    기계는 리뷰어 **자신의 `.raw.md`** 와 대조하는데(`verdict.check_review`
    가 받는 `raw_text`) 스킬 다섯은 "diff 원문의 부분문자열" 이라 적고 있었다.
    문서대로 diff 를 인용하면 exit 8 이고 **그 반려는 리뷰어 잘못이 아니다.**

    픽스처가 아니라 **실물**을 읽는다 — 정합을 물어야 할 대상이 실물이고,
    `COPIED` 가 이미 같은 규율로 실물을 복사한다.
    """

    def test_모든_스킬이_대조_대상을_자기_원문으로_적는다(self):
        docs = [rel for rel in COPIED if rel.startswith(".claude/skills/")]
        # **실물 config 의 리뷰어 수와 같아야 한다.** 숫자를 박으면 리뷰어를
        # 더할 때 이 검사가 새 스킬을 안 보는 채로 깨진다 (ADR-H043 에서 5→6).
        real = harness._read_json(ROOT / harness.CONFIG_REL).get("reviewers")
        assert len(docs) == len(real), (docs, [r["code"] for r in real])
        for rel in docs:
            lines = [ln for ln
                     in (ROOT / rel).read_text(encoding="utf-8").splitlines()
                     if "`quote`" in ln and "부분문자열" in ln]
            # **줄이 있는지도 함께 센다.** 없어도 통과하는 검사라면 그 줄을
            # 지우는 것을 못 막고, 그러면 리뷰어는 대조 대상을 어디서도 못 읽는다.
            assert len(lines) == 1, "%s: %r" % (rel, lines)
            assert ".raw.md" in lines[0], "%s: %r" % (rel, lines[0])
            assert "diff" not in lines[0], "%s: %r" % (rel, lines[0])


def _priority(repo, code):
    for r in _config(repo).get("reviewers") or []:
        if r["code"] == code:
            return r["priority"]
    raise AssertionError(code)


# ---------------------------------------------------------------------------
# N. 05 제출 판정 — 01 의 검사 + 리뷰어가 여럿이라 생기는 넷
# ---------------------------------------------------------------------------

RAW_ONE = """## major

`route.ts` 가 인가를 건너뛴다.
"""

RAW_TWO = """## critical

첫째.

## minor

둘째.
"""


def _sub(reviewer="arch", by_checklist=None, **kw):
    payload = {"reviewer": reviewer, "round": 1, "status": "ok",
               "by_checklist": by_checklist if by_checklist is not None else {
                   "의존 방향": [{"id": "F-1", "category": "AUTHZ_MISSING_RULE",
                              "severity": "major", "target_role": "impl",
                              "title": "인가 규칙이 빠졌다", "path": "x.ts",
                              "quote": "인가를 건너뛴다"}],
                   "네이밍": []},
               "resolved_from_previous": [], "need_more_context": []}
    payload.update(kw)
    return payload


class TestReview05Structure:

    def test_missing_by_checklist_is_rejected(self, repo):
        """0건인 체크리스트도 명시해야 한다 — 누락과 '보고 아무것도 없었다'는 다르다."""
        got = rv.check(repo, _config(repo), _sub(by_checklist={}), RAW_ONE, [])
        assert got["exit"] == 8
        assert any("by_checklist" in e for e in got["errors"])

    def test_zero_item_checklist_is_accepted(self, repo):
        got = rv.check(repo, _config(repo), _sub(), RAW_ONE, [])
        assert got["ok"], got["errors"]

    def test_flatten_reads_every_checklist(self, repo):
        payload = _sub(by_checklist={
            "가": [{"id": "F-1", "severity": "major", "title": "하나"}],
            "나": [{"id": "F-2", "severity": "minor", "title": "둘"}]})
        assert len(rv.flatten(payload)) == 2

    def test_heading_count_must_match_findings(self, repo):
        """M20 이 이 검사를 문서화하지 않아 생긴 결함이다. 05 는 리뷰어 수만큼 곱해진다."""
        got = rv.check(repo, _config(repo), _sub(), RAW_TWO, [])
        assert got["exit"] == 8
        assert any("헤딩" in e for e in got["errors"])

    def test_forged_quote_is_rejected(self, repo):
        payload = _sub()
        payload["by_checklist"]["의존 방향"][0]["quote"] = "원문에 없는 인용"
        got = rv.check(repo, _config(repo), payload, RAW_ONE, [])
        assert got["exit"] == 8


class TestReview05Isolation:

    def test_author_agent_submission_is_rejected(self, repo):
        got = rv.check(repo, _config(repo), _sub(reviewer="impl-writer"),
                       RAW_ONE, [])
        assert got["exit"] == 8
        assert any("독립 관측" in e for e in got["errors"])

    def test_unrouted_reviewer_is_rejected(self, repo):
        """라우팅이 부르지 않은 리뷰어의 제출은 받지 않는다."""
        got = rv.check(repo, _config(repo), _sub(reviewer="아무개"), RAW_ONE, [])
        assert got["exit"] == 8


class TestReview05Enforcement:
    """검토 제외 목록의 category 는 드롭하되 **센다.**"""

    def test_excluded_category_is_dropped_and_counted(self, repo):
        payload = _sub(by_checklist={
            "경계": [{"id": "F-1", "category": "BOUNDARY_VIOLATION",
                    "severity": "major", "target_role": "impl",
                    "title": "경계 위반", "quote": "인가를 건너뛴다"}]})
        # 원문에는 리뷰어가 **쓴 만큼** 헤딩이 있다. 드롭은 그 뒤의 일이다.
        got = rv.check(repo, _config(repo), payload, RAW_ONE, [],
                       excluded=["BOUNDARY_VIOLATION"])
        assert got["ok"], got["errors"]
        assert got["dropped_by_enforcement"] == 1
        assert got["findings"] == []
        assert got["dropped_categories"] == ["BOUNDARY_VIOLATION"]

    def test_dropped_findings_do_not_break_the_heading_count(self, repo):
        """드롭은 리뷰어의 잘못이 아니다 — 원문 헤딩은 낸 만큼 있다."""
        payload = _sub(by_checklist={
            "경계": [{"id": "F-1", "category": "BOUNDARY_VIOLATION",
                    "severity": "major", "target_role": "impl",
                    "title": "경계 위반", "quote": "인가를 건너뛴다"}],
            "인가": [{"id": "F-2", "category": "AUTHZ_MISSING_RULE",
                    "severity": "major", "target_role": "impl",
                    "title": "인가 누락", "quote": "인가를 건너뛴다"}]})
        raw = RAW_ONE + "\n" + RAW_ONE      # 헤딩 2개 = findings 2개
        got = rv.check(repo, _config(repo), payload, raw, [],
                       excluded=["BOUNDARY_VIOLATION"])
        assert got["ok"], got["errors"]
        assert len(got["findings"]) == 1
        assert got["dropped_by_enforcement"] == 1
        assert got["dropped_by_enforcement"] == 1


class TestReview05Vocabulary:
    """어휘 밖 category 를 **낸 리뷰어에게** 돌려준다 (M46).

    지금까지 이 검사는 `ledger.append` 에만 있었고, 그것은 **리뷰어 전원이
    모여 병합된 뒤에** 돈다. 그래서 셋 중 하나가 어휘 밖을 내면 exit 8 이
    마지막 제출자에게 가고, 그 제출자는 남의 findings 를 고칠 수 없어
    **스스로 빠져나올 수 없었다.** 빠져나가는 유일한 길이 리뷰 회차 예산을
    태우는 것이고, P6 에서 실제로 셋 전원 재제출을 낳았다(events seq 45~51).

    리뷰어별 층에는 이미 `attempts` 2회 예산과 강등 경로가 있다. 검사를 그
    층으로 내리면 위반한 리뷰어가 그 기계를 그대로 탄다 — M20 이 고친
    "리뷰어의 잘못이 아닌 것으로 리뷰어를 벌한다"의 같은 형태다.
    """

    @staticmethod
    def _taxonomy(repo):
        """실물 어휘를 복사한다 — 실물이 바뀌면 이 검사가 먼저 깨진다."""
        dst = repo / ldg.TAXONOMY_REL
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((ROOT / ldg.TAXONOMY_REL).read_text(encoding="utf-8"),
                       encoding="utf-8")
        return ldg.categories(repo)

    def _bad(self):
        return _sub(by_checklist={
            "의존 방향": [{"id": "F-1", "category": "지어낸_코드",
                       "severity": "major", "target_role": "impl",
                       "title": "인가 규칙이 빠졌다", "path": "x.ts",
                       "quote": "인가를 건너뛴다"}],
            "네이밍": []})

    def test_어휘_밖_category_는_제출_시점에_거부된다(self, repo):
        got = rv.check(repo, _config(repo), self._bad(), RAW_ONE, [],
                       known=self._taxonomy(repo))
        assert got["exit"] == 8
        assert any("taxonomy" in e for e in got["errors"]), got["errors"]

    def test_거부_메시지가_낸_finding_과_어휘를_함께_말한다(self, repo):
        """무엇이 틀렸는지 모르면 재제출이 추측이 된다."""
        got = rv.check(repo, _config(repo), self._bad(), RAW_ONE, [],
                       known=self._taxonomy(repo))
        joined = " ".join(got["errors"])
        assert "F-1" in joined and "지어낸_코드" in joined, joined
        assert "AUTHZ_MISSING_RULE" in joined, "허용 어휘를 보여 줘야 한다"

    def test_어휘_안이면_통과한다(self, repo):
        got = rv.check(repo, _config(repo), _sub(), RAW_ONE, [],
                       known=self._taxonomy(repo))
        assert got["ok"], got["errors"]

    def test_known_을_안_주면_검사하지_않는다(self, repo):
        """호출부가 taxonomy 를 못 읽는 경우까지 여기서 막지 않는다."""
        got = rv.check(repo, _config(repo), self._bad(), RAW_ONE, [])
        assert got["ok"], got["errors"]

    def _at_05(self, repo, paths, s):
        """05 제출을 받을 수 있는 최소 상태 — 대조가 끝났고 라우팅이 확정됐다."""
        self._taxonomy(repo)
        st.set_phase_status(s, "04-gate", "passed")
        s["phase"] = "05-code-review"
        node = s.setdefault("phases", {}).setdefault("05-code-review", {})
        node["trace"] = {"status": "ok", "blocking": 0}
        node["planned"] = ["arch", "sec"]
        st.save(paths, s)

    def _submit(self, repo, paths, payload, code):
        j = paths.run_dir / ("05_review_%s.json" % code)
        j.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        (paths.run_dir / ("05_review_%s.raw.md" % code)).write_text(
            RAW_ONE, encoding="utf-8")
        return cli.run_record(repo, phase="05", file=str(j),
                              reviewer=code, round_=1)

    def test_어휘_위반이_낸_리뷰어에게_돌아간다(self, gated, phases):
        """P6 은 이것이 마지막 제출자에게 갔고 셋 전원이 재제출했다."""
        repo, paths, s = gated
        self._at_05(repo, paths, s)
        got = self._submit(repo, paths, dict(self._bad(), reviewer="arch"), "arch")
        assert got["exit"] == 8, got
        assert "taxonomy" in json.dumps(got, ensure_ascii=False)

    def test_어휘_위반은_그_리뷰어의_제출_시도로_세어진다(self, gated, phases):
        """`attempts` 예산과 강등 경로를 타야 스스로 빠져나올 수 있다."""
        repo, paths, s = gated
        self._at_05(repo, paths, s)
        self._submit(repo, paths, dict(self._bad(), reviewer="arch"), "arch")
        _p, after = st.load(repo, paths.run_id)
        node = after["phases"]["05-code-review"]
        assert node.get("attempts", {}).get("1", {}).get("arch") == 1, node

    def test_봉투가_쓸_수_있는_어휘를_먼저_말한다(self, repo):
        """M20 의 원칙 — 리뷰어가 모르면 exit 8 이고, 모르게 둔 것은 봉투 잘못이다."""
        self._taxonomy(repo)
        got = cli._vocabulary_render(repo)
        assert "AUTHZ_MISSING_RULE" in got and "CONTRACT_DEFECT" in got, got
        assert "지어내지" in got or "지어낸" in got, got

    def test_어휘를_못_읽으면_봉투가_그렇게_말한다(self, repo):
        """빈 목록을 '어휘가 없다'로 내면 리뷰어가 무엇을 써도 튕긴다."""
        got = cli._vocabulary_render(repo)
        assert "읽지 못했다" in got, got

    def test_다른_리뷰어의_슬롯은_말려들지_않는다(self, gated, phases):
        """교착의 핵심은 남의 잘못으로 내가 못 빠져나가는 것이었다."""
        repo, paths, s = gated
        self._at_05(repo, paths, s)
        self._submit(repo, paths, dict(self._bad(), reviewer="arch"), "arch")
        got = self._submit(repo, paths, _sub(reviewer="sec"), "sec")
        _p, after = st.load(repo, paths.run_id)
        node = after["phases"]["05-code-review"]
        assert node.get("attempts", {}).get("1", {}).get("sec") is None, node
        assert got["exit"] != 8 or "taxonomy" not in json.dumps(
            got, ensure_ascii=False), got


class TestReview05SlugVocabulary:
    """어휘가 **선언된** 카테고리에서만 `rule_slug` 가 필수다 (ADR-H035).

    전면 선택이면 리뷰어가 그냥 안 적어 C5 가 아무것도 안 고치고, 전면
    필수면 `OTHER`·`CONTRACT_DEFECT` 에 억지 슬러그를 만들게 되어 M46 이
    고친 회차 예산 소진이 재현되는데 이번엔 **탈출구 자체가 없다.**
    면제 목록은 코드에 없다 — `validate_taxonomy` 가 *"승격 못 하는
    카테고리는 어휘를 선언할 수 없다"* 를 강제하므로 스키마가 답한다.
    """

    _taxonomy = TestReview05Vocabulary.__dict__["_taxonomy"]
    _at_05 = TestReview05Vocabulary._at_05
    _submit = TestReview05Vocabulary._submit

    def _sub_drift(self, rule_slug=None, **kw):
        f = {"id": "F-1", "category": "DOC_CODE_DRIFT", "severity": "major",
             "target_role": "impl", "title": "주석이 코드와 어긋난다",
             "path": "x.ts", "quote": "인가를 건너뛴다"}
        if rule_slug is not None:
            f["rule_slug"] = rule_slug
        return _sub(by_checklist={"문서 정합": [f], "네이밍": []}, **kw)

    def test_어휘가_선언된_카테고리는_슬러그가_필수다(self, repo):
        got = rv.check(repo, _config(repo), self._sub_drift(), RAW_ONE, [],
                       known=self._taxonomy(repo))
        assert got["exit"] == 8, got
        assert any("rule_slug" in e for e in got["errors"]), got["errors"]

    def test_어휘가_없는_카테고리는_면제다(self, repo):
        """**05 회귀 0의 증명이다.** `_sub()` 의 기본값은
        `AUTHZ_MISSING_RULE` 이고 그 카테고리는 어휘를 선언하지 않는다 —
        기존 05 테스트가 한 줄도 안 바뀌어야 하는 이유가 여기 있다.
        """
        got = rv.check(repo, _config(repo), _sub(), RAW_ONE, [],
                       known=self._taxonomy(repo))
        assert got["ok"], got["errors"]

    def test_어휘_안의_슬러그는_통과한다(self, repo):
        got = rv.check(repo, _config(repo),
                       self._sub_drift(rule_slug="doc_contradicts_code"),
                       RAW_ONE, [], known=self._taxonomy(repo))
        assert got["ok"], got["errors"]

    def test_어휘_밖_슬러그는_제출_시점에_거부된다(self, repo):
        """`ledger.append` 는 어휘를 안 본다 — 막는 것은 이 층이다 (M46).

        병합 뒤에 돌면 exit 8 이 마지막 제출자에게 가고 그는 남의 findings 를
        고칠 수 없어 스스로 못 빠져나온다.
        """
        got = rv.check(repo, _config(repo),
                       self._sub_drift(rule_slug="지어낸_슬러그"),
                       RAW_ONE, [], known=self._taxonomy(repo))
        assert got["exit"] == 8, got
        assert any("rule_slug" in e for e in got["errors"]), got["errors"]

    def test_거부_메시지가_그_카테고리의_어휘를_note_와_함께_말한다(self, repo):
        """무엇이 틀렸는지 모르면 재제출이 추측이 된다 (M20).

        `note` 까지 실어야 하는 이유는 실측이다 — `DOC_CODE_DRIFT` 를 내는
        arch·data·sec 는 `docs-reviewer/SKILL.md` 를 읽지 않으므로, 이름만
        나열하면 두 슬러그를 언제 가르는지 모른 채 고른다.
        """
        got = rv.check(repo, _config(repo),
                       self._sub_drift(rule_slug="지어낸_슬러그"),
                       RAW_ONE, [], known=self._taxonomy(repo))
        joined = " ".join(got["errors"])
        assert "F-1" in joined and "지어낸_슬러그" in joined, joined
        assert "doc_contradicts_code" in joined, "허용 어휘를 보여 줘야 한다"
        assert "같은 사실이 두 곳에" in joined, "note 까지 실어야 한다"

    def test_known_을_안_주면_슬러그도_검사하지_않는다(self, repo):
        """호출부가 taxonomy 를 못 읽는 경우까지 여기서 막지 않는다 — 기존 규약."""
        got = rv.check(repo, _config(repo), self._sub_drift(), RAW_ONE, [])
        assert got["ok"], got["errors"]

    def test_슬러그_위반이_그_리뷰어의_제출_시도로_세어진다(self, gated, phases):
        """`attempts` 예산과 강등 경로를 타야 스스로 빠져나올 수 있다 (M46)."""
        repo, paths, s = gated
        self._at_05(repo, paths, s)
        got = self._submit(repo, paths,
                           dict(self._sub_drift(), reviewer="arch"), "arch")
        assert got["exit"] == 8, got
        _p, after = st.load(repo, paths.run_id)
        node = after["phases"]["05-code-review"]
        assert node.get("attempts", {}).get("1", {}).get("arch") == 1, node

    def test_봉투가_카테고리별_슬러그를_note_와_함께_먼저_말한다(self, repo):
        """M20 — 리뷰어가 모르면 exit 8 이고, 모르게 둔 것은 봉투 잘못이다."""
        self._taxonomy(repo)
        got = cli._vocabulary_render(repo)
        assert "doc_contradicts_code" in got, got
        assert "nothing_locked" in got, got
        assert "같은 사실이 두 곳에" in got, "note 가 화물이다"

    def test_봉투가_어휘_없는_카테고리는_요구하지_않는다고_말한다(self, repo):
        """침묵으로 두면 "안 적어도 되나" 가 리뷰어의 추측이 된다."""
        self._taxonomy(repo)
        got = cli._vocabulary_render(repo)
        assert "AUTHZ_MISSING_RULE" in got, got
        assert "요구하지 않는다" in got, got

class TestReview05Truncation:

    def test_over_findings_max_keeps_only_blocking(self, repo):
        cfg = _config(repo)
        cfg["review"]["findings_max"] = 2
        items = [{"id": "F-%d" % i, "category": "NAMING",
                  "severity": "minor" if i % 2 else "critical",
                  "target_role": "impl", "title": "제목%d" % i}
                 for i in range(6)]
        raw = "\n".join("## %s\n\n본문\n" % it["severity"] for it in items)
        got = rv.check(repo, cfg, _sub(by_checklist={"전부": items}), raw, [])
        assert got["truncated"] is True
        assert all(f["severity"] in ("critical", "major") for f in got["findings"])


class TestReview05Merge:
    """2인 이상이 지적한 항목은 severity 가 한 단계 오른다."""

    def _f(self, title="같은 지적", severity="major"):
        return {"category": "NAMING", "target_role": "impl",
                "title": title, "severity": severity}

    def test_two_reviewers_raise_severity(self, repo):
        merged = rv.merge([
            {"reviewer": "arch", "findings": [self._f()]},
            {"reviewer": "sec", "findings": [self._f()]}])
        assert len(merged) == 1
        assert merged[0]["severity"] == "critical"
        assert merged[0]["severity_raised_from"] == "major"
        assert sorted(merged[0]["reported_by"]) == ["arch", "sec"]

    def test_one_reviewer_keeps_severity(self, repo):
        merged = rv.merge([{"reviewer": "arch", "findings": [self._f()]}])
        assert merged[0]["severity"] == "major"
        assert "severity_raised_from" not in merged[0]

    def test_critical_does_not_overflow(self, repo):
        merged = rv.merge([
            {"reviewer": "arch", "findings": [self._f(severity="critical")]},
            {"reviewer": "sec", "findings": [self._f(severity="critical")]}])
        assert merged[0]["severity"] == "critical"

    def test_same_reviewer_twice_does_not_raise(self, repo):
        """한 리뷰어가 두 번 낸 것은 독립 관측 둘이 아니다."""
        merged = rv.merge([
            {"reviewer": "arch", "findings": [self._f()]},
            {"reviewer": "arch", "findings": [self._f()]}])
        assert merged[0]["severity"] == "major"


class TestReview05Status:
    """findings 개수와 **분리한다** — §E1 이 가장 위험한 구멍이라 부른 것."""

    def test_all_ok(self, repo):
        assert rv.status(planned=3, ok=3) == "ok"

    def test_partial_is_degraded(self, repo):
        assert rv.status(planned=3, ok=1) == "degraded"

    def test_all_failed(self, repo):
        assert rv.status(planned=3, ok=0) == "failed"

    def test_zero_planned_is_failed_not_ok(self, repo):
        """아무도 안 부른 것은 통과가 아니라 미수행이다."""
        assert rv.status(planned=0, ok=0) == "failed"


class TestReview05InlineBudget:

    def test_small_diff_goes_inline(self, repo):
        assert rv.inline_budget(_config(repo), "a\nb\n")["inline"] is True

    def test_huge_diff_falls_back_to_paths(self, repo):
        got = rv.inline_budget(_config(repo), "x" * 40000)
        assert got["inline"] is False
        assert got["fallback"] == "경로 전달"
        assert got["over"], "무엇이 상한을 넘었는지 드러나야 한다"

    def test_hangul_counts_as_bytes_not_characters(self, repo):
        """문자로 세면 UTF-8 페이로드가 상한의 3배까지 통과한다."""
        got = rv.inline_budget(_config(repo), "가" * 9000)
        assert got["bytes"] == 27000
        assert got["inline"] is False


# ---------------------------------------------------------------------------
# O. 05 페이즈 파일과 전이 — FUTURE 는 다음 진입점을 계속 가리켜야 한다
# ---------------------------------------------------------------------------

class TestPhase05File:

    def test_phase_file_loads(self, repo):
        loaded, broken = cli.load_phases(ROOT)
        assert broken == []
        assert "05-code-review" in loaded

    def test_transition_chain_reaches_05(self, repo):
        loaded, _ = cli.load_phases(ROOT)
        assert loaded["04-gate"]["front"]["on_success"] == "05-code-review"

    def test_실물_페이즈에_FUTURE_가_남지_않았다(self, repo):
        """**여덟이 다 섰다.** 다섯 번의 증분 동안 다음 진입점을 가리키던
        그 한 줄이 처음으로 사라진다."""
        findings = cli.lint_phases(ROOT)
        future = [f for f in findings
                  if f["rule"] == "on_success" and f["status"] == "WARN"]
        assert future == [], future

    def test_실물_페이즈가_여덟이고_08_은_done_을_가리킨다(self, repo):
        """**M24.** 08 이 아무것도 안 가리키면 런이 닫히는 자리가 없다."""
        loaded, broken = cli.load_phases(ROOT)
        assert broken == []
        assert sorted(loaded) == ["00-triage", "01-plan", "02-cross-verify",
                                  "03-implement", "04-gate", "05-code-review",
                                  "06-pr", "07-pr-review", "08-report"]
        assert loaded["08-report"]["front"].get("on_success") == st.DONE

    def test_전이_사슬이_01_에서_done_까지_이어진다(self, repo):
        """단언 하나로 그래프 전체를 못박는다."""
        loaded, _ = cli.load_phases(ROOT)
        chain, cur = [], "01-plan"
        while cur in loaded:
            chain.append(cur)
            cur = loaded[cur]["front"].get("on_success")
        assert chain == ["01-plan", "02-cross-verify", "03-implement", "04-gate",
                         "05-code-review", "06-pr", "07-pr-review", "08-report"]
        assert cur == st.DONE

    def test_lint_passes_on_the_real_phases(self, repo):
        bad = [f for f in cli.lint_phases(ROOT) if f["status"] == "FAIL"]
        assert bad == [], bad

    def test_taxonomy_is_no_longer_skipped(self, repo):
        """원장이 생겼으므로 SKIP 이 실제 검사로 바뀌어야 한다."""
        findings = cli.lint_phases(ROOT)
        tax = [f for f in findings if f["rule"] == "taxonomy"]
        assert not any(f["status"] == "SKIP" for f in tax)

    def test_submission_format_documents_the_raw_md_rule(self, repo):
        """M20 의 회귀 — 페이즈 파일이 그 규칙을 실제로 적고 있는가."""
        loaded, _ = cli.load_phases(ROOT)
        body = loaded["05-code-review"]["body"]
        section = cli._section(body, "## 제출 형식")
        assert ".raw.md" in section
        assert "헤딩" in section

    def test_review_repair_counter_is_known(self, repo):
        assert "review_repair" in st.COUNTERS


# ---------------------------------------------------------------------------
# P. 05 흐름 — next 가 라우팅을 확정하고 record 가 원장에 쌓는다
# ---------------------------------------------------------------------------

def _enter_05(repo, request_file, phases):
    """04 까지를 상태로 위조하고 05 에 진입시킨다.

    01~04 를 실제로 돌리려면 서브에이전트가 필요하다. 여기서 보려는 것은 05 의
    배선이므로 그 앞은 상태로 세운다 — **다만 계약 파일과 지문은 실물이다.**
    """
    init = cli.run_init(repo, "x", request_file)
    run_id = init["run_id"]
    paths, s = st.load(repo, run_id)
    for pid in ("01-plan", "02-cross-verify", "03-implement", "04-gate"):
        st.set_phase_status(s, pid, "passed")
    s["phase"] = "05-code-review"
    s["contract"] = {"mode": "contract", "present": True}
    st.save(paths, s)

    c = repo / "_workspace" / ("contract_%s.md" % "x")
    c.parent.mkdir(parents=True, exist_ok=True)
    c.write_text(CONTRACT, encoding="utf-8")
    return run_id, paths


def _reviewer_files(paths, code, findings, raw=None):
    j = paths.run_dir / ("05_review_%s.json" % code)
    j.write_text(json.dumps({
        "reviewer": code, "round": 1, "status": "ok",
        "by_checklist": {"전부": findings},
        "resolved_from_previous": [], "need_more_context": []},
        ensure_ascii=False), encoding="utf-8")
    body = raw if raw is not None else "\n".join(
        "## %s\n\n%s\n" % (f["severity"], f.get("quote") or f["title"])
        for f in findings)
    j.with_name(j.name.replace(".json", ".raw.md")).write_text(
        "# 리뷰\n\n" + body, encoding="utf-8")
    return j


class TestPhase05Wiring:

    def test_next_freezes_the_routing(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        (repo / "src" / "app" / "api" / "x").mkdir(parents=True)
        (repo / "src" / "app" / "api" / "x" / "route.ts").write_text(
            "export async function POST() {}\n", encoding="utf-8")
        env = cli.run_next(repo, run_id)
        _paths, s = st.load(repo, run_id)
        node = s["phases"]["05-code-review"]
        assert node["planned"], "라우팅이 상태에 확정돼야 한다"
        assert "리뷰어 라우팅" in env["render"]

    def test_envelope_names_the_excluded_categories(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, _paths = _enter_05(repo, request_file, phases)
        env = cli.run_next(repo, run_id)
        assert "검토 제외" in env["render"]
        assert "BOUNDARY_VIOLATION" in env["render"]

    def test_zero_reviewers_is_named_as_a_failure_not_silence(self, repo,
                                                              request_file, phases):
        run_id, _paths = _enter_05(repo, request_file, phases)
        env = cli.run_next(repo, run_id)
        _p, s = st.load(repo, run_id)
        if not s["phases"]["05-code-review"]["planned"]:
            assert "failed" in env["render"] or "0개" in env["render"]

    def test_record_before_trace_is_refused(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        f = _reviewer_files(paths, "arch", [])
        env = cli.run_record(repo, "05", str(f), reviewer="arch", round_=1,
                             run_id=run_id)
        assert env["exit"] == 3
        assert "계약 대조" in env["render"]

    def test_record_without_reviewer_is_refused(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        env = cli.run_record(repo, "05", str(paths.run_dir / "x.json"),
                             run_id=run_id)
        assert env["exit"] == 2

    def test_missing_raw_md_is_exit_8(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        f = paths.run_dir / "05_review_arch.json"
        f.write_text(json.dumps({"reviewer": "arch", "by_checklist": {"a": []}}),
                     encoding="utf-8")
        env = cli.run_record(repo, "05", str(f), reviewer="arch", run_id=run_id)
        assert env["exit"] == 8
        assert "원문" in env["render"]


class TestReview05Denominator:
    """`review05.status` 의 분모는 **라우팅**이지 제출자가 아니다 (G-4).

    지금까지 `planned or [reviewer]` / `or sorted(slot)` 가 분모를 분자에서
    유도해 비율이 구조적으로 항상 1 이었다. `degraded` 도 `failed` 도 도달
    불가능한 값이었고, 페이즈 파일 232·233행과 `_review_render` 는 그 값이
    난다고 **선언만** 하고 있었다.
    """

    def _planned(self, repo, run_id, codes):
        """라우팅 결과를 강제한다 — glob 우연에 기대지 않는다."""
        paths, s = st.load(repo, run_id)
        node = s.setdefault("phases", {}).setdefault("05-code-review", {})
        node["planned"] = list(codes)
        node.setdefault("routing", {"reviewers": [{"code": c} for c in codes],
                                    "dropped": [], "capped": False})
        node.setdefault("mode", "fanout")
        st.save(paths, s)
        return paths

    def test_zero_routing_is_written_to_state_not_only_rendered(
            self, repo, request_file, phases):
        """산문이 기계 사실을 참칭하지 않는다."""
        run_id, _paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        _p, s = st.load(repo, run_id)
        if s["phases"]["05-code-review"]["planned"]:
            pytest.skip("이 리포 상태에서는 라우팅이 비지 않았다")
        assert s["review05"]["status"] == "failed"
        assert s["review05"]["reviewers_planned"] == 0
        assert "review05:failed" in (s.get("gaps") or [])
        assert s["grade"] != "PASS"

    def test_empty_planned_is_not_replaced_by_the_submitter(
            self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        self._planned(repo, run_id, [])
        f = _reviewer_files(paths, "arch", [])
        env = cli.run_record(repo, "05", str(f), reviewer="arch", round_=1,
                             run_id=run_id)
        _p, s = st.load(repo, run_id)
        assert (s.get("review05") or {}).get("status") != "ok", env["render"]

    def test_unplanned_reviewer_submission_is_refused(self, repo, request_file,
                                                      phases):
        """라우팅이 부르지 않은 리뷰어의 제출은 받지 않는다 (페이즈 파일 164행)."""
        ldg.seed(repo)
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        self._planned(repo, run_id, ["arch"])
        f = _reviewer_files(paths, "sec", [])
        env = cli.run_record(repo, "05", str(f), reviewer="sec", round_=1,
                             run_id=run_id)
        assert env["exit"] == 8
        assert "라우팅" in env["render"] or "계획" in env["render"]

    def test_record_before_next_is_refused(self, repo, request_file, phases):
        """`planned` 의 부재(05 진입 안 함)와 빈 리스트(0명 라우팅)는 다르다."""
        ldg.seed(repo)
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_contract_trace(repo, run_id=run_id)
        f = _reviewer_files(paths, "arch", [])
        env = cli.run_record(repo, "05", str(f), reviewer="arch", round_=1,
                             run_id=run_id)
        assert env["exit"] == 3
        assert "next" in env["render"]


class TestReview05Failure:
    """리뷰어 실패는 오류가 아니라 **데이터**다 — 등급으로 드러나야 한다."""

    def _ready(self, repo, request_file, phases, codes):
        ldg.seed(repo)
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        paths, s = st.load(repo, run_id)
        node = s["phases"]["05-code-review"]
        node["planned"] = list(codes)
        node["routing"] = {"reviewers": [{"code": c} for c in codes],
                           "dropped": [], "capped": False}
        node["mode"] = "fanout"
        st.save(paths, s)
        return run_id, paths

    def test_second_rejection_records_the_reviewer_as_failed(
            self, repo, request_file, phases):
        """재제출 1회 → 2회 실패 시 스킵 + degrade (페이즈 파일 234행)."""
        run_id, paths = self._ready(repo, request_file, phases, ["arch", "test"])
        bad = paths.run_dir / "05_review_arch.json"
        bad.write_text(json.dumps({"reviewer": "arch", "by_checklist": {"a": [
            {"id": "F-1", "severity": "major", "title": "x",
             "category": "NAMING", "target_role": "impl",
             "quote": "원문에없는문장이다"}]}},
            ensure_ascii=False), encoding="utf-8")
        bad.with_name("05_review_arch.raw.md").write_text(
            "# 리뷰\n\n아무 말\n", encoding="utf-8")
        first = cli.run_record(repo, "05", str(bad), reviewer="arch", round_=1,
                               run_id=run_id)
        assert first["exit"] == 8, "1회차는 재제출을 요구한다"
        second = cli.run_record(repo, "05", str(bad), reviewer="arch", round_=1,
                                run_id=run_id)
        assert second["exit"] != 8, "2회차는 실패로 확정하고 흐름을 잇는다"
        _p, s = st.load(repo, run_id)
        slot = s["phases"]["05-code-review"]["rounds"]["1"]
        assert slot["arch"]["keys"] is None
        assert slot["arch"].get("reason")

    def test_one_failed_reviewer_is_degraded(self, repo, request_file, phases):
        run_id, paths = self._ready(repo, request_file, phases, ["arch", "test"])
        env = cli.run_record(repo, "05", None, reviewer="arch", round_=1,
                             run_id=run_id, failed=True, reason="호출이 타임아웃")
        assert env["exit"] == 0, env["render"]
        ok = _reviewer_files(paths, "test", [])
        cli.run_record(repo, "05", str(ok), reviewer="test", round_=1,
                       run_id=run_id)
        _p, s = st.load(repo, run_id)
        assert s["review05"]["status"] == "degraded"
        assert s["review05"]["reviewers_planned"] == 2
        assert s["review05"]["reviewers_ok"] == 1
        assert s["grade"] != "PASS"

    def test_all_failed_is_failed(self, repo, request_file, phases):
        run_id, _paths = self._ready(repo, request_file, phases, ["arch"])
        cli.run_record(repo, "05", None, reviewer="arch", round_=1,
                       run_id=run_id, failed=True, reason="호출 실패")
        _p, s = st.load(repo, run_id)
        assert s["review05"]["status"] == "failed"
        assert "review05:failed" in (s.get("gaps") or [])

    def test_failure_report_is_refused_when_a_valid_submission_exists(
            self, repo, request_file, phases):
        """이 verb 자체가 자진 신고다 — 확인 가능한 만큼만 받는다 (불변식 8)."""
        run_id, paths = self._ready(repo, request_file, phases, ["arch"])
        _reviewer_files(paths, "arch", [])
        env = cli.run_record(repo, "05", None, reviewer="arch", round_=1,
                             run_id=run_id, failed=True, reason="안 돌았다")
        assert env["exit"] == 8
        assert "제출" in env["render"]


class TestReview05EnvelopeContract:
    """M37·M38 — 봉투가 기계 검사를 다 말하지 않아 제출이 두 번 반려됐다.

    둘 다 **페이즈 파일이 아니라 `cli.py` 가 조건부로 그리는 문장**이 원인이다.
    페이즈 파일만 고치면 다음 런이 또 밟는다.
    """

    def _routed(self, mode, codes):
        return {"phases": {"05-code-review": {
            "mode": mode,
            "routing": {"reviewers": [{"code": c, "skill": "%s-reviewer" % c,
                                       "matched_count": 1} for c in codes],
                        "dropped": [], "capped": False}}}}

    def test_merged_봉투가_리뷰어별_제출을_말한다(self):
        """M37 — `mode: merged` 가 "제출도 하나" 로 읽혔다."""
        out = cli._review_render(self._routed("merged", ["data", "sec", "arch"]))
        # 명령 줄에 `merged` 가 제출자로 등장하면 안 된다. 산문은 그 낱말을
        # 쓰지만("`--reviewer merged` 를 받지 않는다") 명령은 쓰지 않는다.
        cmds = [l for l in out.splitlines() if l.startswith("python ")]
        assert cmds, out
        assert not [l for l in cmds if "--reviewer merged" in l], out
        for code in ("data", "sec", "arch"):
            assert [l for l in cmds if "--reviewer %s" % code in l], out
        assert "실행 방식" in out, out

    def test_fanout_봉투는_그_문단을_넣지_않는다(self):
        out = cli._review_render(self._routed("fanout", ["data", "sec"]))
        assert "실행 방식" not in out, out

    def test_merged_제출은_기계가_거부한다(self, repo, request_file, phases):
        """성격 규정 — 지금도 통과한다. 봉투가 말하는 규칙이 기계와 같음을 잠근다."""
        ldg.seed(repo)
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        paths, s = st.load(repo, run_id)
        node = s["phases"]["05-code-review"]
        node["planned"] = ["arch"]
        node["routing"] = {"reviewers": [{"code": "arch"}], "dropped": [],
                           "capped": False}
        node["mode"] = "merged"
        st.save(paths, s)
        f = _reviewer_files(paths, "merged", [])
        env = cli.run_record(repo, "05", str(f), reviewer="merged", round_=1,
                             run_id=run_id)
        assert env["exit"] == 8, env["render"]

    def test_델타_봉투가_minor_회계_의무를_말한다(self):
        """M38 — 봉투는 "Minor 는 고치지 않는다" 만 적었다."""
        blocking = [{"severity": "major", "target_role": "impl",
                     "title": "경계가 새고 있다"}]
        prev = [{"id": "F-9", "severity": "minor", "reviewer": "arch",
                 "key": "k9", "title_norm": "주석이 낡았다"}]
        out = cli._review_repair_render(blocking, 2, delta="arch",
                                        previous_open=prev)
        assert "resolved_from_previous" in out, out
        assert "회계" in out, out
        # 열린 목록을 봉투가 직접 준다 — 모델이 재구성하지 않게
        assert "F-9" in out, out
        assert "주석이 낡았다" in out, out

    def test_열린_지적이_없으면_목록을_적지_않는다(self):
        blocking = [{"severity": "major", "target_role": "impl", "title": "x"}]
        out = cli._review_repair_render(blocking, 2, delta="arch",
                                        previous_open=[])
        assert "F-9" not in out
        assert "회계" in out, "의무 자체는 목록 유무와 무관하다"


class TestReview05DeltaRound:
    """델타 재리뷰는 1명이고(M27), 그 1명이 G-4 를 되돌리지 않는다."""

    def _ready(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        paths, s = st.load(repo, run_id)
        node = s["phases"]["05-code-review"]
        node["planned"] = ["arch", "test"]
        node["routing"] = {"reviewers": [{"code": "arch"}, {"code": "test"}],
                           "dropped": [], "capped": False}
        node["mode"] = "fanout"
        return run_id, paths, s, node

    def test_델타_라운드가_1회차_리뷰어_수를_지우지_않는다(self, repo):
        """M43 — 1회차에 셋이 돌았는데 상태가 `1/1` 로 기록됐다."""
        s, node = {}, {}
        cli._write_review05(s, node, ["data", "sec", "arch"], 3, [],
                            {"data": {"keys": []}, "sec": {"keys": []},
                             "arch": {"keys": []}}, round_=1)
        cli._write_review05(s, node, ["arch"], 1, [],
                            {"arch": {"keys": []}}, round_=2)
        got = s["review05"]
        assert got["reviewers_planned"] == 3, got
        assert got["reviewers_ok"] == 3, got
        assert got["rounds"]["1"]["planned"] == 3, got["rounds"]
        assert got["rounds"]["2"]["planned"] == 1, got["rounds"]

    def test_status_는_델타_뒤에도_최악을_보존한다(self, repo):
        """이 수정이 만들 수 있는 유일한 회귀를 잠근다.

        실적을 최댓값으로 접는다고 `status` 까지 새 값에서 유도하면 델타
        라운드가 1회차의 `degraded` 를 지운다.
        """
        s, node = {}, {}
        cli._write_review05(s, node, ["data", "sec", "arch"], 2, [],
                            {"data": {"keys": []}, "sec": {"keys": []},
                             "arch": {"keys": None}}, round_=1)
        cli._write_review05(s, node, ["arch"], 1, [],
                            {"arch": {"keys": []}}, round_=2)
        got = s["review05"]
        assert got["status"] == "degraded", got
        assert got["reviewers_planned"] == 3, got
        assert got["reviewers_ok"] == 2, got

    def test_실패한_리뷰어가_라운드를_넘어_남는다(self, repo):
        s, node = {}, {}
        cli._write_review05(s, node, ["data", "arch"], 1, [],
                            {"data": {"keys": None}, "arch": {"keys": []}},
                            round_=1)
        cli._write_review05(s, node, ["arch"], 1, [],
                            {"arch": {"keys": []}}, round_=2)
        assert s["review05"]["reviewers_failed"] == ["data"], s["review05"]

    # ------------------------------------------------------------------
    # M53 — 리뷰어가 남긴 신호도 라운드를 가로질러 보존한다.
    # 위 셋(M43)과 **같은 함수의 같은 실패 모드**다: `slot`(현재 라운드
    # 하나)만 읽어 델타 라운드의 1명이 덮었다. 접는 방식은 셋이 다르므로
    # 셋을 따로 잠근다 — 하나가 빨간불일 때 고칠 자리가 각각 다르다.
    # ------------------------------------------------------------------

    def _sub(self, need=None, dropped=0, truncated=False):
        return {"keys": [], "need_more_context": list(need or []),
                "dropped_by_enforcement": dropped, "truncated": truncated}

    def _round(self, node, n, subs):
        """제출을 `node["rounds"]` 에 실물과 같은 모양으로 넣고 그 슬롯을 준다."""
        node.setdefault("rounds", {})[str(n)] = subs
        return subs

    def test_델타_라운드가_1회차_맥락_요청을_지우지_않는다(self, repo):
        """**M53 의 정본.** P7 에서 1회차 5건이 2회차 뒤 **0** 이 됐다.

        리뷰어가 "그 구간이 diff 밖이라 대조하지 못했다" 고 말한 것이 조용히
        증발한다 — 단조성 검사가 findings 에는 걸리는데 이 필드에는 안 걸린다.
        리스트를 그대로 비교해 **첫 등장 순서**까지 함께 못박는다.
        """
        s, node = {}, {}
        r1 = self._round(node, 1, {"data": self._sub(["가", "나"]),
                                   "sec": self._sub(["다"])})
        cli._write_review05(s, node, ["data", "sec"], 2, [], r1, round_=1)
        assert s["review05"]["need_more_context"] == ["가", "나", "다"], s["review05"]
        r2 = self._round(node, 2, {"arch": self._sub([])})
        cli._write_review05(s, node, ["arch"], 1, [], r2, round_=2)
        assert s["review05"]["need_more_context"] == ["가", "나", "다"], s["review05"]

    def test_같은_문구의_맥락_요청은_한_번만_센다(self, repo):
        """접는 규칙이 **누적이 아니라 합집합**이라는 결정을 잠근다.

        델타 라운드는 같은 리뷰어가 같은 문장을 다시 낸다. 누적이면 「맥락 부족
        요청」이 라운드 수에 비례해 자라고, "몇 건을 못 봤나" 가 "몇 라운드
        돌았나" 로 조용히 바뀐다 — M30 이 원장 `count` 에서 고친 그 변질이다.
        """
        s, node = {}, {}
        same = "diff 밖이라 대조 못 했다"
        r1 = self._round(node, 1, {"arch": self._sub([same, "1회차만의 것"])})
        cli._write_review05(s, node, ["arch"], 1, [], r1, round_=1)
        r2 = self._round(node, 2, {"arch": self._sub([same])})
        cli._write_review05(s, node, ["arch"], 1, [], r2, round_=2)
        # 안 접으면 3건, 안 모으면 1건. 둘 다 아니어야 한다.
        assert s["review05"]["need_more_context"] == [same, "1회차만의 것"],             s["review05"]

    def test_드롭_수는_라운드를_가로질러_합쳐진다(self, repo):
        """`need_more_context` 와 달리 **합**이다.

        이 값은 개체 수가 아니라 **기계가 몇 번 되돌려야 했나** 라는 비용이고
        (`_excluded_render`), 재제기는 그 비용을 한 번 더 쓴 것이다. 그래서
        원장의 `finding_key` 접기(M30)와 수가 다를 수 있고 그것이 의도다.
        """
        s, node = {}, {}
        r1 = self._round(node, 1, {"data": self._sub(dropped=2),
                                   "sec": self._sub(dropped=1)})
        cli._write_review05(s, node, ["data", "sec"], 2, [], r1, round_=1)
        assert s["review05"]["dropped_by_enforcement"] == 3, s["review05"]
        r2 = self._round(node, 2, {"arch": self._sub(dropped=0)})
        cli._write_review05(s, node, ["arch"], 1, [], r2, round_=2)
        assert s["review05"]["dropped_by_enforcement"] == 3, s["review05"]

    def test_절단_사실이_델타_뒤에도_남는다(self, repo):
        """`status` 가 "런 안에서 좋아지지 않는다" 인 것의 대칭이다.

        한 번이라도 절단됐으면 그 런의 리뷰 범위는 절단된 것이고, 뒤 라운드의
        `False` 가 그것을 덮으면 신호가 무의미해진다.
        """
        s, node = {}, {}
        r1 = self._round(node, 1, {"arch": self._sub(truncated=True)})
        cli._write_review05(s, node, ["arch"], 1, [], r1, round_=1)
        r2 = self._round(node, 2, {"arch": self._sub(truncated=False)})
        cli._write_review05(s, node, ["arch"], 1, [], r2, round_=2)
        assert s["review05"]["truncated"] is True, s["review05"]

    # ---------------------------------------------------------------- M52
    #
    # 델타 라운드는 설계상 한 명만 돈다. 그 한 명의 제출이 **그 라운드의**
    # merged 이고, PR 본문의 「미해결 Minor」가 거기서 나오면 다른 리뷰어의
    # 열린 Minor 가 사람이 읽는 자리에서만 사라진다 (원장에는 남는다).

    @staticmethod
    def _mf(fid, title, severity="minor", category="RESPONSE_SHAPE",
            role="impl"):
        """`NAMING`·`BOUNDARY_VIOLATION`·`MIG_DESTRUCTIVE` 를 기본값으로 쓰지
        않는다 — 셋은 검토 제외 목록이라 `review.check` 가 드롭한다."""
        return {"id": fid, "category": category, "severity": severity,
                "target_role": role, "title": title, "quote": title}

    @classmethod
    def _mslot(cls, findings, closed=()):
        """성공한 제출 슬롯 하나. `keys` 가 None 이 아닌 것이 성공의 표식이다."""
        return {"mode": "primary", "blocking": 0,
                "keys": [{"key": ldg.finding_key(f), "id": f["id"],
                          "severity": f["severity"], "reraised_from": None}
                         for f in findings],
                "findings": list(findings), "closed": list(closed),
                "dropped_by_enforcement": 0, "truncated": False,
                "need_more_context": []}

    def test_델타_라운드가_다른_리뷰어의_열린_Minor_를_지우지_않는다(self, repo):
        """M52 — P7 2회차가 `arch` 하나였고 `sec`·`data` 의 셋이 사라졌다."""
        major = self._mf("F-9", "인가 누락", "major", "AUTHZ_MISSING_RULE")
        r1 = {"arch": self._mslot([self._mf("A-1", "arch 지적 1"),
                                   self._mf("A-2", "arch 지적 2"), major]),
              "sec": self._mslot([self._mf("S-1", "sec 지적")]),
              "data": self._mslot([self._mf("D-1", "data 지적 1"),
                                   self._mf("D-2", "data 지적 2")])}
        r2 = {"arch": self._mslot([], closed=[ldg.finding_key(major)])}
        open_ = rv.open_findings({"1": r1, "2": r2})
        minors = sorted(f["title"] for f in open_ if f["severity"] == "minor")
        assert minors == ["arch 지적 1", "arch 지적 2", "data 지적 1",
                          "data 지적 2", "sec 지적"], (
            "마지막 라운드만 보면 0건이고 델타의 것만 보면 2건이다 — 다섯이어야 "
            "한다 (M52)")

    def test_닫힌_지적은_열린_목록에_없다(self, repo):
        """접기가 넓어졌다고 이미 해소된 것까지 되살리면 안 된다."""
        major = self._mf("F-9", "인가 누락", "major", "AUTHZ_MISSING_RULE")
        r1 = {"arch": self._mslot([major, self._mf("A-1", "arch 지적 1")])}
        r2 = {"arch": self._mslot([], closed=[ldg.finding_key(major)])}
        titles = [f["title"] for f in rv.open_findings({"1": r1, "2": r2})]
        assert titles == ["arch 지적 1"], titles

    def test_2인_합치로_오른_severity_가_열린_목록에_반영된다(self, repo):
        """`review.merge` 는 2인이 같은 것을 내면 한 단계 올린다.

        그 상승을 잃으면 major 로 오른 지적이 「미해결 Minor」에 실린다 —
        수리 대상인 것을 수리 면제인 것처럼 적는 것이다.
        """
        same = dict(category="RESPONSE_SHAPE", role="impl")
        r1 = {"sec": self._mslot([self._mf("S-1", "같은 지적", **same)]),
              "data": self._mslot([self._mf("D-1", "같은 지적", **same)])}
        open_ = rv.open_findings({"1": r1})
        assert len(open_) == 1, open_
        assert open_[0]["severity"] == "major", open_[0]
        assert [f for f in open_ if f["severity"] == "minor"] == []

    def test_열린_목록이_라운드를_가로질러_severity_를_올리지_않는다(self, repo):
        """라운드를 섞어 한 번에 merge 하면 여기가 빨간불이 된다.

        `sec` 가 1회차에, 델타 `arch` 가 2회차에 **같은** 지적을 낸다. 라운드
        안에서만 merge 하면 둘 다 1인 관측이라 minor 그대로다. 라운드를
        가로질러 합치면 `by` 가 둘이 되어 major 로 오르고, **한 번도 합치된
        적 없는 지적이 합치로 오른 것처럼** 적힌다.
        """
        same = dict(category="RESPONSE_SHAPE", role="impl")
        r1 = {"sec": self._mslot([self._mf("S-1", "같은 지적", **same)])}
        r2 = {"arch": self._mslot([self._mf("A-9", "같은 지적", **same)])}
        open_ = rv.open_findings({"1": r1, "2": r2})
        assert len(open_) == 1, open_
        assert open_[0]["severity"] == "minor", open_[0]
        assert "severity_raised_from" not in open_[0], open_[0]

    def test_실패한_리뷰어의_슬롯은_열린_목록에_안_들어간다(self, repo):
        """`keys: None` 이 실패의 표식이다 (`cli.py` 의 실패 슬롯).

        `_judge_05` 가 병합에서 그것을 빼는 것과 **같은 가드**를 쓴다. 안 빼면
        규약을 어겨 되돌려진 제출의 문장이 PR 본문에 실린다.
        """
        r1 = {"arch": self._mslot([self._mf("A-1", "arch 지적")]),
              "sec": {"mode": "primary", "keys": None, "blocking": 0,
                      "closed": [], "status": "failed",
                      "findings": [self._mf("S-1", "반려된 제출의 문장")],
                      "dropped_by_enforcement": 0, "truncated": False,
                      "need_more_context": []}}
        titles = [f["title"] for f in rv.open_findings({"1": r1})]
        assert titles == ["arch 지적"], titles

    def test_첫_등장의_판정이_원장과_같이_이긴다(self, repo):
        """원장은 1회차 행을 남긴다 (M30 · `ledgered_keys`).

        본문이 마지막 회차의 판정을 적으면 두 영수증이 같은 키를 두고 다른
        말을 한다 — 이 증분이 없애려는 그 어긋남을 방향만 바꿔 되살리는 것이다.
        """
        same = dict(category="RESPONSE_SHAPE", role="impl")
        r1 = {"arch": self._mslot([self._mf("A-1", "같은 지적", **same)])}
        r2 = {"arch": self._mslot(
            [self._mf("A-1", "같은 지적", severity="major", **same)])}
        open_ = rv.open_findings({"1": r1, "2": r2})
        assert len(open_) == 1, open_
        assert open_[0]["severity"] == "minor", open_[0]

    def test_델타의_회계_목록은_여전히_자기_것만이다(self, repo):
        """**(A) 를 안 골랐다는 것을 코드로 잠근다.**

        보고 표면을 넓혔다고 회계 목록까지 넓히면 M21 ③ 이 다시 열린다 —
        두 리뷰어가 모두 `F-1` 을 쓰므로 id 대조가 전역이 되면 한 줄이 서로
        다른 두 지적을 동시에 해소로 계수한다. 누가 나중에 그 필터를 지우면
        여기가 빨간불이 된다.
        """
        r1 = {"arch": self._mslot([self._mf("F-1", "arch 지적")]),
              "sec": self._mslot([self._mf("F-1", "sec 지적")])}
        got = cli._previous_open({"1": r1}, 2, "arch")
        assert [k["id"] for k in got] == ["F-1"], got
        assert len(got) == 1, "sec 의 F-1 이 들어오면 한 줄이 둘을 닫는다"

    def test_worst_status_is_a_pure_function(self, repo):
        assert rv.worst_status(["ok", "degraded"]) == "degraded"
        assert rv.worst_status(["degraded", "ok"]) == "degraded"
        assert rv.worst_status(["failed", "ok", "degraded"]) == "failed"
        assert rv.worst_status(["ok", "ok"]) == "ok"
        assert rv.worst_status([]) == "failed", "라운드가 없는 것은 미수행이다"

    def test_delta_round_waits_for_one_reviewer(self, repo, request_file, phases):
        run_id, paths, s, node = self._ready(repo, request_file, phases)
        node["rounds_planned"] = {"2": ["arch"]}
        st.save(paths, s)
        f = _reviewer_files(paths, "arch", [])
        env = cli.run_record(repo, "05", str(f), reviewer="arch", round_=2,
                             run_id=run_id)
        waiting = (env.get("data") or {}).get("waiting_for") or []
        assert "test" not in waiting, "델타 라운드는 전원을 기다리지 않는다"

    def test_a_clean_delta_round_does_not_heal_the_status(
            self, repo, request_file, phases):
        """G-4 재개봉 방지 — status 는 런 안에서 단조 비개선이다."""
        run_id, paths, s, node = self._ready(repo, request_file, phases)
        node["round_status"] = {"1": "degraded"}
        node["rounds_planned"] = {"2": ["arch"]}
        s["review05"] = {"status": "degraded", "reviewers_planned": 2,
                         "reviewers_ok": 1, "mode": "fanout", "major": 0,
                         "need_more_context": [], "dropped_by_enforcement": 0,
                         "truncated": False}
        st.save(paths, s)
        f = _reviewer_files(paths, "arch", [])
        cli.run_record(repo, "05", str(f), reviewer="arch", round_=2,
                       run_id=run_id)
        _p, s = st.load(repo, run_id)
        assert s["review05"]["status"] == "degraded", \
            "깨끗한 델타 라운드가 앞선 결손을 지우면 E1 가드가 옆문으로 다시 열린다"

    def _context_file(self, paths, code, round_, need, findings=(), resolved=()):
        """`_reviewer_files` 는 `need_more_context` 를 `[]` 로 박아 쓴다."""
        name = ("05_review_%s.json" % code if round_ == 1
                else "05_review_%s_r%d.json" % (code, round_))
        j = paths.run_dir / name
        j.write_text(json.dumps(
            {"reviewer": code, "round": round_, "status": "ok",
             "by_checklist": {"전부": list(findings)},
             "resolved_from_previous": list(resolved),
             "need_more_context": list(need)},
            ensure_ascii=False), encoding="utf-8")
        body = "".join("## %s\n\n%s\n" % (f["severity"], f["quote"])
                       for f in findings)
        j.with_name(name.replace(".json", ".raw.md")).write_text(
            "# 리뷰\n\n" + body + "확인하지 못한 구간이 있다\n",
            encoding="utf-8")
        return j

    def test_실물_델타_라운드가_앞_회차의_맥락_요청을_지우지_않는다(
            self, repo, request_file, phases):
        """P7 이 실제로 밟은 경로다 (M53).

        단위 넷은 `node["rounds"]` 를 손으로 채운다. 이것은 **`record` 가 그
        자리를 실제로 채우는지**와 `_judge_05` 가 그 `node` 를 넘기는지까지
        잰다 — 접는 코드가 맞아도 원천이 안 차 있으면 실물에서는 여전히
        증발한다.

        **1회차가 major 를 내야 델타 라운드가 성립한다.** 지적 0 건이면 05 가
        그 자리에서 통과해 2회차 `record` 가 exit 3 으로 거부되고, 그러면 이
        테스트는 아무것도 안 밟은 채 초록이 된다. `exit != 3` 단언이 그
        헛돎을 막는다.
        """
        run_id, paths, s, node = self._ready(repo, request_file, phases)
        st.save(paths, s)
        major = {"id": "F-1", "category": "AUTHZ_MISSING_RULE",
                 "severity": "major", "target_role": "impl",
                 "title": "인가 누락", "quote": "인가 누락"}
        for code, fs in (("arch", [major]), ("test", [])):
            j = self._context_file(paths, code, 1,
                                   ["%s: 그 구간이 diff 밖이라 대조 못 했다" % code],
                                   findings=fs)
            cli.run_record(repo, "05", str(j), reviewer=code, round_=1,
                           run_id=run_id)
        _p, s = st.load(repo, run_id)
        assert len(s["review05"]["need_more_context"]) == 2, s["review05"]

        s["phases"]["05-code-review"]["rounds_planned"] = {"2": ["arch"]}
        st.save(_p, s)
        j = self._context_file(
            paths, "arch", 2, [],
            resolved=[{"id": "F-1", "resolved_by": "인가 규칙을 넣었다"}])
        env = cli.run_record(repo, "05", str(j), reviewer="arch", round_=2,
                             run_id=run_id)
        assert env["exit"] == 0, (env["exit"], env.get("render"))
        _p, s = st.load(repo, run_id)
        assert "2" in (s["phases"]["05-code-review"].get("rounds") or {}),             "2회차가 슬롯에 안 들어갔으면 이 테스트는 아무것도 안 잰다"
        assert len(s["review05"]["need_more_context"]) == 2,             "델타 라운드의 빈 배열이 1회차의 둘을 지웠다 (M53)"

    def test_실물_델타_라운드_뒤_본문이_모든_리뷰어의_미해결_Minor_를_담는다(
            self, repo, request_file, phases):
        """P8 확인 항목의 문장 그대로다 (M52 · `ROADMAP.md:486`).

        P7 이 실제로 밟은 모양: 1회차에 `arch` 가 major 하나와 minor 하나를,
        `test` 가 minor 하나를 낸다. 2회차 델타는 `arch` 한 명이고, 그의 회계
        목록에는 **`test` 의 minor 가 없다**(M21 ③ 때문에 그래야 한다). 그래서
        2회차 merged 는 `arch` 것뿐이고, 본문이 거기서 나오면 `test` 의 minor 가
        **원장에는 남은 채 사람이 읽는 자리에서만** 사라진다.

        **헛돎 가드 둘** (M53 이 실제로 밟았다): ① 1회차에 major 가 없으면 05 가
        그 자리에서 통과해 2회차 `record` 가 exit 3 이고 아무것도 안 밟은 채
        초록이 된다. ② 2회차가 열린 것을 회계하지 않으면 단조성 검사가 exit 8 을
        낸다. 둘 다 단언으로 잠근다.
        """
        run_id, paths, s, node = self._ready(repo, request_file, phases)
        st.save(paths, s)
        major = self._mf("F-1", "인가 누락", "major", "AUTHZ_MISSING_RULE")
        arch_minor = self._mf("F-2", "arch 가 남긴 미해결 Minor")
        test_minor = self._mf("T-1", "test 가 남긴 미해결 Minor")
        for code, fs in (("arch", [major, arch_minor]), ("test", [test_minor])):
            j = self._context_file(paths, code, 1, [], findings=fs)
            env = cli.run_record(repo, "05", str(j), reviewer=code, round_=1,
                                 run_id=run_id)
        assert env["exit"] == 4, (
            "1회차가 수리를 요구하지 않으면 델타 라운드가 성립하지 않는다 — "
            "이 테스트는 아무것도 안 잰다", env["exit"], env.get("render"))

        _p, s = st.load(repo, run_id)
        s["phases"]["05-code-review"]["rounds_planned"] = {"2": ["arch"]}
        st.save(_p, s)
        # 델타는 자기 회계 의무만 진다 — major 를 닫고 자기 minor 를 다시 낸다.
        # `test` 의 minor 는 애초에 그의 목록에 없다. 그것이 M52 의 기전이다.
        j = self._context_file(
            paths, "arch", 2, [], findings=[arch_minor],
            resolved=[{"id": "F-1", "resolved_by": "인가 규칙을 넣었다"}])
        env = cli.run_record(repo, "05", str(j), reviewer="arch", round_=2,
                             run_id=run_id)
        assert env["exit"] == 0, (env["exit"], env.get("render"))
        _p, s = st.load(repo, run_id)
        assert "2" in (s["phases"]["05-code-review"].get("rounds") or {}),             "2회차가 슬롯에 안 들어갔으면 이 테스트는 아무것도 안 잰다"

        # 그 라운드의 영수증은 델타 것만 적는다 — 그것이 그 파일의 뜻이다.
        got = json.loads((paths.run_dir / "05_review.json").read_text(
            encoding="utf-8"))
        assert [f["title"] for f in got["findings"]] == [arch_minor["title"]],             got["findings"]

        body = pr_mod.build_body(repo, paths, s,
                                 harness._read_json(repo / harness.CONFIG_REL))
        assert arch_minor["title"] in body, body
        assert test_minor["title"] in body,             "델타가 안 본 리뷰어의 미해결 Minor 가 본문에서 사라졌다 (M52)"
        assert major["title"] not in body, "닫힌 지적이 미해결로 되살아났다"


class TestPr06MinorAccounting:
    """M52 — 「미해결 Minor」의 출처는 런 전체이지 마지막 라운드가 아니다."""

    def _body(self, repo, paths, s):
        return pr_mod.build_body(repo, paths, s,
                                 harness._read_json(repo / harness.CONFIG_REL))

    def test_라운드가_없으면_05_review_json_으로_낙하한다(
            self, repo, request_file, phases):
        """옛 런 디렉터리와 리뷰어 0명 경로에서 거동이 그대로다."""
        run_id, paths = _enter_06(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        (paths.run_dir / "05_review.json").write_text(json.dumps(
            {"round": 1, "review05": s["review05"],
             "findings": [{"id": "F-1", "severity": "minor",
                           "title": "옛 런의 미해결 Minor"}]},
            ensure_ascii=False), encoding="utf-8")
        assert "옛 런의 미해결 Minor" in self._body(repo, paths, s)

    def test_전부_닫힌_런은_없다고_적지_낙하하지_않는다(
            self, repo, request_file, phases):
        """**빈 목록과 필드 없음은 다르다.**

        `rounds` 가 있는데 열린 것이 0건인 것을 "출처가 없다" 로 읽어
        `05_review.json` 으로 낙하하면, 이미 닫힌 Minor 가 미해결로 되살아난다.
        """
        run_id, paths = _enter_06(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        f = {"id": "F-1", "category": "RESPONSE_SHAPE", "severity": "minor",
             "target_role": "impl", "title": "닫힌 Minor", "quote": "닫힌 Minor"}
        s["phases"]["05-code-review"]["rounds"] = {
            "1": {"arch": {"keys": [{"key": ldg.finding_key(f), "id": "F-1",
                                     "severity": "minor"}],
                           "findings": [f], "closed": []}},
            "2": {"arch": {"keys": [], "findings": [],
                           "closed": [ldg.finding_key(f)]}}}
        st.save(_p, s)
        (paths.run_dir / "05_review.json").write_text(json.dumps(
            {"round": 1, "review05": s["review05"], "findings": [f]},
            ensure_ascii=False), encoding="utf-8")
        body = self._body(repo, paths, s)
        assert "닫힌 Minor" not in body, body
        assert "- 없다" in body, body


class TestPhase05Ledgering:

    def _prepare(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_05(repo, request_file, phases)
        (repo / "src" / "app").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "app" / "page.tsx").write_text(
            "export default function P() { return null }\n", encoding="utf-8")
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        _p, s = st.load(repo, run_id)
        s["phases"]["05-code-review"]["planned"] = ["arch"]
        st.save(_p, s)
        return run_id, paths

    def test_clean_review_advances_and_writes_the_three_files(self, repo,
                                                              request_file, phases):
        run_id, paths = self._prepare(repo, request_file, phases)
        f = _reviewer_files(paths, "arch", [])
        env = cli.run_record(repo, "05", str(f), reviewer="arch", round_=1,
                             run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        for name in ("05_trace.json", "05_review.json", "05_promo_staged.json"):
            assert (paths.run_dir / name).exists(), name

    def test_review05_status_is_recorded_separately_from_findings(
            self, repo, request_file, phases):
        run_id, paths = self._prepare(repo, request_file, phases)
        f = _reviewer_files(paths, "arch", [])
        cli.run_record(repo, "05", str(f), reviewer="arch", round_=1, run_id=run_id)
        _p, s = st.load(repo, run_id)
        assert s["review05"]["status"] == "ok"
        assert s["review05"]["reviewers_planned"] == 1

    def test_major_finding_blocks_with_exit_4(self, repo, request_file, phases):
        run_id, paths = self._prepare(repo, request_file, phases)
        f = _reviewer_files(paths, "arch", [
            {"id": "F-1", "category": "AUTHZ_MISSING_RULE", "severity": "major",
             "target_role": "impl", "title": "인가 누락", "quote": "인가 누락"}])
        env = cli.run_record(repo, "05", str(f), reviewer="arch", round_=1,
                             run_id=run_id)
        assert env["exit"] == 4
        assert "수리가 필요하다" in env["render"]
        assert "Minor 는 고치지 않는다" in env["render"]

    def test_minor_finding_does_not_block(self, repo, request_file, phases):
        run_id, paths = self._prepare(repo, request_file, phases)
        f = _reviewer_files(paths, "arch", [
            {"id": "F-1", "category": "TX_BOUNDARY", "severity": "minor",
             "target_role": "impl", "title": "이름", "quote": "이름"}])
        env = cli.run_record(repo, "05", str(f), reviewer="arch", round_=1,
                             run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]

    def test_findings_reach_the_ledger(self, repo, request_file, phases):
        run_id, paths = self._prepare(repo, request_file, phases)
        f = _reviewer_files(paths, "arch", [
            {"id": "F-1", "category": "TX_BOUNDARY", "severity": "minor",
             "target_role": "impl", "title": "이름", "quote": "이름"}])
        cli.run_record(repo, "05", str(f), reviewer="arch", round_=1, run_id=run_id)
        rows = ldg.read_all(repo)
        assert any(r["title_norm"] == "이름" for r in rows)
        assert all(r["run_id"] == run_id for r in rows)

    def test_trace_findings_reach_the_ledger_too(self, repo, request_file, phases):
        """기계가 찾은 것과 리뷰어가 찾은 것이 같은 눈금 위에 있어야 한다."""
        run_id, paths = self._prepare(repo, request_file, phases)
        f = _reviewer_files(paths, "arch", [])
        cli.run_record(repo, "05", str(f), reviewer="arch", round_=1, run_id=run_id)
        rows = ldg.read_all(repo)
        assert any(r["source"] == "contract-trace" for r in rows), rows

    def test_unknown_category_from_a_reviewer_is_refused(self, repo,
                                                         request_file, phases):
        run_id, paths = self._prepare(repo, request_file, phases)
        f = _reviewer_files(paths, "arch", [
            {"id": "F-1", "category": "내가지어낸코드", "severity": "minor",
             "target_role": "impl", "title": "x", "quote": "x"}])
        env = cli.run_record(repo, "05", str(f), reviewer="arch", round_=1,
                             run_id=run_id)
        assert env["exit"] == 8


# ---------------------------------------------------------------------------
# J. 등급의 단일 출처 — 06~08 이 얹히기 전에 먼저 세운다
# ---------------------------------------------------------------------------


class TestPhase05Repaired:
    """수리된 지적은 원장에서 `repaired` 다 (M29).

    "닫혔다" 를 모델이 신고하지 않는다 — `review.check` 의 단조성 검사가 이미
    검증한 `closed` 에서만 유도한다 (불변식 8).
    """

    def _ready(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        paths, s = st.load(repo, run_id)
        node = s["phases"]["05-code-review"]
        node["planned"] = ["arch"]
        node["routing"] = {"reviewers": [{"code": "arch"}], "dropped": [],
                           "capped": False}
        node["mode"] = "fanout"
        st.save(paths, s)
        return run_id, paths

    def _finding(self, **kw):
        d = {"id": "F-1", "category": "TX_BOUNDARY", "severity": "major",
             "target_role": "impl", "title": "트랜잭션 경계가 없다",
             "quote": "규약을 벗어난 이름"}
        d.update(kw)
        return d

    def test_다음_라운드에_닫히면_repaired_로_승계된다(self, repo, request_file,
                                                      phases):
        run_id, paths = self._ready(repo, request_file, phases)
        f = self._finding()
        j = _reviewer_files(paths, "arch", [f], raw="## major\n\n규약을 벗어난 이름\n")
        cli.run_record(repo, "05", str(j), reviewer="arch", round_=1,
                       run_id=run_id)
        rows = [r for r in ldg.read_all(repo) if r.get("category") == "TX_BOUNDARY"]
        assert rows, "1라운드가 원장에 쌓았어야 한다"
        key = rows[0]["finding_key"]

        # 2라운드: 그 지적을 해소로 신고한다. 단조성 검사가 이것을 검증한다.
        j2 = paths.run_dir / "05_review_arch_r2.json"
        j2.write_text(json.dumps({
            "reviewer": "arch", "round": 2, "status": "ok",
            "by_checklist": {"전부": []},
            "resolved_from_previous": [{"id": "F-1", "resolved_by": "이름을 고쳤다"}],
            "need_more_context": []}, ensure_ascii=False), encoding="utf-8")
        j2.with_name("05_review_arch_r2.raw.md").write_text(
            "# 리뷰\n\n해소했다\n", encoding="utf-8")
        cli.run_record(repo, "05", str(j2), reviewer="arch", round_=2,
                       run_id=run_id)

        obs = [o for o in ldg.observations(repo) if o["finding_key"] == key]
        assert len(obs) == 1, "라운드마다 한 줄씩 쌓이면 안 된다 (M30)"
        assert obs[0]["resolution"] == "repaired", "닫힌 지적이 deferred 로 남는다 (M29)"

    def test_안_닫힌_것은_deferred_로_남는다(self, repo, request_file, phases):
        run_id, paths = self._ready(repo, request_file, phases)
        f = self._finding(severity="minor")
        j = _reviewer_files(paths, "arch", [f], raw="## minor\n\n규약을 벗어난 이름\n")
        cli.run_record(repo, "05", str(j), reviewer="arch", round_=1,
                       run_id=run_id)
        obs = [o for o in ldg.observations(repo) if o["category"] == "TX_BOUNDARY"]
        assert obs and obs[0]["resolution"] == "deferred"

    def test_repaired_by_를_못_가르면_null_이다(self, repo):
        """지어내지 않는다 — `state.repair` 가 아직 실행기에 없다."""
        ldg.seed(repo)
        ldg.append(repo, "R1", "05", [
            {"category": "TX_BOUNDARY", "severity": "major", "target_role": "impl",
             "title": "x", "resolution": "repaired", "source": "reviewer"}])
        assert ldg.observations(repo)[0]["repaired_by"] is None


class TestGradeSingleSource:
    """등급은 강등만 한다. 그 전에는 나중에 쓰는 쪽이 이겼다."""

    def test_처음_등급은_그대로_설정된다(self):
        s = {}
        assert st.demote(s, "PASS") == "PASS"

    def test_더_나쁜_등급으로만_움직인다(self):
        s = {"grade": "PASS"}
        assert st.demote(s, "PASS_WITH_GAPS") == "PASS_WITH_GAPS"
        assert st.demote(s, "INCOMPLETE") == "INCOMPLETE"

    def test_승격은_거부된다(self):
        """게이트가 05 뒤에 다시 돌아도 PASS_WITH_GAPS 가 PASS 로 되돌아가지 않는다."""
        s = {"grade": "PASS_WITH_GAPS"}
        assert st.demote(s, "PASS") == "PASS_WITH_GAPS"
        s = {"grade": "INCOMPLETE"}
        assert st.demote(s, "PASS_WITH_GAPS") == "INCOMPLETE"

    def test_gap_은_중복없이_쌓인다(self):
        s = {}
        st.demote(s, "PASS_WITH_GAPS", "review05:failed")
        st.demote(s, "PASS_WITH_GAPS", "review05:failed")
        st.demote(s, "PASS_WITH_GAPS", "stage_absent:e2e")
        assert s["gaps"] == ["review05:failed", "stage_absent:e2e"]

    def test_등급이_None_이면_gap_만_쌓고_등급은_안_건드린다(self):
        s = {"grade": "PASS"}
        assert st.demote(s, None, "some_gap") == "PASS"
        assert s["gaps"] == ["some_gap"]

    def test_어휘_밖_등급은_예외다(self):
        with pytest.raises(ValueError):
            st.demote({}, "GREEN")

    def test_gate_가_state_의_등급_어휘를_본다(self):
        """gate.py 가 상수를 다시 적으면 두 곳이 갈라진다."""
        sys.path.insert(0, str(_SCRIPTS / "pipeline"))
        import gate as gate_mod
        assert (gate_mod.GRADE_PASS, gate_mod.GRADE_GAPS,
                gate_mod.GRADE_INCOMPLETE) == st.GRADES

    def test_06_08_의_이벤트_어휘가_있다(self):
        """어휘 밖 kind 는 append_event 가 ValueError 를 던진다."""
        for kind in ("approved", "approval_revoked", "pr_pushed",
                     "pr_opened", "promoted", "run_closed"):
            assert kind in st.EVENT_KINDS

    def test_run_status_어휘가_닫혀_있다(self):
        """리터럴로 흩어져 있던 것을 한 자리로 모은다 (M24).

        `abandoned` 는 넷째다 — "완주했다"(`done`)와 "이어질 일이 없다"를
        원장·보고서가 같은 것으로 읽으면 안 된다.
        """
        assert st.RUN_STATUS == ("active", "escalated", "done", "abandoned")
        assert st.DONE in st.RUN_STATUS

    def test_종단은_둘이고_escalated_는_빠진다(self):
        """`escalated` 는 재개 가능한 런이다 — 안 집으면 화면에서 사라진다."""
        assert st.TERMINAL_STATUS == ("done", "abandoned")
        assert "escalated" not in st.TERMINAL_STATUS

    def test_종단이_아닌_상태로는_close_run_이_거부한다(self):
        """`run_status` 를 옮기는 자리가 하나라는 규율을 함수가 지킨다."""
        import pytest as _pytest
        with _pytest.raises(ValueError):
            st.close_run({}, status="active")

    def test_run_closed_는_horizon_과_다른_사실이다(self):
        """`horizon` 은 "다음 페이즈가 아직 없다", `run_closed` 는 "런이
        끝났다" 다. 같은 kind 로 뭉치면 둘을 구분할 수 없다."""
        assert "horizon" in st.EVENT_KINDS and "run_closed" in st.EVENT_KINDS

    def test_07_수리_카운터가_있다(self):
        assert "pr_repair" in st.COUNTERS

    def test_봉투가_승인과_PR_을_노출한다(self):
        env = st.envelope("x", True, 0, {"approval": {"06": {"granted": True}},
                                         "pr": {"number": 7}}, {}, "", None)
        assert env["state_summary"]["approval"]["06"]["granted"] is True
        assert env["state_summary"]["pr"]["number"] == 7


# ---------------------------------------------------------------------------
# K. precheck 의 선언과 실제를 맞춘다 — 06 이 이 모듈을 그대로 재사용한다
# ---------------------------------------------------------------------------


class TestPrecheckSpecAlignment:
    """§2.5 는 정책이 카운터를 소모하지 않는다고 하고, §2.3 은 exit 10 이
    상태를 잠근다고 한다. 둘 다 코드와 어긋나 있었다."""

    def test_정책_실패는_카운터를_소모하지_않는다(self, repo):
        _branch(repo, "feat-x")
        _bulk_change(repo, 40)          # files_max: 10 초과
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 9
        assert got["classification"] == "policy"
        assert got["counter_consumed"] is False

    def test_인프라_실패가_상태를_실제로_잠근다(self, repo, request_file,
                                              monkeypatch):
        monkeypatch.delenv("EXAMPLE_API_KEY", raising=False)
        _branch(repo, "feat-x")
        p = repo / "src" / "services"
        p.mkdir(parents=True)
        (p / "api-client.ts").write_text("export const a = 1\n", encoding="utf-8")
        _probe_policy(repo, "api_key", "fail")
        cli.run_init(repo, "x", request_file)
        env = cli.run_precheck(repo, scope="pr")
        assert env["exit"] == 10
        _paths, s = st.load(repo)
        assert s["escalated"] is True
        assert (repo / "_workspace" / "runs" / s["run_id"]
                / "ESCALATION.md").exists()

    def test_정책_실패는_상태를_잠그지_않는다(self, repo, request_file):
        _branch(repo, "feat-x")
        cli.run_init(repo, "x", request_file)
        _bulk_change(repo, 40)
        env = cli.run_precheck(repo, scope="pr")
        assert env["exit"] == 9
        _paths, s = st.load(repo)
        assert s["escalated"] is False

    def test_at_05_와_at_06_이_갈린다(self, repo, request_file):
        _branch(repo, "feat-x")
        _bulk_change(repo, 1)
        cli.run_init(repo, "x", request_file)
        cli.run_precheck(repo, scope="pr", phase="05")
        cli.run_precheck(repo, scope="pr", phase="06")
        _paths, s = st.load(repo)
        assert s["precheck"]["at_05"]["files"] >= 0
        assert s["precheck"]["at_06"]["files"] >= 0
        assert "base_behind" in s["precheck"]["at_06"]

    def test_런_없이도_돈다(self, repo):
        """06 이 쓰기 전에 05 가 쓰던 성질이다 — 잃지 않는다."""
        _branch(repo, "feat-x")
        _bulk_change(repo, 1)
        env = cli.run_precheck(repo, scope="pr", phase="06")
        assert env["exit"] == 0


# ---------------------------------------------------------------------------
# L. mask — 외부로 나가는 페이로드에만. 원장·내부 보고서는 원문 보존
# ---------------------------------------------------------------------------

import mask as mask_mod  # noqa: E402


def _secrets(repo, **kv):
    p = repo / ".env.local"
    p.write_text("\n".join("%s=%s" % (k, v) for k, v in kv.items()) + "\n",
                 encoding="utf-8")
    return p


class TestMask:

    def test_비밀_파일의_값만_가리고_키_이름은_남긴다(self, repo):
        _secrets(repo, EXAMPLE_API_KEY="sk-예시-실제값-99")
        got = mask_mod.mask_text(repo, "설정: EXAMPLE_API_KEY=sk-예시-실제값-99 끝")
        assert "sk-예시-실제값-99" not in got["text"]
        assert "EXAMPLE_API_KEY" in got["text"]
        assert "[MASKED]" in got["text"]

    def test_값이_다른_문맥에_나와도_가린다(self, repo):
        """PR 본문에는 KEY=VALUE 형태가 아니라 로그 조각으로 실릴 수 있다."""
        _secrets(repo, EXAMPLE_TTB_KEY="ttbkey12345")
        got = mask_mod.mask_text(repo, "요청 실패: ...&ttbkey=ttbkey12345&q=1")
        assert "ttbkey12345" not in got["text"]

    def test_베어러_토큰_패턴(self, repo):
        got = mask_mod.mask_text(repo, "Authorization: Bearer abc.DEF-123_xyz")
        assert "abc.DEF-123_xyz" not in got["text"]
        assert "Bearer" in got["text"]

    def test_커넥션_문자열의_비밀번호만_가린다(self, repo):
        got = mask_mod.mask_text(repo, "postgres://admin:hunter2@db.example.com:5432/x")
        assert "hunter2" not in got["text"]
        assert "db.example.com" in got["text"]      # 호스트는 남는다
        assert "admin" in got["text"]               # 사용자 이름도 남는다

    def test_클라우드_액세스_키_패턴(self, repo):
        got = mask_mod.mask_text(repo, "key=AKIAIOSFODNN7EXAMPLE rest")
        assert "AKIAIOSFODNN7EXAMPLE" not in got["text"]

    def test_32자_난수처럼_보이는_것을_통째로_가리지_않는다(self, repo):
        """식별자·해시가 지워지면 스택트레이스가 무의미해진다."""
        sha = "c1c558f9ce1b9e16ee4b4acb0be95976fcdb2257"
        got = mask_mod.mask_text(repo, "계약 sha256=%s 이다" % sha)
        assert sha in got["text"]

    def test_비밀_파일_부재는_경고이지_실패가_아니다(self, repo):
        got = mask_mod.mask_text(repo, "평범한 본문")
        assert got["secret_files_missing"] == [".env.local", ".env"]
        assert got["ok"] is True
        assert got["text"] == "평범한 본문"

    def test_짧은_값은_비밀로_보지_않는다(self, repo):
        """빈 값이나 true/1 같은 것을 가리면 본문이 걸레가 된다."""
        _secrets(repo, DEBUG="1", NODE_ENV="test", REAL="비밀값입니다0123")
        got = mask_mod.mask_text(repo, "NODE_ENV=test 이고 DEBUG=1 이다")
        assert got["text"] == "NODE_ENV=test 이고 DEBUG=1 이다"

    def test_cli_가_파일을_읽어_파일로_쓴다(self, repo):
        _secrets(repo, K="비밀값입니다0123")
        src = repo / "in.md"
        src.write_text("본문 비밀값입니다0123\n", encoding="utf-8")
        env = cli.run_mask(repo, str(src), str(repo / "out.md"))
        assert env["exit"] == 0
        out = (repo / "out.md").read_text(encoding="utf-8")
        assert "비밀값입니다0123" not in out
        assert "[MASKED]" in out

    def test_없는_파일은_exit_1(self, repo):
        env = cli.run_mask(repo, str(repo / "없다.md"), str(repo / "out.md"))
        assert env["exit"] == 1


# ---------------------------------------------------------------------------
# N. approve — 승인은 이벤트다. 지문과 등급을 함께 못박는다
# ---------------------------------------------------------------------------


def _enter_06(repo, request_file, phases, grade="PASS"):
    """05 까지를 상태로 위조하고 06 에 세운다. **지문은 실물이다.**"""
    run_id, paths = _enter_05(repo, request_file, phases)
    _p, s = st.load(repo, run_id)
    st.set_phase_status(s, "05-code-review", "passed")
    s["phase"] = "06-pr"
    s["grade"] = grade
    s["review05"] = {"status": "ok", "reviewers_planned": 1, "reviewers_ok": 1,
                     "mode": "merged", "major": 0, "need_more_context": [],
                     "dropped_by_enforcement": 0, "truncated": False}
    config = harness._read_json(repo / harness.CONFIG_REL)
    s["fingerprint"] = st.fingerprint(repo, config)
    st.save(_p, s)
    return run_id, paths


class TestApprove:

    def test_승인이_지문과_등급을_함께_남긴다(self, repo, request_file, phases):
        run_id, _paths = _enter_06(repo, request_file, phases)
        env = cli.run_approve(repo, "06", run_id=run_id)
        assert env["exit"] == 0
        _p, s = st.load(repo, run_id)
        a = s["approval"]["06"]
        assert a["granted"] is True
        assert a["mode"] == "user"
        assert a["scope"] == "push+pr"
        assert a["grade_at_grant"] == "PASS"
        assert a["fingerprint"]["value"] == s["fingerprint"]["value"]

    def test_auto_도_push_pr_까지만_승인한다(self, repo, request_file, phases):
        """06 시점의 등급은 외부 리뷰를 못 본 '예상' 이다 (§3.6)."""
        run_id, _paths = _enter_06(repo, request_file, phases)
        env = cli.run_approve(repo, "06", auto=True, run_id=run_id)
        assert env["exit"] == 0
        _p, s = st.load(repo, run_id)
        assert s["approval"]["06"]["mode"] == "auto"
        assert s["approval"]["06"]["scope"] == "push+pr"

    def test_revoke_가_승인을_되돌리고_사유를_남긴다(self, repo, request_file,
                                                    phases):
        run_id, _paths = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        env = cli.run_approve(repo, "06", revoke=True, run_id=run_id)
        assert env["exit"] == 0
        _p, s = st.load(repo, run_id)
        assert s["approval"]["06"]["granted"] is False
        assert s["approval"]["06"]["revoked_at"]

    def test_05_가_안_끝났으면_exit_3(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        env = cli.run_approve(repo, "06", run_id=run_id)
        assert env["exit"] == 3

    def test_런이_없으면_exit_3(self, repo):
        env = cli.run_approve(repo, "06")
        assert env["exit"] == 3

    def test_승인_뒤_코드가_바뀌면_지문이_어긋난다(self, repo, request_file,
                                                  phases):
        """이 어긋남을 06 이 exit 6 으로 읽는다 — 승인 자동 무효."""
        run_id, _paths = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        (repo / "src" / "lib" / "match.ts").write_text(
            "export function matchTitle(): number { return 1 }\n",
            encoding="utf-8")
        config = harness._read_json(repo / harness.CONFIG_REL)
        _p, s = st.load(repo, run_id)
        saved = s["approval"]["06"]["fingerprint"]
        assert not st.fingerprint_matches(saved, st.fingerprint(repo, config))

    def test_승인이_이벤트로_남는다(self, repo, request_file, phases):
        run_id, paths = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        kinds = [json.loads(l)["kind"]
                 for l in paths.events.read_text(encoding="utf-8").splitlines() if l]
        assert "approved" in kinds


# ---------------------------------------------------------------------------
# O. 06-pr — 승인 · push · PR 요청서. 실행기는 forge 를 부르지 않는다
# ---------------------------------------------------------------------------

import pr as pr_mod  # noqa: E402


def _remote(repo, tmp_path):
    """로컬 bare 리포를 origin 으로 붙인다 — 네트워크를 타지 않는다."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)],
                   capture_output=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main")
    return bare


class TestPr06Preflight:
    """비용 오름차순이고 첫 실패에서 멈춘다. **브랜치를 자동 생성하지 않는다.**"""

    def test_보호_브랜치_위면_exit_3(self, repo, request_file, phases):
        run_id, _p = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 3
        assert "main" in env["render"]

    def test_브랜치_패턴_불일치면_exit_3(self, repo, request_file, phases):
        _branch(repo, "wip")
        run_id, _p = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 3

    def test_승인이_없으면_exit_9_이고_상태를_잠그지_않는다(self, repo,
                                                            request_file, phases,
                                                            tmp_path):
        _branch(repo, "feat-x")
        run_id, _p = _enter_06(repo, request_file, phases)
        _remote(repo, tmp_path)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 9
        assert "승인" in env["render"]
        assert "머지는 포함하지 않습니다" in env["render"]
        _pp, s = st.load(repo, run_id)
        assert s["escalated"] is False

    def test_승인_뒤_코드가_바뀌면_exit_6(self, repo, request_file, phases,
                                          tmp_path):
        _branch(repo, "feat-x")
        run_id, _p = _enter_06(repo, request_file, phases)
        _remote(repo, tmp_path)
        cli.run_approve(repo, "06", run_id=run_id)
        (repo / "src" / "lib" / "match.ts").write_text(
            "export function matchTitle(): number { return 2 }\n",
            encoding="utf-8")
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 6
        assert "재승인" in env["render"]

    def test_철회된_승인은_승인이_아니다(self, repo, request_file, phases,
                                          tmp_path):
        _branch(repo, "feat-x")
        run_id, _p = _enter_06(repo, request_file, phases)
        _remote(repo, tmp_path)
        cli.run_approve(repo, "06", run_id=run_id)
        cli.run_approve(repo, "06", revoke=True, run_id=run_id)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 9


class TestPr06BodyTruth:
    """M41·M42 — 본문이 파이썬 repr 을 찍고 없는 결손을 보고했다."""

    def _body(self, repo, paths, s):
        return pr_mod.build_body(repo, paths, s,
                                 harness._read_json(repo / harness.CONFIG_REL))

    def _verdict(self, paths, adopted):
        (paths.run_dir / "02_verdict.json").write_text(
            json.dumps({"reviewer": "xv", "adopted": adopted},
                       ensure_ascii=False), encoding="utf-8")

    def _plan(self, paths, text):
        (paths.run_dir / "01_plan.md").write_text(text, encoding="utf-8")

    def test_채택_판정이_파이썬_repr_로_나가지_않는다(self, repo, request_file,
                                                    phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        self._verdict(paths, [{"id": "F-1", "verdict": "reject",
                               "reason": "관계의 한쪽 끝이 외부가 아니다"}])
        body = self._body(repo, paths, s)
        assert "{'id'" not in body, body
        assert "F-1" in body and "reject" in body, body
        assert "관계의 한쪽 끝이 외부가 아니다" in body, body

    def test_문자열_원소도_받는다(self, repo, request_file, phases):
        """스키마가 문자열을 금하지 않는다. 본문 조립 중 예외는 06 을 죽인다."""
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        self._verdict(paths, ["F-1 을 채택했다"])
        assert "F-1 을 채택했다" in self._body(repo, paths, s)

    def test_INTENT_블록의_INV_가_본문에_나온다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        intent = json.dumps({"invariants": [
            {"id": "INV-1", "kind": "must", "text": "상한을 바꾸지 않는다"},
            {"id": "INV-2", "kind": "must_not", "text": "로직을 고치지 않는다"}]},
            ensure_ascii=False)
        self._plan(paths, "<!-- INTENT " + intent + " -->" + chr(10) * 2 +
                   "# 플랜" + chr(10))
        body = self._body(repo, paths, s)
        assert "INV-1" in body and "상한을 바꾸지 않는다" in body, body
        assert "INV-2" in body, body
        assert "INV 블록이 없다" not in body, body

    def test_INV_가_진짜_없으면_없다고_적는다(self, repo, request_file, phases):
        """수정이 내용을 지어내지 않게 잠근다."""
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        self._plan(paths, "# 플랜" + chr(10) + chr(10) + "본문뿐이다." + chr(10))
        assert "INV 블록이 없다" in self._body(repo, paths, s)

    def test_헤딩_형태의_INV_도_받는다(self, repo, request_file, phases):
        """다른 스택은 헤딩을 쓸 수 있다 — 폴백을 남긴다."""
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        self._plan(paths, "## INV" + chr(10) * 2 + "- INV-9 지키는 것" +
                   chr(10) * 2 + "## 다음" + chr(10))
        body = self._body(repo, paths, s)
        assert "INV-9" in body and "INV 블록이 없다" not in body, body

    def test_요청_인용이_경계에서_끊기고_끊긴_사실을_적는다(self, repo,
                                                          request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        long_req = (chr(10)).join("%d 번째 줄이다. 문장이 여기서 끝난다." % i
                                  for i in range(200))
        paths.request.write_text(long_req, encoding="utf-8")
        body = self._body(repo, paths, s)
        quoted = [l for l in body.splitlines() if l.startswith("> ")]
        assert quoted, body
        # 마지막 인용 줄이 문장 중간에서 잘리지 않았다
        assert quoted[-1].rstrip().endswith("끝난다."), quoted[-1]
        assert "원문" in body and str(len(long_req)) in body, body


class TestPr06Body:

    def test_본문_최상단이_완료_등급_한_줄이다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases,
                                  grade="PASS_WITH_GAPS")
        _pp, s = st.load(repo, run_id)
        s["gaps"] = ["stage_absent:e2e"]
        st.save(_pp, s)
        body = pr_mod.build_body(repo, paths, s,
                                 harness._read_json(repo / harness.CONFIG_REL))
        first = body.strip().splitlines()[0]
        assert "PASS_WITH_GAPS" in first
        assert "stage_absent:e2e" in body

    def test_본문에_필수_절이_전부_있다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        body = pr_mod.build_body(repo, paths, s,
                                 harness._read_json(repo / harness.CONFIG_REL))
        for sec in ("## 개요", "## 작업 내용", "## 기술적 고려사항",
                    "## 참고사항", "## 체크리스트"):
            assert sec in body, sec

    def test_본문이_마스킹을_거친다(self, repo, request_file, phases, tmp_path):
        _branch(repo, "feat-x")
        _secrets(repo, K="아주비밀한값0123")
        run_id, paths = _enter_06(repo, request_file, phases)
        req = repo / "_workspace" / "requests" / "req.md"
        req.write_text("아주비밀한값0123 을 쓰는 기능\n", encoding="utf-8")
        cli.run_approve(repo, "06", run_id=run_id)
        _remote(repo, tmp_path)
        cli.run_pr(repo, run_id=run_id)
        out = (paths.run_dir / "06_pr_body.md").read_text(encoding="utf-8")
        assert "아주비밀한값0123" not in out


class TestPr06Push:

    def test_성공하면_계약을_지우고_push_하고_요청서를_낸다(self, repo,
                                                          request_file, phases,
                                                          tmp_path):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        contract = repo / "_workspace" / "contract_x.md"
        assert contract.exists()
        cli.run_approve(repo, "06", run_id=run_id)
        _remote(repo, tmp_path)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 0, env["render"]
        assert not contract.exists(), "계약 파일은 push 성공 뒤에 지운다"
        assert (paths.run_dir / "06_pr_body.md").exists()
        req = json.loads((paths.run_dir / "06_pr_req.json")
                         .read_text(encoding="utf-8"))
        assert req["head"] == "feat-x"
        assert req["base"] == "main"
        assert req["forge"] == "github"
        assert req["body_file"].endswith("06_pr_body.md")
        _pp, s = st.load(repo, run_id)
        assert s["pr"]["pushed"] is True

    def test_원격이_없으면_exit_9_삼지선다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, _p = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 9
        assert "원격" in env["render"]
        for opt in ("①", "②", "③"):
            assert opt in env["render"]

    def test_non_fast_forward_는_에스컬레이션이다(self, repo, request_file,
                                                 phases, tmp_path):
        """force-push 금지이므로 자동 해결이 없다 (§E8)."""
        _branch(repo, "feat-x")
        run_id, _p = _enter_06(repo, request_file, phases)
        bare = _remote(repo, tmp_path)
        _git(repo, "push", "-q", "-u", "origin", "feat-x")
        # 원격만 앞서게 만든다 — 다른 클론이 커밋을 얹은 상황
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(bare), str(other)],
                       capture_output=True)
        _git(other, "checkout", "-q", "feat-x")
        (other / "z.txt").write_text("z\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "-c", "user.email=t@e.com", "-c", "user.name=t",
             "commit", "-qm", "other")
        _git(other, "push", "-q", "origin", "feat-x")
        cli.run_approve(repo, "06", run_id=run_id)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 10
        _pp, s = st.load(repo, run_id)
        assert s["escalated"] is True

    def test_force_push_를_쓰지_않는다(self):
        src = (ROOT / "scripts" / "pipeline" / "pr.py").read_text(encoding="utf-8")
        assert "--force" not in src and "-f\"" not in src


class TestPr06ContractLifetime:
    """계약 삭제는 push **이후**다 (G-7).

    실패할 수 있는 `push` 보다 먼저 지우면, push 가 실패했을 때 05 의
    `requires`(계약 파일 실재 + `must_contain`)가 안 채워져 **재개가
    불가능해진다.** 계약은 `_workspace/` 아래 untracked 파일이라 삭제 시점이
    커밋 diff 에 영향을 주지 않는다 — 늦출 이유만 있고 당길 이유가 없다.
    """

    def test_push_가_실패하면_계약이_남아_있다(self, repo, request_file, phases,
                                              tmp_path):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        _remote(repo, tmp_path)
        # 원격 디렉터리를 없애 push 를 실패시킨다.
        import shutil
        shutil.rmtree(str(tmp_path / "origin.git"), ignore_errors=True)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] != 0, env["render"]
        c = repo / "_workspace" / "contract_x.md"
        assert c.exists(), "push 실패 뒤에 05 로 재개할 길이 남아야 한다"

    def test_삭제_전에_스냅샷을_남긴다(self, repo, request_file, phases, tmp_path):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        _remote(repo, tmp_path)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 0, env["render"]
        assert (paths.run_dir / "06_contract_snapshot.md").exists()


class TestPr06ContractAfterDrop:
    """M54 — 계약이 지워진 뒤 `pr` 을 다시 돌리면 본문이 계약 절을 잃었다.

    07 수리를 PR 에 올리려면 재승인 뒤 `pr` 을 다시 돌려야 하는데(P7 이 실제로
    그랬다), 그때 `_contract_sections` 가 이미 없는 파일을 읽어 빈 문자열을
    돌려주고 본문이 **`no_contract` 런이라고 자기를 잘못 보고했다.** 상태는
    여전히 `mode: contract` 라 같은 문서의 체크리스트와 모순됐다.

    되살릴 원본은 이미 있다 — 삭제 직전에 `06_contract_snapshot.md` 로 옮겨
    둔다. 새 사본을 만들지 않고 **읽는 쪽만** 만든다 (M31 · ADR-H022).
    """

    def _body(self, repo, paths, s):
        return pr_mod.build_body(repo, paths, s,
                                 harness._read_json(repo / harness.CONFIG_REL))

    def _with_path(self, repo, run_id):
        """실물 06 은 `_refresh_contract` 가 `path` 를 싣는다. 픽스처는 안 싣는다."""
        p, s = st.load(repo, run_id)
        s["contract"] = dict(s.get("contract") or {},
                             path="_workspace/contract_x.md")
        st.save(p, s)
        return s

    def test_계약이_지워진_뒤_재실행해도_본문이_계약_절을_싣는다(
            self, repo, request_file, phases, tmp_path):
        """가장 중요한 회귀 — P7 이 밟은 정상 경로다."""
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        self._with_path(repo, run_id)
        cli.run_approve(repo, "06", run_id=run_id)
        _remote(repo, tmp_path)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 0, env["render"]
        assert not (repo / "_workspace" / "contract_x.md").exists()

        env2 = cli.run_pr(repo, run_id=run_id)
        assert env2["exit"] == 0, env2["render"]
        body = (paths.run_dir / "06_pr_body.md").read_text(encoding="utf-8")
        assert "matchTitle" in body, body
        assert "POST /api/analyze" in body, body
        assert "no_contract" not in body, body

    def test_삭제가_스냅샷_경로를_상태에_남긴다(self, repo, request_file, phases,
                                                tmp_path):
        """본문이 파일 이름을 짐작하지 않게 한다 — 출처는 상태다."""
        _branch(repo, "feat-x")
        run_id, _paths = _enter_06(repo, request_file, phases)
        self._with_path(repo, run_id)
        cli.run_approve(repo, "06", run_id=run_id)
        _remote(repo, tmp_path)
        assert cli.run_pr(repo, run_id=run_id)["exit"] == 0
        _p, s = st.load(repo, run_id)
        snap = (s.get("contract") or {}).get("snapshot")
        assert snap, s.get("contract")
        assert (repo / snap).exists(), snap

    def test_계약이_살아_있으면_스냅샷을_보지_않는다(self, repo, request_file,
                                                    phases):
        """폴백이 정상 경로를 가로채면 본문이 낡은 계약을 싣는다."""
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        s = self._with_path(repo, run_id)
        snap = paths.run_dir / "06_contract_snapshot.md"
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(CONTRACT.replace("matchTitle", "낡은심볼"),
                        encoding="utf-8")
        s["contract"]["snapshot"] = snap.relative_to(repo).as_posix()
        # 폴백이 실제로 읽히는 경로인지부터 확인한다 — 안 그러면 이 테스트가
        # 「스냅샷을 못 찾았다」를 「스냅샷을 안 봤다」로 잘못 세고 헛돈다.
        assert (repo / s["contract"]["snapshot"]).exists()
        body = self._body(repo, paths, s)
        assert "matchTitle" in body, body
        assert "낡은심볼" not in body, body

    def test_no_contract_런은_문구가_그대로다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        s["contract"] = {"mode": "no_contract", "present": False}
        assert "(no_contract)" in self._body(repo, paths, s)

    def test_계약_런인데_못_읽으면_no_contract_라_적지_않는다(
            self, repo, request_file, phases):
        """**실패와 데이터 없음을 뭉개지 않는다.** 파일도 스냅샷도 없는 것은
        계약이 없는 런이 아니라 지금 못 읽는 것이다."""
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        s = self._with_path(repo, run_id)
        (repo / "_workspace" / "contract_x.md").unlink()
        body = self._body(repo, paths, s)
        assert "no_contract" not in body, body
        assert "읽지 못했다" in body, body


class TestRunAbandon:
    """이어질 일이 없는 런이 `active` 로 남아 있는 것 자체가 거짓이다."""

    def test_abandon_이_런을_닫는다(self, repo, request_file, phases):
        init = cli.run_init(repo, "x", str(request_file))
        run_id = init["run_id"]
        env = cli.run_abandon(repo, run_id=run_id, reason="설계가 바뀌었다")
        assert env["exit"] == 0
        _p, s = st.load(repo, run_id)
        assert s["run_status"] == "abandoned"
        assert s["closed_reason"] == "설계가 바뀌었다"

    def test_사유_없이는_닫지_않는다(self, repo, request_file, phases):
        init = cli.run_init(repo, "x", str(request_file))
        env = cli.run_abandon(repo, run_id=init["run_id"], reason="")
        assert env["exit"] == 2

    def test_버려진_런은_기본값으로_집히지_않는다(self, repo, request_file, phases):
        """살아 있는 런이 따로 있으면 버려진 쪽을 집지 않는다."""
        # run_id 는 요청 바이트에서 유도되므로 두 런의 요청이 달라야 한다.
        other = request_file.with_name("req2.md")
        other.write_text("# 다른 요청\n\n다른 내용이다.\n", encoding="utf-8")
        a = cli.run_init(repo, "a", str(request_file))["run_id"]
        b = cli.run_init(repo, "b", str(other))["run_id"]
        assert a != b
        # 지금 집히는 쪽을 버린다 — 그래야 정렬 운에 기대지 않는다.
        dead = st.latest_run_id(repo)
        alive = b if dead == a else a
        cli.run_abandon(repo, run_id=dead, reason="버린다")
        assert st.latest_run_id(repo) == alive

    def test_에스컬레이션된_런은_계속_집힌다(self, repo, request_file, phases):
        """재개 가능한 런이다 — 안 집으면 화면에서 사라진다."""
        rid = cli.run_init(repo, "a", str(request_file))["run_id"]
        paths, s = st.load(repo, rid)
        st.escalate(paths, s, "사람이 정한다", ["가", "나"], phase="01-plan")
        st.save(paths, s)
        assert st.latest_run_id(repo) == rid

    def test_닫힌_런은_다시_버려지지_않는다(self, repo, request_file, phases):
        rid = cli.run_init(repo, "a", str(request_file))["run_id"]
        cli.run_abandon(repo, run_id=rid, reason="한 번")
        env = cli.run_abandon(repo, run_id=rid, reason="두 번")
        assert env["exit"] == 3


class TestRecord06:
    """PR 결과를 되돌려 받는다. **번호가 갈라지는 것을 여기서 막는다.**"""

    def _pushed(self, repo, request_file, phases, tmp_path):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        _remote(repo, tmp_path)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 0, env["render"]
        return run_id, paths

    def _result(self, paths, **kw):
        d = {"number": 231, "url": "https://example.com/pull/231",
             "state": "open", "action": "created"}
        d.update(kw)
        p = paths.run_dir / "06_pr_result.json"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return p

    def test_결과가_state_pr_에_들어가고_07_로_간다(self, repo, request_file,
                                                    phases, tmp_path):
        run_id, paths = self._pushed(repo, request_file, phases, tmp_path)
        f = self._result(paths)
        env = cli.run_record(repo, "06", str(f), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        _p, s = st.load(repo, run_id)
        assert s["pr"]["number"] == 231
        assert s["pr"]["state"] == "open"
        assert s["phase"] == "07-pr-review"

    def test_번호가_정수가_아니면_exit_8(self, repo, request_file, phases,
                                        tmp_path):
        run_id, paths = self._pushed(repo, request_file, phases, tmp_path)
        f = self._result(paths, number="231")
        env = cli.run_record(repo, "06", str(f), run_id=run_id)
        assert env["exit"] == 8

    def test_상태_어휘_밖은_exit_8(self, repo, request_file, phases, tmp_path):
        run_id, paths = self._pushed(repo, request_file, phases, tmp_path)
        f = self._result(paths, state="draft")
        env = cli.run_record(repo, "06", str(f), run_id=run_id)
        assert env["exit"] == 8

    def test_번호가_갈라지면_exit_8(self, repo, request_file, phases, tmp_path):
        """갱신이어야 할 것을 새로 만들면 07 이 어느 PR 을 볼지 모르게 된다."""
        run_id, paths = self._pushed(repo, request_file, phases, tmp_path)
        _p, s = st.load(repo, run_id)
        s["pr"]["number"] = 7
        st.save(_p, s)
        f = self._result(paths, number=231)
        env = cli.run_record(repo, "06", str(f), run_id=run_id)
        assert env["exit"] == 8
        assert "갈라" in env["render"] or "번호" in env["render"]

    def test_push_전에는_exit_3(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        f = self._result(paths)
        env = cli.run_record(repo, "06", str(f), run_id=run_id)
        assert env["exit"] == 3

    def test_PR_이_이벤트로_남는다(self, repo, request_file, phases, tmp_path):
        run_id, paths = self._pushed(repo, request_file, phases, tmp_path)
        cli.run_record(repo, "06", str(self._result(paths)), run_id=run_id)
        kinds = [json.loads(l)["kind"]
                 for l in paths.events.read_text(encoding="utf-8").splitlines() if l]
        assert "pr_opened" in kinds


# ---------------------------------------------------------------------------
# P. promote — 승격은 07 에서 런당 한 번. 05 는 staged 까지였다
# ---------------------------------------------------------------------------

import promote as promo_mod  # noqa: E402


def _fill_ledger(repo, key_title, category, severity, runs):
    """임계를 넘기도록 같은 유형을 여러 런에 걸쳐 원장에 쌓는다."""
    ldg.seed(repo)
    for rid in runs:
        ldg.append(repo, rid, "05", [
            {"category": category, "severity": severity, "target_role": "impl",
             "title": key_title, "resolution": "repaired",
             "reported_by": ["arch"], "source": "reviewer"}])


def _verdict_file(paths, verdicts):
    p = paths.run_dir / "07_promo_verdict.json"
    p.write_text(json.dumps({"verdicts": verdicts}, ensure_ascii=False),
                 encoding="utf-8")
    return p


class TestPromoteScan:

    def test_후보가_0_이면_모델을_부르지_않고_종결한다(self, repo, request_file,
                                                      phases):
        """초기 런의 최빈 경로다 — 원장이 비어 있다."""
        ldg.seed(repo)
        run_id, _p = _enter_06(repo, request_file, phases)
        env = cli.run_promote(repo, scan=True, run_id=run_id)
        assert env["exit"] == 0
        assert env["data"]["candidates"] == []
        assert env["data"]["needs_model"] is False
        _pp, s = st.load(repo, run_id)
        assert s["promotions"] == []

    def test_임계를_넘으면_후보가_올라온다(self, repo, request_file, phases):
        _fill_ledger(repo, "인가 규칙 누락", "AUTHZ_MISSING_RULE", "critical",
                     ["r1", "r2"])
        run_id, _p = _enter_06(repo, request_file, phases)
        env = cli.run_promote(repo, scan=True, run_id=run_id)
        assert env["exit"] == 0
        assert len(env["data"]["candidates"]) == 1
        assert env["data"]["needs_model"] is True

    def test_한_런에_몰린_것은_후보가_아니다(self, repo, request_file, phases):
        """distinct_runs >= 2 — 그 런의 특성이지 학습 대상이 아니다.

        같은 런의 같은 페이즈에서 두 번 온 것은 이제 **관측 하나**다 (M30).
        그래서 여기서 막는 것은 `distinct_runs` 이전에 누적 자체다.
        """
        _fill_ledger(repo, "인가 규칙 누락", "AUTHZ_MISSING_RULE", "critical",
                     ["r1", "r1"])
        run_id, _p = _enter_06(repo, request_file, phases)
        env = cli.run_promote(repo, scan=True, run_id=run_id)
        assert env["data"]["candidates"] == []
        assert env["data"]["held"] == []

    def test_후보가_0_이면_판정_시한을_함께_말한다(self, repo):
        """후보 0 을 보는 사람이 **그 자리에서** 시한을 본다 (ADR-H033).

        이 분기가 초기 런의 최빈 경로다. "표본이 아직 없다" 만 적으면
        그 말이 몇 런까지 유효한지를 아무도 모른다.
        """
        got = {"candidates": [], "held": [], "distinct_runs": 6,
               "verdict_deadline": {"at": 9, "seen": 6, "remaining": 3,
                                    "due": False}}
        out = cli._promote_scan_render(got)
        assert "판정 시한" in out, out
        assert "distinct_runs" in out, "단위를 말하지 않으면 달력 런으로 읽는다"
        assert "ADR-H033" in out, out

    def test_시한이_지났으면_렌더가_그렇게_말한다(self, repo):
        got = {"candidates": [], "held": [], "distinct_runs": 9,
               "verdict_deadline": {"at": 9, "seen": 9, "remaining": 0,
                                    "due": True}}
        out = cli._promote_scan_render(got)
        assert "지났다" in out or "판정할 때다" in out, out


def _staged_authz(repo, request_file, phases):
    _fill_ledger(repo, "인가 규칙 누락", "AUTHZ_MISSING_RULE", "critical",
                 ["r1", "r2"])
    run_id, paths = _enter_06(repo, request_file, phases)
    cli.run_promote(repo, scan=True, run_id=run_id)
    return run_id, paths


def _one_verdict(**kw):
    """판정은 **어느 후보를 올리는지 가리켜야 한다** — rule_id 는 새로 짓는
    목적지 이름이라 후보의 기본 이름과 다를 수 있다."""
    d = {"action": "create", "judgement": "new", "rule_id": "authz-catchall",
         "category": "AUTHZ_MISSING_RULE", "enforceable": "prose",
         "rationale": "캐치올 위치는 기계가 못 본다"}
    d.update(kw)
    return d


class TestPromoteVerdict:

    def test_duplicate_에서_create_는_금지다(self, repo, request_file, phases):
        run_id, paths = _staged_authz(repo, request_file, phases)
        f = _verdict_file(paths, [_one_verdict(judgement="duplicate",
                                               action="create")])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id)
        assert env["exit"] == 8
        assert "duplicate" in json.dumps(env["data"], ensure_ascii=False)

    def test_contradicts_는_쓰기를_차단하고_에스컬레이션한다(self, repo,
                                                          request_file, phases):
        run_id, paths = _staged_authz(repo, request_file, phases)
        f = _verdict_file(paths, [_one_verdict(judgement="contradicts")])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id)
        assert env["exit"] == 10
        _pp, s = st.load(repo, run_id)
        assert s["escalated"] is True

    def test_기계로_막을_수_있는_규칙의_산문_승격은_exit_8(self, repo,
                                                        request_file, phases):
        _fill_ledger(repo, "경계를 넘는 import", "BOUNDARY_VIOLATION",
                     "critical", ["r1", "r2"])
        run_id, paths = _enter_06(repo, request_file, phases)
        cli.run_promote(repo, scan=True, run_id=run_id)
        f = _verdict_file(paths, [_one_verdict(rule_id="no-restricted-imports",
                                               category="BOUNDARY_VIOLATION",
                                               enforceable="prose")])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id)
        assert env["exit"] == 8

    def test_런당_create_상한은_3건이다(self, repo, request_file, phases):
        run_id, paths = _staged_authz(repo, request_file, phases)
        f = _verdict_file(paths, [_one_verdict(rule_id="r%d" % i)
                                  for i in range(4)])
        # 넷 다 같은 후보를 가리키지만, 상한 검사는 create 의 **개수**를 본다
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id)
        assert env["exit"] == 8
        assert "3" in json.dumps(env["data"], ensure_ascii=False)


class TestPromoteApply:

    def _ready(self, repo, request_file, phases):
        run_id, paths = _staged_authz(repo, request_file, phases)
        f = _verdict_file(paths, [_one_verdict()])
        return run_id, paths, f

    def test_적용이_changelog_에_줄을_남긴다(self, repo, request_file, phases):
        run_id, paths, f = self._ready(repo, request_file, phases)
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id)
        assert env["exit"] == 0, env["render"]
        log = (repo / "docs" / "harness" / "pipeline" / "ledger"
               / "rules_changelog.md").read_text(encoding="utf-8")
        assert "authz-catchall" in log
        assert run_id in log

    def test_적용이_promotions_를_applied_로_만든다(self, repo, request_file,
                                                   phases):
        run_id, paths, f = self._ready(repo, request_file, phases)
        cli.run_promote(repo, apply=True, verdict_file=str(f), run_id=run_id)
        _pp, s = st.load(repo, run_id)
        assert s["promotions"]
        assert all(p["status"] in ("applied", "rejected", "skipped")
                   for p in s["promotions"])
        assert any(p["status"] == "applied" for p in s["promotions"])

    def test_산출_파일이_07_promo_applied_다(self, repo, request_file, phases):
        run_id, paths, f = self._ready(repo, request_file, phases)
        cli.run_promote(repo, apply=True, verdict_file=str(f), run_id=run_id)
        assert (paths.run_dir / "07_promo_applied.json").exists()

    def test_lint_승격인데_베이스라인_diff_가_없으면_rejected(self, repo,
                                                            request_file,
                                                            phases):
        """규칙은 추가했는데 아무것도 안 막는 것이 조용히 통과하지 않는다."""
        _fill_ledger(repo, "경계를 넘는 import", "BOUNDARY_VIOLATION",
                     "critical", ["r1", "r2"])
        run_id, paths = _enter_06(repo, request_file, phases)
        cli.run_promote(repo, scan=True, run_id=run_id)
        f = _verdict_file(paths, [_one_verdict(rule_id="no-restricted-imports",
                                               category="BOUNDARY_VIOLATION",
                                               enforceable="lint",
                                               baseline_diff="")])
        cli.run_promote(repo, apply=True, verdict_file=str(f), run_id=run_id)
        _pp, s = st.load(repo, run_id)
        assert any(p["status"] == "rejected" for p in s["promotions"])
        assert "베이스라인" in json.dumps(s["promotions"], ensure_ascii=False)

    def test_승격이_이벤트로_남는다(self, repo, request_file, phases):
        run_id, paths, f = self._ready(repo, request_file, phases)
        cli.run_promote(repo, apply=True, verdict_file=str(f), run_id=run_id)
        kinds = [json.loads(l)["kind"]
                 for l in paths.events.read_text(encoding="utf-8").splitlines()
                 if l]
        assert "promoted" in kinds


def _baseline_runner(repo, content='{"rules": 1}', code=0, seen=None):
    """`baseline_cmd` 를 흉내낸다 — 베이스라인 파일을 **실제로 쓴다.**"""
    def run(name, argv, cwd, timeout_sec):
        if seen is not None:
            seen.append((name, list(argv)))
        p = Path(repo) / "harness" / "lint-baseline.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return code, "lint 출력"
    return run


def _silent_runner(code=0, seen=None):
    """돌긴 했는데 **아무 파일도 안 바뀐** 경우."""
    def run(name, argv, cwd, timeout_sec):
        if seen is not None:
            seen.append((name, list(argv)))
        return code, ""
    return run


class TestPromoteBaseline:
    """lint 승격의 베이스라인을 **기계가 잰다.**

    이 클래스가 막는 실패는 하나다 — 모델이 `baseline_diff` 에 아무 문자열이나
    적어 보내면 "규칙은 추가했는데 아무것도 안 막는다" 가 통과하는 것. 07 의
    `external` 이 봇 원문에서 다시 세이는 것과 같은 규율이다.
    """

    def _lint_verdict(self, **kw):
        d = {"rule_id": "no-restricted-imports",
             "category": "BOUNDARY_VIOLATION", "enforceable": "lint"}
        d.update(kw)
        return _one_verdict(**d)

    def _ready(self, repo, request_file, phases):
        _fill_ledger(repo, "경계를 넘는 import", "BOUNDARY_VIOLATION",
                     "critical", ["r1", "r2"])
        run_id, paths = _enter_06(repo, request_file, phases)
        cli.run_promote(repo, scan=True, run_id=run_id)
        return run_id, paths

    def test_어댑터의_baseline_cmd_를_실행기가_직접_돌린다(self, repo,
                                                          request_file, phases):
        run_id, paths = self._ready(repo, request_file, phases)
        seen = []
        f = _verdict_file(paths, [self._lint_verdict()])
        cli.run_promote(repo, apply=True, verdict_file=str(f), run_id=run_id,
                        runner=_baseline_runner(repo, seen=seen))
        assert seen, "baseline_cmd 가 한 번도 안 돌았다"
        argv = seen[0][1]
        assert any("lint-baseline.json" in a for a in argv), argv

    def test_prose_승격은_베이스라인을_재지_않는다(self, repo, request_file,
                                                  phases):
        """재는 비용은 lint 승격에만 든다."""
        run_id, paths = _staged_authz(repo, request_file, phases)
        seen = []
        f = _verdict_file(paths, [_one_verdict()])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id, runner=_silent_runner(seen=seen))
        assert env["exit"] == 0
        assert seen == []

    def test_베이스라인이_안_바뀌면_rejected(self, repo, request_file, phases):
        run_id, paths = self._ready(repo, request_file, phases)
        f = _verdict_file(paths, [self._lint_verdict()])
        cli.run_promote(repo, apply=True, verdict_file=str(f), run_id=run_id,
                        runner=_silent_runner())
        _pp, s = st.load(repo, run_id)
        assert any(p["status"] == "rejected" for p in s["promotions"])
        assert "베이스라인" in json.dumps(s["promotions"], ensure_ascii=False)

    def test_베이스라인이_바뀌면_applied_이다(self, repo, request_file, phases):
        run_id, paths = self._ready(repo, request_file, phases)
        f = _verdict_file(paths, [self._lint_verdict()])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id, runner=_baseline_runner(repo))
        assert env["exit"] == 0, env["render"]
        _pp, s = st.load(repo, run_id)
        assert any(p["status"] == "applied" for p in s["promotions"])

    def test_changelog_의_베이스라인_칸은_기계가_잰_값이다(self, repo,
                                                          request_file, phases):
        run_id, paths = self._ready(repo, request_file, phases)
        f = _verdict_file(paths, [self._lint_verdict()])
        cli.run_promote(repo, apply=True, verdict_file=str(f), run_id=run_id,
                        runner=_baseline_runner(repo))
        log = (repo / "docs" / "harness" / "pipeline" / "ledger"
               / "rules_changelog.md").read_text(encoding="utf-8")
        assert "lint-baseline.json" in log

    def test_신고하지_않는_것이_정상_경로다(self, repo, request_file, phases):
        """`baseline_diff` 를 안 실어도 통과한다 — 재는 것은 기계의 일이다."""
        run_id, paths = self._ready(repo, request_file, phases)
        v = self._lint_verdict()
        assert "baseline_diff" not in v
        f = _verdict_file(paths, [v])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id, runner=_baseline_runner(repo))
        assert env["exit"] == 0, env["render"]

    def test_모델_신고가_기계값과_다르면_exit_8_이고_아무것도_안_쓴다(
            self, repo, request_file, phases):
        run_id, paths = self._ready(repo, request_file, phases)
        f = _verdict_file(paths,
                          [self._lint_verdict(baseline_diff="내가 지어낸 diff")])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id, runner=_baseline_runner(repo))
        assert env["exit"] == 8, env["render"]
        assert "내가 지어낸 diff" in env["render"]
        _pp, s = st.load(repo, run_id)
        assert all(p["status"] == "staged" for p in s["promotions"])

    def test_린터의_비영_종료는_실패가_아니다(self, repo, request_file, phases):
        """린터가 위반을 찾으면 0 이 아니다. 그것이 정상이고 판정은 diff 가 한다."""
        run_id, paths = self._ready(repo, request_file, phases)
        f = _verdict_file(paths, [self._lint_verdict()])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id,
                              runner=_baseline_runner(repo, code=1))
        assert env["exit"] == 0, env["render"]
        _pp, s = st.load(repo, run_id)
        assert any(p["status"] == "applied" for p in s["promotions"])

    def test_실행할_수_없으면_infra_이고_아무것도_안_쓴다(self, repo,
                                                        request_file, phases):
        """127 은 데이터 문제가 아니라 시스템 문제다 — rejected 로 적지 않는다."""
        run_id, paths = self._ready(repo, request_file, phases)
        f = _verdict_file(paths, [self._lint_verdict()])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id, runner=_silent_runner(code=127))
        assert env["exit"] == 10, env["render"]
        _pp, s = st.load(repo, run_id)
        assert all(p["status"] == "staged" for p in s["promotions"])

    def test_baseline_cmd_가_없으면_갭으로_강등하고_진행한다(self, repo,
                                                            request_file,
                                                            phases):
        """스킵을 통과로 적지 않는다 — entrypoint_resolver 부재와 같은 처리다."""
        ad_p = repo / "harness" / "adapters" / "nextjs-ts.json"
        ad = json.loads(ad_p.read_text(encoding="utf-8"))
        ad["stages"]["lint"].pop("baseline_cmd")
        ad_p.write_text(json.dumps(ad, ensure_ascii=False, indent=2),
                        encoding="utf-8")

        run_id, paths = self._ready(repo, request_file, phases)
        f = _verdict_file(paths, [self._lint_verdict()])
        env = cli.run_promote(repo, apply=True, verdict_file=str(f),
                              run_id=run_id, runner=_silent_runner())
        assert env["exit"] == 0, env["render"]
        _pp, s = st.load(repo, run_id)
        assert any(p["status"] == "applied" for p in s["promotions"])
        assert s["grade"] == "PASS_WITH_GAPS"
        assert "promotion_baseline_unverified" in s["gaps"]

    def test_갭_어휘가_보고서에서_설명된다(self):
        """어휘에 없으면 보고서가 '설명하지 못한다' 고 적는다 — 그러지 않게 한다."""
        line = rep_mod.explain_gap("promotion_baseline_unverified")
        assert "어휘에 없는" not in line


class TestPromoteTargetMatching:
    """판정이 어느 후보를 가리키는가 (G-1).

    `apply()` 가 한 루프 안에서 `finding_key` 정확 일치와 `category` 약한
    일치를 **섞어** 검사하고 먼저 걸리는 쪽에서 멈췄다. 배열 앞쪽의 약한
    일치가 뒤쪽의 정확한 일치를 이긴다 — 엉뚱한 규칙이 changelog 에 쓰이고
    근거 열도 다른 버킷에서 온다.
    """

    def _promos(self):
        return [
            {"rule_id": "a", "finding_key": "KEY-A", "category": "TX_BOUNDARY",
             "enforceable": "prose", "severity": "major", "count": 3,
             "distinct_runs": 2, "status": "staged", "reason": None},
            {"rule_id": "b", "finding_key": "KEY-B", "category": "TX_BOUNDARY",
             "enforceable": "prose", "severity": "critical", "count": 9,
             "distinct_runs": 4, "status": "staged", "reason": None},
        ]

    def test_finding_key_일치가_category_일치를_이긴다(self, repo):
        ldg.seed(repo)
        promos = self._promos()
        promo_mod.apply(repo, "r9", promos, [
            {"rule_id": "tx-b", "finding_key": "KEY-B", "category": "TX_BOUNDARY",
             "enforceable": "prose", "judgement": "new", "action": "create",
             "rationale": "필요하다"}])
        by_key = {p["finding_key"]: p for p in promos}
        assert by_key["KEY-B"]["status"] == "applied"
        assert by_key["KEY-A"]["status"] == "staged", \
            "앞쪽의 category 일치가 정확한 키 일치를 이기면 안 된다"

    def test_한_판정이_두_후보를_동시에_바꾸지_않는다(self, repo):
        ldg.seed(repo)
        promos = self._promos()
        _p, rows = promo_mod.apply(repo, "r9", promos, [
            {"rule_id": "x", "category": "TX_BOUNDARY", "enforceable": "prose",
             "judgement": "new", "action": "create", "rationale": "하나"},
            {"rule_id": "y", "category": "TX_BOUNDARY", "enforceable": "prose",
             "judgement": "new", "action": "create", "rationale": "둘"}])
        touched = [p for p in promos if p["status"] != "staged"]
        assert len(touched) == 2, "두 판정이 같은 행을 잡아 앞을 덮으면 안 된다"
        assert len(rows) == 2

    def test_없는_finding_key_는_category_로_낙하하지_않는다(self, repo):
        """이름을 부른 것과 다른 지적이 승격되느니 거부가 맞다."""
        ldg.seed(repo)
        promos = self._promos()
        errors, _blocked = promo_mod.check_verdicts(repo, [
            {"rule_id": "z", "finding_key": "KEY-없음", "category": "TX_BOUNDARY",
             "enforceable": "prose", "judgement": "new", "action": "create"}],
            promos)
        assert errors, "후보에 없는 finding_key 는 거부돼야 한다"

    def test_check_verdicts_와_apply_가_같은_후보를_가리킨다(self, repo):
        ldg.seed(repo)
        promos = self._promos()
        v = {"rule_id": "tx", "finding_key": "KEY-B", "category": "TX_BOUNDARY",
             "enforceable": "prose", "judgement": "new", "action": "create",
             "rationale": "r"}
        errors, _b = promo_mod.check_verdicts(repo, [v], promos)
        assert errors == []
        promo_mod.apply(repo, "r9", promos, [v])
        assert [p for p in promos if p["status"] == "applied"][0][
            "finding_key"] == "KEY-B"


class TestPromoteRuleKey:
    """승격 행의 신원은 `rule_key` 다 (ADR-H034). 옛 행은 폴백으로 산다.

    버킷의 대표 `finding_key` 를 신원으로 쓰면 **런마다 다른 인스턴스**가
    실려 나가(`ErrorBanner` → 다음 런엔 `RATE_LIMIT_WINDOW_MS`) 같은 규칙이
    두 승격 행으로 갈라진다. `merge_staged` 가 그것을 합치지 못하고
    `resolve_target` 이 옛 행을 못 찾는다 — 축을 규칙으로 바꾼 값이 여기서
    새어 나간다.
    """

    def _bucket(self, rule_key, finding_keys, **kw):
        b = {"rule_key": rule_key, "rule_slug": "out_of_contract",
             "finding_key": None, "finding_keys": sorted(finding_keys),
             "category": "NAMING", "enforceable": "lint", "rule": None,
             "severity": "major", "count": 3, "distinct_runs": 2}
        b.update(kw)
        return b

    def test_런이_달라도_같은_규칙은_한_행이다(self, repo):
        """`merge_staged` 의 축이 `rule_key` 라야 성립한다."""
        p9 = promo_mod.stage([self._bucket("RK-1", ["fk-a", "fk-b", "fk-c"])])
        assert len(p9) == 1 and p9[0]["rule_key"] == "RK-1", p9
        p10 = promo_mod.stage([self._bucket("RK-1", ["fk-d", "fk-e", "fk-f"],
                                            count=6, distinct_runs=2)])
        merged = promo_mod.merge_staged(p9, p10)
        assert len(merged) == 1, merged
        assert merged[0]["count"] == 6, merged

    def test_판정이_rule_key_로_후보를_집는다(self, repo):
        ldg.seed(repo)
        promos = promo_mod.stage([self._bucket("RK-1", ["fk-a"]),
                                  self._bucket("RK-2", ["fk-b"])])
        errors, _b = promo_mod.check_verdicts(repo, [
            {"rule_id": "naming-out", "rule_key": "RK-2", "category": "NAMING",
             "enforceable": "lint", "judgement": "new", "action": "create"}],
            promos)
        assert errors == [], errors
        promo_mod.apply(repo, "r9", promos, [
            {"rule_id": "naming-out", "rule_key": "RK-2", "category": "NAMING",
             "enforceable": "lint", "judgement": "new", "action": "create",
             "rationale": "반복된다"}])
        by = {p["rule_key"]: p for p in promos}
        assert by["RK-2"]["status"] == "applied", promos
        assert by["RK-1"]["status"] == "staged", promos

    def test_없는_rule_key_는_category_로_낙하하지_않는다(self, repo):
        """`finding_key` 때와 같은 규율이다 — 못 찾는 편이 낫다."""
        ldg.seed(repo)
        promos = promo_mod.stage([self._bucket("RK-1", ["fk-a"])])
        errors, _b = promo_mod.check_verdicts(repo, [
            {"rule_id": "z", "rule_key": "RK-없음", "category": "NAMING",
             "enforceable": "lint", "judgement": "new", "action": "create"}],
            promos)
        assert errors, "후보에 없는 rule_key 는 거부돼야 한다"

    def test_옛_행은_finding_key_로_계속_집힌다(self, repo):
        """`state.promotions` 에 이미 쌓인 행에는 `rule_key` 가 없다."""
        ldg.seed(repo)
        promos = [{"rule_id": "a", "finding_key": "KEY-A", "category": "NAMING",
                   "enforceable": "lint", "severity": "major", "count": 3,
                   "distinct_runs": 2, "status": "staged", "reason": None}]
        errors, _b = promo_mod.check_verdicts(repo, [
            {"rule_id": "a", "finding_key": "KEY-A", "category": "NAMING",
             "enforceable": "lint", "judgement": "new", "action": "create"}],
            promos)
        assert errors == [], errors


class TestPromoteStatePreservation:
    """`--scan` 은 읽기다 (G-3).

    읽기가 상태를 바꾸는 것이 이 결함의 뿌리다. `--scan`/`--stage` 가
    `s["promotions"]` 를 무조건 새 staged 목록으로 덮어써, `--apply` 뒤에
    다시 `--scan` 이 돌면 `applied` 가 사라진다 — `report` 가 exit 6 을 내고
    두 번째 `--apply` 에서 changelog 가 중복된다.
    """

    def test_scan_은_applied_를_되돌리지_않는다(self, repo, request_file, phases):
        _fill_ledger(repo, "인가 규칙 누락", "AUTHZ_MISSING_RULE", "critical",
                     ["r1", "r2"])
        run_id, _p = _enter_06(repo, request_file, phases)
        paths, s = st.load(repo, run_id)
        s["promotions"] = [{"rule_id": "keep", "finding_key": "K",
                            "category": "AUTHZ_MISSING_RULE",
                            "enforceable": "prose", "severity": "critical",
                            "count": 2, "distinct_runs": 2,
                            "status": "applied", "reason": "이미 썼다"}]
        st.save(paths, s)
        cli.run_promote(repo, scan=True, run_id=run_id)
        _pp, s2 = st.load(repo, run_id)
        assert s2["promotions"][0]["status"] == "applied"

    def test_stage_는_applied_를_지우지_않는다(self, repo, request_file, phases):
        _fill_ledger(repo, "인가 규칙 누락", "AUTHZ_MISSING_RULE", "critical",
                     ["r1", "r2"])
        run_id, _p = _enter_06(repo, request_file, phases)
        cli.run_promote(repo, stage=True, run_id=run_id)
        paths, s = st.load(repo, run_id)
        assert s["promotions"], "후보가 올라와야 한다"
        s["promotions"][0]["status"] = "applied"
        s["promotions"][0]["reason"] = "이미 썼다"
        st.save(paths, s)
        cli.run_promote(repo, stage=True, run_id=run_id)
        _pp, s2 = st.load(repo, run_id)
        assert s2["promotions"][0]["status"] == "applied"
        assert s2["promotions"][0]["reason"] == "이미 썼다"

    def test_stage_는_새_후보를_더한다(self, repo, request_file, phases):
        _fill_ledger(repo, "인가 규칙 누락", "AUTHZ_MISSING_RULE", "critical",
                     ["r1", "r2"])
        run_id, _p = _enter_06(repo, request_file, phases)
        cli.run_promote(repo, stage=True, run_id=run_id)
        _fill_ledger(repo, "트랜잭션 경계가 없다", "TX_BOUNDARY", "critical",
                     ["r3", "r4"])
        cli.run_promote(repo, stage=True, run_id=run_id)
        _pp, s = st.load(repo, run_id)
        cats = {p["category"] for p in s["promotions"]}
        assert cats == {"AUTHZ_MISSING_RULE", "TX_BOUNDARY"}

    def test_promotions_가_빈_리스트면_전량_재stage_하지_않는다(
            self, repo, request_file, phases):
        """`or` 가 적법하게 빈 `[]` 를 거짓으로 읽는 자리 — G-4:1655 와 같은 모양."""
        _fill_ledger(repo, "인가 규칙 누락", "AUTHZ_MISSING_RULE", "critical",
                     ["r1", "r2"])
        run_id, paths = _enter_06(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        s["promotions"] = []
        st.save(_p, s)
        vf = paths.run_dir / "07_promo_verdict.json"
        vf.write_text(json.dumps({"verdicts": [
            {"rule_id": "authz", "category": "AUTHZ_MISSING_RULE",
             "enforceable": "prose", "judgement": "new", "action": "create",
             "rationale": "r"}]}, ensure_ascii=False), encoding="utf-8")
        cli.run_promote(repo, apply=True, verdict_file=str(vf), run_id=run_id)
        _pp, s2 = st.load(repo, run_id)
        # 아무것도 staged 되지 않은 런에서 판정만으로 후보가 되살아나면
        # `--stage` 를 건너뛴 승격이 성립한다.
        assert all(p["status"] != "staged" for p in s2["promotions"]), \
            [p["status"] for p in s2["promotions"]]


class TestPromoteFlush:

    def test_flush_가_staged_잔여를_종결한다(self, repo, request_file, phases):
        run_id, _p = _staged_authz(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        assert any(p["status"] == "staged" for p in s["promotions"])
        env = cli.run_promote(repo, flush=True, run_id=run_id)
        assert env["exit"] == 0
        _pp, s = st.load(repo, run_id)
        assert not any(p["status"] == "staged" for p in s["promotions"])
        assert all(p["status"] in ("applied", "rejected", "skipped")
                   for p in s["promotions"])


# ---------------------------------------------------------------------------
# Q. review07 — 생략 조건은 결정론이다. 봇이 없으면 생략이 성립하지 않는다
# ---------------------------------------------------------------------------

import review07 as rv7  # noqa: E402


def _enter_07(repo, request_file, phases, review05_status="ok", major=0,
              decide=False):
    run_id, paths = _enter_06(repo, request_file, phases)
    _p, s = st.load(repo, run_id)
    st.set_phase_status(s, "06-pr", "passed")
    s["phase"] = "07-pr-review"
    s["pr"] = {"number": 231, "state": "open", "pushed": True, "head": "feat-x"}
    s["review05"] = dict(s["review05"], status=review05_status, major=major)
    st.save(_p, s)
    if decide:
        # 07 의 절차는 `review07` → 내장 리뷰 → `record` 다. 그 첫 단계를
        # 건너뛰면 외부 계수의 권위가 제출자에게 넘어간다 (G-6).
        cli.run_review07(repo, run_id=run_id)
    return run_id, paths


def _enable_bot(repo, **kw):
    """봇을 켠다. **끈 상태가 기본**이라 켜는 쪽이 명시적이어야 한다."""
    p = repo / harness.CONFIG_REL
    d = harness._read_json(p)
    d["external_pr_review"] = dict(
        {"enabled": True, "bot_logins": ["some-bot"], "poll_sec": 1,
         "timeout_sec": 1}, **kw)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _external(paths, **kw):
    d = {"status": "reviewed", "major": 0, "findings": [],
         "change_requested": False}
    d.update(kw)
    p = paths.run_dir / "07_external.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return p


def _spend(repo, run_id, counter, reason):
    """카운터를 한 번 태운다 — 04·05 가 수리했다는 흔적이다."""
    _p, s = st.load(repo, run_id)
    st.counter_inc(s, counter, 2, reason)
    st.save(_p, s)


class TestReview07Skip:

    def test_봇이_꺼져_있어도_깨끗한_런은_생략한다(self, repo, request_file,
                                                  phases):
        """05 의 gen 이 일반 정합성을 봤고 04·05 에 수리가 없었다 — 07 의
        내장 리뷰가 더할 것이 없다 (ADR-H043). 관측기를 뺀 것이 아니라 바꾼
        것이므로 gap 도 없다."""
        run_id, paths = _enter_07(repo, request_file, phases)
        env = cli.run_review07(repo, run_id=run_id)
        assert env["exit"] == 0
        assert env["data"]["external"]["status"] == "disabled"
        assert env["data"]["skip"] is True
        assert env["data"]["effort"] == "skipped"
        assert env["data"]["skip_reason"] == "clean_05"
        assert "record --phase 07" in (env["next_command"] or "")
        _pp, s = st.load(repo, run_id)
        assert s["review07"]["skip_reason"] == "clean_05"
        assert not any(g.startswith("external:") for g in s.get("gaps") or [])

    def test_05_에서_수리가_있었으면_low_다(self, repo, request_file, phases):
        """고친 코드는 두 번째 눈을 받는다."""
        run_id, paths = _enter_07(repo, request_file, phases)
        _spend(repo, run_id, "review_repair", "review_blocking")
        env = cli.run_review07(repo, run_id=run_id)
        assert env["data"]["skip"] is False
        assert env["data"]["effort"] == "low"
        assert env["data"]["skip_reason"] is None

    def test_04_게이트_수리가_있었으면_low_다(self, repo, request_file, phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        _spend(repo, run_id, "repair", "gate_failure")
        env = cli.run_review07(repo, run_id=run_id)
        assert env["data"]["skip"] is False
        assert env["data"]["effort"] == "low"

    def test_형식_반려만_있었으면_수리가_아니라_생략한다(self, repo,
                                                       request_file, phases):
        """형식 반려는 예산은 태우지만 코드를 고친 것이 아니다 (ADR-H029)."""
        run_id, paths = _enter_07(repo, request_file, phases)
        _spend(repo, run_id, "review_repair", "format_reject")
        env = cli.run_review07(repo, run_id=run_id)
        assert env["data"]["skip"] is True
        assert env["data"]["effort"] == "skipped"

    def test_05_에_Major_가_남아_있으면_low_다(self, repo, request_file, phases):
        run_id, paths = _enter_07(repo, request_file, phases, major=1)
        env = cli.run_review07(repo, run_id=run_id)
        assert env["data"]["skip"] is False
        assert env["data"]["effort"] == "low"

    def test_봇이_켜져_있는데_무응답이면_low_이고_gap_이다(self, repo,
                                                        request_file, phases):
        """있어야 할 관측기가 없는 것은 결손이다 — config 로 뺀 것과 다르다."""
        run_id, paths = _enter_07(repo, request_file, phases)
        _enable_bot(repo)
        env = cli.run_review07(repo, run_id=run_id)
        assert env["data"]["external"]["status"] == "timeout"
        assert env["data"]["skip"] is False
        assert env["data"]["effort"] == "low"
        _pp, s = st.load(repo, run_id)
        assert "external:timeout" in (s.get("gaps") or [])

    def test_repaired_before_07_은_수리_사유만_센다(self):
        assert rv7.repaired_before_07({}) is False
        assert rv7.repaired_before_07({"counters": {"review_repair": {
            "used": 1, "spent": [{"reason": "format_reject"}]}}}) is False
        assert rv7.repaired_before_07({"counters": {"repair": {
            "used": 1, "spent": [{"reason": "gate_failure"}]}}}) is True
        assert rv7.repaired_before_07({"counters": {"review_repair": {
            "used": 2, "spent": [{"reason": "format_reject"},
                                 {"reason": "review_blocking"}]}}}) is True

    def test_reviewed_이고_major_0_이면_생략한다(self, repo, request_file,
                                                phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        _enable_bot(repo)
        f = _external(paths)
        env = cli.run_review07(repo, external=str(f), run_id=run_id)
        assert env["data"]["skip"] is True
        assert env["data"]["effort"] == "skipped"

    def test_05_가_ok_가_아니면_medium_이다(self, repo, request_file, phases):
        """리뷰 결손을 비싼 쪽으로 메운다."""
        run_id, paths = _enter_07(repo, request_file, phases,
                                  review05_status="degraded")
        _enable_bot(repo)
        f = _external(paths)
        env = cli.run_review07(repo, external=str(f), run_id=run_id)
        assert env["data"]["skip"] is False
        assert env["data"]["effort"] == "medium"

    def test_external_major_가_있으면_생략하지_않는다(self, repo, request_file,
                                                     phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        _enable_bot(repo)
        # major 는 봇의 자진 신고가 아니라 **findings 구조에서 센다.**
        f = _external(paths, findings=[
            {"title": "인가 누락", "severity": "major", "quote": "x"}])
        env = cli.run_review07(repo, external=str(f), run_id=run_id)
        assert env["data"]["skip"] is False

    def test_not_a_review_는_생략_불성립이고_등급을_떨어뜨린다(self, repo,
                                                             request_file,
                                                             phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        _enable_bot(repo)
        f = _external(paths, status="not_a_review")
        env = cli.run_review07(repo, external=str(f), run_id=run_id)
        assert env["data"]["skip"] is False
        assert env["data"]["effort"] == "low"
        _pp, s = st.load(repo, run_id)
        assert s["grade"] == "PASS_WITH_GAPS"
        assert any("external" in g for g in s["gaps"])

    def test_어휘_밖_status_는_exit_8(self, repo, request_file, phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        _enable_bot(repo)
        f = _external(paths, status="좋았음")
        env = cli.run_review07(repo, external=str(f), run_id=run_id)
        assert env["exit"] == 8


class TestReview07Gaps:
    """gap 기록은 effort 분기와 **독립이다** (G-5).

    `decide()` 가 하나의 if/elif 사슬에 두 결정을 엮어 두어, 05 가 `ok` 가
    아니면 `external:*` gap 이 영영 안 생겼다. 결손 둘 중 하나만 보고서에
    남는다.
    """

    def test_05_결손과_외부_결손이_둘_다_남는다(self, repo, request_file, phases):
        run_id, paths = _enter_07(repo, request_file, phases,
                                  review05_status="degraded")
        _enable_bot(repo)      # 켜 놓고 무응답 — 있어야 할 관측기가 없다
        env = cli.run_review07(repo, run_id=run_id)
        _p, s = st.load(repo, run_id)
        gaps = s.get("gaps") or []
        assert any(g.startswith("external:") for g in gaps), gaps
        assert any(g.startswith("review05:") for g in gaps), gaps
        assert env["data"]["effort"] == "medium", "결손은 비싼 쪽으로 메운다"

    def test_gap_은_조합에서_일관된다(self, repo):
        """r05.status × external.status 의 조합. 순서를 바꿔 구멍을 옮기지 않았다.

        `disabled` 는 gap 이 아니다 — config 로 뺀 관측기이고 대체 관측기가
        05 의 gen 이다 (ADR-H043). `timeout` · `not_a_review` 는 있어야 할
        관측기가 없는 것이라 여전히 gap 이다.
        """
        cfg = _config(repo)
        cases = [
            ("ok", "reviewed", []),
            ("ok", "disabled", []),
            ("ok", "timeout", ["external:timeout"]),
            ("degraded", "reviewed", ["review05:degraded"]),
            ("degraded", "disabled", ["review05:degraded"]),
            ("degraded", "not_a_review", ["review05:degraded",
                                          "external:not_a_review"]),
        ]
        for r05, ext, want in cases:
            got = rv7.decide({"review05": {"status": r05, "major": 0}},
                             {"status": ext, "major": 0}, cfg)
            assert sorted(got["gaps"]) == sorted(want), (r05, ext, got["gaps"])


class TestRecord07ExternalAuthority:
    """외부 Major 의 권위는 `review07` 의 재계수에 있다 (G-6 · 불변식 8)."""

    def test_review07_없이_record_하면_거부된다(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases)
        env = cli.run_record(repo, "07", str(_r07(paths)), run_id=run_id)
        assert env["exit"] == 3
        assert "review07" in env["render"]

    def test_자진_신고된_major_를_저장하지_않는다(self, repo, request_file,
                                                 phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        cli.run_record(repo, "07", str(_r07(paths)), run_id=run_id)
        _p, s = st.load(repo, run_id)
        # 봇이 꺼져 있으므로 review07 이 센 값은 disabled · 0 이다.
        assert s["review07"]["external"]["status"] == "disabled"
        assert s["review07"]["external"]["major"] == 0

    def test_신고와_기계_계수가_다르면_exit_8(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        f = _r07(paths, external={"status": "reviewed", "major": 3})
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] == 8
        assert "기계" in env["render"] or "대조" in env["render"]

    def test_신고가_기계_계수와_같으면_통과한다(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        f = _r07(paths, external={"status": "disabled", "major": 0})
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]

    def test_내장_리뷰를_생략한_런도_승격_경로는_그대로다(self, repo,
                                                        request_file, phases):
        """스킵되는 것은 절차 4번(`/code-review`)뿐이다. 05 의 원장 줄은
        `record --phase 05` 가 이미 썼고, `record --phase 07` → `promote --scan`
        은 그대로 돈다 (ADR-H043). 07 을 페이즈째 건너뛰면 승격 쓰기가 사라진다."""
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        _p, s = st.load(repo, run_id)
        assert s["review07"]["code_review"] == "skipped", s["review07"]
        ldg.append(repo, run_id, "05", [_finding(category="NAMING")])
        f = _r07(paths, code_review="skipped")
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        assert len(ldg.read_all(repo)) == 1, "07 이 05 의 관측을 지우지 않는다"
        env = cli.run_promote(repo, scan=True, run_id=run_id)
        assert env["exit"] == 0, env["render"]
        _p, s = st.load(repo, run_id)
        assert s["review07"]["code_review"] == "skipped"
        assert s["review07"]["escaped_05"] == 0


class TestReview07Severity:

    def test_심각도를_못_가르면_Major_로_낙하한다(self):
        """모르는 것이 괜찮은 것이 되면 안 된다 (§E1)."""
        got = rv7.normalize_external({"status": "reviewed", "findings": [
            {"title": "뭔가 이상하다", "quote": "x"}]})
        assert got["findings"][0]["severity"] == "major"
        assert got["major"] == 1

    def test_구조가_없으면_not_a_review_다(self):
        """헤딩 텍스트가 아니라 구조로 판정한다 — 봇 출력 언어에 안 기댄다."""
        got = rv7.normalize_external({"body": "리뷰했습니다. 좋아 보이네요."})
        assert got["status"] == "not_a_review"

    def test_사람_코멘트는_수리_대상이_아니다(self):
        got = rv7.normalize_external({"status": "reviewed", "findings": [
            {"title": "이건 어때요", "severity": "major", "source": "human"}]})
        assert got["findings"] == []
        assert len(got["human_comments"]) == 1
        assert got["major"] == 0


class TestReview07Audit:

    def test_audit_run_은_생략_조건을_만족해도_medium_을_강제한다(
            self, repo, request_file, phases, monkeypatch):
        run_id, paths = _enter_07(repo, request_file, phases)
        _enable_bot(repo)
        f = _external(paths)
        monkeypatch.setattr(rv7, "audit_due", lambda root: True)
        env = cli.run_review07(repo, external=str(f), run_id=run_id)
        assert env["data"]["skip"] is False
        assert env["data"]["effort"] == "medium"
        assert env["data"]["audit_run"] is True
        _pp, s = st.load(repo, run_id)
        assert s["audit"]["is_audit_run"] is True

    def test_audit_주기는_5런마다다(self, repo):
        d = repo / "_workspace" / "runs"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(4):
            (d / ("2026090%d-0000-000%d" % (i, i))).mkdir()
        assert rv7.audit_due(repo) is False
        (d / "20260905-0000-0005").mkdir()
        assert rv7.audit_due(repo) is True


# ---------------------------------------------------------------------------
# R. record --phase 07 — escaped_05 를 세고, 변경 요청은 차단이다
# ---------------------------------------------------------------------------


def _r07(paths, **kw):
    d = {"code_review": "low", "findings": [], "change_requested": False,
         "human_comments": []}
    d.update(kw)
    p = paths.run_dir / "07_pr_review.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return p


class TestRecord07SlugVocabulary:
    """07 도 같은 대조를 한다 — **빼면 반사실의 후보 하나가 사라진다.**

    `doc_contradicts_code` 후보 4건 중 2건이 07 행이다. 그리고 07 은 05 와
    같은 결함에 다른 이름을 붙이면 새 것으로 세어지는 자리라(M48), 어휘가
    한쪽에만 있으면 그 계수가 축을 가로질러 어긋난다.
    """

    def _f(self, rule_slug=None, **kw):
        d = {"id": "R7-1", "category": "DOC_CODE_DRIFT", "severity": "major",
             "target_role": "main", "title": "문서와 코드가 어긋난다",
             "path": "docs/TRD.md", "quote": "x", "source": "code-review",
             "evidence": "같은 자리다"}
        if rule_slug is not None:
            d["rule_slug"] = rule_slug
        d.update(kw)
        return d

    def _record(self, repo, request_file, phases, finding):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        _p, s = st.load(repo, run_id)
        s.setdefault("pr", {})["head_sha"] = _git(
            repo, "rev-parse", "HEAD").stdout.strip()
        st.save(_p, s)
        return cli.run_record(repo, "07", str(_r07(paths, findings=[finding])),
                              run_id=run_id)

    def test_어휘가_선언된_카테고리에_슬러그를_요구한다(
            self, repo, request_file, phases):
        got = self._record(repo, request_file, phases, self._f())
        assert got["exit"] == 8, got
        assert "rule_slug" in json.dumps(got, ensure_ascii=False)

    def test_어휘_밖_슬러그는_거부된다(self, repo, request_file, phases):
        got = self._record(repo, request_file, phases,
                           self._f(rule_slug="지어낸_슬러그"))
        assert got["exit"] == 8, got
        assert "doc_contradicts_code" in json.dumps(got, ensure_ascii=False)

    def test_어휘_안이면_원장에_슬러그가_남는다(
            self, repo, request_file, phases):
        got = self._record(repo, request_file, phases,
                           self._f(rule_slug="doc_contradicts_code"))
        assert got["exit"] != 8, got
        rows = [r for r in ldg.read_all(repo) if r.get("phase") == "07"]
        assert rows and rows[-1]["rule_slug"] == "doc_contradicts_code", rows

class TestRecord07Resolution:
    """07 의 원장 줄이 스스로 모순되지 않는가 (M49).

    `_record_07` 이 `resolution="deferred"` 를 **하드코딩**해서, 메인이 실제로
    고친 지적도 `deferred` 로 굳었다. P6 의 `R7-2` 는 `07_pr_review.json` 이
    `resolution: "repaired"` · `repaired_by: "main"` 으로 적고 실제로
    `7f94226` 이 고쳤는데, 원장 줄은 `deferred` + `repaired_by: "main"` 이다 —
    `dict(f, ...)` 가 `repaired_by` 는 남기고 `resolution` 만 덮었다.
    **한 줄이 스스로 모순된다.**

    `deferred` 는 `EXCLUDED_FROM_COUNT` 에 없으므로 **고쳐진 결함이 "반복되는
    미해결"로 승격 집계에 학습된다.** P2 의 G-6 이 07 경로에서 재발한 것이다.

    다만 자진 신고를 그대로 받지 않는다 — 불변식 8. "고쳤다"는 `git` 으로
    확인 가능하므로 확인한다.
    """

    def _f(self, **kw):
        d = {"id": "R7-2", "category": "DOC_CODE_DRIFT", "severity": "major",
             "target_role": "main", "title": "문서와 코드가 어긋난다",
             "path": "docs/TRD.md", "quote": "x", "source": "code-review",
             # C5 부터 어휘를 선언한 카테고리는 슬러그가 필수다 (ADR-H035).
             "rule_slug": "doc_contradicts_code",
             "evidence": "같은 자리다"}
        d.update(kw)
        return d

    def _push_base(self, repo, run_id):
        """06 이 push 한 시점을 상태에 박는다."""
        _p, s = st.load(repo, run_id)
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        s.setdefault("pr", {})["head_sha"] = head
        st.save(_p, s)
        return head

    def _repair(self, repo, rel, text="바뀐다\n"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "repair")

    def test_기본값은_deferred_다(self, repo, request_file, phases):
        """안 적은 것은 안 고친 것이다 — 여기서는 폴백이 맞다."""
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        self._push_base(repo, run_id)
        cli.run_record(repo, "07", str(_r07(paths, findings=[self._f()])),
                       run_id=run_id)
        rows = [r for r in ldg.read_all(repo) if r.get("phase") == "07"]
        assert rows and rows[-1]["resolution"] == "deferred", rows

    def test_고친_것이_repaired_로_남는다(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        self._push_base(repo, run_id)
        self._repair(repo, "docs/TRD.md")
        env = cli.run_record(repo, "07", str(_r07(paths, findings=[
            self._f(resolution="repaired", repaired_by="main")])), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        rows = [r for r in ldg.read_all(repo) if r.get("phase") == "07"]
        assert rows[-1]["resolution"] == "repaired", rows[-1]
        assert rows[-1]["repaired_by"] == "main", rows[-1]

    def test_안_고쳐_놓고_repaired_라_하면_exit_8(self, repo, request_file, phases):
        """자진 신고 중 기계로 확인 가능한 것은 기계로 확인한다 (불변식 8)."""
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        self._push_base(repo, run_id)
        env = cli.run_record(repo, "07", str(_r07(paths, findings=[
            self._f(resolution="repaired", repaired_by="main")])), run_id=run_id)
        assert env["exit"] == 8, env["render"]
        assert "repaired" in env["render"], env["render"]

    def test_path_없이_repaired_를_주장할_수_없다(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        self._push_base(repo, run_id)
        f = self._f(resolution="repaired", repaired_by="main")
        f.pop("path")
        env = cli.run_record(repo, "07", str(_r07(paths, findings=[f])),
                             run_id=run_id)
        assert env["exit"] == 8, env["render"]

    def test_07_에서는_main_만_수리한다(self, repo, request_file, phases):
        """07 절차에 역할 호출이 없다 — 다른 주체를 적으면 그것은 사실이 아니다."""
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        self._push_base(repo, run_id)
        self._repair(repo, "docs/TRD.md")
        env = cli.run_record(repo, "07", str(_r07(paths, findings=[
            self._f(resolution="repaired", repaired_by="impl")])), run_id=run_id)
        assert env["exit"] == 8, env["render"]

    def test_어휘_밖_resolution_은_거부된다(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        self._push_base(repo, run_id)
        env = cli.run_record(repo, "07", str(_r07(paths, findings=[
            self._f(resolution="고쳤음")])), run_id=run_id)
        assert env["exit"] == 8, env["render"]

    def test_기준점이_없으면_주장을_받지_않고_갭으로_적는다(self, repo, request_file,
                                                          phases):
        """확인할 수 없는 것을 확인한 것처럼 적지 않는다 — 조용히 통과도 아니다."""
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        # push 시점 커밋을 박지 않는다 — 옛 런의 모양이다.
        self._repair(repo, "docs/TRD.md")
        env = cli.run_record(repo, "07", str(_r07(paths, findings=[
            self._f(resolution="repaired", repaired_by="main")])), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        rows = [r for r in ldg.read_all(repo) if r.get("phase") == "07"]
        assert rows[-1]["resolution"] == "deferred", rows[-1]
        _p, s = st.load(repo, run_id)
        assert any("repair_unverified" in g for g in s.get("gaps") or []), s["gaps"]

    def test_06_이_push_시점_커밋을_남긴다(self, repo, request_file, phases,
                                            tmp_path):
        """기계 확인의 기준점이 없으면 07 이 아무것도 대조하지 못한다."""
        _branch(repo, "feat-x")
        run_id, _paths = _enter_06(repo, request_file, phases)
        cli.run_approve(repo, "06", run_id=run_id)
        _remote(repo, tmp_path)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 0, env["render"]
        _p, s = st.load(repo, run_id)
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert s["pr"]["head_sha"] == head, s["pr"]


class TestEscaped05Reraise:
    """07 이 05 의 지적을 **가리킬 수 있는가** (M48).

    `escaped_05` 는 05 라우팅 품질의 유일한 지표인데, dedup 이
    `sha1(category|target_role|title)` 하나뿐이라 **07 이 같은 결함에 다른
    이름을 붙이면 새 것으로 센다.**

    P6 의 `R7-1`(`OTHER` · "정규화 제목이 빈 항목이 한 키로 접혀…")은 05 의
    `data` 가 이미 낸 `F-7`(`CONTRACT_DEFECT` · "제목을 못 읽은 항목이 모두
    같은 mergeKey 라…")과 같은 결함이다. `07_pr_review.json` 의 `note` 가
    사람 말로 그렇게 적는데 기계는 `deduped: 0` · `escaped_05: 2` 를 냈다 —
    **지표가 05 를 실제보다 나쁘게 적었다.**

    M21 이 05 라운드 안에서 같은 문제를 `reraised_from_previous` 라는 1급
    어휘로 풀었다. 그 어휘가 01·02·05 에 있고 **07 에만 없었다.**

    `finding_key` 는 바꾸지 않는다 — 05 단조성과 승격 집계가 같은 함수를 쓰고,
    키를 바꾸면 원장의 과거 키가 전부 무의미해진다. **키를 바꾸는 것이 아니라
    경계에 선언을 하나 더 두는 것**이다.
    """

    OPEN05 = {"key": "a" * 40, "id": "F-7", "severity": "major",
              "reviewer": "data", "title_norm": "제목을 못 읽은 항목이 한 키로 접힌다"}

    def _with_open_05(self, repo, run_id):
        """05 가 major 하나를 열어 둔 채 07 에 온 런."""
        _p, s = st.load(repo, run_id)
        node = s.setdefault("phases", {}).setdefault("05-code-review", {})
        node["rounds"] = {"1": {"data": {"keys": [dict(self.OPEN05)],
                                         "closed": []}}}
        st.save(_p, s)
        return s

    def _finding(self, **kw):
        d = {"id": "R7-1", "category": "OTHER", "severity": "major",
             "target_role": "impl", "title": "정규화 제목이 빈 항목이 한 키로 접힌다",
             "path": "src/lib/merge.ts", "quote": "mergeKey", "source": "code-review",
             "evidence": "같은 자리다"}
        d.update(kw)
        return d

    def test_봉투가_05_의_열린_지적을_싣는다(self, repo, request_file, phases):
        """모델이 재구성하면 그 재구성이 곧 결함이다."""
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases)
        self._with_open_05(repo, run_id)
        env = cli.run_next(repo, run_id=run_id)
        assert self.OPEN05["key"] in env["render"], env["render"]
        assert "reraised_from_previous" in env["render"], env["render"]

    def test_가리킨_지적은_escaped_05_에서_빠진다(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        self._with_open_05(repo, run_id)
        f = _r07(paths, findings=[
            self._finding(reraised_from_previous=self.OPEN05["key"])])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        _p, s = st.load(repo, run_id)
        assert s["review07"]["escaped_05"] == 0, s["review07"]
        assert s["review07"]["deduped"] == 1, s["review07"]

    def test_안_가리키면_여전히_새_것으로_센다(self, repo, request_file, phases):
        """선언 기반이다 — 자동 의미 dedup 이 아니라는 것을 정직하게 잠근다."""
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        self._with_open_05(repo, run_id)
        f = _r07(paths, findings=[self._finding()])
        cli.run_record(repo, "07", str(f), run_id=run_id)
        _p, s = st.load(repo, run_id)
        assert s["review07"]["escaped_05"] == 1, s["review07"]

    def test_열려_있지_않은_것을_가리키면_exit_8(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        self._with_open_05(repo, run_id)
        f = _r07(paths, findings=[
            self._finding(reraised_from_previous="b" * 40)])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] == 8, env["render"]
        assert "reraised_from_previous" in env["render"], env["render"]


class TestRecord07:

    def test_깨끗하면_08_로_간다(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        env = cli.run_record(repo, "07", str(_r07(paths)), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        _p, s = st.load(repo, run_id)
        assert s["phase"] == "08-report"
        assert s["review07"]["escaped_05"] == 0

    def test_05_가_못_잡은_것이_escaped_05_로_센다(self, repo, request_file,
                                                  phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        f = _r07(paths, findings=[
            {"id": "G-1", "category": "TX_BOUNDARY", "severity": "major",
             "target_role": "impl", "title": "트랜잭션 경계가 없다",
             "source": "code-review", "quote": "x"}])
        cli.run_record(repo, "07", str(f), run_id=run_id)
        _p, s = st.load(repo, run_id)
        assert s["review07"]["escaped_05"] == 1

    def test_05_가_이미_낸_것은_dedup_된다(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        same = {"category": "TX_BOUNDARY", "severity": "major",
                "target_role": "impl", "title": "트랜잭션 경계가 없다"}
        ldg.append(repo, run_id, "05", [dict(same, resolution="repaired",
                                             reported_by=["arch"],
                                             source="reviewer")])
        f = _r07(paths, findings=[dict(same, id="G-1", source="code-review",
                                       quote="x")])
        cli.run_record(repo, "07", str(f), run_id=run_id)
        _p, s = st.load(repo, run_id)
        assert s["review07"]["escaped_05"] == 0

    def test_변경_요청_미해결은_exit_10(self, repo, request_file, phases):
        """PR 체크가 빨간불인데 파이프라인이 초록불인 척하지 않는다."""
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        f = _r07(paths, change_requested=True, findings=[
            {"id": "G-1", "category": "TX_BOUNDARY", "severity": "major",
             "target_role": "impl", "title": "고쳐라", "source": "external",
             "quote": "x"}])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] == 10

    def test_변경_요청인데_findings_가_비면_exit_8(self, repo, request_file,
                                                  phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        f = _r07(paths, change_requested=True, findings=[])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] == 8

    def test_어휘_밖_source_는_exit_8(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        f = _r07(paths, findings=[
            {"id": "G-1", "category": "TX_BOUNDARY", "severity": "major",
             "target_role": "impl", "title": "x", "source": "내가지어낸출처",
             "quote": "x"}])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] == 8

    def test_PR_이_머지됐으면_아무것도_안_하고_끝낸다(self, repo, request_file,
                                                     phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        _p, s = st.load(repo, run_id)
        s["pr"]["state"] = "merged"
        st.save(_p, s)
        env = cli.run_record(repo, "07", str(_r07(paths)), run_id=run_id)
        assert env["exit"] in (0, 11)
        _p, s = st.load(repo, run_id)
        assert any("pr_closed" in g or "pr_merged" in g for g in s["gaps"])

    def test_사람_코멘트는_수리_대상이_아니다(self, repo, request_file, phases):
        ldg.seed(repo)
        run_id, paths = _enter_07(repo, request_file, phases, decide=True)
        f = _r07(paths, human_comments=[{"body": "이건 어때요"}])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] in (0, 11)


# ---------------------------------------------------------------------------
# S. 08-report — 재지 못한 것이 조용히 통과하지 않는다
# ---------------------------------------------------------------------------

import report as rep_mod  # noqa: E402


def _enter_08(repo, request_file, phases, grade="PASS"):
    ldg.seed(repo)
    run_id, paths = _enter_07(repo, request_file, phases, decide=True)
    _p, s = st.load(repo, run_id)
    st.set_phase_status(s, "07-pr-review", "passed")
    s["phase"] = "08-report"
    s["grade"] = grade
    s["promotions"] = []
    s["review07"] = {"external": {"status": "disabled", "major": 0},
                     "code_review": "low", "escaped_05": 0}
    st.save(_p, s)
    return run_id, paths


def _seed_timing_events(paths):
    """실물 런의 모양을 심는다 — `_enter_08` 은 상태만 조립하고 이벤트를 안 남긴다.

    P8 이 실제로 그린 궤적을 줄인 것이다: 01 이 한 번 돌고, 02 가 되돌리고,
    **되돌아간 01 에는 진입 이벤트가 없고**, 그 사이에 사람을 기다린다.
    """
    def at(h, m):
        return datetime(2026, 3, 1, h, m, 0, tzinfo=st.TZ)

    # `_enter_08` 이 남긴 `run_created` 는 실제 지금 시각이다. 심는 이벤트가
    # 그보다 과거면 구간이 음수가 된다 — 단위 테스트와 같게 비우고 시작한다.
    paths.events.write_text("", encoding="utf-8")
    st.append_event(paths, "phase_enter", phase="01-plan", now=at(10, 0))
    st.append_event(paths, "escalated", phase="01-plan", now=at(10, 10))
    st.append_event(paths, "resumed", phase="01-plan", now=at(11, 10))
    st.append_event(paths, "phase_pass", phase="01-plan", now=at(11, 20))
    st.append_event(paths, "phase_enter", phase="02-cross-verify", now=at(11, 20))
    # 02 가 되돌린다. 되돌아간 01 에 phase_enter 가 안 찍히는 것이 실물이다.
    st.append_event(paths, "submit_received", phase="01-plan", now=at(11, 30))
    st.append_event(paths, "phase_pass", phase="01-plan", now=at(11, 50))
    st.append_event(paths, "phase_enter", phase="08-report", now=at(12, 0))


def _report_data(paths, **kw):
    d = {"narrative": {"문제": "재시도가 안 됐다", "원인": "상태 머신",
                       "해결": "리듀서 수정", "결과": "통과",
                       "배운 점": "AC 가 증상을 잠가야 한다"}}
    d.update(kw)
    p = paths.run_dir / "08_report_data.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return p


class TestReport08:

    def test_필수_섹션_다섯이_전부_있다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 11, env["render"]
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        for sec in rep_mod.REQUIRED_SECTIONS:
            assert sec in out, sec

    def test_페이즈별_호출과_07_생략_사유가_보고서에_있다(self, repo,
                                                        request_file, phases):
        """05·07 의 비용을 나란히 보는 자리다 (ADR-H043). 생략 런의 escaped_05
        는 표본이 아니라고 보고서가 스스로 말해야 한다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        s.setdefault("budget", {}).setdefault("model_calls", {})["by_phase"] = {
            "05-code-review": 3, "07-pr-review": 1}
        s["review07"] = {"external": {"status": "disabled", "major": 0},
                         "code_review": "skipped", "escaped_05": 0,
                         "skip_reason": "clean_05"}
        st.save(_p, s)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "05-code-review: 3" in out and "07-pr-review: 1" in out, out
        assert "clean_05" in out
        assert "표본 아님" in out

    def test_캘리브레이션_상태가_partial_과_unverified_를_드러낸다(
            self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "partial" in out or "옛 값" in out
        assert "verified" in out or "미검증" in out

    def test_staged_잔여가_있으면_exit_6(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        s["promotions"] = [{"rule_id": "r", "status": "staged", "reason": None}]
        st.save(_p, s)
        _report_data(paths)
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 6
        assert "flush" in env["render"]

    def test_INCOMPLETE_면_08_을_돌리지_않는다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases,
                                  grade="INCOMPLETE")
        _report_data(paths)
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 3
        assert "ESCALATION" in env["render"]

    def test_입력이_없으면_exit_3(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 3

    def test_같은_run_id_로_다시_쓰면_덮어쓴다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        _report_data(paths, narrative={"문제": "두 번째 판"})
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "두 번째 판" in out
        assert out.count("## 완료 등급") == 1

    def test_승격_목록은_원장에서_자동으로_나온다(self, repo, request_file,
                                                phases):
        """모델이 빠뜨릴 수 없다 — 서술이 비어도 표는 나온다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        s["promotions"] = [{"rule_id": "authz-catchall", "status": "applied",
                            "category": "AUTHZ_MISSING_RULE", "reason": "x"}]
        st.save(_p, s)
        _report_data(paths, narrative={})
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "authz-catchall" in out

    def test_승격_절이_판정_시한을_적는다(self, repo, request_file, phases):
        """`## 승격된 규칙` 이 "없다" 로 끝나면 그것이 몇 런까지 정상인지
        아무도 모른다. 시한과 **그 셈의 단위**를 같이 적는다 (ADR-H033)."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        head, _sep, tail = out.partition("## 승격된 규칙")
        assert _sep, out
        section = tail.split("## 건너뛴 게이트")[0]
        assert "판정 시한" in section, section
        assert "distinct_runs" in section, "단위를 안 적으면 달력 런으로 읽힌다"
        assert "지적을 0건 낸 런은" in section, "한계를 칸 이름이 말해야 한다"

    def test_시한_줄이_원장을_못_읽으면_안_적는다(self):
        """못 잰 것을 0 으로 채우지 않는다 ([[ADR-H007]])."""
        assert rep_mod._verdict_deadline_lines({}) == []
        assert rep_mod._verdict_deadline_lines({"ledger": {}}) == []

    def test_카테고리_축_표가_보고서에_나온다(self):
        """`_ledger_axis_lines` 의 첫 회귀다 — 실물 런 보고서로만 확인돼
        있었다. 시한 줄을 같은 절에 붙이므로 여기서 함께 잠근다."""
        data = {"ledger": {"by_category": [
            {"category": "NAMING", "count": 86, "distinct_runs": 4,
             "distinct_keys": 84, "promotable": True}]}}
        lines = rep_mod._ledger_axis_lines(data)
        body = "\n".join(lines)
        assert "`NAMING`" in body and "86" in body and "84" in body, body
        assert rep_mod._ledger_axis_lines({}) == [], "없으면 절을 안 만든다"

    def test_보고서는_파이프라인을_실패시키지_않는다(self, repo, request_file,
                                                   phases):
        """섹션이 비어도 산출되고 **런도 닫힌다** — 원장에 기록만 한다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths, narrative={})
        env = cli.run_report(repo, run_id=run_id)
        assert env["ok"] is True
        assert env["exit"] == 11
        assert env["data"]["closed"] is True, "섹션이 빠져도 런은 닫힌다"

    # ── M24. 08 의 동사가 런을 닫는다.

    def test_report_가_런을_닫는다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 11, env["render"]
        assert env["data"]["closed"] is True
        _p, s = st.load(repo, run_id)
        assert s["phases"]["08-report"]["status"] == "passed"
        assert s["phase"] == st.DONE
        assert s["run_status"] == st.DONE
        assert s.get("closed_at")

    def test_런_완료가_render_에_있다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        env = cli.run_report(repo, run_id=run_id)
        assert "런 완료" in env["render"]
        assert run_id in env["render"], "보고서 경로도 함께 남는다"

    def test_닫힌_런에_다시_쓰면_덮어쓰고_exit_0(self, repo, request_file, phases):
        """`08-report.md` 가 요구하는 재작성 — 전이는 한 번뿐이다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        assert cli.run_report(repo, run_id=run_id)["exit"] == 11
        _report_data(paths, narrative={"문제": "두 번째 판"})
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 0
        assert env["data"]["closed"] is True
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "두 번째 판" in out
        _p, s = st.load(repo, run_id)
        assert s["run_status"] == st.DONE
        kinds = [json.loads(x)["kind"] for x
                 in paths.events.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert kinds.count("run_closed") == 1, "두 번 닫히지 않는다"

    def test_07_이_안_끝났으면_보고서만_쓰고_닫지_않는다(self, repo, request_file,
                                                      phases):
        """전이 조건은 08 자신의 `requires` 다. 없으면 03 에서 부른 report 가
        런을 닫아 버린다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        st.set_phase_status(s, "07-pr-review", "running")
        st.save(_p, s)
        _report_data(paths)
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 0
        assert env["data"]["closed"] is False
        assert (repo / "docs" / "harness" / "pipeline" / "runs"
                / ("%s.md" % run_id)).exists(), "보고서는 그래도 쓴다"
        _p, s = st.load(repo, run_id)
        assert s["run_status"] == "active"
        assert st.phase_status(s, "08-report") != "passed"

    def test_닫힌_런은_latest_run_id_에서_빠진다(self, repo, request_file, phases):
        """`state.py` 의 `!= "done"` 필터에 드디어 생산자가 생긴다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        # 살아 있는 런이 하나라도 있으면 닫힌 런은 뽑히지 않는다.
        other, _ = st.create_run(repo, "other", request_file)
        assert st.latest_run_id(repo) == other.run_id

    def test_run_closed_이벤트가_등급과_gaps_를_담는다(self, repo, request_file,
                                                     phases):
        run_id, paths = _enter_08(repo, request_file, phases,
                                  grade="PASS_WITH_GAPS")
        _p, s = st.load(repo, run_id)
        s["gaps"] = ["stage_absent:e2e"]
        st.save(_p, s)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        ev = [json.loads(x) for x
              in paths.events.read_text(encoding="utf-8").splitlines() if x.strip()]
        closed = [e for e in ev if e["kind"] == "run_closed"]
        assert len(closed) == 1
        assert closed[0]["data"]["grade"] == "PASS_WITH_GAPS"
        assert closed[0]["data"]["gaps"] == ["stage_absent:e2e"]

    def test_닫힌_런에_advance_는_전이하지_않는다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        env = cli.run_advance(repo, "08", run_id=run_id)
        assert env["exit"] == 0
        assert env["data"]["closed"] is True
        ev = [json.loads(x) for x
              in paths.events.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len([e for e in ev if e["kind"] == "phase_pass"
                    and e.get("phase") == "08-report"]) == 1

    def test_record_08_은_report_로_안내한다(self, repo, request_file, phases):
        """"미구현" 이라고 말하던 자리다 — 구현돼 있고 동사가 다를 뿐이다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        src = _report_data(paths)
        env = cli.run_record(repo, "08", str(src), run_id=run_id)
        assert env["exit"] == 2
        assert "report" in env["render"]
        assert "미구현" not in env["render"]

    def test_gaps_가_건너뛴_게이트로_나열된다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases,
                                  grade="PASS_WITH_GAPS")
        _p, s = st.load(repo, run_id)
        s["gaps"] = ["stage_absent:e2e", "adapter_unverified"]
        st.save(_p, s)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "stage_absent:e2e" in out
        assert "adapter_unverified" in out

    def test_모델_호출_수는_근사로_표기된다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "instructed" in out
        assert "과소" in out and "과다" in out, "두 오차 방향이 드러나야 한다"

    # ── C2-1. 08 이 자기 소요를 적는다.

    def test_소요_미측정_문단이_사라졌다(self, repo, request_file, phases):
        """여섯 런이 이 문장을 적었다. 이제 잰다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _seed_timing_events(paths)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "소요 시간은 미측정이다" not in out

    def test_페이즈별_표에_벽시계와_에스컬레이션_대기가_따로_있다(
            self, repo, request_file, phases):
        """**칸 이름이 벽시계라고 말해야 한다.** 이 값에는 사람이 답을 쓰는
        대기가 섞여 있고, P8 은 그것이 60.2% 였다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _seed_timing_events(paths)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "벽시계(대기 포함)" in out
        assert "에스컬레이션 대기" in out
        assert "01-plan" in out
        # 되돌아간 01 의 두 구간이 합산된다 — 1:20:00 + 0:30:00.
        assert "1:50:00" in out
        # 그중 한 시간은 사람을 기다린 것이다.
        assert "1:00:00" in out

    def test_재진입_횟수가_같은_표에_있다(self, repo, request_file, phases):
        """구간 수는 소요의 분모가 아니라 별개 사실이다 — 같은 벽시계라도
        한 번에 지난 페이즈와 세 번 되돌아온 페이즈는 다른 일이다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _seed_timing_events(paths)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "2구간" in out

    def test_timing_이_None_이면_미측정이라고_적는다(self, repo, request_file,
                                                    phases):
        """못 잰 것을 0 으로 채우지 않는다 (`_tbl` 의 규율과 동형)."""
        run_id, _paths = _enter_08(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        text, _missing = rep_mod.build(s, {}, {}, [], None)
        assert "소요 시간은 미측정이다" in text
        assert "벽시계(대기 포함)" not in text

    def test_소요_기준과_사각이_보고서에_인쇄된다(self, repo, request_file,
                                                phases):
        """`모델 호출 수` 칸이 `instructed` 와 사각 둘을 적는 것과 같은 자리다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _seed_timing_events(paths)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert st.PHASE_DURATION_BASIS in out
        for spot in st.PHASE_DURATION_BLIND_SPOTS:
            assert spot in out, spot

    def test_소요_표가_새_섹션을_만들지_않는다(self, repo, request_file, phases):
        """`## 비용과 시간` 이 이미 있다. 섹션 목록은 team-spec 이 잠근다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        heads = [ln for ln in out.splitlines() if ln.startswith("## ")]
        assert len(heads) == len(set(heads)), heads
        for sec in rep_mod.REQUIRED_SECTIONS:
            assert sec in out, sec


# ---------------------------------------------------------------------------
# T. doctor — 06 이 exit 9 로 멈출 것을 기동 전에, 무료로 알려 준다
# ---------------------------------------------------------------------------


class TestDoctorRemote:

    def test_원격이_없으면_WARN_이고_막지는_않는다(self, repo):
        """원격 없이 로컬까지만 가는 것도 정당한 선택이고, 그 선택은 사람의 것이다."""
        config = harness._read_json(repo / harness.CONFIG_REL)
        got = cli._check_remote(repo, config)
        assert got["status"] == "WARN"
        assert "3지선다" in got["message"]

    def test_원격과_base_가_있으면_PASS(self, repo, tmp_path):
        _branch(repo, "feat-x")
        _remote(repo, tmp_path)
        config = harness._read_json(repo / harness.CONFIG_REL)
        got = cli._check_remote(repo, config)
        assert got["status"] == "PASS"

    def test_실물_리포에서_원격_검사가_통과한다(self):
        config = harness._read_json(ROOT / harness.CONFIG_REL)
        got = cli._check_remote(ROOT, config)
        assert got["status"] == "PASS", got["message"]


class TestDoctorExternalBot:

    def test_꺼져_있으면_PASS_이고_그_이유를_적는다(self, repo):
        config = harness._read_json(repo / harness.CONFIG_REL)
        got = cli._check_external_bot(config)
        assert got["status"] == "PASS"
        assert "생략" in got["message"] and "수리" in got["message"]

    def test_켜_놓고_대상이_없으면_FAIL(self, repo):
        _enable_bot(repo, bot_logins=[])
        config = harness._read_json(repo / harness.CONFIG_REL)
        got = cli._check_external_bot(config)
        assert got["status"] == "FAIL"
        assert "기다린다" in got["message"]

    def test_상속값임을_메시지가_밝힌다(self, repo):
        _enable_bot(repo)
        config = harness._read_json(repo / harness.CONFIG_REL)
        got = cli._check_external_bot(config)
        assert got["status"] == "PASS"
        assert "미검증 상속값" in got["message"]


class TestChangelogHeader:

    def test_표_헤더가_산문이_나열한_열_개와_맞는다(self):
        """산문은 열 개를 나열하는데 표 헤더는 아홉이었다 — 철회 사유가 없었다."""
        import ledger as L
        head = [l for l in L.CHANGELOG_HEADER.splitlines()
                if l.startswith("| 날짜")]
        assert head, L.CHANGELOG_HEADER
        cols = [c for c in head[0].split("|") if c.strip()]
        assert len(cols) == 10, cols
        assert "철회 사유" in head[0]

    def test_실물_changelog_도_같은_헤더다(self):
        p = (ROOT / "docs" / "harness" / "pipeline" / "ledger"
             / "rules_changelog.md")
        assert "철회 사유" in p.read_text(encoding="utf-8")


class TestHorizonRender:

    def test_다음이_없으면_런_완료라고_말한다(self):
        got = cli._horizon_render(None)
        assert "런 완료" in got

    def test_범위를_문자열로_박지_않고_페이즈에서_유도한다(self):
        loaded, _broken = cli.load_phases(ROOT)
        got = cli._horizon_render("09-nope", loaded)
        assert "00-triage" in got and "08-report" in got
        assert "01~04" not in got


# ---------------------------------------------------------------------------
# Z. ADR-H041 · H042 — 01 수렴 문턱 · 델타 리뷰어 · 02 미편집 생략 · 계수
# ---------------------------------------------------------------------------

def _crit(id_="F-1", title="설계가 요청과 어긋난다"):
    return {"id": id_, "severity": "critical", "category": "scope",
            "title": title, "quote": "빈 문자열을 먼저 거른다."}


def _minor(id_="F-9", title="이름이 모호하다"):
    return {"id": id_, "severity": "minor", "category": "naming",
            "title": title, "quote": "빈 문자열을 먼저 거른다."}


def _phase_file(repo, name):
    return repo / "harness" / "phases" / name


class TestConvergenceThreshold:
    """ADR-H041 — 라운드를 강제하는 것은 Critical 뿐이다.

    P2 는 Major 0건 · 신규 Minor 1건으로 다섯 라운드를 다 쓰고 에스컬레이션됐다.
    02 는 이미 Critical 만 01 로 되돌리므로 01 이 같은 문턱을 쓰는 것이 일관된다.
    """

    def test_minor_alone_converges_in_round_one(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_minor()]))
        env = _submit_review(repo, paths, _review("xv"))
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"

    def test_major_does_not_force_a_round(self, run01):
        """열린 Major 는 남되 라운드를 강제하지 않는다 — 02 와 같은 문턱."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        major = {"id": "F-1", "severity": "major", "category": "scope",
                 "title": "범위가 넓다", "quote": "빈 문자열을 먼저 거른다."}
        _submit_review(repo, paths, _review("plan", findings=[major]))
        _submit_review(repo, paths, _review("xv"))
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"
        keys = after["phases"]["01-plan"]["rounds"]["1"]["plan"]["keys"]
        assert [k["severity"] for k in keys] == ["major"], "지적은 사라지지 않는다"

    def test_critical_forces_a_second_round(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        env = _submit_review(repo, paths, _review("xv"))
        assert env["exit"] == 0
        assert env["data"]["round"] == 2, env["data"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") != "passed"

    def test_new_minor_in_round_two_does_not_block(self, run01):
        """P2 의 모양 — Critical 을 닫았는데 제목이 다른 Minor 가 새로 나왔다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        _submit_review(repo, paths, _review("xv"))
        env = _submit_review(
            repo, paths,
            _review("plan", round_=2, findings=[_minor(title="전혀 다른 제목")],
                    resolved=[{"id": "F-1", "resolved_by": "범위 절을 고쳤다"}]),
            round_=2)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"

    def test_reraised_critical_is_not_new_but_still_open(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        _submit_review(repo, paths, _review("xv"))
        again = dict(_crit(title="여전히 어긋난다"), reraised_from_previous="F-1")
        env = _submit_review(repo, paths,
                             _review("plan", round_=2, findings=[again]),
                             round_=2)
        assert env["exit"] == 0, env["render"]
        assert "신규" not in env["data"]["reason"], env["data"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") != "passed"

    def test_identical_critical_two_rounds_escalates_before_the_cap(self, run01):
        """`stuck_after_identical: 2` 를 01 도 읽는다 — 상한까지 태우지 않는다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        _submit_review(repo, paths, _review("xv"))
        env = _submit_review(repo, paths,
                             _review("plan", round_=2, findings=[_crit()]),
                             round_=2)
        _, after = st.load(repo, paths.run_id)
        assert after.get("escalated"), env["render"]
        assert "반복" in after["escalation"]["reason"], after["escalation"]
        assert after["counters"]["round"]["used"] < 3, after["counters"]["round"]

    def test_stuck_after_identical_is_read_from_01(self, run01):
        """변이 테스트 — 값을 3 으로 올리면 2라운드 반복은 아직 정체가 아니다."""
        repo, paths, s = run01
        _rewrite(_phase_file(repo, "01-plan.md"),
                 lambda f: f["loop"].__setitem__("stuck_after_identical", 3))
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        _submit_review(repo, paths, _review("xv"))
        _submit_review(repo, paths, _review("plan", round_=2, findings=[_crit()]),
                       round_=2)
        _, after = st.load(repo, paths.run_id)
        assert not after.get("escalated"), after.get("escalation")

    def test_second_round_names_only_the_reviewer_with_open_critical(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        env = _submit_review(repo, paths, _review("xv"))
        assert env["data"]["planned"] == ["plan"], env["data"]
        assert "--reviewer plan" in env["next_command"], env["next_command"]
        assert "xv" not in env["next_command"], env["next_command"]
        _, mid = st.load(repo, paths.run_id)
        assert mid["phases"]["01-plan"]["rounds_planned"]["2"] == ["plan"]

    def test_delta_reviewer_alone_closes_the_round(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        _submit_review(repo, paths, _review("xv"))
        env = _submit_review(
            repo, paths,
            _review("plan", round_=2,
                    resolved=[{"id": "F-1", "resolved_by": "고쳤다"}]),
            round_=2)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"
        assert "xv" not in after["phases"]["01-plan"]["rounds"]["2"]

    def test_both_reviewers_with_critical_are_both_planned(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        env = _submit_review(repo, paths,
                             _review("xv", findings=[_crit(title="xv 의 지적")]))
        assert sorted(env["data"]["planned"]) == ["plan", "xv"], env["data"]

    def test_a_second_round_counts_the_model_calls_it_instructs(self, run01):
        """01 의 루프는 record → record 라 `next` 의 계수를 지나치지 않았다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        before = st.load(repo, paths.run_id)[1]["budget"]["model_calls"]["total"]
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        _submit_review(repo, paths, _review("xv"))
        _, after = st.load(repo, paths.run_id)
        assert after["budget"]["model_calls"]["total"] == before + 1, \
            after["budget"]["model_calls"]
        assert "01:r1:plan" in after["budget"]["model_calls"]["counted"]

    def test_an_exhausted_budget_stops_the_second_round(self, run01):
        repo, paths, s = run01
        s["budget"]["model_calls"]["max"] = 1
        st.save(paths, s)
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        env = _submit_review(repo, paths, _review("xv"))
        assert env["exit"] == 5, env["render"]

    def test_the_normal_cap_is_three(self, run01):
        """미검증 상속값 5 → 3 (ADR-H041). 값의 회귀 방지다."""
        repo, paths, s = run01
        front = _front(_phase_file(repo, "01-plan.md"))
        assert front["converge"]["max_by_profile"] == {"small": 2, "normal": 3}
        assert front["loop"]["max_by_profile"] == front["converge"]["max_by_profile"]

    def test_missing_blocking_severities_is_exit_2(self, run01):
        repo, paths, s = run01
        _rewrite(_phase_file(repo, "01-plan.md"),
                 lambda f: f["converge"].pop("blocking_severities"))
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        env = _submit_review(repo, paths, _review("xv"))
        assert env["exit"] == 2, env["render"]
        assert "blocking_severities" in env["data"]["key"], env["data"]

    def test_lint_rejects_blocking_severities_out_of_vocab(self, repo, phases):
        _rewrite(phases / "01-plan.md",
                 lambda f: f["converge"].__setitem__("blocking_severities",
                                                     ["blocker"]))
        assert _fails(_lint(repo), "blocking_severities"), _lint(repo)

    def test_lint_rejects_missing_blocking_severities(self, repo, phases):
        _rewrite(phases / "01-plan.md",
                 lambda f: f["converge"].pop("blocking_severities"))
        assert _fails(_lint(repo), "blocking_severities"), _lint(repo)

    def test_blocking_severities_is_read_not_hardcoded(self, run01):
        """변이 테스트 — major 를 차단에 넣으면 major 가 다시 라운드를 강제한다."""
        repo, paths, s = run01
        _rewrite(_phase_file(repo, "01-plan.md"),
                 lambda f: f["converge"].__setitem__("blocking_severities",
                                                     ["critical", "major"]))
        _submit_plan(repo, paths, _plan())
        major = {"id": "F-1", "severity": "major", "category": "scope",
                 "title": "범위가 넓다", "quote": "빈 문자열을 먼저 거른다."}
        _submit_review(repo, paths, _review("plan", findings=[major]))
        _submit_review(repo, paths, _review("xv"))
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") != "passed"


class TestCrossVerifySkipWhenUnedited:
    """ADR-H042 — 1라운드 수렴은 편집이 없었다는 뜻이고, 그때 xv 가 본 것이
    곧 전문이다. 02 의 존재 이유(부분 편집의 모순)가 성립하지 않는다."""

    def _converge_r1(self, repo, paths):
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan"))
        return _submit_review(repo, paths, _review("xv"))

    def test_round_one_convergence_skips_02_without_demotion(self, run01):
        repo, paths, s = run01
        env = self._converge_r1(repo, paths)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "02-cross-verify") == "skipped"
        assert after["cross_verify"]["skip_reason"] == "plan_unedited"
        assert after["phase"] == "03-implement", after["phase"]
        assert after.get("grade") in (None, "PASS"), after.get("grade")
        assert "cross_verify_unavailable" not in (after.get("gaps") or [])

    def test_the_skip_does_not_count_an_xv_call(self, run01):
        repo, paths, s = run01
        self._converge_r1(repo, paths)
        _, after = st.load(repo, paths.run_id)
        assert not [k for k in after["budget"]["model_calls"]["counted"]
                    if k.startswith("02:")], after["budget"]["model_calls"]

    def test_round_two_convergence_runs_02(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        _submit_review(repo, paths, _review("xv"))
        env = _submit_review(
            repo, paths,
            _review("plan", round_=2,
                    resolved=[{"id": "F-1", "resolved_by": "고쳤다"}]),
            round_=2)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["phase"] == "02-cross-verify", after["phase"]
        assert st.phase_status(after, "02-cross-verify") == "running"

    def test_next_on_a_skippable_02_also_skips(self, run01):
        """세션 복구로 `next` 가 02 에 서 있어도 같은 판단이다."""
        repo, paths, s = run01
        self._converge_r1(repo, paths)
        _, mid = st.load(repo, paths.run_id)
        mid["phase"] = "02-cross-verify"
        st.set_phase_status(mid, "02-cross-verify", "running")
        st.save(paths, mid)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["phase"] == "03-implement", after["phase"]

    def test_lint_rejects_an_unparseable_skip_condition(self, repo, phases):
        _rewrite(phases / "02-cross-verify.md",
                 lambda f: f["skip_policy"][1].__setitem__("when", "1 == 1"))
        assert _fails(_lint(repo), "skip_policy"), _lint(repo)

    def test_the_report_names_the_skip_reason(self, run01):
        repo, paths, s = run01
        self._converge_r1(repo, paths)
        _, after = st.load(repo, paths.run_id)
        text, _missing = rep_mod.build(after, {}, {}, [])
        assert "plan_unedited" in text, text


class TestMergedModeCountsOnce:
    """05 `merged` 는 한 에이전트다 — 계수도 하나다. 제출은 M37 대로 갈라진다."""

    def _node(self, repo, request_file, phases, mode):
        run_id, paths = _enter_05(repo, request_file, phases)
        paths, s = st.load(repo, run_id)
        node = s["phases"].setdefault("05-code-review", {})
        node["planned"] = ["arch", "test"]
        node["mode"] = mode
        return s, cli.build_context(repo, paths, s)

    def test_merged_counts_one_instruction(self, repo, request_file, phases):
        s, ctx = self._node(repo, request_file, phases, "merged")
        assert cli._instruction_keys(s, "05-code-review", ctx) == ["05:r1:merged"]

    def test_fanout_counts_each_reviewer(self, repo, request_file, phases):
        s, ctx = self._node(repo, request_file, phases, "fanout")
        assert cli._instruction_keys(s, "05-code-review", ctx) == \
            ["05:r1:arch", "05:r1:test"]


class TestPromoteJudgementIsCounted:

    def test_a_scan_with_candidates_counts_the_judgement(self, repo, request_file,
                                                          phases):
        _fill_ledger(repo, "인가 규칙 누락", "AUTHZ_MISSING_RULE", "critical",
                     ["r1", "r2"])
        run_id, _p = _enter_06(repo, request_file, phases)
        env = cli.run_promote(repo, scan=True, run_id=run_id)
        assert env["data"]["needs_model"] is True
        _, s = st.load(repo, run_id)
        assert "07:promote" in s["budget"]["model_calls"]["counted"]

    def test_a_scan_without_candidates_counts_nothing(self, repo, request_file,
                                                      phases):
        ldg.seed(repo)
        run_id, _p = _enter_06(repo, request_file, phases)
        cli.run_promote(repo, scan=True, run_id=run_id)
        _, s = st.load(repo, run_id)
        assert "07:promote" not in s["budget"]["model_calls"]["counted"]


class TestInlineBudgetIsEnforced:
    """`review.inline_max` 는 정의만 있고 아무도 안 읽었다 — 봉투가 정한다."""

    def test_a_huge_diff_is_passed_by_path(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "fixture")
        big = repo / "src" / "lib" / "huge.ts"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text("export const x = 1;\n" * 2000, encoding="utf-8")
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 0, env["render"]
        _, s = st.load(repo, run_id)
        node = s["phases"]["05-code-review"]
        assert node["inline"]["inline"] is False, node["inline"]
        assert "경로" in env["render"] and "인라인" in env["render"]

    def test_a_small_diff_stays_inline(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "fixture")
        small = repo / "src" / "lib" / "small.ts"
        small.parent.mkdir(parents=True, exist_ok=True)
        small.write_text("export const x = 1;\n", encoding="utf-8")
        cli.run_next(repo, run_id)
        _, s = st.load(repo, run_id)
        assert s["phases"]["05-code-review"]["inline"]["inline"] is True


# ---------------------------------------------------------------------------
# J. 00-triage — 요청을 파이프라인에 넣기 전에 레인을 정한다 (ADR-H044)
# ---------------------------------------------------------------------------

import triage as triage_mod  # noqa: E402
import review07 as rv7_mod  # noqa: E402

DOCS_REQUEST = REQUEST_TEXT + "그리고 docs/PRD.md 에 그 규칙을 적어 줘.\n"
SOURCE_REQUEST = REQUEST_TEXT + "src/lib/match.ts 를 고친다.\n"


def _cfg():
    return json.loads((ROOT / "harness" / "config.json").read_text(encoding="utf-8"))


def _init(repo, text, slug="tri", profile=None):
    req = repo / "_workspace" / "requests" / ("%s.md" % slug)
    req.parent.mkdir(parents=True, exist_ok=True)
    req.write_text(text, encoding="utf-8")
    paths, s = st.create_run(repo, slug, req, profile=profile)
    return paths, s


def _submit_triage(repo, paths, payload):
    f = paths.run_dir / "00_triage.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cli.run_record(repo, phase="00", file=str(f), reviewer=None, round_=None)


class TestTriageSignals:
    """순수 함수. 언어 키워드를 쓰지 않는다 — 같은 경로면 어느 언어든 같은 판정."""

    def test_docs_path_in_korean_and_english_prose_decide_the_same(self):
        cfg = _cfg()
        ko = "docs/PRD.md 의 3절을 고쳐 줘."
        en = "Please fix section 3 of docs/PRD.md."
        a = triage_mod.decide(triage_mod.signals(ko, cfg), cfg)
        b = triage_mod.decide(triage_mod.signals(en, cfg), cfg)
        assert a["profile"] == b["profile"] == "docs", (a, b)
        assert a["decided_by"] == "machine"
        assert a["expected_paths"] == ["docs/PRD.md"]

    def test_a_korean_particle_glued_to_the_path_does_not_break_it(self):
        sig = triage_mod.signals("docs/PRD.md를 고쳐줘", _cfg())
        assert sig["paths_docs"] == ["docs/PRD.md"], sig

    def test_role_owned_paths_predict_small_under_the_thresholds(self):
        cfg = _cfg()
        got = triage_mod.decide(triage_mod.signals(SOURCE_REQUEST, cfg), cfg)
        assert got["profile"] == "small", got
        assert got["touches_source"] is True
        assert "src/lib/match.ts" in got["expected_paths"]

    def test_too_many_role_owned_paths_predict_normal(self):
        cfg = _cfg()
        text = " ".join("src/lib/f%d.ts" % i for i in range(5))
        got = triage_mod.decide(triage_mod.signals(text, cfg), cfg)
        assert got["profile"] == "normal", got

    def test_a_long_request_predicts_normal_even_with_one_path(self):
        cfg = _cfg()
        text = "src/lib/match.ts " + ("요구사항 " * 400)
        got = triage_mod.decide(triage_mod.signals(text, cfg), cfg)
        assert got["profile"] == "normal", got

    def test_no_path_at_all_is_undecided(self):
        cfg = _cfg()
        assert triage_mod.decide(triage_mod.signals(REQUEST_TEXT, cfg), cfg) is None

    def test_harness_source_is_not_docs_even_though_main_owned(self):
        """`scripts/**` 는 main_owned 이지만 하네스 소스다 — docs 로 새면 안 된다."""
        cfg = _cfg()
        sig = triage_mod.signals("scripts/pipeline/cli.py 를 고쳐 줘", cfg)
        assert sig["paths_unresolved"] == ["scripts/pipeline/cli.py"], sig
        assert triage_mod.decide(sig, cfg) is None

    def test_a_url_is_not_a_path(self):
        sig = triage_mod.signals("https://example.com/a/b 를 봐", _cfg())
        assert sig["paths_unresolved"] == [] and sig["paths_docs"] == [], sig

    def test_docs_and_unresolved_together_is_undecided(self):
        cfg = _cfg()
        sig = triage_mod.signals("docs/PRD.md 와 tools/x/y.sh", cfg)
        assert triage_mod.decide(sig, cfg) is None

    def test_missing_thresholds_raise_instead_of_falling_back(self):
        """선언이 없으면 폴백이 곧 새 하드코딩이다 (M36)."""
        cfg = _cfg()
        cfg.pop("triage")
        with pytest.raises(ValueError):
            triage_mod.decide(triage_mod.signals(SOURCE_REQUEST, cfg), cfg)

    def test_submission_vocabulary_and_substring_are_checked(self):
        cfg = _cfg()
        assert triage_mod.check_submission(
            {"profile": "huge", "expected_paths": []}, DOCS_REQUEST, cfg)
        errs = triage_mod.check_submission(
            {"profile": "small", "expected_paths": ["src/lib/other.ts"]},
            DOCS_REQUEST, cfg)
        assert any("요청 원문에 없다" in e for e in errs), errs
        assert triage_mod.check_submission(
            {"profile": "docs", "expected_paths": ["docs/PRD.md"]},
            DOCS_REQUEST, cfg) == []

    def test_docs_submission_must_stay_inside_the_docs_globs(self):
        cfg = _cfg()
        errs = triage_mod.check_submission(
            {"profile": "docs", "expected_paths": ["src/lib/match.ts"]},
            SOURCE_REQUEST, cfg)
        assert any("docs glob" in e for e in errs), errs
        errs = triage_mod.check_submission(
            {"profile": "docs", "expected_paths": []}, DOCS_REQUEST, cfg)
        assert any("최소 하나" in e for e in errs), errs


class TestPhase00Wiring:

    def test_a_docs_request_reaches_01_in_one_envelope_without_a_model(self, repo, phases):
        paths, s = _init(repo, DOCS_REQUEST)
        assert s["phase"] == "00-triage"
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 0, env["render"]
        assert env["phase"] == "01-plan", env["phase"]
        assert "리뷰어 — 0명" in env["render"], env["render"]
        assert "prescan" in env["data"]
        _, after = st.load(repo, paths.run_id)
        prof = after["profile"]
        assert prof["name"] == "docs" and prof["source"] == "triage", prof
        assert prof["predicted"]["decided_by"] == "machine"
        assert prof["signals"]["paths_docs"] == ["docs/PRD.md"]
        assert after["contract"]["mode"] == "no_contract"
        assert st.phase_status(after, "00-triage") == "passed"
        assert after["budget"]["model_calls"]["total"] == 0
        assert (paths.run_dir / "00_triage.json").exists()
        kinds = [e["kind"] for e in st.read_events(paths)]
        assert "triage_decided" in kinds, kinds

    def test_an_undecided_request_asks_one_cheap_model_call(self, repo, phases):
        paths, s = _init(repo, REQUEST_TEXT)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 0, env["render"]
        assert env["phase"] == "00-triage"
        assert "model: `haiku`" in env["render"], env["render"]
        assert "00_original_request.md" in env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["budget"]["model_calls"]["counted"] == ["00:triage"]
        assert after["models"]["instructed"]["00:triage"] == "haiku"
        assert after["phases"]["00-triage"]["needs_model"] is True

    def test_next_is_idempotent_on_00(self, repo, phases):
        paths, s = _init(repo, REQUEST_TEXT)
        cli.run_next(repo, run_id=paths.run_id)
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        assert after["budget"]["model_calls"]["total"] == 1

    def test_a_model_prediction_is_recorded_and_advances_to_01(self, repo, phases):
        paths, s = _init(repo, REQUEST_TEXT)
        cli.run_next(repo, run_id=paths.run_id)
        env = _submit_triage(repo, paths, {
            "profile": "small", "expected_paths": [], "touches_source": True,
            "reasons": ["한 함수"]})
        assert env["exit"] == 0, env["render"]
        assert env["phase"] == "01-plan"
        _, after = st.load(repo, paths.run_id)
        assert after["profile"] == dict(after["profile"], name="small",
                                        source="triage")
        assert after["profile"]["predicted"]["decided_by"] == "model"
        assert after["phases"]["00-triage"]["decided_by"] == "model"

    def test_unclear_is_exit_9_and_the_human_answer_wins(self, repo, phases):
        paths, s = _init(repo, REQUEST_TEXT)
        cli.run_next(repo, run_id=paths.run_id)
        env = _submit_triage(repo, paths, {"profile": "unclear",
                                           "expected_paths": [], "reasons": []})
        assert env["exit"] == 9, env["render"]
        assert len(env["data"]["options"]) == 3
        _, mid = st.load(repo, paths.run_id)
        assert mid["phase"] == "00-triage" and not mid.get("escalated")
        env = _submit_triage(repo, paths, {"profile": "normal", "expected_paths": [],
                                           "decided_by": "user"})
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["profile"]["source"] == "user"
        assert after["profile"]["predicted"]["decided_by"] == "user"

    def test_a_bad_submission_is_exit_8(self, repo, phases):
        paths, s = _init(repo, REQUEST_TEXT)
        cli.run_next(repo, run_id=paths.run_id)
        env = _submit_triage(repo, paths, {"profile": "docs",
                                           "expected_paths": ["docs/없는.md"]})
        assert env["exit"] == 8, env["render"]
        assert "record --phase 00" in env["next_command"]

    def test_a_user_profile_is_not_overridden_and_costs_no_model(self, repo, phases):
        paths, s = _init(repo, REQUEST_TEXT, profile="small")
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 0 and env["phase"] == "01-plan", env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["profile"]["name"] == "small"
        assert after["profile"]["source"] == "user"
        assert after["profile"]["predicted"]["decided_by"] == "user"
        assert "request_chars" in after["profile"]["signals"]
        assert after["budget"]["model_calls"]["counted"] == ["01:r0:plan", "01:r0:xv"]

    def test_a_user_docs_profile_turns_the_contract_off(self, repo, phases):
        paths, s = _init(repo, REQUEST_TEXT, profile="docs")
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        assert after["contract"]["mode"] == "no_contract"

    def test_no_model_fallback_goes_normal(self, repo, phases):
        cfg_path = repo / "harness" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["triage"]["model_call_when_undecided"] = False
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        paths, s = _init(repo, REQUEST_TEXT)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["phase"] == "01-plan", env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["profile"]["name"] == "normal"
        assert "undecided_no_model" in after["profile"]["predicted"]["reasons"]
        assert after["budget"]["model_calls"]["total"] == 2   # 01 의 리뷰어 둘

    def test_init_cli_accepts_docs(self, repo, phases, request_file):
        out = _run_cli(repo, "init", "--feature", "d", "--request-file",
                       str(request_file), "--profile", "docs")
        assert out.returncode == 0, out.stderr

    def test_the_first_next_carries_the_prescan(self, repo, phases):
        paths, s = _init(repo, REQUEST_TEXT)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert "prescan" in env["data"]


class TestDocsLane:
    """docs 레인 — 01 리뷰어 0 → 02 docs_profile → 03 역할 0, 모델 호출 0."""

    def _at_01(self, repo):
        paths, s = _init(repo, DOCS_REQUEST)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["phase"] == "01-plan", env["render"]
        return paths

    def test_plan_submission_alone_closes_01_and_skips_02_for_docs(self, repo, phases):
        paths = self._at_01(repo)
        env = _submit_plan(repo, paths, _plan())
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["phases"]["01-plan"]["converged_at_round"] == 1
        assert st.phase_status(after, "02-cross-verify") == "skipped"
        assert after["cross_verify"]["skip_reason"] == "docs_profile"
        assert after["phase"] == "03-implement", after["phase"]
        assert "역할 — 0명" in env["render"], env["render"]
        assert after["profile"]["applied"] == ["01:reviewers=0", "02:skipped"]
        assert after["budget"]["model_calls"]["total"] == 0
        assert after.get("grade") in (None, "PASS"), after.get("gaps")

    def test_a_reviewer_submission_is_rejected_when_none_was_planned(self, repo, phases):
        paths = self._at_01(repo)
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review("plan"))
        assert env["exit"] != 0, env["render"]

    def test_main_edits_docs_and_claims_no_roles(self, repo, phases, monkeypatch):
        paths = self._at_01(repo)
        _submit_plan(repo, paths, _plan())
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "PRD.md").write_text("# PRD\n", encoding="utf-8")
        monkeypatch.setattr(adapters, "run_stage",
                            lambda *a, **k: {"exit": 0, "output": ""})
        claims = paths.run_dir / "03_claims.json"
        claims.write_text('{"schema":1,"roles":[]}', encoding="utf-8")
        env = cli.run_record(repo, phase="03", file=str(claims), reviewer=None,
                             round_=None)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "03-implement") == "passed"
        assert "03:roles=0" in after["profile"]["applied"]
        assert after["budget"]["model_calls"]["total"] == 0

    def test_touching_source_in_the_docs_lane_is_a_triage_miss(self, repo, phases):
        paths = self._at_01(repo)
        _submit_plan(repo, paths, _plan())
        (repo / "src" / "lib" / "match.ts").write_text("export const x = 1\n",
                                                        encoding="utf-8")
        claims = paths.run_dir / "03_claims.json"
        claims.write_text('{"schema":1,"roles":[]}', encoding="utf-8")
        env = cli.run_record(repo, phase="03", file=str(claims), reviewer=None,
                             round_=None)
        assert env["exit"] == 3, env["render"]
        assert "next --run-id" in env["next_command"]
        _, after = st.load(repo, paths.run_id)
        prof = after["profile"]
        assert prof["name"] == "normal" and prof["previous"]["name"] == "docs"
        assert prof["triage_miss"]["at"] == "03-implement"
        assert prof["predicted"]["profile"] == "docs"      # 예측은 남는다
        assert after["contract"]["mode"] == "contract"
        assert after["grade"] == "PASS_WITH_GAPS"
        assert "triage_miss:01:reviewers=0;02:skipped;03:roles=0" in after["gaps"]
        kinds = [e["kind"] for e in st.read_events(paths)]
        assert "triage_miss" in kinds
        # 다음 `next` 는 계약을 요구한다 — 역할 패킷은 그 뒤다.
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 3, env["render"]


class TestTriageMiss:
    """예측은 03·05 의 실물에 밀린다 — 상향만 miss 다."""

    def _at_05(self, repo, paths, s, profile):
        s["profile"] = dict(profile)
        s["contract"]["sha256"] = "낡은값"
        st.set_phase_status(s, "04-gate", "passed")
        s["phase"] = "05-code-review"
        st.save(paths, s)

    def test_small_prediction_beaten_by_a_five_unit_contract_is_a_miss(self, gated, phases):
        repo, paths, s = gated
        self._at_05(repo, paths, s, {"name": "small", "source": "triage",
                                     "predicted": {"profile": "small"},
                                     "applied": ["01:max_rounds=2"]})
        (repo / "_workspace" / "contract_sim.md").write_text(
            FIVE_UNIT_CONTRACT, encoding="utf-8")
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        prof = after["profile"]
        assert prof["name"] == "normal" and prof["source"] == "auto", prof
        assert prof["triage_miss"]["was"] == "small"
        assert prof["triage_miss"]["at"] == "05-code-review"
        assert prof["predicted"]["profile"] == "small"
        assert "triage_miss:01:max_rounds=2" in after["gaps"]
        assert after["grade"] == "PASS_WITH_GAPS"

    def test_normal_prediction_beaten_downward_is_quiet(self, gated, phases):
        repo, paths, s = gated
        self._at_05(repo, paths, s, {"name": "normal", "source": "triage",
                                     "predicted": {"profile": "normal"},
                                     "applied": []})
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        prof = after["profile"]
        assert prof["name"] == "small" and prof["previous"]["name"] == "normal"
        assert "triage_miss" not in prof
        assert not any(g.startswith("triage_miss") for g in after.get("gaps") or [])

    def test_a_confirmed_prediction_keeps_its_source(self, gated, phases):
        repo, paths, s = gated
        self._at_05(repo, paths, s, {"name": "small", "source": "triage",
                                     "predicted": {"profile": "small"},
                                     "applied": ["01:max_rounds=2"]})
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        prof = after["profile"]
        assert prof["name"] == "small" and prof["source"] == "triage", prof
        assert prof["applied"] == ["01:max_rounds=2"]
        assert prof.get("confirmed_at") == "05-code-review"

    def test_07_decide_reads_the_miss_and_the_docs_lane(self):
        cfg = _cfg()
        ext = {"status": "disabled", "major": 0}
        got = rv7_mod.decide({"review05": {"status": "ok", "major": 0},
                              "profile": {"name": "normal",
                                          "triage_miss": {"was": "docs",
                                                          "became": "normal",
                                                          "at": "03-implement",
                                                          "applied": ["01:reviewers=0"]}}},
                             ext, cfg)
        assert got["effort"] == "medium" and got["skip"] is False, got
        assert got["gaps"] == []          # gap 은 miss 시점에 이미 적혔다
        got = rv7_mod.decide({"review05": {"status": "ok", "major": 0},
                              "profile": {"name": "docs"}}, ext, cfg)
        assert got["skip"] is True and got["skip_reason"] == "docs_profile", got
        # 05 결손이 docs 생략보다 앞선다 — 관측이 없었던 것이 먼저다.
        got = rv7_mod.decide({"review05": {"status": "degraded", "major": 0},
                              "profile": {"name": "docs"}}, ext, cfg)
        assert got["effort"] == "medium", got

    def test_the_report_names_the_prediction_and_the_miss(self, gated, phases):
        repo, paths, s = gated
        self._at_05(repo, paths, s, {"name": "small", "source": "triage",
                                     "predicted": {"profile": "small",
                                                   "decided_by": "machine"},
                                     "applied": ["01:max_rounds=2"]})
        s["phases"]["00-triage"] = {"decided_by": "machine", "profile": "small",
                                    "signals": {"paths_role_owned": ["a"],
                                                "paths_docs": [],
                                                "paths_unresolved": [],
                                                "request_chars": 10}}
        st.save(paths, s)
        (repo / "_workspace" / "contract_sim.md").write_text(
            FIVE_UNIT_CONTRACT, encoding="utf-8")
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        text, _missing = rep_mod.build(after, {}, {}, [])
        line = next(l for l in text.splitlines() if l.startswith("| 프로파일"))
        assert "빗나감" in line and "00 예측 small" in line, line
        assert "| 00 트리아지" in text and "machine → small" in text, text
        assert "triage_miss" in text
        assert "| 트리아지 적용 양보" in text


class TestModelTierRouting:
    """등급은 봉투가 정한다 — 지시이지 실측이 아니다."""

    def test_slot_and_profile_fallback(self):
        cfg = _cfg()
        assert cli._model_for(cfg, "01:r0:plan", "normal") == "sonnet"
        assert cli._model_for(cfg, "01:r0:xv", "normal") == "inherit"
        assert cli._model_for(cfg, "03:r0:impl", "small") == "sonnet"
        assert cli._model_for(cfg, "03:r0:impl", "normal") == "inherit"
        assert cli._model_for(cfg, "04:r1:impl", "small") == "sonnet"
        assert cli._model_for(cfg, "05:r1:gen", "normal") == "inherit"
        assert cli._model_for(cfg, "05:r1:gen", "small") == "sonnet"
        assert cli._model_for(cfg, "00:triage", "normal") == "haiku"
        assert cli._model_for(cfg, "07:code-review", "normal") is None
        cfg.pop("models")
        assert cli._model_for(cfg, "01:r0:plan", "normal") is None

    def test_the_01_packet_names_the_tiers_and_state_records_them(self, repo, phases):
        paths, s = _init(repo, SOURCE_REQUEST)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["phase"] == "01-plan", env["render"]
        assert "## 모델 등급" in env["render"]
        assert "`01:r0:plan` → model: `sonnet`" in env["render"], env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["models"]["instructed"] == {"01:r0:plan": "sonnet",
                                                 "01:r0:xv": "inherit"}
        assert after["models"]["basis"] == "instructed"
        assert after["models"]["blind_spots"]

    def test_without_a_models_block_the_packet_says_so(self, repo, phases):
        cfg_path = repo / "harness" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.pop("models")
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        paths, s = _init(repo, SOURCE_REQUEST)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert "`config.models` 가 없다" in env["render"], env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["models"]["instructed"]["01:r0:plan"] is None

    def test_the_report_names_the_instructed_tiers(self, repo, phases):
        paths, s = _init(repo, SOURCE_REQUEST)
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        text, _missing = rep_mod.build(after, {}, {}, [])
        assert "| 지시된 모델 등급" in text and "sonnet: 1" in text, text

    def test_lint_warns_when_an_agent_file_pins_a_model(self, repo, phases):
        (repo / ".claude" / "agents" / "impl-writer.md").write_text(
            "---\nname: impl-writer\nmodel: opus\n---\n# x\n", encoding="utf-8")
        got = [f for f in _lint(repo) if f["rule"] == "agent_model"]
        assert got and got[0]["status"] == "WARN", got
        assert _fails(_lint(repo)) == []


class TestSkipPolicy:

    def test_the_old_key_is_rejected(self, repo, phases):
        _rewrite(phases / "02-cross-verify.md",
                 lambda f: f.__setitem__("skip_unedited", f.pop("skip_policy")[1]))
        assert _fails(_lint(repo), "front_keys"), _lint(repo)

    def test_a_skip_without_a_reason_is_rejected(self, repo, phases):
        _rewrite(phases / "02-cross-verify.md",
                 lambda f: f["skip_policy"][0].pop("reason"))
        assert _fails(_lint(repo), "skip_policy"), _lint(repo)

    def test_an_unparseable_unless_is_rejected(self, repo, phases):
        _rewrite(phases / "01-plan.md",
                 lambda f: f["review"].__setitem__("unless", "profile is docs"))
        assert _fails(_lint(repo), "review_unless"), _lint(repo)
        _rewrite(phases / "03-implement.md",
                 lambda f: f["allow"].__setitem__("unless", "x"))
        assert _fails(_lint(repo), "allow_unless"), _lint(repo)

    def test_index_zero_lints_clean_and_sorts_first(self, repo, phases):
        assert _fails(_lint(repo)) == []
        loaded, _ = cli.load_phases(repo)
        assert sorted(loaded)[0] == "00-triage"
        assert loaded["00-triage"]["front"]["index"] == 0
