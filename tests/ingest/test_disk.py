"""moments-v2 disk guard: a download needs the VOD's estimated size plus `min_free_gb` free."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.ingest import disk, twitch
from clipforge.ingest.download import DiskSpaceError, DownloadError, download_source
from clipforge.schemas import SourceStatus
from tests.ingest.test_twitch_inbox import probe_runner
from tests.ingest.test_twitch_limits import MetaYDL
from tests.support.factories import Factory

GB = disk.GB


def test_estimate_bytes() -> None:
    assert disk.estimate_bytes({"filesize": 5 * GB}) == 5 * GB
    assert disk.estimate_bytes({"filesize_approx": 2.5 * GB}) == int(2.5 * GB)
    assert disk.estimate_bytes({"duration": 3600, "tbr": 8000}) == 3600 * 1000 * 1000  # 8 Mbit/s for an hour
    assert disk.estimate_bytes({}) is None


def test_check_free(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(disk, "free_bytes", lambda _d: 20 * GB)
    assert disk.check_free(tmp_path, 4 * GB, 15) == 20 * GB
    with pytest.raises(DiskSpaceError, match="not enough disk space"):
        disk.check_free(tmp_path, 6 * GB, 15)
    assert issubclass(DiskSpaceError, DownloadError)  # the source stays REGISTERED and is retried next night


def test_twitch_download_refused_before_any_bytes_when_the_disk_is_short(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings, factory: Factory
) -> None:
    monkeypatch.setattr(disk, "free_bytes", lambda _d: 18 * GB)
    MetaYDL.instances.clear()
    MetaYDL.meta = {"uploader_id": "fakestreamer", "is_live": False, "duration": 8 * 3600, "tbr": 6000}  # ~21.6 GB
    c = factory.campaign(allow_stream_download=True, stream_channels=["fakestreamer"])
    src = factory.source(c, url="https://www.twitch.tv/videos/555")
    with pytest.raises(DownloadError, match="not enough disk space"):
        download_source(session, settings, src, ytdl_factory=MetaYDL, runner=probe_runner)
    assert MetaYDL.instances[0].calls == [False]  # metadata only, nothing downloaded
    assert src.status is SourceStatus.REGISTERED
    assert twitch  # module imported for the fake's options


def test_free_bytes_walks_up_to_an_existing_dir(tmp_path: Path) -> None:
    import importlib

    real = importlib.reload(disk).free_bytes  # the conftest patch replaces it; read the real one
    assert real(tmp_path / "not" / "yet" / "there") > 0
