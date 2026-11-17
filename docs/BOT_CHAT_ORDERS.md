# Bot chat orders

Any player on a team can order that team's server-owned bots from chat. Orders
need no admin rights. Spectators are refused, and so is an order when the server
has no bots. Optional arguments name bots (by name prefix or substring); with
none, every bot on your team obeys.

| Command | Aliases | What the bots do |
| --- | --- | --- |
| `/follow [bot ...]` | | Escort you. If you die or change team, each following bot turns into a `/hold` at the spot where it stands and does **not** chase your respawn. If you disconnect, the order is freed (see below). |
| `/hold [bot ...]` | `/stay` | Stay at the spot where the bot stood when ordered. In a fight the bot may strafe and dodge within the hold leash of that spot, then walks back when the fight is over. |
| `/attack [bot ...]` | `/assault` | Push the enemy intel, else the enemy base, else hunt the nearest enemy. While a teammate carries the enemy intel the attackers split by bot id (see below): a share escorts him, the rest keep attacking the enemy base. When the carrier drops or dies, or the intel resets, every attacker pushes the objective again. |
| `/defend [bot ...]` | | Hold **our base**. A thief carrying our intel is chased only while he is within the chase radius of the base; beyond it the bot returns to and holds the base. |
| `/protect [intel] [bot ...]` | `/protectintel` | Intel-centric, no distance leash. See the table below. |
| `/escort [bot ...]` | | Escort the friendly carrier of the enemy intel, else you. Never splits, even when our intel is stolen. |
| `/free [bot ...]` | `/resume`, `/release` | Release the bots; they play normally again. |
| `/split <action> [<action>] [bot ...]` | | Split bots between two actions. See below. |
| `/status [bot ...]` | `/orders` | One line per bot: its order, its `/split` half if any, and what it is doing right now. Players see their own team; **spectators see both teams**, labeled (read-only, so no permission is needed). `/team <color> status` shows one team and `/team all status` both. |

A bot that carries intel always takes it home first, whatever it was ordered.
The carrier is found from the intel objective, which follows its holder, so an
escort works even when the carrier is outside the bot's perception cohort.

## `/protect`

| Our intel | Friendly carrier of enemy intel | Every protect-ordered bot |
| --- | --- | --- |
| safe | none | Guard our intel at base. |
| safe | yes | Escort the carrier. |
| carried by an enemy | none | Hunt the enemy carrier at any distance. |
| carried by an enemy | yes | Split by bot id. The lower share escorts the carrier, the upper share hunts the thief. |

With the default ratio of 0.5, two bots split one and one, four split two and
two, and three split two escorts and one hunter. A single bot escorts. At least
one bot always escorts, even at ratio 1.0.

## Splitting by bot id (`/attack` and `/protect`)

The director splits a team's bots that carry the same order between an escort
job and a push job (`/attack`: escort or keep attacking; `/protect`: escort or
hunt the thief):

- `/attack` splits while a teammate carries the enemy intel. `order_attack_escort_ratio`
  (default 0.5) of the attackers escort, rounded down; the rest keep attacking the
  enemy base (the intel moves with its carrier, so they do not converge on him).
  A single attacking bot attacks, and at least one bot always keeps attacking.
- `/protect` splits while a friendly carrier and a thief both exist
  (`order_protect_hunt_ratio`, as above).
- The escorts are the lowest bot ids. The jobs are **sticky**: fixed per bot id
  for as long as the split condition lasts. A bot that dies is still ordered, so it
  returns to the same job after respawn. Bots freed or newly ordered mid-carry
  never reshuffle the others; a newcomer fills whichever job is short.
- A bot that is itself carrying the intel goes home and takes no job.
- When the carrier drops or dies, the jobs are dropped and every attacker pushes the
  intel again. A later carrier gets a fresh, deterministic split.

## Short forms

Every command and argument has a short form. They work as top-level commands (`/ho`,
`/at rex`, `/st`), as the action arguments of `/split`, and as the command and team
arguments of `/team`, in any case and in any mix of long and short.

| Command | Short | Other aliases |
| --- | --- | --- |
| `/team` | `/t` | |
| `/split` | `/sp` | |
| `/follow` | `/fo` | |
| `/hold` | `/ho` | `/stay` |
| `/attack` | `/at` | `/assault` |
| `/defend` | `/de` | |
| `/protect` | `/pr` | `/protectintel` |
| `/escort` | `/es` | |
| `/free` | `/fr` | `/resume`, `/release` |
| `/status` | `/st` | `/orders` |

| Team argument | Short |
| --- | --- |
| `blue` | `b` |
| `green` | `g` |
| `all` (both teams) | `a` |

The team shorts are arguments only: there is no `/b`, `/g` or `/a` command (`/b` is the
existing `/ban` alias, which is left untouched). Examples: `/t b sp at pr` is `/team blue
split attack protect`; `/T G SP FR AT` is `/team green split free attack`; `/t a st` is
`/team all status`; `/sp ho fr` is `/split hold free`. A short action is also accepted
where `/split` takes one, so `/sp pr intel at` works. No short form collides with an
existing command or alias, and none is allowed to shadow one (a test enforces this).

## `/split`

Actions: `follow`, `hold` (`stay`), `attack` (`assault`), `defend`, `protect`, `escort`, and `free`
(`release`, `resume`). A `free` half plays normally but is still a half of the split.

- **`/split <action>`** keeps the previous order on half of the bots that have an order
  and gives the other half the new action (`/split free` frees that half). Bots without an
  order are left alone. **When no bot has an order** (after `/free`, a map change or a fresh
  start) the selected bots split into half free and half the new action, like
  `/split free <action>`; the larger half is the free one. `/split free` alone with no
  orders is refused, because it would leave everyone free.
- **`/split <a> <b>`** stops everything and splits the team between `a` and `b`; either may be
  `free`: `/split hold free`, `/split free attack`.
- Bots are ordered by id. With an odd count the **larger half** goes to the first-named
  action, or to the existing order in the single form. At least two bots are needed.
- **Halves are fixed by bot id** and stored in the orders, so they survive death and
  respawn. `/status` shows each bot's half (`Rex [hold, half 1]: ...`).
- **A team that is already split cannot be split again**, by hand or automatically; the
  command is refused with a message. Inside a split the automatic `/attack` and `/protect`
  escort/hunt splitting is off: each half runs the plain version of its order (`/attack`
  pushes the objective, `/protect` escorts when a carrier and a thief both exist).
- **A new plain order replaces the whole split.** The bots keep the orders they have, but
  they are no longer in a split, so the team can be split again. `/free` of a whole half
  also leaves no split behind.
- Ownership: the half given by `/split` belongs to whoever typed it; the half that kept
  its order stays with its previous issuer. When an issuer disconnects their orders are
  freed as above, and a split that lost a half dissolves.

## `/team <color|all> <command> ...` (admins and spectators)

An admin, or a spectator when the server allows it, can order a team's bots from
outside the team: `/team blue protect`, `/team blue split attack`,
`/team green split hold attack`, `/team green status`, `/team blue free rex`.

- **`all`** runs the command for **both teams**: `/team all hold`, `/team all split free attack`,
  `/team all status`. Each team answers on its own under a label (`Blue: 4 bot(s) holding
  position`, `Green: ...`), and a refusal for one team (already split, `follow` from outside it)
  never stops the other. The permissions are the same as for a single color.
- **Color** is the server's own team name: `blue` and `green` for the stock teams,
  or `team1` / `team2`, or the configured `[teams]` name. `/team blue` on its own
  still just changes your team (and now accepts `blue` / `green` too).
- **Command** is any of `follow`, `hold`, `attack`, `defend`, `protect`, `escort`, `free`,
  `status` and `split`, with the same aliases, optional bot names and `/split` forms as
  the plain commands.
- **Who may:** admins always. Spectators only when `order_team_command_spectators = true`
  (default `false`). A regular team member is told to use the plain command for their
  own team, and a spectator on a server that does not allow it is told so.
- **`follow`** needs an issuer on that team to follow, so it is refused (`/split` forms
  naming `follow` too) when you are not on it. An admin playing on blue can `/team blue
  follow`.
- It never moves you: your own team does not change. Orders are recorded under your
  player id like any other, so they are freed if you disconnect.
- Other players' orders keep working as before: a team member's plain order replaces
  yours on the bots it covers (they then belong to that member), and any teammate can
  `/free` bots you ordered.

## Order lifetime and ownership

An order belongs to the bot, not to its life. It survives the bot's own death
and respawn (a `/hold` bot walks back to its post after respawning) and ends on:

- `/free`, by **any** teammate, whoever gave the order;
- a map change or round restart, the bot leaving, or the bot switching team;
- the player who issued it **disconnecting**. Every order records its issuer, and
  when that player leaves the server all the orders they issued are freed, of every
  type (follow, hold, escort, attack, defend, protect). Orders issued by other
  players stay.

Ownership follows the latest order: a bot whose order is replaced by someone
else's belongs to the new issuer, and only their disconnect frees it.

`/follow` is the one order that also reacts to its issuer staying connected but
going away. If the issuer dies or changes team, each following bot becomes a
`/hold` at the spot where it stands. A spectator or an issuer who is merely dead
keeps all their other orders.

## Settings (`[bots]` in `config.toml`)

| Key | Default | Meaning |
| --- | --- | --- |
| `order_hold_leash` | `6.0` | Blocks a `/hold` bot may stray from its post while fighting, 0-40. `0` makes it stand still. |
| `order_defend_chase_radius` | `40.0` | Blocks from our base within which `/defend` chases a thief, 0-400. `0` never chases. |
| `order_protect_hunt_ratio` | `0.5` | Share of `/protect` bots that hunt the thief when a carrier also needs escort, 0-1. |
| `order_attack_escort_ratio` | `0.5` | Share of `/attack` bots that escort a friendly carrier of the enemy intel while the rest keep attacking, 0-1. |
| `order_team_command_spectators` | `false` | Lets spectators use `/team <color> <command>` too. Admins always may. |

The director stamps the leash and chase radius, and each bot's job, on every
published order (`ObjectiveSnapshot.params`), so the isolated worker needs no
configuration of its own.

## Notes and limits

- The defend chase has hysteresis: a bot already more than 12 blocks from the
  base keeps chasing until the thief passes 125 % of the radius, so a thief
  walking the edge does not flip bots between chasing and guarding.
- The hold leash applies to weapons that strafe. A planted weapon (the
  Minigun) holds its footing and relocates only when it takes hits, as it does
  without any order.
- Roles are expressed in CTF terms (`ctf_base`, `ctf_intel`). In modes without
  them the orders fall back to the team anchor or the spot where the bot was
  ordered.
- `/status` reads the worker's latest intent, which can be a fraction of a
  second old. A dead bot shows as waiting to respawn.

Implementation: `commands/bot_orders.py` (commands), `server/bot_ai/player_orders.py`
(order book and decisions), `BotDirector.give_order` / `order_status`,
`ModePolicyMemory.decide` (orders are consulted before the mode policy) and the
leash in `SimpleBotBrain._combat_intent`. Tests: `tests/test_bot_player_orders.py`.
