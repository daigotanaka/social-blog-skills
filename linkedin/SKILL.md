---
name: linkedin
description: Use this skill to create, update, add a header image to, and schedule LinkedIn newsletter article drafts through the local scripts/linkedin.py helper, including markdown manuscripts and required accompanying LinkedIn posts.
---

# LinkedIn Newsletter Publishing

Use `scripts/linkedin.py` to automate LinkedIn newsletter drafts through LinkedIn's private browser/Voyager API captured from HAR files. This is not a public LinkedIn SDK. Treat it as a browser-session helper.

The main workflow is:

1. Provide LinkedIn browser session cookies.
2. Create or update a newsletter article draft from markdown.
3. Optionally upload and attach a 16:9 header image.
4. Schedule the article by scheduling an accompanying LinkedIn post.

Run commands from the `linkedin` skill directory unless paths below are adjusted:

```bash
cd /Users/daigotanaka/projects/openclaw_skills/linkedin
```

## Authentication

The script uses browser cookie auth, not OAuth and not JWT bearer auth.

Required values:

- `li_at`: the main logged-in LinkedIn session cookie
- `JSESSIONID`: used to generate the `csrf-token` header

To get these values manually, log into `linkedin.com` in a browser. In Chrome:

1. Right-click the LinkedIn page and choose Inspect.
2. Open Application -> Storage -> Cookies -> `https://www.linkedin.com` or `https://linkedin.com`.
3. Find the `li_at` and `JSESSIONID` cookie rows.
4. Double-click the cryptic string in the Value column to highlight it.
5. Right-click and copy the value.
6. Make sure the copied value is URL-decoded.

Recommended local file layout:

```text
.secrets/li_at.txt
.secrets/JSESSIONID.txt
```

Use them like this:

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py ...
```

`JSESSIONID` may be copied from Chrome with surrounding quotes. The script normalizes this.

Alternative raw cookie form:

```bash
python3 scripts/linkedin.py \
  --cookie 'li_at=...; JSESSIONID="ajax:..."' \
  ...
```

If requests fail with `401`, `403`, `CSRF check failed`, or login HTML instead of JSON, refresh both cookies from the same active Chrome session.

## Known URNs

From the captured HARs:

```text
author profile:  urn:li:fsd_profile:ACoAAAO6oxgBYTC3L2NOWSGcsmc7s4aILfLREQ0
newsletter:      urn:li:fsd_contentSeries:7374948590156357632
```

Use these unless the target author/newsletter changes.

## Markdown Manuscripts

`scripts/linkedin.py` accepts markdown with `--markdown-file` or the note.com-style alias `--body-file`.

The first `# Heading` becomes the article title unless `--title` is provided. The markdown converter is intentionally conservative because LinkedIn rejects some rich private-editor block schemas. It creates plain LinkedIn text blocks for:

- paragraphs
- headings
- quotes
- fenced code blocks
- bullet/numbered list text

Links are converted to their visible text in the LinkedIn block payload. The generated HTML is also sent as `contentHtml`.

## Create a Draft From Markdown

Use `create-article` for a new draft only. This does not schedule or publish.

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py create-article \
  --author-profile-urn urn:li:fsd_profile:ACoAAAO6oxgBYTC3L2NOWSGcsmc7s4aILfLREQ0 \
  --content-series-urn urn:li:fsd_contentSeries:7374948590156357632 \
  --body-file /path/to/article.md
```

The output includes:

```text
entityUrn:          urn:li:fsd_firstPartyArticle:<id>
linkedInArticleUrn: urn:li:linkedInArticle:<id>
```

Editor URL:

```text
https://www.linkedin.com/article/edit/<id>/
```

## Update an Existing Draft From Markdown

Use `save-article` with the article id or either article URN. This updates title/body only unless image flags are also passed.

Important: use the newsletter article draft identifier, not the scheduled share/post identifier.

Accepted article identifiers:

```text
<article-id>
urn:li:fsd_firstPartyArticle:<article-id>
urn:li:linkedInArticle:<article-id>
```

Do not use the scheduled share/post id from `schedule-post` output for draft editing. For example, in one verified run:

```text
edit with:        7479337101969317889
also accepted:    urn:li:fsd_firstPartyArticle:7479337101969317889
also accepted:    urn:li:linkedInArticle:7479337101969317889
do not edit with: 7479342067001794560
```

The last value is the accompanying scheduled LinkedIn post/share id, not the article draft.

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py save-article <article-id> \
  --body-file /path/to/article.md
```

To override the markdown title:

```bash
python3 scripts/linkedin.py save-article <article-id> \
  --body-file /path/to/article.md \
  --title "Custom LinkedIn Title"
```

Do not pass `--header-image` when the existing header should remain unchanged.

## Upload and Attach a Header Image

LinkedIn's editor displays newsletter header images in a 16:9 slot. Good target sizes:

```text
720 x 405 minimum
1280 x 720 recommended
1600 x 900 also safe
```

Attach or replace a header image:

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py save-article <article-id> \
  --header-image .secrets/ok-now-we-need-mcp.jpg \
  --header-image-content-type image/jpeg
```

This performs two operations:

1. Upload media using `voyagerVideoDashMediaUploadMetadata?action=upload`.
2. Patch the article's `coverMediaV2Union.coverImage.originalImageUrn`.

It does not change body text unless markdown/content flags are also passed.

## Schedule With an Accompanying Post

LinkedIn schedules newsletter publication through an accompanying LinkedIn post. Use `schedule-post`; it references the article as media and schedules the share.

Create the post text in a file first to preserve line breaks:

```bash
cat > /tmp/linkedin-post.txt <<'EOF'
If you already have a FastAPI backend, do you really need an MCP server?

That was my honest starting point.

...
EOF
```

Schedule for an ISO datetime with timezone:

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py schedule-post <article-id> \
  --content-series-urn urn:li:fsd_contentSeries:7374948590156357632 \
  --post-text-file /tmp/linkedin-post.txt \
  --scheduled-at "2026-07-06T06:30:00-07:00"
```

The output includes the scheduled share URN:

```text
urn:li:fsd_share:urn:li:ugcPost:<id>
```

This share/post id confirms scheduling, but it is not the draft article id. Keep the original article id for later `save-article`, header-image replacement, or `fetch-latest-draft` commands.

`--scheduled-at` can also be epoch milliseconds.

## Move a Scheduled Newsletter Back to Drafts

LinkedIn scheduled newsletter editing is not intuitive: once a newsletter is scheduled, the article is in `SCHEDULED` state and normal article body PATCHes are rejected. To edit the manuscript, first delete the scheduled share/post. In the web UI this appears as moving the scheduled newsletter back to drafts; in the API capture it is a GraphQL `deleteContentcreationDashShares` request for the scheduled post/share.

There are two different identifiers involved:

```text
article id:        used with save-article and fetch-latest-draft
scheduled post id: used with delete-scheduled-share
```

The safe edit sequence for an already scheduled newsletter is:

1. Find the scheduled post id.
2. Delete the scheduled share/post to move the article back to drafts.
3. Update the article draft with `save-article`.
4. Schedule the article again with the accompanying post text.

Find the scheduled post:

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py list-post \
  --author-profile-urn urn:li:fsd_profile:ACoAAAO6oxgBYTC3L2NOWSGcsmc7s4aILfLREQ0 \
  --filter scheduled
```

Then use `delete-scheduled-share` with the `scheduledPostUrn` or `scheduledShareUrn` from `list-post` output:

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py delete-scheduled-share urn:li:ugcPost:<scheduled-post-id>
```

Accepted scheduled post identifiers:

```text
<scheduled-post-id>
urn:li:ugcPost:<scheduled-post-id>
urn:li:fsd_share:urn:li:ugcPost:<scheduled-post-id>
```

After this succeeds, use the original article draft id with `save-article`, then run `schedule-post` again. Do not pass the article id to `delete-scheduled-share`, and do not pass the scheduled post id to `save-article`.

Example edit and reschedule flow:

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py delete-scheduled-share urn:li:ugcPost:<scheduled-post-id>

LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py save-article <article-id> \
  --body-file /path/to/article.md

LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py schedule-post <article-id> \
  --content-series-urn urn:li:fsd_contentSeries:7374948590156357632 \
  --post-text-file /tmp/linkedin-post.txt \
  --scheduled-at "2026-07-06T06:30:00-07:00"
```

## Full Create, Header, and Schedule Flow

Use separate steps when debugging or when preserving an existing header. Use the combined command only when all inputs are ready.

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py create-newsletter \
  --author-profile-urn urn:li:fsd_profile:ACoAAAO6oxgBYTC3L2NOWSGcsmc7s4aILfLREQ0 \
  --content-series-urn urn:li:fsd_contentSeries:7374948590156357632 \
  --body-file /path/to/article.md \
  --header-image .secrets/header.jpg \
  --header-image-content-type image/jpeg \
  --post-text-file /tmp/linkedin-post.txt \
  --scheduled-at "2026-07-06T06:30:00-07:00"
```

This creates a draft, saves body/title, attaches the header image, then schedules the accompanying post.

## Other Commands

Fetch sharebox metadata:

```bash
python3 scripts/linkedin.py fetch-sharebox
```

Fetch latest draft data for an article:

```bash
python3 scripts/linkedin.py fetch-latest-draft <article-id>
```

List articles by state:

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py list-articles \
  --author-profile-urn urn:li:fsd_profile:ACoAAAO6oxgBYTC3L2NOWSGcsmc7s4aILfLREQ0
```

If `--filter` is omitted, `list-articles` fetches `draft`, `scheduled`, and `published`. `--filter` is comma-separated and accepts `draft`, `scheduled`, and `published`; these map to LinkedIn article-list states `DRAFT`, `SCHEDULED`, and `PUBLISHED`.

List scheduled accompanying posts for newsletter articles:

```bash
LINKEDIN_LI_AT="$(cat .secrets/li_at.txt)" \
LINKEDIN_JSESSIONID="$(cat .secrets/JSESSIONID.txt)" \
python3 scripts/linkedin.py list-post \
  --author-profile-urn urn:li:fsd_profile:ACoAAAO6oxgBYTC3L2NOWSGcsmc7s4aILfLREQ0 \
  --filter scheduled
```

`list-post --filter scheduled` uses the scheduled article listing captured from the web UI and returns the scheduled post URN, scheduled time, article URN, and title. Use the `scheduledPostUrn` value with `delete-scheduled-share` when moving a scheduled newsletter back to drafts.

Upload a cover image without attaching it:

```bash
python3 scripts/linkedin.py upload-cover .secrets/header.jpg --article-id <article-id>
```

## Operational Notes

- Live commands mutate LinkedIn state. Confirm intent before creating drafts, replacing headers, or scheduling posts.
- Keep article draft ids separate from scheduled share/post ids. `save-article` needs an article id or article URN, while `schedule-post` returns a share/post URN.
- Do not PATCH a scheduled article directly. Move it back to drafts with `delete-scheduled-share`, edit with `save-article`, then reschedule.
- Do not print cookie values in responses or logs.
- Keep `.secrets/` out of git.
- If a rich markdown save returns `422`, retry after simplifying the markdown or using the conservative default converter in `linkedin.py`.
- If an existing header image should be preserved, do not pass `--header-image`.
