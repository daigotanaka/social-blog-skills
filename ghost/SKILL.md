---
name: ghost
description: Use this skill for Ghost CMS work: creating drafts, updating posts, uploading feature images, scheduling or publishing posts, fetching post JSON, converting Markdown, and using the Ghost Admin API through the local unified client.
---

# Ghost CMS

## Rule

Use `scripts/ghost.py` as the only supported interface.

Do not call the legacy split scripts in `scripts/old/` unless the user explicitly asks for archaeology or migration help. They are not part of the active workflow.

## Credentials

`scripts/ghost.py` reads credentials automatically from either `scripts/.secrets/` or `.secrets/`:

- `ghost_api_url.txt`
- `ghost_admin_api_key.txt`
- `ghost_content_api_key.txt` or the existing `ghost_content.api_key.txt`

For all create, update, image upload, schedule, and publish operations, use the Admin API key. The client generates the short-lived Ghost Admin JWT internally. Do not ask the user for a JWT token.

Useful environment overrides:

- `GHOST_API_URL`
- `GHOST_ADMIN_API_KEY`
- `GHOST_CONTENT_API_KEY`

## Human Setup

Use this setup when a human needs to create or refresh local Ghost credentials.

1. Open Ghost Admin in a browser.
2. Go to `Settings` -> `Integrations`.
3. Create a new `Custom Integration`, or open the existing integration for this skill.
4. Copy the `Admin API key`. It should look like `<id>:<hex-secret>`.
5. Copy the `API URL`. Use the site/admin domain shown by Ghost, for example `https://example.ghost.io` or `https://www.example.com`.
6. Save the values in the skill secrets directory:

```text
scripts/.secrets/ghost_api_url.txt
scripts/.secrets/ghost_admin_api_key.txt
```

If a Content API key is needed for read-only public content operations, save it separately:

```text
scripts/.secrets/ghost_content_api_key.txt
```

Keep `.secrets` out of git. Do not paste API keys into chat, logs, commits, examples, or generated docs.

The Admin API URL used by requests is derived automatically as:

```text
<ghost_api_url>/ghost/api/admin/
```

## CLI First

Run commands from the skill root unless another working directory is clearly better:

```bash
python3 scripts/ghost.py draft --body-file article.md
python3 scripts/ghost.py draft --title "My Post" --body-file article.md --image cover.jpg --tags '["AI", "Ghost"]' --featured --excerpt "Short summary"
python3 scripts/ghost.py update <post_id> --title "Updated title" --body-file article.md --tags '["AI", "Ghost"]' --featured --excerpt "Short summary"
python3 scripts/ghost.py publish <post_id>
python3 scripts/ghost.py schedule <post_id> --published-at 2026-07-08T13:30:00.000Z
python3 scripts/ghost.py fetch <post_id>
python3 scripts/ghost.py fetch <post_id> --body-only --output body.html
python3 scripts/ghost.py list --limit 20 --compact
python3 scripts/ghost.py upload-image cover.jpg
python3 scripts/ghost.py convert article.md article.html --extract-title
python3 scripts/ghost.py summary posts.json
```

Auth flags work before or after the subcommand:

```bash
python3 scripts/ghost.py --api-url https://example.com list
python3 scripts/ghost.py list --api-url https://example.com
```

## Python API

Use the importable client when the CLI does not expose a needed option.

```python
import sys
sys.path.insert(0, "scripts")
from ghost import Ghost

client = Ghost()
draft = client.create_post(markdown="# Title\n\nBody", status="draft")
post = client.fetch_post(draft["id"])
updated = client.update_post(draft["id"], feature_image_path="cover.jpg")
published = client.publish_post(draft["id"])
```

Common methods:

- `create_post(title=None, html=None, markdown=None, status="draft", feature_image_path=None, feature_image_alt=None, tags=None, custom_excerpt=None, featured=None)`
- `update_post(post_id, title=None, html=None, markdown=None, status=None, published_at=None, feature_image_path=None, clear_feature_image=False, feature_image_alt=None, tags=None, custom_excerpt=None, featured=None, newsletter=None, email_segment=None)`
- `publish_post(post_id, title=None, html=None, markdown=None, tags=None, newsletter=None, email_segment=None)`
- `schedule_post(post_id, published_at, title=None, html=None, markdown=None, tags=None, feature_image_path=None, clear_feature_image=False, feature_image_alt=None, custom_excerpt=None, featured=None, newsletter=None, email_segment=None)`
- `fetch_post(post_id, include="tags,authors", formats="html,lexical")`
- `list_posts(limit=15, page=1, status_filter=None, compact=False)`
- `upload_image(image_path)`
- `convert_markdown(markdown_text)`
- `summarize_posts(posts_response)`

`tags` may be a JSON array of tag names, e.g. `["AI", "Ghost"]`, or tag objects with `id`, `name`, and/or `slug` fields copied from Ghost Admin API responses. Use `--featured` to turn on Ghost's "Feature this post" toggle and `--unfeatured` to turn it off.

## Scheduling

When scheduling a post, convert the requested local time to UTC and pass the UTC timestamp:

```bash
python3 scripts/ghost.py schedule <post_id> --published-at 2026-07-08T13:30:00.000Z
```

This command sets:

- `status: "scheduled"`
- `published_at: "<UTC ISO timestamp>"`
- current `updated_at` from `fetch_post`

By default, `publish` and `schedule` publish to the blog site only and do not send email. To send email subscribers, pass a Ghost newsletter slug explicitly:

```bash
python3 scripts/ghost.py publish <post_id> --newsletter weekly-newsletter --email-segment all
python3 scripts/ghost.py schedule <post_id> \
  --published-at 2026-07-08T13:30:00.000Z \
  --newsletter weekly-newsletter \
  --email-segment all
```

`--email-segment` is optional and only works with `--newsletter`. Common values are `all`, `status:free`, and `status:-free`. Omit both `--newsletter` and `--email-segment` when the user wants to publish only on the blog site without emailing subscribers.

The equivalent Python API is:

```python
import sys
sys.path.insert(0, "scripts")
from ghost import Ghost

post_id = "<post_id>"
published_at = "2026-07-08T13:30:00.000Z"

client = Ghost()
updated = client.schedule_post(post_id, published_at)
```

Always verify scheduling with a follow-up `fetch_post` and report both the user-facing local time and the UTC `published_at`.

## Workflow

1. Locate or receive the Markdown/HTML source.
2. Create a draft first unless the user explicitly asks to publish or schedule immediately.
3. Attach the feature image with `--image` or `update_post(..., feature_image_path=...)`.
4. For updates, let `ghost.py` fetch the current post first so the API receives `updated_at`.
5. For network/DNS sandbox failures, rerun the same Ghost API command with escalation.
6. Report the Ghost post id, status, title, preview/public URL, and any scheduled timestamp.

## Notes

- Ghost Admin API requests default to `Accept-Version: v5.0`.
- Markdown is converted to HTML and sent with `source=html`.
- The first Markdown H1 becomes the title when `--title` is omitted. By default, that H1 is stripped from body HTML.
- `python3` may not have the `markdown` package installed. `ghost.py` has a fallback converter, but high-fidelity Markdown conversion is better when `markdown` is available.
- Draft preview URLs use the post `uuid`: `https://www.example.com/p/<uuid>/`.
- Public URLs use the post slug after publish: `https://www.example.com/<slug>/` or the URL returned by Ghost.

## References

- Official Ghost Admin API overview: `https://docs.ghost.org/admin-api`
- Official Ghost post scheduling docs: `https://docs.ghost.org/admin-api/posts/scheduling-a-post`
- Official Ghost email sending docs: `https://docs.ghost.org/admin-api/posts/sending-a-post`
- Admin API base URL format: `https://{admin_domain}/ghost/api/admin/`
- Authentication reference: Admin API keys come from Ghost Admin custom integrations and are used to generate short-lived JWTs for `Authorization: Ghost <token>`.
- Stable Admin API resources include posts, pages, tags, members, users, images, themes, site, and webhooks.
