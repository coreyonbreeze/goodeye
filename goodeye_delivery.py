"""Durable, local-only agent subscriptions. No shell or model API calls."""
import contextlib
import json
import os
import re
import shutil
import shlex
import sqlite3
import subprocess
import time
import uuid


class DeliveryStore:
    def __init__(self, home):
        self.home = os.path.abspath(home)
        os.makedirs(self.home, exist_ok=True)
        self.path = os.path.join(self.home, 'delivery.sqlite3')
        # Restrict the database before writing thread IDs or review data.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, thread TEXT NOT NULL,
                    executable TEXT NOT NULL, remote TEXT, active INTEGER NOT NULL,
                    start_after INTEGER NOT NULL, created REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_owner ON subscriptions(project) WHERE active=1;
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    project TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, subscription TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, error TEXT, receipt TEXT,
                    created REAL NOT NULL, queued REAL, acknowledged REAL);
                CREATE TABLE IF NOT EXISTS members (
                    subscription TEXT NOT NULL, event INTEGER NOT NULL, delivery TEXT NOT NULL,
                    PRIMARY KEY(subscription,event));
            ''')

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, decisions):
        with self.db() as db:
            for d in decisions:
                db.execute('INSERT OR IGNORE INTO events(id,project,payload,created) VALUES(?,?,?,?)',
                           (d['decision_id'], d.get('project', ''), json.dumps(d), time.time()))

    def subscribe(self, project, thread, executable='codex', remote=None):
        if not project.strip():
            raise ValueError('a nonempty project is required')
        try:
            thread = str(uuid.UUID(thread))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('use an exact Codex thread UUID')
        executable = shutil.which(executable)
        if not executable:
            raise ValueError('codex executable not found')
        if remote and not (remote == 'unix://' or remote.startswith('unix:///')):
            raise ValueError('only a local unix:// Codex endpoint is supported')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM subscriptions WHERE project=? AND active=1', (project,)).fetchone()
            if old:
                if old['thread'] != thread:
                    raise ValueError('project already has a subscriber; unsubscribe before changing its owner')
                return dict(old)
            cutoff = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?)',
                       (ident, project, thread, executable, remote, 1, cutoff, time.time()))
            return dict(db.execute('SELECT * FROM subscriptions WHERE id=?', (ident,)).fetchone())

    def unsubscribe(self, project):
        with self.db() as db:
            db.execute('UPDATE subscriptions SET active=0 WHERE project=?', (project,))

    def make_batches(self, debounce=2):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for sub in db.execute('SELECT * FROM subscriptions WHERE active=1').fetchall():
                events = db.execute('''SELECT seq,created FROM events e WHERE project=? AND seq>?
                    AND NOT EXISTS(SELECT 1 FROM members m WHERE m.subscription=? AND m.event=e.seq)
                    ORDER BY seq LIMIT 50''', (sub['project'], sub['start_after'], sub['id'])).fetchall()
                if not events or time.time() - events[-1]['created'] < debounce:
                    continue
                ident = uuid.uuid4().hex
                db.execute('INSERT INTO deliveries(id,subscription,status,created) VALUES(?,?,?,?)',
                           (ident, sub['id'], 'pending', time.time()))
                db.executemany('INSERT INTO members VALUES(?,?,?)',
                               [(sub['id'], ev['seq'], ident) for ev in events])

    def probe(self, project):
        with self.db() as db:
            if not db.execute('SELECT 1 FROM subscriptions WHERE project=? AND active=1', (project,)).fetchone():
                raise ValueError('subscribe this project first')
        ident = uuid.uuid4().hex
        self.ingest([{'decision_id': ident, 'project': project, 'kind': 'probe',
                      'feedback': 'Delivery test only. Acknowledge receipt; do not change assets.'}])
        self.make_batches(debounce=0)
        with self.db() as db:
            return db.execute('''SELECT delivery FROM members JOIN events ON events.seq=members.event
                JOIN subscriptions s ON s.id=members.subscription WHERE events.id=? AND s.active=1''',
                              (ident,)).fetchone()[0]

    def inbox(self, ident):
        with self.db() as db:
            row = db.execute('''SELECT d.*,s.project,s.thread FROM deliveries d
                JOIN subscriptions s ON s.id=d.subscription WHERE d.id=?''', (ident,)).fetchone()
            if not row:
                raise ValueError('unknown delivery')
            out = dict(row)
            out['decisions'] = [json.loads(r[0]) for r in db.execute('''SELECT payload FROM events
                JOIN members ON events.seq=members.event WHERE delivery=? ORDER BY seq''', (ident,))]
            return out

    def acknowledge(self, ident, thread):
        with self.db() as db:
            row = db.execute('''SELECT s.thread FROM deliveries d JOIN subscriptions s
                ON s.id=d.subscription WHERE d.id=?''', (ident,)).fetchone()
            if not row or row['thread'] != thread:
                raise ValueError('ack requires the subscribed thread UUID (CODEX_THREAD_ID or --thread)')
            db.execute("UPDATE deliveries SET status='acknowledged',acknowledged=?,lease_until=0 WHERE id=?",
                       (time.time(), ident))

    def retry(self, ident):
        with self.db() as db:
            row = db.execute('SELECT status FROM deliveries WHERE id=?', (ident,)).fetchone()
            if not row or row['status'] == 'acknowledged':
                raise ValueError('unknown or already acknowledged delivery')
            db.execute("UPDATE deliveries SET status='pending',next_attempt=0,lease_until=0 WHERE id=?", (ident,))

    def status(self):
        with self.db() as db:
            out = []
            for sub in db.execute('SELECT * FROM subscriptions WHERE active=1').fetchall():
                counts = dict(db.execute('SELECT status,COUNT(*) FROM deliveries WHERE subscription=? GROUP BY status',
                                         (sub['id'],)))
                last = db.execute('SELECT id,status,error,queued,acknowledged FROM deliveries WHERE subscription=? ORDER BY created DESC LIMIT 1',
                                  (sub['id'],)).fetchone()
                out.append({'project': sub['project'], 'thread': sub['thread'], 'subscription': sub['id'],
                            'counts': counts, 'last': dict(last) if last else None})
            return out

    def dispatch_one(self, runner=subprocess.run):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT d.*,s.thread,s.executable,s.remote FROM deliveries d
                JOIN subscriptions s ON s.id=d.subscription WHERE s.active=1 AND d.status='pending'
                AND d.next_attempt<=? AND d.lease_until<=? ORDER BY d.created LIMIT 1''',
                             (time.time(), time.time())).fetchone()
            if not row:
                return False
            row = dict(row)
            db.execute('UPDATE deliveries SET lease_until=?,attempts=attempts+1 WHERE id=?',
                       (time.time() + 60, row['id']))
        ident = row['id']
        # Only stable identifiers go into the wake message. Review text is loaded as data from inbox.
        message = (f'GoodEye feedback notification. Delivery {ident}. '
                   f'Run goodeye inbox --store {shlex.quote(self.home)} --delivery {ident} to read the saved review decisions, then '
                   f'goodeye ack --store {shlex.quote(self.home)} --delivery {ident} after reading them. '
                   'Follow the GoodEye skill for each verdict. A probe only needs acknowledgment. '
                   'Acknowledgment records receipt, not completion or approval. Deduplicate by decision_id.')
        argv = [row['executable'], 'queue', '--thread', row['thread'], '--message', message]
        if row['remote']:
            argv += ['--remote', row['remote']]
        try:
            result = runner(argv, capture_output=True, text=True, timeout=20,
                            stdin=subprocess.DEVNULL, cwd=self.home)
            # A successful CLI must explicitly acknowledge this exact target. Do not store arbitrary output.
            match = re.search(r'Queued message ([A-Za-z0-9-]+) for thread ' + re.escape(row['thread']), result.stdout or '')
            if result.returncode != 0 or not match:
                raise RuntimeError('Codex did not confirm queue acceptance')
            with self.db() as db:
                db.execute("UPDATE deliveries SET status='queued',queued=?,receipt=?,error=NULL,lease_until=0 WHERE id=? AND status='pending'",
                           (time.time(), match.group(1), ident))
        except (OSError, subprocess.SubprocessError, RuntimeError):
            # Retry failed/ambiguous sends; a crash after acceptance can duplicate a wake, never a verdict.
            delay = min(300, 2 ** min(row['attempts'] + 1, 8))
            with self.db() as db:
                db.execute('UPDATE deliveries SET error=?,next_attempt=?,lease_until=0 WHERE id=? AND status=\'pending\'',
                           ('Codex queue unavailable or acceptance unconfirmed; will retry', time.time() + delay, ident))
        return True
