# Master server, self-hosting, and offline play

BattleSpades can run without a master. The game server handles simulation and
ENet/A2S UDP traffic; the optional master provides the public browser, accounts,
join tickets, Workshop, progression, and lobby coordination. Disable online
identity requirements for local/offline players. A local nickname does not prove
ownership of an online account and cannot earn trusted online progression.

The original master/website and relay source is published as
[aos_revival-master](https://github.com/KikoTs/aos_revival-master), under MIT for
project code. The clean source snapshot excludes credentials, databases, private
history, and third-party game assets.

- [Self-hosting guide](https://github.com/KikoTs/aos_revival-master/blob/main/docs/SELF_HOSTING.md): local master, PostgreSQL, administrator setup, HTTPS/proxy boundary, and hosting topology.
- [Master API](https://github.com/KikoTs/aos_revival-master/blob/main/docs/MASTER_SERVER_API.md): server registration, heartbeats, profiles, and join tickets.
- [Relay deployment](https://github.com/KikoTs/aos_revival-master/blob/main/relay/README.md) and [protocol](https://github.com/KikoTs/aos_revival-master/blob/main/docs/LOBBY_RELAY_PROTOCOL.md): the standalone Node/Windows UDP worker.
- [Source scope and security review](https://github.com/KikoTs/aos_revival-master/blob/main/docs/SOURCE_DISTRIBUTION.md): included source, excluded assets, security changes, and review limits.
- [Offline and launch options](OFFLINE_AND_LAUNCH_OPTIONS.md): local profiles, direct joins, custom master selection, and URL handlers for compatible client releases.

The project's checked-in service configuration uses Vercel for the website/API,
Railway PostgreSQL for durable data, Vercel Blob for Workshop uploads, and GitHub
Releases for game distributions. The UDP relay is a separate long-lived service;
its current machine provider is not established by the public configuration.
The self-hosting guide distinguishes those facts from deployment examples.

Use `BattleSpades.exe --offline` (or `python run_server.py --offline`) to disable
master registration, account requests, Steam hosting and update checks together.
This runtime mode also prevents a presented online join ticket or inherited
Steam environment setting from contacting an external service. Individual
`[revival].enabled` and `require_identity` settings are useful for unlisted
servers, but do not provide that complete offline guarantee. Select another
master with `--master-url https://master.example.org`, `[revival].base_url`, or
the `AOS_MASTER_URL` environment override. The explicit launch option wins.
Use HTTPS, or loopback HTTP for local development.
See [ADMIN_GUIDE.md](ADMIN_GUIDE.md) for all server configuration settings.
