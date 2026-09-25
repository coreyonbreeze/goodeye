"""End-to-end tests: real CLI, real server, temporary store. Run: python3 -m unittest discover tests"""
import http.client, json, os, socket, subprocess, sys, tempfile, time, unittest

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
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.1).close()
                break
            except OSError:
                time.sleep(0.1)
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

    def post(self, body, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request("POST", "/api/decide", raw if raw is not None else json.dumps(body), h)
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

    def test_demo_loads(self):
        self.cli("demo")
        out = self.cli("status").stdout
        for item in ("demo-banner@v2", "demo-icon@v1", "demo-hero-a@v1", "demo-hero-b@v1"):
            self.assertIn(item, out)


if __name__ == "__main__":
    unittest.main()
