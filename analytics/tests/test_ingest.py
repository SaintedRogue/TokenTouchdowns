import pandas as pd
import pytest
from tt.ingest import nflverse_url, season_asset, fetch_season, load_seasons


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
        return b"PAR1" + b"x" * 100 + b"PAR1"

    path = fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    assert path.exists()
    assert path.read_bytes() == b"PAR1" + b"x" * 100 + b"PAR1"
    assert len(calls) == 1


def test_fetch_season_is_idempotent_and_does_not_refetch(tmp_path):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"PAR1" + b"x" * 100 + b"PAR1"

    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    fetch_season("stats_player", 2025, tmp_path, fetch=fake_fetch)
    assert len(calls) == 1, "a cached season must not be refetched"


def test_fetch_season_refetches_when_forced(tmp_path):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"PAR1" + b"x" * 100 + b"PAR1"

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


def test_fetch_season_rejects_empty_payload(tmp_path):
    """An empty payload from a CDN hiccup must not be cached."""

    def empty_fetch(url):
        return b""

    with pytest.raises(ValueError, match="empty payload"):
        fetch_season("stats_player", 2025, tmp_path, fetch=empty_fetch)
    # No file should be left behind.
    assert list(tmp_path.glob("*")) == []


def test_fetch_season_rejects_non_parquet_payload(tmp_path):
    """An HTML error page returned as 200 must not be cached."""

    def html_fetch(url):
        return b"<html>error</html>"

    with pytest.raises(ValueError, match="not parquet"):
        fetch_season("stats_player", 2025, tmp_path, fetch=html_fetch)
    # No file should be left behind.
    assert list(tmp_path.glob("*")) == []


def test_fetch_season_accepts_valid_parquet_payload(tmp_path):
    """Valid parquet with PAR1 magic bytes is cached normally."""

    def parquet_fetch(url):
        # Minimal valid parquet: PAR1 header + some content + PAR1 footer
        return b"PAR1" + b"x" * 100 + b"PAR1"

    path = fetch_season("stats_player", 2025, tmp_path, fetch=parquet_fetch)
    assert path.exists()
    assert path.read_bytes().startswith(b"PAR1")
    assert path.read_bytes().endswith(b"PAR1")


def test_load_seasons_concatenates_two_seasons(tmp_path):
    """load_seasons concatenates multiple cached seasons."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Create two small parquet files with real pandas.
    df1 = pd.DataFrame({"player_id": [1, 2], "season": [2024, 2024], "week": [1, 1]})
    df2 = pd.DataFrame({"player_id": [3, 4], "season": [2025, 2025], "week": [1, 1]})

    def fake_fetch(url):
        # Return different dataframes based on which season is in the URL.
        if "2024" in url:
            return df1.to_parquet()
        elif "2025" in url:
            return df2.to_parquet()
        else:
            raise ValueError(f"unexpected URL: {url}")

    result = load_seasons("stats_player", [2024, 2025], data_dir, fetch=fake_fetch)
    # Should have 4 rows total.
    assert len(result) == 4
    assert list(result["player_id"]) == [1, 2, 3, 4]


def test_load_seasons_produces_clean_index(tmp_path):
    """load_seasons with ignore_index=True produces sequential 0..n-1 index."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    df1 = pd.DataFrame({"player_id": [1, 2], "season": [2024, 2024]}, index=[10, 11])
    df2 = pd.DataFrame({"player_id": [3, 4], "season": [2025, 2025]}, index=[20, 21])

    def fake_fetch(url):
        if "2024" in url:
            return df1.to_parquet()
        elif "2025" in url:
            return df2.to_parquet()
        else:
            raise ValueError(f"unexpected URL: {url}")

    result = load_seasons("stats_player", [2024, 2025], data_dir, fetch=fake_fetch)
    # Index should be 0, 1, 2, 3, not [10, 11, 20, 21].
    assert list(result.index) == [0, 1, 2, 3]


def test_load_seasons_rejects_empty_seasons_with_clear_message(tmp_path):
    """An empty seasons iterable raises with a clear error message."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    def fake_fetch(url):
        return b"PAR1" + b"x" + b"PAR1"

    with pytest.raises(ValueError, match="stats_player.*no seasons"):
        load_seasons("stats_player", [], data_dir, fetch=fake_fetch)


def test_load_seasons_respects_injected_fetch(tmp_path):
    """load_seasons uses the injected fetch function, not the network."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    calls = []

    def fake_fetch(url):
        calls.append(url)
        df = pd.DataFrame({"player_id": [1], "season": [2025], "week": [1]})
        return df.to_parquet()

    load_seasons("stats_player", [2025], data_dir, fetch=fake_fetch)
    # Verify fetch was called (no network request was made).
    assert len(calls) > 0
