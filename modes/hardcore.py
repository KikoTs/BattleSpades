"""First-party Hardcore variants of the standard objective modes."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

import shared.constants as C
from server import hardcore as HC
from server.class_selection import normalize_server_selection
from server.lobby import LOBBY_MODES
from server.game_constants import TEAM1, TEAM2

if TYPE_CHECKING:
    from modes.base_mode import BaseMode
    from server.class_selection import ClassSelection
    from server.player import Player
    from shared.packet import InitialInfo, StateData


class HardcoreMixin:
    hardcore = True
    death_representation = "classic_corpse"
    survivor_class_order = HC.CLASS_IDS
    survivor_classes = frozenset(HC.CLASS_IDS)

    def normalize_survivor_selection(
        self, class_id: int, loadout: Iterable[int] = (),
        prefabs: Iterable[str] = (), ugc_tools: Iterable[int] = (),
    ) -> ClassSelection:
        return normalize_server_selection(self.server.config, class_id, loadout)

    def prepare_join_selection(self, team: int, selection: ClassSelection) -> ClassSelection:
        if self.mode_code == "zom":
            return super().prepare_join_selection(team, selection)
        if selection.class_id not in HC.BY_CLASS:
            return HC.normalize_selection(HC.CLASS_IDS[0])
        return HC.normalize_selection(selection.class_id, selection.loadout)

    def allows_class_selection(self, player: Player, selection: ClassSelection) -> bool:
        if self.mode_code == "zom":
            return super().allows_class_selection(player, selection)
        if self.mode_code == "vip" and self.vips.get(player.team) is player:
            return False
        return int(player.team) in (TEAM1, TEAM2) and selection.class_id in HC.BY_CLASS

    def configure_state_data(self, packet: StateData) -> None:
        packet.gravity = 1.0
        configure = getattr(super(), "configure_state_data", None)
        if callable(configure):
            configure(packet)
        if self.mode_code == "zom":
            packet.team2_classes = list(HC.CLASS_IDS)
        else:
            packet.team1_classes = list(HC.CLASS_IDS)
            packet.team2_classes = list(HC.CLASS_IDS)
            packet.team1_locked_class = False
            packet.team2_locked_class = False

    def configure_initial_info(self, packet: InitialInfo) -> None:
        # Classic's objective rules still apply; its one-class menu and
        # classic-only weapon filtering do not belong in this variant.
        if self.mode_code != "cctf":
            configure = getattr(super(), "configure_initial_info", None)
            if callable(configure):
                configure(packet)
        else:
            packet.allow_shooting_holding_intel = int(self.shoot_with_intel)
        packet.classic = 0
        packet.texture_skin = None
        packet.loadout_overrides = HC.loadout_overrides()
        packet.custom_game_rules.append(("BATTLESPADES_HARDCORE", "1"))
        packet.custom_game_rules.append(("RULE_POINTS_FROM_TEABAGGING", "ON"))

    async def on_player_ready(self, player: Player) -> None:
        self._grant_spawn_supplies(player)
        await self.show_rules(player)

    async def show_rules(self, player: Player) -> None:
        if getattr(player, "is_bot", False):
            return
        from commands.command_handler import send_message
        role = HC.BY_CLASS.get(int(player.class_id))
        lines = [
            self.name + ": " + self.description,
            "Classes: Deuce=Soldier, Commando=Heavy, Marksman=Recon, Miner=Demo, Medic=Medic.",
            "Choose frag or fire grenades; everyone also carries a shovel and parachute.",
            "Heavy: body armour. Medic: heal after 3s safe. Soldier: extra spawn ammunition.",
            "Movement: one-block automatic steps, two-block jump ledges; no super jumps.",
            "Teabag an enemy body: 3 crouches within 0.5s each = 2 points, once per death.",
            "No gravestones or corpse explosions. /hcrules repeats this help.",
        ]
        if role:
            lines.append("Your role: " + role.title + ". Equipment choices apply on your next life.")
        import textwrap
        for line in lines:
            for part in textwrap.wrap(line, width=88):
                await send_message(self.server, player, part)

    async def on_player_spawn(self, player: Player) -> None:
        await super().on_player_spawn(player)
        if getattr(getattr(player, "connection", None), "in_game", False):
            self._grant_spawn_supplies(player)

    def _grant_spawn_supplies(self, player: Player) -> None:
        # A real retail Restock(AMMO_CRATE) updates both wallets and HUD.
        life = int(getattr(player, "replication_generation", 0))
        if (player.alive and int(player.class_id) == int(C.CLASS_CLASSIC_SOLDIER)
                and getattr(player, "_hardcore_supplied_life", None) != life):
            player.restock_ammo(int(C.AMMO_CRATE))
            player._hardcore_supplied_life = life

    def _ordinary_selection(self, player: Player) -> ClassSelection:
        return HC.normalize_selection(player.class_id, getattr(player, "loadout", ()))

    def _promote_vip(self, team: int, vip: Player) -> None:
        """Crown the current Hardcore body without replacing its class."""
        if not vip.alive:
            vip.apply_class_selection(self._ordinary_selection(vip))
            self.server.respawn_player(vip)
        self._set_vip_marker(vip, True)
        self.announce_localised_to_team(team, "VIP_NAME_IS_VIP", (str(vip.name),), exclude=vip)
        connection = getattr(vip, "connection", None)
        if connection is not None:
            from server.handlers.team import _tell_player
            _tell_player(vip, "You are the VIP. Stay alive: your death stops your team's respawns.")


def register(registry: dict[str, type[BaseMode]]) -> None:
    for code in HC.BASE_MODES:
        original = registry[code]
        cls = type("Hardcore" + original.__name__, (HardcoreMixin, original), {
            "__module__": __name__,
            "name": "Hardcore " + LOBBY_MODES[code].title,
            "mode_code": code,
        })
        registry["hc_" + code] = cls
        registry["hardcore_" + code] = cls
