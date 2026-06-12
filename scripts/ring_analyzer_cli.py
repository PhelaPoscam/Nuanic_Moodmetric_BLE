#!/usr/bin/env python3
"""Ring data analyzer CLI - Wrapper."""

import sys
from nuanic_ring.cli import ring_analyzer

if __name__ == "__main__":
    sys.exit(ring_analyzer())
