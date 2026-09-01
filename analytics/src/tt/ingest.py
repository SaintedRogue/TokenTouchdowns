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


# Datasets whose rows are per-week and therefore span both the regular
# season and the postseason, tagged by a `season_type` column in
# {"REG", "POST"}. Fantasy football is scored over the regular season only
# (see `projections.REGULAR_SEASON_TYPE`), so a consumer MUST be able to tell
# the two apart. If nflverse ever ships one of these assets without that
# column, every downstream aggregate would silently start averaging over the
# wrong population -- so this module fails loudly instead, in keeping with
# its own "pinned rather than discovered, so a layout change fails loudly"
# contract.
_SEASON_TYPED_DATASETS = frozenset({"stats_player"})


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
    leave a truncated file that later runs would trust. Validates the payload
    before caching to reject CDN hiccups or error pages.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    filename = season_asset(dataset, season)
    target = data_dir / filename
    if target.exists() and not force:
        return target

    payload = fetch(nflverse_url(dataset, filename))
    if not payload:
        raise ValueError(f"{dataset} {season}: empty payload, refusing to cache")
    if target.suffix == ".parquet" and not (
        payload.startswith(b"PAR1") and payload.endswith(b"PAR1")
    ):
        raise ValueError(f"{dataset} {season}: payload is not parquet, refusing to cache")

    tmp = target.with_suffix(target.suffix + ".part")
    try:
        tmp.write_bytes(payload)
        tmp.replace(target)
    except Exception:
        # Clean up the .part file on any write/rename failure.
        tmp.unlink(missing_ok=True)
        raise
    return target


def load_seasons(
    dataset: str,
    seasons: Iterable[int],
    data_dir: Path,
    fetch: Callable[[str], bytes] = _http_get,
) -> pd.DataFrame:
    """Concatenate cached seasons into one frame. Fetches anything missing."""
    season_list = list(seasons)
    if not season_list:
        raise ValueError(f"{dataset}: no seasons to load")
    frames = [
        pd.read_parquet(fetch_season(dataset, season, data_dir, fetch=fetch))
        for season in season_list
    ]
    out = pd.concat(frames, ignore_index=True)
    if dataset in _SEASON_TYPED_DATASETS and "season_type" not in out.columns:
        raise ValueError(
            f"{dataset}: no 'season_type' column -- this asset is expected to "
            "carry REG/POST rows and every consumer filters on it (see "
            "projections.regular_season). Refusing to hand back a frame whose "
            "regular-season rows cannot be told apart from postseason ones."
        )
    return out
