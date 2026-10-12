"""Create an offline backup after stopping all source volume consumers."""

import argparse
from pathlib import Path

from scripts.release_storage import backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    backup(args.project, args.env_file, args.destination)


if __name__ == "__main__":
    main()
