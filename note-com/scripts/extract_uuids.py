#!/usr/bin/env python3
"""Extract note.com UUID/name mappings from saved draft HTML."""

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from note_com import extract_uuids_from_html


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract note.com UUID/name mappings from an HTML file")
    parser.add_argument("html_file", help="Path to saved draft HTML")
    parser.add_argument("--element", default=None, help="Filter by tag name, e.g. p, hr, h2")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    with open(args.html_file, "r", encoding="utf-8") as f:
        html = f.read()

    entries = extract_uuids_from_html(html, args.element)
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0

    print(f"Total UUIDs found: {len(entries)}")
    if args.element:
        print(f"  (filtered by <{args.element}>)")
    print()
    for idx, entry in enumerate(entries, 1):
        preview = entry.get("innerText", "")[:80].replace("\n", " ")
        if entry.get("innerText", "") and len(entry["innerText"]) > 80:
            preview += "..."
        print(f"{idx:3d}. [{entry.get('tag', '?')}] {entry['uuid']}")
        if preview:
            print(f"     {preview}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
