"""Install only committed signed-intake files; preserve existing services/config.

Run locally with --public-key (PUBLIC hex only), then --apply after checks.
No wallet credentials, provider requests, financial actions or Hermes restart.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

FILES = (
    "kaiba/hunters/grok_alpha_bridge.py", "deploy/grok-alpha-monitor.py",
    "deploy/systemd/kaiba-grok-alpha.service", "deploy/grok-alpha.caddy",
    "skills/grok-alpha-processing/SKILL.md",
)
EXPECTED_CADDY = "f476f9e062d249fcca0cda52c81fee3079c127c15effeb44a7a4969f236d4e79"
REMOTE = r'''
import base64,hashlib,json,os,subprocess,sys,time
from pathlib import Path
import cryptography
a=json.load(sys.stdin);root=Path('/home/ubuntu/kaiba');home=Path.home()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def command(args,timeout=15):
 r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 if r.returncode:raise RuntimeError('command_failed:'+args[0]+':'+str(r.returncode))
 return r.stdout
def pids():
 out={}
 for u in ('hermes-gateway-kaiba-operator.service','hermes-gateway-kaiba-research.service','hermes-gateway-kaiba-reflect.service','kaiba-tweet-launch.service','kaiba-protection.service'):
  out[u]=command(['systemctl','--user','show',u,'--property=MainPID','--value']).strip()
 return out
def config_hashes():
 return {n:sha((root/n).read_bytes()) for n in ('config/risk.yaml','config/signer-policy.yaml','config/schedule.yaml')}
caddy=Path('/etc/caddy/Caddyfile');original=caddy.read_bytes()
if caddy.is_symlink():raise RuntimeError('caddy_symlink_refused')
caddy_stat=caddy.stat()
marker=b'    import /etc/caddy/grok-alpha.caddy\n'
already=marker in original
if not already and sha(original)!=a['expected_caddy']:raise RuntimeError('caddy_source_changed')
expected=b'agent.example.com {\n    encode gzip\n    reverse_proxy 127.0.0.1:8794\n}'
replacement=expected.replace(b'    reverse_proxy',marker+b'    reverse_proxy',1)
if not already and original.count(expected)!=1:raise RuntimeError('agent_route_shape_changed')
updated=original if already else original.replace(expected,replacement,1)
for rel,record in a['files'].items():
 raw=base64.b64decode(record['base64']);assert sha(raw)==record['sha256']
 p=root/rel
 if p.exists() and p.read_bytes()!=raw:
  permitted=(rel=='deploy/systemd/kaiba-grok-alpha.service' and a.get('prior_unit_sha')==sha(p.read_bytes()))
  if not permitted:raise RuntimeError('existing_owned_source_differs:'+rel)
settings_path=home/'.config/kaiba/grok-alpha-worker.json'
settings={'port':17876,'public_key_hex':a['public_key']}
if settings_path.exists() and json.loads(settings_path.read_text())!=settings:raise RuntimeError('existing_peer_configuration_differs')
out={'commit':a['commit'],'check_passed':True,'apply':a['apply'],'cryptography':cryptography.__version__,
     'gateway_and_protection_pids_before':pids(),'financial_config_before':config_hashes(),
     'caddy_before_sha256':sha(original),'source_hashes':{n:v['sha256'] for n,v in a['files'].items()},
     'financial_actions':0,'provider_calls':0,'credential_values_exported':False}
if not a['apply']:print(json.dumps(out));raise SystemExit(0)
scratch=root/'data/codex-grok-alpha-install';scratch.mkdir(mode=0o700,parents=True,exist_ok=True)
def put(p,raw,mode):
 p.parent.mkdir(parents=True,exist_ok=True)
 temp=p.with_name(p.name+'.grok-partial')
 if temp.exists():raise RuntimeError('abandoned_partial_inspect:'+str(p))
 with temp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 temp.chmod(mode);os.replace(temp,p)
for rel,record in a['files'].items():put(root/rel,base64.b64decode(record['base64']),0o644)
data=root/'data/grok-alpha';data.mkdir(mode=0o700,parents=True,exist_ok=True);data.chmod(0o700)
settings_path.parent.mkdir(parents=True,exist_ok=True)
put(settings_path,json.dumps(settings).encode(),0o600)
unit=home/'.config/systemd/user/kaiba-grok-alpha.service'
raw=(root/'deploy/systemd/kaiba-grok-alpha.service').read_bytes()
if unit.exists() and unit.read_bytes()!=raw and a.get('prior_unit_sha')!=sha(unit.read_bytes()):raise RuntimeError('unit_source_changed')
put(unit,raw,0o644)
monitor=home/'.hermes/profiles/kaiba-operator/scripts/kaiba-grok-alpha-monitor.py'
raw=(root/'deploy/grok-alpha-monitor.py').read_bytes()
if monitor.exists() and monitor.read_bytes()!=raw:raise RuntimeError('profile_monitor_exists_different')
put(monitor,raw,0o644)
skill=home/'.hermes/profiles/kaiba-operator/skills/grok-alpha-processing/SKILL.md'
raw=(root/'skills/grok-alpha-processing/SKILL.md').read_bytes()
if skill.exists() and skill.read_bytes()!=raw:raise RuntimeError('profile_skill_exists_different')
put(skill,raw,0o644)
snippet=Path('/etc/caddy/grok-alpha.caddy')
snippet_sha=sha((root/'deploy/grok-alpha.caddy').read_bytes())
if snippet.exists() and sha(snippet.read_bytes())!=snippet_sha:raise RuntimeError('existing_proxy_snippet_differs')
command(['sudo','-n','install','-m','0644','-o','root','-g','root',str(root/'deploy/grok-alpha.caddy'),str(snippet)])
candidate=scratch/'Caddyfile.candidate';put(candidate,updated,0o600)
command(['sudo','-n','caddy','validate','--config',str(candidate),'--adapter','caddyfile'],30)
backup=scratch/('Caddyfile.before-'+sha(original))
if not backup.exists():put(backup,original,0o600)
if caddy.read_bytes()!=original:raise RuntimeError('caddy_concurrent_change')
def replace_caddy(content):
 staged=scratch/'Caddyfile.apply';put(staged,content,0o600)
 target=caddy.with_name('Caddyfile.grok-partial')
 if target.exists():raise RuntimeError('caddy_partial_exists')
 command(['sudo','-n','install','-m','0664','-o',str(caddy_stat.st_uid),'-g',str(caddy_stat.st_gid),str(staged),str(target)])
 command(['sudo','-n','mv','-T',str(target),str(caddy)])
if not already:replace_caddy(updated)
try:
 command(['systemctl','--user','daemon-reload'])
 command(['systemctl','--user','enable','--now','kaiba-grok-alpha.service'])
 if a.get('prior_unit_sha'):command(['systemctl','--user','restart','kaiba-grok-alpha.service'])
 command(['sudo','-n','systemctl','reload','caddy'],30)
except BaseException:
 if not already:replace_caddy(original);command(['sudo','-n','systemctl','reload','caddy'],30)
 command(['systemctl','--user','stop','kaiba-grok-alpha.service'])
 raise
out['unit']=command(['systemctl','--user','show','kaiba-grok-alpha.service','--property=ActiveState,SubState,MainPID']).splitlines()
if 'ActiveState=active' not in out['unit'] or 'SubState=running' not in out['unit']:raise RuntimeError('new_service_not_running')
out['gateway_and_protection_pids_after']=pids();out['financial_config_after']=config_hashes()
if out['gateway_and_protection_pids_before']!=out['gateway_and_protection_pids_after']:raise RuntimeError('existing_pid_changed')
if out['financial_config_before']!=out['financial_config_after']:raise RuntimeError('financial_config_changed')
out['caddy_after_sha256']=sha(caddy.read_bytes());out['completed_ms']=int(time.time()*1000)
print(json.dumps(out))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--repair-unit-from", help="Exact prior committed unit SHA256; permits only that file's known repair")
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{64}", args.public_key) is None:
        raise SystemExit("public_key_invalid")
    if args.repair_unit_from is not None and re.fullmatch(r"[0-9a-f]{64}", args.repair_unit_from) is None:
        raise SystemExit("prior_unit_sha_invalid")
    root = Path(__file__).resolve().parents[1]
    def git(*parts):
        return subprocess.check_output(["git", *parts], cwd=root)
    commit = git("rev-parse", "HEAD").decode().strip()
    files = {}
    for name in FILES:
        raw = git("show", f"{commit}:{name}")
        if raw != (root/name).read_bytes():
            raise SystemExit("uncommitted_owned_source:"+name)
        files[name] = {"base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}
    payload = {"commit": commit, "files": files, "public_key": args.public_key,
               "apply": args.apply, "expected_caddy": EXPECTED_CADDY, "prior_unit_sha": args.repair_unit_from}
    # A fixed program, with JSON on stdin: never interpolate paths or content into a shell.
    program = "import base64;exec(base64.b64decode(" + repr(base64.b64encode(REMOTE.encode()).decode()) + "))"
    result = subprocess.run(["ssh", "-i", "C:/Users/<you>/.ssh/kaiba_ultimate_agent_ed25519",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "ubuntu@203.0.113.10",
             "timeout -k 2 110 nice -n 19 ionice -c3 /home/ubuntu/kaiba/.venv/bin/python -I -B -c "+shlex.quote(program)],
             input=json.dumps(payload), capture_output=True, text=True, timeout=120)
    if result.returncode:
        print(json.dumps({"installed": False, "exit": result.returncode, "reason": "remote_install_failed"}))
        return 1
    out = json.loads(result.stdout)
    folder = root/"data/codex-grok-alpha-20261102"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder/(("install-receipt-" if args.apply else "install-check-")+commit[:12]+".json")
    with dest.open("x", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
