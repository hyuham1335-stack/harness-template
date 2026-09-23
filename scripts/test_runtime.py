"""`scripts/runtime.py` — 코어와 실행기가 함께 쓰는 원시요소의 테스트.

**여기 있는 것은 전부 `test_execute.py` 에서 옮겨 온 것이다.** 옮긴 이유는
질문이 바뀌어서가 아니라 **답하는 코드의 주소가 바뀌어서**다 — 트랜스크립트
읽기와 출력 인코딩은 순차 실행기의 것이 아니라 코어의 것이고, 코어가
실행기를 import 하는 한 7페이즈 코어를 실행기 없이 떼어 낼 수 없다.

그래서 **본문은 한 줄도 안 고쳤다.** 바뀐 것은 부르는 이름뿐이다.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import runtime as rt


# ---------------------------------------------------------------------------
# 실행기 자신의 출력 인코딩 (파일럿 런 #3 M8)
# ---------------------------------------------------------------------------

class TestForceUtf8Output:
    """리다이렉트된 stdout 에 ✓ 를 찍다 죽으면 안 된다.

    로캘(cp949) 스트림에 진행 표시를 쓰는 순간 UnicodeEncodeError 가 나고
    실행기가 통째로 멈춘다 — 런 #3에서 step 3 완료 출력이 phase 를 끊었다.
    """

    def test_reconfigures_streams_to_utf8(self):
        calls = []

        class FakeStream:
            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        with patch.object(rt.sys, "stdout", FakeStream()), \
             patch.object(rt.sys, "stderr", FakeStream()):
            rt.force_utf8_output()

        assert len(calls) == 2
        for kwargs in calls:
            assert kwargs["encoding"] == "utf-8"
            assert kwargs["errors"] == "replace"

    def test_stream_without_reconfigure_is_tolerated(self):
        import io as _io
        with patch.object(rt.sys, "stdout", _io.StringIO()), \
             patch.object(rt.sys, "stderr", _io.StringIO()):
            rt.force_utf8_output()

    def test_reconfigure_failure_is_tolerated(self):
        class Hostile:
            def reconfigure(self, **kwargs):
                raise ValueError("detached")

        with patch.object(rt.sys, "stdout", Hostile()), \
             patch.object(rt.sys, "stderr", Hostile()):
            rt.force_utf8_output()


# ---------------------------------------------------------------------------
# 타임존 — 파일럿의 KST 를 템플릿이 물려받지 않는다 (ADR-H038)
# ---------------------------------------------------------------------------

class TestTimezone:
    """`TZ` 는 선언이지 상수가 아니다.

    ADR-H037 이 *"옮기기만 하고 매개변수화하지 않는다 — 세션 E 로 넘긴다"* 고
    적어 둔 자리다. 하드코딩된 +09:00 은 이 리포를 만든 사람의 시간대이지
    클론하는 사람의 것이 아니고, 시각이 기록의 축이라 조용히 틀리면 원장·영수증·
    보고서가 전부 남의 시간으로 남는다.
    """

    def test_환경변수가_없으면_시스템_로컬이다(self, monkeypatch):
        monkeypatch.delenv("HARNESS_TZ", raising=False)
        from datetime import datetime
        assert rt.resolve_tz() == datetime.now().astimezone().tzinfo

    def test_환경변수가_오프셋을_정한다(self, monkeypatch):
        from datetime import datetime, timedelta
        monkeypatch.setenv("HARNESS_TZ", "-05:00")
        assert rt.resolve_tz().utcoffset(datetime.now()) == timedelta(hours=-5)

    def test_읽을_수_없는_값은_조용히_넘기지_않는다(self, monkeypatch):
        """형식이 틀리면 로컬로 폴백하되 **왜 그랬는지**를 남긴다.

        여기서 죽이지 않는 이유는 시각이 게이트가 아니기 때문이고, 조용히
        넘기지 않는 이유는 ADR-H007 과 같다 — 모르는 것은 모른다고 적는다.
        """
        monkeypatch.setenv("HARNESS_TZ", "다섯시")
        with pytest.warns(RuntimeWarning, match="HARNESS_TZ"):
            resolved = rt.resolve_tz()
        from datetime import datetime
        assert resolved == datetime.now().astimezone().tzinfo
