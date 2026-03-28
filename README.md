# shigebot

> ⚠️ **Vibe coded** — this project was written with AI assistance and has not
> been audited. Use at your own risk in production environments.

> ⚠️ **Arbitrary code execution** — any gist added as a command runs as a
> subprocess on your machine with the same privileges as the bot process.
> Only add scripts from authors you trust.

Async Twitch chat bot that runs community-contributed Python scripts hosted on
GitHub Gists. Built on [twitchio 3](https://github.com/TwitchIO/TwitchIO)
using EventSub over WebSocket.

---

## How it works

- Commands are Python scripts hosted on GitHub Gists
- Scripts are fetched on startup and re-checked periodically via the GitHub API
- Each command invocation spawns the script as a subprocess; output goes to chat
- Scripts share per channel and global working directories for state persistence

See [COMMANDS.md](COMMANDS.md) for how to write scripts.

---

## Setup

### 1. Register a Twitch application

Go to <https://dev.twitch.tv/console/apps> → **Register Your Application**.

You can register under any Twitch account — this does not determine which
account sends chat messages. That is determined by the token in step 3.

- OAuth Redirect URL: `http://localhost:18756`
- Category: Chat Bot

Copy the **Client ID** and generate a **Client Secret**.

### 2. Get the bot account's numeric user ID

`bot_id` in `shigebot.toml` must be the numeric Twitch user ID of the bot
account. Run `shigebot-auth` in the next step — it prints this for you.

### 3. Generate tokens for the bot account

```sh
nix run .#shigebot-auth
# or in the dev shell:
shigebot-auth
```

**Log into Twitch as the bot account** before pressing Enter. The utility
opens the authorization page, waits for the redirect on `localhost:18756`,
and prints the four env var lines plus `bot_id` ready to paste.

> The bot needs a **refresh token** so twitchio can rotate the access token
> automatically. `twitch token -u` (Twitch CLI device flow) does not issue
> a refresh token — use `shigebot-auth` instead.

<details>
<summary>Manual token generation (headless server)</summary>

**Option A — run shigebot-auth on the server, authorize from another device**

1. Start `shigebot-auth` on the server — it prints the auth URL and waits.
2. Open the URL on any device, logged in as the bot account. Twitch redirects
   to `http://localhost:18756/?code=...` which will fail to load.
3. Copy the full redirect URL from the address bar, then on the server:

   ```sh
   curl "http://localhost:18756/?code=CODE&state=STATE"
   ```

   The waiting server receives the code, completes the exchange, and prints
   the tokens.

**Option B — fully manual**

```sh
# Step A: open in browser as bot account (replace CLIENT_ID)
https://id.twitch.tv/oauth2/authorize?response_type=code
  &client_id=CLIENT_ID&redirect_uri=http://localhost:18756
  &scope=user:read:chat+user:write:chat+user:bot&force_verify=true

# Copy the code= from the redirect URL, then:
curl -X POST https://id.twitch.tv/oauth2/token \
  -d "client_id=CLIENT_ID" \
  -d "client_secret=CLIENT_SECRET" \
  -d "code=CODE" \
  -d "grant_type=authorization_code" \
  -d "redirect_uri=http://localhost:18756"

# Get bot_id:
curl -H "Authorization: OAuth ACCESS_TOKEN" https://id.twitch.tv/oauth2/validate
```

</details>

### 4. Create the environment file

```sh
cat > /var/lib/secrets/shigebot-env << EOF
TWITCH_CLIENT_ID=your_client_id
TWITCH_CLIENT_SECRET=your_client_secret
TWITCH_BOT_TOKEN=your_access_token
TWITCH_BOT_REFRESH=your_refresh_token
# Optional — raises GitHub API rate limit from 60 to 5000 req/hour.
# Generate at https://github.com/settings/tokens (no scopes needed).
# GITHUB_TOKEN=your_github_token
EOF
chmod 600 /var/lib/secrets/shigebot-env
```

### 5. Configure shigebot.toml

```sh
cp shigebot.toml.example shigebot.toml
$EDITOR shigebot.toml
# Fill in nick, bot_id, channels, and scripts
```

---

## Running

```sh
# dev shell
nix develop
shigebot shigebot.toml

# with verbose logging
shigebot --debug shigebot.toml
```

---

## Configuration

See `shigebot.toml.example` for a fully annotated config. Key options:

```toml
[bot]
nick   = "shigebot"    # display/logging only
bot_id = "123456789"   # numeric Twitch user ID of the bot account

[channels]
# Explicit list:
mychannel = ["8ball", "flip"]
# All scripts:
mychannel = ["@all"]
# All scripts except some:
mychannel = ["@all", "-8ball"]
# "Ambient" script - the twitter script is called on every message, with the
# contents of the message as its arguments
mychannel = ["@all", "#twitter"]

[scripts]
# Command name → gist URL.
# Names with special characters use TOML quoted keys:
hi      = "https://gist.github.com/Francesco149/43beeeda657c2fc99cb68fa64a72cd82"
8ball   = "https://gist.github.com/Francesco149/087c9dffeaa90a03b0dff68e883de79a"
pepe    = "https://gist.github.com/Francesco149/cd81efba346932dc58efe6a50c7b752f"
"4/4"   = "https://gist.github.com/Francesco149/c12a9d0cc5b5c02b8b4b8eb47ce556f5"
simple  = "https://gist.github.com/Francesco149/91b1837fccd26b49394e9b03b0337faa"
flip    = "https://gist.github.com/Francesco149/cebae605c965260db8d9a0e3dcea60f6"
slap    = "https://gist.github.com/Francesco149/47e663b1a71ba97885be5b9de7268e04"
urban   = "https://gist.github.com/Francesco149/722223e7af346eb1e2dbc3e9f6fe1a53"
twitter = "https://gist.github.com/Francesco149/e21bd2f769cc3484d3224343e933f319"

# Scripts not in any channel list are downloaded as helper modules only:
openrouter = "https://gist.github.com/Francesco149/5a49381aa6a637ee0c3975781fd69d8c"
```

---

## NixOS integration

```nix
# flake.nix
inputs.shigebot.url = "github:you/shigebot";
inputs.shigebot.inputs.nixpkgs.follows = "nixpkgs";
```

```nix
imports = [ inputs.shigebot.nixosModules.shigebot ];

services.shigebot = {
  enable          = true;
  package         = inputs.shigebot.packages.${pkgs.system}.default;
  configFile      = ./shigebot.toml;
  environmentFile = "/var/lib/secrets/shigebot-env";
};
```

The service runs as a dedicated system user (`shigebot`) with a hardened
systemd unit: no new privileges, private /tmp, read-only system, restricted
syscalls. The only writable path is `/var/lib/shigebot` where scripts and
pickle state live.

Logs:

```sh
journalctl -u shigebot -f
```

---

## Gist update behaviour

Scripts are fetched via the **GitHub Gist API** (not the CDN raw endpoint,
which caches aggressively for several minutes). The API returns the file
content directly and includes an `updated_at` timestamp.

**Three layers of freshness:**

1. **On every command invocation** — a background refresh of that specific
   script starts in parallel with running it, adding zero latency. If the
   script was updated, the bot notifies chat and suggests re-running the
   command. Subject to a bot-internal budget (`refresh_user_limit` /
   `refresh_user_window`) shared across all channels; silently skips if
   exhausted.

2. **`!refresh` command** — force an immediate fetch. Each user has their own
   independent budget (same limits as above, tracked separately).

3. **Background poll** — every `gist_refresh_interval` seconds, all scripts
   are re-checked regardless of activity.

Add a `GITHUB_TOKEN` to raise the API rate limit from 60 to 5000 req/hour
if you have many scripts or a high command volume.

---

## Rate limiting

Twitch enforces two limits simultaneously:

**Global** (all channels combined, one shared counter):

| Status              | Limit                               |
| ------------------- | ----------------------------------- |
| Regular (default)   | 20 msg / 30 s                       |
| Mod/VIP/broadcaster | 100 msg / 30 s (fills same counter) |

**Per-channel hard cap**: 1 message per second, non-elevated channels only.

Elevation is detected from badge data on each incoming message — no
separate API calls needed.
