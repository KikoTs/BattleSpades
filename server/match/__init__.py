"""server.match — match lifecycle placeholder.

Owns the warmup → countdown → match → end → rotation loop. Today this
isn't really wired: matches start with the server, end never (no
MapEnded(52) emission), rotation is driven by /map admin command only.

GOAL.md Phase 5 fills this in. The intended shape:

    MatchController:
        - warmup(): no scoring, free join
        - start(): countdown via DisplayCountdown(84)
        - tick(): per-frame, asks the active mode whether the win condition
          is met
        - end(winning_team): broadcast MapEnded(52), GameStats(67),
          ShowGameStats(53), ForceShowScores(72)
        - rotate(): pick next map+mode from playlist, restart full server

    Playlist parser:
        - parses ../aceofspades_nonsteam/playlists/*.txt format
        - exposes (mode_code, map_name, max_players, custom_rules) per
          rotation entry

    Score / rank:
        - emit RankUps(66) at end of map for XP changes
"""
