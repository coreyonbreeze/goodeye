#!/usr/bin/env python3
"""GoodEye: a local review board where AI agents submit creative work and a human signs off.

  goodeye submit FILE --id ID --reasoning R.json [--title T] [--scores S.json] [--context a,b] [--project P]
  goodeye submit --options O.json --id ID --reasoning R.json     a choice: the reviewer ranks options
  goodeye wait [--project P]       block until a verdict arrives, print it, exit (run it in the background)
  goodeye status [--project P]     list items and their latest state
  goodeye slot KEY ID... [--label] put competing items in one slot: approving one closes the others
  goodeye serve [--port 4400]      run the board (submit starts it if it is down)
  goodeye open                     open the board in the browser
  goodeye demo                     load sample items so you can try the board
  goodeye phone [--off]            let your phone open the board over Wi-Fi (token-protected, prints a QR code)
  goodeye notify --ntfy URL        push a phone notification when new work arrives (opt-in; sends title only)
  goodeye export DIR [--project P] copy approved final files plus a manifest, for handoff or upload

Store: $GOODEYE_HOME (default ~/.goodeye). Port: $GOODEYE_PORT (default 4400). Python 3.9+, standard library only.
"""
import argparse, atexit, signal, urllib.request, datetime, threading, http.server, http.cookies, webbrowser, zlib, struct, secrets, hmac, gzip, hashlib, io, json, mimetypes, os, re, shutil, socket, subprocess, sys, time, urllib.parse, uuid

__version__ = "0.3.0"
HOME = os.path.expanduser(os.environ.get("GOODEYE_HOME", "~/.goodeye"))
ASSETS = os.path.join(HOME, "assets")
DECISIONS = os.path.join(HOME, "decisions.jsonl")
DELIVERED = os.path.join(HOME, "delivered.json")
CONFIG = os.path.join(HOME, "config.json")
AGENTS = os.path.join(HOME, "agents")
REMINDED = os.path.join(HOME, "reminded.json")
TOKEN_FILE = os.path.join(HOME, "phone-token")
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
PORT = int(os.environ.get("GOODEYE_PORT", "4400"))
URL = f"http://localhost:{PORT}"
CONTEXTS = ["linkedin-banner", "linkedin-company-cover", "linkedin-post", "x-banner", "x-post", "instagram-post", "instagram-story", "email",
            "website-hero", "browser-tab", "youtube-thumbnail", "phone"]
OPEN = ("pending", "changes")          # still competing in a slot
FILLED = ("approved", "picked")       # holds a slot
LOCK = threading.Lock()
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
VERDICTS = ("approved", "changes", "rejected", "picked", "not_chosen", "reopened")
MAX_BODY = 1 << 20
for ext, typ in ((".webp", "image/webp"), (".avif", "image/avif"), (".mp4", "video/mp4"), (".m4v", "video/mp4"),
                 (".webm", "video/webm"), (".mov", "video/quicktime"), (".svg", "image/svg+xml")):
    mimetypes.add_type(typ, ext)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def die(msg):
    print(f"goodeye: {msg}", file=sys.stderr)
    sys.exit(2)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".gif":
        return "gif"
    if ext in (".mp4", ".mov", ".webm", ".m4v"):
        return "video"
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".avif"):
        return "image"
    die(f"unsupported file type {ext}")


# ---------- store ----------

def versions_of(asset_id):
    d = os.path.join(ASSETS, asset_id)
    if not os.path.isdir(d):
        return []
    metas = [read_json(os.path.join(d, v, "meta.json")) for v in os.listdir(d)]
    return sorted([m for m in metas if m], key=lambda m: m["seq"])


def decisions():
    out = []
    try:
        with open(DECISIONS) as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
    except OSError:
        pass
    return out


def all_items():
    decs = decisions()
    by_key = {}
    for d in decs:
        by_key.setdefault(f'{d["id"]}@{d["version"]}', []).append(d)
    items = []
    if os.path.isdir(ASSETS):
        for asset_id in os.listdir(ASSETS):
            vs = versions_of(asset_id)
            if not vs:
                continue
            for v in vs:
                v["decisions"] = by_key.get(f'{asset_id}@{v["version"]}', [])
                v["checks"] = checks_for(v)
                last = v["decisions"][-1]["verdict"] if v["decisions"] else "pending"
                v["status"] = "pending" if last == "reopened" else last
            latest = vs[-1]
            items.append({"id": asset_id, "project": latest.get("project", ""), "title": latest.get("title", asset_id),
                          "status": latest["status"], "updated": latest["submitted_at"], "slot": latest.get("slot"), "versions": vs})
    items.sort(key=lambda i: i["updated"], reverse=True)
    return items


# ---------- placement spec checks ----------

def image_size(path):
    """(width, height) from the file header for PNG, GIF, JPEG, WebP and simple SVG. None if unknown."""
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", head[16:24])
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", head[6:10])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        tag = head[12:16]
        if tag == b"VP8 ":
            w, h = struct.unpack("<HH", head[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if tag == b"VP8L":
            b = head[21:25]
            return 1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        if tag == b"VP8X":
            return 1 + int.from_bytes(head[24:27], "little"), 1 + int.from_bytes(head[27:30], "little")
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    if b"<svg" in head[:4096]:
        txt = head[:4096].decode("utf-8", "ignore")
        vb = re.search(r'viewBox="\s*[-\d.]+[ ,]+[-\d.]+[ ,]+([\d.]+)[ ,]+([\d.]+)', txt)
        if vb:
            return float(vb.group(1)), float(vb.group(2))
    return None


# Publisher guidance as of 2026. Warnings only: the reviewer decides.
SPECS = {
    "linkedin-banner": {"size": (1584, 396), "max_mb": 8},
    "linkedin-company-cover": {"size": (1128, 191)},
    "x-banner": {"size": (1500, 500), "max_mb": 2},
    "youtube-thumbnail": {"size": (1280, 720), "max_mb": 2},
    "instagram-story": {"ratio": (9 / 16, 9 / 16), "min_w": 1080},
    "instagram-post": {"ratio": (4 / 5, 1.91), "min_w": 1080},
    "linkedin-post": {"ratio": (4 / 5, 1.91)},
    "x-post": {"ratio": (9 / 16, 2.0)},
    "email": {"max_w": 1200, "gif_mb": 1, "max_mb": 3},
    "browser-tab": {"ratio": (1, 1), "min_w": 32},
    "website-hero": {"max_mb_image": 2, "max_mb_video": 10},
}


def spec_checks(path, kind, contexts, media=None):
    """Warnings for placements this file does not fit. Returns [{"context", "msg"}]."""
    if not contexts or not os.path.isfile(path):
        return []
    dims = image_size(path) if kind in ("image", "gif") else None
    if not dims and media and media.get("width"):
        dims = (media["width"], media["height"])
    mb = os.path.getsize(path) / 1048576
    out = []
    for c in contexts:
        r = SPECS.get(c)
        if not r:
            continue
        say = lambda msg: out.append({"context": c, "msg": msg})
        is_svg = path.lower().endswith(".svg")
        if dims and not is_svg:
            w, h = dims
            if "size" in r:
                tw, th = r["size"]
                if abs(w / h - tw / th) > 0.02:
                    say(f"{w}x{h} is not the {tw}x{th} shape ({tw / th:.2f}:1); the platform will crop it")
                elif w < tw:
                    say(f"{w}x{h} is smaller than {tw}x{th}; it will look soft")
            if "ratio" in r:
                lo, hi = r["ratio"]
                if not (lo - 0.02 <= w / h <= hi + 0.02):
                    say(f"aspect {w / h:.2f} is outside {lo:.2f} to {hi:.2f}; it will be cropped or letterboxed")
            if "min_w" in r and w < r["min_w"]:
                say(f"{w} px wide; at least {r['min_w']} px is recommended")
            if "max_w" in r and w > r["max_w"]:
                say(f"{w} px wide; emails show 600 px (1200 px for sharp screens)")
        if kind == "gif" and "gif_mb" in r and mb > r["gif_mb"]:
            say(f"{mb:.1f} MB GIF; keep email GIFs under {r['gif_mb']} MB so they load on phones")
        if "max_mb" in r and mb > r["max_mb"]:
            say(f"{mb:.1f} MB; the limit is about {r['max_mb']} MB")
        key = "max_mb_video" if kind == "video" else "max_mb_image"
        if key in r and mb > r[key]:
            say(f"{mb:.1f} MB; over {r[key]} MB slows the page")
    return out


_CHECK_CACHE = {}


def checks_for(meta):
    """Checks stored at submit, or computed once for items submitted before checks existed."""
    if "checks" in meta:
        return meta["checks"]
    key = meta["dir"]
    if key not in _CHECK_CACHE:
        base = os.path.join(ASSETS, meta["dir"])
        if meta.get("kind") == "choice":
            _CHECK_CACHE[key] = [{"context": c["context"], "msg": f'{o["label"]}: {c["msg"]}'} for o in meta.get("options", [])
                                 for c in spec_checks(os.path.join(base, "options", o["file"]), o["kind"], meta.get("contexts"), o.get("media"))]
        else:
            _CHECK_CACHE[key] = spec_checks(os.path.join(base, meta["file"]), meta.get("kind"), meta.get("contexts"), meta.get("media"))
    return _CHECK_CACHE[key]


# ---------- agents, reminders, notifications ----------

def agents_alive():
    out = []
    if not os.path.isdir(AGENTS):
        return out
    for name in os.listdir(AGENTS):
        info = read_json(os.path.join(AGENTS, name))
        if not info:
            continue
        try:
            os.kill(int(info["pid"]), 0)
        except ProcessLookupError:
            try:
                os.remove(os.path.join(AGENTS, name))
            except OSError:
                pass
            continue
        except (PermissionError, ValueError, KeyError):
            pass
        out.append({"project": info.get("project") or "", "since": info.get("since")})
    return out


def stale_hours():
    return float(load_config().get("stale_hours", 12))


def hours_since(ts):
    if not ts:
        return float("inf")
    try:
        return (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except (TypeError, ValueError):
        return 0


def notify_new(meta):
    """Opt-in push (ntfy). Sends the title and a board link only: no image, no reasoning."""
    url = (load_config().get("notify") or {}).get("ntfy")
    if not url:
        return
    cfg = load_config()
    host = f"localhost:{PORT}"
    if cfg.get("lan"):
        addrs = lan_addresses()
        if addrs:
            host = f"{addrs[0][1]}:{PORT}"
    title = f"{meta.get('project') + ': ' if meta.get('project') else ''}{meta['title']}"
    req = urllib.request.Request(url, data=f"{meta['version']} is ready for review".encode(), method="POST",
                                 headers={"Title": title.encode("ascii", "replace").decode(), "Click": f"http://{host}/#{meta['id']}",
                                          "Tags": "eyes"})
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except OSError as e:
        print(f"goodeye: notification not sent ({e})", file=sys.stderr)


# ---------- scores ----------

def normalize_scores(raw):
    """Scores from any judge (an LLM judge, a linter, a measurement). Returns (scores, judge).

    Accepted shapes:
      {"scores": {"hook": {"value": 2.9, "max": 4, "bar": 2.8, "group": "Quality"}}, "judge": {"name", "pass", "notes"}}
      {"hook": 2.9, "clarity": 3.1}                  flat numbers
      {"hook": {"score": 2.9}, "clarity": {"score": 3.1}}
    """
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        die("scores JSON must be an object")
    if isinstance(raw.get("scores"), dict):
        scores, judge = raw["scores"], raw.get("judge")
    elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw.values()):
        scores, judge = {k: {"value": v} for k, v in raw.items()}, None
    elif all(isinstance(v, dict) and isinstance(v.get("score", v.get("value")), (int, float)) for v in raw.values()):
        scores, judge = {k: {**v, "value": v.get("value", v.get("score"))} for k, v in raw.items()}, None
    else:
        die("scores JSON is not in a known shape (see README: Scores)")
    for k, v in scores.items():
        if not isinstance(v, dict) or not isinstance(v.get("value"), (int, float)):
            die(f"score {k!r} needs a numeric 'value'")
    if judge is not None and not isinstance(judge, dict):
        die("'judge' must be an object: {name, pass, notes}")
    return scores, judge


def probe_media(path, kind):
    """Timeline facts for animated assets: fps, duration, frames; for GIFs, what the file itself does."""
    info = {}
    if kind == "gif":
        with open(path, "rb") as f:
            b = f.read()
        i = b.find(b"NETSCAPE2.0")
        info["width"], info["height"] = int.from_bytes(b[6:8], "little"), int.from_bytes(b[8:10], "little")
        info["file_loops"] = "once" if i < 0 else ("forever" if int.from_bytes(b[i + 13:i + 15], "little") == 0 else f"{int.from_bytes(b[i + 13:i + 15], 'little')} extra times")
    if kind == "video" and shutil.which("ffprobe"):
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate,nb_frames,duration,width,height",
                                  "-of", "json", path], capture_output=True, text=True, timeout=20).stdout
            st = (json.loads(out).get("streams") or [{}])[0]
            num, _, den = st.get("r_frame_rate", "0/1").partition("/")
            if float(den or 1):
                info["fps"] = round(float(num) / float(den or 1), 3)
            for k in ("duration",):
                if st.get(k):
                    info[k] = float(st[k])
            if st.get("nb_frames"):
                info["frames"] = int(st["nb_frames"])
            info.update({k: st[k] for k in ("width", "height") if st.get(k)})
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    return info


def validate_reasoning(r, is_revision):
    if not isinstance(r, dict) or not str(r.get("summary", "")).strip():
        die("reasoning needs a non-empty 'summary'")
    decs = r.get("decisions") or []
    if not decs or not all(isinstance(d, dict) and d.get("choice") and d.get("why") for d in decs):
        die("reasoning needs 'decisions': [{\"choice\": ..., \"why\": ...}] with at least one entry")
    if is_revision and not (r.get("changes") or []):
        die("this is a new version: reasoning needs 'changes': [{\"change\": ..., \"why\": ..., \"feedback\": optional quote}]")


# ---------- commands ----------

def load_options(path):
    """options.json -> list of option dicts with absolute file paths. See SKILL.md for the format."""
    spec = read_json(path)
    if not isinstance(spec, dict) or not isinstance(spec.get("options"), list) or len(spec["options"]) < 2:
        die("options JSON needs 'options': [at least 2 of {key, label, file, note?, scores?}]")
    base = os.path.dirname(os.path.abspath(path))
    keys, out = set(), []
    for o in spec["options"]:
        key = str(o.get("key", "")).strip()
        if not ID_RE.match(key) or key in keys:
            die(f"option key {key!r} must be unique, lowercase letters, digits, dot, dash, underscore")
        keys.add(key)
        f = os.path.abspath(os.path.join(base, os.path.expanduser(str(o.get("file", "")))))
        if not os.path.isfile(f):
            die(f"option {key}: no such file {f}")
        sc = o.get("scores") or {}
        sc = {k: (v if isinstance(v, dict) else {"value": v}) for k, v in sc.items()}
        out.append({"key": key, "label": o.get("label") or key, "src": f, "note": o.get("note", ""), "scores": sc})
    rec = spec.get("recommended")
    if rec is not None and rec not in keys:
        die(f"recommended {rec!r} is not an option key")
    return out, rec, spec.get("question", "")


def parse_slot(key, label):
    if not ID_RE.match(key or ""):
        die("slot key must be lowercase letters, digits, dot, dash, underscore")
    for it in all_items():   # reuse the label already on the slot so every member says the same thing
        if it.get("slot") and it["slot"]["key"] == key and not label:
            return it["slot"]
    return {"key": key, "label": label or key}


def cmd_slot(a):
    """Put existing items (all their versions) into one slot."""
    slot = parse_slot(a.key, a.label)
    for asset_id in a.ids:
        vs = versions_of(asset_id)
        if not vs:
            die(f"no item {asset_id}")
        for v in vs:
            path = os.path.join(ASSETS, v["dir"], "meta.json")
            m = read_json(path)
            m["slot"] = slot
            write_json(path, m)
        print(f"{asset_id} -> slot {slot['key']} ({slot['label']})")


def cmd_submit(a):
    if bool(a.file) == bool(a.options):
        die("give either FILE or --options options.json, not both")
    if a.options:
        options, recommended, question = load_options(a.options)
        src = os.path.abspath(a.options)
    else:
        src = os.path.abspath(a.file)
        if not os.path.isfile(src):
            die(f"no such file {src}")
    if not ID_RE.match(a.id):
        die("id must be lowercase letters, digits, dot, dash, underscore")
    reasoning = read_json(a.reasoning)
    if reasoning is None:
        die(f"cannot read reasoning JSON {a.reasoning}")
    prior = versions_of(a.id)
    validate_reasoning(reasoning, bool(prior))
    scores, judge = normalize_scores(read_json(a.scores) if a.scores else None)
    if a.scores and not scores:
        die(f"cannot read scores JSON {a.scores}")
    seq = (prior[-1]["seq"] + 1) if prior else 1
    version = a.version or f"v{seq}"
    if any(p["version"] == version for p in prior):
        die(f"{a.id}@{version} already exists; versions are frozen, pick a new one")
    contexts = [c.strip() for c in (a.context or "").split(",") if c.strip()]
    for c in contexts:
        if c not in CONTEXTS:
            die(f"unknown context {c}; use one of {', '.join(CONTEXTS)}")
    vdir = os.path.join(ASSETS, a.id, re.sub(r"[^A-Za-z0-9._-]", "_", version))
    os.makedirs(vdir)
    if a.options:
        os.makedirs(os.path.join(vdir, "options"))
        for o in options:
            o["file"] = f'{o["key"]}{os.path.splitext(o["src"])[1].lower()}'
            o["kind"] = kind_of(o["src"])
            o["media"] = probe_media(o["src"], o["kind"])
            shutil.copy2(o["src"], os.path.join(vdir, "options", o["file"]))
        first = next((o for o in options if o["key"] == recommended), options[0])
        fname, kind = f'options/{first["file"]}', "choice"
    else:
        fname, kind = os.path.basename(src), kind_of(src)
        shutil.copy2(src, os.path.join(vdir, fname))
    meta = {"id": a.id, "version": version, "seq": seq, "project": a.project or (prior[-1].get("project") if prior else ""),
            "title": a.title or (prior[-1]["title"] if prior else a.id), "kind": kind, "file": fname,
            "source_path": src, "size_bytes": os.path.getsize(os.path.join(vdir, fname)), "contexts": contexts or (prior[-1]["contexts"] if prior else []),
            "submitted_at": now(), "media": {} if kind == "choice" else probe_media(src, kind), "reasoning": reasoning, "scores": scores, "judge": judge,
            "dir": os.path.relpath(vdir, ASSETS)}
    if kind == "choice":
        meta.update(options=options, recommended=recommended, question=question)
    slot = parse_slot(a.slot, a.slot_label) if a.slot else (prior[-1].get("slot") if prior else None)
    if slot:
        meta["slot"] = slot
    meta["checks"] = checks_for(meta)
    write_json(os.path.join(vdir, "meta.json"), meta)
    ensure_server()
    print(f"submitted {a.id}@{version} ({meta['kind']}) -> {URL}/#{a.id}")
    for c in meta["checks"]:
        print(f"  SPEC WARNING [{c['context']}]: {c['msg']}")
    if meta["checks"]:
        print("  The reviewer sees these warnings. Fix and resubmit now if the placement is right, or explain in reasoning.")
    notify_new(meta)
    print("next: run `goodeye wait` in the background to receive the verdict")


def cmd_wait(a):
    os.makedirs(AGENTS, exist_ok=True)
    mine = os.path.join(AGENTS, f"{os.getpid()}.json")
    write_json(mine, {"pid": os.getpid(), "project": a.project or "", "since": now()})
    atexit.register(lambda: os.path.exists(mine) and os.remove(mine))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Nudge once per 6 hours about changes the reviewer asked for that never came back (only when no new verdict is waiting).
    pending_verdicts = [d for d in decisions() if d["decision_id"] not in set(read_json(DELIVERED, [])) and (not a.project or d.get("project") == a.project)]
    reminded = read_json(REMINDED, {}) or {}
    stale = [i for i in all_items() if i["status"] == "changes" and (not a.project or i["project"] == a.project)
             and hours_since(i["versions"][-1]["decisions"][-1]["at"]) >= stale_hours()
             and hours_since(reminded.get(i["id"] + "@" + i["versions"][-1]["version"], "")) >= 6]
    if stale and not pending_verdicts:
        for i in stale:
            v = i["versions"][-1]
            print(f"REMINDER: changes requested {hours_since(v['decisions'][-1]['at']):.0f} h ago on {i['id']}@{v['version']} ({i['title']}); no new version yet.")
            print(f"  feedback: {v['decisions'][-1]['feedback'].strip()}")
            reminded[i["id"] + "@" + v["version"]] = now()
        write_json(REMINDED, reminded)
        print("next: submit the new versions, then run `goodeye wait` again.")
        return
    delivered = set(read_json(DELIVERED, []))
    start = time.time()
    while True:
        new = [d for d in decisions() if d["decision_id"] not in delivered and (not a.project or d.get("project") == a.project)]
        if new:
            for d in new:
                meta = read_json(os.path.join(ASSETS, d["dir"], "meta.json"), {})
                print(f"VERDICT {d['verdict'].upper()}: {d['id']}@{d['version']}  ({meta.get('title', '')})")
                print(f"  at: {d['at']}")
                print(f"  source file: {meta.get('source_path', '')}")
                for i, r in enumerate(d.get("ranking") or []):
                    opt = next((o for o in meta.get("options", []) if o["key"] == r["key"]), {})
                    print(f"  pick {i + 1}: {r['key']} ({opt.get('label', '')})  file: {opt.get('src', '')}")
                    if r.get("note", "").strip():
                        print(f"    note: {r['note'].strip()}")
                for r in d.get("option_notes") or []:
                    print(f"  note on unranked option {r['key']}: {r['note'].strip()}")
                if d["verdict"] != "not_chosen":
                    print(f"  feedback: {d['feedback'].strip() or '(none)'}")
                if d.get("closed"):
                    print(f"  this approval filled slot '{d.get('slot_label', '')}' and closed: {', '.join(d['closed'])}")
                has_notes = bool(d["feedback"].strip() or any(r.get("note", "").strip() for r in (d.get("ranking") or []) + (d.get("option_notes") or [])))
                if d["verdict"] in ("approved", "picked"):
                    what = "pick 1" if d["verdict"] == "picked" else "this asset"
                    if has_notes:
                        print(f"  next: APPROVED WITH NOTES. The reviewer trusts you to apply the notes to {what} (combine parts from other")
                        print("        options where a note asks). Do NOT resubmit for review. Record the approval with the notes and the")
                        print("        final file path in the project's approval record, then do the follow-up work. Ask only if a note is impossible.")
                    else:
                        print(f"  next: {what} is approved as is. Record it in the project's approval record, then do the follow-up work.")
                    if d["verdict"] == "picked":
                        print("        Pick 2 is the fallback if pick 1 cannot work.")
                elif d["verdict"] == "reopened":
                    print("  next: REOPENED. The reviewer brought this back into review. Do not change it yet; wait for its verdict.")
                elif d["verdict"] == "not_chosen":
                    print(f"  next: NOT CHOSEN. {d['feedback']} Stop work on this asset. Do not resubmit it.")
                elif d["verdict"] == "changes":
                    print("  next: REVIEW AGAIN. Make a new version that answers every point (for a choice: start from pick 1,")
                    print("        or offer new options if nothing was picked), then `goodeye submit` it with the same id and a 'changes' list.")
                elif d["verdict"] == "rejected":
                    print("  next: stop work on this asset. Do not resubmit unless the reviewer asks.")
                print()
            delivered |= {d["decision_id"] for d in new}
            write_json(DELIVERED, sorted(delivered))
            return
        if a.timeout and time.time() - start > a.timeout:
            print("no verdict yet (timeout)")
            sys.exit(1)
        time.sleep(1.5)


def cmd_status(a):
    for it in all_items():
        if a.project and it["project"] != a.project:
            continue
        v = it["versions"][-1]
        slot = f"  [slot {it['slot']['key']}]" if it.get("slot") else ""
        print(f"{it['status']:<10} {it['id'] + '@' + v['version']:<36} {len(it['versions'])} version(s)  {it['title']}{slot}")


def port_open():
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def ensure_server():
    if port_open():
        return
    log = open(os.path.join(HOME, "server.log"), "a")
    subprocess.Popen([sys.executable, os.path.realpath(__file__), "serve"], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(20):
        if port_open():
            return
        time.sleep(0.1)


# ---------- phone access ----------

def load_config():
    return read_json(CONFIG, {}) or {}


def phone_token():
    """A random secret for phone access. Stored readable by this user only."""
    tok = None
    try:
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
    except OSError:
        pass
    if not tok:
        tok = secrets.token_urlsafe(24)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
    return tok


def lan_addresses():
    """This machine's Wi-Fi/LAN address, plus a Tailscale address when Tailscale is installed."""
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))    # no packet is sent; this only picks the outgoing interface
        out.append(("Wi-Fi", s.getsockname()[0]))
    except OSError:
        pass
    finally:
        s.close()
    if shutil.which("tailscale"):
        try:
            ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
            if ip:
                out.append(("Tailscale", ip[0]))
        except (subprocess.SubprocessError, OSError):
            pass
    return [(label, ip) for label, ip in out if not ip.startswith("127.")]


def phone_urls(port=None):
    tok = phone_token()
    return [(label, f"http://{ip}:{port or PORT}/?t={tok}") for label, ip in lan_addresses()]


ICON_CACHE = {}


def png_bytes(w, h, draw):
    buf = io.BytesIO()
    raw = b"".join(b"\x00" + bytes(c for x in range(w) for c in draw(x, y)) for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    buf.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
              + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    return buf.getvalue()


def app_icon(size):
    """The home-screen icon: a 3x3 tile grid, the same mark as the board's idle screen."""
    if size not in ICON_CACHE:
        colors = [(27, 175, 122), (42, 120, 214), (27, 175, 122), (237, 161, 0), (27, 175, 122), (235, 104, 52),
                  (27, 175, 122), (42, 120, 214), (27, 175, 122)]
        bg, cell = (17, 17, 17), size / 4.2
        off = (size - cell * 3 - cell * 0.3 * 2) / 2

        def draw(x, y):
            for i in range(9):
                cx, cy = off + (i % 3) * cell * 1.15, off + (i // 3) * cell * 1.15
                if cx <= x < cx + cell and cy <= y < cy + cell:
                    return colors[i]
            return bg
        ICON_CACHE[size] = png_bytes(size, size, draw)
    return ICON_CACHE[size]


MANIFEST = {"name": "GoodEye", "short_name": "GoodEye", "start_url": "/", "display": "standalone",
            "background_color": "#111111", "theme_color": "#111111",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}
LAN = False     # set at serve time from config.json


BOARD_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' blob: data:; "
             "media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "goodeye"
    sys_version = ""

    def log_message(self, *args):
        pass

    def client_is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def host_is_local(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}

    def has_token(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return False
        tok = jar.get("goodeye")
        return bool(tok) and hmac.compare_digest(tok.value, phone_token())

    def allowed_host(self):
        """Who may use the board.
        This machine: only when the request really comes from loopback AND names a loopback host (blocks DNS rebinding).
        A phone: only in phone mode, and only with the secret token cookie."""
        if self.host_is_local():
            return self.client_is_local()
        return LAN and self.has_token()

    def allowed_origin(self):
        """Writes must come from the board page itself. Browsers always send Origin on cross-site POSTs."""
        origin = self.headers.get("Origin")
        if origin is None:
            return self.client_is_local() and self.host_is_local()   # a CLI client on this machine
        return origin.lower() == "http://" + (self.headers.get("Host") or "").lower()

    def token_login(self):
        """/?t=TOKEN from the QR code: set a long-lived cookie, then redirect so the token leaves the address bar."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = (q.get("t") or [""])[0]
        if not (LAN and tok and hmac.compare_digest(tok, phone_token())):
            return False
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"goodeye={tok}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("Content-Length", "0")
        self.common_headers()
        self.end_headers()
        return True

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def send(self, code, body, ctype="application/json", csp=None, cache="no-cache"):
        """JSON and HTML get an ETag (unchanged data costs a 304 and no body) and gzip when the client accepts it."""
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":")).encode()
        etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"' if code == 200 else None
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.common_headers()
            self.end_headers()
            return
        gz = len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "") and not ctype.startswith("image/")
        if gz:
            body = gzip.compress(body, 6)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Vary", "Accept-Encoding, Cookie")
        if etag:
            self.send_header("ETag", etag)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.common_headers()
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.host_is_local() and self.token_login():
            return
        if not self.allowed_host():
            return self.send(403, b"GoodEye: open the link from `goodeye phone` on this device first.", "text/plain; charset=utf-8")
        path = urllib.parse.urlparse(self.path).path
        if path == "/manifest.webmanifest":
            return self.send(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
        if path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            return self.send(200, app_icon(512 if "512" in path else 192 if "192" in path else 180), "image/png", cache="max-age=86400")
        if path == "/api/pair":
            if not self.client_is_local():
                return self.send(403, {"error": "pair from this computer"})
            urls = phone_urls(self.server.server_address[1]) if LAN else []
            from goodeye_qr import qr_matrix, to_svg
            return self.send(200, {"lan": LAN, "urls": [{"label": l, "url": u, "svg": to_svg(qr_matrix(u))} for l, u in urls]})
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "board.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8", csp=BOARD_CSP)
        if path == "/api/items":
            return self.send(200, {"items": all_items(), "agents": agents_alive(), "stale_hours": stale_hours()})
        if path.startswith("/files/"):
            rel = urllib.parse.unquote(path[len("/files/"):])
            full = os.path.realpath(os.path.join(ASSETS, rel))
            if not full.startswith(os.path.realpath(ASSETS) + os.sep) or not os.path.isfile(full):
                return self.send(404, {"error": "not found"})
            return self.send_file(full)
        self.send(404, {"error": "not found"})

    def send_file(self, full):
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.common_headers()
        # Uploaded files never run code: an SVG opened directly gets a sandboxed, script-free origin.
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'")
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")   # versions are frozen, so files never change
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        if not self.allowed_host() or not self.allowed_origin():
            return self.send(403, {"error": "forbidden origin"})
        if urllib.parse.urlparse(self.path).path != "/api/decide":
            return self.send(404, {"error": "not found"})
        # A JSON content type forces a CORS preflight for cross-site callers, which this server never approves.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.send(415, {"error": "send application/json"})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self.send(400, {"error": "bad length"})
        if length > MAX_BODY:
            return self.send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self.send(400, {"error": "bad json"})
        verdict = body.get("verdict")
        if verdict not in VERDICTS:
            return self.send(400, {"error": "verdict must be one of " + ", ".join(VERDICTS)})
        if not ID_RE.match(str(body.get("id", ""))):
            return self.send(400, {"error": "bad id"})
        feedback = str(body.get("feedback", ""))[:20000]
        rows_in = [r for r in (body.get("ranking") or []) + (body.get("option_notes") or []) if isinstance(r, dict)]
        notes = any(str(r.get("note", "")).strip() for r in rows_in)
        if verdict == "changes" and not feedback.strip() and not notes:
            return self.send(400, {"error": "say what to change"})
        meta = next((v for v in versions_of(str(body.get("id", ""))) if v["version"] == body.get("version")), None)
        if not meta:
            return self.send(404, {"error": "unknown asset version"})
        if verdict == "reopened":
            cur = next((i for i in all_items() if i["id"] == meta["id"]), None)
            if not cur or cur["versions"][-1]["version"] != meta["version"] or cur["status"] not in ("rejected", "not_chosen"):
                return self.send(400, {"error": "only a rejected or not-chosen latest version can be reopened"})
        d = {"decision_id": uuid.uuid4().hex, "id": meta["id"], "version": meta["version"], "dir": meta["dir"],
             "project": meta.get("project", ""), "verdict": verdict, "feedback": feedback, "at": now()}
        if meta.get("kind") == "choice":
            keys = {o["key"] for o in meta.get("options", [])}
            clean = lambda rows: [{"key": str(r.get("key")), "note": str(r.get("note", ""))[:5000]} for r in rows or [] if isinstance(r, dict) and r.get("key") in keys]
            d["ranking"], d["option_notes"] = clean(body.get("ranking")), [r for r in clean(body.get("option_notes")) if r["note"].strip()]
            if verdict == "picked" and not d["ranking"]:
                return self.send(400, {"error": "pick at least one option"})
        elif verdict == "picked":
            return self.send(400, {"error": "picked is only for choice items"})
        with LOCK:
            rows = [d]
            slot = meta.get("slot")
            if slot and verdict == "not_chosen":
                filler = next((i for i in all_items() if i.get("slot") and i["slot"]["key"] == slot["key"] and i["status"] in FILLED), None)
                d["feedback"] = feedback or (f"Slot '{slot['label']}' was filled by {filler['id']}@{filler['versions'][-1]['version']}." if filler else f"Closed in slot '{slot['label']}'.")
            if slot and verdict in FILLED:
                d["slot_label"], d["closed"] = slot["label"], []
                for sib in all_items():
                    if sib["id"] == meta["id"] or not sib.get("slot") or sib["slot"]["key"] != slot["key"]:
                        continue
                    if sib["status"] in OPEN + FILLED:
                        sv = sib["versions"][-1]
                        why = "replaced by" if sib["status"] in FILLED else "filled by"
                        rows.append({"decision_id": uuid.uuid4().hex, "id": sib["id"], "version": sv["version"], "dir": sv["dir"],
                                     "project": sv.get("project", ""), "verdict": "not_chosen", "at": d["at"], "by": d["decision_id"],
                                     "feedback": f"Slot '{slot['label']}' was {why} {meta['id']}@{meta['version']}."})
                        d["closed"].append(f"{sib['id']}@{sv['version']}")
            with open(DECISIONS, "a") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        self.send(200, d)


# ---------- demo ----------

def png(path, w, h, draw):
    """Write an RGB PNG with the standard library. draw(x, y) -> (r, g, b)."""
    raw = b"".join(b"\x00" + bytes(c for x in range(w) for c in draw(x, y)) for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def tiles(bg, colors, cols, rows, w, h, pad=0.12):
    """A grid of rounded-ish color tiles on a background: enough to judge color and layout."""
    cw, ch = w / cols, h / rows
    def draw(x, y):
        cx, cy = int(x // cw), int(y // ch)
        fx, fy = (x % cw) / cw, (y % ch) / ch
        if pad < fx < 1 - pad and pad < fy < 1 - pad:
            return colors[(cx * 7 + cy * 3) % len(colors)]
        return bg
    return draw


def cmd_demo(a):
    """Three sample submissions: a single asset with two versions, a choice, and a slot of two candidates."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="goodeye-demo-")
    warm = [(214, 88, 38), (237, 161, 0), (122, 60, 30)]
    cool = [(42, 120, 214), (27, 175, 122), (74, 58, 167)]
    green = [(40, 110, 70), (90, 160, 110), (160, 200, 150)]
    cream = (243, 240, 228)
    png(f"{tmp}/banner-v1.png", 792, 198, tiles(cream, warm, 16, 4, 792, 198))
    png(f"{tmp}/banner-v2.png", 792, 198, tiles(cream, green, 16, 4, 792, 198, 0.08))
    for name, pal in (("warm", warm), ("cool", cool), ("green", green)):
        png(f"{tmp}/icon-{name}.png", 256, 256, tiles(cream, pal, 2, 2, 256, 256, 0.1))
    png(f"{tmp}/hero-a.png", 640, 400, tiles(cream, cool, 8, 5, 640, 400))
    png(f"{tmp}/hero-b.png", 640, 400, tiles((20, 20, 20), green, 8, 5, 640, 400))

    def write(name, data):
        path = f"{tmp}/{name}"
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def run(*args):
        subprocess.run([sys.executable, os.path.realpath(__file__), "submit", *args], check=True, stdout=subprocess.DEVNULL)

    r1 = write("r1.json", {"summary": "Profile banner, first pass: warm tiles on cream.", "decisions": [
        {"choice": "Warm palette", "why": "Stands out in a mostly blue feed."}]})
    r2 = write("r2.json", {"summary": "Profile banner, second pass: brand greens.", "decisions": [
        {"choice": "Tighter tile spacing", "why": "Reads as one mark at small sizes."}],
        "changes": [{"change": "Switched to the green palette", "why": "Matches the logo.", "feedback": "use our greens"}]})
    s1 = write("s1.json", {"scores": {"clarity": {"value": 2.6, "max": 4, "bar": 2.8, "group": "Quality (0 to 4)"},
                                      "craft": {"value": 3.0, "max": 4, "bar": 2.8, "group": "Quality (0 to 4)"},
                                      "ship": {"value": 0.61, "max": 1, "bar": 0.75, "group": "Ready to ship (chance of yes)"}},
                           "judge": {"name": "Example judge", "pass": False, "notes": ["Clarity is under the bar."]}})
    s2 = write("s2.json", {"scores": {"clarity": {"value": 3.2, "max": 4, "bar": 2.8, "group": "Quality (0 to 4)"},
                                      "craft": {"value": 3.3, "max": 4, "bar": 2.8, "group": "Quality (0 to 4)"},
                                      "ship": {"value": 0.82, "max": 1, "bar": 0.75, "group": "Ready to ship (chance of yes)"}},
                           "judge": {"name": "Example judge", "pass": True, "notes": []}})
    run(f"{tmp}/banner-v1.png", "--id", "demo-banner", "--title", "Profile banner", "--project", "Demo",
        "--context", "linkedin-banner,x-banner", "--reasoning", r1, "--scores", s1)
    decide_local("demo-banner", "v1", "changes", "use our greens")
    run(f"{tmp}/banner-v2.png", "--id", "demo-banner", "--reasoning", r2, "--scores", s2)

    opts = write("options.json", {"question": "Which app icon should we ship?", "recommended": "green", "options": [
        {"key": "warm", "label": "Warm", "file": "icon-warm.png", "note": "Highest contrast.", "scores": {"judge": 0.71}},
        {"key": "cool", "label": "Cool", "file": "icon-cool.png", "note": "Calm, but close to competitors.", "scores": {"judge": 0.55}},
        {"key": "green", "label": "Green", "file": "icon-green.png", "note": "Matches the brand.", "scores": {"judge": 0.84}}]})
    rc = write("rc.json", {"summary": "Three icon directions at the same scale.", "decisions": [
        {"choice": "Recommend green", "why": "Best judge score and matches the brand."}]})
    run("--options", opts, "--id", "demo-icon", "--title", "App icon", "--project", "Demo", "--context", "browser-tab,phone", "--reasoning", rc)

    for key, f, line in (("a", "hero-a.png", "Light, calm"), ("b", "hero-b.png", "Dark, bold")):
        rh = write(f"rh-{key}.json", {"summary": f"Website hero candidate: {line}.", "decisions": [{"choice": line, "why": "Demo candidate."}]})
        run(f"{tmp}/{f}", "--id", f"demo-hero-{key}", "--title", f"Website hero: {line}", "--project", "Demo",
            "--context", "website-hero", "--reasoning", rh, "--slot", "demo-hero", "--slot-label", "Website hero image")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"demo loaded: 4 items in project 'Demo'. Open {URL}")


def decide_local(asset_id, version, verdict, feedback):
    """Record a verdict without the browser (used by the demo). Marked delivered so no agent acts on it."""
    meta = next(v for v in versions_of(asset_id) if v["version"] == version)
    d = {"decision_id": uuid.uuid4().hex, "id": asset_id, "version": version, "dir": meta["dir"], "project": meta.get("project", ""),
         "verdict": verdict, "feedback": feedback, "at": now()}
    with LOCK:
        with open(DECISIONS, "a") as f:
            f.write(json.dumps(d) + "\n")
        write_json(DELIVERED, sorted(set(read_json(DELIVERED, [])) | {d["decision_id"]}))


def watch_code():
    """Restart the server in place when this file changes (git pull), so an update never leaves a stale server."""
    watched = [os.path.realpath(__file__), os.path.join(HERE, "goodeye_qr.py"), CONFIG]
    stamp = lambda: [os.path.getmtime(p) if os.path.exists(p) else 0 for p in watched]
    start = stamp()
    while True:
        time.sleep(2)
        try:
            if stamp() != start:
                print("code or config changed; restarting", flush=True)
                os.execv(sys.executable, [sys.executable, watched[0]] + sys.argv[1:])
        except OSError:
            pass


def cmd_serve(a):
    global LAN
    LAN = bool(load_config().get("lan"))
    if LAN:
        phone_token()
    threading.Thread(target=watch_code, daemon=True).start()
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    # Loopback only unless phone mode is on. In phone mode every non-local request needs the token cookie.
    srv = http.server.ThreadingHTTPServer(("0.0.0.0" if LAN else "127.0.0.1", a.port), Handler)
    print(f"GoodEye board on http://localhost:{a.port}  (store: {HOME}, phone mode {'on' if LAN else 'off'})", flush=True)
    srv.serve_forever()


def cmd_notify(a):
    cfg = load_config()
    if a.off:
        cfg.pop("notify", None)
        write_json(CONFIG, cfg)
        print("Notifications off.")
        return
    if a.ntfy:
        if not a.ntfy.startswith("https://"):
            die("use an https:// ntfy topic URL, e.g. https://ntfy.sh/your-long-random-topic")
        cfg["notify"] = {"ntfy": a.ntfy}
        write_json(CONFIG, cfg)
        print("Notifications on. Each new submission sends its title and a board link (no image, no reasoning) to that topic.")
        print("Anyone who knows the topic name can read it: use a long random name.")
    url = (cfg.get("notify") or {}).get("ntfy")
    if not url:
        die("not configured. Run: goodeye notify --ntfy https://ntfy.sh/<long-random-topic>")
    if a.test or a.ntfy:
        notify_new({"id": "test", "title": "GoodEye test notification", "version": "v1", "project": ""})
        print("Sent a test notification.")


def cmd_export(a):
    """Copy each approved item's final file (pick 1 for choices) and a manifest into DIR."""
    os.makedirs(a.dir, exist_ok=True)
    manifest = []
    for it in all_items():
        if a.project and it["project"] != a.project:
            continue
        v = next((x for x in reversed(it["versions"]) if x["status"] in FILLED), None)
        if not v:
            continue
        d = v["decisions"][-1]
        rel = v["file"]
        if v["kind"] == "choice" and d.get("ranking"):
            opt = next((o for o in v["options"] if o["key"] == d["ranking"][0]["key"]), None)
            rel = "options/" + opt["file"] if opt else rel
        src = os.path.join(ASSETS, v["dir"], rel)
        dest_dir = os.path.join(a.dir, it["id"])
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{re.sub(r'[^A-Za-z0-9._-]', '_', v['version'])}-{os.path.basename(rel)}")
        shutil.copy2(src, dest)
        notes = [d["feedback"].strip()] if d["feedback"].strip() else []
        notes += [f"{r['key']}: {r['note'].strip()}" for r in (d.get("ranking") or []) + (d.get("option_notes") or []) if r.get("note", "").strip()]
        manifest.append({"id": it["id"], "title": it["title"], "project": it["project"], "version": v["version"],
                         "verdict": d["verdict"], "approved_at": d["at"], "file": os.path.relpath(dest, a.dir),
                         "notes_to_apply": notes, "source_path": v.get("source_path", "")})
    write_json(os.path.join(a.dir, "manifest.json"), manifest)
    flagged = sum(1 for m in manifest if m["notes_to_apply"])
    print(f"exported {len(manifest)} approved item(s) to {a.dir}")
    if flagged:
        print(f"{flagged} were approved with notes: the exported file is the version the reviewer saw, before the agent applied the notes.")


def cmd_phone(a):
    """Turn phone mode on or off, restart the board, and print the link and QR code."""
    from goodeye_qr import qr_matrix, to_terminal
    cfg = load_config()
    want = not a.off
    if a.new_token and os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)          # every phone must scan again
    if cfg.get("lan") != want:
        cfg["lan"] = want
        write_json(CONFIG, cfg)
        if port_open():
            time.sleep(3)              # the running board restarts itself when config.json changes
    ensure_server()
    for _ in range(50):
        if port_open():
            break
        time.sleep(0.1)
    if not want:
        print("Phone mode off. The board only answers this computer again.")
        return
    urls = phone_urls()
    if not urls:
        die("no Wi-Fi or LAN address found. Connect to a network and try again.")
    print("Phone mode on. Scan with your phone's camera (same Wi-Fi as this computer):\n")
    print(to_terminal(qr_matrix(urls[0][1])))
    for label, url in urls:
        print(f"\n  {label}: {url}")
    print("\nThe link signs your phone in once. Keep it private: anyone on your network with it can review.")
    print("Tip: add the page to your home screen. `goodeye phone --off` turns this off; `--new-token` signs every phone out.")


def main():
    os.makedirs(ASSETS, exist_ok=True)
    p = argparse.ArgumentParser(prog="goodeye", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"goodeye {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("file", nargs="?")
    s.add_argument("--slot", help="slot key: items that compete for one placement; approving one closes the others")
    s.add_argument("--slot-label", help="human name of the slot, e.g. 'App store hero image'")
    s.add_argument("--options", help="JSON: {question, recommended, options: [{key, label, file, note, scores}]} for a pick-one decision")
    s.add_argument("--id", required=True)
    s.add_argument("--title")
    s.add_argument("--project")
    s.add_argument("--version")
    s.add_argument("--context", help=",".join(CONTEXTS))
    s.add_argument("--reasoning", required=True, help="JSON: summary, decisions[], changes[] (required from v2)")
    s.add_argument("--scores", help="JSON: {scores: {metric: {value, max, bar, group}}, judge: {name, pass, notes}}")
    w = sub.add_parser("wait")
    w.add_argument("--project")
    w.add_argument("--timeout", type=int, default=0)
    st = sub.add_parser("status")
    st.add_argument("--project")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=PORT)
    sub.add_parser("open")
    sub.add_parser("demo", help="load sample items into the store so you can try the board")
    nt = sub.add_parser("notify", help="phone notifications for new work (ntfy)")
    nt.add_argument("--ntfy", help="https ntfy topic URL")
    nt.add_argument("--off", action="store_true")
    nt.add_argument("--test", action="store_true")
    ex = sub.add_parser("export", help="copy approved final files plus a manifest")
    ex.add_argument("dir")
    ex.add_argument("--project")
    ph = sub.add_parser("phone", help="let your phone open the board over Wi-Fi")
    ph.add_argument("--off", action="store_true", help="turn phone mode off")
    ph.add_argument("--new-token", action="store_true", help="make a new link; every phone must scan again")
    sl = sub.add_parser("slot", help="put existing items into one slot")
    sl.add_argument("key")
    sl.add_argument("ids", nargs="+")
    sl.add_argument("--label")
    a = p.parse_args()
    if a.cmd == "open":
        ensure_server()
        webbrowser.open(URL)
        return
    {"submit": cmd_submit, "demo": cmd_demo, "phone": cmd_phone, "notify": cmd_notify, "export": cmd_export, "slot": cmd_slot, "wait": cmd_wait, "status": cmd_status, "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    main()
