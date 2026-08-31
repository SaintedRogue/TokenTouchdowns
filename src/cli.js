/** Split argv into a command, positional arguments, and long flags. */
export function parseArgs(argv) {
  const flags = {};
  const args = [];
  for (const token of argv) {
    if (token === '-h' || token === '--help') { flags.help = true; continue; }
    if (token.startsWith('--')) {
      const [name, value] = token.slice(2).split('=');
      flags[name] = value ?? true;
      continue;
    }
    args.push(token);
  }
  const command = flags.help || args.length === 0 ? 'help' : args.shift();
  return { command, args, flags };
}

/** Render rows as an aligned plain-text table. */
export function formatTable(rows, columns) {
  if (rows.length === 0) return '(none)';
  const cell = (row, col) => {
    const v = row[col.key];
    // '-' means "no such value"; an empty string is a deliberate blank
    // (e.g. an unset marker column) and should render as whitespace.
    if (v === undefined || v === null) return '-';
    return String(v);
  };
  const widths = columns.map((col) =>
    Math.max(col.label.length, ...rows.map((r) => cell(r, col).length)));
  const line = (cells) =>
    cells.map((c, i) => (i === cells.length - 1 ? c : c.padEnd(widths[i]))).join('  ');
  return [
    line(columns.map((c) => c.label)),
    ...rows.map((r) => line(columns.map((c) => cell(r, c)))),
  ].join('\n');
}

import { SessionExpiredError, YahooApiError } from './client.js';

const USAGE = `tokentouchdowns — Yahoo fantasy football CLI

Usage: tt <command> [args] [--json]

Commands:
  login                  Sign in to Yahoo (opens a browser once)
  leagues                List your fantasy leagues
  teams [league_key]     List teams in a league
  standings [league_key] Show league standings
  roster [team_key]      Show a team roster (--week=N for a past/future week)
  matchup [week]         Show your matchup for a week (default: current)
  transactions [league]  Recent adds, drops and trades
  players [league_key]   Browse the player pool
  help                   Show this message

Player filters:
  --position=QB  --status=A  --search=kelce  --count=25  --sort=OR

Transaction filters:
  --type=add,drop  --team=<team_key>  --count=25

Omitted keys are resolved from your own leagues and team.`;

const LEAGUES_RESOURCE = 'users;use_login=1/games;game_keys=nfl/leagues';

/** Flags that map to Yahoo player matrix parameters. Anything else is ignored. */
const PLAYER_FILTERS = ['position', 'status', 'search', 'sort'];

/**
 * Build a `players` resource path with Yahoo's matrix-parameter syntax.
 * Always paginated: an unbounded player request pulls thousands of rows.
 */
export function buildPlayersResource(leagueKey, flags = {}) {
  // Default to overall rank: Yahoo's natural order is arbitrary, which puts
  // fringe players ahead of stars when browsing a position.
  const withDefaults = { sort: 'OR', ...flags };
  const parts = PLAYER_FILTERS
    .filter((f) => withDefaults[f] !== undefined && withDefaults[f] !== true)
    .map((f) => `${f}=${withDefaults[f]}`);
  parts.push(`start=${flags.start ?? 0}`, `count=${flags.count ?? 25}`);
  return `league/${leagueKey}/players;${parts.join(';')}`;
}

/**
 * Build a `roster` resource path, optionally scoped to a week.
 * A bare `--week` parses as `true` and must not become ";week=true".
 */
export function buildRosterResource(teamKey, flags = {}) {
  const week = flags.week;
  if (week === undefined || week === true) return `team/${teamKey}/roster`;
  return `team/${teamKey}/roster;week=${week}`;
}

/** Yahoo epoch seconds -> YYYY-MM-DD. UTC so output does not vary by machine. */
export function formatTimestamp(ts) {
  const n = Number(ts);
  if (!ts || !Number.isFinite(n)) return '-';
  return new Date(n * 1000).toISOString().slice(0, 10);
}

/** Build a `transactions` resource path. Always paginated. */
export function buildTransactionsResource(leagueKey, flags = {}) {
  const parts = [];
  if (flags.type && flags.type !== true) parts.push(`types=${flags.type}`);
  if (flags.team && flags.team !== true) parts.push(`team_key=${flags.team}`);
  parts.push(`count=${flags.count ?? 25}`);
  return `league/${leagueKey}/transactions;${parts.join(';')}`;
}

/**
 * One row per player movement: an add/drop transaction moves two players and
 * should read as two lines, not one.
 */
export function transactionRows(transactions) {
  const rows = [];
  for (const t of transactions ?? []) {
    const players = t.players ?? [];
    for (const p of Array.isArray(players) ? players : [players]) {
      // transaction_data is an object in every shape observed, but Yahoo
      // sometimes wraps single children in arrays -- tolerate both.
      const raw = p.transaction_data;
      const d = (Array.isArray(raw) ? raw[0] : raw) ?? {};
      rows.push({
        date: formatTimestamp(t.timestamp),
        type: t.type,
        player: p.name?.full,
        move: `${d.source_team_name ?? d.source_type ?? '?'} -> ${d.destination_team_name ?? d.destination_type ?? '?'}`,
      });
    }
  }
  return rows;
}

/** The team the logged-in user owns in a league. */
async function myTeamKey(client, leagueKey) {
  const { league } = await client.get(`league/${leagueKey}/teams`);
  const mine = league.teams.find((t) => t.is_owned_by_current_login === 1);
  if (!mine) throw new YahooApiError('Could not find your team in that league');
  return mine.team_key;
}

/** Every league the logged-in user plays in. */
async function myLeagues(client) {
  const data = await client.get(LEAGUES_RESOURCE);
  return data.users?.[0]?.games?.[0]?.leagues ?? [];
}

/** Use the supplied key, else fall back to the user's first league. */
async function resolveLeagueKey(client, given) {
  if (given) return given;
  const leagues = await myLeagues(client);
  if (leagues.length === 0) throw new YahooApiError('No leagues found for this account');
  return leagues[0].league_key;
}

export async function runCommand(
  { command, args, flags },
  { client, out = process.stdout, err = process.stderr, interactive = false } = {},
) {
  const emit = (rows, columns) => {
    out.write(flags?.json ? `${JSON.stringify(rows, null, 2)}\n` : `${formatTable(rows, columns)}\n`);
  };

  try {
    switch (command) {
      case 'help':
        out.write(`${USAGE}\n`);
        return 0;

      case 'leagues': {
        const leagues = await myLeagues(client);
        emit(leagues, [
          { key: 'league_key', label: 'KEY' },
          { key: 'name', label: 'NAME' },
          { key: 'num_teams', label: 'TEAMS' },
          { key: 'draft_status', label: 'DRAFT' },
        ]);
        return 0;
      }

      case 'teams': {
        const key = await resolveLeagueKey(client, args[0]);
        const { league } = await client.get(`league/${key}/teams`);
        const rows = league.teams.map((t) => ({
          ...t, mine: t.is_owned_by_current_login === 1 ? '*' : '',
        }));
        emit(rows, [
          { key: 'mine', label: '' },
          { key: 'team_key', label: 'KEY' },
          { key: 'name', label: 'NAME' },
        ]);
        return 0;
      }

      case 'standings': {
        const key = await resolveLeagueKey(client, args[0]);
        const { league } = await client.get(`league/${key}/standings`);
        const rows = league.standings.teams.map((t) => ({
          name: t.name,
          rank: t.team_standings?.rank,
          wins: t.team_standings?.outcome_totals?.wins,
          losses: t.team_standings?.outcome_totals?.losses,
        }));
        emit(rows, [
          { key: 'rank', label: 'RANK' },
          { key: 'name', label: 'NAME' },
          { key: 'wins', label: 'W' },
          { key: 'losses', label: 'L' },
        ]);
        return 0;
      }

      case 'roster': {
        let teamKey = args[0];
        if (!teamKey) {
          const leagueKey = await resolveLeagueKey(client, undefined);
          const { league } = await client.get(`league/${leagueKey}/teams`);
          teamKey = league.teams.find((t) => t.is_owned_by_current_login === 1)?.team_key;
          if (!teamKey) throw new YahooApiError('Could not find your team in that league');
        }
        const { team } = await client.get(buildRosterResource(teamKey, flags));
        if (!flags?.json) out.write(`Week ${team.roster?.week ?? '?'}\n`);
        const rows = (team.roster?.players ?? []).map((p) => ({
          name: p.name?.full,
          pos: p.display_position,
          team: p.editorial_team_abbr,
          status: p.status ?? '',
        }));
        emit(rows, [
          { key: 'name', label: 'PLAYER' },
          { key: 'pos', label: 'POS' },
          { key: 'team', label: 'TEAM' },
          { key: 'status', label: 'STATUS' },
        ]);
        return 0;
      }

      case 'players': {
        const key = await resolveLeagueKey(client, args[0]);
        const { league } = await client.get(buildPlayersResource(key, flags));
        const rows = (league.players ?? []).map((p) => ({
          name: p.name?.full,
          pos: p.display_position,
          team: p.editorial_team_abbr,
          bye: p.bye_weeks?.week,
          status: p.status ?? '',
        }));
        emit(rows, [
          { key: 'name', label: 'PLAYER' },
          { key: 'pos', label: 'POS' },
          { key: 'team', label: 'TEAM' },
          { key: 'bye', label: 'BYE' },
          { key: 'status', label: 'STATUS' },
        ]);
        return 0;
      }

      case 'transactions': {
        const key = await resolveLeagueKey(client, args[0]);
        const { league } = await client.get(buildTransactionsResource(key, flags));
        emit(transactionRows(league.transactions), [
          { key: 'date', label: 'DATE' },
          { key: 'type', label: 'TYPE' },
          { key: 'player', label: 'PLAYER' },
          { key: 'move', label: 'MOVE' },
        ]);
        return 0;
      }

      case 'matchup': {
        const week = args[0];
        const leagueKey = await resolveLeagueKey(client, flags.league);
        const teamKey = flags.team ?? (await myTeamKey(client, leagueKey));
        const { team } = await client.get(`team/${teamKey}/matchups`);
        const matchups = team.matchups ?? [];
        const found = week
          ? matchups.find((m) => String(m.week) === String(week))
          // "Current" = the first week not already played.
          : matchups.find((m) => m.status !== 'postevent') ?? matchups[0];
        if (!found) {
          err.write(`No matchup found for week ${week ?? '(current)'}\n`);
          return 1;
        }
        if (flags?.json) {
          out.write(`${JSON.stringify(found, null, 2)}\n`);
          return 0;
        }
        out.write(`Week ${found.week}  (${found.status})\n`);
        const rows = found.teams.map((t) => ({
          mine: t.is_owned_by_current_login === 1 ? '*' : '',
          name: t.name,
          points: t.team_points?.total,
          projected: t.team_projected_points?.total,
        }));
        out.write(`${formatTable(rows, [
          { key: 'mine', label: '' },
          { key: 'name', label: 'TEAM' },
          { key: 'points', label: 'PTS' },
          { key: 'projected', label: 'PROJ' },
        ])}\n`);
        return 0;
      }

      default:
        err.write(`Unknown command: ${command}\n\n${USAGE}\n`);
        return 1;
    }
  } catch (e) {
    if (e instanceof SessionExpiredError) {
      // Design doc §9.1: only auto-launch a browser where a human can use it.
      err.write(
        interactive
          ? 'Session expired. Re-authenticating...\n'
          : 'Session expired or rejected by Yahoo.\nRun: tt login\n',
      );
      return 2;
    }
    if (e instanceof YahooApiError) {
      err.write(`Yahoo API error: ${e.message}\n`);
      return 3;
    }
    throw e;
  }
}
