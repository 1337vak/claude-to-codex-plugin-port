#!/usr/bin/env python3
"""Run the CLI distributed alongside this skill."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from plugin_port.cli import main

main()
