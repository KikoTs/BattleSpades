# BattleSpades - Ace of Spades Server
# Protocol 1.0 Battle Builders

A high-performance Python + Cython server for Ace of Spades using the Battle Builders protocol.

## Features

- **ENet Networking** - Reliable UDP networking with pyenet
- **Asyncio** - Non-blocking async I/O for high concurrency
- **Cython Core** - Performance-critical physics and map operations in Cython
- **Modular Design** - Easy to extend with game modes, commands, and plugins

## Project Structure

```
BattleSpades/
├── aoslib/          # Cython core library (VXL, physics, math)
├── server/          # Main server logic
├── protocol/        # Packet definitions and handlers
├── modes/           # Game modes (CTF, TDM, Arena)
├── commands/        # Player and admin commands
├── plugins/         # Optional plugin system
├── maps/            # VXL map files
├── scripts/         # Build and utility scripts
└── tests/           # Unit tests
```

## Requirements

- Python 3.8+
- Cython
- pyenet
- numpy
- toml

## Installation

1. Clone the repository:
   ```bash
   git clone <repository>
   cd BattleSpades
   ```

2. Install dependencies:
   ```bash
   python3.10 -m venv venv
   .\venv\Scripts\activate.ps1  # Windows
   source venv/bin/activate   # Linux/MacOS
   pip install -r requirements.txt
   ```

3. Build Cython extensions:
   ```bash
   python scripts/build.py
   # or
   python setup.py build_ext --inplace
   ```

4. Run the server:
   ```bash
   python run_server.py
   ```

## Configuration

Edit `config.toml` to configure:

- Server name, port, max players
- Default game mode and map
- Team names and colors
- Weapon damage values
- Admin password

## Commands

### Player Commands
- `/help` - Show available commands
- `/kill` - Kill yourself
- `/team <blue|green>` - Change team
- `/score` - Show team scores
- `/players` - List players
- `/pm <player> <msg>` - Private message

### Admin Commands
- `/admin <password>` - Login as admin
- `/kick <player> [reason]` - Kick player
- `/ban <player> [reason]` - Ban player
- `/mute <player>` - Mute player
- `/map <name>` - Change map
- `/mode <ctf|tdm|arena>` - Change game mode
- `/tp <player>` - Teleport

## Game Modes

- **CTF** - Capture the Flag
- **TDM** - Team Deathmatch
- **Arena** - Round-based elimination

## Development

Run tests:
```bash
pytest tests/ -v
```

## License

MIT License
