# Runbook: first install

**Question this answers:** I have a bare Ubuntu 24.04 VPS. How do I get Kaiba onto it?

**Time:** about 45 minutes, most of it waiting for you to fetch credentials.

**You will not be trading at the end of this.** This gets the machinery installed and
verified. Arming is [arming.md](arming.md).

---

## 0. Before you touch the box

Have ready:

- the host you are deploying to, as a variable you will export: `KAIBA_HOST`
- a DNS name or sslip.io name that resolves to it: `KAIBA_DASHBOARD_HOSTNAME`
- an age public key (`age-keygen` on your laptop — keep the private half **off** the VPS)
- the credentials from `deploy/rotate-keys.sh --list`, already rotated in each provider's
  dashboard

Decide now, and write it in the work log: **which VPS**. PLAN §11 Phase 0 recommends the
existing hardened Tencent box. Do not put a second agent on a machine that already runs
one — two agents sharing a `/run` and a Caddy is how you get a 3am outage on the wrong
service.

## 1. Prepare the host

```sh
sudo apt update
sudo apt install -y python3.12 python3.12-venv sqlite3 rsync chrony age git
sudo systemctl enable --now chrony      # preflight fails on clock skew, and it is right to
```

Node 24 only if you use the GMGN lane (`gmgn-cli` is a node program):

```sh
curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
sudo apt install -y nodejs && sudo npm i -g gmgn-cli
```

The scanner and engine keep `MemoryDenyWriteExecute=yes` by default. If you enable a
GMGN lane that actually launches Node, set `KAIBA_ALLOW_NODE_JIT` to the specific unit
names before installation so the installer renders the narrow drop-in; do not relax this
hardening globally. The scanner unit is `kaiba-scan.service`.

## 2. Get the code onto the box

```sh
export KAIBA_HOST=<your host>
rsync -a --exclude .git --exclude .venv --exclude data --exclude .env \
      ./ "root@$KAIBA_HOST:/root/kaiba-src/"
```

Never rsync `.env`. Credentials go on with `rotate-keys.sh`, one at a time, and are
never copied from the dev box.

## 3. Install

```sh
ssh "root@$KAIBA_HOST"
cd /root/kaiba-src
./deploy/install.sh            # add --dry-run first if you want to read it
```

This creates the four users, the directory layout, the release under
`/opt/kaiba/releases/<stamp>`, the venv, the database schema, and the systemd units. It
**enables** units; it starts nothing.

On the first install it seeds `/etc/kaiba/config/schedule.yaml` from the release
template. A non-empty schedule is preserved on later upgrades, so operator interval and
budget edits do not disappear when `current` moves to a new release. `signals.yaml` is
optional; the signal poller uses its documented defaults until you create that file.

Read the last screen it prints. It is the list of things it cannot do for you.

## 4. Credentials

```sh
sudo deploy/rotate-keys.sh --all
```

Prompts for each, hides the input, records a sha256 fingerprint and nothing else.
Press Enter to skip anything you do not have yet.

Check what landed:

```sh
sudo deploy/rotate-keys.sh --verify
```

## 5. Wallet keys

Keys are generated **on the box, by the signer**, and never imported from a laptop.

```sh
sudo systemctl start kaiba-signer
sudo -u kaiba-signer /opt/kaiba/venv/bin/python -m kaiba.cli.main signer keygen --chain sol
sudo -u kaiba-signer /opt/kaiba/venv/bin/python -m kaiba.cli.main signer keygen --chain robinhood
```

Put each resulting address into `chains.<chain>.wallet` in
`/etc/kaiba/config/risk.yaml`. For the GMGN lane, confirm in the GMGN portal that the
API key is bound to that same wallet on that chain — a mismatch here is exactly what
stopped the previous repo from ever trading.

Leave `bankroll_base_units` at 0 until the wallet is funded.

## 6. Hermes

Install the profiles **before** authenticating, and run both operations as `kaiba-agent`.
The service reads this exact Hermes home; running the installer as root would write a
different profile tree under `/root`.

```sh
sudo -u kaiba-agent env HOME=/var/lib/kaiba/hermes HERMES_HOME=/var/lib/kaiba/hermes/.hermes \
  /opt/kaiba/current/deploy/install-hermes.sh --root /opt/kaiba/current \
  --python /opt/kaiba/venv/bin/python --hermes /opt/kaiba/hermes-venv/bin/hermes

sudo -u kaiba-agent env HOME=/var/lib/kaiba/hermes HERMES_HOME=/var/lib/kaiba/hermes/.hermes \
  /opt/kaiba/hermes-venv/bin/hermes --profile kaiba-operator auth add anthropic
sudo -u kaiba-agent env HOME=/var/lib/kaiba/hermes HERMES_HOME=/var/lib/kaiba/hermes/.hermes \
  /opt/kaiba/hermes-venv/bin/hermes --profile kaiba-operator auth add openai-codex
```

Repeat the two `auth add` commands for `kaiba-research` and `kaiba-reflect`. The Codex
login uses Hermes' device-code flow on the pinned VPS runtime. Hermes stores its own
OAuth credentials under `/var/lib/kaiba/hermes/.hermes`; do not copy or share
`~/.codex/auth.json`, because rotating refresh tokens can invalidate the other process.

Check status without printing credential material, then run the provider smoke test only
after both logins succeed:

```sh
sudo -u kaiba-agent env HOME=/var/lib/kaiba/hermes HERMES_HOME=/var/lib/kaiba/hermes/.hermes \
  /opt/kaiba/hermes-venv/bin/hermes --profile kaiba-operator auth status openai-codex
sudo -u kaiba-agent env HOME=/var/lib/kaiba/hermes HERMES_HOME=/var/lib/kaiba/hermes/.hermes \
  /opt/kaiba/hermes-venv/bin/hermes --profile kaiba-operator doctor
sudo -u kaiba-agent env HOME=/var/lib/kaiba/hermes HERMES_HOME=/var/lib/kaiba/hermes/.hermes \
  /opt/kaiba/hermes-venv/bin/hermes --profile kaiba-operator chat --oneshot -q \
  "Reply exactly CODEX_READY" --provider openai-codex --model gpt-6-astra --reasoning high
sudo -u kaiba-agent env HOME=/var/lib/kaiba/hermes HERMES_HOME=/var/lib/kaiba/hermes/.hermes \
  /opt/kaiba/hermes-venv/bin/hermes --profile kaiba-operator chat --oneshot -q \
  "Call kaiba_status and report the lane modes in one sentence." \
  --provider openai-codex --model gpt-6-astra --reasoning high
```

The first command is the direct Codex/Astra/high smoke test; the second is read-only and
confirms that the fallback can reach Kaiba's MCP status surface. Both consume subscription
quota and are intentionally run only after Claude's setup is complete. Then configure the
Telegram gateway per the installer’s closing instructions. Do not start the trading
services until the preflight and arming gates below pass.

## 7. TLS

```sh
sudo systemctl edit caddy      # set KAIBA_DASHBOARD_HOSTNAME and KAIBA_ACME_EMAIL
sudo /opt/kaiba/current/deploy/install.sh --with-caddy --skip-bootstrap
```

If `caddy validate` complains about `rate_limit`, your Caddy has no rate-limit plugin.
Either rebuild it (`xcaddy build --with github.com/mholt/caddy-ratelimit`) or comment out
the two blocks marked `[PLUGIN]` in `/etc/caddy/Caddyfile`. Do not leave `/control/*`
reachable without one of the two.

## 8. The gate

```sh
sudo /opt/kaiba/current/deploy/preflight.py
```

Every line must be green. A `SKIP` counts as a failure: you cannot arm on a check nobody
ran. Fix and re-run until the exit code is 0.

## 9. Start, in this order

```sh
sudo systemctl start kaiba-signer kaiba-mcp kaiba-protection
sudo systemctl start kaiba-ingest kaiba-ops kaiba-scan kaiba-dashboard kaiba-hermes
sudo systemctl start kaiba-engine
sudo systemctl start kaiba-reconcile.timer kaiba-backup.timer
```

Protection starts before the engine, always. Nothing should be able to open a position
before the thing that closes it is already running.

The scanner drains the durable tier-0 triage queue and must be active before the engine
can receive fresh lane signals. Check it with `kaiba scan status --json` and
`journalctl -u kaiba-scan`.

The maintenance scheduler is a separate long-running service. It owns scheduled backfills,
budget checks, journal verification, and native-price sampling; check it with
`kaiba ops status` and `journalctl -u kaiba-ops`.

## 10. Confirm

```sh
sudo /opt/kaiba/current/deploy/status.sh
```

Expect: every service active, event age in seconds, no open positions, lanes in
`shadow`, `kill_switch=false`. Then watch it for 24 hours before you think about
[arming.md](arming.md).

---

## If something did not start

```sh
systemctl status kaiba-<name>
journalctl -u kaiba-<name> -n 80 --no-pager
```

Two failures are common enough to name:

- **`gmgn-cli` exits immediately with no output.** `MemoryDenyWriteExecute=yes` blocks
  V8's JIT. Apply `deploy/systemd/dropins/allow-node-jit.conf` to that unit only, and
  record why in the work log.
- **"attempt to write a readonly database".** Something ran sqlite as root and left
  `kaiba.db-wal` owned by root. Fix:
  `sudo chown kaiba-core:kaiba-state /var/lib/kaiba/db/kaiba.db*`
