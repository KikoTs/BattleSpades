# Score, assist, award, and map vote audit

## Recovered retail data

Local source: `G:/AoSRevival/aceofspades_source`.

- `shared/constants_gamemode.py:206–227`: ordinary kill 100, headshot 150,
  melee 150, assist 50, suicide and teamkill -100, assist percentage 50.0.
- `shared/constants_gamemode.py:236`: TDM team score per enemy kill is 1.
  There is no corresponding team headshot-bonus constant. The optional
  custom-server setting remains supported, with the shipped default now 0.
- `shared/constants.py:1978–2044`: three game-stat rows per team; award ids
  are categories, not score values. Type 0 is distance travelled, 5 kills,
  8 headshots, 11 kill streak, 14 melee kills, and 18 assists.
- `aoslib/scenes/ingame_menus/__init__.py:214–281`: original
  `draw_game_stats` renders exactly three rows using `GAME_STAT_TYPES`.
- The results agent confirmed original GameScene `process_packet_game_stats`
  at `0x102479C0`: packet 67 appends award rows to the wire team 2 or 3.
  Its team field does not encode the winning team.
- Recovered packet schemas: SetScore 85 is type/reason/specifier bytes plus
  signed absolute 32-bit score; GameStats 67 is count, team id, and pairs of
  player id/award id. GenericVote 47 CAST echoes an advertised candidate's
  literal localization token. The server continues matching the token exactly.

## Fixes

- TDM now uses the recovered personal amounts and reason ids, gives one team
  point for every enemy kill by default, and applies personal suicide/teamkill
  penalties. Forced class/team transitions do not count as normal deaths.
  Friendly kills no longer increase kill counts or streaks.
- Accepted damage records actual HP removed after damage rules and protection.
  An eligible teammate who contributed at least half of full health receives
  +50 once when a different teammate lands the enemy kill. Healing removes
  damage credit. Self, friendly, environmental, transition, stale, disconnected,
  replaced-player-id, changed-team, and respawned-life contributions cannot
  receive assists. Dead contributors may receive an assist until they respawn.
  Round end/editor modes suppress these rewards. History is consumed before
  score emission, and the death generation prevents repeated awards.
- GameStats now sends separate team packets with up to three actual positive
  combat awards. Unsupported metrics are omitted. A once-per-round guard
  prevents duplicated rows in the original client's append-only receiver.
- Same-map rounds clear match scores and combat counters. Lifetime profile
  counters remain cumulative, and the results bridge resets only the match
  counter baselines so the next round still credits its score deltas.
- Clients that finish loading receive reliable absolute scores after roster
  and terrain catch-up. Scores for unknown dead player ids wait until their
  next CreatePlayer. Replayed scores never generate profile rewards.
- Vote casts validate current player identity and a connected in-game human
  connection. Repeating the same choice sends no extra update. A revote replaces
  that player's choice. Deadline checks run before casts/reveals, and the live
  tick/waiter use a monotonic deadline. A departing nonvoter lets remaining
  complete ballots finish. An old cancelled waiter cannot resolve a new vote.
  Kick resolution retires its state before calling disconnect callbacks.

## Limits

There is no original dedicated-server binary. The assist reward and threshold
are recovered constants; interpreting 50% as half of full player health, a
10-second contribution window, healing-credit reduction, posthumous assists,
and the deterministic combat-award priority/tie rules are explicit server
policies, not claims of exact retail server behavior. Other objective modes'
existing kill/objective score policies were not replaced by this TDM audit.

The client results UI remains responsible for showing packet 67 data. Packet
53 is still reserved for a full map transition; same-map restarts preserve the
existing GameScene.

## Validation

The final combined targeted run passed **245 tests in 50.03 seconds**.
Coverage exercises TDM damage/death dispatch, score wire data,
assist boundaries and abuse cases, repeated awards, round reset/profile
baselines, map ballot timeouts/revotes/identity changes, loading catch-up,
and map transition/end-sequence orchestration. No server was restarted by
this agent.
