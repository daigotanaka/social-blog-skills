#!/usr/bin/env python3
"""Unified note.com API client.

Consolidates save_draft.py, publish.py, update_draft.py, engage.py,
search.py, list_magazine.py, like.py, get_article.py, get_related_articles.py,
and add_to_magazine.py into a single class-based interface.

Usage (CLI -- requires --help for full options):
    python3 note_com.py draft --title "My Article" --body-file article.md
    python3 note_com.py publish <note_key>
    python3 note_com.py update <note_key> --title "New Title"
    python3 note_com.py search "python" --size 20
    python3 note_com.py list-magazine m6dc9256d1e54
    python3 note_com.py extract_uuids <note_key>
    python3 note_com.py like n427957bbee03
    python3 note_com.py engage m6dc9256d1e54

Usage (import):
    from note_com import NoteCom
    client = NoteCom()
    result = client.create_draft(title="Hello", body_file="article.md")
"""

import os
import re
import sys
import json
import time
import uuid
import mimetypes
import argparse
from html.parser import HTMLParser
from urllib.parse import quote
from typing import Any, Dict, List, Optional

import requests
import markdown as md_lib


# ===================================================================
# Shared constants & helpers
# ===================================================================

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_NOTE_UUID_TAGS = {
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}

def _get_token(token: Optional[str] = None, token_path: Optional[str] = None) -> str:
    """Resolve a _note_session_v5 cookie from CLI args, a file, or the environment."""
    if token:
        return token
    if token_path and os.path.exists(token_path):
        return open(token_path, "r", encoding="utf-8").read().strip()
    env = os.environ.get("NOTE_COM_TOKEN", "")
    if env:
        return env
    raise ValueError("token required. Pass --token, --token-path, or set NOTE_COM_TOKEN.")


def _browser_headers(token: str, note_key: Optional[str] = None) -> dict:
    """Return browser-like headers for all note.com API calls."""
    h = {
        "Cookie": f"_note_session_v5={token}",
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": _BROWSER_UA,
        "Origin": "https://note.com",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if note_key:
        h["Referer"] = f"https://note.com/n/{note_key}"
    else:
        h["Referer"] = "https://note.com/w/new"
    return h


def _json_headers(token: str) -> dict:
    h = _browser_headers(token)
    h["Content-Type"] = "application/json"
    return h


def _normalize_tag(tag: str) -> dict:
    """Normalize a tag to dict API format: strip '#', wrap in {hashtag: {name}}."""
    return {"hashtag": {"name": tag.lstrip("#")}}


def _normalize_tag_str(tag: str) -> str:
    """Normalize a tag to string API format: strip '#', prepend '#'.

    Used by publish_draft (PUT endpoint) which expects str hashtags like
    ["#AI", "#note"], not the dict format used by save_draft/update_draft.
    """
    return f"#{tag.lstrip('#')}"


def format_price(price: int) -> str:
    return f"\u00a5{price:,}" if price else "\u7121\u6599"


def format_date(date_str: str) -> str:
    if not date_str:
        return "?"
    return date_str[:10]


# ===================================================================
# Main Client Class
# ===================================================================

class _PaidBodySplitter(HTMLParser):
    """Split HTML into free/pay bodies at the separator element."""

    def __init__(self, separator_uuid: str):
        super().__init__(convert_charrefs=False)
        self.separator_uuid = separator_uuid
        self.before: list[str] = []
        self.after: list[str] = []
        self.stack: list[bool] = []
        self.found = False
        self.in_separator = False

    def _target(self) -> list[str]:
        return self.before if not self.found or self.in_separator else self.after

    @staticmethod
    def _format_attrs(attrs: list[tuple[str, Optional[str]]]) -> str:
        if not attrs:
            return ""
        parts = []
        for key, value in attrs:
            if value is None:
                parts.append(key)
            else:
                escaped = value.replace("&", "&amp;").replace('"', "&quot;")
                parts.append(f'{key}="{escaped}"')
        return " " + " ".join(parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        is_sep = attrs_dict.get("id") == self.separator_uuid and attrs_dict.get("name") == self.separator_uuid
        if is_sep:
            self.found = True
            self.in_separator = True
        self._target().append(f"<{tag}{self._format_attrs(attrs)}>")
        self.stack.append(is_sep)

    def handle_endtag(self, tag: str) -> None:
        self._target().append(f"</{tag}>")
        if self.stack:
            was_sep = self.stack.pop()
            if was_sep:
                self.in_separator = False

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        is_sep = attrs_dict.get("id") == self.separator_uuid and attrs_dict.get("name") == self.separator_uuid
        if is_sep:
            self.before.append(f"<{tag}{self._format_attrs(attrs)} />")
            self.found = True
        else:
            self._target().append(f"<{tag}{self._format_attrs(attrs)} />")

    def handle_data(self, data: str) -> None:
        self._target().append(data)

    def handle_entityref(self, name: str) -> None:
        self._target().append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._target().append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._target().append(f"<!--{data}-->")



# ===================================================================
# Main Client Class
# ===================================================================

class NoteCom:
    """Unified note.com API client.

    Consolidates draft creation, publishing, updating, searching,
    magazine management, liking, and engagement into one interface.

    Usage:
        client = NoteCom()
        client.create_draft(title="Hello", body_file="article.md")
        client.publish_draft("na89d642394f3", publish=True)
        results = client.search("python", size=20)
    """

    def __init__(self, token: Optional[str] = None, token_path: Optional[str] = None):
        """Initialize with optional pre-fetched token."""
        self._token = _get_token(token, token_path)

    # ----------------------------------------------------------------
    # Token management
    # ----------------------------------------------------------------

    def refresh_token(self) -> str:
        """Fetch a fresh _note_session_v5 cookie. Valid for ~5 minutes."""
        self._token = _get_token()
        return self._token

    @property
    def token(self) -> str:
        return self._token

    # ----------------------------------------------------------------
    # Authentication helpers
    # ----------------------------------------------------------------

    def _get_headers(self, note_key: Optional[str] = None) -> dict:
        h = _browser_headers(self._token, note_key)
        return h

    def _json_headers(self, note_key: Optional[str] = None) -> dict:
        h = _browser_headers(self._token, note_key)
        h["Content-Type"] = "application/json"
        return h

    # ----------------------------------------------------------------
    # Markdown / note.com HTML helpers
    # ----------------------------------------------------------------

    @staticmethod
    def _with_fresh_note_uuids(html: str) -> str:
        """Add fresh matching name/id UUIDs to markdown block elements."""
        tag_pat = re.compile(r'<([A-Za-z][\w:-]*)(\s+[^<>]*?)?(\s*/?)>', re.DOTALL)
        attr_pat = re.compile(r'\s+(?:name|id)\s*=\s*([\'\"])[^\'\"]*\1', re.IGNORECASE)

        def replace_tag(match: re.Match) -> str:
            tag = match.group(1)
            tag_lower = tag.lower()
            if tag_lower not in _NOTE_UUID_TAGS:
                return match.group(0)

            attrs = attr_pat.sub('', match.group(2) or '')
            closing = match.group(3) or ''
            block_uuid = str(uuid.uuid4())
            return f'<{tag} name="{block_uuid}" id="{block_uuid}"{attrs}{closing}>'

        return tag_pat.sub(replace_tag, html)

    @classmethod
    def _markdown_to_note_html(cls, markdown_text: str) -> str:
        return cls._with_fresh_note_uuids(md_lib.markdown(markdown_text))

    # ----------------------------------------------------------------
    # Draft Creation (from save_draft.py)
    # ----------------------------------------------------------------

    def create_note_shell(self, title: str, body: str = "") -> Dict[str, Any]:
        """Create a text-note shell on note.com (Step 1).

        Returns {"success": True, "note_id": int, "key": str, "type": str}
        or {"success": False, "error": str} on failure.
        """
        headers = self._json_headers()
        resp = requests.post(
            "https://note.com/api/v1/text_notes",
            cookies={"_note_session_v5": self._token},
            headers=headers,
            json={"name": title, "body": body},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            d = resp.json()["data"]
            return {"success": True, "note_id": d["id"], "key": d["key"], "type": d["type"]}
        return {"success": False, "error": f"Step 1 CREATE failed HTTP {resp.status_code}: {resp.text}"}

    def upload_eyecatch(
        self,
        note_id: int,
        image_path: str,
        width: int = 1280,
        height: int = 670,
    ) -> Dict[str, Any]:
        """Upload a cover image (eyecatch) for a note.

        Returns {"success": True, "eyecatch_url": str, "eyecatch_id": int}
        or {"success": False, "error": str} on failure.
        """
        if not os.path.isfile(image_path):
            return {"success": False, "error": f"Image file not found: {image_path}"}
        mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        with open(image_path, "rb") as f:
            file_data = f.read()
        headers = self._get_headers()
        resp = requests.post(
            "https://note.com/api/v1/image_upload/note_eyecatch",
            cookies={"_note_session_v5": self._token},
            headers=headers,
            files={"file": ("eyecatch.jpg", file_data, mime_type)},
            data={"note_id": str(note_id), "width": str(width), "height": str(height)},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            url = resp.json()["data"]["url"]
            image_id = int(url.rstrip("/").split("/")[-2])
            return {"success": True, "eyecatch_url": url, "eyecatch_id": image_id, "width": width, "height": height}
        return {"success": False, "error": f"Upload failed HTTP {resp.status_code}: {resp.text}"}

    def save_draft(
        self,
        note_id: int,
        title: str,
        body_md_file: str,
        eyecatch: Optional[Dict[str, Any]] = None,
        hashtags: Optional[list[str]] = None,
        shell: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Save markdown article content into an existing note shell.

        Converts markdown to HTML before sending to note.com.
        """
        with open(body_md_file, "r", encoding="utf-8") as f:
            md_text = f.read()
        html_body = self._markdown_to_note_html(md_text)

        headers = self._json_headers()
        payload = {
            "name": title,
            "body": html_body,
            "body_length": len(html_body),
            "index": False,
            "is_lead_form": False,
        }
        if eyecatch and eyecatch.get("success"):
            payload.update({
                "eyecatch_url": eyecatch["eyecatch_url"],
                "eyecatch_image_id": str(eyecatch["eyecatch_id"]),
                "eyecatch_image_type": "image/jpeg",
                "eyecatch_width": eyecatch.get("width", 1280),
                "eyecatch_height": eyecatch.get("height", 670),
            })
        if hashtags:
            payload["hashtags"] = [_normalize_tag(t) for t in hashtags]

        resp = requests.post(
            "https://note.com/api/v1/text_notes/draft_save",
            cookies={"_note_session_v5": self._token},
            headers=headers,
            json=payload,
            params={"id": note_id, "is_temp_saved": "true"},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            result = {"success": True, "note_id": note_id, "key": shell["key"] if shell else "", "body": html_body, "status": "draft"}
            return result
        return {"success": False, "error": f"Step 2 DRAFT save failed HTTP {resp.status_code}: {resp.text}"}

    def create_draft(
        self,
        title: str,
        body_file: str,
        eyecatch_image: Optional[str] = None,
        hashtags: Optional[list[str]] = None,
        width: int = 1280,
        height: int = 670,
    ) -> Dict[str, Any]:
        """Full draft creation flow: shell -> eyecatch -> save.

        Creates a draft (does not publish). Returns dict with note_id, key, status.
        """
        shell = self.create_note_shell(title)
        if not shell["success"]:
            return shell
        eyecatch = None
        if eyecatch_image:
            eyecatch = self.upload_eyecatch(shell["note_id"], eyecatch_image, width=width, height=height)
            if eyecatch and eyecatch["success"]:
                time.sleep(1)
        draft = self.save_draft(
            shell["note_id"], title, body_md_file=body_file,
            eyecatch=eyecatch, shell=shell, hashtags=hashtags,
        )
        if not draft["success"]:
            return draft
        result = {
            "success": True, "note_id": draft["note_id"], "key": shell["key"],
            "status": "draft",
            "eyecatch_url": eyecatch["eyecatch_url"] if eyecatch and eyecatch.get("success") else None,
        }
        return result

    # ----------------------------------------------------------------
    # Internal utility: Split HTML into free/paid body sections
    # ----------------------------------------------------------------

    def _split_paid_body(self, html_body: str, separator_uuid: str) -> Optional[tuple[str, str]]:
        """Split HTML body into (free_body, pay_body) at the separator UUID element.

        The separator element itself (where name=id=separator_uuid) is kept
        in the free body as the last element.

        Args:
            html_body: Full HTML body string.
            separator_uuid: UUID string matching name/id on the separator element.

        Returns:
            (free_body, pay_body) tuple, or None if separator not found.
        """
        parser = _PaidBodySplitter(separator_uuid)
        parser.feed(html_body)
        if not parser.found:
            return None
        return "".join(parser.before), "".join(parser.after)

    # ----------------------------------------------------------------
    # Internal utility: Insert TOC after a searched element
    # ----------------------------------------------------------------

    def _insert_toc_after_search(
        self,
        html: str,
        search_text: str,
    ) -> Optional[str]:
        """Find the first element whose ``innerText`` contains ``search_text``,
        then insert a ``<table-of-contents>`` element with a fresh UUID
        immediately after that element's closing tag.

        Returns the modified HTML string, or ``None`` if the element is not found.
        """
        import uuid as _uuid

        entries = self._extract_uuids(html)
        target_uuid = None
        for entry in entries:
            if search_text.lower() in entry["innerText"].lower():
                target_uuid = entry["uuid"]
                break
        if target_uuid is None:
            # Fallback: no UUID-named elements found (e.g. markdown-created drafts).
            # Fall back to plain-text search: find the first element whose
            # innerText contains search_text, then locate it in the HTML by
            # matching the text content.
            import html as _html

            target_tag = None
            target_end = None
            tag_stack: list[tuple[str, int]] = []  # (tag_name, end_pos)
            pos = 0
            search_lower = search_text.lower()
            while pos < len(html):
                open_m = re.search(r'<(\w+)([^>]*)/?>', html[pos:], re.IGNORECASE)
                if not open_m:
                    break
                tag_name = open_m.group(1)
                attr_str = open_m.group(2) or ""
                is_self_closing = '/>' in attr_str or attr_str.rstrip().endswith('/')
                full_open_end = pos + open_m.end()
                if tag_name.lower() in ('br', 'hr', 'img', 'input', 'meta', 'link', 'area', 'base', 'col', 'embed', 'source', 'track', 'wbr'):
                    pos = full_open_end
                    continue
                # Find closing tag
                close_m = re.search(r'</' + re.escape(tag_name) + r'\s*>', html[full_open_end:], re.IGNORECASE)
                if not close_m:
                    pos = full_open_end
                    continue
                full_close_end = full_open_end + close_m.end()
                inner = html[full_open_end:full_close_end]
                # Strip nested tags to get innerText
                inner_text = re.sub(r'<[^>]+>', '', inner).strip()
                if search_lower in inner_text.lower():
                    target_tag = tag_name
                    target_end = full_close_end
                    break
                pos = full_close_end

            if target_tag is None:
                return None

            new_uuid = str(_uuid.uuid4())
            toc_tag = f'<table-of-contents name="{new_uuid}" id="{new_uuid}"><br></table-of-contents>'
            return html[:target_end] + toc_tag + html[target_end:]

        if target_uuid is None:
            return None

        new_uuid = str(_uuid.uuid4())
        toc_tag = f'<table-of-contents name="{new_uuid}" id="{new_uuid}"><br></table-of-contents>'

        # Find the element by its name=UUID attribute, then locate its full
        # HTML span (opening tag through closing tag) and insert after it.
        # Pattern: <tagname ...name="UUID"...> ... </tagname>
        # We need to find the tag name first, then match the full element.
        tag_name = None
        for entry in entries:
            if entry["uuid"] == target_uuid:
                # Find the opening tag for this UUID in the HTML
                pat = re.compile(
                    r'<(\w+)[^>]*?name="' + re.escape(target_uuid) + r'"[^>]*>',
                    re.IGNORECASE,
                )
                m = pat.search(html)
                if m:
                    tag_name = m.group(1)
                break

        if tag_name is None:
            return None

        # Build a regex that matches the full element (handles nesting).
        # We use a manual scan: find the opening tag, then track depth.
        open_pat = re.compile(
            r'<(' + re.escape(tag_name) + r')[^>]*name="' + re.escape(target_uuid) + r'"[^>]*>',
            re.IGNORECASE,
        )
        m = open_pat.search(html)
        if m is None:
            return None

        start = m.start()
        depth = 1
        pos = m.end()
        end_close_pat = re.compile(
            r'</' + re.escape(tag_name) + r'\s*>',
            re.IGNORECASE,
        )
        open_pat2 = re.compile(
            r'<(' + re.escape(tag_name) + r')([^>]*)/?>',
            re.IGNORECASE,
        )

        end = None
        while pos < len(html) and depth > 0:
            next_open = open_pat2.search(html, pos)
            next_close = end_close_pat.search(html, pos)

            if next_close is None:
                break

            if next_open is not None and next_open.start() < next_close.start():
                attr_str = next_open.group(2) or ""
                if '/>' in attr_str or attr_str.rstrip().endswith('/'):
                    pass  # self-closing, depth unchanged
                else:
                    depth += 1
                pos = next_open.end()
            else:
                depth -= 1
                if depth == 0:
                    end = next_close.end()
                    break
                pos = next_close.end()

        if end is None:
            return None

        return html[:end] + toc_tag + html[end:]

    # ----------------------------------------------------------------
    # Internal utility: UUID extraction from HTML
    # ----------------------------------------------------------------

    def _extract_uuids(
        self,
        html: str,
        element: Optional[str] = None,
        uuid_regex: str = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    ) -> List[Dict[str, str]]:
        """Extract elements whose ``name`` attribute matches a UUID from HTML.

        Walks the HTML tree in document order and returns entries for every
        element where the ``name`` attribute is a valid UUID string.

        Args:
            html: HTML string to scan.
            element: If given, only consider elements with this tag name
                     (e.g. ``"p"``).  If ``None``, all elements are scanned.
            uuid_regex: Regex pattern for a UUID (case-insensitive match).

        Returns:
            A list of dicts in document order:
            ``[{"uuid": "...", "innerText": "...", "tag": "..."}, ...]``
        """
        uuid_pat = re.compile(uuid_regex, re.IGNORECASE)
        tag_pat = re.compile(r'<([A-Za-z][\w:-]*)(\s+[^>]*)?/?>', re.IGNORECASE | re.DOTALL)
        name_pat = re.compile(r'\bname\s*=\s*([\'"])(?P<uuid>' + uuid_regex + r')\1', re.IGNORECASE)
        tag_filter = element.lower() if element else None
        void_tags = {"br", "hr", "img", "input", "meta", "link"}
        results: List[Dict[str, str]] = []

        for match in tag_pat.finditer(html):
            tag_name = match.group(1).lower()
            if tag_filter is not None and tag_name != tag_filter:
                continue

            attrs = match.group(2) or ""
            name_match = name_pat.search(attrs)
            if name_match is None:
                continue

            raw_tag = match.group(0).rstrip()
            if raw_tag.endswith("/>") or tag_name in void_tags:
                inner = ""
            else:
                close_pat = re.compile(r'</\s*' + re.escape(tag_name) + r'\s*>', re.IGNORECASE)
                close_match = close_pat.search(html, match.end())
                raw_inner = html[match.end():close_match.start()] if close_match else ""
                inner = re.sub(r'<[^>]+>', '', raw_inner).strip()

            results.append({"uuid": name_match.group("uuid"), "innerText": inner, "tag": tag_name})

        return results

    # ----------------------------------------------------------------
    # Fetch / Publish / Update (from publish.py, update_draft.py)
    # ----------------------------------------------------------------

    def fetch_draft(self, note_key: str) -> Dict[str, Any]:
        """Fetch draft metadata/body by note key."""
        headers = self._json_headers(note_key)
        resp = requests.get(
            f"https://note.com/api/v3/notes/{note_key}",
            params={"draft": "true"},
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 200:
            raise ValueError(f"Draft fetch failed HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json().get("data", {})
        if not data.get("id"):
            raise ValueError(f"Draft fetched for key={note_key}, but response did not include an id.")
        return data

    def publish_draft(
        self,
        note_key: str,
        title: Optional[str] = None,
        hashtags: Optional[list[str]] = None,
        price: Optional[int] = None,
        eyecatch_url: Optional[str] = None,
        eyecatch_image_id: Optional[str] = None,
        index: Optional[bool] = None,
        paid_separator_uuid: Optional[str] = None,
        paid_separator_search: Optional[str] = None,
        insert_toc_search: Optional[str] = None,
        publish: bool = False,
    ) -> Dict[str, Any]:
        """Update and/or publish an existing note.com draft by key.

        If ``paid_separator_uuid`` is not given but ``paid_separator_search`` is,
        the method searches the draft body for the first element whose ``name``
        attribute contains the search text (case-insensitive) and uses that UUID
        as the paid separator.

        ``insert_toc_search`` is accepted for backward compatibility but is not
        applied here. Use ``update_draft`` to insert a table of contents before
        publishing so this method can preserve the saved draft body.
        """
        has_update = any(v is not None for v in (title, hashtags, price, eyecatch_url, eyecatch_image_id, index, paid_separator_uuid, paid_separator_search)) or publish
        if not has_update:
            return {"success": False, "error": "No updates specified. Provide publish=True or other update args."}

        draft = self.fetch_draft(note_key)
        note_id = int(draft["id"])
        existing_body = draft.get("body") or ""

        # Fall back to draft's name if no title provided
        if title is None:
            title = draft.get("name")

        # Auto-resolve UUID from search text
        if paid_separator_uuid is None and paid_separator_search is not None:
            found = self._extract_uuids(existing_body)
            for entry in found:
                if paid_separator_search.lower() in entry["innerText"].lower():
                    paid_separator_uuid = entry["uuid"]
                    break
            if paid_separator_uuid is None:
                return {"success": False, "error": f"Paid separator text '{paid_separator_search}' not found in draft HTML."}

        paid_split = None
        if paid_separator_uuid is not None:
            paid_split = self._split_paid_body(existing_body, paid_separator_uuid)
            if paid_split is None:
                return {"success": False, "error": f"Paid separator UUID '{paid_separator_uuid}' not found in draft HTML."}

        payload: Dict[str, Any] = {
            "body": existing_body,
            "body_length": len(existing_body),
        }
        if title is not None:
            payload["name"] = title
        if hashtags is not None:
            payload["hashtags"] = [_normalize_tag_str(t) for t in hashtags]
        if price is not None:
            payload["price"] = price
        if eyecatch_url is not None:
            payload["eyecatch_url"] = eyecatch_url
        if eyecatch_image_id is not None:
            payload["eyecatch_image_id"] = str(eyecatch_image_id)
        if index is not None:
            payload["index"] = index
        elif publish and "<table-of-contents" in existing_body:
            payload["index"] = True
        if paid_separator_uuid is not None:
            assert paid_split is not None
            payload["free_body"] = paid_split[0]
            payload["pay_body"] = paid_split[1]
            payload["separator"] = paid_separator_uuid
        else:
            payload["free_body"] = existing_body
            payload["pay_body"] = ""
            payload["separator"] = None
        if publish:
            payload["status"] = "published"
            if price is not None or draft.get("price"):
                payload["is_paid"] = True
            payload["send_notifications_flag"] = True

        headers = self._json_headers(note_key)
        resp = requests.put(
            f"https://note.com/api/v1/text_notes/{note_id}",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if resp.status_code in (200, 201, 204, 206):
            data = resp.json().get("data", {}) if resp.text else {}
            return {
                "success": True, "note_id": note_id,
                "key": data.get("key", note_key),
                "status": data.get("status"),
                "url": data.get("url"),
            }
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}

    def update_draft(
        self,
        note_key: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
        eyecatch_url: Optional[str] = None,
        eyecatch_image_id: Optional[str] = None,
        eyecatch_image_type: Optional[str] = None,
        eyecatch_width: Optional[int] = None,
        eyecatch_height: Optional[int] = None,
        hashtags: Optional[list[str]] = None,
        insert_toc_search: Optional[str] = None,
        publish: bool = False,
    ) -> Dict[str, Any]:
        """Update an existing note.com draft by key via POST /draft_save.

        If ``insert_toc_search`` is set, the method finds the first element whose
        ``innerText`` contains the search text and inserts a
        ``<table-of-contents>`` element with a fresh UUID immediately after it.
        """
        has_update = any(v is not None for v in (title, body, eyecatch_url, eyecatch_image_id, eyecatch_image_type,
                       eyecatch_width, eyecatch_height, hashtags, insert_toc_search)) or publish
        if not has_update:
            return {"success": False, "error": "No updates specified."}

        draft = self.fetch_draft(note_key)
        note_id = int(draft["id"])
        existing_body = self._with_fresh_note_uuids(body) if body is not None else draft.get("body", "")

        # Insert TOC after the searched element
        if insert_toc_search is not None:
            existing_body = self._insert_toc_after_search(existing_body, insert_toc_search)
            if existing_body is None:
                return {"success": False, "error": f"TOC insert search text '{insert_toc_search}' not found in draft HTML."}

        payload: Dict[str, Any] = {
            "body": existing_body,
            "body_length": len(existing_body),
            "index": False,
            "is_lead_form": False,
        }
        payload["name"] = title or draft["name"]
        if eyecatch_url is not None:
            payload["eyecatch_url"] = eyecatch_url
        if eyecatch_image_id is not None:
            payload["eyecatch_image_id"] = str(eyecatch_image_id)
        if eyecatch_image_type is not None:
            payload["eyecatch_image_type"] = eyecatch_image_type
        if eyecatch_width is not None:
            payload["eyecatch_width"] = eyecatch_width
        if eyecatch_height is not None:
            payload["eyecatch_height"] = eyecatch_height
        if hashtags is not None:
            payload["hashtags"] = [_normalize_tag(t) for t in hashtags]
        if publish:
            payload["status"] = "published"
            if draft.get("price"):
                payload["is_paid"] = True
            payload["send_notifications_flag"] = True

        headers = self._json_headers(note_key)
        resp = requests.post(
            "https://note.com/api/v1/text_notes/draft_save",
            cookies={"_note_session_v5": self._token},
            headers=headers,
            json=payload,
            params={"id": note_id, "is_temp_saved": "true"},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data = resp.json().get("data", {})
            return {
                "success": True, "note_id": note_id, "key": note_key,
                "updated_at": data.get("updated_at"), "result": data.get("result"),
            }
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}

    # ----------------------------------------------------------------
    # Search (from search.py)
    # ----------------------------------------------------------------

    def search_notes(self, keyword: str, size: int = 10, start: int = 0) -> list:
        """Search articles on note.com (context=note), returns list of note dicts."""
        url = "https://note.com/api/v3/searches"
        r = requests.get(
            url,
            params={"context": "note", "q": keyword, "size": size, "start": start},
            cookies={"_note_session_v5": self._token},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Cookie": f"_note_session_v5={self._token}",
                "User-Agent": _BROWSER_UA,
                "Referer": f"https://note.com/search?q={quote(keyword)}",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()["data"]
        notes_data = data.get("notes", {})
        return notes_data.get("contents", [])

    # ----------------------------------------------------------------
    # Magazine operations (from list_magazine.py, add_to_magazine.py)
    # ----------------------------------------------------------------

    def get_magazine_info(self, magazine_id: str) -> dict:
        """Get magazine metadata."""
        url = f"https://note.com/api/v1/magazines/{magazine_id}"
        r = requests.get(
            url,
            cookies={"_note_session_v5": self._token},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Cookie": f"_note_session_v5={self._token}",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["data"]

    def list_magazine_articles(self, magazine_id: str, page: int = 1) -> list[dict]:
        """Get articles from a magazine (magazine owner's articles only)."""
        url = f"https://note.com/api/v1/magazines/{magazine_id}/notes"
        r = requests.get(
            url,
            params={"page": page},
            cookies={"_note_session_v5": self._token},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Cookie": f"_note_session_v5={self._token}",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()["data"]
        return data.get("notes", [])

    def resolve_note_id(self, note_key: str) -> int:
        """Resolve a public note key to the internal numeric note ID."""
        response = requests.get(
            f"https://note.com/api/v3/notes/{note_key}",
            cookies={"_note_session_v5": self._token},
            headers=self._get_headers(note_key),
            timeout=10,
        )
        response.raise_for_status()
        note_id = response.json().get("data", {}).get("id")
        if note_id is None:
            raise ValueError(f"note ID was not present in the response for {note_key}")
        return int(note_id)

    def add_to_magazine(self, magazine_id: str, note_key: str, note_id: Optional[int] = None) -> dict:
        """Add a published article to a magazine owned by the authenticated user."""
        if note_id is None:
            note_id = self.resolve_note_id(note_key)
        response = requests.post(
            f"https://note.com/api/v1/our/magazines/{magazine_id}/notes",
            cookies={"_note_session_v5": self._token},
            headers=self._get_headers(note_key),
            json={"note_id": note_id, "note_key": note_key},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    # ----------------------------------------------------------------
    # Article fetching & liking (from get_article.py, like.py)
    # ----------------------------------------------------------------

    def fetch_article(self, article_id: str) -> dict:
        """Get article details from note.com API."""
        url = f"https://note.com/api/v3/notes/{article_id}"
        r = requests.get(
            url,
            cookies={"_note_session_v5": self._token},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Cookie": f"_note_session_v5={self._token}",
                "User-Agent": _BROWSER_UA,
                "Referer": f"https://note.com/p/{article_id}",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def like_article(self, article_id: str) -> dict:
        """Send a like to a note.com article."""
        url = f"https://note.com/api/v3/notes/{article_id}/likes"
        r = requests.post(
            url,
            cookies={"_note_session_v5": self._token},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Cookie": f"_note_session_v5={self._token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"https://note.com/n/{article_id}",
                "User-Agent": _BROWSER_UA,
                "X-Requested-With": "XMLHttpRequest",
            },
            data={"like": "true"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def get_related_articles(self, article_key: str) -> list:
        """Get recommended/related articles for a note.com article.

        Uses the mkit_layouts JSON endpoint (related_notes_revelio context),
        which reliably returns related articles. The older /api/v3/notes/{key}
        endpoint's recommended_notes.contents often returns empty.
        """
        url = "https://note.com/api/v3/mkit_layouts/json"
        params = {
            "context": "related_notes_revelio",
            "page": 1,
            "args[note_key]": article_key,
        }
        r = requests.get(
            url,
            params=params,
            cookies={"_note_session_v5": self._token},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Cookie": f"_note_session_v5={self._token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"https://note.com/n/{article_key}",
                "User-Agent": _BROWSER_UA,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()

        # Related articles are in data.sections (2D array of article dicts).
        data = body.get("data", {})
        sections = data.get("sections", [])

        related = []
        for section in sections:
            for article in section:
                if isinstance(article, dict) and "key" in article:
                    related.append(article)

        return related

    # ----------------------------------------------------------------
    # Engagement (from engage.py)
    # ----------------------------------------------------------------

    def engage(
        self,
        magazine_id: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 30,
        nth: int = 0,
        tagindex: int = 0,
        delay: float = 5.0,
        verbose: bool = False,
    ) -> dict:
        """Engage with note.com articles: search and like.

        Two modes:
          - keyword: search directly with keyword, like results
          - magazine: list magazine -> pick article -> extract hashtag -> search -> like

        Keyword takes priority if both provided.

        Returns {"liked": [(url, keyword), ...], "failed": [(url, keyword, error), ...]}
        """
        if not magazine_id and not keyword:
            return {"success": False, "error": "Provide either magazine_id or keyword."}

        # keyword takes priority
        if keyword:
            mode = "keyword"
            magazine_id = None
        else:
            mode = "magazine"

        if mode == "keyword":
            results = self.search_notes(keyword, size=limit, start=0)
            if not results:
                return {"success": True, "liked": [], "failed": []}
        else:
            articles = self.list_magazine_articles(magazine_id, page=1)
            if not articles:
                return {"success": False, "error": "No articles found in magazine."}
            articles.sort(
                key=lambda a: a.get("publish_at") or a.get("created_at") or "",
                reverse=True,
            )
            source = articles[nth] if nth < len(articles) else articles[0]
            article_key = source["key"]
            if verbose:
                source_title = source.get("name", source.get("title", ""))
                print(f"Source article: {article_key} {source_title}", flush=True)
            related = self.get_related_articles(article_key)
            if not related:
                return {"success": False, "error": "No related articles found."}
            results = related

        liked: list[tuple[str, str]] = []
        failed: list[tuple[str, str, str]] = []
        to_like = results[:limit]
        if verbose:
            print(f"Engaging with {len(to_like)} article(s); delay={delay:g}s", flush=True)
        for idx, item in enumerate(to_like, 1):
            aid = item.get("key", "")
            art_title = item.get("name", item.get("title", ""))
            user = item.get("publisher", item.get("user", {}))
            user_handle = user.get("urlname", user.get("path", "?"))
            url = f"https://note.com/{user_handle}/n/{aid}"
            if verbose:
                print(f"[{idx}/{len(to_like)}] {aid} {art_title}", flush=True)
            try:
                self.like_article(aid)
                liked.append((url, keyword or "related"))
                if verbose:
                    print("  liked", flush=True)
            except Exception as e:
                failed.append((url, keyword or "related", str(e)[:80]))
                if verbose:
                    print(f"  failed: {e}", flush=True)
            if idx < len(to_like) and delay > 0:
                if verbose:
                    print(f"  sleeping {delay:g}s", flush=True)
                time.sleep(delay)

        return {
            "success": True,
            "liked": liked,
            "failed": failed,
            "summary": f"Done! Liked: {len(liked)}  Failed: {len(failed)}",
        }


# ===================================================================
# CLI entry point
# ===================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Unified note.com API client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command")

    # --- draft (create) ---
    draft_p = sub.add_parser("draft", help="Create a new draft")
    draft_p.add_argument("--title", required=True, help="Draft title")
    draft_p.add_argument("--body-file", required=True, help="Path to markdown body file")
    draft_p.add_argument("--cover", help="Cover image path")
    draft_p.add_argument("--token", help="Cookie token")
    draft_p.add_argument("--token-path", help="Path to token file")
    draft_p.add_argument("--tags", help='JSON tag array, e.g. "[AI,note-com]"')
    draft_p.add_argument("--width", type=int, default=1280, help="Cover width")
    draft_p.add_argument("--height", type=int, default=670, help="Cover height")

    # --- fetch ---
    fetch_p = sub.add_parser("fetch", help="Fetch draft by key")
    fetch_p.add_argument("note_key", help="Note key, e.g. na89d642394f3")
    fetch_p.add_argument("--token", help="Cookie token")
    fetch_p.add_argument("--token-path", help="Path to token file")
    # --- publish ---
    pub_p = sub.add_parser("publish", help="Publish an existing draft")
    pub_p.add_argument("note_key", help="Note key, e.g. na89d642394f3")
    pub_p.add_argument("--hashtags", help='JSON tag array')
    pub_p.add_argument("--price", type=int, help="Paid content price in yen")
    pub_p.add_argument("--paid-separator-uuid", help="Separator UUID")
    pub_p.add_argument("--paid-separator-search", help="Search text to auto-locate paid separator in draft body")
    pub_p.add_argument("--eyecatch-url", help="Cover image URL")
    pub_p.add_argument("--eyecatch-id", help="Cover image ID")
    pub_p.add_argument("--index", action="store_true", help="Include in index")
    pub_p.add_argument("--token", help="Cookie token")
    pub_p.add_argument("--token-path", help="Path to token file")

    # --- update ---
    upd_p = sub.add_parser("update", help="Update an existing draft")
    upd_p.add_argument("note_key", help="Note key")
    upd_p.add_argument("--title", help="New title")
    upd_p.add_argument("--body-file", help="Path to markdown file")
    upd_p.add_argument("--eyecatch-url", help="Cover image URL")
    upd_p.add_argument("--eyecatch-id", help="Cover image ID")
    upd_p.add_argument("--eyecatch-type", default="image/jpeg", help="Cover MIME type")
    upd_p.add_argument("--eyecatch-width", type=int, help="Cover width")
    upd_p.add_argument("--eyecatch-height", type=int, help="Cover height")
    upd_p.add_argument("--hashtags", help='JSON tag array')
    upd_p.add_argument("--insert-toc-search", help="Search text to insert a table of contents after")
    upd_p.add_argument("--save-body", help="Save draft HTML body to file")
    upd_p.add_argument("--token", help="Cookie token")
    upd_p.add_argument("--token-path", help="Path to token file")

    # --- extract_uuids ---
    xu_p = sub.add_parser("extract_uuids", help="Extract UUID/name mappings from a draft")
    xu_p.add_argument("note_key", help="Draft note key, e.g. na89d642394f3")
    xu_p.add_argument("--element", default=None, help="Filter by tag name (e.g. p, hr, h2)")
    xu_p.add_argument("--json", action="store_true", help="Output as JSON")
    xu_p.add_argument("--token", help="Cookie token")
    xu_p.add_argument("--token-path", help="Path to token file")

    # --- search ---
    s_p = sub.add_parser("search", help="Search note.com articles")
    s_p.add_argument("keyword", help="Search keyword")
    s_p.add_argument("--size", type=int, default=10, help="Max results")
    s_p.add_argument("--start", type=int, default=0, help="Pagination offset")
    s_p.add_argument("--output", help="Save full JSON result to file")
    s_p.add_argument("--token", help="Cookie token")
    s_p.add_argument("--token-path", help="Path to token file")

    # --- list-magazine ---
    lm_p = sub.add_parser("list-magazine", help="List magazine articles")
    lm_p.add_argument("magazine_id", help="Magazine ID")
    lm_p.add_argument("--page", type=int, default=1, help="Page number")
    lm_p.add_argument("--token", help="Cookie token")
    lm_p.add_argument("--token-path", help="Path to token file")

    # --- add-to-magazine ---
    am_p = sub.add_parser("add-to-magazine", help="Add a published article to a magazine")
    am_p.add_argument("magazine_id", help="Magazine ID")
    am_p.add_argument("note_key", help="Published note key")
    am_p.add_argument("--note-id", type=int, help="Internal numeric note ID (auto-resolved if omitted)")
    am_p.add_argument("--token", help="Cookie token")
    am_p.add_argument("--token-path", help="Path to token file")

    # --- like ---
    lk_p = sub.add_parser("like", help="Like article(s)")
    lk_p.add_argument("article_ids", nargs="*", help="Article key(s)")
    lk_p.add_argument("--file", help="Read article keys from file (one per line)")
    lk_p.add_argument("--list", help='JSON list of article keys, e.g. \'["id1","id2"]\'')
    lk_p.add_argument("--token", help="Cookie token")
    lk_p.add_argument("--token-path", help="Path to token file")

    # --- engage ---
    me_p = sub.add_parser("engage", help="Engage: search and like")
    me_p.add_argument("--magazine-id", "--mag", dest="magazine_id", help="Magazine ID")
    me_p.add_argument("--keyword", dest="keyword", help="Search keyword")
    me_p.add_argument("--limit", type=int, default=30, help="Max articles to like")
    me_p.add_argument("--nth", type=int, default=0, help="Which newest magazine article to use as source (0=latest)")
    me_p.add_argument("--delay", type=float, default=5.0, help="Seconds to wait between likes (default 5)")
    me_p.add_argument("--token", help="Cookie token")
    me_p.add_argument("--token-path", help="Path to token file")

    args = ap.parse_args()
    if not args.command:
        ap.print_help()
        return 1

    client = NoteCom(token=getattr(args, "token", None), token_path=getattr(args, "token_path", None))

    try:
        # --- draft ---
        if args.command == "draft":
            result = client.create_draft(
                title=args.title,
                body_file=args.body_file,
                eyecatch_image=getattr(args, "cover", None),
                hashtags=json.loads(args.tags) if args.tags else None,
                width=args.width,
                height=args.height,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["success"] else 1

        # --- fetch ---
        elif args.command == "fetch":
            draft = client.fetch_draft(args.note_key)
            for key in ("id", "key", "name", "status", "price"):
                val = draft.get(key)
                if val is not None:
                    print(f"  {key}: {val}")
            print(f"  body_length: {len(draft.get('body') or '')}")
            body = draft.get("body", "")
            if body:
                print(f"\n--- BODY ({len(body)} chars) ---\n{body}\n--- END BODY ---")
            return 0

        # --- extract_uuids ---
        elif args.command == "extract_uuids":
            draft = client.fetch_draft(args.note_key)
            body = draft.get("body", "")
            entries = client._extract_uuids(body, getattr(args, "element", None))
            if getattr(args, "json", False):
                print(json.dumps(entries, ensure_ascii=False, indent=2))
            else:
                print(f"Total UUIDs found: {len(entries)}")
                if getattr(args, "element", None):
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

        # --- publish ---
        elif args.command == "publish":
            # Fetch draft to get the title (name) from note.com
            draft = client.fetch_draft(args.note_key)
            result = client.publish_draft(
                args.note_key,
                title=draft.get("name"),
                hashtags=json.loads(args.hashtags) if args.hashtags else None,
                price=args.price,
                paid_separator_uuid=getattr(args, "paid_separator_uuid", None),
                paid_separator_search=getattr(args, "paid_separator_search", None),
                eyecatch_url=getattr(args, "eyecatch_url", None),
                eyecatch_image_id=getattr(args, "eyecatch_id", None),
                index=True if args.index else None,
                publish=True,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["success"] else 1

        # --- update ---
        elif args.command == "update":
            # --save-body: fetch and write draft HTML to a file
            if getattr(args, "save_body", None):
                draft = client.fetch_draft(args.note_key)
                body = draft.get("body", "")
                with open(args.save_body, "w", encoding="utf-8") as f:
                    f.write(body)
                print(f"Saved draft HTML body to {args.save_body} ({len(body)} chars)")
                print(f"  id: {draft['id']}")
                print(f"  name: {draft['name']}")
                return 0

            body = None
            if getattr(args, "body_file", None):
                if not os.path.isfile(args.body_file):
                    print(f"Error: body file not found: {args.body_file}")
                    return 1
                with open(args.body_file, "r", encoding="utf-8") as f:
                    md_text = f.read()
                body = client._markdown_to_note_html(md_text)
            result = client.update_draft(
                args.note_key,
                title=args.title,
                body=body,
                eyecatch_url=getattr(args, "eyecatch_url", None),
                eyecatch_image_id=getattr(args, "eyecatch_id", None),
                eyecatch_image_type=getattr(args, "eyecatch_type", "image/jpeg"),
                eyecatch_width=getattr(args, "eyecatch_width", None),
                eyecatch_height=getattr(args, "eyecatch_height", None),
                hashtags=json.loads(args.hashtags) if args.hashtags else None,
                insert_toc_search=getattr(args, "insert_toc_search", None),
                publish=False,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["success"] else 1

        # --- search ---
        elif args.command == "search":
            results = client.search_notes(args.keyword, size=args.size, start=args.start)
            print(f"Search: '{args.keyword}' (size={args.size}, start={args.start})")
            print(f"  Results: {len(results)}")
            print()
            for idx, note in enumerate(results, 1):
                title = note.get("name", "?")
                key = note.get("key", "")
                price = note.get("price", 0)
                pub = note.get("publish_at", note.get("created_at", ""))
                publisher = note.get("publisher", note.get("user", {}))
                username = publisher.get("urlname", "?")
                author = publisher.get("name", publisher.get("nickname", "?"))
                url = f"https://note.com/{username}/n/{key}"
                print(f"{idx}. [{format_price(price):>4}] {format_date(pub):>10}  by {author}  ->  {url}")
                print(f"   {title}")
                print()
            # Save full JSON to file if requested
            if getattr(args, "output", None):
                # Re-fetch raw JSON for output
                import json as _json
                url = "https://note.com/api/v3/searches"
                r = requests.get(
                    url,
                    params={"context": "note", "q": args.keyword, "size": args.size, "start": args.start},
                    cookies={"_note_session_v5": client._token},
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {client._token}",
                        "Cookie": f"_note_session_v5={client._token}",
                        "User-Agent": _BROWSER_UA,
                        "Referer": f"https://note.com/search?q={quote(args.keyword)}",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=10,
                )
                r.raise_for_status()
                with open(args.output, "w", encoding="utf-8") as f:
                    _json.dump(r.json(), f, ensure_ascii=False, indent=2)
                print(f"Full result saved to {args.output}")
            return 0

        # --- add-to-magazine ---
        elif args.command == "add-to-magazine":
            result = client.add_to_magazine(args.magazine_id, args.note_key, note_id=getattr(args, "note_id", None))
            if result.get("success") or (isinstance(result, dict) and result.get("data", {}).get("status") == "success"):
                print(f"Successfully added note_key={args.note_key} to magazine={args.magazine_id}")
                return 0
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 1

        # --- list-magazine ---
        elif args.command == "list-magazine":
            mag_info = client.get_magazine_info(args.magazine_id)
            print(f"Magazine: {mag_info.get('name', args.magazine_id)} ({args.magazine_id})")
            print(f"  Articles: {mag_info.get('note_count', 0)}")
            print(f"  Status: {mag_info.get('status', '?')}")
            print()
            articles = client.list_magazine_articles(args.magazine_id, page=args.page)
            for idx, article in enumerate(articles, 1):
                title = article.get("name", "?")
                key = article.get("key", "")
                price = article.get("price", 0)
                pub = article.get("publish_at", article.get("created_at", ""))
                print(f"{idx}. [{format_price(price):>4}] {format_date(pub):>10}  ->  https://note.com/w/p/{key}")
                print(f"   {title}")
                print()
            return 0

        # --- like ---
        elif args.command == "like":
            # Collect article IDs from positional args, --file, or --list
            ids: list[str] = list(getattr(args, "article_ids", []) or [])
            if getattr(args, "file", None):
                if os.path.exists(args.file):
                    ids.extend([l.strip() for l in open(args.file, encoding="utf-8") if l.strip()])
                else:
                    print(f"Error: file not found: {args.file}", file=sys.stderr)
                    return 1
            if getattr(args, "list", None):
                try:
                    ids.extend(json.loads(args.list))
                except json.JSONDecodeError as e:
                    print(f"Error: --list JSON parse failed: {e}", file=sys.stderr)
                    return 1
            if not ids:
                print("Usage: note_com.py like <id1> [id2 ...]  or  --file <path>  or  --list '[\"id1\"]'", file=sys.stderr)
                return 1
            print(f"\n❤️ Liking {len(ids)} article(s)...\n")
            for idx, aid in enumerate(ids, 1):
                print(f"[{idx}/{len(ids)}] → {aid}")
                try:
                    result = client.like_article(aid)
                    print("   Liked!")
                except Exception as e:
                    print(f"   Failed: {e}")
                if idx < len(ids):
                    time.sleep(5)
            print(f"\n✅ Done!")
            return 0

        # --- engage ---
        elif args.command == "engage":
            result = client.engage(
                magazine_id=getattr(args, "magazine_id", None),
                keyword=getattr(args, "keyword", None),
                limit=args.limit,
                nth=args.nth,
                delay=args.delay,
                verbose=True,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("success") else 1

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
