# Reel Cookbook

A self-hosted recipe book for the recipes you save on Instagram and TikTok.

Paste a link to a Reel, TikTok or photo post, and Reel Cookbook downloads it, keeps the caption, transcribes what's said in the video, saves the cover and a few still frames, and reads any recipe website the caption links to. Claude then writes it up as a proper recipe, with ingredients, method, tags and per-serving macros, and saves it as a draft for you to check. Videos are deleted straight away; only the text and a few pictures are kept.

It also:

- imports recipes from **recipe websites** (paste the link) and **screenshots** (from an app, a cookbook, anything),
- bulk-imports **every post you've saved** from an Instagram or TikTok data export,
- lets you **search, filter and sort** by cuisine, meat, tags, protein and calories,
- gives you a **tick-list of ingredients** to copy into your shopping app,
- has **logins for everyone in the household**, and works as a home-screen app on iPhone and Android.

Everything runs on your own computer, and your recipes stay in one folder on it.

## What you need

- A computer that stays on (a home server, NAS, Raspberry Pi 4/5 or an old PC) with **Docker** installed ([Get Docker](https://docs.docker.com/get-docker/)).
- About 2 GB of disk space, plus room for your recipes' pictures.
- **Recommended:** a way for Claude to write the recipes for you. Either:
  - a **Claude Pro or Max subscription** (from [claude.ai](https://claude.ai)): it uses your plan's allowance, with nothing extra to pay, or
  - an **API key** from the [Claude Console](https://platform.claude.com): pay as you go, a few cents per post.

  Without either, posts are still downloaded and transcribed, and you write each recipe up yourself from the caption, transcript and pictures. See [Connect Claude](#connect-claude).

## Quick start

```
git clone https://github.com/<you>/reelcookbook.git
cd reelcookbook
cp .env.example .env
nano .env                  # set your TIMEZONE
docker compose up -d
```

Then open **http://\<the computer's IP address\>:8095** on your phone or computer. The first visit asks you to create an account. Then go to **Settings → Claude** to [connect Claude](#connect-claude), and add everyone else in **Settings → Household**.

The first video import takes a minute longer than the rest, while the speech-to-text model downloads (about 140 MB, once).

## Everyday commands

Run these in the `reelcookbook` folder.

| What | Command |
|---|---|
| Start | `docker compose up -d` |
| Stop | `docker compose down` |
| Is it running? | `docker compose ps` (look for "Up" and "healthy") |
| See what it's doing | `docker compose logs --tail 50` (add `-f` to keep watching) |
| Apply a change to `.env` | `docker compose up -d` |
| Update to the latest version | `git pull && docker compose up -d --build` |

It starts by itself when the computer boots.

## Connect Claude

In the app: **Settings → Claude**. Pick one, paste in the token or key, and tap **Save and test**: it checks it works straight away.

### With a Claude subscription (recommended)

You need a paid Claude plan (Pro or Max). You make a sign-in token once; it lasts a year and only lets Reel Cookbook ask Claude questions (it can't see your chats or change your account).

1. On the computer running Reel Cookbook, in the `reelcookbook` folder, run:
   ```
   docker compose exec reelcookbook claude setup-token
   ```
2. It shows a link. Open it in any browser (your phone is fine), sign in to Claude and tap **Authorize**.
3. Claude shows a code. Paste it back into the terminal and press Enter.
4. The terminal prints your token, starting `sk-ant-oat01-`. Copy all of it into **Settings → Claude → Subscription token**.

(If you already have [Claude Code](https://claude.com/claude-code) on another computer, `claude setup-token` there works too.)

Each post uses some of your plan's allowance, like a short chat with pictures. Importing hundreds of saved posts at once can reach your plan's usage limit; Reel Cookbook then pauses and carries on by itself when the limit resets. Use your own Claude account: don't share a token with another household.

### With an API key

1. Go to the [Claude Console](https://platform.claude.com) and sign up or log in. (It's separate from a claude.ai subscription.)
2. Open **Billing** and add some credit. $5 goes a long way: each post costs a few cents with the default model.
3. Open **API keys → Create key**, name it "Reel Cookbook" and copy it. It starts `sk-ant-api03-` and is only shown once.
4. Paste it into **Settings → Claude → API key**.

### Model and units

Also in Settings → Claude: **Opus 5.5** writes the best recipes; **Sonnet 5.5** and **Haiku 5.5** use less of your plan or credit. Quantities can be metric (g, ml, °C) or US (cups, oz, °F).

## Adding recipes

- **One post:** in Instagram or TikTok, tap **Share → Copy link**, then open Reel Cookbook → **Import** and paste it. On Android, once the app is on your home screen, you can share a post straight to Reel Cookbook instead.
- **A recipe website:** Recipes → **+ Add**, and paste the link.
- **Screenshots:** Import → From screenshots. Up to 10 pictures of one recipe.
- **Everything you've saved:** download your data from Instagram or TikTok (the Import page has the steps) and upload the file. Not every saved post is a recipe; Claude skips the ones that aren't. Instagram posts are downloaded about two minutes apart so your account isn't flagged, so a big list takes a while.

Recipes written by Claude show **Draft** until someone taps **Looks good** or edits them. Estimated macros are marked as estimates.

### Instagram needs your login

Instagram only lets logged-in accounts download posts. TikTok doesn't need this.

1. On a computer, log in to instagram.com in Chrome or Firefox.
2. Install the browser extension **"Get cookies.txt LOCALLY"**.
3. On instagram.com, open the extension and choose **Export**. You get a file like `www.instagram.com_cookies.txt`.
4. In Reel Cookbook: Import → Instagram login → upload it.

It's stored privately in `data/secrets/`. Treat it like a password. It stops working if you log out of Instagram in that browser; just upload a new one.

## Put it on your phone's home screen

- **iPhone:** open the app in **Safari**, tap Share → **Add to Home Screen**.
- **Android:** open it in Chrome, tap ⋮ → **Add to Home screen** (or **Install app**).

## Settings in `.env`

Claude is set up in the app. A few things that need a restart live in `.env`; run `docker compose up -d` after changing it.

| Setting | What it does |
|---|---|
| `TIMEZONE` | Your time zone, e.g. `Australia/Brisbane`, `Europe/London`, `America/New_York`. |
| `PORT` | The port the app is opened on (default 8095). |
| `APP_NAME` | The name shown in the app and under the home-screen icon. |
| `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `UNITS` | Optional: Claude settings, if you'd rather keep them here. Anything saved in the app takes priority. |

## Where things live

| Path | What it is |
|---|---|
| `data/` | **All your data**: the database, recipe photos, post pictures, the Instagram login and your Claude token or key. Back this up. |
| `data/reelcookbook.db` | The database (recipes, imports, accounts, and the Claude settings). |
| `data/recipe-images/` | Recipe photos. |
| `data/social/` | The covers, still frames and slides from imported posts. |
| `data/secrets/` | The Instagram login cookies. |
| `data/models/` | The speech-to-text model (downloaded again automatically if deleted). |
| `data/claude-code/` | Claude Code's settings folder (no conversations are kept). |
| `.env` | A few settings. Never share it or put it in git. |
| `app/` | The code. |

## Back up

The app is briefly stopped so the database is copied cleanly.

```
docker compose stop
tar czf ~/reelcookbook-backup-$(date +%Y-%m-%d).tar.gz data .env
docker compose start
```

Copy the file somewhere else (another computer, a NAS, cloud storage). It contains your Claude token or key and your Instagram login, so keep it private.

To restore: `docker compose down`, move the old `data` folder aside, `tar xzf` the backup in the `reelcookbook` folder, then `docker compose up -d`.

## Using it away from home

The app is meant for your home network. To use it on your phone when you're out, install [Tailscale](https://tailscale.com) (free) on the server and your phone, and open `http://<the server's Tailscale IP>:8095`. Don't open the port to the internet on your router.

## Troubleshooting

- **Every Instagram post fails with "wants a login":** upload a fresh cookies file (above).
- **Downloads fail for everything:** Instagram and TikTok change often. Update to the latest version (`git pull && docker compose up -d --build`); if that doesn't help, bump `yt-dlp` in `app/requirements.txt` to the [latest release](https://pypi.org/project/yt-dlp/#history) and run `docker compose up -d --build`.
- **The Import page says Claude didn't accept the token or key:** subscription tokens last a year. Make a new one ([above](#with-a-claude-subscription-recommended)) and paste it into Settings → Claude.
- **"Out of credit" (API key):** add credit at platform.claude.com → Billing, then tap **Write now** on the Import page.
- **"Usage limit reached" (subscription):** nothing to do. It carries on by itself when your plan's limit resets. Pick Sonnet or Haiku in Settings → Claude to make your allowance go further.
- **Forgotten password:** another household member can't reset it yet. Stop the app and delete that person's row, or ask whoever set it up.
- **Updating Claude Code:** it's pinned in `app/Dockerfile` (`CLAUDE_CODE_VERSION`). Change the number and run `docker compose up -d --build`.
- **`unable to open database file` in the logs:** the `data` folder belongs to root. Run `sudo chown -R $(id -u):$(id -g) data` and start again.

## How it works

A small Python web app (FastAPI and SQLite) in one Docker container:

- `app/reelcookbook/social.py`: the import queue. [yt-dlp](https://github.com/yt-dlp/yt-dlp) downloads each post, ffmpeg takes still frames and the audio, [faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes it on your own computer, and [recipe-scrapers](https://github.com/hhursev/recipe-scrapers) reads any linked recipe site.
- `app/reelcookbook/writer.py`: sends each downloaded post (text plus pictures) to Claude in one request, and gets the recipe back in a fixed shape. With a subscription it runs [Claude Code](https://claude.com/claude-code) (installed in the container) with every tool switched off: no files, no shell, no web; with an API key it calls the Claude API.
- `app/reelcookbook/recipes.py`: the recipe pages, website import, tags, macros and filters.

Only the post's text and pictures (and the titles of your saved recipes, so Claude can spot duplicates) are sent to Anthropic, and only when Claude is connected.

## Credits

Fonts: [Instrument Serif](https://fonts.google.com/specimen/Instrument+Serif) and [Plus Jakarta Sans](https://fonts.google.com/specimen/Plus+Jakarta+Sans), both under the SIL Open Font License.
