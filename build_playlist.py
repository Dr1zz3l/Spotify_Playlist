#!/usr/bin/env python3
"""
Build Spotify playlists for the poolparty from tracklist.yaml.

Usage:
    uv run python build_playlist.py            # build all three playlists
    uv run python build_playlist.py --dry-run  # search only, no playlists created
    uv run python build_playlist.py --phase chill|party|ausklang
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
import yaml

SCOPE = "playlist-modify-public playlist-modify-private"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
TRACKLIST_FILE = "tracklist.yaml"
REPORT_FILE = "report.csv"
TRACK_CACHE_FILE = ".track_cache.json"

# Clock time each phase starts at – used to project hh:mm positions in the report
PHASE_START_TIMES = {
    "Chill 16-20":    "16:00",
    "Party 20-23":    "20:00",
    "Ausklang 23-02": "23:00",
}

# Map --phase argument to the phase name used in tracklist.yaml
PHASE_FILTER = {
    "chill":    "Chill 16-20",
    "party":    "Party 20-23",
    "ausklang": "Ausklang 23-02",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    """Lowercase + collapse whitespace for loose string comparison."""
    return " ".join(s.lower().split())


def is_unwanted_version(track_name: str, want_live: bool) -> bool:
    """Return True when the track looks like a live/karaoke/tribute recording."""
    if want_live:
        return False
    keywords = ("live", "karaoke", "instrumental", "tribute", "acoustic version", "re-recorded")
    low = track_name.lower()
    return any(k in low for k in keywords)


def fmt_mmss(ms: int) -> str:
    """Format milliseconds as M:SS."""
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def fmt_hhmmss(seconds: int) -> str:
    """Format total seconds as H:MM:SS or M:SS."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def add_clock_time(start: str, seconds: int) -> str:
    """Return the clock time after adding `seconds` to a HH:MM start (wraps at midnight)."""
    h, m = map(int, start.split(":"))
    total = (h * 3600 + m * 60 + seconds) % 86400
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"


# ── Search result cache (avoids re-querying Spotify on repeated runs) ────────

def load_cache() -> dict:
    try:
        return json.loads(Path(TRACK_CACHE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    Path(TRACK_CACHE_FILE).write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Spotify search ───────────────────────────────────────────────────────────

def search_track(sp: spotipy.Spotify, artist: str, title: str, cache: dict) -> dict | None:
    """
    Search Spotify for the best-matching track.
    Strategy: try quoted search first; fall back to unquoted.
    Scoring: exact artist match > title match > popularity; penalise live/karaoke.
    Returns a Spotify track dict or None when nothing usable is found.
    """
    cache_key = f"{artist}|{title}"
    if cache_key in cache:
        return cache[cache_key]  # None is stored for known misses

    want_live = "live" in title.lower()
    norm_artist = normalize(artist)
    norm_title = normalize(title)

    queries = [
        f'track:"{title}" artist:"{artist}"',
        f'track:"{title}"',
        f"{artist} {title}",
    ]

    candidates: list[dict] = []
    for q in queries:
        try:
            res = sp.search(q=q, type="track", limit=10)
            items = (res.get("tracks") or {}).get("items") or []
            if items:
                candidates = items
                break
        except Exception:
            continue

    if not candidates:
        return None

    # Significant words in the wanted artist name (skip tokens <= 2 chars like "&")
    want_words = {w for w in norm_artist.split() if len(w) > 2}

    def artist_word_overlap(track_artists: list[str]) -> bool:
        """Return True when at least one significant word is shared with the wanted artist."""
        if not want_words:
            return True  # single short token (e.g. "U2") — skip guard
        all_result_words = {w for a in track_artists for w in a.split() if len(w) > 2}
        return bool(want_words & all_result_words)

    def score(t: dict) -> float:
        track_artists = [normalize(a["name"]) for a in t["artists"]]
        track_title = normalize(t["name"])
        artist_exact = any(norm_artist in a or a in norm_artist for a in track_artists)
        artist_overlap = artist_word_overlap(track_artists)
        title_match = norm_title in track_title or track_title in norm_title
        unwanted = is_unwanted_version(t["name"], want_live)
        return (
            int(artist_exact) * 100
            + int(artist_overlap) * 40
            + int(title_match) * 50
            + (t.get("popularity") or 0)
            - int(unwanted) * 200
        )

    candidates.sort(key=score, reverse=True)
    best = candidates[0]

    # Reject if no significant artist word overlaps — avoids wrong-artist false positives
    best_artists = [normalize(a["name"]) for a in best["artists"]]
    if not artist_word_overlap(best_artists):
        return None

    result = best if score(best) >= 50 else None
    cache[cache_key] = result  # store hit or None-miss
    return result


# ── Phase builder ─────────────────────────────────────────────────────────────

def build_phase(
    sp: spotipy.Spotify,
    user_id: str,
    phase: dict,
    overrides: dict[str, str],
    cache: dict,
    dry_run: bool,
) -> list[dict]:
    """
    Search every track in `phase`, optionally create the playlist and add matched URIs.
    Returns a list of result rows for the CSV report.
    """
    phase_name   = phase["name"]
    playlist_name = phase["playlist_name"]
    start_time   = PHASE_START_TIMES.get(phase_name, "00:00")
    tracks       = phase.get("tracks", [])

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"\n{prefix}-- {phase_name} ({len(tracks)} tracks) --")

    uris: list[str] = []
    results: list[dict] = []
    cumulative_s = 0

    for t in tracks:
        artist = t.get("artist", "")
        title  = t.get("title", "")
        gen    = t.get("gen", "")

        # Placeholder / TBD entries
        if t.get("tbd") or not title:
            print(f"  SKIP (TBD)  {artist} — {title or '???'}")
            results.append(_row(phase_name, gen, artist, title, status="TBD"))
            continue

        override_uri = overrides.get(f"{artist}|{title}")

        if override_uri:
            # Fetch track metadata so the report has accurate info
            try:
                info = sp.track(override_uri)
                status       = "OVERRIDE"
                uri          = override_uri
                duration_ms  = info["duration_ms"]
                matched_name = info["name"]
                matched_artist = info["artists"][0]["name"]
                popularity   = info.get("popularity", 0)
            except Exception as exc:
                print(f"  ERROR  fetching override '{override_uri}': {exc}")
                results.append(_row(phase_name, gen, artist, title, status="OVERRIDE_ERROR"))
                continue
        else:
            match = search_track(sp, artist, title, cache)
            save_cache(cache)  # persist immediately so progress survives interruption
            if match:
                uri          = match["uri"]
                duration_ms  = match["duration_ms"]
                matched_name = match["name"]
                matched_artist = match["artists"][0]["name"]
                popularity   = match.get("popularity", 0)
                status       = "OK"
            else:
                print(f"  UNMATCHED   {artist} — {title}")
                results.append(_row(phase_name, gen, artist, title, status="UNMATCHED"))
                continue

        cumulative_s += duration_ms // 1000
        projected = add_clock_time(start_time, cumulative_s)

        flag = "+" if status == "OVERRIDE" else " "
        print(f"  [{flag}] {artist} — {title}")
        print(f"       → {matched_artist} — {matched_name}  ({fmt_mmss(duration_ms)})  [{projected}]")

        uris.append(uri)
        results.append(_row(
            phase_name, gen, artist, title,
            matched_name=matched_name,
            matched_artist=matched_artist,
            uri=uri,
            popularity=popularity,
            duration=fmt_mmss(duration_ms),
            cumulative=fmt_hhmmss(cumulative_s),
            projected_time=projected,
            status=status,
        ))

    # Create playlist and add tracks (unless dry run)
    if not dry_run and uris:
        playlist = sp._post("me/playlists", payload={
            "name": playlist_name,
            "public": False,
            "description": f"Poolparty – {phase_name}",
        })
        pid = playlist["id"]
        for i in range(0, len(uris), 100):   # Spotify allows max 100 URIs per call
            sp.playlist_add_items(pid, uris[i:i + 100])
        print(f"\n  OK Created '{playlist_name}' with {len(uris)} tracks.")
    elif dry_run:
        print(f"\n  (dry run) would add {len(uris)} tracks to '{playlist_name}'")

    return results


def _row(phase, gen, artist, title, *, matched_name="", matched_artist="",
         uri="", popularity="", duration="", cumulative="", projected_time="", status="") -> dict:
    return {
        "phase":          phase,
        "gen":            gen,
        "artist":         artist,
        "title":          title,
        "matched_artist": matched_artist,
        "matched_name":   matched_name,
        "uri":            uri,
        "popularity":     popularity,
        "duration":       duration,
        "cumulative":     cumulative,
        "projected_time": projected_time,
        "status":         status,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def write_report(results: list[dict], path: str) -> None:
    fields = [
        "phase", "gen", "artist", "title",
        "matched_artist", "matched_name", "uri",
        "popularity", "duration", "cumulative", "projected_time", "status",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)
    print(f"\nReport written → {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Build Spotify poolparty playlists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Search only – do not create or modify any playlists")
    parser.add_argument("--phase", choices=list(PHASE_FILTER),
                        help="Build a single phase instead of all three")
    args = parser.parse_args()

    load_dotenv()
    missing = [k for k in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET") if not os.getenv(k)]
    if missing:
        print(f"Error: missing env vars: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your Spotify credentials.")
        sys.exit(1)

    auth_manager = SpotifyOAuth(
        scope=SCOPE,
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", REDIRECT_URI),
        open_browser=True,
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    user_id = sp.current_user()["id"]
    print(f"Authenticated as: {user_id}")

    data      = yaml.safe_load(Path(TRACKLIST_FILE).read_text(encoding="utf-8"))
    overrides = data.get("overrides") or {}
    phases    = data.get("phases", [])

    if args.phase:
        target_name = PHASE_FILTER[args.phase]
        phases = [p for p in phases if p["name"] == target_name]
        if not phases:
            print(f"Phase '{target_name}' not found in {TRACKLIST_FILE}")
            sys.exit(1)

    cache = load_cache()
    cached_count = sum(1 for v in cache.values() if v is not None)
    if cache:
        print(f"Cache loaded: {cached_count} hits / {len(cache)} entries ({Path(TRACK_CACHE_FILE).name})")

    all_results: list[dict] = []
    for phase in phases:
        rows = build_phase(sp, user_id, phase, overrides, cache, dry_run=args.dry_run)
        all_results.extend(rows)
        save_cache(cache)  # persist after each phase so progress survives interruption

    write_report(all_results, REPORT_FILE)

    # Summary
    by_status: dict[str, list] = {}
    for r in all_results:
        by_status.setdefault(r["status"], []).append(r)

    ok = len(by_status.get("OK", [])) + len(by_status.get("OVERRIDE", []))
    print(f"\nSummary: {ok} matched", end="")
    for bad in ("UNMATCHED", "TBD", "OVERRIDE_ERROR"):
        if by_status.get(bad):
            print(f"  ·  {len(by_status[bad])} {bad}", end="")
    print()

    if by_status.get("UNMATCHED"):
        print("\nUNMATCHED – add overrides to tracklist.yaml:")
        for r in by_status["UNMATCHED"]:
            print(f"  {r['artist']} — {r['title']}")


if __name__ == "__main__":
    main()
