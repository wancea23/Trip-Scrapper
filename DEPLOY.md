# Deploying Trip-Scrapper live (Render free tier)

The app is a normal Python web server (`web.py`). It reads `PORT` from the environment and
binds `0.0.0.0` automatically, so it runs unchanged on Render / Railway / Fly.io.

There are two ways. **Option B (Railway) is easiest — no GitHub needed.**

---

## Option A — Render (via GitHub)

### What you need
- A free **GitHub** account, a free **Render** account (https://render.com).

### Step 1 — put just this folder in its own GitHub repo
This folder lives *inside* your bigger `Code` git repo, so don't `git init` here (it would nest).
Instead copy it out to a standalone folder first, then push that:

```bash
# from a terminal, anywhere outside the Code repo:
cp -r "/c/Users/johns/OneDrive - Technical University of Moldova/Code/Python/Trip-Scrapper" ./trip-scrapper
cd trip-scrapper
git init && git add . && git commit -m "Trip-Scrapper"
# create an EMPTY repo on github.com first (e.g. "trip-scrapper"), then:
git remote add origin https://github.com/<your-username>/trip-scrapper.git
git branch -M main && git push -u origin main
```

> 🔐 Token note: `config.json` contains your Travelpayouts token. If the GitHub repo is **public**,
> replace it with the placeholder `PUT_YOUR_TRAVELPAYOUTS_TOKEN_HERE` and use the Render env var
> (Step 2) instead. If the repo is **private**, leaving it is fine.

### Step 2 — create the Render web service
1. Go to https://dashboard.render.com → **New** → **Web Service**.
2. Connect your GitHub and pick the `trip-scrapper` repo.
3. Render auto-detects Python from `requirements.txt`. Confirm:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `python web.py`
   - **Instance type:** Free
4. Under **Environment**, add these variables:
   - `TRAVELPAYOUTS_TOKEN` = your token (the one from config.json / token.txt)
   - `TELEGRAM_BOT_TOKEN` = the token from your local **`tg_token.txt`** — **required for the
     Telegram bot to work on the live site.** `tg_token.txt` is gitignored, so it is NOT
     deployed; without this env var the bot is silently OFF on the host.
5. Click **Create Web Service**.

(There's a `render.yaml` in the repo, so you can also use Render's **Blueprint** flow, which fills
all of the above in automatically — just add the token in the dashboard.)

### Step 3 — open your live URL
Render gives you `https://trip-scrapper-XXXX.onrender.com`. Open it — same UI as local.

---

## Option B — Railway (no GitHub, deploy straight from this folder)
Needs Node/npm (you have it). From inside this `Trip-Scrapper` folder:

```bash
npm i -g @railway/cli
railway login            # opens browser once
railway init             # name the project
railway up               # uploads THIS folder and builds it
railway variables set TRAVELPAYOUTS_TOKEN=<your-token>
railway variables set TELEGRAM_BOT_TOKEN=<your-bot-token>   # from tg_token.txt (bot on the live site)
railway domain           # gives you a public https URL
```

Railway auto-detects Python from `requirements.txt` + `Procfile`. That's the whole deploy.

## Telegram bot on the live site
- Set **`TELEGRAM_BOT_TOKEN`** (same value as your local `tg_token.txt`) in the host's env vars.
  Without it the bot is OFF on the host — that's the usual "the bot doesn't work when deployed".
- The app **keeps itself awake** by pinging its own URL every 10 min (`RENDER_EXTERNAL_URL` /
  `RAILWAY_PUBLIC_DOMAIN`, or set `PUBLIC_URL`), so the polling bot keeps replying and sending
  price-drop alerts instead of dying on the free tier's 15-min sleep.
- Run the bot in **only one place at a time** (either the live host OR your PC, not both) — Telegram
  lets a single poller consume updates; two at once conflict and neither works reliably.

## Things to know about the free tier
- **It sleeps after ~15 min idle** → the first visit after a nap takes ~50s to wake up. The built-in
  keep-alive ping mitigates this so the bot stays live; a small always-on host is still more reliable.
- **The price-history database resets on each redeploy** (ephemeral disk). The live search/compare
  don't need it; only the long-term `--history` watcher does.
- **Airbnb scraping from a cloud IP may occasionally be blocked/rate-limited.** Flights always work;
  if stays come back empty, that's the cause. (This was the trade-off vs. running locally.)

## Updating later
Push to the GitHub repo and Render auto-redeploys:
```bash
git add . && git commit -m "update" && git push
```
