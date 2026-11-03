# Podshelf

Self-hosted podcast downloader built for Plex and Emby. It picks up the useful parts of PodFetch and Audiobookshelf and fixes the problems they have with downloads, tags and the UI.

## Features

- **Reliable downloads**: the queue is stored in the database, so it survives restarts. Downloads stream to a `.part` file and are checked for completeness. Failed downloads retry with backoff (1, 2, 4, 8 min…) and are listed under Downloads with the error.
- **Per-podcast rules** (like ABS): keep N newest episodes (0 = keep all), check interval in hours, how many to download when subscribing, max new episodes per check, auto download on/off.
- **Pinning**: episodes you download by hand are kept even when retention would delete them. You can pin or unpin any episode.
- **Plex/Emby tags**: album, artist and album artist are all set to the podcast title, the track number is the chronological episode number, artwork is embedded (episode image, or the podcast cover as fallback), disc, compilation and year tags are removed. The file date is set to the publish date. `cover.jpg` goes in every podcast folder.
- **Episode browser**: filters, search, sorting, descriptions, download, delete, pin, and an in-browser player.
- **Add podcasts** by searching Apple Podcasts, pasting a feed URL, importing OPML, or **importing straight from Audiobookshelf** (keep count, schedule and max new are copied over). Files that are already in the folder are adopted (renamed and tagged) instead of downloaded again.
- **Fix tags & covers** button to rewrite all files of a podcast.
- **Played status**: mark episodes as played, "Unplayed" filter, unplayed counter per podcast. Synced both ways with **Plex and Emby**; optional "delete played episodes after X days".
- **Download rules** per podcast: include/exclude title filters (plain words or `re:` regex), minimum/maximum length, keep for X days.
- **Ad skipping**: cut X seconds from start/end and remove chapters named like ads (Werbung, Anzeige, Sponsor…), without re-encoding (ffmpeg).
- **Chapters** from Podlove and Podcasting 2.0 feeds are embedded into mp3/m4a and shown in the episode browser (click to jump).
- **Download window** and a global **bandwidth limit**.
- **Premium feeds** with username/password (sent only to the feed's own server).
- **Separate folders/albums** for seasons and bonus episodes, **tags/collections**, editable episode titles and numbers, display name per podcast.
- **Statistics**, **duplicate finder** (new duplicates are skipped automatically), **backup and restore** of the whole database.
- Synthwave theme, login, works on mobile.

## Folder layout

```
/podcasts/Kein Mucks/cover.jpg
/podcasts/Kein Mucks/2026-10-29 - Der Fall Mona Lisa.mp3   (track 214)
```

## Deploy (GitHub + GHCR)

1. Create a new GitHub repo named `podshelf` and push the contents of this folder:
   ```bash
   git init && git add . && git commit -m "Podshelf"
   git branch -M main
   git remote add origin https://github.com/YOUR_GITHUB_USER/podshelf.git
   git push -u origin main
   ```
2. The workflow under **Actions** builds `ghcr.io/YOUR_GITHUB_USER/podshelf:latest` (amd64 + arm64).
3. Make the image public: GitHub → your profile → **Packages** → podshelf → Package settings → Change visibility → Public. (Or keep it private and add GHCR credentials in TrueNAS.)
4. On TrueNAS create the config folder `/mnt/Seagate/Daten/Config/Podshelf` and give user/group `apps` (568) access, like you did for the Podcasts folder.
5. Apps → Discover Apps → ⋮ → **Install via YAML**, name `podshelf`, paste `docker-compose.yml` (with your user name and password filled in).
6. Open `http://192.168.178.100:30080`.

To update: push to GitHub, wait for the build, then click **Update** on the app in TrueNAS (or pull the image again).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / empty | Login. An empty password turns the login off |
| `DATA_DIR` | `/data` | Database, cache, session key |
| `PODCAST_DIR` | `/podcasts` | Download target |
| `MAX_ATTEMPTS` | `5` | Download attempts before an episode is marked failed |
| `HTTP_TIMEOUT` | `60` | Seconds |
| `TZ` | `Europe/Berlin` | Time zone |

## Plex / Emby

- **Plex**: Music library pointing at the Podcasts folder, "Prefer local metadata" on, album view. Each podcast is one album, sorted by track number.
- **Emby**: Music or Audiobook library pointing at the same folder. Embedded artwork and `cover.jpg` are picked up.

After switching from ABS or PodFetch, run **Fix tags & covers** once per podcast (or import from ABS, which does this automatically), then refresh the metadata of the library in Plex.
