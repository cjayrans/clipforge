"""Stall watchdog and stage ceilings (moments-v2): active time ignores host sleep; a step with no progress is retried
then fails; a stage at its ceiling is stopped; a failed stage marks the source FAILED and the night goes on."""

from __future__ import annotations

import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from clipforge.ingest import media, watchdog


class FakeClock:
    """Active time that follows the real monotonic clock, minus any simulated sleep."""

    def __init__(self) -> None:
        self.offset = 0.0

    def now(self) -> float:
        return time.monotonic() + self.offset


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(watchdog, "CLOCK", c)
    return c


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_active_clock_ignores_a_host_sleep() -> None:
    c = watchdog.ActiveClock(tick_s=1.0, max_gap_s=5.0)
    c._thread = object()  # type: ignore[assignment]  # no ticker thread: drive it by hand
    for gap in (1.0, 1.2, 0.9):
        c.account(gap)
    c.account(40_887.0)  # the 11 h idle sleep of 27 Nov counts as one tick
    assert c.now() == pytest.approx(4.1)


def test_a_command_making_progress_finishes(clock: FakeClock) -> None:
    code, out, _ = watchdog.run_watched(
        py("import time,sys\nfor i in range(4):\n print(i, flush=True); time.sleep(0.3)"), stall_s=1.0, poll_s=0.1
    )
    assert code == 0
    assert out.split() == ["0", "1", "2", "3"]


def test_a_silent_command_is_retried_then_fails(clock: FakeClock) -> None:
    started = time.monotonic()
    with pytest.raises(watchdog.StepStalled, match="no progress"):
        watchdog.run_watched(py("import time; time.sleep(30)"), stall_s=0.5, retries=2, poll_s=0.1)
    assert time.monotonic() - started < 10  # three short attempts, not 30 s each


def test_the_stage_ceiling_kills_a_running_step(clock: FakeClock) -> None:
    with watchdog.stage("download", ceiling_s=0.6), pytest.raises(watchdog.StageTimeout, match="download"):
        watchdog.run_watched(py("import time\nwhile True: print('x', flush=True); time.sleep(0.05)"), poll_s=0.1)


def test_check_outside_and_inside_stages(clock: FakeClock) -> None:
    watchdog.check()  # no stage: nothing to enforce
    with (
        watchdog.stage("source", ceiling_s=100),
        watchdog.stage("rank", ceiling_s=0.0),
        pytest.raises(watchdog.StageTimeout, match="rank"),
    ):
        watchdog.check()  # the tightest nested stage counts
    left, st = watchdog.remaining()
    assert st is None


def test_ceilings_are_about_3x_the_first_full_vod_run() -> None:
    c = watchdog.CEILINGS
    assert c["download"] == 40 * 60  # 13.4 min measured
    assert c["source"] == 90 * 60  # 27 min measured
    assert watchdog.STALL_S == 600


def test_media_run_watches_ffmpeg_and_adds_progress(tmp_path: Path, clock: FakeClock, monkeypatch) -> None:
    fake = tmp_path / "bin" / "ffmpeg"
    fake.parent.mkdir()
    seen = tmp_path / "args.txt"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(seen)!r}, 'w').write(' '.join(sys.argv[1:]))\nprint('hello')\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert media.run([str(fake), "-i", "in.mp4", "out.mp3"]).strip() == "hello"
    assert seen.read_text().startswith("-progress ")
    fail = tmp_path / "bin2" / "ffmpeg"
    fail.parent.mkdir()
    fail.write_text(f"#!{sys.executable}\nimport sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
    fail.chmod(fail.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(media.MediaToolError, match="exit 3"):
        media.run([str(fail), "-i", "x"])


def test_download_guard_stops_at_the_ceiling_and_on_a_stall(clock: FakeClock) -> None:
    from clipforge.ingest.twitch import DownloadGuard

    guard = DownloadGuard()
    guard.hook({"status": "downloading", "downloaded_bytes": 10})
    clock.offset += watchdog.STALL_S + 1  # no new bytes for 10 min of active time
    with pytest.raises(watchdog.StepStalled):
        guard.hook({"status": "downloading", "downloaded_bytes": 10})
    with watchdog.stage("download", ceiling_s=0.0):
        guard = DownloadGuard()
        with pytest.raises(watchdog.StageTimeout):
            guard.hook({"status": "downloading", "downloaded_bytes": 1})
        assert isinstance(guard.error, watchdog.StageTimeout)


def test_stalled_twitch_download_is_retried(tmp_path: Path, clock: FakeClock) -> None:
    from clipforge.ingest import twitch

    calls: list[int] = []

    class StallingYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            self.opts = opts

        def __enter__(self) -> StallingYDL:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            calls.append(1)
            hook = self.opts["progress_hooks"][0]
            if len(calls) < 3:  # two stalled attempts
                hook({"status": "downloading", "downloaded_bytes": 5})
                clock.offset += watchdog.STALL_S + 1
                hook({"status": "downloading", "downloaded_bytes": 5})
            path = Path(self.opts["outtmpl"]["default"].replace("%(ext)s", "mp4"))
            path.write_bytes(b"video")
            return {"extractor_key": "TwitchVod", "requested_downloads": [{"filepath": str(path)}]}

    path = twitch.download("https://www.twitch.tv/videos/1", tmp_path, "1", ytdl_factory=StallingYDL)
    assert path.read_bytes() == b"video"
    assert len(calls) == 3


def test_a_retried_download_rechecks_the_channel_before_fetching(tmp_path: Path, clock: FakeClock) -> None:
    """COMPLIANCE #1: every attempt checks the channel before any media is fetched, retries included."""
    from clipforge.compliance import ComplianceError
    from clipforge.ingest import twitch

    calls: list[bool] = []

    class FlakyYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            self.opts = opts

        def __enter__(self) -> FlakyYDL:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            calls.append(download)
            attempt = calls.count(False)
            if not download:  # the metadata now names another channel on the retry
                return {"extractor_key": "TwitchVod", "uploader_id": "fakestreamer" if attempt == 1 else "someone"}
            hook = self.opts["progress_hooks"][0]
            hook({"status": "downloading", "downloaded_bytes": 5})
            clock.offset += watchdog.STALL_S + 1
            hook({"status": "downloading", "downloaded_bytes": 5})
            raise AssertionError("unreachable")

    with pytest.raises(ComplianceError, match="someone"):
        twitch.download("https://www.twitch.tv/videos/1", tmp_path, "1", ytdl_factory=FlakyYDL,
                        channels=["fakestreamer"])  # fmt: skip
    assert calls == [False, True, False]  # attempt 2 was refused at the metadata check, before any media


def test_a_step_outside_any_stage_has_a_ceiling(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watchdog, "DEFAULT_STEP_CEILING_S", 0.5)
    with pytest.raises(watchdog.StageTimeout, match="outside a stage"):
        watchdog.run_watched(py("import time\nwhile True: print('x', flush=True); time.sleep(0.05)"), poll_s=0.1)
