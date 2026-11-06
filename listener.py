#!/usr/bin/env python3
"""Backyard wildlife listener for a Raspberry Pi.

Records short clips from the USB mic, identifies wildlife sounds with the
BirdNET model (birds, plus some frogs/insects), and POSTs confident hits to
a small web API, which a dashboard reads from.

Setup: see README.md. Copy .env.example and fill it in.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import soundfile as sf
from scipy.signal import butter, sosfilt

from birdnetlib import Recording
from birdnetlib.analyzer import Analyzer

from health import Health

API = os.environ["DETECTIONS_API"]
AUDIO_API = os.environ["AUDIO_API"]
TOKEN = os.environ["API_TOKEN"]
MIC_DEVICE = os.environ.get("MIC_DEVICE", "default")  # find yours with: arecord -l
LAT = float(os.environ["LATITUDE"])   # BirdNET uses location to narrow the species list
LON = float(os.environ["LONGITUDE"])
CLIP_SECONDS = int(os.environ.get("CLIP_SECONDS", "15"))
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.70"))
# Analyse down to a lower bar than we publish, so the log shows near-misses —
# that is what tells us whether the threshold or the mic gain needs tuning.
LOG_CONFIDENCE = float(os.environ.get("LOG_CONFIDENCE", "0.25"))
# Outdoors, mains hum and wind rumble below ~200 Hz swamp the clip: on the deck they held
# 93% of all energy and buried the 2-4 kHz band where birdsong lives. Filter them out
# before analysis or BirdNET finds nothing at any threshold.
HIGHPASS_HZ = float(os.environ.get("HIGHPASS_HZ", "250"))
# A quiet, distant mic rarely clears the bar in one go, but a bird that keeps calling is
# real: hearing the same species several times in a short window counts as corroboration.
CORROBORATE_MIN = float(os.environ.get("CORROBORATE_MIN", "0.30"))
# BirdNET is a bird model and scores the household dog weakly: barks land around 0.28,
# well under the bar meant to keep wild species honest, so the dog gets a lower one.
SPECIES_MIN = {"Dog": float(os.environ.get("DOG_MIN", "0.22"))}
CORROBORATE_HITS = int(os.environ.get("CORROBORATE_HITS", "3"))
CORROBORATE_WINDOW = int(os.environ.get("CORROBORATE_WINDOW", "1200"))   # seconds
# BirdNET drops anything under the min_conf we hand it, before our own bars are applied.
# Ask it for everything that any threshold might care about, or a species floor set below
# this value is silently unreachable — which is exactly how the dog went missing at 0.22
# while the analyzer was refusing to report anything under 0.25.
ANALYSIS_FLOOR = min([LOG_CONFIDENCE, CORROBORATE_MIN, MIN_CONFIDENCE] + list(SPECIES_MIN.values()))

_recent = {}          # species -> [timestamps of low-confidence hits]
# Every post and every audio upload is a Cloudflare KV write, and the free tier allows
# 1,000 a day. Unthrottled we burned through that by 1am and started losing detections,
# so a species is only re-posted after a cooldown and only ever uploads audio once a day.
POST_COOLDOWN = int(os.environ.get("POST_COOLDOWN", "2700"))    # seconds per species
_posted = {}          # species -> last time we sent it
_audio_done = set()   # species we already have a clip for today
_day = None


def _roll_day():
    """Reset the per-day bookkeeping at midnight."""
    global _day, _audio_done, _posted
    today = time.strftime("%Y-%m-%d")
    if today != _day:
        _day, _audio_done, _posted = today, set(), {}


def corroborated(name, conf, now):
    """True when a below-bar species has been heard enough times to trust it."""
    if conf < CORROBORATE_MIN:
        return False
    hits = [t for t in _recent.get(name, []) if now - t < CORROBORATE_WINDOW]
    hits.append(now)
    _recent[name] = hits
    if len(hits) >= CORROBORATE_HITS:
        _recent[name] = []        # start a fresh window so it does not repost every clip
        return True
    return False

analyzer = Analyzer()

# BirdNET knows "Dog", but the location filter keeps only the ~259 species expected at
# these coordinates and drops it. Extend that list after it is computed so the household
# dog still registers, without giving up the filter's accuracy for birds.
EXTRA_SPECIES = [s for s in os.environ.get("EXTRA_SPECIES", "Dog_Dog").split(",") if s]
_orig_species_list = analyzer.set_predicted_species_list_from_position


def _species_list_with_extras(recording):
    _orig_species_list(recording)
    for extra in EXTRA_SPECIES:
        if extra not in analyzer.custom_species_list:
            analyzer.custom_species_list.append(extra)


analyzer.set_predicted_species_list_from_position = _species_list_with_extras

# What the household calls them, rather than what the model calls them.
NICKNAMES = {"Dog": os.environ.get("DOG_NAME", "Dog")}


# A USB mic that drops off the bus does not always make arecord fail: sometimes it just
# never returns, and the listener sat "active" for days recording nothing. Bound it.
RECORD_TIMEOUT = CLIP_SECONDS + int(os.environ.get("RECORD_GRACE", "20"))
# After this many failed recordings in a row, exit and let systemd restart us; repeated
# restarts escalate to a reboot (StartLimitAction in the unit), which re-enumerates USB.
MIC_FAIL_EXIT = int(os.environ.get("MIC_FAIL_EXIT", "4"))
health = Health(MIC_DEVICE)


def record(path):
    subprocess.run(
        ["arecord", "-q", "-D", MIC_DEVICE, "-f", "S16_LE", "-r", "48000",
         "-c", "1", "-d", str(CLIP_SECONDS), path],
        check=True, timeout=RECORD_TIMEOUT,
    )
    if os.path.getsize(path) < 48000 * 2 * CLIP_SECONDS // 2:   # under half the audio we asked for
        raise RuntimeError(f"short recording ({os.path.getsize(path)} bytes)")


def highpass(path):
    """Strip sub-250 Hz rumble in place, then re-normalise what is left."""
    data, sr = sf.read(path)
    sos = butter(4, HIGHPASS_HZ, "hp", fs=sr, output="sos")
    out = sosfilt(sos, data)
    peak = max(abs(out.max()), abs(out.min()))
    if peak > 0:
        out = out * (0.7 / peak)      # bring the surviving signal back up for the model
    sf.write(path, out, sr)


def upload_clip(wav, name, det):
    """Send the seconds we actually heard, so the screen can play this bird back."""
    start = max(0.0, float(det.get("start_time", 0)) - 1.0)
    end = min(float(CLIP_SECONDS), float(det.get("end_time", start + 3)) + 1.0)
    dur = max(2.0, end - start)
    mp3 = wav + ".mp3"
    try:
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
             "-i", wav, "-ac", "1", "-ar", "32000", "-codec:a", "libmp3lame", "-b:a", "64k", mp3],
            check=True,
        )
        with open(mp3, "rb") as fh:
            body = fh.read()
        req = urllib.request.Request(
            AUDIO_API + "?species=" + urllib.parse.quote(name),
            data=body, method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "audio/mpeg",
                     "User-Agent": "BirdListener/1.0 (Raspberry Pi)"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        print(f"  uploaded {len(body)//1024}kB of audio for {name}")
    except Exception as e:
        print("  audio upload failed:", e)
    finally:
        try:
            os.remove(mp3)
        except OSError:
            pass


QUEUE_FILE = os.path.expanduser(os.environ.get("QUEUE_FILE", "~/bird-listener/pending.json"))


def queue_add(detection):
    """Park a detection we could not deliver (quota exhausted, wifi down) for later."""
    try:
        q = json.load(open(QUEUE_FILE)) if os.path.exists(QUEUE_FILE) else []
    except Exception:
        q = []
    q.append(detection)
    json.dump(q[-300:], open(QUEUE_FILE, "w"))


def queue_flush():
    """Try to deliver anything parked. Detections carry their original timestamp."""
    if not os.path.exists(QUEUE_FILE):
        return
    try:
        q = json.load(open(QUEUE_FILE))
    except Exception:
        return
    if not q:
        return
    # The worker re-stamps anything older than 36 h with "now", so a long outage would dump a
    # burst of misdated birds into the Logbook. Drop those rather than lie about when.
    cutoff = (time.time() - 30 * 3600) * 1000
    stale = [d for d in q if d.get("ts", 0) < cutoff]
    if stale:
        print(f"  dropping {len(stale)} queued detection(s) older than 30h")
        q = [d for d in q if d.get("ts", 0) >= cutoff]
    left = []
    for i, det in enumerate(q):
        if i >= 20:                       # trickle, so a big backlog cannot re-blow the quota
            left.append(det)
            continue
        try:
            post(det, queue_on_fail=False)
        except Exception:
            left.extend(q[i:])
            break
    json.dump(left, open(QUEUE_FILE, "w"))
    if len(left) < len(q):
        print(f"  delivered {len(q) - len(left)} queued detection(s), {len(left)} still waiting")


HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "1800"))
_last_beat = 0
_last_trouble = 0


def _queue_len():
    try:
        return len(json.load(open(QUEUE_FILE)))
    except Exception:
        return 0


def heartbeat(ok=True):
    """Tell the station how we are, so a quiet yard is not mistaken for a dead mic.

    A healthy beat (ok=True) is only sent after a clip records cleanly: beating before the
    recording would make a Pi with an unplugged mic look healthy. A trouble beat (ok=False)
    does not count as proof of life on the worker; it only carries the reason, so the alert
    it files can say what broke. Either way the reply may carry a command ("restart",
    "reboot") queued from the phone.
    """
    global _last_beat, _last_trouble
    now = time.time()
    if ok:
        if now - _last_beat < HEARTBEAT_SECONDS:
            return
        _last_beat = now
    else:
        if now - _last_trouble < HEARTBEAT_SECONDS:
            return
        _last_trouble = now
    try:
        req = urllib.request.Request(
            API, data=json.dumps({"heartbeat": True, "ok": ok, "note": "listener",
                                  "health": health.snapshot(_queue_len())}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                     "User-Agent": "BirdListener/1.0 (Raspberry Pi)"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            reply = json.loads(r.read() or b"{}")
    except Exception as e:
        if getattr(e, "code", None) == 401:
            e = "server rejected API_TOKEN (401): token changed? update .env"
        health.error(f"heartbeat failed: {e}")
        if ok:
            _last_beat = now - HEARTBEAT_SECONDS + 300    # retry in five minutes, not thirty
        return
    if reply.get("command"):
        health.obey(reply["command"])


def post(detection, queue_on_fail=True):
    req = urllib.request.Request(
        API,
        data=json.dumps(detection).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            # Cloudflare's browser-integrity check answers the default Python-urllib
            # user agent with "error code: 1010", so every detection was being dropped.
            "User-Agent": "BirdListener/1.0 (Raspberry Pi)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception:
        if queue_on_fail:
            detection.setdefault("ts", int(time.time() * 1000))
            queue_add(detection)
            print(f"  queued for later: {detection.get('species')}")
        raise


def main():
    print(f"listening on {MIC_DEVICE}, {CLIP_SECONDS}s clips, min confidence {MIN_CONFIDENCE}")
    while True:
        wav = raw = None
        try:
            fd, wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                record(wav)
            except Exception as e:
                health.mic_failed(e)
                heartbeat(ok=False)     # say why, without claiming to be alive
                if health.mic_failures >= MIC_FAIL_EXIT:
                    print(f"{health.mic_failures} failed recordings in a row; exiting for a clean restart")
                    sys.exit(69)
                time.sleep(10)
                continue
            heartbeat()                 # proof of life: the mic just delivered audio
            raw = wav + ".raw.wav"      # keep the unfiltered audio: it is what gets played back
            shutil.copyfile(wav, raw)
            highpass(wav)
            rec = Recording(analyzer, wav, lat=LAT, lon=LON, min_conf=ANALYSIS_FLOOR)
            rec.analyze()
            health.good_cycle()             # heard and analysed: feeds the systemd watchdog
            best = {}
            for d in rec.detections:
                name = NICKNAMES.get(d["common_name"], d["common_name"])
                if name not in best or d["confidence"] > best[name]["confidence"]:
                    best[name] = d
            for name, d in best.items():
                if d["confidence"] < SPECIES_MIN.get(name, MIN_CONFIDENCE):
                    if corroborated(name, d["confidence"], time.time()):
                        print(f"corroborated: {name} ({d['confidence']:.2f}) heard {CORROBORATE_HITS}x")
                    else:
                        print(f"heard (below bar): {name} ({d['confidence']:.2f})")
                        continue
                _roll_day()
                queue_flush()               # deliver anything the quota outage stranded
                now = time.time()
                if now - _posted.get(name, 0) < POST_COOLDOWN:
                    print(f"heard again (within cooldown): {name} ({d['confidence']:.2f})")
                    continue
                post({
                    "species": name,
                    "sci": d.get("scientific_name", ""),
                    "confidence": round(d["confidence"], 2),
                })
                _posted[name] = now
                print(f"posted: {name} ({d['confidence']:.2f})")
                if name not in _audio_done:      # one clip per species per day
                    upload_clip(raw, name, d)
                    _audio_done.add(name)
        except Exception as e:  # keep listening through wifi blips; the watchdog catches hangs
            health.error(e)
            time.sleep(10)
        finally:
            for path in (wav, raw):
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass


if __name__ == "__main__":
    main()
