"""
ADK agent package for `google.adk.cli web` and mounted REST API.

Directory name = app name = `env_setup_validation`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `from backend...` when agents_dir is on sys.path (CLI adds parents).
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.validation_app import build_validation_app

app = build_validation_app()
