#!/usr/bin/env python3
"""scripts/harness.py — init · doctor 단위 테스트.

ROADMAP 1단계 게이트 G1: "일부러 깨뜨린 config를 doctor가 전부 거부하는가."
거부만 검사하면 절반이다. 정상 config가 전부에서 통과하는 것(오탐 없음)까지 같이 본다.

각 테스트는 tmpdir에 최소 리포를 세우고 그 위에서 doctor를 돌린다.
스키마·어댑터·계약 템플릿은 실물을 복사하므로, 실물이 바뀌면 이 테스트가 먼저 깨진다.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import harness  # noqa: E402


# 실물에서 픽스처로 복사하는 파일들 — 하네스 계층의 정본
COPIED = [
    "harness/config.schema.json",
    "harness/adapters/adapter.schema.json",
    "harness/adapters/nextjs-ts.json",
    "harness/templates/contract.md",
    "harness/config.json",
    "harness/profiles/nextjs-ts/config.json",
]

# 어댑터가 `run <script>` 로 참조하는 스크립트가 전부 들어 있어야 한다
FIXTURE_PACKAGE_JSON = {
    "name": "fixture",
    "private": True,
    "scripts": {
        "dev": "next dev",
        "typecheck": "tsc --noEmit",
        "lint": "eslint .",
        "test": "vitest run",
        "build": "next build",
        "audit": "npm audit --audit-level=high",
    },
}

# 역할 소유 경계가 실제로 갈리는지 보려면 impl 파일과 test 파일이 둘 다 있어야 한다
FIXTURE_SOURCES = [
    "src/app/page.tsx",
    "src/app/api/analyze/route.ts",
    "src/components/upload/Picker.tsx",
    "src/lib/match.ts",
    "src/lib/match.test.ts",
    "src/services/api-client.ts",
    "src/types/book.ts",
]


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_fixture(root: Path):
    for rel in COPIED:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dst)
    # 픽스처는 **Next.js 모양의 리포**다 (package.json · src/app/** · vitest).
    # 이 리포 자신은 파이썬이라 config 의 adapter 가 self-python 이고, 둘은 다른
    # 사실이다. 실물 config 를 복사하는 값(스키마·역할·계약 절이 실물과 같이
    # 움직인다)은 지키되 어댑터만 픽스처의 스택으로 되돌린다 (ADR-H038).
    cfg_path = root / "harness/config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["adapter"] = "nextjs-ts"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + chr(10),
                        encoding="utf-8")
    # 어댑터는 픽스처에서 **미검증**이다 — 클론이 `verify-adapter` 로 실물을
    # 올려도(ADR-H047) 여기 테스트가 그 값에 묶이지 않는다.
    ad_path = root / "harness/adapters/nextjs-ts.json"
    ad = json.loads(ad_path.read_text(encoding="utf-8"))
    ad["verified"] = False
    ad_path.write_text(json.dumps(ad, ensure_ascii=False, indent=2) + chr(10),
                       encoding="utf-8")
    _write(root / "package.json", json.dumps(FIXTURE_PACKAGE_JSON, indent=2) + "\n")
    _write(root / "CLAUDE.md", "# fixture\n")
    for rel in FIXTURE_SOURCES:
        _write(root / rel, "// fixture\n")


class DoctorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        _build_fixture(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    # --- 픽스처 조작 ---

    def _load(self, rel):
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    def _save(self, rel, data):
        _write(self.root / rel, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def config(self):
        return self._load("harness/config.json")

    def save_config(self, cfg):
        self._save("harness/config.json", cfg)

    def adapter(self):
        return self._load("harness/adapters/nextjs-ts.json")

    def save_adapter(self, ad):
        self._save("harness/adapters/nextjs-ts.json", ad)

    # --- 실행 & 단언 ---

    def doctor(self):
        return harness.run_doctor(self.root)

    def assertRejected(self, report, needle):
        self.assertEqual(2, report.exit_code, "거부되지 않았다:\n" + report.text())
        hit = any(needle in c.message or needle in c.name for c in report.failures)
        self.assertTrue(hit, "실패 사유에 %r 가 없다:\n%s" % (needle, report.text()))


class BaselinePassesTest(DoctorTestBase):
    """정상 config는 통과해야 한다 — 오탐 검사."""

    def test_clean_config_passes(self):
        report = self.doctor()
        self.assertEqual(0, report.exit_code, report.text())
        self.assertEqual([], report.failures, report.text())

    def test_null_cmd_stages_are_reported_not_silently_passed(self):
        """cmd: null 스테이지는 '없는 것'이지 '통과한 것'이 아니다."""
        text = self.doctor().text()
        for stage in ("e2e", "docs"):
            self.assertIn(stage, text, "%s 스킵이 보고되지 않았다:\n%s" % (stage, text))

    def test_unverified_adapter_and_missing_calibration_warn(self):
        report = self.doctor()
        warn_text = "\n".join(c.message for c in report.checks if c.status == "WARN")
        self.assertIn("verified", warn_text, report.text())
        self.assertIn("캘리브레이션", warn_text, report.text())


class BrokenConfigRejectedTest(DoctorTestBase):
    """G1 — 깨뜨린 config 8종을 전부 거부하는가."""

    def test_1_role_ownership_overlap(self):
        cfg = self.config()
        for role in cfg["roles"]:
            role.pop("excludes", None)
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "src/lib/match.test.ts")

    def test_2_contract_section_mismatch(self):
        cfg = self.config()
        cfg["contract"]["sections"]["units"] = "## 존재하지않는절"
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "## 존재하지않는절")

    def test_2b_role_condition_names_an_unknown_section(self):
        """조건부 역할이 없는 절을 가리키면 그 역할은 조용히 영영 미호출이다 (ADR-H057)."""
        cfg = self.config()
        next(r for r in cfg["roles"] if r["id"] == "ui")["when_contract_section"] = "screenz"
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "screenz")

    def test_3_runner_bin_not_whitelisted(self):
        ad = self.adapter()
        ad["runner"]["bin"] = "sh"
        self.save_adapter(ad)
        self.assertRejected(self.doctor(), "sh")

    def test_9_model_tier_out_of_vocabulary(self):
        """`config.models` 의 등급은 닫힌 어휘다 (ADR-H044) — 밖의 값은 거부."""
        cfg = self.config()
        cfg["models"]["plan"]["default"] = "gpt-9"
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "gpt-9")

    def test_4_stage_cmd_references_missing_script(self):
        ad = self.adapter()
        ad["stages"]["compile"]["cmd"] = ["run", "nonexistent"]
        self.save_adapter(ad)
        self.assertRejected(self.doctor(), "nonexistent")

    def test_5_adapter_requires_unmet(self):
        pkg = self._load("package.json")
        del pkg["scripts"]["test"]
        self._save("package.json", pkg)
        self.assertRejected(self.doctor(), "scripts.test")

    def test_6_schema_type_violation(self):
        cfg = self.config()
        cfg["budget"]["files_max"] = "10"
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "budget.files_max")

    def test_7_unknown_adapter_id(self):
        cfg = self.config()
        cfg["adapter"] = "does-not-exist"
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "does-not-exist")

    def test_8_main_owned_overlaps_role_owned(self):
        cfg = self.config()
        cfg["main_owned_paths"].append("src/lib/**")
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "src/lib/match.ts")

    # --- 부수 거부 케이스 ---

    def test_primary_role_must_exist(self):
        cfg = self.config()
        cfg["primary_role"] = "nope"
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "primary_role")

    def test_missing_config_file(self):
        (self.root / "harness/config.json").unlink()
        self.assertRejected(self.doctor(), "harness/config.json")

    def test_malformed_json_is_rejected(self):
        _write(self.root / "harness/config.json", "{ not json")
        self.assertRejected(self.doctor(), "harness/config.json")

    def test_unowned_source_file_is_surfaced(self):
        """어느 역할도 소유하지 않는 소스 파일은 조용히 넘어가지 않는다."""
        _write(self.root / "src/orphan/stray.ts", "// fixture\n")
        self.assertIn("src/orphan/stray.ts", self.doctor().text())


class StageCommandDeclarationTest(DoctorTestBase):
    """스테이지 명령 검사는 어댑터 선언을 읽는다 — 코어가 스택 이름을 알지 않는다.

    러너가 `<verb> <script>` 문법을 쓰는지, 스크립트가 어디에 선언돼 있는지는
    스택의 사실이지 실행기의 사실이 아니다. 코어에 그 목록을 두면 같은 지식이
    스키마 enum 과 코드 두 곳에 살고 한쪽만 고쳐지는 날이 온다 (ADR-H031).
    """

    STAGE_CHECK = u"스테이지 명령"

    def _check(self, report, name):
        for c in report.checks:
            if c.name == name:
                return c
        self.fail("검사 %r 가 리포트에 없다 — %s" % (name, report.text()))

    def test_declared_manifest_still_catches_missing_script(self):
        """회귀 — 선언으로 옮긴 뒤에도 없는 스크립트를 FAIL 로 잡는다."""
        ad = self.adapter()
        ad["stages"]["compile"]["cmd"] = ["run", "nonexistent"]
        self.save_adapter(ad)
        self.assertRejected(self.doctor(), "nonexistent")

    def test_declared_manifest_still_catches_missing_baseline_script(self):
        """회귀 — baseline_cmd 도 같은 규칙으로 대조된다."""
        ad = self.adapter()
        ad["stages"]["lint"]["baseline_cmd"] = ["run", "no-such-baseline"]
        self.save_adapter(ad)
        self.assertRejected(self.doctor(), "no-such-baseline")

    def test_verb_mismatch_is_left_alone(self):
        """check 의 `audit` 은 verb 가 run 이 아니므로 대조 대상이 아니다 — 지금 동작 그대로."""
        report = self.doctor()
        self._check(report, self.STAGE_CHECK)
        self.assertEqual("PASS", self._check(report, self.STAGE_CHECK).status, report.text())

    def test_missing_manifest_warns_not_fails(self):
        """선언이 없으면 '검사 안 함'이다 — 조용한 통과가 아니고 FAIL 도 아니다."""
        ad = self.adapter()
        del ad["runner"]["script_manifest"]
        self.save_adapter(ad)
        report = self.doctor()
        check = self._check(report, self.STAGE_CHECK)
        self.assertEqual("WARN", check.status, report.text())
        self.assertIn(u"검사 안 함", check.message, report.text())
        self.assertEqual([], report.failures, report.text())

    def test_manifest_pointing_at_missing_file_warns_with_the_name(self):
        """선언은 있는데 그 파일이 없으면, 무엇을 못 읽었는지가 리포트에 뜬다."""
        ad = self.adapter()
        ad["runner"]["script_manifest"]["file"] = "does-not-exist.json"
        self.save_adapter(ad)
        report = self.doctor()
        check = self._check(report, self.STAGE_CHECK)
        self.assertEqual("WARN", check.status, report.text())
        self.assertIn("does-not-exist.json", check.message, report.text())
        self.assertEqual([], report.failures, report.text())

    def test_manifest_pointing_at_missing_pointer_warns(self):
        """파일은 있는데 그 안의 키가 없으면 같은 처리다."""
        ad = self.adapter()
        ad["runner"]["script_manifest"]["pointer"] = "no.such.key"
        self.save_adapter(ad)
        report = self.doctor()
        check = self._check(report, self.STAGE_CHECK)
        self.assertEqual("WARN", check.status, report.text())
        self.assertIn("no.such.key", check.message, report.text())
        self.assertEqual([], report.failures, report.text())

    def test_non_manifest_runner_needs_no_code_change(self):
        """매니페스트 개념이 없는 스택으로 갈아도 코어를 고칠 일이 없다.

        runner.bin 이 이 머신 PATH 에 있는지는 별개의 검사이므로 여기서는
        스테이지 명령 검사만 본다 — 그것이 이 테스트가 재는 것이다.
        """
        ad = self.adapter()
        ad["runner"]["bin"] = "make"
        del ad["runner"]["script_manifest"]
        ad["stages"]["compile"]["cmd"] = ["build"]
        self.save_adapter(ad)
        report = self.doctor()
        check = self._check(report, self.STAGE_CHECK)
        self.assertEqual("WARN", check.status, report.text())
        self.assertNotIn(self.STAGE_CHECK, [c.name for c in report.failures], report.text())


class CoreHasNoStackNamesTest(unittest.TestCase):
    """ADR-H013 승격 게이트 1번의 자물쇠 — 코어에 스택 고유명사를 되돌려 놓지 못한다."""

    def test_node_runners_list_is_not_in_scripts(self):
        hits = []
        for path in sorted((ROOT / "scripts").rglob("*.py")):
            if path.name == "test_harness.py":
                continue
            if "NODE_RUNNERS" in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual([], hits,
                         "스택 이름 목록이 실행기 코드로 돌아왔다 — 선언은 어댑터가 든다 (ADR-H031)")


class TemplateHasNoPilotNamesTest(unittest.TestCase):
    """추출 게이트의 자물쇠 — 파일럿(Shelfie)의 고유명사가 템플릿에 남으면 안 된다.

    `CoreHasNoStackNamesTest`(위)와 잡는 것이 다르다. 그쪽은 **스택 실행기 이름
    목록**이 코어 코드로 돌아오는 것을 막고, 이쪽은 **파일럿 프로젝트의 고유명사**가
    템플릿에 남는 것을 막는다. 클론하는 사람이 남의 앱 이름을 물려받으면 그것을
    지우는 일이 첫 작업이 된다.

    **단어 목록에 `npm`·`next` 를 넣지 않는다.** 그 둘은 러너 화이트리스트
    (`adapters/adapter.schema.json`)와 `script_manifest` 설명에 **정당하게** 있다.
    잡을 수 없는 것에 자물쇠를 걸면 다음 사람이 자물쇠를 지운다 (ADR-H038).
    """

    #: 파일럿(Shelfie)과 그 스택의 고유명사. 소문자로 비교한다.
    NAMES = ("shelfie", "aladin", "anthropic", "supabase", "vercel")

    #: 이 파일 자신만 남는다 — 위 NAMES 가 여기 적혀 있기 때문이다.
    #: `CoreHasNoStackNamesTest` 가 같은 이유로 자기를 제외하는 것과 같은 자리다.
    #: 세션 F 가 넷(`docs/harness/`·`CLAUDE.md`·`calibration.json`·`keep-paths.txt`)
    #: 을 지웠다 — 하나를 다시 쓸 때마다 제외에서 빼면 자물쇠가 그 자리를 맡는다
    #: (ADR-H039).
    SKIP_PREFIXES = ("scripts/test_harness.py",)

    def test_pilot_names_are_gone_from_the_template(self):
        hits = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith((".git/", "__pycache__/", ".pytest_cache/")):
                continue
            if "/__pycache__/" in rel or rel.startswith(self.SKIP_PREFIXES):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            lowered = text.lower()
            for name in self.NAMES:
                if name in lowered:
                    hits.append("%s: %s" % (rel, name))
        self.assertEqual([], hits,
                         "파일럿의 고유명사가 템플릿에 남았다 — 세션 E 의 스크럽 "
                         "게이트다 (ADR-H038)")


class CoreDoesNotImportTheExecutorTest(unittest.TestCase):
    """추출 게이트의 자물쇠 — 8페이즈 코어가 순차 실행기를 다시 물면 안 된다.

    `harness-template` 은 `scripts/execute.py` 를 안 싣는다 (ROADMAP 36).
    코어가 그것을 import 하면 추출본은 **테스트 수집 단계에서** 죽는다 —
    `state.py` 의 import 가 모듈 최상위라 `test_pipeline.py` 전체가 error 다.
    실행기 없이 도는지는 추출해 봐야만 알 수 있는 것이 아니라 **여기서**
    알 수 있어야 한다 (ADR-H037).

    `NODE_RUNNERS` 자물쇠(위)와 같은 모양이다: 방침을 산문이 아니라 기계가 든다.
    """

    #: 실행기 자신과 그 입력 형식(`phases/*/index.json`)을 읽는 것들.
    #: 셋 다 추출 범위 밖이라 execute 를 물어도 템플릿에 영향이 없다.
    ALLOWED = {"scripts/execute.py", "scripts/backfill_reads.py",
               "scripts/test_execute.py", "scripts/test_harness.py"}

    def test_only_the_executor_family_imports_execute(self):
        hits = []
        for path in sorted((ROOT / "scripts").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in self.ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import execute", "from execute ")):
                    hits.append("%s: %s" % (rel, stripped))
        self.assertEqual([], hits,
                         "코어가 순차 실행기를 물었다 — 공유 원시요소는 "
                         "scripts/runtime.py 가 든다 (ADR-H037)")


class TemplateDocsAreNotDanglingTest(unittest.TestCase):
    """가리키는 문서가 실재하는지 묻는다 — 매달린 참조는 조용히 통과한다.

    추출은 `docs/` 직속 문서를 안 실었는데(ADR-H002 가 *"프로젝트가 채우는 자리"*
    라 정했다) `CLAUDE.md` 의 문서 표와 `.claude/commands/log.md` 는 그것들을
    읽으라고 가리켰다. **가리키는 쪽과 가리켜지는 쪽이 어긋나도 아무것도 안
    깨진다** — 클론하는 사람이 없는 파일을 찾다 포기할 뿐이다.

    `CoreHasNoStackNamesTest` 가 "있으면 안 되는 것"을 잡는다면 이쪽은 "없으면
    안 되는 것"을 잡는다. 세션 F 가 걸었다 (ADR-H039).
    """

    #: 참조를 캐낼 파일들. 산문이 아니라 **경로를 지시로 쓰는** 자리만 본다.
    SOURCES = ("README.md", "CLAUDE.md", ".claude/commands/feature.md",
               ".claude/commands/log.md")

    #: `docs/…` 형태의 마크다운 경로. 백틱 안팎을 모두 잡되 확장자로 좁힌다.
    PATTERN = re.compile(r"/?(docs/[A-Za-z0-9_\-./]+\.md)")

    def _referenced(self):
        found = {}
        for src in self.SOURCES:
            path = ROOT / src
            if not path.exists():
                continue
            for match in self.PATTERN.finditer(path.read_text(encoding="utf-8")):
                found.setdefault(match.group(1), src)
        return found

    def test_every_referenced_doc_exists(self):
        missing = ["%s ← %s" % (rel, src)
                   for rel, src in sorted(self._referenced().items())
                   if not (ROOT / rel).exists()]
        self.assertEqual([], missing,
                         "가리키는 문서가 없다 — 골격을 만들거나 참조를 지운다 "
                         "(ADR-H039)")

    def test_the_check_actually_found_something(self):
        """참조를 하나도 못 캐면 위 검사는 **아무것도 재지 않는다.**

        정규식이 조용히 안 맞게 되는 것이 이 검사가 죽는 가장 흔한 길이고,
        그때 초록불은 통과가 아니라 검사 부재다 (ADR-H007).
        """
        self.assertGreaterEqual(len(self._referenced()), 6)


class SchemaValidatorTest(unittest.TestCase):
    """스키마에 적었는데 검사되지 않는 규칙 = 이 계층에서 가장 위험한 조용한 통과."""

    def test_unsupported_keyword_raises(self):
        with self.assertRaises(harness.SchemaError):
            harness.validate({"a": 1}, {"type": "object", "oneOf": [{}]})

    def test_underscore_keys_allowed_under_additional_properties_false(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "integer"}},
        }
        self.assertEqual([], harness.validate({"a": 1, "_note": "주석"}, schema))
        self.assertNotEqual([], harness.validate({"a": 1, "b": 2}, schema))

    def test_type_list_and_null(self):
        schema = {"type": ["array", "null"], "items": {"type": "string"}}
        self.assertEqual([], harness.validate(None, schema))
        self.assertEqual([], harness.validate(["x"], schema))
        self.assertNotEqual([], harness.validate([1], schema))

    def test_bool_is_not_integer(self):
        self.assertNotEqual([], harness.validate(True, {"type": "integer"}))

    def test_local_ref(self):
        schema = {
            "definitions": {"n": {"type": "integer", "minimum": 1}},
            "type": "object",
            "properties": {"x": {"$ref": "#/definitions/n"}},
        }
        self.assertEqual([], harness.validate({"x": 3}, schema))
        self.assertNotEqual([], harness.validate({"x": 0}, schema))

    def test_error_path_points_at_the_offending_key(self):
        schema = {
            "type": "object",
            "properties": {"budget": {"type": "object", "properties": {"files_max": {"type": "integer"}}}},
        }
        errs = harness.validate({"budget": {"files_max": "10"}}, schema)
        self.assertTrue(any("budget.files_max" in e for e in errs), errs)


class GlobTest(unittest.TestCase):
    """소유 경계 판정이 이 함수 하나에 달려 있다."""

    def assertMatch(self, pattern, path, expected=True):
        self.assertEqual(expected, harness.glob_match(pattern, path), "%s vs %s" % (pattern, path))

    def test_double_star(self):
        self.assertMatch("src/lib/**", "src/lib/match.ts")
        self.assertMatch("src/lib/**", "src/lib/deep/nested.ts")
        self.assertMatch("src/lib/**", "src/app/page.tsx", False)

    def test_leading_double_star(self):
        self.assertMatch("**/*.test.ts", "src/lib/match.test.ts")
        self.assertMatch("**/*.test.ts", "match.test.ts")
        self.assertMatch("**/*.test.ts", "src/lib/match.ts", False)

    def test_single_star_does_not_cross_slash(self):
        self.assertMatch("src/*.ts", "src/a.ts")
        self.assertMatch("src/*.ts", "src/lib/a.ts", False)

    def test_dotted_pattern(self):
        self.assertMatch("*.config.*", "vitest.config.ts")
        self.assertMatch("*.config.*", "src/vitest.config.ts", False)

    def test_exact_path(self):
        self.assertMatch("package.json", "package.json")
        self.assertMatch("package.json", "sub/package.json", False)


class InitTest(DoctorTestBase):
    def test_refuses_to_overwrite_existing_config(self):
        self.assertEqual(2, harness.run_init(self.root, adapter="nextjs-ts", name="demo"))

    def test_creates_config_from_profile_seed(self):
        target = self.root / "harness/config.json"
        target.unlink()
        self.assertEqual(0, harness.run_init(self.root, adapter="nextjs-ts", name="bookshelf"))
        raw = target.read_text(encoding="utf-8")
        cfg = json.loads(raw)
        self.assertEqual("bookshelf", cfg["project"]["name"])
        self.assertEqual("nextjs-ts", cfg["adapter"])
        self.assertNotIn("{{", raw)

    def test_seeded_config_passes_doctor(self):
        """시드가 doctor를 통과하지 못하면 init은 깨진 리포를 만드는 것이다."""
        (self.root / "harness/config.json").unlink()
        harness.run_init(self.root, adapter="nextjs-ts", name="bookshelf")
        report = self.doctor()
        self.assertEqual(0, report.exit_code, report.text())

    def test_force_overwrites(self):
        self.assertEqual(0, harness.run_init(self.root, adapter="nextjs-ts", name="other", force=True))
        self.assertEqual("other", self.config()["project"]["name"])

    def test_unknown_adapter_refused(self):
        (self.root / "harness/config.json").unlink()
        self.assertEqual(2, harness.run_init(self.root, adapter="nope", name="x"))

    def test_invalid_name_refused(self):
        (self.root / "harness/config.json").unlink()
        self.assertEqual(2, harness.run_init(self.root, adapter="nextjs-ts", name="Not A Slug"))


JUNIT_FIXTURE = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="203" failures="0" errors="0" time="0.22">
  <testsuite name="a.test.ts" tests="120" failures="0" errors="0" />
  <testsuite name="b.test.ts" tests="83" failures="0" errors="0" />
</testsuites>
"""


class JunitByFileTest(unittest.TestCase):
    """파일별 케이스 수 — PR 본문의 「무엇이 검증됐나」 표가 읽는다 (ADR-H058 추기)."""

    ADAPTER = {"test_report": {"format": "junit-xml", "glob": ["reports/*.xml"]}}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def parse(self, xml=None):
        if xml is not None:
            _write(self.root / "reports" / "r.xml", xml)
        return harness.parse_test_report(self.root, self.ADAPTER)

    def test_file_속성이_먼저고_없으면_classname(self):
        got = self.parse(
            '<testsuites tests="3"><testsuite tests="3">'
            '<testcase file="src/lib/match.test.ts" classname="x" name="a"/>'
            '<testcase file="src/lib/match.test.ts" classname="x" name="b"/>'
            '<testcase classname="src\\app\\api\\route.test.ts" name="c"/>'
            '</testsuite></testsuites>')
        self.assertEqual({"src/lib/match.test.ts": 2, "src/app/api/route.test.ts": 1},
                         got["by_file"])

    def test_케이스가_없으면_빈_dict(self):
        self.assertEqual({}, self.parse(JUNIT_FIXTURE)["by_file"])

    def test_리포트가_없으면_None(self):
        self.assertIsNone(self.parse()["by_file"])


class VerifyAdapterTest(DoctorTestBase):
    """[[ADR-H047]] 결정 3 — 어댑터 `verified` 는 완주 런 수로 올린다.

    15런을 완주한 뒤에도 `verified: false` 였고 그래서 `PASS` 가 구조적으로
    나올 수 없었다(`adapter_unverified` 가 매 런 gap). 일부러 실패를 만들지
    않는다 — 실패를 만든 것이 아니라 **완주를 셌다.**
    """

    PHASES = ["00-triage", "01-plan", "02-cross-verify", "03-implement",
              "04-gate", "05-code-review", "06-pr", "07-pr-review", "08-report"]

    def setUp(self):
        super().setUp()
        for pid in self.PHASES:
            _write(self.root / "harness" / "phases" / (pid + ".md"), "---\n---\n")
        _write(self.root / "harness" / "calibration.json", json.dumps({
            "measured_at": "2026-01-01T00:00:00+0900", "adapter": "nextjs-ts",
            "adapter_verified": False, "stages": {}, "derived": {}}))

    def _run(self, run_id, adapter="nextjs-ts", statuses=None):
        ph = {pid: {"status": (statuses or {}).get(pid, "passed")}
              for pid in self.PHASES}
        _write(self.root / "_workspace" / "runs" / run_id / "state.json",
               json.dumps({"run_id": run_id, "run_status": "done",
                           "closed_at": "2026-02-01T00:00:00+0900",
                           "adapter": {"id": adapter}, "phases": ph}))

    def test_완주_런이_기준을_채우면_올린다(self):
        for i in range(harness.ADAPTER_VERIFY_MIN_RUNS):
            self._run("r%d" % i)
        self.assertEqual(0, harness.run_verify_adapter(self.root))
        ad = self.adapter()
        self.assertTrue(ad["verified"])
        self.assertIn("verify-adapter", ad["_verified_note"])
        self.assertIn("r0", ad["_verified_note"])
        cal = self._load("harness/calibration.json")
        self.assertTrue(cal["adapter_verified"])
        self.assertEqual(harness.ADAPTER_VERIFY_MIN_RUNS,
                         len(cal["adapter_verified_source"]["runs"]))

    def test_부족하면_exit_3_이고_아무것도_안_바꾼다(self):
        for i in range(harness.ADAPTER_VERIFY_MIN_RUNS - 1):
            self._run("r%d" % i)
        self.assertEqual(3, harness.run_verify_adapter(self.root))
        self.assertFalse(self.adapter()["verified"])
        self.assertFalse(self._load("harness/calibration.json")["adapter_verified"])

    def test_skipped_페이즈가_있는_런은_세지_않는다(self):
        """정책 생략은 관측이 없었던 것이다 — ADR 문면대로 `passed` 만 센다."""
        for i in range(harness.ADAPTER_VERIFY_MIN_RUNS):
            self._run("r%d" % i)
        self._run("r0", statuses={"02-cross-verify": "skipped"})
        self.assertEqual(3, harness.run_verify_adapter(self.root))
        self.assertFalse(self.adapter()["verified"])

    def test_다른_어댑터의_런은_세지_않는다(self):
        for i in range(harness.ADAPTER_VERIFY_MIN_RUNS):
            self._run("r%d" % i, adapter="self-python")
        self.assertEqual(3, harness.run_verify_adapter(self.root))
        self.assertFalse(self.adapter()["verified"])

    def test_기준은_인자로_낮출_수_있다(self):
        self._run("r0")
        self.assertEqual(0, harness.run_verify_adapter(self.root, min_runs=1))
        self.assertTrue(self.adapter()["verified"])


class CalibrateTest(DoctorTestBase):
    """실측이 정책의 입력이 되므로, 재지 않은 것을 잰 척하는 경로가 하나도 없어야 한다."""

    def setUp(self):
        super().setUp()
        _write(self.root / "reports/junit/vitest.xml", JUNIT_FIXTURE)

    def fake_runner(self, durations=None, failing=None):
        """(stage, cmd, cwd, timeout) -> (exit_code, seconds). 실제 npm 을 돌리지 않는다."""
        durations = durations or {}
        failing = set(failing or [])
        calls = []

        def run(stage, cmd, cwd, timeout_sec):
            calls.append((stage, tuple(cmd)))
            return (1 if stage in failing else 0), durations.get(stage, 1.0)

        run.calls = calls
        return run

    def load(self):
        return json.loads((self.root / "harness/calibration.json").read_text(encoding="utf-8"))

    # --- 기본 동작 ---

    def test_writes_calibration_file(self):
        code = harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        self.assertEqual(0, code)
        data = self.load()
        self.assertEqual("nextjs-ts", data["adapter"])
        self.assertIn("measured_at", data)
        self.assertEqual(4.7, data["stages"]["full"]["sec"])
        self.assertTrue(data["stages"]["full"]["ok"])

    def test_carries_the_retry_ledger(self):
        """attempts 원장이 calibration 에 실려야 상한을 유도할 수 있다 (ADR-H007)."""
        _write(self.root / "phases" / "p" / "index.json",
               json.dumps({"steps": [{"step": 0, "name": "a",
                                      "status": "completed", "attempts": 2}]}))
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        data = self.load()
        self.assertEqual(1, data["retry"]["steps_recorded"])
        self.assertEqual(2, data["retry"]["max_attempts_observed"])
        self.assertIn("retry_budget", data["derived"])

    def test_null_cmd_stage_is_skipped_not_measured(self):
        runner = self.fake_runner()
        harness.run_calibrate(self.root, runner=runner)
        for stage in ("e2e", "docs"):
            entry = self.load()["stages"][stage]
            self.assertIsNone(entry["sec"])
            self.assertTrue(entry["skipped"])
            self.assertIn("cmd:null", entry["reason"])
        ran = set(stage for stage, _ in runner.calls)
        self.assertNotIn("e2e", ran)
        self.assertNotIn("docs", ran)

    def test_not_applicable_stage_is_na_not_absent(self):
        """구조적으로 없는 스테이지(`not_applicable`)는 부재와 다른 칸이다 (ADR-H047 추기)."""
        harness.run_calibrate(self.root, runner=self.fake_runner())
        stages = self.load()["stages"]
        self.assertEqual("na", stages["docs"]["state"])
        self.assertIn("문서 빌드", stages["docs"]["reason"])
        self.assertEqual("absent", stages["e2e"]["state"])

    def test_scoped_is_skipped_without_select(self):
        harness.run_calibrate(self.root, runner=self.fake_runner())
        entry = self.load()["stages"]["scoped"]
        self.assertIsNone(entry["sec"])
        self.assertTrue(entry["skipped"])
        self.assertIn("선택 대상", entry["reason"])

    def test_scoped_is_measured_with_select(self):
        """select 는 어댑터의 문법으로 붙는다.

        nextjs-ts 는 select 블록을 두지 않으므로 선택자가 인자로 그대로
        붙는다 = 경로 필터다. -t(이름 필터)로 재던 런 #3~#5 의 83%→101%→100%
        는 스코프 실행의 값이 아니었다 — 경로 필터는 14.6% 였다 (M16).
        """
        runner = self.fake_runner({"scoped": 2.0})
        harness.run_calibrate(self.root, runner=runner, select="src/lib/a.test.ts")
        entry = self.load()["stages"]["scoped"]
        self.assertEqual(2.0, entry["sec"])
        self.assertFalse(entry.get("skipped", False))
        scoped_cmd = next(cmd for stage, cmd in runner.calls if stage == "scoped")
        self.assertNotIn("-t", scoped_cmd)
        self.assertEqual("src/lib/a.test.ts", scoped_cmd[-1])

    # --- 부분 측정이 전체를 지우지 않는다 (파일럿 런 #3 M9) ---

    def test_stage_run_merges_into_existing_file(self):
        """--stage 는 그 스테이지만 갱신한다. 나머지 실측을 지우면 안 된다.

        실측이 정책의 유일한 근거인데 부분 측정 한 번이 전체 근거를 지우면
        되돌릴 방법이 재측정밖에 없다 (파일럿 런 #3 M9).
        """
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7, "build": 7.9}))
        harness.run_calibrate(self.root, stage="lint",
                              runner=self.fake_runner({"lint": 9.9}))

        stages = self.load()["stages"]
        self.assertEqual(9.9, stages["lint"]["sec"], "새로 잰 값이 반영돼야 한다")
        self.assertEqual(4.7, stages["full"]["sec"], "이전 실측이 살아 있어야 한다")
        self.assertEqual(7.9, stages["build"]["sec"])

    def test_stage_run_keeps_derived_policy(self):
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        harness.run_calibrate(self.root, stage="lint", runner=self.fake_runner())

        derived = self.load()["derived"]
        self.assertIsNotNone(derived["full_timeout_sec"])
        self.assertIsNotNone(derived["tests_ran_floor"])
        self.assertIs(False, derived["background_full_regression"])

    def test_merged_entries_keep_their_own_measured_at(self):
        """언제 잰 값인지 잃지 않는다 — 옛 값을 방금 잰 척하지 않는다."""
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}),
                              now="2026-01-01T00:00:00+0900")
        harness.run_calibrate(self.root, stage="lint", runner=self.fake_runner(),
                              now="2026-02-02T00:00:00+0900")

        data = self.load()
        self.assertEqual("2026-02-02T00:00:00+0900", data["measured_at"])
        self.assertEqual("2026-02-02T00:00:00+0900", data["stages"]["lint"]["measured_at"])
        self.assertEqual("2026-01-01T00:00:00+0900", data["stages"]["full"]["measured_at"])

    def test_partial_is_false_once_every_stage_is_known(self):
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        harness.run_calibrate(self.root, stage="lint", runner=self.fake_runner())
        self.assertFalse(self.load()["partial"])

    def test_partial_is_true_without_a_prior_file(self):
        harness.run_calibrate(self.root, stage="lint", runner=self.fake_runner())
        data = self.load()
        self.assertTrue(data["partial"])
        self.assertEqual({"lint"}, set(data["stages"]))

    def test_replace_discards_prior_measurements(self):
        """전체 교체는 명시적으로 요구했을 때만."""
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        harness.run_calibrate(self.root, stage="lint", runner=self.fake_runner(),
                              replace=True)
        data = self.load()
        self.assertEqual({"lint"}, set(data["stages"]))
        self.assertTrue(data["partial"])

    def test_single_stage_only(self):
        runner = self.fake_runner()
        harness.run_calibrate(self.root, stage="lint", runner=runner)
        self.assertEqual({"lint"}, set(stage for stage, _ in runner.calls))

    # --- 못 잰 것이 잰 것을 지우지 않는다 (파일럿 런 #5 M14) ---

    def test_full_run_keeps_a_prior_scoped_measurement(self):
        """--select 없는 전체 calibrate 가 scoped 의 실측을 덮으면 안 된다.

        M9 의 형제다 — 그때는 부분이 전체를 덮었고 이번은 전체가 부분을
        덮었다. 런 #4 의 21.81s 가 그렇게 사라졌고, 그런데도 파일은
        partial: false 라고 적어 doctor 가 통과시켰다.
        """
        harness.run_calibrate(self.root, runner=self.fake_runner({"scoped": 2.0}),
                              select="src/lib/a.test.ts", now="2026-01-01T00:00:00+0900")
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}),
                              now="2026-02-02T00:00:00+0900")

        entry = self.load()["stages"]["scoped"]
        self.assertEqual(2.0, entry["sec"], "옛 실측이 살아 있어야 한다")
        self.assertEqual("stale", entry["state"])
        self.assertEqual("2026-01-01T00:00:00+0900", entry["measured_at"],
                         "옛 값을 방금 잰 척하지 않는다")
        self.assertIn("선택 대상", entry["stale_reason"])

    def test_stale_stage_makes_the_file_partial(self):
        harness.run_calibrate(self.root, runner=self.fake_runner({"scoped": 2.0}),
                              select="src/lib/a.test.ts")
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        self.assertTrue(self.load()["partial"],
                        "이번 런이 재지 못한 칸이 있으면 완전하다고 적지 않는다")

    def test_three_states_are_distinguished(self):
        """없는 것 · 모르는 것 · 옛 값을 한 칸에 뭉개지 않는다."""
        harness.run_calibrate(self.root, runner=self.fake_runner({"scoped": 2.0}),
                              select="src/lib/a.test.ts")
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))

        stages = self.load()["stages"]
        self.assertEqual("absent", stages["e2e"]["state"], "cmd:null 은 없는 것이다")
        self.assertEqual("stale", stages["scoped"]["state"], "옛 값이 있다")
        self.assertEqual("measured", stages["full"]["state"])

    def test_unmeasured_stays_unmeasured_without_a_prior_value(self):
        """옛 값이 없으면 되살릴 것도 없다 — 모르는 것은 모르는 것이다."""
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        entry = self.load()["stages"]["scoped"]
        self.assertEqual("unmeasured", entry["state"])
        self.assertIsNone(entry["sec"])

    def test_stale_measurement_keeps_deriving_policy(self):
        """살린 값은 정책 유도에도 그대로 쓰인다 — 되살리는 이유가 그것이다."""
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        self.assertIsNotNone(self.load()["derived"]["full_timeout_sec"])

    def test_runner_bin_is_resolved_to_a_real_path(self):
        """Windows 에서 npm 은 npm.cmd 다.

        shutil.which 가 찾았다고 subprocess 가 bare name 으로 띄울 수 있는 것이
        아니다 — 해석된 경로를 argv[0] 에 넣어야 한다. shell=True 는 쓰지 않는다:
        러너 화이트리스트가 막으려는 임의 명령 실행 벡터가 되살아난다.
        """
        original = harness.shutil.which
        harness.shutil.which = lambda name: "C:\\fake\\npm.cmd" if name == "npm" else original(name)
        try:
            runner = self.fake_runner()
            harness.run_calibrate(self.root, stage="lint", runner=runner)
        finally:
            harness.shutil.which = original
        argv = next(cmd for stage, cmd in runner.calls if stage == "lint")
        self.assertEqual("C:\\fake\\npm.cmd", argv[0])

    def test_unresolvable_bin_blocks_before_running_anything(self):
        """해석되지 않는 러너는 doctor 가 먼저 막는다 — 잴 기회조차 오지 않는다.

        _resolve_bin 이 bare name 을 그대로 돌려주는 것이 곧 '못 찾았다'는 신호이고,
        doctor 의 러너 검사가 그 신호를 FAIL 로 바꾼다.
        """
        original = harness.shutil.which
        harness.shutil.which = lambda name: None
        try:
            self.assertEqual("npm", harness._resolve_bin(self.root, "npm"))
            runner = self.fake_runner()
            code = harness.run_calibrate(self.root, stage="lint", runner=runner)
        finally:
            harness.shutil.which = original
        self.assertEqual(2, code)
        self.assertEqual([], runner.calls)

    # --- 실패한 트리에서 잰 값은 캘리브레이션이 아니다 ---

    def test_failing_stage_exits_10_and_writes_nothing(self):
        code = harness.run_calibrate(self.root, runner=self.fake_runner(failing={"lint"}))
        self.assertEqual(10, code)
        self.assertFalse(
            (self.root / "harness/calibration.json").exists(),
            "빨간 트리에서 잰 값이 근거로 커밋되면 안 된다",
        )

    def test_refuses_when_doctor_fails(self):
        cfg = self.config()
        cfg["adapter"] = "does-not-exist"
        self.save_config(cfg)
        code = harness.run_calibrate(self.root, runner=self.fake_runner())
        self.assertEqual(2, code)
        self.assertFalse((self.root / "harness/calibration.json").exists())

    # --- junit 파싱 ---

    def test_parses_junit_report(self):
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        full = self.load()["stages"]["full"]
        self.assertEqual(203, full["tests_ran"])
        self.assertEqual(2, full["suites"])
        self.assertEqual(0, full["failures"])

    def test_missing_report_is_recorded_as_unmatched(self):
        (self.root / "reports/junit/vitest.xml").unlink()
        harness.run_calibrate(self.root, runner=self.fake_runner())
        data = self.load()
        self.assertFalse(data["report_glob_matched"])
        self.assertIsNone(data["stages"]["full"]["tests_ran"])

    # --- 정책은 상수가 아니라 캘리브레이션의 함수다 ---

    def test_derived_policy_short_suite(self):
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))
        d = self.load()["derived"]
        self.assertFalse(d["background_full_regression"])
        self.assertEqual(300, d["full_timeout_sec"])
        self.assertEqual(182, d["tests_ran_floor"])

    def test_derived_policy_long_suite(self):
        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 500.0}))
        d = self.load()["derived"]
        self.assertTrue(d["background_full_regression"])
        self.assertEqual(2000, d["full_timeout_sec"])

    # --- 시크릿 유출 차단 ---

    def test_env_probe_records_presence_not_value(self):
        import os

        os.environ["EXAMPLE_API_KEY"] = "sk-secret-value-do-not-leak"
        try:
            harness.run_calibrate(self.root, runner=self.fake_runner())
        finally:
            del os.environ["EXAMPLE_API_KEY"]
        raw = (self.root / "harness/calibration.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-secret-value-do-not-leak", raw)
        self.assertIs(True, self.load()["infra"]["api_key"])

    # --- doctor 연동 ---

    def test_doctor_stops_warning_once_calibrated(self):
        before = "\n".join(c.message for c in self.doctor().checks if c.status == "WARN")
        self.assertIn("캘리브레이션", before)

        harness.run_calibrate(self.root, runner=self.fake_runner({"full": 4.7}))

        report = self.doctor()
        self.assertEqual(0, report.exit_code, report.text())
        calibration = next(c for c in report.checks if c.name == "캘리브레이션 상태")
        self.assertEqual("PASS", calibration.status, report.text())
        # 어댑터 검증과 캘리브레이션은 다른 것이다 — verified:false 경고는 남는다
        warns = "\n".join(c.message for c in report.checks if c.status == "WARN")
        self.assertIn("verified", warns)


class RetryBudgetTest(unittest.TestCase):
    """재시도 상한을 실측에서 유도한다 (ROADMAP 17 · ADR-H007).

    MAX_RETRIES = 3 은 파일럿 20 step 동안 한 번도 발동하지 않은 상수였다.
    더 나쁜 것은 그 20 step 이 attempts 를 **어디에도 남기지 않았다**는 점이다 —
    "재시도 0회"는 콘솔을 지켜본 사람의 기억이지 데이터가 아니었다.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _phase(self, name, steps):
        _write(self.tmp / "phases" / name / "index.json",
               json.dumps({"steps": steps}, ensure_ascii=False))

    def test_no_phases_at_all(self):
        got = harness._collect_retry(self.tmp)
        self.assertEqual(0, got["steps_recorded"])
        self.assertIsNone(got["max_attempts_observed"])

    def test_finished_steps_without_attempts_are_unrecorded_not_zero(self):
        """미측정과 0을 같은 칸에 쓰지 않는다 — 이 파일 전체의 규율이다."""
        self._phase("lib-core", [{"step": 0, "name": "a", "status": "completed"},
                                 {"step": 1, "name": "b", "status": "completed"}])
        got = harness._collect_retry(self.tmp)
        self.assertEqual(0, got["steps_recorded"])
        self.assertEqual(2, got["steps_unrecorded"])
        self.assertEqual(0, got["steps_retried"])

    def test_pending_steps_are_neither(self):
        self._phase("x", [{"step": 0, "name": "a", "status": "pending"}])
        got = harness._collect_retry(self.tmp)
        self.assertEqual(0, got["steps_recorded"])
        self.assertEqual(0, got["steps_unrecorded"])

    def test_counts_attempts_across_phases(self):
        self._phase("one", [{"step": 0, "name": "a", "status": "completed", "attempts": 1},
                            {"step": 1, "name": "b", "status": "completed", "attempts": 3}])
        self._phase("two", [{"step": 0, "name": "c", "status": "error", "attempts": 2},
                            {"step": 1, "name": "d", "status": "completed"}])
        got = harness._collect_retry(self.tmp)
        self.assertEqual(3, got["steps_recorded"])
        self.assertEqual(1, got["steps_unrecorded"])
        self.assertEqual(2, got["steps_retried"])
        self.assertEqual(3, got["max_attempts_observed"])

    def test_broken_index_is_skipped_not_fatal(self):
        _write(self.tmp / "phases" / "bad" / "index.json", "{ not json")
        self._phase("good", [{"step": 0, "name": "a", "status": "completed", "attempts": 1}])
        self.assertEqual(1, harness._collect_retry(self.tmp)["steps_recorded"])

    # --- 유도 ---

    def test_budget_is_none_below_the_sample_floor(self):
        retry = {"steps_recorded": harness.RETRY_RECORD_MIN - 1, "steps_unrecorded": 0,
                 "steps_retried": 0, "max_attempts_observed": 1}
        derived = harness._derive_policy({}, {}, retry)
        self.assertIsNone(derived["retry_budget"],
                          "표본이 모자라면 상한을 내지 않는다 — 상수를 박는 것과 같아진다")

    def test_budget_derived_once_the_sample_is_enough(self):
        retry = {"steps_recorded": harness.RETRY_RECORD_MIN, "steps_unrecorded": 0,
                 "steps_retried": 0, "max_attempts_observed": 1}
        self.assertEqual(harness.RETRY_BUDGET_FLOOR,
                         harness._derive_policy({}, {}, retry)["retry_budget"])

    def test_budget_leaves_headroom_above_what_was_observed(self):
        retry = {"steps_recorded": 20, "steps_unrecorded": 0,
                 "steps_retried": 4, "max_attempts_observed": 3}
        self.assertEqual(4, harness._derive_policy({}, {}, retry)["retry_budget"])

    def test_derive_without_retry_input_keeps_the_key_null(self):
        self.assertIsNone(harness._derive_policy({}, {})["retry_budget"])


class RealRepoTest(unittest.TestCase):
    """실물 리포에서도 통과해야 한다 — 픽스처만 통과하는 것은 의미가 없다."""

    def test_doctor_passes_on_this_repo(self):
        report = harness.run_doctor(ROOT)
        self.assertEqual(0, report.exit_code, report.text())

    def test_cli_exit_code(self):
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "harness.py"), "doctor"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(0, r.returncode, (r.stdout or "") + (r.stderr or ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
