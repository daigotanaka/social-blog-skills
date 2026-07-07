# social-blog-skills

`social-blog-skills` is a collection of Codex skills and helper scripts for
working with social blog, newsletter, and publishing platforms.

Many of these skills use browser sessions, captured web traffic, or
reverse-engineered private web APIs rather than official public SDKs. Treat
credentials, cookies, and HAR files as sensitive secrets.

## Supported platforms

| Platform | Skill | What it supports |
|---|---|---|
| Ghost CMS | [ghost/SKILL.md](ghost/SKILL.md) | Create drafts, update posts, upload feature images, schedule or publish posts, fetch post JSON, and convert Markdown through the Ghost Admin API. |
| LinkedIn | [linkedin/SKILL.md](linkedin/SKILL.md) | Create, update, add header images to, and schedule LinkedIn newsletter article drafts through LinkedIn browser/Voyager API calls. |
| note.com | [note-com/SKILL.md](note-com/SKILL.md) | Create and update Markdown drafts, publish free or paid articles, add table-of-contents blocks, list articles, manage magazines, search, like, and engage. |
| Substack | [substack/SKILL.md](substack/SKILL.md) | Post short notes, save article drafts, fetch drafts, list articles, upload images, run prepublish checks, and publish existing drafts. |
| X.com Articles | [x-com/SKILL.md](x-com/SKILL.md) | Create Markdown article drafts, set title/content/cover, publish, unpublish, and list draft or published long-form articles. |

## HAR/API exploration

The [har/SKILL.md](har/SKILL.md) skill is not a posting platform integration.
It helps explore `.har` files exported from Chrome DevTools so you can inspect
browser network traffic, isolate API endpoints, redact or preserve sensitive
fields, and replay captured requests while reverse-engineering web API
behavior.

Use this skill carefully. HAR files can contain cookies, authorization headers,
CSRF tokens, request payloads, and other account-sensitive data.

## How to contribute

Contributions are welcome. A good contribution usually follows this shape:

1. For larger changes, open an issue or discussion if you want to share context,
   but pull requests with working code and evidence are much more useful than
   issue reports alone.
2. Keep each platform in its own folder with a clear `SKILL.md` and any helper
   scripts under that folder.
3. Document authentication requirements, required cookies or API keys, setup
   steps, and safe secret-handling practices.
4. Include concise quick-start examples and note which flows are verified.
5. Avoid committing credentials, cookies, `.secrets/` directories, HAR files
   with sensitive data, generated drafts, or account-specific local notes.
6. Prefer small pull requests with focused changes and a short description of
   the tested workflow.

Please do not assume issue reports will be actively read or investigated. The
capability of each skill is limited by official APIs where they exist, and by
what can be carefully reverse-engineered from browser behavior and HAR files
where they do not. If you run into a limitation or broken workflow, the most
productive first step is usually to record a fresh HAR for the exact action,
work with an AI coding agent to analyze the request flow, and update the CLI.

When you fix an issue or add a feature, please submit a pull request with test
evidence: the command you ran, what it changed or returned, and any relevant
redacted output. Do not include credentials, cookies, raw HAR files, or other
sensitive account data.

When adding a new platform, include:

- a platform folder
- a `SKILL.md`
- any scripts needed by the skill
- README updates linking to the new skill
- notes about whether the integration uses an official API, browser cookies, or
  reverse-engineered web endpoints

Tip: when exploring a new platform, a good starting point is usually a Chrome
HAR recording of the target workflow. Open DevTools, clear the Network log,
reload the page, then complete only the necessary workflow so the HAR contains
enough data to understand the endpoint while staying as small as possible.

`*.har` files contain sensitive information. Do not leave them in repository
files or commit them. This repository ignores `.secrets/` folders, so placing
temporary HAR captures there is relatively safer, but deleting the HAR file as
soon as the analysis is done is strongly recommended.

Keep new platform folders simple. Most platform skills should only need a
`SKILL.md` plus one script such as `scripts/substack.py`. A good script shape is
a small client class, helper functions for request/formatting details, and CLI
commands at the end. CLI commands are essential because they let both humans and
AI agents run the workflow efficiently and repeatably.

When starting a new platform, use an existing platform folder as the reference
structure. A useful AI-agent prompt after saving a HAR file is:

```text
I saved a HAR file under .secrets for <platform> while I performed x, y, z.
Create a new SKILL.md and scripts/<platform>.py in folder <name>, just like the
note-com skill.
```

That usually gives the new integration a practical starting point.

## License and responsibility

This project is released under the [MIT License](LICENSE).

Use these skills at your own risk. The author(s) and contributors take no
responsibility for how the tools are used or for any consequences, including
rate limits, failed posts, deleted drafts, account restrictions, account bans,
or violations of platform terms.

Use the project responsibly, with moderation, and in accordance with the rules
of each platform you interact with.
