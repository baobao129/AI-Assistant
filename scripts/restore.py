"""Restore an offline snapshot only into a completely new Compose project."""

import argparse
from pathlib import Path

from scripts.release_storage import restore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    restore(args.project, args.env_file, args.source)


if __name__ == "__main__":
    main()
