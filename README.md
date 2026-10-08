# Twitch → PS5 pick tracker

Every 30 minutes, GitHub Actions checks the top 6 categories on Twitch. The **pick** is the
highest-ranked one that:

- is a game that's on **PS5**
- is **not free-to-play**
- was **released this year**, counting either its first release on any platform or its PS5 release

If the pick changes, you get a Telegram message. If nothing in the top 6 qualifies, the
previous pick stays. Every check is saved, and a web page shows the history.

## One-time setup (about 10 minutes)

### 1. Twitch API key (also covers IGDB)
1. Go to https://dev.twitch.tv/console/apps and log in. Twitch requires two-factor authentication to be on.
2. **Register Your Application**: any name, OAuth Redirect URL `http://localhost`, category "Other", client type "Confidential".
3. Open the app. Copy the **Client ID**, then click **New Secret** and copy the **Client Secret**.

### 2. Telegram bot
1. In Telegram, message **@BotFather**, send `/newbot` and follow the steps. Copy the **bot token**.
2. Open a chat with your new bot and send it any message (for example "hi").
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find
   `"chat":{"id":123456789,...}`. That number is your **chat ID**.

### 3. GitHub
1. Create a new **public** repository on GitHub. Public repos get unlimited free Actions
   minutes and free GitHub Pages. Your keys stay private either way, because they're stored as secrets.
2. Push this folder to it:
   ```
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. Go to repo **Settings → Secrets and variables → Actions → New repository secret** and add four secrets:
   `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
4. Go to **Settings → Pages**. Under Source, choose "Deploy from a branch", then branch `main`, folder `/docs`.
   Your history page will be at `https://<you>.github.io/<repo>/`.
5. Go to **Actions → track → Run workflow** to run it once right away. After that it runs by itself.

## Fixing a wrong guess

The free-to-play check uses IGDB's "free-to-play" tag and Steam's `is_free` flag. If a game
gets it wrong, add its Twitch category name to `config.json`:

```json
{ "force_free": ["Some F2P Game"], "force_paid": ["Some Paid Game"], "ignore": ["Something to skip"] }
```

You can edit the file directly on github.com. The next run uses the new settings.

## Files
- `tracker.py`: the script (Python standard library only)
- `data/snapshots.jsonl`: one line per check, the full history
- `data/state.json`: the current pick and the pick timeline
- `data/games.json`: cached game info (refreshed daily)
- `docs/index.html` + `docs/data.json`: the history page

## Notes
- GitHub sometimes starts scheduled runs 5–15 minutes late. That doesn't matter here.
- The year is based on UTC, so on New Year's Eve the switch happens at midnight UTC.
- A game only counts once its release date has passed, so upcoming releases aren't picked.
- To test Telegram on your own machine, set the two Telegram variables and run `python tracker.py --test-telegram`.
