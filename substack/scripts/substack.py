#!/usr/bin/env python3
"""Small Substack API client.

Usage:
    python3 scripts/substack.py post-note "Hi, I'm just looking around."
    python3 scripts/substack.py create-draft \
        --publication-url https://daigotanaka.substack.com \
        --publication-id 1324369 \
        --byline-user-id 31220959 \
        --title "API Draft Test" \
        --subtitle "A small unpublished test draft from the API" \
        --paragraph "This is a short sample body paragraph." \
        --heading "Sample Section" \
        --paragraph "Here is a second paragraph."
    python3 scripts/substack.py save-draft 204727458 \
        --publication-url https://daigotanaka.substack.com \
        --title "Test post" \
        --subtitle "Subtitle of a test post" \
        --paragraph "Here you go..." \
        --heading "Some section headline" \
        --paragraph "Some text" \
        --image-url "https://substack-post-media.s3.amazonaws.com/public/images/example.png" \
        --image-width 960 \
        --image-height 540 \
        --image-bytes 172512 \
        --image-content-type image/png \
        --paragraph "That's it!" \
        --send-email

Usage as a module:
    from substack import Substack
    client = Substack(token_path=".secrets/substack-token.txt")
    result = client.post_note("Hello from Substack")
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


POST_NOTE_URL = "https://substack.com/api/v1/comment/feed"
DEFAULT_TOKEN_PATH = Path(".secrets/substack-token.txt")
ARTICLE_STATES = ("published", "draft", "scheduled")
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


class _AppendBlockAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | list[str] | None,
        option_string: str | None = None,
    ) -> None:
        blocks = getattr(namespace, self.dest, None) or []
        kind = "heading" if option_string == "--heading" else "paragraph"
        if not isinstance(values, str):
            parser.error(f"{option_string} requires text")
        blocks.append((kind, values))
        setattr(namespace, self.dest, blocks)


def _get_token(token: str | None = None, token_path: str | Path | None = None) -> str:
    if token:
        return token.strip()
    if token_path:
        path = Path(token_path)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    env = os.environ.get("SUBSTACK_SID", "")
    if env:
        return env.strip()
    if DEFAULT_TOKEN_PATH.exists():
        return DEFAULT_TOKEN_PATH.read_text(encoding="utf-8").strip()
    raise ValueError(
        "Missing Substack cookie. Provide --token, --token-path, SUBSTACK_SID, "
        f"or {DEFAULT_TOKEN_PATH}."
    )


def _note_payload(text: str, reply_minimum_role: str = "everyone") -> dict[str, Any]:
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text.strip())
        if paragraph.strip()
    ]
    if not paragraphs:
        paragraphs = [""]
    return {
        "bodyJson": {
            "type": "doc",
            "attrs": {
                "schemaVersion": "v1",
                "title": None,
            },
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": paragraph,
                        }
                    ],
                }
                for paragraph in paragraphs
            ],
        },
        "replyMinimumRole": reply_minimum_role,
    }


def paragraph_node(text: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": "paragraph",
        "attrs": {"textAlign": None},
    }
    if text:
        node["content"] = [{"type": "text", "text": text}]
    return node


def heading_node(text: str, level: int = 1) -> dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {
            "textAlign": None,
            "level": level,
        },
        "content": [{"type": "text", "text": text}],
    }


def code_block_node(text: str, language: str | None = None) -> dict[str, Any]:
    node_type = "highlighted_code_block" if language else "code_block"
    attrs: dict[str, Any] = {"language": language}
    if language:
        attrs["nodeId"] = str(uuid.uuid4())
    return {
        "type": node_type,
        "attrs": attrs,
        "content": [{"type": "text", "text": text}] if text else [],
    }


def blockquote_node(children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "type": "blockquote",
        "content": children or [paragraph_node()],
    }


def bullet_list_node(items: list[str]) -> dict[str, Any]:
    return {
        "type": "bullet_list",
        "content": [
            {
                "type": "list_item",
                "content": [paragraph_node(item)],
            }
            for item in items
        ],
    }


def image_node(
    publication_url: str,
    draft_id: int | str,
    src: str,
    width: int,
    height: int,
    byte_count: int,
    content_type: str = "image/png",
    alt: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    publication_url = publication_url.rstrip("/")
    return {
        "type": "captionedImage",
        "content": [
            {
                "type": "image2",
                "attrs": {
                    "src": src,
                    "srcNoWatermark": None,
                    "fullscreen": None,
                    "imageSize": None,
                    "height": height,
                    "width": width,
                    "resizeWidth": None,
                    "bytes": byte_count,
                    "alt": alt,
                    "title": title,
                    "type": content_type,
                    "href": None,
                    "belowTheFold": False,
                    "topImage": False,
                    "internalRedirect": f"{publication_url}/i/{draft_id}?img={quote(src, safe='')}",
                    "isProcessing": False,
                    "align": None,
                    "offset": False,
                },
            }
        ],
    }


def doc_node(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "doc",
        "content": content or [paragraph_node()],
    }


def _data_url(path: Path, content_type: str | None = None) -> str:
    content_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _clean_markdown_inline(text: str) -> str:
    text = _LINK_RE.sub(lambda match: f"{match.group(1)} ({match.group(2)})", text)
    text = _INLINE_CODE_RE.sub(lambda match: match.group(1), text)
    text = text.replace("**", "").replace("__", "")
    return text


def markdown_to_doc(path: Path) -> dict[str, Any]:
    """Convert a conservative subset of Markdown into Substack editor JSON."""
    lines = path.read_text(encoding="utf-8").splitlines()
    nodes: list[dict[str, Any]] = []
    paragraph_buf: list[str] = []
    list_buf: list[str] = []
    quote_buf: list[str] = []
    code_buf: list[str] = []
    code_language: str | None = None
    in_code = False

    def flush_paragraph() -> None:
        if paragraph_buf:
            text = " ".join(part.strip() for part in paragraph_buf).strip()
            nodes.append(paragraph_node(_clean_markdown_inline(text)))
            paragraph_buf.clear()

    def flush_list() -> None:
        if list_buf:
            nodes.append(bullet_list_node([_clean_markdown_inline(item) for item in list_buf]))
            list_buf.clear()

    def flush_quote() -> None:
        if quote_buf:
            text = " ".join(part.strip() for part in quote_buf).strip()
            nodes.append(blockquote_node([paragraph_node(_clean_markdown_inline(text))]))
            quote_buf.clear()

    def flush_code() -> None:
        if code_buf:
            nodes.append(code_block_node("\n".join(code_buf), code_language))
            code_buf.clear()

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        fence_match = re.match(r"^```\s*([A-Za-z0-9_+.-]+)?\s*$", line)
        if fence_match:
            if in_code:
                flush_code()
                code_language = None
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                flush_quote()
                code_language = fence_match.group(1) or None
                in_code = True
            continue

        if in_code:
            code_buf.append(line)
            continue

        if not line.strip():
            flush_paragraph()
            flush_list()
            flush_quote()
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            flush_paragraph()
            flush_list()
            flush_quote()
            level = min(len(heading_match.group(1)), 3)
            nodes.append(heading_node(_clean_markdown_inline(heading_match.group(2).strip()), level))
            continue

        if line.startswith(">"):
            flush_paragraph()
            flush_list()
            quote_buf.append(line.lstrip("> ").strip())
            continue

        list_match = re.match(r"^[-*]\s+(.+)$", line)
        if list_match:
            flush_paragraph()
            flush_quote()
            list_buf.append(list_match.group(1).strip())
            continue

        paragraph_buf.append(line)

    if in_code:
        flush_code()
    flush_paragraph()
    flush_list()
    flush_quote()
    return doc_node(nodes)


class Substack:
    def __init__(
        self,
        token: str | None = None,
        token_path: str | Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token = _get_token(token=token, token_path=token_path)
        self.timeout = timeout

    def _headers(self, origin: str = "https://substack.com", referer: str | None = None) -> dict[str, str]:
        referer = referer or f"{origin.rstrip('/')}/"
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Cookie": f"substack.sid={self._token}",
            "Origin": origin.rstrip("/"),
            "Referer": referer,
            "User-Agent": _BROWSER_UA,
        }

    def post_note(self, text: str, reply_minimum_role: str = "everyone") -> dict[str, Any]:
        response = requests.post(
            POST_NOTE_URL,
            headers=self._headers(),
            json=_note_payload(text, reply_minimum_role),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def upload_image(
        self,
        publication_url: str,
        image_path: str | Path,
        draft_id: int | str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        publication_url = publication_url.rstrip("/")
        image_path = Path(image_path)
        referer = f"{publication_url}/publish/post/{draft_id}" if draft_id else f"{publication_url}/publish/post/new"
        response = requests.post(
            f"{publication_url}/api/v1/image",
            headers=self._headers(origin=publication_url, referer=referer),
            json={"image": _data_url(image_path, content_type)},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def append_image_to_draft(
        self,
        publication_url: str,
        draft_id: int | str,
        image_path: str | Path,
        byline_user_id: int | None = None,
        content_type: str | None = None,
        alt: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        uploaded = self.upload_image(
            publication_url=publication_url,
            image_path=image_path,
            draft_id=draft_id,
            content_type=content_type,
        )
        draft = self.fetch_draft(publication_url, draft_id)
        body = json.loads(draft.get("draft_body") or '{"type":"doc","content":[]}')
        body.setdefault("content", []).append(
            image_node(
                publication_url=publication_url,
                draft_id=draft_id,
                src=uploaded["url"],
                width=uploaded["imageWidth"],
                height=uploaded["imageHeight"],
                byte_count=uploaded["bytes"],
                content_type=uploaded.get("contentType") or content_type or "image/png",
                alt=alt,
                title=title,
            )
        )
        saved = self.save_draft(
            publication_url=publication_url,
            draft_id=draft_id,
            title=draft.get("draft_title") or "",
            subtitle=draft.get("draft_subtitle") or "",
            body=body,
            byline_user_id=byline_user_id,
            should_send_email=bool(draft.get("should_send_email")),
            last_updated_at=draft.get("draft_updated_at"),
        )
        saved["_uploaded_image"] = uploaded
        return saved

    def create_draft(
        self,
        publication_url: str,
        publication_id: int,
        byline_user_id: int,
        draft_type: str = "newsletter",
    ) -> dict[str, Any]:
        publication_url = publication_url.rstrip("/")
        payload = {
            "type": draft_type,
            "publication_id": publication_id,
            "draft_bylines": [{"id": byline_user_id, "is_guest": False}],
        }
        response = requests.post(
            f"{publication_url}/api/v1/drafts",
            headers=self._headers(
                origin=publication_url,
                referer=f"{publication_url}/publish/post/new",
            ),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def fetch_draft(self, publication_url: str, draft_id: int | str) -> dict[str, Any]:
        publication_url = publication_url.rstrip("/")
        response = requests.get(
            f"{publication_url}/api/v1/drafts/{draft_id}",
            headers=self._headers(
                origin=publication_url,
                referer=f"{publication_url}/publish/post/{draft_id}",
            ),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def list_articles(
        self,
        publication_url: str,
        states: list[str] | tuple[str, ...] = ARTICLE_STATES,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        publication_url = publication_url.rstrip("/")
        results: dict[str, Any] = {}
        for state in states:
            if state == "draft":
                path = "/api/v1/drafts"
                params = {"offset": offset, "limit": limit}
                referer_path = "/publish/posts/drafts"
            elif state == "published":
                path = "/api/v1/post_management/published"
                params = {
                    "offset": offset,
                    "limit": limit,
                    "order_by": "post_date",
                    "order_direction": "desc",
                }
                referer_path = "/publish/posts"
            elif state == "scheduled":
                path = "/api/v1/post_management/scheduled"
                params = {
                    "offset": offset,
                    "limit": limit,
                    "order_by": "trigger_at",
                    "order_direction": "asc",
                }
                referer_path = "/publish/posts/scheduled"
            else:
                raise ValueError(f"Unsupported article state: {state}")

            response = requests.get(
                f"{publication_url}{path}",
                headers=self._headers(
                    origin=publication_url,
                    referer=f"{publication_url}{referer_path}",
                ),
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            results[state] = response.json()
        return results

    def save_draft(
        self,
        publication_url: str,
        draft_id: int | str,
        title: str,
        subtitle: str = "",
        body: dict[str, Any] | None = None,
        byline_user_id: int | None = None,
        should_send_email: bool = False,
        audience: str = "everyone",
        write_comment_permissions: str = "everyone",
        meter_type: str = "none",
        last_updated_at: str | None = None,
    ) -> dict[str, Any]:
        publication_url = publication_url.rstrip("/")
        if last_updated_at is None:
            current = self.fetch_draft(publication_url, draft_id)
            last_updated_at = current.get("draft_updated_at") or current.get("updated_at")
        payload: dict[str, Any] = {
            "draft_title": title,
            "draft_subtitle": subtitle,
            "draft_podcast_url": None,
            "draft_podcast_duration": None,
            "draft_body": json.dumps(body or doc_node([]), separators=(",", ":")),
            "section_chosen": False,
            "draft_section_id": None,
            "detect_language": True,
            "translations": [],
            "last_updated_at": last_updated_at
            or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "audience": audience,
            "audience_before_archived": None,
            "syndicate_voiceover_to_rss": False,
            "syndicate_to_section_id": None,
            "should_syndicate_to_other_feed": None,
            "write_comment_permissions": write_comment_permissions,
            "default_comment_sort": None,
            "should_send_email": should_send_email,
            "meter_type": meter_type,
            "cover_image": None,
            "search_engine_title": None,
            "search_engine_description": None,
        }
        if byline_user_id is not None:
            payload["draft_bylines"] = [{"id": byline_user_id, "is_guest": False}]

        url = f"{publication_url}/api/v1/drafts/{draft_id}"
        response = requests.put(
            url,
            headers=self._headers(
                origin=publication_url,
                referer=f"{publication_url}/publish/post/{draft_id}",
            ),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def prepublish_draft(
        self,
        publication_url: str,
        draft_id: int | str,
        publish_date: str | None = None,
    ) -> dict[str, Any]:
        publication_url = publication_url.rstrip("/")
        params = {"publish_date": publish_date} if publish_date else None
        response = requests.get(
            f"{publication_url}/api/v1/drafts/{draft_id}/prepublish",
            headers=self._headers(
                origin=publication_url,
                referer=f"{publication_url}/publish/post/{draft_id}",
            ),
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def publish_draft(self, publication_url: str, draft_id: int | str, send: bool = True) -> dict[str, Any]:
        publication_url = publication_url.rstrip("/")
        response = requests.post(
            f"{publication_url}/api/v1/drafts/{draft_id}/publish",
            headers=self._headers(
                origin=publication_url,
                referer=f"{publication_url}/publish/post/{draft_id}",
            ),
            json={"send": send},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def schedule_draft(
        self,
        publication_url: str,
        draft_id: int | str,
        trigger_at: str,
        post_audience: str = "everyone",
        email_audience: str = "everyone",
        run_prepublish: bool = True,
        should_send_email: bool = False,
        byline_user_id: int | None = None,
    ) -> dict[str, Any]:
        publication_url = publication_url.rstrip("/")
        if should_send_email:
            draft = self.fetch_draft(publication_url, draft_id)
            body = json.loads(draft.get("draft_body") or '{"type":"doc","content":[]}')
            self.save_draft(
                publication_url=publication_url,
                draft_id=draft_id,
                title=draft.get("draft_title") or "",
                subtitle=draft.get("draft_subtitle") or "",
                body=body,
                byline_user_id=byline_user_id,
                should_send_email=True,
                last_updated_at=draft.get("draft_updated_at") or draft.get("updated_at"),
            )
        if run_prepublish:
            self.prepublish_draft(publication_url, draft_id, publish_date=trigger_at)
        response = requests.post(
            f"{publication_url}/api/v1/drafts/{draft_id}/scheduled_release",
            headers=self._headers(
                origin=publication_url,
                referer=f"{publication_url}/publish/post/{draft_id}",
            ),
            json={
                "trigger_at": trigger_at,
                "post_audience": post_audience,
                "email_audience": email_audience,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def unschedule_draft(self, publication_url: str, draft_id: int | str) -> Any:
        publication_url = publication_url.rstrip("/")
        response = requests.delete(
            f"{publication_url}/api/v1/drafts/{draft_id}/scheduled_release",
            headers=self._headers(
                origin=publication_url,
                referer=f"{publication_url}/publish/post/{draft_id}",
            ),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


def _print_summary(result: Any) -> None:
    if not isinstance(result, dict):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if "url" in result and "imageWidth" in result:
        summary = {
            "id": result.get("id"),
            "url": result.get("url"),
            "contentType": result.get("contentType"),
            "bytes": result.get("bytes"),
            "imageWidth": result.get("imageWidth"),
            "imageHeight": result.get("imageHeight"),
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    summary = {
        "id": result.get("id"),
        "status": result.get("status"),
        "date": result.get("date"),
        "type": result.get("type"),
        "reply_minimum_role": result.get("reply_minimum_role"),
    }
    if "postSchedules" in result:
        summary["postSchedules"] = result.get("postSchedules")
    for key in ["post_date", "should_send_email", "email_sent_at", "is_published"]:
        if key in result:
            summary[key] = result.get(key)
    if "_uploaded_image" in result:
        uploaded = result["_uploaded_image"]
        summary["uploaded_image"] = {
            "id": uploaded.get("id"),
            "url": uploaded.get("url"),
            "contentType": uploaded.get("contentType"),
            "bytes": uploaded.get("bytes"),
            "imageWidth": uploaded.get("imageWidth"),
            "imageHeight": uploaded.get("imageHeight"),
        }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def _print_draft_summary(draft: dict[str, Any]) -> None:
    summary = {
        "id": draft.get("id"),
        "type": draft.get("type"),
        "title": draft.get("draft_title") or draft.get("title"),
        "subtitle": draft.get("draft_subtitle") or draft.get("subtitle"),
        "audience": draft.get("audience"),
        "draft_updated_at": draft.get("draft_updated_at") or draft.get("updated_at"),
        "post_date": draft.get("post_date"),
        "scheduled_release_at": draft.get("scheduled_release_at"),
        "should_send_email": draft.get("should_send_email"),
        "body_chars": len(draft.get("draft_body") or ""),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def _parse_article_states(value: str) -> list[str]:
    aliases = {
        "published": "published",
        "publish": "published",
        "draft": "draft",
        "drafts": "draft",
        "scheduled": "scheduled",
        "schedule": "scheduled",
    }
    requested = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not requested or "all" in requested:
        return list(ARTICLE_STATES)

    states: list[str] = []
    for state in requested:
        normalized = aliases.get(state)
        if not normalized:
            allowed = ", ".join([*ARTICLE_STATES, "all"])
            raise ValueError(f"Unsupported --state value {state!r}. Expected one or more of: {allowed}.")
        if normalized not in states:
            states.append(normalized)
    return states


def _article_posts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        posts = payload.get("posts") or payload.get("drafts") or []
    elif isinstance(payload, list):
        posts = payload
    else:
        posts = []
    return [post for post in posts if isinstance(post, dict)]


def _article_summary(post: dict[str, Any], state: str) -> dict[str, Any]:
    schedule = post.get("postSchedule") or post.get("post_schedule") or {}
    if not isinstance(schedule, dict):
        schedule = {}
    return {
        "state": state,
        "id": post.get("id") or post.get("post_id"),
        "type": post.get("type"),
        "title": post.get("draft_title") or post.get("title"),
        "subtitle": post.get("draft_subtitle") or post.get("subtitle"),
        "slug": post.get("slug"),
        "post_date": post.get("post_date"),
        "updated_at": post.get("draft_updated_at") or post.get("updated_at"),
        "trigger_at": schedule.get("trigger_at") or post.get("trigger_at") or post.get("scheduled_release_at"),
        "is_published": post.get("is_published"),
        "is_scheduled": post.get("is_scheduled"),
    }


def _print_article_list_summary(result: dict[str, Any]) -> None:
    summary: dict[str, Any] = {"states": {}}
    for state, payload in result.items():
        posts = _article_posts(payload)
        state_summary: dict[str, Any] = {
            "count": len(posts),
            "articles": [_article_summary(post, state) for post in posts],
        }
        if isinstance(payload, dict):
            for key in ["offset", "limit", "total", "isCapped", "hasMore", "nextCursor"]:
                if key in payload:
                    state_summary[key] = payload.get(key)
        summary["states"][state] = state_summary
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def _load_body_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("type") != "doc":
        raise ValueError("Body JSON file must contain a Substack doc node.")
    return data


def _body_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.body_json:
        return _load_body_file(args.body_json)
    if args.body_file:
        return markdown_to_doc(args.body_file)

    nodes: list[dict[str, Any]] = []
    for kind, value in args.block or []:
        if kind == "paragraph":
            nodes.append(paragraph_node(value))
        elif kind == "heading":
            nodes.append(heading_node(value, args.heading_level))

    if args.image_url:
        nodes.append(
            image_node(
                publication_url=args.publication_url,
                draft_id=args.draft_id,
                src=args.image_url,
                width=args.image_width,
                height=args.image_height,
                byte_count=args.image_bytes,
                content_type=args.image_content_type,
                alt=args.image_alt,
                title=args.image_title,
            )
        )

    return doc_node(nodes)


def main() -> None:
    parser = argparse.ArgumentParser(description="Substack API helper.")
    parser.add_argument("--token", help="Raw substack.sid cookie value.")
    parser.add_argument(
        "--token-path",
        type=Path,
        help=f"Path to raw substack.sid cookie value (default: {DEFAULT_TOKEN_PATH}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default: 30).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    post_note = subparsers.add_parser("post-note", help="Post a Substack note.")
    post_note.add_argument("text", help="Note text to post.")
    post_note.add_argument(
        "--reply-minimum-role",
        default="everyone",
        help="Who can reply to the note (default: everyone).",
    )
    upload_image = subparsers.add_parser("upload-image", help="Upload an image to Substack media.")
    upload_image.add_argument("image_path", type=Path, help="Image file to upload.")
    upload_image.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")
    upload_image.add_argument("--draft-id", help="Optional draft id for the Referer header.")
    upload_image.add_argument("--content-type", help="Override image content type.")

    append_image = subparsers.add_parser("append-image", help="Upload an image and append it to an existing draft.")
    append_image.add_argument("draft_id", help="Existing draft id.")
    append_image.add_argument("image_path", type=Path, help="Image file to upload and append.")
    append_image.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")
    append_image.add_argument("--byline-user-id", type=int, help="Optional Substack user id for draft_bylines.")
    append_image.add_argument("--content-type", help="Override image content type.")
    append_image.add_argument("--alt", help="Image alt text.")
    append_image.add_argument("--title", help="Image title.")

    fetch_draft = subparsers.add_parser("fetch-draft", help="Fetch an existing Substack article draft.")
    fetch_draft.add_argument("draft_id", help="Existing draft id.")
    fetch_draft.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")
    fetch_draft.add_argument("--full", action="store_true", help="Print the full draft JSON instead of a compact summary.")

    list_articles = subparsers.add_parser("list-articles", help="List Substack publication articles by state.")
    list_articles.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")
    list_articles.add_argument(
        "--state",
        default="published,draft,scheduled",
        help="Comma-separated states to include: published,draft,scheduled,all (default: all).",
    )
    list_articles.add_argument("--offset", type=int, default=0, help="Pagination offset for each requested state (default: 0).")
    list_articles.add_argument("--limit", type=int, default=25, help="Pagination limit for each requested state (default: 25).")
    list_articles.add_argument("--full", action="store_true", help="Print the raw JSON grouped by state instead of a compact summary.")

    create_draft = subparsers.add_parser("create-draft", help="Create an unpublished Substack article draft.")
    create_draft.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")
    create_draft.add_argument("--publication-id", required=True, type=int, help="Publication id.")
    create_draft.add_argument("--byline-user-id", required=True, type=int, help="Substack user id for draft_bylines.")
    create_draft.add_argument("--draft-type", default="newsletter", help="Draft type (default: newsletter).")
    create_draft.add_argument("--title", help="Optional title to save after creating the draft.")
    create_draft.add_argument("--subtitle", default="", help="Optional subtitle to save after creating the draft.")
    create_draft.add_argument("--send-email", action="store_true", help="Mark the draft as intended for subscriber email.")
    create_draft.add_argument("--body-json", type=Path, help="Path to a complete Substack doc JSON body.")
    create_draft.add_argument("--body-file", type=Path, help="Path to a markdown body file.")
    create_draft.add_argument(
        "--paragraph",
        dest="block",
        action=_AppendBlockAction,
        metavar="TEXT",
        help="Append a paragraph block. Repeat with --heading to preserve order.",
    )
    create_draft.add_argument(
        "--heading",
        dest="block",
        action=_AppendBlockAction,
        metavar="TEXT",
        help="Append a heading block. Repeat with --paragraph to preserve order.",
    )
    create_draft.add_argument("--heading-level", type=int, default=1, help="Heading level for --heading blocks (default: 1).")
    create_draft.add_argument("--image-url", help="Already-uploaded Substack image URL to embed.")
    create_draft.add_argument("--image-width", type=int, default=0, help="Image width in pixels.")
    create_draft.add_argument("--image-height", type=int, default=0, help="Image height in pixels.")
    create_draft.add_argument("--image-bytes", type=int, default=0, help="Image byte size.")
    create_draft.add_argument("--image-content-type", default="image/png", help="Image content type (default: image/png).")
    create_draft.add_argument("--image-alt", help="Image alt text.")
    create_draft.add_argument("--image-title", help="Image title.")

    save_draft = subparsers.add_parser("save-draft", help="Save an existing Substack article draft.")
    save_draft.add_argument("draft_id", help="Existing draft id, e.g. 204727458.")
    save_draft.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")
    save_draft.add_argument("--title", required=True, help="Draft title.")
    save_draft.add_argument("--subtitle", default="", help="Draft subtitle.")
    save_draft.add_argument("--byline-user-id", type=int, help="Optional Substack user id for draft_bylines.")
    save_draft.add_argument("--send-email", action="store_true", help="Mark the draft as intended for subscriber email.")
    save_draft.add_argument("--body-json", type=Path, help="Path to a complete Substack doc JSON body.")
    save_draft.add_argument("--body-file", type=Path, help="Path to a markdown body file.")
    save_draft.add_argument(
        "--paragraph",
        dest="block",
        action=_AppendBlockAction,
        metavar="TEXT",
        help="Append a paragraph block. Repeat with --heading to preserve order.",
    )
    save_draft.add_argument(
        "--heading",
        dest="block",
        action=_AppendBlockAction,
        metavar="TEXT",
        help="Append a heading block. Repeat with --paragraph to preserve order.",
    )
    save_draft.add_argument("--heading-level", type=int, default=1, help="Heading level for --heading blocks (default: 1).")
    save_draft.add_argument("--image-url", help="Already-uploaded Substack image URL to embed.")
    save_draft.add_argument("--image-width", type=int, default=0, help="Image width in pixels.")
    save_draft.add_argument("--image-height", type=int, default=0, help="Image height in pixels.")
    save_draft.add_argument("--image-bytes", type=int, default=0, help="Image byte size.")
    save_draft.add_argument("--image-content-type", default="image/png", help="Image content type (default: image/png).")
    save_draft.add_argument("--image-alt", help="Image alt text.")
    save_draft.add_argument("--image-title", help="Image title.")

    prepublish = subparsers.add_parser("prepublish", help="Run Substack's prepublish check for a draft.")
    prepublish.add_argument("draft_id", help="Existing draft id.")
    prepublish.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")
    prepublish.add_argument("--publish-date", help="Optional scheduled publish timestamp, e.g. 2026-07-06T13:30:00.000Z.")

    publish = subparsers.add_parser("publish-draft", help="Publish an existing Substack article draft.")
    publish.add_argument("draft_id", help="Existing draft id.")
    publish.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")
    publish.add_argument("--send-email", action="store_true", help="Email subscribers when publishing.")

    schedule = subparsers.add_parser("schedule-draft", help="Schedule an existing Substack article draft.")
    schedule.add_argument("draft_id", help="Existing draft id.")
    schedule.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")
    schedule.add_argument(
        "--trigger-at",
        required=True,
        help="UTC ISO timestamp, e.g. 2026-07-06T13:30:00.000Z.",
    )
    schedule.add_argument("--post-audience", default="everyone", help="Post audience (default: everyone).")
    schedule.add_argument("--email-audience", default="everyone", help="Email audience (default: everyone).")
    schedule.add_argument("--send-email", action="store_true", help="Mark the draft for subscriber email before scheduling.")
    schedule.add_argument("--byline-user-id", type=int, help="Optional Substack user id to preserve draft_bylines when using --send-email.")
    schedule.add_argument(
        "--skip-prepublish",
        action="store_true",
        help="Skip the prepublish check with publish_date before scheduling.",
    )

    unschedule = subparsers.add_parser("unschedule-draft", help="Unschedule an existing Substack article draft.")
    unschedule.add_argument("draft_id", help="Existing draft id.")
    unschedule.add_argument("--publication-url", required=True, help="Publication origin, e.g. https://example.substack.com.")

    args = parser.parse_args()

    try:
        client = Substack(
            token=args.token,
            token_path=args.token_path,
            timeout=args.timeout,
        )
        if args.command == "post-note":
            result = client.post_note(args.text, args.reply_minimum_role)
        elif args.command == "upload-image":
            result = client.upload_image(
                publication_url=args.publication_url,
                image_path=args.image_path,
                draft_id=args.draft_id,
                content_type=args.content_type,
            )
        elif args.command == "append-image":
            result = client.append_image_to_draft(
                publication_url=args.publication_url,
                draft_id=args.draft_id,
                image_path=args.image_path,
                byline_user_id=args.byline_user_id,
                content_type=args.content_type,
                alt=args.alt,
                title=args.title,
            )
        elif args.command == "fetch-draft":
            result = client.fetch_draft(args.publication_url, args.draft_id)
            if args.full:
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return
            _print_draft_summary(result)
            return
        elif args.command == "list-articles":
            result = client.list_articles(
                publication_url=args.publication_url,
                states=_parse_article_states(args.state),
                offset=args.offset,
                limit=args.limit,
            )
            if args.full:
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return
            _print_article_list_summary(result)
            return
        elif args.command == "create-draft":
            result = client.create_draft(
                publication_url=args.publication_url,
                publication_id=args.publication_id,
                byline_user_id=args.byline_user_id,
                draft_type=args.draft_type,
            )
            if args.title or args.body_json or args.body_file or args.block or args.image_url:
                result = client.save_draft(
                    publication_url=args.publication_url,
                    draft_id=result["id"],
                    title=args.title or "",
                    subtitle=args.subtitle,
                    body=_body_from_args(args),
                    byline_user_id=args.byline_user_id,
                    should_send_email=args.send_email,
                    last_updated_at=result.get("draft_updated_at"),
                )
        elif args.command == "save-draft":
            result = client.save_draft(
                publication_url=args.publication_url,
                draft_id=args.draft_id,
                title=args.title,
                subtitle=args.subtitle,
                body=_body_from_args(args),
                byline_user_id=args.byline_user_id,
                should_send_email=args.send_email,
            )
        elif args.command == "prepublish":
            result = client.prepublish_draft(args.publication_url, args.draft_id, publish_date=args.publish_date)
        elif args.command == "publish-draft":
            result = client.publish_draft(args.publication_url, args.draft_id, send=args.send_email)
        elif args.command == "schedule-draft":
            result = client.schedule_draft(
                publication_url=args.publication_url,
                draft_id=args.draft_id,
                trigger_at=args.trigger_at,
                post_audience=args.post_audience,
                email_audience=args.email_audience,
                run_prepublish=not args.skip_prepublish,
                should_send_email=args.send_email,
                byline_user_id=args.byline_user_id,
            )
        elif args.command == "unschedule-draft":
            result = client.unschedule_draft(args.publication_url, args.draft_id)
        else:
            parser.error(f"Unsupported command: {args.command}")
    except requests.HTTPError as exc:
        response = exc.response
        print(f"HTTP {response.status_code}: {response.text[:1000]}", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"Substack request failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    _print_summary(result)


if __name__ == "__main__":
    main()
