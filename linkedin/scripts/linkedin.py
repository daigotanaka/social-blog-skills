#!/usr/bin/env python3
"""Small LinkedIn newsletter helper built from a captured Voyager HAR.

This uses LinkedIn's private web API shape captured in
../linkedin/.secrets/linkedin-newsletter.har. It is intentionally small and
HAR-native, like ../substack/scripts/substack.py, rather than a public SDK.

Usage:
    python3 scripts/linkedin.py --cred-dir .secrets schedule-newsletter <article-id> \
        --content-series-urn urn:li:fsd_contentSeries:<newsletter-id> \
        --title "Article title" \
        --subtitle "Short article subtitle or description" \
        --header-image ./cover.jpg \
        --post-text-file ./post.txt \
        --scheduled-at 2026-07-05T09:30:00-07:00

    python3 scripts/linkedin.py --cred-dir .secrets save-article urn:li:fsd_firstPartyArticle:<article-id> \
        --title "Article title" \
        --subtitle "Short article subtitle or description" \
        --paragraph "First paragraph." \
        --heading "Section heading" \
        --paragraph "Second paragraph."

    python3 scripts/linkedin.py --cred-dir .secrets schedule-post <article-id> \
        --content-series-urn <newsletter-id> \
        --post-text "Accompanying LinkedIn post text." \
        --scheduled-at 1783344600000

Usage as a module:
    from linkedin import LinkedIn
    client = LinkedIn(cred_dir=".secrets")
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
import time
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
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
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
    if value[0] in ('"', "'"):
        return _strip_wrapping_quotes(value)
    if "," in value:
        return [_strip_wrapping_quotes(part) for part in value.split(",") if part.strip()]
    return _strip_wrapping_quotes(value)


def parse_markdown_front_matter(markdown_text: str) -> tuple[str, dict[str, Any]]:
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


def _metadata_text(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and not isinstance(value, (list, dict)):
            return str(value)
    return None


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


def _find_credential_file(directory: Path, filename: str) -> Path | None:
    exact_path = directory / filename
    if exact_path.exists():
        return exact_path

    filename_lower = filename.lower()
    for child in directory.iterdir():
        if child.is_file() and child.name.lower() == filename_lower:
            return child
    return None


def _read_credential_file(path: Path, label: str) -> str:
    value = _strip_wrapping_quotes(path.read_text(encoding="utf-8").strip())
    if not value:
        raise ValueError(f"{label} credential file is empty: {path}")
    return value


def _get_cookie(
    cookie: str | None = None,
    cookie_path: str | Path | None = None,
    cred_dir: str | Path | None = None,
) -> str:
    if cookie:
        return cookie.strip()
    if cookie_path:
        path = Path(cookie_path)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    if cred_dir:
        directory = Path(cred_dir)
        if not directory.exists():
            raise ValueError(f"--cred-dir does not exist: {directory}")
        if not directory.is_dir():
            raise ValueError(f"--cred-dir is not a directory: {directory}")

        li_at_path = _find_credential_file(directory, "li_at.txt")
        jsessionid_path = _find_credential_file(directory, "JSESSIONID.txt")
        missing = []
        if li_at_path is None:
            missing.append("li_at.txt")
        if jsessionid_path is None:
            missing.append("JSESSIONID.txt")
        if missing:
            raise ValueError(
                f"--cred-dir must contain {', '.join(missing)}. "
                "File names are matched case-insensitively."
            )

        li_at = _read_credential_file(li_at_path, "li_at")
        jsessionid = _read_credential_file(jsessionid_path, "JSESSIONID")
        return f'li_at={li_at}; JSESSIONID="{jsessionid}"'
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
        "Missing LinkedIn cookie. Provide --cookie, --cookie-path, --cred-dir, "
        "LINKEDIN_COOKIE, "
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


_FORMAT_TOKEN = re.compile(
    r"`([^`\n]+)`"
    r"|\*\*([^*\n]+)\*\*"
    r"|\*([^*\n]+)\*"
    r"|\[([^\]]+)\]\(([^)\s]+)\)"
)

_TEXT_ATTR_TYPE = "com.linkedin.voyager.dash.common.text.TextAttribute"


def _span(start: int, length: int, detail: dict) -> dict:
    return {
        "start": start,
        "length": length,
        "detailDataUnion": detail,
        "$type": _TEXT_ATTR_TYPE,
    }


def render_inline(markdown_text: str) -> tuple[str, list[dict]]:
    """Inline markdown -> (plain_text, text_attributes) with absolute offsets."""
    if not markdown_text:
        return "", []
    buf = ""
    marks: list[dict] = []
    idx = 0
    for match in _FORMAT_TOKEN.finditer(markdown_text):
        if match.start() > idx:
            buf += markdown_text[idx:match.start()]
        idx = match.end()
        code, bold, ital, link_label, link_href = match.groups()
        if code is not None:
            buf += code  # inline code renders as plain text (no schema evidence)
        elif bold is not None:
            text, sub_marks = render_inline(bold)
            start = len(buf)
            buf += text
            marks.extend({**a, "start": a["start"] + start} for a in sub_marks)
            marks.append(_span(start, len(text), {"style": "BOLD"}))
        elif ital is not None:
            text, sub_marks = render_inline(ital)
            start = len(buf)
            buf += text
            marks.extend({**a, "start": a["start"] + start} for a in sub_marks)
            marks.append(_span(start, len(text), {"style": "ITALIC"}))
        else:
            text, sub_marks = render_inline(link_label)
            start = len(buf)
            buf += text
            # keep only nested links from the label; style spans would overlap
            # the hyperlink span, and the link wins in the flat renderer
            marks.extend(
                {**a, "start": a["start"] + start}
                for a in sub_marks
                if a["detailDataUnion"].get("hyperlink")
            )
            marks.append(_span(start, len(text), {"hyperlink": link_href}))
    buf += markdown_text[idx:]
    marks.sort(key=lambda a: (a["start"], -a["length"]))
    return buf, marks


def text_block_rich(markdown_text: str, block_type: str = "PARAGRAPH") -> dict[str, Any]:
    text, marks = render_inline(markdown_text)
    block = text_block(text, block_type)
    if marks:
        block["textBlock"]["content"]["attributesV2"] = marks
    return block



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

    def _detail_html(src: str, spans: list[dict[str, Any]]) -> str:
        ordered = sorted(spans, key=lambda a: int(a.get("start", 0)))
        chunks: list[str] = []
        cursor = 0
        for attr in ordered:
            s, ln = int(attr.get("start", 0)), int(attr.get("length", 0))
            if s < cursor:
                continue
            detail = (attr.get("detailDataUnion") or {})
            if detail.get("hyperlink"):
                open_tag, close_tag = f'<a href="{escape(detail["hyperlink"], quote=True)}" target="_blank">', "</a>"
            elif detail.get("style") == "BOLD":
                open_tag, close_tag = "<strong>", "</strong>"
            elif detail.get("style") == "ITALIC":
                open_tag, close_tag = "<em>", "</em>"
            else:
                continue
            chunks.append(escape(src[cursor:s]))
            chunks.append(open_tag + escape(src[s:s + ln]) + close_tag)
            cursor = s + ln
        chunks.append(escape(src[cursor:]))
        return "".join(chunks)

    parts: list[str] = []
    for block in content:
        if block.get("dividerBlock"):
            parts.append("<hr>")
            continue
        image_value = block.get("imageBlock")
        if image_value:
            vector = (((image_value.get("content") or {}).get("attributes") or [{}])[0]
                      .get("detailDataUnion") or {}).get("vectorImage") or {}
            alt = ((image_value.get("caption") or {}).get("text")) or ""
            parts.append(figure_html_from_asset(vector.get("digitalmediaAsset", ""), alt))
            continue
        text_block_value = block.get("textBlock") or {}
        block_type = text_block_value.get("type") or "PARAGRAPH"
        text = (text_block_value.get("content") or {}).get("text") or ""
        attrs = [a for a in ((text_block_value.get("content") or {}).get("attributesV2") or [])]
        list_attrs = [a for a in attrs if (a.get("detailDataUnion") or {}).get("listItemStyle")]
        inner_attrs = [a for a in attrs if a not in list_attrs]

        if list_attrs:
            ordered = all((a["detailDataUnion"]["listItemStyle"].get("type") == "NUMBERED") for a in list_attrs)
            tag = "ol" if ordered else "ul"
            items_html: list[str] = []
            bounds = [int(a["start"]) for a in list_attrs] + [len(text)]
            for i, a in enumerate(list_attrs):
                body_start = int(a["start"]) + int(a["length"])
                body_end = bounds[i + 1]
                rel = []
                for b in inner_attrs:
                    bs, bl = int(b.get("start", 0)), int(b.get("length", 0))
                    if bs >= body_start and bs + bl <= body_end:
                        rel.append({**b, "start": bs - body_start})
                items_html.append(f"<li><p>{_detail_html(text[body_start:body_end].rstrip(), rel)}</p></li>")
            parts.append(f"<{tag}>{''.join(items_html)}</{tag}>")
        elif block_type == "CODE_BLOCK":
            parts.append(f"<pre>{escape(text)}</pre>")
        else:
            body = _detail_html(text, inner_attrs)
            if block_type == "HEADING_1":
                parts.append(f"<h2>{body}</h2>")
            elif block_type == "HEADING_2":
                parts.append(f"<h3>{body}</h3>")
            elif block_type == "QUOTE":
                parts.append(f"<blockquote><p>{body.replace(chr(10), '<br>')}</p></blockquote>")
            else:
                parts.append(f"<p>{body.replace(chr(10), '<br>')}</p>")
    return "".join(parts)


def markdown_to_article(markdown_text: str, images_dir: str | Path | None = None) -> dict[str, Any]:
    markdown_text, metadata = parse_markdown_front_matter(markdown_text)
    title: str | None = _metadata_text(metadata, "title")
    content: list[dict[str, Any]] = []
    html_parts: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_html_items: list[str] = []
    list_ordered = False
    quote: list[str] = []
    code_lines: list[str] = []
    in_code = False
    images: list[dict[str, Any]] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        md_text = " ".join(part.strip() for part in paragraph).strip()
        if md_text:
            content.append(text_block_rich(md_text, "PARAGRAPH"))
            html_parts.append(f"<p>{_inline_html(md_text)}</p>")
        paragraph = []

    def flush_list() -> None:
        nonlocal list_items, list_html_items, list_ordered
        if not list_items:
            return
        prefix = "1. " if list_ordered else "- "
        style_type = "NUMBERED" if list_ordered else "SOLID_BULLET"
        rendered = [render_inline(item) for item in list_items]
        attrs: list[dict[str, Any]] = []
        texts: list[str] = []
        pos = 0
        for i, (item_text, item_marks) in enumerate(rendered):
            attrs.append(_span(pos, len(prefix), {"listItemStyle": {"type": style_type, "index": i}}))
            attrs.extend({**a, "start": a["start"] + pos + len(prefix)} for a in item_marks)
            texts.append(item_text)
            pos += len(prefix) + len(item_text) + 1
        block = text_block("\n".join(f"{prefix}{t}" for t in texts), "PARAGRAPH")
        block["textBlock"]["content"]["attributesV2"] = attrs
        content.append(block)
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
            content.append(text_block_rich(md_text, "QUOTE"))
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
            if level == 1 and text == title:
                continue
            block_type = "HEADING_1" if level <= 1 else "HEADING_2"
            content.append(text_block_rich(heading.group(2), block_type))
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
            list_items.append(item_md.strip())
            list_html_items.append(f"<li><p>{_inline_html(item_md)}</p></li>")
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            flush_list()
            quote.append(stripped.lstrip(">").strip())
            continue

        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush_paragraph()
            flush_list()
            flush_quote()
            content.append({"dividerBlock": {"dividerBlockType": "DEFAULT", "$type": "com.linkedin.voyager.dash.publishing.DividerBlock"}})
            continue

        image_line = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", stripped)
        if image_line:
            flush_paragraph()
            flush_list()
            flush_quote()
            images.append({
                "alt": image_line.group(1),
                "src": image_line.group(2).strip(),
                "uploaded": None,
            })
            content.append({"imagePlaceholder": len(images) - 1})
            continue

        paragraph.append(stripped)

    if in_code:
        flush_code()
    flush_paragraph()
    flush_list()
    flush_quote()

    return {
        "title": title,
        "metadata": metadata,
        "content": content,
        "content_html": content_blocks_to_html(content),
        "images": images,
        "images_dir": Path(images_dir) if images_dir else None,
    }


def load_markdown_arg(markdown: str | None, path: Path | None) -> dict[str, Any] | None:
    if markdown is None and path is None:
        return None
    text = markdown if markdown is not None else path.read_text(encoding="utf-8")
    article = markdown_to_article(text)
    if path is not None:
        article["images_dir"] = Path(path).expanduser().resolve().parent
    return article


def resolve_image_path(entry: dict[str, Any], images_dir: Path | None) -> Path:
    src_path = Path(entry["src"])
    if src_path.is_absolute():
        if src_path.exists():
            return src_path
        raise FileNotFoundError(f"inline image not found: {src_path}")
    if images_dir is not None:
        candidate = (images_dir / src_path).resolve()
        if candidate.exists():
            return candidate
        candidate = (images_dir.parent / src_path).resolve()
        if candidate.exists():
            return candidate
    candidate = src_path.expanduser()
    if candidate.exists():
        return candidate.resolve()
    raise FileNotFoundError(f"inline image not found: {entry['src']} (images_dir={images_dir})")


def image_block_from_asset(asset_urn: str, alt: str = "") -> dict[str, Any]:
    media_id = asset_urn.rsplit(":", 1)[-1]
    root_url = f"https://media.licdn.com/dms/image/v2/{media_id}/article-inline_image-shrink_"
    return {
        "imageBlock": {
            "caption": {
                "text": alt,
                "attributesV2": [],
                "$type": "com.linkedin.voyager.dash.common.text.TextViewModel",
            },
            "alignment": "RESIZE",
            "content": {
                "attributes": [
                    {
                        "detailDataUnion": {
                            "vectorImage": {
                                "digitalmediaAsset": asset_urn,
                                "artifacts": [],
                                "rootUrl": root_url,
                                "$type": "com.linkedin.common.VectorImage",
                            }
                        },
                        "$type": "com.linkedin.voyager.dash.common.image.ImageAttribute",
                    }
                ],
                "$type": "com.linkedin.voyager.dash.common.image.ImageViewModel",
            },
            "$type": "com.linkedin.voyager.dash.publishing.ImageBlock",
        }
    }


def figure_html_from_asset(asset_urn: str, alt: str = "") -> str:
    media_id = asset_urn.rsplit(":", 1)[-1]
    src = f"https://media.licdn.com/dms/image/v2/{media_id}/article-inline_image-shrink_800_450/"
    caption = f"<figcaption>{escape(alt)}</figcaption>" if alt else ""
    return f'<figure><img data-media-urn="{asset_urn}" src="{src}">{caption}</figure>'


def resolve_inline_images(
    client: "LinkedIn",
    article: dict[str, Any] | None,
    *,
    images_dir: str | Path | None = None,
    enabled: bool = True,
) -> dict[str, Any] | None:
    """Upload markdown-referenced images and swap placeholder blocks for real
    LinkedIn imageBlocks. Mutates and returns the same article dict."""
    if not article or not article.get("images") or article.get("_images_resolved"):
        return article
    article["_images_resolved"] = True
    if not enabled:
        article["content"] = [b for b in article["content"] if "imagePlaceholder" not in b]
        article["content_html"] = content_blocks_to_html(article["content"])
        return article
    base_dir = Path(images_dir) if images_dir else article.get("images_dir")
    for block in article["content"]:
        idx = block.get("imagePlaceholder")
        if idx is None:
            continue
        entry = article["images"][idx]
        path = resolve_image_path(entry, base_dir)
        meta = client.upload_media(path, "PUBLISHING_INLINE_IMAGE")
        entry["uploaded"] = {"urn": meta["urn"], "path": str(path), "status": meta.get("_upload_status")}
        block.clear()
        block.update(image_block_from_asset(meta["urn"], entry["alt"]))
    article["content_html"] = content_blocks_to_html(article["content"])
    return article


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
        cred_dir: str | Path | None = None,
        csrf_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._cookie = _get_cookie(cookie=cookie, cookie_path=cookie_path, cred_dir=cred_dir)
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

    def upload_media(
        self,
        image_path: str | Path,
        upload_type: str,
        referer: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        image_path = Path(image_path)
        content_type = content_type or mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        metadata_response = requests.post(
            UPLOAD_METADATA_URL,
            headers=self._headers(referer=referer or f"{LINKEDIN_ORIGIN}/"),
            json={
                "mediaUploadType": upload_type,
                "fileSize": image_path.stat().st_size,
                "filename": image_path.name,
            },
            timeout=self.timeout,
        )
        metadata_response.raise_for_status()
        metadata = metadata_response.json()["data"]["value"]
        upload_headers = self._headers(
            accept="*/*",
            content_type=content_type,
            referer=referer or f"{LINKEDIN_ORIGIN}/",
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
        polling_url = metadata.get("pollingUrl")
        if polling_url:
            for _ in range(20):
                try:
                    status_response = requests.get(
                        polling_url,
                        headers=self._headers(accept="*/*", content_type=None),
                        timeout=self.timeout,
                    )
                    blob = status_response.text.lower()
                except Exception:  # noqa: BLE001
                    break
                if "failed" in blob:
                    raise RuntimeError(f"upload processing failed for {image_path.name}")
                if "succeed" in blob:
                    break
                time.sleep(1.5)
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



def _find_article_item(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        if obj.get("$type") == "com.linkedin.voyager.dash.publishing.FirstPartyArticle" and "content" in obj:
            return obj
        for value in obj.values():
            found = _find_article_item(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_article_item(value)
            if found:
                return found
    return None


def _check_summary(client: "LinkedIn", article: str, author_profile_urn: str | None = None) -> dict[str, Any]:
    import re as _re
    draft = _find_article_item(client.fetch_latest_draft(article)) or {}
    shares: list[dict[str, Any]] = []
    try:
        profile = author_profile_urn or _config().get("author_profile_urn")
        if profile:
            listing = _extract_post_list_summary(
                client.list_posts(author_profile_urn=profile, filters=["SCHEDULED"])
            )
            tail = str(article).rsplit(":", 1)[-1]
            for group in listing.get("filters", []):
                for p in group.get("posts", []):
                    if (p.get("articleUrn") or "").rsplit(":", 1)[-1] == tail:
                        shares.append({
                            "scheduledPostUrn": p.get("scheduledPostUrn"),
                            "scheduledAt": p.get("scheduledAt"),
                        })
    except Exception as exc:  # noqa: BLE001
        shares = [{"error": str(exc)}]
    blocks = draft.get("content") or []
    blob = json.dumps(blocks)
    image_urns = sorted(set(_re.findall(r"urn:li:digitalmediaAsset:[A-Za-z0-9_.-]+", blob)))
    link_urls = sorted(set(
        a["detailDataUnion"]["hyperlink"]
        for b in blocks if b.get("textBlock")
        for a in (b["textBlock"].get("content") or {}).get("attributesV2") or []
        if (a.get("detailDataUnion") or {}).get("hyperlink")
    ))
    return {
        "title": draft.get("title"),
        "state": draft.get("state"),
        "scheduledAt": draft.get("scheduledAt"),
        "contentBlocks": len(blocks),
        "imageBlocks": blob.count('"imageBlock"'),
        "hyperlinks": len(link_urls),
        "hyperlinkUrls": link_urls,
        "boldSpans": blob.count('"BOLD"'),
        "italicSpans": blob.count('"ITALIC"'),
        "dividerBlocks": blob.count('"dividerBlock"'),
        "mediaAssets": image_urns,
        "markdownDebris": bool(_re.search(r"\*\*|\]\(https?|</?img", blob)),
        "contentHtmlChars": len(draft.get("contentHtml") or ""),
        "scheduledShares": shares,
    }


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
        if result.get("_inline_images"):
            summary["inlineImages"] = result["_inline_images"]
    elif result.get("data", {}).get("data", {}).get("createContentcreationDashShares"):
        summary = _extract_share_summary(result)
        if result.get("_inline_images"):
            summary["inlineImages"] = result["_inline_images"]
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


def _add_inline_image_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--inline-images",
        choices=("auto", "off"),
        default="auto",
        help="Upload and embed markdown ![alt](path) images in the article body (default: auto).",
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        help="Base directory for relative image paths (default: the markdown file's directory).",
    )


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


def _subtitle_from_args(args: argparse.Namespace) -> str | None:
    explicit = getattr(args, "subtitle", None)
    if explicit is not None:
        return explicit
    article = _markdown_article_from_args(args)
    metadata = article.get("metadata") if article else None
    return _metadata_text(metadata or {}, "subtitle", "excerpt", "description")


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


def _config() -> dict[str, Any]:
    if not hasattr(_config, "_cache"):
        data: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except json.JSONDecodeError:
                pass
        _config._cache = data  # type: ignore[attr-defined]
    return _config._cache  # type: ignore[attr-defined]


def _author_profile_from_args(args: argparse.Namespace) -> str:
    value = (
        getattr(args, "author_profile_urn", None)
        or os.environ.get("LINKEDIN_AUTHOR_PROFILE_URN")
        or _config().get("author_profile_urn")
    )
    if not value:
        raise ValueError(
            "Missing author profile URN. Provide --author-profile-urn, "
            "LINKEDIN_AUTHOR_PROFILE_URN, or author_profile_urn in the skill's config.json."
        )
    return value


def _series_from_args(args: argparse.Namespace) -> str:
    value = (
        getattr(args, "content_series_urn", None)
        or os.environ.get("LINKEDIN_CONTENT_SERIES_URN")
        or _config().get("content_series_urn")
    )
    if not value:
        raise ValueError(
            "Missing content series URN. Provide --content-series-urn, "
            "LINKEDIN_CONTENT_SERIES_URN, or content_series_urn in the skill's config.json."
        )
    return value


def _front_matter_header(args: argparse.Namespace) -> Path | None:
    """Header image from markdown front matter `image:` when --header-image is absent."""
    if getattr(args, "header_image", None):
        return None
    article = _markdown_article_from_args(args)
    if not article:
        return None
    ref = _metadata_text(article.get("metadata") or {}, "image", "header_image", "cover")
    if not ref:
        return None
    base = article.get("images_dir") or Path(".")
    for candidate in ((Path(base) / ref).resolve(), (Path(base).parent / ref).resolve(), Path(ref)):
        if candidate.exists():
            return candidate
    print(f"warning: front-matter image not found: {ref}", file=sys.stderr)
    return None


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
    parser.add_argument(
        "--cred-dir",
        type=Path,
        help="Directory containing li_at.txt and JSESSIONID.txt. File names are matched case-insensitively.",
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
    create.add_argument("--author-profile-urn", help="Author profile URN (default: config.json/env).")
    create.add_argument("--content-series-urn", help="Newsletter/content series URN (default: config.json/env).")
    create.add_argument("--title", help="Initial article title. Defaults to the first # heading from markdown.")
    create.add_argument("--header-image", type=Path, help="Upload and set header image (default: front-matter `image:`).")
    create.add_argument("--header-image-content-type", help="Override header image content type.")
    create.add_argument("--content-html", help="Optional complete article HTML.")
    create.add_argument("--content-html-file", type=Path, help="Path to optional complete article HTML.")
    _add_inline_image_args(create)
    _add_article_content_args(create, include_raw_set=False)

    fetch = subparsers.add_parser("fetch-latest-draft", help="Fetch the latest draft for an article id/URN.")
    fetch.add_argument("article", help="Article id, fsd_firstPartyArticle URN, or linkedInArticle URN.")

    check = subparsers.add_parser("check", help="One-shot draft health summary: state, images, links, spans, debris, schedule.")
    check.add_argument("article", help="Article id, fsd_firstPartyArticle URN, or linkedInArticle URN.")
    check.add_argument("--author-profile-urn", help="Author profile URN for share lookup (default: config.json/env).")

    update_post = subparsers.add_parser(
        "update-scheduled-post",
        help="Replace text/time of a scheduled newsletter post: finds and deletes the existing share, then reschedules.",
    )
    update_post.add_argument("article", help="Article id or URN (the newsletter article, not the share).")
    update_post.add_argument("--post-text", help="New accompanying post text.")
    update_post.add_argument("--post-text-file", type=Path, help="Path to new post text.")
    update_post.add_argument("--scheduled-at", help="New ISO datetime or epoch ms (default: keep existing time).")
    update_post.add_argument("--content-series-urn", help="Newsletter/content series URN (default: config.json/env).")

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
    _add_inline_image_args(save)
    _add_article_content_args(save)

    schedule = subparsers.add_parser("schedule-post", help="Schedule the accompanying post for an article.")
    schedule.add_argument("article", help="Article id, fsd_firstPartyArticle URN, or linkedInArticle URN.")
    schedule.add_argument("--content-series-urn", help="Newsletter/content series id or fsd_contentSeries URN (default: config.json/env).")
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
    newsletter.add_argument("--content-series-urn", help="Newsletter/content series id or fsd_contentSeries URN (default: config.json/env).")
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
    _add_inline_image_args(newsletter)
    _add_article_content_args(newsletter)

    create_newsletter = subparsers.add_parser(
        "create-newsletter",
        help="Create a newsletter article, patch optional title/subtitle/header/body, then schedule the accompanying post.",
    )
    create_newsletter.add_argument("--author-profile-urn", help="Author profile URN (default: config.json/env).")
    create_newsletter.add_argument("--content-series-urn", help="Newsletter/content series URN (default: config.json/env).")
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
    _add_inline_image_args(create_newsletter)
    _add_article_content_args(create_newsletter)

    args = parser.parse_args()

    try:
        client = LinkedIn(
            cookie=args.cookie,
            cookie_path=args.cookie_path,
            cred_dir=args.cred_dir,
            csrf_token=args.csrf_token,
            timeout=args.timeout,
        )
        def _resolve_inline() -> None:
            resolve_inline_images(
                client,
                _markdown_article_from_args(args),
                images_dir=getattr(args, "images_dir", None),
                enabled=getattr(args, "inline_images", "auto") != "off",
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
            _resolve_inline()
            result = client.create_article(
                author_profile_urn=_author_profile_from_args(args),
                content_series=_series_from_args(args),
                title=_title_from_args(args, required=True),
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
            )
            header = args.header_image or _front_matter_header(args)
            if header:
                uploaded = client.upload_cover_image(header, article=result["data"]["entityUrn"])
                client.patch_article(result["data"]["entityUrn"], cover_image_urn=uploaded["urn"])
                result["_uploaded_cover_image"] = uploaded
        elif args.command == "fetch-latest-draft":
            result = client.fetch_latest_draft(args.article)
        elif args.command == "check":
            result = _check_summary(client, args.article, getattr(args, "author_profile_urn", None))
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
            _resolve_inline()
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
                subtitle=_subtitle_from_args(args),
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
                cover_image_urn=cover_urn,
                cover_caption=args.cover_caption,
                raw_set=_raw_set_from_args(args),
            )
            result["_uploaded_cover_image"] = uploaded
            inline_art = _markdown_article_from_args(args)
            if inline_art and any(e.get("uploaded") for e in inline_art.get("images", [])):
                result["_inline_images"] = [e["uploaded"] for e in inline_art["images"] if e.get("uploaded")]
        elif args.command == "schedule-post":
            result = client.schedule_article_share(
                args.article,
                content_series=_series_from_args(args),
                post_text=load_text_arg(args.post_text, args.post_text_file, "post-text"),
                scheduled_at=args.scheduled_at,
            )
        elif args.command == "delete-scheduled-share":
            result = client.delete_scheduled_share(args.post)
        elif args.command == "update-scheduled-post":
            article_target = first_party_article_urn(args.article)
            listings = client.list_posts(
                author_profile_urn=_author_profile_from_args(args),
                filters=["SCHEDULED"],
            )
            summary = _extract_post_list_summary(listings)
            matches = [
                p
                for group in summary.get("filters", [])
                for p in group.get("posts", [])
                if p.get("articleUrn") == article_target or (p.get("articleUrn") or "").rsplit(":", 1)[-1] == str(args.article).rsplit(":", 1)[-1]
            ]
            if len(matches) > 1:
                raise ValueError(f"multiple scheduled shares match article {args.article}; delete one explicitly")
            if matches:
                client.delete_scheduled_share(matches[0]["scheduledPostUrn"])
            scheduled_at = args.scheduled_at or (matches[0].get("scheduledAt") if matches else None)
            if not scheduled_at:
                raise ValueError("No existing schedule to keep; provide --scheduled-at")
            result = client.schedule_article_share(
                args.article,
                content_series=_series_from_args(args),
                post_text=load_text_arg(args.post_text, args.post_text_file, "post-text"),
                scheduled_at=scheduled_at,
            )
            result["_replaced_share"] = matches[0]["scheduledPostUrn"] if matches else None
        elif args.command == "schedule-newsletter":
            _resolve_inline()
            result = client.schedule_newsletter(
                args.article,
                content_series=_series_from_args(args),
                post_text=load_text_arg(args.post_text, args.post_text_file, "post-text"),
                scheduled_at=args.scheduled_at,
                title=_title_from_args(args),
                subtitle=_subtitle_from_args(args),
                header_image=args.header_image or _front_matter_header(args),
                header_image_content_type=args.header_image_content_type,
                cover_caption=args.cover_caption,
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
                raw_set=_raw_set_from_args(args),
            )
        elif args.command == "create-newsletter":
            title = _title_from_args(args, required=True)
            _resolve_inline()
            created = client.create_article(
                author_profile_urn=_author_profile_from_args(args),
                content_series=_series_from_args(args),
                title=title,
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
            )
            article_urn = created["data"]["entityUrn"]
            result = client.schedule_newsletter(
                article_urn,
                content_series=_series_from_args(args),
                post_text=load_text_arg(args.post_text, args.post_text_file, "post-text"),
                scheduled_at=args.scheduled_at,
                title=title,
                subtitle=_subtitle_from_args(args),
                header_image=args.header_image or _front_matter_header(args),
                header_image_content_type=args.header_image_content_type,
                cover_caption=args.cover_caption,
                content=_content_from_args(args),
                content_html=_content_html_from_args(args),
                raw_set=_raw_set_from_args(args),
            )
            result["_created_article"] = created
            inline_art = _markdown_article_from_args(args)
            if inline_art and any(e.get("uploaded") for e in inline_art.get("images", [])):
                result["_inline_images"] = [e["uploaded"] for e in inline_art["images"] if e.get("uploaded")]
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
