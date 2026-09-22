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

    def test_unmatched_test_report_warns(self):
        """리포트를 셀 수 없는 상태는 통과가 아니라 경고다."""
        report = self.doctor()
        check = next(c for c in report.checks if c.name == "테스트 리포트")
        self.assertEqual("WARN", check.status, report.text())
        self.assertIn("매칭 0건", check.message)


class BrokenConfigRejectedTest(DoctorTestBase):
    """G1 — 깨뜨린 config 8종을 전부 거부하는가."""

    def test_1_role_ownership_overlap(self):
        cfg = self.config()
        cfg["roles"].append({"id": "dup", "agent": "impl-writer", "code": "dup",
                             "owns": ["src/lib/**"]})
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "src/lib/match.ts")

    def test_2_contract_section_mismatch(self):
        cfg = self.config()
        cfg["contract"]["sections"]["units"] = "## 존재하지않는절"
        self.save_config(cfg)
        self.assertRejected(self.doctor(), "## 존재하지않는절")

    def test_3_runner_bin_not_whitelisted(self):
        ad = self.adapter()
        ad["runner"]["bin"] = "sh"
        self.save_adapter(ad)
        self.assertRejected(self.doctor(), "sh")

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
    라 정했다) `CLAUDE.md` 의 문서 표는 그것들을
    읽으라고 가리켰다. **가리키는 쪽과 가리켜지는 쪽이 어긋나도 아무것도 안
    깨진다** — 클론하는 사람이 없는 파일을 찾다 포기할 뿐이다.

    `CoreHasNoStackNamesTest` 가 "있으면 안 되는 것"을 잡는다면 이쪽은 "없으면
    안 되는 것"을 잡는다. 세션 F 가 걸었다 (ADR-H039).
    """

    #: 참조를 캐낼 파일들. 산문이 아니라 **경로를 지시로 쓰는** 자리만 본다.
    SOURCES = ("README.md", "CLAUDE.md", ".claude/commands/feature.md")

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


class ProfileParityTest(unittest.TestCase):
    """프로필 시드가 템플릿 config 의 키를 빠뜨리면 클론에서 그 기능이 조용히
    꺼진다 — `reviewers` 가 없어 05 가 통째로 비활성이던 결함이다 (ADR-H063)."""

    def _load(self, rel):
        return json.loads((ROOT / rel).read_text(encoding="utf-8"))

    def test_profiles_declare_every_template_key(self):
        tmpl = {k for k in self._load("harness/config.json") if not k.startswith("_")}
        for prof in sorted((ROOT / "harness" / "profiles").glob("*/config.json")):
            got = {k for k in json.loads(prof.read_text(encoding="utf-8"))
                   if not k.startswith("_")}
            self.assertEqual(tmpl, got, prof)

    def test_profiles_carry_the_template_review_blocks(self):
        tmpl = self._load("harness/config.json")
        prof = self._load("harness/profiles/nextjs-ts/config.json")
        for key in ("reviewers", "review"):
            self.assertEqual(tmpl[key], prof[key], key)

    def test_schema_requires_the_review_blocks(self):
        schema = self._load("harness/config.schema.json")
        for key in ("reviewers", "review"):
            self.assertIn(key, schema["required"])
        prof = self._load("harness/profiles/nextjs-ts/config.json")
        prof["project"]["name"] = "fixture"  # `{{name}}` 은 init 이 채운다
        self.assertEqual([], harness.validate(prof, schema))


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
