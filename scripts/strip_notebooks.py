"""Strip execution outputs from Jupyter notebooks before committing.

The notebooks in this repo were run against live data: their cell outputs contain
personal information (email addresses of people who filed reports). The repository
is public, so outputs must never be committed.

Usage:
    python scripts/strip_notebooks.py            # strip every *.ipynb in the repo
    python scripts/strip_notebooks.py --check    # exit 1 if any notebook has outputs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def strip(path: Path, check_only: bool = False) -> bool:
    """Clear outputs in one notebook. Returns True if it had any."""
    nb = json.loads(path.read_text(encoding="utf-8"))
    dirty = False
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        if cell.get("outputs") or cell.get("execution_count") is not None:
            dirty = True
        cell["outputs"] = []
        cell["execution_count"] = None
    if dirty and not check_only:
        path.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return dirty


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report notebooks with outputs instead of stripping them")
    args = ap.parse_args()

    notebooks = sorted(p for p in ROOT.rglob("*.ipynb")
                       if ".ipynb_checkpoints" not in p.parts)
    dirty = [p for p in notebooks if strip(p, check_only=args.check)]

    for p in dirty:
        print(("has outputs: " if args.check else "stripped: ") + str(p.relative_to(ROOT)))
    if not dirty:
        print(f"{len(notebooks)} notebook(s) already clean")
    return 1 if (args.check and dirty) else 0


if __name__ == "__main__":
    sys.exit(main())
