# -*- coding: utf-8 -*-
"""PreToolUse(Bash) 훅 — 위험한 셸 명령을 막는다.

Claude Code 훅의 계약: 입력은 **stdin 의 JSON 하나**(`tool_input.command` 에
명령), 차단은 **exit 2 + stderr**. 환경변수 `CLAUDE_TOOL_INPUT` 은 없고 exit 1 은
막지 않는다 — 옛 인라인 셸 훅이 그 둘을 전제해 아무것도 막지 못했다 (ADR-H076).

셸과 무관하게 돌도록 파이썬 파일이다. **훅의 작업 디렉터리는 프로젝트 루트가
아니다** — Claude 가 `cd` 하면 따라간다. 그래서 settings.json 은 이 파일을
`$CLAUDE_PROJECT_DIR` 기준으로 부른다. 상대 경로였을 때는 하위 폴더에서 python 이
「파일 없음」 exit 2 를 내 모든 Bash 가 막혔다 (ADR-H076 fix).
"""
import json
import re
import sys

PATTERNS = re.compile(
    r"rm\s+-rf|git\s+push\s+--force|git\s+reset\s+--hard|DROP\s+TABLE")


def main():
    if hasattr(sys.stderr, "reconfigure"):
        # Claude Code 는 stderr 를 UTF-8 로 읽는다 — cp949 콘솔 기본값이면 깨진다.
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        data = json.load(sys.stdin)
    except ValueError:
        # 차단 훅이 무해한 명령을 막으면 그것이 새 결함이다 — 못 읽으면 통과.
        return 0
    command = ((data or {}).get("tool_input") or {}).get("command") or ""
    hit = PATTERNS.search(command)
    if hit is None:
        return 0
    sys.stderr.write("BLOCKED: 위험한 명령어가 감지되었습니다 — `%s`. "
                     "이 훅은 rm -rf · git push --force · git reset --hard · "
                     "DROP TABLE 을 막는다. 필요하면 사람이 직접 실행한다.\n"
                     % hit.group(0))
    return 2


if __name__ == "__main__":
    sys.exit(main())
