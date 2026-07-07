---
name: x-com
description: API access for X.com (Twitter) long-form Articles — create markdown drafts, set title/content/cover, publish, unpublish, and list draft or published articles.
---

# x-com

Before using this skill, read `AGENTS.md` in this directory if it exists. That file is for local, untracked rules and notes (token sources, personal account details) that should not be committed with the skill.

Use `scripts/x_com.py` for X.com Articles work: draft creation, title/content/cover updates, publishing, unpublishing, and listing drafts or published articles. The client is reverse-engineered from the X web composer's GraphQL traffic.

## Requirements

Use a Python environment with `requests` installed:

```bash
python3 -c "import requests; print('x-com deps ok')"
```

The script can also be imported from Python for workflows not exposed as CLI flags.

## Authentication

There is **no API key and no developer account**. This skill drives the same
private web endpoints the browser uses, so it authenticates with **browser
session cookies** copied from a logged-in x.com session.

### Which cookies are needed

| Cookie | Required for | Purpose |
|---|---|---|
| `auth_token` | **every command** | Login session (grants full account access — treat like a password) |
| `ct0` | **every command** | CSRF token, also sent as the `x-csrf-token` header |
| `twid` | **only `list`** | Holds your numeric user id (`u=<id>`); not needed for create/publish/unpublish |

So **create, publish, and unpublish need only `auth_token` + `ct0`.** The `list`
command additionally needs your user id, which comes from `twid` (or `--user-id`).

The public web bearer token (shipped in x.com's JS bundle — not a secret) is
sent automatically; the user does not provide it.

### How to obtain the cookies (guide the user through this)

If the user has not provided credentials, walk them through these steps:

1. Log in to **https://x.com** in a desktop browser.
2. Open **DevTools** (F12 or right-click → Inspect).
3. Go to **Application** (Chrome/Edge) or **Storage** (Firefox) → **Cookies** → `https://x.com`.
4. Copy the **Value** of each cookie: `auth_token`, `ct0`, and — only if they want
   to use `list` — `twid`.
5. Save them (see below). Remind the user these expire and are as sensitive as a
   password, so they must stay in `.secrets/` (gitignored) and never be committed.

### How to provide the cookies

The client checks these sources in priority order; the last one means an agent
can drop two files and run with no flags:

```bash
# 1. Discrete files (what this repo uses by default)
#    .secrets/auth_token.txt  -> the auth_token value
#    .secrets/ct0.txt         -> the ct0 value
python3 scripts/x_com.py publish <id>            # no flags: reads .secrets/*.txt
python3 scripts/x_com.py set-content <id> --auth-token-path a.txt --ct0-path c.txt

# 2. Discrete values on the command line
python3 scripts/x_com.py publish <id> --auth-token "..." --ct0 "..."

# 3. A raw browser Cookie header string (include twid for list)
python3 scripts/x_com.py list --cookie "auth_token=...; ct0=...; twid=u%3D29117025"

# 4. A file containing that Cookie header string
python3 scripts/x_com.py list --cookie-path .secrets/x_com_cookie.txt

# 5. Environment variables
X_AUTH_TOKEN=... X_CT0=... python3 scripts/x_com.py publish <id>
X_COM_COOKIE="auth_token=...; ct0=...; twid=u%3D29117025" python3 scripts/x_com.py list
```

**Default files:** with no auth flags, the client reads `.secrets/auth_token.txt`
and `.secrets/ct0.txt` (relative to the current directory). Dropping those two
files is enough for create/publish/unpublish.

Cookies expire — refresh them from the browser if calls start returning HTTP
401/403.

### User id for listing

`list` needs the numeric user id. It is auto-parsed from a `twid` cookie (`u=<id>`)
when a cookie string/file is provided; otherwise pass `--user-id 29117025`. The
discrete `.secrets/auth_token.txt` + `.secrets/ct0.txt` files do **not** carry a
user id, so `list` with only those two files requires `--user-id`. It is not
needed for create/publish/unpublish.

## Quick Start

```bash
# Create a draft from a markdown file (create shell + title + content)
python3 scripts/x_com.py draft --title "My Article" --body-file article.md

# Create and publish in one step
python3 scripts/x_com.py draft --title "My Article" --body-file article.md --publish

# List drafts / published articles
python3 scripts/x_com.py list --status draft
python3 scripts/x_com.py list --status published

# Publish / unpublish an existing article by id
python3 scripts/x_com.py publish 2074335832941719552
python3 scripts/x_com.py unpublish 2074335832941719552
```

## Publishing Workflow

The core content loop mirrors the web composer:

1. Create an article draft shell (`ArticleEntityDraftCreate`).
2. Set the title (`ArticleEntityUpdateTitle`).
3. Set the body content (`ArticleEntityUpdateContent`), converted from markdown to a Draft.js `content_state`.
4. Optionally set a cover image (`ArticleEntityUpdateCoverMedia`).
5. Publish (`ArticleEntityPublish`).

The `draft` command runs steps 1–4 (and step 5 with `--publish`). The intermediate steps are also exposed as their own subcommands for incremental editing.

## Draft Creation

```bash
python3 scripts/x_com.py draft --title "Title" --body-file article.md [options]
```

| Option | Description |
|---|---|
| `--title <str>` | Article title (required) |
| `--body-file <path>` | Markdown file for the body |
| `--cover-image <path>` | Local image file to upload and set as the cover |
| `--cover-media-id <id>` | Already-uploaded media id to set as the cover |
| `--publish` | Publish immediately after creating |
| `--visibility <str>` | Publish visibility, default `Public` |

Prints the new numeric `article_id` and the editor URL `https://x.com/compose/articles/edit/<article_id>`.

Python API:

```python
from x_com import XCom

client = XCom(cookie_path=".secrets/x_com_cookie.txt")
result = client.create_article(title="Hello", body_file="article.md")
print(result["article_id"])
client.publish_article(result["article_id"])
```

## Incremental Editing

Build or edit a draft step by step:

```bash
# Empty draft shell -> prints article_id
python3 scripts/x_com.py create

python3 scripts/x_com.py set-title 2074335832941719552 --title "New Title"
python3 scripts/x_com.py set-content 2074335832941719552 --body-file article.md

# Cover: upload a local image, or reuse an already-uploaded media id
python3 scripts/x_com.py set-cover 2074335832941719552 --image cover.jpg
python3 scripts/x_com.py set-cover 2074335832941719552 --media-id 2074335921839988736
```

### Cover images

`set-cover --image <path>` (and `draft --cover-image <path>`) uploads the file
through X's chunked media endpoint (`upload.x.com/i/media/upload.json`,
INIT → APPEND → FINALIZE) and then calls `ArticleEntityUpdateCoverMedia`. The
whole file is sent as one APPEND segment, which is fine for cover-sized images.
Use `--media-id` instead to reuse a media id you already uploaded.

Python API:

```python
shell = client.create_draft_shell()
client.set_title(shell["article_id"], "New Title")
client.set_content(shell["article_id"], body_file="article.md")
```

## Markdown → content_state

`scripts/x_com.py` converts markdown to X's Draft.js `content_state` block model. The mappings below reflect what X's schema actually accepts — X supports **only two heading levels** and has **no code-block or inline-code type**.

| Markdown | Block type |
|---|---|
| `# text` | `header-one` |
| `## text` (and `###`+ deeper) | `header-two` |
| `> text` | `blockquote` |
| `- text` / `* text` | `unordered-list-item` |
| `1. text` | `ordered-list-item` |
| ` ``` ` fenced block | `atomic` block + `MARKDOWN` entity (holds the whole fenced snippet verbatim) |
| `---` / `***` / `___` | `atomic` block + `DIVIDER` entity |
| anything else | `unstyled` paragraph |

Inline `**bold**` and `*italic*` become Draft.js inline style ranges — X accepts only `Bold` and `Italic`. Backticks and `_` are left literal (X has no inline-code style, and this avoids mangling URLs and snake_case). Blank lines separate blocks. Use `\` to escape a literal marker.

**Schema constraints (learned the hard way — X returns an opaque `GRAPHQL_VALIDATION_FAILED` or `OperationalError: Internal: Unspecified` when violated):**

- Only `header-one` and `header-two` exist; `header-three`/`header-four` are rejected.
- There is no `code-block` type; fenced code must be a `MARKDOWN` atomic entity.
- Only `Bold`/`Italic` inline styles; `Code` (and others) are rejected.
- Block `text` must be single-line; embed no `\n` (each line is its own block).

To supply a raw `content_state` directly, call `client.set_content(article_id, content_state=...)` from Python.

## Publishing

```bash
python3 scripts/x_com.py publish <article_id> [--visibility Public]
```

Uses `ArticleEntityPublish` with `visibilitySetting` (default `Public`). Returns the resulting `lifecycle` (`Published`).

## Unpublishing

```bash
python3 scripts/x_com.py unpublish <article_id>
```

Uses `ArticleEntityUnpublish`. The article returns to `Draft` state; the underlying draft content is preserved so it can be re-published.

## Deleting

```bash
python3 scripts/x_com.py delete <article_id> [<article_id> ...]
```

Uses `ArticleEntityDelete`. Works on both drafts and published articles and accepts multiple ids. **Permanent and irreversible** — the article is gone, not just unpublished. (Deleting a *published* article's associated post is a separate `DeleteTweet` operation, not handled here.)

Python API:

```python
client.delete_article("2074349652854603776")
```

## Listing

```bash
python3 scripts/x_com.py list --status published
python3 scripts/x_com.py list --status draft --count 20
python3 scripts/x_com.py list --status published --cursor <next_cursor>
python3 scripts/x_com.py list --status published --json
```

Uses `ArticleEntitiesSlice` with `lifecycle` = `Published` or `Draft`. Each item includes `article_id`, `title`, `lifecycle`, `modified_at_secs`, and `preview_text`. Paginate with the printed `next_cursor` value via `--cursor`.

Python API:

```python
page = client.list_articles(lifecycle="Draft", count=20)
for item in page["items"]:
    print(item["article_id"], item["title"])
next_cursor = page["next_cursor"]
```

## Smoke Test

Because the captured HAR is sanitized (cookies and bearer stripped), the client
has only been verified offline. Run this once with live cookies to confirm the
query IDs and auth still work end to end. It creates a throwaway draft, edits it,
publishes, unpublishes, and lists — exercising every operation.

Save a fresh browser Cookie header string (including `auth_token`, `ct0`, and
`twid`) to `.secrets/x_com_cookie.txt` first, then:

```bash
COOKIE=.secrets/x_com_cookie.txt

# 1. Create + edit a draft from markdown
printf '# Smoke Test\n\nHello from the x-com skill.\n' > /tmp/x_smoke.md
python3 scripts/x_com.py draft --title "Smoke Test" --body-file /tmp/x_smoke.md \
  --cookie-path "$COOKIE"
# -> note the printed article_id

# 2. It should appear under drafts
python3 scripts/x_com.py list --status draft --cookie-path "$COOKIE"

# 3. Publish it, confirm it moves to published
python3 scripts/x_com.py publish <article_id> --cookie-path "$COOKIE"
python3 scripts/x_com.py list --status published --cookie-path "$COOKIE"

# 4. Unpublish it back to draft (cleanup)
python3 scripts/x_com.py unpublish <article_id> --cookie-path "$COOKIE"
```

What to check:

- Every step prints `"success": true` (or a populated list) rather than an
  `Error:` line.
- An HTTP 404 / persisted-query error on any step means the query IDs rotated —
  recapture a HAR and update `_Q` in `scripts/x_com.py`.
- An HTTP 401 / 403 means the cookies are stale — grab fresh ones.
- If publish/unpublish specifically fail while create/list succeed, the
  `x-client-transaction-id` header may now be required (see Pitfalls).

`.secrets/` is gitignored, so the cookie file stays local. Delete the test
article from the X composer afterward if you don't want it lingering as a draft.

## API Reference

All calls hit `https://x.com/i/api/graphql/<queryId>/<Operation>`:

| Operation | Method | Purpose |
|---|---|---|
| `ArticleEntityDraftCreate` | POST | Create an empty draft |
| `ArticleEntityUpdateTitle` | POST | Set title |
| `ArticleEntityUpdateContent` | POST | Set body `content_state` |
| `ArticleEntityUpdateCoverMedia` | POST | Set cover from a media id |
| `upload.x.com/i/media/upload.json` | POST | Chunked image upload (INIT/APPEND/FINALIZE) |
| `ArticleEntityPublish` | POST | Publish (`visibilitySetting`) |
| `ArticleEntityUnpublish` | POST | Unpublish back to draft |
| `ArticleEntityDelete` | POST | Permanently delete an article |
| `ArticleEntitiesSlice` | GET | List drafts/published, cursor-paginated |

The article id used in requests is the numeric `rest_id` (e.g. `2074335832941719552`), also derivable by base64-decoding the GraphQL `id` (`ArticleEntity:<numeric>`).

## Pitfalls

- **Query IDs rotate.** The `<queryId>` per operation is baked into the current web bundle. When a call fails with HTTP 404 or a persisted-query error, recapture a HAR of the article flow and update the `_Q` map in `scripts/x_com.py`.
- **Cookies expire.** HTTP 401/403 usually means a stale `auth_token`/`ct0`. Grab fresh cookies from the browser.
- **`ct0` must match the cookie.** The `x-csrf-token` header must equal the `ct0` cookie value; the script keeps them in sync automatically.
- **Cover uploads** go to `upload.x.com` (a different host from the GraphQL API) and send the whole image as a single APPEND segment — fine for cover-sized JP/PNG images, but very large files would need real multi-segment chunking, which is not implemented.
- **`x-client-transaction-id`.** The web app sends a per-request `x-client-transaction-id` header that this client omits. GraphQL article endpoints have accepted requests without it, but if X tightens verification and calls start failing, that header may need to be generated.
- **Listing needs a user id.** Include the `twid` cookie or pass `--user-id`; create/publish/unpublish do not need it.
