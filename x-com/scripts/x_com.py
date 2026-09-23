#!/usr/bin/env python3
"""Unified X.com (Twitter) Articles API client.

Reverse-engineered from the X web app's GraphQL traffic. Supports the full
long-form Article workflow: create a draft, set its title/content/cover,
publish it, unpublish a published article, and list drafts or published
articles for the authenticated user.

Usage (CLI -- run with --help for full options):
    python3 x_com.py draft --title "My Article" --body-file article.md
    python3 x_com.py create
    python3 x_com.py set-title <article_id> --title "New Title"
    python3 x_com.py set-content <article_id> --body-file article.md
    python3 x_com.py publish <article_id>
    python3 x_com.py unpublish <article_id>
    python3 x_com.py delete <article_id>
    python3 x_com.py list --status draft
    python3 x_com.py list --status published

Usage (import):
    from x_com import XCom
    client = XCom(cookie_path=".secrets/x_com_cookie.txt")
    result = client.create_article(title="Hello", body_file="article.md")
    client.publish_article(result["article_id"])

Authentication
--------------
X authenticates these calls with two session cookies:

    auth_token   long-lived session cookie
    ct0          CSRF cookie, also echoed in the ``x-csrf-token`` header

Provide them one of these ways (highest priority first):

    --auth-token / --ct0
    --cookie "auth_token=...; ct0=...; twid=u%3D<user_id>; ..."
    --cookie-path <file containing the raw browser Cookie header>
    env X_AUTH_TOKEN + X_CT0, or env X_COM_COOKIE (full cookie string)

Listing articles needs the numeric user id. It is parsed from the ``twid``
cookie when present, otherwise pass ``--user-id``.
"""

import os
import re
import sys
import json
import base64
import string
import random
import hashlib
import uuid
import argparse
import mimetypes
from urllib.parse import quote
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests


# ===================================================================
# Shared constants
# ===================================================================

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Public web bearer token used by the x.com SPA for all GraphQL calls.
# It is not a secret -- it is shipped in the site's JavaScript bundle.
_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# GraphQL query IDs captured from the web app, keyed by the exact operation name
# used in the request URL (.../graphql/<queryId>/<OperationName>). X rotates
# these when it ships a new bundle; if a call starts returning HTTP 404 or a
# GRAPHQL_VALIDATION_FAILED error, recapture a HAR and refresh the ids below.
_Q = {
    "ArticleEntityDraftCreate": "_rbmb_NKLqKVBr5X_MSoMQ",
    "ArticleEntityUpdateTitle": "brHFCBTXXg8WOqc7BnXfAw",
    "ArticleEntityUpdateContent": "x4Pz2ifYkOD6uSvzxOIUig",
    "ArticleEntityUpdateCoverMedia": "TRcLHwCpNAkBVcxy4FLH1A",
    "ArticleEntityPublish": "xkPT5esHwPTNtJfp-1xKaQ",
    "ArticleEntityUnpublish": "4M8Wv2IADEw61KFMrXqOSQ",
    "ArticleEntityDelete": "e4lWqB6m2TA8Fn_j9L9xEA",
    "ArticleEntitiesSlice": "oLK4ja2Rj6q21t4ktw8pYA",
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
    # A quoted scalar is a string even if it contains commas; only bare
    # comma-separated values are lists (YAML-lite front matter).
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    if "," in value:
        return [_strip_wrapping_quotes(part) for part in value.split(",") if part.strip()]
    return _strip_wrapping_quotes(value)


def parse_markdown_front_matter(markdown_text: str) -> Tuple[str, Dict[str, Any]]:
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

    metadata: Dict[str, Any] = {}
    for line in lines[1:end_index]:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower().replace("-", "_")] = _parse_front_matter_value(value)

    body = "\n".join(lines[end_index + 1:])
    return body.lstrip("\n"), metadata


def _metadata_text(metadata: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = metadata.get(key)
        if value is not None and not isinstance(value, (list, dict)):
            return str(value)
    return None


# Feature flags the endpoints expect. Sent verbatim on every request.
_FEATURES = {
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}


# ===================================================================
# Credential helpers
# ===================================================================

def _parse_cookie_string(cookie: str) -> Dict[str, str]:
    """Parse a raw browser Cookie header into a name->value dict."""
    out: Dict[str, str] = {}
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        out[name.strip()] = value.strip()
    return out


def _user_id_from_twid(twid: Optional[str]) -> Optional[str]:
    """Extract the numeric user id from a ``twid`` cookie (``u%3D<id>``)."""
    if not twid:
        return None
    # twid looks like "u=<id>" or url-encoded "u%3D<id>"
    m = re.search(r"u(?:=|%3D)(\d+)", twid, re.IGNORECASE)
    return m.group(1) if m else None


# Default files searched (relative to cwd) when no credentials are passed. Lets
# an agent drop the two values under .secrets/ and run commands with no flags.
_DEFAULT_AUTH_TOKEN_PATH = ".secrets/auth_token.txt"
_DEFAULT_CT0_PATH = ".secrets/ct0.txt"


def _read_file(path: Optional[str]) -> Optional[str]:
    if path and os.path.exists(path):
        return open(path, "r", encoding="utf-8").read().strip()
    return None


def _resolve_creds(
    auth_token: Optional[str] = None,
    ct0: Optional[str] = None,
    cookie: Optional[str] = None,
    cookie_path: Optional[str] = None,
    auth_token_path: Optional[str] = None,
    ct0_path: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """Resolve (auth_token, ct0, user_id) from args, files, a cookie string, or env.

    Precedence for each token: explicit value > --auth-token-path/--ct0-path >
    --cookie > --cookie-path > env X_AUTH_TOKEN/X_CT0 > env X_COM_COOKIE >
    default files (.secrets/auth_token.txt, .secrets/ct0.txt).
    """
    resolved_user = user_id

    # 1. Discrete token files (.secrets/auth_token.txt, .secrets/ct0.txt style).
    auth_token = auth_token or _read_file(auth_token_path)
    ct0 = ct0 or _read_file(ct0_path)

    # 2. A cookie string from --cookie, --cookie-path, or the environment. Also
    #    the only source of twid -> user_id.
    cookie_str = cookie
    if not cookie_str:
        cookie_str = _read_file(cookie_path)
    if not cookie_str:
        cookie_str = os.environ.get("X_COM_COOKIE", "")
    if cookie_str:
        jar = _parse_cookie_string(cookie_str)
        auth_token = auth_token or jar.get("auth_token")
        ct0 = ct0 or jar.get("ct0")
        resolved_user = resolved_user or _user_id_from_twid(jar.get("twid"))

    # 3. Discrete env vars.
    auth_token = auth_token or os.environ.get("X_AUTH_TOKEN")
    ct0 = ct0 or os.environ.get("X_CT0")

    # 4. Default files under .secrets/ so no-flag invocations still work.
    auth_token = auth_token or _read_file(_DEFAULT_AUTH_TOKEN_PATH)
    ct0 = ct0 or _read_file(_DEFAULT_CT0_PATH)

    if not auth_token or not ct0:
        raise ValueError(
            "X credentials required. Provide --auth-token and --ct0, point "
            "--auth-token-path/--ct0-path at files, use --cookie/--cookie-path, "
            "set X_AUTH_TOKEN/X_CT0 (or X_COM_COOKIE), or drop the values in "
            ".secrets/auth_token.txt and .secrets/ct0.txt."
        )
    return auth_token, ct0, resolved_user


def _block_key() -> str:
    """Draft.js block keys are short random alphanumeric strings."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=5))


# ===================================================================
# Markdown -> Draft.js content_state conversion
# ===================================================================

# Inline markers -> Draft.js inline style names. Only ``**`` (Bold) and ``*``
# (Italic) are emitted; X's article schema rejects any other inline style enum.
# X articles have no inline-code style, so backticks are kept literal, and ``_``
# is left literal too (to avoid mangling URLs and snake_case words).
_LINK_RE = re.compile(r'\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)')
_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)')
# The established convention (other projects) for an image caption: a pure
# italic line ``*this is the caption*`` just under an image -> MEDIA data.caption.
_CAPTION_RE = re.compile(r"\*([^*\[\]()]+)\*")
_TABLE_ROW_RE = re.compile(r"^\|.*\|\s*$")


def _parse_inline(
    text: str,
    link_sink: Optional[Callable[[str], int]] = None,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Strip inline markdown -> (clean_text, inline_style_ranges, entity_ranges).

    ``link_sink(url)`` returns the entity key to attach to a link's label
    range. When it is None, link syntax is kept verbatim (also used when
    re-parsing a label, so nested links are never turned into entities twice).
    """
    ranges: List[Dict[str, Any]] = []
    eranges: List[Dict[str, Any]] = []
    out: List[str] = []
    open_at: Dict[str, int] = {}  # style -> offset where it opened
    i, n = 0, len(text)

    def toggle(style: str) -> None:
        if style in open_at:
            start = open_at.pop(style)
            length = len(out) - start
            if length > 0:
                ranges.append({"offset": start, "length": length, "style": style})
        else:
            open_at[style] = len(out)

    while i < n:
        if text.startswith("**", i):
            toggle("Bold")
            i += 2
            continue
        ch = text[i]
        if ch == "\\" and i + 1 < n:  # escaped marker -> literal next char
            out.append(text[i + 1])
            i += 2
            continue
        if ch == "*":
            toggle("Italic")
            i += 1
            continue
        if ch == "[" and link_sink is not None and (i == 0 or text[i - 1] != "!"):
            m = _LINK_RE.match(text, i)
            if m:
                start = len(out)
                label_text, label_styles, _ = _parse_inline(m.group(1))
                out.append(label_text)
                for s in label_styles:
                    ranges.append({"offset": start + s["offset"],
                                   "length": s["length"], "style": s["style"]})
                eranges.append({"key": link_sink(m.group(2)),
                                "offset": start, "length": len(label_text)})
                i = m.end()
                continue
        out.append(ch)
        i += 1

    # Any marker left unclosed is not real formatting; toggle() only records a
    # range on close, so we never emit a partial range spilling to block end.
    ranges.sort(key=lambda r: r["offset"])
    return "".join(out), ranges, eranges


def _text_block(
    block_type: str,
    raw_text: str,
    link_sink: Optional[Callable[[str], int]] = None,
) -> Dict[str, Any]:
    clean, styles, eranges = _parse_inline(raw_text, link_sink)
    return {
        "data": {},
        "text": clean,
        "key": _block_key(),
        "type": block_type,
        "entity_ranges": eranges,
        "inline_style_ranges": styles,
    }


def markdown_to_content_state(
    markdown_text: str,
    media_uploader: Optional[Callable[[str], str]] = None,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert markdown into an X Articles ``content_state`` object.

    Supported block constructs (mappings reflect what X's schema accepts):
        #                     header-one
        ## and deeper         header-two  (X has only two heading levels)
        > quote               blockquote
        - or *                unordered-list-item
        1.                    ordered-list-item
        ``` fenced ```        atomic block + MARKDOWN entity (holds the whole
                              fenced snippet verbatim -- there is no code-block
                              Draft.js type)
        --- / *** / ___       divider (atomic block + DIVIDER entity)
        ![alt](path)          atomic block + MEDIA entity; image paths resolve
                              against ``base_dir`` and upload via
                              ``media_uploader`` (path -> media_id). Skipped
                              with a warning when no uploader is given.
        anything else         unstyled paragraph

    Inline ``**bold**`` / ``*italic*`` become Draft.js inline style ranges and
    ``[label](url)`` becomes a LINK entity. A leading ``# Title`` identical to
    the front-matter title is dropped (X renders the title separately from the
    body). Blank lines separate blocks.
    """
    markdown_text, metadata = parse_markdown_front_matter(markdown_text)
    fm_title = (_metadata_text(metadata, "title") or "").strip()
    if fm_title:
        markdown_text = re.sub(
            r"\A\s*#[ \t]*" + re.escape(fm_title) + r"[ \t]*\r?\n",
            "", markdown_text, count=1)
    blocks: List[Dict[str, Any]] = []
    entity_map: List[Dict[str, Any]] = []
    link_keys: Dict[str, int] = {}
    media_seq = 0
    pending_media_key: Optional[int] = None  # MEDIA entity awaiting a caption line
    pending_gap = 0                          # blank lines seen since pending armed
    in_table = False
    table_lines: List[str] = []
    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    in_code = False
    code_lines: List[str] = []
    code_lang = ""

    def add_entity(value: Dict[str, Any]) -> int:
        key = len(entity_map)
        entity_map.append({"key": str(key), "value": value})
        return key

    def add_link(url: str) -> int:
        # X reuses one LINK entity per url; deduping keeps entity_map small
        # for articles that cite the same source many times.
        if url in link_keys:
            return link_keys[url]
        key = add_entity({"data": {"url": url}, "type": "LINK", "mutability": "Mutable"})
        link_keys[url] = key
        return key

    def add_atomic(entity_value: Dict[str, Any]) -> int:
        """Append an atomic block backed by a new entity (divider, code, media)."""
        entity_key = add_entity(entity_value)
        blocks.append({
            "data": {},
            "text": " ",
            "key": _block_key(),
            "type": "atomic",
            "entity_ranges": [{"key": entity_key, "offset": 0, "length": 1}],
            "inline_style_ranges": [],
        })
        return entity_key

    def flush_code() -> None:
        # X renders a fenced code block as an atomic block backed by a MARKDOWN
        # entity whose data.markdown holds the whole fenced snippet verbatim
        # (language + code + fences). There is no code-block Draft.js type.
        fenced = "```" + code_lang + "\n" + "\n".join(code_lines) + "\n```"
        add_atomic({"data": {"markdown": fenced}, "type": "MARKDOWN", "mutability": "Mutable"})

    def flush_table() -> None:
        # Pipe tables ride the same MARKDOWN entity mechanism: the composer
        # sends the verbatim markdown table text (header/separator/rows).
        if table_lines:
            add_atomic({"data": {"markdown": "\n".join(table_lines)},
                        "type": "MARKDOWN", "mutability": "Mutable"})
            table_lines.clear()

    def add_divider() -> None:
        add_atomic({"data": {}, "type": "DIVIDER", "mutability": "Immutable"})

    def add_media(raw_path: str) -> None:
        # Composer-captured MEDIA shape: Immutable entity carrying a fresh
        # uuid4 entity_key plus media_items referencing an uploaded
        # (tweet_image) media id under the DraftTweetImage category. An
        # optional data.caption renders under the image in the X composer.
        nonlocal media_seq, pending_media_key, pending_gap
        if media_uploader is None:
            print(f"warning: inline image '{raw_path}' skipped (upload disabled)",
                  file=sys.stderr)
            return
        path = raw_path if os.path.isabs(raw_path) else os.path.normpath(
            os.path.join(base_dir or os.getcwd(), raw_path))
        try:
            media_id = media_uploader(path)
        except Exception as exc:
            print(f"warning: inline image '{raw_path}' upload failed: {exc}",
                  file=sys.stderr)
            return
        media_seq += 1
        pending_gap = 0
        pending_media_key = add_atomic({
            "type": "MEDIA",
            "mutability": "Immutable",
            "data": {
                "entity_key": str(uuid.uuid4()),
                "media_items": [{
                    "local_media_id": media_seq,
                    "media_category": "DraftTweetImage",
                    "media_id": str(media_id),
                }],
            },
        })

    for line in lines:
        stripped = line.strip()

        if in_table and not stripped.startswith("|"):
            flush_table()
            in_table = False

        if stripped.startswith("```"):
            if in_code:
                flush_code()
                code_lines = []
                code_lang = ""
                in_code = False
            else:
                in_code = True
                code_lines = []
                code_lang = stripped[3:].strip()
            continue
        if in_code:
            code_lines.append(line)
            continue

        if not stripped:
            if pending_media_key is not None:
                pending_gap += 1
                if pending_gap > 1:  # paragraph break ends the caption window
                    pending_media_key = None
            continue

        # A pure-italic line directly under an image (one blank line allowed)
        # becomes that image's caption (MEDIA entity data.caption), not a paragraph.
        if pending_media_key is not None:
            cm = _CAPTION_RE.fullmatch(stripped)
            if cm:
                caption_text, _styles, _ers = _parse_inline(stripped)
                entity_map[pending_media_key]["value"]["data"]["caption"] = caption_text
                pending_media_key = None
                continue

        # Pipe table rows accumulate into one MARKDOWN entity.
        if _TABLE_ROW_RE.match(stripped):
            if not in_table:
                in_table = True
                table_lines.clear()
            table_lines.append(stripped)
            pending_media_key = None
            continue
        if in_table:
            flush_table()
            in_table = False

        # Peel image refs off the line; each becomes its own atomic MEDIA
        # block after whatever text remains on the line.
        img_paths = [mm.group(2) for mm in _IMAGE_RE.finditer(stripped)]
        if img_paths:
            stripped = _IMAGE_RE.sub("", stripped).strip()
            if not stripped:
                for p in img_paths:
                    add_media(p)
                continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            pending_media_key = None
            add_divider()
            for p in img_paths:
                add_media(p)
            continue

        m = re.match(r"(#{1,6})\s+(.*)", stripped)
        if m:
            # X articles only support two heading levels. # -> H1, everything
            # deeper (## and beyond) collapses to H2.
            block_type = "header-one" if len(m.group(1)) == 1 else "header-two"
            inner = m.group(2)
        else:
            m = re.match(r">\s?(.*)", stripped)
            if m:
                block_type, inner = "blockquote", m.group(1)
            else:
                m = re.match(r"(?:[-*+])\s+(.*)", stripped)
                if m:
                    block_type, inner = "unordered-list-item", m.group(1)
                else:
                    m = re.match(r"\d+\.\s+(.*)", stripped)
                    if m:
                        block_type, inner = "ordered-list-item", m.group(1)
                    else:
                        block_type, inner = "unstyled", stripped
        pending_media_key = None
        blocks.append(_text_block(block_type, inner, add_link))
        for p in img_paths:
            add_media(p)

    if in_code:  # unterminated fence
        flush_code()
    if in_table:
        flush_table()

    return {"blocks": blocks, "entity_map": entity_map}


# ===================================================================
# Main client class
# ===================================================================

class XCom:
    """X.com (Twitter) Articles API client.

    Wraps the article GraphQL endpoints used by the web composer:
    draft creation, title/content/cover updates, publish, unpublish,
    and listing drafts or published articles.
    """

    def __init__(
        self,
        auth_token: Optional[str] = None,
        ct0: Optional[str] = None,
        cookie: Optional[str] = None,
        cookie_path: Optional[str] = None,
        auth_token_path: Optional[str] = None,
        ct0_path: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self._auth_token, self._ct0, self._user_id = _resolve_creds(
            auth_token=auth_token, ct0=ct0, cookie=cookie,
            cookie_path=cookie_path, auth_token_path=auth_token_path,
            ct0_path=ct0_path, user_id=user_id,
        )

    # ----------------------------------------------------------------
    # HTTP helpers
    # ----------------------------------------------------------------

    def _headers(self, referer: str = "https://x.com/compose/articles") -> Dict[str, str]:
        return {
            "authorization": f"Bearer {_BEARER}",
            "x-csrf-token": self._ct0,
            "cookie": f"auth_token={self._auth_token}; ct0={self._ct0}",
            "content-type": "application/json",
            "accept": "*/*",
            "origin": "https://x.com",
            "referer": referer,
            "user-agent": _BROWSER_UA,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
        }

    def _post(self, op: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Run a mutation-style GraphQL POST and return the parsed JSON."""
        query_id = _Q[op]
        url = f"https://x.com/i/api/graphql/{query_id}/{op}"
        payload = {"variables": variables, "features": _FEATURES, "queryId": query_id}
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=30)
        return self._parse(op, resp)

    def _get(self, op: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Run a query-style GraphQL GET and return the parsed JSON."""
        query_id = _Q[op]
        url = f"https://x.com/i/api/graphql/{query_id}/{op}"
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(_FEATURES, separators=(",", ":")),
        }
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        return self._parse(op, resp)

    @staticmethod
    def _parse(op: str, resp: requests.Response) -> Dict[str, Any]:
        if resp.status_code not in (200, 201):
            raise ValueError(f"{op} failed HTTP {resp.status_code}: {resp.text[:400]}")
        body = resp.json() if resp.text else {}
        if isinstance(body, dict) and body.get("errors"):
            raise ValueError(f"{op} GraphQL error: {json.dumps(body['errors'])[:400]}")
        return body

    # ----------------------------------------------------------------
    # ID helpers
    # ----------------------------------------------------------------

    @staticmethod
    def _article_id(result: Dict[str, Any]) -> str:
        """Return the numeric article entity id from an article result dict.

        Prefers ``rest_id``; falls back to decoding the base64 ``id`` field
        (``ArticleEntity:<numeric>``).
        """
        rid = result.get("rest_id")
        if rid:
            return str(rid)
        encoded = result.get("id")
        if encoded:
            decoded = base64.b64decode(encoded).decode("utf-8", "replace")
            return decoded.split(":", 1)[-1]
        raise ValueError("Could not determine article id from response.")

    @property
    def user_id(self) -> Optional[str]:
        return self._user_id

    # ----------------------------------------------------------------
    # Draft lifecycle
    # ----------------------------------------------------------------

    def create_draft_shell(
        self,
        title: str = "",
        content_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create an empty article draft. Returns {"article_id", "result"}."""
        variables = {
            "content_state": content_state or {"blocks": [], "entity_map": []},
            "title": title,
        }
        body = self._post("ArticleEntityDraftCreate", variables)
        result = (
            body["data"]["articleentity_create_draft"]
            ["article_entity_results"]["result"]
        )
        return {"success": True, "article_id": self._article_id(result), "result": result}

    def set_title(self, article_id: str, title: str) -> Dict[str, Any]:
        """Set an article draft's title."""
        self._post("ArticleEntityUpdateTitle", {"articleEntityId": str(article_id), "title": title})
        return {"success": True, "article_id": str(article_id), "title": title}

    def set_content(
        self,
        article_id: str,
        content_state: Optional[Dict[str, Any]] = None,
        body_file: Optional[str] = None,
        markdown: Optional[str] = None,
        upload_images: bool = True,
    ) -> Dict[str, Any]:
        """Set an article draft's body.

        Provide one of ``content_state`` (raw Draft.js), ``markdown`` (a
        markdown string), or ``body_file`` (a path to a markdown file). Inline
        ``![alt](path)`` images are uploaded and inserted as MEDIA entities
        (paths resolved against the body file's directory) unless
        ``upload_images`` is False.
        """
        if content_state is None:
            if markdown is None and body_file is not None:
                with open(body_file, "r", encoding="utf-8") as f:
                    markdown = f.read()
            if markdown is None:
                raise ValueError("set_content needs content_state, markdown, or body_file.")
            cache: Dict[str, str] = {}

            def _upload(path: str) -> str:
                if path not in cache:
                    cache[path] = self.upload_media(path)
                return cache[path]

            content_state = markdown_to_content_state(
                markdown,
                media_uploader=_upload if upload_images else None,
                base_dir=os.path.dirname(os.path.abspath(body_file)) if body_file else None,
            )
        self._post("ArticleEntityUpdateContent", {
            "content_state": content_state,
            "article_entity": str(article_id),
        })
        return {"success": True, "article_id": str(article_id),
                "blocks": len(content_state.get("blocks", []))}

    def upload_media(self, image_path: str, media_category: str = "tweet_image") -> str:
        """Upload an image via the chunked media endpoint; return its media_id.

        Runs INIT -> APPEND -> FINALIZE against ``upload.x.com``. The whole file
        is sent as a single APPEND segment (fine for cover-sized images).
        """
        if not os.path.isfile(image_path):
            raise ValueError(f"image file not found: {image_path}")
        with open(image_path, "rb") as f:
            data = f.read()
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        base = "https://upload.x.com/i/media/upload.json"
        headers = self._headers(referer="https://x.com/")
        headers.pop("content-type", None)  # let requests set it per request

        # INIT
        r = requests.post(base, headers=headers, params={
            "command": "INIT", "total_bytes": str(len(data)),
            "media_type": mime, "media_category": media_category,
        }, timeout=60)
        if r.status_code not in (200, 201, 202):
            raise ValueError(f"media INIT failed HTTP {r.status_code}: {r.text[:300]}")
        media_id = r.json()["media_id_string"]

        # APPEND (single segment)
        r = requests.post(base, headers=headers, params={
            "command": "APPEND", "media_id": media_id, "segment_index": "0",
        }, files={"media": ("blob", data, "application/octet-stream")}, timeout=120)
        if r.status_code not in (200, 201, 202, 204):
            raise ValueError(f"media APPEND failed HTTP {r.status_code}: {r.text[:300]}")

        # FINALIZE
        r = requests.post(base, headers=headers, params={
            "command": "FINALIZE", "media_id": media_id,
            "original_md5": hashlib.md5(data).hexdigest(),
        }, timeout=60)
        if r.status_code not in (200, 201):
            raise ValueError(f"media FINALIZE failed HTTP {r.status_code}: {r.text[:300]}")
        return media_id

    def set_cover_media(
        self,
        article_id: str,
        media_id: str,
        media_category: str = "DraftTweetImage",
    ) -> Dict[str, Any]:
        """Attach an already-uploaded media id as the article cover image.

        To upload a local image file instead, use ``set_cover_image``.
        """
        self._post("ArticleEntityUpdateCoverMedia", {
            "articleEntityId": str(article_id),
            "coverMedia": {"media_id": str(media_id), "media_category": media_category},
        })
        return {"success": True, "article_id": str(article_id), "media_id": str(media_id)}

    def set_cover_image(self, article_id: str, image_path: str) -> Dict[str, Any]:
        """Upload a local image and set it as the article's cover in one step."""
        media_id = self.upload_media(image_path, media_category="tweet_image")
        result = self.set_cover_media(article_id, media_id)
        result["image_path"] = image_path
        return result

    def publish_article(self, article_id: str, visibility: str = "Public") -> Dict[str, Any]:
        """Publish an article draft. ``visibility`` is typically ``Public``."""
        body = self._post("ArticleEntityPublish", {
            "articleEntityId": str(article_id),
            "visibilitySetting": visibility,
        })
        result = (
            body.get("data", {}).get("articleentity_publish", {})
            .get("article_entity_results", {}).get("result", {})
        )
        return {
            "success": True,
            "article_id": str(article_id),
            "lifecycle": result.get("lifecycle_state", {}).get("lifecycle"),
        }

    def unpublish_article(self, article_id: str) -> Dict[str, Any]:
        """Unpublish a published article, returning it to draft state."""
        self._post("ArticleEntityUnpublish", {"articleEntityId": str(article_id)})
        return {"success": True, "article_id": str(article_id)}

    def delete_article(self, article_id: str) -> Dict[str, Any]:
        """Permanently delete an article (draft or published). Irreversible."""
        body = self._post("ArticleEntityDelete", {"articleEntityId": str(article_id)})
        return {
            "success": True,
            "article_id": str(article_id),
            "result": body.get("data", {}).get("articleentity_delete"),
        }

    # ----------------------------------------------------------------
    # Combined create flow
    # ----------------------------------------------------------------

    def create_article(
        self,
        title: Optional[str],
        body_file: Optional[str] = None,
        markdown: Optional[str] = None,
        cover_media_id: Optional[str] = None,
        cover_image: Optional[str] = None,
        publish: bool = False,
        visibility: str = "Public",
        upload_images: bool = True,
    ) -> Dict[str, Any]:
        """Full flow: create draft -> set title -> set content -> (cover) -> (publish).

        ``cover_image`` uploads a local image file; ``cover_media_id`` uses an
        already-uploaded media id. With neither given, the body file's
        front-matter ``image:`` is auto-resolved (relative to the body file)
        and used as the cover. Returns a dict including the new ``article_id``.
        """
        metadata: Dict[str, Any] = {}
        if markdown is None and body_file is not None:
            with open(body_file, "r", encoding="utf-8") as f:
                markdown = f.read()
        if markdown is not None:
            markdown, metadata = parse_markdown_front_matter(markdown)
            title = title or _metadata_text(metadata, "title")
        if not title:
            raise ValueError("title is required. Provide --title or front matter title in --body-file.")

        if not cover_image and not cover_media_id and body_file:
            fm_img = _metadata_text(metadata, "image")
            if fm_img:
                cand = os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(body_file)), fm_img))
                if os.path.isfile(cand):
                    cover_image = cand

        shell = self.create_draft_shell()
        article_id = shell["article_id"]

        if title:
            self.set_title(article_id, title)
        if markdown is not None:
            self.set_content(article_id, markdown=markdown, body_file=body_file,
                             upload_images=upload_images)
        if cover_image:
            self.set_cover_image(article_id, cover_image)
        elif cover_media_id:
            self.set_cover_media(article_id, cover_media_id)

        result = {
            "success": True,
            "article_id": article_id,
            "status": "draft",
            "url": f"https://x.com/compose/articles/edit/{article_id}",
        }
        if publish:
            pub = self.publish_article(article_id, visibility=visibility)
            result["status"] = "published"
            result["lifecycle"] = pub.get("lifecycle")
        return result

    # ----------------------------------------------------------------
    # Listing
    # ----------------------------------------------------------------

    def list_articles(
        self,
        lifecycle: str = "Published",
        count: int = 20,
        cursor: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List the authenticated user's articles.

        ``lifecycle`` is ``"Published"`` or ``"Draft"``. Returns
        ``{"items": [...], "next_cursor": str|None}``.
        """
        uid = user_id or self._user_id
        if not uid:
            raise ValueError(
                "user_id required to list articles. Pass --user-id or provide a "
                "cookie containing the 'twid' cookie so it can be auto-resolved."
            )
        variables: Dict[str, Any] = {
            "userId": str(uid),
            "lifecycle": lifecycle,
            "count": count,
        }
        if cursor:
            variables["cursor"] = cursor
        body = self._get("ArticleEntitiesSlice", variables)
        slice_ = (
            body.get("data", {}).get("user", {}).get("result", {})
            .get("articles_article_mixer_slice", {})
        )
        items: List[Dict[str, Any]] = []
        for raw in slice_.get("items", []):
            result = raw.get("article_entity_results", {}).get("result", {})
            if not result:
                continue
            items.append({
                "article_id": self._article_id(result),
                "title": result.get("title", ""),
                "lifecycle": result.get("lifecycle_state", {}).get("lifecycle"),
                "modified_at_secs": result.get("lifecycle_state", {}).get("modified_at_secs"),
                "preview_text": result.get("preview_text", ""),
            })
        next_cursor = slice_.get("slice_info", {}).get("next_cursor")
        return {"success": True, "items": items, "next_cursor": next_cursor or None}


# ===================================================================
# CLI entry point
# ===================================================================

def _add_auth_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--auth-token", help="auth_token cookie value")
    p.add_argument("--ct0", help="ct0 cookie value (also used as x-csrf-token)")
    p.add_argument("--auth-token-path", help="File containing the auth_token value (default: .secrets/auth_token.txt)")
    p.add_argument("--ct0-path", help="File containing the ct0 value (default: .secrets/ct0.txt)")
    p.add_argument("--cookie", help="Raw Cookie header string containing auth_token/ct0/twid")
    p.add_argument("--cookie-path", help="File containing the raw Cookie header string")
    p.add_argument("--user-id", help="Numeric user id (for listing; auto-read from twid if omitted)")


def _default_twid_user_id() -> Optional[str]:
    """Read the numeric user id from .secrets/twid (holds `u=<id>` or a raw id)."""
    try:
        with open(os.path.join(os.path.dirname(_DEFAULT_AUTH_TOKEN_PATH), "twid")) as f:
            raw = f.read().strip()
    except OSError:
        return None
    from urllib.parse import unquote
    m = re.search(r"u(?:=|%3D)(\d+)", unquote(raw))
    return m.group(1) if m else (raw if raw.isdigit() else None)


def _client(args: argparse.Namespace) -> XCom:
    return XCom(
        auth_token=getattr(args, "auth_token", None),
        ct0=getattr(args, "ct0", None),
        cookie=getattr(args, "cookie", None),
        cookie_path=getattr(args, "cookie_path", None),
        auth_token_path=getattr(args, "auth_token_path", None),
        ct0_path=getattr(args, "ct0_path", None),
        user_id=getattr(args, "user_id", None),
    )


def _read_markdown(body_file: Optional[str]) -> Optional[str]:
    if not body_file:
        return None
    if not os.path.isfile(body_file):
        raise ValueError(f"body file not found: {body_file}")
    with open(body_file, "r", encoding="utf-8") as f:
        return f.read()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="X.com (Twitter) Articles API client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command")

    # --- draft (full create flow) ---
    d_p = sub.add_parser("draft", help="Create an article draft from markdown (create+title+content)")
    d_p.add_argument("--title", help="Article title")
    d_p.add_argument("--body-file", help="Path to markdown body file")
    d_p.add_argument("--cover-image", help="Local image file to upload and set as cover")
    d_p.add_argument("--cover-media-id", help="Already-uploaded media id to set as cover")
    d_p.add_argument("--no-inline-images", action="store_true",
                     help="Skip uploading/inserting inline ![](path) images")
    d_p.add_argument("--publish", action="store_true", help="Publish immediately after creating")
    d_p.add_argument("--visibility", default="Public", help="Publish visibility (default: Public)")
    _add_auth_args(d_p)

    # --- create (empty draft shell) ---
    c_p = sub.add_parser("create", help="Create an empty article draft, print its id")
    _add_auth_args(c_p)

    # --- set-title ---
    st_p = sub.add_parser("set-title", help="Set an article draft's title")
    st_p.add_argument("article_id", help="Numeric article entity id")
    st_p.add_argument("--title", required=True, help="New title")
    _add_auth_args(st_p)

    # --- set-content ---
    sc_p = sub.add_parser("set-content", help="Set an article draft's body from markdown")
    sc_p.add_argument("article_id", help="Numeric article entity id")
    sc_p.add_argument("--body-file", required=True, help="Path to markdown body file")
    sc_p.add_argument("--no-inline-images", action="store_true",
                      help="Skip uploading/inserting inline ![](path) images")
    _add_auth_args(sc_p)

    # --- set-cover ---
    scov_p = sub.add_parser("set-cover", help="Set an article's cover image")
    scov_p.add_argument("article_id", help="Numeric article entity id")
    scov_g = scov_p.add_mutually_exclusive_group(required=True)
    scov_g.add_argument("--image", help="Local image file to upload and set as cover")
    scov_g.add_argument("--media-id", help="Already-uploaded media id")
    scov_p.add_argument("--media-category", default="DraftTweetImage", help="Media category (for --media-id)")
    _add_auth_args(scov_p)

    # --- publish ---
    p_p = sub.add_parser("publish", help="Publish an article draft")
    p_p.add_argument("article_id", help="Numeric article entity id")
    p_p.add_argument("--visibility", default="Public", help="Visibility (default: Public)")
    _add_auth_args(p_p)

    # --- unpublish ---
    u_p = sub.add_parser("unpublish", help="Unpublish a published article")
    u_p.add_argument("article_id", help="Numeric article entity id")
    _add_auth_args(u_p)

    # --- delete ---
    del_p = sub.add_parser("delete", help="Permanently delete an article (draft or published)")
    del_p.add_argument("article_id", nargs="+", help="Numeric article entity id(s)")
    _add_auth_args(del_p)

    # --- list ---
    l_p = sub.add_parser("list", help="List drafts or published articles")
    l_p.add_argument("--status", choices=["draft", "published"], default="published",
                     help="Which articles to list (default: published)")
    l_p.add_argument("--count", type=int, default=20, help="Max results per page")
    l_p.add_argument("--cursor", help="Pagination cursor from a previous page")
    l_p.add_argument("--json", action="store_true", help="Output raw JSON")
    _add_auth_args(l_p)

    args = ap.parse_args()
    if getattr(args, "user_id", None) is None:
        args.user_id = _default_twid_user_id()
    if not args.command:
        ap.print_help()
        return 1

    try:
        client = _client(args)

        if args.command == "draft":
            result = client.create_article(
                title=args.title,
                body_file=getattr(args, "body_file", None),
                cover_image=getattr(args, "cover_image", None),
                cover_media_id=getattr(args, "cover_media_id", None),
                publish=getattr(args, "publish", False),
                visibility=getattr(args, "visibility", "Public"),
                upload_images=not getattr(args, "no_inline_images", False),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.command == "create":
            result = client.create_draft_shell()
            print(json.dumps({"success": True, "article_id": result["article_id"],
                              "url": f"https://x.com/compose/articles/edit/{result['article_id']}"},
                             indent=2, ensure_ascii=False))
            return 0

        if args.command == "set-title":
            result = client.set_title(args.article_id, args.title)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.command == "set-content":
            _read_markdown(args.body_file)  # validate path early
            result = client.set_content(args.article_id, body_file=args.body_file,
                                        upload_images=not args.no_inline_images)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.command == "set-cover":
            if getattr(args, "image", None):
                result = client.set_cover_image(args.article_id, args.image)
            else:
                result = client.set_cover_media(args.article_id, args.media_id,
                                                media_category=args.media_category)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.command == "publish":
            result = client.publish_article(args.article_id, visibility=args.visibility)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.command == "unpublish":
            result = client.unpublish_article(args.article_id)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.command == "delete":
            results = [client.delete_article(aid) for aid in args.article_id]
            out = results[0] if len(results) == 1 else results
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0

        if args.command == "list":
            lifecycle = "Draft" if args.status == "draft" else "Published"
            result = client.list_articles(lifecycle=lifecycle, count=args.count,
                                          cursor=getattr(args, "cursor", None))
            if getattr(args, "json", False):
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 0
            print(f"{args.status.capitalize()} articles: {len(result['items'])}\n")
            for idx, item in enumerate(result["items"], 1):
                print(f"{idx}. [{item['lifecycle']}] {item['article_id']}")
                print(f"   {item['title']}")
                preview = (item.get("preview_text") or "")[:100].replace("\n", " ")
                if preview:
                    print(f"   {preview}")
                print()
            if result["next_cursor"]:
                print(f"next_cursor: {result['next_cursor']}")
            return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
