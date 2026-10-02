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
import contextlib, math, tempfile
import argparse, atexit, shlex, signal, sqlite3, urllib.request, datetime, threading, http.server, http.cookies, webbrowser, zlib, struct, secrets, hmac, gzip, hashlib, io, json, mimetypes, os, re, shutil, socket, subprocess, sys, time, urllib.parse, uuid

__version__ = "0.8.0"
HOME = os.path.abspath(os.path.expanduser(os.environ.get("GOODEYE_HOME", "~/.goodeye")))
ASSETS = os.path.join(HOME, "assets")
DECISIONS = os.path.join(HOME, "decisions.jsonl")
DELIVERED = os.path.join(HOME, "delivered.json")
CONFIG = os.path.join(HOME, "config.json")
AGENTS = os.path.join(HOME, "agents")
REMINDED = os.path.join(HOME, "reminded.json")
TOKEN_FILE = os.path.join(HOME, "phone-token")
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
from goodeye_delivery import DeliveryStore
import goodeye_direction as direction
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


@contextlib.contextmanager
def atomic_file(path, mode="w"):
    """Readers see either complete version; every writer owns its temporary file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".write-")
    try:
        with os.fdopen(fd, mode) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, data):
    with atomic_file(path) as f:
        json.dump(data, f, indent=1, allow_nan=False)


@contextlib.contextmanager
def store_lock():
    """Serialize filesystem mutations across CLI processes and server threads."""
    with LOCK:
        db = sqlite3.connect(os.path.join(HOME, "store-lock.sqlite3"), timeout=60)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield
        finally:
            db.rollback()
            db.close()


@contextlib.contextmanager
def new_version_dir(path):
    if os.path.exists(path):
        die("version directory already exists; choose a different --version")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Stage outside assets so a crash cannot expose or reserve an incomplete version.
    staging = tempfile.mkdtemp(prefix=".submission-", dir=HOME)
    try:
        yield staging
        os.rename(staging, path)
    finally:
        if os.path.exists(staging):
            shutil.rmtree(staging)


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

def project_key(name):
    return hashlib.sha256(name.encode()).hexdigest()[:24]


def item_key(project, asset_id):
    return project_key(project) + ":" + asset_id


def asset_groups():
    """Read both legacy directories and project directories without moving frozen files."""
    groups = {}
    roots = [ASSETS]
    scoped = os.path.join(ASSETS, "_projects")
    if os.path.isdir(scoped):
        roots += [os.path.join(scoped, p) for p in os.listdir(scoped)]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for asset_id in os.listdir(root):
            d = os.path.join(root, asset_id)
            if not ID_RE.fullmatch(asset_id) or not os.path.isdir(d):
                continue
            for version in os.listdir(d):
                m = read_json(os.path.join(d, version, "meta.json"))
                if m:
                    groups.setdefault((m.get("project") or "", asset_id), []).append(m)
    return {key: sorted(vs, key=lambda m: m["seq"]) for key, vs in groups.items()}


def versions_of(asset_id, project=None):
    groups = asset_groups()
    if project is None:
        matches = [p for p, i in groups if i == asset_id]
        if len(matches) > 1:
            raise ValueError(f"{asset_id} exists in several projects; specify --project (or project in JSON)")
        project = matches[0] if matches else ""
    return groups.get((project, asset_id), [])


PROFILE_FIELDS = direction.FIELDS


def project_record(name):
    return {"name": name, "key": project_key(name), "icon": name[:2].upper() or "—", "accent": "#326653",
            "description": "", "collections": [], "revision": 0, "profile": {k: "" for k in PROFILE_FIELDS}}


def project_catalog(items=None):
    items = all_items() if items is None else items
    saved = read_json(os.path.join(HOME, "projects.json"), {}) or {}
    names = set(saved) | {i["project"] for i in items}
    out = []
    for name in sorted(names, key=str.casefold):
        p = {**project_record(name), **saved.get(name, {})}
        p["profile"] = {**project_record(name)["profile"], **p["profile"]}
        conversation = p.pop("direction", {})
        requests = conversation.get("requests", [])
        p["direction_summary"] = {"serial": conversation.get("serial", 0), "status": requests[-1]["status"] if requests else "not_started"}
        work = [i for i in items if i["project"] == name]
        p["collections"] = sorted(set(p["collections"]) | {i["collection"] for i in work if i.get("collection")}, key=str.casefold)
        p.update(pending=sum(i["status"] == "pending" for i in work), changes=sum(i["status"] == "changes" for i in work),
                 total=len(work), updated=max((i["updated"] for i in work), default=p.get("updated", "")))
        out.append(p)
    return out


def save_project(body):
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 120:
        raise ValueError("project name must be 1–120 characters without surrounding spaces")
    saved = read_json(os.path.join(HOME, "projects.json"), {}) or {}
    old = saved.get(name, project_record(name))
    if type(body.get("expected_revision")) is not int or body["expected_revision"] != old["revision"]:
        raise ValueError("project changed; reload its profile before saving")
    result = dict(old)
    for field, limit in (("icon", 12), ("description", 500), ("accent", 7)):
        value = body.get(field, old[field])
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"invalid {field}")
        result[field] = value.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", result["accent"]):
        raise ValueError("accent must be a six-digit hex color")
    collections = body.get("collections", old["collections"])
    if not isinstance(collections, list) or len(collections) > 100 or any(not isinstance(c, str) or not c.strip() or len(c) > 100 for c in collections):
        raise ValueError("collections must be a list of names (100 characters each)")
    result["collections"] = list(dict.fromkeys(c.strip() for c in collections))
    profile = body.get("profile", old["profile"])
    if not isinstance(profile, dict) or set(profile) - set(PROFILE_FIELDS):
        raise ValueError("unknown profile fields")
    result["profile"] = {**old["profile"], **profile}
    if any(not isinstance(v, str) or len(v) > 20000 for v in result["profile"].values()):
        raise ValueError("profile fields must be text, up to 20,000 characters each")
    result.update(revision=old["revision"] + 1, updated=now())
    saved[name] = result
    write_json(os.path.join(HOME, "projects.json"), saved)
    return result


def load_project(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("choose a named project")
    saved = read_json(os.path.join(HOME, "projects.json"), {}) or {}
    p = saved.get(name)
    if p is None:
        if not any(i["project"] == name for i in all_items()):
            raise ValueError("unknown project")
        p = project_record(name)
    p["profile"] = {**project_record(name)["profile"], **p["profile"]}
    return p


def write_project(p):
    saved = read_json(os.path.join(HOME, "projects.json"), {}) or {}
    saved[p["name"]] = p
    write_json(os.path.join(HOME, "projects.json"), saved)


def direction_prompt(p):
    name = shlex.quote(p["name"])
    skill = os.path.join(HERE, "skills", "goodeye-brand", "SKILL.md")
    return (f"Use the GoodEye brand workflow at {skill}. Work on project {p['name']!r}.\n"
            f"GoodEye store: {HOME}\n"
            f"Run GOODEYE_HOME={shlex.quote(HOME)} goodeye direction show --project {name} first. "
            "Read the conversation and source paths before making anything. Recover existing approved branding and rejection reasons. "
            "Do not assume an empty GoodEye profile means there is no established identity.\n"
            f"Join this workflow with GOODEYE_HOME={shlex.quote(HOME)} goodeye direction join --project {name} --as brand. "
            "If another session owns that name, follow the handoff instructions; do not replace it silently.\n"
            "Start with product truth, audience, origin, and a defensible strategy. Ask only for missing decisions. "
            "Use Codex image generation or the available Nano Banana workflow for raster exploration, grounded in inspected references. "
            "Propose work in GoodEye and wait for the reviewer's steering in Brand & direction. "
            "Keep approved rules until a proposed change is accepted. Do not generate a generic brand kit or invent approval.")


def direction_view(name):
    p = load_project(name)
    s = direction.state(p)
    store = delivery_store()
    requests = []
    for r in s["requests"][-30:]:
        shown = {k: v for k, v in r.items() if k not in ("baseline", "fingerprint")}
        if r.get("delivery"):
            box = store.inbox(r["delivery"])
            shown["delivery_status"] = box["status"]
            shown["delivery_error"] = box.get("error")
            shown["receiving_agent"] = box["watcher"]
        requests.append(shown)
    return {"project": name, "revision": p["revision"], "profile": p["profile"], "sources": s["sources"],
            "requests": requests, "serial": s["serial"], "source_revision": s["source_revision"], "prompt": direction_prompt(p),
            "skill": os.path.join(HERE, "skills", "goodeye-brand", "SKILL.md")}


def mutate_direction(body):
    p = load_project(body.get("project"))
    action = body.get("action")
    if action == "request":
        target = body.get("agent")
        # Check a new target only; replaying a saved request remains idempotent after a handoff.
        seen = any(r["id"] == body.get("request_id") for r in direction.state(p)["requests"])
        if target and not seen and not delivery_store().watcher(p["name"], target):
            raise ValueError("that agent is no longer connected to this project; choose another or save without an agent")
        direction.request(p, body)
    elif action == "sources":
        if body.get("expected_source_revision") != direction.state(p)["source_revision"]:
            raise ValueError("source material changed; compare the saved sources before replacing them")
        direction.state(p)["sources"] = direction.text(body.get("sources"), "sources", required=False)
        direction.state(p)["source_revision"] += 1
        direction.changed(p)
    elif action == "accept":
        r = direction.current(p, body.get("request"), editable=False)
        if r.get("delivery"):
            r["agent"] = delivery_store().inbox(r["delivery"])["watcher"]
        direction.accept(p, body.get("request"), body.get("proposal"))
    else:
        raise ValueError("unknown direction action")
    write_project(p)
    return direction_view(p["name"])


def route_direction_requests():
    """Recoverable outbox: publish first, then record the stable delivery ID."""
    with store_lock():
        saved = read_json(os.path.join(HOME, "projects.json"), {}) or {}
        store = delivery_store()
        dirty = False
        for p in saved.values():
            for r in p.get("direction", {}).get("requests", []):
                if r["status"] == "superseded":
                    continue
                accepted = r["status"] == "accepted"
                delivery_field = "accepted_delivery" if accepted else "delivery"
                if r.get(delivery_field):
                    continue
                target = r.get("agent")
                if not target:
                    owner, _, active = store.owner(p["name"], "brand-direction")
                    target = owner if active else None
                if not target:
                    continue
                event_id = project_key(p["name"]) + "-" + r["id"] + ("-accepted" if accepted else "-direction")
                delivery = store.direct_event(p["name"], target, {
                    "decision_id": event_id, "request": r["id"], "operation": "accepted" if accepted else "request",
                    "skill": os.path.join(HERE, "skills", "goodeye-brand", "SKILL.md"),
                    "next": "Read the saved conversation with goodeye direction show --project " + shlex.quote(p["name"]),
                })
                if delivery:
                    r[delivery_field] = delivery
                    r["agent"] = store.inbox(delivery)["watcher"]
                    if r["status"] == "waiting":
                        r["status"] = "queued"
                    direction.changed(p)
                    dirty = True
        if dirty:
            write_json(os.path.join(HOME, "projects.json"), saved)


def validate_direction_artifacts(project, proposal):
    if not isinstance(proposal, dict) or not isinstance(proposal.get("artifacts", []), list):
        raise ValueError("proposal artifacts must be a list")
    out = []
    for a in proposal.get("artifacts", []):
        if not isinstance(a, dict) or not isinstance(a.get("id"), str) or not ID_RE.fullmatch(a["id"]):
            raise ValueError("each artifact needs a submitted asset id and version")
        v = next((v for v in versions_of(a["id"], project) if v["version"] == a.get("version")), None)
        if not v:
            raise ValueError("artifact version does not exist in this project")
        origin = a.get("origin")
        if origin not in ("generated", "reference", "native"):
            raise ValueError("artifact origin must be generated, reference, or native")
        provenance = direction.text(a.get("provenance"), "artifact provenance")
        prompt = direction.text(a.get("prompt", ""), "generation prompt", required=origin == "generated")
        tool = direction.text(a.get("tool", ""), "generation tool", 200, required=origin == "generated")
        out.append({"id": a["id"], "version": a["version"], "origin": origin, "provenance": provenance,
                    "prompt": prompt, "tool": tool, "dir": v["dir"], "file": v["file"], "kind": v["kind"], "title": v["title"]})
    if proposal.get("phase") == "concept" and out and not any(a["origin"] in ("generated", "reference") for a in out):
        raise ValueError("concept exploration needs an image-tool study or an inspected existing reference")
    return {**proposal, "artifacts": out}


def cmd_direction(a):
    if a.action == "show":
        print(json.dumps(direction_view(a.project), indent=2))
        return
    if a.action == "join":
        load_project(a.project)
        runtime, thread, me = session_identity(a)
        me = me or "brand"
        store = delivery_store()
        store.watch(a.project, me, runtime, thread, a.codex, a.remote)
        store.claim(a.project, "brand-direction", me)
        with store_lock():
            p = load_project(a.project)
            if not direction.state(p)["requests"]:
                direction.request(p, {"request_id": uuid.uuid4().hex, "agent": me,
                                     "message": "Develop this project's brand and art direction from existing evidence. Recover approved work before proposing changes."})
            write_project(p)
        route_direction_requests()
        ensure_server()
        print(json.dumps(direction_view(a.project), indent=2))
        print(f"next: follow the goodeye-brand skill; {wait_command(a.project, me)} receives steering requests. Acknowledge deliveries after reading.")
        return
    with store_lock():
        p = load_project(a.project)
        if a.action == "sources":
            if not a.file:
                die("sources needs --file with source paths or links, one per line")
            with open(a.file) as f:
                sources = f.read()
            mutate_direction({"project": a.project, "action": "sources", "sources": sources,
                              "expected_source_revision": direction.state(p)["source_revision"]})
        elif a.action == "update":
            direction.update(p, a.request, a.status, a.message)
            write_project(p)
        elif a.action == "propose":
            proposal = validate_direction_artifacts(a.project, read_json(a.file) if a.file else None)
            direction.propose(p, a.request, proposal)
            write_project(p)
    print(json.dumps(direction_view(a.project), indent=2))


def set_collection(body):
    asset_id, project, collection = body.get("id"), body.get("project"), body.get("collection")
    if not isinstance(asset_id, str) or not ID_RE.fullmatch(asset_id) or not isinstance(project, str):
        raise ValueError("id and project are required")
    if not isinstance(collection, str) or len(collection) > 100:
        raise ValueError("collection must be text, up to 100 characters")
    vs = versions_of(asset_id, project)
    if not vs:
        raise ValueError("unknown item")
    # Organization is mutable; the submitted files and profile snapshots stay frozen.
    assignments = read_json(os.path.join(HOME, "collections.json"), {}) or {}
    assignments[item_key(project, asset_id)] = {"collection": collection.strip(), "seq": vs[-1]["seq"]}
    write_json(os.path.join(HOME, "collections.json"), assignments)
    return {"ok": True}


def decisions():
    out = []
    try:
        with open(DECISIONS) as f:
            for line in f:
                # An interrupted append may leave an incomplete last record.
                if line.endswith("\n") and line.strip():
                    record = json.loads(line)
                    out.extend(record if isinstance(record, list) else [record])
    except OSError:
        pass
    return out


def append_decisions(rows):
    """Publish a whole verdict batch atomically, preserving the existing JSONL format.

    Caller holds store_lock. A partial final record from an older writer is discarded.
    """
    try:
        with open(DECISIONS, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        data = b""
    if data and not data.endswith(b"\n"):
        data = data[:data.rfind(b"\n") + 1]
    with atomic_file(DECISIONS, "wb") as f:
        f.write(data)
        for row in rows:
            f.write((json.dumps(row, allow_nan=False) + "\n").encode())


def item_collection(meta, assignments):
    assignment = assignments.get(item_key(meta.get("project") or "", meta["id"]))
    if assignment and assignment["seq"] >= meta["seq"]:
        return assignment["collection"]
    return meta.get("collection", "")


def all_items():
    by_dir = {}
    by_key = {}
    for d in decisions():
        by_key.setdefault((d.get("project") or "", d["id"], d["version"]), []).append(d)
        if d.get("dir"):
            by_dir.setdefault(d["dir"], []).append(d)
    assignments = read_json(os.path.join(HOME, "collections.json"), {}) or {}
    items = []
    for (project, asset_id), vs in asset_groups().items():
        for v in vs:
            v["decisions"] = by_dir.get(v["dir"], by_key.get((project, asset_id, v["version"]), []))
            v["checks"] = checks_for(v)
            add_dims(v)
            last = v["decisions"][-1]["verdict"] if v["decisions"] else "pending"
            v["status"] = "pending" if last == "reopened" else last
        latest = vs[-1]
        key = item_key(project, asset_id)
        items.append({"id": asset_id, "key": key, "project": project, "title": latest.get("title", asset_id),
                      "collection": item_collection(latest, assignments),
                      "status": latest["status"], "updated": max([latest["submitted_at"]] + [d["at"] for v in vs for d in v["decisions"]]),
                      "slot": latest.get("slot"), "versions": vs})
    items.sort(key=lambda i: i["updated"], reverse=True)
    return items


# ---------- placement spec checks ----------

def _image_size(path):
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


def image_size(path):
    try:
        dims = _image_size(path)
        return dims if dims and all(math.isfinite(n) and n > 0 for n in dims) else None
    except (ValueError, IndexError, struct.error):
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
        if dims and all(n > 0 for n in dims) and not is_svg:
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
_DIMS = {}


def add_dims(v):
    """Width and height for still images (GIF and video already have them), so the phone card can size itself."""
    def dims(rel):
        key = os.path.join(v["dir"], rel)
        if key not in _DIMS:
            d = image_size(os.path.join(ASSETS, key))
            _DIMS[key] = {"width": d[0], "height": d[1]} if d else {}
        return _DIMS[key]
    if v.get("kind") == "image" and not (v.get("media") or {}).get("width"):
        v["media"] = {**(v.get("media") or {}), **dims(v["file"])}
    for o in v.get("options") or []:
        if o.get("kind") == "image" and not (o.get("media") or {}).get("width"):
            o["media"] = {**(o.get("media") or {}), **dims("options/" + o["file"])}


def checks_for(meta, base=None):
    """Checks stored at submit, or computed once for items submitted before checks existed."""
    if "checks" in meta:
        return meta["checks"]
    key = meta["dir"]
    if base is not None or key not in _CHECK_CACHE:
        base = base or os.path.join(ASSETS, meta["dir"])
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


def notify_new(meta, force=False):
    """Opt-in push (ntfy). Sends the title and a board link only: no image, no reasoning."""
    cfg = load_config()
    n = cfg.get("notify") or {}
    url = n.get("ntfy")
    if not url or (n.get("enabled") is False and not force):
        return
    host = f"localhost:{PORT}"
    if cfg.get("lan"):
        addrs = lan_addresses()
        if addrs:
            host = f"{addrs[0][1]}:{PORT}"
    title = f"{meta.get('project') + ': ' if meta.get('project') else ''}{meta['title']}"
    req = urllib.request.Request(url, data=f"{meta['version']} is ready for review".encode(), method="POST",
                                 headers={"Title": title.encode("ascii", "replace").decode(), "Click": f"http://{host}/#{item_key(meta.get('project') or '', meta['id'])}",
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
        if not isinstance(v, dict) or any(
                isinstance(v.get(n), bool) or not isinstance(v.get(n), (int, float)) or not math.isfinite(v[n])
                for n in ("value", "max", "bar") if n == "value" or n in v):
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
    if not isinstance(r, dict) or not isinstance(r.get("summary"), str) or not r["summary"].strip():
        die("reasoning needs a non-empty 'summary'")
    decs = r.get("decisions") or []
    if not isinstance(decs, list) or not decs or not all(isinstance(d, dict) and d.get("choice") and d.get("why") for d in decs):
        die("reasoning needs 'decisions': [{\"choice\": ..., \"why\": ...}] with at least one entry")
    changes = r.get("changes", [])
    if not isinstance(changes, list) or not all(isinstance(c, dict) and c.get("change") and c.get("why") for c in changes):
        die("reasoning changes must be a list of {change, why, feedback?}")
    if not isinstance(r.get("open_questions", []), list):
        die("reasoning open_questions must be a list")
    if is_revision and not changes:
        die("this is a new version: reasoning needs 'changes': [{\"change\": ..., \"why\": ..., \"feedback\": optional quote}]")


# ---------- commands ----------

def load_options(path):
    """options.json -> list of option dicts with absolute file paths. See SKILL.md for the format."""
    spec = read_json(path)
    if not isinstance(spec, dict) or not isinstance(spec.get("options"), list) or not 2 <= len(spec["options"]) <= 9:
        die("options JSON needs 'options': [2 to 9 of {key, label, file, note?, scores?}]")
    base = os.path.dirname(os.path.abspath(path))
    keys, out = set(), []
    for o in spec["options"]:
        if not isinstance(o, dict):
            die("each option must be an object")
        key = str(o.get("key", "")).strip()
        if not ID_RE.match(key) or key in keys:
            die(f"option key {key!r} must be unique, lowercase letters, digits, dot, dash, underscore")
        keys.add(key)
        f = os.path.abspath(os.path.join(base, os.path.expanduser(str(o.get("file", "")))))
        if not os.path.isfile(f):
            die(f"option {key}: no such file {f}")
        sc = o.get("scores") or {}
        sc, _ = normalize_scores(sc)
        out.append({"key": key, "label": o.get("label") or key, "src": f, "note": o.get("note", ""), "scores": sc})
    rec = spec.get("recommended")
    if rec is not None and rec not in keys:
        die(f"recommended {rec!r} is not an option key")
    return out, rec, spec.get("question", "")


def parse_slot(key, label, project):
    if not ID_RE.match(key or ""):
        die("slot key must be lowercase letters, digits, dot, dash, underscore")
    for it in all_items():   # reuse the label already on the slot so every member says the same thing
        if it["project"] == project and it.get("slot") and it["slot"]["key"] == key and not label:
            return it["slot"]
    return {"key": key, "label": label or key}


def cmd_slot(a):
    """Put existing items (all their versions) into one slot."""
    with store_lock():
        groups = [versions_of(asset_id, a.project) for asset_id in a.ids]
        if any(not vs for vs in groups):
            die("one or more items do not exist in this project")
        projects = {vs[-1].get("project") or "" for vs in groups}
        if len(projects) != 1:
            die("a slot must belong to one project; specify --project")
        slot = parse_slot(a.key, a.label, projects.pop())
        for vs in groups:
            for v in vs:
                path = os.path.join(ASSETS, v["dir"], "meta.json")
                v["slot"] = slot
                write_json(path, v)
            print(f"{vs[-1]['id']} -> slot {slot['key']} ({slot['label']})")


def save_submission(a):
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
    if a.project is not None and (not a.project.strip() or a.project != a.project.strip() or len(a.project) > 120):
        die("project name must be 1–120 characters without surrounding spaces")
    prior = versions_of(a.id, a.project)
    project = a.project if a.project is not None else (prior[-1].get("project") or "" if prior else "")
    collection = getattr(a, "collection", None)
    if collection is not None and len(collection) > 100:
        die("collection must be at most 100 characters")
    profile = (read_json(os.path.join(HOME, "projects.json"), {}) or {}).get(project, project_record(project))
    validate_reasoning(reasoning, bool(prior))
    scores, judge = normalize_scores(read_json(a.scores) if a.scores else None)
    if a.scores and not scores:
        die(f"cannot read scores JSON {a.scores}")
    seq = (prior[-1]["seq"] + 1) if prior else 1
    version = a.version or f"v{seq}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", version):
        die("version must start with a letter or digit and contain only letters, digits, dot, dash, underscore (max 81)")
    if any(p["version"] == version for p in prior):
        die(f"{a.id}@{version} already exists; versions are frozen, pick a new one")
    contexts = [c.strip() for c in (a.context or "").split(",") if c.strip()]
    for c in contexts:
        if c not in CONTEXTS:
            die(f"unknown context {c}; use one of {', '.join(CONTEXTS)}")
    vdir = os.path.join(ASSETS, "_projects", project_key(project), a.id, version)
    with new_version_dir(vdir) as staging:
        if a.options:
            os.makedirs(os.path.join(staging, "options"))
            for o in options:
                o["file"] = f'{o["key"]}{os.path.splitext(o["src"])[1].lower()}'
                o["kind"] = kind_of(o["src"])
                o["media"] = probe_media(o["src"], o["kind"])
                shutil.copy2(o["src"], os.path.join(staging, "options", o["file"]))
            first = next((o for o in options if o["key"] == recommended), options[0])
            fname, kind = f'options/{first["file"]}', "choice"
        else:
            fname, kind = os.path.basename(src), kind_of(src)
            shutil.copy2(src, os.path.join(staging, fname))
        meta = {"id": a.id, "version": version, "seq": seq, "project": project,
                "collection": collection.strip() if collection is not None else (item_collection(prior[-1], read_json(os.path.join(HOME, "collections.json"), {}) or {}) if prior else ""),
                "profile_revision": profile["revision"], "profile_snapshot": profile["profile"],
                "title": a.title or (prior[-1]["title"] if prior else a.id), "kind": kind, "file": fname,
                "source_path": src, "size_bytes": os.path.getsize(os.path.join(staging, fname)), "contexts": contexts or (prior[-1]["contexts"] if prior else []),
                "submitted_at": now(), "media": {} if kind == "choice" else probe_media(src, kind), "reasoning": reasoning, "scores": scores, "judge": judge,
                "dir": os.path.relpath(vdir, ASSETS)}
        if kind == "choice":
            meta.update(options=options, recommended=recommended, question=question)
        slot = parse_slot(a.slot, a.slot_label, project) if a.slot else (prior[-1].get("slot") if prior else None)
        if slot:
            meta["slot"] = slot
        meta["checks"] = checks_for(meta, staging)
        write_json(os.path.join(staging, "meta.json"), meta)
    return meta


def cmd_submit(a):
    with store_lock():
        meta = save_submission(a)
    version = meta["version"]
    ensure_server()
    print(f"submitted {a.id}@{version} ({meta['kind']}) -> {URL}/#{item_key(meta.get('project') or '', a.id)}")
    for c in meta["checks"]:
        print(f"  SPEC WARNING [{c['context']}]: {c['msg']}")
    if meta["checks"]:
        print("  The reviewer sees these warnings. Fix and resubmit now if the placement is right, or explain in reasoning.")
    notify_new(meta)
    project = meta.get("project") or ""
    store = delivery_store()
    watchers = store.status(project) if project else []
    me = agent_name(a)
    if me and project and store.watcher(project, me):
        try:
            store.claim(project, a.id, me)
            print(f"claimed {a.id} for {me}: its verdicts go to {me} only.")
        except ValueError as exc:
            print(f"  not claimed: {exc}")
    if watchers:
        names = ", ".join(f"{w['name']} ({w['runtime']})" for w in watchers)
        owner, _, active = store.owner(project, a.id)
        route = f"its owner {owner}" if owner and active else "every watcher (unclaimed; first claim wins)"
        print(f"next: subscription active. Watchers: {names}. Feedback on {a.id} goes to {route}.")
    elif os.environ.get("CODEX_THREAD_ID"):
        print("next: run `goodeye watch --project PROJECT --as NAME` for this conversation, then verify with `goodeye subscription-test --project PROJECT`.")
    else:
        print("next: run `goodeye watch --project PROJECT --as NAME`, then `goodeye wait --project PROJECT --as NAME` in the background. "
              "Plain `goodeye wait` also works as a terminal fallback (no automatic wake guarantee).")


def delivery_store():
    return DeliveryStore(HOME)


def delivery_worker():
    store = delivery_store()
    while True:
        try:
            with LOCK:
                store.ingest(decisions())
            route_direction_requests()
            store.make_batches()
            store.dispatch_one()
        except (OSError, ValueError, sqlite3.Error):
            print("agent delivery store temporarily unavailable; retrying", flush=True)
        time.sleep(1)


def agent_name(a):
    return getattr(a, "as_", None) or os.environ.get("GOODEYE_AGENT") or None


def session_identity(a):
    """(runtime, thread, default name) for this agent session. Codex threads get a push; everything else pulls."""
    runtime = getattr(a, "runtime", None)
    codex_thread = getattr(a, "thread", None) or os.environ.get("CODEX_THREAD_ID")
    if not runtime:
        runtime = "codex" if os.environ.get("CODEX_THREAD_ID") else "pull"
    if runtime == "codex":
        thread = codex_thread
        default = "codex-" + (thread or "")[:8] if thread else None
    else:
        thread = getattr(a, "thread", None) or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
        default = ("claude-" + thread[:8]) if os.environ.get("CLAUDE_CODE_SESSION_ID") and thread else None
    return runtime, thread, agent_name(a) or default


def wait_command(project, name):
    return f"goodeye wait --project {shlex.quote(project)} --as {shlex.quote(name)}"


def print_verdict(d):
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


def age(ts):
    if not ts:
        return "never"
    s = max(0, time.time() - ts)
    return f"{s / 60:.0f} min ago" if s < 5400 else f"{s / 3600:.1f} h ago"


def cmd_brief(a):
    """Everything a new session needs to join or take over a project, in one printout."""
    store = delivery_store()
    store.ingest(decisions())
    project, me = a.project, agent_name(a)
    items = [i for i in all_items() if i["project"] == project]
    watchers = store.status(project)
    holds = {h["item"]: h for h in store.holds(project)}
    print(f"GoodEye brief: project {project}  board {URL}")
    print(f"store {HOME}")
    profile = next((p for p in project_catalog(items) if p["name"] == project), project_record(project))
    print(f"\nBrand / art direction — revision {profile['revision']}")
    for field, value in profile["profile"].items():
        if value:
            print(f"  {field}: {value}")
    if not any(profile["profile"].values()):
        print("  No guidelines set. Do not invent established brand rules.")
    print("Collections: " + (", ".join(profile["collections"]) or "none"))
    print("Brand workflow: goodeye direction show --project " + shlex.quote(project))
    raw = load_project(project)
    if direction.state(raw)["sources"]:
        print("Brand source material:\n" + direction.state(raw)["sources"])
    requests = direction.state(raw)["requests"]
    if requests:
        print("Current direction request: " + requests[-1]["id"] + " (" + requests[-1]["status"] + ")")
    print(f"Submit with --project {shlex.quote(project)} --collection NAME; every version snapshots these guidelines.")
    print("\nWatchers (agent sessions that receive this project's verdicts):")
    for w in watchers:
        c = w["counts"]
        unacked = c.get("pending", 0) + c.get("queued", 0)
        print(f"  - {w['name']} ({w['runtime']}), last seen {age(w['last_seen'])}; claims: {', '.join(w['claims']) or 'none'}; "
              f"unacknowledged deliveries: {unacked}")
    if not watchers:
        print("  none. Verdicts are saved but reach no agent until someone watches.")
    open_items = [i for i in items if i["status"] in ("pending", "changes")]
    print("\nOpen items (pending = reviewer's turn; changes = an agent must make a new version):")
    for i in open_items:
        v = i["versions"][-1]
        owner, pattern, active = store.owner(project, i["id"])
        who = "unclaimed" if not owner else owner + ("" if active else " (inactive watcher)")
        line = f"  - {i['status']:<8} {i['id']}@{v['version']}  owner: {who}  {i['title']}"
        print(line)
        if i["id"] in holds:
            print(f"      ON HOLD: {holds[i['id']]['note']}")
        if i["status"] == "changes":
            print(f"      feedback: {v['decisions'][-1]['feedback'].strip()}")
    if not open_items:
        print("  none")
    others = [h for k, h in holds.items() if k not in {i["id"] for i in open_items}]
    for h in others:
        print(f"  hold on {h['item']}: {h['note']}")
    counts = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    print("\nAll items: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"))
    name = me or "YOURNAME"
    print("\nTo join this project:")
    if watchers:
        print(f"  take over one watcher:   goodeye handoff --project {shlex.quote(project)} --from {watchers[0]['name']} --to {name}")
    print(f"  or watch alongside:      goodeye watch --project {shlex.quote(project)} --as {name}")
    print(f"  split the work:          goodeye claim 'ITEM-OR-GLOB' --project {shlex.quote(project)} --as {name}")
    print(f"  then listen (pull):      {wait_command(project, name)}   (run it in the background; rerun after each result)")
    print("  Codex sessions get pushed wake-ups instead; they read with `goodeye inbox` and confirm with `goodeye ack`.")


def cmd_subscription(a):
    store = DeliveryStore(getattr(a, "store", None) or HOME)
    try:
        if a.cmd == "subscribe":
            store.ingest(decisions())
            result = store.subscribe(a.project, a.thread or os.environ.get("CODEX_THREAD_ID"), a.codex, a.remote, agent_name(a))
            ensure_server()
            print(json.dumps({"project": result["project"], "name": result["name"], "thread": result["thread"], "subscription": result["id"]}))
            print("Subscribed to future decisions. Run goodeye subscription-test --project " + repr(a.project) + " to verify receipt.")
        elif a.cmd == "watch":
            store.ingest(decisions())
            runtime, thread, name = session_identity(a)
            if not name:
                die("name this watcher with --as NAME (or set GOODEYE_AGENT)")
            result = store.watch(a.project, name, runtime, thread, a.codex, a.remote)
            if runtime == "codex":
                ensure_server()
            print(json.dumps({"project": result["project"], "name": result["name"], "runtime": result["runtime"], "subscription": result["id"]}))
            if runtime == "pull":
                print(f"next: run `{wait_command(a.project, name)}` in the background; rerun it after each result.")
            else:
                print(f"next: verify with `goodeye subscription-test --project {shlex.quote(a.project)} --as {name}`.")
        elif a.cmd in ("unwatch", "unsubscribe"):
            store.unwatch(a.project, agent_name(a) if a.cmd == "unwatch" else getattr(a, "as_", None))
            print("Stopped watching. Saved decisions, claims and delivery history retained.")
        elif a.cmd in ("subscriptions", "watchers"):
            print(json.dumps(store.status(getattr(a, "project", None)), indent=2))
        elif a.cmd == "subscription-test":
            ensure_server()
            for ident in store.probe(a.project, agent_name(a)):
                print("Test delivery: " + ident)
        elif a.cmd == "inbox":
            box = store.inbox(a.delivery)
            print(json.dumps(box, indent=2))
            unclaimed = sorted({d["id"] for d in box["decisions"] if d.get("kind") not in ("probe", "direction") and not d.get("owner")})
            if unclaimed:
                print(f"UNCLAIMED: {', '.join(unclaimed)}. Every watcher ({', '.join(box['watchers'])}) got these. Before you work on one, run "
                      f"`goodeye claim ID --project {shlex.quote(box['project'])} --as {box['watcher']}`. First claim wins; if yours fails, leave it to the owner.")
            print("next: acknowledge receipt with goodeye ack, then act on each verdict using the GoodEye skill. For kind=direction, follow the goodeye-brand skill and the current saved conversation. A probe needs no asset changes.")
        elif a.cmd == "ack":
            store.acknowledge(a.delivery, agent_name(a) or a.thread or os.environ.get("CODEX_THREAD_ID"))
            print("Receipt acknowledged. This is not an approval or completion record.")
        elif a.cmd == "retry-delivery":
            store.retry(a.delivery)
            ensure_server()
            print("Delivery queued for retry with the same ID.")
        elif a.cmd == "claim":
            me = agent_name(a) or die("claim needs --as NAME (or GOODEYE_AGENT)")
            store.claim(a.project, a.item, me, a.note, a.force)
            print(f"{a.item} is claimed by {me}. Its verdicts now go to {me} only.")
        elif a.cmd == "release":
            store.release(a.project, a.item, agent_name(a))
            print(f"{a.item} released. Its next verdict goes to every watcher.")
        elif a.cmd == "claims":
            rows = store.claims(a.project)
            print(json.dumps(rows, indent=2) if a.json else "\n".join(f"{r['pattern']:<32} {r['owner']}" + (f"  ({r['note']})" if r["note"] else "") for r in rows) or "no claims")
        elif a.cmd == "hold":
            store.hold(a.project, a.item, a.note)
            print(f"{a.item} is on hold: no reminders until `goodeye unhold {a.item} --project {shlex.quote(a.project)}`.")
        elif a.cmd == "unhold":
            store.unhold(a.project, a.item)
            print(f"{a.item} is off hold.")
        elif a.cmd == "handoff":
            runtime, thread, _ = session_identity(a)
            out = store.handoff(a.project, a.to, a.from_, runtime, thread, a.codex, a.remote)
            print(json.dumps(out))
            print(f"{out['to']} now holds {out['from']}'s claims and cursor. {len(out['moved_deliveries'])} unacknowledged delivery(ies) moved.")
            if runtime == "pull":
                print(f"next: run `{wait_command(a.project, a.to)}` in the background.")
            else:
                ensure_server()
    except ValueError as exc:
        die(str(exc))


def remind(project, reminded, keep):
    """Changes the reviewer asked for that never came back: once per 6 h, never for held items."""
    stale = [i for i in all_items() if i["status"] == "changes" and (not project or i["project"] == project) and keep(i)
             and hours_since(i["versions"][-1]["decisions"][-1]["at"]) >= stale_hours()
             and hours_since(reminded.get(item_key(i["project"], i["id"]) + "@" + i["versions"][-1]["version"], "")) >= 6]
    for i in stale:
        v = i["versions"][-1]
        print(f"REMINDER: changes requested {hours_since(v['decisions'][-1]['at']):.0f} h ago on {i['id']}@{v['version']} ({i['title']}); no new version yet.")
        print(f"  feedback: {v['decisions'][-1]['feedback'].strip()}")
        reminded[item_key(i["project"], i["id"]) + "@" + v["version"]] = now()
    if stale:
        write_json(REMINDED, reminded)
    return bool(stale)


def cmd_wait(a):
    me = agent_name(a)
    if me and not a.project:
        die("--as needs --project")
    store = delivery_store()
    sub = None
    if me:
        runtime, thread, _ = session_identity(argparse.Namespace(runtime="pull", thread=None, as_=me))
        try:
            sub = store.watch(a.project, me, "pull", thread)
        except ValueError as exc:
            die(str(exc))
    os.makedirs(AGENTS, exist_ok=True)
    mine = os.path.join(AGENTS, f"{os.getpid()}.json")
    write_json(mine, {"pid": os.getpid(), "project": a.project or "", "name": me or "", "since": now()})
    atexit.register(lambda: os.path.exists(mine) and os.remove(mine))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    held = {(h["project"], h["item"]) for h in store.holds()}
    reminded = read_json(REMINDED, {}) or {}
    if sub:
        return wait_pull(a, store, sub, me, held, reminded)
    # Legacy terminal listener: every verdict, tracked in delivered.json. Independent of watchers.
    pending_verdicts = [d for d in decisions() if d["decision_id"] not in set(read_json(DELIVERED, [])) and (not a.project or d.get("project") == a.project)]
    if not pending_verdicts and remind(a.project, reminded, lambda i: (i["project"], i["id"]) not in held):
        print("next: submit the new versions, then run `goodeye wait` again.")
        return
    delivered = set(read_json(DELIVERED, []))
    start = time.time()
    while True:
        new = [d for d in decisions() if d["decision_id"] not in delivered and (not a.project or d.get("project") == a.project)]
        if new:
            for d in new:
                print_verdict(d)
                print()
            delivered |= {d["decision_id"] for d in new}
            write_json(DELIVERED, sorted(delivered))
            return
        if a.timeout and time.time() - start > a.timeout:
            print("no verdict yet (timeout)")
            sys.exit(1)
        time.sleep(1.5)


def wait_pull(a, store, sub, me, held, reminded):
    project, start, first = a.project, time.time(), True
    while True:
        with LOCK:
            store.ingest(decisions())
        store.make_batches(debounce=0 if first else 2)
        got = store.pull(sub["id"])
        if got:
            for box in got:
                print(f"DELIVERY {box['id']} to {me} ({len(box['decisions'])} decision(s))")
                for d in box["decisions"]:
                    if d.get("kind") == "probe":
                        print("PROBE: delivery test only. Acknowledge it; change nothing.\n")
                        continue
                    if d.get("kind") == "direction":
                        print("BRAND DIRECTION: " + json.dumps(d))
                        print("  next: read the goodeye-brand skill and run goodeye direction show --project " + shlex.quote(project))
                        continue
                    print_verdict(d)
                    if d.get("owner") == me:
                        print("  owner: you")
                    else:
                        print(f"  owner: UNCLAIMED. All watchers got this ({', '.join(box['watchers'])}). Before you work on it, run")
                        print(f"        goodeye claim {d['id']} --project {shlex.quote(project)} --as {me}")
                        print("        First claim wins. If yours fails, another watcher owns it: leave it alone.")
                    print()
                print(f"ack: goodeye ack --delivery {box['id']} --as {me}   (receipt only; not approval or completion)\n")
            print(f"next: act on each verdict, ack each delivery, then run `{wait_command(project, me)}` again.")
            return
        if first:
            first = False
            def mine_or_free(i):
                owner, _, active = store.owner(project, i["id"])
                return (project, i["id"]) not in held and (owner == me or not (owner and active))
            if remind(project, reminded, mine_or_free):
                print(f"next: submit the new versions (or `goodeye hold ID --project {shlex.quote(project)} --note WHY` if the reviewer paused them), "
                      f"then run `{wait_command(project, me)}` again.")
                return
        store.touch(sub["id"])
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


def server_ready():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1) as response:
            state = json.load(response)
        return state.get("service") == "goodeye" and state.get("store") == os.path.realpath(HOME)
    except (OSError, ValueError, AttributeError):
        return False


def ensure_server():
    if port_open():
        if server_ready():
            return
        die(f"port {PORT} is serving another app or store; choose a different GOODEYE_PORT")
    with open(os.path.join(HOME, "server.log"), "a") as log:
        subprocess.Popen([sys.executable, os.path.realpath(__file__), "serve"], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if server_ready():
            return
        time.sleep(0.1)
    die(f"board did not start on port {PORT}; see {os.path.join(HOME, 'server.log')}")


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


def media_key():
    """Derived from the phone token: opens files only (never verdicts or settings); changes with --new-token."""
    return hmac.new(phone_token().encode(), b"goodeye-media", hashlib.sha256).hexdigest()[:32]


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

    def log_request(self, code="-", size="-"):
        """Phone troubleshooting: one line per request from another device, in ~/.goodeye/access.log (kept small)."""
        if self.client_is_local():
            return
        try:
            path = os.path.join(HOME, "access.log")
            if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
                os.replace(path, path + ".1")
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            auth = "cookie" if "goodeye=" in (self.headers.get("Cookie") or "") else ("key" if q.get("k") else "none")
            ua = (self.headers.get("User-Agent") or "")[:90]
            with open(path, "a") as f:
                f.write(f"{now()} {self.client_address[0]} {self.command} {u.path} {code} auth={auth} range={self.headers.get('Range') or '-'} ua={ua}\n")
        except OSError:
            pass

    def client_is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def host_is_local(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}

    def has_media_key(self):
        """Read-only key for /files. iPhone video (and Home Screen apps) can load media without the sign-in cookie."""
        if not LAN or not urllib.parse.urlparse(self.path).path.startswith("/files/"):
            return False
        k = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("k") or [""])[0]
        return bool(k) and hmac.compare_digest(k, media_key())

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
        return LAN and (self.has_token() or self.has_media_key())

    def allowed_origin(self):
        """Writes must come from the board page itself. Browsers always send Origin on cross-site POSTs."""
        origin = self.headers.get("Origin")
        if origin is None:
            return self.client_is_local() and self.host_is_local()   # a CLI client on this machine
        return origin.lower() == "http://" + (self.headers.get("Host") or "").lower()

    def save_settings(self, body):
        """Board-wide settings. Phone mode is not switchable here: turning it off from a phone would lock the phone out."""
        with store_lock():
            cfg = load_config()
            n = dict(cfg.get("notify") or {})
            if "ntfy" in body:
                url = str(body["ntfy"] or "").strip()
                if url and not re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9_-]{8,}$", url):
                    return self.send(400, {"error": "use an https ntfy topic URL with a long topic name"})
                if url:
                    n["ntfy"] = url
                else:
                    n.pop("ntfy", None)
            if "notify_enabled" in body:
                n["enabled"] = bool(body["notify_enabled"])
            cfg["notify"] = n
            if "stale_hours" in body:
                try:
                    value = float(body["stale_hours"])
                    if not math.isfinite(value):
                        raise ValueError("nonfinite hours")
                    cfg["stale_hours"] = max(1.0, min(24 * 14.0, value))
                except (TypeError, ValueError):
                    return self.send(400, {"error": "stale_hours must be a number"})
            write_json(CONFIG, cfg)
        if body.get("test_notify"):
            if not n.get("ntfy"):
                return self.send(400, {"error": "set an ntfy topic first"})
            notify_new({"id": "test", "title": "GoodEye test notification", "version": "v1", "project": ""}, force=True)
        return self.send(200, {"ok": True})

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
        if path == "/api/health" and self.client_is_local():
            return self.send(200, {"service": "goodeye", "store": os.path.realpath(HOME)})
        if path == "/manifest.webmanifest":
            return self.send(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
        if path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            return self.send(200, app_icon(512 if "512" in path else 192 if "192" in path else 180), "image/png", cache="max-age=86400")
        if path == "/api/settings":
            cfg = load_config()
            n = cfg.get("notify") or {}
            return self.send(200, {"version": __version__, "notify": {"configured": bool(n.get("ntfy")), "enabled": bool(n.get("ntfy")) and n.get("enabled") is not False,
                                                                      "topic": n.get("ntfy", "")},
                                   "stale_hours": stale_hours(), "phone_mode": bool(cfg.get("lan")), "local": self.client_is_local()})
        if path == "/api/pair":
            if not self.client_is_local():
                return self.send(403, {"error": "pair from this computer"})
            urls = phone_urls(self.server.server_address[1]) if LAN else []
            from goodeye_qr import qr_matrix, to_svg
            return self.send(200, {"lan": LAN, "urls": [{"label": l, "url": u, "svg": to_svg(qr_matrix(u))} for l, u in urls]})
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "board.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8", csp=BOARD_CSP)
        if path == "/api/direction":
            project = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("project", [""])[0]
            try:
                return self.send(200, direction_view(project))
            except ValueError as exc:
                return self.send(404, {"error": str(exc)})
        if path == "/api/items":
            extra = {"mkey": media_key()} if LAN and not self.host_is_local() and self.has_token() else {}
            store = delivery_store()
            items = all_items()
            return self.send(200, {"items": items, "projects": project_catalog(items), "agents": agents_alive(), "subscriptions": store.status(), "claims": store.claims(), "holds": store.holds(), "stale_hours": stale_hours(), **extra})
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
        # WebKit can apply document sandbox restrictions to its native video player.
        # These fixed video MIME types use the media decoder (with nosniff above).
        # Keep the script-free sandbox for SVG, HTML, and every other uploaded type.
        if ctype not in ("video/mp4", "video/webm", "video/quicktime"):
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
        route = urllib.parse.urlparse(self.path).path
        if route not in ("/api/decide", "/api/settings", "/api/clientlog", "/api/project", "/api/collection", "/api/direction"):
            return self.send(404, {"error": "not found"})
        # A JSON content type forces a CORS preflight for cross-site callers, which this server never approves.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.send(415, {"error": "send application/json"})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self.send(400, {"error": "bad length"})
        if length < 0:
            return self.send(400, {"error": "bad length"})
        if length > MAX_BODY:
            return self.send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self.send(400, {"error": "bad json"})
        if route == "/api/clientlog":
            try:
                with open(os.path.join(HOME, "client.log"), "a") as f:
                    f.write(f"{now()} {self.client_address[0]} {json.dumps(body)[:2000]}\n")
            except OSError:
                pass
            return self.send(200, {"ok": True})
        if route == "/api/settings":
            return self.save_settings(body)
        with store_lock():
            try:
                if route == "/api/direction":
                    return self.send(200, mutate_direction(body))
                if route == "/api/project":
                    return self.send(200, save_project(body))
                if route == "/api/collection":
                    return self.send(200, set_collection(body))
                return self.save_decision(body)
            except ValueError as exc:
                return self.send(409, {"error": str(exc)})

    def save_decision(self, body):
        request_id = body.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{16,80}", request_id)):
            return self.send(400, {"error": "invalid request_id"})
        request_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if request_id:
            previous = next((d for d in decisions() if d.get("request_id") == request_id), None)
            if previous:
                if previous.get("request_hash") != request_hash:
                    return self.send(409, {"error": "request_id already used for a different verdict"})
                return self.send(200, previous)
        verdict = body.get("verdict")
        if verdict not in VERDICTS:
            return self.send(400, {"error": "verdict must be one of " + ", ".join(VERDICTS)})
        if not ID_RE.match(str(body.get("id", ""))):
            return self.send(400, {"error": "bad id"})
        feedback = str(body.get("feedback", ""))[:20000]
        for field in ("ranking", "option_notes"):
            if field in body and (not isinstance(body[field], list) or any(not isinstance(r, dict) for r in body[field])):
                return self.send(400, {"error": field + " must be a list of objects"})
        rows_in = (body.get("ranking") or []) + (body.get("option_notes") or [])
        notes = any(str(r.get("note", "")).strip() for r in rows_in)
        if verdict == "changes" and not feedback.strip() and not notes:
            return self.send(400, {"error": "say what to change"})
        if "project" in body and not isinstance(body["project"], str):
            return self.send(400, {"error": "project must be text"})
        meta = next((v for v in versions_of(str(body.get("id", "")), body.get("project")) if v["version"] == body.get("version")), None)
        if not meta:
            return self.send(404, {"error": "unknown asset version"})
        if verdict == "reopened":
            cur = next((i for i in all_items() if i["id"] == meta["id"] and i["project"] == (meta.get("project") or "")), None)
            if not cur or cur["versions"][-1]["version"] != meta["version"] or cur["status"] not in ("rejected", "not_chosen"):
                return self.send(400, {"error": "only a rejected or not-chosen latest version can be reopened"})
        d = {"decision_id": uuid.uuid4().hex, "id": meta["id"], "version": meta["version"], "dir": meta["dir"],
             "project": meta.get("project", ""), "verdict": verdict, "feedback": feedback, "at": now()}
        if meta.get("kind") == "choice":
            keys = {o["key"] for o in meta.get("options", [])}
            clean = lambda rows: [{"key": str(r.get("key")), "note": str(r.get("note", ""))[:5000]} for r in rows or [] if isinstance(r.get("key"), str) and r["key"] in keys]
            d["ranking"], d["option_notes"] = clean(body.get("ranking")), [r for r in clean(body.get("option_notes")) if r["note"].strip()]
            if verdict == "picked" and not d["ranking"]:
                return self.send(400, {"error": "pick at least one option"})
        elif verdict == "picked":
            return self.send(400, {"error": "picked is only for choice items"})
        if verdict == "changes" and not feedback.strip() and not any(
                r["note"].strip() for r in d.get("ranking", []) + d.get("option_notes", [])):
            return self.send(400, {"error": "say what to change on a valid option"})
        latest = versions_of(meta["id"], meta.get("project") or "")[-1]
        if latest["version"] != meta["version"]:
            return self.send(409, {"error": "a newer version is ready; refresh before reviewing"})
        if request_id:
            d.update(request_id=request_id, request_hash=request_hash)
        rows = [d]
        slot = meta.get("slot")
        if slot and verdict == "not_chosen":
            filler = next((i for i in all_items() if i.get("slot") and i["slot"]["key"] == slot["key"] and i["project"] == meta.get("project", "") and i["status"] in FILLED), None)
            d["feedback"] = feedback or (f"Slot '{slot['label']}' was filled by {filler['id']}@{filler['versions'][-1]['version']}." if filler else f"Closed in slot '{slot['label']}'.")
        if slot and verdict in FILLED:
            d["slot_label"], d["closed"] = slot["label"], []
            for sib in all_items():
                if sib["project"] != meta.get("project", "") or sib["id"] == meta["id"] or not sib.get("slot") or sib["slot"]["key"] != slot["key"]:
                    continue
                if sib["status"] in OPEN + FILLED:
                    sv = sib["versions"][-1]
                    why = "replaced by" if sib["status"] in FILLED else "filled by"
                    rows.append({"decision_id": uuid.uuid4().hex, "id": sib["id"], "version": sv["version"], "dir": sv["dir"],
                                 "project": sv.get("project", ""), "verdict": "not_chosen", "at": d["at"], "by": d["decision_id"],
                                 "feedback": f"Slot '{slot['label']}' was {why} {meta['id']}@{meta['version']}."})
                    d["closed"].append(f"{sib['id']}@{sv['version']}")
        append_decisions(rows)
        self.send(200, d)


# ---------- demo ----------

def png(path, w, h, draw):
    """Write an RGB PNG with the standard library. draw(x, y) -> (r, g, b)."""
    with open(path, "wb") as f:
        f.write(png_bytes(w, h, draw))


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
    with store_lock():
        append_decisions([d])
        write_json(DELIVERED, sorted(set(read_json(DELIVERED, [])) | {d["decision_id"]}))


def watch_code():
    """Restart the server in place when this file changes (git pull), so an update never leaves a stale server."""
    watched = [os.path.realpath(__file__), os.path.join(HERE, "goodeye_qr.py"), os.path.join(HERE, "goodeye_delivery.py"), os.path.join(HERE, "goodeye_direction.py")]
    stamp = lambda: [os.path.getmtime(p) if os.path.exists(p) else 0 for p in watched] + [bool(load_config().get("lan"))]
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
    threading.Thread(target=delivery_worker, daemon=True).start()
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
        v = it["versions"][-1]
        if v["status"] not in FILLED:
            continue
        d = v["decisions"][-1]
        rel = v["file"]
        if v["kind"] == "choice" and d.get("ranking"):
            opt = next((o for o in v["options"] if o["key"] == d["ranking"][0]["key"]), None)
            rel = "options/" + opt["file"] if opt else rel
        src = os.path.join(ASSETS, v["dir"], rel)
        dest_dir = os.path.join(a.dir, *([] if a.project else [project_key(it["project"])]), it["id"])
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{re.sub(r'[^A-Za-z0-9._-]', '_', v['version'])}-{os.path.basename(rel)}")
        shutil.copy2(src, dest)
        notes = [d["feedback"].strip()] if d["feedback"].strip() else []
        notes += [f"{r['key']}: {r['note'].strip()}" for r in (d.get("ranking") or []) + (d.get("option_notes") or []) if r.get("note", "").strip()]
        manifest.append({"id": it["id"], "title": it["title"], "project": it["project"], "version": v["version"],
                         "verdict": d["verdict"], "approved_at": d["at"], "file": os.path.relpath(dest, a.dir),
                         "collection": it["collection"], "profile_revision": v.get("profile_revision"), "profile_snapshot": v.get("profile_snapshot"),
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
    s.add_argument("--collection", help="collection within the project")
    s.add_argument("--version")
    s.add_argument("--context", help=",".join(CONTEXTS))
    s.add_argument("--reasoning", required=True, help="JSON: summary, decisions[], changes[] (required from v2)")
    s.add_argument("--scores", help="JSON: {scores: {metric: {value, max, bar, group}}, judge: {name, pass, notes}}")
    s.add_argument("--as", dest="as_", help="submitting watcher; claims the item if unclaimed (default GOODEYE_AGENT)")
    w = sub.add_parser("wait")
    w.add_argument("--project")
    w.add_argument("--as", dest="as_", help="pull deliveries for this watcher (registers it); omit for the legacy all-verdicts listener")
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
    sl.add_argument("--project")
    pr = sub.add_parser("project", help="show or update a project profile")
    pr.add_argument("name")
    pr.add_argument("--config", help="JSON profile including expected_revision; omit to show current values")
    org = sub.add_parser("organize", help="assign an item to a collection")
    org.add_argument("id")
    org.add_argument("--project", required=True)
    org.add_argument("--collection", required=True)
    sub.add_parser("projects", help="list project workspaces")
    su = sub.add_parser("subscribe", help="0.7 interface: wake an existing Codex thread on future project verdicts (same as watch --runtime codex)")
    su.add_argument("--project", required=True)
    su.add_argument("--thread", help="exact UUID; defaults to CODEX_THREAD_ID")
    su.add_argument("--codex", default="codex", help="local Codex executable supporting queue")
    su.add_argument("--remote", help="optional local unix:// endpoint")
    su.add_argument("--as", dest="as_", help="watcher name (default codex-<thread prefix>)")
    wa = sub.add_parser("watch", help="register this session as a named watcher of a project (many per project)")
    wa.add_argument("--project", required=True)
    wa.add_argument("--as", dest="as_", help="watcher name, unique in the project (default GOODEYE_AGENT or derived from the session)")
    wa.add_argument("--runtime", choices=["codex", "pull"], help="codex = pushed via codex queue; pull = `goodeye wait --as` (default: codex inside Codex, else pull)")
    wa.add_argument("--thread", help="session id; Codex needs the exact thread UUID (default CODEX_THREAD_ID / CLAUDE_CODE_SESSION_ID)")
    wa.add_argument("--codex", default="codex")
    wa.add_argument("--remote")
    for name in ("unsubscribe", "unwatch"):
        us = sub.add_parser(name, help="stop watching (unwatch: one watcher; unsubscribe: all, or --as one)")
        us.add_argument("--project", required=True)
        us.add_argument("--as", dest="as_")
    for name in ("subscriptions", "watchers"):
        ws = sub.add_parser(name, help="watchers with their claims and queued versus acknowledged deliveries")
        ws.add_argument("--project")
    probe = sub.add_parser("subscription-test", help="send a receipt test without changing assets or decisions")
    probe.add_argument("--project", required=True)
    probe.add_argument("--as", dest="as_", help="test one watcher only")
    for name in ("inbox", "ack", "retry-delivery"):
        parser = sub.add_parser(name)
        parser.add_argument("--delivery", required=True)
        if name in ("inbox", "ack"):
            parser.add_argument("--store", help="store path supplied by the notification")
        if name == "ack":
            parser.add_argument("--thread", help="subscribed UUID; defaults to CODEX_THREAD_ID")
            parser.add_argument("--as", dest="as_", help="receiving watcher name")
    cl = sub.add_parser("claim", help="own an item (or a glob such as film-*): its verdicts go to you only. First claim wins")
    cl.add_argument("item")
    cl.add_argument("--project", required=True)
    cl.add_argument("--as", dest="as_")
    cl.add_argument("--note", help="why you own it, for the other watchers")
    cl.add_argument("--force", action="store_true", help="take it from another watcher (agree first)")
    rl = sub.add_parser("release", help="give up a claim; the next verdict goes to every watcher")
    rl.add_argument("item")
    rl.add_argument("--project", required=True)
    rl.add_argument("--as", dest="as_")
    cs = sub.add_parser("claims", help="who owns what")
    cs.add_argument("--project")
    cs.add_argument("--json", action="store_true")
    ho = sub.add_parser("hold", help="pause an item: no reminders until unhold")
    ho.add_argument("item")
    ho.add_argument("--project", required=True)
    ho.add_argument("--note", required=True, help="why, and who asked")
    uh = sub.add_parser("unhold")
    uh.add_argument("item")
    uh.add_argument("--project", required=True)
    hf = sub.add_parser("handoff", help="move a watcher's claims, cursor and unacknowledged deliveries to a new session")
    hf.add_argument("--project", required=True)
    hf.add_argument("--to", required=True, help="new watcher name (this session)")
    hf.add_argument("--from", dest="from_", help="old watcher name (required when several watch)")
    hf.add_argument("--runtime", choices=["codex", "pull"])
    hf.add_argument("--thread")
    hf.add_argument("--codex", default="codex")
    hf.add_argument("--remote")
    br = sub.add_parser("brief", help="print what a new session needs to join or take over a project")
    br.add_argument("--project", required=True)
    br.add_argument("--as", dest="as_")
    dr = sub.add_parser("direction", help="agent-driven brand strategy and styling conversation")
    dr.add_argument("action", choices=["show", "join", "update", "propose", "sources"])
    dr.add_argument("--project", required=True)
    dr.add_argument("--request")
    dr.add_argument("--file", help="proposal JSON, or a text file of source paths/links")
    dr.add_argument("--status", choices=["working", "needs_input"])
    dr.add_argument("--message")
    dr.add_argument("--as", dest="as_")
    dr.add_argument("--runtime", choices=["codex", "pull"])
    dr.add_argument("--thread")
    dr.add_argument("--codex", default="codex")
    dr.add_argument("--remote")
    a = p.parse_args()
    if a.cmd == "direction":
        cmd_direction(a)
        return
    if a.cmd in ("subscribe", "watch", "unsubscribe", "unwatch", "subscriptions", "watchers", "subscription-test", "inbox", "ack",
                 "retry-delivery", "claim", "release", "claims", "hold", "unhold", "handoff"):
        cmd_subscription(a)
        return
    if a.cmd == "projects":
        print(json.dumps(project_catalog(), indent=2))
        return
    if a.cmd == "project":
        if a.config:
            body = read_json(a.config)
            if not isinstance(body, dict):
                die("config must be a JSON object")
            with store_lock():
                result = save_project({**body, "name": a.name})
        else:
            result = next((p for p in project_catalog() if p["name"] == a.name), project_record(a.name))
        print(json.dumps(result, indent=2))
        return
    if a.cmd == "organize":
        with store_lock():
            set_collection(vars(a))
        print(f"{a.id} -> {a.project} / {a.collection or 'Unsorted'}")
        return
    if a.cmd == "brief":
        cmd_brief(a)
        return
    if a.cmd == "open":
        ensure_server()
        webbrowser.open(URL)
        return
    {"submit": cmd_submit, "demo": cmd_demo, "phone": cmd_phone, "notify": cmd_notify, "export": cmd_export, "slot": cmd_slot, "wait": cmd_wait, "status": cmd_status, "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        die(str(exc))
