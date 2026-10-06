"""Check locally built release artifacts against the current checkout."""

import hashlib
import json
import sys
import tomllib
import tarfile
import zipfile
from pathlib import Path

if len(sys.argv) != 3:
    raise SystemExit("Usage: check_distribution.py WHEEL SDIST")
repo = Path(__file__).resolve().parents[1]
version = tomllib.loads((repo / 'pyproject.toml').read_text())['project']['version']
wheel, sdist = map(Path, sys.argv[1:])
required = [
    'djcode/daf_engine/Cargo.lock',
    'djcode/daf_engine/Cargo.toml',
    'djcode/daf_engine/NOTICE',
    'djcode/daf_engine/crates/daf-ddal/tests/wire_contract.rs',
]
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
    assert set(required) <= names, set(required) - names
    assert archive.read('djcode/daf_engine/NOTICE') == (repo / 'src/djcode/daf_engine/NOTICE').read_bytes()
    assert not any('/target/' in name or '/.venv/' in name for name in names)
    checked = []
    for path in (repo / 'src/djcode').rglob('*'):
        if not path.is_file() or path.suffix not in {'.py', '.rs', '.toml', '.lock'}:
            continue
        if any(part in {'target', '__pycache__', '.venv'} for part in path.parts):
            continue
        relative = path.relative_to(repo / 'src').as_posix()
        assert relative in names, relative
        assert archive.read(relative) == path.read_bytes(), relative
        checked.append(relative)
with tarfile.open(sdist) as archive:
    names = set(archive.getnames())
    for relative in [f'src/{item}' for item in checked] + ['src/djcode/daf_engine/NOTICE']:
        name = f'djcode-{version}/' + relative
        assert name in names, relative
        assert archive.extractfile(name).read() == (repo / relative).read_bytes(), relative
    docs = [path.relative_to(repo).as_posix() for path in (repo / 'docs').rglob('*')
            if path.is_file() and path.suffix in {'.md', '.svg'}]
    for relative in docs + ['README.md', 'CHANGELOG.md', 'LICENSE', 'pyproject.toml', 'uv.lock']:
        name = f'djcode-{version}/' + relative
        assert name in names, relative
        assert archive.extractfile(name).read() == (repo / relative).read_bytes(), relative
    assert not any('/target/' in name or '/.venv/' in name for name in names)
print(json.dumps({
    'wheel_current_source_files_verified': len(checked),
    'sdist_current_source_files_verified': len(checked),
    'sdist_documentation_files_verified': len(docs),
    'bundled_locked_engine_and_notice': True,
    'sdist_guides_and_captures_match': True,
    'build_and_virtualenv_directories_excluded': True,
    'artifacts': [{
        'name': path.name,
        'bytes': path.stat().st_size,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    } for path in (wheel, sdist)],
}, indent=2))
