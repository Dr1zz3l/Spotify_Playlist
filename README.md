# Poolparty Playlist Builder

Automatically creates three Spotify playlists (Chill / Party / Ausklang) for a 70-person poolside party via the Spotify Web API.

## Party schedule

| Playlist | Time | Duration | Buffer |
|---|---|---|---|
| Poolparty · Chill | 16:00 → 20:00 | 4 h | +1.5–2 h |
| Poolparty · Party | 20:00 → 23:00 | 3 h | +1.5–2 h |
| Poolparty · Ausklang | 23:00 → 02:00 | 3 h | +1.5–2 h |

Switch playlists manually at 20:00 and 23:00. Buffer songs only play if a phase runs over.

## Setup

### 1. Create a Spotify Developer app (~5 min)

1. Go to <https://developer.spotify.com/dashboard> and log in.
2. Click **Create app** — give it any name (e.g. "Poolparty").
3. Under **Redirect URIs** add: `http://127.0.0.1:8888/callback`
4. Tick **Web API**, then save.
5. Open **Settings** and copy your **Client ID** and **Client Secret**.

### 2. Configure credentials

```sh
cp .env.example .env
# Edit .env and paste your Client ID and Client Secret
```

### 3. Install dependencies

```sh
uv sync
```

### 4. Dry run (search & report — no playlists created)

```sh
uv run python build_playlist.py --dry-run
```

Open `report.csv` to review matched tracks, projected clock times, and any UNMATCHED entries.
Fix ambiguous titles in the `overrides` section of `tracklist.yaml` if needed.

### 5. Build playlists

```sh
uv run python build_playlist.py
```

A browser window opens for Spotify OAuth. After authorising, three private playlists appear in your account.

To rebuild a single phase only:

```sh
uv run python build_playlist.py --phase chill
uv run python build_playlist.py --phase party
uv run python build_playlist.py --phase ausklang
```

## Files

| File | Purpose |
|---|---|
| `tracklist.yaml` | Master song list — edit here to add/remove/reorder tracks |
| `build_playlist.py` | Auth, search, matching, playlist creation |
| `report.csv` | Generated after each run — match quality + projected clock times |
| `.env` | Your Spotify credentials (not committed) |
| `.cache` | OAuth token cache created by spotipy (not committed) |

## Ambiguous titles to confirm before building

After a dry run, check these in `report.csv` and add exact Spotify URIs to `overrides` in `tracklist.yaml` if the auto-match is wrong:

- **Reezy – Monster** (confirm the 2022 version)
- **Alle Farben – Leaves Are Falling** (confirm correct track)
- **"In meinem Benz"** (artist TBD — fill in `tracklist.yaml`)
- **Berq** (exact track titles TBD)
- **Alphaville – Forever Young** (must be the 1984 original, not a cover)
- **Wencke Myhre – Er hat ein knallrotes Gummiboot** (original 1970, not Dieter Thomas Kuhn cover)
