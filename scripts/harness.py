#!/usr/bin/env python3
"""Harness 계약 계층 CLI — init · doctor.

하네스는 프로젝트의 언어를 쓰지 않는다. python stdlib 만으로 돌아가므로
의존성·빌드 산출물이 깨진 상태에서도 게이트가 동작한다.

Usage:
    python scripts/harness.py doctor
    python scripts/harness.py init --adapter nextjs-ts --name my-app [--force]

종료 코드:
    0  통과 (경고는 허용한다 — 단, 전부 출력에 드러난다)
    2  미통과. 무엇이 어긋났는지 출력에 명시된다

순차 step 실행기는 이 템플릿에 없다 (ADR-H037). 8페이즈 실행기는
scripts/pipeline/cli.py 다.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent

CONFIG_REL = "harness/config.json"
CONFIG_SCHEMA_REL = "harness/config.schema.json"
ADAPTER_DIR_REL = "harness/adapters"
ADAPTER_SCHEMA_REL = "harness/adapters/adapter.schema.json"
CONTRACT_TEMPLATE_REL = "harness/templates/contract.md"
PHASES_DIR_REL = "harness/phases"
# 런 디렉터리. `state.py` 가 이 값을 그대로 쓴다 — 완주 런을 세는 쪽
# (`completed_runs`)이 이 층에 있어야 다른 층이 같은 것을 센다. state 는
# harness 를 import 하므로 역방향은 안 된다.
RUNS_REL = "_workspace/runs"

PROFILE_DIR_REL = "harness/profiles"

# 경로 240자 상한 — 한글 식별자가 흔한 리포에서 이게 깨지면 원장이 조용히 오염된다
PATH_LIMIT = 240

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# 파일 목록 폴백 탐색에서 건너뛰는 디렉토리
WALK_SKIP = {".git", "node_modules", ".next", "out", "dist", "build", "target",
             "reports", "__pycache__", ".venv", "coverage"}


# ---------------------------------------------------------------- 스키마 검증기
#
# 서드파티 금지(jsonschema·PyYAML 등)이므로 필요한 만큼만 직접 해석한다.
# 미지원 키워드를 조용히 무시하지 않는 것이 이 검증기의 핵심이다 —
# "스키마에 적었는데 검사되지 않는 규칙"이 이 계층에서 가장 위험한 조용한 통과다.

class SchemaError(Exception):
    """스키마 자체가 잘못됐다 (인스턴스가 아니라)."""


SUPPORTED_KEYWORDS = {
    "$schema", "$id", "$ref", "title", "description", "definitions",
    "type", "enum", "required", "properties", "additionalProperties",
    "items", "minItems", "minimum", "maximum", "pattern",
}

TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _label(path):
    return path or "<root>"


def _resolve_ref(ref, root_schema):
    if not ref.startswith("#/"):
        raise SchemaError("로컬 $ref 만 지원한다: %r" % ref)
    node = root_schema
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise SchemaError("$ref 를 찾을 수 없다: %r" % ref)
        node = node[part]
    return node


def validate(instance, schema, root_schema=None, path=""):
    """인스턴스를 스키마로 검증하고 사람이 읽을 오류 문자열 목록을 반환한다."""
    if root_schema is None:
        root_schema = schema
    if not isinstance(schema, dict):
        raise SchemaError("스키마 노드가 객체가 아니다: %r" % (schema,))

    unknown = set(schema) - SUPPORTED_KEYWORDS
    if unknown:
        raise SchemaError(
            "%s: 이 검증기가 해석하지 못하는 스키마 키워드 %s — "
            "조용히 무시하지 않는다. 검증기에 추가하거나 스키마에서 빼라."
            % (_label(path), sorted(unknown))
        )

    if "$ref" in schema:
        extra = set(schema) - {"$ref", "description", "title"}
        if extra:
            raise SchemaError("%s: $ref 와 %s 를 같이 쓸 수 없다" % (_label(path), sorted(extra)))
        return validate(instance, _resolve_ref(schema["$ref"], root_schema), root_schema, path)

    errors = []

    if "type" in schema:
        types = schema["type"]
        types = [types] if isinstance(types, str) else types
        for t in types:
            if t not in TYPE_CHECKS:
                raise SchemaError("%s: 알 수 없는 type %r" % (_label(path), t))
        if not any(TYPE_CHECKS[t](instance) for t in types):
            return ["%s: 타입이 %s 여야 하는데 %s 다" % (_label(path), "|".join(types), _typename(instance))]
        if instance is None:
            return []  # null 허용 — 이하 키워드는 적용 대상이 아니다

    if "enum" in schema:
        if not any(_json_equal(instance, v) for v in schema["enum"]):
            errors.append("%s: %r 는 허용된 값이 아니다 (%s)"
                          % (_label(path), instance, " | ".join(repr(v) for v in schema["enum"])))

    if isinstance(instance, dict):
        errors.extend(_validate_object(instance, schema, root_schema, path))
    elif isinstance(instance, list):
        errors.extend(_validate_array(instance, schema, root_schema, path))
    elif isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append("%s: %r 가 패턴 %s 에 맞지 않는다" % (_label(path), instance, schema["pattern"]))
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("%s: %r 는 최소 %r 미만이다" % (_label(path), instance, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("%s: %r 는 최대 %r 초과다" % (_label(path), instance, schema["maximum"]))

    return errors


def _validate_object(instance, schema, root_schema, path):
    errors = []
    props = schema.get("properties", {})

    for key in schema.get("required", []):
        if key not in instance:
            errors.append("%s: 필수 키 %r 가 없다" % (_label(path), key))

    if schema.get("additionalProperties") is False:
        for key in instance:
            # 밑줄로 시작하는 키는 주석이다 — JSON 에 주석이 없어서 쓰는 관례
            if key not in props and not key.startswith("_"):
                errors.append("%s: 알 수 없는 키 %r" % (_label(path), key))

    for key, subschema in props.items():
        if key in instance:
            child = "%s.%s" % (path, key) if path else key
            errors.extend(validate(instance[key], subschema, root_schema, child))

    return errors


def _validate_array(instance, schema, root_schema, path):
    errors = []
    if "minItems" in schema and len(instance) < schema["minItems"]:
        errors.append("%s: 항목이 %d 개 이상이어야 한다 (현재 %d)"
                      % (_label(path), schema["minItems"], len(instance)))
    if "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], root_schema, "%s[%d]" % (_label(path), i)))
    return errors


def _json_equal(a, b):
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _typename(value):
    for name, check in TYPE_CHECKS.items():
        if name != "number" and check(value):
            return name
    return type(value).__name__


# ---------------------------------------------------------------------- glob
#
# fnmatch 의 * 는 / 를 넘어가므로 소유 경계 판정에 쓸 수 없다. 직접 번역한다.

_GLOB_CACHE = {}


def _glob_regex(pattern):
    cached = _GLOB_CACHE.get(pattern)
    if cached:
        return cached
    out = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == n:
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    compiled = re.compile("^" + "".join(out) + "$")
    _GLOB_CACHE[pattern] = compiled
    return compiled


def glob_match(pattern, path):
    """POSIX 경로 하나가 glob 하나에 맞는가. ** 는 디렉토리를 가로지르고 * 는 넘지 않는다."""
    return bool(_glob_regex(pattern).match(path))


def glob_any(patterns, path):
    return any(glob_match(p, path) for p in patterns or [])


# -------------------------------------------------------------------- 보고서

class Check(object):
    __slots__ = ("name", "status", "message")

    def __init__(self, name, status, message=""):
        self.name = name
        self.status = status  # PASS | WARN | FAIL | SKIP
        self.message = message


class Report(object):
    def __init__(self):
        self.checks = []

    def add(self, name, status, message=""):
        self.checks.append(Check(name, status, message))
        return self.checks[-1]

    @property
    def failures(self):
        return [c for c in self.checks if c.status == "FAIL"]

    @property
    def warnings(self):
        return [c for c in self.checks if c.status == "WARN"]

    @property
    def exit_code(self):
        return 2 if self.failures else 0

    def text(self):
        lines = []
        for c in self.checks:
            lines.append("  %-4s %s" % (c.status, c.name))
            for line in (c.message or "").splitlines():
                if line.strip():
                    lines.append("         %s" % line)
        lines.append("")
        if self.failures:
            lines.append("  doctor 미통과 — 실패 %d건. 위 FAIL 항목을 고친 뒤 다시 실행하라."
                         % len(self.failures))
        else:
            lines.append("  doctor 통과 (경고 %d건)." % len(self.warnings))
        return "\n".join(lines)


# ---------------------------------------------------------------------- 헬퍼

def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _git(root, *args):
    try:
        return subprocess.run(["git"] + list(args), cwd=str(root),
                              capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return None


def list_files(root):
    """리포의 파일 목록. git 이 있으면 추적 파일, 없으면 파일 시스템 탐색."""
    r = _git(root, "ls-files")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return sorted(line.strip() for line in r.stdout.splitlines() if line.strip())
    found = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in WALK_SKIP]
        for name in filenames:
            rel = Path(dirpath, name).relative_to(root).as_posix()
            found.append(rel)
    return sorted(found)


def list_files_with_untracked(root):
    """추적 파일 **+ 아직 커밋되지 않은 새 파일.**

    `list_files` 는 `git ls-files` 라 추적 파일만 낸다. 04 게이트와 05 의 계약
    대조가 도는 시점은 03 이 방금 코드를 쓴 직후이고 **그 파일들은 아직
    추적되지 않는다** — 추적분만 보면 새로 만든 유닛이 컨테이너를 못 찾아
    `unmatched` 로 떨어지고, 그 유닛의 테스트가 `scoped` 선택에서 통째로 빠진다.
    **이 검사가 가장 필요한 런에서 정확히 반대로 동작한다** (M50).

    무시 목록(`--exclude-standard`)은 존중한다. `_workspace/` 의 계약 파일이
    소스로 세어지면 안 된다.

    git 이 없으면 `list_files` 의 파일 시스템 탐색이 이미 미추적을 포함하므로
    그대로 돌려준다.
    """
    files = set(list_files(root))
    r = _git(root, "ls-files", "--others", "--exclude-standard")
    if r is not None and r.returncode == 0:
        files |= {line.strip().replace("\\", "/")
                  for line in r.stdout.splitlines() if line.strip()}
    return sorted(files)


def _json_pointer(data, pointer):
    node = data
    for part in pointer.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def effective_owner_globs(role):
    return role.get("owns", []), role.get("excludes", [])


def owns_file(role, path):
    owns, excludes = effective_owner_globs(role)
    return glob_any(owns, path) and not glob_any(excludes, path)


# --------------------------------------------------------------------- doctor

def run_doctor(root):
    root = Path(root)
    report = Report()

    _check_runtime(report)
    config = _check_config(root, report)
    if config is None:
        _skip_rest(report, ["어댑터", "어댑터 전제조건", "러너 바이너리", "스테이지 명령",
                            "스킵될 스테이지", "역할", "소유 경계", "메인 소유 경계",
                            "계약 절 ↔ 템플릿", "VCS", "경로 길이", "테스트 리포트"])
        return report

    adapter = _check_adapter(root, config, report)
    if adapter is None:
        _skip_rest(report, ["어댑터 전제조건", "러너 바이너리", "스테이지 명령", "스킵될 스테이지",
                            "테스트 리포트"])
    else:
        _check_adapter_requires(root, adapter, report)
        _check_runner_bin(root, adapter, report)
        _check_stage_commands(root, adapter, report)
        _report_null_stages(adapter, report)

    files = list_files(root)
    _check_roles(root, config, report)
    _check_ownership(config, files, report)
    _check_main_owned(config, files, report)
    _check_contract_sections(root, config, report)
    _check_vcs(root, config, report)
    _check_path_limit(root, config, report)
    if adapter is not None:
        _check_test_report(root, adapter, report)

    return report


def _skip_rest(report, names):
    for name in names:
        report.add(name, "SKIP", "앞선 실패 때문에 검사하지 못했다.")


def _check_runtime(report):
    version = "%d.%d.%d" % sys.version_info[:3]
    if sys.version_info < (3, 8):
        report.add("python 런타임", "FAIL",
                   "python 3.8 이상이 필요하다 (현재 %s). 프로젝트 스택과 무관하게 "
                   "하네스가 요구하는 유일한 외부 의존이다." % version)
        return
    # 파일 I/O 는 전부 encoding="utf-8" 로 못박혀 있으므로 여기서 보는 것은 콘솔 출력뿐이다.
    # 출력이 깨지는 것은 원장을 오염시키지 않으므로 차단이 아니라 경고다.
    if not _can_print_non_ascii():
        report.add("python 런타임", "WARN",
                   "python %s · stdout 인코딩(%s)이 비ASCII를 못 쓴다. 보고서 출력이 깨진다 — "
                   "PYTHONIOENCODING=utf-8 로 실행하라. 파일 I/O 는 영향받지 않는다."
                   % (version, sys.stdout.encoding))
        return
    report.add("python 런타임", "PASS", "python %s · stdout %s" % (version, sys.stdout.encoding))


def _can_print_non_ascii():
    probe = "한글 · 経路"
    for attempt in range(2):
        try:
            probe.encode(sys.stdout.encoding or "utf-8")
            return True
        except (UnicodeEncodeError, LookupError):
            if attempt == 0:
                try:
                    sys.stdout.reconfigure(encoding="utf-8")
                except (AttributeError, ValueError):
                    return False
    return False


def _check_config(root, report):
    path = root / CONFIG_REL
    if not path.exists():
        report.add("config", "FAIL",
                   "%s 가 없다. `python scripts/harness.py init --adapter <id> --name <slug>` 로 만든다."
                   % CONFIG_REL)
        return None
    try:
        config = _read_json(path)
    except ValueError as exc:
        report.add("config", "FAIL", "%s 를 JSON 으로 읽을 수 없다: %s" % (CONFIG_REL, exc))
        return None

    schema_path = root / CONFIG_SCHEMA_REL
    if not schema_path.exists():
        report.add("config", "FAIL", "%s 가 없다." % CONFIG_SCHEMA_REL)
        return None
    try:
        errors = validate(config, _read_json(schema_path))
    except SchemaError as exc:
        report.add("config", "FAIL", "%s 가 잘못됐다: %s" % (CONFIG_SCHEMA_REL, exc))
        return None

    if errors:
        report.add("config", "FAIL", "%s 스키마 위반 %d건:\n%s"
                   % (CONFIG_REL, len(errors), "\n".join(errors)))
        return None
    report.add("config", "PASS", "%s — 스키마 통과" % CONFIG_REL)
    return config


def _check_adapter(root, config, report):
    adapter_id = config["adapter"]
    path = root / ADAPTER_DIR_REL / ("%s.json" % adapter_id)
    if not path.exists():
        available = sorted(
            p.stem for p in (root / ADAPTER_DIR_REL).glob("*.json")
            if p.name != "adapter.schema.json"
        )
        report.add("어댑터", "FAIL",
                   "config.adapter 가 %r 를 가리키는데 %s 가 없다. 있는 어댑터: %s"
                   % (adapter_id, path.relative_to(root).as_posix(), ", ".join(available) or "없음"))
        return None
    try:
        adapter = _read_json(path)
    except ValueError as exc:
        report.add("어댑터", "FAIL", "%s 를 JSON 으로 읽을 수 없다: %s" % (path.name, exc))
        return None

    schema_path = root / ADAPTER_SCHEMA_REL
    if not schema_path.exists():
        report.add("어댑터", "FAIL", "%s 가 없다." % ADAPTER_SCHEMA_REL)
        return None
    schema = _read_json(schema_path)
    try:
        errors = validate(adapter, schema)
    except SchemaError as exc:
        report.add("어댑터", "FAIL", "%s 가 잘못됐다: %s" % (ADAPTER_SCHEMA_REL, exc))
        return None
    if errors:
        report.add("어댑터", "FAIL", "%s.json 스키마 위반 %d건:\n%s"
                   % (adapter_id, len(errors), "\n".join(errors)))
        return None

    # 선택되지 않은 어댑터도 깨져 있으면 언젠가 밟는다 — 경고로 드러낸다
    stale = []
    for other in sorted((root / ADAPTER_DIR_REL).glob("*.json")):
        if other.name in ("adapter.schema.json", "%s.json" % adapter_id):
            continue
        try:
            if validate(_read_json(other), schema):
                stale.append(other.name)
        except (ValueError, SchemaError):
            stale.append(other.name)
    notes = []
    if stale:
        notes.append("다른 어댑터가 스키마를 어긴다: %s" % ", ".join(stale))

    if notes:
        report.add("어댑터", "WARN", "%s.json — 스키마 통과.\n%s"
                   % (adapter_id, "\n".join("- " + n for n in notes)))
    else:
        report.add("어댑터", "PASS", "%s.json — 스키마 통과" % adapter_id)
    return adapter


def _check_adapter_requires(root, adapter, report):
    missing = []
    for req in adapter.get("requires", []):
        target = root / req["path"]
        kind = req["kind"]
        if kind == "file" and not target.is_file():
            missing.append("파일 %s 없음" % req["path"])
        elif kind == "dir" and not target.is_dir():
            missing.append("디렉토리 %s 없음" % req["path"])
        elif kind == "json_key":
            pointer = req.get("pointer", "")
            if not target.is_file():
                missing.append("파일 %s 없음 (%s 확인 불가)" % (req["path"], pointer))
                continue
            try:
                data = _read_json(target)
            except ValueError:
                missing.append("%s 를 JSON 으로 읽을 수 없음" % req["path"])
                continue
            if _json_pointer(data, pointer) is None:
                missing.append("%s 의 %s 없음" % (req["path"], pointer))
    if missing:
        report.add("어댑터 전제조건", "FAIL",
                   "어댑터가 요구하는 것이 리포에 없다 — 25분 뒤가 아니라 지금 막는다:\n" +
                   "\n".join("- " + m for m in missing))
    else:
        report.add("어댑터 전제조건", "PASS", "requires %d건 충족" % len(adapter.get("requires", [])))


def _check_runner_bin(root, adapter, report):
    binary = adapter["runner"]["bin"]
    resolved = _resolve_bin(root, binary)
    if resolved != binary:
        report.add("러너 바이너리", "PASS", "%s → %s" % (binary, resolved))
    else:
        report.add("러너 바이너리", "FAIL",
                   "runner.bin %r 를 PATH 에서도 리포 루트에서도 찾을 수 없다." % binary)


def _load_script_manifest(root, adapter):
    """어댑터가 선언한 스크립트 매니페스트를 읽는다.

    어느 파일의 어느 키에 스크립트가 선언돼 있고 러너가 그것을 어떤 동사로 부르는가는
    스택의 사실이다. 코어가 그 목록을 들면 같은 지식이 스키마 enum 과 코드 두 곳에
    살게 되고 한쪽만 고쳐지는 날이 온다 (ADR-H031).

    (scripts, verb, note) 를 돌려준다. 읽지 못했으면 scripts 는 None 이고 note 가
    왜 못 읽었는지 말한다 — 조용한 통과가 아니라 '검사 안 함'으로 드러내기 위한 값이다.
    """
    manifest = adapter["runner"].get("script_manifest")
    if not manifest:
        return None, None, ("어댑터에 runner.script_manifest 선언이 없다. "
                            "이 러너가 매니페스트의 스크립트를 부르는지 코어는 모른다")
    rel, pointer, verb = manifest["file"], manifest["pointer"], manifest["verb"]
    target = root / rel
    if not target.is_file():
        return None, verb, "script_manifest.file %s 가 리포에 없다" % rel
    try:
        data = _read_json(target)
    except ValueError:
        return None, verb, "%s 를 JSON 으로 읽을 수 없다" % rel
    node = _json_pointer(data, pointer)
    if not isinstance(node, dict):
        return None, verb, "%s 의 %s 가 없거나 스크립트 맵이 아니다" % (rel, pointer)
    return node, verb, None


def _check_stage_commands(root, adapter, report):
    scripts, verb, note = _load_script_manifest(root, adapter)
    problems = []
    checked = 0
    for name in sorted(adapter["stages"]):
        stage = adapter["stages"][name]
        cmd = stage.get("cmd")
        if not cmd:
            continue
        checked += 1
        if scripts is None:
            continue
        if cmd[0] == verb:
            if len(cmd) < 2:
                problems.append("stages.%s.cmd 가 `%s` 뒤에 스크립트명이 없다" % (name, verb))
            elif cmd[1] not in scripts:
                problems.append(
                    "stages.%s.cmd 가 매니페스트에 없는 스크립트 %r 를 참조한다 (있는 것: %s)"
                    % (name, cmd[1], ", ".join(sorted(scripts)) or "없음"))
    if problems:
        report.add("스테이지 명령", "FAIL", "\n".join("- " + p for p in problems))
    elif scripts is None:
        report.add("스테이지 명령", "WARN",
                   "스테이지 %d개의 명령이 실물 스크립트를 가리키는지 검사 안 함 — %s. "
                   "검사하지 않은 것이지 통과한 것이 아니다." % (checked, note))
    else:
        report.add("스테이지 명령", "PASS", "실행 가능한 스테이지 %d개 전부 실물과 일치" % checked)


def _report_null_stages(adapter, report):
    """cmd:null 은 '없는 것'이지 '통과한 것'이 아니다. 등급에 반영되도록 드러낸다."""
    stages = adapter["stages"]
    nulls = [name for name in sorted(stages) if not stages[name].get("cmd")]
    na = [n for n in nulls if str(stages[n].get("not_applicable") or "").strip()]
    nulls = [n for n in nulls if n not in na]
    if na:
        report.add("해당 없는 스테이지", "PASS",
                   "not_applicable — %s. 게이트는 `stage_na:<id>` 로 기록하고 "
                   "등급을 내리지 않는다." % ", ".join(na))
    if nulls:
        report.add("스킵될 스테이지", "WARN",
                   "cmd:null — %s. 게이트는 이 스테이지를 '스킵됨'으로 기록한다. "
                   "통과한 것이 아니며 등급에 반영된다." % ", ".join(nulls))
    else:
        report.add("스킵될 스테이지", "PASS", "8종 전부 명령이 있다")


def _check_roles(root, config, report):
    ids = [r["id"] for r in config["roles"]]
    problems = []
    if len(set(ids)) != len(ids):
        problems.append("roles[].id 가 중복된다: %s" % ids)
    if config["primary_role"] not in ids:
        problems.append("primary_role %r 가 roles[].id 에 없다 (%s)"
                        % (config["primary_role"], ", ".join(ids)))
    codes = [r["code"] for r in config["roles"]]
    if len(set(codes)) != len(codes):
        problems.append("roles[].code 가 중복된다: %s" % codes)
    if problems:
        report.add("역할", "FAIL", "\n".join("- " + p for p in problems))
        return
    missing_agents = [r["agent"] for r in config["roles"]
                      if not (root / ".claude" / "agents" / ("%s.md" % r["agent"])).exists()]
    if missing_agents:
        report.add("역할", "WARN",
                   "역할 %d개 정의됨. 에이전트 정의(.claude/agents/*.md)가 아직 없다: %s — "
                   "역할 분리 실행은 ROADMAP 3단계에서 붙는다."
                   % (len(ids), ", ".join(missing_agents)))
    else:
        report.add("역할", "PASS", "역할 %d개 · primary=%s" % (len(ids), config["primary_role"]))


def _check_ownership(config, files, report):
    """owns − excludes 의 실효 집합을 리포의 실제 파일에 대고 판정한다.

    glob 집합의 교집합을 추상적으로 계산하면 src/lib/** 와 src/**/*.test.ts 처럼
    excludes 를 거쳐야 비로소 disjoint 가 되는 정상 설정을 거짓 거부한다.
    """
    roles = config["roles"]
    overlaps = []
    unowned = []
    main_paths = config.get("main_owned_paths", [])

    for path in files:
        owners = [r["id"] for r in roles if owns_file(r, path)]
        if len(owners) > 1:
            overlaps.append("%s → %s" % (path, ", ".join(owners)))
        elif not owners and not glob_any(main_paths, path):
            unowned.append(path)

    if overlaps:
        report.add("소유 경계", "FAIL",
                   "같은 파일을 두 역할이 소유한다 — 소유 경계가 뜻을 잃는다:\n" +
                   "\n".join("- " + o for o in overlaps[:20]) +
                   ("\n  … 외 %d건" % (len(overlaps) - 20) if len(overlaps) > 20 else ""))
        return
    if unowned:
        report.add("소유 경계", "WARN",
                   "어느 역할도 소유하지 않고 메인 소유도 아닌 파일 %d건 — 워커가 손대도 "
                   "지문이 그것을 보지 못한다:\n%s%s"
                   % (len(unowned), "\n".join("- " + p for p in unowned[:20]),
                      "\n  … 외 %d건" % (len(unowned) - 20) if len(unowned) > 20 else ""))
        return
    report.add("소유 경계", "PASS", "파일 %d개 — 겹침 0건, 미소유 0건" % len(files))


def _check_main_owned(config, files, report):
    roles = config["roles"]
    main_paths = config.get("main_owned_paths", [])
    clashes = []
    for path in files:
        if not glob_any(main_paths, path):
            continue
        owners = [r["id"] for r in roles if owns_file(r, path)]
        if owners:
            clashes.append("%s → main + %s" % (path, ", ".join(owners)))
    if clashes:
        report.add("메인 소유 경계", "FAIL",
                   "메인 단독 소유여야 할 파일을 역할이 같이 소유한다 — 하네스 자신·문서·설정을 "
                   "워커가 고칠 수 있게 된다:\n" + "\n".join("- " + c for c in clashes[:20]))
    else:
        report.add("메인 소유 경계", "PASS", "main_owned_paths %d개 — 역할과 겹침 없음" % len(main_paths))


def _check_contract_sections(root, config, report):
    template = root / CONTRACT_TEMPLATE_REL
    if not template.is_file():
        report.add("계약 절 ↔ 템플릿", "FAIL", "%s 가 없다." % CONTRACT_TEMPLATE_REL)
        return
    headings = set()
    for line in template.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            headings.add(stripped)

    sections = config["contract"]["sections"]
    missing = [(key, value) for key, value in sorted(sections.items()) if value not in headings]
    unknown_required = [k for k in config["contract"]["required"] if k not in sections]

    problems = []
    if missing:
        problems.extend("contract.sections.%s = %r 가 %s 에 없다"
                        % (k, v, CONTRACT_TEMPLATE_REL) for k, v in missing)
    if unknown_required:
        problems.extend("contract.required 의 %r 가 sections 에 정의되지 않았다" % k
                        for k in unknown_required)
    if problems:
        report.add("계약 절 ↔ 템플릿", "FAIL",
                   "한쪽만 고치면 계약 추적이 아무것도 못 찾고 조용히 통과한다:\n" +
                   "\n".join("- " + p for p in problems))
    else:
        report.add("계약 절 ↔ 템플릿", "PASS", "절 %d개 일치" % len(sections))


def _check_vcs(root, config, report):
    vcs = config["vcs"]
    head = _git(root, "rev-parse", "--git-dir")
    if head is None:
        report.add("VCS", "WARN", "git 을 실행할 수 없다. 브랜치·원격 검사를 건너뛴다.")
        return
    if head.returncode != 0:
        report.add("VCS", "WARN",
                   "git 리포가 아니다. base_branch·원격 검사를 건너뛴다 — "
                   "PR 단계 전에 해소돼야 한다.")
        return

    base = vcs["base_branch"]
    if _git(root, "rev-parse", "--verify", "--quiet", base).returncode != 0:
        report.add("VCS", "FAIL",
                   "base_branch %r 가 없다. 하네스는 브랜치를 임의로 만들지 않는다." % base)
        return

    remote = _git(root, "remote", "get-url", vcs["remote"])
    if remote.returncode != 0:
        report.add("VCS", "WARN",
                   "원격 %r 가 없다. PR 단계에서 '원격을 붙이거나 로컬 커밋까지만'을 "
                   "묻게 된다 — 하네스가 원격을 자동으로 만들지는 않는다." % vcs["remote"])
        return
    report.add("VCS", "PASS", "base=%s · remote=%s" % (base, vcs["remote"]))


def _check_path_limit(root, config, report):
    slug = "x" * 40
    candidates = [
        config["contract"]["path_template"].replace("{slug}", slug),
    ]
    too_long = []
    for rel in [c for c in candidates if c]:
        full = str(root / rel)
        if len(full) > PATH_LIMIT:
            too_long.append("%s (%d자)" % (full, len(full)))
    if too_long:
        report.add("경로 길이", "FAIL",
                   "경로 %d자 상한을 넘는다 — 더 얕은 디렉토리에서 작업하거나 템플릿을 줄여라:\n%s"
                   % (PATH_LIMIT, "\n".join("- " + t for t in too_long)))
    else:
        report.add("경로 길이", "PASS", "하네스가 만들 경로가 %d자 이내" % PATH_LIMIT)


def _check_test_report(root, adapter, report):
    """테스트 리포트를 셀 수 있는가 — 빈 스위트의 초록불을 막는 유일한 신호다."""
    notes = []
    globs = adapter["test_report"]["glob"]
    matched = [p for g in globs for p in root.glob(g)]
    if adapter["test_report"]["format"] == "none":
        notes.append("test_report.format 이 none 이다 — 테스트가 몇 개 돌았는지 셀 수 없다. "
                     "빈 테스트 스위트가 초록불로 통과하는 것을 막을 수 없다.")
    elif not matched:
        notes.append("test_report.glob(%s) 매칭 0건 — 아직 테스트를 한 번도 돌리지 않았거나 "
                     "리포트 경로 설정이 어긋난 것이다. 게이트 전에 한 번 돌려 확인하라."
                     % ", ".join(globs))
    if notes:
        report.add("테스트 리포트", "WARN", "\n".join("- " + n for n in notes))
        return
    report.add("테스트 리포트", "PASS",
               "test_report.glob(%s) 매칭 %d건" % (", ".join(globs), len(matched)))


def completed_runs(root):
    """완주(`run_status: done`)한 런의 state 목록, `closed_at` 순.

    `_workspace/` 는 gitignore 라 **로컬에만 있는 자료**다 — 새 클론에서는
    0 이다. 그래서 여기서 유도하는 값(04 의 테스트 수 하한)은 없을 수 있고,
    없으면 없다고 적는다. 깨진 state 파일은 건너뛴다 — 세지 못한 런은 없는 런이다.
    """
    runs = Path(root) / RUNS_REL
    if not runs.is_dir():
        return []
    out = []
    for d in sorted(p for p in runs.iterdir() if p.is_dir()):
        try:
            s = _read_json(d / "state.json")
        except (OSError, ValueError):
            continue
        if isinstance(s, dict) and s.get("run_status") == "done":
            out.append(s)
    return sorted(out, key=lambda s: s.get("closed_at") or "")


# ------------------------------------------------------------------ 스테이지

# 이름 8종은 코어가 고정한다. 명령만 스택이 정한다.
STAGE_ORDER = ["compile", "lint", "check", "scoped", "full", "e2e", "build", "docs"]


def _resolve_bin(root, name):
    """러너 바이너리를 실행 가능한 경로로 해석한다.

    Windows 에서 러너 바이너리는 흔히 `.cmd` 래퍼이고, subprocess 는 shell 없이
    bare name 으로 이것을 띄우지 못한다(WinError 2). shell=True 로 푸는 것은 러너
    화이트리스트가 막으려는 임의 명령 실행 벡터를 되살리는 일이라 쓰지 않는다.
    """
    resolved = shutil.which(name)
    if resolved:
        return resolved
    local = root / name
    if local.exists():
        return str(local)
    return name


def _stage_argv(root, adapter, stage_name, select):
    runner_cfg = adapter["runner"]
    stage = adapter["stages"][stage_name]
    argv = ([_resolve_bin(root, runner_cfg["bin"])]
            + list(runner_cfg.get("common_args") or [])
            + list(stage["cmd"]))
    if stage_name == "scoped" and select:
        sel = stage.get("select")
        if sel:
            argv += [sel["flag"], select]
        else:
            argv += [select]
    return argv


EMPTY_REPORT = {"ran": None, "suites": None, "failures": None,
                "matched": False, "failed_units": [], "by_file": None}


def parse_test_report(root, adapter, report_root=None):
    """어댑터가 선언한 형식으로 테스트 리포트를 읽어 정규화한다.

    반환: {format, ran, suites, failures, matched, failed_units[]}

    **왜 여기에 있는가**: 파이프라인 코어(scripts/pipeline/)에는 스택 고유명사가
    한 건도 없어야 한다 (ADR-H013 의 3단계 게이트 1번). 형식 이름으로 분기하는
    자리를 코어에 두면 그 이름이 코어에 박히므로, 분기를 계약 계층인 이 파일에
    두고 코어는 이 함수만 부른다.

    `matched: False` 는 "리포트를 못 찾았다"이지 "테스트가 0개다"가 아니다.
    둘을 같은 칸에 넣으면 경로 설정 오류가 초록불로 통과한다 (team-spec P1).
    """
    fmt = (adapter.get("test_report") or {}).get("format")
    if fmt == "junit-xml":
        return dict(_junit_report(root, adapter, report_root), format=fmt)
    return dict(EMPTY_REPORT, format=fmt, failed_units=[])


def _junit_report(root, adapter, report_root=None):
    """junit XML 하나 이상을 합산한다. 못 찾으면 matched: False."""
    base = report_root or root
    paths = sorted(p for g in adapter["test_report"]["glob"] for p in base.glob(g))
    if not paths:
        return dict(EMPTY_REPORT, failed_units=[])
    tests = failures = suites = 0
    units = []
    by_file = {}
    for path in paths:
        try:
            tree = ElementTree.parse(str(path))
        except (ElementTree.ParseError, OSError):
            continue
        root_el = tree.getroot()
        found = root_el.findall(".//testsuite")
        suites += len(found)
        if root_el.tag == "testsuites" and root_el.get("tests") is not None:
            tests += int(root_el.get("tests") or 0)
            failures += int(root_el.get("failures") or 0)
        else:
            for suite in found:
                tests += int(suite.get("tests") or 0)
                failures += int(suite.get("failures") or 0)
        units.extend(_junit_failed_units(root_el))
        # 파일별 케이스 수 — PR 본문의 검증 표가 읽는다 (ADR-H058 추기).
        # `file` 속성이 먼저고, 없는 리포터는 `classname` 에 경로를 둔다.
        for case in root_el.iter("testcase"):
            key = (case.get("file") or case.get("classname") or "").replace("\\", "/")
            if key:
                by_file[key] = by_file.get(key, 0) + 1
    return {"ran": tests, "suites": suites, "failures": failures,
            "matched": True, "failed_units": units, "by_file": by_file}


def _junit_failed_units(root_el):
    """실패한 테스트를 {unit, file, ftype, message, detail} 로 뽑는다.

    `detail` 은 스택 원문 그대로다 — 프레임 추출은 여기서 하지 않는다.
    어느 토큰이 진짜 파일인지는 리포의 파일 목록을 알아야 판정할 수 있고,
    그 판정은 귀속 계층의 몫이다.
    """
    out = []
    for case in root_el.iter("testcase"):
        for tag in ("failure", "error"):
            for node in case.findall(tag):
                out.append({
                    "unit": case.get("name") or "",
                    "file": case.get("classname") or "",
                    "ftype": node.get("type") or "",
                    "message": (node.get("message") or "").strip(),
                    "detail": (node.text or "").strip(),
                })
    return out


def _parse_junit(root, adapter):
    """junit XML 에서 테스트 수를 읽는다. 없으면 (None, None, None, False).

    좁은 시각이다. 정본은 parse_test_report 다.
    """
    if adapter["test_report"]["format"] != "junit-xml":
        return None, None, None, False
    r = _junit_report(root, adapter)
    return r["ran"], r["suites"], r["failures"], r["matched"]


def run_init(root, adapter, name, force=False):
    root = Path(root)
    if not NAME_RE.match(name or ""):
        print("ERROR: --name 은 소문자 슬러그여야 한다 (^[a-z0-9][a-z0-9-]*$): %r" % name)
        return 2

    seed = root / PROFILE_DIR_REL / adapter / "config.json"
    if not seed.is_file():
        available = sorted(p.name for p in (root / PROFILE_DIR_REL).glob("*") if p.is_dir())
        print("ERROR: 프로파일 %r 가 없다. 있는 것: %s" % (adapter, ", ".join(available) or "없음"))
        return 2

    target = root / CONFIG_REL
    if target.exists() and not force:
        print("ERROR: %s 가 이미 있다. 덮어쓰려면 --force. "
              "init 은 기존 설정을 조용히 지우지 않는다." % CONFIG_REL)
        return 2

    text = seed.read_text(encoding="utf-8").replace("{{name}}", name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print("생성: %s (프로파일 %s · 이름 %s)" % (CONFIG_REL, adapter, name))
    print("다음: python scripts/harness.py doctor")
    print("init 은 git remote·커밋 이력·README 를 건드리지 않는다 — 그건 사람이 한다.")
    return 0


# ------------------------------------------------------------------------ CLI

def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="Harness 계약 계층 CLI")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="설정과 리포의 어긋남을 실행 전에 잡는다")

    p_init = sub.add_parser("init", help="프로파일 시드로 harness/config.json 을 만든다")
    p_init.add_argument("--adapter", required=True)
    p_init.add_argument("--name", required=True)
    p_init.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        return run_init(ROOT, adapter=args.adapter, name=args.name, force=args.force)
    if args.cmd == "doctor":
        report = run_doctor(ROOT)
        print("\n  harness doctor — %s" % ROOT)
        print(report.text())
        return report.exit_code

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
