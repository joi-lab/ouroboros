#!/usr/bin/env python3
"""Select full browser CI unless the complete event diff is documentation only.

Conservative by construction: only prose the product cannot import or serve is
documentation. `docs/` Markdown/reStructuredText/plain text, the few top-level
project documents and the pull-request template qualify. `site/` (the published
pages are built code), HTML, assets, `requirements.txt` and every unknown path
do not — an unrecognized path always runs the lane.
"""
import argparse
from pathlib import PurePosixPath
import subprocess


TOP_LEVEL_DOCUMENT_SUFFIXES = {".md", ".rst"}
TOP_LEVEL_DOCUMENTS = {"LICENSE"}
DOCUMENTATION_SUFFIXES = {".md", ".rst", ".txt"}


def documentation_only(paths) -> bool:
    def document(path):
        name = PurePosixPath(path)
        if path == ".github/PULL_REQUEST_TEMPLATE.md":
            return True
        if len(name.parts) == 1:
            # Deliberately NOT every top-level suffix: requirements.txt is a
            # dependency input that reads like a text file.
            return name.suffix in TOP_LEVEL_DOCUMENT_SUFFIXES or path in TOP_LEVEL_DOCUMENTS
        return name.parts[0] == "docs" and name.suffix in DOCUMENTATION_SUFFIXES
    return bool(paths) and all(document(path) for path in paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", args.base, args.head, "--"],
        capture_output=True, check=False,
    )
    paths = result.stdout.decode("utf-8", "surrogateescape").rstrip("\0").split("\0") if result.stdout else []
    # An absent/unreadable comparison is more coverage, never a docs-only skip.
    print("run_browser=" + str(result.returncode != 0 or not documentation_only(paths)).lower())


if __name__ == "__main__":
    main()
