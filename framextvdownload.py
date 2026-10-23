#!/usr/bin/env python3
"""
framextvdownload.py - download videos from framextv.cc

Run:
  python3 framextvdownload.py
Then paste a link, pick quality, pick subtitles, download.
Subtitles are saved as separate .srt files.
"""
import argparse, base64, collections, concurrent.futures as cf, glob, json, math, os, re, shutil, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.request
from urllib.parse import urljoin

try:
    from textual.app import App, ComposeResult
    from textual.containers import Center, Vertical
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        Button, Footer, Header, Input, Label, ListItem, ListView,
        LoadingIndicator, ProgressBar, Sparkline, Static,
    )
    from rich.text import Text
    from rich.markup import escape
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")
ORIGIN = "https://www.movy.bz"
REFERER = "https://www.movy.bz/"
MOVY = "https://api.wecollege.net"
PROVIDERS = ["atlanta", "dallas", "miami"]
API_BASES = ["https://api.framextv.cc", "https://api.framextv.xyz", "https://api.framextv.tech"]
QUIET = False
_PRINT_LOCK = threading.Lock()


def ts():
    return time.strftime("%H:%M:%S,") + f"{int(time.time() * 1000) % 1000:03d}"


def log(msg):
    if not QUIET:
        with _PRINT_LOCK:
            print(f"[{ts()}] {msg}", flush=True)


def fmt_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} {u}"
        n /= 1024


def pick_numbered(title, options, default=0):
    print(title)
    for i, (label, _) in enumerate(options):
        print(f"[ {i} ] {label}")
    while True:
        try:
            s = input(f"> [{default}] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return options[default][1]
        if not s:
            return options[default][1]
        if s.isdigit() and int(s) < len(options):
            return options[int(s)][1]
        print("  invalid number, try again")


def parse_watch_url(url):
    """Return (tmdb:int, media_type, season, episode) or None.
    Accepts /watch/<id>[/<season>/<ep>] AND /movie/<id> (detail-page
    format — same TMDB id, so Chain/sources work the same)."""
    m = re.search(r"/watch/(\d+)(?:/(\d+)/(\d+))?", url)
    if m:
        tmdb = int(m.group(1))
        if m.group(2):
            return tmdb, "tv", int(m.group(2)), int(m.group(3))
        return tmdb, "movie", None, None
    m = re.search(r"/movie/(\d+)", url)
    if m:
        return int(m.group(1)), "movie", None, None
    return None


# ---------------------------------------------------------------- subtitle (merged from framextv_subs.py)
# The framextv player merges stream subtitles + /api/subtitles, dedup by URL.
# /api/subtitles needs no auth; movy has 0-1 tracks (Our Father: 0).
# Metadata says format "vtt" but strem.io files are actually already SRT,
# titan files are real VTT — download by content, not metadata.
LANG_NAMES = {
    "en": "English", "ms": "Malay", "id": "Indonesian", "es": "Spanish",
    "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "pt-br": "Portuguese (Brazil)", "ja": "Japanese", "zh": "Chinese",
    "zh-tw": "Chinese (Traditional)", "ko": "Korean", "ar": "Arabic",
    "ru": "Russian", "nl": "Dutch", "sv": "Swedish", "no": "Norwegian",
    "da": "Danish", "fi": "Finnish", "pl": "Polish", "cs": "Czech",
    "hu": "Hungarian", "el": "Greek", "he": "Hebrew", "tr": "Turkish",
    "th": "Thai", "vi": "Vietnamese", "uk": "Ukrainian", "ro": "Romanian",
    "hr": "Croatian", "bs": "Bosnian", "sl": "Slovenian", "et": "Estonian",
    "sq": "Albanian", "fa": "Persian",
}


def normalize_lang(raw):
    """Return a normalized language code. Movy: 'English - eng' -> 'en'.
    Spaces around the hyphen are REQUIRED so zh-tw / pt-br are not broken."""
    raw = (raw or "").strip().lower()
    if raw == "tw":
        return "zh-tw"
    m = re.match(r"^(.+?)\s+-\s+([a-z]{2,3})$", raw)
    if m:
        return {"eng": "en"}.get(m.group(2), m.group(2))
    if raw in LANG_NAMES:
        return raw
    rev = {v.lower(): k for k, v in LANG_NAMES.items()}
    return rev.get(raw, raw)


def normalize_sub(s, source=""):
    """Normalize the subtitle dict shape across sources: url/lang/label/source."""
    lang = normalize_lang(s.get("lang") or s.get("language") or "")
    label = (s.get("label") or "").replace("Subtitles • ", "").replace("Subtitles ", "").strip()
    if not label and s.get("lang"):
        # movy: 'English - eng' -> label 'English'
        m = re.match(r"^(.+?)\s+-\s+[a-z]{2,3}$", (s.get("lang") or "").strip(), re.I)
        label = m.group(1).strip() if m else ""
    return {"url": s.get("url", ""), "lang": lang, "label": label,
            "format": s.get("format") or "", "source": source or s.get("source") or ""}


def sub_display(s):
    name = LANG_NAMES.get(s["lang"], s["label"] or s["lang"] or "?")
    label = s["label"] or ""
    if not label or label.lower() == name.lower():
        return name
    if label.lower().startswith(name.lower()):
        return label
    return f"{name} ({label})"


def merge_subs(primary, secondary):
    seen, out = set(), []
    for s in primary + secondary:
        if s["url"] and s["url"] not in seen:
            seen.add(s["url"])
            out.append(s)
    return out


def fetch_framextv_subs(tmdb, media_type, season, episode, page=""):
    """Source A: /api/subtitles (stdlib, no requests/auth needed).
    Return (normalized subtitle list, successful base) or ([], None)."""
    for base in API_BASES:
        q = f"{base}/api/subtitles?id={tmdb}&type={media_type}"
        if media_type == "tv":
            q += f"&season={season or 1}&episode={episode or 1}"
        try:
            req = urllib.request.Request(q, headers={"User-Agent": UA, "Referer": "https://framextv.cc/"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            continue
        subs = d.get("subtitles") or []
        if d.get("success") or subs:
            return [normalize_sub(s, s.get("source") or "framextv") for s in subs if s.get("url")], base
    return [], None


def parse_sub_selection(raw, subs):
    """Turn the user choice into a track list. raw: 'all' | 'none' | 'ms,en' | '0,5'.
    Return a track list (may be empty). Invalid values are skipped silently."""
    if raw is None:
        return None
    raw = str(raw).strip().lower()
    if raw in ("", "none", "no"):
        return []
    # "0" alone = first track index, NOT none (old bug: "0" -> []).
    if raw == "all":
        return list(subs)
    out, seen = [], set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hit = []
        if part.isdigit() and int(part) < len(subs):
            hit = [subs[int(part)]]
        else:
            hit = [s for s in subs if s["lang"] == part]
        for s in hit:
            if id(s) not in seen:
                seen.add(id(s))
                out.append(s)
    return out


def subs_for_selection(selected, current):
    """Match an old subtitle choice to the CURRENT subtitle list.
    Called at subtitle download time, not at pick time — so after
    a sources refresh, or for the next queued title in the TUI, old
    URLs (expired / other title) are not carried over. Rules:
    - selected None -> None (all current tracks)
    - selected [] -> [] (none)
    - if all chosen URLs are still in the current list -> use
      them directly (most accurate: still distinguishes en vs en-2)
    - otherwise, match by language code + count per code (e.g. pick
      1x en -> take the first en of the current title, not all en)."""
    if selected is None:
        return None
    if not selected:
        return []
    if current is None:
        return list(selected)
    urls = {s.get("url") for s in current}
    if all(s.get("url") in urls for s in selected if s.get("url")):
        return list(selected)
    need = {}
    for s in selected:
        code = s.get("lang") or "sub"
        need[code] = need.get(code, 0) + 1
    out = []
    for s in current:
        code = s.get("lang") or "sub"
        if need.get(code, 0) > 0:
            out.append(s)
            need[code] -= 1
    return out


def prompt_sub_selection(subs):
    """Interactive CLI picker (called AFTER quality is picked).
    Type numbers / comma-separated language codes, 'all', or empty Enter = none."""
    print("\nPick subtitles (separate .srt files, NOT muxed into the MP4):")
    for i, s in enumerate(subs):
        print(f"  [{i:2d}] {s['lang'] or '?':6s} {sub_display(s)}  [{s['source']}]")
    try:
        raw = input("Pick (e.g. 25 / ms,en / all / empty Enter = none) > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return []
    if not raw:
        return []
    return parse_sub_selection(raw, subs)


def resolve_sub_selection(ch, a):
    """Decide which subtitles to download for one video, by flag / mode.
    Called after the Chain is built (subtitle list already exists)."""
    if getattr(a, "no_subs", False):
        return []
    subs_flag = getattr(a, "subs", None)
    if subs_flag is not None:
        return parse_sub_selection(subs_flag, ch.all_subtitles)
    if getattr(a, "parallel", 1) and a.parallel > 1:
        return []  # prompts from many threads would fight over stdin
    if sys.stdin.isatty():
        return prompt_sub_selection(ch.all_subtitles)
    return list(ch.all_subtitles)


class HttpError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


# ---------------------------------------------------------------- cipher XOR (from Player JS, functions w/ce/le/ae/oe/se)
_M32 = 0xFFFFFFFF


def _u32(x):
    return x & _M32


def _imul(a, b):
    return _u32(a * b)


def _ae(e):
    t = e & _M32
    t ^= t >> 16
    t = _u32(t * 2246822507)
    t ^= t >> 13
    t = _u32(t * 3266489909)
    return _u32(t ^ (t >> 16))


def _oe(e, t):
    n = t & 31
    return e & _M32 if n == 0 else _u32((e << n) | (e >> (32 - n)))


def _se(e):
    t = 2166136261
    for ch in e:
        t = _imul(t ^ ord(ch), 16777619)
    return _ae(t)


def _ce(seed, tmdb):
    n = [0] * 61
    r = [0] * 61
    i = _ae(_se(seed) ^ _ae(_u32(tmdb ^ 2654435769)))
    for e in range(8):
        t = i % 61
        i = _oe(_u32(i + 2654435769), 7 + (7 & e))
        n[t] = _u32(i ^ _ae(i))
        r[t] = 1
        i = _ae(_u32(i + t))
    return {"s": n, "isSet": r, "acc": _ae(_u32(2779096485 ^ i))}


def _le(st, t):
    n = st["acc"]
    r = n % 61
    i = 0
    a = 0
    if st["isSet"][r]:
        i = 0xFFFFFFFF
        a = st["s"][r]
    o = _imul(t + 1, 2654435769)
    s = n
    c = _u32(a ^ o)
    n = _ae(_u32(_oe(_u32((s ^ c | s & c & i) + n), 31 & r) ^ _oe(n, (31 & r) * 7)) + 2654435769)
    st["s"][r] = n
    st["isSet"][r] = 1
    st["acc"] = n
    return n


def decrypt_sources(ciphertext, seed, tmdb):
    import base64
    t = ciphertext.strip().replace("-", "+").replace("_", "/")
    while len(t) % 4:
        t += "="
    r = bytearray(base64.b64decode(t))
    if len(r) <= 4:
        raise ValueError("ciphertext too short")
    st = _ce(seed, tmdb)
    a = len(r)
    o = 0
    s = 0
    while s < a:
        e = _le(st, o)
        o += 1
        r[s] ^= e & 255
        s += 1
        if s < a:
            r[s] ^= (e >> 8) & 255
            s += 1
        if s < a:
            r[s] ^= (e >> 16) & 255
            s += 1
        if s < a:
            r[s] ^= (e >> 24) & 255
            s += 1
    if bytes(r[:4]) != b"mvm1":
        raise ValueError("bad magic prefix")
    return bytes(r[4:]).decode("utf-8")


# ---------------------------------------------------------------- HTTP (urllib + fallback curl, gaya pinayflix)
def fetch(url, referer, timeout=40):
    global _USE_CURL
    if _USE_CURL:
        return _fetch_curl(url, referer, timeout)
    try:
        return _fetch_urllib(url, referer, timeout)
    except HttpError:
        raise
    except Exception as e:
        log(f"urllib failed ({type(e).__name__}), switching to curl...")
        _USE_CURL = True
        return _fetch_curl(url, referer, timeout)


def _fetch_urllib(url, referer, timeout=40):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer, "Origin": ORIGIN, "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit() and len(data) != int(cl):
                raise IOError(f"truncated {len(data)}/{cl}")
            return data
    except urllib.error.HTTPError as e:
        raise HttpError(e.code)


def _fetch_curl(url, referer, timeout=40):
    if not shutil.which("curl"):
        raise RuntimeError("curl is not in PATH")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".fxdl")
    tmp.close()
    try:
        p = subprocess.run(
            ["curl", "-sS", "--max-time", str(timeout),
             "-D", "-", "-o", tmp.name,
             "-A", UA, "-e", referer, "-H", f"Origin: {ORIGIN}",
             "-H", "Accept-Encoding: identity", url],
            capture_output=True, text=True, timeout=timeout + 10)
        if p.returncode != 0:
            raise IOError(f"curl rc={p.returncode}: {p.stderr.strip()[:150]}")
        code, clen = 0, None
        for line in p.stdout.splitlines():
            if line.startswith("HTTP/"):
                try:
                    code = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
            elif line.lower().startswith("content-length:"):
                try:
                    clen = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        with open(tmp.name, "rb") as f:
            data = f.read()
        if code in (401, 403, 410):
            raise HttpError(code)
        if code and not 200 <= code < 300:
            raise IOError(f"curl HTTP {code}")
        if clen and len(data) != clen:
            raise IOError(f"terpotong {len(data)}/{clen}")
        # An empty 200 response (b"") is RETURNED, not an error: the segments
        # layer decides (a 0-byte segments = a hole from the server -> skipped).
        return data
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


_USE_CURL = False


def fetch_retry(url, referer, tries=4, base_sleep=2, min_bytes=1):
    last = None
    for i in range(tries):
        try:
            data = fetch(url, referer)
            if len(data) >= min_bytes:
                return data
            last = IOError("response too small")
        except HttpError as e:
            if e.code in (401, 403, 410):
                raise
            last = e
        except Exception as e:
            last = e
        time.sleep(base_sleep * (i + 1))
    raise RuntimeError(f"failed to fetch {url[:90]}: {last}")


# ---------------------------------------------------------------- Chain: URL -> sources -> playlist -> segments
class Chain:
    """Hold the current segments URL. refresh() is shared & thread-safe (by generation)."""

    def __init__(self, page, quality=None):
        self.page, self.quality = page, quality
        self.lock = threading.Lock()
        self.gen = 0
        self.build()

    def _title(self, tmdb, media_type):
        for base in API_BASES:
            try:
                d = json.loads(fetch_retry(
                    f"{base}/api/details?type={media_type}&id={tmdb}", self.page, tries=2, min_bytes=10))
                if d.get("success") and d.get("title"):
                    return d["title"]
            except Exception:
                continue
        return ""

    def _sources(self, tmdb, media_type, season, episode):
        """Seed + sources MUST share one session (IP-bound seed)."""
        if not HAS_REQUESTS:
            raise RuntimeError("needs 'pip install requests' (seed is tied to the connection)")
        from urllib.parse import urlencode
        sess = requests.Session()
        headers = {"User-Agent": UA, "Origin": ORIGIN, "Referer": REFERER}
        last_err = None
        for prov in PROVIDERS:
            try:
                seed = sess.get(f"{MOVY}/seed?mediaId={tmdb}", headers=headers, timeout=30).json()["seed"]
                q = {"mediaType": media_type, "tmdbId": str(tmdb), "enc": "2", "seed": seed}
                if media_type == "tv":
                    q["seasonId"] = str(season)
                    q["episodeId"] = str(episode)
                r = sess.get(f"{MOVY}/{prov}/sources?{urlencode(q)}", headers=headers, timeout=30)
                if r.status_code != 200:
                    # Include the body message so debugging is not blind (before: code only)
                    last_err = f"{prov} HTTP {r.status_code}: {r.text.strip()[:100]}"
                    continue
                raw = r.text.strip()
                if raw.startswith("{"):
                    last_err = f"{prov}: {raw[:80]}"
                    continue
                data = json.loads(decrypt_sources(raw, seed, tmdb))
                if data.get("sources"):
                    return data, prov
                last_err = f"{prov}: no sources"
            except Exception as e:
                last_err = f"{prov}: {type(e).__name__}: {str(e)[:100]}"
        raise RuntimeError(f"all providers failed ({last_err})")

    def build(self):
        parsed = parse_watch_url(self.page)
        if not parsed:
            raise RuntimeError("Invalid URL (e.g. https://framextv.cc/watch/335032/1/1 or /movie/550)")
        tmdb, media_type, season, episode = parsed
        self.tmdb, self.media_type, self.season, self.episode = tmdb, media_type, season, episode
        self.title = self._title(tmdb, media_type)
        data, prov = self._sources(tmdb, media_type, season, episode)
        self.provider = prov
        # Movy subtitles (in sources) + framextv subtitles (/api/subtitles),
        # merged the way the player does (dedup by URL). movy usually has 0-1 tracks.
        movy_subs = [normalize_sub(s, f"movy/{prov}") for s in (data.get("subtitles") or []) if s.get("url")]
        fx_subs, fx_base = fetch_framextv_subs(tmdb, media_type, season, episode, self.page)
        self.movy_subtitles, self.fx_subtitles, self.fx_base = movy_subs, fx_subs, fx_base
        self.all_subtitles = merge_subs(fx_subs, movy_subs)
        self.subtitles = self.all_subtitles  # old-code compatible

        # pick quality: quality = max height (e.g. 720) or None = highest
        def qnum(s):
            m = re.search(r"(\d+)", s.get("quality", ""))
            return int(m.group(1)) if m else 0
        variants = sorted(data["sources"], key=qnum, reverse=True)
        self.variants = [{"quality": s["quality"], "h": qnum(s), "url": s["url"]} for s in variants]
        if self.quality:
            ok = [v for v in self.variants if v["h"] <= self.quality]
            chosen = ok[0] if ok else self.variants[-1]
        else:
            chosen = self.variants[0]
        self.chosen = chosen
        if self.gen == 0 and not QUIET:
            with _PRINT_LOCK:
                print("    qualities available:", ", ".join(v["quality"] for v in self.variants), flush=True)
                print(f"    picked: {chosen['quality']} (provider {prov})", flush=True)

        md = fetch_retry(chosen["url"], REFERER, min_bytes=100).decode("utf-8", "ignore")
        urls, durs, pending = [], [], None
        for ln in md.splitlines():
            ln = ln.strip()
            if ln.startswith("#EXTINF:"):
                pending = ln[8:].split(",")[0]
            elif ln and not ln.startswith("#") and pending is not None:
                urls.append(urljoin(chosen["url"], ln))
                durs.append(pending)
                pending = None
        if not urls:
            raise RuntimeError("no segments in the media playlist")
        seq = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", md)

        self.seg_urls, self.extinfs = urls, durs
        self.seq = int(seq.group(1)) if seq else 0
        self.gen += 1

    def snapshot(self, n):
        with self.lock:
            return self.gen, self.seg_urls[n - 1], REFERER

    def refresh_if_stale(self, seen_gen):
        with self.lock:
            if self.gen == seen_gen:
                log("Segments ended (403?) -> refresh sources + playlist...")
                self.build()
            return self.gen


# ---------------------------------------------------------------- download
def _vtt_ts(t):
    """Normalize a VTT timestamp to SRT: HH:MM:SS.mmm or MM:SS.mmm
    -> HH:MM:SS,mmm. Before: MM:SS.mmm was dropped entirely (HH-only regex)."""
    parts = t.split(":")
    if len(parts) == 2:
        mm, ss = parts
        return f"00:{mm}:{ss.replace('.', ',')}"
    return t.replace(".", ",")


def vtt_to_srt(text):
    """Convert WebVTT to SRT (if the provider gives .vtt).
    Supports HH:MM:SS.mmm AND MM:SS.mmm timestamps; settings after
    the timestamp (e.g. position:...) are ignored, HTML tags are removed."""
    ts_pat = re.compile(r"(\d+:\d+(?::\d+)?\.\d+)\s*-->\s*(\d+:\d+(?::\d+)?\.\d+)")
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines) and not ts_pat.search(lines[i]):
        i += 1
    n = 0
    while i < len(lines):
        line = lines[i].strip()
        m = ts_pat.search(line)
        if m:
            n += 1
            out.append(str(n))
            out.append(f"{_vtt_ts(m.group(1))} --> {_vtt_ts(m.group(2))}")
            i += 1
            while i < len(lines) and lines[i].strip():
                out.append(re.sub(r"<[^>]+>", "", lines[i]))
                i += 1
            out.append("")
        else:
            i += 1
    return "\n".join(out) + "\n"


def print_summary(res):
    sep = "=" * 62
    with _PRINT_LOCK:
        print(sep)
        print(f"Status   : {res['status']}")
        print(f"Video    : {res['vid']}")
        print(f"Quality  : {res['quality']}")
        if res["size"]:
            print(f"Size     : {fmt_size(res['size'])}")
        print(f"Location : {res['path']}")
        for s in res.get("subs") or []:
            print(f"Subtitle : {s}")
        print(f"Time     : {res['elapsed']:.1f}s")
        print(sep, flush=True)


class DownloadJob:
    """One download job. Shared by CLI and TUI (one engine only).
    states: 0=waiting 1=active 2=done 3=failed 4=empty from server (skipped).
    Controls: cancel_ev, run_ev (clear = pause)."""

    def __init__(self, page, ch, workers=8, clean=True, emit=None, no_subs=False, selected_subs=None):
        self.page, self.ch, self.workers, self.clean, self.no_subs = page, ch, workers, clean, no_subs
        # selected_subs: None = all merged tracks (old-code compatible);
        # [] = no subtitles; track list = only tracks the user picked.
        self.selected_subs = selected_subs
        self.emit = emit or (lambda kind, **kw: None)
        parsed = parse_watch_url(page)
        tmdb, media_type, season, episode = parsed
        self.vid = str(tmdb) + (f"S{season}E{episode}" if season else "")
        self.dir = os.path.join(os.getcwd(), f"dl_framextv_{self.vid}")
        self.total = len(ch.seg_urls)
        self.states = [0] * self.total
        self.done_count = 0
        self.bytes_done = 0
        self.samples = collections.deque()
        self.lock = threading.Lock()
        self.cancel_ev = threading.Event()
        self.run_ev = threading.Event()
        self.run_ev.set()
        self.phase = "init"
        self.merge_pct = 0.0
        self.est_total = 0
        self.dl_start = None
        self.t0 = None
        title = getattr(ch, "title", "") or ""
        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title).strip(" .")[:80]
        suffix = f" [{self.vid}]" if self.vid else ""
        self.out = os.path.join(self.dir, f"{safe}{suffix}.mp4" if safe else f"framextv-{self.vid}.mp4")
        self.result = {"status": "FAILED", "size": 0, "path": "", "verify": "", "elapsed": 0.0}

    def _sp(self, n):
        return os.path.join(self.dir, f"seg-{n:04d}.ts")

    def _valid(self, n):
        p = self._sp(n)
        return os.path.exists(p) and os.path.getsize(p) > 1000

    def _sleep(self, s):
        self.cancel_ev.wait(s)

    def _fail(self, msg):
        self.result["status"] = "FAILED"
        self.emit("log", msg=msg)
        self.emit("error", msg=msg)

    def _stopped(self):
        for f in glob.glob(os.path.join(self.dir, "*.part")):
            try:
                os.remove(f)
            except OSError:
                pass
        self.result["status"] = "STOPPED"
        self.emit("log", msg="Stopped. Run again to resume.")
        self.emit("stopped")

    def speed(self):
        now = time.time()
        with self.lock:
            while self.samples and self.samples[0][0] < now - 5:
                self.samples.popleft()
            b = sum(x[1] for x in self.samples)
        span = min(5.0, max(now - (self.dl_start or now), 0.5))
        return b / span

    def est_bytes(self):
        if self.done_count >= 5:
            return self.bytes_done / self.done_count * self.total
        return self.est_total

    def eta(self):
        spd = self.speed()
        if spd <= 0 or self.done_count == 0:
            return None
        return (self.total - self.done_count) * (self.bytes_done / self.done_count) / spd

    def _dl_one(self, n):
        ch = self.ch
        for attempt in range(8):
            while not self.run_ev.is_set():
                if self.cancel_ev.is_set():
                    return False
                self.run_ev.wait(0.3)
            if self.cancel_ev.is_set():
                return False
            gen, url, referer = ch.snapshot(n)
            self.states[n - 1] = 1
            try:
                data = fetch(url, referer)
                if len(data) == 0:
                    # Server answered HTTP 200 but 0 bytes: the segments is genuinely empty
                    # from the source (an encoding hole, confirmed on 720p E2#264/E3#244).
                    # Do not retry in vain — mark empty & skip at merge time.
                    self.states[n - 1] = 4
                    with self.lock:
                        self.done_count += 1
                        dc = self.done_count
                    self.emit("log", msg=f"Segment {n} empty from server (0 bytes) - skipped")
                    self.emit("progress", done=dc, total=self.total)
                    return True
                if len(data) <= 1000:
                    raise IOError("invalid segments")
                tmp = self._sp(n) + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, self._sp(n))
                with self.lock:
                    self.bytes_done += len(data)
                    self.done_count += 1
                    self.samples.append((time.time(), len(data)))
                    dc = self.done_count
                self.states[n - 1] = 2
                self.emit("progress", done=dc, total=self.total)
                return True
            except HttpError as e:
                if e.code in (401, 403, 410):
                    try:
                        ch.refresh_if_stale(gen)
                    except Exception as ex:
                        self.emit("log", msg=f"Refresh failed: {ex}")
                        self._sleep(5)
                else:
                    self._sleep(min(30, 3 * (attempt + 1)))
            except Exception:
                self._sleep(min(20, 2 * (attempt + 1)))
        self.states[n - 1] = 3
        return False

    def download_subtitles(self):
        """Download ONLY the picked subtitles as SEPARATE .srt files
        next to the MP4 (not muxed/burned — the MP4 merge uses the local.m3u8 video
        only, no subtitle input). Name: <video>.<code>[-SDH][-2].srt
        If selected_subs is None (old code / no picker): all tracks."""
        if self.no_subs:
            return []
        all_subs = getattr(self.ch, "all_subtitles", None)
        if all_subs is None:
            all_subs = getattr(self.ch, "subtitles", []) or []
        # Match again to the CURRENT list (important after a sources refresh
        # and for the next TUI queue title — do not download the old title's URLs)
        subs = subs_for_selection(self.selected_subs, all_subs)
        if not subs:
            self.emit("log", msg="No subtitles picked")
            return []
        base = os.path.splitext(self.out)[0]
        # Count repeats per code in this pick only (for -2, -3)
        counts = {}
        for s in subs:
            code = s.get("lang") or "sub"
            counts[code] = counts.get(code, 0) + 1
        seen = {}
        saved = []
        for s in subs:
            url = s.get("url", "")
            if not url:
                continue
            code = s.get("lang") or "sub"
            seen[code] = seen.get(code, 0) + 1
            dup = f"-{seen[code]}" if counts[code] > 1 and seen[code] > 1 else ""
            sdh = "-SDH" if "SDH" in (s.get("label") or "").upper() else ""
            out = f"{base}.{code}{sdh}{dup}.srt"
            if os.path.exists(out) and os.path.getsize(out) > 100:
                saved.append(out)
                continue
            try:
                data = self._fetch_subtitle(s)
                # Strip the BOM if present — otherwise the WEBVTT check fails and
                # the VTT file is saved verbatim as .srt (broken).
                while data.startswith(b"\xef\xbb\xbf"):
                    data = data[3:]
                if data.lstrip().startswith(b"WEBVTT"):
                    data = vtt_to_srt(data.decode("utf-8", "ignore")).encode("utf-8")
                # If it is not WEBVTT, the content is already SRT (strem.io case) -> verbatim
                if data.count(b"-->") == 0 or len(data) < 100:
                    raise IOError("empty subtitle file / no cues")
                with open(out, "wb") as f:
                    f.write(data)
                saved.append(out)
                self.emit("log", msg=f"Subtitle OK: {os.path.basename(out)}")
            except Exception as e:
                self.emit("log", msg=f"Subtitle failed ({code}): {e}")
        return saved

    def _fetch_subtitle(self, s):
        """Direct first (stdlib, framextv Referer, no movy Origin),
        framextv proxy fallback, last the old fetch_retry."""
        url = s["url"]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://framextv.cc/"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            if len(data) > 100:
                return data
        except Exception:
            pass
        ref = "https://www.opensubtitles.org/" if ("opensubtitles" in url or "strem.io" in url or "vdrk" in url) \
            else "https://www.movy.bz/" if ("movy" in url or "wecollege" in url or "vidapi" in url) else ""
        for base in API_BASES:
            proxy = f"{base}/api/subtitles/proxy?url={urllib.parse.quote(url)}"
            if ref:
                proxy += "&ref=" + base64.b64encode(ref.encode()).decode().replace("=", "")
            try:
                req = urllib.request.Request(proxy, headers={"User-Agent": UA, "Referer": "https://framextv.cc/"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                if len(data) > 100:
                    return data
            except Exception:
                continue
        return fetch_retry(url, REFERER, tries=3, min_bytes=100)

    def run(self):
        self.t0 = time.time()
        try:
            self._run()
        except KeyboardInterrupt:
            self.cancel_ev.set()
            self.run_ev.set()
            self._stopped()
        except Exception as e:
            self._fail(f"Ralat tak dijangka: {e}")
        finally:
            self.result["elapsed"] = time.time() - self.t0
            self.phase = "finished"
        return self.result

    def _run(self):
        ch = self.ch
        os.makedirs(self.dir, exist_ok=True)
        for f in glob.glob(os.path.join(self.dir, "*.part")):
            try:
                os.remove(f)
            except OSError:
                pass
        # Video already exists: pick the largest MP4 (not raw glob order
        # if there is more than one mp4 in the folder), and match
        # self.out to the real file — otherwise, subtitles are saved
        # under a different out name, and the player will not auto-load them.
        cands = [c for c in glob.glob(os.path.join(self.dir, "*.mp4"))
                 if os.path.getsize(c) > 0]
        if cands:
            cand = max(cands, key=os.path.getsize)
            sz = os.path.getsize(cand)
            self.out = cand
            self.phase = "subs"
            subs = [] if self.no_subs else self.download_subtitles()
            self.result.update(status="SKIPPED", size=sz, path=cand, subs=subs)
            self.emit("log", msg=f"Already exists, skip: {cand}")
            self.emit("done", path=cand, size=sz, verify="", skipped=True)
            return

        self.phase = "prepare"
        dur = sum(float(d) for d in ch.extinfs)
        if self.est_total:
            free = shutil.disk_usage(self.dir).free
            if free < self.est_total * 1.6:
                return self._fail(f"Not enough disk space: need ~{fmt_size(self.est_total * 1.6)}, have {fmt_size(free)}")

        for n in range(1, self.total + 1):
            if self._valid(n):
                self.states[n - 1] = 2
                self.done_count += 1
                self.bytes_done += os.path.getsize(self._sp(n))
        if self.done_count:
            self.emit("log", msg=f"Resume: {self.done_count}/{self.total} segments already here")

        self.phase = "download"
        self.dl_start = time.time()
        for rnd in range(1, 4):
            need = [n for n in range(1, self.total + 1) if self.states[n - 1] not in (2, 4)]
            if not need:
                break
            self.emit("log", msg=f"Round {rnd}: {len(need)} segments")
            for n in need:
                self.states[n - 1] = 0
            ex = cf.ThreadPoolExecutor(max_workers=self.workers)
            try:
                results = list(ex.map(self._dl_one, need))
            except KeyboardInterrupt:
                self.cancel_ev.set()
                self.run_ev.set()
                ex.shutdown(wait=False, cancel_futures=True)
                return self._stopped()
            ex.shutdown()
            if self.cancel_ev.is_set():
                return self._stopped()
            fails = [n for n, ok in zip(need, results) if not ok]
            self.emit("log", msg=f"Round {rnd} finished, failed: {len(fails)}")
            if fails and rnd < 3:
                self._sleep(10)
        need = [n for n in range(1, self.total + 1) if self.states[n - 1] not in (2, 4)]
        if need:
            return self._fail(f"{len(need)} segments still failing - press r to retry / change network")
        empty = [n for n in range(1, self.total + 1) if self.states[n - 1] == 4]
        if empty:
            gap = sum(float(ch.extinfs[n - 1]) for n in empty)
            self.emit("log", msg=f"Note: {len(empty)} empty segments skipped (hole ~{gap:.0f}s in the video)")

        self.phase = "merge"
        self.emit("log", msg="Merge MP4...")
        target = math.ceil(max(float(d) for d in ch.extinfs))
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}",
                 f"#EXT-X-MEDIA-SEQUENCE:{ch.seq}"]
        for n, d in enumerate(ch.extinfs, start=1):
            if self.states[n - 1] == 4:
                continue
            lines += [f"#EXTINF:{d},", f"seg-{n:04d}.ts"]
        lines.append("#EXT-X-ENDLIST")
        open(os.path.join(self.dir, "local.m3u8"), "w").write("\n".join(lines) + "\n")

        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
               "-allowed_extensions", "ALL", "-i", "local.m3u8",
               "-c", "copy", "-movflags", "+faststart", self.out]
        p = subprocess.Popen(cmd, cwd=self.dir, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        tail = []
        for line in p.stdout:
            line = line.strip()
            mm = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if mm:
                self.merge_pct = min(100.0, int(mm.group(1)) / 1e6 / max(dur, 1) * 100)
            elif line and not re.match(r"^\w+=\S*$", line):
                tail.append(line)
            if self.cancel_ev.is_set():
                p.kill()
                break
        p.wait()
        if self.cancel_ev.is_set():
            try:
                os.remove(self.out)
            except OSError:
                pass
            return self._stopped()
        if p.returncode != 0:
            return self._fail("ffmpeg failed: " + " | ".join(tail[-3:]))
        self.merge_pct = 100.0

        verify = ""
        if shutil.which("ffprobe"):
            self.phase = "verify"
            pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", self.out], capture_output=True, text=True)
            try:
                d = float(pr.stdout.strip())
                ok = abs(d - dur) <= max(3, dur * 0.02)
                verify = f"Verify: {d:.0f}s vs {dur:.0f}s " + ("OK ✓" if ok else "DIFFERENT - video may be broken ⚠")
            except ValueError:
                verify = "Verify: ffprobe could not read the duration ⚠"
            self.emit("log", msg=verify)

        size = os.path.getsize(self.out)
        self.phase = "subs"
        subs = [] if self.no_subs else self.download_subtitles()
        self.result.update(status="SUCCESS", size=size, path=self.out, verify=verify, subs=subs)
        if self.clean:
            for n in range(1, self.total + 1):
                try:
                    os.remove(self._sp(n))
                except OSError:
                    pass
            try:
                os.remove(os.path.join(self.dir, "local.m3u8"))
            except OSError:
                pass
        self.emit("done", path=self.out, size=size, verify=verify, skipped=False)


def download_one_video(page, a, tag=""):
    t0 = time.time()
    res = {"vid": "?", "status": "FAILED", "quality": "-", "size": 0, "path": "", "elapsed": 0.0}
    prefix = f"[{tag}] " if tag else ""

    def finish():
        res["elapsed"] = time.time() - t0
        print_summary(res)
        return res

    parsed = parse_watch_url(page)
    if not parsed:
        print(f"  Invalid URL: {page}")
        return finish()
    tmdb, media_type, season, episode = parsed
    res["vid"] = str(tmdb) + (f"S{season}E{episode}" if season else "")
    with _PRINT_LOCK:
        print(f"# Download {res['vid']} :")
    existing = [c for c in glob.glob(os.path.join(os.getcwd(), f"dl_framextv_{res['vid']}", "*.mp4"))
                if os.path.getsize(c) > 0]
    if existing:
        # Video already exists, but subtitles still need to be fetched (separate files)
        log(f"Already exists, skip video: {existing[0]}")
        try:
            ch0 = Chain(page, a.quality)
            # Video already exists: the subtitle picker still runs (after quality)
            selected0 = resolve_sub_selection(ch0, a)
            job0 = DownloadJob(page, ch0, a.workers, a.clean,
                               lambda kind, **kw: log(kw["msg"]) if kind == "log" else None,
                               no_subs=getattr(a, "no_subs", False),
                               selected_subs=selected0)
            # Match the output name to the existing file — otherwise,
            # subtitles are saved under the new title's name, the player will not
            # auto-load them (this path does not go through DownloadJob._run).
            job0.out = existing[0]
            subs = job0.download_subtitles()
        except Exception as e:
            log(f"Subtitle failed: {e}")
            subs = []
        res.update(status="SKIPPED", size=os.path.getsize(existing[0]), path=existing[0], subs=subs)
        return finish()

    log("Start")
    try:
        ch = Chain(page, a.quality)
    except Exception as e:
        log(f"Chain failed: {e}")
        return finish()
    res["quality"] = ch.chosen["quality"]
    log(f"Metadata: {len(ch.seg_urls)} segments, {res['quality']}" + (f", {ch.title}" if ch.title else ""))
    log(f"Subtitles available: {len(ch.all_subtitles)} tracks "
        f"(framextv {len(getattr(ch, 'fx_subtitles', []))} + movy {len(getattr(ch, 'movy_subtitles', []))})")

    # STEP 2 (after picking resolution): pick subtitles
    selected = resolve_sub_selection(ch, a)
    if getattr(a, "no_subs", False) or not selected:
        log("Subtitles: none picked")
    else:
        log("Subtitles picked: " + ", ".join(f"{s['lang']} ({sub_display(s)})" for s in selected))

    marks = set()

    def emit(kind, **kw):
        if kind == "log":
            log(prefix + kw["msg"])
        elif kind == "progress":
            d, t = kw["done"], kw["total"]
            for q in (t // 4, t // 2, 3 * t // 4):
                if q > 0 and d == q and q not in marks:
                    marks.add(q)
                    log(f"{prefix}Segment {d}/{t}")

    job = DownloadJob(page, ch, a.workers, a.clean, emit,
                      no_subs=getattr(a, "no_subs", False), selected_subs=selected)
    r = job.run()
    res.update(status=r["status"], size=r["size"], path=r["path"] or job.out, subs=r.get("subs") or [])
    return finish()


# ---------------------------------------------------------------- TUI ala pinayflix
if HAS_TEXTUAL:

    _GLYPHS = {
        "F": ("┏━╸", "┣╸ ", "╹  "), "R": ("┏━┓", "┣┳┛", "╹┗╸"),
        "A": ("┏━┓", "┣━┫", "╹ ╹"), "M": ("┏┓┏┓", "┃┗┛┃", "╹  ╹"),
        "E": ("┏━╸", "┣━╸", "┗━╸"), "X": ("╻ ╻", "┏╋┛", "╹ ╹"),
        "T": ("┏━┓", " ┃ ", " ╹ "), "V": ("╻ ╻", "┃ ┃", "┗━┛"),
    }

    def _lerp(c1, c2, t):
        return "#%02x%02x%02x" % tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

    def build_logo(word="FRAMEXTV", c1=(122, 215, 255), c2=(245, 140, 255)):
        rows = ["", "", ""]
        for ch in word:
            g = _GLYPHS[ch]
            for r in range(3):
                rows[r] += g[r] + " "
        width = max(len(r) for r in rows)
        t = Text(justify="center")
        for ri, row in enumerate(rows):
            for ci, c in enumerate(row):
                t.append(c, style="bold " + _lerp(c1, c2, ci / max(width - 1, 1)))
            if ri < 2:
                t.append("\n")
        return t

    def read_clipboard_url():
        cmds = [["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"],
                ["pbpaste"], ["termux-clipboard-get"]]
        for c in cmds:
            if shutil.which(c[0]):
                try:
                    out = subprocess.run(c, capture_output=True, text=True, timeout=0.6).stdout.strip()
                except Exception:
                    continue
                if ("/watch/" in out or "/movie/" in out) and "\n" not in out and len(out) < 300:
                    return out
        return ""

    class TuiHomeScreen(Screen):
        BINDINGS = [("q", "app.quit", "Quit"),
                    ("escape", "clear_url", "Clear")]

        def __init__(self, start_url=""):
            super().__init__()
            self.start_url = start_url

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Vertical(id="home"):
                yield Static(build_logo(), id="logo")
                yield Label("framextv HLS downloader · movy seed decrypt · auto refresh", id="tagline")
                yield Static("", id="chips")
                yield Input(placeholder="🔗  paste a framextv link here…",
                            id="url", value=self.start_url)
                yield Label("", id="status")
                yield Button("⬇  DOWNLOAD", id="yoink", variant="primary")
                yield LoadingIndicator(id="spin")
                yield Label("e.g. https://framextv.cc/watch/335032/1/1", id="example")
            yield Footer()

        def on_mount(self):
            home = self.query_one("#home")
            home.border_title = " framextv "
            home.border_subtitle = " Enter = download "
            self.query_one("#spin", LoadingIndicator).display = False
            ff = "[black on #9ece6a] ffmpeg ✓ [/]" if shutil.which("ffmpeg") \
                else "[white on #f7768e] ffmpeg ✗ [/]"
            rq = "[black on #9ece6a] requests ✓ [/]" if HAS_REQUESTS \
                else "[white on #f7768e] requests ✗ [/]"
            self.query_one("#chips", Static).update(
                f"{ff}  {rq}  [black on #7aa2f7] 8 workers [/]  [black on #bb9af7] auto quality [/]")
            inp = self.query_one("#url", Input)
            if not inp.value:
                clip = read_clipboard_url()
                if clip:
                    inp.value = clip
                    self.query_one("#status", Label).update("📋 link taken from clipboard")
            inp.focus()
            home.styles.opacity = 0.0
            home.styles.animate("opacity", 1.0, duration=0.5)

        def action_clear_url(self):
            inp = self.query_one("#url", Input)
            if not inp.disabled:
                inp.value = ""
                inp.focus()

        def on_input_changed(self, event: Input.Changed) -> None:
            v = event.value.strip()
            inp = self.query_one("#url", Input)
            st = self.query_one("#status", Label)
            m = parse_watch_url(v)
            inp.set_class(bool(m), "valid")
            inp.set_class(bool(v) and not m, "invalid")
            if m:
                st.update(f"[#9ece6a]✓ valid link · TMDB {m[0]}" + (f" S{m[2]}E{m[3]}" if m[2] else " (movie)") + "[/]")
            elif v:
                st.update("[#e0af68]⚠ link must be framextv.cc/watch/ID or /movie/ID (add /season/ep for TV)[/]")
            else:
                st.update("")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "yoink":
                self.submit()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.submit()

        def set_busy(self, busy, msg=""):
            self.query_one("#spin", LoadingIndicator).display = busy
            self.query_one("#yoink", Button).disabled = busy
            self.query_one("#url", Input).disabled = busy
            if busy:
                self.query_one("#status", Label).update("[#7aa2f7]fetching seed & sources…[/]")
            elif msg:
                self.query_one("#status", Label).update(msg)

        def submit(self):
            url = self.query_one("#url", Input).value.strip()
            if not parse_watch_url(url):
                self.query_one("#status", Label).update("[#f7768e]✗ URL must be framextv.cc/watch/<id> or /movie/<id>[/]")
                self.query_one("#url", Input).add_class("invalid")
                return
            self.set_busy(True)
            threading.Thread(target=self.fetch_meta, args=(url,), daemon=True).start()

        def fetch_meta(self, url):
            try:
                ch = Chain(url, None)
            except Exception as e:
                self.app.call_from_thread(self.fetch_failed, str(e)[:120])
                return
            self.app.call_from_thread(self.fetch_done, ch, url)

        def fetch_failed(self, msg):
            self.set_busy(False, f"[#f7768e]✗ Failed: {msg}[/]")
            self.query_one("#url", Input).focus()

        def fetch_done(self, ch, url):
            self.set_busy(False, "")
            self.app.push_screen(TuiQualityScreen(ch, url))

    class TuiQualityScreen(Screen):
        BINDINGS = [("q", "app.quit", "Quit")]

        def __init__(self, ch, url):
            super().__init__()
            self.ch = ch
            self.url = url
            self.busy = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Center():
                with Vertical(id="box"):
                    yield Label("Pick quality:", id="qtitle")
                    total_dur = sum(float(d) for d in self.ch.extinfs)
                    items = []
                    for v in self.ch.variants:
                        star = " ★ auto" if v["quality"] == self.ch.chosen["quality"] else ""
                        items.append(ListItem(Label(
                            f"{v['quality']}   {len(self.ch.seg_urls)} seg · {total_dur:.0f}s{star}")))
                    yield ListView(*items, id="qlist")
                    yield LoadingIndicator(id="qspin")
                    yield Label("↑↓ pick · Enter download · 0-9 quick · Esc back", id="hint")
            yield Footer()

        def on_mount(self):
            self.query_one("#qspin", LoadingIndicator).display = False
            self.query_one("#qlist", ListView).focus()

        def action_back(self):
            if not self.busy:
                self.app.pop_screen()

        def on_key(self, event):
            if event.key == "escape":
                self.action_back()
            elif event.key.isdigit() and not self.busy:
                i = int(event.key)
                if i < len(self.ch.variants):
                    self.choose(self.ch.variants[i])

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            if self.busy:
                return
            idx = self.query_one("#qlist", ListView).index
            if idx is None or idx >= len(self.ch.variants):
                return
            self.choose(self.ch.variants[idx])

        def choose(self, v):
            if self.busy:
                return
            if v["quality"] == self.ch.chosen["quality"]:
                self.app.push_screen(TuiSubtitleScreen(self.ch, self.url))
                return
            self.busy = True
            self.query_one("#qspin", LoadingIndicator).display = True
            self.query_one("#hint", Label).update("Re-fetching for the picked quality...")
            threading.Thread(target=self.rebuild, args=(v,), daemon=True).start()

        def rebuild(self, v):
            try:
                ch2 = Chain(self.url, v["h"])
            except Exception as e:
                self.app.call_from_thread(self.rebuild_failed, str(e)[:120])
                return
            self.app.call_from_thread(self.rebuild_done, ch2)

        def rebuild_failed(self, msg):
            self.busy = False
            self.query_one("#qspin", LoadingIndicator).display = False
            self.query_one("#hint", Label).update(f"Failed: {msg}")

        def rebuild_done(self, ch2):
            self.app.push_screen(TuiSubtitleScreen(ch2, self.url))

    class TuiSubtitleScreen(Screen):
        """STEP 2 (after picking resolution): pick subtitles.
        Space/click a track = toggle track, Enter = start download,
        a = all, n = none, d = start download.
        Enter on a highlighted track that is not ticked yet will
        download that track (not a download without subtitles) — unless
        the user already pressed n (none). Clicking START DOWNLOAD instead
        follows the current ticks (no ticks + no n = highlighted track).
        Subtitles come out as separate .srt files next to the MP4, not muxed."""
        BINDINGS = [("q", "app.quit", "Quit"),
                    ("a", "select_all", "All"),
                    ("n", "select_none", "None"),
                    ("d", "start_download", "Start download"),
                    ("escape", "back", "Back")]

        def __init__(self, ch, url):
            super().__init__()
            self.ch, self.url = ch, url
            self.selected = set()
            # True only after the user presses n (none) — so the
            # highlight fallback in start() does not override the "none" intent.
            self._explicit_none = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Center():
                with Vertical(id="box"):
                    yield Label("Pick subtitles (separate .srt files):", id="stitle")
                    subs = getattr(self.ch, "all_subtitles", []) or []
                    if subs:
                        items = [ListItem(Label(self._item_text(i, s))) for i, s in enumerate(subs)]
                        yield ListView(*items, id="slist")
                    else:
                        yield Label("No subtitles for this title.", id="snone")
                    yield Button("⬇  START DOWNLOAD", id="gostart", variant="primary")
                    yield Label("Space/click pick · Enter start download · a all · n none · d start · Esc back", id="hint")
            yield Footer()

        def on_mount(self):
            # Focus the list so Enter/Space toggle right away; if there are no
            # subtitles, focus the button so Enter starts the download right away.
            if self.query("#slist"):
                self.query_one("#slist", ListView).focus()
            else:
                self.query_one("#gostart", Button).focus()

        def _toggle(self, idx):
            if idx is None:
                return
            subs = getattr(self.ch, "all_subtitles", []) or []
            if idx >= len(subs):
                return
            if idx in self.selected:
                self.selected.discard(idx)
            else:
                self.selected.add(idx)
            self._explicit_none = False
            self._refresh()

        def on_key(self, event):
            # BUG 2026-10-23: Enter used to trigger ListView.Selected =
            # toggle only, download never started. Now Enter = start
            # download (stop the event so Selected/Button do not double-fire).
            # Confirmed by the Textual 8.2.8 pilot: Enter on the list AND on
            # the button both become a single start() only, list clicks still
            # Selected = toggle, button clicks still Pressed = start.
            if event.key == "enter":
                self.start()
                event.stop()
            # Space in a Textual ListView does not trigger Selected — so
            # catch it here so the "Space toggle" hint really works.
            elif event.key == "space" and self.query("#slist"):
                self._toggle(self.query_one("#slist", ListView).index)
                event.stop()

        def _item_text(self, i, s):
            mark = "[x]" if i in self.selected else "[ ]"
            return f"{mark}  {s['lang'] or '?':6s} {sub_display(s)}  [{s['source']}]"

        def _refresh(self):
            lv = self.query_one("#slist", ListView) if self.query("#slist") else None
            if lv is None:
                return
            subs = getattr(self.ch, "all_subtitles", []) or []
            for i, item in enumerate(lv.children):
                item.query_one(Label).update(self._item_text(i, subs[i]))

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            self._toggle(self.query_one("#slist", ListView).index)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "gostart":
                self.start()

        def action_select_all(self):
            subs = getattr(self.ch, "all_subtitles", []) or []
            self.selected = set(range(len(subs)))
            self._explicit_none = False
            self._refresh()

        def action_select_none(self):
            self.selected = set()
            self._explicit_none = True
            self._refresh()

        def action_back(self):
            self.app.pop_screen()

        def action_start_download(self):
            self.start()

        def start(self):
            subs = getattr(self.ch, "all_subtitles", []) or []
            chosen = [subs[i] for i in sorted(self.selected) if i < len(subs)]
            if not chosen and not self._explicit_none and self.query("#slist"):
                # No ticks but a track is highlighted: download that
                # track, not a video without subtitles. Press n first if
                # you really want no subtitles.
                idx = self.query_one("#slist", ListView).index
                if idx is not None and idx < len(subs):
                    chosen = [subs[idx]]
            self.app.pop_screen()
            self.app.push_screen(TuiDownloadScreen(self.ch, self.url, selected_subs=chosen))

    def open_path(path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa
            else:
                for c in (["open"] if sys.platform == "darwin" else ["xdg-open", "termux-open"]):
                    if shutil.which(c):
                        subprocess.Popen([c, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return True
        except Exception:
            pass
        return False

    class SegGrid(Static):
        """Segment heatmap: green=done, yellow=active, red=failed, grey=waiting."""

        def __init__(self, job, **kw):
            super().__init__("", **kw)
            self.job = job

        def refresh_grid(self):
            w = max(self.size.width, 20)
            total = self.job.total
            per = max(1, math.ceil(total / (w * 6)))
            states = self.job.states[:]
            t = Text()
            cells = 0
            for i in range(0, total, per):
                grp = states[i:i + per]
                if 3 in grp:
                    col = "#f7768e"
                elif 1 in grp:
                    col = "#e0af68"
                elif all(s == 2 for s in grp):
                    col = "#9ece6a"
                elif 2 in grp:
                    col = "#4f8a5b"
                elif 4 in grp:
                    col = "#565f89"
                else:
                    col = "#3b4261"
                t.append("■", style=col)
                cells += 1
                if cells % w == 0 and i + per < total:
                    t.append("\n")
            self.update(t)

    def extract_urls(text):
        out = []
        for u in re.findall(r"https?://\S*?/(?:watch|movie)/\d+(?:/\d+/\d+)?\S*", text):
            u = u.rstrip(",;)'\"")
            if u not in out:
                out.append(u)
        return out

    class AddUrlScreen(ModalScreen):
        BINDINGS = [("escape", "cancel_add", "Cancel")]

        def __init__(self, exclude=""):
            super().__init__()
            self.exclude = exclude

        def compose(self) -> ComposeResult:
            with Vertical(id="addbox"):
                yield Label("➕ Add to queue", id="addtitle")
                yield Input(placeholder="🔗  paste one or more framextv links…", id="addurl")
                yield Label("", id="addstatus")
                yield Label("Enter add · Esc cancel", id="addhint")

        def on_mount(self):
            inp = self.query_one("#addurl", Input)
            clip = read_clipboard_url()
            if clip and clip != self.exclude:
                inp.value = clip
                self.query_one("#addstatus", Label).update("📋 taken from clipboard")
            inp.focus()

        def on_input_changed(self, e: Input.Changed) -> None:
            n = len(extract_urls(e.value))
            st = self.query_one("#addstatus", Label)
            if n:
                st.update(f"[#9ece6a]✓ {n} valid links[/]")
            elif e.value.strip():
                st.update("[#e0af68]⚠ no framextv /watch/<id> or /movie/<id> links[/]")
            else:
                st.update("")

        def on_input_submitted(self, e: Input.Submitted) -> None:
            urls = extract_urls(e.value)
            if not urls:
                self.query_one("#addstatus", Label).update("[#f7768e]✗ invalid link[/]")
                return
            self.dismiss(urls)

        def action_cancel_add(self):
            self.dismiss(None)

    class TuiDownloadScreen(Screen):
        BINDINGS = [("q", "app.quit", "Quit"),
                    ("a", "add_url", "Add video"),
                    ("p", "toggle_pause", "Pause/Resume"),
                    ("c", "cancel", "Cancel"),
                    ("n", "next_item", "Next"),
                    ("x", "unqueue", "Remove last"),
                    ("l", "toggle_log", "Log"),
                    ("r", "retry", "Retry"),
                    ("o", "open_folder", "Open folder"),
                    ("d", "again", "Download again")]

        PHASES = {"init": "⏳ Preparing…", "prepare": "🔎 Checking disk space…",
                  "download": "⬇  Download",
                  "merge": "🎞  Merge MP4", "verify": "🔍 Verify video…",
                  "subs": "💬 Subtitles…",
                  "finished": ""}
        ICONS = {"done": "[#9ece6a]✓[/]", "failed": "[#f7768e]✗[/]", "stopped": "[#e0af68]■[/]",
                 "running": "[#7aa2f7]▶[/]", "fetching": "[#7aa2f7]…[/]", "waiting": "[#787c99]○[/]"}

        def __init__(self, ch, url, workers=8, selected_subs=None):
            super().__init__()
            self.ch, self.url, self.n_workers = ch, url, workers
            # Subtitle choice from the picker screen (None = all, [] = none).
            # The next queue item uses the same choice, but it is rematched
            # by language for that title (subs_for_selection in
            # download_subtitles) — not the first title's URLs.
            self.selected_subs = selected_subs
            self.ev = __import__("queue").Queue()
            self.job = DownloadJob(url, ch, workers, True, self._emit,
                                   selected_subs=selected_subs)
            self.items = [{"url": url, "vid": self.job.vid, "status": "running",
                           "title": getattr(ch, "title", "") or ""}]
            self.cur_item = self.items[0]
            self.advancing = False
            self.logs = []
            self.state = "running"
            self.cancelling = False
            self.spark = [0.0] * 40
            self.tick_n = 0
            self.last_phase = None
            self._queue_shown = None

        def _emit(self, kind, **kw):
            if kind != "progress":
                self.ev.put((kind, kw))

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Vertical(id="dbox"):
                yield Label("", id="dtitle")
                yield Label("", id="dphase")
                yield ProgressBar(total=self.job.total, show_eta=False, id="bar")
                yield SegGrid(self.job, id="grid")
                yield Label("[#9ece6a]■[/] done  [#e0af68]■[/] active  [#f7768e]■[/] failed  [#565f89]■[/] empty  [#3b4261]■[/] waiting", id="legend")
                yield Sparkline(self.spark, summary_function=max, id="spark")
                yield Label("", id="dstat")
                yield Static("", id="queue")
                yield Static("", id="dlog")
                yield Label("", id="ddone")
            yield Footer()

        def on_mount(self):
            self.query_one("#queue", Static).display = False
            self._set_titles()
            self._start()
            self.set_interval(0.25, self.tick)

        def _set_titles(self):
            title = escape((getattr(self.ch, "title", "") or "")[:44])
            self.query_one("#dtitle", Label).update(f"⬇ {self.job.vid}" + (f"  ·  {title}" if title else ""))
            box = self.query_one("#dbox")
            box.border_title = f" {self.ch.chosen['quality']} "
            box.border_subtitle = " a add · p pause · c cancel · l log "

        def _start(self):
            self.state = "running"
            self.cancelling = False
            threading.Thread(target=self.job.run, daemon=True).start()
            self.refresh_bindings()

        def _waiting(self):
            return any(it["status"] == "waiting" for it in self.items)

        def check_action(self, action, parameters):
            st = self.state
            if action in ("toggle_pause", "cancel"):
                return st == "running" and not self.cancelling
            if action == "retry":
                return st in ("failed", "stopped") and self.cur_item["status"] in ("failed", "stopped")
            if action == "again":
                return st in ("done", "failed", "stopped") and not self._waiting() and not self.advancing
            if action == "open_folder":
                return st == "done"
            if action == "next_item":
                return st == "stopped" and self._waiting()
            if action == "unqueue":
                return self._waiting()
            return True

        def action_toggle_pause(self):
            if self.job.run_ev.is_set():
                self.job.run_ev.clear()
            else:
                self.job.run_ev.set()

        def action_cancel(self):
            self.cancelling = True
            self.job.cancel_ev.set()
            self.job.run_ev.set()
            self.refresh_bindings()

        def action_toggle_log(self):
            d = self.query_one("#dlog")
            d.display = not d.display

        def action_open_folder(self):
            open_path(os.path.dirname(self.job.out))

        def action_again(self):
            # Stack now: Home -> Quality -> Subtitle -> Download.
            # Before, popping 2x was enough (no subtitle screen); now you must
            # pop 3x to reach Home — if you pop 2x, the user lands
            # on the OLD title's quality screen and could download the old
            # title again without realising.
            for _ in range(3):
                if len(self.app.screen_stack) > 1:
                    self.app.pop_screen()

        def action_retry(self):
            self.cur_item["status"] = "running"
            self.job = DownloadJob(self.url, self.ch, self.n_workers, True, self._emit,
                                   selected_subs=self.selected_subs)
            self.query_one("#grid", SegGrid).job = self.job
            self.query_one("#bar", ProgressBar).update(total=self.job.total, progress=0)
            d = self.query_one("#ddone", Label)
            d.update("")
            d.remove_class("err")
            self.logs = []
            self._set_titles()
            self._start()

        def action_add_url(self):
            self.app.push_screen(AddUrlScreen(self.url), self._added)

        def _added(self, urls):
            if not urls:
                return
            active = {it["vid"] for it in self.items if it["status"] in ("waiting", "running", "fetching")}
            added = dup = 0
            for u in urls:
                parsed = parse_watch_url(u)
                vid = str(parsed[0]) + (f"S{parsed[2]}E{parsed[3]}" if parsed[2] else "")
                if vid in active:
                    dup += 1
                    continue
                self.items.append({"url": u, "vid": vid, "status": "waiting", "title": ""})
                active.add(vid)
                added += 1
            if added:
                self.app.notify(f"{added} videos added to queue" + (f" ({dup} duplicates skipped)" if dup else ""),
                                title="Queue")
            elif dup:
                self.app.notify("That video is already in the queue", severity="warning", title="Queue")
            self.refresh_bindings()

        def action_next_item(self):
            if self.state == "stopped" and self._waiting():
                self._advance()

        def action_unqueue(self):
            for it in reversed(self.items):
                if it["status"] == "waiting":
                    self.items.remove(it)
                    self.app.notify(f"Removed from queue: {it['vid']}", title="Queue")
                    break
            self.refresh_bindings()

        def _advance(self):
            it = next((i for i in self.items if i["status"] == "waiting"), None)
            if it is None:
                return
            it["status"] = "fetching"
            self.advancing = True
            self.state = "fetching"
            self.add_log(f"Next: {it['vid']} - fetching metadata…")
            self.refresh_bindings()
            threading.Thread(target=self._build_chain, args=(it, None),
                             daemon=True).start()

        def _build_chain(self, it, quality):
            try:
                ch = Chain(it["url"], quality)
            except Exception as e:
                self.app.call_from_thread(self._chain_failed, it, str(e)[:120])
                return
            self.app.call_from_thread(self._chain_ready, it, ch)

        def _chain_failed(self, it, msg):
            it["status"] = "failed"
            self.advancing = False
            self.add_log(f"{it['vid']} failed: {msg}")
            self.app.notify(f"{it['vid']}: {msg}", severity="error", title="Queue")
            self.state = "failed"
            self.refresh_bindings()

        def _chain_ready(self, it, ch):
            it.update(status="running", title=getattr(ch, "title", "") or "")
            self.ch, self.url, self.cur_item = ch, it["url"], it
            self.job = DownloadJob(it["url"], ch, self.n_workers, True, self._emit,
                                   selected_subs=self.selected_subs)
            self.query_one("#grid", SegGrid).job = self.job
            self.query_one("#bar", ProgressBar).update(total=self.job.total, progress=0)
            d = self.query_one("#ddone", Label)
            d.update("")
            d.remove_class("err")
            self.spark = [0.0] * 40
            self.last_phase = None
            self.logs = []
            self.query_one("#dlog", Static).update("")
            self._set_titles()
            self.advancing = False
            self._start()

        def add_log(self, msg):
            self.logs = (self.logs + [f"[{time.strftime('%H:%M:%S')}] {msg}"])[-4:]
            self.query_one("#dlog", Static).update(escape("\n".join(self.logs)))

        def _finish(self, state, text, err=False):
            self.state = state
            self.cancelling = False
            d = self.query_one("#ddone", Label)
            d.set_class(err, "err")
            d.update(text)
            self.query_one("#dbox").border_subtitle = {
                "done": " a add · o open folder · d again · q quit ",
                "failed": " a add · r retry · d back · q quit ",
                "stopped": " a add · r resume · n next · q quit "}.get(state, "")
            self.refresh_bindings()

        def render_queue(self):
            q = self.query_one("#queue", Static)
            show = len(self.items) > 1
            if show != self._queue_shown:
                q.display = show
                self._queue_shown = show
            if not show:
                return
            fin = [i for i in self.items if i["status"] in ("done", "failed", "stopped")][-2:]
            act = [i for i in self.items if i["status"] in ("running", "fetching")]
            wait = [i for i in self.items if i["status"] == "waiting"]
            lines = [f"[b]QUEUE[/b]  ·  {len(wait)} waiting"]
            for it in fin + act + wait[:5]:
                t = escape((it.get("title") or "")[:30])
                extra = "  [#787c99]menunggu[/]" if it["status"] == "waiting" else ""
                lines.append(f" {self.ICONS[it['status']]} {it['vid']}  {t}{extra}")
            if len(wait) > 5:
                lines.append(f"   [#787c99]+{len(wait) - 5} more…[/]")
            q.update("\n".join(lines))

        def tick(self):
            import queue as _q
            job = self.job
            bar = self.query_one("#bar", ProgressBar)
            try:
                while True:
                    kind, kw = self.ev.get_nowait()
                    if kind == "log":
                        self.add_log(kw["msg"])
                    elif kind == "done":
                        bar.update(total=100, progress=100)
                        vf = f"\n  {escape(kw['verify'])}" if kw.get("verify") else ""
                        head = "Already exists" if kw.get("skipped") else "✓ Done"
                        self.cur_item["status"] = "done"
                        self._finish("done", f"{head}: {escape(kw['path'])}\n  Saiz: {fmt_size(kw['size'])}{vf}")
                        self.app.notify("Download done", title=job.vid)
                    elif kind == "error":
                        self.cur_item["status"] = "failed"
                        self._finish("failed", f"✗ {escape(kw['msg'])}\n  Press r to retry", err=True)
                    elif kind == "stopped":
                        self.cur_item["status"] = "stopped"
                        extra = " · n to continue the queue" if self._waiting() else ""
                        self._finish("stopped", f"■ Stopped. Press r to resume{extra}")
            except _q.Empty:
                pass

            if self.state in ("done", "failed") and not self.advancing and self._waiting():
                self._advance()

            running = self.state == "running"
            if job.phase != self.last_phase:
                self.last_phase = job.phase
                if job.phase == "merge":
                    bar.update(total=100, progress=0)
            if self.state in ("running",):
                if job.phase == "merge":
                    bar.update(progress=job.merge_pct)
                elif job.phase in ("download", "prepare", "init"):
                    bar.update(total=job.total, progress=job.done_count)

            lab = self.query_one("#dphase", Label)
            if running:
                txt = self.PHASES.get(job.phase, "")
                if not job.run_ev.is_set():
                    txt = "⏸  Paused  (press p to resume)"
                if self.cancelling:
                    txt = "⏹  Cancelling…"
                if job.phase == "merge":
                    txt += f"  {job.merge_pct:.0f}%"
                lab.update(txt)
            elif self.state == "fetching":
                lab.update("🔎 Preparing the next video in the queue…")
            else:
                lab.update({"done": "✓ Done", "failed": "✗ Failed", "stopped": "■ Stopped"}.get(self.state, ""))

            spd = job.speed() if running else 0.0
            self.tick_n += 1
            if self.tick_n % 2 == 0:
                self.spark = (self.spark + [spd / 1e6])[-40:]
                self.query_one("#spark", Sparkline).data = self.spark
            eta = job.eta() if running else None
            eta_s = "--:--" if eta is None else f"{int(eta) // 60:02d}:{int(eta) % 60:02d}"
            failed = sum(1 for s in job.states if s == 3)
            est = job.est_bytes()
            self.query_one("#dstat", Label).update(
                f"{job.done_count}/{job.total} seg · {fmt_size(job.bytes_done)}"
                + (f" / ~{fmt_size(est)}" if est else "")
                + f" · {spd / 1e6:.1f} MB/s · ETA {eta_s}\n"
                f"{self.n_workers} workers · refresh ♻ {max(self.ch.gen - 1, 0)}× · failed {failed}")
            self.query_one("#grid", SegGrid).refresh_grid()
            self.render_queue()

    class FrameApp(App):
        TITLE = "framextv download"
        CSS = """
        Screen { align: center middle; }
        #box {
            width: 62; height: auto; max-height: 90%;
            border: solid #666666; padding: 1 2;
        }
        #logo { width: 1fr; text-align: center; text-style: bold; color: #7fd4ff; }
        #url { margin: 1 0; }
        #yoink { width: 100%; margin-top: 1; }
        #err { color: #ff6666; text-align: center; }
        #qtitle, #dtitle { text-style: bold; margin-bottom: 1; }
        #qlist { height: auto; max-height: 12; }
        #stitle { text-style: bold; margin-bottom: 1; }
        #slist { height: auto; max-height: 14; }
        #snone { color: #888888; margin: 1 0; }
        #hint { color: #888888; margin-top: 1; }
        #dstat { margin: 1 0; }
        #dlog { color: #888888; }
        #ddone { color: #7fff9f; text-style: bold; margin-top: 1; }
        #dbtns { margin-top: 1; }

        /* ---------- HOME ---------- */
        #home {
            width: 70; max-width: 96%; height: auto;
            border: round $primary; background: $surface;
            padding: 1 4 1 4;
        }
        #home Static, #home Label { background: transparent; }
        #home #logo { width: 1fr; content-align: center middle; height: 3; margin-top: 1; }
        #tagline { width: 1fr; text-align: center; color: $text-muted; margin-top: 1; }
        #chips { width: 1fr; text-align: center; margin: 1 0; height: 1; }
        #home Input {
            border: round $panel-lighten-2; background: $boost; margin-top: 1;
        }
        #home Input:focus { border: round $accent; }
        #home Input.valid { border: round #9ece6a; }
        #home Input.invalid { border: round #e0af68; }
        #status { width: 1fr; text-align: center; height: 1; margin: 1 0 0 0; }
        #home #yoink {
            width: 100%; margin-top: 1; height: 3; border: none;
            background: #7aa2f7; color: #1a1b26; text-style: bold;
            content-align: center middle;
        }
        #home #yoink:hover { background: #bb9af7; }
        #home #yoink:disabled { background: #3b4261; color: #787c99; }
        #home #spin { height: 1; margin-top: 1; color: $accent; }
        #example { width: 1fr; text-align: center; color: $text-disabled; margin-top: 1; }

        /* ---------- DOWNLOAD ---------- */
        #dbox {
            width: 76; max-width: 96%; height: auto;
            border: round $primary; background: $surface; padding: 1 3;
        }
        #dbox Label, #dbox Static { background: transparent; }
        #dtitle { text-style: bold; color: $accent; margin-bottom: 0; }
        #dphase { color: $text-muted; margin-bottom: 1; }
        #bar { width: 1fr; margin-bottom: 1; }
        #bar Bar { width: 1fr; }
        #bar Bar > .bar--bar { color: #7aa2f7; }
        #bar Bar > .bar--complete { color: #9ece6a; }
        #grid { height: auto; min-height: 2; margin-top: 0; }
        #legend { color: $text-muted; margin-bottom: 1; }
        #spark { height: 2; margin-bottom: 1; }
        #spark > .sparkline--max-color { color: #bb9af7; }
        #spark > .sparkline--min-color { color: #7aa2f7; }
        #dstat { margin-bottom: 1; height: auto; }
        #dlog { color: $text-muted; height: auto; }
        #ddone { color: #9ece6a; text-style: bold; margin-top: 1; height: auto; }
        #ddone.err { color: #f7768e; }

        /* ---------- QUEUE + MODAL ---------- */
        #queue {
            height: auto; margin-top: 1; padding: 0 1;
            border-left: thick #7aa2f7; background: transparent;
        }
        AddUrlScreen { align: center middle; background: $background 70%; }
        #addbox {
            width: 66; max-width: 96%; height: auto;
            border: round $accent; background: $surface; padding: 1 3;
        }
        #addbox Label { background: transparent; width: 1fr; }
        #addtitle { text-style: bold; color: $accent; }
        #addurl { margin-top: 1; border: round $panel-lighten-2; background: $boost; }
        #addurl:focus { border: round $accent; }
        #addstatus { height: 1; margin-top: 1; text-align: center; }
        #addhint { color: $text-muted; text-align: center; margin-top: 1; }
        """

        def __init__(self, start_url=""):
            super().__init__()
            self.start_url = start_url

        async def action_quit(self):
            for scr in self.screen_stack:
                j = getattr(scr, "job", None)
                if j is not None and j.phase != "finished":
                    j.cancel_ev.set()
                    j.run_ev.set()
            self.exit()

        def on_mount(self):
            try:
                self.theme = "tokyo-night"
            except Exception:
                pass
            self.push_screen(TuiHomeScreen(self.start_url))

    def run_tui(start_url=""):
        global QUIET
        QUIET = True
        try:
            FrameApp(start_url).run()
        finally:
            QUIET = False


def main():
    ap = argparse.ArgumentParser(description="Framextv HLS downloader — interactive mode + batch queue")
    ap.add_argument("page", nargs="*", default=[],
                    help="one or more framextv URLs (leave empty for interactive mode)")
    ap.add_argument("--quality", type=int, help="maximum height, e.g. 720 (default: highest)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--clean", dest="clean", action="store_true", default=True,
                    help="delete segments after finishing (this is now the default)")
    ap.add_argument("--no-clean", dest="clean", action="store_false",
                    help="KEEP .ts segments after finishing (debug only)")
    ap.add_argument("--parallel", type=int, default=1,
                    help="number of videos to download at once (default: 1 = one by one)")
    ap.add_argument("--no-subs", action="store_true", help="do not download subtitles (= --subs none)")
    ap.add_argument("--subs", default=None,
                    help="pick subtitles without a prompt: all | none | comma-separated codes (ms,en,id) | numbers (0,5). "
                         "If not given: interactive prompt per video; parallel/non-tty = none")
    a = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is not in PATH")
    if not HAS_REQUESTS:
        sys.exit("needs 'pip install requests' (seed is tied to the connection)")

    pages = list(a.page)
    interactive = not pages
    if interactive and HAS_TEXTUAL and sys.stdin.isatty():
        run_tui()
        return
    if interactive and not HAS_TEXTUAL:
        print("Note: the TUI needs 'pip install textual' — use CLI mode for now.")
    if interactive:
        print("Enter framextv URLs (one per line, empty line = start download):")
        while True:
            try:
                u = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not u:
                break
            pages.append(u)
        a.quality = pick_numbered("Pick quality:", [
            ("Auto (highest)", None),
            ("720p", 720),
            ("480p", 480),
            ("360p", 360),
        ], default=0)
        a.workers = pick_numbered("Pick workers (parallel downloads):", [
            ("8 (default)", 8),
            ("4 (safe, avoids throttling)", 4),
            ("16 (fast, throttling risk)", 16),
        ], default=0)
    pages = [p for p in pages if parse_watch_url(p)]
    if not pages:
        sys.exit("No valid URLs given.")

    if a.parallel and a.parallel > 1:
        if a.subs is None and not a.no_subs:
            print("Note: parallel mode without --subs -> subtitles are skipped "
                  "(prompts cannot fight over stdin). Use --subs ms,en,id to auto-pick.")
        # Parallel mode: each video gets its own job (own folder/state/emit).
        # Workers are split so total connections stay ~8 (avoid throttling).
        per_workers = max(2, a.workers // a.parallel)
        print(f"{len(pages)} videos, {a.parallel} at once, {per_workers} workers per video "
              f"(total ~{per_workers * a.parallel} connections)...", flush=True)

        def run_one(pg):
            import copy as _copy
            aa = _copy.copy(a)
            aa.workers = per_workers
            parsed = parse_watch_url(pg)
            vid = str(parsed[0]) + (f"S{parsed[2]}E{parsed[3]}" if parsed[2] else "")
            return download_one_video(pg, aa, tag=vid)

        with cf.ThreadPoolExecutor(max_workers=a.parallel) as ex:
            results = list(ex.map(run_one, pages))
        ok = sum(1 for r in results if r["status"] in ("SUCCESS", "SKIPPED"))
        fail = [r["vid"] for r in results if r["status"] not in ("SUCCESS", "SKIPPED")]
        print(f"\nDONE: {ok} succeeded, {len(fail)} failed"
              + (f" ({', '.join(fail)})" if fail else ""), flush=True)
        return

    print(f"{len(pages)} videos in queue. Downloading one by one...", flush=True)
    ok, fail = 0, []
    for i, pg in enumerate(pages, 1):
        parsed = parse_watch_url(pg)
        vid = str(parsed[0]) + (f"S{parsed[2]}E{parsed[3]}" if parsed[2] else "")
        print(f"\n===== VIDEO {i}/{len(pages)}: {vid} =====", flush=True)
        res = download_one_video(pg, a)
        st = res["status"]
        if st == "SUCCESS" or st == "SKIPPED":
            ok += 1
        elif st == "STOPPED":
            ans = input("Continue to the next video? [y/N] ").strip().lower()
            fail.append(f"{vid} (dihentikan)")
            if ans != "y":
                break
        else:
            fail.append(vid)
    print(f"\nDONE: {ok} succeeded, {len(fail)} failed"
          + (f" ({', '.join(fail)})" if fail else ""), flush=True)


if __name__ == "__main__":
    main()
