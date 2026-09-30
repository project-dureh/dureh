#!/usr/bin/env python3
"""DUREH command-line entry point. Uses only the Python standard library."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dureh_core.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
