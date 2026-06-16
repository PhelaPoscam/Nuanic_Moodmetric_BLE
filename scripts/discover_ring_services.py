#!/usr/bin/env python3
"""Ring diagnostics CLI — thin wrapper."""

import asyncio
import sys

from nuanic_ring.discover_services import main

if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted by user")
        raise SystemExit(1)
