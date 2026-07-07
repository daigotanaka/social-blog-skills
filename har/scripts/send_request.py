#!/usr/bin/env python3
"""
Send an HTTP request using headers and payload loaded from JSON files.

Examples:
  python3 scripts/send_request.py \
    --method POST \
    --url "https://example.com/api/items" \
    --headers-file headers.json \
    --payload-file payload.json

  python3 scripts/send_request.py \
    --method GET \
    --url "https://example.com/api/items" \
    --headers-file headers.json \
    --output-file response.json

  python3 scripts/send_request.py \
    --request-file extracted_requests.json \
    --request-index 0 \
    --print-response-body
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests


def load_json_file(path: Path | None) -> Any:
    if path is None:
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise ValueError("Headers JSON must be an object mapping header names to values.")
    sanitized_headers: dict[str, str] = {}
    for key, value in headers.items():
        header_name = str(key)
        if header_name.startswith(":"):
            continue
        sanitized_headers[header_name] = str(value)
    return sanitized_headers


def load_extracted_request(path: Path, request_index: int) -> dict[str, Any]:
    data = load_json_file(path)

    if isinstance(data, list):
        try:
            request_entry = data[request_index]
        except IndexError as exc:
            raise ValueError(
                f"Request index {request_index} is out of range for {len(data)} extracted request(s)."
            ) from exc
    elif isinstance(data, dict):
        request_entry = data
    else:
        raise ValueError("Request file must contain either one request object or a list of request objects.")

    if not isinstance(request_entry, dict):
        raise ValueError("Selected request entry must be a JSON object.")

    request = request_entry.get("request")
    if not isinstance(request, dict):
        raise ValueError("Request file entry must contain a 'request' object.")

    return request


def maybe_write_response_body(response: requests.Response, output_file: Path | None) -> None:
    if output_file is None:
        return

    content_type = response.headers.get("content-type", "")
    if "json" in content_type.lower():
        try:
            output_file.write_text(
                json.dumps(response.json(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return
        except ValueError:
            pass

    output_file.write_text(response.text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send an HTTP request using JSON header and payload files."
    )
    parser.add_argument("--method", help="HTTP method, e.g. GET or POST.")
    parser.add_argument("--url", help="Full request URL.")
    parser.add_argument(
        "--headers-file",
        type=Path,
        help="Path to a JSON file containing request headers.",
    )
    parser.add_argument(
        "--payload-file",
        type=Path,
        help="Path to a JSON file containing the request payload.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        help="Optional path to write the response body.",
    )
    parser.add_argument(
        "--print-response-body",
        action="store_true",
        help="Print the response body to stdout after the status line.",
    )
    parser.add_argument(
        "--request-file",
        type=Path,
        help="Path to JSON exported by extract_request.py --json.",
    )
    parser.add_argument(
        "--request-index",
        type=int,
        default=0,
        help="Index to use when --request-file contains a list (default: 0).",
    )
    args = parser.parse_args()

    try:
        if args.request_file:
            extracted_request = load_extracted_request(
                args.request_file,
                args.request_index,
            )
            method = args.method or extracted_request.get("method")
            url = args.url or extracted_request.get("url")
            headers = validate_headers(
                load_json_file(args.headers_file)
                if args.headers_file
                else extracted_request.get("headers")
            )
            payload = (
                load_json_file(args.payload_file)
                if args.payload_file
                else extracted_request.get("postData")
            )
        else:
            method = args.method
            url = args.url
            headers = validate_headers(load_json_file(args.headers_file))
            payload = load_json_file(args.payload_file)

        if not method:
            raise ValueError("Missing request method. Provide --method or --request-file.")
        if not url:
            raise ValueError("Missing request URL. Provide --url or --request-file.")
        if not args.request_file and not args.headers_file:
            raise ValueError("Missing headers. Provide --headers-file or --request-file.")

        response = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            json=payload,
            timeout=args.timeout,
        )
    except Exception as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"{response.status_code} {response.reason}")
    print(f"Content-Type: {response.headers.get('content-type', '')}")

    maybe_write_response_body(response, args.output_file)

    if args.print_response_body:
        content_type = response.headers.get("content-type", "")
        if "json" in content_type.lower():
            try:
                print(json.dumps(response.json(), indent=2, ensure_ascii=False))
            except ValueError:
                print(response.text)
        else:
            print(response.text)


if __name__ == "__main__":
    main()
