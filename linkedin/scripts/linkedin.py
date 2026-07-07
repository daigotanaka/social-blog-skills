#!/usr/bin/env python3
"""Small LinkedIn newsletter helper built from a captured Voyager HAR.

This uses LinkedIn's private web API shape captured in
../linkedin/.secrets/linkedin-newsletter.har. It is intentionally small and
HAR-native, like ../substack/scripts/substack.py, rather than a public SDK.

Usage:
    python3 scripts/linkedin.py schedule-newsletter 7478956580596654080 \
        --content-series-urn urn:li:fsd_contentSeries:7374948590156357632 \
        --title "Article title" \
        --subtitle "Short article subtitle or description" \
        --header-image ./cover.jpg \
        --post-text-file ./post.txt \
        --scheduled-at 2026-07-05T09:30:00-07:00

    python3 scripts/linkedin.py save-article urn:li:fsd_firstPartyArticle:7478956580596654080 \
        --title "Article title" \
        --subtitle "Short article subtitle or description" \
        --paragraph "First paragraph." \
        --heading "Section heading" \
        --paragraph "Second paragraph."

    python3 scripts/linkedin.py schedule-post 7478956580596654080 \
        --content-series-urn 7374948590156357632 \
        --post-text "Accompanying LinkedIn post text." \
        --scheduled-at 1783344600000

Usage as a module:
    from linkedin import LinkedIn
    client = LinkedIn(cookie_path=".secrets/linkedin-cookie.txt")
    client.schedule_article_share(...)
"""

from __future__ import annotations

import argparse
from html import escape
import json
import mimetypes
import os
import re
import sys
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import requests


LINKEDIN_ORIGIN = "https://www.linkedin.com"
UPLOAD_METADATA_URL = f"{LINKEDIN_ORIGIN}/voyager/api/voyagerVideoDashMediaUploadMetadata?action=upload"
ARTICLE_URL = f"{LINKEDIN_ORIGIN}/voyager/api/voyagerPublishingDashFirstPartyArticles"
GRAPHQL_URL = f"{LINKEDIN_ORIGIN}/voyager/api/graphql"
SHAREBOX_QUERY_ID = "voyagerContentcreationDashSharebox.6065bbd24f145384527c50bfc0c387ed"
CREATE_SHARE_QUERY_ID = "voyagerContentcreationDashShares.80089eb2e82a2dfa23cb621fb09eb7bf"
DELETE_SHARE_QUERY_ID = "voyagerContentcreationDashShares.b7155044c276d51764fc9981037204b3"
DEFAULT_COOKIE_PATH = Path(".secrets/linkedin-cookie.txt")
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)
_DEFAULT_X_LI_TRACK = {
    "clientVersion": "1.13.45173",
    "mpVersion": "1.13.45173",
    "osName": "web",
    "timezoneOffset": -7,
    "timezone": "America/Los_Angeles",
    "deviceFormFactor": "DESKTOP",
    "mpName": "voyager-web",
    "displayDensity": 2,
    "displayWidth": 3840,
    "displayHeight": 2160,
}


class _AppendBlockAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | list[str] | None,
        option_string: str | None = None,
    ) -> None:
        blocks = getattr(namespace, self.dest, None) or []
        if not isinstance(values, str):
            parser.error(f"{option_string} requires text")
        kind_by_option = {
            "--paragraph": "PARAGRAPH",
            "--heading": "HEADING_2",
            "--heading1": "HEADING_1",
            "--quote": "QUOTE",
            "--code": "CODE_BLOCK",
        }
        blocks.append((kind_by_option.get(option_string, "PARAGRAPH"), values))
        setattr(namespace, self.dest, blocks)


def _get_cookie(cookie: str | None = None, cookie_path: str | Path | None = None) -> str:
    if cookie:
        return cookie.strip()
    if cookie_path:
        path = Path(cookie_path)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    env_cookie = os.environ.get("LINKEDIN_COOKIE", "")
    if env_cookie:
        return env_cookie.strip()
    li_at = os.environ.get("LINKEDIN_LI_AT", "")
    if li_at:
        parts = [f"li_at={li_at.strip()}"]
        jsessionid = os.environ.get("LINKEDIN_JSESSIONID", "")
        if jsessionid:
            parts.append(f'JSESSIONID="{jsessionid.strip().strip(chr(34))}"')
        return "; ".join(parts)
    if DEFAULT_COOKIE_PATH.exists():
        return DEFAULT_COOKIE_PATH.read_text(encoding="utf-8").strip()
    raise ValueError(
        "Missing LinkedIn cookie. Provide --cookie, --cookie-path, LINKEDIN_COOKIE, "
        f"or {DEFAULT_COOKIE_PATH}. The cookie should include li_at and JSESSIONID."
    )


def _cookie_value(cookie_header: str, name: str) -> str | None:
    parsed = SimpleCookie()
    try:
        parsed.load(cookie_header)
    except Exception:
        parsed = SimpleCookie()
    if name in parsed:
        return parsed[name].value

    prefix = f"{name}="
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix) :].strip().strip('"')
    return None


def _get_csrf_token(cookie_header: str, csrf_token: str | None = None) -> str:
    if csrf_token:
        return csrf_token.strip().strip('"')
    env = os.environ.get("LINKEDIN_CSRF_TOKEN", "")
    if env:
        return env.strip().strip('"')
    jsessionid = _cookie_value(cookie_header, "JSESSIONID")
    if jsessionid:
        return jsessionid.strip('"')
    raise ValueError(
        "Missing LinkedIn CSRF token. Provide --csrf-token, LINKEDIN_CSRF_TOKEN, "
        "or a cookie containing JSESSIONID."
    )


def first_party_article_urn(article: str | int) -> str:
    text = str(article)
    if text.startswith("urn:li:fsd_firstPartyArticle:"):
        return text
    if text.startswith("urn:li:linkedInArticle:"):
        return "urn:li:fsd_firstPartyArticle:" + text.rsplit(":", 1)[-1]
    return f"urn:li:fsd_firstPartyArticle:{text}"


def linked_in_article_urn(article: str | int) -> str:
    text = str(article)
    if text.startswith("urn:li:linkedInArticle:"):
        return text
    if text.startswith("urn:li:fsd_firstPartyArticle:"):
        return "urn:li:linkedInArticle:" + text.rsplit(":", 1)[-1]
    return f"urn:li:linkedInArticle:{text}"


def ugc_post_urn(post: str | int) -> str:
    text = str(post)
    if text.startswith("urn:li:fsd_share:urn:li:ugcPost:"):
        return text.replace("urn:li:fsd_share:", "", 1)
    if text.startswith("urn:li:ugcPost:"):
        return text
    return f"urn:li:ugcPost:{text}"


def content_series_urn(series: str | int) -> str:
    text = str(series)
    if text.startswith("urn:li:fsd_contentSeries:"):
        return text
    return f"urn:li:fsd_contentSeries:{text}"


def article_list_state(value: str) -> str:
    normalized = value.strip().upper()
    aliases = {
        "DRAFT": "DRAFT",
        "DRAFTS": "DRAFT",
        "SCHEDULED": "SCHEDULED",
        "SCHEDULE": "SCHEDULED",
        "PUBLISHED": "PUBLISHED",
        "PUBLISH": "PUBLISHED",
    }
    if normalized not in aliases:
        raise ValueError(f"Unsupported article filter: {value}. Use draft, scheduled, or published.")
    return aliases[normalized]


def post_list_filter(value: str) -> str:
    normalized = value.strip().upper()
    aliases = {
        "SCHEDULED": "SCHEDULED",
        "SCHEDULE": "SCHEDULED",
    }
    if normalized not in aliases:
        raise ValueError(f"Unsupported post filter: {value}. Use scheduled.")
    return aliases[normalized]


def text_view(text: str) -> dict[str, Any]:
    return {
        "text": text,
        "attributesV2": [],
        "$type": "com.linkedin.voyager.dash.common.text.TextViewModel",
    }


def text_block(text: str, block_type: str = "PARAGRAPH") -> dict[str, Any]:
    return {
        "textBlock": {
            "type": block_type,
            "content": text_view(text),
            "$type": "com.linkedin.voyager.dash.publishing.TextBlock",
        }
    }


def content_from_blocks(blocks: list[tuple[str, str]] | None) -> list[dict[str, Any]] | None:
    if not blocks:
        return None
    return [text_block(text, kind) for kind, text in blocks]


def list_block(items: list[str], ordered: bool = False) -> dict[str, Any]:
    prefix = "1. " if ordered else "- "
    return text_block("\n".join(f"{prefix}{item}" for item in items), "PARAGRAPH")


def _inline_text(markdown_text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", markdown_text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return text.strip()


def _inline_html(markdown_text: str) -> str:
    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\u0000{len(placeholders) - 1}\u0000"

    def image_repl(match: re.Match[str]) -> str:
        alt = escape(match.group(1), quote=True)
        src = escape(match.group(2), quote=True)
        return stash(f'<img src="{src}" alt="{alt}">')

    def link_repl(match: re.Match[str]) -> str:
        label = escape(match.group(1))
        href = escape(match.group(2), quote=True)
        return stash(f'<a href="{href}">{label}</a>')

    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", image_repl, markdown_text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, text)
    text = escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    for index, value in enumerate(placeholders):
        text = text.replace(f"\u0000{index}\u0000", value)
    return text


def content_blocks_to_html(content: list[dict[str, Any]] | None) -> str:
    if not content:
        return ""

    parts: list[str] = []
    for block in content:
        text_block_value = block.get("textBlock") or {}
        block_type = text_block_value.get("type") or "PARAGRAPH"
        text = (text_block_value.get("content") or {}).get("text") or ""
        html_text = escape(text).replace("\n", "<br>")
        attrs = (text_block_value.get("content") or {}).get("attributesV2") or []

        if attrs and all((attr.get("detailDataUnion") or {}).get("listItemStyle") for attr in attrs):
            ordered = any((attr.get("detailDataUnion") or {}).get("listItemStyle", {}).get("type") == "ORDERED" for attr in attrs)
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            for attr in attrs:
                start = int(attr.get("start") or 0)
                length = int(attr.get("length") or 0)
                items.append(f"<li><p>{escape(text[start:start + length])}</p></li>")
            parts.append(f"<{tag}>{''.join(items)}</{tag}>")
        elif block_type == "HEADING_1":
            parts.append(f"<h2>{html_text}</h2>")
        elif block_type == "HEADING_2":
            parts.append(f"<h3>{html_text}</h3>")
        elif block_type == "QUOTE":
            parts.append(f"<blockquote><p>{html_text}</p></blockquote>")
        elif block_type == "CODE_BLOCK":
            parts.append(f"<pre>{escape(text)}</pre>")
        else:
            parts.append(f"<p>{html_text}</p>")
    return "".join(parts)


def markdown_to_article(markdown_text: str) -> dict[str, Any]:
    title: str | None = None
    content: list[dict[str, Any]] = []
    html_parts: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_html_items: list[str] = []
    list_ordered = False
    quote: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        md_text = " ".join(part.strip() for part in paragraph).strip()
        if md_text:
            content.append(text_block(_inline_text(md_text), "PARAGRAPH"))
            html_parts.append(f"<p>{_inline_html(md_text)}</p>")
        paragraph = []

    def flush_list() -> None:
        nonlocal list_items, list_html_items, list_ordered
        if not list_items:
            return
        content.append(list_block(list_items, ordered=list_ordered))
        html_parts.append(content_blocks_to_html([content[-1]]))
        list_items = []
        list_html_items = []
        list_ordered = False

    def flush_quote() -> None:
        nonlocal quote
        if not quote:
            return
        md_text = " ".join(part.strip() for part in quote).strip()
        if md_text:
            content.append(text_block(_inline_text(md_text), "QUOTE"))
            html_parts.append(f"<blockquote><p>{_inline_html(md_text)}</p></blockquote>")
        quote = []

    def flush_code() -> None:
        nonlocal code_lines
        text = "\n".join(code_lines).rstrip()
        content.append(text_block(text + ("\n" if text else ""), "CODE_BLOCK"))
        html_parts.append(f"<pre>{escape(text)}</pre>")
        code_lines = []

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            flush_quote()
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not stripped:
            flush_paragraph()
            flush_list()
            flush_quote()
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            flush_quote()
            level = len(heading.group(1))
            text = _inline_text(heading.group(2))
            html = _inline_html(heading.group(2))
            if level == 1 and title is None:
                title = text
                continue
            block_type = "HEADING_1" if level <= 1 else "HEADING_2"
            content.append(text_block(text, block_type))
            html_tag = "h2" if block_type == "HEADING_1" else "h3"
            html_parts.append(f"<{html_tag}>{html}</{html_tag}>")
            continue

        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        ordered = re.match(r"^\d+\.\s+(.+)$", stripped)
        if bullet or ordered:
            flush_paragraph()
            flush_quote()
            is_ordered = ordered is not None
            item_md = (ordered or bullet).group(1)
            if list_items and list_ordered != is_ordered:
                flush_list()
            list_ordered = is_ordered
            list_items.append(_inline_text(item_md))
            list_html_items.append(f"<li><p>{_inline_html(item_md)}</p></li>")
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            flush_list()
            quote.append(stripped.lstrip(">").strip())
            continue

        paragraph.append(stripped)

    if in_code:
        flush_code()
    flush_paragraph()
    flush_list()
    flush_quote()

    return {
        "title": title,
        "content": content,
        "content_html": content_blocks_to_html(content),
    }


def load_markdown_arg(markdown: str | None, path: Path | None) -> dict[str, Any] | None:
    if markdown is None and path is None:
        return None
    text = markdown if markdown is not None else path.read_text(encoding="utf-8")
    return markdown_to_article(text)


def load_json_file(path: Path | None) -> Any:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_text_arg(text: str | None, path: Path | None, label: str) -> str:
    if text is not None:
        return text
    if path is not None:
        return path.read_text(encoding="utf-8")
    raise ValueError(f"Missing {label}. Provide --{label} or --{label}-file.")


def parse_scheduled_at(value: str | int) -> str:
    text = str(value).strip()
    if text.isdigit():
        return text

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    millis = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    return str(millis)


class LinkedIn:
    def __init__(
        self,
        cookie: str | None = None,
        cookie_path: str | Path | None = None,
        csrf_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._cookie = _get_cookie(cookie=cookie, cookie_path=cookie_path)
        self._csrf_token = _get_csrf_token(self._cookie, csrf_token)
        self.timeout = timeout

    def _headers(
        self,
        *,
        accept: str = "application/vnd.linkedin.normalized+json+2.1",
        content_type: str | None = "application/json; charset=UTF-8",
        referer: str | None = None,
        pem_metadata: str | None = None,
        include_restli: bool = True,
    ) -> dict[str, str]:
        headers = {
            "accept": accept,
            "cookie": self._cookie,
            "csrf-token": self._csrf_token,
            "origin": LINKEDIN_ORIGIN,
            "referer": referer or f"{LINKEDIN_ORIGIN}/",
            "user-agent": _BROWSER_UA,
            "x-li-lang": "en_US",
            "x-li-track": json.dumps(_DEFAULT_X_LI_TRACK, separators=(",", ":")),
        }
        if content_type is not None:
            headers["content-type"] = content_type
        if include_restli:
            headers["x-restli-protocol-version"] = "2.0.0"
        if pem_metadata:
            headers["x-li-pem-metadata"] = pem_metadata
        return headers

    def fetch_sharebox(self, origin: str = "PUBLISHING") -> dict[str, Any]:
        payload = {
            "variables": {"origin": origin},
            "queryId": SHAREBOX_QUERY_ID,
            "includeWebMetadata": True,
        }
        response = requests.post(
            f"{GRAPHQL_URL}?action=execute&queryId={SHAREBOX_QUERY_ID}",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def upload_cover_image(
        self,
        image_path: str | Path,
        article: str | int | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        image_path = Path(image_path)
        content_type = content_type or mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        referer = (
            f"{LINKEDIN_ORIGIN}/article/edit/{str(article).rsplit(':', 1)[-1]}/"
            if article
            else f"{LINKEDIN_ORIGIN}/"
        )
        metadata_payload = {
            "mediaUploadType": "PUBLISHING_COVER_IMAGE",
            "fileSize": image_path.stat().st_size,
            "filename": image_path.name,
        }
        metadata_response = requests.post(
            UPLOAD_METADATA_URL,
            headers=self._headers(referer=referer),
            json=metadata_payload,
            timeout=self.timeout,
        )
        metadata_response.raise_for_status()
        metadata = metadata_response.json()["data"]["value"]

        upload_headers = self._headers(
            accept="*/*",
            content_type=content_type,
            referer=referer,
            include_restli=False,
        )
        for key, value in (metadata.get("singleUploadHeaders") or {}).items():
            upload_headers[key] = str(value)
        upload_response = requests.put(
            metadata["singleUploadUrl"],
            headers=upload_headers,
            data=image_path.read_bytes(),
            timeout=self.timeout,
        )
        upload_response.raise_for_status()

        result = dict(metadata)
        result["_upload_status"] = upload_response.status_code
        return result

    def create_article(
        self,
        *,
        author_profile_urn: str,
        content_series: str | int,
        title: str,
        content: list[dict[str, Any]] | None = None,
        content_html: str | None = None,
        state: str = "AUTOSAVED",
    ) -> dict[str, Any]:
        payload = {
            "authors": [{"profileUrn": author_profile_urn}],
            "contentHtml": content_html if content_html is not None else content_blocks_to_html(content),
            "state": state,
            "title": title,
            "series": {
                "entityUrn": content_series_urn(content_series),
            },
        }
        if content is not None:
            payload["content"] = content

        response = requests.post(
            f"{ARTICLE_URL}/",
            headers=self._headers(
                referer=f"{LINKEDIN_ORIGIN}/preload/?_bprMode=vanilla",
                pem_metadata="Voyager - Article Creator=autosave-article",
            ),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def fetch_latest_draft(self, article: str | int) -> dict[str, Any]:
        article_urn = first_party_article_urn(article)
        response = requests.get(
            ARTICLE_URL,
            headers=self._headers(
                referer=f"{LINKEDIN_ORIGIN}/preload/?_bprMode=vanilla",
                pem_metadata="Voyager - Article Creator=fetch-article",
            ),
            params={
                "q": "latestDraftById",
                "firstPartyArticleUrn": article_urn,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def list_articles(
        self,
        *,
        author_profile_urn: str,
        states: list[str],
        start: int = 0,
    ) -> dict[str, Any]:
        results = []
        for state in states:
            response = requests.get(
                ARTICLE_URL,
                headers=self._headers(
                    referer=f"{LINKEDIN_ORIGIN}/preload/?_bprMode=vanilla",
                    pem_metadata="Voyager - Article Creator=list-articles",
                ),
                params={
                    "author": author_profile_urn,
                    "q": "author",
                    "start": start,
                    "state": state,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            results.append({"state": state, "response": response.json()})
        return {"_article_lists": results}

    def list_posts(
        self,
        *,
        author_profile_urn: str,
        filters: list[str],
        start: int = 0,
    ) -> dict[str, Any]:
        results = []
        for post_filter in filters:
            response = requests.get(
                ARTICLE_URL,
                headers=self._headers(
                    referer=f"{LINKEDIN_ORIGIN}/preload/?_bprMode=vanilla",
                    pem_metadata="Voyager - Publishing=list-post",
                ),
                params={
                    "author": author_profile_urn,
                    "q": "author",
                    "start": start,
                    "state": post_filter,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            results.append({"filter": post_filter, "response": response.json()})
        return {"_post_lists": results}

    def patch_article(
        self,
        article: str | int,
        *,
        title: str | None = None,
        subtitle: str | None = None,
        content: list[dict[str, Any]] | None = None,
        content_html: str | None = None,
        cover_image_urn: str | None = None,
        cover_caption: str = "",
        raw_set: dict[str, Any] | None = None,
        state: str = "AUTOSAVED",
    ) -> dict[str, Any]:
        article_urn = first_party_article_urn(article)
        patch_set: dict[str, Any] = {"state": state}
        if title is not None:
            patch_set["title"] = title
            patch_set["seoTitle"] = title
        if subtitle is not None:
            patch_set["contentDescription"] = subtitle
            patch_set["seoDescription"] = subtitle
        if content is not None:
            patch_set["content"] = content
            patch_set["contentHtml"] = content_html if content_html is not None else content_blocks_to_html(content)
        elif content_html is not None:
            patch_set["contentHtml"] = content_html
        if cover_image_urn is not None:
            patch_set["coverMediaV2Union"] = {
                "coverImage": {
                    "originalImageUrn": cover_image_urn,
                    "caption": {"text": cover_caption},
                }
            }
        if raw_set:
            patch_set.update(raw_set)

        response = requests.post(
            f"{ARTICLE_URL}/{article_urn}",
            headers=self._headers(
                referer=f"{LINKEDIN_ORIGIN}/article/edit/{article_urn.rsplit(':', 1)[-1]}/",
                pem_metadata="Voyager - Article Creator=autosave-article",
            ),
            json={"patch": {"$set": patch_set}},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def schedule_article_share(
        self,
        article: str | int,
        *,
        content_series: str | int,
        post_text: str,
        scheduled_at: str | int,
        allowed_commenters_scope: str = "ALL",
        origin: str = "PUBLISHING",
    ) -> dict[str, Any]:
        article_media_urn = linked_in_article_urn(article)
        series_urn = content_series_urn(content_series)
        payload = {
            "variables": {
                "post": {
                    "allowedCommentersScope": allowed_commenters_scope,
                    "intendedShareLifeCycleState": "SCHEDULED",
                    "origin": origin,
                    "visibilityDataUnion": {
                        "containerVisibility": {
                            "variant": "GROUP",
                            "containerV2": {
                                "containerEntityUrn": series_urn,
                            },
                        }
                    },
                    "commentary": {
                        "text": post_text,
                        "attributesV2": [],
                    },
                    "media": {
                        "mediaUrn": article_media_urn,
                        "category": "URN_REFERENCE",
                    },
                    "scheduledAt": parse_scheduled_at(scheduled_at),
                }
            },
            "queryId": CREATE_SHARE_QUERY_ID,
            "includeWebMetadata": True,
        }
        response = requests.post(
            f"{GRAPHQL_URL}?action=execute&queryId={CREATE_SHARE_QUERY_ID}",
            headers=self._headers(
                referer=f"{LINKEDIN_ORIGIN}/article/edit/{article_media_urn.rsplit(':', 1)[-1]}/",
                pem_metadata=(
                    "Voyager - Sharing - CreateShare=sharing-create-content,"
                    "Voyager - Groups - Post to Group=post-to-group"
                ),
            ),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def delete_scheduled_share(self, post: str | int) -> dict[str, Any]:
        resource_key = ugc_post_urn(post)
        payload = {
            "variables": {
                "resourceKey": resource_key,
            },
            "queryId": DELETE_SHARE_QUERY_ID,
            "includeWebMetadata": True,
        }
        response = requests.post(
            f"{GRAPHQL_URL}?action=execute&queryId={DELETE_SHARE_QUERY_ID}",
            headers=self._headers(
                referer=f"{LINKEDIN_ORIGIN}/preload/?_bprMode=vanilla",
            ),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def schedule_newsletter(
        self,
        article: str | int,
        *,
        content_series: str | int,
        post_text: str,
        scheduled_at: str | int,
        title: str | None = None,
        subtitle: str | None = None,
        header_image: str | Path | None = None,
        header_image_content_type: str | None = None,
        cover_caption: str = "",
        content: list[dict[str, Any]] | None = None,
        content_html: str | None = None,
        raw_set: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cover_urn = None
        uploaded = None
        if header_image is not None:
            uploaded = self.upload_cover_image(
                header_image,
                article=article,
                content_type=header_image_content_type,
            )
            cover_urn = uploaded["urn"]

        saved = None
        if title is not None or subtitle is not None or content is not None or content_html is not None or cover_urn is not None or raw_set:
            saved = self.patch_article(
                article,
                title=title,
                subtitle=subtitle,
                content=content,
                content_html=content_html,
                cover_image_urn=cover_urn,
                cover_caption=cover_caption,
                raw_set=raw_set,
            )

        scheduled = self.schedule_article_share(
            article,
            content_series=content_series,
            post_text=post_text,
            scheduled_at=scheduled_at,
        )
        scheduled["_uploaded_cover_image"] = uploaded
        scheduled["_saved_article"] = saved
        return scheduled


def _extract_article_summary(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") or {}
    return {
        "entityUrn": data.get("entityUrn"),
        "linkedInArticleUrn": data.get("linkedInArticleUrn"),
        "title": data.get("title"),
        "state": data.get("state"),
        "scheduledAt": data.get("scheduledAt"),
        "updatedAt": data.get("updatedAt"),
    }


def _extract_share_summary(result: dict[str, Any]) -> dict[str, Any]:
    created = (
        result.get("data", {})
        .get("data", {})
        .get("createContentcreationDashShares", {})
    )
    return {
        "resourceKey": created.get("resourceKey"),
        "entity": created.get("*entity"),
    }


def _extract_deleted_share_summary(result: dict[str, Any]) -> dict[str, Any]:
    deleted = (
        result.get("data", {})
        .get("data", {})
        .get("deleteContentcreationDashShares", {})
    )
    return {
        "resourceKey": deleted.get("resourceKey"),
    }


def _article_item_by_urn(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for item in result.get("included") or []:
        if not isinstance(item, dict):
            continue
        if item.get("$type") != "com.linkedin.voyager.dash.publishing.FirstPartyArticle":
            continue
        urn = item.get("entityUrn")
        if urn:
            items[urn] = item
    return items


def _summarize_article_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "entityUrn": item.get("entityUrn"),
        "linkedInArticleUrn": item.get("linkedInArticleUrn"),
        "title": item.get("title"),
        "state": item.get("state"),
        "createdAt": item.get("createdAt"),
        "updatedAt": item.get("updatedAt"),
        "publishedAt": item.get("publishedAt"),
        "scheduledAt": item.get("scheduledAt"),
        "initialUpdateUrn": item.get("initialUpdateUrn"),
        "contentBlocks": len(item.get("content") or []),
    }


def _extract_article_list_summary(result: dict[str, Any]) -> dict[str, Any]:
    summaries = []
    for list_result in result.get("_article_lists") or []:
        state = list_result.get("state")
        response = list_result.get("response") or {}
        articles_by_urn = _article_item_by_urn(response)
        elements = (response.get("data") or {}).get("*elements") or []
        articles = []
        seen: set[str] = set()
        for urn in elements:
            item = articles_by_urn.get(urn)
            if item:
                articles.append(_summarize_article_item(item))
                seen.add(urn)
        for urn, item in articles_by_urn.items():
            if urn not in seen:
                articles.append(_summarize_article_item(item))
        summaries.append({"filter": state.lower(), "count": len(articles), "articles": articles})
    return {"filters": summaries}


def _summarize_post_item(item: dict[str, Any]) -> dict[str, Any]:
    scheduled_post_urn = item.get("initialUpdateUrn")
    scheduled_share_urn = f"urn:li:fsd_share:{scheduled_post_urn}" if scheduled_post_urn else None
    return {
        "scheduledPostUrn": scheduled_post_urn,
        "scheduledShareUrn": scheduled_share_urn,
        "scheduledAt": item.get("scheduledAt"),
        "articleUrn": item.get("entityUrn"),
        "linkedInArticleUrn": item.get("linkedInArticleUrn"),
        "title": item.get("title"),
        "state": item.get("state"),
        "updatedAt": item.get("updatedAt"),
        "contentBlocks": len(item.get("content") or []),
    }


def _extract_post_list_summary(result: dict[str, Any]) -> dict[str, Any]:
    summaries = []
    for list_result in result.get("_post_lists") or []:
        post_filter = list_result.get("filter")
        response = list_result.get("response") or {}
        articles_by_urn = _article_item_by_urn(response)
        elements = (response.get("data") or {}).get("*elements") or []
        posts = []
        seen: set[str] = set()
        for urn in elements:
            item = articles_by_urn.get(urn)
            if item:
                posts.append(_summarize_post_item(item))
                seen.add(urn)
        for urn, item in articles_by_urn.items():
            if urn not in seen:
                posts.append(_summarize_post_item(item))
        summaries.append({"filter": post_filter.lower(), "count": len(posts), "posts": posts})
    return {"filters": summaries}


def _print_summary(result: dict[str, Any]) -> None:
    if "singleUploadUrl" in result and "urn" in result:
        summary = {
            "urn": result.get("urn"),
            "mediaArtifactUrn": result.get("mediaArtifactUrn"),
            "recipes": result.get("recipes"),
            "pollingUrl": result.get("pollingUrl"),
            "uploadStatus": result.get("_upload_status"),
        }
    elif result.get("data", {}).get("linkedInArticleUrn"):
        summary = _extract_article_summary(result)
    elif result.get("data", {}).get("data", {}).get("createContentcreationDashShares"):
        summary = _extract_share_summary(result)
        if result.get("_uploaded_cover_image"):
            summary["uploadedCoverImage"] = {
                "urn": result["_uploaded_cover_image"].get("urn"),
                "uploadStatus": result["_uploaded_cover_image"].get("_upload_status"),
            }
        if result.get("_saved_article"):
            summary["savedArticle"] = _extract_article_summary(result["_saved_article"])
        if result.get("_created_article"):
            summary["createdArticle"] = _extract_article_summary(result["_created_article"])
    elif result.get("data", {}).get("data", {}).get("deleteContentcreationDashShares"):
        summary = _extract_deleted_share_summary(result)
    elif result.get("_article_lists"):
        summary = _extract_article_list_summary(result)
    elif result.get("_post_lists"):
        summary = _extract_post_list_summary(result)
    else:
        summary = result
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def _add_article_content_args(parser: argparse.ArgumentParser, include_raw_set: bool = True) -> None:
    parser.add_argument("--markdown", help="Markdown article body. The first # heading is used as the title unless --title is set.")
    parser.add_argument(
        "--markdown-file",
        "--body-file",
        dest="markdown_file",
        type=Path,
        help="Path to markdown article body. Alias: --body-file.",
    )
    parser.add_argument("--content-json", type=Path, help="Path to a complete LinkedIn article content-block JSON array.")
    if include_raw_set:
        parser.add_argument("--raw-set-json", type=Path, help="Extra article PATCH $set object to merge into the request.")
    parser.add_argument(
        "--paragraph",
        dest="block",
        action=_AppendBlockAction,
        metavar="TEXT",
        help="Append a paragraph block. Repeat with --heading, --quote, or --code.",
    )
    parser.add_argument(
        "--heading",
        dest="block",
        action=_AppendBlockAction,
        metavar="TEXT",
        help="Append a HEADING_2 block.",
    )
    parser.add_argument(
        "--heading1",
        dest="block",
        action=_AppendBlockAction,
        metavar="TEXT",
        help="Append a HEADING_1 block.",
    )
    parser.add_argument(
        "--quote",
        dest="block",
        action=_AppendBlockAction,
        metavar="TEXT",
        help="Append a quote block.",
    )
    parser.add_argument(
        "--code",
        dest="block",
        action=_AppendBlockAction,
        metavar="TEXT",
        help="Append a code block.",
    )


def _markdown_article_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    if hasattr(args, "_linkedin_markdown_article"):
        return args._linkedin_markdown_article
    article = load_markdown_arg(
        getattr(args, "markdown", None),
        getattr(args, "markdown_file", None),
    )
    setattr(args, "_linkedin_markdown_article", article)
    return article


def _title_from_args(args: argparse.Namespace, required: bool = False) -> str | None:
    explicit = getattr(args, "title", None)
    if explicit:
        return explicit
    article = _markdown_article_from_args(args)
    title = article.get("title") if article else None
    if required and not title:
        raise ValueError("Missing title. Provide --title or a markdown file whose first heading is '# Title'.")
    return title


def _content_from_args(args: argparse.Namespace) -> list[dict[str, Any]] | None:
    if args.content_json:
        data = load_json_file(args.content_json)
        if not isinstance(data, list):
            raise ValueError("--content-json must contain a JSON array of LinkedIn content blocks.")
        return data
    if args.block:
        return content_from_blocks(args.block)
    article = _markdown_article_from_args(args)
    return article.get("content") if article else None


def _content_html_from_args(args: argparse.Namespace) -> str | None:
    if args.content_html is not None or args.content_html_file is not None:
        return load_text_arg(args.content_html, args.content_html_file, "content-html")
    article = _markdown_article_from_args(args)
    return article.get("content_html") if article else None


def _raw_set_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    raw_set = load_json_file(args.raw_set_json)
    if raw_set is not None and not isinstance(raw_set, dict):
        raise ValueError("--raw-set-json must contain a JSON object.")
    return raw_set


def _author_profile_from_args(args: argparse.Namespace) -> str:
    value = getattr(args, "author_profile_urn", None) or os.environ.get("LINKEDIN_AUTHOR_PROFILE_URN")
    if not value:
        raise ValueError("Missing author profile URN. Provide --author-profile-urn or LINKEDIN_AUTHOR_PROFILE_URN.")
    return value


def _article_states_from_filter(value: str) -> list[str]:
    states = []
    seen = set()
    for part in value.split(","):
        if not part.strip():
            continue
        state = article_list_state(part)
        if state not in seen:
            states.append(state)
            seen.add(state)
    if not states:
        raise ValueError("Missing article filter. Use --filter draft or --filter draft,scheduled,published.")
    return states


def _post_filters_from_filter(value: str) -> list[str]:
    filters = []
    seen = set()
    for part in value.split(","):
        if not part.strip():
            continue
        post_filter = post_list_filter(part)
        if post_filter not in seen:
            filters.append(post_filter)
            seen.add(post_filter)
    if not filters:
        raise ValueError("Missing post filter. Use --filter scheduled.")
    return filters


def main() -> None:
    parser = argparse.ArgumentParser(description="LinkedIn newsletter API helper.")
    parser.add_argument("--cookie", help="Raw LinkedIn Cookie header value.")
    parser.add_argument(
        "--cookie-path",
        type=Path,
        help=f"Path to a raw LinkedIn Cookie header value (default: {DEFAULT_COOKIE_PATH}).",
    )
    parser.add_argument("--csrf-token", help="LinkedIn csrf-token header value, usually the JSESSIONID cookie value.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout in seconds (default: 30).")

    subparsers = parser.add_subparsers(dest="command", required=True)

    sharebox = subparsers.add_parser("fetch-sharebox", help="Fetch LinkedIn sharebox metadata.")
    sharebox.add_argument("--origin", default="PUBLISHING", help="Sharebox origin (default: PUBLISHING).")

    upload = subparsers.add_parser("upload-cover", help="Upload an article/newsletter cover image.")
    upload.add_argument("image_path", type=Path, help="Image file to upload.")
    upload.add_argument("--article-id", help="Optional article id or URN for the Referer header.")
    upload.add_argument("--content-type", help="Override image content type.")

    create = subparsers.add_parser("create-article", help="Create a LinkedIn newsletter article draft.")
    create.add_argument("--author-profile-urn", required=True, help="Author profile URN, e.g. urn:li:fsd_profile:...")
    create.add_argument("--content-series-urn", required=True, help="Newsletter/content series id or fsd_contentSeries URN.")
    create.add_argument("--title", help="Initial article title. Defaults to the first # heading from markdown.")
    create.add_argument("--content-html", help="Optional complete article HTML.")
    create.add_argument("--content-html-file", type=Path, help="Path to optional complete article HTML.")
    _add_article_content_args(create, include_raw_set=False)

    fetch = subparsers.add_parser("fetch-latest-draft", help="Fetch the latest draft for an article id/URN.")
    fetch.add_argument("article", help="Article id, fsd_firstPartyArticle URN, or linkedInArticle URN.")

    list_article = subparsers.add_parser("list-articles", help="List newsletter articles by state.")
    list_article.add_argument("--author-profile-urn", help="Author profile URN, e.g. urn:li:fsd_profile:...")
    list_article.add_argument(
        "--filter",
        default="draft,scheduled,published",
        help='Comma-separated article filters: "draft", "scheduled", "published", or combinations (default: all).',
    )
    list_article.add_argument("--start", type=int, default=0, help="Pagination start offset (default: 0).")

    list_post = subparsers.add_parser("list-post", help="List scheduled LinkedIn posts for newsletter articles.")
    list_post.add_argument("--author-profile-urn", help="Author profile URN, e.g. urn:li:fsd_profile:...")
    list_post.add_argument(
        "--filter",
        default="scheduled",
        help='Comma-separated post filters. Currently supports "scheduled" (default: scheduled).',
    )
    list_post.add_argument("--start", type=int, default=0, help="Pagination start offset (default: 0).")

    save = subparsers.add_parser("save-article", help="Patch an existing LinkedIn newsletter article draft.")
    save.add_argument("article", help="Article id, fsd_firstPartyArticle URN, or linkedInArticle URN.")
    save.add_argument("--title", help="Article title.")
    save.add_argument("--subtitle", help="Article subtitle/description.")
    save.add_argument("--content-html", help="Optional complete article HTML to save with --content-json or block args.")
    save.add_argument("--content-html-file", type=Path, help="Path to optional complete article HTML.")
    save.add_argument("--header-image", type=Path, help="Upload and set a header image.")
    save.add_argument("--header-image-content-type", help="Override header image content type.")
    save.add_argument("--cover-caption", default="", help="Header image caption (default: empty).")
    _add_article_content_args(save)

    schedule = subparsers.add_parser("schedule-post", help="Schedule the accompanying post for an article.")
    schedule.add_argument("article", help="Article id, fsd_firstPartyArticle URN, or linkedInArticle URN.")
    schedule.add_argument("--content-series-urn", required=True, help="Newsletter/content series id or fsd_contentSeries URN.")
    schedule.add_argument("--post-text", help="Accompanying post text.")
    schedule.add_argument("--post-text-file", type=Path, help="Path to accompanying post text.")
    schedule.add_argument("--scheduled-at", required=True, help="ISO datetime or epoch milliseconds.")

    unschedule = subparsers.add_parser(
        "delete-scheduled-share",
        help="Delete a scheduled article share, moving the newsletter article back to drafts.",
    )
    unschedule.add_argument(
        "post",
        help="Scheduled post id, urn:li:ugcPost URN, or urn:li:fsd_share:urn:li:ugcPost URN.",
    )

    newsletter = subparsers.add_parser(
        "schedule-newsletter",
        help="Patch title/subtitle/header image, then schedule the accompanying article post.",
    )
    newsletter.add_argument("article", help="Article id, fsd_firstPartyArticle URN, or linkedInArticle URN.")
    newsletter.add_argument("--content-series-urn", required=True, help="Newsletter/content series id or fsd_contentSeries URN.")
    newsletter.add_argument("--title", help="Article title.")
    newsletter.add_argument("--subtitle", help="Article subtitle/description.")
    newsletter.add_argument("--content-html", help="Optional complete article HTML to save with --content-json or block args.")
    newsletter.add_argument("--content-html-file", type=Path, help="Path to optional complete article HTML.")
    newsletter.add_argument("--header-image", type=Path, help="Upload and set a header image.")
    newsletter.add_argument("--header-image-content-type", help="Override header image content type.")
    newsletter.add_argument("--cover-caption", default="", help="Header image caption (default: empty).")
    newsletter.add_argument("--post-text", help="Accompanying post text.")
    newsletter.add_argument("--post-text-file", type=Path, help="Path to accompanying post text.")
    newsletter.add_argument("--scheduled-at", required=True, help="ISO datetime or epoch milliseconds.")
    _add_article_content_args(newsletter)

    create_newsletter = subparsers.add_parser(
        "create-newsletter",
        help="Create a newsletter article, patch optional title/subtitle/header/body, then schedule the accompanying post.",
    )
    create_newsletter.add_argument("--author-profile-urn", required=True, help="Author profile URN, e.g. urn:li:fsd_profile:...")
    create_newsletter.add_argument("--content-series-urn", required=True, help="Newsletter/content series id or fsd_contentSeries URN.")
    create_newsletter.add_argument("--title", help="Article title. Defaults to the first # heading from markdown.")
    create_newsletter.add_argument("--subtitle", help="Article subtitle/description.")
    create_newsletter.add_argument("--content-html", help="Optional complete article HTML to save with --content-json or block args.")
    create_newsletter.add_argument("--content-html-file", type=Path, help="Path to optional complete article HTML.")
    create_newsletter.add_argument("--header-image", type=Path, help="Upload and set a header image.")
    create_newsletter.add_argument("--header-image-content-type", help="Override header image content type.")
    create_newsletter.add_argument("--cover-caption", default="", help="Header image caption (default: empty).")
    create_newsletter.add_argument("--post-text", help="Accompanying post text.")
    create_newsletter.add_argument("--post-text-file", type=Path, help="Path to accompanying post text.")
    create_newsletter.add_argument("--scheduled-at", required=True, help="ISO datetime or epoch milliseconds.")
    _add_article_content_args(create_newsletter)

    args = parser.parse_args()

    try:
        client = LinkedIn(
            cookie=args.cookie,
            cookie_path=args.cookie_path,
            csrf_token=args.csrf_token,
            timeout=args.timeout,
        )
        if args.command == "fetch-sharebox":
            result = client.fetch_sharebox(origin=args.origin)
        elif args.command == "upload-cover":
            result = client.upload_cover_image(
                args.image_path,
                article=args.article_id,
                content_type=args.content_type,
            )
        elif args.command == "create-article":
            result = client.create_article(
                author_profile_urn=args.author_profile_urn,
                content_series=args.content_series_urn,
                title=_title_from_args(args, required=True),
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
            )
        elif args.command == "fetch-latest-draft":
            result = client.fetch_latest_draft(args.article)
        elif args.command == "list-articles":
            result = client.list_articles(
                author_profile_urn=_author_profile_from_args(args),
                states=_article_states_from_filter(args.filter),
                start=args.start,
            )
        elif args.command == "list-post":
            result = client.list_posts(
                author_profile_urn=_author_profile_from_args(args),
                filters=_post_filters_from_filter(args.filter),
                start=args.start,
            )
        elif args.command == "save-article":
            uploaded = None
            cover_urn = None
            if args.header_image:
                uploaded = client.upload_cover_image(
                    args.header_image,
                    article=args.article,
                    content_type=args.header_image_content_type,
                )
                cover_urn = uploaded["urn"]
            result = client.patch_article(
                args.article,
                title=_title_from_args(args),
                subtitle=args.subtitle,
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
                cover_image_urn=cover_urn,
                cover_caption=args.cover_caption,
                raw_set=_raw_set_from_args(args),
            )
            result["_uploaded_cover_image"] = uploaded
        elif args.command == "schedule-post":
            result = client.schedule_article_share(
                args.article,
                content_series=args.content_series_urn,
                post_text=load_text_arg(args.post_text, args.post_text_file, "post-text"),
                scheduled_at=args.scheduled_at,
            )
        elif args.command == "delete-scheduled-share":
            result = client.delete_scheduled_share(args.post)
        elif args.command == "schedule-newsletter":
            result = client.schedule_newsletter(
                args.article,
                content_series=args.content_series_urn,
                post_text=load_text_arg(args.post_text, args.post_text_file, "post-text"),
                scheduled_at=args.scheduled_at,
                title=_title_from_args(args),
                subtitle=args.subtitle,
                header_image=args.header_image,
                header_image_content_type=args.header_image_content_type,
                cover_caption=args.cover_caption,
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
                raw_set=_raw_set_from_args(args),
            )
        elif args.command == "create-newsletter":
            title = _title_from_args(args, required=True)
            created = client.create_article(
                author_profile_urn=args.author_profile_urn,
                content_series=args.content_series_urn,
                title=title,
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
            )
            article_urn = created["data"]["entityUrn"]
            result = client.schedule_newsletter(
                article_urn,
                content_series=args.content_series_urn,
                post_text=load_text_arg(args.post_text, args.post_text_file, "post-text"),
                scheduled_at=args.scheduled_at,
                title=title,
                subtitle=args.subtitle,
                header_image=args.header_image,
                header_image_content_type=args.header_image_content_type,
                cover_caption=args.cover_caption,
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
                raw_set=_raw_set_from_args(args),
            )
            result["_created_article"] = created
        else:
            parser.error(f"Unsupported command: {args.command}")
    except requests.HTTPError as exc:
        response = exc.response
        print(f"HTTP {response.status_code}: {response.text[:1000]}", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"LinkedIn request failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    _print_summary(result)


if __name__ == "__main__":
    main()
