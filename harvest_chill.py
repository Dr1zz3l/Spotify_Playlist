#!/usr/bin/env python3
"""
Harvest Chill/Ausklang candidates from the user's own playlists + AnnenMayKantereit
top tracks, classify them by ReccoBeats energy, and write a ranked shortlist.

Read-only against Spotify (it never modifies playlists). Reuses the ReccoBeats
feature fetcher from sort_playlist.py. Output: harvest_report.csv + console summary.

Bands (by ReccoBeats energy):
    energy >= 0.72            -> skip   (too energetic for a chill/wind-down set)
    energy <= 0.32            -> ausklang candidate
    otherwise                 -> chill candidate

Usage: uv run python harvest_chill.py
"""

import csv
import json
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
import yaml

from sort_playlist import (
    fetch_audio_features, load_audio_cache, _norm, _recco_get, RECCO_BASE,
    TRACKLIST_FILE, TRACK_CACHE_FILE,
)

SCOPE = "playlist-modify-public playlist-modify-private playlist-read-private"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
REPORT = "harvest_report.csv"

SOURCES = {
    "Oldies": "5prU6soWUvzEAG3zn36dYW",
    "Calm": "2Hs8E27JJ8x4Y131unZooD",
    "Calm morning Beats": "3aem0Dv1bz6J85bqM2e6Ss",
}

E_SKIP = 0.72       # >= this = too energetic
E_AUSKLANG = 0.32   # <= this = wind-down
PER_ARTIST_CAP = 4  # variety: at most N per artist per band
AMK_CAP = 7         # the user explicitly wants more AnnenMayKantereit
TOP_CHILL = 50      # shortlist size per band (ranked by popularity)
TOP_AUSKLANG = 15


def fetch_popularity(ids: list[str]) -> dict:
    """
    Spotify strips popularity from playlist items and 403s /v1/tracks for new apps,
    but ReccoBeats returns the same popularity metric on its /v1/track endpoint.
    """
    pop: dict[str, int] = {}
    for i in range(0, len(ids), 40):
        d = _recco_get(f"{RECCO_BASE}/track?ids={','.join(ids[i:i + 40])}")
        for t in d.get("content", []):
            sid = t.get("href", "").rstrip("/").split("/")[-1]
            if sid:
                pop[sid] = t.get("popularity") or 0
    return pop


def track_from_item(item: dict) -> dict | None:
    """Extract the track dict from a playlist item (Feb-2026 schema uses 'item')."""
    tr = item.get("item")
    if not isinstance(tr, dict):
        tr = item.get("track")
    if not isinstance(tr, dict) or not tr.get("id"):
        return None
    return tr


def read_playlist(sp, pid: str) -> list[dict]:
    out, results = [], sp.playlist_items(pid, limit=100)
    while results:
        for item in results["items"]:
            tr = track_from_item(item)
            if tr:
                out.append(tr)
        results = sp.next(results) if results.get("next") else None
    return out


def candidate(tr: dict) -> dict:
    return {
        "id": tr["id"],
        "uri": tr["uri"],
        "artist": tr["artists"][0]["name"] if tr.get("artists") else "",
        "title": tr["name"],
        "popularity": tr.get("popularity", 0),
    }


def main() -> None:
    load_dotenv()
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        scope=SCOPE, redirect_uri=REDIRECT_URI, open_browser=True,
    ))

    # --- Exclusion set: everything already curated, by normalised name AND exact
    #     Spotify track ID (catches title-suffix variants like "- Original Version"
    #     and cross-artist same-recording overrides such as Komet). ---
    data = yaml.safe_load(Path(TRACKLIST_FILE).read_text(encoding="utf-8"))
    overrides = data.get("overrides") or {}
    tcache = json.loads(Path(TRACK_CACHE_FILE).read_text(encoding="utf-8"))
    have, have_ids = set(), set()
    for ph in data.get("phases", []):
        for t in ph.get("tracks", []):
            have.add(_norm(f"{t.get('artist','')} {t.get('title','')}"))
            key = f"{t.get('artist','')}|{t.get('title','')}"
            uri = overrides.get(key) or (tcache.get(key) or {}).get("uri")
            if uri:
                have_ids.add(uri.split(":")[-1])

    # --- Collect candidates from source playlists ---
    cands: dict[str, dict] = {}  # norm key -> candidate (keep highest popularity)
    for name, pid in SOURCES.items():
        tracks = read_playlist(sp, pid)
        print(f"  read '{name}': {len(tracks)} tracks")
        for tr in tracks:
            c = candidate(tr)
            key = _norm(f"{c['artist']} {c['title']}")
            if key in have or c["id"] in have_ids:
                continue
            if key not in cands or c["popularity"] > cands[key]["popularity"]:
                cands[key] = c

    # --- AnnenMayKantereit tracks via search (artist_top_tracks is 403 for new apps) ---
    amk_tracks = []
    try:
        res = sp.search(q='artist:"AnnenMayKantereit"', type="track", limit=20)
        items = (res.get("tracks") or {}).get("items", [])
        amk_tracks = [t for t in items
                      if t.get("artists") and _norm(t["artists"][0]["name"]) == "annenmaykantereit"]
    except Exception:
        pass
    try:  # ensure the Kylie cover is included
        res = sp.search(q='artist:AnnenMayKantereit Can\'t Get You Out of My Head',
                        type="track", limit=3)
        amk_tracks += (res.get("tracks") or {}).get("items", [])
    except Exception:
        pass
    for tr in amk_tracks:
        c = candidate(tr)
        key = _norm(f"{c['artist']} {c['title']}")
        if key in have or c["id"] in have_ids:
            continue
        if key not in cands or c["popularity"] > cands[key]["popularity"]:
            cands[key] = c

    candidates = list(cands.values())
    print(f"\n{len(candidates)} unique new candidates.")

    # --- Real popularity (playlist items no longer carry it) ---
    pop = fetch_popularity([c["id"] for c in candidates])
    for c in candidates:
        c["popularity"] = pop.get(c["id"], 0)

    # --- Audio features ---
    print("Fetching ReccoBeats features...")
    cache = load_audio_cache()
    feats = fetch_audio_features([c["id"] for c in candidates], cache)

    # --- Classify ---
    for c in candidates:
        f = feats.get(c["id"])
        c["covered"] = bool(f)
        c["energy"] = f["energy"] if f else None
        c["bpm"] = f["tempo"] if f else None
        c["dance"] = f["danceability"] if f else None
        if not f:
            c["band"] = "no-data"
        elif f["energy"] >= E_SKIP:
            c["band"] = "skip"
        elif f["energy"] <= E_AUSKLANG:
            c["band"] = "ausklang"
        else:
            c["band"] = "chill"

    # --- Rank within band by popularity, with per-artist variety cap ---
    def select(band: str, top_n: int) -> list[dict]:
        pool = sorted((c for c in candidates if c["band"] == band),
                      key=lambda c: -c["popularity"])
        seen: dict[str, int] = {}
        out = []
        for c in pool:
            a = _norm(c["artist"])
            cap = AMK_CAP if "annenmaykantereit" in a else PER_ARTIST_CAP
            if seen.get(a, 0) >= cap:
                continue
            seen[a] = seen.get(a, 0) + 1
            out.append(c)
            if len(out) >= top_n:
                break
        return out

    chill = select("chill", TOP_CHILL)
    ausk = select("ausklang", TOP_AUSKLANG)

    # --- Report ---
    rows = []
    for band, lst in (("chill", chill), ("ausklang", ausk)):
        for c in lst:
            rows.append({
                "band": band, "energy": c["energy"], "bpm": c["bpm"],
                "dance": c["dance"], "popularity": c["popularity"],
                "artist": c["artist"], "title": c["title"], "uri": c["uri"],
            })
    with open(REPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["band", "energy", "bpm", "dance",
                                          "popularity", "artist", "title", "uri"])
        w.writeheader()
        w.writerows(rows)

    nodata = [c for c in candidates if c["band"] == "no-data"]
    skipped = [c for c in candidates if c["band"] == "skip"]
    print(f"\nCHILL candidates: {len(chill)}   AUSKLANG candidates: {len(ausk)}")
    print(f"(skipped too-energetic: {len(skipped)} · no ReccoBeats data: {len(nodata)})")
    print(f"Report -> {REPORT}\n")

    for band, lst in (("CHILL", chill), ("AUSKLANG", ausk)):
        print(f"=== {band} ({len(lst)}) ===")
        for c in lst:
            print(f"  e{c['energy']:.2f} {c['bpm']:5.0f}bpm pop{c['popularity']:3d}  "
                  f"{c['artist']} - {c['title']}")
        print()


if __name__ == "__main__":
    main()
