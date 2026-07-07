#!/usr/bin/env python3
"""
Extract and redact matching requests from a HAR file.

Usage:
  python3 scripts/extract_request.py path/to/file.har --target-path-fragment "/api/items"
  python3 scripts/extract_request.py path/to/file.har \
    --target-path-fragment "/api/items" \
    --json
  python3 scripts/extract_request.py path/to/file.har \
    --target-path-fragment "/api/items" \
    --method GET
  python3 scripts/extract_request.py path/to/file.har \
    --target-path-fragment "/some/api/path" \
    --method POST \
    --include-response-content
  python3 scripts/extract_request.py path/to/file.har \
    --json \
    --no-redact
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse


SENSITIVE_NAME_PATTERN = re.compile(
    r"(authorization|cookie|set-cookie|token|secret|session|password|auth)",
    re.IGNORECASE,
)


def redact_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return redact_structure(value)
    text = str(value)
    if len(text) <= 8:
        return "[REDACTED]"
    return f"{text[:4]}...[REDACTED]...{text[-4:]}"


def redact_structure(value: Any, key: str | None = None) -> Any:
    if key and SENSITIVE_NAME_PATTERN.search(key):
        return redact_value(value)

    if isinstance(value, dict):
        return {child_key: redact_structure(child_value, child_key) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    return value


def header_map(headers: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for header in headers or []:
        name = header.get("name")
        if not name:
            continue
        result[name] = header.get("value")
    return result


def cookies_map(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for cookie in cookies or []:
        name = cookie.get("name")
        if not name:
            continue
        result[name] = cookie.get("value")
    return result


def parse_post_data(post_data: dict[str, Any] | None) -> Any:
    if not post_data:
        return None

    text = post_data.get("text")
    mime_type = post_data.get("mimeType")

    if text:
        if mime_type and "json" in mime_type.lower():
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text

    params = post_data.get("params")
    if params:
        return {param.get("name"): param.get("value") for param in params if param.get("name")}

    return None


def parse_response_content(response: dict[str, Any]) -> Any:
    content = response.get("content", {})
    text = content.get("text")
    mime_type = content.get("mimeType", "")

    if text is None:
        return None

    if "json" in mime_type.lower():
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    return text


def maybe_redact(value: Any, enabled: bool) -> Any:
    return redact_structure(value) if enabled else value


def summarize_entry(
    entry: dict[str, Any],
    include_response_content: bool = False,
    redact: bool = True,
) -> dict[str, Any]:
    request = entry.get("request", {})
    response = entry.get("response", {})
    url = request.get("url", "")
    parsed_url = urlparse(url)

    summary = {
        "startedDateTime": entry.get("startedDateTime"),
        "request": {
            "method": request.get("method"),
            "url": url,
            "path": parsed_url.path,
            "query": dict(parse_qsl(parsed_url.query, keep_blank_values=True)),
            "headers": maybe_redact(header_map(request.get("headers", [])), redact),
            "cookies": maybe_redact(cookies_map(request.get("cookies", [])), redact),
            "postData": maybe_redact(parse_post_data(request.get("postData")), redact),
        },
        "response": {
            "status": response.get("status"),
            "statusText": response.get("statusText"),
            "headers": maybe_redact(header_map(response.get("headers", [])), redact),
            "cookies": maybe_redact(cookies_map(response.get("cookies", [])), redact),
        },
    }
    if include_response_content:
        summary["response"]["content"] = maybe_redact(
            parse_response_content(response),
            redact,
        )
    return summary


def find_matching_entries(
    har: dict[str, Any],
    target_path_fragment: str,
    method: str,
) -> list[dict[str, Any]]:
    entries = har.get("log", {}).get("entries", [])
    return [
        entry
        for entry in entries
        if target_path_fragment in entry.get("request", {}).get("url", "")
        and entry.get("request", {}).get("method") == method
    ]


def print_human_readable(summaries: list[dict[str, Any]]) -> None:
    if not summaries:
        print("No matching requests found.")
        return

    print(f"Found {len(summaries)} matching request(s).\n")
    for index, summary in enumerate(summaries, start=1):
        request = summary["request"]
        response = summary["response"]
        print(f"Request #{index}")
        print(f"  Started: {summary['startedDateTime']}")
        print(f"  Method:  {request['method']}")
        print(f"  URL:     {request['url']}")
        print(f"  Query:   {json.dumps(request['query'], indent=2)}")
        print(f"  Payload: {json.dumps(request['postData'], indent=2)}")
        print(f"  Status:  {response['status']} {response['statusText'] or ''}".rstrip())
        print("  Headers:")
        print(json.dumps(request["headers"], indent=2))
        print("  Cookies:")
        print(json.dumps(request["cookies"], indent=2))
        if "content" in response:
            print("  Response content:")
            print(json.dumps(response["content"], indent=2))
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract redacted matching requests from a HAR file."
    )
    parser.add_argument("har_file", type=Path, help="Path to the HAR file to inspect.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a human-readable summary.",
    )
    parser.add_argument(
        "--target-path-fragment",
        required=True,
        help="Only include requests whose URL contains this path fragment.",
    )
    parser.add_argument(
        "--method",
        default="POST",
        help="Only include requests with this HTTP method (default: POST).",
    )
    parser.add_argument(
        "--include-response-content",
        action="store_true",
        help="Include the full response body, decoding JSON responses when possible.",
    )
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help="Disable redaction and print sensitive values exactly as captured in the HAR.",
    )
    args = parser.parse_args()

    with args.har_file.open("r", encoding="utf-8") as handle:
        har = json.load(handle)

    summaries = [
        summarize_entry(
            entry,
            include_response_content=args.include_response_content,
            redact=not args.no_redact,
        )
        for entry in find_matching_entries(
            har,
            args.target_path_fragment,
            args.method.upper(),
        )
    ]

    if args.json:
        print(json.dumps(summaries, indent=2))
    else:
        print_human_readable(summaries)


if __name__ == "__main__":
    main()
