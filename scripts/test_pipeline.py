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
import os
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
import verdict as verdict_mod  # noqa: E402

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
        st.append_event(paths, "phase_enter", phase="03-implement",
                        now=self._at(10, 30))
        st.append_event(paths, "run_closed", phase="03-implement",
                        now=self._at(11))

        t = st.phase_durations(paths)
        assert t["wall_sec"] == 3600
        assert sum(p["wall_sec"] for p in t["phases"].values()) == t["wall_sec"]

    def test_재진입한_페이즈의_두_구간이_합산된다(self, repo, request_file):
        """01 → 03 → 01 → pass. **짝 맞추기 기준이면 여기서 깨진다.**"""
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="01-plan", now=self._at(10))
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(10, 10))
        st.append_event(paths, "phase_enter", phase="03-implement",
                        now=self._at(10, 10))
        # 되돌아간 01 에 phase_enter 가 안 찍히는 것이 실물이다.
        st.append_event(paths, "submit_received", phase="01-plan",
                        now=self._at(10, 20))
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(10, 50))

        t = st.phase_durations(paths)
        assert t["phases"]["01-plan"]["wall_sec"] == 600 + 1800
        assert t["phases"]["01-plan"]["segments"] == 2
        assert t["phases"]["03-implement"]["wall_sec"] == 600

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
        (부르는 쪽의 예산이 묶는다 · M32).
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

        **오늘 실물 경로로는 도달하지 않는다** — 지급은 05 의 `severity_raised`
        한 곳에서 일어나고 그 전에 반드시 카운터를 소모해 노드가 이미
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

        P6 의 `events.jsonl` 은 `counter_inc` 0건이었다 — 여섯 호출처 중
        하나만 이벤트를 냈기 때문이다.
        """
        text = (ROOT / "scripts" / "pipeline" / "cli.py").read_text(encoding="utf-8")
        spots = [m.start() for m in re.finditer(r"st\.counter_inc\(", text)]
        assert len(spots) >= 4, "호출처를 못 찾았다 — 이 검사가 무의미해졌다"
        for i in spots:
            window = text[i:i + 320]
            assert any('"%s"' % r in window for r in st.COUNTER_REASONS), window
            assert "paths=paths" in window, window


class TestWaitingHuman:
    """[[ADR-H052]] 결정 1 — 사람 대기를 페이즈 벽시계에서 분리해 잰다.

    `728c` 는 총 2h48m 중 1h09m(41.4%) 이 05 의 사람 대기였고, `40dc` 의
    첫 페이즈 36m53s 는 exit 9 로 사람을 기다린 시간이다 —
    둘 다 "05 가 1h50m 걸렸다" 로 읽혔다. exit 10 은 `escalated → resumed` 로
    이미 잰다. exit 9 는 `waiting_human` → **다음 이벤트** 까지다.
    """

    def _at(self, h, m=0, s=0):
        return datetime(2026, 3, 1, h, m, s, tzinfo=st.TZ)

    def test_대기가_페이즈에_귀속되고_순_작업이_남는다(self, repo, request_file):
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="06-pr", now=self._at(10))
        st.append_event(paths, "waiting_human", phase="06-pr",
                        reason="approval", now=self._at(10, 5))
        st.append_event(paths, "submit_received", phase="06-pr",
                        now=self._at(10, 35))
        st.append_event(paths, "phase_pass", phase="06-pr", now=self._at(10, 40))
        st.append_event(paths, "phase_enter", phase="01-plan", now=self._at(10, 40))
        st.append_event(paths, "run_closed", phase="01-plan", now=self._at(11))
        t = st.phase_durations(paths)
        cell = t["phases"]["06-pr"]
        assert cell["human_wait_sec"] == 1800, cell
        assert cell["work_sec"] == 600, cell
        assert t["human_wait_sec"] == 1800, t
        assert "human_wait_sec" not in t["phases"]["01-plan"]

    def test_불변식_순_작업_더하기_대기는_벽시계다(self, repo, request_file):
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="01-plan", now=self._at(10))
        st.append_event(paths, "escalated", phase="01-plan", now=self._at(10, 10))
        st.append_event(paths, "resumed", phase="01-plan", now=self._at(11, 10))
        st.append_event(paths, "waiting_human", phase="01-plan",
                        reason="precheck_policy", now=self._at(11, 20))
        st.append_event(paths, "stage_done", phase="01-plan", now=self._at(11, 30))
        st.append_event(paths, "phase_pass", phase="01-plan", now=self._at(11, 40))
        t = st.phase_durations(paths)
        for name, cell in t["phases"].items():
            assert (cell["work_sec"] + cell.get("escalation_wait_sec", 0)
                    + cell.get("human_wait_sec", 0)) == cell["wall_sec"], (name, cell)

    def test_답이_안_온_대기는_길이가_없고_횟수만_센다(self, repo, request_file):
        paths, _ = st.create_run(repo, "demo", request_file)
        paths.events.write_text("", encoding="utf-8")
        st.append_event(paths, "phase_enter", phase="06-pr", now=self._at(10))
        st.append_event(paths, "waiting_human", phase="06-pr",
                        reason="approval", now=self._at(10, 5))
        t = st.phase_durations(paths)
        assert t["unanswered_waits"] == 1, t
        assert "human_wait_sec" not in t["phases"]["06-pr"], t["phases"]

    def test_사각이_이름으로_남는다(self):
        assert any("다음 이벤트" in b for b in st.PHASE_DURATION_BLIND_SPOTS)


class TestWaitingHumanEvents:
    """exit 9 를 내는 두 자리가 전부 `waiting_human` 을 남긴다."""

    def _waits(self, paths):
        return [e for e in st.read_events(paths) if e["kind"] == "waiting_human"]

    def test_precheck_정책_실패가_남긴다(self, repo, request_file):
        _branch(repo, "feat-x")
        cli.run_init(repo, "x", request_file)
        _bulk_change(repo, 40)
        env = cli.run_precheck(repo, scope="pr")
        assert env["exit"] == 9
        paths, _ = st.load(repo)
        got = self._waits(paths)
        assert got and got[-1]["data"]["reason"] == "precheck_policy", got

    def test_승인_대기가_남긴다(self, repo, request_file, phases, tmp_path):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _remote(repo, tmp_path)
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 9
        got = self._waits(paths)
        assert got and got[-1]["data"]["reason"] == "approval", got
class TestCounterExceededIsConsumed:
    """[[ADR-H048]] 결정 1 — `counter_inc` 의 `exceeded` 를 무시하는 호출부가 없다.

    `3b43` 의 `state.json` 은 `counters.xverify_return.used = 2, max = 1` 이다 —
    상한 1 인 카운터가 2 까지 올라간 채 기록됐다. 02 record 경로가 반환 3항을
    버리고 `used > max_` 를 따로 판정했기 때문이다. 판정은 한 곳(`counter_inc`)
    이 하고 호출부는 그것을 읽는다.
    """

    def test_모든_호출처가_exceeded_를_읽는다(self, repo):
        """3항 언패킹이거나, 안 읽는 이유를 같은 자리에 적은 것만 허용한다."""
        text = (ROOT / "scripts" / "pipeline" / "cli.py").read_text(encoding="utf-8")
        spots = [m.start() for m in re.finditer(r"st\.counter_inc\(", text)]
        assert len(spots) >= 4, "호출처를 못 찾았다 — 이 검사가 무의미해졌다"
        for i in spots:
            before = text[max(0, i - 160):i]
            around = text[max(0, i - 400):i + 200]
            unpacked = re.search(r"\w+, \w+, exceeded = \s*$", before)
            assert unpacked or "exceeded 무시" in around, text[i - 160:i + 120]

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

    def test_01_진입이_리뷰어_하나를_지시로_센다(self, run01):
        """01 은 이제 내부 plan-reviewer 하나만 부른다(ADR-H045) — 봉투가 그것을 지시한다."""
        repo, paths, s = run01
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        mc = after["budget"]["model_calls"]
        assert mc["total"] == 1
        assert mc["by_phase"] == {"01-plan": 1}

    def test_같은_지시를_두_번_세지_않는다(self, run01):
        """`next` 는 같은 페이즈에서 여러 번 불린다 — 왕복 횟수를 세면 안 된다."""
        repo, paths, s = run01
        cli.run_next(repo, run_id=paths.run_id)
        cli.run_next(repo, run_id=paths.run_id)
        _, after = st.load(repo, paths.run_id)
        assert after["budget"]["model_calls"]["total"] == 1

    def test_제출은_세지_않는다(self, run01):
        """제출 기준은 두 방향으로 틀렸다 (M26) — 이제 지시만 센다.

        01 이 리뷰어 하나로 줄어(ADR-H045) 그 리뷰 제출은 항상 라운드를
        완결시켜 다음 지시(2라운드 속행 또는 02 로의 전이)를 낸다 — 그 지시가
        세지는 것이지 제출 자체가 세지는 게 아니다. 순수하게 "제출은 안
        세진다"는 것은 지시를 유발하지 않는 계약 제출(`_submit_plan`)로
        확인한다.
        """
        repo, paths, s = run01
        before = st.load(repo, paths.run_id)[1]["budget"]["model_calls"]["total"]
        _submit_plan(repo, paths, _plan())
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
        s["budget"]["model_calls"]["max"] = 1
        st.save(paths, s)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 5, env["render"]
        assert "예산" in env["render"]

    def test_the_packet_header_names_what_it_counts(self, run01):
        """무엇을 세는지 이름으로 말한다 — "근사" 는 그것을 말하지 못한다."""
        repo, paths, s = run01
        env = cli.run_next(repo, run_id=paths.run_id)
        assert "모델 호출 1/24" in env["render"]
        assert "지시 기준" in env["render"]


# ---------------------------------------------------------------------------
# B. lint-phases — 페이즈 파일이 깨진 채로 /feature 가 시작하지 않는다
# ---------------------------------------------------------------------------

PHASE_IDS = ["01-plan", "03-implement", "04-gate",
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
    for role in ("impl-writer", "test-writer", "ui-writer"):
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

    def test_requires_state_pointer_must_name_a_real_phase(self, repo, phases):
        """`phases.<id>.status` 가 없는 페이즈를 가리키면 `check_requires` 가 전이를
        **조용히 보류**한다 — 페이즈를 지울 때 lint 가 먼저 문다."""
        _rewrite(phases / "08-report.md",
                 lambda f: f["requires"].__setitem__(
                     0, dict(f["requires"][0], pointer="phases.99-nope.status")))
        assert _fails(_lint(repo), "requires_phase")

    def test_requires_file_nobody_writes_fails(self, repo, phases):
        """어느 페이즈의 produces 에도 본문에도 실행기 목록에도 없는 파일을 요구하면
        FAIL — `08_instruction_review.json` 이 그렇게 고아가 된 적이 있다."""
        _rewrite(phases / "07-pr-review.md",
                 lambda f: f["requires"].append(
                     {"kind": "file", "path": "${run.dir}/nobody_writes_me.json",
                      "min_bytes": 1}))
        assert _fails(_lint(repo), "requires_file")

    def test_requires_file_written_by_init_passes(self, repo, phases):
        """`00_original_request.md` 는 `init` 이 쓴다 — produces 에 없어도 통과다."""
        assert _fails(_lint(repo), "requires_file") == []
        assert _fails(_lint(repo), "requires_phase") == []

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
        _rewrite(phases / "03-implement.md",
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
        p = phases / "03-implement.md"
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
        _rewrite(phases / "03-implement.md",
                 lambda f: f["requires"].append({"kind": "vibes"}))
        assert _fails(_lint(repo), "requires_kind")

    def test_unknown_produces_kind(self, repo, phases):
        _rewrite(phases / "03-implement.md",
                 lambda f: f["produces"][0].__setitem__("kind", "yaml"))
        assert _fails(_lint(repo), "produces_kind")

    def test_id_must_match_filename(self, repo, phases):
        _rewrite(phases / "03-implement.md", lambda f: f.__setitem__("id", "03-other"))
        assert _fails(_lint(repo), "id")

    def test_duplicate_index(self, repo, phases):
        _rewrite(phases / "03-implement.md", lambda f: f.__setitem__("index", 1))
        assert _fails(_lint(repo), "index")

    def test_unknown_loop_counter(self, repo, phases):
        _rewrite(phases / "04-gate.md",
                 lambda f: f["loop"].__setitem__("counter", "made_up"))
        assert _fails(_lint(repo), "counter")

    def test_duplicate_produces_key(self, repo, phases):
        _rewrite(phases / "04-gate.md",
                 lambda f: f["produces"][1].__setitem__("key", "gate_report"))
        assert _fails(_lint(repo), "produces_key")

    def test_cli_lint_phases_exits_two_on_failure(self, repo, phases):
        (phases / "01-plan.md").write_text("# 깨짐\n", encoding="utf-8")
        out = _run_cli(repo, "lint-phases")
        assert out.returncode == 2
        assert json.loads(out.stdout)["exit"] == 2

    def test_an_unknown_produces_key_fails(self, repo, phases):
        """`produces[]` 의 키 집합도 닫혀 있다 — `FRONT_KEYS` 와 대칭."""
        _rewrite(phases / "03-implement.md",
                 lambda f: f["produces"][0].update({"schema": "contract"}))
        assert _fails(_lint(repo), "produces_keys")

    def test_no_shipped_phase_declares_a_produces_schema(self, repo, phases):
        """ADR-H073 이 걷어냈다 — 읽는 코드도 그 이름의 아티팩트도 없었다.

        되돌아오면 이 테스트가 문다. `test_an_unknown_produces_key_fails` 는
        lint 가 막는가를 묻고, 이것은 **실물이 실제로 비었는가**를 묻는다.
        """
        left = [(p.name, pr) for p in sorted(phases.glob("*.md"))
                for pr in (_front(p).get("produces") or []) if "schema" in pr]
        assert left == [], left


class TestProfileCapsHaveNoFallback:
    """ADR-H073 (백로그 28 곁가지) — 폴백이 곧 새 하드코딩이다 (ADR-H025).

    `DEFAULT_CAPS` 와 `config.json` 이 어긋나 있었고, **config 가 이겨서
    무해했기 때문에** 아무도 눈치채지 못했다. M36 의 모양 그대로다.
    """

    def test_빠진_레인은_기동_전에_잡힌다(self, repo):
        cfg = harness._read_json(repo / harness.CONFIG_REL)
        del cfg["review"]["profile_caps"]["docs"]
        assert any("profile_caps" in e for e in rv.validate(repo, cfg))

    def test_선언이_없으면_기본값으로_낙하하지_않는다(self, repo):
        cfg = harness._read_json(repo / harness.CONFIG_REL)
        cfg["review"]["profile_caps"] = {}
        with pytest.raises(ValueError):
            rv.route(cfg, ["src/lib/x.ts"], "normal")

    def test_선언을_바꾸면_절단이_따라_바뀐다(self, repo):
        """**변이 테스트** — 「읽는지」만 보는 단언은 폴백을 되살려도 초록이다."""
        cfg = harness._read_json(repo / harness.CONFIG_REL)
        paths = ["src/lib/schemas.ts", "src/app/api/x/route.ts",
                 "src/lib/match.test.ts"]
        cfg["review"]["profile_caps"]["normal"] = 1
        one = rv.route(cfg, paths, "normal")
        cfg["review"]["profile_caps"]["normal"] = 3
        three = rv.route(cfg, paths, "normal")
        assert len(one["reviewers"]) == 1 and len(three["reviewers"]) == 3, (one, three)

    def test_스키마가_세_레인을_전부_요구한다(self, repo):
        schema = harness._read_json(repo / harness.CONFIG_SCHEMA_REL)
        caps = schema["properties"]["review"]["properties"]["profile_caps"]
        assert sorted(caps["required"]) == ["docs", "fix", "normal"]


class TestSubmitCheckDeclarationsAreRead:
    """ADR-H073 — `submit_checks[].id` 가 실재하는 구현을 가리킨다.

    **ADR-H025 의 함정이 여기서도 그대로다** (M36): 지금 동작이 선언값과
    우연히 일치하므로 "레지스트리가 있다" 만 보는 단언은 **되돌려도 초록이다.**
    여기 있는 것은 전부 **선언을 바꾸고 lint 가 무는가**를 묻는 변이 테스트이고,
    마지막 하나는 **레지스트리의 종료 코드를 실제 런의 exit 와 대조한다** —
    그것이 없으면 레지스트리 자신이 두 번째 하드코딩이 된다.
    """

    def test_every_shipped_declaration_is_in_the_registry(self, repo, phases):
        """실물 8개가 선언한 id 전부가 어휘 안에 있다."""
        unknown = [(p.name, c["id"])
                   for p in sorted(phases.glob("*.md"))
                   for c in (_front(p).get("submit_checks") or [])
                   if c["id"] not in cli.SUBMIT_CHECKS]
        assert unknown == [], unknown

    def test_every_registry_impl_resolves(self):
        """**반-M36 의 핵심.** 레지스트리가 기계의 소재를 거짓말할 수 없다.

        호출하지는 않는다 — 해석만 한다. 구현 함수의 이름이 바뀌거나 사라지면
        여기서 문다. 「그 선언이 그 런에서 실제로 돌았다」는 여전히 증명하지
        않는다 (미구현 백로그 31).
        """
        for cid, spec in cli.SUBMIT_CHECKS.items():
            assert spec["impl"], cid
            for ptr in spec["impl"]:
                assert callable(cli._resolve_submit_impl(ptr)), (cid, ptr)

    def test_an_unknown_id_fails_lint(self, repo, phases):
        _rewrite(phases / "01-plan.md",
                 lambda f: f["submit_checks"].append(
                     {"id": "made_up_check", "on_fail": 8}))
        assert _fails(_lint(repo), "submit_check_id")

    def test_an_impl_pointing_at_nothing_fails_lint(self, repo, phases, monkeypatch):
        """레지스트리 자신이 검사 대상이다 — 가짜 포인터는 lint 를 통과 못 한다."""
        broken = dict(cli.SUBMIT_CHECKS)
        broken["pr_number_is_int"] = dict(broken["pr_number_is_int"],
                                          impl=("cli:_no_such_function",))
        monkeypatch.setattr(cli, "SUBMIT_CHECKS", broken)
        assert _fails(_lint(repo), "submit_check_impl")

    def test_on_fail_must_equal_the_registry_exit(self, repo, phases):
        """「페이즈는 4 라는데 코드는 8 을 낸다」를 잡는다 — M36 의 드리프트."""
        _rewrite(phases / "06-pr.md",
                 lambda f: f["submit_checks"].__setitem__(
                     0, dict(f["submit_checks"][0], on_fail=4)))
        assert _fails(_lint(repo), "submit_check_exit")

    def test_on_fail_outside_the_exit_table_fails_lint(self, repo, phases):
        _rewrite(phases / "06-pr.md",
                 lambda f: f["submit_checks"].__setitem__(
                     0, dict(f["submit_checks"][0], on_fail=99)))
        assert _fails(_lint(repo), "submit_check_exit")

    def test_a_registry_id_no_phase_declares_warns_but_does_not_fail(
            self, repo, phases, monkeypatch):
        """WARN 이지 FAIL 이 아니다.

        M36 이 금지한 것은 **어휘가 기계를 앞서는 것** 한 방향이다. 기계를 먼저
        만들고 선언을 나중에 다는 것은 정당한 순서라 lint 가 막지 않는다 —
        `impl` 해석이 통과한 이상 기계는 실재한다.
        """
        extra = dict(cli.SUBMIT_CHECKS)
        extra["nobody_declares_me"] = {"exit": 8, "impl": ("verdict:check_vocabulary",),
                                       "why": "테스트가 심은 항목"}
        monkeypatch.setattr(cli, "SUBMIT_CHECKS", extra)
        findings = _lint(repo)
        assert _fails(findings, "submit_check_unused") == []
        warns = [f for f in findings
                 if f["rule"] == "submit_check_unused" and f["status"] == "WARN"]
        assert warns, findings

    def test_the_registry_exit_is_the_exit_a_real_run_returns(self, run01):
        """**레지스트리가 두 번째 하드코딩이 되지 않게 하는 유일한 방어.**

        리터럴 `8` 과 비교하지 않는다 — **레지스트리 값과** 비교한다. 그래야
        `check_vocabulary` 가 바뀌면 lint 가 아니라 여기가 먼저 문다. 이것이
        없으면 레지스트리는 페이즈 파일과 **독립된 두 번째 출처**가 되고, M36 이
        이름한 결함이 한 층 위로 이사할 뿐이다.
        """
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review(reviewer="main"))
        assert env["exit"] == cli.SUBMIT_CHECKS["reviewer_not_main"]["exit"], env


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
    선택에서 빠졌다.
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

    def test_entrypoint_role_tag_is_parsed_outside_backticks(self, repo):
        """`[id]` 는 경로 파라미터라 태그가 아니다 — 태그는 백틱 밖에서만 뽑는다."""
        doc = "## 진입점\n\n- `GET /api/x/[id]` [admin] → 200\n- `POST /api/y` → 201\n"
        p = self._parse(repo, doc)
        assert [e["tags"] for e in p["entrypoints"]] == [["admin"], []]

    def test_path_segment_is_not_a_tag_without_backticks(self, repo):
        p = self._parse(repo, "## 진입점\n\n- GET /api/x/[id] [admin] → 200\n")
        assert p["entrypoints"][0]["path"] == "/api/x/[id]"
        assert p["entrypoints"][0]["tags"] == ["admin"]

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
                 "must_contain": "## 유닛"}]
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
# E. 01 판정 — 리뷰어 제출 · 수렴 · false_positive
# ---------------------------------------------------------------------------

REQUEST_TEXT = (
    "책 제목 유사도를 재는 함수를 만들어 줘. 빈 문자열은 0 을 돌려주고, "
    "외부 서비스를 새로 부르지는 마.\n")


def _plan(body=None):
    """정상 플랜 하나 — 형식은 자유이고 200바이트 이상이면 된다."""
    return body if body is not None else (
        "# 플랜\n\n## 경계값\n빈 문자열을 먼저 거른다.\n\n"
        "## 외부 경계\n순수 함수다. 아무것도 부르지 않는다.\n" + "여백 " * 60)


def _review(reviewer="plan", round_=1, findings=None, false_positive=None):
    out = {"reviewer": reviewer, "round": round_,
           "findings": findings if findings is not None else []}
    if false_positive is not None:
        out["false_positive"] = false_positive
    return out


def _raw(findings):
    """리뷰어 원문. quote 와 심각도 헤딩 개수가 json 과 맞아야 한다."""
    lines = ["# 리뷰"]
    for f in findings:
        lines.append("## %s" % f["severity"])
        lines.append(f.get("quote", ""))
    return "\n".join(lines) + "\n"


@pytest.fixture
def run01(repo, phases):
    """01 지시문까지 진행된 런."""
    req = repo / "_workspace" / "requests" / "sim.md"
    req.parent.mkdir(parents=True, exist_ok=True)
    req.write_text(REQUEST_TEXT, encoding="utf-8")
    paths, s = st.create_run(repo, "sim", req)
    st.set_phase_status(s, "01-plan", "running")
    st.save(paths, s)
    return repo, paths, s


def _submit_plan(repo, paths, text):
    p = paths.run_dir / "01_plan.md"
    p.write_text(text, encoding="utf-8")
    return cli.run_record(repo, phase="01", file=str(p), reviewer=None, round_=None)


def _submit_review(repo, paths, payload, round_=1):
    code = payload["reviewer"]
    j = paths.run_dir / ("01_review_r%d.json" % round_)
    r = paths.run_dir / (j.name.replace(".json", ".raw.md"))
    j.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    r.write_text(_raw(payload["findings"]), encoding="utf-8")
    return cli.run_record(repo, phase="01", file=str(j), reviewer=code, round_=round_)


class TestReviewConvergence:
    """01 은 내부 plan-reviewer 만 라운드마다 부른다.

    아래는 전부 `plan` 리뷰어 단독 제출로 수렴/미수렴을 판정한다. 두 리뷰어가
    동시에 있어야만 성립하던 시나리오(폴백 혼입·같은 id 를 서로 다른 리뷰어가
    씀)는 01 에 리뷰어가 하나뿐이라 더는 구성할 수 없어 뺐다.
    """

    def test_reviewer_main_is_rejected(self, run01):
        """작성자가 자기 글을 리뷰한 것은 독립 관측이 아니다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review(reviewer="main"))
        assert env["exit"] == 8

    def test_one_round_converges_when_plan_is_clean(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review("plan"))
        assert env["exit"] == 0
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"

    def test_critical_forces_a_second_round(self, run01):
        """ADR-H041 — 라운드를 강제하는 것은 Critical 뿐이다 (Major 는 아니다)."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        finding = {"id": "F-1", "severity": "critical", "category": "scope",
                   "title": "범위가 넓다", "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[finding]))
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

    # ── M21: 단조성 검사가 세 방향으로 샜다

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
        for name in ("01-plan.md",):
            body = (ROOT / "harness" / "phases" / name).read_text(encoding="utf-8")
            submit = body.split("## 제출 형식", 1)[1].split("\n## ", 1)[0]
            assert ".raw.md" in submit, name
            for sev in ("critical", "major", "minor"):
                assert sev in submit, "%s 가 %s 를 적지 않는다" % (name, sev)

    def test_convergence_keeps_the_round_record(self, run01):
        """수렴이 라운드 기록을 지우지 않는다 — 보고서와 08 이 회차별 제출을 읽는다.

        예전에는 수렴 경로가 `phases["01-plan"]["rounds"]` 에 **수렴 회차(정수)**
        를 대입해 회차별 제출 기록을 통째로 날렸다 (P3).
        """
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        assert _submit_review(repo, paths, _review("plan"))["exit"] == 0

        _, after = st.load(repo, paths.run_id)
        node = after["phases"]["01-plan"]
        assert st.phase_status(after, "01-plan") == "passed"
        assert isinstance(node["rounds"], dict), node["rounds"]
        assert set(node["rounds"]["1"]) == {"plan"}
        assert node["converged_at_round"] == 1

    def test_a_second_round_without_a_critical_closes_the_phase(self, run01):
        """단조성은 01 에서 꺼졌다 — 다음 회차에 그 Critical 이 안 나오면 닫힌 것이다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        crit = {"id": "F-1", "severity": "critical", "title": "범위",
                "quote": "범위가 넓다"}
        _submit_review(repo, paths, _review("plan", findings=[crit]))
        env = _submit_review(repo, paths, _review("plan", round_=2), round_=2)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"

    # ── false_positive — 메인이 코드 근거로 Critical 을 기각한다 (마스터 R-04)

    FP = {"id": "F-1", "reason": "요청이 가리키는 함수는 이미 빈 문자열을 거른다",
          "evidence": "harness/config.json:1"}

    def _critical(self):
        return {"id": "F-1", "severity": "critical", "title": "범위",
                "quote": "범위가 넓다"}

    def test_a_false_positive_with_evidence_does_not_block(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review(
            "plan", findings=[self._critical()], false_positive=[dict(self.FP)]))
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"
        sub = after["phases"]["01-plan"]["rounds"]["1"]["plan"]
        assert sub["blocking"] == 0 and sub["false_positive"] == [self.FP], sub
        assert sub["keys"][0]["false_positive"] is True, sub["keys"]
        text, _missing = rep_mod.build(after, {})
        assert "| 01 기각(false_positive) | 1 |" in text, text

    def test_a_false_positive_without_an_existing_path_is_rejected(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        bad = dict(self.FP, evidence="src/nowhere.ts:3")
        env = _submit_review(repo, paths, _review(
            "plan", findings=[self._critical()], false_positive=[bad]))
        assert env["exit"] == 8, env["render"]
        assert "evidence" in " ".join(env["data"]["errors"]), env["data"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") != "passed"

    def test_a_false_positive_must_name_a_finding_of_this_round(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review(
            "plan", findings=[self._critical()],
            false_positive=[dict(self.FP, id="F-404")]))
        assert env["exit"] == 8, env["render"]
        assert "findings" in " ".join(env["data"]["errors"]), env["data"]


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


class TestLoopDeclarationsAreRead:
    """M36 — 선언만 있고 코드가 안 읽는 설정을 잡는다.

    **지금 동작이 선언값과 우연히 일치했다.** 그래서 "읽는지" 만 보는 단언은
    되돌려도 초록이다. 여기 있는 것은 전부 **값을 바꾸는 변이 테스트**다 —
    선언을 고치면 동작이 따라 바뀌는가를 묻는다. 02 가 사라진 뒤 01 의
    라운드 루프가 유일한 `record → record` 루프다.
    """

    CRITICAL = {"id": "F-1", "severity": "critical", "title": "설계를 뒤집는다",
                "quote": "빈 문자열을 먼저 거른다."}

    def _p01(self, repo):
        return repo / "harness" / "phases" / "01-plan.md"

    def _set_max(self, repo, n):
        def mutate(f):
            for key in ("converge", "loop"):
                f[key]["max_by_profile"] = dict(f[key]["max_by_profile"], normal=n)
        _rewrite(self._p01(repo), mutate)

    def _critical_round(self, repo, paths, round_=1):
        return _submit_review(
            repo, paths, _review("plan", round_=round_, findings=[dict(self.CRITICAL)]),
            round_=round_)

    def test_라운드_상한을_프론트매터에서_읽는다(self, run01):
        """상한 1 이면 첫 Critical 에서 에스컬레이션이다."""
        repo, paths, s = run01
        self._set_max(repo, 1)
        _submit_plan(repo, paths, _plan())
        env = self._critical_round(repo, paths)
        _, after = st.load(repo, paths.run_id)
        assert after.get("escalated"), env["render"]
        assert after["counters"]["round"]["max"] == 1, after["counters"]

    def test_선언값이면_두_번째_라운드를_연다(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = self._critical_round(repo, paths)
        assert env["exit"] == 0 and env["data"]["round"] == 2, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert not after.get("escalated"), after.get("escalation")

    def test_카운터를_지우면_거부한다(self, run01):
        repo, paths, s = run01
        _rewrite(self._p01(repo), lambda f: f["loop"].pop("counter"))
        _submit_plan(repo, paths, _plan())
        env = self._critical_round(repo, paths)
        assert env["exit"] == 2, env["render"]
        assert env["data"]["key"] == "loop.counter", env["data"]

    def test_on_exceed_어휘_밖은_런타임이_거부한다(self, run01):
        repo, paths, s = run01
        self._set_max(repo, 1)

        def mutate(f):
            f["loop"]["on_exceed"] = "continue"
            f["converge"]["on_exceed"] = "continue"
        _rewrite(self._p01(repo), mutate)
        _submit_plan(repo, paths, _plan())
        env = self._critical_round(repo, paths)
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

    def test_상한이_없으면_lint_가_거부한다(self, repo, phases):
        _rewrite(phases / "04-gate.md", lambda f: f["loop"].pop("max"))
        assert _fails(_lint(repo), "loop_max"), _lint(repo)


HEADING_ONLY_CONTRACT = """# 계약: 제목 유사도

## 스키마·데이터 변경

없음.

## 유닛

### lib/match.ts · matchTitle(a: string, b: string): number

정상: 0~1 유사도. 예외: 빈 문자열은 0. 이 절은 헤딩으로 적어서 파서가 유닛을
하나도 못 읽는 모양이다 — 파일럿 40dc 의 계약이 정확히 이랬다.

## 진입점

없음.

## 오류 어휘

- `MATCH_EMPTY` (400)
"""


def _assert_error_in_tests(repo):
    """`CONTRACT_MD` 의 오류 어휘를 픽스처 테스트가 단언한다 — 03 이 첫 런부터
    `untested_error_symbol` 을 거부하므로(ADR-H058 결정 8) 다른 검사를 묻는 테스트가
    그 거부에 걸리지 않게 한다."""
    p = repo / "src" / "lib" / "match.test.ts"
    p.write_text(p.read_text(encoding="utf-8") + "// expect(code).toBe('MATCH_EMPTY')\n",
                 encoding="utf-8")


class TestRecord03ContractUnitsZero:
    """**계약 파일이 있는데 유닛이 0 이면 03 은 받지 않는다** (ADR-H049).

    파일럿 40dc(FR-014) 의 `04_gate_report.json` 은 `contract: {units: 0,
    entrypoints: 0}` 인데 게이트를 통과했다 — `## 유닛` 을 `### ` 헤딩으로
    적어 파서(`_units`, 최상위 `- ` 불릿만)가 아무것도 못 읽었고, 그 결과
    계약에 서술된 심볼까지 `out_of_contract` 로 잡혔고 scoped 는
    `no_selector` 로 스킵됐다. 파일 크기와 절 제목만 보는 `requires` 는 이것을
    못 가른다.
    """

    def _enter_03(self, repo, request_file, contract_text):
        init = cli.run_init(repo, "x", request_file)
        run_id = init["run_id"]
        paths, s = st.load(repo, run_id)
        st.set_phase_status(s, "01-plan", "passed")
        s["phase"] = "03-implement"
        s["contract"] = {"mode": "contract", "present": True,
                         "path": "_workspace/contract_x.md"}
        st.save(paths, s)
        (paths.run_dir / "01_plan.md").write_text(
            _plan(),
            encoding="utf-8")
        c = repo / "_workspace" / "contract_x.md"
        c.parent.mkdir(parents=True, exist_ok=True)
        c.write_text(contract_text, encoding="utf-8")
        claims = paths.run_dir / "03_claims.json"
        claims.write_text(json.dumps(_claims()), encoding="utf-8")
        return run_id, paths, claims

    def test_유닛_0_계약은_exit_8_이고_형식을_알려준다(self, repo, request_file,
                                                      phases):
        run_id, paths, claims = self._enter_03(repo, request_file,
                                               HEADING_ONLY_CONTRACT)
        env = cli.run_record(repo, "03", str(claims), run_id=run_id)
        assert env["exit"] == 8, env["render"]
        assert "유닛" in env["render"] and "- `" in env["render"]
        assert env["data"]["contract"]["units"] == 0
        _p, s = st.load(repo, run_id)
        assert st.phase_status(s, "03-implement") != "passed"

    def test_정상_계약은_이_검사를_지난다(self, repo, request_file, phases,
                                          monkeypatch):
        monkeypatch.setattr(adapters, "run_stage",
                            lambda *a, **k: {"id": "compile", "state": "ran",
                                             "exit": 0, "sec": 0.1})
        run_id, paths, claims = self._enter_03(repo, request_file, CONTRACT_MD)
        _assert_error_in_tests(repo)
        claims.write_text(json.dumps(_claims(test=["src/lib/match.test.ts"],
                                             rules_read=_rules_read(repo))),
                          encoding="utf-8")
        env = cli.run_record(repo, "03", str(claims), run_id=run_id)
        assert env["exit"] != 8, env["render"]


class TestRecord03RulesRead:
    """[[ADR-H055]] — 워커의 규칙 읽기를 게이트가 묻는다.

    `CLAUDE.md` 는 자동 주입되지 않고(ADR-H037) 03 의 「읽을 곳」이 가리키기만
    한다. 파일럿 15런에서 역할 에이전트가 규칙 파일을 열었는지는 어디에도
    기록이 없다. 제출의 `rules_read: [{path, sha256}]` 를 현재 해시와 대조한다 —
    누락·불일치는 exit 8. **해시 일치는 "읽었다" 의 증명이 아니다.** 그러나
    "열어 보지도 않고 지켰다고 보고" 는 막힌다.
    """

    def _enter(self, repo, request_file, monkeypatch):
        monkeypatch.setattr(adapters, "run_stage",
                            lambda *a, **k: {"id": "compile", "state": "ran",
                                             "exit": 0, "sec": 0.1})
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "ARCHITECTURE.md").write_text("# 구조\n", encoding="utf-8")
        (repo / "docs" / "harness").mkdir(exist_ok=True)
        (repo / "docs" / "harness" / "DECISIONS.md").write_text("# 하위 — 대상 아님\n",
                                                                encoding="utf-8")
        return TestRecord03ContractUnitsZero()._enter_03(repo, request_file,
                                                        CONTRACT_MD)

    def _submit(self, repo, run_id, claims, payload):
        claims.write_text(json.dumps(payload), encoding="utf-8")
        return cli.run_record(repo, "03", str(claims), run_id=run_id)

    def test_기대_목록은_지시_파일과_rules_dir_직속_md_다(self, repo):
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "PRD.md").write_text("# PRD\n", encoding="utf-8")
        (repo / "docs" / "harness").mkdir(exist_ok=True)
        (repo / "docs" / "harness" / "DECISIONS.md").write_text("x", encoding="utf-8")
        config = harness._read_json(repo / harness.CONFIG_REL)
        got = cli._rules_read_expected(repo, config)
        assert set(got) == {"CLAUDE.md", "docs/PRD.md"}, got
        assert got["CLAUDE.md"] == st._sha256_file(repo / "CLAUDE.md")

    def test_없으면_exit_8_이고_경로만_알려준다(self, repo, request_file, phases, monkeypatch):
        run_id, paths, claims = self._enter(repo, request_file, monkeypatch)
        env = self._submit(repo, run_id, claims, _claims())
        assert env["exit"] == 8, env["render"]
        assert "rules_read" in env["render"]
        assert "CLAUDE.md" in env["render"] and "docs/ARCHITECTURE.md" in env["render"]
        assert "docs/harness/DECISIONS.md" not in env["render"], "직속만이다"
        sha = st._sha256_file(repo / "CLAUDE.md")
        assert sha not in env["render"], "봉투가 답을 주면 안 열고도 맞춘다"
        _p, s = st.load(repo, run_id)
        assert st.phase_status(s, "03-implement") != "passed"
        assert env["next_command"] and "record --phase 03" in env["next_command"]

    def test_해시가_다르면_exit_8(self, repo, request_file, phases, monkeypatch):
        run_id, paths, claims = self._enter(repo, request_file, monkeypatch)
        rr = _rules_read(repo)
        rr[0]["sha256"] = "0" * 64
        env = self._submit(repo, run_id, claims, _claims(rules_read=rr))
        assert env["exit"] == 8, env["render"]
        assert "불일치" in env["render"], env["render"]

    def test_한_역할만_빠져도_exit_8(self, repo, request_file, phases, monkeypatch):
        run_id, paths, claims = self._enter(repo, request_file, monkeypatch)
        payload = _claims(rules_read=_rules_read(repo))
        payload["roles"][1].pop("rules_read")
        env = self._submit(repo, run_id, claims, payload)
        assert env["exit"] == 8, env["render"]
        assert "test" in env["render"]

    def test_일치하면_지난다(self, repo, request_file, phases, monkeypatch):
        run_id, paths, claims = self._enter(repo, request_file, monkeypatch)
        _assert_error_in_tests(repo)
        env = self._submit(repo, run_id, claims, _claims(
            test=["src/lib/match.test.ts"], rules_read=_rules_read(repo)))
        assert env["exit"] != 8, env["render"]
        _p, s = st.load(repo, run_id)
        assert st.phase_status(s, "03-implement") == "passed"

    def test_런_중에_규칙이_바뀌면_재제출이다(self, repo, request_file, phases, monkeypatch):
        """바뀐 규칙을 안 본 제출이다 — 그것이 의도다 (ADR-H055)."""
        run_id, paths, claims = self._enter(repo, request_file, monkeypatch)
        rr = _rules_read(repo)
        (repo / "CLAUDE.md").write_text("# 가드레일\n\n- 새 규칙\n", encoding="utf-8")
        env = self._submit(repo, run_id, claims, _claims(rules_read=rr))
        assert env["exit"] == 8, env["render"]
        assert "CLAUDE.md" in env["render"]

    def test_docs_레인의_역할_0명은_대상이_아니다(self, repo, request_file, phases,
                                                monkeypatch):
        run_id, paths, claims = self._enter(repo, request_file, monkeypatch)
        env = self._submit(repo, run_id, claims, {"schema": 1, "roles": []})
        assert "rules_read" not in env["render"], env["render"]

    def test_거부가_원장에_남는다(self, repo, request_file, phases, monkeypatch):
        run_id, paths, claims = self._enter(repo, request_file, monkeypatch)
        self._submit(repo, run_id, claims, _claims())
        got = [e for e in st.read_events(paths)
               if e["kind"] == "check_fail" and e["data"].get("rules_read")]
        assert got, "무엇이 빠졌는지가 원장에 있어야 한다"

    def test_하네스_자신이_쓰는_파일은_규칙_집합에_없다(self, repo):
        """미구현 백로그 23 — `docs/PIPELINE-LOG.md` 는 `/log` 가 쓰는 파일이다.

        `rules_dir` 직속 `*.md` 를 통째로 규칙으로 보면 **하네스 자신의 쓰기**가
        모든 역할의 증명을 무효로 만든다. 제외 목록은 config 가 정한다 —
        클론이 자기 파일을 더할 수 있어야 하기 때문이다.
        """
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "PRD.md").write_text("# PRD\n", encoding="utf-8")
        (repo / "docs" / "PIPELINE-LOG.md").write_text("# 로그\n", encoding="utf-8")
        config = harness._read_json(repo / harness.CONFIG_REL)
        assert "docs/PIPELINE-LOG.md" in (config["project"]["rules_exclude"]), \
            config["project"]
        got = cli._rules_read_expected(repo, config)
        assert set(got) == {"CLAUDE.md", "docs/PRD.md"}, got

    def test_런_중_log_가_돌아도_제출이_통과한다(self, repo, request_file, phases,
                                                monkeypatch):
        """`/log` 한 번이 모든 역할의 증명을 무효로 만들던 경로다 (백로그 23).

        실측: 클론 4런 중 2런이 `rules_read` 사유로 `format_reject`.
        """
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "PIPELINE-LOG.md").write_text("# 로그\n", encoding="utf-8")
        run_id, paths, claims = self._enter(repo, request_file, monkeypatch)
        _assert_error_in_tests(repo)
        rr = _rules_read(repo)
        (repo / "docs" / "PIPELINE-LOG.md").write_text(
            "# 로그\n\n## 5. 발생한 문제와 해결\n\n- 한 줄 승격\n", encoding="utf-8")
        env = self._submit(repo, run_id, claims, _claims(
            test=["src/lib/match.test.ts"], rules_read=rr))
        assert env["exit"] != 8, env["render"]

    def test_03_본문과_에이전트_정의가_같은_것을_말한다(self, repo):
        p03 = (ROOT / "harness" / "phases" / "03-implement.md").read_text(encoding="utf-8")
        assert "rules_read_sha" in p03 and "rules_read" in p03
        assert "증명이 아니" in p03, "한계를 적는다 (결정 2)"
        for name in ("impl-writer", "test-writer", "ui-writer"):
            text = (ROOT / ".claude" / "agents" / (name + ".md")).read_text(encoding="utf-8")
            assert "rules_read" in text and "sha256" in text, name
            assert "증명이 아니" in text, name


# 진입점 하나(태그)와 오류 어휘 하나 — 03 이 요구하는 테스트를 셋 다 만든다.
TESTS_REQUIRED_CONTRACT = """# 계약: 제목 유사도

## 스키마·데이터 변경

없음.

## 외부 경계

없음.

## 유닛

- `lib/match.ts · matchTitle(a: string, b: string): number`
  - 정상: 0~1 유사도 / 예외: 빈 문자열 → `0`

## 진입점

- `POST /api/analyze` [admin] → 200

## 오류 어휘

- `MATCH_EMPTY` (400)
"""


class TestTestsRequiredAt03:
    """**검사만 빡세지면 수리만 는다** — 요구를 03 으로 당긴다 (ADR-H058 결정 6·7).

    05 의 `contract-trace` 는 Major 를 원장에 `deferred` 로 쌓을 뿐 수리 루프를
    돌리지 않는다(루프는 리뷰어 병합 결과만 본다). 그래서 test-writer 가 목록을
    모르면 지적만 쌓이고 아무도 안 고친다. 03 패킷이 목록을 주고, 03 제출이 같은
    검사를 돌려 빠지면 첫 런부터 거부한다(결정 8 — 유예 없음).
    """

    def _enter(self, repo, request_file, monkeypatch, text=TESTS_REQUIRED_CONTRACT):
        monkeypatch.setattr(adapters, "run_stage",
                            lambda *a, **k: {"id": "compile", "state": "ran",
                                             "exit": 0, "sec": 0.1})
        return TestRecord03ContractUnitsZero()._enter_03(repo, request_file, text)

    def _route(self, repo, test_body):
        _route(repo, "analyze", test_body)

    def _submit(self, repo, run_id, claims):
        d = "src/app/api/analyze/"
        claims.write_text(json.dumps(_claims(
            impl=[d + "route.ts"], test=[d + "route.test.ts"],
            rules_read=_rules_read(repo))), encoding="utf-8")
        return cli.run_record(repo, "03", str(claims), run_id=run_id)

    # --- 패킷 ---------------------------------------------------------------

    def test_03_패킷이_게이트가_세는_목록을_준다(self, repo, request_file, phases,
                                                  monkeypatch):
        run_id, _paths, _c = self._enter(repo, request_file, monkeypatch)
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 0, env["render"]
        r = env["render"]
        assert "게이트가 세는 테스트" in r
        assert "POST /api/analyze" in r and "admin" in r
        assert "MATCH_EMPTY" in r

    def test_목록은_계약에서_나온다_태그가_없으면_거부_경로를_요구하지_않는다(
            self, repo, request_file, phases, monkeypatch):
        text = TESTS_REQUIRED_CONTRACT.replace(" [admin]", "")
        run_id, _paths, _c = self._enter(repo, request_file, monkeypatch, text)
        r = cli.run_next(repo, run_id)["render"]
        line = next(l for l in r.splitlines() if "`POST /api/analyze`" in l)
        assert "성공 경로" in line and "거부 경로" not in line, line

    # --- 제출 ---------------------------------------------------------------

    def test_첫_런부터_거부한다_유예가_없다(self, repo, request_file, phases,
                                           monkeypatch):
        """원장이 비어 있어도 거부다 (ADR-H058 결정 8). 둘 다 빠지면 둘 다 알린다."""
        run_id, _paths, claims = self._enter(repo, request_file, monkeypatch)
        self._route(repo, "expect(res.status).toBe(200)\n")
        env = self._submit(repo, run_id, claims)
        assert env["exit"] == 8, env["render"]
        _p, s = st.load(repo, run_id)
        assert st.phase_status(s, "03-implement") != "passed"
        assert "untested_error_symbol" in env["render"]
        assert "authz_untested" in env["render"]
        assert "authz_denied_pattern" in env["render"], "오탐이면 고칠 자리를 알린다"

    def test_빠진_테스트는_03_을_거부한다(self, repo, request_file, phases,
                                         monkeypatch):
        run_id, paths, claims = self._enter(repo, request_file, monkeypatch)
        self._route(repo, "expect(res.status).toBe(403)\n")
        env = self._submit(repo, run_id, claims)
        assert env["exit"] == 8, env["render"]
        assert "MATCH_EMPTY" in env["render"] and "test" in env["render"]
        assert "record --phase 03" in (env["next_command"] or "")
        _p, s = st.load(repo, run_id)
        assert st.phase_status(s, "03-implement") != "passed"
        got = [e for e in st.read_events(paths)
               if e["kind"] == "check_fail" and e["data"].get("tests_required")]
        assert got, "무엇이 빠졌는지가 이벤트에 남아야 한다"

    def test_테스트가_있으면_지난다(self, repo, request_file, phases, monkeypatch):
        run_id, _paths, claims = self._enter(repo, request_file, monkeypatch)
        self._route(repo, "expect(body.code).toBe('MATCH_EMPTY')\n"
                          "expect(res.status).toBe(403)\n")
        env = self._submit(repo, run_id, claims)
        assert env["exit"] != 8, env["render"]
        _p, s = st.load(repo, run_id)
        assert st.phase_status(s, "03-implement") == "passed"

    def test_05_와_03_이_같은_함수로_센다(self, repo):
        """두 자리가 다른 목록을 보면 03 통과가 05 지적을 예고하지 못한다."""
        config, adapter = _load(repo)
        p = _write_contract(repo, TESTS_REQUIRED_CONTRACT)
        _route(repo, "analyze")
        req = tr.required_tests(repo, config, adapter, p)
        full = tr.run(repo, config, adapter, p, changed=[])
        codes = ("untested_entrypoint", "untested_error_symbol", "authz_untested")
        assert (sorted(f["code"] for f in req["findings"])
                == sorted(f["code"] for f in full["findings"] if f["code"] in codes))


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
        # 첫 패킷은 01 이다 — 레인은 `init` 이 선언했고 예측 단계가 없다.
        assert env2["phase"] == "01-plan"
        assert "01_plan.md" in env2["render"]
        assert "record --phase 01" in (env2["next_command"] or "")

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
        env = _submit_plan(repo, paths, _plan())
        assert env["exit"] == 3


# ---------------------------------------------------------------------------
# F. clean_ownership — 소유 경계 · orphan
# ---------------------------------------------------------------------------

def _claims(impl=None, test=None, rules_read=None, ui=None):
    """`rules_read` 는 [{path, sha256}] — 없으면 안 싣는다 (ADR-H055 이전 모양).
    `ui` 는 주면(빈 목록 포함) ui 역할을 싣는다 (ADR-H057)."""
    roles = [
        {"role": "impl", "agent": "impl-writer", "status": "ok",
         "claimed_files": impl or [], "contract_symbols_implemented": []},
        {"role": "test", "agent": "test-writer", "status": "ok",
         "claimed_files": test or [], "contract_symbols_covered": []}]
    if ui is not None:
        roles.append({"role": "ui", "agent": "ui-writer", "status": "ok",
                      "claimed_files": ui, "contract_symbols_implemented": [],
                      "ui_guide_checked": []})
    if rules_read is not None:
        for r in roles:
            r["rules_read"] = list(rules_read)
    return {"schema": 1, "roles": roles}


def _rules_read(repo):
    """실물 규칙 파일의 현재 해시 — 워커가 냈어야 할 그대로."""
    config = harness._read_json(repo / harness.CONFIG_REL)
    return [{"path": p, "sha256": h}
            for p, h in sorted(cli._rules_read_expected(repo, config).items())]


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
        _config, adapter = adapters.load(repo)
        assert adapters.stage_state(adapter, "e2e") == "absent"
        called = []
        got = adapters.run_stage(repo, adapter, "e2e",
                                 runner=lambda *a: called.append(a) or (0, ""))
        assert got == {"id": "e2e", "state": "skipped", "reason": "absent"}
        assert not called, "없는 스테이지를 실행하지 않는다"
        assert "sec" not in got, "못 잰 값에 0 을 넣지 않는다"

    def test_when_touched_miss(self, repo):
        _config, adapter = adapters.load(repo)
        assert adapters.when_touched_hit(adapter, "build", ["docs/TRD.md"]) is False
        assert adapters.when_touched_hit(adapter, "build", ["src/app/page.tsx"]) is True
        assert adapters.when_touched_hit(adapter, "compile", ["x"]) is None

    def test_full_timeout_comes_from_the_adapter(self, repo):
        """어댑터 선언이 없으면 기본값이다 — 값과 출처가 같이 남는다."""
        _config, adapter = adapters.load(repo)
        assert adapter["stages"]["full"]["timeout_sec"] == 1800
        assert adapters.stage_timeout(adapter, "full") == (1800, "adapter")
        assert adapters.stage_timeout({"stages": {"full": {"cmd": ["x"]}}}, "full") == (
            adapters.DEFAULT_TIMEOUT_SEC, "default")

    def test_multi_selector_becomes_path_arguments(self, repo):
        """이 어댑터는 select 를 두지 않는다 — 선택자가 경로 필터로 붙는다."""
        _config, adapter = adapters.load(repo)
        argv = adapters.stage_argv(repo, adapter, "scoped",
                                   ["src/lib/a.test.ts", "src/lib/b.test.ts"])
        assert argv[-2:] == ["src/lib/a.test.ts", "src/lib/b.test.ts"]

    def test_parse_report_agrees_with_the_contract_layer(self, repo):
        _config, adapter = adapters.load(repo)
        _write_report(repo, tests=7, failures=2)
        mine = adapters.parse_report(repo, adapter)
        theirs = harness._parse_junit(repo, adapter)
        assert (mine["ran"], mine["suites"], mine["failures"], mine["matched"]) == theirs

    def test_infra_pattern_ignored_when_exit_is_zero(self, repo):
        _config, adapter = adapters.load(repo)
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
        _c, adapter = adapters.load(repo)
        log = ("src/lib/match.test.ts(3,10): error TS2305: "
               "Module './match' has no exported member 'matchTitle'.")
        got = attr.attribute_compile(adapter, config, {"matchTitle"}, log)
        assert got and got[0]["owner"] == config["primary_role"]
        assert "primary_role" in got[0]["owner_reason"]

    def test_plain_compile_error_uses_the_path(self, repo, config):
        _c, adapter = adapters.load(repo)
        log = "src/lib/match.ts(9,3): error TS2322: Type 'string' is not assignable."
        got = attr.attribute_compile(adapter, config, set(), log)
        assert got and got[0]["owner"] == "impl"

    def test_assertion_in_contract_is_ambiguous(self, repo, config):
        _c, adapter = adapters.load(repo)
        units = [{"unit": "matchTitle 는 0 을 돌려준다", "file": "src/lib/match.test.ts",
                  "ftype": "AssertionError", "message": "expected 1 to be 0",
                  "detail": "at src/lib/match.ts:4"}]
        got = attr.attribute_tests(adapter, config, {"matchTitle"}, units,
                                   repo_files=["src/lib/match.ts", "src/lib/match.test.ts"])
        assert got[0]["owner"] == "ambiguous"

    def test_assertion_outside_contract_goes_to_the_test_role(self, repo, config):
        _c, adapter = adapters.load(repo)
        units = [{"unit": "지어낸 심볼", "file": "src/lib/match.test.ts",
                  "ftype": "AssertionError", "message": "expected", "detail": ""}]
        got = attr.attribute_tests(adapter, config, {"matchTitle"}, units,
                                   repo_files=["src/lib/match.test.ts"])
        assert got[0]["owner"] == "test"
        assert "out_of_contract" in got[0]["owner_reason"]

    def test_frames_only_count_files_that_exist(self, repo, config):
        """스택 문법에 의존하지 않는다 — 리포에 실재하는 파일만 프레임이다."""
        _c, adapter = adapters.load(repo)
        frames = attr.frames_from(
            "at wonder (src/lib/match.ts:4)\nat nowhere (vendor/ghost.ts:9)",
            ["src/lib/match.ts", "src/lib/match.test.ts"])
        assert frames == ["src/lib/match.ts"]

    def test_ambiguous_goes_to_primary_then_flips(self, repo, config):
        """**어댑터 없는 경로다** — 테스트 파일인지 물을 수단이 없으면
        지금대로 `primary_role` 이 먼저다 ([[ADR-H072]]).
        """
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

    def test_프레임이_전부_테스트_파일이면_테스트_역할이_먼저다(self, repo,
                                                              config):
        """[[ADR-H072]] 결정 3 — `ambiguous` 는 예외가 아니라 **기본값**이다.

        스텁 픽스처가 원인인 실패를 `primary_role` 에 먼저 보내면 구현이
        스텁에 맞추려 계약에 없는 특수 분기를 프로덕션에 넣는다. 클론 1런이
        실제로 그랬고 사람이 되돌렸다 — 오배정의 대가가 「라운드 하나」가
        아니라 **프로덕션 코드 오염**이다 (백로그 27).
        """
        _c, adapter = adapters.load(repo)
        f = {"id": "F-1", "kind": "test", "owner": "ambiguous", "sig": "t1",
             "file": "src/lib/match.test.ts",
             "frames": ["src/lib/match.test.ts"]}
        got = attr.resolve_ambiguous([dict(f)], config, {}, adapter=adapter)
        assert got[0]["owner"] == "test", got[0]
        assert "테스트 파일" in got[0]["owner_reason"], got[0]

    def test_앱_프레임이_섞이면_기본_역할이_먼저다(self, repo, config):
        """면제가 아니라 **전부** 테스트 파일일 때의 규칙이다."""
        _c, adapter = adapters.load(repo)
        f = {"id": "F-1", "kind": "test", "owner": "ambiguous", "sig": "t2",
             "file": "src/lib/match.test.ts",
             "frames": ["src/lib/match.test.ts", "src/lib/match.ts"]}
        got = attr.resolve_ambiguous([dict(f)], config, {}, adapter=adapter)
        assert got[0]["owner"] == config["primary_role"], got[0]

    def test_사다리가_뒤집혀도_역할을_건너뛰지_않는다(self, repo, config):
        """[[ADR-H072]] — 배정은 순서 인덱스가 아니라 **미시도 집합**에서.

        같은 sig 가 다른 프레임으로 재발하면 사다리가 뒤집힌다 —
        `signature()` 는 프레임을 해시에 넣지 않는다. 인덱스로 고르면 r2 가
        r1 과 같은 역할을 다시 받고 `impl` 은 한 번도 안 시도된 채 계약
        결함이 된다 — [[ADR-H023]]·M33 이 막으려던 모양 그대로다.
        """
        _c, adapter = adapters.load(repo)
        flip = {}
        only_test = {"id": "F-1", "kind": "test", "owner": "ambiguous",
                     "sig": "t3", "file": "src/lib/match.test.ts",
                     "frames": ["src/lib/match.test.ts"]}
        mixed = dict(only_test,
                     frames=["src/lib/match.test.ts", "src/lib/match.ts"])
        r1 = attr.resolve_ambiguous([dict(only_test)], config, flip,
                                    adapter=adapter)
        r2 = attr.resolve_ambiguous([dict(mixed)], config, flip,
                                    adapter=adapter)
        r3 = attr.resolve_ambiguous([dict(only_test)], config, flip,
                                    adapter=adapter)
        assert r1[0]["owner"] == "test", r1[0]
        assert r2[0]["owner"] == "impl", "이미 시도한 역할을 다시 주지 않는다"
        assert r2[0].get("carry_contract") is True, r2[0]
        assert r3[0]["owner"] == "contract", "둘을 다 돌았으면 계약 결함이다"

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

    def test_a_deferred_flip_is_rolled_back(self, repo, config):
        """미룬 배정은 지시로 안 나갔다 — flip 인덱스도 정체 체인도 그것을 세면
        다음 라운드에 역할 하나를 건너뛰거나(ADR-H023 별건) 즉시 정체로 잡힌다."""
        f1 = {"id": "F-1", "owner": "test", "sig": "a", "frames": ["src/lib/match.ts"]}
        f2 = {"id": "F-2", "owner": "ambiguous", "sig": "b",
              "frames": ["src/lib/match.ts"]}
        flip = {}
        got = attr.dispatch([f1, f2], config, prev_sigs=[], flip_state=flip)
        assert got["owner"] == "test" and got["deferred"][0]["owner"] == "impl"
        assert flip["b"]["assigned"] == [], "안 나간 배정이 flip 에 남았다"
        assert "impl|b" not in got["pairs"], "안 나간 쌍이 정체 체인에 들어간다"
        again = attr.dispatch([dict(f2)], config, prev_sigs=got["pairs"],
                              flip_state=flip)
        assert again["owner"] == "impl", "같은 역할이 처음으로 시도해야 한다"
        assert again["stuck"] is False

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
    st.set_phase_status(s, "03-implement", "passed")
    s["phase"] = "04-gate"
    st.save(paths, s)
    return repo, paths, s


def _gate(repo, fixture, **kw):
    return cli.run_gate_cmd(repo, phase="04", replay=str(fixture), **kw)


def _finished_run(repo, run_id, closed_at, ran):
    """완주 런 하나를 `_workspace/runs/` 에 세운다 — 04 가 테스트 수 하한을 읽는 곳."""
    d = repo / "_workspace" / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    s = {"run_id": run_id, "run_status": "done", "closed_at": closed_at}
    if ran is not None:
        s["tests"] = {"ran": ran}
    (d / "state.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


class TestRulesFired:
    """어떤 어댑터 규칙이 **판정을 냈는가** — 불린 것과 결정한 것은 다르다.

    banana 19런의 실패 기록 21건은 전부 프레임이 테스트 파일이라
    `first_app_frame` 이 늘 `None` 이었다. 규칙은 불렸지만 아무것도 결정하지
    않았고, 그런데도 `verified` 가 올라갔다 ([[ADR-H069]]).
    """

    def test_컴파일_기록은_정규식이_매칭됐다는_증거다(self, repo, config):
        _c, adapter = adapters.load(repo)
        log = "src/lib/match.ts(9,3): error TS2322: Type 'string' is not assignable."
        got = attr.attribute_compile(adapter, config, set(), log)
        assert attr.rules_fired(adapter, got) == {"compile_error_regex"}

    def test_심볼_강제는_따로_센다(self, repo, config):
        _c, adapter = adapters.load(repo)
        log = ("src/lib/match.test.ts(3,10): error TS2305: "
               "Module './match' has no exported member 'matchTitle'.")
        got = attr.attribute_compile(adapter, config, {"matchTitle"}, log)
        assert attr.rules_fired(adapter, got) == {"compile_error_regex",
                                                  "symbol_not_found_patterns"}

    def test_앱_프레임이_잡혀야_접두와_glob_이_결정한_것이다(self, repo, config):
        """단언이 아닌 예외 + 앱 프레임 — `first_app_frame` 이 값을 내는 유일한 경로."""
        _c, adapter = adapters.load(repo)
        units = [{"unit": "u", "file": "src/lib/match.test.ts", "ftype": "TypeError",
                  "message": "boom", "detail": "at src/lib/match.ts:4"}]
        got = attr.attribute_tests(adapter, config, set(), units,
                                   repo_files=["src/lib/match.ts",
                                               "src/lib/match.test.ts"])
        assert attr.rules_fired(adapter, got) == {"app_frame_prefixes",
                                                  "test_file_globs"}

    def test_테스트_프레임만_있으면_아무것도_결정하지_않았다(self, repo, config):
        """banana 21건이 전부 이 모양이었다 — 관측 0 이어야 한다."""
        _c, adapter = adapters.load(repo)
        units = [{"unit": "u", "file": "src/lib/match.test.ts",
                  "ftype": "AssertionError", "message": "expected 1 to be 0",
                  "detail": "at src/lib/match.test.ts:9"}]
        got = attr.attribute_tests(adapter, config, set(), units,
                                   repo_files=["src/lib/match.test.ts"])
        assert attr.rules_fired(adapter, got) == set()

    def test_못_읽은_대체_기록은_규칙이_아니다(self, repo):
        _c, adapter = adapters.load(repo)
        assert attr.rules_fired(adapter, [{"kind": "stage", "frames": []}]) == set()

    def test_실패가_없으면_증거도_없다(self, repo):
        _c, adapter = adapters.load(repo)
        assert attr.rules_fired(adapter, []) == set()

    def test_어댑터가_선언하지_않은_규칙은_관측되지_않는다(self, repo):
        """선언이 없으면 그 규칙은 돌 수가 없다 — 관측에도 나오면 안 된다."""
        _c, adapter = adapters.load(repo)
        adapter["attribution"] = dict(adapter["attribution"])
        adapter["attribution"]["symbol_not_found_patterns"] = []
        got = attr.rules_fired(adapter, [{"kind": "compile", "in_contract": True}])
        assert got == {"compile_error_regex"}


class TestGateLoopStage:
    """**05 의 재게이트는 04 보다 약하지 않다** (ADR-H046).

    파일럿 e355(FR-008) 의 05 수리 라운드에서 test-writer 가 추가한 테스트에
    타입 에러가 있었는데, 재게이트 지시가 `gate --phase 04 --stage scoped`
    (vitest 만, 타입체크 없음) 라 걸러지지 않았고 PR #18 이 배포 플랫폼의
    `next build` 에서 처음 깨졌다 (파일럿 커밋 `de4760e`). `--stage` 가 단일
    스테이지만 받아 compile 을 함께 돌릴 방법이 선언에 없었다.

    - 05 의 `gate.steps` 가 `compile` 을 루프 스테이지로 선언한다
    - `--stage loop` 는 그 페이즈의 **루프 구간 전부**를 돈다 (선언이 단일
      출처다 — 지시문이 스테이지 이름을 나열하지 않는다)
    """

    def _at_05(self, gated):
        repo, paths, s = gated
        st.set_phase_status(s, "04-gate", "passed")
        s["phase"] = "05-code-review"
        st.save(paths, s)
        return repo, paths, s

    def test_05_는_compile_을_루프_스테이지로_선언한다(self):
        loaded, broken = cli.load_phases(ROOT)
        assert broken == []
        steps = loaded["05-code-review"]["front"]["gate"]["steps"]
        ids = [x["id"] for x in steps]
        assert "compile" in ids and "scoped" in ids
        assert ids.index("compile") < ids.index("scoped")
        assert steps[ids.index("compile")].get("loop_stage") is True

    def test_stage_loop_은_compile_실패를_잡는다(self, gated, fxdir):
        repo, paths, s = self._at_05(gated)
        fx = make_fixture(fxdir, "compile-fails",
                          dict(ALL_PASS, compile={"exit": 1}))
        env = cli.run_gate_cmd(repo, phase="05", only_stage="loop",
                               replay=str(fx))
        assert env["exit"] == 4, env["render"]
        ran = {x["id"]: x for x in env["data"]["stages"]}
        assert ran["compile"]["exit"] == 1
        assert "scoped" not in ran, "fail_fast — compile 이 깨지면 scoped 를 안 돈다"
        assert "compile" in env["render"]

    def test_stage_loop_전부_통과면_0_이고_둘_다_돈다(self, gated, fxdir):
        repo, paths, s = self._at_05(gated)
        fx = make_fixture(fxdir, "loop-pass", dict(ALL_PASS))
        env = cli.run_gate_cmd(repo, phase="05", only_stage="loop",
                               replay=str(fx))
        assert env["exit"] == 0, env["render"]
        assert [x["id"] for x in env["data"]["stages"]] == ["compile", "scoped"]

    def test_04_의_loop_은_루프_구간_넷이다(self, gated, fxdir):
        """`loop` 의 뜻은 페이즈 선언에서 나온다 — 04 는 compile·lint·check·scoped."""
        repo, paths, s = gated
        fx = make_fixture(fxdir, "loop-04", dict(ALL_PASS))
        env = cli.run_gate_cmd(repo, phase="04", only_stage="loop",
                               replay=str(fx))
        assert env["exit"] == 0, env["render"]
        assert [x["id"] for x in env["data"]["stages"]] == [
            "compile", "lint", "check", "scoped"]

    def test_단일_stage_는_종전대로_그_하나만_돈다(self, gated, fxdir):
        repo, paths, s = gated
        fx = make_fixture(fxdir, "single", dict(ALL_PASS))
        env = cli.run_gate_cmd(repo, phase="04", only_stage="scoped",
                               replay=str(fx))
        assert env["exit"] == 0
        assert [x["id"] for x in env["data"]["stages"]] == ["scoped"]
        assert env["data"]["stage"]["id"] == "scoped", "옛 키는 유지한다"

    def test_지시문이_scoped_단독_재게이트를_더_말하지_않는다(self):
        """문서가 코드와 같은 말을 해야 워커가 옛 명령을 치지 않는다."""
        for rel in ("harness/phases/05-code-review.md",
                    ".claude/commands/feature.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            assert "--stage scoped" not in text, rel
            assert "--stage loop" in text, rel


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
        """cmd:null 스테이지는 스킵으로 기록되고 등급에 반영된다.

        docs 는 어댑터가 `not_applicable` 로 선언해 `stage_na` 다 — 부재가 아니다.
        """
        repo, paths, s = gated
        fx = make_fixture(fxdir, "all-pass", dict(ALL_PASS))
        env = _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert "stage_absent:e2e" in report["gaps"]
        assert "stage_na:docs" in report["gaps"]
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
        """직전 완주 런의 테스트 수 × 0.9 가 하한이다 — 삭제·스킵을 잡는다."""
        repo, paths, s = gated
        _finished_run(repo, "prev", "2026-01-01T00:00:00+0900", ran=1300)
        fx = make_fixture(fxdir, "tests-shrank", dict(ALL_PASS), tests=100)
        env = _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert report["tests"]["status"] == "shrank"
        assert report["tests"]["expected_min"] == 1170
        assert report["tests"]["source"] == "previous_run"
        assert env["exit"] in (4, 5, 10)

    def test_no_finished_run_means_no_floor(self, gated, fxdir):
        """새 클론의 첫 런은 하한이 없다 — 0 이 아니라 None 이고 `ok` 다."""
        repo, paths, s = gated
        fx = make_fixture(fxdir, "no-floor", dict(ALL_PASS), tests=100)
        env = _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert report["tests"]["expected_min"] is None
        assert report["tests"]["status"] == "ok"
        assert env["exit"] == 0, env["render"]

    def test_floor_skips_finished_runs_without_a_test_count(self, gated, fxdir):
        """docs 레인처럼 full 이 안 돈 완주 런이 감지를 끄지 않는다."""
        repo, paths, s = gated
        _finished_run(repo, "older", "2026-01-01T00:00:00+0900", ran=1300)
        _finished_run(repo, "newer", "2026-01-02T00:00:00+0900", ran=None)
        fx = make_fixture(fxdir, "skip-docs-run", dict(ALL_PASS), tests=100)
        _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert report["tests"]["expected_min"] == 1170

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


    def test_귀속_규칙_관측이_런에_남는다(self, gated, fxdir):
        """승격 판정의 증거는 **판정이 일어난 순간**에 적힌다 ([[ADR-H069]]).

        완주 런을 나중에 긁는 대신 여기서 적으면 `--replay` 가 공짜로 따라온다
        — 러너만 갈아끼우고 귀속은 그대로 돌기 때문이다.
        """
        repo, paths, s = gated
        stages = dict(ALL_PASS, compile={"exit": 2})
        log = ("src/lib/match.test.ts(3,10): error TS2305: "
               "Module './match' has no exported member 'matchTitle'.")
        fx = make_fixture(fxdir, "rules-observed", stages, stdouts={"compile": log})
        _gate(repo, fx)
        _, after = st.load(repo, paths.run_id)
        got = (after["phases"]["04-gate"] or {}).get("attribution_rules")
        assert got == ["compile_error_regex", "symbol_not_found_patterns"], got

    def test_규칙_관측은_회차를_거듭해도_중복되지_않는다(self, gated, fxdir):
        repo, paths, s = gated
        stages = dict(ALL_PASS, compile={"exit": 2})
        log = "src/lib/match.ts(9,3): error TS2322: Type 'string' is not assignable."
        fx = make_fixture(fxdir, "rules-twice", stages, stdouts={"compile": log})
        _gate(repo, fx)
        _gate(repo, fx)
        _, after = st.load(repo, paths.run_id)
        assert after["phases"]["04-gate"]["attribution_rules"] == ["compile_error_regex"]

    def test_통과한_회차는_관측을_남기지_않는다(self, gated, fxdir):
        """실패가 없었던 것과 규칙이 돌았다는 것은 다른 사실이다."""
        repo, paths, s = gated
        _gate(repo, make_fixture(fxdir, "rules-pass", dict(ALL_PASS)))
        _, after = st.load(repo, paths.run_id)
        assert (after["phases"]["04-gate"] or {}).get("attribution_rules") == []

    def test_실패_항목을_못_읽으면_gap_이_붙는다(self, gated, fxdir):
        """파싱이 깨져도 게이트는 멈추지 않는다 — 표시가 없으면 조용히 산다.

        2026-09-03 에 `compile_error_regex` 가 실물 출력에 0건 매칭이던 버그가
        오래 살아남은 구조가 이것이다 ([[ADR-H069]]).
        """
        repo, paths, s = gated
        stages = dict(ALL_PASS, lint={"exit": 1})
        fx = make_fixture(fxdir, "unparsed", stages,
                          stdouts={"lint": "무엇인지 알 수 없는 출력"})
        _gate(repo, fx)
        _, after = st.load(repo, paths.run_id)
        assert "attribution_unparsed" in (after.get("gaps") or []), after.get("gaps")
        assert after["grade"] == "PASS_WITH_GAPS"

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
    """resume · 에스컬레이션 잠금"""

    def test_escalated_state_locks_every_command(self, gated):
        repo, paths, s = gated
        st.escalate(paths, s, "사람 판단", ["가", "나"], phase=s.get("phase"))
        for env in (cli.run_next(repo, paths.run_id),
                    cli.run_gate_cmd(repo, run_id=paths.run_id)):
            assert env["exit"] == 10, env["cmd"]

    def test_resume_needs_an_explicit_ack(self, gated):
        repo, paths, s = gated
        st.escalate(paths, s, "사람 판단", ["가", "나"], phase=s.get("phase"))
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
    config, adapter = _load(repo)
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
        config, adapter = _load(repo)
        adapter = dict(adapter)
        adapter.pop("entrypoint_resolver", None)
        got = tr.run(repo, config, adapter, _write_contract(repo))
        assert got["entrypoint_resolver"] == "none"
        assert "missing_entrypoint" in got["skipped"]
        assert [f for f in got["findings"] if f["code"] == "missing_entrypoint"] == []
        # 오류 어휘 검사는 그대로 돌아야 한다.
        assert any(f["code"] == "missing_error_symbol" for f in got["findings"])
        # 진입점 해석에 기대는 셋만 빠지고 나머지 일곱은 돈다.
        assert set(got["skipped"]) == {"missing_entrypoint", "untested_entrypoint",
                                       "authz_untested"}
        assert len(got["checks_run"]) == 7


class TestContractTraceAdapterConventions:
    """**contract-trace 는 어댑터가 선언한 관례를 읽는다** (ADR-H049).

    파일럿 원장 140건 중 41건이 `contract-trace` 출처였고 그중
    `maxduration-route-config-not-out-of-contract` 가 9회/7런 반복 오탐이었다 —
    라우트 파일이 내보내는 `POST`·`maxDuration` 은 진입점 관례인데 계약의
    `## 유닛` 에 다시 적지 않으면 `out_of_contract` 로 잡혔다. 또 ad59(FR-007)
    는 계약에 API_SPEC 표기 `{id}` 를 그대로 써 `[id]` 폴더를 못 찾아
    `missing_entrypoint` critical 을 냈다. 둘 다 스택 관례라 **어댑터가
    선언하고 코어는 읽기만 한다** (ADR-H031).
    """

    ROUTE = ("export const maxDuration = 30\n"
             "export async function POST() {}\n"
             "export function helperNotInContract(): void {}\n")

    def _route_file(self, repo, *parts):
        d = repo / "src" / "app" / "api"
        for part in parts:
            d = d / part
        d.mkdir(parents=True, exist_ok=True)
        f = d / "route.ts"
        f.write_text(self.ROUTE, encoding="utf-8")
        return f.relative_to(repo).as_posix()

    def _ooc(self, got):
        return sorted(f["symbol"] for f in got["findings"]
                      if f["code"] == "out_of_contract")

    def _missing(self, got):
        return [f for f in got["findings"] if f["code"] == "missing_entrypoint"]

    def test_계약의_중괄호_파라미터가_대괄호_폴더로_해석된다(self, repo):
        self._route_file(repo, "items", "[id]")
        c = CONTRACT.replace("- `POST /api/analyze` → 200",
                             "- `POST /api/items/{id}` → 200")
        got = _trace(repo, _write_contract(repo, c))
        assert self._missing(got) == [], got["findings"]

    def test_대괄호_표기는_그대로_해석된다(self, repo):
        self._route_file(repo, "items", "[id]")
        c = CONTRACT.replace("- `POST /api/analyze` → 200",
                             "- `POST /api/items/[id]` → 200")
        got = _trace(repo, _write_contract(repo, c))
        assert self._missing(got) == [], got["findings"]

    def test_진입점_파일의_관례_export_는_계약_밖이_아니다(self, repo):
        rel = self._route_file(repo, "analyze")
        got = _trace(repo, _write_contract(repo), changed=[rel])
        assert self._ooc(got) == ["helperNotInContract"], got["findings"]

    def test_진입점이_아닌_파일의_같은_이름은_여전히_잡힌다(self, repo):
        """관례는 **진입점 파일**에만 있다 — 다른 파일의 `maxDuration` 은 신규 심볼이다."""
        f = repo / "src" / "lib" / "limits.ts"
        f.write_text("export const maxDuration = 30\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo), changed=["src/lib/limits.ts"])
        assert self._ooc(got) == ["maxDuration"], got["findings"]

    def test_어댑터가_관례를_선언하지_않으면_종전_동작이다(self, repo):
        rel = self._route_file(repo, "analyze")
        config, adapter = _load(repo)
        adapter = json.loads(json.dumps(adapter))
        adapter["entrypoint_resolver"].pop("implied_exports", None)
        adapter["entrypoint_resolver"].pop("param_styles", None)
        got = tr.run(repo, config, adapter, _write_contract(repo), changed=[rel])
        assert self._ooc(got) == ["POST", "helperNotInContract", "maxDuration"]

    def test_실물_어댑터가_둘_다_선언한다(self):
        a = harness._read_json(ROOT / "harness/adapters/nextjs-ts.json")
        r = a["entrypoint_resolver"]
        assert "POST" in r["implied_exports"] and "maxDuration" in r["implied_exports"]
        assert r["param_styles"] == ["[]"]
        # 스키마가 두 필드를 받는다 — 어댑터가 doctor 를 통과해야 한다.
        errors = harness.validate(
            a, harness._read_json(ROOT / "harness/adapters/adapter.schema.json"))
        assert errors == [], errors


class TestConventionFilesBeyondTheEntrypointMap:
    """미구현 백로그 24 — 관례 면제가 **진입점 맵에만** 걸려 있었다.

    [[ADR-H049]] 의 면제는 작동하지만 `is_entrypoint_file()` 이 참일 때만
    걸리는데 `nextjs-ts` 의 진입점 맵은 API route 하나뿐이다. 그래서 화면
    파일이 관례로 내보내는 이름이 계약에 없다는 이유로 major 가 됐다 —
    클론 4런의 `contract-trace` findings 6건 중 5건이 `out_of_contract`
    major 였고 전부 deferred 로 버려졌다.

    **관례 파일 목록은 어댑터가 선언하고 코어는 읽기만 한다** (ADR-H031 ·
    ADR-H038). 진입점이 아닌 관례 파일이므로 `is_entrypoint_file` 의 뜻은
    넓히지 않고 `is_convention_file` 을 따로 둔다.

    **타입 전용 export 는 [[ADR-H072]] 결정 4 가 닫았다** — `type`·`interface`
    는 `out_of_contract` 가 아예 안 본다. `enum` 은 런타임 객체를 내보내므로
    면제가 아니다.
    """

    PAGE = ("export const dynamic = 'force-dynamic'\n"
            "export const metadata = {}\n"
            "export async function generateMetadata() {}\n"
            "export function generateStaticParams() {}\n"
            "export const viewport = {}\n"
            "export default function Page() {}\n")

    def _ooc(self, got):
        return sorted(f["symbol"] for f in got["findings"]
                      if f["code"] == "out_of_contract")

    def _write(self, repo, rel, text):
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        return rel

    def test_화면_관례_파일의_export_는_한_건도_안_잡힌다(self, repo):
        rel = self._write(repo, "src/app/items/page.tsx", self.PAGE)
        got = _trace(repo, _write_contract(repo), changed=[rel])
        assert self._ooc(got) == [], got["findings"]

    def test_레이아웃도_같다(self, repo):
        rel = self._write(repo, "src/app/layout.tsx",
                          "export const metadata = {}\n"
                          "export default function Layout() {}\n")
        got = _trace(repo, _write_contract(repo), changed=[rel])
        assert self._ooc(got) == [], got["findings"]

    def test_관례_파일이_아니면_같은_이름도_잡힌다(self, repo):
        """면제는 **파일**에 걸린다 — 아무 데서나 `metadata` 를 내보내는 것은 신규 심볼이다."""
        rel = self._write(repo, "src/lib/meta.ts", "export const metadata = {}\n")
        got = _trace(repo, _write_contract(repo), changed=[rel])
        assert self._ooc(got) == ["metadata"], got["findings"]

    def test_어댑터가_관례_글롭을_선언하지_않으면_종전_동작이다(self, repo):
        rel = self._write(repo, "src/app/items/page.tsx", self.PAGE)
        config, adapter = _load(repo)
        adapter = json.loads(json.dumps(adapter))
        adapter["entrypoint_resolver"].pop("convention_globs", None)
        got = tr.run(repo, config, adapter, _write_contract(repo), changed=[rel])
        assert "dynamic" in self._ooc(got), got["findings"]

    def test_타입_전용_export_는_계약에_없어도_안_잡힌다(self, repo):
        """[[ADR-H072]] 결정 4 — 계약은 런타임 심볼을 본다.

        클론 4런의 `out_of_contract` 6건 중 3건이 타입 별칭이었고 전부
        major · 전부 deferred 였다. 탈출구가 「01 이 내부 타입까지 계약에
        열거한다」뿐이면 계약이 타입 선언서가 된다 (백로그 24).
        """
        rel = self._write(repo, "src/lib/shapes.ts",
                          "export type ItemRow = { id: string }\n"
                          "export interface ItemView { id: string }\n")
        got = _trace(repo, _write_contract(repo), changed=[rel])
        assert self._ooc(got) == [], got["findings"]

    def test_enum_은_런타임_값이라_그대로_잡힌다(self, repo):
        """`export enum` 은 런타임 객체를 내보낸다 — 다른 모듈이 값으로 쓴다."""
        rel = self._write(repo, "src/lib/shapes.ts",
                          "export enum Status { Open = 'open' }\n")
        got = _trace(repo, _write_contract(repo), changed=[rel])
        assert self._ooc(got) == ["Status"], got["findings"]

    def test_런타임_export_는_그대로_잡힌다(self, repo):
        rel = self._write(repo, "src/lib/shapes.ts",
                          "export const ROWS = 1\n"
                          "export function toRow() {}\n")
        got = _trace(repo, _write_contract(repo), changed=[rel])
        assert self._ooc(got) == ["ROWS", "toRow"], got["findings"]

    def test_어댑터가_정규식을_덮으면_면제도_어댑터_몫이다(self, repo):
        """override 정규식에는 `kw` 그룹이 없다 — `groupdict()` 로 읽어
        터지지 않고, 면제는 그 어댑터가 정한다.
        """
        rel = self._write(repo, "src/lib/shapes.ts",
                          "export type ItemRow = { id: string }\n")
        config, adapter = _load(repo)
        adapter = json.loads(json.dumps(adapter))
        adapter.setdefault("attribution", {})["public_symbol_regex"] = (
            r"^\s*export\s+(?:type|const)\s+(?P<name>[^\W\d][\w$]*)")
        got = tr.run(repo, config, adapter, _write_contract(repo),
                     changed=[rel])
        assert self._ooc(got) == ["ItemRow"], got["findings"]

    def test_실물_어댑터가_글롭과_이름을_선언한다(self):
        a = harness._read_json(ROOT / "harness/adapters/nextjs-ts.json")
        r = a["entrypoint_resolver"]
        assert r["convention_globs"], r
        for name in ("metadata", "generateMetadata", "generateStaticParams",
                     "viewport"):
            assert name in r["implied_exports"], name
        errors = harness.validate(
            a, harness._read_json(ROOT / "harness/adapters/adapter.schema.json"))
        assert errors == [], errors


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
        config, adapter = _load(repo)
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
        config, adapter = _load(repo)
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
        config, adapter = _load(repo)
        contract_path = _write_contract(repo, self.CONTRACT)
        trace = tr.run(repo, config, adapter, contract_path, changed=[])
        assert got["repo_files"] == trace["repo_files"], (got, trace["repo_files"])
        assert got["repo_files"] > 0


class TestContractTraceBaseline:
    """오탐 이력이 있는 둘(`untested_contract_item`·`out_of_contract`)은 **언제나** warn_only 다.

    원장이 없으니 「그 검사가 지적을 낸 런 수」로 유예를 셀 수 없다 — 78/78 · 6/6
    오탐 이력을 근거로 상수로 둔다. 둘 자체는 Wave 4 가 지운다.
    """

    def _contract_untested(self, repo):
        (repo / "src" / "lib" / "match.test.ts").write_text("// 아무것도 안 부른다\n",
                                                            encoding="utf-8")
        return _write_contract(repo)

    def test_untested_is_always_warn_only(self, repo):
        got = _trace(repo, self._contract_untested(repo))
        f = next(f for f in got["findings"] if f["code"] == "untested_contract_item")
        assert f["resolution"] == "warn_only"
        assert f["why_warn_only"]
        assert "baseline" not in got, "런 수 유예는 원장과 함께 사라졌다"

    def test_test_existence_checks_have_no_grace(self, repo):
        """존재 검사 셋은 유예가 없다 — 첫 런부터 지적이다 (ADR-H058 결정 8)."""
        got = _trace(repo, self._contract_untested(repo))
        f = next(f for f in got["findings"] if f["code"] == "untested_error_symbol")
        assert f["resolution"] == "deferred"

    def test_symbol_referenced_by_test_is_clean(self, repo):
        """심볼 문자열 **또는** 진입점 경로 — 둘 다 실패할 때만 지적한다."""
        got = _trace(repo, _write_contract(repo))   # match.test.ts 가 matchTitle 을 import 한다
        assert [f for f in got["findings"] if f["code"] == "untested_contract_item"] == []

    def test_out_of_contract_is_always_warn_only(self, repo):
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


def _route(repo, name, test_body=None):
    """`src/app/api/<name>/route.ts` 와 (주면) 그 옆의 `route.test.ts`."""
    d = repo / "src" / "app" / "api" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "route.ts").write_text("export async function POST() {}\n",
                                encoding="utf-8")
    if test_body is not None:
        (d / "route.test.ts").write_text(test_body, encoding="utf-8")
    return d


def _codes(got, code):
    return [f for f in got["findings"] if f["code"] == code]


class TestContractTraceUntestedEntrypoint:
    """진입점마다 **그 진입점의** 테스트 파일이 있는가 (ADR-H058).

    스코프 선택의 `_tests_for_source` 를 쓰지 않는다 — 앱라우터에서 진입점
    파일이 전부 `route.ts` 라 스템 일치가 리포의 모든 `route.test.ts` 를 잡고,
    존재 검사에서 그 과선택은 거짓 통과다 (N1).
    """

    def test_route_test_next_to_route_passes(self, repo):
        _route(repo, "analyze", "it('ok', () => {})\n")
        got = _trace(repo, _write_contract(repo), changed=[])
        assert _codes(got, "untested_entrypoint") == [], got["findings"]

    def test_no_test_is_major_for_the_test_role(self, repo):
        _route(repo, "analyze")
        got = _trace(repo, _write_contract(repo), changed=[])
        miss = _codes(got, "untested_entrypoint")
        assert len(miss) == 1
        assert miss[0]["severity"] == "major"
        assert miss[0]["target_role"] == "test"
        assert miss[0]["category"] == "TEST_MISSING_FAILURE_PATH"

    def test_another_routes_test_does_not_count(self, repo):
        """스템이 같은 `route.test.ts` 라도 다른 라우트 것이면 통과가 아니다."""
        _route(repo, "analyze")
        _route(repo, "other", "it('ok', () => {})\n")
        got = _trace(repo, _write_contract(repo), changed=[])
        assert len(_codes(got, "untested_entrypoint")) == 1, got["findings"]

    def test_alias_import_from_elsewhere_counts(self, repo):
        _route(repo, "analyze")
        (repo / "src" / "lib" / "analyze-flow.test.ts").write_text(
            'import { POST } from "@/app/api/analyze/route";\n', encoding="utf-8")
        got = _trace(repo, _write_contract(repo), changed=[])
        assert _codes(got, "untested_entrypoint") == [], got["findings"]

    def test_relative_import_from_elsewhere_counts(self, repo):
        _route(repo, "analyze")
        (repo / "src" / "lib" / "analyze-flow.test.ts").write_text(
            "const m = await import(\n  '../app/api/analyze/route'\n)\n",
            encoding="utf-8")
        got = _trace(repo, _write_contract(repo), changed=[])
        assert _codes(got, "untested_entrypoint") == [], got["findings"]

    def test_unresolved_entrypoint_is_recorded_not_flagged(self, repo):
        """진입점이 없으면 `missing_entrypoint` 의 몫이다 — 여기서 겹쳐 지적하지 않는다."""
        got = _trace(repo, _write_contract(repo), changed=[])
        assert _codes(got, "untested_entrypoint") == []
        assert got["entrypoints_unresolved"] == ["POST /api/analyze"]

    def test_no_resolver_skips_it(self, repo):
        config, adapter = _load(repo)
        adapter = dict(adapter)
        adapter.pop("entrypoint_resolver", None)
        got = tr.run(repo, config, adapter, _write_contract(repo), changed=[])
        assert "untested_entrypoint" in got["skipped"]
        assert got["skip_reasons"]["untested_entrypoint"]


class TestContractTraceUntestedErrorSymbol:

    def test_error_constant_absent_from_tests_is_major(self, repo):
        got = _trace(repo, _write_contract(repo), changed=[])
        miss = _codes(got, "untested_error_symbol")
        assert len(miss) == 1 and miss[0]["severity"] == "major"
        assert miss[0]["symbol"] == "MATCH_FAILED"
        assert miss[0]["target_role"] == "test"

    def test_error_constant_in_a_test_is_clean(self, repo):
        (repo / "src" / "lib" / "match.test.ts").write_text(
            "expect(code).toBe('MATCH_FAILED')\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo), changed=[])
        assert _codes(got, "untested_error_symbol") == []

    def test_a_longer_name_is_not_a_match(self, repo):
        (repo / "src" / "lib" / "match.test.ts").write_text(
            "expect(code).toBe('MATCH_FAILED_TWICE')\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo), changed=[])
        assert len(_codes(got, "untested_error_symbol")) == 1


class TestContractTraceAuthzUntested:
    """`[역할]` 태그 진입점은 **그 진입점의** 테스트에 거부 단언이 있어야 한다."""

    TAGGED = CONTRACT.replace("- `POST /api/analyze` → 200",
                              "- `POST /api/analyze` [admin] → 200")

    def test_denial_in_the_routes_own_test_passes(self, repo):
        _route(repo, "analyze", "expect(res.status).toBe(403)\n")
        got = _trace(repo, _write_contract(repo, self.TAGGED), changed=[])
        assert _codes(got, "authz_untested") == [], got["findings"]

    def test_no_denial_is_major(self, repo):
        _route(repo, "analyze", "expect(res.status).toBe(200)\n")
        got = _trace(repo, _write_contract(repo, self.TAGGED), changed=[])
        miss = _codes(got, "authz_untested")
        assert len(miss) == 1 and miss[0]["severity"] == "major"
        assert miss[0]["category"] == "AUTHZ_MISSING_RULE"

    def test_denial_in_another_routes_test_does_not_count(self, repo):
        _route(repo, "analyze", "expect(res.status).toBe(200)\n")
        _route(repo, "other", "expect(res.status).toBe(403)\n")
        got = _trace(repo, _write_contract(repo, self.TAGGED), changed=[])
        assert len(_codes(got, "authz_untested")) == 1, got["findings"]

    def test_untagged_entrypoint_is_not_checked(self, repo):
        _route(repo, "analyze", "expect(res.status).toBe(200)\n")
        got = _trace(repo, _write_contract(repo), changed=[])
        assert _codes(got, "authz_untested") == []

    def test_adapter_without_pattern_skips_with_a_reason(self, repo):
        _route(repo, "analyze", "expect(res.status).toBe(200)\n")
        config, adapter = _load(repo)
        adapter = json.loads(json.dumps(adapter))
        adapter["attribution"].pop("authz_denied_pattern", None)
        got = tr.run(repo, config, adapter, _write_contract(repo, self.TAGGED),
                     changed=[])
        assert "authz_untested" in got["skipped"]
        assert "authz_denied_pattern" in got["skip_reasons"]["authz_untested"]
        assert _codes(got, "authz_untested") == []

    def test_real_adapter_declares_pattern_and_aliases(self):
        a = harness._read_json(ROOT / "harness/adapters/nextjs-ts.json")
        assert re.search(a["attribution"]["authz_denied_pattern"], "toBe(403)")
        assert a["attribution"]["import_aliases"] == {"@/": "src/"}
        errors = harness.validate(
            a, harness._read_json(ROOT / "harness/adapters/adapter.schema.json"))
        assert errors == [], errors


class TestContractTraceNoContract:

    def test_no_contract_mode_is_recorded_not_passed(self, repo):
        config, adapter = _load(repo)
        got = tr.run(repo, config, adapter, None, no_contract=True)
        assert got["status"] == "skipped_no_contract"
        assert got["findings"] == []
        assert got["checks_run"] == []


class TestContractTraceCli:

    def test_cli_emits_a_single_envelope_and_writes_the_file(self, repo, request_file):
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

    def test_test_files_do_not_count_toward_files_max(self, repo):
        """ADR-H066 — 파일 수 예산은 소스만 센다. 줄 수는 여전히 전체다."""
        _branch(repo, "feat-x")
        _bulk_change(repo, 3)
        for i in range(11):
            (repo / "src" / "lib" / ("t%d.test.ts" % i)).write_text(
                "export const t = %d\n" % i, encoding="utf-8")
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 0, got["checks"]
        assert got["budget"]["files"] == 3, got["budget"]
        assert got["budget"]["test_files_excluded"] == 11, got["budget"]

    def test_source_files_alone_still_exceed_files_max(self, repo):
        _branch(repo, "feat-x")
        _bulk_change(repo, 11)
        (repo / "src" / "lib" / "a.test.ts").write_text("export const t = 1\n",
                                                        encoding="utf-8")
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 9
        assert got["budget"]["files"] == 11, got["budget"]


class TestPrecheckPolicyOverride:
    """미구현 백로그 26 — 같은 정책을 05·06 에서 두 번 묻지 않는다.

    exit 9 는 상태를 잠그지도 카운터를 쓰지도 않고 오버라이드 플래그도 없었다 —
    **사람이 「그대로 간다」를 고른 사실이 어디에도 남지 않았다.** §E13 이 재개마다
    재실행을 요구하므로 06 에서 연속 두 번 묻는 일까지 생긴다. 클론 4런에서
    3런이 05·06 양쪽에서 exit 9 였고 사람 대기 8.5분이 전체 대기의 30% 였다.

    **검사는 계속 돈다.** 바뀌는 것은 「같은 사유·같은 값이면 다시 묻지 않는다」
    뿐이고, 넘어간 사실은 `precheck_policy_override` gap 으로 등급이 치른다 —
    면제는 통과가 아니다 (ADR-H027).
    """

    def _over(self, repo, files=12):
        _branch(repo, "feat-x")
        _bulk_change(repo, files)

    def test_지문이_실패_사유와_값을_담는다(self, repo):
        self._over(repo)
        got = pc.run(repo, scope="pr")
        fp = got["policy_fingerprint"]
        assert fp["reasons"] == ["예산"], fp
        assert fp["files"] >= 12 and fp["lines"] >= 12, fp

    def test_통과한_런은_지문이_없다(self, repo):
        _branch(repo, "feat-x")
        _bulk_change(repo, 1)
        got = pc.run(repo, scope="pr")
        assert got["exit"] == 0 and got["policy_fingerprint"] is None, got

    def test_ack_없이는_종전대로_다시_묻는다(self, repo, request_file):
        self._over(repo)
        cli.run_init(repo, "x", request_file)
        assert cli.run_precheck(repo, scope="pr", phase="05")["exit"] == 9
        assert cli.run_precheck(repo, scope="pr", phase="06")["exit"] == 9

    def test_ack_한_뒤_같은_지문이면_06_이_묻지_않는다(self, repo, request_file):
        self._over(repo)
        cli.run_init(repo, "x", request_file)
        assert cli.run_precheck(repo, scope="pr", phase="05")["exit"] == 9
        acked = cli.run_precheck(repo, scope="pr", phase="05", ack_policy=True)
        assert acked["exit"] == 0, acked["render"]
        env = cli.run_precheck(repo, scope="pr", phase="06")
        assert env["exit"] == 0, env["render"]
        assert "precheck_policy_override" in (env["data"].get("gaps") or []), env["data"]

    def test_대기는_런당_한_번뿐이다(self, repo, request_file):
        """닫힘 조건 — `waiting_human {"reason":"precheck_policy"}` 가 런당 1회 이하."""
        self._over(repo)
        cli.run_init(repo, "x", request_file)
        cli.run_precheck(repo, scope="pr", phase="05")
        cli.run_precheck(repo, scope="pr", phase="05", ack_policy=True)
        cli.run_precheck(repo, scope="pr", phase="06")
        cli.run_precheck(repo, scope="pr", phase="06")
        paths, _ = st.load(repo)
        waits = [e for e in st.read_events(paths)
                 if e["kind"] == "waiting_human"
                 and e["data"]["reason"] == "precheck_policy"]
        assert len(waits) == 1, waits

    def test_값이_나빠지면_다시_묻는다(self, repo, request_file):
        """오버라이드는 사람이 본 범위까지다 — 더 커진 것은 사람이 안 본 것이다."""
        self._over(repo)
        cli.run_init(repo, "x", request_file)
        cli.run_precheck(repo, scope="pr", phase="05")
        cli.run_precheck(repo, scope="pr", phase="05", ack_policy=True)
        _bulk_change(repo, 30)
        assert cli.run_precheck(repo, scope="pr", phase="06")["exit"] == 9

    def test_새_사유가_붙으면_다시_묻는다(self, repo, request_file):
        self._over(repo)
        cli.run_init(repo, "x", request_file)
        cli.run_precheck(repo, scope="pr", phase="05")
        cli.run_precheck(repo, scope="pr", phase="05", ack_policy=True)
        _git(repo, "checkout", "-q", "main")
        env = cli.run_precheck(repo, scope="pr", phase="06")
        assert env["exit"] == 9, env["render"]

    def test_넘어간_사실이_등급을_내린다(self, repo, request_file):
        self._over(repo)
        cli.run_init(repo, "x", request_file)
        cli.run_precheck(repo, scope="pr", phase="05")
        cli.run_precheck(repo, scope="pr", phase="05", ack_policy=True)
        _, s = st.load(repo)
        assert s["grade"] == st.GRADES[1], s["grade"]
        assert "precheck_policy_override" in (s.get("gaps") or []), s.get("gaps")

    def test_ack_는_통과한_런에서는_아무것도_안_남긴다(self, repo, request_file):
        """넘길 것이 없는데 오버라이드를 적으면 다음 초과를 조용히 삼킨다."""
        _branch(repo, "feat-x")
        _bulk_change(repo, 1)
        cli.run_init(repo, "x", request_file)
        env = cli.run_precheck(repo, scope="pr", phase="05", ack_policy=True)
        assert env["exit"] == 0
        _, s = st.load(repo)
        assert not (s.get("precheck") or {}).get("policy_override"), s.get("precheck")

    def test_봉투가_고르는_법을_이름으로_알려준다(self, repo, request_file):
        """사람에게 선택지를 주면서 그것을 못박는 법을 안 적으면 아무도 안 쓴다."""
        self._over(repo)
        cli.run_init(repo, "x", request_file)
        env = cli.run_precheck(repo, scope="pr", phase="05")
        assert "--ack-policy" in env["render"], env["render"]

    def test_넘어간_런의_봉투가_프로브_면제로_읽히지_않는다(self, repo, request_file):
        self._over(repo)
        cli.run_init(repo, "x", request_file)
        cli.run_precheck(repo, scope="pr", phase="05")
        env = cli.run_precheck(repo, scope="pr", phase="05", ack_policy=True)
        assert "면제된 프로브" not in env["render"], env["render"]
        assert "통과가 아니라" in env["render"], env["render"]

    def test_gap_이_이름으로_설명된다(self):
        import report as rep
        assert rep.gap_reason("precheck_policy_override"), \
            "어휘에 없으면 보고서와 PR 본문이 설명하지 못한다"
        assert not rep.is_non_demoting("precheck_policy_override")


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


class TestStageNotApplicable:
    """[[ADR-H047]] 추기 — 스택에 **구조적으로 없는** 스테이지는 부재와 다른 사실이다.

    banana 15런 전부 `stage_absent:docs` 로 강등됐는데, docs 는 그 스택에
    처음부터 없는 것이고 e2e 는 TRD 가 미룬 것이다. 같은 gap 코드면 "도입을
    미뤘다" 와 "해당 없다" 가 구분되지 않는다. 어댑터가 `not_applicable` 로
    사유를 선언하면 `stage_na:<id>` 로 남되 등급은 내리지 않는다.
    """

    NA = {"cmd": None, "not_applicable": "문서 빌드 산출물이 없다."}

    def _adapter(self, repo, **stages):
        p = repo / "harness" / "adapters" / "nextjs-ts.json"
        ad = json.loads(p.read_text(encoding="utf-8"))
        ad["stages"].update(stages)
        p.write_text(json.dumps(ad, ensure_ascii=False), encoding="utf-8")

    def test_선언이_있으면_na_이고_없거나_비면_absent_다(self):
        assert adapters.stage_state({"stages": {"docs": dict(self.NA)}}, "docs") == "na"
        assert adapters.stage_state({"stages": {"docs": {"cmd": None}}}, "docs") == "absent"
        assert adapters.stage_state(
            {"stages": {"docs": {"cmd": None, "not_applicable": " "}}}, "docs") == "absent"

    def test_run_stage_는_na_로_건너뛴다(self, repo):
        got = adapters.run_stage(repo, {"stages": {"docs": dict(self.NA)}}, "docs",
                                 runner=lambda *a, **k: pytest.fail("실행했다"))
        assert got == {"id": "docs", "state": "skipped", "reason": "na"}

    def test_비강등_판정은_하나의_함수다(self):
        assert rep_mod.is_non_demoting("stage_na:docs")
        assert not rep_mod.is_non_demoting("stage_absent:e2e")
        assert not rep_mod.is_non_demoting("attribution_unparsed"),             "파싱이 깨진 것은 표시가 아니라 결함이다"
        assert rep_mod.gap_reason("stage_na:docs"), "어휘에 있어야 보고서가 설명한다"

    def test_동봉_어댑터가_구조적_부재와_미룬_부재를_가른다(self):
        """[[ADR-H072]] 결정 1 — 산문이 말하던 것을 기계가 말하게 한다.

        `self-python` 의 compile·build·e2e 는 이 스택에 **구조적으로** 없다
        (파이썬에 컴파일 단계가 없고, 빌드 산출물이 없고, E2E 개념이 없다).
        lint·check 는 **아직 안 들인 것**이라 `absent` 로 남는다 — 그 둘이
        등급을 깎는 것은 결함이 아니라 어댑터가 비었다는 정확한 신호다.
        `nextjs-ts` 의 e2e 도 미룬 부재다 — Playwright 는 TRD 의 [Scale]
        단계이고 러너를 안 깐 것은 구조적 부재가 아니다 (백로그 17 · 22).
        """
        def ad(name):
            return harness._read_json(
                ROOT / "harness" / "adapters" / ("%s.json" % name))

        sp = ad("self-python")
        for sid in ("compile", "build", "e2e", "docs"):
            assert adapters.stage_state(sp, sid) == "na", sid
        for sid in ("lint", "check"):
            assert adapters.stage_state(sp, sid) == "absent", sid
        nx = ad("nextjs-ts")
        assert adapters.stage_state(nx, "e2e") == "absent"
        assert adapters.stage_state(nx, "docs") == "na"

    def test_na_선언과_note_가_반대를_말하지_않는다(self):
        """같은 스테이지에서 `not_applicable`(등급 안 내림)과 `_note`
        ("통과가 아니다")가 공존하면 doctor 의 두 칸이 서로를 반박한다.
        """
        for name in ("self-python", "nextjs-ts", "_template"):
            a = harness._read_json(
                ROOT / "harness" / "adapters" / ("%s.json" % name))
            for sid, spec in (a.get("stages") or {}).items():
                if str(spec.get("not_applicable") or "").strip():
                    assert "통과가 아니다" not in (spec.get("_note") or ""), \
                        "%s.%s" % (name, sid)

    def test_게이트는_stage_na_만으로_등급을_내리지_않는다(self, gated, fxdir,
                                                    monkeypatch):
        repo, paths, s = gated
        # 다른 gap 을 걷어내 stage_na 만 남긴다 — 1파일 픽스처는 scoped 가
        # 늘 퇴화이고, build 는 when_touched 밖이라 stage_not_touched 다.
        monkeypatch.setattr(contract_mod, "DEGENERATE_RATIO", 2.0)
        self._adapter(repo, e2e=dict(self.NA), docs=dict(self.NA),
                      build={"cmd": ["run", "build"]})
        fx = make_fixture(fxdir, "all-pass", dict(ALL_PASS))
        _gate(repo, fx)
        report = json.loads((paths.run_dir / "04_gate_report.json")
                            .read_text(encoding="utf-8"))
        assert "stage_na:docs" in report["gaps"] and "stage_na:e2e" in report["gaps"]
        assert "stage_absent:docs" not in report["gaps"]
        assert [g for g in report["gaps"] if not rep_mod.is_non_demoting(g)] == [], \
            report["gaps"]
        assert report["grade"] == "PASS", report["gaps"]

    def test_PR_본문의_건너뛴_게이트에_남는다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        s["gaps"] = ["stage_na:docs"]
        st.save(_pp, s)
        body = pr_mod.build_body(repo, paths, s,
                                 harness._read_json(repo / harness.CONFIG_REL))
        assert "- stage_na:docs" in body, body


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

    def test_a_test_change_keeps_the_test_reviewer_under_the_normal_cap(self, repo):
        """테스트 파일이 바뀌면 `test` 가 cap 에 잘리지 않는다 — 잘리는 것은
        `arch` 다 (ADR-H062). 파일럿 5런이 `test` 를 `dropped` 로 잃었다."""
        changed = ["src/lib/schemas.ts", "src/app/api/x/route.ts",
                   "src/lib/a.test.ts"]
        got = rv.route(_config(repo), changed, "normal")
        codes = [r["code"] for r in got["reviewers"]]
        assert "test" in codes, got
        assert "arch" not in codes, got

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

    def test_실물_페이즈가_일곱이고_08_은_done_을_가리킨다(self, repo):
        """**M24.** 08 이 아무것도 안 가리키면 런이 닫히는 자리가 없다."""
        loaded, broken = cli.load_phases(ROOT)
        assert broken == []
        assert sorted(loaded) == ["01-plan", "03-implement", "04-gate",
                                  "05-code-review", "06-pr", "07-pr-review",
                                  "08-report"]
        assert loaded["08-report"]["front"].get("on_success") == st.DONE

    def test_전이_사슬이_01_에서_done_까지_이어진다(self, repo):
        """단언 하나로 그래프 전체를 못박는다."""
        loaded, _ = cli.load_phases(ROOT)
        chain, cur = [], "01-plan"
        while cur in loaded:
            chain.append(cur)
            cur = loaded[cur]["front"].get("on_success")
        assert chain == ["01-plan", "03-implement", "04-gate",
                         "05-code-review", "06-pr", "07-pr-review", "08-report"]
        assert cur == st.DONE

    def test_lint_passes_on_the_real_phases(self, repo):
        bad = [f for f in cli.lint_phases(ROOT) if f["status"] == "FAIL"]
        assert bad == [], bad

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
    for pid in ("01-plan", "03-implement", "04-gate"):
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
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        f = paths.run_dir / "05_review_arch.json"
        f.write_text(json.dumps({"reviewer": "arch", "by_checklist": {"a": []}}),
                     encoding="utf-8")
        env = cli.run_record(repo, "05", str(f), reviewer="arch", run_id=run_id)
        assert env["exit"] == 8
        assert "원문" in env["render"]


class TestReview05RoutingRefusesCommittedOnly:
    """**라우팅 대상이 워킹트리에 없으면 failed 를 쓰지 않고 멈춘다** (ADR-H046).

    파일럿 40dc(FR-014): 06 의 브랜치 검사를 미리 통과시키려고 05 라우팅
    **전에** 커밋했다 → `_plan_05_review` 가 worktree diff 0 을 보고 리뷰어
    0명 → `review05.status: failed` 와 gap `review05:failed` 가 append-only
    로 박혔다. `git reset --mixed` 로 되돌려 4/4 리뷰를 정상 수행했는데도
    상태는 `status: ok` 와 `gaps: [review05:failed]` 가 공존한 채 닫혔다.
    라우팅 scope 자체(worktree, ADR-H028)는 유지한다 — 커밋만 있고 워킹트리가
    깨끗한 것은 라우팅 실패가 아니라 **절차 오류**이고, exit 3 으로 되돌린다.
    """

    def _commit_only(self, repo):
        _git(repo, "branch", "-M", "main")
        _git(repo, "checkout", "-qb", "feat-x")
        (repo / "src" / "lib" / "match.ts").write_text(
            "export function matchTitle(a: string, b: string): number { return 1 }\n",
            encoding="utf-8")
        # 소유 범위 파일만 커밋한다 — `-A` 면 픽스처가 뒤에 깐 `.claude/**` 도
        # 커밋돼 "커밋에만 있는 변경" 에 섞인다.
        _git(repo, "add", "src/lib/match.ts")
        _git(repo, "commit", "-qm", "feat: early commit")

    def test_커밋만_있으면_exit_3_이고_failed_를_쓰지_않는다(self, repo,
                                                            request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        self._commit_only(repo)
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 3, env["render"]
        assert "커밋" in env["render"] and "reset --mixed" in env["render"]
        _p, s = st.load(repo, run_id)
        assert s.get("review05") is None
        assert "review05:failed" not in (s.get("gaps") or [])
        assert env["data"]["pr_scope_changed"] == ["src/lib/match.ts"]

    def test_되돌린_뒤에는_정상_라우팅된다(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        self._commit_only(repo)
        _git(repo, "reset", "-q", "--mixed", "HEAD~1")
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 0, env["render"]
        _p, s = st.load(repo, run_id)
        assert s["phases"]["05-code-review"]["planned"]

    def test_진짜_변경_0_은_종전대로_failed_다(self, repo, request_file, phases):
        """커밋도 워킹트리도 비었으면 그것은 관측된 사실이고 G-4 대로 failed 다."""
        run_id, paths = _enter_05(repo, request_file, phases)
        env = cli.run_next(repo, run_id)
        _p, s = st.load(repo, run_id)
        if not s["phases"]["05-code-review"]["planned"]:
            assert env["exit"] == 0
            assert s["review05"]["status"] == "failed"


class TestReview05DispatchFingerprint:
    """**리뷰 대상 코드가 리뷰 뒤에 바뀌었으면 record 가 거부한다** (ADR-H046).

    파일럿 세션 a3decd93 에서 05 가 Major 를 내자 "코드부터 고치고 리뷰
    record 는 나중에" 순서로 진행했다. `review_repair` 카운터는 record 시점에
    소모되므로(`_planned_for_round` 가 `used + 1` 을 읽는다) 이미 두 번 다
    해소된 지적인데도 카운터가 소진돼 exit 10(에스컬레이션)이 떴다 — 실제
    미해결 결함이 아니라 순수한 record 순서 문제였다. 지시(`next`) 시점의
    지문을 라운드에 남기고 record 가 대조하면 그 순서가 기계로 강제된다.
    """

    def _dispatched(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        (repo / "src" / "lib" / "match.ts").write_text(
            "export function matchTitle(a: string, b: string): number { return 1 }\n",
            encoding="utf-8")
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        return run_id, paths

    def test_next_가_라운드의_지문을_남긴다(self, repo, request_file, phases):
        run_id, paths = self._dispatched(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        node = s["phases"]["05-code-review"]
        assert node["dispatched_fp"]["1"]["value"]
        config = harness._read_json(repo / harness.CONFIG_REL)
        assert st.fingerprint_matches(node["dispatched_fp"]["1"],
                                      st.fingerprint(repo, config))

    def test_지시_뒤_코드가_바뀌면_record_는_exit_3(self, repo, request_file,
                                                    phases):
        run_id, paths = self._dispatched(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        code = s["phases"]["05-code-review"]["planned"][0]
        (repo / "src" / "lib" / "match.ts").write_text(
            "export function matchTitle(a: string, b: string): number { return 2 }\n",
            encoding="utf-8")
        f = _reviewer_files(paths, code, [])
        env = cli.run_record(repo, "05", str(f), reviewer=code, round_=1,
                             run_id=run_id)
        assert env["exit"] == 3, env["render"]
        assert "리뷰 뒤에 바뀌었다" in env["render"]
        _p, s = st.load(repo, run_id)
        assert ((s.get("counters") or {}).get("review_repair") or {}).get("used", 0) == 0

    def test_바뀌지_않았으면_record_가_통과한다(self, repo, request_file, phases):
        run_id, paths = self._dispatched(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        code = s["phases"]["05-code-review"]["planned"][0]
        f = _reviewer_files(paths, code, [])
        env = cli.run_record(repo, "05", str(f), reviewer=code, round_=1,
                             run_id=run_id)
        assert "리뷰 뒤에 바뀌었다" not in env["render"]
        assert env["exit"] != 3, env["render"]

    def test_옛_상태는_지문이_없어도_거부하지_않는다(self, repo, request_file,
                                                    phases):
        run_id, paths = self._dispatched(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        code = s["phases"]["05-code-review"]["planned"][0]
        s["phases"]["05-code-review"].pop("dispatched_fp", None)
        st.save(_p, s)
        f = _reviewer_files(paths, code, [])
        env = cli.run_record(repo, "05", str(f), reviewer=code, round_=1,
                             run_id=run_id)
        assert "리뷰 뒤에 바뀌었다" not in env["render"]


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

    def _sub(self, need=None, truncated=False):
        return {"keys": [], "need_more_context": list(need or []),
                "truncated": truncated}

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
                "keys": [{"key": verdict_mod.finding_key(f), "id": f["id"],
                          "severity": f["severity"], "reraised_from": None}
                         for f in findings],
                "findings": list(findings), "closed": list(closed),
                "truncated": False,
                "need_more_context": []}

    def test_델타_라운드가_다른_리뷰어의_열린_Minor_를_지우지_않는다(self, repo):
        """M52 — P7 2회차가 `arch` 하나였고 `sec`·`data` 의 셋이 사라졌다."""
        major = self._mf("F-9", "인가 누락", "major", "AUTHZ_MISSING_RULE")
        r1 = {"arch": self._mslot([self._mf("A-1", "arch 지적 1"),
                                   self._mf("A-2", "arch 지적 2"), major]),
              "sec": self._mslot([self._mf("S-1", "sec 지적")]),
              "data": self._mslot([self._mf("D-1", "data 지적 1"),
                                   self._mf("D-2", "data 지적 2")])}
        r2 = {"arch": self._mslot([], closed=[verdict_mod.finding_key(major)])}
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
        r2 = {"arch": self._mslot([], closed=[verdict_mod.finding_key(major)])}
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
                      "truncated": False,
                      "need_more_context": []}}
        titles = [f["title"] for f in rv.open_findings({"1": r1})]
        assert titles == ["arch 지적"], titles

    def test_첫_등장의_판정이_원장과_같이_이긴다(self, repo):
        """같은 키는 첫 등장이 이긴다 (M30).

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
                         "need_more_context": [],
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
            "1": {"arch": {"keys": [{"key": verdict_mod.finding_key(f), "id": "F-1",
                                     "severity": "minor"}],
                           "findings": [f], "closed": []}},
            "2": {"arch": {"keys": [], "findings": [],
                           "closed": [verdict_mod.finding_key(f)]}}}
        st.save(_p, s)
        (paths.run_dir / "05_review.json").write_text(json.dumps(
            {"round": 1, "review05": s["review05"], "findings": [f]},
            ensure_ascii=False), encoding="utf-8")
        body = self._body(repo, paths, s)
        assert "닫힌 Minor" not in body, body
        assert "- 없다" in body, body


# ---------------------------------------------------------------------------
# J. 등급의 단일 출처 — 06~08 이 얹히기 전에 먼저 세운다
# ---------------------------------------------------------------------------


class TestReview05SeverityRaisedGrant:
    """[[ADR-H048]] 결정 2 — 재상정 승격은 지급이다.

    `728c` 의 05 는 같은 지적이 1라운드 minor → 2라운드 major 로 재상정되며
    `review_repair` 예산 2 를 소진했다 — 재수리 기회 없이. 심각도 상승은
    리뷰어가 처음에 낮게 본 것이라 수리자의 잘못이 아니다. 그 비용을 수리자의
    예산에서 빼지 않는다: `counter_grant("review_repair", 1, "severity_raised")`
    를 **런당 1회** 지급한다. 새 키가 major 로 나는 것은 새 지적이라 지급이 아니다.
    """

    RAISED = {"id": "F-2", "category": "CONCURRENCY", "severity": "minor",
              "target_role": "impl", "title": "동시 갱신에 잠금이 없다",
              "quote": "잠금이 없다"}
    BLOCK = {"id": "F-1", "category": "TX_BOUNDARY", "severity": "major",
             "target_role": "impl", "title": "트랜잭션 경계가 없다",
             "quote": "트랜잭션이 없다"}

    def _ready(self, repo, request_file, phases):
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

    def _submit(self, repo, paths, run_id, round_, findings, resolved=()):
        name = ("05_review_arch.json" if round_ == 1
                else "05_review_arch_r%d.json" % round_)
        j = paths.run_dir / name
        j.write_text(json.dumps({
            "reviewer": "arch", "round": round_, "status": "ok",
            "by_checklist": {"전부": list(findings)},
            "resolved_from_previous": list(resolved),
            "need_more_context": []}, ensure_ascii=False), encoding="utf-8")
        body = "".join("## %s\n\n%s\n" % (f["severity"], f["quote"])
                       for f in findings)
        j.with_name(name.replace(".json", ".raw.md")).write_text(
            "# 리뷰\n\n" + body, encoding="utf-8")
        return cli.run_record(repo, "05", str(j), reviewer="arch",
                              round_=round_, run_id=run_id)

    def _first_round(self, repo, request_file, phases):
        run_id, paths = self._ready(repo, request_file, phases)
        env = self._submit(repo, paths, run_id, 1, [self.BLOCK, self.RAISED])
        assert env["exit"] == 4, env["render"]
        return run_id, paths

    def test_같은_키의_심각도가_오르면_한_번_지급한다(self, repo, request_file,
                                                   phases):
        run_id, paths = self._first_round(repo, request_file, phases)
        env = self._submit(repo, paths, run_id, 2,
                           [dict(self.RAISED, severity="major")],
                           resolved=[{"id": "F-1", "resolved_by": "고쳤다"}])
        _, s = st.load(repo, run_id)
        assert not s.get("escalated"), env["render"]
        assert env["exit"] == 4, env["render"]
        node = s["counters"]["review_repair"]
        assert [(g["extra"], g["reason"]) for g in node.get("grants") or []]             == [(1, "severity_raised")], node
        assert node["used"] == 2 and node["max"] == 3, node
        grant = s["phases"]["05-code-review"].get("severity_raised_grant")
        assert grant and grant["round"] == 2, grant
        assert "severity_raised" in env["render"], env["render"]

    def test_지급은_이벤트로도_남는다(self, repo, request_file, phases):
        run_id, paths = self._first_round(repo, request_file, phases)
        self._submit(repo, paths, run_id, 2, [dict(self.RAISED, severity="major")],
                     resolved=[{"id": "F-1", "resolved_by": "고쳤다"}])
        events = [json.loads(l) for l in
                  paths.events.read_text(encoding="utf-8").splitlines() if l.strip()]
        got = [e for e in events if e["kind"] == "counter_grant"
               and e["data"].get("reason") == "severity_raised"]
        assert len(got) == 1, [e for e in events if e["kind"] == "counter_grant"]

    def test_새_키가_major_로_나는_것은_지급이_아니다(self, repo, request_file,
                                                    phases):
        """새 공격면이 라운드마다 드러나는 경우(`e7ff`)는 새 지적이다 —
        예산이 모자라면 에스컬레이션이 맞다."""
        run_id, paths = self._first_round(repo, request_file, phases)
        new = {"id": "F-3", "category": "TX_BOUNDARY", "severity": "major",
               "target_role": "impl", "title": "전혀 다른 새 지적",
               "quote": "새로 찾은 것"}
        self._submit(repo, paths, run_id, 2, [self.RAISED, new],
                     resolved=[{"id": "F-1", "resolved_by": "고쳤다"}])
        _, s = st.load(repo, run_id)
        assert not s["counters"]["review_repair"].get("grants"), s["counters"]
        assert s.get("escalated"), "예산 2 를 다 썼으면 에스컬레이션이다"

    def test_두_번째_상승은_지급하지_않는다(self, repo, request_file, phases):
        run_id, paths = self._first_round(repo, request_file, phases)
        self._submit(repo, paths, run_id, 2, [dict(self.RAISED, severity="major")],
                     resolved=[{"id": "F-1", "resolved_by": "고쳤다"}])
        self._submit(repo, paths, run_id, 3,
                     [dict(self.RAISED, severity="critical")])
        _, s = st.load(repo, run_id)
        node = s["counters"]["review_repair"]
        assert len(node.get("grants") or []) == 1, node
        assert s.get("escalated"), "실효 상한 3 을 다 썼다"


class TestEscalationPlansTheDeltaRound:
    """미구현 백로그 20 — 에스컬레이션 경로도 다음 라운드의 델타를 세우고 간다.

    05 판정의 `escalate` 분기가 `rounds_planned[round+1]` 을 쓰기 **전에**
    return 해서, 재개된 3라운드가 `_planned_for_round` 의 폴백(전원)을 받았다.
    클론 4런에서 에스컬레이션 2런 모두 `round_reviewers = {"1":4, "2":1, "3":4}`
    였고 **r3 수확은 major 0 · critical 0** 인 반면 r2(델타 1인)는 둘 다 major
    1건을 잡았다 — 좁은 재리뷰는 무는데 넓은 재확인은 안 문다.

    **고칠 곳이 둘이다.** 상태(`rounds_planned`)만 고치면 05 패킷은 여전히
    라우팅 전원을 이름 짓고 `_planned_guard` 가 그중 셋을 exit 8 로 되돌린다 —
    봉투와 검사가 같은 것을 봐야 한다 (M38).
    """

    BLOCK = {"id": "F-1", "category": "TX_BOUNDARY", "severity": "major",
             "target_role": "impl", "title": "트랜잭션 경계가 없다",
             "quote": "트랜잭션이 없다"}

    ROUTED = {"reviewers": [{"code": "arch", "skill": "architecture-reviewer",
                             "matched_count": 1},
                            {"code": "test", "skill": "test-quality-reviewer",
                             "matched_count": 1}],
              "dropped": [], "capped": False}

    def _ready(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        cli.run_next(repo, run_id)
        cli.run_contract_trace(repo, run_id=run_id)
        paths, s = st.load(repo, run_id)
        node = s["phases"]["05-code-review"]
        node["planned"] = ["arch", "test"]
        node["routing"] = json.loads(json.dumps(self.ROUTED))
        node["mode"] = "fanout"
        st.save(paths, s)
        return run_id, paths

    def _submit(self, repo, paths, run_id, code, round_, findings, resolved=()):
        name = ("05_review_%s.json" % code if round_ == 1
                else "05_review_%s_r%d.json" % (code, round_))
        j = paths.run_dir / name
        j.write_text(json.dumps({
            "reviewer": code, "round": round_, "status": "ok",
            "by_checklist": {"전부": list(findings)},
            "resolved_from_previous": list(resolved),
            "need_more_context": []}, ensure_ascii=False), encoding="utf-8")
        body = "".join("## %s\n\n%s\n" % (f["severity"], f["quote"])
                       for f in findings)
        j.with_name(name.replace(".json", ".raw.md")).write_text(
            "# 리뷰\n\n" + body, encoding="utf-8")
        return cli.run_record(repo, "05", str(j), reviewer=code,
                              round_=round_, run_id=run_id)

    def test_에스컬레이션_뒤_3라운드도_델타_한_명이다(self, repo, request_file,
                                                    phases):
        run_id, paths = self._ready(repo, request_file, phases)
        self._submit(repo, paths, run_id, "arch", 1, [self.BLOCK])
        env = self._submit(repo, paths, run_id, "test", 1, [])
        assert env["exit"] == 4, env["render"]
        _, s = st.load(repo, run_id)
        node = s["phases"]["05-code-review"]
        assert node["rounds_planned"]["2"] == ["arch"], node["rounds_planned"]

        self._submit(repo, paths, run_id, "arch", 2, [self.BLOCK])
        _, s = st.load(repo, run_id)
        assert s.get("escalated"), "예산 2 를 다 썼으면 에스컬레이션이다"
        planned = (s["phases"]["05-code-review"].get("rounds_planned") or {}).get("3")
        assert planned == ["arch"], (
            "에스컬레이션이 델타를 안 세우면 3라운드가 전원 재팬아웃한다", planned)

    def test_델타_라운드의_패킷은_한_명만_이름_짓는다(self):
        s = {"counters": {"review_repair": {"used": 2, "max": 2}},
             "phases": {"05-code-review": {
                 "mode": "fanout", "planned": ["arch", "test"],
                 "rounds_planned": {"3": ["arch"]},
                 "routing": json.loads(json.dumps(self.ROUTED))}}}
        out = cli._review_render(s)
        assert "architecture-reviewer" in out, out
        assert "test-quality-reviewer" not in out, (
            "부르지 않을 리뷰어를 패킷이 이름 지으면 그 제출이 exit 8 로 튕긴다",
            out)

    def test_1라운드_패킷은_전원을_이름_짓는다(self):
        """좁히기는 델타 라운드에만 걸린다 — 1라운드는 키가 없어 전원 폴백이다."""
        s = {"counters": {},
             "phases": {"05-code-review": {
                 "mode": "fanout", "planned": ["arch", "test"],
                 "routing": json.loads(json.dumps(self.ROUTED))}}}
        out = cli._review_render(s)
        assert "architecture-reviewer" in out, out
        assert "test-quality-reviewer" in out, out


class TestEscalationMenuHasTheCommonAnswer:
    """미구현 백로그 30 — 가장 자주 나오는 답이 메뉴에 있어야 한다.

    05 수리 상한 초과의 선택지는 셋(계약 결함 의심 · 이대로 진행 · 중단)인데
    **클론 4런의 에스컬레이션 2런 모두 사람이 「기타」를 골랐고**, 그 답은 둘 다
    「좁게 보강하고 진행」 계열이었다. 메뉴가 실제 답을 담지 않으면 선택지는
    기록이 아니라 장식이다.

    `options` 는 표시 전용이다 — `state.escalate` 가 `ESCALATION.md` 와 상태에
    적을 뿐 분기하는 코드가 없다. 그래서 **문자열이 곧 전부**이고, 그 문자열이
    실제로 사람에게 간다는 것만 통합으로 한 번 확인한다.
    """

    NARROW = "좁게 보강하고 진행한다"

    def test_메뉴에_좁은_보강이_있다(self):
        assert any(self.NARROW in o for o in cli.REVIEW_ESCALATION_OPTIONS), \
            cli.REVIEW_ESCALATION_OPTIONS

    def test_에스컬레이션한_런이_그_메뉴를_받는다(self, repo, request_file, phases):
        """상수가 실제 에스컬레이션까지 간다 — 선언만 있고 안 쓰이는 것을 막는다."""
        peer = TestEscalationPlansTheDeltaRound()
        run_id, paths = peer._ready(repo, request_file, phases)
        peer._submit(repo, paths, run_id, "arch", 1, [peer.BLOCK])
        peer._submit(repo, paths, run_id, "test", 1, [])
        peer._submit(repo, paths, run_id, "arch", 2, [peer.BLOCK])
        _, s = st.load(repo, run_id)
        assert s.get("escalated"), s.get("run_status")
        assert s["escalation"]["options"] == list(cli.REVIEW_ESCALATION_OPTIONS), \
            s["escalation"]["options"]


class TestFormatRejectCount:
    """[[ADR-H052]] 결정 3 — 형식 반려(exit 8 재제출)를 이벤트로 세고 08 에 적는다.

    `e7ff` 의 sec 는 3라운드에서 accounting 형식 위반으로 두 번 튕겨 failed 로
    닫혔는데 원장에는 '실패' 로만 남았다. 계수 지점은 `run_record` 하나다 —
    핸들러가 exit 8 을 돌려주면 그 자리에서 `format_reject` 를 남긴다.
    """


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
                     "pr_opened", "run_closed"):
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


# ---------------------------------------------------------------------------
# N. approve — 승인은 이벤트다. 지문과 등급을 함께 못박는다
# ---------------------------------------------------------------------------


PR_NOTES = {"schema": 1,
            "flow": [{"step": "두 제목의 유사도를 잰다", "refs": ["matchTitle"]}],
            "verify": ["POST /api/analyze 에 두 제목을 보내 0~1 이 오는지 본다"]}


def _pr_notes(paths, notes=None):
    """06 흐름 노트 — 메인이 `pr` 전에 쓴다 (ADR-H058 추기)."""
    p = paths.run_dir / "06_pr_notes.json"
    p.write_text(json.dumps(PR_NOTES if notes is None else notes,
                            ensure_ascii=False), encoding="utf-8")
    return p


def _enter_06(repo, request_file, phases, grade="PASS"):
    """05 까지를 상태로 위조하고 06 에 세운다. **지문은 실물이다.**

    흐름 노트도 쓴다 — 없으면 첫 `pr` 이 exit 8 이다."""
    run_id, paths = _enter_05(repo, request_file, phases)
    _pr_notes(paths)
    _p, s = st.load(repo, run_id)
    st.set_phase_status(s, "05-code-review", "passed")
    s["phase"] = "06-pr"
    s["grade"] = grade
    s["review05"] = {"status": "ok", "reviewers_planned": 1, "reviewers_ok": 1,
                     "mode": "merged", "major": 0, "need_more_context": [],
                     "truncated": False}
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

    def _plan(self, paths, text):
        (paths.run_dir / "01_plan.md").write_text(text, encoding="utf-8")

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
        for sec in ("## 개요", "## 작업 내용", "## 참고사항", "## 체크리스트"):
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


class TestPr06BodyReadability:
    """가독성 개선 — 원문 사실은 그대로 두고 렌더링만 사람이 읽기 좋게 감싼다.

    05-code-review 의 findings 스키마가 아니라 여기(pr.py 의 조립부)가
    "기계적이다" 는 지적의 실제 출처였다 — 실제 PR
    (banana-island-ops#2)을 읽고 확인했다.
    """

    def _body(self, repo, paths, s):
        return pr_mod.build_body(repo, paths, s,
                                 harness._read_json(repo / harness.CONFIG_REL))

    def test_아는_gap_코드는_한글_설명이_붙는다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases,
                                  grade="PASS_WITH_GAPS")
        _pp, s = st.load(repo, run_id)
        s["gaps"] = ["review05:failed"]
        st.save(_pp, s)
        first = self._body(repo, paths, s).strip().splitlines()[0]
        assert "review05:failed (" in first, first

    def test_모르는_gap_코드는_라벨_없이_원래대로_나온다(self, repo, request_file,
                                                       phases):
        """표가 불완전해도 정보가 사라지면 안 된다 — 안전 폴백."""
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases,
                                  grade="PASS_WITH_GAPS")
        _pp, s = st.load(repo, run_id)
        s["gaps"] = ["내가_지어낸_코드"]
        st.save(_pp, s)
        first = self._body(repo, paths, s).strip().splitlines()[0]
        assert "내가_지어낸_코드" in first
        assert "내가_지어낸_코드 (" not in first

    def test_개요는_원본_요청_인용으로_시작한다(self, repo, request_file, phases):
        """플랜에서 아무것도 옮겨 적지 않는다 — 요청 원문만 인용한다."""
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        (paths.run_dir / "01_plan.md").write_text(
            "# 플랜" + chr(10) * 2 + "본문뿐이다." + chr(10), encoding="utf-8")
        body = self._body(repo, paths, s)
        lines = body.splitlines()
        i = lines.index("## 개요")
        assert lines[i + 2] == "**원본 요청**", body

    def test_작업_내용의_계약_원문이_details_로_접힌다(self, repo, request_file,
                                                    phases):
        """실물 06 은 `_refresh_contract` 가 `path` 를 싣는다 — 픽스처는 직접
        싣는다 (TestPr06ContractAfterDrop 의 `_with_path` 와 같은 이유)."""
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        s["contract"] = dict(s.get("contract") or {},
                             path="_workspace/contract_x.md")
        st.save(_pp, s)
        body = self._body(repo, paths, s)
        assert "<details>" in body and "</details>" in body, body
        assert (body.index("## 작업 내용") < body.index("<details>")
                < body.index("</details>") < body.index("## 참고사항")), body

    def test_계약이_없으면_details_로_감싸지_않는다(self, repo, request_file,
                                                 phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pp, s = st.load(repo, run_id)
        s["contract"] = {"mode": "no_contract", "present": False}
        body = self._body(repo, paths, s)
        assert "<details>" not in body, body
        assert "(no_contract)" in body, body


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


class TestLatestRunPicksEscalated:
    """재개 가능한 런이다 — 안 집으면 화면에서 사라진다."""

    def test_에스컬레이션된_런은_계속_집힌다(self, repo, request_file, phases):
        rid = cli.run_init(repo, "a", str(request_file))["run_id"]
        paths, s = st.load(repo, rid)
        st.escalate(paths, s, "사람이 정한다", ["가", "나"], phase="01-plan")
        st.save(paths, s)
        assert st.latest_run_id(repo) == rid


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
# P. 07-pr-review — /code-review 1회. 05 가 낸 키를 가리키지 않은 Major+ 만 escaped 다
# ---------------------------------------------------------------------------


def _enter_07(repo, request_file, phases, review05_status="ok", major=0):
    run_id, paths = _enter_06(repo, request_file, phases)
    _p, s = st.load(repo, run_id)
    st.set_phase_status(s, "06-pr", "passed")
    s["phase"] = "07-pr-review"
    s["pr"] = {"number": 231, "state": "open", "pushed": True, "head": "feat-x"}
    s["review05"] = dict(s["review05"], status=review05_status, major=major)
    st.save(_p, s)
    return run_id, paths


def _r07(paths, **kw):
    d = {"code_review": "done", "findings": []}
    d.update(kw)
    p = paths.run_dir / "07_pr_review.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return p


def _seed_05(repo, run_id, title="트랜잭션 경계가 없다", severity="major",
             reviewer="gen", round_="1", closed=False):
    """05 가 낸 지적 하나를 `phases.05-code-review.rounds` 에 심는다 — 07 의
    `dup_05` 가 대조하는 원천은 `05_review.json` 이 아니라 이것이다. 반환은 키."""
    _p, s = st.load(repo, run_id)
    f = {"id": "F-1", "category": "TX_BOUNDARY", "severity": severity,
         "target_role": "impl", "title": title, "quote": "x"}
    key = verdict_mod.finding_key(f)
    node = s.setdefault("phases", {}).setdefault("05-code-review", {})
    slot = node.setdefault("rounds", {}).setdefault(round_, {})
    slot[reviewer] = {"keys": [{"key": key, "id": "F-1", "severity": severity}],
                      "closed": [key] if closed else [], "blocking": 0,
                      "findings": [f]}
    st.save(_p, s)
    return key


def _major(title="새 결함", **kw):
    d = {"id": "G-1", "severity": "major", "title": title,
         "path": "src/lib/match.ts"}
    d.update(kw)
    return d


class TestRecord07:
    """07 은 `/code-review` 1회의 계수다 — 생략 조건도 수리 루프도 없다."""

    def test_깨끗하면_08_로_간다(self, repo, request_file, phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        env = cli.run_record(repo, "07", str(_r07(paths)), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        _p, s = st.load(repo, run_id)
        assert s["phase"] == "08-report"
        assert s["review07"] == {"code_review": "done", "skip_reason": None,
                                 "findings": 0, "dup_05": 0, "escaped": []}
        assert s["grade"] == "PASS"

    def test_skipped_인데_사유가_없으면_exit_8(self, repo, request_file, phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        env = cli.run_record(repo, "07", str(_r07(paths, code_review="skipped")),
                             run_id=run_id)
        assert env["exit"] == 8, env["render"]
        env = cli.run_record(repo, "07", str(_r07(paths, code_review="skipped",
                                                   skip_reason="  ")), run_id=run_id)
        assert env["exit"] == 8

    def test_skipped_는_사유가_있어도_gap_이다(self, repo, request_file, phases):
        """스킵은 통과가 아니다 — 05 가 놓친 것을 잴 표본이 이 런에는 없다."""
        run_id, paths = _enter_07(repo, request_file, phases)
        env = cli.run_record(repo, "07", str(_r07(
            paths, code_review="skipped", skip_reason="/code-review 가 불통이다")),
            run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        _p, s = st.load(repo, run_id)
        assert "pr_review_skipped" in s["gaps"]
        assert s["grade"] == "PASS_WITH_GAPS"
        assert s["review07"]["skip_reason"] == "/code-review 가 불통이다"

    def test_05_가_낸_키를_가리키면_dup_이고_escaped_가_아니다(self, repo, request_file,
                                                            phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        key = _seed_05(repo, run_id)
        f = _r07(paths, findings=[_major("트랜잭션 경계가 없다", finding_key=key)])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        _p, s = st.load(repo, run_id)
        assert s["review07"]["findings"] == 1 and s["review07"]["dup_05"] == 1
        assert s["review07"]["escaped"] == []
        assert "pr_review_open" not in s["gaps"] and s["grade"] == "PASS"

    def test_키_없는_Major_는_escaped_이고_등급이_내려간다(self, repo, request_file,
                                                        phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        _seed_05(repo, run_id)
        f = _r07(paths, findings=[_major(), _major("사소한 것", id="G-2",
                                                    severity="minor")])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        _p, s = st.load(repo, run_id)
        assert s["review07"]["escaped"] == [
            {"severity": "major", "title": "새 결함", "path": "src/lib/match.ts"}]
        assert s["review07"]["findings"] == 2 and s["review07"]["dup_05"] == 0
        assert "pr_review_open" in s["gaps"]
        assert s["grade"] == "PASS_WITH_GAPS"
        assert s["phase"] == "08-report", "07 은 멈추지 않는다 — 수리는 사람이 정한다"

    def test_05_목록_밖_키는_exit_8(self, repo, request_file, phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        _seed_05(repo, run_id)
        f = _r07(paths, findings=[_major(finding_key="b" * 16)])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] == 8, env["render"]
        assert "05" in env["render"]

    def test_델타_라운드_뒤에도_1회차_키가_남는다(self, repo, request_file, phases):
        """원천은 `rounds` 전부다 — `05_review.json` 은 마지막 라운드만 남긴다."""
        run_id, paths = _enter_07(repo, request_file, phases)
        key1 = _seed_05(repo, run_id, title="1회차 것", reviewer="gen", round_="1")
        key2 = _seed_05(repo, run_id, title="2회차 것", reviewer="data", round_="2")
        keys = {k["key"] for k in cli._keys_from_05(st.load(repo, run_id)[1])}
        assert keys == {key1, key2}
        f = _r07(paths, findings=[_major("1회차 것", finding_key=key1)])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]

    def test_severity_어휘_밖은_exit_8(self, repo, request_file, phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        f = _r07(paths, findings=[_major(severity="치명")])
        env = cli.run_record(repo, "07", str(f), run_id=run_id)
        assert env["exit"] == 8

    def test_PR_이_머지됐으면_아무것도_안_하고_끝낸다(self, repo, request_file,
                                                     phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        s["pr"]["state"] = "merged"
        st.save(_p, s)
        env = cli.run_record(repo, "07", str(_r07(paths)), run_id=run_id)
        assert env["exit"] in (0, 11)
        _p, s = st.load(repo, run_id)
        assert "pr_merged" in s["gaps"]
        assert "pr_review_skipped" not in s["gaps"]
        assert s["review07"]["skip_reason"] == "pr_merged"

    def test_07_패킷은_05_의_키_목록을_주고_code_review_를_한_번_센다(
            self, repo, request_file, phases):
        run_id, paths = _enter_07(repo, request_file, phases)
        key = _seed_05(repo, run_id, closed=True)
        env = cli.run_next(repo, run_id=run_id)
        assert env["exit"] == 0, env["render"]
        assert key in env["render"] and "닫힘" in env["render"], env["render"]
        assert "record --phase 07" in env["next_command"]
        cli.run_next(repo, run_id=run_id)          # 같은 키는 두 번 세지 않는다
        _p, s = st.load(repo, run_id)
        assert s["budget"]["model_calls"]["by_phase"]["07-pr-review"] == 1
        assert "07:code-review" in s["budget"]["model_calls"]["counted"]
# ---------------------------------------------------------------------------
# S. 08-report — 재지 못한 것이 조용히 통과하지 않는다
# ---------------------------------------------------------------------------

import report as rep_mod  # noqa: E402


def _enter_08(repo, request_file, phases, grade="PASS"):
    run_id, paths = _enter_07(repo, request_file, phases)
    _p, s = st.load(repo, run_id)
    st.set_phase_status(s, "07-pr-review", "passed")
    s["phase"] = "08-report"
    s["grade"] = grade
    s["review07"] = {"code_review": "done", "skip_reason": None,
                     "findings": 0, "dup_05": 0, "escaped": []}
    st.save(_p, s)
    return run_id, paths


def _seed_timing_events(paths):
    """실물 런의 모양을 심는다 — `_enter_08` 은 상태만 조립하고 이벤트를 안 남긴다.

    P8 이 실제로 그린 궤적을 줄인 것이다: 01 이 한 번 돌고, 되돌아가고,
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
    st.append_event(paths, "phase_enter", phase="03-implement", now=at(11, 20))
    # 되돌아간 01 에 phase_enter 가 안 찍히는 것이 실물이다.
    st.append_event(paths, "submit_received", phase="01-plan", now=at(11, 30))
    st.append_event(paths, "phase_pass", phase="01-plan", now=at(11, 50))
    st.append_event(paths, "phase_enter", phase="08-report", now=at(12, 0))


LONG_LESSON = ("AC 가 증상을 잠가야 한다 — 재시도 버튼이 두 번째 클릭에서 "
               "죽는 것은 상태 머신의 전이가 아니라 리듀서의 초기화 순서였고, "
               "그것을 테스트가 먼저 잠갔어야 했다. 다음 런은 증상을 AC 로 적는다.")
LONG_NEXT = ("다음 런에서는 계약의 유닛 절에 실패 경로를 먼저 적고, 리듀서의 "
             "초기화 순서를 잠그는 테스트를 03 이 먼저 쓰게 한다. 05 의 test "
             "리뷰어가 빠지지 않도록 라우팅을 확인한다.")
LONG_GAPS = ("계약의 유닛 절에 재시도 버튼의 두 번째 클릭 경로가 없었다 — 상태 머신의 "
             "전이만 적고 리듀서 초기화 순서는 적지 않아 테스트가 그 자리를 잠그지 못했다.")


def _report_data(paths, **kw):
    d = {"narrative": {"문제": LONG_LESSON, "원인": LONG_LESSON, "해결": LONG_LESSON,
                       "결과": "통과", "배운 점": LONG_LESSON},
         "contract_gaps": LONG_GAPS, "next_run": LONG_NEXT}
    d.update(kw)
    p = paths.run_dir / "08_report_data.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return p


class TestModelsReported:
    """[[ADR-H052]] 결정 2 — 리뷰어의 `model_used` 자진신고(선택). 기준은
    `instructed+reported` 이고, 자진신고는 실측이 아니라는 사각을 같이 적는다."""

    def test_기준이_둘을_말한다(self):
        assert st.MODELS_BASIS == "instructed+reported"

    def test_자진신고가_상태에_쌓인다(self, repo, request_file):
        _, s = st.create_run(repo, "demo", request_file)
        st.note_model_reported(s, "05:r1:arch", "claude-sonnet-5")
        assert s["models"]["reported"] == {"05:r1:arch": "claude-sonnet-5"}
        cell = rep_mod._models_cell(s)
        assert cell and "claude-sonnet-5" in cell, cell
        assert "자진신고" in cell, cell

    def test_05_제출의_model_used_가_슬롯과_상태에_남는다(self, repo, request_file,
                                                        phases):
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
        j = paths.run_dir / "05_review_arch.json"
        j.write_text(json.dumps({
            "reviewer": "arch", "round": 1, "status": "ok",
            "model_used": "claude-opus-5",
            "by_checklist": {"전부": []}, "resolved_from_previous": [],
            "need_more_context": []}, ensure_ascii=False), encoding="utf-8")
        j.with_name("05_review_arch.raw.md").write_text("# 리뷰\n", encoding="utf-8")
        env = cli.run_record(repo, "05", str(j), reviewer="arch", round_=1,
                             run_id=run_id)
        assert env["exit"] != 8, env["render"]
        _, after = st.load(repo, run_id)
        rounds = after["phases"]["05-code-review"]["rounds"]["1"]
        assert rounds["arch"]["model_used"] == "claude-opus-5", rounds
        assert after["models"]["reported"]["05:r1:arch"] == "claude-opus-5"

    def test_문자열이_아니면_거부한다(self, repo):
        got = rv.check(repo, _config(repo), _sub(model_used=5), RAW_ONE, [])
        assert got["exit"] == 8
        assert any("model_used" in e for e in got["errors"]), got["errors"]


class TestReport08NarrativeMinChars:
    """서술 4절(문제·원인·해결·계약이 어디서 부족했는가)은 80자 미만이면 되묻는다.

    `5568`(FR-009) 의 08 은 서술이 통째로 비었고 그 런은 07 major 6건으로
    최다였다 — 「왜 그랬는가는 이 런이 말하지 않았다」 (ADR-H052 결정 5).
    **등급은 건드리지 않는다.** 보고서 파일은 쓰되 런을 닫지 않고 같은 명령을
    다시 청한다. 이미 닫힌 런의 재작성은 되묻지 않는다 — 전이는 한 번뿐이다.
    """

    def test_79자면_exit_8_이고_등급은_그대로다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths, narrative={"문제": "가" * 79, "원인": LONG_LESSON,
                                       "해결": LONG_LESSON})
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 8, env["render"]
        assert env["data"]["closed"] is False
        assert "문제" in env["render"] and "80" in env["render"], env["render"]
        assert env["next_command"] and "report" in env["next_command"]
        _p, s = st.load(repo, run_id)
        assert s["grade"] == "PASS", "등급 X"
        assert s["run_status"] != st.DONE
        kinds = [e for e in st.read_events(paths) if e["kind"] == "check_fail"
                 and e["data"].get("short_narrative")]
        assert kinds, "되물은 사실이 기록에 남는다"

    def test_contract_gaps_도_같은_규칙이다(self, repo, request_file, phases):
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths, contract_gaps="짧다")
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 8, env["render"]
        assert "contract_gaps" in env["render"]

    def test_배운_점과_next_run_은_비어도_닫힌다(self, repo, request_file, phases):
        """네 절 밖은 선택이다 — 있으면 렌더하고 없어도 되묻지 않는다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths, narrative={"문제": LONG_LESSON, "원인": LONG_LESSON,
                                       "해결": LONG_LESSON}, next_run="")
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 11, env["render"]

    def test_short_narrative_는_순수_함수다(self):
        full = {"narrative": {"문제": "x" * 80, "원인": "x" * 80, "해결": "x" * 80},
                "contract_gaps": "y" * 80}
        assert rep_mod.short_narrative(full) == []
        got = rep_mod.short_narrative(dict(full, contract_gaps="y"))
        assert [k for k, _n in got] == ["contract_gaps"], got
        assert rep_mod.NARRATIVE_MIN_CHARS == 80
        assert rep_mod.NARRATIVE_REQUIRED == (
            ("narrative", "문제"), ("narrative", "원인"), ("narrative", "해결"),
            ("contract_gaps",))


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

    def test_07_passed_뒤_report_가_런을_닫는다(self, repo, request_file, phases):
        """07 record(done · 0건) → 08 requires 충족 → report 가 exit 11 로 닫는다."""
        run_id, paths = _enter_07(repo, request_file, phases)
        env = cli.run_record(repo, "07", str(_r07(paths)), run_id=run_id)
        assert env["exit"] in (0, 11), env["render"]
        _report_data(paths)
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 11, env["render"]
        _p, s = st.load(repo, run_id)
        assert s["run_status"] == st.DONE

    def test_페이즈별_호출과_07_escaped_가_보고서에_있다(self, repo, request_file,
                                                     phases):
        """05·07 의 비용을 나란히 보는 자리다. escaped 는 05 가 낸 키를 가리키지
        않은 Major+ 이고, 제목이 보고서에 남아야 사람이 정할 수 있다."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        s.setdefault("budget", {}).setdefault("model_calls", {})["by_phase"] = {
            "05-code-review": 3, "07-pr-review": 1}
        s["review07"] = {"code_review": "done", "skip_reason": None,
                         "findings": 2, "dup_05": 1,
                         "escaped": [{"severity": "major", "title": "새 결함",
                                      "path": "src/x.ts"}]}
        st.save(_p, s)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "05-code-review: 3" in out and "07-pr-review: 1" in out, out
        assert "07 escaped" in out and "새 결함" in out, out

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

    def test_보고서는_파이프라인을_실패시키지_않는다(self, repo, request_file,
                                                   phases):
        """섹션이 비어도 **산출은 된다** — 원장에 기록만 한다. 다만 서술이
        짧으면 봉투가 되묻고(exit 8) 런은 그때 닫히지 않는다 (ADR-H052)."""
        run_id, paths = _enter_08(repo, request_file, phases)
        _report_data(paths, narrative={}, next_run="")
        env = cli.run_report(repo, run_id=run_id)
        assert env["exit"] == 8, env["render"]
        assert env["data"]["closed"] is False
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id))
        assert out.exists(), "보고서 파일은 나온다"

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
        s["gaps"] = ["stage_absent:e2e", "stage_na:docs"]
        st.save(_p, s)
        _report_data(paths)
        cli.run_report(repo, run_id=run_id)
        out = (repo / "docs" / "harness" / "pipeline" / "runs"
               / ("%s.md" % run_id)).read_text(encoding="utf-8")
        assert "stage_absent:e2e" in out
        assert "stage_na:docs" in out

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
        text, _missing = rep_mod.build(s, {}, None)
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


class TestGapVocabulary:
    """코어가 만드는 gap 사유는 **전부** `GAP_REASONS` 안이다 (ADR-H050).

    파일럿 40dc(FR-014) 의 08 보고서가 `stage_no_selector:scoped` 를
    "어휘에 없는 사유다 (보고서가 설명하지 못한다)" 로 적었다 — gate.py 가
    만드는 사유 넷(`stage_no_selector` · `scoped_degenerate` ·
    `test_report_missing` · `tests_ran_zero`)이 어휘에
    없었다. 08-report.md 도 "gaps[] 의 어휘가 열거형으로 정의돼 있지 않다" 를
    명시적 미규정으로 적어 뒀다. 이 테스트가 그 열거를 강제한다 — 새 gap 을
    만드는 코드는 여기서 먼저 깨진다.
    """

    # `%s` 로 조립되는 머리는 코드에서 정적으로 못 읽는다 — 가능한 꼬리를 여기
    # 선언한다. gate.py 의 `stage_%s` 는 adapters/gate 의 skip reason 셋이고,
    # cli.py 의 `pr_%s` 는 pr.py 의 PR 상태 둘이다.
    DYNAMIC = {"stage_%s": ("absent", "not_touched", "no_selector"),
               "pr_%s": ("closed", "merged")}
    # gap 코드는 전부 밑줄 또는 콜론을 담는다 (`GAP_REASONS` 의 키가 그렇다).
    # 같은 줄의 `probe.get("name")` 같은 키 이름은 그
    # 둘이 없어 걸러진다 — 허용 목록을 손으로 늘리는 대신 형으로 가른다.
    LOOKS_LIKE_GAP = re.compile(r"[_:]")

    def _emitted(self):
        pat_call = re.compile(r"gaps\.append\(|demote\(")
        pat_lit = re.compile(r'"([a-z0-9_%]+(?::[a-z0-9_%]+)?)"')
        lits = set()
        for py in sorted((_SCRIPTS / "pipeline").glob("*.py")):
            for line in py.read_text(encoding="utf-8").splitlines():
                if not pat_call.search(line):
                    continue
                lits.update(pat_lit.findall(line))
        return lits

    def test_코어가_만드는_사유가_전부_어휘_안이다(self):
        lits = self._emitted()
        assert lits, "스캔이 아무것도 못 찾았다 — 정규식을 확인한다"
        for lit in sorted(lits):
            head = lit.split(":")[0]
            if not self.LOOKS_LIKE_GAP.search(lit):
                continue
            if head in self.DYNAMIC:
                for tail in self.DYNAMIC[head]:
                    full = head.replace("%s", tail)
                    assert full in rep_mod.GAP_REASONS, (head, tail)
                continue
            assert "어휘에 없는 사유" not in rep_mod.explain_gap(
                lit.replace("%s", "x")), lit

    def test_gate_가_만드는_네_사유가_설명된다(self):
        for gap in ("stage_no_selector:scoped", "scoped_degenerate",
                    "test_report_missing", "tests_ran_zero"):
            assert "어휘에 없는 사유" not in rep_mod.explain_gap(gap), gap


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


class TestHorizonRender:

    def test_다음이_없으면_런_완료라고_말한다(self):
        got = cli._horizon_render(None)
        assert "런 완료" in got

    def test_범위를_문자열로_박지_않고_페이즈에서_유도한다(self):
        loaded, _broken = cli.load_phases(ROOT)
        got = cli._horizon_render("09-nope", loaded)
        assert "01-plan" in got and "08-report" in got
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

    01 은 이제 `plan` 리뷰어 단독이라, 두 리뷰어가 동시에 있어야만 구성되던
    시나리오(누가 critical 을 냈는지로 다음 라운드 대상을 가르는 것)는 하나뿐인
    리뷰어에서는 항상 자명해 뺐다.
    """

    def test_minor_alone_converges_in_round_one(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review("plan", findings=[_minor()]))
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
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"
        keys = after["phases"]["01-plan"]["rounds"]["1"]["plan"]["keys"]
        assert [k["severity"] for k in keys] == ["major"], "지적은 사라지지 않는다"

    def test_critical_forces_a_second_round(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        assert env["exit"] == 0
        assert env["data"]["round"] == 2, env["data"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") != "passed"

    def test_new_minor_in_round_two_does_not_block(self, run01):
        """P2 의 모양 — Critical 을 닫았는데 제목이 다른 Minor 가 새로 나왔다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        env = _submit_review(
            repo, paths,
            _review("plan", round_=2, findings=[_minor(title="전혀 다른 제목")]),
            round_=2)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"

    def test_delta_round_still_names_the_sole_reviewer(self, run01):
        """01 리뷰어가 하나뿐이라 델타 선택은 늘 자명하다 — 그래도 선언대로
        `plan` 을 이름으로 낸다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        assert env["data"]["planned"] == ["plan"], env["data"]
        assert "--reviewer plan" in env["next_command"], env["next_command"]
        _, mid = st.load(repo, paths.run_id)
        assert mid["phases"]["01-plan"]["rounds_planned"]["2"] == ["plan"]

    def test_delta_reviewer_alone_closes_the_round(self, run01):
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        env = _submit_review(repo, paths, _review("plan", round_=2), round_=2)
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") == "passed"
        assert set(after["phases"]["01-plan"]["rounds"]["2"]) == {"plan"}

    def test_a_second_round_counts_the_model_calls_it_instructs(self, run01):
        """01 의 루프는 record → record 라 `next` 의 계수를 지나치지 않았다."""
        repo, paths, s = run01
        _submit_plan(repo, paths, _plan())
        before = st.load(repo, paths.run_id)[1]["budget"]["model_calls"]["total"]
        _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        _, after = st.load(repo, paths.run_id)
        assert after["budget"]["model_calls"]["total"] == before + 1, \
            after["budget"]["model_calls"]
        assert "01:r1:plan" in after["budget"]["model_calls"]["counted"]

    def test_an_exhausted_budget_stops_the_second_round(self, run01):
        repo, paths, s = run01
        s["budget"]["model_calls"]["max"] = 1
        st.save(paths, s)
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review("plan", findings=[_crit()]))
        assert env["exit"] == 5, env["render"]

    def test_the_normal_cap_is_two(self, run01):
        """5 → 3 (ADR-H041) → 2 (덜어내기 Wave 3 — 리뷰어가 리포를 읽으므로 1~2라운드). 값의 회귀 방지다."""
        repo, paths, s = run01
        front = _front(_phase_file(repo, "01-plan.md"))
        assert front["converge"]["max_by_profile"] == {"fix": 1, "normal": 2}
        assert front["loop"]["max_by_profile"] == front["converge"]["max_by_profile"]

    def test_missing_blocking_severities_is_exit_2(self, run01):
        repo, paths, s = run01
        _rewrite(_phase_file(repo, "01-plan.md"),
                 lambda f: f["converge"].pop("blocking_severities"))
        _submit_plan(repo, paths, _plan())
        env = _submit_review(repo, paths, _review("plan"))
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
        _, after = st.load(repo, paths.run_id)
        assert st.phase_status(after, "01-plan") != "passed"


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
# J. 레인 — `init --profile` 의 선언으로 01 부터 간다 (ADR-H044 · 덜어내기 Wave 3)
# ---------------------------------------------------------------------------

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


class TestInitLane:
    """레인은 `init --profile` 의 사용자 선언이다 — 없으면 `normal`, 첫 페이즈는 01."""

    def test_init_starts_at_01_with_the_default_lane(self, repo, phases):
        paths, s = _init(repo, SOURCE_REQUEST)
        assert s["phase"] == "01-plan"
        assert s["profile"] == {"name": "normal", "source": "default"}
        assert s["contract"]["mode"] == "contract"
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 0 and env["phase"] == "01-plan", env["render"]
        assert "prescan" in env["data"], "런 전체의 사전 검사는 첫 페이즈에서 한 번이다"
        _, after = st.load(repo, paths.run_id)
        assert "01:r0:plan" in after["budget"]["model_calls"]["counted"]

    def test_a_docs_declaration_means_no_contract_and_no_reviewer(self, repo, phases):
        paths, s = _init(repo, DOCS_REQUEST, profile="docs")
        assert s["profile"] == {"name": "docs", "source": "user"}
        assert s["contract"]["mode"] == "no_contract"
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["phase"] == "01-plan" and "리뷰어 — 0명" in env["render"], env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["budget"]["model_calls"]["total"] == 0

    def test_the_cli_accepts_three_lanes_only(self, repo, phases):
        req = repo / "_workspace" / "requests" / "x.md"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text(REQUEST_TEXT, encoding="utf-8")
        out = _run_cli(repo, "init", "--feature", "demo", "--request-file", str(req),
                       "--profile", "docs")
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["run_id"]
        bad = _run_cli(repo, "init", "--feature", "demo2", "--request-file", str(req),
                       "--profile", "small")
        assert bad.returncode != 0, "small 레인은 사라졌다"


class TestDocsLane:
    """docs 레인 — 01 리뷰어 0 → 03 역할 0, 모델 호출 0."""

    def _at_01(self, repo):
        paths, s = _init(repo, DOCS_REQUEST, profile="docs")
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["phase"] == "01-plan", env["render"]
        return paths

    def test_plan_submission_alone_closes_01_for_docs(self, repo, phases):
        paths = self._at_01(repo)
        env = _submit_plan(repo, paths, _plan())
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["phases"]["01-plan"]["converged_at_round"] == 1
        assert after["phase"] == "03-implement", after["phase"]
        assert "역할 — 0명" in env["render"], env["render"]
        assert after["profile"]["applied"] == ["01:reviewers=0"]
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

    def test_touching_source_in_the_docs_lane_is_a_lane_miss(self, repo, phases):
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
        assert prof["lane_miss"]["at"] == "03-implement"
        assert "predicted" not in prof
        assert after["contract"]["mode"] == "contract"
        assert after["grade"] == "PASS_WITH_GAPS"
        assert "lane_miss:01:reviewers=0;03:roles=0" in after["gaps"]
        kinds = [e["kind"] for e in st.read_events(paths)]
        assert "lane_miss" in kinds
        # 다음 `next` 는 계약을 요구한다 — 역할 패킷은 그 뒤다.
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 3, env["render"]


class TestModelTierRouting:
    """등급은 봉투가 정한다 — 지시이지 실측이 아니다."""

    # 슬롯 × 레인 전체. 작성자(roles)는 싸게, 검사자(plan · reviewers)는
    # normal 에서 opus — 계약이 작성자의 자유도를 묶었고 검사자의 내용 품질은
    # 기계가 못 잰다 (ADR-H061). `inherit` 는 표에 없다.
    EXPECTED_TIERS = {
        "01:r0:plan":     {"docs": "sonnet", "fix": "sonnet", "small": "sonnet", "normal": "opus"},
        "03:r0:impl":     {"docs": "sonnet", "fix": "sonnet", "small": "sonnet", "normal": "sonnet"},
        "04:r1:impl":     {"docs": "sonnet", "fix": "sonnet", "small": "sonnet", "normal": "sonnet"},
        "05:r1:gen":      {"docs": "sonnet", "fix": "sonnet", "small": "sonnet", "normal": "opus"},
        "05:r1:repair:impl": {"docs": "sonnet", "fix": "sonnet", "small": "sonnet", "normal": "sonnet"},
    }

    def test_slot_and_profile_fallback(self):
        cfg = _cfg()
        for key, by_lane in self.EXPECTED_TIERS.items():
            for lane, tier in by_lane.items():
                assert cli._model_for(cfg, key, lane) == tier, (key, lane)
                assert cli._model_for(cfg, key, lane) != "inherit", (key, lane)
        assert cli._model_for(cfg, "07:code-review", "normal") is None
        cfg.pop("models")
        assert cli._model_for(cfg, "01:r0:plan", "normal") is None

    def test_the_profile_template_declares_the_same_tiers(self):
        tmpl = json.loads((ROOT / "harness" / "profiles" / "nextjs-ts" / "config.json")
                          .read_text(encoding="utf-8"))
        assert tmpl["models"] == _cfg()["models"]

    def test_the_01_packet_names_the_tiers_and_state_records_them(self, repo, phases):
        paths, s = _init(repo, SOURCE_REQUEST)
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["phase"] == "01-plan", env["render"]
        assert "## 모델 등급" in env["render"]
        assert "`01:r0:plan` → model: `opus`" in env["render"], env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["models"]["instructed"] == {"01:r0:plan": "opus"}
        assert after["models"]["basis"] == st.MODELS_BASIS
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
        text, _missing = rep_mod.build(after, {})
        assert "| 지시된 모델 등급" in text and "opus: 1" in text, text

    def test_lint_warns_when_an_agent_file_pins_a_model(self, repo, phases):
        (repo / ".claude" / "agents" / "impl-writer.md").write_text(
            "---\nname: impl-writer\nmodel: opus\n---\n# x\n", encoding="utf-8")
        got = [f for f in _lint(repo) if f["rule"] == "agent_model"]
        assert got and got[0]["status"] == "WARN", got
        assert _fails(_lint(repo)) == []

    # effort 는 Agent 호출 인자로 못 넘긴다 — 에이전트 프론트매터가 유일한
    # 자리이고 역할 종류별로 정적이다 (ADR-H061). 모델 등급과 달리 봉투 출처가
    # 없어 두 출처 문제가 없으므로 WARN 대상이 아니다.
    EXPECTED_EFFORT = {"impl-writer": "medium", "test-writer": "high",
                       "ui-writer": "medium", "plan-reviewer": "high"}

    def test_agent_files_pin_effort_per_role(self):
        for agent, want in self.EXPECTED_EFFORT.items():
            text = (ROOT / ".claude" / "agents" / ("%s.md" % agent)).read_text(encoding="utf-8")
            front = text.split("---", 2)[1]
            got = [ln.split(":", 1)[1].strip() for ln in front.splitlines()
                   if ln.strip().startswith("effort:")]
            assert got == [want], (agent, got)
            assert want in ("low", "medium", "high", "xhigh", "max")
            assert not any(ln.strip().startswith("model:") for ln in front.splitlines()), agent

    def test_lint_does_not_warn_on_effort_alone(self, repo, phases):
        (repo / ".claude" / "agents" / "impl-writer.md").write_text(
            "---\nname: impl-writer\neffort: medium\n---\n# x\n", encoding="utf-8")
        assert [f for f in _lint(repo) if f["rule"] == "agent_model"] == []


class TestConditionLint:

    def test_an_unparseable_unless_is_rejected(self, repo, phases):
        _rewrite(phases / "01-plan.md",
                 lambda f: f["review"].__setitem__("unless", "profile is docs"))
        assert _fails(_lint(repo), "review_unless"), _lint(repo)
        _rewrite(phases / "03-implement.md",
                 lambda f: f["allow"].__setitem__("unless", "x"))
        assert _fails(_lint(repo), "allow_unless"), _lint(repo)

    def test_index_zero_lints_clean_and_sorts_first(self, repo, phases):
        """`index: 0` 은 거짓값이지만 유효한 인덱스다 — falsy 로 다루면 첫 페이즈가 사라진다."""
        _rewrite(phases / "01-plan.md", lambda f: f.__setitem__("index", 0))
        assert _fails(_lint(repo)) == []
        loaded, _ = cli.load_phases(repo)
        assert sorted(loaded)[0] == "01-plan"
        assert loaded["01-plan"]["front"]["index"] == 0


# ---------------------------------------------------------------------------
# G. e2e 준비 — 계약 `## 여정` (ADR-H058 추기)
# ---------------------------------------------------------------------------

JOURNEY_CONTRACT = TESTS_REQUIRED_CONTRACT.replace(" [admin]", "") + """
## 여정

- `e2e/analyze.spec.ts · analyzeJourney`
  - POST /api/analyze
"""


def _journey_contract(steps="POST /api/analyze"):
    return JOURNEY_CONTRACT.replace("  - POST /api/analyze", "  - " + steps)


def _set_e2e(repo, stage):
    a = repo / "harness" / "adapters" / "nextjs-ts.json"
    data = json.loads(a.read_text(encoding="utf-8"))
    data["stages"]["e2e"] = stage
    a.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _journey_spec(repo, body):
    p = repo / "e2e" / "analyze.spec.ts"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


class TestContractJourneys:
    """계약 `## 여정` 파서 — 유닛과 같은 형식에 들여쓴 줄이 진입점 순서다."""

    def _parse(self, repo, text):
        cfg = json.loads((repo / "harness" / "config.json").read_text(encoding="utf-8"))
        return contract_mod.parse(text, cfg)

    def test_여정과_단계를_읽는다(self, repo):
        doc = ("## 여정\n\n- `e2e/a.spec.ts · aJourney`\n"
               "  - `POST /api/a` → GET /api/a/[id] -> `DELETE /api/a/[id]`\n")
        j = self._parse(repo, doc)["journeys"]
        assert [(x["container"], x["symbol"]) for x in j] == [("e2e/a.spec.ts", "aJourney")]
        assert j[0]["steps"] == [{"method": "POST", "path": "/api/a"},
                                 {"method": "GET", "path": "/api/a/[id]"},
                                 {"method": "DELETE", "path": "/api/a/[id]"}]

    def test_템플릿의_기본_본문은_여정이_없다(self, repo):
        text = (repo / "harness" / "templates" / "contract.md").read_text(encoding="utf-8")
        p = self._parse(repo, text)
        assert "## 여정" in text
        assert p["journeys"] == [] and p["journeys_dropped"] == []

    def test_여정_슬러그는_계약이_이름_붙인_것이다(self, repo):
        p = self._parse(repo, JOURNEY_CONTRACT)
        assert "analyzeJourney" in contract_mod.symbols(p)

    def test_문제_목록(self, repo):
        ok = self._parse(repo, JOURNEY_CONTRACT)
        assert contract_mod.journey_problems(ok) == []
        for steps in ("GET /api/analyze", "POST /api/other", "없음"):
            p = self._parse(repo, _journey_contract(steps))
            assert contract_mod.journey_problems(p), steps
        bad = self._parse(repo, JOURNEY_CONTRACT.replace(
            "`e2e/analyze.spec.ts · analyzeJourney`", "`0`"))
        assert bad["journeys_dropped"] and contract_mod.journey_problems(bad)


class TestContract03Journeys:
    """**러너 없는 여정은 디스패치 전에 거부한다** — 워커가 스펙을 쓴 뒤가 아니라."""

    def _enter(self, repo, request_file, monkeypatch, text=JOURNEY_CONTRACT):
        monkeypatch.setattr(adapters, "run_stage",
                            lambda *a, **k: {"id": "compile", "state": "ran",
                                             "exit": 0, "sec": 0.1})
        return TestRecord03ContractUnitsZero()._enter_03(repo, request_file, text)

    def _total(self, repo, run_id):
        _p, s = st.load(repo, run_id)
        return st._budget_node(s).get("total", 0)

    def test_e2e_가_없으면_next_가_exit_8_이고_지시를_세지_않는다(
            self, repo, request_file, phases, monkeypatch):
        run_id, _paths, _c = self._enter(repo, request_file, monkeypatch)
        before = self._total(repo, run_id)
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 8, env["render"]
        assert "e2e" in env["render"] and "ADR" in env["render"]
        assert "next" in (env["next_command"] or "")
        assert self._total(repo, run_id) == before

    def test_해당_없음_스택은_그렇게_말한다(self, repo, request_file, phases,
                                           monkeypatch):
        _set_e2e(repo, {"cmd": None, "not_applicable": "브라우저가 없다."})
        run_id, _paths, _c = self._enter(repo, request_file, monkeypatch)
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 8 and "해당 없음" in env["render"], env["render"]

    def test_이미_쓴_스펙은_지우라고_한다(self, repo, request_file, phases,
                                         monkeypatch):
        run_id, _paths, _c = self._enter(repo, request_file, monkeypatch)
        _journey_spec(repo, "describe('analyzeJourney', () => {})\n")
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 8 and "e2e/analyze.spec.ts" in env["render"], env["render"]

    def test_진입점에_없는_단계는_거부한다(self, repo, request_file, phases,
                                        monkeypatch):
        _set_e2e(repo, {"cmd": ["run", "e2e"]})
        for steps in ("GET /api/analyze", "POST /api/other"):
            run_id, _paths, _c = self._enter(repo, request_file, monkeypatch,
                                             _journey_contract(steps))
            env = cli.run_next(repo, run_id)
            assert env["exit"] == 8, (steps, env["render"])
            assert steps in env["render"], env["render"]

    def test_record_03_이_백스톱이다(self, repo, request_file, phases, monkeypatch):
        run_id, _paths, claims = self._enter(repo, request_file, monkeypatch)
        env = cli.run_record(repo, "03", str(claims), run_id=run_id)
        assert env["exit"] == 8 and "여정" in env["render"], env["render"]
        _p, s = st.load(repo, run_id)
        assert st.phase_status(s, "03-implement") != "passed"

    def test_러너가_있으면_패킷에_여정_행이_있다(self, repo, request_file, phases,
                                               monkeypatch):
        _set_e2e(repo, {"cmd": ["run", "e2e"]})
        run_id, _paths, _c = self._enter(repo, request_file, monkeypatch)
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 0, env["render"]
        line = next(l for l in env["render"].splitlines() if "analyzeJourney" in l)
        assert "e2e/analyze.spec.ts" in line

    def test_여정이_없는_계약은_영향이_없다(self, repo, request_file, phases,
                                          monkeypatch):
        run_id, _paths, _c = self._enter(repo, request_file, monkeypatch,
                                         TESTS_REQUIRED_CONTRACT)
        assert cli.run_next(repo, run_id)["exit"] == 0


class TestContractTraceJourneys:
    """`missing_journey_spec` — 스펙이 실재하고 슬러그가 **선언**으로 있는가."""

    def _got(self, repo):
        _route(repo, "analyze", "expect(body.code).toBe('MATCH_EMPTY')\n")
        return _codes(_trace(repo, _write_contract(repo, JOURNEY_CONTRACT),
                             changed=[]), "missing_journey_spec")

    def test_스펙이_없으면_critical(self, repo):
        got = self._got(repo)
        assert got and got[0]["severity"] == "critical"
        assert got[0]["target_role"] == "test"

    def test_주석에만_있으면_지적한다(self, repo):
        _journey_spec(repo, "// analyzeJourney\n")
        assert self._got(repo)

    def test_describe_문자열이나_export_const_면_통과한다(self, repo):
        for body in ("test.describe('analyzeJourney', () => {})\n",
                     'describe("analyzeJourney", () => {})\n',
                     "export const analyzeJourney = 1\n"):
            _journey_spec(repo, body)
            assert self._got(repo) == [], body

    def test_03_도_같은_검사를_센다(self, repo):
        config, adapter = _load(repo)
        p = _write_contract(repo, JOURNEY_CONTRACT)
        _route(repo, "analyze", "expect(body.code).toBe('MATCH_EMPTY')\n")
        req = tr.required_tests(repo, config, adapter, p)
        assert [f["code"] for f in req["findings"]] == ["missing_journey_spec"]

    def test_오류_상수가_e2e_스펙에만_있으면_지적한다(self, repo):
        """e2e 가 화면 문구로 상수를 단언해도 유닛 테스트 부재를 가리지 않는다."""
        _route(repo, "analyze", "expect(res.status).toBe(200)\n")
        _journey_spec(repo, "describe('analyzeJourney', () => {})\n// MATCH_EMPTY\n")
        got = _trace(repo, _write_contract(repo, JOURNEY_CONTRACT), changed=[])
        assert _codes(got, "untested_error_symbol"), got["findings"]

    def test_유닛_심볼이_e2e_스펙에만_있으면_지적한다(self, repo):
        _journey_spec(repo, "describe('analyzeJourney', () => {})\n// matchTitle\n")
        (repo / "src" / "lib" / "match.test.ts").write_text("// 없음\n", encoding="utf-8")
        got = _trace(repo, _write_contract(repo, JOURNEY_CONTRACT), changed=[])
        assert _codes(got, "untested_contract_item"), got["findings"]


class TestJourneyOwnership:

    def test_e2e_디렉터리는_test_소유다(self, repo, config):
        for rel in ("e2e/x.spec.ts", "e2e/fixtures/x.ts"):
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("// x\n", encoding="utf-8")
        got = attr.clean_ownership(repo, config, _claims(
            test=["e2e/x.spec.ts", "e2e/fixtures/x.ts"]))
        assert got["ok"], got["message"]


class TestJourneyHint:
    """계약을 쓰라는 봉투가 e2e 부재를 말한다 — 메인이 어댑터를 추론하지 않는다."""

    HINT = "e2e 가 없다"

    def _at_03_without_contract(self, repo, request_file, mode="contract", plan=True):
        init = cli.run_init(repo, "x", request_file)
        paths, s = st.load(repo, init["run_id"])
        st.set_phase_status(s, "01-plan", "passed")
        s["phase"] = "03-implement"
        s["contract"] = {"mode": mode, "present": False,
                         "path": "_workspace/contract_x.md"}
        st.save(paths, s)
        if plan:
            (paths.run_dir / "01_plan.md").write_text(
                _plan(),
                encoding="utf-8")
        return paths

    def test_next_거부_봉투가_e2e_부재를_말한다(self, repo, request_file, phases):
        paths = self._at_03_without_contract(repo, request_file)
        env = cli.run_next(repo, paths.run_id)
        assert env["exit"] == 3 and "진입 거부" in env["render"], env["render"]
        failed = [c for c in env["data"]["requires_report"] if not c["ok"]]
        assert failed and all("contract_file" in c["message"] for c in failed), failed
        assert self.HINT in env["render"] and "없음" in env["render"], env["render"]

    def test_해당_없음_스택(self, repo, request_file, phases):
        _set_e2e(repo, {"cmd": None, "not_applicable": "브라우저가 없다."})
        paths = self._at_03_without_contract(repo, request_file)
        env = cli.run_next(repo, paths.run_id)
        assert env["exit"] == 3 and "해당 없음" in env["render"], env["render"]

    def test_e2e_가_있으면_힌트가_없다(self, repo, request_file, phases):
        _set_e2e(repo, {"cmd": ["run", "e2e"]})
        paths = self._at_03_without_contract(repo, request_file)
        env = cli.run_next(repo, paths.run_id)
        assert env["exit"] == 3, env["render"]
        assert "## 여정" not in env["render"], env["render"]

    def test_전이_봉투도_말한다(self, repo, request_file, phases):
        paths = self._at_03_without_contract(repo, request_file)
        _p, s = st.load(repo, paths.run_id)
        s["phase"] = "01-plan"
        s["phases"]["01-plan"].pop("status", None)
        st.save(paths, s)
        loaded, _ = cli.load_phases(repo)
        env = cli._advance_to_next(repo, paths, s, loaded["01-plan"],
                                   cli.build_context(repo, paths, s))
        assert "선행 조건이 남았다" in env["render"], env["render"]
        assert self.HINT in env["render"], env["render"]

    def test_no_contract_런은_힌트가_없다(self, repo, request_file, phases):
        paths = self._at_03_without_contract(repo, request_file, mode="no_contract",
                                             plan=False)
        env = cli.run_next(repo, paths.run_id)
        assert env["exit"] == 3 and "진입 거부" in env["render"], env["render"]
        assert self.HINT not in env["render"], env["render"]

    def test_docs_선언_빗나감_봉투도_말한다(self, repo, phases):
        paths = TestDocsLane()._at_01(repo)
        _submit_plan(repo, paths, _plan())
        (repo / "src" / "lib" / "match.ts").write_text("export const x = 1\n",
                                                        encoding="utf-8")
        claims = paths.run_dir / "03_claims.json"
        claims.write_text('{"schema":1,"roles":[]}', encoding="utf-8")
        env = cli.run_record(repo, phase="03", file=str(claims), reviewer=None,
                             round_=None)
        assert env["exit"] == 3 and "docs 레인 선언" in env["render"], env["render"]
        assert self.HINT in env["render"], env["render"]


# ---------------------------------------------------------------------------
# C. ui 역할 — 계약 `## 화면` 이 있을 때만 디스패치한다 (ADR-H057)
# ---------------------------------------------------------------------------

import gate as gate_mod  # noqa: E402

SCREENS = """
## 화면

- `components/analyze/AnalyzeForm.tsx · AnalyzeForm`
  - 정상: 파일을 고르면 분석 버튼이 켜진다
"""

SCREEN_CONTRACT = TESTS_REQUIRED_CONTRACT + SCREENS

SCREEN_FILE = "src/components/analyze/AnalyzeForm.tsx"


def _screen(repo, body="export function AnalyzeForm() { return null }\n"):
    p = repo / SCREEN_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


class TestContractScreens:

    def _parse(self, repo, text):
        cfg = json.loads((repo / "harness" / "config.json").read_text(encoding="utf-8"))
        return contract_mod.parse(text, cfg)

    def test_화면은_유닛과_같은_형식이고_들여쓴_줄은_세지_않는다(self, repo):
        got = self._parse(repo, SCREEN_CONTRACT)["screens"]
        assert [(s["container"], s["symbol"]) for s in got] == [
            ("components/analyze/AnalyzeForm.tsx", "AnalyzeForm")]

    def test_화면_심볼은_계약이_이름_붙인_것이다(self, repo):
        p = self._parse(repo, SCREEN_CONTRACT)
        assert "AnalyzeForm" in contract_mod.symbols(p)

    def test_템플릿의_기본_본문은_화면이_없다(self, repo):
        """파싱되는 예시는 첫 런에 베껴져 백엔드 런에 ui 가 불린다 (G2 와 같다)."""
        text = (repo / "harness" / "templates" / "contract.md").read_text(encoding="utf-8")
        assert "## 화면" in text
        p = self._parse(repo, text)
        assert p["screens"] == [] and p["screens_dropped"] == []


class TestUiRoleOwnership:

    def test_화면은_ui_소유이고_api_는_impl_소유다(self, repo, config):
        roles = {r["id"]: r for r in config["roles"]}
        assert harness.owns_file(roles["ui"], "src/components/a/B.tsx")
        assert harness.owns_file(roles["ui"], "src/app/page.tsx")
        assert not harness.owns_file(roles["ui"], "src/app/api/x/route.ts")
        assert not harness.owns_file(roles["ui"], "src/components/a/B.test.tsx")
        assert harness.owns_file(roles["impl"], "src/app/api/x/route.ts")
        assert not harness.owns_file(roles["impl"], "src/components/a/B.tsx")
        assert not harness.owns_file(roles["impl"], "src/app/page.tsx")

    def test_ui_가_claim_하면_소유_검사를_지난다(self, repo, config):
        _screen(repo)
        got = attr.clean_ownership(repo, config, _claims(ui=[SCREEN_FILE]))
        assert got["ok"], got["message"]

    def test_impl_이_화면을_claim_하면_위반이다(self, repo, config):
        _screen(repo)
        got = attr.clean_ownership(repo, config, _claims(impl=[SCREEN_FILE]))
        assert not got["ok"]


class TestUiDispatch:
    """**디스패치는 결정론이다** — 계약에 화면이 있을 때만 ui 를 부른다.

    자진신고(`not_dispatched`)가 아니라 계약 파서가 정한다. 03 제출은 같은
    필터를 다시 계산해 저장값과 대조하고, claims 가 디스패치 목록과 같은지 본다.
    """

    def _enter(self, repo, request_file, monkeypatch, text=SCREEN_CONTRACT):
        monkeypatch.setattr(adapters, "run_stage",
                            lambda *a, **k: {"id": "compile", "state": "ran",
                                             "exit": 0, "sec": 0.1})
        return TestRecord03ContractUnitsZero()._enter_03(repo, request_file, text)

    def _dispatched(self, repo, run_id):
        _p, s = st.load(repo, run_id)
        return ((s.get("phases") or {}).get("03-implement") or {}).get("dispatched_roles")

    def _passing_tests(self, repo):
        _route(repo, "analyze", "expect(body.code).toBe('MATCH_EMPTY')\n"
                                "expect(res.status).toBe(403)\n")

    def _submit(self, repo, run_id, claims, ui=None):
        d = "src/app/api/analyze/"
        claims.write_text(json.dumps(_claims(
            impl=[d + "route.ts"], test=[d + "route.test.ts"], ui=ui,
            rules_read=_rules_read(repo))), encoding="utf-8")
        return cli.run_record(repo, "03", str(claims), run_id=run_id)

    # --- 패킷 ---------------------------------------------------------------

    def test_화면이_없으면_ui_를_부르지_않고_그렇게_말한다(self, repo, request_file,
                                                        phases, monkeypatch):
        run_id, _p, _c = self._enter(repo, request_file, monkeypatch,
                                     TESTS_REQUIRED_CONTRACT)
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 0, env["render"]
        assert self._dispatched(repo, run_id) == ["impl", "test"]
        assert "미호출" in env["render"] and "`ui`" in env["render"], env["render"]
        paths, s = st.load(repo, run_id)
        front = cli.load_phases(repo)[0]["03-implement"]["front"]
        keys = cli._instruction_keys(s, "03-implement",
                                     cli.build_context(repo, paths, s), front)
        assert keys == ["03:r0:impl", "03:r0:test"], keys

    def test_화면이_있으면_ui_를_지시한다(self, repo, request_file, phases,
                                        monkeypatch):
        run_id, _p, _c = self._enter(repo, request_file, monkeypatch)
        env = cli.run_next(repo, run_id)
        assert env["exit"] == 0, env["render"]
        assert self._dispatched(repo, run_id) == ["impl", "test", "ui"]
        assert "03:r0:ui" in env["render"], env["render"]
        assert "미호출" not in env["render"], env["render"]

    # --- 제출 ---------------------------------------------------------------

    def test_디스패치된_ui_가_claims_에_없으면_exit_8(self, repo, request_file,
                                                    phases, monkeypatch):
        run_id, _p, claims = self._enter(repo, request_file, monkeypatch)
        cli.run_next(repo, run_id)
        self._passing_tests(repo)
        env = self._submit(repo, run_id, claims)
        assert env["exit"] == 8, env["render"]
        assert "`ui`" in env["render"] and "claims" in env["render"], env["render"]
        _p, s = st.load(repo, run_id)
        assert st.phase_status(s, "03-implement") != "passed"

    def test_디스패치되지_않은_ui_가_claims_에_있으면_exit_8(
            self, repo, request_file, phases, monkeypatch):
        run_id, _p, claims = self._enter(repo, request_file, monkeypatch,
                                         TESTS_REQUIRED_CONTRACT)
        cli.run_next(repo, run_id)
        self._passing_tests(repo)
        env = self._submit(repo, run_id, claims, ui=[])
        assert env["exit"] == 8, env["render"]
        assert "`ui`" in env["render"], env["render"]

    def test_패킷_뒤에_계약이_바뀌면_next_를_다시_받으라고_한다(
            self, repo, request_file, phases, monkeypatch):
        run_id, _p, claims = self._enter(repo, request_file, monkeypatch,
                                         TESTS_REQUIRED_CONTRACT)
        cli.run_next(repo, run_id)
        (repo / "_workspace" / "contract_x.md").write_text(SCREEN_CONTRACT,
                                                          encoding="utf-8")
        _screen(repo)
        self._passing_tests(repo)
        env = self._submit(repo, run_id, claims, ui=[SCREEN_FILE])
        assert env["exit"] == 8, env["render"]
        assert "계약이 바뀌었다" in env["render"], env["render"]
        assert "next" in (env["next_command"] or "")
        again = cli.run_next(repo, run_id)
        assert again["exit"] == 0, again["render"]
        assert self._dispatched(repo, run_id) == ["impl", "test", "ui"]

    def test_ui_를_포함한_제출이_지난다(self, repo, request_file, phases,
                                      monkeypatch):
        run_id, _p, claims = self._enter(repo, request_file, monkeypatch)
        cli.run_next(repo, run_id)
        _screen(repo)
        self._passing_tests(repo)
        env = self._submit(repo, run_id, claims, ui=[SCREEN_FILE])
        assert env["exit"] != 8, env["render"]
        _p, s = st.load(repo, run_id)
        assert st.phase_status(s, "03-implement") == "passed"

    def test_디스패치_기록이_없으면_다시_계산해_진행한다(
            self, repo, request_file, phases, monkeypatch):
        """`next` 를 거치지 않은 옛 런 — 계약에서 다시 계산한다."""
        run_id, _p, claims = self._enter(repo, request_file, monkeypatch,
                                         TESTS_REQUIRED_CONTRACT)
        self._passing_tests(repo)
        env = self._submit(repo, run_id, claims)
        assert env["exit"] != 8, env["render"]


class TestUiAttribution:
    """귀속 사다리는 **이 런에 디스패치된 역할**로만 만든다."""

    F = {"id": "F-1", "owner": "ambiguous", "sig": "abc", "kind": "stage",
         "file": "src/lib/match.ts"}

    def _ladder(self, config, failure, roles):
        flip, out = {}, []
        for _ in range(4):
            out.append(attr.resolve_ambiguous([dict(failure)], config, flip,
                                              roles=roles)[0]["owner"])
        return out

    def test_디스패치된_ui_가_사다리에_든다(self, repo, config):
        assert self._ladder(config, self.F, ["impl", "test", "ui"]) == [
            "impl", "test", "ui", "contract"]

    def test_단언_실패는_ui_를_건너뛴다(self, repo, config):
        """ui 는 테스트를 소유하지 않는다 — 단언 실패를 고칠 수 없다."""
        f = dict(self.F, kind="test")
        assert self._ladder(config, f, ["impl", "test", "ui"])[:3] == [
            "impl", "test", "contract"]

    def test_기록이_없으면_조건부_역할은_빠진다(self, repo, config):
        assert self._ladder(config, self.F, None)[:3] == ["impl", "test", "contract"]

    def test_게이트가_디스패치_기록을_넘긴다(self, repo, config):
        _c, adapter = adapters.load(repo)
        state = {"phases": {"03-implement": {"dispatched_roles": ["impl", "test", "ui"]}}}
        report = {"failed": {"id": "full", "exit": 1, "output": "boom"}}
        owners = []
        for _ in range(3):
            got = gate_mod.attribute(repo, config, adapter, report, state,
                                     log_text="boom")
            owners.append(got["failures"][0]["owner"])
        assert owners == ["impl", "test", "ui"], owners


class TestContractTraceScreens:

    def test_화면이_없으면_critical_이고_ui_에게_간다(self, repo):
        got = _codes(_trace(repo, _write_contract(repo, SCREEN_CONTRACT), changed=[]),
                     "missing_screen")
        assert got and got[0]["severity"] == "critical"
        assert got[0]["target_role"] == "ui"

    def test_화면이_있으면_지난다(self, repo):
        _screen(repo)
        got = _trace(repo, _write_contract(repo, SCREEN_CONTRACT), changed=[])
        assert _codes(got, "missing_screen") == []
        assert "missing_screen" in got["checks_run"]
        assert got["contract"]["screens"] == 1

    def test_화면의_테스트는_묻지_않고_그렇게_남긴다(self, repo):
        """UI 단위테스트는 두지 않는다 — 통과가 아니라 미수행이다."""
        _screen(repo)
        got = _trace(repo, _write_contract(repo, SCREEN_CONTRACT), changed=[])
        assert "untested_screen" in got["skipped"]
        assert "ADR-H057" in got["skip_reasons"]["untested_screen"]
        assert not [f for f in got["findings"] if f.get("symbol") == "AnalyzeForm"]

    def test_화면이_없는_계약은_미수행을_적지_않는다(self, repo):
        got = _trace(repo, _write_contract(repo, TESTS_REQUIRED_CONTRACT), changed=[])
        assert "untested_screen" not in got["skipped"]


# ---------------------------------------------------------------------------
# E. PR 본문 재구성 — 흐름·확인법·검증 표·05 한 줄 (ADR-H058 추기)
# ---------------------------------------------------------------------------

class TestPr06Notes:
    """흐름 노트의 `refs` 는 계약 식별자여야 한다 — 산문은 검사하지 않는다."""

    def _check(self, repo, paths, notes):
        _pr_notes(paths, notes)
        _p, s = st.load(repo, paths.run_id)
        return pr_mod.check_notes(repo, paths, s,
                                  harness._read_json(repo / harness.CONFIG_REL))[1]

    def _notes(self, refs, step="흐름", verify=("확인",)):
        return {"schema": 1, "flow": [{"step": step, "refs": list(refs)}],
                "verify": list(verify)}

    def test_계약_식별자면_통과한다(self, repo, request_file, phases):
        _run_id, paths = _enter_06(repo, request_file, phases)
        for refs in (["matchTitle"], ["MATCH_FAILED"], ["POST /api/analyze"],
                     ["/api/analyze"], ["lib/match.ts"], ["src/lib/match.ts"]):
            assert self._check(repo, paths, self._notes(refs)) == [], refs

    def test_계약에_없는_ref_는_이름으로_거부한다(self, repo, request_file, phases):
        _run_id, paths = _enter_06(repo, request_file, phases)
        got = self._check(repo, paths, self._notes(["지어낸심볼", "src/lib/other.ts"]))
        assert any("지어낸심볼" in g for g in got), got
        assert any("src/lib/other.ts" in g for g in got), got

    def test_step_산문의_백틱은_보지_않는다(self, repo, request_file, phases):
        _run_id, paths = _enter_06(repo, request_file, phases)
        notes = self._notes(["matchTitle"], step="`아무말` 로 부른다")
        assert self._check(repo, paths, notes) == []

    def test_형식이_틀리면_거부한다(self, repo, request_file, phases):
        _run_id, paths = _enter_06(repo, request_file, phases)
        for notes in (self._notes([]), self._notes(["matchTitle"], step=""),
                      self._notes(["matchTitle"], verify=()),
                      {"schema": 1, "flow": [], "verify": ["x"]}, []):
            assert self._check(repo, paths, notes), notes

    def test_계약_경로가_state_에_없어도_읽는다(self, repo, request_file, phases):
        """`_drop_contract` 와 같은 낙하 — 경로의 단일 출처는 `path_template` 이다."""
        run_id, paths = _enter_06(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        assert not (s.get("contract") or {}).get("path")
        assert self._check(repo, paths, self._notes(["matchTitle"])) == []

    # --- pr 배선 -----------------------------------------------------------

    def test_노트가_없으면_첫_pr_이_exit_8(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        (paths.run_dir / "06_pr_notes.json").unlink()
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 8, env["render"]
        assert "06_pr_notes.json" in env["render"], env["render"]
        assert "pr" in (env["next_command"] or "")

    def test_틀린_ref_는_pr_이_exit_8_로_알린다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        _pr_notes(paths, self._notes(["지어낸심볼"]))
        env = cli.run_pr(repo, run_id=run_id)
        assert env["exit"] == 8 and "지어낸심볼" in env["render"], env["render"]

    def test_닫힌_런_재실행은_노트를_검사하지_않는다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        (paths.run_dir / "06_pr_notes.json").unlink()
        _p, s = st.load(repo, run_id)
        s["run_status"] = st.DONE
        st.save(_p, s)
        assert cli.run_pr(repo, run_id=run_id)["exit"] != 8

    def test_no_contract_런은_노트가_선택이다(self, repo, request_file, phases):
        _branch(repo, "feat-x")
        run_id, paths = _enter_06(repo, request_file, phases)
        (paths.run_dir / "06_pr_notes.json").unlink()
        _p, s = st.load(repo, run_id)
        s["contract"] = {"mode": "no_contract"}
        st.save(_p, s)
        assert cli.run_pr(repo, run_id=run_id)["exit"] != 8


class TestPr06WorkSection:
    """`## 작업 내용` — 흐름 → 확인법 → 검증 표 → 05 한 줄 → 변경 규모 → 계약 상세."""

    def _body(self, repo, run_id, mutate=None):
        paths, s = st.load(repo, run_id)
        if mutate:
            mutate(s)
            st.save(paths, s)
        return pr_mod.build_body(repo, paths, s,
                                 harness._read_json(repo / harness.CONFIG_REL))

    def _work(self, body):
        return body.split("## 작업 내용", 1)[1].split("## 참고사항", 1)[0]

    def _row(self, body):
        return next(l for l in body.splitlines()
                    if l.startswith("|") and "matchTitle" in l)

    def test_순서가_흐름_확인법_검증_05_계약상세다(self, repo, request_file, phases):
        run_id, _paths = _enter_06(repo, request_file, phases)
        work = self._work(self._body(repo, run_id))
        marks = ["핵심 흐름", "직접 확인하는 법", "무엇이 검증됐나", "05 리뷰",
                 "변경 규모", "<details>"]
        idx = [work.index(m) for m in marks]
        assert idx == sorted(idx), list(zip(marks, idx))
        assert "1. 두 제목의 유사도를 잰다 — `matchTitle`" in work, work
        assert "- POST /api/analyze 에 두 제목을" in work, work

    def test_노트가_없으면_없다고_적는다(self, repo, request_file, phases):
        run_id, paths = _enter_06(repo, request_file, phases)
        (paths.run_dir / "06_pr_notes.json").unlink()
        assert "_흐름 노트 없음_" in self._body(repo, run_id)

    def test_유닛의_테스트_파일과_케이스_수(self, repo, request_file, phases):
        run_id, _paths = _enter_06(repo, request_file, phases)
        body = self._body(repo, run_id, lambda s: s.__setitem__(
            "tests", {"ran": 3, "by_file": {"src/lib/match.test.ts": 3}}))
        row = self._row(body)
        assert "`src/lib/match.test.ts`" in row and "| 3 |" in row, row
        assert "partial" not in self._work(body)

    def test_by_file_에_없는_파일은_미측정이다(self, repo, request_file, phases):
        run_id, _paths = _enter_06(repo, request_file, phases)
        body = self._body(repo, run_id, lambda s: s.__setitem__(
            "tests", {"ran": 3, "by_file": {"src/other.test.ts": 3}}))
        assert "미측정" in self._row(body)

    def test_합이_ran_과_다르면_partial(self, repo, request_file, phases):
        run_id, _paths = _enter_06(repo, request_file, phases)
        body = self._body(repo, run_id, lambda s: s.__setitem__(
            "tests", {"ran": 5, "by_file": {"src/lib/match.test.ts": 3}}))
        assert "partial" in self._work(body)

    def test_by_file_이_없으면_표_전체가_미측정(self, repo, request_file, phases):
        run_id, _paths = _enter_06(repo, request_file, phases)
        body = self._body(repo, run_id, lambda s: s.__setitem__("tests", {"ran": 3}))
        assert "미측정" in self._row(body)

    def test_05_한_줄이_수리된_Major_와_남은_Minor_를_센다(self, repo, request_file,
                                                        phases):
        run_id, _paths = _enter_06(repo, request_file, phases)
        major = {"id": "F-1", "category": "RESPONSE_SHAPE", "severity": "major",
                 "target_role": "impl", "title": "고친 Major", "quote": "q"}
        minor = {"id": "F-2", "category": "NAMING", "severity": "minor",
                 "target_role": "impl", "title": "남은 Minor", "quote": "q"}
        k1, k2 = verdict_mod.finding_key(major), verdict_mod.finding_key(minor)

        def mutate(s):
            s["phases"]["05-code-review"]["rounds"] = {
                "1": {"arch": {"keys": [{"key": k1, "id": "F-1", "severity": "major"}],
                               "findings": [major], "closed": []},
                      "gen": {"keys": [{"key": k2, "id": "F-2", "severity": "minor"}],
                              "findings": [minor], "closed": []}},
                "2": {"arch": {"keys": [], "findings": [], "closed": [k1]}}}
        line = next(l for l in self._work(self._body(repo, run_id, mutate)).splitlines()
                    if "05 리뷰" in l)
        assert "arch" in line and "gen" in line, line
        assert "Major 수리 1" in line and "미해결 Minor 1" in line, line


class TestGateTestsByFile:

    def test_full_실행이_파일별_케이스_수를_남긴다(self, gated, fxdir):
        repo, paths, s = gated
        cases = ('<testcase classname="src/lib/match.test.ts" name="a"/>'
                 '<testcase classname="src/lib/match.test.ts" name="b"/>')
        env = _gate(repo, make_fixture(fxdir, "byfile", dict(ALL_PASS), tests=1300,
                                       cases=cases))
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["tests"]["by_file"] == {"src/lib/match.test.ts": 2}, after["tests"]


class TestFixLane:
    """`fix` 레인 (ADR-H053). 사용자가 `init --profile fix` 로 선언한다.

    절차: 01 1라운드 → 03 impl·test → 05 cap 1(`gen`).
    """

    def test_a_user_fix_profile_is_kept_at_init(self, repo, phases):
        paths, s = _init(repo, SOURCE_REQUEST, profile="fix")
        env = cli.run_next(repo, run_id=paths.run_id)
        assert env["exit"] == 0 and env["phase"] == "01-plan", env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["profile"]["name"] == "fix"
        assert after["profile"]["source"] == "user"
        assert after["contract"].get("mode") != "no_contract"

    def test_01_converges_in_one_round(self, repo, phases):
        paths, s = _init(repo, SOURCE_REQUEST, profile="fix")
        cli.run_next(repo, run_id=paths.run_id)
        assert _submit_plan(repo, paths, _plan())["exit"] == 0
        env = _submit_review(repo, paths, _review("plan"))
        assert env["exit"] == 0, env["render"]
        _, after = st.load(repo, paths.run_id)
        assert after["phases"]["01-plan"]["converged_at_round"] == 1
        assert after["counters"]["round"]["max"] == 1
        assert after["phase"] == "03-implement", after["phase"]
        assert "01:max_rounds=1" in after["profile"]["applied"]
        assert after.get("grade") in (None, "PASS"), after.get("gaps")

    def test_05_cap_routes_gen_only(self):
        cfg = _cfg()
        assert cfg["review"]["profile_caps"]["fix"] == 1
        routed = rv.route(cfg, ["src/lib/schemas.ts", "src/app/api/x/route.ts"], "fix")
        assert [r["code"] for r in routed["reviewers"]] == ["gen"], routed
        assert routed["capped"] is True

    def test_model_slots_accept_fix(self):
        cfg = _cfg()
        for slot in ("plan", "roles", "reviewers"):
            assert cfg["models"][slot]["fix"] == "sonnet", slot


class TestReviewDepth:
    """05 의 리뷰 범위는 레인별로 봉투가 찍는다 (`review.depth`).

    FR-007 의 동시성 결함은 05 가 diff 만 봐서 놓쳤고 07 이 기존 `transition()`
    과의 상호작용에서 잡았다 — 리뷰 범위가 레인과 무관하게 고정돼 있었다.
    `normal` 은 `diff+refs`(계약이 참조하는 기존 파일까지), 나머지는 `diff`.
    """

    def test_config_declares_a_depth_per_lane(self):
        cfg = _cfg()
        assert cfg["review"]["depth"] == {"docs": "diff", "fix": "diff",
                                          "normal": "diff+refs"}

    def test_schema_and_config_are_paired(self):
        """스키마 없이 config 만 고치면 doctor 가 기동 전에 거부한다."""
        cfg, schema = _cfg(), json.loads(
            (ROOT / "harness" / "config.schema.json").read_text(encoding="utf-8"))
        assert harness.validate(cfg, schema) == []
        without = json.loads(json.dumps(schema))
        del without["properties"]["review"]["properties"]["depth"]
        errs = harness.validate(cfg, without)
        assert any("depth" in e for e in errs), errs
        bad = json.loads(json.dumps(cfg))
        bad["review"]["depth"]["normal"] = "everything"
        assert harness.validate(bad, schema), "어휘 밖 값은 거부다"

    def test_05_entry_freezes_the_depth_and_the_envelope_says_it(
            self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        # 계약 유닛 수 재판정에 밀리지 않게 사람이 정한 normal 로 둔다.
        s["profile"] = {"name": "normal", "source": "user"}
        st.save(_p, s)
        (repo / "src" / "app" / "api" / "x").mkdir(parents=True)
        (repo / "src" / "app" / "api" / "x" / "route.ts").write_text(
            "export async function POST() {}\n", encoding="utf-8")
        env = cli.run_next(repo, run_id)
        _p, s = st.load(repo, run_id)
        assert s["phases"]["05-code-review"]["depth"] == "diff+refs"
        assert "리뷰 범위: **diff+refs**" in env["render"], env["render"]

    def test_fix_lane_reviews_the_diff_only(self, repo, request_file, phases):
        run_id, paths = _enter_05(repo, request_file, phases)
        _p, s = st.load(repo, run_id)
        s["profile"] = {"name": "fix", "source": "user"}
        st.save(_p, s)
        (repo / "src" / "lib" / "match.ts").write_text("export const x = 1\n",
                                                        encoding="utf-8")
        env = cli.run_next(repo, run_id)
        _p, s = st.load(repo, run_id)
        assert s["phases"]["05-code-review"]["depth"] == "diff"
        assert [r["code"] for r in s["phases"]["05-code-review"]["routing"]["reviewers"]] == ["gen"]
        assert "리뷰 범위: **diff**" in env["render"], env["render"]
        assert "리뷰 범위: **diff+refs**" not in env["render"]

    def test_render_explains_each_depth(self):
        def node(depth):
            return {"phases": {"05-code-review": {
                "mode": "fanout", "depth": depth,
                "routing": {"reviewers": [{"code": "gen", "skill": "general-reviewer",
                                           "matched_count": 1}],
                            "dropped": [], "capped": False}}}}
        wide = cli._review_render(node("diff+refs"))
        assert "리뷰 범위: **diff+refs**" in wide and "참조" in wide, wide
        narrow = cli._review_render(node("diff"))
        assert "리뷰 범위: **diff**" in narrow and "diff+refs" not in narrow, narrow

    def test_depth_lands_in_review05_and_the_report(self):
        s, node = {}, {"depth": "diff+refs"}
        cli._write_review05(s, node, planned=["gen"], ok=1, merged=[], slot={},
                            round_=1)
        assert s["review05"]["depth"] == "diff+refs"
        s.update({"run_id": "r", "slug": "x", "grade": "PASS", "phases": {},
                  "counters": {}, "budget": {}, "profile": {"name": "normal"}})
        text, _missing = rep_mod.build(s, {})
        assert "05 리뷰 범위" in text and "diff+refs" in text, text


