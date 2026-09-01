# Draft Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A validated draft board, a survival-adjusted pick recommender, and a mock-draft simulator, ready before the 2026-09-09 live draft.

**Architecture:** The Node CLI exports the real league configuration (scoring, roster slots, team count) to JSON; the Python engine reads it instead of hardcoding. Projections come from nflverse history composed through Phase 1's simulator. VOR uses replacement levels derived from the exported roster. Survival probabilities come from FFC's ADP mean and standard deviation. Mock drafts falsify the strategy before it is trusted live.

**Tech Stack:** Node ESM (existing `tt` CLI) + Python 3.14 (`analytics/`, uv-managed).

**Spec:** `docs/draft-engine-design.md`

## Global Constraints

- Node tests: `npm test` (176 currently). Python: `cd analytics && .venv/bin/python -m pytest -q` (80 currently). Both must stay green.
- TDD mandatory: failing test first, watch it fail for the right reason, then implement.
- **Scoring and roster slots are DERIVED from the league export, never hardcoded.** `HALF_PPR` becomes a documented fallback only.
- **Team count is an explicit parameter.** Never assume 10. Replacement level is `slots x teams`, so this changes every number.
- Point-in-time discipline still applies to any historical feature: route through `prior_weeks`.
- Data cache stays gitignored. Never commit parquet/CSV.
- League config exports go to `analytics/data/` (gitignored) — they contain league identifiers.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/cli.js` (modify) | add `league export` command |
| `analytics/src/tt/league.py` | load exported config; derive scoring weights and starter slots |
| `analytics/src/tt/projections.py` | 2026 per-player volume and point distributions from history |
| `analytics/src/tt/vor.py` | replacement level, VOR, tiers |
| `analytics/src/tt/survival.py` | P(available at pick k) from ADP mean/stdev |
| `analytics/src/tt/draft.py` | pick recommendation |
| `analytics/src/tt/mock.py` | draft simulator and strategy comparison |

---

### Task 1: Export league configuration from Node

**Files:** Modify `src/cli.js`, `bin/tt.js`; test `test/cli.test.js`

**Interfaces:** Produces `leagueConfig(league)` returning `{leagueKey, name, numTeams, maxTeams, rosterSlots, scoring, draftStatus}`; new CLI command `league export [--out PATH]`.

- [ ] **Step 1: Write the failing test**

```js
test('leagueConfig derives starter slots and scoring from league settings', () => {
  const league = normalize(JSON.parse(readFileSync(
    new URL('./fixtures/league-settings.json', import.meta.url), 'utf8'))).league;
  const cfg = leagueConfig(league);

  assert.equal(cfg.numTeams, 4);
  assert.equal(cfg.maxTeams, 10);
  // Starters only -- BN and IR are not lineup slots.
  assert.deepEqual(cfg.rosterSlots,
    { QB: 1, RB: 2, WR: 2, TE: 1, 'W/R/T': 1, K: 1, DEF: 1 });
  // Scoring comes from stat_modifiers joined to stat_categories, not a constant.
  assert.equal(cfg.scoring.Rec, 0.5);
  assert.equal(cfg.scoring['Rush Yds'], 0.1);
  assert.equal(cfg.scoring['Pass TD'], 4);
});

test('leagueConfig omits bench and IR from roster slots', () => {
  const league = normalize(JSON.parse(readFileSync(
    new URL('./fixtures/league-settings.json', import.meta.url), 'utf8'))).league;
  const cfg = leagueConfig(league);
  assert.equal(cfg.rosterSlots.BN, undefined);
  assert.equal(cfg.rosterSlots.IR, undefined);
});

test('league export writes JSON a downstream tool can read', async () => {
  const out = capture();
  const client = { async get() {
    return normalize(JSON.parse(readFileSync(
      new URL('./fixtures/league-settings.json', import.meta.url), 'utf8'))); } };
  const code = await runCommand({ command: 'league', args: ['export'], flags: { json: true } },
    { client, out });
  assert.equal(code, 0);
  const cfg = JSON.parse(out.text());
  assert.equal(cfg.rosterSlots.RB, 2);
});
```

- [ ] **Step 2: Run and watch it fail**

Run: `node --test test/cli.test.js`
Expected: FAIL — `leagueConfig` is not exported.

- [ ] **Step 3: Implement**

In `src/cli.js`:

```js
/** Roster entries that are not lineup slots. */
const NON_STARTING_SLOTS = new Set(['BN', 'IR']);

/**
 * Derive the parameters a draft engine needs from a league's own settings.
 *
 * Reading these rather than hardcoding them is what keeps the engine usable in
 * any league -- and replacement level, which every VOR number depends on, is a
 * direct function of `rosterSlots` and `numTeams`.
 */
export function leagueConfig(league) {
  const s = league.settings;
  const slots = {};
  for (const p of s.roster_positions ?? []) {
    if (p.is_starting_position === 1 && !NON_STARTING_SLOTS.has(p.position)) {
      slots[p.position] = Number(p.count);
    }
  }
  // stat_modifiers carries values keyed by stat_id; stat_categories carries the
  // human names. Neither is useful without the other.
  const names = Object.fromEntries(
    (s.stat_categories?.stats ?? []).map((c) => [String(c.stat_id), c.display_name]));
  const scoring = {};
  for (const m of s.stat_modifiers?.stats ?? []) {
    const name = names[String(m.stat_id)];
    if (name) scoring[name] = Number(m.value);
  }
  return {
    leagueKey: league.league_key,
    name: league.name,
    numTeams: Number(league.num_teams),
    maxTeams: Number(league.max_teams),
    draftStatus: league.draft_status,
    rosterSlots: slots,
    scoring,
  };
}
```

Add a `league` case to `runCommand` handling the `export` subcommand: fetch `league/<key>/settings`, build the config, and either write it to `flags.out` or print it as JSON.

- [ ] **Step 4: Run and watch it pass**

Run: `npm test` — expect 179 passing.

- [ ] **Step 5: Export the real config**

```bash
node bin/tt.js league export --out analytics/data/league.json
cat analytics/data/league.json
```
Confirm it shows QB1/RB2/WR2/TE1/W-R-T1/K1/DEF1 and half-PPR scoring. Confirm `git status` shows nothing staged (it is gitignored).

- [ ] **Step 6: Commit**

```bash
git add src/cli.js bin/tt.js test/cli.test.js && git commit -m "feat: export league configuration for the draft engine"
```

---

### Task 2: League config loader in Python

**Files:** Create `analytics/src/tt/league.py`, `analytics/tests/test_league.py`

**Interfaces:** Produces `load_config(path) -> LeagueConfig`, `scoring_weights(config) -> dict[str, float]`, `starters_per_team(config) -> dict[str, float]`.

- [ ] **Step 1: Write the failing test**

```python
import json
import pytest
from tt.league import load_config, scoring_weights, starters_per_team

CONFIG = {
    "leagueKey": "470.l.1433971", "name": "Definitely Not Bots",
    "numTeams": 4, "maxTeams": 10, "draftStatus": "predraft",
    "rosterSlots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1},
    "scoring": {"Rec": 0.5, "Rec Yds": 0.1, "Rec TD": 6, "Rush Yds": 0.1,
                "Rush TD": 6, "Pass Yds": 0.04, "Pass TD": 4, "Int": -1},
}


def test_load_config_reads_the_exported_file(tmp_path):
    p = tmp_path / "league.json"
    p.write_text(json.dumps(CONFIG))
    cfg = load_config(p)
    assert cfg.num_teams == 4
    assert cfg.roster_slots["RB"] == 2


def test_scoring_weights_map_yahoo_names_to_nflverse_columns():
    # Yahoo says "Rec"; nflverse says "receptions". Without this mapping the
    # league's own scoring cannot be applied to the historical data.
    w = scoring_weights(load_config_from_dict(CONFIG))
    assert w["receptions"] == 0.5
    assert w["rushing_yards"] == 0.1
    assert w["passing_tds"] == 4


def test_starters_per_team_splits_flex_across_eligible_positions():
    # A W/R/T slot is filled by an RB, WR or TE. Attributing it wholly to one
    # position would misstate replacement level for all three.
    s = starters_per_team(load_config_from_dict(CONFIG))
    assert s["QB"] == 1.0
    # 2 RB + a share of the single flex
    assert 2.0 < s["RB"] < 3.0
    assert 2.0 < s["WR"] < 3.0
    assert pytest.approx(s["RB"] + s["WR"] + s["TE"], rel=1e-6) == 2 + 2 + 1 + 1


def test_starters_per_team_leaves_non_flex_positions_untouched():
    s = starters_per_team(load_config_from_dict(CONFIG))
    assert s["K"] == 1.0
    assert s["DEF"] == 1.0
```

Add a `load_config_from_dict` helper to the module for testing without a file.

- [ ] **Step 2: Run and watch it fail** — `ModuleNotFoundError: No module named 'tt.league'`

- [ ] **Step 3: Implement**

```python
"""League configuration.

Scoring and roster slots are read from the league itself rather than hardcoded,
because replacement level -- and therefore every VOR number -- is a direct
function of starting slots and team count. A constant would silently produce a
wrong draft board in any other league.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Yahoo's display names -> nflverse column names. Without this bridge the
# league's own scoring cannot be applied to historical data.
STAT_COLUMNS: dict[str, str] = {
    "Rec": "receptions",
    "Rec Yds": "receiving_yards",
    "Rec TD": "receiving_tds",
    "Rush Yds": "rushing_yards",
    "Rush TD": "rushing_tds",
    "Pass Yds": "passing_yards",
    "Pass TD": "passing_tds",
    "Int": "passing_interceptions",
}

# Which real positions a flex slot can be filled by.
FLEX_ELIGIBLE: dict[str, tuple[str, ...]] = {
    "W/R/T": ("RB", "WR", "TE"),
    "W/R": ("RB", "WR"),
    "Q/W/R/T": ("QB", "RB", "WR", "TE"),
}


@dataclass(frozen=True)
class LeagueConfig:
    league_key: str
    name: str
    num_teams: int
    max_teams: int
    draft_status: str
    roster_slots: dict[str, int]
    scoring: dict[str, float]


def load_config_from_dict(raw: dict) -> LeagueConfig:
    return LeagueConfig(
        league_key=raw["leagueKey"], name=raw["name"],
        num_teams=int(raw["numTeams"]), max_teams=int(raw["maxTeams"]),
        draft_status=raw.get("draftStatus", ""),
        roster_slots=dict(raw["rosterSlots"]), scoring=dict(raw["scoring"]),
    )


def load_config(path: str | Path) -> LeagueConfig:
    return load_config_from_dict(json.loads(Path(path).read_text()))


def scoring_weights(config: LeagueConfig) -> dict[str, float]:
    """League scoring keyed by nflverse column name."""
    return {
        STAT_COLUMNS[name]: float(value)
        for name, value in config.scoring.items()
        if name in STAT_COLUMNS
    }


def starters_per_team(config: LeagueConfig) -> dict[str, float]:
    """Starting slots per team, with flex shared across eligible positions.

    A flex slot is not owned by any one position, so attributing it wholly to
    RB (say) would understate replacement level for WR and TE. Splitting it
    evenly is a deliberate simplification: the true split depends on which
    position happens to be deepest, which is not knowable before the draft.
    """
    out: dict[str, float] = {}
    for slot, count in config.roster_slots.items():
        eligible = FLEX_ELIGIBLE.get(slot)
        if eligible:
            for position in eligible:
                out[position] = out.get(position, 0.0) + count / len(eligible)
        else:
            out[slot] = out.get(slot, 0.0) + float(count)
    return out
```

- [ ] **Step 4: Run and watch it pass** — 4 new tests.

- [ ] **Step 5: Commit**

```bash
git add analytics/src/tt/league.py analytics/tests/test_league.py
git commit -m "feat(analytics): derive scoring and starter slots from the league"
```

---

### Task 3: Season projections from history

**Files:** Create `analytics/src/tt/projections.py`, `analytics/tests/test_projections.py`

**Interfaces:** Produces `season_volume(history, seasons, weights) -> DataFrame` with per-player expected per-game `carries` and `targets`; `project_players(history, config, seasons, games=17) -> DataFrame` with `player_id, position, proj_points, p10, p90`.

- [ ] **Step 1: Write the failing test**

```python
def history():
    """Two players, two seasons, stable usage."""
    rows = []
    for season in (2024, 2025):
        for week in range(1, 18):
            rows.append({"player_id": "A", "season": season, "week": week,
                         "position": "RB", "carries": 18, "targets": 3,
                         "receptions": 2, "rushing_yards": 80, "receiving_yards": 15,
                         "rushing_tds": 0.5, "receiving_tds": 0.0})
            rows.append({"player_id": "B", "season": season, "week": week,
                         "position": "WR", "carries": 0, "targets": 9,
                         "receptions": 6, "rushing_yards": 0, "receiving_yards": 75,
                         "rushing_tds": 0.0, "receiving_tds": 0.5})
    return pd.DataFrame(rows)


def test_season_volume_weights_recent_seasons_more_heavily():
    # A player whose usage doubled last season should project above the flat
    # average of both seasons.
    h = history()
    h.loc[(h.player_id == "A") & (h.season == 2025), "carries"] = 36
    out = season_volume(h, seasons=(2024, 2025), recency_weights={2024: 1.0, 2025: 3.0})
    carries = out.set_index("player_id").loc["A", "carries_per_game"]
    assert carries > 27.0   # flat mean would be 27
    assert carries < 36.0   # and never exceeds the most recent season


def test_season_volume_returns_one_row_per_player():
    out = season_volume(history(), seasons=(2024, 2025))
    assert sorted(out["player_id"]) == ["A", "B"]


def test_project_players_produces_a_distribution_not_just_a_mean():
    out = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025))
    row = out.set_index("player_id").loc["A"]
    assert row["p10"] < row["proj_points"] < row["p90"]


def test_project_players_scales_with_the_number_of_games():
    short = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025), games=1)
    full = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025), games=17)
    assert full.set_index("player_id").loc["A", "proj_points"] > \
           10 * short.set_index("player_id").loc["A", "proj_points"]


def test_project_players_uses_league_scoring_not_a_constant():
    # A league that scores receptions at 1.0 must value a target-heavy WR more
    # than a half-PPR league does.
    half = project_players(history(), CONFIG_OBJ, seasons=(2024, 2025))
    ppr_cfg = replace_scoring(CONFIG_OBJ, {"Rec": 1.0})
    full_ppr = project_players(history(), ppr_cfg, seasons=(2024, 2025))
    assert full_ppr.set_index("player_id").loc["B", "proj_points"] > \
           half.set_index("player_id").loc["B", "proj_points"]
```

- [ ] **Step 2: Run and watch it fail**

- [ ] **Step 3: Implement** — compute recency-weighted per-game volume, shrink efficiency and TD rate toward positional priors with `features.shrunk_rate`, then compose a season distribution with `models.compose.simulate_points` scaled to `games`, scoring with `league.scoring_weights`.

- [ ] **Step 4: Run and watch it pass**

- [ ] **Step 5: Commit**

---

### Task 4: VOR and tiers

**Files:** Create `analytics/src/tt/vor.py`, `analytics/tests/test_vor.py`

**Interfaces:** `replacement_levels(config, teams) -> dict[str, int]`, `add_vor(projections, config, teams) -> DataFrame` adding `vor` and `tier`.

- [ ] **Step 1: Write the failing test**

```python
def test_replacement_level_scales_with_team_count():
    # THE most consequential number in the draft engine. At 10 teams 20 RBs
    # start; at 4 teams only 8 do, so RB10-20 go from starters to waiver fodder.
    deep = replacement_levels(CONFIG_OBJ, teams=10)
    shallow = replacement_levels(CONFIG_OBJ, teams=4)
    assert deep["RB"] > shallow["RB"]
    assert deep["RB"] == pytest.approx(round(2.5 * 10))
    assert shallow["RB"] == pytest.approx(round(2.5 * 4))


def test_vor_is_zero_at_the_replacement_player():
    proj = fake_projection_table()  # 40 RBs, descending points
    out = add_vor(proj, CONFIG_OBJ, teams=4)
    level = replacement_levels(CONFIG_OBJ, teams=4)["RB"]
    at_replacement = out[out.position == "RB"].sort_values("proj_points", ascending=False)
    assert at_replacement.iloc[level - 1]["vor"] == pytest.approx(0.0, abs=1e-9)


def test_vor_is_negative_below_replacement():
    out = add_vor(fake_projection_table(), CONFIG_OBJ, teams=4)
    rbs = out[out.position == "RB"].sort_values("proj_points", ascending=False)
    assert rbs.iloc[-1]["vor"] < 0


def test_shallower_leagues_compress_vor_at_the_position():
    # With fewer teams, replacement is a better player, so everyone's VOR falls.
    proj = fake_projection_table()
    deep = add_vor(proj, CONFIG_OBJ, teams=10).set_index("player_id")["vor"]
    shallow = add_vor(proj, CONFIG_OBJ, teams=4).set_index("player_id")["vor"]
    assert shallow.max() < deep.max()


def test_tiers_break_at_the_largest_value_gaps():
    out = add_vor(table_with_an_obvious_cliff(), CONFIG_OBJ, teams=10)
    top = out.sort_values("vor", ascending=False)
    assert top.iloc[0]["tier"] == 1
    # The cliff player starts a new tier.
    assert top.iloc[3]["tier"] > top.iloc[2]["tier"]
```

- [ ] **Step 2-5:** fail, implement, pass, commit.

---

### Task 5: ADP survival model

**Files:** Create `analytics/src/tt/survival.py`, `analytics/tests/test_survival.py`

**Interfaces:** `p_available(adp, stdev, pick) -> float`, `add_survival(board, pick, next_pick) -> DataFrame` adding `p_available_next` and `p_gone_by_next`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_player_drafted_far_earlier_is_almost_never_available():
    assert p_available(adp=5.0, stdev=2.0, pick=40) < 0.01


def test_a_player_drafted_far_later_is_almost_always_available():
    assert p_available(adp=120.0, stdev=10.0, pick=40) > 0.99


def test_probability_is_one_half_at_the_adp_itself():
    assert p_available(adp=40.0, stdev=8.0, pick=40) == pytest.approx(0.5, abs=0.01)


def test_availability_falls_monotonically_as_the_pick_gets_later():
    ps = [p_available(adp=40.0, stdev=8.0, pick=k) for k in (10, 30, 50, 70)]
    assert ps == sorted(ps, reverse=True)


def test_a_zero_stdev_player_is_a_step_function():
    # FFC occasionally reports stdev 0 for rarely-drafted players. It must not
    # divide by zero.
    assert p_available(adp=40.0, stdev=0.0, pick=39) == 1.0
    assert p_available(adp=40.0, stdev=0.0, pick=41) == 0.0
```

- [ ] **Step 2-5:** fail, implement with a normal CDF (`scipy.stats.norm.sf`), guard `stdev <= 0`, pass, commit.

---

### Task 6: The pick recommender

**Files:** Create `analytics/src/tt/draft.py`, `analytics/tests/test_draft.py`

**Interfaces:** `roster_need(taken, config) -> dict[str, int]`, `recommend(board, taken, pick, next_pick, config, teams) -> DataFrame` ranked by `expected_loss`.

- [ ] **Step 1: Write the failing test**

```python
def test_recommends_the_player_you_would_lose_not_the_best_one():
    # THE core rule. Two players of near-equal value: one certain to survive to
    # your next pick, one certain to be gone. Take the one you would lose.
    board = pd.DataFrame([
        {"player_id": "safe", "position": "RB", "vor": 50.0, "adp": 200.0, "stdev": 5.0},
        {"player_id": "scarce", "position": "WR", "vor": 48.0, "adp": 21.0, "stdev": 3.0},
    ])
    out = recommend(board, taken=[], pick=20, next_pick=30, config=CONFIG_OBJ, teams=10)
    assert out.iloc[0]["player_id"] == "scarce"


def test_a_player_certain_to_survive_has_near_zero_expected_loss():
    board = pd.DataFrame([
        {"player_id": "safe", "position": "RB", "vor": 50.0, "adp": 200.0, "stdev": 5.0}])
    out = recommend(board, taken=[], pick=20, next_pick=30, config=CONFIG_OBJ, teams=10)
    assert out.iloc[0]["expected_loss"] < 1.0


def test_already_drafted_players_are_excluded():
    board = pd.DataFrame([
        {"player_id": "gone", "position": "RB", "vor": 99.0, "adp": 5.0, "stdev": 2.0},
        {"player_id": "here", "position": "RB", "vor": 10.0, "adp": 30.0, "stdev": 5.0}])
    out = recommend(board, taken=["gone"], pick=20, next_pick=30, config=CONFIG_OBJ, teams=10)
    assert "gone" not in set(out["player_id"])


def test_a_filled_position_is_deprioritised():
    # With every RB slot filled and bench space tight, an RB of equal VOR should
    # rank below a starter-slot need.
    board = pd.DataFrame([
        {"player_id": "rb", "position": "RB", "vor": 30.0, "adp": 25.0, "stdev": 4.0},
        {"player_id": "te", "position": "TE", "vor": 30.0, "adp": 25.0, "stdev": 4.0}])
    taken_rbs = [{"player_id": f"r{i}", "position": "RB"} for i in range(3)]
    out = recommend(board, taken=taken_rbs, pick=20, next_pick=30,
                    config=CONFIG_OBJ, teams=10, respect_need=True)
    assert out.iloc[0]["player_id"] == "te"
```

- [ ] **Step 2-5:** fail, implement, pass, commit.

---

### Task 7: Mock draft simulator

**Files:** Create `analytics/src/tt/mock.py`, `analytics/tests/test_mock.py`

**Interfaces:** `simulate_draft(board, teams, rounds, my_slot, strategy, seed, adp_noise) -> DataFrame` (the drafted roster); `compare_strategies(board, strategies, trials, teams, my_slot, seed) -> DataFrame`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_draft_gives_every_team_the_right_number_of_picks():
    result = simulate_draft(board(), teams=10, rounds=15, my_slot=3,
                            strategy=strategy_adp, seed=1)
    assert len(result) == 15


def test_snake_order_reverses_each_round():
    picks = draft_order(teams=4, rounds=2)
    assert picks[:4] == [0, 1, 2, 3]
    assert picks[4:] == [3, 2, 1, 0]


def test_no_player_is_drafted_twice():
    result = simulate_draft(board(), teams=10, rounds=15, my_slot=3,
                            strategy=strategy_adp, seed=1, return_all=True)
    assert result["player_id"].is_unique


def test_the_same_seed_reproduces_the_same_draft():
    a = simulate_draft(board(), teams=10, rounds=15, my_slot=3, strategy=strategy_adp, seed=7)
    b = simulate_draft(board(), teams=10, rounds=15, my_slot=3, strategy=strategy_adp, seed=7)
    pd.testing.assert_frame_equal(a, b)


def test_adp_noise_changes_the_draft():
    calm = simulate_draft(board(), teams=10, rounds=15, my_slot=3,
                          strategy=strategy_adp, seed=7, adp_noise=0.1)
    wild = simulate_draft(board(), teams=10, rounds=15, my_slot=3,
                          strategy=strategy_adp, seed=7, adp_noise=20.0)
    assert list(calm["player_id"]) != list(wild["player_id"])


def test_compare_strategies_reports_one_row_per_strategy():
    out = compare_strategies(board(), {"adp": strategy_adp, "vor": strategy_vor},
                             trials=5, teams=10, my_slot=3, seed=1)
    assert set(out["strategy"]) == {"adp", "vor"}
    assert (out["trials"] == 5).all()
```

- [ ] **Step 2-5:** fail, implement, pass, commit.

---

### Task 8: Wire it up and run real mock drafts

**Files:** Modify `analytics/src/tt/__init__.py` if needed; create `analytics/src/tt/studies/draft_board.py`

- [ ] **Step 1: Build the real board**

Load the exported league config, nflverse history 2015-2025, and the cached FFC ADP; produce a board with projections, VOR, tiers and ADP joined via the existing crosswalk. Save nothing to git.

- [ ] **Step 2: Run the strategy comparison**

Run `compare_strategies` over at least 200 trials for team counts 4, 6, 8 and 10, comparing: pure ADP, pure VOR, and VOR-with-survival. Record the full table.

- [ ] **Step 3: Report honestly**

If VOR-with-survival does NOT beat pure ADP, say so plainly. That is a finding, not a bug — and it is exactly what mock drafts exist to discover. Do not tune the strategy until it wins; report what the simulation says.

- [ ] **Step 4: Commit and document**

Record the comparison table in `docs/draft-engine-design.md` under a new "Measured strategy comparison" section.

---

## Deferred to Phase 3

- In-season lineup and playoff optimisers (spec §3.6) — needed from week 1, not draft day.
- Trade valuation.
- Keeper/dynasty valuation.
