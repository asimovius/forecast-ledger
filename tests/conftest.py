"""Pytest discovery for the ForecastLedger MCP server tests.

Adds the server directory to sys.path so ``import mcp_server`` works
regardless of how pytest was invoked.
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SERVER_DIR = REPO_ROOT / "server"

for entry in (str(SERVER_DIR), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)