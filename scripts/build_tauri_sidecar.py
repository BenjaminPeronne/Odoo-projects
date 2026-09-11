#!/usr/bin/env python3
"""Compatibility entry point; desktop builds now use Electron."""
from build_electron_sidecar import main

if __name__ == "__main__":
    print("Le backend est désormais construit pour Electron (build_electron_sidecar.py).")
    main()
