"""Produce a static preview that never attempts to call a generation backend."""
import json
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
dist = root / 'dist'
if dist.exists():
    shutil.rmtree(dist)
shutil.copytree(root / 'web', dist)
(dist / 'site-config.json').write_text(json.dumps({'api': False}) + '\n')
print(f'Static preview built: {dist}')
