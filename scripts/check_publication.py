"""Fail on obvious secrets and private artifacts; report paths, never matched values."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [r'\bsk-(?:or-v1-)?[A-Za-z0-9_-]{24,}', r'\bgh[pousr]_[A-Za-z0-9]{30,}', r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', r'\bAKIA[A-Z0-9]{16}\b']


def check():
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT, capture_output=True, check=True)
    paths = [Path(p) for p in result.stdout.decode().split('\0') if p]
    failures = []
    for path in paths:
        if path.name.startswith('.env') and path.name != '.env.example':
            failures.append(f'{path}: private environment file')
        if set(path.parts) & {'var', '__pycache__', '.venv', 'node_modules', 'logs', 'results'}:
            failures.append(f'{path}: runtime artifact')
        raw = (ROOT / path).read_bytes()
        if len(raw) > 2*1024*1024:
            failures.append(f'{path}: unexpected large file')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if path.as_posix() != 'scripts/check_publication.py' and any(re.search(pattern, text) for pattern in PATTERNS):
            failures.append(f'{path}: possible secret')
        if path.as_posix() != 'scripts/check_publication.py' and re.search(r'(?<![A-Za-z0-9:/])/(?:Users|home)/[^/\s]+/', text):
            failures.append(f'{path}: machine-specific home path')
    if failures:
        raise SystemExit('\n'.join(failures))
    print(f'Checked {len(paths)} tracked and non-ignored files: no obvious secrets or private runtime artifacts.')


if __name__ == '__main__':
    check()
