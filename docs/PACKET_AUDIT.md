# Unused Packet Audit

This is the evidence-backed review of packet definitions which are not part of
BattleSpades' normal runtime path. It complements the master table in
`PROTOCOL.md`; it is not permission to register every packet as client input.

## Audit method

Each packet was classified on four independent axes before considering an
implementation:

1. **Direction** — client-to-server, server-to-client, handshake-only, or
   bidirectional.
2. **Phase** — handshake, map transfer, GameScene, round transition, or editor.
3. **Authority** — request, authoritative state, presentation-only, or local
   client state.
4. **Framing and bounds** — exact field order, signedness, fixed-point format,
   count limits, and legal lifecycle teardown.

Evidence came from `shared/packet.pyx`, the clean retail Python 2
`shared/packet.pyd`, native `gameScene.pyd` receiver decompilation, recovered
server mode code, and the current handler/sender call sites. Golden vectors in
`tests/test_recovered_objective_packets.py` are produced from the clean retail
module rather than this repository's own reader.

## Findings implemented or corrected

| ID | Packet | Finding |
|----|--------|---------|
| 25 | StopSound | Native `process_packet_stop_sound` (`gameScene.pyd:0x1019CCD0`) resolves `loop_id` through the media manager and catches the missing-id path. BattleSpades now exposes validated global and per-player teardown helpers. |
| 44 | MinimapZoneClear | Already active in objective-mode lifecycle cleanup. Its six shorts are the exact packet-43 zone identity; the old catalog entry was stale. |
| 106 | TerritoryBaseState | Already active in Territory Control for join replay and owner/attacker/capture updates. Retail bytes match. |
| 108 | LockToZone | Already active during Demolition's build phase. Retail bytes match. |
| 109 | HelpMessage | Already active in Tutorial. The delay is the protocol's unusual big-endian float, followed by bounded null-terminated localization ids. Retail bytes match. |
| 117 | TeamProgress | Already active for Demolition base health. Both its flag byte and fixed16-percent variant match retail. |

All six are **server-to-client only**. Adding receive handlers for them would
turn presentation or rule state into an untrusted-client authority path.

## Reversed but blocked

### ProgressBar (65)

The 1.x wire packet encodes `progress` and `rate` as signed fixed16 values. The
native receiver (`gameScene.pyd:0x1019E3A0`) still contains a legacy
`is_stopped()` branch which hides the HUD when progress is NaN. That sentinel
belonged to the older float32 packet:
the clean 1.x writer's `stopped` setter does nothing, and attempting to encode
NaN cannot produce a valid fixed16 packet.

Sending an active bar is easy, but there is no evidence-backed way to dismiss
it. BattleSpades therefore does not emit packet 65. Territory Control and
Occupation must keep their current marker/score feedback until a live-compatible
hide transition is recovered.

## Deliberately unused or unsafe

| IDs | Reason |
|-----|--------|
| 3, 96 | Entity delta/disable paths have no recovered lifecycle that is safer than the active Create/Change/Destroy entity path. Packet 3 is also rejected at the final outbound boundary: a truncated five-byte instance makes the retail reader consume a missing short count and crash with `NoDataLeft`. |
| 14 | `ExistingPlayer.pickup` has no safe empty sentinel in this client. Roster replay intentionally uses CreatePlayer (28). |
| 34, 38, 39 | Block ownership/manager packets do not repair VXL topology; packet 39 is a native no-op. Authoritative block changes continue through the verified Damage/build paths. |
| 73 | Selects one of nine compiled messages; it is not free text. Packets 49/50 own broadcasts. |
| 101 | Steam-lobby host progress only. It is unnecessary and crash-prone during dedicated direct-connect map loading. |

## Valid candidates when a real feature needs them

| IDs | Conditions before implementation |
|-----|----------------------------------|
| 18 | POIFocus is presentation-only and server-to-client. Use only when a mode has an exact focus event and clear/expiry semantics. |
| 41, 42 | Billboard add/clear are a paired lifecycle keyed by entity id. Do not duplicate packet-43 CTF zones or native intel entities. |
| 72 | ForceShowScores needs an exact round-state transition and a verified release/toggle path; ShowGameStats (53) already owns the current end screen. |
| 75, 79–82 | Runtime rule mutations only. Join/spawn truth must still be reflected in StateData, and no client packet may set these values. |
| 61–63 | Resource-pack transfer requires a separate phase machine, byte/count caps, checksum validation, acknowledgement correlation, timeout, and cancellation. |
| 66 | Rank progression needs persistent authoritative progression; a display packet alone is not a progression system. |
| 103 | Voice requires bounded codec/frame validation, rate limiting, routing policy, mute/abuse controls, and no gameplay-thread decoding. |
| 107 | DebugDraw must remain authenticated development tooling and server-to-client only. |
| 111–113 | Password challenge/response requires pre-GameScene phase gating, attempt throttling, constant-time comparison, and secret-safe logging. |

The priority rule is simple: implement a packet only when its full state
transition is recovered. A known byte layout without direction, authority, and
cleanup behavior is not an implementation contract.
