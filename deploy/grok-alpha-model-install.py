"""Update only owned Grok profile artifacts and prepare one paused model cron.

Claude's scoped agreement: docs/worklogs/claude-20261102.md. Never changes auth,
gateways, other jobs, lanes or signer policy. New code must already be committed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

SOURCES = {
    "deploy/grok-alpha-monitor.py": "scripts/kaiba-grok-alpha-monitor.py",
    "skills/grok-alpha-processing/SKILL.md": "skills/grok-alpha-processing/SKILL.md",
    "skills/kaiba-baw-mint/SKILL.md": "skills/kaiba-baw-mint/SKILL.md",
}
REMOTE = r'''
import base64,hashlib,json,os,subprocess,sys,time
from pathlib import Path
a=json.load(sys.stdin); root=Path('/home/ubuntu/kaiba')
profile=Path.home()/'.hermes/profiles/kaiba-operator'
def sha(raw):return hashlib.sha256(raw).hexdigest()
def cli(args):
 r=subprocess.run(['/home/ubuntu/.local/bin/hermes','--profile','kaiba-operator',*args],capture_output=True,text=True,timeout=20)
 if r.returncode:raise RuntimeError('supported_cli_failed:'+str(r.returncode))
 return r.stdout
job=a['job'];name=job['name'];assert name=='kaiba-grok-model-followup' and job['paused'] is True
before=cli(['cron','list']); exists=('Name:      '+name) in before
targets=[]
for rel,record in a['files'].items():
 raw=base64.b64decode(record['base64']);assert sha(raw)==record['sha256']
 for p in (root/rel,profile/record['profile_path']):
  if p.is_symlink() or (not p.is_file() and p!=root/rel):raise RuntimeError('owned_target_missing_or_symlink:'+str(p))
  actual=sha(p.read_bytes()) if p.is_file() else None
  if actual is not None and actual not in (record['old_sha256'],record['sha256']):raise RuntimeError('owned_source_changed:'+str(p))
  targets.append((p,raw,actual))
out={'commit':a['commit'],'apply':a['apply'],'cron_already_exists':exists,'checked_files':len(targets),'financial_actions':0,'provider_calls':0,'model_calls':0}
if not a['apply']:print(json.dumps(out));raise SystemExit(0)
for p,raw,expected in targets:
 actual=sha(p.read_bytes()) if p.is_file() else None
 if p.is_symlink() or actual!=expected:raise RuntimeError('concurrent_source_change')
 p.parent.mkdir(parents=True,exist_ok=True)
 temp=p.with_name(p.name+'.grok-model-partial')
 with temp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 temp.chmod(0o644);os.replace(temp,p)
if not exists:
 args=['cron','create',job['schedule'],job['prompt'],'--name',name,'--model',job['model'],
       '--provider',job['provider'],'--reasoning-effort',job['reasoning_effort'],'--deliver','local',
       '--workdir',str(root),'--monitor-script',job['monitor_script'],'--paused','--paused-reason',job['paused_reason']]
 for skill in job['skills']:args+=['--skill',skill]
 out['creation']=cli(args)
out['listing']=cli(['cron','list']);out['completed_ms']=int(time.time()*1000)
out['file_hashes']={str(p):sha(p.read_bytes()) for p,_,_ in targets}
print(json.dumps(out))
'''


def main():
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-commit", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    def git(*parts):
        return subprocess.check_output(["git", *parts], cwd=root)

    commit = git("rev-parse", "HEAD").decode().strip()
    files = {}
    for name, target in SOURCES.items():
        raw = git("show", f"{commit}:{name}")
        if raw != (root/name).read_bytes():
            raise SystemExit("uncommitted_source:" + name)
        files[name] = {"base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest(),
                       "old_sha256": hashlib.sha256(git("show", f"{args.from_commit}:{name}")).hexdigest(),
                       "profile_path": target}
    raw = git("show", f"{commit}:hermes/cron/jobs.yaml")
    if raw != (root/"hermes/cron/jobs.yaml").read_bytes():
        raise SystemExit("uncommitted_cron_spec")
    jobs = yaml.safe_load(raw)["jobs"]
    job, = [j for j in jobs if j["name"] == "kaiba-grok-model-followup"]
    payload = {"commit": commit, "files": files, "job": job, "apply": args.apply}
    program = "import base64;exec(base64.b64decode(" + repr(base64.b64encode(REMOTE.encode()).decode()) + "))"
    result = subprocess.run(["ssh", "-i", "C:/Users/<you>/.ssh/kaiba_ultimate_agent_ed25519", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10", "ubuntu@203.0.113.10", "timeout -k 2 55 nice -n 19 ionice -c3 "
        "/home/ubuntu/kaiba/.venv/bin/python -I -B -c " + shlex.quote(program)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60)
    if result.returncode:
        print(json.dumps({"installed": False, "exit": result.returncode, "detail": result.stderr[-1800:]}))
        return 1
    out = json.loads(result.stdout)
    folder = root/"data/codex-grok-alpha-20261102"
    receipt = folder/f"model-{'install' if args.apply else 'check'}-{commit[:12]}.json"
    with receipt.open("x", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    # Full cron listing includes unrelated operational data; preserve it privately.
    print(json.dumps({k: v for k, v in out.items() if k != "listing"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
