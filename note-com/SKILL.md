---
name: note-com
description: API access for note.com to create and update markdown drafts, insert table-of-contents blocks, publish free or paid articles, list articles, manage magazines, search, like, and engage.
---

# note-com

Before using this skill, read `AGENTS.md` in this directory if it exists. That file is for local, untracked rules and notes that should not be committed with the skill.

Use `scripts/note_com.py` for note.com API work: draft creation, draft updates, publishing, paid cutoffs, table-of-contents insertion, search, magazine operations, likes, and engagement.

## Requirements

Use a Python environment with these packages installed:

- `requests`
- `markdown`

If the current `python3` cannot import `markdown`, use another environment or install the package before running the CLI. The script can also be imported from Python for workflows that are not exposed as CLI flags.

## Authentication

Most commands need a fresh `_note_session_v5` cookie value. The response is the raw cookie value. It is not a JWT and expires quickly.

Pass credentials explicitly:

```bash
python3 scripts/note_com.py fetch <note_key> --token-path .secrets/note_com_token.txt
python3 scripts/note_com.py fetch <note_key> --token "$NOTE_COM_TOKEN"
```

If `--token` and `--token-path` are omitted, `scripts/note_com.py` falls back to the `NOTE_COM_TOKEN` environment variable.

### Auth Error Shape

note.com often hides authentication or authorization failures as `404 not_found` instead of returning `401 Unauthorized`. For example, fetching a private draft with an invalid token can return:

```text
Draft fetch failed HTTP 404: {"error":{"message":"","type":"not_found"}}
```

When a known draft suddenly looks missing, first verify the token freshness and account permissions before assuming the note key is wrong.

## Quick Start

```bash
# Create a draft from markdown
python3 scripts/note_com.py draft --title "Title" --body-file article.md

# Fetch draft/public note state and body
python3 scripts/note_com.py fetch <note_key>

# Publish a free article
python3 scripts/note_com.py publish <note_key>

# Publish a paid article
python3 scripts/note_com.py publish <note_key> \
  --price 980 \
  --paid-separator-search "ここから先は有料部分です" \
  --hashtags '["#ビジネス","#AI","#データ","#IT","#IT業界","#データエンジニアリング"]'

# Update an existing draft
python3 scripts/note_com.py update <note_key> --title "New Title"
python3 scripts/note_com.py update <note_key> --body-file article.md

# Search and engagement
python3 scripts/note_com.py search "python" --size 20
python3 scripts/note_com.py engage --mag m6dc9256d1e54
python3 scripts/note_com.py engage --keyword "python"
```

## User Publishing Workflow

See [references/publishing-workflow.md](references/publishing-workflow.md) for the user's content series, cadence, pricing strategy, and growth tactics. The core content loop is:

1. Generate article draft.
2. Create or update a note.com draft.
3. Optionally insert a table of contents.
4. Publish as free or paid.
5. Engage with related articles after publishing.

## Draft Creation

```bash
python3 scripts/note_com.py draft --title "Title" --body-file article.md [options]
```

Options:

| Option | Description |
|---|---|
| `--cover <path>` | Upload a cover/eyecatch image for the new note |
| `--tags <json>` | JSON tag array, e.g. `'["AI","note-com"]'` |
| `--width <px>` | Cover width, default `1280` |
| `--height <px>` | Cover height, default `670` |
| `--token <value>` | Raw `_note_session_v5` cookie value |
| `--token-path <path>` | File containing the raw cookie value |

`draft` runs this internal flow:

1. `POST /api/v1/text_notes` creates a note shell and returns `{id, key}`.
2. `POST /api/v1/image_upload/note_eyecatch` uploads the cover image if provided.
3. `POST /api/v1/text_notes/draft_save?id={note_id}&is_temp_saved=true` saves the markdown body converted to HTML.

Python API:

```python
from note_com import NoteCom

client = NoteCom(token_path=".secrets/note_com_token.txt")
result = client.create_draft(
    title="My Draft",
    body_file="article.md",
    eyecatch_image="cover.jpg",
    hashtags=["AI", "note-com"],
)
```

## Updating Drafts

Use `update` to modify a draft without publishing:

```bash
python3 scripts/note_com.py update <note_key> --title "New Title"
python3 scripts/note_com.py update <note_key> --body-file article.md
python3 scripts/note_com.py update <note_key> --save-body /tmp/draft.html
```

Options:

| Option | Description |
|---|---|
| `--title <str>` | New note title |
| `--body-file <path>` | Markdown file, converted to HTML before saving |
| `--eyecatch-url <url>` | Existing cover image URL |
| `--eyecatch-id <id>` | Existing cover image ID |
| `--eyecatch-type <mime>` | Cover MIME type, default `image/jpeg` |
| `--eyecatch-width <px>` | Cover width |
| `--eyecatch-height <px>` | Cover height |
| `--hashtags <json>` | JSON tag array |
| `--save-body <path>` | Fetch latest draft body HTML and write it to a file |
| `--token <value>` | Raw cookie value |
| `--token-path <path>` | File containing the raw cookie value |

Implementation details:

- Endpoint: `POST /api/v1/text_notes/draft_save?id={note_id}&is_temp_saved=true`
- `name` is always included. If no title is provided, the existing draft title is reused.
- `index: false` and `is_lead_form: false` are included for draft saves.
- Python `update_draft(body=...)` expects HTML, not markdown. The CLI handles markdown conversion.

## Table Of Contents

TOC insertion is currently a Python API workflow, not a CLI flag. Insert the TOC while the article is still a draft, then publish the saved draft.

```python
from note_com import NoteCom

client = NoteCom(token_path=".secrets/note_com_token.txt")

result = client.update_draft(
    note_key="n244b7f74e361",
    insert_toc_search="この段落の直後に目次を入れる",
)
```

How TOC insertion works:

1. Fetches the current draft body.
2. Finds the first element whose `innerText` contains `insert_toc_search`, case-insensitive.
3. Inserts this custom element immediately after that element:

```html
<table-of-contents name="{uuid}" id="{uuid}"><br></table-of-contents>
```

4. Saves the updated draft via `draft_save`.

Important behavior verified against note.com:

- The editor view can show the TOC as soon as the custom `<table-of-contents>` element exists in the draft body.
- The public reader view also needs note.com's generated heading `index`.
- `publish_draft()` now preserves the saved body and automatically sends `index: true` when publishing a body that contains `<table-of-contents`, unless an explicit `index` argument is provided.
- For paid articles, place the TOC before the paid cutoff if it must be visible in the free area.

Recommended TOC workflow:

```python
client.update_draft(note_key, insert_toc_search="intro paragraph text")
draft = client.fetch_draft(note_key)
assert "<table-of-contents" in draft["body"]
client.publish_draft(note_key, publish=True)
```

For paid articles:

```python
client.update_draft(note_key, insert_toc_search="intro paragraph text")
client.publish_draft(
    note_key,
    publish=True,
    paid_separator_search="ここから先は有料部分です",
    price=980,
    hashtags=["#ビジネス", "#AI", "#データ", "#IT", "#IT業界", "#データエンジニアリング"],
)
```

## Publishing

```bash
python3 scripts/note_com.py publish <note_key> [options]
```

Options:

| Option | Description |
|---|---|
| `--hashtags <json>` | JSON tag array, e.g. `'["#AI","#note"]'` |
| `--price <yen>` | Paid-content price in yen |
| `--paid-separator-uuid <uuid>` | UUID of the free-to-paid boundary element |
| `--paid-separator-search <text>` | Search text used to locate the paid separator element by `innerText` |
| `--eyecatch-url <url>` | Cover image URL |
| `--eyecatch-id <id>` | Cover image ID |
| `--index` | Explicitly request heading index generation |
| `--token <value>` | Raw cookie value |
| `--token-path <path>` | File containing the raw cookie value |

`publish` always publishes. There is no extra `--publish` flag.

### Free Publish

```bash
python3 scripts/note_com.py publish <note_key> \
  --hashtags '["#AI","#データ"]'
```

Internally, `publish_draft()`:

- Fetches the current draft via `GET /api/v3/notes/{key}?draft=true`.
- Sends `PUT /api/v1/text_notes/{note_id}`.
- Preserves the fetched `body`.
- For free articles, sends `free_body` as the full body, `pay_body` as `""`, and `separator` as `null`.
- Sets `status: "published"` and `send_notifications_flag: true`.
- Sends `index: true` automatically when the body contains a TOC element.

### Paid Publish

Use `--paid-separator-search` whenever possible because UUIDs can change after body updates:

```bash
python3 scripts/note_com.py publish <note_key> \
  --price 980 \
  --paid-separator-search "ここから先は有料部分です" \
  --hashtags '["#ビジネス","#AI","#データ","#IT","#IT業界","#データエンジニアリング"]'
```

Paid publish behavior:

- Finds the separator element in the fetched draft body.
- Uses that element's UUID as the server-side `separator`.
- Splits the body into `free_body` and `pay_body`.
- Keeps the separator element as the last element of `free_body`.
- Sends `price`, `is_paid`, `status: "published"`, and `send_notifications_flag: true`.
- Preserves the fetched full `body`.
- If a TOC exists before the separator, the public free area can render the TOC.

Python API:

```python
result = client.publish_draft(
    note_key="na89d642394f3",
    publish=True,
    price=980,
    paid_separator_search="ここから先は有料部分です",
    hashtags=["#ビジネス", "#AI", "#データ", "#IT", "#IT業界", "#データエンジニアリング"],
)
```

## Fetching

```bash
python3 scripts/note_com.py fetch <note_key>
```

`fetch_draft()` uses `GET /api/v3/notes/{note_key}?draft=true` and returns note metadata plus body HTML. The CLI prints selected fields and the full body.

Python API:

```python
draft = client.fetch_draft("n23052bf8a287")
print(draft["status"], draft["body"], draft.get("index"))
```

## UUIDs And Paid Separators

See [references/uuid-pattern.md](references/uuid-pattern.md).

note.com editor-created block elements usually have matching `name` and `id` UUIDs:

```html
<p name="d557826d-64ba-479a-a4bc-54a6f027ea5d" id="d557826d-64ba-479a-a4bc-54a6f027ea5d">...</p>
```

Use the companion helper to inspect draft HTML:

```bash
python3 scripts/note_com.py update <note_key> --save-body /tmp/draft.html
python3 scripts/extract_uuids.py /tmp/draft.html
python3 scripts/extract_uuids.py /tmp/draft.html --element hr --json
```

Prefer `--paid-separator-search` over `--paid-separator-uuid` because updating a body can regenerate UUIDs.

## Search

```bash
python3 scripts/note_com.py search "python" --size 20
```

Endpoint: `GET /api/v3/searches?context=note&q=<keyword>&size=<n>&start=<n>`

Python API:

```python
results = client.search_notes("python", size=20, start=0)
```

## Magazine Operations

List magazine articles:

```bash
python3 scripts/note_com.py list-magazine m6dc9256d1e54
```

Add an article to a magazine:

```bash
python3 scripts/note_com.py add-to-magazine m6dc9256d1e54 <note_key>
python3 scripts/note_com.py add-to-magazine m6dc9256d1e54 <note_key> --note-id <id>
```

Python API:

```python
info = client.get_magazine_info("m6dc9256d1e54")
articles = client.list_magazine_articles("m6dc9256d1e54", page=1)
client.add_to_magazine("m6dc9256d1e54", "nafa54f191655")
```

## Likes And Engagement

Like articles:

```bash
python3 scripts/note_com.py like <id1> [id2 ...]
python3 scripts/note_com.py like --file /tmp/note_keys.txt
python3 scripts/note_com.py like --list '["id1","id2"]'
```

Engage by keyword:

```bash
python3 scripts/note_com.py engage --keyword "python" --limit 20
```

Engage from a magazine article's related notes:

```bash
python3 scripts/note_com.py engage --mag m6dc9256d1e54
python3 scripts/note_com.py engage --magazine-id m6dc9256d1e54 --nth 2
```

Notes:

- `like_article()` calls `POST /api/v3/notes/{id}/likes`.
- `engage` keyword mode searches note.com directly.
- `engage --mag` sorts magazine articles newest-first, picks the `--nth` source article (`0` means latest), fetches related articles through `GET /api/v3/mkit_layouts/json` with `context=related_notes_revelio`, then likes them.
- Like calls include a delay to avoid rate limits.

## Pitfalls

- `--hashtags` must be valid JSON. Use `'["#AI"]'`, not `'[\"#AI\"]'`.
- `update_draft(body=...)` expects HTML in Python. CLI `update --body-file` converts markdown for you.
- Cover images are generally safest when uploaded for the target note shell. Reusing old `eyecatch_url`/`eyecatch_id` can be rejected by note.com.
- If you re-upload or regenerate the body, old UUIDs may become stale. Re-resolve paid separators with `--paid-separator-search`.
- note.com often returns `404 not_found` for invalid or unauthorized tokens, especially when fetching drafts. Treat unexpected 404s as possible auth failures.
- For reader-facing TOC rendering, publishing must generate the heading `index`. The current `publish_draft()` does this automatically when a TOC element is present.
- For paid articles, keep the TOC before the paid separator if the TOC should be visible without purchase.
