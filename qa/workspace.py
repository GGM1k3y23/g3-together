"""Where the private workspace lives: test copies of the game, isolation receipts, builds, run logs.

It is never part of this repository. Default: a sibling folder `g3-workspace` next to this repository;
override with the environment variable G3_WORKSPACE.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = Path(os.environ.get('G3_WORKSPACE') or REPO.parent/'g3-workspace').resolve()
if not (WORKSPACE/'project.json').is_file():
    raise SystemExit(f'No workspace at {WORKSPACE} (project.json missing). Set G3_WORKSPACE.')
