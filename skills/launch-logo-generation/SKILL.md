---
name: launch-logo-generation
description: "Generate token logos from Hermes-selected tweet context and metadata using Hermes image_generate, inspect the result, and prepare a local image for Kaiba launch metadata."
metadata:
  hermes:
    tags: [Creative, Image, Launch]
---

# Launch Logo Generation

Hermes owns the concept and prompt. Use the chosen name/ticker and `logo_prompt` from `launch-metadata`. Prefer the post's relevant uploaded image when it is already the intended logo. For a vamp, preserve the source image by default. Generate when no usable source image exists or the owner requests a new logo.

Use Hermes's actual `image_generate` tool when enabled and available:

```json
{"prompt":"A cheerful copper robot duck with a bold readable silhouette, centered circular badge inside a square canvas, warm copper and teal, plain background, no text or tiny details.","aspect_ratio":"square"}
```

Tool name is `image_generate`, toolset `image_gen`. Add `image_url` or `reference_image_urls` only if the active tool schema advertises reference/edit support. For edits state which visual details must stay. Keep a single readable subject with generous margins; inspect at thumbnail size. Do not use a real author's profile picture as a substitute for a logo. Text is optional only when requested and should be reviewed for accuracy. Request transparent output only from a backend that advertises it; never call an opaque image transparent.

Check `success` and the returned `image` / `agent_visible_image`; read/view the actual artifact, not just its URL. Preserve the model/provider provenance. Save a versioned copy in `~/kaiba/data/social/creative/` before handing it to a launch. Do not overwrite an earlier reviewed asset.

Readiness check: `image_gen` must be enabled in the Hermes profile and a backend credential
(FAL / OpenAI / OpenRouter) must be configured. If it is not, say so; do not claim generation
works, import or export credentials, buy a plan, or bypass a disabled tool via direct plugin calls.

The existing launcher also has `tweet_creative.make_logo`: tweet image first, then its configured OpenAI/Pollinations paths. Their existence is not proof of a successful generation; do not call a failed or402response a logo. No provider/model selection changes were made by this skill installation. [Hermes native image documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/image-generation/).

For a generated local PNG/JPEG/WebP already reviewed by Hermes, validate the handoff:

```sh
cd ~/kaiba
.venv/bin/python -m kaiba.execution.launch_assets prepare --metadata data/social/metadata.json --logo data/social/creative/logo.png --out data/social/creative/manifest.json
```

The helper checks file size and raster signature, not visual quality/full decoding. Its90,000byte cap comes from the existing launcher's Linux argument limit. `metadata_argv` returns only name/ticker/description/image flags; wallet, route,5%supply buy, approved launch/daily caps, holder routing and watchdog exits stay with `tweet-auto-launch`. If the image is too large, use a supported provider output/compression setting or explicitly authorized format conversion; do not truncate image bytes or add a fake URL. No token is launched by generation or packaging.
