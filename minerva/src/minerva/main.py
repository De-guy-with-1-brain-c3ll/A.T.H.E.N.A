"""Minerva entry point."""
from __future__ import annotations

import asyncio

from minerva.core import MinervaCoordinator


def main() -> int:
    coordinator = MinervaCoordinator()
    try:
        asyncio.run(coordinator.run())
    except KeyboardInterrupt:
        print("\nMinerva stopped.")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
