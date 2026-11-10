"""Shared bootstrap for the backup_docker_compose_projects test suite.

Puts the repository root on sys.path so the tests can import the script.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backup_docker_compose_projects as bdcp  # noqa: E402
