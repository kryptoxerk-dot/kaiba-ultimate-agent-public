"""Hermes monitor for the analyst desk: settle what is due, and wake the model only for a new batch.

Installed as ``scripts/kaiba-desk-monitor.py`` by deploy/desk-model-install.py. Hermes hashes the exact
bytes printed here: unchanged bytes suppress the model run, so an empty queue costs nothing. A failure
exits non-zero, which Hermes reports as an error and never as a change. No provider, RPC or GMGN call:
the child reads files and the database through GuardedReader, under the DATABASE READ RULE's limits.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/ubuntu/kaiba")
TIMEOUT_S = 100


def command():
    return ["timeout", "-k", "2", str(TIMEOUT_S), "nice", "-n", "19", "ionice", "-c3",
            str(ROOT/".venv/bin/python"), "-m", "kaiba.learning.desk", "monitor"]


def main():
    result = subprocess.run(command(), cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT_S + 8)
    if result.returncode:
        sys.stderr.write(result.stderr[-800:])
        return result.returncode
    sys.stdout.write(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
