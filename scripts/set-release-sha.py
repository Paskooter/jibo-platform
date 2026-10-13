#!/usr/bin/env python3
"""Fill the documented install pin in this Git checkout; never publish or commit."""
from pathlib import Path
import argparse
import os
import re
import subprocess

# Keep the placeholder itself only in the user-facing install command.
TOKEN = ("PLUGIN_" + "RELEASE_SHA").encode("ascii")


def set_release_sha(root, sha):
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha) or sha == "0" * 40:
        raise ValueError("SHA must be a nonzero, full 40-character hexadecimal commit SHA")
    root = Path(root).resolve()
    top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root, text=True).strip()
    if Path(top).resolve() != root:
        raise ValueError("run the script from its own Git checkout")
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    paths = [root / os.fsdecode(name) for name in names if name]
    # Inspect the complete population before any write; never follow symlinks.
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("tracked file is missing or a symlink: " + str(path.relative_to(root)))
    replacements = [(path, path.read_bytes()) for path in paths]
    replacements = [(path, data) for path, data in replacements if TOKEN in data]
    if not replacements:
        raise ValueError("no release token found; this checkout is already filled")
    occurrences = sum(data.count(TOKEN) for _, data in replacements)
    for path, data in replacements:
        path.write_bytes(data.replace(TOKEN, sha.encode("ascii")))
    for path in paths:
        if TOKEN in path.read_bytes():
            raise ValueError("release token remains after replacement: " + str(path.relative_to(root)))
    return occurrences, len(replacements)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha", help="Published plugin commit: full 40-character hexadecimal SHA")
    args = parser.parse_args()
    try:
        occurrences, changed = set_release_sha(Path(__file__).resolve().parents[1], args.sha)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    print(f"Replaced {occurrences} occurrence(s) in {changed} file(s).")


if __name__ == "__main__":
    main()
