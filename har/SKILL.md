---
name: harprobe
description: Use this skill to reverse-engineer the API calls. Inspect HAR files, extract matching HTTP requests, redact or preserve sensitive fields, and replay captured requests for API investigation. Use when Codex needs to reverse-engineer web app traffic, analyze browser-exported HAR files, compare captured requests, or reproduce a request outside the browser.
---

# Harprobe

Use the bundled scripts in `scripts/` to turn browser network captures into inspectable and replayable request artifacts.

## Core workflow

1. Start from a HAR exported from the browser Network tab.
2. Use `scripts/extract_request.py` to isolate the endpoint of interest.
3. Keep redaction enabled by default while exploring request structure.
4. Use `--no-redact` only when the task requires replaying live credentials or comparing exact captured values.
5. Replay a request with `scripts/send_request.py` using either:
   - separate `--method`, `--url`, `--headers-file`, and `--payload-file` inputs, or
   - a single JSON artifact produced by `extract_request.py --json`.

## Capture a clean HAR in Chrome

Use this capture procedure when the user needs to reverse-engineer one specific web-app action:

1. Open the target web app in Chrome and get it into the state needed before the action.
2. Open DevTools and switch to the **Network** panel.
3. Ensure network recording is active. Chrome records requests in the Network panel while DevTools is open.
4. Clear the current Network log.
5. Enable **Preserve log** when the target flow may navigate or reload the page.
6. Refresh the browser page once so startup/auth requests needed by the action are captured from a clean baseline.
7. Perform only the targeted action you want to study.
8. Stop there; avoid unrelated clicks so the HAR stays small and interpretable.
9. Export the capture as HAR:
   - use the sanitized HAR export for general analysis
   - use the sensitive-data HAR export only when auth headers/cookies are required for replay

For the cleanest minimal HAR, prefer:

```text
open target page → clear Network log → enable recording/preserve log → refresh once → perform only the target action → export HAR
```

Why this matters:

- the refresh captures prerequisite initialization/auth requests
- clearing first prevents stale traffic from obscuring the target flow
- limiting the action scope makes endpoint discovery and request comparison much easier
- sanitized HAR exports omit sensitive headers by default; replay work may require exporting with sensitive data enabled in DevTools settings

## Extract requests

Require a target path fragment so extraction stays intentional:

```bash
python3 scripts/extract_request.py session.har \
  --target-path-fragment "/api/items"
```

Useful options:

- `--method GET` or `--method POST`
- `--json` for machine-readable output
- `--include-response-content` to include response bodies
- `--no-redact` to preserve captured secrets exactly

Prefer the smallest matching fragment that still identifies the intended request clearly.

## Replay requests

Replay from extractor output:

```bash
python3 scripts/send_request.py \
  --request-file extracted_requests.json \
  --request-index 0 \
  --print-response-body
```

Replay from separate files:

```bash
python3 scripts/send_request.py \
  --method POST \
  --url "https://example.com/api/items" \
  --headers-file headers.json \
  --payload-file payload.json
```

`send_request.py` removes HTTP/2 pseudo-headers such as `:authority` before using `requests`.

## Safety rules

- Treat HAR files and no-redact exports as sensitive secrets.
- Prefer redacted outputs for analysis and sharing.
- Expect some captured requests to expire quickly because tokens may rotate.
- If replay fails despite matching visible headers, consider browser-bound state, CSRF, short-lived auth, or anti-bot checks before assuming the endpoint is reusable as a stable API.

## Files

- `scripts/list_endpoints.py` — list distinct sorted endpoints from HAR files as a JSON tree.
- `scripts/extract_request.py` — extract matching requests from HAR files.
- `scripts/send_request.py` — replay extracted requests with Python `requests`.
