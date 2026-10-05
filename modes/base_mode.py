"""
Base game mode class.
Provides hooks for game events that modes can override.
"""

from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING, Optional, List, Tuple

import shared.constants as _C


if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


class BaseMode(ABC):
    """
    Abstract base class for game modes.
    Override the event methods to implement custom game logic.
    """
    
    # Mode metadata
    name: str = "Base Mode"
    description: str = "Base game mode"
    # Most Battle Builder modes add an entity-11 gravestone after KillAction.
    # Classic CTF overrides this with the client-owned ClassicCorpse Character.
    death_representation: str = "grave"
    
    # Scoring
    score_limit: int = 10
    time_limit: int = 0  # Seconds, 0 = unlimited
    # Retail generic personal kill/suicide/team-kill scores. Tutorial and
    # UGC have no combat score economy (combat_scores._scoring_active
    # excludes them too); Zombie scores only through its ZOM_* rules.
    generic_scoring_enabled: bool = True

    # Kill types that are self-inflicted even without a killing player
    # (retail GENERIC_SCORE_SUICIDE covers /kill, own explosives and
    # falling; the stock tables have no drowning kill type).
    SELF_INFLICTED_WORLD_KILL_TYPES = frozenset((int(_C.FALL_KILL),))
    # Objective/environment kills: bombs and airstrikes are server-owned
    # blasts. Their victims (and a carrier holding a detonating bomb) are
    # never charged a suicide or team-kill penalty.
    OBJECTIVE_KILL_TYPES = frozenset((int(_C.AIRSTRIKE_KILL), int(_C.BOMB_KILL)))
    
    def __init__(self, server: 'BattleSpadesServer'):
        self.server = server
        self.started = False
        self.ended = False
        self.winner: Optional[int] = None  # Winning team ID
        # Retail countdown cues fired this round (thresholds in seconds).
        self._countdown_fired: set[float] = set()
        self._countdown_last_remaining: float = float("inf")

        # Round timing
        self.start_time: float = 0.0
        self.elapsed_time: float = 0.0
        # Fixed remainder of accepted events at the first timeout check.
        # Later arrivals must not extend the deadline or change its winner.
        self._timeout_events_remaining: int | None = None
        # Timeout music fires exactly once, TIMEOUT_MUSIC_SECONDS before the end.
        self._timeout_music_played = False
        # Gameplay music bed is re-sent on a cadence so a finite track never
        # leaves the round silent; this stamps the last (re)start time.
        self._last_music_at: float = 0.0
        # Guards the async end sequence so it runs exactly once.
        self._end_sequence_running = False
        self._end_task = None
        # Set by the match transition while this mode's roster is detached
        # (admin map/mode change or same-map restart): leave hooks may still
        # run, but the retiring mode must never finish the match, open a map
        # vote or start its end sequence underneath the replacement.
        self.retiring = False
    
    # =========================================================================
    # Lifecycle Events
    # =========================================================================
    
    async def on_mode_start(self):
        """Called when the mode starts (also on every round restart)."""
        import time
        self.started = True
        # Reset the full end-of-round state so a RESTART after a natural end
        # actually revives the mode (previously ended/winner stuck True and the
        # timer + win checks never ran again).
        self.ended = False
        self.retiring = False
        self.winner = None
        self._timeout_music_played = False
        self._end_sequence_running = False
        self.start_time = time.time()
        self.elapsed_time = 0.0
        self._timeout_events_remaining = None
        self._countdown_fired.clear()
        self._countdown_last_remaining = float("inf")

        from server.scoreboard import reset_round_scores
        reset_round_scores(self.server)

        # Kick off the in-game music bed so the round is never silent. This
        # is a deliberate deviation (retail rounds were silent until the
        # final 61 s); [audio] mode_start_music = false restores retail and
        # then this only stops the previous round's track.
        from server.audio import play_gameplay_music
        play_gameplay_music(self.server)
        self._last_music_at = self.start_time

        # Map resources belong to every ruleset, not just TDM. Mode-specific
        # objectives are created by subclasses after this shared boundary.
        map_resources = getattr(self.server, "map_resources", None)
        if map_resources is not None:
            map_resources.rebuild()

    async def on_mode_end(self, winner: Optional[int] = None):
        """Called when the mode ends — run the full end-of-round sequence
        (victory audio → stats screen → restart), not just a chat line."""
        self.ended = True
        if self.retiring:
            # A transition is replacing this mode: no vote, no end screen.
            return
        import time
        vote_manager = getattr(self.server, "vote_manager", None)
        ensure_vote = getattr(
            vote_manager,
            "ensure_round_end_map_vote",
            None,
        )
        if not callable(ensure_vote):
            ensure_vote = getattr(vote_manager, "ensure_map_vote", None)
        if callable(ensure_vote):
            ensure_vote(time.time())
        self.winner = winner
        await self._run_end_sequence(winner)

    async def cancel_end_sequence(self):
        """Cancel a delayed victory restart before an admin transition.

        This runs on the gameplay event loop and emits no packets. Its only job
        is to ensure an old mode cannot wake later and reset a replacement map
        or mode underneath connected clients.
        """
        import asyncio

        task = self._end_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._end_task = None
        self._end_sequence_running = False

    def begin_retirement(self, *, end_round: bool = True) -> None:
        """Mark this mode as being replaced by a match transition.

        ``end_round`` also sets ``ended`` so leave hooks skip gameplay (a map
        or mode rollover). A same-map restart keeps the round live for leave
        bookkeeping; ``on_mode_start`` clears the flag again.
        """
        self.retiring = True
        if end_round:
            self.ended = True

    async def deactivate(self):
        """Retire this mode without starting the normal victory sequence."""
        self.retiring = True
        await self.cancel_end_sequence()
        self.started = False
        self.ended = True

    async def on_tick(self, tick: int):
        """Called every game tick."""
        if self.ended:
            return
        import time
        now = time.time()
        self.elapsed_time = now - self.start_time

        # Low-frequency (1 Hz) map-escape / hiding reveal; self-throttled.
        if self.escape_watch_enabled:
            try:
                from server import escape_watch

                escape_watch.tick(self.server, self)
            except Exception:
                logging.getLogger(__name__).exception("escape watch failed")

        # The gameplay bed (started in on_mode_start) alure-loops forever, so no
        # re-send is needed. Swap to the last-minute "game_ending" track once,
        # 61s before the clock runs out.
        from server.audio import TIMEOUT_MUSIC_SECONDS, play_timeout_music
        if self.time_limit > 0 and not self._timeout_music_played:
            if self.time_limit - self.elapsed_time <= TIMEOUT_MUSIC_SECONDS:
                self._timeout_music_played = True
                play_timeout_music(self.server)

        if self.time_limit > 0:
            self._announce_countdown(self.time_limit - self.elapsed_time)

        # F1/F2/F3 are dedicated map-vote bindings in the shipped client.
        # Present the ballot TIME_AFTER_MAP_VOTE_START_BEFORE_END (10 s)
        # before the clock runs out.
        if self.time_limit > 0:
            from server.voting import MAP_VOTE_LEAD_SECONDS
            remaining = self.time_limit - self.elapsed_time
            if remaining <= MAP_VOTE_LEAD_SECONDS:
                vote_manager = getattr(self.server, "vote_manager", None)
                ensure_vote = getattr(vote_manager, "ensure_map_vote", None)
                if callable(ensure_vote):
                    ensure_vote(now)

        # A bounded drain may split an accepted death/kill pair across ticks.
        # Capture its fixed remainder once: waiting for the entire live queue
        # to become empty would let a steady stream of late events extend the
        # round indefinitely. SimulationRuntime drains this snapshot within
        # the normal budget before delivering any newer mode events.
        if self.time_limit > 0 and self.elapsed_time >= self.time_limit:
            if self._timeout_events_remaining is None:
                self._timeout_events_remaining = len(getattr(self.server, "_mode_events", ()))
            if self._timeout_events_remaining == 0:
                await self._end_by_time()
        else:
            self._timeout_events_remaining = None
    
    async def on_round_start(self):
        """Called when a new round starts."""
        pass
    
    async def on_round_end(self, winner: Optional[int] = None):
        """Called when a round ends."""
        pass
    
    # =========================================================================
    # Player Events
    # =========================================================================
    
    async def on_player_join(self, player: 'Player'):
        """Called when a player joins the game."""
        pass
    
    async def on_player_leave(self, player: 'Player'):
        """Called when a player leaves the game."""
        pass
    
    async def on_player_spawn(self, player: 'Player'):
        """Called when a player spawns."""
        pass
    
    async def on_player_kill(self, killer: 'Player', victim: 'Player', kill_type: int):
        """Called when a player kills an enemy (cross-team only).

        Awards the generic retail personal kill score. Subclasses that add
        objective extras call ``super()`` first; a mode with its own score
        economy (Zombie) overrides without ``super()``.
        """
        self.award_generic_kill_score(killer, victim, kill_type)

    async def on_player_death(self, player: 'Player', killer: Optional['Player'], kill_type: int):
        """Called for every death.

        Player.die queues ``on_player_kill`` only for cross-team kills, so
        suicides and team kills arrive here alone: this applies their
        generic retail penalty exactly once. Subclasses overriding this hook
        must call ``super()`` to keep the penalty.
        """
        self.apply_generic_death_penalty(player, killer, kill_type)
        from server.spawn_selection import record_death

        record_death(self.server, player)

    # Retail generic personal scores (shared/constants_gamemode.py).

    def _generic_scoring_blocked(self, *players, kill_type: int, mode_opted_in: bool = False) -> bool:
        import shared.constants as C
        from server.game_constants import TEAM1, TEAM2

        if self.ended or not (self.generic_scoring_enabled or mode_opted_in):
            return True
        config = getattr(self.server, "config", None)
        if bool(getattr(config, "ugc_runtime", False)) or str(
            getattr(config, "default_mode", "")
        ).lower() in {"ugc", "tut", "tutorial"}:
            return True
        if int(kill_type) in {
            int(C.FORCED_TEAM_CHANGE_KILL),
            int(C.TEAM_CHANGE_KILL),
            int(C.CLASS_CHANGE_KILL),
        }:
            return True
        return any(
            int(getattr(player, "team", -1)) not in (TEAM1, TEAM2)
            for player in players
        )

    def award_generic_kill_score(self, killer, victim, kill_type: int, *, mode_opted_in: bool = False) -> int:
        """Give ``killer`` GENERIC_SCORE_KILL/HEADSHOT/MELEE for an enemy kill.

        Returns the points awarded (0 when the kill is not eligible).
        ``mode_opted_in`` lets a mode with its own economy (Zombie) pay the
        generic score for the kills it chooses.
        """
        import shared.constants as C
        import shared.constants_gamemode as CG
        from server.game_constants import KILL_HEADSHOT, KILL_MELEE

        if killer is None or victim is None or killer is victim:
            return 0
        if self._generic_scoring_blocked(killer, victim, kill_type=kill_type, mode_opted_in=mode_opted_in):
            return 0
        if int(killer.team) == int(victim.team):
            return 0
        if int(kill_type) == int(KILL_HEADSHOT):
            amount, reason = CG.GENERIC_SCORE_HEADSHOT, C.KILL_SCORE_HEADSHOT_REASON
        elif int(kill_type) == int(KILL_MELEE):
            amount, reason = CG.GENERIC_SCORE_MELEE, C.KILL_SCORE_MELEE_REASON
        else:
            amount, reason = CG.GENERIC_SCORE_KILL, C.KILL_SCORE_REASON
        self._add_generic_score(killer, int(amount), int(reason))
        bonuses = getattr(killer, "kill_bonuses", None)
        revenge, payback = (
            bonuses.pop(int(victim.id), (False, False))
            if isinstance(bonuses, dict) else (False, False)
        )
        if revenge:
            self._add_generic_score(
                killer, int(CG.GENERIC_SCORE_REVENGE),
                int(C.KILL_SCORE_REVENGE_REASON),
            )
            amount += int(CG.GENERIC_SCORE_REVENGE)
        if payback:
            self._add_generic_score(
                killer, int(CG.GENERIC_SCORE_PAYBACK),
                int(C.KILL_SCORE_PAYBACK_REASON),
            )
            amount += int(CG.GENERIC_SCORE_PAYBACK)
        if self.GENERIC_TEAMPLAY_AWARDS:
            amount += self._award_teamplay_kill_events(killer, victim)
        return int(amount)

    # TDM_Reload / TDM_Defend / TDM_Distract (reasons 10/11/8, 50 points
    # each: GENERIC_SCORE_RELOAD, GENERIC_SCORE_DEFEND, TDM_SCORE_DISTRACT)
    # are the plain combat economy's team-play events; objective modes pay
    # their own <MODE>_Defend/_Distract instead, so only TDM enables them.
    GENERIC_TEAMPLAY_AWARDS = False

    def _award_teamplay_kill_events(self, killer, victim) -> int:
        """Pay the generic team-play kill events (rules audit 2026-09-27 #13).

        Triggers are inferred -- the retail server code is not recovered:
        * Reloading Kill (killer): the victim died mid-reload.
        * Defend (killer): the victim had hurt one of the killer's living
          teammates within PLAYER_INTERACTION_EXPIRY_SECONDS (5 s).
        * Distraction (each such teammate): it drew the victim's fire while
          the killer finished him. The stock commendation table groups
          Distract with Assist/Reload (COM_TDM_ASSIST), i.e. a supporting
          award, and Defend with the kill scores (TDM_TOTAL_SCORE).
        """
        import time

        import shared.constants as C
        import shared.constants_gamemode as CG
        from server.combat_scores import ASSIST_WINDOW_SECONDS, round_stats

        paid = 0
        if bool(getattr(victim, "died_reloading", False)):
            self._add_generic_score(
                killer, int(CG.GENERIC_SCORE_RELOAD), int(C.KILL_SCORE_RELOAD_REASON)
            )
            paid += int(CG.GENERIC_SCORE_RELOAD)
        now = time.monotonic()
        victim_id = int(getattr(victim, "id", -1))
        distracted = []
        for mate in list(getattr(self.server, "players", {}).values()):
            if (
                mate is killer
                or mate is victim
                or not bool(getattr(mate, "alive", False))
                or getattr(mate, "team", None) != getattr(killer, "team", None)
            ):
                continue
            contribution = (getattr(mate, "damage_contributions", {}) or {}).get(victim_id)
            if (
                contribution is not None
                and contribution.player is victim
                and now - float(contribution.touched) <= ASSIST_WINDOW_SECONDS
            ):
                distracted.append(mate)
        if distracted:
            self._add_generic_score(
                killer, int(CG.GENERIC_SCORE_DEFEND), int(C.KILL_SCORE_DEFEND_REASON)
            )
            paid += int(CG.GENERIC_SCORE_DEFEND)
            round_stats(killer).increment(C.MOST_DEFENDS)
            for mate in distracted:
                self._add_generic_score(
                    mate, int(CG.TDM_SCORE_DISTRACT), int(C.KILL_SCORE_DISTRACT_REASON)
                )
                round_stats(mate).increment(C.MOST_DISTRACTIONS)
        return paid

    def apply_generic_death_penalty(self, player, killer, kill_type: int) -> int:
        """GENERIC_SCORE_SUICIDE to a real suicide (killer is the victim) or a
        self-inflicted world death (fall), GENERIC_SCORE_TEAMKILL to a team
        killer. Objective blasts (bomb/airstrike) and unattributed deaths are
        exempt. Returns the (negative) points applied, else 0."""
        import shared.constants as C
        import shared.constants_gamemode as CG

        if player is None:
            return 0
        if getattr(player, "death_penalty_exempt", False):
            # Server-forced deaths (bot terrain recovery) cost nothing.
            player.death_penalty_exempt = False
            return 0
        if int(kill_type) in self.OBJECTIVE_KILL_TYPES:
            # Bomb/airstrike blasts are objective events, not suicides or
            # team kills (their "thrower" is the server or a bomb carrier).
            return 0
        if killer is None and int(kill_type) not in self.SELF_INFLICTED_WORLD_KILL_TYPES:
            # A world death with no killing player (ownerless mine, server
            # blast) is not self-inflicted.
            return 0
        if killer is None or killer is player:
            if self._generic_scoring_blocked(player, kill_type=kill_type):
                return 0
            self._add_generic_score(
                player, int(CG.GENERIC_SCORE_SUICIDE), int(C.SUICIDE_SCORE_REASON)
            )
            return int(CG.GENERIC_SCORE_SUICIDE)
        if self._generic_scoring_blocked(player, killer, kill_type=kill_type):
            return 0
        if int(killer.team) != int(player.team):
            return 0
        self._add_generic_score(
            killer, int(CG.GENERIC_SCORE_TEAMKILL), int(C.KILL_SCORE_TEAMKILL_REASON)
        )
        return int(CG.GENERIC_SCORE_TEAMKILL)

    def _add_generic_score(self, player, amount: int, reason: int) -> None:
        from server.scoreboard import send_player_score

        players = getattr(self.server, "players", None)
        if players is not None:
            try:
                owner = players.get(int(getattr(player, "id", -1)))
            except (TypeError, ValueError):
                owner = None
            if owner is not player:
                # Departed (its compact id may already name a newcomer): no
                # SetScore for a ghost.
                return
        player.score = int(getattr(player, "score", 0)) + int(amount)
        send_player_score(self.server, player, reason=int(reason))

    def award_objective_event(self, player, amount: int, reason: int, *,
                              award: int | None = None) -> bool:
        """Pay one retail mode score event (SetScore popup ``reason``).

        Shared entry point for objective modes: skips an ended/retiring
        round, tutorial/UGC, spectators and departed/re-used ids; bots are
        eligible. ``score_changed`` rolls the reason into its COM_*
        commendation aggregate; ``award`` optionally counts a GameStats
        round award (e.g. ``C.MOST_DEFENDS``). See
        ``server.combat_scores.award_score_event``.
        """
        from server.combat_scores import award_score_event

        return award_score_event(
            self.server, player, int(amount), int(reason), award=award, mode=self
        )

    async def on_player_team_change(self, player: 'Player', old_team: int, new_team: int):
        """Called when a player changes team."""
        pass
    
    # =========================================================================
    # Block Events
    # =========================================================================
    
    async def on_block_build(self, player: 'Player', x: int, y: int, z: int):
        """Called when a player places a block."""
        pass
    
    async def on_block_destroy(self, player: 'Player', x: int, y: int, z: int):
        """Called when a player destroys a block."""
        pass

    async def on_blocks_destroyed(
        self,
        player: 'Player',
        positions: tuple[tuple[int, int, int], ...],
        mined: bool,
    ):
        """Called once for an authoritative bulk terrain removal.

        ``mined`` is captured synchronously at removal time so delayed mode
        event processing cannot mistake a later tool swap for the action that
        actually removed the voxels.
        """
        pass
    
    async def on_block_line(self, player: 'Player', x1: int, y1: int, z1: int, 
                            x2: int, y2: int, z2: int):
        """Called when a player builds a line of blocks."""
        pass
    
    # =========================================================================
    # Combat Events
    # =========================================================================
    
    async def on_grenade_explode(self, player: 'Player', x: float, y: float, z: float):
        """Called when a grenade explodes."""
        pass
    
    async def on_player_damage(self, player: 'Player', attacker: Optional['Player'], 
                               damage: int, kill_type: int) -> int:
        """
        Called when a player takes damage.
        Return modified damage value (can reduce/increase).
        """
        return damage
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    # =========================================================================
    # Late-joiner reveal and bot retirement hooks
    # =========================================================================

    def reveal_to(self, connection) -> None:
        """Replay shared round state to one newly settled GameScene.

        Subclasses extend this (``super().reveal_to(connection)`` first).
        """
        self.reveal_round_state_to(connection)
        try:
            from server import escape_watch

            escape_watch.reveal_to(self.server, connection)
        except Exception:
            logging.getLogger(__name__).debug(
                "escape marker reveal failed", exc_info=True
            )

    # =========================================================================
    # Map-escape watch hooks (server/escape_watch.py)
    # =========================================================================

    # Modes without combat stakes (tutorial, UGC building) turn this off.
    escape_watch_enabled: bool = True

    def mode_marks_player(self, player) -> bool:
        """Whether this mode currently owns ``player``'s high-minimap marker.

        The escape watch never clears a marker the mode set (CTF carrier,
        VIP crown, last Zombie survivor).
        """
        return False

    def escape_watch_objective_player(self, player) -> bool:
        """Whether sealing ``player`` away in a blocked pocket matters here.

        True for objective carriers/holders; the escape watch then also
        reveals them when they entomb themselves. Default: nobody.
        """
        return False

    def objective_presence_eligible(self, player) -> bool:
        """Whether ``player`` may count toward holding an objective zone.

        Excludes AFK bodies (no movement and no aim change for
        ``objective_afk_seconds``, default 60, 0 disables) and players the
        escape watch has revealed as escaped/sealed.
        """
        from . import objective_guard

        return objective_guard.presence_eligible(self.server, player)

    def reveal_round_state_to(self, connection) -> None:
        """Hand an end-screen joiner the forced scoreboard.

        A joiner arriving during the end dwell otherwise walks around a live
        scene while everyone else sees ForceShowScores(1). Its music (the
        ending/timeout track) is chosen by ``join_music_track`` and started by
        the world reveal before the catch-up burst.
        """
        try:
            if self.ended and self._end_sequence_running and not self.retiring:
                if self._end_round_scoreboard_enabled():
                    from shared.packet import ForceShowScores

                    packet = ForceShowScores()
                    packet.forced = 1
                    connection.send(bytes(packet.generate()), reliable=True)
        except Exception:
            logging.getLogger(__name__).debug(
                "round state reveal failed", exc_info=True
            )

    def join_music_track(self) -> Optional[str]:
        """Specific track a newly settled client should hear, or None.

        The world reveal starts the joiner's music BEFORE its terrain and
        roster catch-up: that burst of hit/build effects exhausts the stock
        client's 128 OpenAL sources, after which a stream cannot start (live
        2026-09-26). The mode therefore answers up front: the game_ending
        track for an end-screen or final-minute joiner, else None (the
        caller's gameplay bed).
        """
        import random
        from server.audio import GAME_ENDING_TRACKS

        if self.ended and self._end_sequence_running and not self.retiring:
            return random.choice(GAME_ENDING_TRACKS)
        if self.started and not self.ended and self._timeout_music_played:
            return random.choice(GAME_ENDING_TRACKS)
        return None

    def _owns_slot(self, player) -> bool:
        """True while ``player`` still owns its compact id in the roster.

        Queued mode events and id-keyed mode state can outlive a departure;
        the slot may already belong to a newcomer, who must never be paid
        (or charged) for the previous body.
        """
        if player is None:
            return False
        players = getattr(self.server, "players", None)
        if players is None:
            return True
        try:
            return players.get(int(getattr(player, "id", -1))) is player
        except (TypeError, ValueError):
            return False

    def bot_retire_safe(self, bot) -> bool:
        """Veto retiring ``bot`` for a joining human when that would decide
        the round (last survivor, last fighter...). Default: always safe."""
        return True

    def bot_retire_rank(self, bot) -> int:
        """Lower ranks are retired first among equally safe bots."""
        return 0

    async def _recover_failed_round_restart(self, winner: Optional[int] = None) -> None:
        """Never leave a sub-round intermission frozen after a failed reset.

        Falls back to the guarded full in-place restart; if even that fails,
        end the match cleanly through the ordinary end sequence.
        """
        log = logging.getLogger(__name__)
        server = self.server
        if (
            getattr(server, "_stopping", False)
            or getattr(server, "mode", None) is not self
            or self.ended
            or self.retiring
        ):
            return
        try:
            await self._restart_round()
            return
        except Exception:
            log.exception("fallback in-place restart after failed sub-round failed")
        if getattr(server, "mode", None) is not self or self.retiring:
            return
        try:
            await self.on_mode_end(winner)
        except Exception:
            log.exception("failed to end the match after a failed sub-round")

    # Threat-aware team spawns (server/spawn_selection.py) against spawn
    # camping. Modes with authored per-player spawns turn this off.
    smart_spawns = True

    def get_spawn_point(self, player: 'Player') -> Tuple[float, float, float]:
        """
        Get spawn point for a player.
        Override to customize spawn logic.
        """
        if self.smart_spawns:
            from server.spawn_selection import choose_team_spawn

            try:
                position = choose_team_spawn(self.server, player)
            except Exception:
                logging.getLogger(__name__).exception(
                    "smart spawn failed; using the plain resolver"
                )
                position = None
            if position is not None:
                return position
        return self.server.world_manager.get_spawn_point(player.team)
    
    async def broadcast_message(self, message: str):
        """Broadcast a free-form message in the retail top-screen lane."""
        from server.announcements import broadcast_overlay

        broadcast_overlay(self.server, message)

    async def broadcast_localised_message(
        self,
        string_id: str,
        parameters=(),
        *,
        localise_parameters: bool = False,
        override_previous: bool = False,
    ):
        """Broadcast a string-table template in the retail top-screen lane."""
        from server.announcements import broadcast_localised_overlay

        broadcast_localised_overlay(
            self.server,
            string_id,
            parameters,
            localise_parameters=localise_parameters,
            override_previous=override_previous,
        )

    def send_localised_message_to(
        self,
        connection,
        string_id: str,
        parameters=(),
        *,
        localise_parameters: bool = False,
        override_previous: bool = False,
    ) -> None:
        """Send one retail string-table announcement to one GameScene."""

        from server.announcements import build_localised_overlay

        connection.send(
            build_localised_overlay(
                string_id,
                parameters,
                localise_parameters=localise_parameters,
                override_previous=override_previous,
            ),
            reliable=True,
        )

    # -------------------------------------------------------------------------
    # Retail mode-start cue (TEAM_DEATHMATCH_START, TC_START, ...)
    # -------------------------------------------------------------------------

    def start_cue_for(self, player) -> Optional[str]:
        """Retail start-cue string id for ``player`` (None = no cue).

        Subclasses with a per-mode (or per-team) start line override this.
        No client binary sends these ids, so they are server-sent.
        """
        return None

    def send_start_cue_to(self, connection, player=None) -> bool:
        """Send this mode's start cue to one settled GameScene."""
        if player is None:
            player = getattr(connection, "player", None)
        string_id = self.start_cue_for(player if player is not None else connection)
        if not string_id:
            return False
        try:
            self.send_localised_message_to(
                connection, string_id, override_previous=True
            )
        except Exception:  # noqa: BLE001 - the cue is cosmetic
            logging.getLogger(__name__).debug("%s skipped", string_id)
            return False
        return True

    def broadcast_start_cue(self) -> None:
        """Round start: send the start cue to every in-game player.

        Late joiners get it from ``reveal_to``; loading peers (in_game
        false) are skipped here and reach it through that path instead.
        """
        for player in list(getattr(self.server, "players", {}).values()):
            connection = getattr(player, "connection", None)
            if connection is None or not getattr(connection, "in_game", True):
                continue
            if bool(getattr(player, "is_bot", False)):
                continue
            self.send_start_cue_to(connection, player)

    def announce_localised(
        self,
        string_id: str,
        parameters=(),
        *,
        localise_parameters: bool = False,
        override_previous: bool = False,
    ) -> None:
        """Synchronous retail string-table announcement to every GameScene."""
        from server.announcements import broadcast_localised_overlay

        try:
            broadcast_localised_overlay(
                self.server,
                string_id,
                parameters,
                localise_parameters=localise_parameters,
                override_previous=override_previous,
            )
        except ValueError:
            # An unnamed test double or oversized value must never take
            # the gameplay event down; the cue is cosmetic.
            logging.getLogger(__name__).debug("announcement %s skipped", string_id)

    def announce_localised_to_team(
        self,
        team: int,
        string_id: str,
        parameters=(),
        *,
        exclude=None,
        localise_parameters: bool = False,
        override_previous: bool = False,
    ) -> None:
        """Send one retail string-table announcement to one team's GameScenes.

        Retail objective cues are team-relative ("{0} has your intel!" vs
        "{0} has the enemy intel!"), so most mode events send two of these.
        ``exclude`` skips one player (typically the actor). Only settled
        connections receive gameplay packets, like ``server.broadcast``.
        """
        from server.announcements import build_localised_overlay

        try:
            data = build_localised_overlay(
                string_id,
                parameters,
                localise_parameters=localise_parameters,
                override_previous=override_previous,
            )
        except ValueError:
            logging.getLogger(__name__).debug("announcement %s skipped", string_id)
            return
        team = int(team)
        exclude_id = int(getattr(exclude, "id", -1)) if exclude is not None else -1
        for player in list(getattr(self.server, "players", {}).values()):
            if int(getattr(player, "team", -1)) != team:
                continue
            if exclude_id >= 0 and int(getattr(player, "id", -2)) == exclude_id:
                continue
            connection = getattr(player, "connection", None)
            if connection is None or not getattr(connection, "in_game", True):
                continue
            connection.send(data, reliable=True)

    def announce_localised_to_player(
        self,
        player,
        string_id: str,
        parameters=(),
        *,
        localise_parameters: bool = False,
        override_previous: bool = False,
    ) -> None:
        """Send one retail string-table announcement to one player."""
        connection = getattr(player, "connection", None)
        if connection is None or not getattr(connection, "in_game", True):
            return
        try:
            self.send_localised_message_to(
                connection,
                string_id,
                parameters,
                localise_parameters=localise_parameters,
                override_previous=override_previous,
            )
        except ValueError:
            logging.getLogger(__name__).debug("announcement %s skipped", string_id)

    # Retail clock cues. COUNTDOWN_MINUTES, ONE_MINUTE_LEFT and
    # COUNTDOWN_SECONDS exist only in the client's string table (no client
    # binary references them), so the original server sent them as
    # LocalisedMessage(50). The exact retail schedule is unrecorded; each
    # cue fires once, only inside a five-second window, so a stalled tick
    # or a short round never replays stale cues (the ChatMessage-flood
    # lesson from the time-limit re-fire bug).
    COUNTDOWN_CUES = (
        (300.0, "COUNTDOWN_MINUTES", ("5",)),
        (120.0, "COUNTDOWN_MINUTES", ("2",)),
        (60.0, "ONE_MINUTE_LEFT", ()),
        (30.0, "COUNTDOWN_SECONDS", ("30",)),
        (10.0, "COUNTDOWN_SECONDS", ("10",)),
    )
    # Retail COUNTDOWN_FROM_TEN "{0}!" (EN:116, in the server-sent block with
    # COUNTDOWN_SECONDS/_MINUTES; no client binary references it): the final
    # count after the 10 s cue.  One cue per second, 9..1 (schedule inferred
    # from the id); a cue whose second already passed stays quiet.
    FINAL_COUNTDOWN_FROM = 9

    def _announce_countdown(self, remaining: float) -> None:
        if remaining > self._countdown_last_remaining + 1.0:
            # The clock restarted (Zombie outbreak, round restart): re-arm.
            self._countdown_fired.clear()
        self._countdown_last_remaining = remaining
        for threshold, string_id, parameters in self.COUNTDOWN_CUES:
            if threshold in self._countdown_fired or remaining > threshold:
                continue
            self._countdown_fired.add(threshold)
            if remaining <= threshold - 5.0:
                continue
            self.announce_localised(string_id, parameters, override_previous=True)
        for second in range(int(self.FINAL_COUNTDOWN_FROM), 0, -1):
            threshold = float(second)
            if threshold in self._countdown_fired or remaining > threshold:
                continue
            self._countdown_fired.add(threshold)
            if remaining <= threshold - 1.0:
                continue
            self.announce_localised(
                "COUNTDOWN_FROM_TEN", (str(second),), override_previous=True
            )

    async def check_win_condition(self) -> Optional[int]:
        """
        Check if a team has won.
        Returns winning team ID or None.
        """
        if self.score_limit <= 0:
            return None  # 0 = no score limit (UGC, time-only rounds)
        for team_id, team in self.server.teams.items():
            if team.score >= self.score_limit:
                return team_id
        return None
    
    async def _end_by_score(self, winner: int):
        """End game due to score limit reached (fires exactly once)."""
        if self.ended or self.retiring:
            return
        team = self.server.teams[winner]
        await self.broadcast_localised_message(
            "TEAM_DEFEAT", (team.name,), localise_parameters=True
        )
        await self.on_mode_end(winner)

    async def _end_by_time(self):
        """End game due to time limit (fires exactly once).

        Without the `ended` guard this re-fires EVERY TICK once the timer
        expires, flooding ChatMessage on the single reliable ENet channel
        and starving every other gameplay packet (blocks, kills, entities,
        score) — which reads in-game as "nothing works".
        """
        if self.ended or self.retiring:
            return
        # Determine winner by score
        scores = [(t.id, t.score) for t in self.server.teams.values()]
        scores.sort(key=lambda x: x[1], reverse=True)

        # Any team count: a sole team wins, no teams or a tie at the top
        # is a draw (never index a second team that may not exist).
        if scores and (len(scores) == 1 or scores[0][1] > scores[1][1]):
            winner = scores[0][0]
            team = self.server.teams[winner]
            await self.broadcast_localised_message(
                "TEAM_DEFEAT", (team.name,), localise_parameters=True
            )
        else:
            await self.broadcast_localised_message("GAME_DRAWN")
            winner = None

        await self.on_mode_end(winner)

    # =========================================================================
    # End-of-round sequence  (win message already sent by the caller)
    # =========================================================================

    # Compatibility default for old config objects used by plugins/tests.
    # Production reads lobby.end_screen_seconds through ServerConfig.
    SCORES_SCREEN_SECONDS = 12.0

    async def _run_end_sequence(self, winner: Optional[int]):
        """Victory audio → (5s) stats/credits screen → (hold) → restart.

        Runs once per end. The whole thing is fire-and-forget on the event
        loop so the caller (a mode hook on the game thread) isn't blocked."""
        if self._end_sequence_running or getattr(self.server, "_stopping", False):
            return
        self._end_sequence_running = True
        import asyncio
        # HOLD A REFERENCE: asyncio keeps only a weak ref to a bare task, so a
        # GC during the ~17s of awaits below can collect it mid-flight and the
        # round then never restarts.
        self._end_task = asyncio.ensure_future(self._end_sequence_task(winner))

    def end_message_id(self, winner: Optional[int]) -> int:
        """Retail ShowTextMessage(73) id for this round's result.

        The scoreboard headline strings live in the client's HUD; the nine
        ids select one. Score modes use TEAM_SCORES_MESSAGE ("{0} wins!")
        or TEAM_SCORES_DRAW; Zombie, VIP, Occupation and Demolition
        override with their dedicated ids.

        TEAM_SCORES_MESSAGE makes the client name the team with the higher
        score (ViewGameStats.set_message). When the server's winner is not
        that team (an admin or plugin forced end), the explicit
        VIP_TEAM1/2_WIN_MESSAGE ids, which read the same "{0} wins!", name
        the real winner instead of the score leader.
        """
        import shared.constants as C
        from server.game_constants import TEAM1, TEAM2
        if winner is None:
            return int(C.TEAM_SCORES_DRAW)
        teams = getattr(getattr(self, "server", None), "teams", None) or {}
        try:
            score1 = int(getattr(teams.get(TEAM1), "score", 0) or 0)
            score2 = int(getattr(teams.get(TEAM2), "score", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return int(C.TEAM_SCORES_MESSAGE)
        leader = TEAM1 if score1 > score2 else TEAM2 if score2 > score1 else None
        if leader is not None and int(winner) == leader:
            return int(C.TEAM_SCORES_MESSAGE)
        if int(winner) == TEAM1:
            return int(C.VIP_TEAM1_WIN_MESSAGE)
        if int(winner) == TEAM2:
            return int(C.VIP_TEAM2_WIN_MESSAGE)
        return int(C.TEAM_SCORES_MESSAGE)

    def _end_screen_seconds(self) -> float:
        return min(
            120.0,
            max(
                0.0,
                float(
                    getattr(
                        getattr(self.server, "config", None),
                        "end_screen_seconds",
                        self.SCORES_SCREEN_SECONDS,
                    )
                ),
            ),
        )

    def _end_round_scoreboard_enabled(self) -> bool:
        return bool(getattr(getattr(self.server, "config", None),
                            "end_round_scoreboard", True))

    def _end_round_headline_enabled(self) -> bool:
        return bool(getattr(getattr(self.server, "config", None),
                            "end_round_headline", True))

    async def _end_sequence_task(self, winner: Optional[int]):
        import asyncio
        from server.audio import play_ending_music, TIME_AFTER_WIN_BEFORE_SCORES
        from server.scoreboard import broadcast_game_stats
        try:
            # 1) Victory sting immediately on the win, then the retail
            # forced scoreboard (ForceShowScores 72) for the dwell. Live
            # 2026-09-24: the client shows its scores scene, keeps the
            # connection and the GameScene, and returns on forced=0.
            play_ending_music(self.server)
            if self._end_round_scoreboard_enabled():
                from server.scoreboard import force_show_scores

                force_show_scores(self.server, True)
            sign_off = getattr(getattr(self.server, "bots", None), "on_match_phase", None)
            if callable(sign_off):
                sign_off("end")

            # 2) Send final leaderboard data without a terminal UI trigger.
            # ShowGameStats opens a native statistics overlay which is safe
            # only when a full map rollover will follow; this packet alone is
            # intentionally safe for a same-GameScene restart.
            await asyncio.sleep(TIME_AFTER_WIN_BEFORE_SCORES)
            broadcast_game_stats(self.server, winner)

            # 3) A score-limit win can occur before the final-minute ballot.
            # Never consume an unresolved vote: wait for all eligible players
            # or its bounded 15-second deadline before choosing the scene path.
            vote_manager = getattr(self.server, "vote_manager", None)
            wait_for_map = getattr(vote_manager, "wait_for_map_result", None)
            if callable(wait_for_map):
                await wait_for_map()
            consume_map = getattr(vote_manager, "consume_next_map", None)
            next_map = consume_map() if callable(consume_map) else None
            current_map = str(
                getattr(getattr(self.server, "config", None), "default_map", "")
            )
            if (
                next_map
                and current_map
                and str(next_map).casefold() == current_map.casefold()
            ):
                # ShowGameStats is not reversible enough for a same-GameScene
                # restart. A forged/synthetic same-map winner therefore stays
                # on the safe in-place path.
                next_map = None

            end_screen_seconds = self._end_screen_seconds()

            # 4) A full map rollover may use the native scores overlay. The
            # transition service preflights the target first, holds the overlay
            # for the configured dwell, then emits MapEnded(52). Same-map
            # restarts deliberately keep the active GameScene and omit packet
            # 53 because its statistics menu is not an in-place reset signal.
            transition = getattr(self.server, "match_transition", None)
            if transition is None:
                await asyncio.sleep(end_screen_seconds)
                await self._restart_round()
            elif next_map:
                change_after_scores = getattr(
                    transition,
                    "change_map_after_end_screen",
                    None,
                )
                if callable(change_after_scores):
                    # The ShowTextMessage(73) headline only renders inside
                    # the ViewGameStats screen, so the transition sends it
                    # right after ShowGameStats(53).
                    result = await change_after_scores(
                        next_map,
                        end_screen_seconds=end_screen_seconds,
                        headline_message_id=(
                            self.end_message_id(winner)
                            if self._end_round_headline_enabled() else None
                        ),
                    )
                else:
                    # Compatibility for a legacy transition façade. It cannot
                    # safely emit packet 53 because it has no preflight hook.
                    await asyncio.sleep(end_screen_seconds)
                    result = await transition.change_map(next_map)
                if not result.ok and not getattr(
                    result,
                    "reconnect_required",
                    False,
                ):
                    import logging
                    logging.getLogger(__name__).warning(
                        "voted map %s failed preflight; restarting current map: %s",
                        next_map,
                        result.message,
                    )
                    result = await transition.restart_round()
                if not result.ok:
                    raise RuntimeError(result.message)
            else:
                await asyncio.sleep(end_screen_seconds)
                result = await transition.restart_round()
                if not result.ok:
                    raise RuntimeError(result.message)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("end sequence failed")
            self._end_sequence_running = False
            await self._recover_failed_end_sequence()

    async def _recover_failed_end_sequence(self) -> None:
        """Never leave a finished round stuck behind ForceShowScores(1).

        A failed restart/rollover used to only clear the running flag: the
        mode stayed ``ended`` (no clock, no win checks) and every client kept
        the forced scoreboard forever. Fall back to the in-place restart
        unless the mode was replaced or another transition owns the epoch
        (that transition then restarts or replaces this mode itself).
        """
        import logging

        log = logging.getLogger(__name__)
        server = self.server
        if (
            getattr(server, "_stopping", False)
            or getattr(server, "mode", None) is not self
            or not self.ended
        ):
            return
        transition = getattr(server, "match_transition", None)
        busy = getattr(transition, "_transition_busy", None)
        if callable(busy) and busy(allow_current_request=True):
            return
        try:
            await self._restart_round()
            return
        except Exception:
            log.exception("fallback in-place restart failed")
        # Last resort: at least hand the clients their GameScene back.
        if self._end_round_scoreboard_enabled():
            try:
                from server.scoreboard import force_show_scores

                force_show_scores(server, False)
            except Exception:
                log.debug("failed to release forced scoreboard", exc_info=True)

    async def _restart_round(self):
        """Reset scores + respawn everyone + revive the mode for a new round.
        Mirrors the reference server's reset(): stop → start → respawn all →
        reset teams (aosmodes/__init__.py reset())."""
        # GameScene remains alive: release the forced end-of-round scoreboard
        # before the new round's packets arrive.
        if self._end_round_scoreboard_enabled():
            from server.scoreboard import force_show_scores

            force_show_scores(self.server, False)
        # Remove its old transient entities before the
        # mode re-creates crates/objectives and reuses registry ids.
        bots = getattr(self.server, "bots", None)
        prepare_bots = getattr(bots, "prepare_for_game_transition", None)
        if callable(prepare_bots):
            await prepare_bots()
        reset_runtime = getattr(self.server, "reset_round_runtime", None)
        if reset_runtime is not None:
            reset_runtime()

        # Reset team scores and re-broadcast the zeroed bars.
        from server.scoreboard import send_team_score
        for team in self.server.teams.values():
            team.reset()
        # on_mode_start clears ended/winner/timeout flags and restarts music.
        await self.on_mode_start()
        for team in self.server.teams.values():
            send_team_score(self.server, team)
        # Respawn every connected player through the ordinary CreatePlayer and
        # restock path while the client is still in its original GameScene.
        # Spectators keep their native camera: respawning one would publish a
        # visible team-0 body.
        from server.game_constants import TEAM1, TEAM2

        for player in list(self.server.players.values()):
            try:
                if int(getattr(player, "team", -1)) not in (TEAM1, TEAM2):
                    continue
                if getattr(player, "connection", None) is not None:
                    self.server.respawn_player(player)
            except Exception:
                import logging
                logging.getLogger(__name__).debug(
                    "restart respawn failed for %s", getattr(player, "id", "?"),
                    exc_info=True)

        # The mode and VXL object identities survive this in-place restart, so
        # BotDirector cannot discover the new game through its ordinary map /
        # mode signature check. Reset after every new body exists: no path,
        # stuck timer, squad assignment, held action, or worker result from the
        # completed game is allowed to drive the next one.
        bots = getattr(self.server, "bots", None)
        reset_bots = getattr(bots, "reset_after_round_restart", None)
        if callable(reset_bots):
            result = reset_bots()
            if result is not None:
                await result
