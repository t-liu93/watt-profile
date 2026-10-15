"""Build a disposable synthetic Logger capacity fixture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from watt_profile.logger.capacity import build_capacity_fixture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a synthetic Logger capacity fixture.")
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--records", type=int, default=241_920)
    args = parser.parse_args(argv)
    if not args.output.is_absolute():
        print("output directory must be absolute", file=sys.stderr)
        return 1
    try:
        result = build_capacity_fixture(args.output, args.records)
    except FileExistsError:
        print("output directory already exists", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError):
        print("capacity fixture failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
