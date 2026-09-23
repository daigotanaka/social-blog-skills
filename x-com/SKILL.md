---
name: x-com
description: API access for X.com (Twitter) long-form Articles — create markdown drafts, set title/content/cover, publish, unpublish, and list draft or published articles.
---

# x-com

Before using this skill, read `AGENTS.md` in this directory if it exists. That file is for local, untracked rules and notes (token sources, personal account details) that should not be committed with the skill.

Use `scripts/x_com.py` for X.com Articles work: draft creation, title/content/cover updates, publishing, unpublishing, and listing drafts or published articles. The client is reverse-engineered from the X web composer's GraphQL traffic.

**Run all commands from this skill's directory** (`cd` here first). The default
credential files (`.secrets/*.txt`) and the `scripts/x_com.py` path in every
example below resolve relative to the current directory. Pass body/cover files
as absolute paths or paths relative to this directory.

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
command additionally needs your user id, read automatically from
`.secrets/twid` when present (or pass `--user-id`).

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

The client resolves credentials in this priority order: CLI flags > `--cookie`
string > `--cookie-path` file > environment variables > the two default
`.secrets/*.txt` files. The default-files path means an agent can drop two
files once and then run every command with no flags:

```bash
# 1. Discrete files (what this repo uses by default)
#    .secrets/auth_token.txt  -> the auth_token value
#    .secrets/ct0.txt         -> the ct0 value
python3 scripts/x_com.py publish <id>            # no flags: reads .secrets/*.txt
python3 scripts/x_com.py set-content <id> --auth-token-path a.txt --ct0-path c.txt

# 2. Discrete values on the command line
python3 scripts/x_com.py publish <id> --auth-token "..." --ct0 "..."

# 3. A raw browser Cookie header string (include twid for list)
python3 scripts/x_com.py list --cookie "auth_token=...; ct0=...; twid=u%3D<user-id>"

# 4. A file containing that Cookie header string
python3 scripts/x_com.py list --cookie-path .secrets/x_com_cookie.txt

# 5. Environment variables
X_AUTH_TOKEN=... X_CT0=... python3 scripts/x_com.py publish <id>
X_COM_COOKIE="auth_token=...; ct0=...; twid=u%3D<user-id>" python3 scripts/x_com.py list
```

**Default files:** with no auth flags, the client reads `.secrets/auth_token.txt`
and `.secrets/ct0.txt` (relative to the current directory). Dropping those two
files is enough for create/publish/unpublish.

Cookies expire — refresh them from the browser if calls start returning HTTP
401/403.

### User id for listing

`list` needs the numeric user id. It is auto-parsed from a `twid` cookie
(`u=<id>`) in a cookie string/file, or from a `.secrets/twid` file (holding
`u=<id>` or the bare id) sitting next to the auth-token file. Pass
`--user-id <user-id>` to override. If none of those exist, `list` errors and
everything else still works — the id is not needed for create/publish/unpublish.

## Quick Start

```bash
# Create a draft from a markdown file (create shell + title + content).
# --title is optional when the file has front matter with a title:, and a
# leading "# <same title>" heading in the body is dropped automatically.
python3 scripts/x_com.py draft --title "My Article" --body-file article.md

# Create and publish in one step
python3 scripts/x_com.py draft --title "My Article" --body-file article.md --publish

# List drafts / published articles (user id auto-read from .secrets/twid)
python3 scripts/x_com.py list --status draft
python3 scripts/x_com.py list --status published

# Publish / unpublish an existing article by id
python3 scripts/x_com.py publish <article_id>
python3 scripts/x_com.py unpublish <article_id>
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
| `--title <str>` | Article title (optional when the body file has a front-matter `title:`) |
| `--body-file <path>` | Markdown file for the body |
| `--cover-image <path>` | Local image file to upload and set as the cover |
| `--cover-media-id <id>` | Already-uploaded media id to set as the cover |
| `--no-inline-images` | Skip uploading/inserting `![...](path)` images |
| `--publish` | Publish immediately after creating |
| `--visibility <str>` | Publish visibility, default `Public` |

When neither `--cover-image` nor `--cover-media-id` is given, a front-matter `image:` entry in the body file is auto-resolved relative to that file and used as the cover.

Prints the new numeric `article_id` and the editor URL `https://x.com/compose/articles/edit/<article_id>`.

Python API:

```python
from x_com import XCom

client = XCom()  # auth from .secrets/*.txt (cwd = skill dir); or XCom(cookie_path=...)
result = client.create_article(title="Hello", body_file="article.md")
print(result["article_id"])
client.publish_article(result["article_id"])
```

## Incremental Editing

Build or edit a draft step by step:

```bash
# Empty draft shell -> prints article_id
python3 scripts/x_com.py create

python3 scripts/x_com.py set-title <article_id> --title "New Title"
python3 scripts/x_com.py set-content <article_id> --body-file article.md   # links + inline images handled
python3 scripts/x_com.py set-content <article_id> --body-file article.md --no-inline-images

# Cover: upload a local image, or reuse an already-uploaded media id
python3 scripts/x_com.py set-cover <article_id> --image cover.jpg
python3 scripts/x_com.py set-cover <article_id> --media-id <media_id>
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
| `![alt](path)` | `atomic` block + `MEDIA` entity (image auto-uploaded first; path resolved relative to the body file) |
| `\| col \| col \|` pipe table | `atomic` block + `MARKDOWN` entity (verbatim table text; same mechanism as code fences) |
| `*caption text*` line under an image | becomes that image's `data.caption` (renders as X's figure caption), *not* a paragraph |
| anything else | `unstyled` paragraph |

Inline `**bold**` and `*italic*` become Draft.js inline style ranges — X accepts only `Bold` and `Italic`. Backticks and `_` are left literal (X has no inline-code style, and this avoids mangling URLs and snake_case). `![alt](url)` (remote image) also matches the image rule — it is uploaded from disk only when the path is a real local file; otherwise the block is skipped with a warning. Blank lines separate blocks. Use `\` to escape a literal marker.

**Image captions:** the established authoring convention (same as the user's
other projects) is a pure-italic line `*like this*` on the line right after an
image, at most one blank line separating them:

```markdown
![ignored alt text](img.png)

*Fig 1. The caption that X actually renders under the image*
```

The converter attaches it to the preceding MEDIA entity as `data.caption` —
exactly what the web composer's caption field sends (verified from HAR).
Note the **alt text in `![...]` is discarded** (X articles have no alt field);
captions come ONLY from the following italic line. Two blank lines, or any
other block in between, ends the caption window (the line then stays a normal
paragraph). A line containing other markers (e.g. `*see [a](b)*` or bold `**`)
is never treated as a caption.

**Hyperlinked images** (`[![alt](img.png)](url)`): X's composer serializes
these as a plain MEDIA entity and *drops the link* (verified from HAR) — the
converter mirrors that: the image is inserted, the wrapping URL is discarded.

Inline `[label](url)` becomes a **LINK entity** — `{"data": {"url": ...}, "type": "LINK", "mutability": "Mutable"}` in `entity_map` plus an `entity_range` over the label text (this is the exact shape the browser composer sends, captured from HAR; per-article duplicate urls share one entity). A leading `# Title` line identical to the front-matter `title:` is dropped automatically — X renders the title above the body, and keeping the H1 duplicates it.

**Schema constraints (learned the hard way — X returns an opaque `GRAPHQL_VALIDATION_FAILED` or `OperationalError: Internal: Unspecified` when violated):**

- Only `header-one` and `header-two` exist; `header-three`/`header-four` are rejected.
- There is no `code-block` type; fenced code must be a `MARKDOWN` atomic entity.
- Only `Bold`/`Italic` inline styles; `Code` (and others) are rejected.
- Block `text` must be single-line; embed no `\n` (each line is its own block).
- LINK entities: `data` carries **only** `url`, and `mutability` is `Mutable`. A `link_name` field is rejected (validation error on `data.link_name`).
- Pipe tables are NOT a native block type: they ride a MARKDOWN atomic entity
  whose `data.markdown` is the verbatim `| a | b |` text (composer-captured).
- MEDIA `data.caption` is plain text (inline styles stripped); it renders as
  the caption under the figure.
- MEDIA (inline image) entities: shape is `{"type": "MEDIA", "mutability": "Immutable", "data": {"entity_key": "<uuid4>", "media_items": [{"local_media_id": <int>, "media_category": "DraftTweetImage", "media_id": "<id>"}]}}` on an `atomic` block. Upload the file first via the `tweet_image` media category. **Do not hand-build
`data.mediaId`/`data.url` variants** — `mediaId` is rejected by validation and
the url-only shape, while accepted, is not what the composer produces.
- The WRITE payload uses snake_case (`entity_map` on the content_state, `entity_ranges` per block), but a READ via `ArticleEntitiesSlice` returns the stored state with **`entityMap`** (camelCase) at the content_state level. When verifying a draft, walk the response for the article's `content_state` dict and read `blocks` + `entityMap`.
- `media_entities` at the article level lists every media id referenced by the body ( handy to confirm inline uploads landed).

To supply a raw `content_state` directly, call `client.set_content(article_id, content_state=...)` from Python.

### Verifying a draft after writing it

`list` output does not include the body. To confirm links/images/captions
landed server-side, fetch `ArticleEntitiesSlice` and walk the response for the
article's `content_state`, then inspect `blocks` + `entityMap` (note the
camelCase on reads) and the article-level `media_entities` (every media id
referenced by the body). Example one-liner pattern:

```python
body = client._get("ArticleEntitiesSlice", {"userId": uid, "lifecycle": "Draft", "count": 5})
# walk dicts for {"rest_id": <article_id>} -> res["content_state"]["entityMap"]
```

## Publishing

```bash
python3 scripts/x_com.py publish <article_id> [--visibility Public]
```

Uses `ArticleEntityPublish` with `visibilitySetting` (default `Public`). Returns the resulting `lifecycle` (`Published`).

> [!WARNING]
> **Publishing is rate-limited and can be flagged as automation.** `publish` (and
> `unpublish`/`delete`) are account "write" actions. X can reject them with an
> `AuthorizationError` even when your cookies are valid and the code is correct.
> Two distinct errors show up:
>
> | Code | Message | Meaning | What to do |
> |---|---|---|---|
> | **344** | "You have reached your daily limit for sending Tweets and messages. Please try again later." | Hard daily write cap reached | Wait for the ~24h rolling window to clear; don't retry sooner. |
> | **226** | "This request looks like it might be automated. To protect our users from spam and other malicious activity, we can't complete this action right now. Please try again later." | Anti-spam / bot-detection heuristic tripped | **Back off and wait (longer than 344).** Do **not** retry in a loop — repeated automated attempts make it worse. |
>
> **Error 226 is transient, not a bug.** In practice the *same* `publish` command
> that returned 226 succeeded later after a longer cool-down. What trips it is
> **behavior, not credentials**: bursts of automated writes, or a fresh headless
> session firing publishes back-to-back (e.g. a scheduled task retrying too
> aggressively). Mitigations:
>
> - Space out writes; avoid publishing several articles in quick succession.
> - If a scheduled/automated publish returns 226, let it fail and retry **hours**
>   later — not minutes. Don't put `publish` in a tight retry loop.
> - If it needs to go out urgently, publish that one manually from the X web UI.
> - Heavy debugging that creates/deletes many drafts can prime both limits; give
>   the account a rest before publishing for real.

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
client.delete_article("<article_id>")
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

Run this when in doubt (e.g. after X ships a new web bundle) to confirm the
query IDs and auth still work end to end. It creates a throwaway draft,
publishes, unpublishes, and lists — exercising every operation. Auth comes
from the usual sources; with `.secrets/*.txt` in place no flags are needed.
Skip it unless you must — publish/unpublish churn can trip the account-level
rate/automation limits (see Publishing).

```bash
# 1. Create + edit a draft from markdown
printf '# Smoke Test\n\nHello from the x-com skill.\n' > /tmp/x_smoke.md
python3 scripts/x_com.py draft --title "Smoke Test" --body-file /tmp/x_smoke.md
# -> note the printed article_id

# 2. It should appear under drafts
python3 scripts/x_com.py list --status draft

# 3. Publish it, confirm it moves to published, then UNPUBLISH and DELETE it
python3 scripts/x_com.py publish <article_id>
python3 scripts/x_com.py list --status published
python3 scripts/x_com.py unpublish <article_id>
python3 scripts/x_com.py delete <article_id>
```

What to check:

- Every step prints `"success": true` (or a populated list) rather than an
  `Error:` line.
- An HTTP 404 / persisted-query error on any step means the query IDs rotated —
  recapture a HAR and update `_Q` in `scripts/x_com.py`.
- An HTTP 401 / 403 means the cookies are stale — grab fresh ones.
- If publish/unpublish specifically fail while create/list succeed, the
  `x-client-transaction-id` header may now be required (see Pitfalls).

`.secrets/` is gitignored, so credentials stay local. The smoke draft ends
deleted; if any step fails midway, clean it up with `delete`.

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

The article id used in requests is the numeric `rest_id`, also derivable by base64-decoding the GraphQL `id` (`ArticleEntity:<numeric>`).

## Pitfalls

- **Query IDs rotate.** The `<queryId>` per operation is baked into the current web bundle. When a call fails with HTTP 404 or a persisted-query error, recapture a HAR of the article flow and update the `_Q` map in `scripts/x_com.py`.
- **Cookies expire.** HTTP 401/403 usually means a stale `auth_token`/`ct0`. Grab fresh cookies from the browser.
- **Write limits & automation flags (`AuthorizationError` 344 / 226).** Publishing can be rejected even with valid auth: `344` = daily write cap (wait ~24h), `226` = "looks automated" anti-spam block (back off for hours, never loop-retry). See the warning under [Publishing](#publishing). These are account-level, not code bugs.
- **`ct0` must match the cookie.** The `x-csrf-token` header must equal the `ct0` cookie value; the script keeps them in sync automatically.
- **Cover uploads** go to `upload.x.com` (a different host from the GraphQL API) and send the whole image as a single APPEND segment — fine for cover-sized JP/PNG images, but very large files would need real multi-segment chunking, which is not implemented.
- **`x-client-transaction-id`.** The web app sends a per-request `x-client-transaction-id` header that this client omits. GraphQL article endpoints have accepted requests without it, but if X tightens verification and calls start failing, that header may need to be generated.
- **Listing needs a user id.** Auto-read from a `twid` cookie or `.secrets/twid`; otherwise pass `--user-id`. create/publish/unpublish do not need it.
