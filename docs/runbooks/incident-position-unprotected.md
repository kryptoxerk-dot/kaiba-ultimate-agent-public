# Incident: an open position has no protection

<!-- Codex coordination note 2026-09-20: P7-1l documents the read-only inspection
     command below; execution and protection actions remain Claude-owned. -->

**Question this answers:** `status.sh` shows `prot=NO`, or `kaiba-protection` is not
running. What now?

**This is the most urgent runbook in this directory.** An unprotected memecoin position
can go to zero in the time it takes to read the rest of this page. Do step 1 before you
finish reading.

---

## 1. Is the watchdog running? (do this first)

```sh
systemctl is-active kaiba-protection || sudo systemctl start kaiba-protection
sudo systemctl status kaiba-protection --no-pager | head -20
```

The unit restarts every 1 second, forever, with no burst limit. If it is *inactive*,
something stopped it deliberately or it is failing to start at all. If it is
`activating` in a loop, read the log:

```sh
sudo journalctl -u kaiba-protection -n 60 --no-pager
```

## 2. Stop adding to the problem

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk pause
```

One unprotected position is an incident. Five is a catastrophe. Pause entries now; you
can resume in a minute.

## 3. Find out what is actually exposed

```sh
sudo /opt/kaiba/current/deploy/status.sh | sed -n '/POSITIONS/,/PROVIDERS/p'
```

For each `prot=NO` line you need the persisted entry and stop state before you can decide.
The read-only command below does not fetch a live quote; obtain the current price from an
independent provider or the dashboard before making the protection decision.

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main positions show <position_id>
```

## 4. Protect it, in this order of preference

**a. Let the watchdog re-attach.** If step 1 brought the service back, give it one poll
interval (`protection.poll_interval_s`, default 5s) and re-check. The watchdog is
supposed to adopt positions it did not open; that is why it reads `positions` rather
than keeping its own list.

**b. Provider-side condition orders.** They survive our downtime, which is the whole
reason `protection.use_provider_orders` defaults to true:

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main protection attach <position_id>
```

**c. Exit manually.** If you cannot protect it in the next minute and the position is
moving against you, close it. A realised small loss beats an unmonitored one.

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main positions close <position_id> --reason unprotected_incident
```

There is no fourth option called "watch it for a bit".

## 5. Why was it unprotected?

Work this out **after** the book is safe, not before. The usual causes, in the order
they actually happen:

| Cause | How you tell | Fix |
|---|---|---|
| Watchdog was stopped for an upgrade and not restarted | `systemctl show -p ActiveEnterTimestamp` gap matches the deploy | Cold upgrades flatten the book first: [upgrade.md](upgrade.md) |
| Position opened while the watchdog was down | `opened_ms` falls inside the gap | The engine must not run without protection; check the unit ordering |
| Provider condition order was placed but silently cancelled | `protection_ids_json` is non-empty but the provider shows nothing | Fall back to local protection for that provider |
| The signer refused the protective order | signer log shows a refusal | The policy is working; the *order shape* is wrong. Fix the shape, not the policy. |
| Restart lost in-memory state | protection has no local persistence for that position | This is a bug. Protection state belongs in the database. |

## 6. Record it properly

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main journal add correction \
  "position <id> unprotected for <n> minutes from <t>; cause: <one line>; \
   outcome: <re-attached | closed at -x%>; prevention: <the change you are making>"
```

An unprotected position is a `correction`, not an `observation`. The distinction matters
because the nightly reflection weights corrections and the weekly evaluation counts
them against a lane's promotion.

## 7. Before you resume

```sh
sudo /opt/kaiba/current/deploy/preflight.py
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk resume
```

And if this happened during an arming sequence: the lane goes back one step. A lane that
lost protection has not earned canary, let alone live. [arming.md](arming.md).
