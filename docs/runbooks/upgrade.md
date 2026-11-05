# Runbook: release, reconcile, rollback

**Question this answers:** how do I ship a change to a machine that is holding money?

**The one rule:** *never hot-edit a funded service.* No `vim` on a file under
`/opt/kaiba/current`, no `pip install` into the live venv, no editing `risk.yaml` with
the engine running. A release is a new directory and a symlink flip, so that a rollback
is also a symlink flip.

---

## Decide what kind of upgrade this is

| Kind | What it touches | Can positions be open? |
|---|---|---|
| **Cold** | migrations, signer, execution, protection | **No.** Flatten first. |
| **Warm** | ingest, intelligence, dashboard, Hermes profiles, skills | Yes, with reconcile around it. |
| **Config** | `risk.yaml` values inside the existing bounds | Yes. |

If you are unsure, it is cold.

## Before anything

```sh
sudo /opt/kaiba/current/deploy/status.sh
sudo systemctl start kaiba-backup.service      # do not upgrade onto an old backup
```

Write down the last event id from `status.sh`. That is the line you reconcile back to if
this goes wrong.

## Warm upgrade

```sh
# 1. stop entries, leave protection running
sudo systemctl stop kaiba-engine kaiba-scan kaiba-ops kaiba-ingest

# 2. deploy the new release (new directory, then the symlink flips)
cd /root/kaiba-src && git pull
sudo ./deploy/install.sh --skip-bootstrap

# 3. reconcile before anything decides again
sudo systemctl start kaiba-reconcile.service
sudo journalctl -u kaiba-reconcile -n 40 --no-pager

# 4. gate
sudo /opt/kaiba/current/deploy/preflight.py

# 5. back up
sudo systemctl start kaiba-ingest kaiba-ops kaiba-scan
sudo systemctl start kaiba-engine
```

`kaiba-protection` keeps running throughout. It is the only service that is not part of
a warm upgrade, because the whole point is that it never stops.

## Cold upgrade

A cold upgrade touches the code that decides what to sell and how. It runs against a
**flat book**.

```sh
# 1. stop taking new positions
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk pause
#    (or hit Pause in the dashboard; it sets entries_paused in risk.yaml)

# 2. reduce-only, then wait for the book to flatten on its own ladder
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk reduce-only --on
watch -n 30 'sudo /opt/kaiba/current/deploy/status.sh | sed -n "/POSITIONS/,/PROVIDERS/p"'

# 3. only when POSITIONS says "none open":
sudo systemctl stop kaiba.target

# 4. deploy
cd /root/kaiba-src && git pull
sudo ./deploy/install.sh --skip-bootstrap      # runs migrations

# 5. gate, then start in the deploy.md order
sudo /opt/kaiba/current/deploy/preflight.py
```

Do not "just close them at market" to get to a flat book faster unless you have a reason
that is better than impatience. Forced exits at a bad moment cost more than a deploy
that waits a day.

## Rollback

The previous five releases are on disk. Rolling back the **code** is a symlink flip:

```sh
ls -1 /opt/kaiba/releases/          # pick the one before the current target
sudo systemctl stop kaiba.target
sudo ln -sfn /opt/kaiba/releases/<previous> /opt/kaiba/current.new
sudo mv -Tf /opt/kaiba/current.new /opt/kaiba/current
sudo /opt/kaiba/venv/bin/python -m pip install --quiet /opt/kaiba/current
sudo /opt/kaiba/current/deploy/preflight.py
```

**Migrations do not roll back.** `kaiba/core/migrations/` is append-only by contract
(CONTRACT.md), and preflight will FAIL with "database has migrations this release does
not" if you roll the code back past a schema change. When that happens you have two
honest options:

1. roll **forward** with a fix, which is almost always right; or
2. restore the database snapshot that matches the old release:
   `sudo deploy/restore.sh --archive <the one from before the upgrade> --identity <key> --activate`
   and accept that everything the agent did since that snapshot is gone from the
   database. It is still in the journal export inside the newer archive; reconcile
   against the chain afterwards.

Never edit an applied migration to make a rollback work. That desynchronises every other
copy of the database, including the backups.

## After any upgrade

```sh
sudo /opt/kaiba/current/deploy/status.sh
```

Check three things specifically:

- the event id is moving again (ingestion reconnected),
- open positions still show `prot=yes` (protection re-attached to them, rather than
  quietly forgetting the ones it did not create),
- lane modes are what they were before, not what the shipped `risk.yaml` defaults to.
  A release that resets a live lane to shadow is annoying; one that resets shadow to
  live is a loss.

Record in `docs/worklogs/`: release stamp, what changed, the event id before and after,
and the reconcile output.
