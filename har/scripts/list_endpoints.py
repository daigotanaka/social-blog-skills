#!/usr/bin/env python3
"""
List distinct request endpoints from a HAR file as a JSON tree.

Usage:
  python3 scripts/list_endpoints.py path/to/file.har
  python3 scripts/list_endpoints.py path/to/file.har --include-host
  python3 scripts/list_endpoints.py path/to/file.har --method GET
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlparse


UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
INTEGER_PATTERN = re.compile(r"^\d+$")
LONG_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,}$")


def extract_endpoints(
    har: dict,
    include_host: bool = False,
    methods: set[str] | None = None,
) -> dict[str, dict[str, set[str]]]:
    endpoints: dict[str, dict[str, set[str]]] = {}

    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        url = request.get("url")
        method = request.get("method")
        if not url:
            continue
        if methods and (not method or method.upper() not in methods):
            continue

        parsed_url = urlparse(url)
        if include_host:
            endpoint = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        else:
            endpoint = parsed_url.path

        if endpoint:
            endpoint_info = endpoints.setdefault(
                endpoint,
                {
                    "path": endpoint,
                    "methods": set(),
                    "queryParams": set(),
                },
            )
            if method:
                endpoint_info["methods"].add(method.upper())
            endpoint_info["queryParams"].update(
                query_param_name
                for query_param_name, _ in parse_qsl(
                    parsed_url.query,
                    keep_blank_values=True,
                )
            )

    return endpoints


def normalize_segment(segment: str) -> str:
    if UUID_PATTERN.match(segment):
        return "{uuid}"
    if INTEGER_PATTERN.match(segment):
        return "{id}"
    if LONG_ID_PATTERN.match(segment) and any(char.isdigit() for char in segment):
        return "{id}"
    return segment


def build_endpoint_tree(endpoints: dict[str, dict[str, set[str]]]) -> dict:
    tree: dict = {}

    for endpoint, endpoint_info in sorted(endpoints.items(), key=lambda item: item[0]):
        parts = [normalize_segment(part) for part in endpoint.split("/") if part]
        node = tree
        for index, part in enumerate(parts):
            is_leaf = index == len(parts) - 1
            if is_leaf:
                leaf = node.setdefault(
                    part,
                    {
                        "path": endpoint,
                        "methods": set(),
                        "queryParams": set(),
                    },
                )
                leaf["path"] = endpoint
                leaf["methods"].update(endpoint_info["methods"])
                leaf["queryParams"].update(endpoint_info["queryParams"])
            else:
                node = node.setdefault(part, {})

    return sort_tree(tree)


def sort_tree(tree: dict | set[str] | str) -> dict | list[str] | str:
    if isinstance(tree, set):
        return sorted(tree)
    if not isinstance(tree, dict):
        return tree
    return {
        key: sort_tree(value)
        for key, value in sorted(tree.items(), key=lambda item: item[0])
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List distinct request endpoints from a HAR file as a JSON tree."
    )
    parser.add_argument("har_file", type=Path, help="Path to the HAR file to inspect.")
    parser.add_argument(
        "--include-host",
        action="store_true",
        help="Include scheme and host in each endpoint instead of printing only the path.",
    )
    parser.add_argument(
        "--method",
        action="append",
        dest="methods",
        help="Only include requests with this HTTP method. Repeat to include multiple methods.",
    )
    args = parser.parse_args()

    with args.har_file.open("r", encoding="utf-8") as handle:
        har = json.load(handle)

    methods = {method.upper() for method in args.methods} if args.methods else None
    endpoints = extract_endpoints(
        har,
        include_host=args.include_host,
        methods=methods,
    )
    print(json.dumps(build_endpoint_tree(endpoints), indent=2))


if __name__ == "__main__":
    main()
