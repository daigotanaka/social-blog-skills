#!/usr/bin/env python3
"""Unified Ghost Admin API client.

Consolidates the existing Ghost scripts into a single class-based interface and
CLI. Posting endpoints use the Ghost Admin API key and generate the short-lived
JWT required by the Admin API automatically.

Usage (CLI):
    python3 ghost.py draft --title "My Post" --body-file post.md
    python3 ghost.py publish <post_id>
    python3 ghost.py schedule <post_id> --published-at 2026-07-08T13:30:00.000Z
    python3 ghost.py update <post_id> --title "New title" --body-file post.md
    python3 ghost.py fetch <post_id>
    python3 ghost.py list --limit 20
    python3 ghost.py convert post.md post.html

Usage (import):
    from ghost import Ghost
    client = Ghost()
    post = client.create_post(markdown="# Hello\n\nWorld")
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html as html_lib
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


DEFAULT_API_VERSION = "v5.0"
DEFAULT_LIMIT = 15
DEFAULT_PAGE = 1

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SCRIPT_DIR.parent

_SECRET_DIRS = (
    _SCRIPT_DIR / ".secrets",
    _PROJECT_DIR / ".secrets",
)


# ===================================================================
# Shared constants & helpers
# ===================================================================

def _read_text(path: str | Path) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def _read_file(path: str | Path) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8")


def _first_existing_secret(names: Iterable[str]) -> Optional[Path]:
    for directory in _SECRET_DIRS:
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


def _get_secret(
    value: Optional[str],
    path: Optional[str],
    env_name: str,
    default_names: Iterable[str],
    label: str,
) -> str:
    if value:
        return value.strip()
    if path:
        return _read_text(path)
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value
    secret_path = _first_existing_secret(default_names)
    if secret_path:
        return _read_text(secret_path)
    raise ValueError(
        f"{label} required. Pass a value/path, set {env_name}, or create one of: "
        + ", ".join(str(d / n) for d in _SECRET_DIRS for n in default_names)
    )


def _get_api_url(api_url: Optional[str] = None, api_url_path: Optional[str] = None) -> str:
    return _get_secret(
        value=api_url,
        path=api_url_path,
        env_name="GHOST_API_URL",
        default_names=("ghost_api_url.txt",),
        label="Ghost API URL",
    ).rstrip("/")


def _get_admin_api_key(
    admin_api_key: Optional[str] = None,
    admin_key_path: Optional[str] = None,
) -> str:
    return _get_secret(
        value=admin_api_key,
        path=admin_key_path,
        env_name="GHOST_ADMIN_API_KEY",
        default_names=("ghost_admin_api_key.txt",),
        label="Ghost Admin API key",
    )


def _get_content_api_key(
    content_api_key: Optional[str] = None,
    content_key_path: Optional[str] = None,
    required: bool = False,
) -> Optional[str]:
    try:
        return _get_secret(
            value=content_api_key,
            path=content_key_path,
            env_name="GHOST_CONTENT_API_KEY",
            default_names=("ghost_content_api_key.txt", "ghost_content.api_key.txt"),
            label="Ghost Content API key",
        )
    except ValueError:
        if required:
            raise
        return None


def _admin_base_url(api_url: str) -> str:
    base = api_url.rstrip("/")
    if base.endswith("/ghost/api/admin"):
        return base
    return f"{base}/ghost/api/admin"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_admin_jwt(admin_api_key: str, ttl_seconds: int = 300) -> str:
    """Generate a Ghost Admin API JWT without requiring PyJWT."""
    try:
        key_id, secret_hex = admin_api_key.strip().split(":", 1)
    except ValueError as exc:
        raise ValueError("Ghost Admin API key must be in '<id>:<hex-secret>' format.") from exc

    now = int(time.time())
    header = {"alg": "HS256", "kid": key_id, "typ": "JWT"}
    payload = {"iat": now, "exp": now + ttl_seconds, "aud": "/admin/"}

    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}"

    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError as exc:
        raise ValueError("Ghost Admin API key secret must be hexadecimal.") from exc

    signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _normalize_tags(tags: Optional[Iterable[Any]]) -> Optional[List[Dict[str, str]]]:
    if tags is None:
        return None
    normalized: list[dict[str, str]] = []
    for tag in tags:
        if not tag:
            continue
        if isinstance(tag, str):
            normalized.append({"name": tag.lstrip("#")})
            continue
        if isinstance(tag, dict):
            item = {
                key: str(tag[key])
                for key in ("id", "name", "slug")
                if tag.get(key)
            }
            if item:
                normalized.append(item)
            continue
        raise ValueError("Tags must be strings or objects with id/name/slug fields.")
    return normalized


def _parse_tags(value: Optional[str], label: str) -> Optional[List[Any]]:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON array, e.g. '[\"AI\", \"Ghost\"]'.") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{label} must be a JSON array.")
    for item in parsed:
        if isinstance(item, str):
            continue
        if isinstance(item, dict) and any(item.get(key) for key in ("id", "name", "slug")):
            continue
        raise ValueError(f"{label} entries must be strings or objects with id/name/slug fields.")
    return parsed


def _email_delivery_params(newsletter: Optional[str], email_segment: Optional[str]) -> Optional[dict[str, str]]:
    if email_segment and not newsletter:
        raise ValueError("--email-segment only has an effect with --newsletter. Omit both for site-only publishing.")
    if not newsletter:
        return None
    params = {"newsletter": newsletter}
    if email_segment:
        params["email_segment"] = email_segment
    return params


def _looks_like_markdown(path: str | Path) -> bool:
    return Path(path).suffix.lower() in {".md", ".markdown", ".mdown", ".mkd"}


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_front_matter_value(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value[0] in ("[", "{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if "," in value:
        return [_strip_wrapping_quotes(part) for part in value.split(",") if part.strip()]
    return _strip_wrapping_quotes(value)


def parse_markdown_front_matter(markdown_text: str) -> tuple[str, dict[str, Any]]:
    """Return markdown without leading YAML-ish front matter plus parsed metadata."""
    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return markdown_text, {}

    lines = normalized.split("\n")
    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        return markdown_text, {}

    metadata: dict[str, Any] = {}
    for line in lines[1:end_index]:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower().replace("-", "_")] = _parse_front_matter_value(value)

    body = "\n".join(lines[end_index + 1:])
    return body.lstrip("\n"), metadata


def _metadata_tags(metadata: dict[str, Any]) -> Optional[list[Any]]:
    tags = metadata.get("tags")
    if tags is None:
        tags = metadata.get("tag")
    if tags is None:
        return None
    if isinstance(tags, str):
        return [tag.strip() for tag in tags.split(",") if tag.strip()]
    if isinstance(tags, list):
        return tags
    return None


def _metadata_text(metadata: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = metadata.get(key)
        if value is not None and not isinstance(value, (list, dict)):
            return str(value)
    return None


# ===================================================================
# Markdown / summary helpers
# ===================================================================

def basic_markdown_to_html(markdown_text: str) -> str:
    """Small fallback markdown converter for environments without markdown."""
    lines = markdown_text.splitlines()
    out: list[str] = []
    in_code = False
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            close_lists()
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            continue

        if in_code:
            out.append(html_lib.escape(raw_line))
            continue

        if not stripped:
            close_lists()
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            close_lists()
            level = len(heading.group(1))
            text = html_lib.escape(heading.group(2))
            out.append(f"<h{level}>{text}</h{level}>")
            continue

        unordered = re.match(r"^[-*]\s+(.+)$", stripped)
        if unordered:
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline_markdown(unordered.group(1))}</li>")
            continue

        ordered = re.match(r"^\d+\.\s+(.+)$", stripped)
        if ordered:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline_markdown(ordered.group(1))}</li>")
            continue

        close_lists()
        out.append(f"<p>{_inline_markdown(stripped)}</p>")

    close_lists()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _inline_markdown(text: str) -> str:
    escaped = html_lib.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def convert_markdown_to_html(markdown_text: str) -> str:
    markdown_text, _metadata = parse_markdown_front_matter(markdown_text)
    try:
        from markdown import markdown as markdown_convert
    except ImportError:
        return basic_markdown_to_html(markdown_text)
    return markdown_convert(
        markdown_text,
        extensions=["extra", "codehilite", "fenced_code", "tables"],
        output_format="html5",
    )


def extract_title_from_markdown(markdown_text: str) -> str:
    markdown_text, metadata = parse_markdown_front_matter(markdown_text)
    title = _metadata_text(metadata, "title")
    if title:
        return title
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled Post"


def strip_first_h1(html: str) -> str:
    return re.sub(r"<h1\b[^>]*>.*?</h1>", "", html, count=1, flags=re.IGNORECASE | re.DOTALL)


def create_ghost_html(markdown_text: str, strip_h1: bool = False) -> str:
    converted = convert_markdown_to_html(markdown_text)
    if strip_h1:
        return strip_first_h1(converted)
    return converted


def create_compact_summary(data: Any) -> str:
    summary: list[str] = []
    if isinstance(data, dict) and ("posts" in data or "data" in data):
        posts = data.get("posts", data.get("data", []))
        if not isinstance(posts, list):
            return "Posts data is not a list"
        summary.append(f"Total Posts: {len(posts)}")
        statuses: dict[str, int] = {}
        for post in posts:
            status = post.get("status", "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        summary.append("Status Breakdown:")
        for status, count in sorted(statuses.items()):
            summary.append(f"  - {status}: {count}")
        summary.append("\nPost Titles:")
        for idx, post in enumerate(posts[:10], 1):
            title = (post.get("title") or "Untitled")[:70]
            slug = (post.get("slug") or "no-slug")[:70]
            published = (post.get("published_at") or "N/A")[:19]
            summary.append(f"{idx}. {title}\n   Slug: {slug}\n   Published: {published}")
        if len(posts) > 10:
            summary.append(f"\n... and {len(posts) - 10} more posts")
        return "\n".join(summary)
    if isinstance(data, list):
        return f"List with {len(data)} items"
    if isinstance(data, dict):
        return f"Dict with keys: {', '.join(data.keys())}"
    return f"Unexpected data type: {type(data).__name__}"


# ===================================================================
# Main client class
# ===================================================================

class Ghost:
    """Unified Ghost Admin API client."""

    def __init__(
        self,
        api_url: Optional[str] = None,
        admin_api_key: Optional[str] = None,
        content_api_key: Optional[str] = None,
        api_url_path: Optional[str] = None,
        admin_key_path: Optional[str] = None,
        content_key_path: Optional[str] = None,
        api_version: str = DEFAULT_API_VERSION,
        timeout: int = 30,
        session: Optional[requests.Session] = None,
    ):
        self.api_url = _get_api_url(api_url, api_url_path)
        self.admin_base_url = _admin_base_url(self.api_url)
        self.admin_api_key = _get_admin_api_key(admin_api_key, admin_key_path)
        self.content_api_key = _get_content_api_key(content_api_key, content_key_path, required=False)
        self.api_version = api_version
        self.timeout = timeout
        self.session = session or requests.Session()
        self._jwt: Optional[str] = None
        self._jwt_expires_at = 0

    # ----------------------------------------------------------------
    # Authentication helpers
    # ----------------------------------------------------------------

    def make_admin_token(self, force: bool = False) -> str:
        now = int(time.time())
        if force or not self._jwt or now >= self._jwt_expires_at:
            self._jwt = _make_admin_jwt(self.admin_api_key)
            self._jwt_expires_at = now + 240
        return self._jwt

    def _admin_headers(self, content_type: Optional[str] = "application/json") -> dict[str, str]:
        headers = {
            "Authorization": f"Ghost {self.make_admin_token()}",
            "Accept-Version": self.api_version,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _admin_url(self, path: str) -> str:
        clean = path.lstrip("/")
        return f"{self.admin_base_url}/{clean}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        content_type = None if files else "application/json"
        response = self.session.request(
            method,
            self._admin_url(path),
            headers=self._admin_headers(content_type=content_type),
            params=params,
            json=json_body,
            files=files,
            data=data,
            timeout=self.timeout,
        )

        try:
            body = response.json() if response.text else {}
        except ValueError:
            body = {"raw": response.text}

        if response.status_code >= 400 or (isinstance(body, dict) and body.get("errors")):
            message = _format_api_error(response.status_code, body)
            raise RuntimeError(message)
        return body if isinstance(body, dict) else {"data": body}

    # ----------------------------------------------------------------
    # Posts
    # ----------------------------------------------------------------

    def list_posts(
        self,
        limit: int = DEFAULT_LIMIT,
        page: int = DEFAULT_PAGE,
        order: str = "published_at desc",
        status_filter: Optional[str] = None,
        include: Optional[str] = "tags,authors",
        fields: Optional[str] = None,
        formats: Optional[str] = None,
        compact: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "page": page, "order": order}
        if status_filter:
            params["filter"] = f"status:{status_filter}"
        if include:
            params["include"] = include
        if fields:
            params["fields"] = fields
        if formats:
            params["formats"] = formats
        data = self._request("GET", "/posts/", params=params)
        if compact:
            for post in data.get("posts", []):
                post.pop("lexical", None)
                post.pop("mobiledoc", None)
                post.pop("html", None)
        return data

    def fetch_post(
        self,
        post_id: str,
        include: Optional[str] = "tags,authors",
        formats: Optional[str] = "html,lexical",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if include:
            params["include"] = include
        if formats:
            params["formats"] = formats
        return self._request("GET", f"/posts/{post_id}/", params=params)

    def upload_image(self, image_path: str, purpose: str = "image") -> str:
        path = Path(image_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Image file not found: {image_path}")
        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        with path.open("rb") as handle:
            files = {"file": (path.name, handle, mime_type)}
            data = self._request("POST", "/images/upload/", files=files, data={"purpose": purpose})
        images = data.get("images", [])
        if not images:
            raise RuntimeError(f"No image returned by Ghost: {data}")
        url = images[0].get("url") or images[0].get("src")
        if not url:
            raise RuntimeError(f"No image URL returned by Ghost: {images[0]}")
        return str(url)

    def create_post(
        self,
        title: Optional[str] = None,
        html: Optional[str] = None,
        markdown: Optional[str] = None,
        status: str = "draft",
        feature_image_path: Optional[str] = None,
        feature_image_alt: Optional[str] = None,
        feature_image_caption: Optional[str] = None,
        tags: Optional[Iterable[Any]] = None,
        custom_excerpt: Optional[str] = None,
        featured: Optional[bool] = None,
        strip_title_h1: bool = True,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if markdown is not None:
            markdown, metadata = parse_markdown_front_matter(markdown)
            title = title if title is not None else _metadata_text(metadata, "title")
            tags = tags if tags is not None else _metadata_tags(metadata)
            custom_excerpt = custom_excerpt if custom_excerpt is not None else _metadata_text(metadata, "excerpt", "description")
        title, html = self._resolve_title_and_html(title, html, markdown, strip_title_h1)
        payload_post: dict[str, Any] = {"title": title, "status": status}
        if html is not None:
            payload_post["html"] = html
        if custom_excerpt is not None:
            payload_post["custom_excerpt"] = custom_excerpt
        if featured is not None:
            payload_post["featured"] = featured
        normalized_tags = _normalize_tags(tags)
        if normalized_tags is not None:
            payload_post["tags"] = normalized_tags
        if feature_image_path:
            payload_post["feature_image"] = self.upload_image(feature_image_path)
        if feature_image_alt is not None:
            payload_post["feature_image_alt"] = feature_image_alt
        if feature_image_caption is not None:
            payload_post["feature_image_caption"] = feature_image_caption

        params = {"source": "html"} if html is not None else None
        data = self._request("POST", "/posts/", params=params, json_body={"posts": [payload_post]})
        return _first_resource(data, "posts")

    def update_post(
        self,
        post_id: str,
        title: Optional[str] = None,
        html: Optional[str] = None,
        markdown: Optional[str] = None,
        status: Optional[str] = None,
        feature_image_path: Optional[str] = None,
        clear_feature_image: bool = False,
        feature_image_alt: Optional[str] = None,
        feature_image_caption: Optional[str] = None,
        tags: Optional[Iterable[Any]] = None,
        custom_excerpt: Optional[str] = None,
        featured: Optional[bool] = None,
        published_at: Optional[str] = None,
        newsletter: Optional[str] = None,
        email_segment: Optional[str] = None,
        strip_title_h1: bool = True,
    ) -> dict[str, Any]:
        current = _first_resource(self.fetch_post(post_id), "posts")
        if markdown is not None:
            markdown, metadata = parse_markdown_front_matter(markdown)
            title = title if title is not None else _metadata_text(metadata, "title")
            tags = tags if tags is not None else _metadata_tags(metadata)
            custom_excerpt = custom_excerpt if custom_excerpt is not None else _metadata_text(metadata, "excerpt", "description")
        if markdown is not None:
            title, html = self._resolve_title_and_html(title, html, markdown, strip_title_h1)

        payload_post: dict[str, Any] = {
            "id": post_id,
            "updated_at": current["updated_at"],
        }
        if title is not None:
            payload_post["title"] = title
        if html is not None:
            payload_post["html"] = html
        if status is not None:
            payload_post["status"] = status
        if published_at is not None:
            payload_post["published_at"] = published_at
        if custom_excerpt is not None:
            payload_post["custom_excerpt"] = custom_excerpt
        if featured is not None:
            payload_post["featured"] = featured
        normalized_tags = _normalize_tags(tags)
        if normalized_tags is not None:
            payload_post["tags"] = normalized_tags
        if clear_feature_image:
            payload_post["feature_image"] = None
            payload_post["feature_image_alt"] = None
            payload_post["feature_image_caption"] = None
        elif feature_image_path:
            payload_post["feature_image"] = self.upload_image(feature_image_path)
        if feature_image_alt is not None:
            payload_post["feature_image_alt"] = feature_image_alt
        if feature_image_caption is not None:
            payload_post["feature_image_caption"] = feature_image_caption

        if len(payload_post) == 2:
            return {"success": False, "error": "No updates specified.", "id": post_id}

        params = {"source": "html"} if html is not None else {}
        email_params = _email_delivery_params(newsletter, email_segment)
        if email_params:
            params.update(email_params)
        if not params:
            params = None
        data = self._request("PUT", f"/posts/{post_id}/", params=params, json_body={"posts": [payload_post]})
        return _first_resource(data, "posts")

    def publish_post(
        self,
        post_id: str,
        title: Optional[str] = None,
        html: Optional[str] = None,
        markdown: Optional[str] = None,
        tags: Optional[Iterable[Any]] = None,
        newsletter: Optional[str] = None,
        email_segment: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.update_post(
            post_id=post_id,
            title=title,
            html=html,
            markdown=markdown,
            status="published",
            tags=tags,
            newsletter=newsletter,
            email_segment=email_segment,
        )

    def schedule_post(
        self,
        post_id: str,
        published_at: str,
        title: Optional[str] = None,
        html: Optional[str] = None,
        markdown: Optional[str] = None,
        tags: Optional[Iterable[Any]] = None,
        feature_image_path: Optional[str] = None,
        clear_feature_image: bool = False,
        feature_image_alt: Optional[str] = None,
        feature_image_caption: Optional[str] = None,
        custom_excerpt: Optional[str] = None,
        featured: Optional[bool] = None,
        newsletter: Optional[str] = None,
        email_segment: Optional[str] = None,
        strip_title_h1: bool = True,
    ) -> dict[str, Any]:
        return self.update_post(
            post_id=post_id,
            title=title,
            html=html,
            markdown=markdown,
            status="scheduled",
            published_at=published_at,
            feature_image_path=feature_image_path,
            clear_feature_image=clear_feature_image,
            feature_image_alt=feature_image_alt,
            feature_image_caption=feature_image_caption,
            tags=tags,
            custom_excerpt=custom_excerpt,
            featured=featured,
            newsletter=newsletter,
            email_segment=email_segment,
            strip_title_h1=strip_title_h1,
        )

    def convert_markdown(self, markdown_text: str, strip_title_h1: bool = True) -> Tuple[str, str]:
        title = extract_title_from_markdown(markdown_text)
        return create_ghost_html(markdown_text, strip_h1=strip_title_h1), title

    def summarize_posts(self, posts_response: dict[str, Any]) -> str:
        return create_compact_summary(posts_response)

    def _resolve_title_and_html(
        self,
        title: Optional[str],
        html: Optional[str],
        markdown: Optional[str],
        strip_title_h1: bool,
    ) -> tuple[str, Optional[str]]:
        if markdown is not None:
            converted_html, extracted_title = self.convert_markdown(markdown, strip_title_h1=strip_title_h1)
            html = converted_html
            if title is None:
                title = extracted_title
        if title is None:
            raise ValueError("title is required when not inferrable from markdown")
        return title, html


def _first_resource(data: dict[str, Any], resource: str) -> dict[str, Any]:
    items = data.get(resource, [])
    if not items:
        raise RuntimeError(f"No {resource[:-1] or resource} returned by Ghost: {data}")
    return items[0]


def _format_api_error(status_code: int, body: dict[str, Any]) -> str:
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        if isinstance(first, dict):
            detail = first.get("message") or first.get("context") or json.dumps(first)
        else:
            detail = str(first)
        return f"Ghost API error HTTP {status_code}: {detail}"
    raw = body.get("raw") if isinstance(body, dict) else body
    return f"Ghost API error HTTP {status_code}: {raw}"


def _read_body_file(path: str, body_format: str) -> tuple[Optional[str], Optional[str]]:
    text = _read_file(path)
    resolved_format = body_format
    if body_format == "auto":
        resolved_format = "markdown" if _looks_like_markdown(path) else "html"
    if resolved_format == "markdown":
        return None, text
    return text, None


def _post_result(post: dict[str, Any], success: bool = True) -> dict[str, Any]:
    keys = (
        "id",
        "uuid",
        "title",
        "slug",
        "status",
        "url",
        "published_at",
        "updated_at",
        "feature_image",
        "featured",
        "custom_excerpt",
    )
    result = {"success": success}
    result.update({key: post.get(key) for key in keys if post.get(key) is not None})
    return result


# ===================================================================
# CLI entry point
# ===================================================================

def _add_auth_args(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    version_default = argparse.SUPPRESS if suppress_defaults else DEFAULT_API_VERSION
    parser.add_argument("--api-url", default=default, help="Ghost site base URL or full admin API URL")
    parser.add_argument("--api-url-path", default=default, help="Path to ghost_api_url.txt")
    parser.add_argument("--admin-key", default=default, help="Ghost Admin API key (<id>:<hex-secret>)")
    parser.add_argument("--admin-key-path", default=default, help="Path to ghost_admin_api_key.txt")
    parser.add_argument("--api-version", default=version_default, help=f"Admin API version (default: {DEFAULT_API_VERSION})")


def _add_body_args(parser: argparse.ArgumentParser, *, title_required: bool = False) -> None:
    parser.add_argument("--title", required=title_required, help="Post title")
    parser.add_argument("--body-file", help="Path to markdown or HTML body file")
    parser.add_argument("--format", choices=("auto", "markdown", "html"), default="auto", help="Body file format")
    parser.add_argument("--keep-title-h1", action="store_true", help="Keep the first markdown H1 in the post HTML")


def _add_post_metadata_args(parser: argparse.ArgumentParser, *, include_clear_image: bool = False) -> None:
    parser.add_argument("--image", dest="image_file", help="Featured image path")
    if include_clear_image:
        parser.add_argument("--clear-image", action="store_true", help="Clear featured image on update")
    parser.add_argument("--image-alt", help="Featured image alt text")
    parser.add_argument("--image-caption", help="Featured image caption")
    parser.add_argument(
        "--tags",
        help='JSON tag array, e.g. ["AI", "Ghost"] or [{"id":"...","name":"AI","slug":"ai"}]',
    )
    parser.add_argument("--excerpt", "--custom-excerpt", dest="excerpt", help="Custom excerpt")
    featured_group = parser.add_mutually_exclusive_group()
    featured_group.add_argument("--featured", dest="featured", action="store_true", help="Feature this post")
    featured_group.add_argument("--unfeatured", dest="featured", action="store_false", help="Do not feature this post")
    parser.set_defaults(featured=None)


def _add_email_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--newsletter",
        help="Newsletter slug. When provided, Ghost emails the post during publish/schedule.",
    )
    parser.add_argument(
        "--email-segment",
        help='Optional member filter for --newsletter, e.g. "all", "status:free", or "status:-free".',
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Unified Ghost Admin API client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_auth_args(ap)
    sub = ap.add_subparsers(dest="command")

    draft_p = sub.add_parser("draft", help="Create a new draft post")
    _add_auth_args(draft_p, suppress_defaults=True)
    _add_body_args(draft_p, title_required=False)
    _add_post_metadata_args(draft_p)

    publish_p = sub.add_parser("publish", help="Publish an existing post")
    _add_auth_args(publish_p, suppress_defaults=True)
    publish_p.add_argument("post_id", help="Ghost post id")
    _add_body_args(publish_p)
    publish_p.add_argument("--tags", help='JSON tag array, e.g. ["AI", "Ghost"]')
    _add_email_args(publish_p)

    schedule_p = sub.add_parser("schedule", help="Schedule an existing post")
    _add_auth_args(schedule_p, suppress_defaults=True)
    schedule_p.add_argument("post_id", help="Ghost post id")
    schedule_p.add_argument("--published-at", required=True, help="UTC publish time, e.g. 2026-07-08T13:30:00.000Z")
    _add_body_args(schedule_p)
    _add_post_metadata_args(schedule_p, include_clear_image=True)
    _add_email_args(schedule_p)

    update_p = sub.add_parser("update", help="Update an existing post")
    _add_auth_args(update_p, suppress_defaults=True)
    update_p.add_argument("post_id", help="Ghost post id")
    _add_body_args(update_p)
    update_p.add_argument("--status", choices=("draft", "published", "scheduled"), help="New post status")
    update_p.add_argument("--published-at", help="UTC publish time for scheduled posts, e.g. 2026-07-08T13:30:00.000Z")
    _add_post_metadata_args(update_p, include_clear_image=True)
    _add_email_args(update_p)

    fetch_p = sub.add_parser("fetch", help="Fetch a post by id")
    _add_auth_args(fetch_p, suppress_defaults=True)
    fetch_p.add_argument("post_id", help="Ghost post id")
    fetch_p.add_argument("--formats", default="html,lexical", help="Formats to request (default: html,lexical)")
    fetch_p.add_argument("--include", default="tags,authors", help="Relations to include")
    fetch_p.add_argument("--body-only", action="store_true", help="Print only post HTML body")
    fetch_p.add_argument("--output", help="Save JSON or body output to file")

    list_p = sub.add_parser("list", aliases=["posts"], help="List recent posts")
    _add_auth_args(list_p, suppress_defaults=True)
    list_p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Posts per page (default: {DEFAULT_LIMIT})")
    list_p.add_argument("--page", type=int, default=DEFAULT_PAGE, help=f"Page number (default: {DEFAULT_PAGE})")
    list_p.add_argument("--order", default="published_at desc", help="Ghost order expression")
    list_p.add_argument("--status", choices=("draft", "published", "scheduled", "sent"), help="Filter by status")
    list_p.add_argument("--include", default="tags,authors", help="Relations to include")
    list_p.add_argument("--fields", help="Comma-separated fields to request")
    list_p.add_argument("--formats", help="Formats to request")
    list_p.add_argument("--compact", action="store_true", help="Strip bulky content fields")
    list_p.add_argument("--json", action="store_true", help="Print full JSON")
    list_p.add_argument("--output", help="Save full JSON result to file")

    image_p = sub.add_parser("upload-image", help="Upload an image and print its URL")
    _add_auth_args(image_p, suppress_defaults=True)
    image_p.add_argument("image_path", help="Path to image file")
    image_p.add_argument("--purpose", default="image", help="Ghost upload purpose")

    convert_p = sub.add_parser("convert", help="Convert markdown to Ghost-compatible HTML")
    convert_p.add_argument("input_file", help="Markdown input file")
    convert_p.add_argument("output_file", nargs="?", help="HTML output file; omit to print to stdout")
    convert_p.add_argument("--extract-title", action="store_true", help="Print extracted title to stderr")
    convert_p.add_argument("--keep-title-h1", action="store_true", help="Keep first markdown H1 in the HTML")

    summary_p = sub.add_parser("summary", help="Summarize Ghost posts JSON")
    summary_p.add_argument("input_file", nargs="?", default="-", help="JSON input file, or '-' for stdin")

    args = ap.parse_args()
    if not args.command:
        ap.print_help()
        return 1

    try:
        if args.command == "convert":
            markdown_text = _read_file(args.input_file)
            output = create_ghost_html(markdown_text, strip_h1=not args.keep_title_h1)
            if args.output_file:
                Path(args.output_file).expanduser().write_text(output, encoding="utf-8")
                print(f"Saved HTML to {args.output_file} ({len(output)} chars)")
            else:
                print(output)
            if args.extract_title:
                print(f"Title: {extract_title_from_markdown(markdown_text)}", file=sys.stderr)
            return 0

        if args.command == "summary":
            if args.input_file == "-":
                data = json.loads(sys.stdin.read())
            else:
                data = json.loads(Path(args.input_file).expanduser().read_text(encoding="utf-8"))
            print(create_compact_summary(data))
            return 0

        client = Ghost(
            api_url=getattr(args, "api_url", None),
            api_url_path=getattr(args, "api_url_path", None),
            admin_api_key=getattr(args, "admin_key", None),
            admin_key_path=getattr(args, "admin_key_path", None),
            api_version=getattr(args, "api_version", DEFAULT_API_VERSION),
        )

        if args.command == "draft":
            html, markdown = (None, None)
            if args.body_file:
                html, markdown = _read_body_file(args.body_file, args.format)
            result = client.create_post(
                title=args.title,
                html=html,
                markdown=markdown,
                status="draft",
                feature_image_path=args.image_file,
                feature_image_alt=args.image_alt,
                feature_image_caption=args.image_caption,
                tags=_parse_tags(args.tags, "--tags"),
                custom_excerpt=args.excerpt,
                featured=args.featured,
                strip_title_h1=not args.keep_title_h1,
            )
            print(json.dumps(_post_result(result), indent=2, ensure_ascii=False))
            return 0

        if args.command == "publish":
            html, markdown = (None, None)
            if args.body_file:
                html, markdown = _read_body_file(args.body_file, args.format)
            result = client.publish_post(
                post_id=args.post_id,
                title=args.title,
                html=html,
                markdown=markdown,
                tags=_parse_tags(args.tags, "--tags"),
                newsletter=args.newsletter,
                email_segment=args.email_segment,
            )
            print(json.dumps(_post_result(result), indent=2, ensure_ascii=False))
            return 0

        if args.command == "schedule":
            html, markdown = (None, None)
            if args.body_file:
                html, markdown = _read_body_file(args.body_file, args.format)
            result = client.schedule_post(
                post_id=args.post_id,
                published_at=args.published_at,
                title=args.title,
                html=html,
                markdown=markdown,
                feature_image_path=args.image_file,
                clear_feature_image=args.clear_image,
                feature_image_alt=args.image_alt,
                feature_image_caption=args.image_caption,
                tags=_parse_tags(args.tags, "--tags"),
                custom_excerpt=args.excerpt,
                featured=args.featured,
                newsletter=args.newsletter,
                email_segment=args.email_segment,
                strip_title_h1=not args.keep_title_h1,
            )
            print(json.dumps(_post_result(result), indent=2, ensure_ascii=False))
            return 0

        if args.command == "update":
            html, markdown = (None, None)
            if args.body_file:
                html, markdown = _read_body_file(args.body_file, args.format)
            result = client.update_post(
                post_id=args.post_id,
                title=args.title,
                html=html,
                markdown=markdown,
                status=args.status,
                published_at=args.published_at,
                feature_image_path=args.image_file,
                clear_feature_image=args.clear_image,
                feature_image_alt=args.image_alt,
                feature_image_caption=args.image_caption,
                tags=_parse_tags(args.tags, "--tags"),
                custom_excerpt=args.excerpt,
                featured=args.featured,
                newsletter=args.newsletter,
                email_segment=args.email_segment,
                strip_title_h1=not args.keep_title_h1,
            )
            print(json.dumps(_post_result(result) if result.get("success") is not False else result, indent=2, ensure_ascii=False))
            return 0 if result.get("success") is not False else 1

        if args.command == "fetch":
            data = client.fetch_post(args.post_id, include=args.include, formats=args.formats)
            post = _first_resource(data, "posts")
            output = post.get("html", "") if args.body_only else json.dumps(data, indent=2, ensure_ascii=False)
            if args.output:
                Path(args.output).expanduser().write_text(output, encoding="utf-8")
                print(f"Saved output to {args.output} ({len(output)} chars)")
            else:
                print(output)
            return 0

        if args.command in ("list", "posts"):
            data = client.list_posts(
                limit=args.limit,
                page=args.page,
                order=args.order,
                status_filter=args.status,
                include=args.include,
                fields=args.fields,
                formats=args.formats,
                compact=args.compact,
            )
            if args.output:
                Path(args.output).expanduser().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"Saved {len(data.get('posts', []))} posts to {args.output}")
            if args.json:
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                print(create_compact_summary(data))
            return 0

        if args.command == "upload-image":
            print(client.upload_image(args.image_path, purpose=args.purpose))
            return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
