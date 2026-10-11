#!/usr/bin/env python3
"""Fetch or verify the exact Claudexor archive selected by Ouroboros."""

from __future__ import annotations

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ouroboros.claudexor_runtime import (  # noqa: E402
    ClaudexorRuntimeError,
    fetch_runtime_archive,
    load_runtime_pin,
    verify_runtime_archive,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=pathlib.Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    try:
        pin = load_runtime_pin()
        if pin is None:
            raise ClaudexorRuntimeError(
                "runtime_pin_unpublished",
                "the Claudexor runtime release has not been pinned yet",
            )
        target = args.output_dir / pin.archive_name
        if args.verify_only:
            verify_runtime_archive(target, pin)
        else:
            fetch_runtime_archive(pin, target)
    except ClaudexorRuntimeError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1

    print(f"Claudexor {pin.version} runtime verified: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
