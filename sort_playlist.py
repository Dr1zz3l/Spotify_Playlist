#!/usr/bin/env python3
"""
Reorder a Spotify playlist into a warmup -> peak -> cooldown energy arc.

Spotify deprecated its /v1/audio-features endpoint for new apps (Nov 2024), so
audio features (energy + tempo/BPM) come from ReccoBeats instead, matched by the
exact Spotify track ID. ReccoBeats is free and needs no API key. Spotify (spotipy)
is used ONLY to read the source playlist and to create the new sorted playlist.

Pipeline:
  1. Fetch the source playlist's tracks via spotipy.
  2. Map each Spotify ID -> ReccoBeats ID, then fetch energy/tempo/danceability.
     Results are cached in .audio_cache.json so reruns are instant.
  3. Bucket tracks into Warmup / Peak / Cooldown by an intensity score.
  4. Order within buckets to minimise BPM jumps between adjacent tracks
     (warmup ramps up, peak stays high, cooldown ramps down).
  5. Create a new private playlist and push the sorted tracks.

Usage:
  uv run python sort_playlist.py --playlist <url|id>
  uv run python sort_playlist.py --playlist <url|id> --name "Party (sorted)" --dry-run
"""

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
import yaml

SCOPE = "playlist-modify-public playlist-modify-private playlist-read-private"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
TRACKLIST_FILE = "tracklist.yaml"
TRACK_CACHE_FILE = ".track_cache.json"   # built by build_playlist.py: "artist|title" -> track dict
AUDIO_CACHE_FILE = ".audio_cache.json"
SORT_REPORT_FILE = "sort_report.csv"

# Per-phase config for the "2.0" set: which signal/shape sorts it, the energy a
# track should have to belong there (used to dedup tracks that appear in several
# phases), and the new playlist name.
PHASE_2_0 = {
    "Chill 16-20": {
        "signal": "energy", "shape": "peak", "target": 0.45,
        "name": "Poolparty · Chill · 2.0 (16-20 Uhr)",
    },
    "Party 20-23": {
        "signal": "dance", "shape": "peak", "target": 0.85,
        "name": "Poolparty · Party · 2.0 (20-23 Uhr)",
    },
    "Ausklang 23-02": {
        "signal": "energy", "shape": "winddown", "target": 0.30,
        "name": "Poolparty · Ausklang · 2.0 (23-02 Uhr)",
    },
}

RECCO_BASE = "https://api.reccobeats.com/v1"
RECCO_HEADERS = {"Accept": "application/json", "User-Agent": "poolparty-sorter/1.0"}
RECCO_CHUNK = 40       # Spotify IDs per /track request
RECCO_FEAT_CHUNK = 20  # ReccoBeats IDs per /audio-features request (smaller cap)

# Bucket sizes as a fraction of the track count. The crowd has already been easing
# in during the 4 h chill phase, so the party warmup is short — peak lands well
# before the 22:00 German quiet-hours threshold.
WARMUP_FRAC = 0.15
COOLDOWN_FRAC = 0.20

# Intensity = how hard a track drives the floor. Energy dominates; BPM refines.
ENERGY_WEIGHT = 0.7
TEMPO_WEIGHT = 0.3
TEMPO_MIN, TEMPO_MAX = 90.0, 150.0  # BPM range mapped to 0..1 for the tempo term


# ── ReccoBeats audio features ────────────────────────────────────────────────

def load_audio_cache() -> dict:
    try:
        return json.loads(Path(AUDIO_CACHE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_audio_cache(cache: dict) -> None:
    Path(AUDIO_CACHE_FILE).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _recco_get(url: str) -> dict:
    """GET a ReccoBeats endpoint, retrying on transient errors. Returns {} on failure."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=RECCO_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503):  # rate limited / transient server error
                time.sleep(1 + attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1 + attempt)
    return {}


def _fetch_features_for_recco_ids(recco_ids: list[str]) -> dict:
    """
    Return {recco_id: feature_dict} for the given ReccoBeats IDs. Tries the batch
    endpoint in small chunks; falls back to per-ID calls if a batch comes back empty.
    """
    feats: dict[str, dict] = {}
    for j in range(0, len(recco_ids), RECCO_FEAT_CHUNK):
        sub = recco_ids[j:j + RECCO_FEAT_CHUNK]
        fdata = _recco_get(f"{RECCO_BASE}/audio-features?ids={','.join(sub)}")
        batch = {f["id"]: f for f in fdata.get("content", [])}
        for rid in sub:
            if rid in batch:
                feats[rid] = batch[rid]
            else:  # batch missed this one — try the single-track endpoint
                single = _recco_get(f"{RECCO_BASE}/track/{rid}/audio-features")
                if single.get("id"):
                    feats[rid] = single
    return feats


def fetch_audio_features(spotify_ids: list[str], cache: dict) -> dict:
    """
    Return {spotify_id: {energy, tempo, danceability}} for the given IDs.
    Uses ReccoBeats (Spotify ID -> ReccoBeats ID -> audio features) and caches
    every lookup, including misses (stored as None).
    """
    todo = [sid for sid in spotify_ids if sid not in cache]
    if todo:
        print(f"Fetching audio features for {len(todo)} new tracks from ReccoBeats...")

    for i in range(0, len(todo), RECCO_CHUNK):
        chunk = todo[i:i + RECCO_CHUNK]

        # Step 1: Spotify ID -> ReccoBeats track object (href carries the Spotify ID).
        data = _recco_get(f"{RECCO_BASE}/track?ids={','.join(chunk)}")
        spotify_to_recco: dict[str, str] = {}
        for t in data.get("content", []):
            sid = t.get("href", "").rstrip("/").split("/")[-1]
            if sid and t.get("id"):
                spotify_to_recco[sid] = t["id"]

        # Step 2: ReccoBeats IDs -> audio features.
        recco_ids = list(spotify_to_recco.values())
        feats = _fetch_features_for_recco_ids(recco_ids) if recco_ids else {}

        # Join back to Spotify IDs; store None for anything ReccoBeats didn't have.
        for sid in chunk:
            rid = spotify_to_recco.get(sid)
            f = feats.get(rid) if rid else None
            if f and f.get("energy") is not None and f.get("tempo"):
                cache[sid] = {
                    "energy": round(f["energy"], 3),
                    "tempo": round(f["tempo"], 1),
                    "danceability": round(f.get("danceability") or 0, 3),
                }
            else:
                cache[sid] = None
        save_audio_cache(cache)
        time.sleep(0.2)  # be polite to the free API

    return {sid: cache.get(sid) for sid in spotify_ids}


# ── Curated-energy fallback (from tracklist.yaml) ────────────────────────────

def load_curated_energy() -> dict:
    """Map normalised 'artist - title' -> energy (1-5) for tracks ReccoBeats misses."""
    try:
        data = yaml.safe_load(Path(TRACKLIST_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    out: dict[str, int] = {}
    for phase in data.get("phases", []):
        for t in phase.get("tracks", []):
            key = _norm(f"{t.get('artist', '')} {t.get('title', '')}")
            if t.get("energy"):
                out[key] = t["energy"]
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


# ── Spotify playlist I/O ─────────────────────────────────────────────────────

def parse_playlist_id(value: str) -> str:
    """Accept a full URL, spotify:playlist:ID, or a bare ID."""
    m = re.search(r"playlist[:/]([A-Za-z0-9]+)", value)
    if m:
        return m.group(1)
    return value.split("?")[0].strip()


def fetch_playlist_tracks(sp: spotipy.Spotify, pid: str) -> tuple[str, list[dict]]:
    """Return (playlist_name, [{id, uri, artist, title}]) for all tracks."""
    meta = sp.playlist(pid, fields="name")
    name = meta.get("name", pid)

    tracks: list[dict] = []
    results = sp.playlist_items(pid, limit=100)
    while results:
        for item in results["items"]:
            # Feb-2026 Web API migration nests the track under "item"; older
            # responses used "track". Support both.
            tr = item.get("item")
            if not isinstance(tr, dict):
                tr = item.get("track")
            if not isinstance(tr, dict) or not tr.get("id"):
                continue
            tracks.append({
                "id": tr["id"],
                "uri": tr["uri"],
                "artist": tr["artists"][0]["name"] if tr.get("artists") else "",
                "title": tr.get("name", ""),
            })
        results = sp.next(results) if results.get("next") else None
    return name, tracks


# ── Sorting: warmup -> peak -> cooldown ──────────────────────────────────────

def tempo_norm(bpm: float) -> float:
    return max(0.0, min(1.0, (bpm - TEMPO_MIN) / (TEMPO_MAX - TEMPO_MIN)))


def attach_features(tracks: list[dict], feats: dict, curated: dict,
                    signal: str = "dance") -> list[dict]:
    """
    Annotate each track with energy (0-1), bpm, danceability, and an `arc_score`
    that drives the macro warmup-peak-cooldown shape. The arc_score source depends
    on `signal`:
      energy  - ReccoBeats sonic energy (loud = peak)
      dance   - blend of energy + danceability (best for a dancefloor)
      curated - the hand-tuned 1-5 hype ranking from tracklist.yaml
    """
    for t in tracks:
        f = feats.get(t["id"])
        if f:
            t["energy"] = f["energy"]
            # Tempos above ~190 are almost always double-time detection errors
            # (e.g. a 109-BPM track reported as 218); fold them back to half-time.
            bpm = f["tempo"]
            t["bpm"] = round(bpm / 2, 1) if bpm > 190 else bpm
            t["danceability"] = f["danceability"]
            t["estimated"] = False
        else:
            # Fall back to the curated 1-5 energy; estimate a plausible BPM from it.
            # A small title-derived spread keeps same-energy estimates from clumping
            # at one identical BPM value.
            ce = curated.get(_norm(f"{t['artist']} {t['title']}"))
            energy = (ce - 1) / 4 if ce else 0.5
            spread = (len(t["title"]) % 11) - 5  # deterministic -5..+5 BPM nudge
            t["energy"] = round(energy, 3)
            t["bpm"] = round(TEMPO_MIN + energy * (TEMPO_MAX - TEMPO_MIN) + spread, 1)
            t["danceability"] = None
            t["estimated"] = True

        ce = curated.get(_norm(f"{t['artist']} {t['title']}"))
        if signal == "energy":
            t["arc_score"] = t["energy"]
        elif signal == "curated":
            t["arc_score"] = (ce - 1) / 4 if ce else t["energy"]
        else:  # "dance"
            dance = t["danceability"] if t["danceability"] is not None else t["energy"]
            t["arc_score"] = 0.5 * t["energy"] + 0.5 * dance
    return tracks


def build_arc(tracks: list[dict], shape: str = "arc") -> list[dict]:
    """
    Order tracks into an energy arc. Energy drives the macro shape; within each
    segment tracks are ordered by BPM to minimise beat-matching jumps.

    shape="arc"     : warmup (low, rising) -> peak (high) -> cooldown (low, falling).
    shape="peak"    : warmup (low, rising) -> peak (high), ending hot. Best when a
                      separate playlist handles the wind-down afterwards.
    shape="winddown": pure descending arc — opens at the highest energy (to catch a
                      hot handoff) and eases down to the calmest. For an Ausklang set.
    """
    by_energy = sorted(tracks, key=lambda t: t["arc_score"])
    n = len(by_energy)
    n_warm = round(n * WARMUP_FRAC)
    cooldown: list[dict] = []

    if shape == "winddown":
        # Descend by energy; this whole set is the wind-down.
        warmup = []
        peak = sorted(tracks, key=lambda t: t["arc_score"], reverse=True)
        for t in peak:
            t["bucket"] = "WINDDOWN"
        return peak
    elif shape == "peak":
        warmup = by_energy[:n_warm]
        peak = by_energy[n_warm:]
    else:  # "arc"
        n_cool = round(n * COOLDOWN_FRAC)
        # The lowest-energy tracks bookend the set: the very lowest open (warmup),
        # the next-lowest close it (cooldown). Everything else is the peak.
        low = by_energy[:n_warm + n_cool]
        peak = by_energy[n_warm + n_cool:]
        warmup = low[:n_warm]
        cooldown = low[n_warm:]

    warmup.sort(key=lambda t: t["bpm"])                  # ascending ramp
    peak.sort(key=lambda t: t["bpm"])                    # ramp up to the global max
    cooldown.sort(key=lambda t: t["bpm"], reverse=True)  # descending ramp

    for t in warmup:
        t["bucket"] = "WARMUP"
    for t in peak:
        t["bucket"] = "PEAK"
    for t in cooldown:
        t["bucket"] = "COOLDOWN"
    return warmup + peak + cooldown


def total_bpm_jump(tracks: list[dict]) -> float:
    return sum(abs(tracks[i]["bpm"] - tracks[i - 1]["bpm"]) for i in range(1, len(tracks)))


# ── Report ───────────────────────────────────────────────────────────────────

def write_sort_report(tracks: list[dict], path: str) -> None:
    fields = ["pos", "bucket", "energy", "bpm", "danceability",
              "estimated", "artist", "title", "uri"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, t in enumerate(tracks, 1):
            w.writerow({
                "pos": i,
                "bucket": t["bucket"],
                "energy": t["energy"],
                "bpm": t["bpm"],
                "danceability": t.get("danceability"),
                "estimated": t["estimated"],
                "artist": t["artist"],
                "title": t["title"],
                "uri": t["uri"],
            })
    print(f"\nReport written -> {path}")


# ── 2.0 set: build all three playlists from the curated YAML ─────────────────

def load_yaml_phase_tracks() -> dict:
    """
    Read tracklist.yaml and resolve every (non-TBD) track to a Spotify URI using
    the overrides table and the build cache (.track_cache.json). Returns
    {phase_name: [{id, uri, artist, title}]}. Tracks with no known URI are skipped.
    """
    data = yaml.safe_load(Path(TRACKLIST_FILE).read_text(encoding="utf-8"))
    overrides = data.get("overrides") or {}
    try:
        track_cache = json.loads(Path(TRACK_CACHE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        track_cache = {}

    out: dict[str, list[dict]] = {}
    for phase in data.get("phases", []):
        rows: list[dict] = []
        for t in phase.get("tracks", []):
            if t.get("tbd") or not t.get("title"):
                continue
            key = f"{t['artist']}|{t['title']}"
            uri = overrides.get(key)
            if not uri:
                cached = track_cache.get(key)
                uri = cached.get("uri") if cached else None
            if not uri:
                continue  # never matched on Spotify — skip
            rows.append({
                "id": uri.split(":")[-1],
                "uri": uri,
                "artist": t["artist"],
                "title": t["title"],
            })
        out[phase["name"]] = rows
    return out


def find_playlist_by_name(sp: spotipy.Spotify, name: str) -> str | None:
    """Return the id of a current-user playlist with this exact name, or None."""
    results = sp.current_user_playlists(limit=50)
    while results:
        for p in results["items"]:
            if p and p.get("name") == name:
                return p["id"]
        results = sp.next(results) if results.get("next") else None
    return None


def create_playlist(sp: spotipy.Spotify, name: str, arc: list[dict], description: str) -> None:
    """Create the playlist, or replace its contents in place if it already exists."""
    uris = [t["uri"] for t in arc]
    existing = find_playlist_by_name(sp, name)
    if existing:
        sp.playlist_replace_items(existing, uris[:100])   # replaces all current items
        for i in range(100, len(uris), 100):
            sp.playlist_add_items(existing, uris[i:i + 100])
        print(f"  OK Updated '{name}' -> {len(uris)} tracks (replaced in place).")
        return
    playlist = sp._post("me/playlists", payload={
        "name": name, "public": False, "description": description,
    })
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(playlist["id"], uris[i:i + 100])
    print(f"  OK Created '{name}' with {len(uris)} tracks.")


def build_2_0_set(sp: spotipy.Spotify, dry_run: bool) -> None:
    """
    Generate the curated + sorted "2.0" playlist set directly from tracklist.yaml.
    Tracks that appear in several phases are kept only in the phase whose target
    energy is closest to the track's measured energy.
    """
    phase_tracks = load_yaml_phase_tracks()
    curated = load_curated_energy()

    # One audio-feature fetch for every track across all phases.
    all_ids = {t["id"] for rows in phase_tracks.values() for t in rows}
    cache = load_audio_cache()
    feats = fetch_audio_features(sorted(all_ids), cache)

    # Give every track an energy value (ReccoBeats, else curated fallback).
    for rows in phase_tracks.values():
        attach_features(rows, feats, curated, signal="energy")

    # Dedup across phases: assign each track to the phase whose target energy is
    # nearest its own. Ties keep the earlier phase (Chill < Party < Ausklang).
    track_phases: dict[str, list[str]] = {}
    for phase_name, rows in phase_tracks.items():
        for t in rows:
            track_phases.setdefault(t["id"], []).append(phase_name)

    moved: list[str] = []
    keep_in: dict[str, str] = {}
    for tid, phases in track_phases.items():
        if len(phases) == 1:
            keep_in[tid] = phases[0]
            continue
        energy = next(t["energy"] for t in phase_tracks[phases[0]] if t["id"] == tid)
        best = min(phases, key=lambda p: abs(energy - PHASE_2_0[p]["target"]))
        keep_in[tid] = best
        sample = next(t for t in phase_tracks[phases[0]] if t["id"] == tid)
        moved.append(f"  {sample['artist']} - {sample['title']} (e{energy:.2f}) "
                     f"-> {best.split()[0]}  [was in {', '.join(p.split()[0] for p in phases)}]")

    if moved:
        print(f"\nDedup ({len(moved)} tracks kept in one playlist by energy fit):")
        for line in sorted(moved):
            print(line)

    # Build, sort, and create each phase.
    for phase_name, cfg in PHASE_2_0.items():
        rows = [t for t in phase_tracks.get(phase_name, []) if keep_in.get(t["id"]) == phase_name]
        if not rows:
            continue
        attach_features(rows, feats, curated, signal=cfg["signal"])
        arc = build_arc(rows, shape=cfg["shape"])
        avg_jump = total_bpm_jump(arc) / max(1, len(arc) - 1)
        n_est = sum(1 for t in arc if t["estimated"])
        print(f"\n{cfg['name']}  ({len(arc)} tracks, signal={cfg['signal']}, "
              f"shape={cfg['shape']}, avg BPM jump {avg_jump:.1f}"
              f"{f', {n_est} estimated' if n_est else ''})")
        for i, t in enumerate(arc, 1):
            tag = "~" if t["estimated"] else " "
            print(f"  {i:3d} [{t['bucket']:8s}] e{t['energy']:.2f} {t['bpm']:5.0f}bpm {tag} "
                  f"{t['artist']} - {t['title']}")
        if not dry_run:
            create_playlist(sp, cfg["name"], arc,
                            f"Poolparty 2.0 — curated + BPM/energy sorted ({cfg['shape']})")

    if dry_run:
        print("\n(dry run) no playlists created.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Sort a Spotify playlist into an energy arc")
    ap.add_argument("--playlist", help="Source playlist URL, URI, or ID")
    ap.add_argument("--build2", action="store_true",
                    help="Build the full curated + sorted 'Poolparty 2.0' set from tracklist.yaml")
    ap.add_argument("--name", help="Name for the new sorted playlist")
    ap.add_argument("--shape", choices=["arc", "peak", "winddown"], default="arc",
                    help="arc = warmup-peak-cooldown; peak = ramp up and end hot; "
                         "winddown = pure descending (for an Ausklang set)")
    ap.add_argument("--signal", choices=["dance", "energy", "curated"], default="dance",
                    help="What drives the arc: dance blend / sonic energy / curated 1-5")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and report the order without creating a playlist")
    args = ap.parse_args()

    if not args.build2 and not args.playlist:
        ap.error("provide --playlist <url> or --build2")

    load_dotenv()
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        scope=SCOPE, redirect_uri=REDIRECT_URI, open_browser=True,
    ))
    user_id = sp.current_user()["id"]

    if args.build2:
        build_2_0_set(sp, dry_run=args.dry_run)
        return

    pid = parse_playlist_id(args.playlist)
    src_name, tracks = fetch_playlist_tracks(sp, pid)
    print(f"Source: '{src_name}' ({len(tracks)} tracks)")
    if not tracks:
        print("No tracks found.")
        sys.exit(1)

    cache = load_audio_cache()
    feats = fetch_audio_features([t["id"] for t in tracks], cache)
    curated = load_curated_energy()

    tracks = attach_features(tracks, feats, curated, signal=args.signal)
    n_estimated = sum(1 for t in tracks if t["estimated"])

    before = total_bpm_jump(tracks)
    arc = build_arc(tracks, shape=args.shape)
    after = total_bpm_jump(arc)

    # Show the planned order.
    for i, t in enumerate(arc, 1):
        tag = "~" if t["estimated"] else " "
        print(f"  {i:3d} [{t['bucket']:8s}] e{t['energy']:.2f} {t['bpm']:5.0f}bpm {tag} "
              f"{t['artist']} - {t['title']}")

    counts: dict[str, int] = {}
    for t in arc:
        counts[t["bucket"]] = counts.get(t["bucket"], 0) + 1
    avg_jump_before = before / max(1, len(tracks) - 1)
    avg_jump_after = after / max(1, len(arc) - 1)
    print("\nBuckets: " + " / ".join(f"{v} {k.lower()}" for k, v in counts.items()))
    print(f"Avg adjacent BPM jump: {avg_jump_before:.1f} -> {avg_jump_after:.1f}")
    if n_estimated:
        print(f"Note: {n_estimated} track(s) not in ReccoBeats — used curated energy "
              f"(marked '~').")

    write_sort_report(arc, SORT_REPORT_FILE)

    if args.dry_run:
        print("\n(dry run) no playlist created.")
        return

    new_name = args.name or f"{src_name} (sorted)"
    playlist = sp._post("me/playlists", payload={
        "name": new_name, "public": False,
        "description": "Auto-sorted warmup -> peak -> cooldown (energy + BPM)",
    })
    uris = [t["uri"] for t in arc]
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(playlist["id"], uris[i:i + 100])
    print(f"\nOK Created '{new_name}' with {len(uris)} tracks.")


if __name__ == "__main__":
    main()
