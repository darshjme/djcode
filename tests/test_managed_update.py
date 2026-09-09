"""Updater acceptance with real release directories and atomic symlinks, no installs."""
import hashlib
import json
import subprocess

import httpx
import pytest
from djcode import managed_update as updater
from djcode import config


@pytest.fixture
def managed(monkeypatch, tmp_path):
    prefix = tmp_path / 'managed'
    prefix.mkdir()
    def release(name, commit):
        directory = prefix / ('release.' + name)
        (directory / 'venv').mkdir(parents=True)
        receipt = {'prefix': str(prefix), 'repository': updater.REPOSITORY, 'commit': commit, 'version': '4.1.0'}
        (directory / '.djcode-install.json').write_text(json.dumps(receipt))
        return directory
    old = release('old', 'a' * 40)
    previous = release('previous', 'b' * 40)
    (prefix / 'current').symlink_to(old)
    (prefix / 'previous').symlink_to(previous)
    monkeypatch.setattr(updater.sys, 'prefix', str(old / 'venv'))
    monkeypatch.setattr(config, 'load_config', lambda: {'update_mode': 'auto'})
    monkeypatch.delenv('DJCODE_NO_UPDATE_CHECK', raising=False)
    modes = []
    monkeypatch.setattr(config, 'set_value', lambda *args: modes.append(args))
    return prefix, old, previous, release, modes


def manifest():
    commit = 'c' * 40
    return {'schema': 1, 'repository': updater.REPOSITORY, 'branch': 'main', 'commit': commit, 'version': '4.2.0', 'run_id': 17, 'sha256': hashlib.sha256(b'wheel').hexdigest(), 'wheel_url': f'https://github.com/{updater.REPOSITORY}/releases/download/build-{commit[:12]}/djcode-4.2.0-py3-none-any.whl'}


def test_update_and_stale_process_is_current(managed, monkeypatch):
    prefix, old, previous, release, _ = managed
    new = release('new', 'c' * 40)
    monkeypatch.setattr(updater, 'verified_manifest', lambda client: manifest())
    staged = []
    monkeypatch.setattr(updater, 'stage_build', lambda *args: staged.append(True) or new)
    monkeypatch.setattr(updater, 'run', lambda *a, **kw: 'djcode, version 4.2.0')
    assert updater.perform_update()['updated']
    assert (prefix / 'current').resolve() == new
    assert (prefix / 'previous').resolve() == old
    assert previous.exists()
    assert updater.perform_update()['status'] == 'current'
    assert staged == [True]


def test_update_staging_failure_keeps_links_and_sanitizes_error(managed, monkeypatch):
    prefix, old, previous, _, _ = managed
    monkeypatch.setattr(updater, 'verified_manifest', lambda client: manifest())
    def fail(*args):
        raise RuntimeError('secret-token-must-not-appear')
    monkeypatch.setattr(updater, 'stage_build', fail)
    answer = updater.perform_update()
    assert not answer['ok'] and 'secret-token' not in answer['message']
    assert (prefix / 'current').resolve() == old
    assert (prefix / 'previous').resolve() == previous


def test_update_activation_failure_restores_current(managed, monkeypatch):
    prefix, old, _, release, _ = managed
    new = release('new', 'c' * 40)
    monkeypatch.setattr(updater, 'verified_manifest', lambda client: manifest())
    monkeypatch.setattr(updater, 'stage_build', lambda *args: new)
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('validation', 1)
    monkeypatch.setattr(updater, 'run', fail)
    assert not updater.perform_update()['ok']
    assert (prefix / 'current').resolve() == old


def test_rollback_and_partial_failure(managed, monkeypatch):
    prefix, old, previous, _, modes = managed
    link = updater.atomic_link
    def fail(target, path):
        if path.name == 'previous':
            raise OSError('fixture failure')
        link(target, path)
    monkeypatch.setattr(updater, 'atomic_link', fail)
    assert not updater.rollback()['ok']
    assert (prefix / 'current').resolve() == old
    assert (prefix / 'previous').resolve() == previous
    monkeypatch.setattr(updater, 'atomic_link', link)
    assert updater.rollback()['updated']
    assert (prefix / 'current').resolve() == previous
    assert (prefix / 'previous').resolve() == old
    assert modes[-1] == ('update_mode', 'manual')


def test_both_operations_respect_lock(managed):
    import fcntl
    prefix, old, previous, _, _ = managed
    with (prefix / '.update.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert updater.perform_update()['status'] == 'busy'
        assert updater.rollback()['status'] == 'busy'
    assert (prefix / 'current').resolve() == old
    assert (prefix / 'previous').resolve() == previous


def test_unmanaged_and_offline_preserve_install(managed, monkeypatch):
    prefix, old, previous, _, _ = managed
    def offline(*args):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(updater, 'verified_manifest', offline)
    assert updater.perform_update()['status'] == 'unavailable'
    monkeypatch.setattr(updater.sys, 'prefix', str(prefix / 'developer'))
    assert updater.perform_update()['status'] == 'manual_required'
    assert (prefix / 'current').resolve() == old
    assert (prefix / 'previous').resolve() == previous


@pytest.mark.parametrize('field,value', [('schema',True), ('repository','other/repo'), ('wheel_url','https://evil.invalid/code.whl'), ('commit','main'), ('commit',17), ('version',None), ('sha256',None), ('run_id',True)])
def test_manifest_rejects_wrong_origin_and_types(field, value):
    data = manifest()
    data[field] = value
    with pytest.raises(ValueError):
        updater.validate_manifest(data)


@pytest.mark.parametrize('field,value', [('head_sha','a'*40), ('head_branch','feature'), ('conclusion','failure'), ('event','pull_request'), ('path','.github/workflows/other.yml'), ('head_repository', {'full_name':'other/repo'})])
def test_ci_identity_is_verified(field, value):
    data = manifest()
    run = {'head_sha':data['commit'], 'head_branch':'main', 'conclusion':'success', 'status':'completed', 'event':'push', 'path':'.github/workflows/ci.yml', 'head_repository':{'full_name':updater.REPOSITORY}}
    run[field] = value
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=data if str(req.url) == updater.MANIFEST_URL else run))
    with httpx.Client(transport=transport) as client, pytest.raises(ValueError):
        updater.verified_manifest(client)


def test_stage_checksum_failure_only_removes_new_directory(managed):
    prefix, old, previous, _, _ = managed
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'wrong'))) as client:
        with pytest.raises(ValueError, match='checksum'):
            updater.stage_build(prefix, updater.installation()[1], manifest(), client)
    assert sorted(p.name for p in prefix.glob('release.*')) == sorted([old.name, previous.name])


def test_malformed_receipt_is_unmanaged(managed):
    _, old, _, _, _ = managed
    (old / '.djcode-install.json').write_text('[]')
    assert updater.installation() is None


@pytest.mark.parametrize('payload', [[], {'prefix': '/unrelated', 'repository': updater.REPOSITORY}])
def test_bad_current_receipt_preserves_links(managed, monkeypatch, payload):
    prefix, old, previous, _, _ = managed
    # Keep the running release valid while testing a separately selected current release.
    selected = prefix / 'release.selected'
    selected.mkdir()
    (selected / '.djcode-install.json').write_text(json.dumps(payload))
    updater.atomic_link(selected, prefix / 'current')
    monkeypatch.setattr(updater, 'verified_manifest', lambda client: manifest())
    assert updater.perform_update()['status'] == 'unavailable'
    assert (prefix / 'current').resolve() == selected
    assert (prefix / 'previous').resolve() == previous


def test_rollback_rejects_outside_target(managed, tmp_path):
    prefix, old, _, _, _ = managed
    outside = tmp_path / 'release.outside'
    outside.mkdir()
    (outside / '.djcode-install.json').write_text(json.dumps({'repository': updater.REPOSITORY, 'prefix': str(prefix)}))
    updater.atomic_link(outside, prefix / 'previous')
    assert updater.rollback()['status'] == 'unavailable'
    assert (prefix / 'current').resolve() == old


def test_metadata_deadline_and_shape_close_stream(monkeypatch):
    closed = []
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{}'
        def close(self):
            closed.append(True)
    times = iter([0, 9])
    monkeypatch.setattr(updater.time, 'monotonic', lambda: next(times))
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream()))) as client:
        with pytest.raises(ValueError, match='time/size'):
            updater.fetch_json(client, updater.MANIFEST_URL)
    assert closed == [True]


def test_metadata_non_object_rejected():
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[]))) as client:
        with pytest.raises(ValueError, match='object'):
            updater.fetch_json(client, updater.MANIFEST_URL)


def test_staging_failure_after_download_cleans_only_staged_build(managed, monkeypatch):
    prefix, old, previous, _, _ = managed
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('fixture install', 1)
    monkeypatch.setattr(updater, 'run', fail)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'wheel'))) as client:
        with pytest.raises(subprocess.TimeoutExpired):
            updater.stage_build(prefix, updater.installation()[1], manifest(), client)
    assert sorted(p.name for p in prefix.glob('release.*')) == sorted([old.name, previous.name])
