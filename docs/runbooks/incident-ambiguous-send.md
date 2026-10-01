# Incident: an order is in the UNKNOWN state

**Question this answers:** we sent something and we do not know whether it landed. What
do I do?

**The rule, and it has no exceptions: reconcile, never blindly resubmit.**

A resubmit that turns out to be a duplicate is the single most expensive mistake
available here. You do not lose the slippage; you lose a whole second position, opened
at a worse price, that nothing is tracking, with a size the risk envelope never
approved. The first one may already have been filled and may already be running its
exit ladder. Two ladders on one token fight each other.

---

## What UNKNOWN actually means

An order is UNKNOWN when we submitted it and did not get a definitive answer. Common
shapes:

- the RPC/provider connection dropped between submit and receipt;
- the provider returned a 5xx *after* accepting the transaction;
- we timed out waiting for confirmation but the transaction is still in the mempool;
- the process restarted between the reservation and the receipt.

In every one of those, the transaction may be confirmed, pending, or dead. Three
outcomes, and guessing gets you the wrong one at least a third of the time.

## 1. Stop the thing that might send again

```sh
sudo systemctl stop kaiba-engine
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk pause
```

Leave `kaiba-protection` running. If the order *did* fill, that position needs its
watchdog.

## 2. Look at what we recorded

```sh
sudo /opt/kaiba/current/deploy/status.sh | grep -A3 'unresolved'
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main orders show <order_id>
```

You want: the submit timestamp, the transaction hash or signature if we got one, the
reservation, and whether a `position_id` was created.

The order state machine reserves **before** it submits, precisely so this moment has a
record to reconcile against. If there is no reservation, the send never happened.

## 3. Ask the chain, not the provider

The chain is the only authority. A provider that already lied once by timing out does
not get to arbitrate.

```sh
sudo systemctl start kaiba-reconcile.service
sudo journalctl -u kaiba-reconcile -n 60 --no-pager
```

Reconciliation walks every non-terminal order, resolves it against the chain, repairs
the position rows and rewrites the six report files. Run it as many times as you like;
it is idempotent, which is why it is also on a 5-minute timer.

If you need to check by hand while you wait:

- **Solana:** look the signature up on the RPC (`getSignatureStatuses`); a signature we
  never got back means checking the wallet's recent transactions for the mint in the
  window.
- **EVM:** `eth_getTransactionReceipt` on the hash; no hash means checking the nonce.
  **If the nonce has advanced, something was sent.** Find out what.

## 4. The three outcomes

| Chain says | Do |
|---|---|
| **Confirmed** | Reconcile creates/repairs the position. Verify protection attached (`prot=yes`). Do **not** resubmit. |
| **Dropped / never landed** | The order is dead. *Now* you may re-enter — but re-enter as a **new decision** at the current price, not as a "resubmit" of a stale intent. The thesis may have expired with the price. |
| **Still pending** | Wait. Do nothing else. A pending transaction that you "replace" becomes two transactions unless you replace it by nonce with the exact same nonce, and on Solana you cannot. |

## 5. When it is genuinely stuck

An EVM transaction pending long enough to block the nonce queue needs a **replacement
at the same nonce**, not a new send:

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main orders replace <order_id> --same-nonce --bump-gas
```

That is a cancel-by-replacement, and it is the only "send again" that is safe, because
the chain can only accept one of the two.

On Solana there is no nonce to replace. A transaction either lands inside its blockhash
window (about 60–90 seconds) or it is dead. Wait out the window, confirm it is dead,
then treat it as a new decision.

## 6. Never

- Never resubmit because "it has been a while".
- Never resubmit because the dashboard shows no position — the dashboard reads the
  database, and the database is exactly what is out of date during this incident.
- Never clear an UNKNOWN order by hand-editing the database. `orders` and `order_events`
  are the audit trail; a manual UPDATE makes the next reconcile lie to you.
- Never let Hermes resolve this. It is a typed MCP surface with no "resubmit" tool for
  good reason. If a model is asking to resend, the answer is reconcile.

## 7. Afterwards

```sh
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main journal add correction \
  "order <id> UNKNOWN for <n>m after <cause>; chain said <confirmed|dropped|pending>; \
   resolved by reconcile; no resubmit issued"
sudo /opt/kaiba/current/deploy/preflight.py
sudo systemctl start kaiba-engine
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main risk resume
```

If reconcile could **not** resolve it — the chain has no record and the nonce did not
move and hours have passed — leave the order UNKNOWN and leave entries paused on that
chain. An order you cannot resolve is a reason to stop trading that chain, not a reason
to guess.
