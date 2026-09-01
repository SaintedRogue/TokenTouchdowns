import pandas as pd
import pytest
from tt.ingest import nflverse_url, season_asset, fetch_season


def test_nflverse_url_pins_the_release_asset_path():
    url = nflverse_url("stats_player", "stats_player_week_2025.parquet")
    assert url == (
        "https://github.com/nflverse/nflverse-data/releases/download/"
        "stats_player/stats_player_week_2025.parquet"
    )


def test_season_asset_names_the_per_season_file():
    assert season_asset("stats_player", 2025) == "stats_player_week_2025.parquet"


def test_fetch_season_writes_the_payload_and_returns_its_path(tmp_path):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"PARQUET-BYTES"

    path = fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    assert path.exists()
    assert path.read_bytes() == b"PARQUET-BYTES"
    assert len(calls) == 1


def test_fetch_season_is_idempotent_and_does_not_refetch(tmp_path):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"PARQUET-BYTES"

    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    assert len(calls) == 1, "a cached season must not be refetched"


def test_fetch_season_refetches_when_forced(tmp_path):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"X"

    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch, force=True)
    assert len(calls) == 2


def test_fetch_season_does_not_leave_a_partial_file_on_failure(tmp_path):
    def boom(url):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        fetch_season("stats_player", 2025, tmp_path, fetch=boom)
    # A half-written cache would be silently trusted on the next run.
    assert list(tmp_path.glob("*")) == []
