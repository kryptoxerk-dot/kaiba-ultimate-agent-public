# Incident: a provider is down or rate-limiting

**Question this answers:** GMGN/Helius/DexScreener is failing. What do I do, and what
must I not do?

**The thing to get right first:** a provider outage is dangerous because of the
**positions you already hold**, not the entries you are missing. Protect the book, then
worry about the feed.

---

## 1. Confirm it, in 30 seconds

```sh
sudo /opt/kaiba/current/deploy/status.sh
```

Read the PROVIDERS block. `cooldown=<n>s` means our own limiter parked that provider
after a 429; `families_cooled` means only some endpoints are parked.

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main probe
```

Tells you whether it is us (a bad key, an expired JWT, our own cooldown) or them.

## 2. Is anything unprotected?

This is the question that matters.

```sh
sudo /opt/kaiba/current/deploy/status.sh | sed -n '/POSITIONS/,/PROVIDERS/p'
```

If any open position shows `prot=NO`, stop reading this runbook and go to
[incident-position-unprotected.md](incident-position-unprotected.md).

If positions are protected **through the provider** (GMGN condition orders), they
survive our outage and theirs differently: a GMGN outage takes both our quotes *and*
their condition orders with it. Assume provider-side protection is gone during a
provider outage, and fall back to the local watchdog.

## 3. Stop taking new risk you cannot price

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk pause
```

Pausing entries is cheap and reversible. Entering a position while the thing that
prices your exit is down is not.

Do **not** hit the kill switch reflexively. `kill_switch: true` sets every lane's effective
entry mode to OFF. The protection watchdog and executor still allow exits for already-open
positions. Pause and reduce-only also stop entries while leaving exits working; choose kill
when you need the stronger entry halt because you do not trust the system right now.

## 4. Decide: wait, or switch lanes

The design has two execution lanes for exactly this (PLAN §3). If GMGN is the one that
is down and the chain is supported directly:

```sh
# check what the direct lane can still do
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main probe --only jupiter,helius
```

If the direct lane is green, the engine will route through it on its own. Your job is
to confirm it did, not to force it:

```sh
sudo /opt/kaiba/current/deploy/status.sh | sed -n '/EVENTS/,/POSITIONS/p'
```

If **both** lanes are down for a chain, that chain is uncontrollable. Reduce-only it and
say so out loud in the journal.

## 5. Do not do these

- **Do not raise the rate limits in `risk.yaml` to "get through".** The limiter is what
  keeps us from a longer ban. `provider_budgets.<provider>.capacity` exists to be
  lowered in an incident, not raised.
- **Do not clear a cooldown by hand** unless you know the provider's reset window has
  passed. `limiter.reset()` exists; using it during a 429 storm earns a longer ban.
- **Do not restart services in a loop.** Restarting does not refill a leaky bucket that
  lives in the database, and it does lose the in-flight reconnect backoff.
- **Do not resubmit an order whose state is unknown.** That is a different incident:
  [incident-ambiguous-send.md](incident-ambiguous-send.md).

## 6. Recovery

When the probe comes back green:

```sh
sudo systemctl start kaiba-reconcile.service     # what happened while we were blind
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk resume
sudo /opt/kaiba/current/deploy/status.sh
```

Reconcile **before** resume, every time. The outage window is precisely when an order
changed state without us seeing it.

## 7. Write it down

Append to the journal, because the nightly reflection reads it and the weekly provider
budget audit needs the history:

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main journal add observation \
  "gmgn 429 from 14:02 to 14:39 UTC; entries paused; 2 positions held on local watchdog; \
   no exits missed; reconcile found 0 drifted orders"
```

If the same provider does this twice in a week, it is not an incident any more, it is a
capacity problem. Lower its budget in `risk.yaml`, or move the lane that depends on it.
