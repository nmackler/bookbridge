"""Regression for issue #455: ABS audio must be local before CTC runs."""

import logging
from pathlib import Path
from unittest.mock import MagicMock

from src.api.api_clients import ABSClient
from src.services.audio_source_adapters import ABSAudioSourceAdapter
from src.sync_manager import SyncManager


def test_abs_ctc_preflight_downloads_and_reuses_all_tracks(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_ENABLED", "true")
    client = MagicMock()
    client.get_audio_files.return_value = [
        {"stream_url": "http://abs/track-1?token=secret", "ext": "mp3"},
        {"stream_url": "http://abs/track-2?token=secret", "ext": "m4b"},
    ]

    def download(_url, destination):
        Path(destination).write_bytes(b"audio" * 300)
        return True

    client.download_file.side_effect = download
    adapter = ABSAudioSourceAdapter(client, tmp_path)

    paths = SyncManager._ctc_local_audio_paths(None, adapter, "item-1", "abs-1")
    assert paths == [
        str(tmp_path / "audio_cache/abs-1/source_tracks/track_000.mp3"),
        str(tmp_path / "audio_cache/abs-1/source_tracks/track_001.m4b"),
    ]
    assert all(Path(path).read_bytes() == b"audio" * 300 for path in paths)
    assert client.download_file.call_count == 2

    assert SyncManager._ctc_local_audio_paths(None, adapter, "item-1", "abs-1") == paths
    assert client.download_file.call_count == 2


def test_abs_ctc_preflight_falls_back_to_stream_on_failed_download(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("CTC_ENABLED", "true")
    client = MagicMock()
    client.get_audio_files.return_value = [
        {"stream_url": "http://abs/track-1", "ext": "mp3"},
    ]
    client.download_file.return_value = False
    adapter = ABSAudioSourceAdapter(client, tmp_path)

    assert SyncManager._ctc_local_audio_paths(None, adapter, "item-1", "abs-1") is None
    assert "ABS track download failed for item_id=item-1 track_index=0" in caplog.text
    assert client.get_audio_files.return_value[0]["stream_url"] == "http://abs/track-1"
    assert not (tmp_path / "audio_cache/abs-1/source_tracks/track_000.mp3").exists()


def test_abs_audio_stays_remote_when_ctc_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_ENABLED", "false")
    client = MagicMock()
    client.get_audio_files.return_value = [{"stream_url": "http://abs/track", "ext": "mp3"}]
    files = ABSAudioSourceAdapter(client, tmp_path).get_audio_files("item-1", bridge_key="abs-1")
    assert files == [{"stream_url": "http://abs/track", "ext": "mp3"}]
    client.download_file.assert_not_called()


def test_abs_track_download_log_hides_stream_token(tmp_path, caplog):
    client = ABSClient(credentials={"ABS_KEY": "secret"})
    response = MagicMock()
    response.headers = {"Content-Length": "1500"}
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"audio" * 300]
    client.session = MagicMock()
    client.session.get.return_value = response

    with caplog.at_level(logging.INFO, logger="src.api.api_clients"):
        assert client.download_file("http://abs/track?token=secret", tmp_path / "track.mp3")
    assert "http://abs/track" in caplog.text
    assert "secret" not in caplog.text

    caplog.clear()
    client.session.get.side_effect = RuntimeError("GET http://abs/track?token=secret failed")
    with caplog.at_level(logging.ERROR, logger="src.api.api_clients"):
        assert not client.download_file("http://abs/track?token=secret", tmp_path / "failed.mp3")
    assert "secret" not in caplog.text


def _mapped_client(abs_paths):
    client = MagicMock()
    client.get_audio_files.return_value = [
        {"stream_url": f"http://abs/track-{i}", "ext": "mp3", "abs_path": p}
        for i, p in enumerate(abs_paths)
    ]
    return client


def test_abs_local_path_map_reads_mounted_library_without_downloading(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_ENABLED", "true")
    library = tmp_path / "mnt" / "Author" / "Book"
    library.mkdir(parents=True)
    for name in ("01.mp3", "02.mp3"):
        (library / name).write_bytes(b"audio")
    monkeypatch.setenv(
        "ABS_LOCAL_PATH_MAP", f"/other=/nowhere; /audiobooks={tmp_path / 'mnt'}"
    )
    client = _mapped_client(["/audiobooks/Author/Book/01.mp3", "/audiobooks/Author/Book/02.mp3"])
    adapter = ABSAudioSourceAdapter(client, tmp_path / "data")

    paths = SyncManager._ctc_local_audio_paths(None, adapter, "item-1", "abs-1")

    assert paths == [str(library / "01.mp3"), str(library / "02.mp3")]
    client.download_file.assert_not_called()
    assert not (tmp_path / "data" / "audio_cache").exists()


def test_abs_local_path_map_downloads_only_tracks_it_cannot_find(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CTC_ENABLED", "true")
    library = tmp_path / "mnt" / "Book"
    library.mkdir(parents=True)
    (library / "01.mp3").write_bytes(b"audio")
    monkeypatch.setenv("ABS_LOCAL_PATH_MAP", f"/audiobooks={tmp_path / 'mnt'}")
    client = _mapped_client(["/audiobooks/Book/01.mp3", "/audiobooks/Book/02.mp3"])

    def download(_url, destination):
        Path(destination).write_bytes(b"audio" * 300)
        return True

    client.download_file.side_effect = download
    adapter = ABSAudioSourceAdapter(client, tmp_path / "data")

    paths = SyncManager._ctc_local_audio_paths(None, adapter, "item-1", "abs-1")

    assert paths == [
        str(library / "01.mp3"),
        str(tmp_path / "data/audio_cache/abs-1/source_tracks/track_001.mp3"),
    ]
    assert client.download_file.call_count == 1
    assert "resolved 1 of 2 track(s) for item_id=item-1" in caplog.text


def test_abs_local_path_map_matches_whole_path_components(tmp_path, monkeypatch):
    (tmp_path / "mnt" / "Book").mkdir(parents=True)
    (tmp_path / "mnt" / "Book" / "01.mp3").write_bytes(b"audio")
    monkeypatch.setenv("ABS_LOCAL_PATH_MAP", f"/audiobooks={tmp_path / 'mnt'}")
    monkeypatch.setenv("CTC_ENABLED", "false")
    # "/audiobooks2" must not match the "/audiobooks" prefix, and ".." must not escape it.
    client = _mapped_client(["/audiobooks2/Book/01.mp3", "/audiobooks/../mnt/Book/01.mp3"])

    files = ABSAudioSourceAdapter(client, tmp_path / "data").get_audio_files("item-1", bridge_key="abs-1")

    assert [f.get("local_path") for f in files] == [None, None]
    client.download_file.assert_not_called()


def test_abs_client_returns_abs_side_path():
    client = ABSClient(credentials={"ABS_SERVER": "http://abs.example", "ABS_KEY": "secret"})
    response = MagicMock(status_code=200)
    response.json.return_value = {"media": {"audioFiles": [
        {"ino": "2", "ext": ".mp3", "track": 2, "metadata": {"path": "/audiobooks/B/02.mp3"}},
        {"ino": "1", "ext": ".mp3", "track": 1, "metadata": {"path": "/audiobooks/B/01.mp3"}},
    ]}}
    client.session = MagicMock()
    client.session.get.return_value = response

    files = client.get_audio_files("item-1")

    assert [f["abs_path"] for f in files] == ["/audiobooks/B/01.mp3", "/audiobooks/B/02.mp3"]
