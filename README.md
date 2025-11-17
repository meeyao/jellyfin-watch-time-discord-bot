# WatchtimeBot

A lightweight Discord bot that reads Jellyfin's Playback Reporting plugin database (`playback_reporting.db`) to surface per-user stats such as total watchtime, the most recent play, and quick history lists. Playback stats come directly from the SQLite file, while the optional self-serve account linking flow uses Jellyfin's public REST API.

## Features
- `!watchtime [days|all]` – Sum of `PlayDuration` for the mapped Jellyfin user. Defaults to the window configured in `default_watch_window_days`.
- `!lastwatched` – Shows the latest entry with timestamp, item type, and playback client.
- `!recentplays [count]` – Lists up to 10 of the most recent plays (default 5).
- `!link <username>` / `!unlink` – Users can self-serve by telling the bot their Jellyfin username; it fuzzy-matches against your server’s user list and stores the mapping.
- `!forcelink` / `!forceunlink` – Admin overrides for situations where you need to wire up a user manually.

## Prerequisites
- Python 3.10+ with `pip`
- Access to the Jellyfin `playback_reporting.db` file (read-only is sufficient)
- A Discord bot token

## Installation
```bash
cd watchtimebot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration
1. Copy the sample config and populate the values:
   ```bash
   cp watchtimebot.yaml.example watchtimebot.yaml
   ```
2. Set your Discord bot token, preferred prefix, and point `jellyfin.playback_db` at the absolute path of `playback_reporting.db`. Using `~/...` or relative paths is supported.
3. If you want users to self-link, set `jellyfin.server_url` (e.g. `http://jellyfin:8096` inside Docker) **and** generate a Jellyfin API key for the bot (`jellyfin.api_key`). The bot uses it to list available usernames. Keys are created under Jellyfin **Dashboard → API Keys**.
4. `linking.database` controls where dynamic mappings are stored (defaults to `watchtime_links.db` next to your config). The path is created automatically if it does not exist.
5. Mapping Discord user IDs manually in the `users` section is still supported. You can look up Jellyfin IDs in Jellyfin (`Dashboard → Users → select user → Copy User Id`), or by querying the plugin DB:
   ```bash
   sqlite3 /path/to/playback_reporting.db 'SELECT DISTINCT UserId FROM PlaybackActivity;'
   ```

Environment variable `WATCHTIMEBOT_CONFIG` can override the config location (defaults to `watchtimebot.yaml` next to `bot.py`).

## Running the bot
```bash
source .venv/bin/activate
python -m watchtimebot.bot
```

The bot logs in under the configured token. Ensure it has the `MESSAGE CONTENT INTENT` enabled in the Discord developer portal if you plan to keep the default prefix commands.

### Linking workflow for end users
1. From any server channel they run `!link <username>` (or DM the bot). The bot normalizes the string and looks for a matching Jellyfin account via the API key you provided.
2. If the name is unique, the link is stored instantly and the user can call `!watchtime`.
3. If multiple users share the same name, or there’s no exact match, the bot suggests the closest matches so the user can retry with the precise spelling.
4. Users can remove the association by running `!unlink`. If the bot can’t list users (missing API key), it will instruct them to ask an admin to use `!forcelink`.

### Admin overrides
- `!forcelink @Member JellyfinUserId [Display Name]` – requires `Manage Server`. This immediately creates/updates the mapping.
- `!forceunlink @Member` – removes the stored mapping.

Both commands DM the affected member (best-effort) so they know what changed.

## Systemd / Docker notes
- Mount the `playback_reporting.db` file as read-only into the container/host where this bot runs. The bot never writes to the database but requires read permissions.
- Restart the bot whenever you rotate the playback database or config file.

## Extending
`watchtimebot/jellyfin_reporting.py` centralizes all SQL access. You can safely add new commands by querying additional aggregates (top shows, device breakdowns, etc.) using the same helper.

## Docker / docker-compose
1. Copy your config to `watchtimebot/config/watchtimebot.yaml` (ignored by git) and set `jellyfin.playback_db` to the in-container mount, e.g. `/data/playback_reporting.db`.
2. The supplied `Dockerfile` builds a minimal image:
   ```bash
   docker build -t watchtimebot ./watchtimebot
   docker run --rm \
     -e WATCHTIMEBOT_CONFIG=/config/watchtimebot.yaml \
     -v $(pwd)/watchtimebot/config:/config:ro \
     -v $(pwd)/jellyfin/config/data/playback_reporting.db:/data/playback_reporting.db:ro \
     -v $(pwd)/watchtimebot/data:/state \
     watchtimebot
   ```
3. A ready-to-edit `docker-compose.example.yaml` lives at the repo root. Copy it into place and set the absolute path to your playback DB:
   ```bash
   cp docker-compose.example.yaml docker-compose.yaml
   # Edit docker-compose.yaml + config/watchtimebot.yaml before running
   docker compose up -d --build
   ```

### docker-compose example
```yaml
services:
  watchtimebot:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: watchtimebot
    environment:
      - WATCHTIMEBOT_CONFIG=/config/watchtimebot.yaml
    env_file:
      - .env
    volumes:
      - ./config/watchtimebot.yaml:/config/watchtimebot.yaml:ro
      - ./data:/state
      - /absolute/path/to/playback_reporting.db:/data/playback_reporting.db:ro
    restart: unless-stopped
```

`env_file` lets you keep Discord/Jellyfin secrets in `.env` (already ignored). The `/state` mount is where the bot writes the dynamic link database so you can persist mappings across restarts.


