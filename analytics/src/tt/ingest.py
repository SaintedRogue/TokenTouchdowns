"""nflverse release ingestion.

Assets are pinned by name rather than discovered, so a layout change fails
loudly instead of silently fetching the wrong file.
"""
from __future__ import annotations

import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

import pandas as pd

BASE = "https://github.com/nflverse/nflverse-data/releases/download"

# dataset -> filename template. Extend deliberately, not dynamically.
ASSETS: dict[str, str] = {
    "stats_player": "stats_player_week_{season}.parquet",
    "snap_counts": "snap_counts_{season}.parquet",
    "weekly_rosters": "roster_weekly_{season}.parquet",
}


def nflverse_url(dataset: str, filename: str) -> str:
    return f"{BASE}/{dataset}/{filename}"


def season_asset(dataset: str, season: int) -> str:
    try:
        return ASSETS[dataset].format(season=season)
    except KeyError:
        raise ValueError(
            f"unknown dataset {dataset!r}; known: {sorted(ASSETS)}"
        ) from None


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        return response.read()


def fetch_season(
    dataset: str,
    season: int,
    data_dir: Path,
    fetch: Callable[[str], bytes] = _http_get,
    force: bool = False,
) -> Path:
    """Download one season to `data_dir`, returning its path.

    Writes to a temporary file and renames, so an interrupted download can never
    leave a truncated file that later runs would trust.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    filename = season_asset(dataset, season)
    target = data_dir / filename
    if target.exists() and not force:
        return target

    payload = fetch(nflverse_url(dataset, filename))
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(payload)
    tmp.replace(target)
    return target


def load_seasons(dataset: str, seasons: Iterable[int], data_dir: Path) -> pd.DataFrame:
    """Concatenate cached seasons into one frame. Fetches anything missing."""
    frames = [
        pd.read_parquet(fetch_season(dataset, season, data_dir))
        for season in seasons
    ]
    return pd.concat(frames, ignore_index=True)
