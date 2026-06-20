# Poolparty Playlist Project — Working Notes

Spotify playlists for a poolside party. **70 German guests**, mixed generations:
few grandparents (70–80), many parents (40–65), many students (born 2000–05).
Explicit/original versions allowed. All code comments in English.

**Timeline (user DJs by manually switching playlists):**
- Chill **16:00–20:00** · Party **20:00–23:00** · Ausklang **23:00–02:00**
- German **Nachtruhe at 22:00** → loud peak must land 20:00–22:00; switch to
  Ausklang ~22:00 (flexible to 23:00). Each playlist has +1–2 h buffer.

## Files
- **`tracklist.yaml`** — curated source of truth. Per track: `{artist, title, gen, energy 1-5}`;
  optional `tbd: true` (skip). `overrides:` maps `"Artist|Title"` → `spotify:track:URI`
  for forced/ambiguous tracks. Phases: "Chill 16-20", "Party 20-23", "Ausklang 23-02".
- **`build_playlist.py`** — searches Spotify, writes `report.csv`, creates the 3 base playlists.
  `--dry-run`, `--phase chill|party|ausklang`. Caches search hits in `.track_cache.json`.
- **`sort_playlist.py`** — reorders into an energy arc using ReccoBeats energy+BPM.
  - `--playlist <url>` sorts one existing playlist → new sorted playlist.
  - `--build2` rebuilds the whole curated+sorted **2.0 set** from `tracklist.yaml`.
  - `--shape arc|peak|winddown` · `--signal dance|energy|curated` · `--dry-run`
  - Caches ReccoBeats features in `.audio_cache.json`.
- **`harvest_chill.py`** — mines the user's own playlists (Oldies/Calm/Calm morning Beats)
  + AnnenMayKantereit, classifies candidates by ReccoBeats energy into chill/ausklang/skip,
  ranks by popularity, writes `harvest_report.csv`. Read-only against Spotify.
- Gitignored: `.env`, `.cache`, `.track_cache.json`, `.audio_cache.json`, `report.csv`, `sort_report.csv`.
- Run with **uv**: `uv run python sort_playlist.py --build2`. Set `$env:PYTHONIOENCODING="utf-8"` on Windows.

## Spotify API gotchas (verified 2026-06)
- **Audio features deprecated**: `/v1/audio-features` returns 403 for new apps. No native replacement.
- **ReccoBeats** is the free, no-auth replacement, queried by exact Spotify track ID:
  1. `GET https://api.reccobeats.com/v1/track?ids=<spotify_ids>` → maps Spotify ID → ReccoBeats id
     (the `href` field carries the Spotify ID). **Must send a `User-Agent` header** or urllib gets 403.
  2. `GET https://api.reccobeats.com/v1/audio-features?ids=<reccobeats_ids>` → real `energy`, `tempo`(BPM),
     `danceability`, `valence`. Batch cap ~20 ids (40+ → 500); fallback single `GET /v1/track/{rid}/audio-features`.
  - Tempos > 190 are usually double-time errors → folded to half in `sort_playlist.py`.
  - "energy" = sonic loudness, NOT crowd-hype. Party uses a **danceability blend**; Chill/Ausklang use sonic energy.
- **Feb-2026 Web API migration** (spotipy expects old schemas):
  - Playlist items nest the track under `item["item"]`, NOT `item["track"]` (now a bool). Read both.
  - Full playlist object exposes `items`, not `tracks`.
  - Create playlists via `sp._post("me/playlists", payload=...)`; `user_playlist_create` 403s.
  - **More 403s for new apps**: `sp.tracks`/`sp.track` (`/v1/tracks`), `sp.artist_top_tracks`.
    Playlist items no longer carry `popularity` either → get popularity from **ReccoBeats**
    (`/v1/track` returns it). `sp.search` limit is capped at **20** (50 → 400 Invalid limit).
- Re-auth note: `sort_playlist.py` added scope `playlist-read-private` → delete `.cache` to re-authorize.
- A new Spotify dev app resets the daily rate limit (the original app hit a ~24h 429 lock during dev).

## Current state — "Poolparty 2.0" set is LIVE on the account
Originals (Chill/Party/Ausklang, unsorted) are untouched. The 2.0 set:

| Playlist | Tracks | Sort | Avg BPM jump |
|---|---|---|---|
| Poolparty · Chill · 2.0 (16-20 Uhr)  | 126 | peak (gentle rise), signal=energy | 2.6 |
| Poolparty · Party · 2.0 (20-23 Uhr)  | 111 | peak (ends hot), signal=dance | **2.4** (best) |
| Poolparty · Ausklang · 2.0 (23-02 Uhr) | 58 | winddown (descending), signal=energy | 26.3 |

**Chill/Ausklang extended (+~3 h)** by harvesting the user's Oldies/Calm/Calm morning Beats
playlists + AnnenMayKantereit via `harvest_chill.py`: +48 Chill, +11 Ausklang, classified by
ReccoBeats energy (≥0.72 skip, ≤0.32 ausklang, else chill), ranked by popularity. Exact URIs
pinned as `overrides`. AMK "Can't Get You Out of My Head" cover isn't on Spotify (Kylie original
used instead); a few clear misfits dropped (Manson–Sweet Dreams, 9-min November Rain, Oliver
Anthony–Rich Men, deep Pink Floyd cuts).

`--build2` is **idempotent**: it replaces the contents of an existing same-named playlist
in place (no duplicates), so re-run it freely after editing `tracklist.yaml`.

**ABBA** (crowd favourite): 13 songs in Party (Dancing Queen, Mamma Mia, Waterloo, Gimme!,
Voulez-Vous, Does Your Mother Know, Money Money Money, Take a Chance on Me, Super Trouper,
Knowing Me Knowing You, SOS, Lay All Your Love on Me, Honey Honey) + 4 ballads in Ausklang
(The Winner Takes It All, Chiquitita, Fernando, I Have a Dream). All full ReccoBeats coverage.

**Playlist IDs:** Party `1a00DeRVq4ftH2asLpVaTd`, Ausklang `0017OMyqkPlVCvqMHhztMI`,
Chill `0BDd1pmVhcqVfbOAVC4kka` (these are the **originals**; the 2.0 set has new IDs).

### Curation changes baked into 2.0
- **Rammstein – Zeit** moved Party → Ausklang (too slow for the dance peak; now opens the wind-down).
- **30 cross-playlist duplicates auto-resolved** by nearest energy target
  (Chill 0.45 / Party 0.85 / Ausklang 0.30). No song repeats across the night.
  Auf uns→Ausklang, AMK ballads→Ausklang, Wanda/Madsen/Wir-sind-Helden→Chill, Take On Me→Party, etc.
- Bad BPM data corrected (Leaves Are Falling 218→109 via half-time fold).

### Known caveats
- Party 2.0 = the standout (near-perfect coverage, silky transitions).
- Ausklang 2.0 prioritizes energy descent over BPM (21.5 jump is expected). 43 tracks (~2.5 h) is a
  touch short if started at 22:00 — could pad.
- Chill/Ausklang have ReccoBeats coverage gaps (indie/electronic German tracks placed by curated 1–5 energy).

### ReccoBeats misses (placed by curated energy → user should sanity-check placement)
- **Chill (13):** Dua Lipa–Don't Start Now, Gestört aber GeiL–Unter meiner Haut, Gestört aber GeiL–Ich & Du,
  Major Lazer–Lean On, Alle Farben–Bad Ideas, Giant Rooks–Wild Stare, Josh.–Cordula Grün, Coldplay–Viva la Vida,
  Maroon 5–Sugar, Provinz–Walzer, Bilderbuch–Maschin, Mark Forster–Au revoir, Seeed–Augenbling
- **Party (7):** Mr. President–Coco Jamboo, Nena–99 Luftballons, Fettes Brot–Jein, Seeed–Ding,
  Fettes Brot–Emanuela, DJ Ötzi–Ein Stern, Timmy Trumpet–Freaks
- **Ausklang (11):** Andreas Bourani–Auf uns, Oasis–Wonderwall, Oasis–Don't Look Back in Anger,
  Provinz–Reicht dir das, Giant Rooks–Watershed, Bilderbuch–Bungalow, Fettes Brot–Bettina, Beginner–Füchse,
  Dua Lipa–Levitating, Philipp Poisel–Eiserner Steg, Philipp Poisel–Wie soll ein Mensch das ertragen

## Outstanding / TBD
- 7 `tbd: true` tracks in YAML (title unknown, skipped): Paul Wetz–Gib mir die Sonne, Yu–Zu nah,
  Schmyt–Bitte mach das Licht aus, Giant Rooks–Springtime Sehnsucht, Montez–Tanz mit mir.
- Changes not yet committed to git (new `sort_playlist.py`, edits to `tracklist.yaml`/`build_playlist.py`/`.gitignore`).
