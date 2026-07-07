---
name: substack
description: API access for substack.com using a captured substack.sid cookie; post short notes, save article drafts, and publish existing drafts with optional subscriber email.
---

# substack

Use `scripts/substack.py` for Substack API work. Verified flows use the browser session cookie `substack.sid` to post notes, save existing article drafts, run prepublish checks, and publish drafts.

## Requirements

Use a Python environment with `requests` installed.

Verify dependencies before running the CLI:

```bash
python3 -c "import requests; print('substack deps ok')"
```

## Authentication

The script authenticates with the raw `substack.sid` cookie value, not a Bearer token.

Token resolution order:

1. `--token <value>`
2. `--token-path <path>`
3. `SUBSTACK_SID` environment variable
4. `.secrets/substack-token.txt`

In this local skill directory, first look for the cookie at:

```text
.secrets/substack_sid.txt
```

Older helper workflows may also have a token at:

```text
/Users/daigotanaka/projects/openclaw_skills/harprobe/.secrets/substack-token.txt
```

Keep these `.secrets/` files out of git. The file should contain only the raw cookie value, without `substack.sid=` and without surrounding quotes.

### Getting `substack.sid`

The user needs to get `substack.sid` by logging into `substack.com` in a browser. In Chrome:

1. Log into `https://substack.com`.
2. Right-click the page and choose Inspect.
3. Open Application -> Storage -> Cookies -> `https://substack.com`.
4. Select the `substack.sid` cookie.
5. Make sure the value is URL-decoded.
6. Double-click the cryptic string to highlight it, then right-click and copy.

Save only the raw copied value in `.secrets/substack_sid.txt`.

## Quick Start

```bash
# Post a note
python3 scripts/substack.py --token-path .secrets/substack_sid.txt \
  post-note "Hi, I'm just looking around."

# Use an explicit cookie path
python3 scripts/substack.py --token-path .secrets/substack_sid.txt \
  post-note "Hello from the API"

# Save an existing article draft
python3 scripts/substack.py create-draft \
  --publication-url https://daigotanaka.substack.com \
  --publication-id 1324369 \
  --byline-user-id 31220959 \
  --title "API Draft Test" \
  --subtitle "A small unpublished test draft from the API" \
  --body-file article.md

# Fetch an existing article draft
python3 scripts/substack.py fetch-draft 204727458 \
  --publication-url https://daigotanaka.substack.com

# List published, draft, and scheduled articles
python3 scripts/substack.py list-articles \
  --publication-url https://daigotanaka.substack.com

# List only drafts and scheduled articles
python3 scripts/substack.py list-articles \
  --publication-url https://daigotanaka.substack.com \
  --state draft,scheduled

# Upload and append an image to an existing draft
python3 scripts/substack.py append-image 204728902 .secrets/qwen-image-analysis.png \
  --publication-url https://daigotanaka.substack.com \
  --byline-user-id 31220959

# Save an existing article draft
python3 scripts/substack.py save-draft 204727458 \
  --publication-url https://daigotanaka.substack.com \
  --title "Test post" \
  --subtitle "Subtitle of a test post" \
  --body-file article.md

# Save an existing article draft with explicit blocks
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
  --send-email

# Publish and email subscribers
python3 scripts/substack.py prepublish 204727458 \
  --publication-url https://daigotanaka.substack.com
python3 scripts/substack.py publish-draft 204727458 \
  --publication-url https://daigotanaka.substack.com \
  --send-email

# Publish without emailing subscribers
python3 scripts/substack.py publish-draft 204727458 \
  --publication-url https://daigotanaka.substack.com

# Schedule a draft
python3 scripts/substack.py schedule-draft 205125195 \
  --publication-url https://daigotanaka.substack.com \
  --trigger-at 2026-07-06T13:30:00.000Z

# Schedule a draft and email subscribers at the scheduled time
python3 scripts/substack.py schedule-draft 205125195 \
  --publication-url https://daigotanaka.substack.com \
  --trigger-at 2026-07-06T13:30:00.000Z \
  --send-email \
  --byline-user-id 31220959

# Unschedule a draft
python3 scripts/substack.py unschedule-draft 205125195 \
  --publication-url https://daigotanaka.substack.com
```

## Posting Notes

Endpoint verified from HAR capture:

```text
POST https://substack.com/api/v1/comment/feed
Cookie: substack.sid=<raw cookie value>
Content-Type: application/json
Origin: https://substack.com
Referer: https://substack.com/
```

The note body uses Substack's editor JSON shape. For visible paragraph breaks, send separate paragraph nodes rather than one text node containing raw newlines. The helper splits note text on blank lines and creates one paragraph node per paragraph:

```json
{
  "bodyJson": {
    "type": "doc",
    "attrs": {
      "schemaVersion": "v1",
      "title": null
    },
    "content": [
      {
        "type": "paragraph",
        "content": [
          {
            "type": "text",
            "text": "note text"
          }
        ]
      }
    ]
  },
  "replyMinimumRole": "everyone"
}
```

Successful responses include fields such as `id`, `status`, `date`, `type`, and `reply_minimum_role`.

Example multiline note:

```bash
python3 scripts/substack.py --token-path .secrets/substack_sid.txt \
  post-note "$(cat /tmp/note.txt)"
```

## Article Drafts And Publishing

Create an unpublished article draft:

```text
POST https://<publication-subdomain>.substack.com/api/v1/drafts
Cookie: substack.sid=<raw cookie value>
Content-Type: application/json
Origin: https://<publication-subdomain>.substack.com
Referer: https://<publication-subdomain>.substack.com/publish/post/new
```

Verified creation payload:

```json
{
  "type": "newsletter",
  "publication_id": 1324369,
  "draft_bylines": [
    {
      "id": 31220959,
      "is_guest": false
    }
  ]
}
```

`draft_bylines` is required; omitting it returns `400` with `param: "draft_bylines"`.

Verified draft save endpoint:

```text
PUT https://<publication-subdomain>.substack.com/api/v1/drafts/<draft_id>
Cookie: substack.sid=<raw cookie value>
Content-Type: application/json
Origin: https://<publication-subdomain>.substack.com
Referer: https://<publication-subdomain>.substack.com/publish/post/<draft_id>
```

Important draft payload fields:

```json
{
  "draft_title": "Title",
  "draft_subtitle": "Subtitle",
  "draft_podcast_url": null,
  "draft_podcast_duration": null,
  "draft_body": "{\"type\":\"doc\",\"content\":[]}",
  "section_chosen": false,
  "draft_section_id": null,
  "detect_language": true,
  "translations": [],
  "draft_bylines": [
    {
      "id": 31220959,
      "is_guest": false
    }
  ],
  "last_updated_at": "2026-07-02T18:56:07.985Z",
  "audience": "everyone",
  "audience_before_archived": null,
  "syndicate_voiceover_to_rss": false,
  "syndicate_to_section_id": null,
  "should_syndicate_to_other_feed": null,
  "write_comment_permissions": "everyone",
  "default_comment_sort": null,
  "should_send_email": true,
  "meter_type": "none",
  "cover_image": null,
  "search_engine_title": null,
  "search_engine_description": null
}
```

Substack uses an optimistic-lock timestamp on draft saves. Before saving an existing draft, fetch it and send its current `draft_updated_at` as `last_updated_at`. If the value is stale, the API can return:

```json
{
  "error": "Post is out of date",
  "type": "single"
}
```

Fetch current draft state:

```text
GET https://<publication-subdomain>.substack.com/api/v1/drafts/<draft_id>
```

Fetch it with the CLI:

```bash
python3 scripts/substack.py fetch-draft 204727458 \
  --publication-url https://daigotanaka.substack.com

python3 scripts/substack.py fetch-draft 204727458 \
  --publication-url https://daigotanaka.substack.com \
  --full
```

`draft_body` is a JSON-encoded Substack editor document. Common nodes:

List article summaries by state:

```bash
python3 scripts/substack.py list-articles \
  --publication-url https://daigotanaka.substack.com

python3 scripts/substack.py list-articles \
  --publication-url https://daigotanaka.substack.com \
  --state published

python3 scripts/substack.py list-articles \
  --publication-url https://daigotanaka.substack.com \
  --state draft,scheduled \
  --limit 10
```

`--state` accepts `published`, `draft`, `scheduled`, or `all`, as a comma-separated list. The default is all states. Pass `--full` to print the raw JSON grouped by state.

Verified list endpoints:

```text
GET /api/v1/post_management/published?offset=0&limit=25&order_by=post_date&order_direction=desc
GET /api/v1/drafts?offset=0&limit=25
GET /api/v1/post_management/scheduled?offset=0&limit=25&order_by=trigger_at&order_direction=asc
```

### How To Update An Already Scheduled Draft

Updating draft text does not require changing the schedule. Use the draft save endpoint only; do not call `schedule-draft` or `unschedule-draft` unless the publish time itself should change.

First, fetch the current draft and confirm its schedule:

```bash
python3 scripts/substack.py fetch-draft 205126743 \
  --publication-url https://daigotanaka.substack.com
```

Look for `postSchedules` in the full JSON if you need the schedule id and trigger time:

```bash
python3 scripts/substack.py fetch-draft 205126743 \
  --publication-url https://daigotanaka.substack.com \
  --full
```

If the existing draft body does not contain a Substack-only top image/header block that must be preserved, the CLI path is:

```bash
python3 scripts/substack.py save-draft 205126743 \
  --publication-url https://daigotanaka.substack.com \
  --title "If I Already Have a FastAPI Server, Why Do I Need an MCP Server?" \
  --body-file /path/to/manuscript.md
```

This performs `PUT /api/v1/drafts/<draft_id>`. It does not call the schedule endpoints, so an existing schedule should remain attached. Fetch the draft afterward and verify `postSchedules` is still present.

If the current Substack draft has a top `captionedImage` node, such as a header image added in the editor, the plain `save-draft --body-file` command will replace the whole body and can remove that image. In that case, use the module helper directly:

```python
import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import substack

publication_url = "https://daigotanaka.substack.com"
draft_id = "205126743"
manuscript = Path("/path/to/manuscript.md")

client = substack.Substack(token_path=".secrets/substack-token.txt")
current = client.fetch_draft(publication_url, draft_id)

old_body = json.loads(current.get("draft_body") or '{"type":"doc","content":[]}')
old_content = old_body.get("content") or []
header = []

first = old_content[0] if old_content else {}
first_attrs = {}
if first.get("type") == "captionedImage":
    first_attrs = ((first.get("content") or [{}])[0].get("attrs") or {})
if first.get("type") == "captionedImage" and first_attrs.get("topImage"):
    header = [first]

new_doc = substack.markdown_to_doc(manuscript)
new_content = new_doc.get("content") or []

title = next(
    line[2:].strip()
    for line in manuscript.read_text(encoding="utf-8").splitlines()
    if line.startswith("# ")
)

if new_content and new_content[0].get("type") == "heading":
    attrs = new_content[0].get("attrs") or {}
    heading_text = "".join(
        part.get("text", "")
        for part in new_content[0].get("content", [])
        if isinstance(part, dict)
    ).strip()
    if attrs.get("level") == 1 and heading_text == title:
        new_content = new_content[1:]

byline_user_id = None
for byline in current.get("postBylines") or []:
    if byline.get("user_id"):
        byline_user_id = byline["user_id"]
        break

saved = client.save_draft(
    publication_url=publication_url,
    draft_id=draft_id,
    title=title,
    subtitle=current.get("draft_subtitle") or "",
    body={"type": "doc", "content": header + new_content},
    byline_user_id=byline_user_id,
    should_send_email=bool(current.get("should_send_email")),
    last_updated_at=current.get("draft_updated_at") or current.get("updated_at"),
)
```

After either path, fetch the draft again and verify:

- `postSchedules` still has the expected `trigger_at`
- the first body node is still the expected `captionedImage` if preserving a header image
- the new manuscript text appears in `draft_body`

```json
{
  "type": "doc",
  "content": [
    {
      "type": "paragraph",
      "attrs": {
        "textAlign": null
      },
      "content": [
        {
          "type": "text",
          "text": "Body text"
        }
      ]
    },
    {
      "type": "heading",
      "attrs": {
        "textAlign": null,
        "level": 1
      },
      "content": [
        {
          "type": "text",
          "text": "Section headline"
        }
      ]
    }
  ]
}
```

### Markdown Input

Use `--body-file <path>` with `create-draft` or `save-draft` to convert a markdown file into Substack editor JSON:

```bash
python3 scripts/substack.py create-draft \
  --publication-url https://daigotanaka.substack.com \
  --publication-id 1324369 \
  --byline-user-id 31220959 \
  --title "Article title" \
  --body-file article.md
```

Supported markdown structures:

- headings: `#`, `##`, `###` and deeper, capped to Substack heading level `3`
- paragraphs
- blockquotes
- bullet lists
- fenced code blocks

Markdown links are flattened to readable text as `label (url)` because rich link marks are not yet verified.

Verified list node names from fetched Substack draft JSON are `bullet_list` and `list_item`.

Verified plain code block:

```json
{
  "type": "code_block",
  "attrs": {
    "language": null
  },
  "content": [
    {
      "type": "text",
      "text": "plain code or shell-like text"
    }
  ]
}
```

Verified Python highlighted code block:

```json
{
  "type": "highlighted_code_block",
  "attrs": {
    "language": "python",
    "nodeId": "1cb6c4f6-323a-4688-bde4-562f1aa7d261"
  },
  "content": [
    {
      "type": "text",
      "text": "from fastapi import FastAPI\n\napp = FastAPI()"
    }
  ]
}
```

`nodeId` is a UUID per highlighted code block. Use `highlighted_code_block` when a markdown fence declares `python`; otherwise use `code_block` with `language: null`.

Example markdown:

````markdown
```python
def greet(name):
    print(f"Hello, {name}!")
```
````

Substack JSON:

```json
{
  "type": "highlighted_code_block",
  "attrs": {
    "language": "python",
    "nodeId": "<uuid>"
  },
  "content": [
    {
      "type": "text",
      "text": "def greet(name):\n    print(f\"Hello, {name}!\")"
    }
  ]
}
```

### Images In Article Bodies

The captured flow uploaded an image with:

```text
POST https://<publication-subdomain>.substack.com/api/v1/image
Content-Type: application/json
```

Verified upload payload:

```json
{
  "image": "data:image/png;base64,<base64 image bytes>"
}
```

Successful upload response:

```json
{
  "id": 291719856,
  "url": "https://substack-post-media.s3.amazonaws.com/public/images/example_1179x2201.png",
  "contentType": "image/png",
  "bytes": 1443056,
  "imageWidth": 1179,
  "imageHeight": 2201
}
```

After upload, images are embedded in `draft_body` as `captionedImage` nodes containing `image2` attrs:

```json
{
  "type": "captionedImage",
  "content": [
    {
      "type": "image2",
      "attrs": {
        "src": "https://substack-post-media.s3.amazonaws.com/public/images/example.png",
        "srcNoWatermark": null,
        "fullscreen": null,
        "imageSize": null,
        "height": 540,
        "width": 960,
        "resizeWidth": null,
        "bytes": 172512,
        "alt": null,
        "title": null,
        "type": "image/png",
        "href": null,
        "belowTheFold": false,
        "topImage": false,
        "internalRedirect": "https://<publication-subdomain>.substack.com/i/<draft_id>?img=<urlencoded-src>",
        "isProcessing": false,
        "align": null,
        "offset": false
      }
    }
  ]
}
```

Use `append-image` to upload an image file, fetch the current draft timestamp, append a `captionedImage` node, and save the draft without publishing.

### Publishing

Run Substack's prepublish check before publishing:

```text
GET https://<publication-subdomain>.substack.com/api/v1/drafts/<draft_id>/prepublish
```

Publish endpoint:

```text
POST https://<publication-subdomain>.substack.com/api/v1/drafts/<draft_id>/publish
Content-Type: application/json
```

Request body:

```json
{
  "send": true
}
```

Set `send` to `true` to email subscribers. Set it to `false` to publish without sending email. The final draft save also used `should_send_email: true` when the email option was selected. The CLI defaults to no email; pass `publish-draft --send-email` to send to subscribers.

### Scheduling

Run Substack's prepublish check with the future publish timestamp before scheduling:

```text
GET https://<publication-subdomain>.substack.com/api/v1/drafts/<draft_id>/prepublish?publish_date=2026-07-06T13%3A30%3A00.000Z
```

Schedule endpoint:

```text
POST https://<publication-subdomain>.substack.com/api/v1/drafts/<draft_id>/scheduled_release
Content-Type: application/json
```

Verified schedule payload:

```json
{
  "trigger_at": "2026-07-06T13:30:00.000Z",
  "post_audience": "everyone",
  "email_audience": "everyone"
}
```

When the browser workflow schedules a post with "deliver email to followers" enabled, it also saves the draft with `should_send_email: true` before scheduling. Use `schedule-draft --send-email` to perform that save step, then schedule with `email_audience: "everyone"`.

Use UTC ISO timestamps with milliseconds and `Z`, matching Substack's captured format.

Unschedule endpoint:

```text
DELETE https://<publication-subdomain>.substack.com/api/v1/drafts/<draft_id>/scheduled_release
```

The captured unschedule response was a JSON list containing the removed schedule id, e.g. `[11349791]`.

## Safety

- Treat `substack.sid` as a live browser session secret.
- Do not print or log the cookie value.
- If posting fails with authentication-like errors, refresh the cookie from a logged-in browser session before changing request logic.
