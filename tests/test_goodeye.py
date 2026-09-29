"""End-to-end tests: real CLI, real server, temporary store. Run: python3 -m unittest discover tests"""
import http.client, json, os, re, socket, subprocess, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = [sys.executable, os.path.join(ROOT, "goodeye.py")]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class GoodEyeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.env = dict(os.environ, GOODEYE_HOME=os.path.join(self.tmp.name, "store"), GOODEYE_PORT=str(self.port))
        self.server = subprocess.Popen(CLI + ["serve", "--port", str(self.port)], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("the board server did not start")
        self.png = self.file("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.reason = self.json("r.json", {"summary": "s", "decisions": [{"choice": "c", "why": "w"}]})
        self.reason2 = self.json("r2.json", {"summary": "s", "decisions": [{"choice": "c", "why": "w"}],
                                             "changes": [{"change": "x", "why": "y"}]})

    def tearDown(self):
        self.server.terminate()
        self.server.wait(5)
        self.tmp.cleanup()

    # helpers
    def file(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def json(self, name, obj):
        return self.file(name, json.dumps(obj).encode())

    def cli(self, *args, ok=True):
        r = subprocess.run(CLI + list(args), env=self.env, capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def post(self, body, headers=None, raw=None, path="/api/decide"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request("POST", path, raw if raw is not None else json.dumps(body), h)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def get(self, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        return r.status, r.read()

    def wait_output(self):
        return self.cli("wait", "--timeout", "5").stdout

    # tests
    def test_persistent_subscription_survives_restart_and_ack_is_separate(self):
        import uuid
        thread = str(uuid.uuid4())
        log = os.path.join(self.tmp.name, "queue.log")
        fake = self.file("codex", ("#!" + sys.executable + "\n" +
            "import sys\n" +
            "with open(" + repr(log) + ", 'a') as f: f.write('queued\\n')\n" +
            "print('Queued message test-receipt for thread ' + sys.argv[sys.argv.index('--thread')+1])\n").encode())
        os.chmod(fake, 0o700)
        self.cli("subscribe", "--project", "Mosaic", "--thread", thread, "--codex", fake)
        submitted = self.cli("submit", self.png, "--id", "watched", "--project", "Mosaic", "--reasoning", self.reason)
        self.assertIn("subscription active", submitted.stdout)
        self.assertNotIn("run `goodeye wait`", submitted.stdout)
        status, decision = self.post({"id": "watched", "version": "v1", "verdict": "changes", "feedback": "less hopping"})
        self.assertEqual(status, 200)
        # A legacy consumer must not steal this event from the durable subscriber.
        self.assertIn("VERDICT CHANGES", self.wait_output())
        for _ in range(100):
            subs = json.loads(self.cli("subscriptions").stdout)
            if subs[0]["counts"].get("queued"):
                break
            time.sleep(.1)
        else:
            self.fail("server did not queue subscription notification")
        delivery = subs[0]["last"]["id"]
        self.assertIn(decision["decision_id"], self.cli("inbox", "--delivery", delivery).stdout)
        self.assertIsNone(subs[0]["last"]["acknowledged"])
        self.server.terminate()
        self.server.wait(5)
        self.server = subprocess.Popen(CLI + ["serve", "--port", str(self.port)], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        with open(log) as f:
            self.assertEqual(f.read().splitlines(), ["queued"])
        self.assertNotEqual(self.cli("ack", "--delivery", delivery, "--thread", str(uuid.uuid4()), ok=False).returncode, 0)
        self.cli("ack", "--delivery", delivery, "--thread", thread)
        _, data = self.get("/api/items")
        state = json.loads(data)
        self.assertEqual(state["subscriptions"][0]["counts"], {"acknowledged": 1})
        self.assertEqual(state["items"][0]["status"], "changes")

    def test_two_sessions_split_work_by_claims(self):
        for name in ("video", "brand"):
            self.cli("watch", "--project", "Mosaic", "--as", name, "--runtime", "pull")
        out = self.cli("submit", self.png, "--id", "film-01", "--project", "Mosaic", "--reasoning", self.reason, "--as", "video").stdout
        self.assertIn("claimed film-01 for video", out)
        self.assertIn("goes to its owner video", out)
        self.cli("submit", self.png, "--id", "logo", "--project", "Mosaic", "--reasoning", self.reason)   # unclaimed
        self.post({"id": "film-01", "version": "v1", "verdict": "changes", "feedback": "tighter"})
        self.post({"id": "logo", "version": "v1", "verdict": "changes", "feedback": "bolder"})
        video = self.cli("wait", "--project", "Mosaic", "--as", "video", "--timeout", "10").stdout
        brand = self.cli("wait", "--project", "Mosaic", "--as", "brand", "--timeout", "10").stdout
        self.assertIn("film-01@v1", video)
        self.assertIn("owner: you", video)
        self.assertIn("logo@v1", video)
        self.assertNotIn("film-01", brand)
        self.assertIn("UNCLAIMED", brand)
        self.cli("claim", "logo", "--project", "Mosaic", "--as", "brand")
        lost = self.cli("claim", "logo", "--project", "Mosaic", "--as", "video", ok=False)
        self.assertNotEqual(lost.returncode, 0)
        self.assertIn("claimed by brand", lost.stderr)
        delivery = re.search(r"ack: goodeye ack --delivery (\w+) --as video", video).group(1)
        self.assertNotEqual(self.cli("ack", "--delivery", delivery, "--as", "brand", ok=False).returncode, 0)
        self.cli("ack", "--delivery", delivery, "--as", "video")
        brief = self.cli("brief", "--project", "Mosaic", "--as", "newbie").stdout
        self.assertIn("video (pull)", brief)
        self.assertIn("owner: brand", brief)
        self.assertIn("goodeye handoff --project Mosaic --from", brief)
        state = json.loads(self.get("/api/items")[1])
        self.assertEqual(sorted(c["owner"] for c in state["claims"]), ["brand", "video"])
        self.cli("handoff", "--project", "Mosaic", "--from", "video", "--to", "video2", "--runtime", "pull")
        self.assertIn("owner: video2", self.cli("brief", "--project", "Mosaic").stdout)

    def test_hold_mutes_reminders(self):
        with open(os.path.join(self.env["GOODEYE_HOME"], "config.json"), "w") as f:
            json.dump({"stale_hours": 0}, f)
        self.cli("watch", "--project", "Demo", "--as", "me", "--runtime", "pull")
        self.cli("submit", self.png, "--id", "paused", "--project", "Demo", "--reasoning", self.reason, "--as", "me")
        self.post({"id": "paused", "version": "v1", "verdict": "changes", "feedback": "later"})
        self.cli("wait", "--project", "Demo", "--as", "me", "--timeout", "10")      # delivers the verdict
        self.cli("hold", "paused", "--project", "Demo", "--note", "reviewer paused this")
        quiet = self.cli("wait", "--project", "Demo", "--as", "me", "--timeout", "2", ok=False).stdout
        self.assertNotIn("REMINDER", quiet)
        self.assertIn("ON HOLD: reviewer paused this", self.cli("brief", "--project", "Demo").stdout)
        self.cli("unhold", "paused", "--project", "Demo")
        self.assertIn("REMINDER", self.cli("wait", "--project", "Demo", "--as", "me", "--timeout", "2").stdout)

    def test_reasoning_is_required(self):
        bad = self.json("bad.json", {"summary": "s"})
        self.assertNotEqual(self.cli("submit", self.png, "--id", "x", "--reasoning", bad, ok=False).returncode, 0)
        self.cli("submit", self.png, "--id", "x", "--reasoning", self.reason)
        r = self.cli("submit", self.png, "--id", "x", "--reasoning", self.reason, ok=False)
        self.assertIn("changes", r.stderr)
        self.cli("submit", self.png, "--id", "x", "--reasoning", self.reason2)
        self.assertIn("x@v2", self.cli("status").stdout)

    def test_approve_with_notes_means_no_second_review(self):
        self.cli("submit", self.png, "--id", "banner", "--reasoning", self.reason)
        status, _ = self.post({"id": "banner", "version": "v1", "verdict": "approved", "feedback": "nudge the logo left"})
        self.assertEqual(status, 200)
        out = self.wait_output()
        self.assertIn("VERDICT APPROVED", out)
        self.assertIn("APPROVED WITH NOTES", out)
        self.assertIn("Do NOT resubmit", out)

    def test_changes_need_feedback(self):
        self.cli("submit", self.png, "--id", "banner", "--reasoning", self.reason)
        self.assertEqual(self.post({"id": "banner", "version": "v1", "verdict": "changes"})[0], 400)
        self.assertEqual(self.post({"id": "banner", "version": "v1", "verdict": "changes", "feedback": "bigger"})[0], 200)
        self.assertIn("REVIEW AGAIN", self.wait_output())

    def test_slot_approval_closes_the_others(self):
        for i in ("a", "b", "c"):
            self.cli("submit", self.png, "--id", f"hero-{i}", "--reasoning", self.reason, "--slot", "hero", "--slot-label", "Hero")
        status, d = self.post({"id": "hero-b", "version": "v1", "verdict": "approved", "feedback": ""})
        self.assertEqual(status, 200)
        self.assertEqual(sorted(d["closed"]), ["hero-a@v1", "hero-c@v1"])
        out = self.wait_output()
        self.assertEqual(out.count("VERDICT NOT_CHOSEN"), 2)
        self.assertEqual(self.post({"id": "hero-b", "version": "v1", "verdict": "reopened"})[0], 400)   # approved: not reopenable
        self.assertEqual(self.post({"id": "hero-a", "version": "v1", "verdict": "reopened"})[0], 200)
        self.assertIn("REOPENED", self.wait_output())
        self.assertIn("pending    hero-a@v1", self.cli("status").stdout)

    def test_choice_pick_with_notes(self):
        self.file("o1.png", b"1")
        self.file("o2.png", b"2")
        opts = self.json("opts.json", {"question": "Which?", "recommended": "one", "options": [
            {"key": "one", "label": "One", "file": "o1.png"}, {"key": "two", "label": "Two", "file": "o2.png"}]})
        self.cli("submit", "--options", opts, "--id", "pick", "--reasoning", self.reason)
        self.assertEqual(self.post({"id": "pick", "version": "v1", "verdict": "picked", "ranking": []})[0], 400)
        status, _ = self.post({"id": "pick", "version": "v1", "verdict": "picked", "feedback": "",
                               "ranking": [{"key": "two", "note": ""}, {"key": "one", "note": ""}],
                               "option_notes": [{"key": "bogus", "note": "ignored"}]})
        self.assertEqual(status, 200)
        out = self.wait_output()
        self.assertIn("pick 1: two", out)
        self.assertNotIn("bogus", out)

    def test_score_shapes(self):
        for i, body in enumerate(({"clarity": 3.1}, {"clarity": {"score": 3.1}},
                                  {"scores": {"clarity": {"value": 3.1, "max": 4}}, "judge": {"name": "j"}})):
            self.cli("submit", self.png, "--id", f"s{i}", "--reasoning", self.reason, "--scores", self.json(f"s{i}.json", body))
        bad = self.json("bad.json", {"clarity": "high"})
        self.assertNotEqual(self.cli("submit", self.png, "--id", "sx", "--reasoning", self.reason, "--scores", bad, ok=False).returncode, 0)

    def test_cross_site_writes_are_refused(self):
        self.cli("submit", self.png, "--id", "banner", "--reasoning", self.reason)
        body = {"id": "banner", "version": "v1", "verdict": "approved"}
        self.assertEqual(self.post(body, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post(body, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post(body, {"Host": f"evil.example:{self.port}"})[0], 403)
        self.assertEqual(self.post({"id": "../etc", "version": "v1", "verdict": "approved"})[0], 400)
        self.assertEqual(self.cli("status").stdout.split()[0], "pending")

    def test_reads_are_limited_to_the_store(self):
        self.cli("submit", self.png, "--id", "banner", "--reasoning", self.reason)
        self.assertEqual(self.get("/files/banner/v1/a.png")[0], 200)
        self.assertEqual(self.get("/files/../decisions.jsonl")[0], 404)
        self.assertEqual(self.get("/files/%2e%2e/decisions.jsonl")[0], 404)
        self.assertEqual(self.get("/api/items", host="evil.example")[0], 403)

    def test_phone_mode_needs_the_token(self):
        with open(os.path.join(self.env["GOODEYE_HOME"], "config.json"), "w") as f:
            json.dump({"lan": True}, f)
        self.server.terminate()
        self.server.wait(5)
        self.server = subprocess.Popen(CLI + ["serve", "--port", str(self.port)], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        self.cli("submit", self.png, "--id", "banner", "--reasoning", self.reason)
        with open(os.path.join(self.env["GOODEYE_HOME"], "phone-token")) as f:
            tok = f.read().strip()
        lan = f"192.168.50.7:{self.port}"
        self.assertEqual(self.get("/api/items", host=lan)[0], 403)                 # no token
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/?t=wrong", headers={"Host": lan})
        self.assertEqual(c.getresponse().status, 403)                               # wrong token
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", f"/?t={tok}", headers={"Host": lan})
        r = c.getresponse()
        self.assertEqual(r.status, 303)
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        jar = cookie.split(";")[0]
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/items", headers={"Host": lan, "Cookie": jar})
        self.assertEqual(c.getresponse().status, 200)
        body = {"id": "banner", "version": "v1", "verdict": "approved", "feedback": ""}
        self.assertEqual(self.post(body, {"Host": lan, "Cookie": jar, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post(body, {"Host": lan, "Cookie": jar})[0], 403)      # remote writes need Origin
        self.assertEqual(self.post(body, {"Host": lan, "Cookie": jar, "Origin": f"http://{lan}"})[0], 200)

    def test_video_ranges_and_document_sandbox(self):
        # Safari probes two bytes, then requests the movie or its trailing metadata.
        data = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
        directory = os.path.join(self.env["GOODEYE_HOME"], "assets", "media", "v1")
        os.makedirs(directory)
        for name in ("clip.mp4", "clip.m4v", "clip.webm", "clip.mov", "drawing.svg", "page.html"):
            with open(os.path.join(directory, name), "wb") as f:
                f.write(data)
        def request(name, byte_range=None):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/files/media/v1/" + name, headers={"Range": byte_range} if byte_range else {})
            r = c.getresponse()
            result = (r.status, dict(r.getheaders()), r.read())
            c.close()
            return result
        for name in ("clip.mp4", "clip.m4v", "clip.webm", "clip.mov"):
            for requested, expected in ((None, data), ("bytes=0-1", data[:2]),
                                        ("bytes=12-", data[12:]), ("bytes=-16", data[-16:])):
                status, headers, body = request(name, requested)
                self.assertEqual(status, 206 if requested else 200)
                self.assertEqual(body, expected)
                self.assertEqual(int(headers["Content-Length"]), len(expected))
                self.assertEqual(headers["Accept-Ranges"], "bytes")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertTrue(headers["Content-Type"].startswith("video/"))
                self.assertNotIn("Content-Security-Policy", headers)
                if requested == "bytes=0-1":
                    self.assertEqual(headers["Content-Range"], f"bytes 0-1/{len(data)}")
            self.assertEqual(request(name, f"bytes={len(data)}-")[0], 416)
        for name in ("drawing.svg", "page.html"):
            for requested in (None, "bytes=0-1"):
                _, headers, _ = request(name, requested)
                self.assertIn("sandbox;", headers["Content-Security-Policy"])
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_unchanged_items_cost_a_304(self):
        self.cli("submit", self.png, "--id", "banner", "--reasoning", self.reason)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/items", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse(); r.read()
        etag = r.getheader("ETag")
        c.request("GET", "/api/items", headers={"Host": f"127.0.0.1:{self.port}", "If-None-Match": etag})
        self.assertEqual(c.getresponse().status, 304)

    def real_png(self, name, w, h):
        import struct, zlib
        raw = b"".join(b"\x00" + b"\x80\x80\x80" * w for _ in range(h))
        chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return self.file(name, b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    def test_spec_checks_warn_the_agent(self):
        wrong = self.real_png("header.png", 600, 400)
        out = self.cli("submit", wrong, "--id", "xh", "--reasoning", self.reason, "--context", "x-banner").stdout
        self.assertIn("SPEC WARNING [x-banner]", out)
        right = self.real_png("header2.png", 1500, 500)
        out = self.cli("submit", right, "--id", "xh2", "--reasoning", self.reason, "--context", "x-banner").stdout
        self.assertNotIn("SPEC WARNING", out)

    def test_export_copies_approved_files(self):
        self.cli("submit", self.png, "--id", "keep", "--reasoning", self.reason)
        self.cli("submit", self.png, "--id", "drop", "--reasoning", self.reason)
        self.post({"id": "keep", "version": "v1", "verdict": "approved", "feedback": "move logo left"})
        dest = os.path.join(self.tmp.name, "out")
        out = self.cli("export", dest).stdout
        self.assertIn("exported 1", out)
        with open(os.path.join(dest, "manifest.json")) as f:
            m = json.load(f)
        self.assertEqual(m[0]["id"], "keep")
        self.assertEqual(m[0]["notes_to_apply"], ["move logo left"])
        self.assertTrue(os.path.isfile(os.path.join(dest, m[0]["file"])))

    def test_stale_changes_remind_the_agent_once(self):
        with open(os.path.join(self.env["GOODEYE_HOME"], "config.json"), "w") as f:
            json.dump({"stale_hours": 0}, f)
        self.cli("submit", self.png, "--id", "slow", "--reasoning", self.reason)
        self.post({"id": "slow", "version": "v1", "verdict": "changes", "feedback": "bigger"})
        self.wait_output()                                   # delivers the verdict
        first = self.cli("wait", "--timeout", "2").stdout
        self.assertIn("REMINDER", first)
        again = self.cli("wait", "--timeout", "2", ok=False).stdout
        self.assertNotIn("REMINDER", again)                  # once per 6 hours

    def test_agent_presence_is_reported(self):
        self.cli("submit", self.png, "--id", "p", "--reasoning", self.reason, "--project", "Demo")
        w = subprocess.Popen(CLI + ["wait", "--project", "Demo"], env=self.env, stdout=subprocess.DEVNULL)
        try:
            time.sleep(1)
            status, body = self.get("/api/items")
            self.assertEqual(json.loads(body)["agents"][0]["project"], "Demo")
        finally:
            w.terminate(); w.wait(5)
        time.sleep(0.3)
        self.assertEqual(json.loads(self.get("/api/items")[1])["agents"], [])

    def test_settings_round_trip(self):
        self.assertEqual(self.post({"stale_hours": 5}, raw=None, headers=None, path="/api/settings")[0], 200)
        self.assertEqual(self.post({"ntfy": "http://insecure/topic"}, path="/api/settings")[0], 400)
        self.assertEqual(self.post({"ntfy": "https://ntfy.sh/a-long-topic-name", "notify_enabled": False}, path="/api/settings")[0], 200)
        s = json.loads(self.get("/api/settings")[1])
        self.assertEqual(s["stale_hours"], 5)
        self.assertEqual(s["notify"]["enabled"], False)
        self.assertEqual(self.post({"stale_hours": 5}, {"Origin": "https://evil.example"}, path="/api/settings")[0], 403)

    def test_demo_loads(self):
        self.cli("demo")
        out = self.cli("status").stdout
        for item in ("demo-banner@v2", "demo-icon@v1", "demo-hero-a@v1", "demo-hero-b@v1"):
            self.assertIn(item, out)


if __name__ == "__main__":
    unittest.main()
