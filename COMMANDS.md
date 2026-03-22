# Writing scripts for shigebot

Scripts are plain Python files hosted on GitHub Gists. shigebot fetches them
automatically and runs them as subprocesses when the command is triggered.

The goal was simplicity over efficiency.

---

## How scripts receive input

Each script gets:

| Source                              | Value                                         |
| ----------------------------------- | --------------------------------------------- |
| `sys.argv[0]`                       | the script's own filename                     |
| `sys.argv[1:]`                      | user-supplied words after the command         |
| `os.environ["NICK"]`                | name of the user who ran the command          |
| `os.environ["CHANNEL"]`             | channel name (no leading `#`)                 |
| `os.environ["REPLY_TO_USER"]`       | name of the user being replied to             |
| `os.environ["REPLY_TO_MESSAGE"]`    | contents of the message being replied to      |
| `os.environ["REPLY_TO_MESSAGE_ID"]` | id of the message being replied to            |
| `os.environ["MSG_ID"]`              | uuid unique to each message                   |
| `os.environ["TIMESTAMP"]`           | timestamp in iso format `YYYY-MM-DDTHH:MM:SS` |
| `os.environ["PREFIX"]`              | `prefix` from config                          |
| `os.environ["BOT_NICK"]`            | `nick` from config                            |
| working directory                   | `working_dir / channel_name` from config      |

---

## Minimal example

```python
import os
print(f"Hello {os.environ['NICK']}!")
```

Trigger: `!hello` → `Hello headpats!`

---

## Reading arguments

```python
import os, sys

nick = os.environ["NICK"]
args = sys.argv[1:]

if args:
    target = args[0]
    print(f"{nick} slaps {target}")
else:
    print(f"{nick} slaps themselves")
```

Trigger: `!slap lolisamurai` → `headpats slaps lolisamurai`

---

## Making HTTP requests

The standard library `urllib` works out of the box. Third-party `requests`
is also available.

```python
import sys, urllib.request, urllib.parse, json

term = " ".join(sys.argv[1:]).strip() or "python"
url = "https://api.urbandictionary.com/v0/define?" + urllib.parse.urlencode({"term": term})

with urllib.request.urlopen(url, timeout=8) as resp:
    entries = json.loads(resp.read()).get("list", [])

if entries:
    print(entries[0]["definition"][:400])
else:
    print("no results")
```

---

## Persisting state between calls

Scripts share a working directory. Use pickle files for persistent state —
they survive bot restarts and are visible to all scripts in the same channel.

```python
import os, sys, pickle
from pathlib import Path

DATA = Path("points.pkl")
nick = os.environ["NICK"]

def load():
    return pickle.loads(DATA.read_bytes()) if DATA.exists() else {}

def save(d):
    DATA.write_bytes(pickle.dumps(d))

points = load()
points[nick] = points.get(nick, 0) + 1
save(points)
print(f"{nick} has {points[nick]} points")
```

---

## Importing other scripts as modules

Scripts in the same `working_dir` can be imported directly. This is how
helper modules work — list the helper in `[scripts]` without adding it
to any channel list, and it will be downloaded but not user-callable.

```toml
[scripts]
slots     = "https://gist.github.com/..."
slotshelp = "https://gist.github.com/..."  # helper, not in any channel list
```

```python
# in slots.py
import slotshelp
```

---

## Scripts that sleep (trivia, countdowns, etc.)

Output is streamed line by line as the script prints it. A script can sleep
between lines and each line will be sent to chat as it is produced:

```python
import time, sys

print("Question: what is 2 + 2?")
sys.stdout.flush()   # not required but harmless — stdout is always unbuffered
time.sleep(20)
print("Time's up! The answer was 4.")
```

The total runtime is bounded by `script_timeout` in the config (default 10s —
raise it for scripts with intentional sleeps).

---

## Available third-party packages

These are bundled with shigebot and importable from any script:

- `numpy`
- `pandas`
- `scipy`
- `requests`
- `pyowm`
- `yt_dlp`

Everything in the Python standard library is available too.

---

## Output limits

- Maximum **10 lines** per invocation
- Maximum **350 characters** per line

Lines beyond these limits are silently dropped.

---

## Built-in commands

These are handled by the bot itself and do not need gist entries.

### `!refresh [script]`

Force an immediate re-fetch of gists without waiting for the automatic poll.

```text
!refresh           — refresh all scripts, replies with which ones changed
!refresh 8ball     — refresh only 8ball
```

Available to all users. Each user has their own independent rate limit budget
(default: 10 uses per 60s, configurable with `refresh_user_limit` /
`refresh_user_window`).

**Auto-refresh on invocation:** every time a command is triggered, the bot
starts a background refresh of that script in parallel with running it. This
adds zero latency to the command. If the script was updated, the bot sends a
follow-up message suggesting to re-run the command so the caller gets the
latest version. The background refresh uses a separate bot-internal budget
(same limits as `!refresh`) so it never interferes with a user's own
`!refresh` budget, and silently skips if the budget is exhausted.

---

## Example scripts

The example `shigebot.toml.example` comes with a lot of example scripts that me
and my friends have written for our community and our own specific purposes.
A lot of these can either be repurposed or just used straight up.

### `!hi`

```toml
hi = "https://gist.github.com/Francesco149/43beeeda657c2fc99cb68fa64a72cd82"
```

Simple test "ping" command that just replies with `hi :)` followed by a random
number between 0.0-100.0 .

### `!8ball`

Originally made by [Painketsu](https://twitch.tv/Painketsu) .

```toml
8ball = "https://gist.github.com/Francesco149/087c9dffeaa90a03b0dff68e883de79a"
```

Prints a random answer like `Maybe someday.` . Ask it any question or life
advice and let it decide for you. We do not take responsibility for any terrible
life choices resulting from this script.

### `!pepe`

```toml
pepe = "https://gist.github.com/Francesco149/cd81efba346932dc58efe6a50c7b752f"
```

Prints a random pepe emote.

#### pepes

- `FeelsAmazingMan`
- `FeelsGoodMan`
- `FeelsBadMan`
- `FeelsBirthdayMan`

#### limited halloween pepes

Only available in October (UTC).

- `FeelsPumpkinMan`

#### limited christmas pepes

Only available in December (UTC).

- `FeelsSnowyMan`
- `FeelsSnowMan`

### pepe exports

```python
import pepe

SET = pepe.get_set() # current pepe set (taking holidays into account)
SET = pepe.get_set(10) # set for month 10 (october)
```

### `!4/4`

```toml
"4/4" = "https://gist.github.com/Francesco149/c12a9d0cc5b5c02b8b4b8eb47ce556f5"
```

Slots machine type thing. Attempt to guess N/N pepes by simulating running
`!pepe` and betting on pulling a specific pepe repeatedly. N is the number of
possible pepes, which varies by season as explained in [#pepes](!pepe) .

Example output:

```text
!pepe FeelsBirthdayMan
FeelsBirthdayMan
!pepe FeelsBadMan
FeelsAmazingMan
!pepe FeelsAmazingMan
FeelsGoodMan
!pepe FeelsGoodMan
FeelsBadMan
1/4
```

#### 4/4 required modules

- [#pepe](pepe)

### ambient: simple

```toml
simple = "https://gist.github.com/Francesco149/91b1837fccd26b49394e9b03b0337faa"
```

A simple ambient script that matches the start of the message with a dictionary
of simple text commands. It specifically matches messages that _start_ with the
command, rather that perfectly matching it.

Intended to be edited, default commands are:

- `!area` links a joke intentionally deep fried screenshot of hvick's tablet
  area to confuse plebs .
- `!camp` explains why people are camping in hvick's chat .

### weather

```toml
weather = "https://gist.github.com/Francesco149/97deeabf4b8af0991df890da927a972e"
```

Pulls the current weather for the specified location, using openweathermap.

- `!weather Rome`

#### weather: required secrets

- `OPENWEATHERMAP_API_KEY` make an account and create one
  [on openweathermap](https://home.openweathermap.org/api_keys) .

### flip

Originally made by [Painketsu](https://twitch.tv/Painketsu) .

```toml
flip = "https://gist.github.com/Francesco149/cebae605c965260db8d9a0e3dcea60f6"
```

Simple coin flip command, BUT it has a `1/101` probability to land on the side.

- `!flip` flip a coin.

### `!slap`

```toml
slap = "https://gist.github.com/Francesco149/47e663b1a71ba97885be5b9de7268e04"
```

Reference to the classic IRC `!slap` command.

- `!slap` _"user slaps themselves around a bit with a large trout"_
- `!slap target` _"user slaps target around a bit with a large trout"_

### `!urban`

```toml
urban = "https://gist.github.com/Francesco149/722223e7af346eb1e2dbc3e9f6fe1a53"
```

Pull definitions from [urban dictionary](https://www.urbandictionary.com/) .

> ⚠️ **Explicit language** — this command can pull definitions that are very
> explicit or politically incorrect so make sure everyone in the chat is
> comfortable with that type of humor.

- `!urban` random definition
- `!urban word` pulls the definition for "word"

### minigames

A series of minigames that all share the same virtual currency (campbucks/lts).

#### `!bank`

Originally made by [Painketsu](https://twitch.tv/Painketsu) .

```toml
bank = "https://gist.github.com/Francesco149/91869583ea4c0bd89377082a9e5d9d91"
```

- `!bank` check your campbucks balance
- `!bank claim` claim your daily campbucks check-in
- `!bank transfer user amount` send campbucks to another user
- `!bank help` explain the command
- `!bank add amount user` (admin debug command) add campbucks to a user
- `!bank rem amount user` (admin debug command) remove campbucks from a user
- `!bank toint user` (admin debug command) old data migration command

#### `!rr`

Originally made by [Painketsu](https://twitch.tv/Painketsu) .

```toml
rr      = "https://gist.github.com/Francesco149/1fc928a677ecd355a60f4cc25d9c9740"
```

Daily russian roulette minigame. The longer you streak, the more campbucks you
earn. If you die, you lose campbucks and can't play for the rest of the day.

- `!rr` try your luck at russian roulette
- `!rr help` explain the command
- `!rr check` check status
- `!rr stats` check your own stats
- `!rr stats user` check a user's stats

#### `!slots`

Originally made by [Painketsu](https://twitch.tv/Painketsu) .

```toml
slots   = "https://gist.github.com/Francesco149/d86242fcf4581e10f88f583f81e70ecd"
```

Slots machine minigame. You can infinitely bet your campbucks , or play daily
spins for free until you lose, with different rewards and difficulty than the
infinite spins.

It will print a 3x3 (infinite spins) or 4x4 (daily) matrix of emotes. Different
emote types have different bonuses.

For the infinite spins, hitting one or more lines (including diagonals) is
considered a win. Different bonuses depending on certain line combinations like
a full screen, double, triple, etc.

For the daily spins,it rewards clusters of 5 or more emotes, again with
different bonuses based on emote types.

- `!slots daily` play daily spin for free. If you win, you get to go again
- `!slots` bet 100 campbucks
- `!slots amount` bet custom amount
- `!slots reset user` (admin debug command) resets daily slots completion for user
- `!slots stats/logs (user)/me/camp` see slots usage stats for you, a user, or
  the whole chatroom (camp)

#### `!dailies`

Originally made by [HK_BLAU](https://twitch.tv/HK_BLAU) .

```toml
dailies = "https://gist.github.com/Francesco149/3f75997e3bf32657b87102347528f30b"
```

Utility to check the status of all daily minigames at once.

- `!dailies` check dailies status for yourself
- `!dailies user` check dailies status for a user

#### `!fish`

Originally made by [HK_BLAU](https://twitch.tv/HK_BLAU) .

```toml
fish = "https://gist.github.com/Francesco149/8fffa1d982967b011143c87d8642cb73"
```

Fishing minigame. This slowly earns you campbucks if you're consistent with it,
but it's not really spammable nor does it give too much unless you're very
lucky.

Claim your daily goodies for a few casts with increased probabilities.

Fish have attributes like weight and size which will affect their campbucks
value. Discover all the types of fish and check the top catches in the
leaderboards.

It includes random weather events affecting the probabilities of certain fish
and random item finds that help you catch rare fish.

- `!fish` try your luck at fishing
- `!fish claim` claim daily goodies
- `!fish logs/stats casts/fishes/(fish) camp/(user)` stats for user or the whole
  chatrooom (camp)
- `!fish prob/p fishes/(fish) camp/(user)` observed probabilities of all fishing
  attempts
- `!fish devreset` old admin debug command to reset the daily `!mirage`, which
  used to be part of the `!fish` script
- for fishes with 2 words or more, replace the space with an underscore

#### `!mirage`

Originally made by [HK_BLAU](https://twitch.tv/HK_BLAU) .

```toml
mirage = "https://gist.github.com/Francesco149/3be418db34a5bafe39c7217f95f7a16a"
```

Find the mirage fish! In a 4x4 pond (grid), each cell starts with 0. The cell
with the mirage fish is incremented by 1. The mirage fish places 5 decoys in the
pond, each adding 1 to adjacent cells and itself (+ shape).

The decoys cannot be placed on top of each other, but can be placed on the
mirage fish itself. You have 30 seconds to catch it.

Use chess notation to attempt a catch, e.g `!mirage a1` to attempt a catch on
the bottom left corner, or `!mirage d4` to attempt a catch on the top right.

- `!mirage` no time limit, it gives you a decode command to run to get the answer.
- `!mirage daily` daily challenge until you lose, with a 30 seconds time limit.
- `!mirage a answer` answer.
- `!mirage decode` decode an answer.
- `!mirage example` tutorial.
- `!mirage stats` prints stats for a given user or for the chatroom (camp).
  Values in brackets include correct answers outside of the allocated time.
- `!mirage challenge/duel` challenge a user to a game of mirage.
- `!mirage accept/deny` accept or deny a user's challenge.
- `!mirage cancel` cancel challenge.
- `!mirage help/elp/hep` explain the command
- `!mirage stats help/elp/hep` explain `!mirage stats`
- `!mirage fix` old data migration command.

#### `!trivia`

Originally made by [Painketsu](https://twitch.tv/Painketsu) .

```toml
trivia  = "https://gist.github.com/Francesco149/101a73359e61582491eca6329b104a78"
```

Play random trivias from [opentb](https://opentdb.com) . You have 12 seconds to
answer before the answer is revealed.

Play the daily challenge for campbucks.

- `!trivia [category] [easy/medium/hard]` trivia of specified category and
  difficulty. if unspecified, it picks one at random. Categories: anime, games,
  music, animals, computers/comp, math, history, geography/geo, science/sci,
  mythology, general .
- `!trivia a [1/2/3/4]|[true/t/false/f]` answer trivia
- `!trivia help` explain command
- `!trivia statsfix [user] [add/remove] [multi/bool/streak]` admin debugging
  command.

### ambient: twitter

```toml
twitter = "https://gist.github.com/Francesco149/e21bd2f769cc3484d3224343e933f319"
```

Pulls text and links to images/videos when a twitter/X link is shared in chat.
Also translates any non-english using [ambient
translate](#ambientmodule-translate) .

#### twitter: required environment variables

- `OPENROUTER_API_KEY` your [openrouter](https://openrouter.ai/) API key.

#### twitter: required modules

- [ambient/module translate](#ambientmodule-translate) .
- [module openrouter](#module-openrouter) .

### ambient: lurk

```toml
lurk = "https://gist.github.com/Francesco149/ca1301804f7221af37e232ed80ab8119"
```

Funny chatbot that lurks in chat and responds when you least expect it. Has a
ice-cold personality that humiliates the user.

It will spontaneously chime in based on chat history and activity, as well as
responding to mentions of the bot's nickname.

Remembers of ~10 minutes of each user's interactions and the general chat activity.

Rate limit of 100 memories per hour global, 1000 per user per week.

Rate limit of 100 mentions per week per user, 50 per hour global.

Use the companion [lurk-monitor](https://github.com/Francesco149/lurk-monitor)
to monitor the memories and chatrooms.

#### lurk: required environment variables

- `OPENROUTER_API_KEY` your [openrouter](https://openrouter.ai/) API key.

#### lurk: required modules

- [module openrouter](#module-openrouter) .
- [lurk_db](#module-lurk_db)

### ambient/module: translate

```toml
translate  = "https://gist.github.com/Francesco149/d77859397ca795b60bc08ac620e378da"
```

Automatically translate non-english text posted in chat.

Listens to every message and queries `meta-llama/llama-3.1-8b-instruct` (very
cheap model) to determine whether the message is worth translating. If it is,
queries `openai/gpt-4o-mini` for the actual translation, while also adding flag
emojis matching the language being translated.

Rate limit of 100 global per hour, 1000 per user per week.

#### translate: exports

```python
import translate

resp = translate.smart("Puoi tradurre questo messaggio?")
if "<english>" not in resp:
    print(resp)
```

#### translate: required environment variables

- `OPENROUTER_API_KEY` your [openrouter](https://openrouter.ai/) API key.

#### translate: required modules

- [module openrouter](#module-openrouter) .

### ambient: logs

```toml
logs = "https://gist.github.com/Francesco149/95c1fd7a6b45a8b7c9cd948975dbc2cb"
```

> ⚠️ **GDPR** — Do NOT run this without users' consent. If you are storing the
> logs long term, you have to disclose what you store, why and provide a way for
> users to erase themselves, to comply with GDPR regulations. This is intended
> to be used at small scale for groups of friends who all agree to be logged.

Logs all chat messages so they can be pulled up later. Stored in `logs.db` on a
per-channel basis. Includes username, message, timestamp and message being
replied to.

Also incrementally populates a per-user markov chain database to be used by
[`!markov`](#markov) .

#### logs: required modules

- [log_utils](#module-log_utils)

### `!logs`

```toml
log_pull = "https://gist.github.com/Francesco149/bb00d65e05e6693f9b941f6b79dc94cb"
```

Search or pull random logs logged by [ambient logs](#ambient-logs) .

- `!logs` pull 1 random log
- `!logs3` pull 3 random logs. Works up to 10.

#### `!logs` search syntax

Append these arguments to the command (e.g `!logs u user`) to filter logs.

- `me` your own logs.
- `u user` user's logs.
- `-u user` exclude this user.
- `+u user` include this user. Users `shigebot` and `shigemirror` are excluded
  by default, so this is used to bypass that.
- `+commands` include messages that are using commands that start with `!` or
  whatever prefix the bot is configured with.
- `=text` logs that start with text.
- `=?text` logs that contain text.
- `/regex` logs that match the regular expression.
- Example: `/[0-9][^0-9]` messages that start with a digit followed by a non-digit.
- All text after `=` `=?` or `/` including spaces is used, so these must always
  be at the end of the message.

#### `!logs`: required modules

- [log_utils](#module-log_utils)

### `!markov`

```toml
markov = "https://gist.github.com/Francesco149/b54b2c29d9bf85d36509cce36e3535d8"
```

Generates messages using the markov chain database trained by [ambient
logs](#ambient-logs) .

- `!markov` generate 1 message
- `!markov3` generate 3 messages. Works up to 10.

Uses the [same search syntax as `!logs`](#logs-search-syntax) but ignores some
options such as `+commands` and user exclusions.

#### `!markov`: required modules

- [log_utils](#module-log_utils)

### module: ratelimit

```toml
ratelimit  = "https://gist.github.com/Francesco149/b788b0706b7a09cf6b8c20aa6b9be2e4"
```

Utility to rate limit things.

#### ratelimit exports

```python
import ratelimit as rl

lim = rl.RateLimit(
    state_file="../my-rate_limit_state.json", # same file = shared rate limit
    global_limit_per_hour=100,
    user_limit_per_week=1000,
    weekly_reset_day=3, # when wednesday turns into thursday
    weekly_reset_hour=0, # midnight UTC
    message_template="Slow down! Next reset in {time}",
)

allow, error_message = lim.allow(os.environ['NICK'])

```

### module: openrouter

```toml
openrouter = "https://gist.github.com/Francesco149/ca21f8a928ff22f695369056f1a3691e"
```

Utility to query LLMs on openrouter.

#### openrouter exports

```python
import openrouter

# defaults to a cheap model
resp = openrouter.ask("what is the meaning of life")
print(resp)

resp = openrouter.ask(
    user="what is the meaning of life",
    system="""
You are a wise bot with the ultimate answers to the universe.
Respond in a cryptic way, make the user ponder
    """,
    model="arcee-ai/trinity-mini",
    timeout=40,
)
print(resp)
```

#### openrouter: required environment variables

- `OPENROUTER_API_KEY` your [openrouter](https://openrouter.ai/) API key.

### module log_utils

```toml
log_utils  = "https://gist.github.com/Francesco149/4a9149fe286f39d00984a60a239cba98"
```

Common database utilities for log-related commands.

#### log_utils exports

```python
import sys
import log_utils

user = os.environ["NICK"]
message = " ".join(sys.argv[1:])

conn = log_utils.open_db()
if log_utils.log_message(conn, user, message):
    # message was not already in the db
    log_utils.update_markov(conn, user, message)
```

```python
import sys
import log_utils

msg = " ".join(sys.argv[1:])

p = log_utils.parse_twitch_cmd(msg, "logs")
if p:
    conn = log_utils.open_db()
    cursor = conn.cursor()

    base_where = " WHERE 1=1"
    params = []

    # Target User
    if p['user']:
        base_where += " AND LOWER(username) = ?"
        params.append(p['user'])

    # ...
```

### module: lurk_db

```toml
lurk_db    = "https://gist.github.com/Francesco149/9ceb2ec1b1856cb8e44aa60bb56ff2f2"
```

Common database utilities for things related to [ambient lurk](#ambient-lurk) .

#### lurk_db exports

```python
from lurk_db import DB

with DB() as db:
    db.prune_old_messages(1800)
    chan_mems = db.get_memories("#channel")

    messages = db.get_recent_messages(600, limit=20)
    print(messages)

    style_history = db.get_state("style_history", [])

    # ...

    db.set_state("style_history", style_history[-10:])
    db.set_state("last_spoke_ts", now)

    # ...

    db.add_memory("user", "user did a thing")
    db.prune_memories("user", 3)

    # ...

    db.add_message(
        ts=now,
        username=username,
        content=content,
        reply_to_user=reply_to_user,
        reply_to_msg=reply_to_msg,
    )

    # ...

    db.mark_seen(target["id"])
```
