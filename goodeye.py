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

Store: $GOODEYE_HOME (default ~/.goodeye). Port: $GOODEYE_PORT (default 4400). Python 3.9+, standard library only.
"""
import argparse, datetime, threading, http.server, webbrowser, zlib, struct, json, mimetypes, os, re, shutil, socket, subprocess, sys, time, urllib.parse, uuid

__version__ = "0.1.0"
HOME = os.path.expanduser(os.environ.get("GOODEYE_HOME", "~/.goodeye"))
ASSETS = os.path.join(HOME, "assets")
DECISIONS = os.path.join(HOME, "decisions.jsonl")
DELIVERED = os.path.join(HOME, "delivered.json")
HERE = os.path.dirname(os.path.realpath(__file__))
PORT = int(os.environ.get("GOODEYE_PORT", "4400"))
URL = f"http://localhost:{PORT}"
CONTEXTS = ["linkedin-banner", "linkedin-post", "x-banner", "x-post", "instagram-post", "instagram-story", "email",
            "website-hero", "browser-tab", "youtube-thumbnail", "phone"]
OPEN = ("pending", "changes")          # still competing in a slot
FILLED = ("approved", "picked")       # holds a slot
LOCK = threading.Lock()
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
VERDICTS = ("approved", "changes", "rejected", "picked", "not_chosen")
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
                v["status"] = v["decisions"][-1]["verdict"] if v["decisions"] else "pending"
            latest = vs[-1]
            items.append({"id": asset_id, "project": latest.get("project", ""), "title": latest.get("title", asset_id),
                          "status": latest["status"], "updated": latest["submitted_at"], "slot": latest.get("slot"), "versions": vs})
    items.sort(key=lambda i: i["updated"], reverse=True)
    return items


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
    write_json(os.path.join(vdir, "meta.json"), meta)
    ensure_server()
    print(f"submitted {a.id}@{version} ({meta['kind']}) -> {URL}/#{a.id}")
    print("next: run `goodeye wait` in the background to receive the verdict")


def cmd_wait(a):
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


BOARD_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' blob: data:; "
             "media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "goodeye"
    sys_version = ""

    def log_message(self, *args):
        pass

    def allowed_host(self):
        """Refuse requests whose Host is not this machine: blocks DNS-rebinding pages from reading the board."""
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}

    def allowed_origin(self):
        """Writes must come from the board itself. Browsers always send Origin on cross-site POSTs."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True   # same-origin fetch in some browsers, or a CLI client
        port = self.server.server_address[1]
        return origin.lower() in {f"http://localhost:{port}", f"http://127.0.0.1:{port}", f"http://[::1]:{port}"}

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def send(self, code, body, ctype="application/json", csp=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.common_headers()
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed_host():
            return self.send(403, {"error": "forbidden host"})
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "board.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8", csp=BOARD_CSP)
        if path == "/api/items":
            return self.send(200, all_items())
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
        self.send_header("Cache-Control", "no-store")
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
    me = os.path.realpath(__file__)
    start = os.path.getmtime(me)
    while True:
        time.sleep(2)
        try:
            if os.path.getmtime(me) != start:
                print("goodeye.py changed; restarting", flush=True)
                os.execv(sys.executable, [sys.executable, me] + sys.argv[1:])
        except OSError:
            pass


def cmd_serve(a):
    threading.Thread(target=watch_code, daemon=True).start()
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)   # loopback only, never the network
    print(f"GoodEye board on http://localhost:{a.port}  (store: {HOME})", flush=True)
    srv.serve_forever()


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
    sl = sub.add_parser("slot", help="put existing items into one slot")
    sl.add_argument("key")
    sl.add_argument("ids", nargs="+")
    sl.add_argument("--label")
    a = p.parse_args()
    if a.cmd == "open":
        ensure_server()
        webbrowser.open(URL)
        return
    {"submit": cmd_submit, "demo": cmd_demo, "slot": cmd_slot, "wait": cmd_wait, "status": cmd_status, "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    main()
