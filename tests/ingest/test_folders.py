"""A2b: Drive folder links register each media file as its own source; Dropbox folders are refused."""

from __future__ import annotations

from collections import namedtuple
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import EventRow, SourceRow
from clipforge.ingest import folders, registry
from tests.support.factories import Factory

FOLDER = "https://drive.google.com/drive/folders/FOLDERID?usp=sharing"
Entry = namedtuple("Entry", ["id", "path", "local_path"])  # gdown.GoogleDriveFileToDownload's fields


def _fake_gdown(
    monkeypatch: pytest.MonkeyPatch, result: Any = None, exc: Exception | None = None
) -> list[dict[str, Any]]:
    import gdown

    calls: list[dict[str, Any]] = []

    def download_folder(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(gdown, "download_folder", download_folder)
    return calls


def test_list_gdrive_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_gdown(
        monkeypatch,
        [
            Entry("id2", "day2/ep2.MP4", "/x/day2/ep2.MP4"),
            Entry("id1", "ep1.mp4", "/x/ep1.mp4"),
            Entry("id3", "brief.pdf", "/x/brief.pdf"),
            Entry("id4", "thumbs/cover.png", "/x/thumbs/cover.png"),
        ],
    )
    assert folders.list_gdrive_folder(FOLDER) == [
        ("https://drive.google.com/file/d/id2/view", "day2/ep2.MP4"),
        ("https://drive.google.com/file/d/id1/view", "ep1.mp4"),
    ]
    assert calls[0]["skip_download"] is True
    assert calls[0]["url"] == FOLDER


@pytest.mark.parametrize("result", [None, []])
def test_empty_or_private_folder(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    _fake_gdown(monkeypatch, result)
    with pytest.raises(folders.FolderListError, match="shared"):
        folders.list_gdrive_folder(FOLDER)


def test_gdown_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gdown(monkeypatch, exc=RuntimeError("Failed to retrieve folder contents"))
    with pytest.raises(folders.FolderListError, match="retrieve"):
        folders.list_gdrive_folder(FOLDER)


def test_add_source_drive_folder_registers_each_file(
    session: Session, settings: Settings, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gdown(monkeypatch, [Entry("id1", "ep1.mp4", ""), Entry("id2", "sub/ep2.mov", ""), Entry("x", "a.txt", "")])
    c = factory.campaign()
    rows = registry.add_source(session, settings, c.id, FOLDER)
    assert [r.url for r in rows] == [
        "https://drive.google.com/file/d/id1/view",
        "https://drive.google.com/file/d/id2/view",
    ]
    assert {r.folder_url for r in rows} == {FOLDER}
    # Re-adding the folder (e.g. after the campaign uploads more files) only adds the new ones.
    _fake_gdown(
        monkeypatch, [Entry("id1", "ep1.mp4", ""), Entry("id2", "sub/ep2.mov", ""), Entry("id5", "ep5.mp4", "")]
    )
    again = registry.add_source(session, settings, c.id, FOLDER)
    assert {r.id for r in rows} < {r.id for r in again}
    assert len(again) == 3
    assert len(session.scalars(select(SourceRow)).all()) == 3
    # The folder itself is never a source row.
    assert FOLDER not in session.scalars(select(SourceRow.url)).all()


def test_add_source_folder_list_failure_alerts(
    session: Session, settings: Settings, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gdown(monkeypatch, None)
    c = factory.campaign()
    assert registry.add_source(session, settings, c.id, FOLDER) == []
    (alert,) = session.scalars(select(EventRow).where(EventRow.entity == "alert"))
    assert alert.to_state == "download_failed"
    assert "could not list folder" in alert.detail


def test_folder_lister_injection(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign()
    rows = registry.add_source(
        session, settings, c.id, FOLDER, folder_lister=lambda url: [(folders.gdrive_file_url("abc"), "x.mp4")]
    )
    assert [r.url for r in rows] == ["https://drive.google.com/file/d/abc/view"]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.dropbox.com/scl/fo/abc/def?rlkey=x&dl=0",
        "https://www.dropbox.com/sh/abc/def?dl=0",
    ],
)
def test_dropbox_folder_refused_with_guidance(session: Session, settings: Settings, factory: Factory, url: str) -> None:
    c = factory.campaign(name="Dylan Page")
    assert registry.add_source(session, settings, c.id, url) == []
    (alert,) = session.scalars(select(EventRow).where(EventRow.entity == "alert"))
    assert "Dropbox folder" in alert.detail
    assert f"inbox/{c.slug}/" in alert.detail


@pytest.mark.parametrize(
    ("url", "is_folder"),
    [
        ("https://drive.google.com/drive/folders/abc", True),
        ("https://drive.google.com/drive/u/0/folders/abc", True),
        ("https://drive.google.com/embeddedfolderview?id=abc", False),
        ("https://drive.google.com/file/d/abc/view", False),
    ],
)
def test_is_gdrive_folder(url: str, is_folder: bool) -> None:
    assert registry.is_gdrive_folder(url) is is_folder
