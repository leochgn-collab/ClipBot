#!/usr/bin/env python3
"""ClipBot : récupère des clips de créateurs AUTORISÉS (Twitch / YouTube),
les monte en vertical 9:16 (fond flou + sous-titres) et les publie
automatiquement sur YouTube Shorts et TikTok. Notifications via ntfy.

Usage :
    python clipbot.py -c config.yaml            # passage normal
    python clipbot.py -c config.yaml --dry-run  # monte les vidéos sans publier
"""

import argparse
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
import yt_dlp

log = logging.getLogger("clipbot")
BASE = Path(__file__).resolve().parent
MB = 1024 * 1024


# --------------------------------------------------------------------------
# Config et état
# --------------------------------------------------------------------------
def load_config(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve(p):
    p = Path(p)
    return p if p.is_absolute() else BASE / p


class State:
    """Base SQLite : évite de reposter deux fois le même clip."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS clips (
                source_id TEXT PRIMARY KEY, platform TEXT, channel TEXT,
                title TEXT, status TEXT, youtube_id TEXT, tiktok_id TEXT,
                tries INTEGER DEFAULT 0, error TEXT, updated TEXT)"""
        )
        self.db.commit()

    def get(self, sid):
        row = self.db.execute("SELECT * FROM clips WHERE source_id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def is_done(self, sid, max_tries):
        row = self.get(sid)
        return bool(row) and (row["status"] == "done" or row["tries"] >= max_tries)

    def save(self, c, **fields):
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO clips (source_id, platform, channel, title, updated)
               VALUES (?,?,?,?,?) ON CONFLICT(source_id) DO NOTHING""",
            (c["source_id"], c["platform"], c["channel"], c["title"], now),
        )
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(
                f"UPDATE clips SET {cols}, updated=? WHERE source_id=?",
                (*fields.values(), now, c["source_id"]),
            )
        self.db.commit()

    def fail(self, c, error):
        self.save(c)
        self.db.execute(
            "UPDATE clips SET status='error', tries=tries+1, error=? WHERE source_id=?",
            (error[:500], c["source_id"]),
        )
        self.db.commit()

    def last_post_time(self):
        row = self.db.execute("SELECT MAX(updated) FROM clips WHERE status='done'").fetchone()
        if not row or not row[0]:
            return None
        t = datetime.fromisoformat(row[0])
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    def last_post_by_channel(self):
        return {r[0].lower(): r[1] for r in self.db.execute(
            "SELECT channel, MAX(updated) FROM clips WHERE status='done' GROUP BY channel") if r[0]}

    def posted_last_24h(self):
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        return self.db.execute(
            "SELECT COUNT(*) FROM clips WHERE status='done' AND updated>=?", (since,)
        ).fetchone()[0]


# --------------------------------------------------------------------------
# Notifications ntfy
# --------------------------------------------------------------------------
def notify(cfg, title, message, priority=3, tags=None):
    n = cfg.get("ntfy") or {}
    if not n.get("url") or not n.get("topic"):
        return
    headers = {}
    if n.get("token"):
        headers["Authorization"] = "Bearer " + n["token"]
    try:
        requests.post(
            n["url"].rstrip("/"),
            json={"topic": n["topic"], "title": title, "message": message,
                  "priority": priority, "tags": tags or []},
            headers=headers, timeout=15,
        )
    except requests.RequestException as e:
        log.warning("ntfy injoignable : %s", e)


# --------------------------------------------------------------------------
# Sources : Twitch
# --------------------------------------------------------------------------
class Twitch:
    API = "https://api.twitch.tv/helix"

    def __init__(self, client_id, client_secret):
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": client_id, "client_secret": client_secret,
                  "grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        self.headers = {"Client-ID": client_id,
                        "Authorization": "Bearer " + r.json()["access_token"]}

    def _get(self, path, **params):
        r = requests.get(self.API + path, headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()["data"]

    def game_ids(self, names):
        """Noms de jeux Twitch -> identifiants (ex. « Grand Theft Auto V » -> 32982)."""
        ids = {}
        for i in range(0, len(names), 100):
            r = requests.get(self.API + "/games", headers=self.headers,
                             params=[("name", n) for n in names[i:i + 100]], timeout=30)
            r.raise_for_status()
            ids.update({g["id"]: g["name"] for g in r.json()["data"]})
        missing = set(names) - set(ids.values())
        if missing:
            log.warning("Jeux introuvables sur Twitch (vérifie l'orthographe) : %s", ", ".join(missing))
        return ids

    def user_id(self, login):
        if not hasattr(self, "_uids"):
            self._uids = {}
        if login not in self._uids:
            users = self._get("/users", login=login)
            self._uids[login] = users[0]["id"] if users else None
        return self._uids[login]

    def videos(self, login, first=3):
        """Rediffusions (VOD) récentes de la chaîne."""
        uid = self.user_id(login)
        return self._get("/videos", user_id=uid, type="archive", first=first) if uid else []

    def clips(self, login, since, until):
        uid = self.user_id(login)
        if not uid:
            log.warning("Twitch : chaîne introuvable '%s'", login)
            return []
        return self._get(
            "/clips", broadcaster_id=uid, first=50,
            started_at=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ended_at=until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


def twitch_candidates(cfg, state=None, scan_vods=True):
    src = cfg["sources"].get("twitch") or {}
    if not src.get("enabled"):
        return []
    max_d = cfg["video"]["max_duration"]
    now = datetime.now(timezone.utc)
    # L'API Twitch limite la fenêtre à 7 jours
    since = now - timedelta(hours=min(src.get("lookback_hours", 48), 168))
    tw = Twitch(src["client_id"], src["client_secret"])
    games = [g.strip() for g in src.get("games") or [] if str(g).strip()]
    allowed_games = tw.game_ids(games) if games else None
    if allowed_games is not None:
        log.info("Filtre jeux : %s", ", ".join(allowed_games.values()) or "aucun jeu reconnu")
    out = []
    for ch in src.get("channels", []):
        login = ch["login"] if isinstance(ch, dict) else ch
        min_views = (ch.get("min_views") if isinstance(ch, dict) else None) or src.get("min_views", 0)
        try:
            clips = tw.clips(login, since, now)
        except requests.RequestException as e:
            log.error("Twitch %s : %s", login, e)
            continue
        vods = src.get("vods") or {}
        if vods.get("enabled"):
            try:
                out += vod_candidates(cfg, tw, login, clips, state, scan_vods)
            except Exception as e:  # une rediffusion illisible ne bloque pas les clips
                log.error("Rediffusions de %s : %s", login, e)
        for cl in clips:
            if cl["view_count"] < min_views or cl["duration"] > max_d + 5:
                continue
            if allowed_games is not None and cl.get("game_id") not in allowed_games:
                log.debug("Ignoré (hors jeux choisis) : %s", cl["title"])
                continue
            out.append({
                "source_id": "tw:" + cl["id"], "platform": "twitch",
                "channel": cl["broadcaster_name"], "title": cl["title"],
                "url": cl["url"], "views": cl["view_count"],
                "credit_url": f"https://twitch.tv/{login}",
                "start": None, "end": None,
            })
    log.info("Twitch : %d clip(s) candidat(s)", len(out))
    return out


# --------------------------------------------------------------------------
# Sources : rediffusions Twitch (moments forts détectés au son)
# --------------------------------------------------------------------------
def _twitch_duration(text):
    """« 4h3m10s » -> secondes."""
    return sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+)([hms])", text or ""))


def loudness_per_second(stream):
    """Volume sonore (dB) seconde par seconde, calculé par ffmpeg sans tout télécharger sur le disque."""
    cmd = ["ffmpeg", "-nostats", "-loglevel", "error", "-i", stream, "-vn", "-ac", "1",
           "-af", "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
                  "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
           "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    levels = []
    for line in out.splitlines():
        if "RMS_level=" in line:
            v = line.split("=", 1)[1].strip()
            levels.append(-90.0 if v in ("-inf", "inf", "nan") else max(-90.0, float(v)))
    return levels


def vod_loudness(cfg, vod_id, vod_url):
    """Analyse d'une rediffusion, gardée en cache (elle ne change plus une fois le live fini)."""
    cache = resolve(cfg.get("work_dir", "work")) / "vodcache" / f"{vod_id}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    log.info("Analyse du son de la rediffusion %s (quelques minutes la première fois)…", vod_id)
    with yt_dlp.YoutubeDL({**ydl_base_opts(cfg), "format": "Audio_Only/audio_only/bestaudio/worst"}) as y:
        info = y.extract_info(vod_url, download=False)
    stream = info.get("url") or info["requested_formats"][0]["url"]
    levels = loudness_per_second(stream)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(levels))
    return levels


def pick_highlights(levels, length, count, blocked=(), min_gap=120, boosts=(), step=5, min_excess=3.0):
    """Les `count` passages de `length` s les plus « forts » (son au-dessus du niveau habituel du live),
    sans chevaucher les passages déjà pris (`blocked`) ni être à moins de `min_gap` s l'un de l'autre.
    `boosts` : instants où les viewers ont créé des clips (bonus)."""
    n = len(levels)
    if n < length:
        return []
    med = sorted(levels)[n // 2]
    score = [max(0.0, v - med) for v in levels]
    for b in boosts:
        for t in range(max(0, int(b)), min(n, int(b) + 30)):
            score[t] += 8.0
    pref = [0.0]
    for v in score:
        pref.append(pref[-1] + v)
    taken = [(max(0, a - min_gap), b + min_gap) for a, b in blocked]
    picks = []
    windows = sorted(((pref[t + length] - pref[t], t) for t in range(0, n - length, step)), reverse=True)
    for total, t in windows:
        # en moyenne au moins `min_excess` dB au-dessus du niveau habituel (sinon : passage calme)
        if len(picks) >= count or total < min_excess * length:
            break
        if any(a < t + length and t < b for a, b in taken):
            continue
        picks.append((t, t + length))
        taken.append((t - min_gap, t + length + min_gap))
    return sorted(picks)


def vod_candidates(cfg, tw, login, clips, state, scan=True):
    src = cfg["sources"]["twitch"]
    vcfg = src.get("vods") or {}
    length = int(cfg["video"]["max_duration"])
    keywords = [k.lower() for k in vcfg.get("title_keywords") or [] if k]
    max_age = timedelta(hours=vcfg.get("lookback_hours", 72))
    now = datetime.now(timezone.utc)
    out = []
    for v in tw.videos(login, first=int(vcfg.get("max_vods", 2))):
        created = datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
        if now - created > max_age:
            continue
        if keywords and not any(k in v["title"].lower() for k in keywords):
            log.debug("Rediffusion ignorée (titre) : %s", v["title"])
            continue
        cached = (resolve(cfg.get("work_dir", "work")) / "vodcache" / f"{v['id']}.json").exists()
        if not scan and not cached:
            continue
        levels = vod_loudness(cfg, v["id"], v["url"])
        # Passages déjà publiés : extraits de cette rediffusion + clips Twitch publiés de ce live
        blocked = []
        if state is not None:
            for sid, st in state.db.execute(
                    "SELECT source_id, status FROM clips WHERE source_id LIKE ?", (f"vod:{v['id']}:%",)):
                if st == "done":
                    start = int(sid.rsplit(":", 1)[1])
                    blocked.append((start, start + length))
        boosts = []
        for cl in clips:
            if cl.get("video_id") == v["id"] and cl.get("vod_offset") is not None:
                boosts.append(cl["vod_offset"])
                row = state.get("tw:" + cl["id"]) if state is not None else None
                if row and row["status"] == "done":
                    blocked.append((cl["vod_offset"], cl["vod_offset"] + int(cl["duration"])))
        for start, end in pick_highlights(levels, length, int(vcfg.get("highlights_per_vod", 6)),
                                          blocked, int(vcfg.get("min_gap_seconds", 120)), boosts):
            out.append({
                "source_id": f"vod:{v['id']}:{start}", "platform": "twitch-vod",
                "channel": v["user_name"], "title": v["title"], "url": v["url"],
                "views": 0, "credit_url": f"https://twitch.tv/{login}",
                "start": start, "end": end,
            })
    return out


# --------------------------------------------------------------------------
# Sources : YouTube (moments les plus revus via la heatmap)
# --------------------------------------------------------------------------
def ydl_base_opts(cfg):
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    cookies = cfg.get("ytdlp_cookies")
    if cookies:
        opts["cookiefile"] = str(resolve(cookies))
    return opts


def best_windows(heatmap, duration, length, count):
    """Trouve les `count` fenêtres de `length` s les plus revues (sans chevauchement)."""
    secs = int(duration)
    if secs <= length:
        return [(0, secs)]
    score = [0.0] * secs
    for seg in heatmap:
        for s in range(int(seg["start_time"]), min(int(seg["end_time"]) + 1, secs)):
            score[s] = max(score[s], seg["value"])
    picks = []
    for _ in range(count):
        pref = [0.0]
        for v in score:
            pref.append(pref[-1] + v)
        best, best_start = -1.0, 0
        for s in range(0, secs - length):
            total = pref[s + length] - pref[s]
            if total > best:
                best, best_start = total, s
        if best <= 0:
            break
        picks.append((best_start, best_start + length))
        for s in range(max(0, best_start - length), min(secs, best_start + 2 * length)):
            score[s] = 0.0  # on évite deux extraits quasi identiques
    return picks


def youtube_candidates(cfg):
    src = cfg["sources"].get("youtube") or {}
    if not src.get("enabled"):
        return []
    length = cfg["video"]["max_duration"]
    per_video = src.get("clips_per_video", 1)
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=src.get("lookback_hours", 72))
    min_age = timedelta(hours=src.get("min_age_hours", 12))
    out = []
    for ch in src.get("channels", []):
        url = ch["url"] if isinstance(ch, dict) else ch
        name = ch.get("name") if isinstance(ch, dict) else None
        try:
            with yt_dlp.YoutubeDL({**ydl_base_opts(cfg), "extract_flat": True,
                                   "playlistend": src.get("videos_per_channel", 5)}) as y:
                listing = y.extract_info(url, download=False)
        except Exception as e:  # yt-dlp lève des types variés
            log.error("YouTube %s : %s", url, e)
            continue
        for entry in listing.get("entries") or []:
            vid = entry.get("id")
            if not vid:
                continue
            sid_prefix = f"yt:{vid}:"
            try:
                with yt_dlp.YoutubeDL(ydl_base_opts(cfg)) as y:
                    info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            except Exception as e:
                log.warning("YouTube %s : %s", vid, e)
                continue
            ts = info.get("timestamp") or info.get("release_timestamp")
            if ts:
                age = now - datetime.fromtimestamp(ts, timezone.utc)
                if age > max_age or age < min_age:
                    continue
            heatmap = info.get("heatmap")
            if not heatmap:
                log.info("Pas de heatmap pour %s (vidéo trop récente ou peu vue), ignorée", vid)
                continue
            for k, (s, e) in enumerate(best_windows(heatmap, info["duration"], length, per_video)):
                out.append({
                    "source_id": sid_prefix + str(k), "platform": "youtube",
                    "channel": name or info.get("channel") or info.get("uploader", "?"),
                    "title": info.get("title", ""), "url": info["webpage_url"],
                    "views": info.get("view_count") or 0,
                    "credit_url": info.get("channel_url") or url,
                    "start": s, "end": e,
                })
    log.info("YouTube : %d extrait(s) candidat(s)", len(out))
    return out


# --------------------------------------------------------------------------
# Téléchargement et montage
# --------------------------------------------------------------------------
def download(c, work, cfg):
    opts = {
        **ydl_base_opts(cfg),
        "outtmpl": str(work / "source.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
    }
    if c.get("start") is not None:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(c["start"], c["end"])])
        opts["force_keyframes_at_cuts"] = True
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([c["url"]])
    files = sorted(work.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("téléchargement vide")
    return files[0]


_whisper_model = None


def _ts(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"


def transcribe(src, srt_path, cfg):
    """Sous-titres façon TikTok (3-4 mots à la fois). Retourne False si indisponible."""
    sub = cfg["video"].get("subtitles") or {}
    if not sub.get("enabled"):
        return False
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper non installé : pas de sous-titres")
        return False
    if _whisper_model is None:
        _whisper_model = WhisperModel(sub.get("model", "small"), device="cpu", compute_type="int8")
    segments, _ = _whisper_model.transcribe(
        str(src), language=sub.get("language", "fr"), vad_filter=True, word_timestamps=True)
    words = [w for seg in segments for w in (seg.words or [])]
    if not words:
        return False
    per = sub.get("words_per_line", 4)
    lines = []
    for i in range(0, len(words), per):
        chunk = words[i:i + per]
        text = " ".join(w.word.strip() for w in chunk).upper()
        lines.append(f"{len(lines) + 1}\n{_ts(chunk[0].start)} --> {_ts(chunk[-1].end)}\n{text}\n")
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return True


FONT_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _dt_escape(text):
    """Échappe un texte pour le filtre drawtext de ffmpeg."""
    return (text.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:")
            .replace("%", "\\%").replace(",", "\\,"))


def _srt_to_ass(srt_path, ass_path, fontname, accent_hex):
    """Sous-titres au format ASS en repère 1080x1920 : placés juste sous le gameplay,
    la seconde moitié de chaque ligne en couleur d'accent (comme la maquette)."""
    r, g, b = accent_hex[0:2], accent_hex[2:4], accent_hex[4:6]
    accent_ass = f"&H{b}{g}{r}&"
    def t(ts):  # 00:00:01,234 -> 0:00:01.23
        h, m, rest = ts.strip().split(":")
        sec, ms = rest.split(",")
        return f"{int(h)}:{m}:{sec}.{ms[:2]}"
    events = []
    for block in srt_path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, end = lines[1].split("-->")
        words = " ".join(lines[2:]).replace("{", "(").replace("}", ")").split()
        cut = max(1, len(words) // 2)
        text = " ".join(words[:cut]) + (" {\\c" + accent_ass + "}" + " ".join(words[cut:]) if words[cut:] else "")
        events.append(f"Dialogue: 0,{t(start)},{t(end)},Sub,,0,0,0,,{text}")
    ass_path.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Sub,{fontname},62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,4,0,8,60,60,1240,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events) + "\n", encoding="utf-8")


def render_bandeau(src, work, cfg, with_subs):
    """Design A : fond noir, logo + « ABONNEZ-VOUS ET LIKEZ ! » en haut, gameplay encadré
    de jaune au centre, sous-titres en dessous, pied de page discret."""
    v = cfg["video"]
    b = v.get("bandeau") or {}
    accent = b.get("accent", "#FFD21F").lstrip("#")
    font = resolve(b.get("font", "assets/fonts/RubikMonoOne-Regular.ttf"))
    font = str(font) if font.exists() else FONT_FALLBACK
    lg = v.get("logo") or {}
    logo = resolve(lg.get("file", "assets/logo.png"))
    use_logo = logo.exists()
    tx = 264 if use_logo else 54                      # texte à droite du logo
    line1 = _dt_escape(b.get("line1", "ABONNEZ-VOUS"))
    line2 = _dt_escape(b.get("line2", "ET LIKEZ !"))
    footer = _dt_escape(b.get("footer", "REDIFSTREAM"))
    dur = float(v["max_duration"])

    g = (f"color=c=0x0E0F13:s=1080x1920:r=30:d={dur}[bg0];"
         "[0:v]scale=1080:608:force_original_aspect_ratio=decrease,"
         "pad=1080:608:(ow-iw)/2:(oh-ih)/2:color=0x0E0F13,fps=30[fg];"
         f"[bg0]drawbox=x=0:y=0:w=1080:h=24:color=0x{accent}:t=fill,"
         f"drawbox=x=0:y=555:w=1080:h=9:color=0x{accent}:t=fill,"
         f"drawbox=x=0:y=1173:w=1080:h=9:color=0x{accent}:t=fill[bg1];"
         "[bg1][fg]overlay=0:564:shortest=1[b2];")
    last = "b2"
    if use_logo:
        g += "[1:v]scale=174:174:force_original_aspect_ratio=decrease[lg];[b2][lg]overlay=54:168[b3];"
        last = "b3"
    g += (f"[{last}]drawtext=fontfile='{font}':expansion=none:text='{line1}':fontsize=57:fontcolor=white:x={tx}:y=190,"
          f"drawtext=fontfile='{font}':expansion=none:text='{line2}':fontsize=57:fontcolor=0x{accent}:x={tx}:y=262,"
          f"drawtext=fontfile='{font}':expansion=none:text='{footer}':fontsize=34:fontcolor=0x9AA1AE:"
          "x=(w-text_w)/2:y=1440,format=yuv420p")
    if with_subs and (work / "subs.srt").exists():
        fontname = "Rubik Mono One" if font != FONT_FALLBACK else "DejaVu Sans"
        _srt_to_ass(work / "subs.srt", work / "subs.ass", fontname, accent)
        g += f",subtitles=subs.ass:fontsdir='{Path(font).parent}'"
    g += "[v]"
    out = work / "final.mp4"
    inputs = ["-i", src.name] + (["-i", str(logo)] if use_logo else [])
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-t", str(dur),
           "-filter_complex", g, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(v.get("crf", 21)),
           "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", out.name]
    subprocess.run(cmd, cwd=work, check=True)
    return out


def render(src, work, cfg, with_subs):
    v = cfg["video"]
    if v.get("layout", "flou") == "bandeau":
        return render_bandeau(src, work, cfg, with_subs)
    lg = v.get("logo") or {}
    logo = resolve(lg.get("file", "assets/logo.png"))
    use_logo = bool(lg.get("enabled")) and logo.exists()
    if lg.get("enabled") and not logo.exists():
        log.warning("Logo activé mais fichier absent (%s) : vidéo sans logo", logo)
    graph = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=20:5[bg];"
        "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30[base];"
    )
    last = "base"
    if use_logo:
        width = int(lg.get("width", 220))
        margin = int(lg.get("margin", 150))
        # En haut : dans la bande floue au-dessus de l'image. En bas : au-dessus de l'interface YouTube.
        y = str(margin) if lg.get("position", "top") == "top" else "H-h-200"
        opacity = float(lg.get("opacity", 1.0))
        graph += (f"[1:v]scale={width}:-1,format=rgba,colorchannelmixer=aa={opacity}[lg];"
                  f"[base][lg]overlay=(W-w)/2:{y}[withlogo];")
        last = "withlogo"
    graph += f"[{last}]format=yuv420p"
    if with_subs:
        graph += (",subtitles=subs.srt:force_style='FontName=DejaVu Sans,FontSize=16,Bold=1,"
                  "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,"
                  "Outline=2,Shadow=0,Alignment=2,MarginV=70'")
    graph += "[v]"
    out = work / "final.mp4"
    inputs = ["-i", src.name] + (["-i", str(logo)] if use_logo else [])
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", *inputs,
        "-t", str(v["max_duration"]), "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(v.get("crf", 21)),
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        "-movflags", "+faststart", out.name,
    ]
    subprocess.run(cmd, cwd=work, check=True)
    return out


def build_meta(cfg, c):
    m = cfg["metadata"]
    raw = c["title"].strip()
    if len(raw.replace(" ", "")) < m.get("min_title_length", 4):  # titres du genre « x »
        raw = m.get("fallback_title") or "Moment fou en live"
    fields = {"title": raw, "channel": c["channel"],
              "url": c["url"], "credit_url": c["credit_url"]}

    def fill(tpl):  # remplacement simple : pas de str.format sur un modèle modifiable
        for k, v in fields.items():
            tpl = tpl.replace("{" + k + "}", str(v))
        return tpl

    title = fill(m["title"]).replace("<", "").replace(">", "")
    desc = fill(m["description"])
    tiktok_caption = fill(m.get("tiktok_caption", "{title}"))
    return {"title": title[:100], "description": desc[:4900],
            "tags": m.get("tags", []), "tiktok_caption": tiktok_caption[:2200]}


# --------------------------------------------------------------------------
# Publication : YouTube
# --------------------------------------------------------------------------
class YouTubeUploader:
    SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

    def __init__(self, ycfg):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_file = resolve(ycfg["token_file"])
        creds = Credentials.from_authorized_user_file(str(token_file), self.SCOPES)
        if not creds.valid:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
        self.yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        self.cfg = ycfg

    def upload(self, path, meta):
        from googleapiclient.http import MediaFileUpload

        body = {
            "snippet": {"title": meta["title"], "description": meta["description"],
                        "tags": meta["tags"], "categoryId": str(self.cfg.get("category_id", 20))},
            "status": {"privacyStatus": self.cfg.get("privacy", "public"),
                       "selfDeclaredMadeForKids": False},
        }
        req = self.yt.videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(str(path), mimetype="video/mp4",
                                       chunksize=8 * MB, resumable=True))
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp["id"]


# --------------------------------------------------------------------------
# Publication : TikTok (Content Posting API)
# --------------------------------------------------------------------------
class TikTokUploader:
    API = "https://open.tiktokapis.com/v2"

    def __init__(self, tcfg):
        self.cfg = tcfg
        self.token_file = resolve(tcfg["token_file"])
        self._refresh()

    def _refresh(self):
        tok = json.loads(self.token_file.read_text())
        r = requests.post(
            self.API + "/oauth/token/",
            data={"client_key": self.cfg["client_key"], "client_secret": self.cfg["client_secret"],
                  "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        data = r.json()
        if "access_token" not in data:
            raise RuntimeError(f"TikTok : rafraîchissement du jeton impossible ({data})")
        self.token_file.write_text(json.dumps(data))
        os.chmod(self.token_file, 0o600)
        self.headers = {"Authorization": "Bearer " + data["access_token"],
                        "Content-Type": "application/json; charset=UTF-8"}

    def _post(self, path, payload):
        r = requests.post(self.API + path, headers=self.headers, json=payload, timeout=60)
        data = r.json()
        err = data.get("error") or {}
        if r.status_code != 200 or err.get("code", "ok") != "ok":
            raise RuntimeError(f"TikTok {path} : {err.get('code')} {err.get('message')}")
        return data.get("data") or {}

    def upload(self, path, meta):
        size = path.stat().st_size
        # Règles TikTok : morceaux de 5 à 64 Mo, le dernier absorbe le reste
        if size <= 64 * MB:
            chunk, count = size, 1
        else:
            chunk = 10 * MB
            count = size // chunk
        source = {"source": "FILE_UPLOAD", "video_size": size,
                  "chunk_size": chunk, "total_chunk_count": count}

        if self.cfg.get("mode", "inbox") == "direct":
            info = self._post("/post/publish/creator_info/query/", {})
            wanted = self.cfg.get("privacy_level", "PUBLIC_TO_EVERYONE")
            options = info.get("privacy_level_options") or ["SELF_ONLY"]
            privacy = wanted if wanted in options else options[-1]
            data = self._post("/post/publish/video/init/", {
                "post_info": {"title": meta["tiktok_caption"], "privacy_level": privacy,
                              "disable_duet": False, "disable_comment": False,
                              "disable_stitch": False, "video_cover_timestamp_ms": 1000},
                "source_info": source})
        else:
            data = self._post("/post/publish/inbox/video/init/", {"source_info": source})

        upload_url, publish_id = data["upload_url"], data["publish_id"]
        with open(path, "rb") as f:
            for i in range(count):
                start = i * chunk
                length = chunk if i < count - 1 else size - start
                r = requests.put(upload_url, data=f.read(length), timeout=300, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{start + length - 1}/{size}"})
                if r.status_code not in (200, 201, 206):
                    raise RuntimeError(f"TikTok envoi morceau {i + 1}/{count} : HTTP {r.status_code}")

        for _ in range(30):
            time.sleep(10)
            st = self._post("/post/publish/status/fetch/", {"publish_id": publish_id})
            status = st.get("status")
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return publish_id
            if status == "FAILED":
                raise RuntimeError(f"TikTok : échec ({st.get('fail_reason')})")
        log.warning("TikTok : statut final non confirmé pour %s", publish_id)
        return publish_id


# --------------------------------------------------------------------------
# Boucle principale
# --------------------------------------------------------------------------
def run(cfg, dry_run=False, only=None, force=False):
    state = State(resolve(cfg.get("state_db", "clipbot.db")))
    max_tries = cfg.get("max_tries", 3)

    # Pause : seuls les essais et les publications demandées à la main passent
    if cfg.get("paused") and not dry_run and not only and not force:
        log.info("ClipBot est en pause : aucune publication automatique.")
        return

    if force:
        log.info("Lancement manuel : heures d'activité et intervalle ignorés.")
    if not dry_run and not only and not force:
        sch = cfg.get("schedule") or {}
        # Heures d'activité (heure de Paris)
        try:
            from zoneinfo import ZoneInfo
            hour = datetime.now(ZoneInfo(sch.get("timezone", "Europe/Paris"))).hour
        except Exception:
            hour = datetime.now().hour
        h_start, h_end = int(sch.get("start_hour", 0)), int(sch.get("end_hour", 24))
        if not (h_start <= hour < h_end):
            log.info("Hors des heures d'activité (%dh-%dh) : rien à faire.", h_start, h_end)
            return
        # Intervalle entre deux clips
        interval = int(sch.get("interval_minutes", 0))
        last = state.last_post_time()
        if interval and last:
            wait = last + timedelta(minutes=interval) - datetime.now(timezone.utc)
            if wait.total_seconds() > 60:
                log.info("Prochain clip dans %d min.", wait.total_seconds() // 60 + 1)
                return

    candidates = twitch_candidates(cfg, state) + youtube_candidates(cfg)
    seen = set()  # un même clip ne doit être traité qu'une fois par passage
    if only:
        # « Publier maintenant » : ce clip-là seulement, sauf s'il est déjà publié
        row = state.get(only)
        candidates = [c for c in candidates if c["source_id"] == only][:1]
        if row and row["status"] == "done":
            log.info("Ce clip est déjà publié.")
            return
        if not candidates:
            log.error("Clip %s introuvable parmi les clips disponibles (trop ancien ?).", only)
            notify(cfg, "ClipBot : clip introuvable", only, 4, ["warning"])
            return
        budget = 1
    else:
        candidates = [c for c in candidates
                      if c["source_id"] not in seen and not seen.add(c["source_id"])
                      and not state.is_done(c["source_id"], max_tries)]
        candidates.sort(key=lambda c: c["views"], reverse=True)
        if cfg.get("rotation", True):
            # Le créateur publié il y a le plus longtemps passe en premier
            # (à égalité : le clip le plus vu). Tri stable : garde l'ordre par vues.
            last_by_channel = state.last_post_by_channel()
            candidates.sort(key=lambda c: last_by_channel.get(c["channel"].lower(), ""))
        budget = min(cfg.get("max_posts_per_run", 2),
                     cfg.get("max_posts_per_day", 6) - state.posted_last_24h())
    if budget <= 0 or not candidates:
        log.info("Rien à publier (budget %d, %d candidat(s))", budget, len(candidates))
        return

    pub = cfg["publish"]
    yt = tt = browser = None
    if not dry_run:
        try:
            needs_browser = any(pub[p].get("enabled") and pub[p].get("method") == "browser"
                                for p in ("youtube", "tiktok"))
            if needs_browser:
                from browser_upload import BrowserSession, BrowserTikTok, BrowserYouTube
                browser = BrowserSession(pub.get("browser") or {}, resolve, resolve("screenshots"))
            if pub["youtube"].get("enabled"):
                yt = (BrowserYouTube(browser, pub["youtube"].get("privacy", "public"))
                      if pub["youtube"].get("method") == "browser" else YouTubeUploader(pub["youtube"]))
            if pub["tiktok"].get("enabled"):
                tt = (BrowserTikTok(browser) if pub["tiktok"].get("method") == "browser"
                      else TikTokUploader(pub["tiktok"]))
        except Exception as e:
            log.exception("Connexion aux plateformes impossible")
            notify(cfg, "ClipBot : erreur de connexion", str(e), 4, ["warning"])
            if browser:
                browser.close()
            return
    try:
        _process(cfg, state, candidates, budget, yt, tt, dry_run)
    finally:
        if browser:
            browser.close()


def _process(cfg, state, candidates, budget, yt, tt, dry_run):
    pub = cfg["publish"]

    work_root = resolve(cfg.get("work_dir", "work"))
    work_root.mkdir(parents=True, exist_ok=True)
    posted = 0
    for c in candidates:
        if posted >= budget:
            break
        log.info("→ %s | %s (%d vues)", c["channel"], c["title"], c["views"])
        work = Path(tempfile.mkdtemp(prefix="clip-", dir=work_root))
        try:
            src = download(c, work, cfg)
            try:
                subs = transcribe(src, work / "subs.srt", cfg)
            except Exception as e:  # les sous-titres ne doivent jamais bloquer un clip
                log.warning("Sous-titres impossibles (%s), clip publié sans", e)
                subs = False
            final = render(src, work, cfg, subs)
            meta = build_meta(cfg, c)

            if dry_run:
                preview = resolve("preview")
                preview.mkdir(exist_ok=True)
                dest = preview / (c["source_id"].replace(":", "_") + ".mp4")
                shutil.copy(final, dest)
                log.info("   [dry-run] %s | titre : %s", dest, meta["title"])
                posted += 1
                continue

            state.save(c)
            row = state.get(c["source_id"]) or {}
            links = []
            if yt:
                yt_id = row.get("youtube_id") or yt.upload(final, meta)
                state.save(c, youtube_id=yt_id)
                links.append("YouTube : " + (f"https://youtube.com/shorts/{yt_id}"
                                             if yt_id != "navigateur" else "publié"))
            if tt:
                tt_id = row.get("tiktok_id") or tt.upload(final, meta)
                state.save(c, tiktok_id=tt_id)
                inbox = pub["tiktok"].get("method") != "browser" and pub["tiktok"].get("mode", "inbox") != "direct"
                links.append("TikTok : " + ("brouillon dans ta boîte" if inbox else "publié"))
            if yt or tt:
                time.sleep(random.uniform(20, 60))  # pause entre deux clips
            state.save(c, status="done", error=None)
            posted += 1
            notify(cfg, f"Clip publié : {c['channel']}", f"{meta['title']}\n" + "\n".join(links),
                   3, ["clapper"])
        except Exception as e:
            log.exception("Échec pour %s", c["source_id"])
            if not dry_run:
                state.fail(c, str(e))
            notify(cfg, "ClipBot : échec", f"{c['channel']} – {c['title']}\n{e}", 4, ["warning"])
        finally:
            shutil.rmtree(work, ignore_errors=True)
    log.info("Terminé : %d clip(s) traité(s)", posted)


def main():
    ap = argparse.ArgumentParser(description="ClipBot")
    ap.add_argument("-c", "--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="monte sans publier (dossier preview/)")
    ap.add_argument("--force", action="store_true",
                    help="lancement manuel : ignore la pause, les heures d'activité et l'intervalle")
    ap.add_argument("--only", metavar="ID", help="publie tout de suite ce clip-là (id de l'historique)")
    ap.add_argument("--check-browser", action="store_true",
                    help="vérifie la connexion TikTok/YouTube du navigateur (captures dans screenshots/)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    from logging.handlers import RotatingFileHandler
    (BASE / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(BASE / "logs" / "clipbot.log", maxBytes=2 * MB,
                                      backupCount=3, encoding="utf-8")])

    # Un seul passage à la fois (minuteur systemd + bouton de l'interface web)
    import fcntl
    lock = open(BASE / "clipbot.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Un passage est déjà en cours, on s'arrête.")
        return
    cfg = load_config(args.config)
    if args.check_browser:
        from browser_upload import BrowserSession
        s = BrowserSession(cfg["publish"].get("browser") or {}, resolve, resolve("screenshots"))
        try:
            for name, (ok, shot, url) in s.check_logins().items():
                print(f"{name:8} {'✅ connecté' if ok else '❌ non connecté'}  {url}  ({shot})")
        finally:
            s.close()
        return
    run(cfg, dry_run=args.dry_run, only=args.only, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
