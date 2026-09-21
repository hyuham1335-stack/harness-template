"""코어와 순차 실행기가 함께 쓰는 원시요소.

**이 파일이 생긴 이유는 소유권이지 재사용이 아니다.** 트랜스크립트를 읽는 일과
출력 인코딩을 고정하는 일은 `scripts/execute.py`(순차 step 실행기) 안에 살고
있었는데, 8페이즈 코어(`scripts/pipeline/*`)가 그것을 쓰려고
실행기를 import 했다. 그래서 **코어가 실행기에 의존**했다.

`harness-template` 추출은 실행기를 안 싣는다 — 헤드리스 승인 우회를 클론하는
사람이 물려받게 하지 않기 위해서다 (ROADMAP 36). 그 상태로 추출하면 코어가
없는 모듈을 물고 죽는다. 지식을 **그것을 소유해야 할 계층**으로 내린 것이 이
파일이다 (ADR-H037). [[ADR-H031]] 이 스택 실행기 이름 목록을 코어에서 어댑터
선언으로 내린 것과 방향만 반대이고 규율은 같다 — 그 상수 이름을 여기 적지 않는
것은 그것을 감시하는 자물쇠(`CoreHasNoStackNamesTest`)가 인용까지 잡기 때문이고,
잡는 것이 맞다.

**실행기는 여전히 이것을 쓴다** — 여기서 내보내고 `execute.py` 가 읽는다.
같은 지식이 두 곳에 살면 한쪽만 고쳐지는 날이 온다.

모듈명 `runtime` 은 stdlib 과 겹치지 않는다(확인함). `scripts/pipeline/` 이
stdlib `trace` 를 가리지 않으려고 `trace_contract.py` 를 쓴 것과 같은 확인이다.
"""

import os
import re
import sys
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

def resolve_tz():
    """기록에 찍을 타임존을 정한다 — `HARNESS_TZ`, 없으면 시스템 로컬.

    **템플릿에 만든 사람의 시간대를 하드코딩하지 않는다** (ADR-H038). 시각은
    원장·영수증·보고서의 축이라, 조용히 틀리면 클론한 사람의 기록이 전부 남의
    시간으로 남는다. ADR-H037 이 *"옮기기만 하고 매개변수화는 세션 E 로 넘긴다"*
    고 적어 둔 자리가 여기다.

    형식은 `+09:00`·`-05:00` 같은 UTC 오프셋이다. 읽을 수 없으면 로컬로 가되
    **경고를 남긴다** — 모르는 것을 모른다고 적는 것이 이 리포의 규율이다
    (ADR-H007).
    """
    raw = os.environ.get("HARNESS_TZ", "").strip()
    if not raw:
        return datetime.now().astimezone().tzinfo
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", raw)
    if not match:
        warnings.warn("HARNESS_TZ %r 를 UTC 오프셋으로 읽을 수 없다 — "
                      "시스템 로컬 시간대로 간다 (형식: +09:00)" % raw,
                      RuntimeWarning, stacklevel=2)
        return datetime.now().astimezone().tzinfo
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    return timezone(-delta if sign == "-" else delta)


#: import 시점에 한 번 정한다. 한 프로세스 안에서 시각의 축이 흔들리면
#: 같은 런의 두 기록이 다른 시간대로 남는다.
TZ = resolve_tz()


def force_utf8_output():
    """이 프로세스의 출력을 UTF-8 로 고정한다.

    리다이렉트된 stdout 은 로캘(cp949)을 쓴다. 진행 표시의 ✓ · ▶ 나 한글
    에러 메시지가 그 순간 UnicodeEncodeError 를 내고 **실행기가 죽는다** —
    런 #3에서 step 3 완료를 출력하다 phase 가 중단됐다. 출력 하나 때문에
    완주가 깨지면 안 되므로 errors="replace" 로 넘어간다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


# import 만으로 인코딩이 고정된다 — 코어의 진입점들이 이것에 기대고 있다.
force_utf8_output()

