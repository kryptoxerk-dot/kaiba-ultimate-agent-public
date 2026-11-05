"""Execute the profile updater against isolated files and a simulated Hermes CLI."""
import base64
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest


def setup(tmp_path, monkeypatch, *, apply=False):
    spec = importlib.util.spec_from_file_location('grok_model_install', Path(__file__).parents[1]/'deploy/grok-alpha-model-install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, home = tmp_path/'kaiba', tmp_path/'home'
    profile = home/'.hermes/profiles/kaiba-operator'
    files = {}
    for rel, destination in module.SOURCES.items():
        old, raw = b'old committed bytes', ('new committed ' + rel).encode()
        files[rel] = {'base64': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest(),
                      'old_sha256': hashlib.sha256(old).hexdigest(), 'profile_path': destination}
        for path in (root/rel, profile/destination):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(old)
    job = {'name': 'kaiba-grok-model-followup', 'paused': True, 'paused_reason': 'Model credentials unavailable',
           'prompt': 'Verify only model handoffs', 'schedule': 'every 5m', 'model': 'existing-model',
           'provider': 'anthropic', 'reasoning_effort': 'low', 'monitor_script': 'kaiba-grok-alpha-monitor.py',
           'skills': ['grok-alpha-processing']}
    data = {'files': files, 'job': job, 'apply': apply, 'commit': 'fixture'}
    code = module.REMOTE.replace("Path('/home/ubuntu/kaiba')", f'Path({str(root)!r})')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(data)))
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='existing unrelated job\n', stderr='')

    monkeypatch.setattr(subprocess, 'run', run)
    return code, data, root, profile, commands


def test_check_is_readonly_and_profile_drift_aborts_before_any_write(tmp_path, monkeypatch, capsys):
    code, data, root, profile, commands = setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        exec(compile(code, 'profile_updater', 'exec'), {})
    assert stopped.value.code == 0
    assert json.loads(capsys.readouterr().out)['checked_files'] == 6
    assert all(p.read_bytes() == b'old committed bytes' for p in root.rglob('*') if p.is_file())
    (profile/'skills/kaiba-baw-mint/SKILL.md').write_text('other agent edit')
    data['apply'] = True
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(data)))
    with pytest.raises(RuntimeError, match='owned_source_changed'):
        exec(compile(code, 'profile_updater', 'exec'), {})
    assert all(p.read_bytes() == b'old committed bytes' for p in root.rglob('*') if p.is_file())
    assert not any('create' in c for c in commands)


def test_apply_creates_only_paused_local_monitored_job_and_preserves_other_files(tmp_path, monkeypatch, capsys):
    code, data, root, profile, commands = setup(tmp_path, monkeypatch, apply=True)
    # A committed skill may exist only in the profile from an earlier installer.
    (root/'skills/kaiba-baw-mint/SKILL.md').unlink()
    auth = profile/'auth.json'
    auth.write_text('fixture authentication stays unchanged')
    exec(compile(code, 'profile_updater', 'exec'), {})
    result = json.loads(capsys.readouterr().out)
    created, = [c for c in commands if 'create' in c]
    assert '--paused' in created and created[created.index('--deliver')+1] == 'local'
    assert created[created.index('--monitor-script')+1] == 'kaiba-grok-alpha-monitor.py'
    assert not any('auth' in c or 'restart' in c or 'edit' in c or 'resume' in c for c in commands)
    assert auth.read_text() == 'fixture authentication stays unchanged'
    assert result['model_calls'] == 0 and result['provider_calls'] == 0
    for name, record in data['files'].items():
        assert (root/name).read_bytes() == (profile/record['profile_path']).read_bytes()
