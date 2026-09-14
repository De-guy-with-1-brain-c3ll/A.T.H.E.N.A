"""Switch the Orange Pi back to an already installed ATHENA release."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0, "/opt/athena")
from update_client import restart_service, switch_link


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--service", default="athena-feishu.service")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.version):
        raise ValueError("Invalid release version.")
    release = Path("/opt/athena/releases") / args.version
    if not release.is_dir() or release.is_symlink():
        raise ValueError("That installed release does not exist.")
    switch_link(Path("/opt/athena/current"), release.resolve())
    restart_service(args.service)
    print(f"ATHENA rolled back to {args.version}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Rollback failed: {error}", file=sys.stderr)
        raise SystemExit(1)
