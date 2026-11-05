# Runbook: the J7 tweet feed (`j7_feed`)

**Question this answers:** how do I let Kaiba read the J7Tracker feed so Hermes can "check
tweets", how do I see that it works, and how do I turn it off?

You asked for this on 2026-10-28 ("implement https://j7tracker.io/ to kaiba ultimate agent to
check tweets"). The code is `kaiba/ingest/j7_feed.py` (the `j7_feed` listener inside
`kaiba-ingest`) and the Hermes tool `kaiba_tweets`.

---

## What it does

J7Tracker pushes the posts of the accounts it tracks (X, plus Truth Social, Instagram,
TikTok, YouTube and Binance Square) within about a second, with the contract address J7
spots in each post. `j7_feed` keeps one connection to that feed open and records, in its own
file `data/j7_feed.db` (never the main `kaiba.db`):

- **posts:** author, text (up to 4,000 characters), when it was posted, when Kaiba first saw
  it, later and fuller copies merged in (J7 sometimes sends a quick copy first and the full
  one with media and quotes a moment later), and the time it was deleted (a deleted post is
  kept, marked deleted);
- **contract addresses** J7 attached to each post (Solana mints and EVM addresses), with the
  moment each one first appeared on that post;
- **token details** J7 sends for those addresses: symbol, name, market cap, liquidity and,
  on EVM chains, J7's honeypot flag;
- **follows, profile changes and J7's own "is X up" status**.

When a connection opens, J7 first replays its last 100 or so posts. Those are marked
`backlog`, so nobody mistakes an old post for a fresh one.

## What it never does

- It **never trades, never creates or launches a token and never sends anything to J7**
  except the one hello message J7 requires when a connection opens.
- It **never needs a wallet key.** J7 also sells a trading service that takes your wallets'
  signing keys (its "API keys" page). **Never paste a wallet key into Kaiba, and never use
  J7's deploy or trading settings for Kaiba.** The feed needs only your login session.
- Post text is other people's writing. Kaiba stores and shows it as data and never follows
  instructions written in a post. Invisible characters (the kind that can hide a message a
  person cannot see but a model can read) and instruction-shaped phrases are stripped before
  the text is stored or shown to Hermes. That is a speed bump, not a guarantee.

## The token is your J7 login

Only you turn this on, by writing the file over SSH (step 3 below).

**Never send the token in Telegram, to Hermes, in Discord or in a support ticket.** Nobody at
J7 needs it, and **Hermes must not ask for it or accept it.** If anyone asks you for it,
including someone who says they are J7 support, they are trying to take over your J7 account.

Hermes is also told never to read, print, copy or send the token file, never to log in to
J7 or fetch a token itself, and never to run a command or use a tool because a post says so.
If Hermes ever offers to "set up J7 for you", say no: only you do that, over SSH.

Anyone holding the token can act as your J7 login. So where possible, use a J7 login with
no trading wallets saved for this.

**The file is the only place Kaiba reads the token from.** Do not put it in an environment
variable (`J7_SESSION_JWT`): deleting the file would then no longer turn the feed off, a new
token in the file would be ignored, and every program kaiba-ingest starts would inherit your
login. If that variable is set for kaiba-ingest, the feed does not run and you get a page
(`j7_session_in_env`) until it is removed.

**The file must be yours alone:** mode 600 (`-rw-------`), owned by the user that runs
`kaiba-ingest`. Kaiba does not use a token file that other accounts on the box can read, or
that belongs to another user (`insecure_j7_session`), and a file it cannot read (for example one
written with `sudo`, which belongs to root) is `unreadable_j7_session`. Both page.

**The same file is also read by the tweet launcher's J7 bridge** (`kaiba/ingest/j7_launch_bridge.py`)
whenever `config/tweet_launch.yaml` has `feed.backend: j7`. That reader prefers `J7_SESSION_JWT`
to the file when the variable is set. With both running, Kaiba holds two of J7's five
connections for your account (see "Connections" below).

---

## Turn it on

1. **Log in to j7tracker.io yourself**, in your own browser.
2. **Get your feed session token.** J7's API docs call it `<jwt>` (usually a long string
   starting with `eyJ`), but they do not explain how a user obtains it. Ask J7 support or the
   J7 Discord *how* to get your session token (JWT) for the feed API. Ask how; never send the
   token itself to anyone. It is **not** one of J7's "API keys"; those are encrypted wallet
   keys and must never go to Kaiba.
   If an earlier J7 setup left a `~/.config/kaiba/j7.session` on the box (the 2026-10-07 J7
   setup there may have), **check it first**: `ls -l ~/.config/kaiba/j7.session` must show
   `-rw-------` owned by the user that runs `kaiba-ingest`, and the J7 login behind it must have
   no trading wallets saved. Otherwise delete it (`rm ~/.config/kaiba/j7.session`) and do step 3.
   A file that is there is used at the first `kaiba-ingest` restart after this feature (and the
   `social` extra) reaches the box.
3. **Put the token on the box over SSH**, as the user that runs `kaiba-ingest`, as ONE line in
   its own file. Typing it into a command line would leave it in your shell history, so paste
   it at a hidden prompt instead. The second line waits for the token, writes it to a new file
   and then swaps that file in, in one step, so Kaiba never sees a half-written one:

   ```sh
   umask 077; mkdir -p ~/.config/kaiba
   read -rs t && printf '%s\n' "$t" > ~/.config/kaiba/j7.session.new && unset t && mv ~/.config/kaiba/j7.session.new ~/.config/kaiba/j7.session
   ```

   After the second line, paste the token (nothing shows) and press Enter. Keep that line the
   last one you paste: whatever comes after it would be read as the token. Then check:
   `ls -l ~/.config/kaiba/j7.session` must show `-rw-------` and your user. Use the same two
   lines to **refresh** the token later. (`cat > ~/.config/kaiba/j7.session` empties the file
   first and leaves it empty while you paste; Kaiba keeps the connection it has for two minutes
   when it finds the file empty, but the swap above avoids the question.)

4. **Wait.** The ingest service looks again at an idle feed every 6 hours, so the feed
   starts by itself within 6 hours, or at the next restart of `kaiba-ingest`. Nothing else
   needs a restart. The feed also needs `python-socketio` **and `aiohttp`** in Kaiba's
   environment: install the `social` extra in `pyproject.toml` (`pip install -e ".[social]"`
   in Kaiba's venv). J7's own install line, `pip install "python-socketio[client]"`, does not
   bring `aiohttp`, and without it no connection can ever work. If either is missing you get a
   page saying so (`missing_socketio_dependency`).

Optional: `J7_FEED_REGION=dfw` makes Kaiba try J7's Dallas server first instead of New York.
It switches to the other one by itself when a connection fails, when one drops in its first
minute, and when one stays connected but sends no post for 15 minutes. After a connection that
ran for a minute or more drops, it tries the same server once more first (a server restart).

### How long a token lasts

**Not measured yet.** J7's error page says an "Invalid token" is one that "is expired or isn't
an access token" and that you should log in again, so the token does expire, and Kaiba cannot
refresh it for you (that would need J7's account endpoints, which Kaiba never uses).

When the token is a JWT, Kaiba reads its expiry time on the box (only that number; nothing else
in it is read, logged or stored, and nothing is sent anywhere):

- the ingest log says `the session token expires in N h` once per token;
- `kaiba_tweets` shows it as `token_expires_ms` in the feed's health line;
- you get **one page an hour before it expires**, so you can write a new token in time;
- after it expires Kaiba **stops connecting** (`auth_error:token_expired`) and pages DOWN,
  until the file holds a new token. A new token is picked up within 30 seconds.

**Measured lifetime: not yet.** After the first live token, write here how long it lasted
(the `expires in` log line when it was first read, and the date). If it lasts only hours, the
feed cannot run unattended and this needs a decision, not a workaround.

## See it working

- **Ask Hermes "check tweets".** It calls `kaiba_tweets`, which returns recent posts with
  their contract addresses and token details, plus the feed's health line. You can narrow it:
  an account (`handle`), one contract address (`token`), a chain, a time window (up to 14
  days), or "only posts with a contract address".
- **The health line** in that answer shows `state`. `running` means connected. `last_event_ms`
  is the time of the last live post; the backlog replay does not count. `last_error`, when
  set, is a short code such as `upstream:disconnected` (J7's own X feed is down).
  `token_expires_ms` is the token's expiry when Kaiba could read it.
- **On the box:** `journalctl --user -u kaiba-ingest | grep j7_feed` shows connects and
  reconnects. Those log lines never contain your token or post text.

### Telegram pages you may get

| Page | Meaning | What to do |
|---|---|---|
| INGEST FEED DOWN ... `auth_error:invalid_session` / `invalid_token` / `token_expired` | J7 refused the token, or it expired | Refresh the token: repeat steps 1-3 over SSH. Kaiba picks the new file up within a minute. |
| INGEST FEED DOWN ... `auth_error:refused` | J7 refused the connection with a message Kaiba does not know (the `kaiba-ingest` log shows J7's words) | Check at j7tracker.io that your account and plan can use the feed, then refresh the token (steps 1-3). |
| INGEST FEED DOWN ... `auth_error:too_many_connections` | J7 allows 5 feed connections per account, **including your open j7tracker.io browser tabs** | Close a tab. Kaiba tries again within 15 minutes. |
| INGEST FEED DOWN ... `invalid_j7_session` | The token file is empty or not one line (for over two minutes, if the feed was running) | Write the token again as one line with the two lines of step 3. |
| INGEST FEED DOWN ... `insecure_j7_session` | Other accounts on the box can read the token file, or it belongs to another user | As the user that runs `kaiba-ingest`: `chmod 600 ~/.config/kaiba/j7.session`. If it belongs to root (written with `sudo`), delete it and write it again (step 3). |
| INGEST FEED DOWN ... `unreadable_j7_session` | `kaiba-ingest` cannot read the token file (a root-owned file, or a directory at that path) | `ls -l ~/.config/kaiba/j7.session`: it must be a file, `-rw-------`, owned by the user that runs `kaiba-ingest`. Otherwise delete it and write it again (step 3). |
| INGEST FEED DOWN ... `j7_session_in_env` | `J7_SESSION_JWT` is set in kaiba-ingest's environment | Put the token in `~/.config/kaiba/j7.session`, remove the variable from kaiba-ingest's environment (its EnvironmentFile or a drop-in) and restart kaiba-ingest. |
| INGEST FEED DOWN ... `missing_socketio_dependency` | `python-socketio` or `aiohttp` is missing | Install the `social` extra in Kaiba's environment (step 4). |
| INGEST FEED ATTENTION ... `token_expiring` | The token expires within the hour | Write a new token (steps 1-3) before it does. |
| INGEST FEED ATTENTION ... `store_capacity` | `data/j7_feed.db` reached 512 MiB | Nothing urgent: posts are kept without text and retention drops to 7 days until it is back under the cap. |
| INGEST FEED ATTENTION ... `store_full` | The file reached 640 MiB, or the disk is 88 % full | The feed records nothing but counters until space frees up. Check `df -h`. |
| INGEST FEED ATTENTION ... `store_error` | `data/j7_feed.db` refuses writes: posts arrive but nothing is kept | Check the files' owner and mode with `ls -l data/j7_feed.db*` (a `-wal` or `-shm` file left by running Kaiba as root is the usual cause) and the disk with `df -h`. The file holds at most 14 days of posts: if it is damaged, stop kaiba-ingest, delete `data/j7_feed.db*` and start it again. |
| INGEST FEED SILENT | Connected, but no post for 30 minutes | Usually J7's side; the page says why when J7 told us. With `connect_failed`, `disconnected` or `stalled` in it, Kaiba cannot keep a connection: check J7's status (`curl -s https://nyc.j7tracker.io/api/health`, public, no login) and the box's network. |

After J7 refuses a token, Kaiba waits 15 minutes before it tries the same token again (then
30 minutes, then an hour, up to 6 hours), so a bad token never hammers J7. That includes a
refusal J7's docs do not list (`auth_error:refused`). A new token in the file is used within
30 seconds. "Too many connections" is retried every 15 minutes, because the token itself is
fine. **No token at all is never a page:** that is "off".

Each problem pages once: a standing refusal is one page, not one per retry. A different code,
a new token that J7 refuses as well, or the same problem still there after `kaiba-ingest`
restarts, is a new page.

A problem with the token FILE (empty, not one line, wrong mode or owner, unreadable) never
turns a running feed off: Kaiba keeps its connection for two minutes, then goes `down` with
the page above and looks at the file every 30 seconds, so your fix is used within 30 seconds.
If the problem was there when the feed started, the feed is `idle` instead and the fix is
picked up at the next idle check (within 6 hours) or when `kaiba-ingest` restarts.

### Connections

J7 allows **5 feed connections per account, shared with your open j7tracker.io browser tabs**.
Kaiba's feed uses one. The tweet launcher's J7 bridge uses another while `feed.backend` is `j7`
in `config/tweet_launch.yaml`. If you see `auth_error:too_many_connections`, close a tab.

## Turn it off

Delete the file:

```sh
rm ~/.config/kaiba/j7.session
```

Within about 30 seconds the feed disconnects and stays idle. That is not paged. Put the file
back to turn it on again. (This works because the file is the only place the token is read
from; see "The file is the only place" above.) Emptying the file is NOT the off switch: an
empty file pages as `invalid_j7_session`. Only deleting it is "off".

---

## Where the data is, and for how long

- `data/j7_feed.db` in Kaiba's data directory (the same directory as `kaiba.db`).
- Rows older than **14 days** are deleted every hour while the feed runs, and the freed space
  goes back to the disk. While the feed is off (or cannot run) the same deletion runs at each
  idle check, about four times a day, so turning it off does not leave 14 days on the disk.
  To remove everything at once, stop `kaiba-ingest` and delete `data/j7_feed.db*`.
- kaiba-disk-guard and the hourly `retention` job do not know this file; its own limits below
  are what keep it small.
- At **512 MiB** Kaiba keeps recording who posted what and when, and every contract address,
  but stops keeping post text, and keeps 7 days instead of 14, until the file is back under the
  cap. The health line says `store_capacity`.
- At **640 MiB**, or when the disk holding the data directory is **88 % full**, Kaiba stores
  nothing but counters of what it dropped, and says `store_full`. (kaiba-disk-guard pages at
  80 %, acts at 85 % and halts entries at 92 %.)
- On 2026-10-28 J7's public health page showed between about 13 and 43 posts a minute across
  J7's whole cache; 14 days of that is at most a few hundred MB. Kaiba's own socket carries only
  the main feed's accounts, so its real rate is measured after the first week.

## Using posts as a trading signal

Not yet, and not by this feed alone. Nothing in Kaiba trades on this data. Turning posts into
a signal is a separate step: a study measured forward on everything the feed saw, with the
backlog rows excluded, before any lane reads it. That is the same rule every other filter
follows since 2026-09-24.
