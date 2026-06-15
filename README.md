---
title: TikTok Video Editor Assistant
emoji: 🎬
colorFrom: red
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
short_description: Edit vertical videos with ffmpeg + AI chat
---

# 🎬 TikTok Video Editor Assistant

Gradio app that downloads clips from TikTok / Instagram / Threads / YouTube,
edits them into a vertical (1080x1920) TikTok-ready MP4 with ffmpeg
transitions, chats with a 9router LLM assistant, and ships the result to a
Telegram channel.

Deployed as a **Hugging Face Space (Docker SDK)**. Source lives in
**GitHub** (auto-mirrored).

See [`docs/PRD.md`](docs/PRD.md) for the full product spec.

## Quick start (local)

```bash
git clone https://github.com/arkydarmalik-coder/tiktok-video-editor-asisten
cd tiktok-video-editor-asisten
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# system deps: ffmpeg + ffprobe
cp .env.example .env
# fill in NINEROUTER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
python app.py
```

Then open <http://localhost:7860>.

## Deploy to a new HF Space

1. Click **New Space** on <https://huggingface.co/new-space>.
2. **SDK**: Docker · **Hardware**: CPU basic (free) is enough for short clips.
3. **Repo source**: `arkydarmalik-coder/tiktok-video-editor-asisten` (this repo).
4. Once created, open **Settings → Variables and secrets** and add:
   - `NINEROUTER_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `CLOUDFLARE_WORKER_URL` (optional, for keep-alive)
5. The Space builds the Docker image and starts `python app.py` on port 7860.

## Cloudflare Worker keep-alive (optional)

A tiny Worker that returns `200 OK` on `GET /ping`. Schedule a cron
trigger every ~5 minutes to hit the Space URL. Without this, the HF
Space will cold-start after ~15 minutes of inactivity.

```js
// workers/tvea-ping.js
export default {
  async scheduled(event, env, ctx) {
    await fetch("https://<your-space>.hf.space/");
  },
  async fetch() {
    return new Response("pong", { status: 200 });
  },
};
```

## Project layout

```
.
├── app.py                    # Gradio UI + pipeline orchestration
├── Dockerfile                # python:3.11-slim + ffmpeg
├── requirements.txt
├── README.md
├── docs/PRD.md               # Full product spec
├── .env.example              # Template for local secrets
├── .gitignore
└── src/
    ├── config.py             # env-driven settings
    ├── downloader.py         # yt-dlp wrapper
    ├── ffmpeg_utils.py       # filter graphs + concat
    ├── nine_router.py        # OpenAI-compatible LLM client
    ├── telegram_sender.py    # Bot API client
    └── keepalive.py          # Cloudflare ping helper
```

## License

MIT
