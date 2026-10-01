# Runbook: keys and credentials

**Question this answers:** where does every secret live, who can read it, and how do I
change it without breaking anything or leaking it?

**Two categories, and they are not handled the same way.**

| | Provider credentials | Wallet keys |
|---|---|---|
| Where | `/etc/kaiba/*.env`, root-owned | `/etc/kaiba/signer/keys`, kaiba-signer 0700 |
| Who reads them | systemd (as root), then the service | the signer process, nobody else |
| Rotation | `deploy/rotate-keys.sh` | generate a new wallet, migrate funds, retire the old |
| Backed up | **no** — rotated, not restored | **no** — see below |
| If leaked | revoke at the provider, rotate | the funds are gone; assume it immediately |

---

## The layout

```
/etc/kaiba                    root:kaiba 0750     traverse only
  core.env                    root:kaiba-core   0640   provider + chain credentials
  hermes.env                  root:kaiba-agent  0640   telegram token only
  dashboard.env               root:kaiba-dash   0640   console password only
  signer.env                  root:kaiba-signer 0640   keystore passphrase source
  deploy.env                  root:root         0600   backup recipients, RPC allow list
  signer/keys/                kaiba-signer      0700   the wallet keystore
  policy/signer-policy.yaml   root:root         0644   read-only to everyone, including the agent
  fingerprints/current.tsv    root:kaiba-core   0640   sha256 only, never a value
```

Two properties worth stating plainly:

- **systemd reads `EnvironmentFile=` as root**, before dropping privileges. The service
  user does not need read permission on its own credentials, and the group read above is
  a convenience for `kaiba probe`, not a requirement.
- **`kaiba-agent` — the identity Hermes runs as — is in none of those file groups**, and
  `kaiba-hermes.service` lists `/etc/kaiba/signer` and `/etc/kaiba/core.env` under
  `InaccessiblePaths`. The model context never has a credential in its environment
  (`UnsetEnvironment` for every secret name) and cannot read one off disk.

Verify it rather than believing it:

```sh
sudo -u kaiba-agent test -r /etc/kaiba/core.env && echo LEAK || echo ok
sudo -u kaiba-agent test -r /etc/kaiba/signer   && echo LEAK || echo ok
sudo /opt/kaiba/current/deploy/preflight.py --quiet     # the `layout` check does this
```

---

## Rotating a provider credential

Everything in `docs/research/01-prior-work-inventory.md` §8 must be treated as exposed:
those keys sat in plain-text `.txt` files for months. Rotate all of them once, then
rotate on a schedule.

```sh
sudo deploy/rotate-keys.sh --list          # what is rotatable and where to regenerate it
sudo deploy/rotate-keys.sh GMGN_API_KEY    # one at a time
sudo deploy/rotate-keys.sh --all           # or the lot
sudo deploy/rotate-keys.sh --verify        # fingerprints of what is installed
```

The order matters: **regenerate and revoke in the provider's dashboard first**, then
install the new value here. A key that still works in two places is duplicated, not
rotated.

The script never prints a value — not the old one, not the new one, not a prefix. It
records a sha256 fingerprint in `/etc/kaiba/fingerprints/current.tsv` and a `change`
entry in the hash-chained journal. That is enough to answer "is the box running the key
I made on Tuesday?" and useless to anyone reading over your shoulder.

**Restart what reads it.** Environment files are read at service start:

```sh
sudo systemctl restart kaiba-ingest kaiba-ops kaiba-scan kaiba-engine kaiba-mcp   # core.env
sudo systemctl restart kaiba-hermes                          # hermes.env
sudo systemctl restart kaiba-dashboard                       # dashboard.env
sudo systemctl restart kaiba-signer                          # signer.env
sudo -u kaiba-core /opt/kaiba/venv/bin/python -m kaiba.cli.main probe
```

---

## Wallet keys

### Generating

On the box, by the signer, never imported from a laptop:

```sh
sudo systemctl start kaiba-signer
sudo -u kaiba-signer /opt/kaiba/venv/bin/python -m kaiba.cli.main signer keygen --chain sol
```

The private key never leaves the signer process. The command prints an address; that is
all you get, and all you need for `chains.<chain>.wallet` in `risk.yaml`.

### Backups do not contain them

`deploy/backup.sh` explicitly excludes `/etc/kaiba/signer/**`, and
`kaiba-backup.service` lists `/etc/kaiba/signer/keys` under `InaccessiblePaths` so the
backup process could not read them even if the script tried. A nightly encrypted copy of
your wallet, pushed to object storage, is a second wallet with worse access control.

The consequence, stated so it is not a surprise later: **rebuilding this host means new
wallets.** Plan the migration, do not plan the restore.

### Rotating the keystore passphrase

```sh
sudo deploy/rotate-keys.sh KAIBA_SIGNER_PASSPHRASE
sudo systemctl restart kaiba-signer
sudo -u kaiba-signer /opt/kaiba/venv/bin/python -m kaiba.cli.main signer rekey
```

Order matters and the failure mode is total: **keep the old passphrase until `rekey`
reports success.** Between the rotation and the rekey, the keystore is still encrypted
with the old one. Losing it there loses the wallets.

### Rotating a wallet

There is no in-place wallet rotation, because there is no withdrawal path — by design
(PLAN §1, §6.4). Moving funds between wallets is a thing *you* do, with your own wallet
software, not something the agent can be asked to do:

1. Flatten the book on that chain: `kaiba risk reduce-only --on`, wait.
2. Generate the new wallet with `signer keygen`.
3. Move the funds yourself, outside the agent.
4. Update `chains.<chain>.wallet` and, for GMGN, rebind the key in the portal.
5. `sudo deploy/preflight.py` — the `chains bound and funded` check is what catches a
   half-finished rotation.
6. Retire the old key: `signer retire --address <old>`.

---

## Backup encryption keys

`deploy/backup.sh` encrypts to a **public** recipient. The private half lives on your
laptop and in a password manager, never on the VPS, which is why a compromised host
yields ciphertext.

```sh
# on your laptop, once
age-keygen -o kaiba-backup.identity        # keep this
grep 'public key' kaiba-backup.identity    # this goes in /etc/kaiba/deploy.env
```

```sh
# /etc/kaiba/deploy.env
AGE_RECIPIENTS=age1...              # public, safe to have on the box
KAIBA_BACKUP_KEEP_DAYS=14
RCLONE_REMOTE=                      # optional offsite
```

Test the restore path before you need it. A backup you have never restored is a belief,
not a backup:

```sh
sudo deploy/restore.sh --archive /var/lib/kaiba/backups/kaiba-<stamp>.tar.age \
     --identity /root/kaiba-backup.identity --into /var/lib/kaiba/restore-test
```

That verifies the manifest digests, `PRAGMA integrity_check`, and the journal hash chain,
and refuses to exit 0 if the chain is broken. It does not touch the live installation
without `--activate`.

The archive keeps `/etc/kaiba/config`, `/etc/kaiba/policy`, and the credential
fingerprints in separate paths. This matters during activation: `schedule.yaml` and the
risk envelope return to `config/`, while signer policy and gates return to `policy/`.
Older archives that merged those files are still classified by filename during restore.

---

## If you think something leaked

In this order:

1. **Kill the agent's ability to act:** `kaiba risk kill`.
2. **Revoke at the provider.** Not rotate — revoke. Rotation leaves the old one valid
   until you remember to remove it.
3. **If a wallet key is involved, assume the funds are gone** and move anything
   remaining out yourself, now, with your own wallet software. Do not wait to
   investigate first.
4. Rotate everything that shared a file with the leaked credential, on the assumption
   that whatever read one read the file.
5. `journal add correction` with what leaked, how, and the window. Never the value.
6. Then investigate.
