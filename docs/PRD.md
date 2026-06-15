# Product Requirements Document

**Project**: TikTok Video Editor Assistant (`tiktok-video-editor-asisten`)
**Stack**: Gradio + ffmpeg + yt-dlp + 9router (LLM) + Telegram Bot + Cloudflare Worker
**Deploy target**: Hugging Face Space (Docker SDK), source mirrored to GitHub.

---

## 1. Problem

Short-form creators on TikTok / Instagram Reels / Threads spend too much time
on repetitive editing work: downloading source clips, normalizing them to a
vertical 9:16 canvas, adding modern transition effects (whip-pan, speed ramp,
motion blur, chromatic aberration, film grain, flash frame), and pushing the
finished video to their distribution channel.

## 2. Goals

- One-paste workflow: drop in a few source URLs, get a TikTok-ready MP4 back.
- A chat assistant that proposes transitions, hook scripts, captions, and hashtags.
- One-click push to a Telegram channel for review or scheduling.
- Always-on: kept alive by a Cloudflare Worker ping so the HF Space does not
  cold-start in the middle of a session.

## 3. Non-Goals (v1)

- Multi-user accounts / auth.
- Cloud render queue (v1 runs on the Space's CPU; long videos may take a while).
- Auto-scheduling to TikTok itself (only Telegram push in v1).
- Music / voiceover generation (planned, not in v1).

## 4. User Stories

1. As a creator, I paste 3 TikTok URLs and one Instagram Reel, click **Edit**,
   and within ~2 minutes I have a 1080x1920 MP4 with whip-pan transitions
   and slow push-in zoom.
2. As a creator, I ask the AI assistant "Give me a 12-word hook for a perfume
   reveal" and get a usable line in the chat.
3. As a creator, I click **Send to Telegram** and the file lands in my channel
   with the caption I typed.
4. As a creator, I leave the tab open for 30 minutes; the Space does not
   sleep because the Cloudflare Worker keeps pinging it.

## 5. Functional Requirements

| # | Capability | Source / API |
|---|------------|--------------|
| F1 | Download video from URL | `yt-dlp` CLI (subprocess) |
| F2 | Accept TikTok, Instagram, Threads, YouTube | `yt-dlp` site coverage |
| F3 | Vertical 1080x1920 output | `ffmpeg` filter graph |
| F4 | Transitions: whip_pan, slide, fade, zoom, pixelize, circle_open | `ffmpeg xfade` |
| F5 | Optional effects: motion blur, camera shake, chromatic aberration, film grain, flash frame | `ffmpeg` filters |
| F6 | Chat assistant (plan / hook / hashtags) | 9router (`https://arkydar-9router.hf.space/v1`, model `utama`) |
| F7 | Send MP4 to Telegram chat | Telegram Bot API (`sendVideo`) |
| F8 | Keep-alive ping | Cloudflare Worker → HF Space `/` |
| F9 | Configurable transitions and duration | Gradio dropdown + env vars |
| F10| Self-hosted on HF Space (Docker SDK) | Dockerfile + `app.py` |

## 6. Non-Functional Requirements

- Cold start: under 90 seconds on HF free CPU.
- Per-clip duration: capped at 120 s by default.
- File size: capped at 200 MB per source (yt-dlp `--max-filesize`).
- Secrets: never logged or echoed back to the UI.
- All tokens read from env (HF Space secrets in production, `.env` locally).

## 7. Architecture

```
+----------------+    +-----------------+    +----------------+
|  Gradio (UI)   |--->|  Pipeline (bg)  |--->|  yt-dlp -> dl/ |
|  app.py        |    |  ffmpeg concat  |    |  output/       |
+----------------+    +-----------------+    +----------------+
        |                      |                      |
        v                      v                      v
+----------------+    +-----------------+    +----------------+
| 9router chat   |    | Cloudflare ping |    | Telegram send  |
| (LLM)          |    | (keep-alive)    |    | (Bot API)      |
+----------------+    +-----------------+    +----------------+
```

## 8. Data Flow

1. User pastes 1..N URLs into the **Sources** tab.
2. Clicks **Download + Edit** in the **Edit** tab.
3. Background thread: `yt-dlp` downloads each clip into `work_dir/<session>/`,
   then `ffmpeg` concatenates with the chosen xfade transition into
   `output_dir/tvea_<session>_<ts>.mp4`.
4. User previews the video in the same tab; once satisfied, switches to
   **Send to Telegram** tab and clicks **Send**.
5. Bot API uploads the MP4 to the configured chat.
6. User can ask the assistant in the **AI Chat** tab at any point.

## 9. Configuration (env)

| Var | Purpose | Default |
|-----|---------|---------|
| `NINEROUTER_BASE_URL` | LLM base URL | `https://arkydar-9router.hf.space/v1` |
| `NINEROUTER_API_KEY`  | LLM bearer | (required for chat) |
| `NINEROUTER_MODEL`    | Model name | `utama` |
| `TELEGRAM_BOT_TOKEN`  | Bot token | (required for send) |
| `TELEGRAM_CHAT_ID`    | Target chat | (required for send) |
| `CLOUDFLARE_WORKER_URL` | Keep-alive target | (optional) |
| `WORK_DIR`            | Download dir | `/tmp/tvea_work` |
| `OUTPUT_DIR`          | Render dir | `/tmp/tvea_output` |
| `MAX_VIDEO_DURATION_S`| Per-clip cap | `120` |
| `DEFAULT_TRANSITION`  | Initial transition | `whip_pan_right` |

## 10. Deployment

1. Push to GitHub: `github.com/<owner>/tiktok-video-editor-asisten`.
2. Create a HF Space: `huggingface.co/new-space` → Docker → link the GitHub repo.
3. Add Space secrets for: `NINEROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`, `CLOUDFLARE_WORKER_URL`.
4. Deploy a Cloudflare Worker that returns `200 OK` on `GET /ping` and
   schedules a cron to hit the Space URL every ~5 minutes.
5. Smoke test: open the Space URL, paste one TikTok URL, click **Edit**, verify
   the output video appears.

## 11. Open Questions / v2

- Music: integrate AudioCraft / HeartMuLa for background tracks.
- Auto-captions: whisper.cpp inside the Docker image.
- Scheduling: post directly to TikTok via their Content Posting API.
- Multi-render: a small queue + status polling in the UI.
