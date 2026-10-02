"""Durable, local-only agent delivery. No shell or model API calls.

A project can have several watchers (agent sessions). Each watcher has a unique name in its project.
- runtime "codex": the board server pushes a wake message with `codex queue`.
- runtime "pull": the agent runs `goodeye wait --as NAME` in the background and reads the output.

Claims route work. A claim maps an item id (or a glob such as `film-*`) to one watcher. A decision on a
claimed item goes to its owner only. A decision on an unclaimed item goes to every watcher, and the first
`claim` wins. Holds mute reminders for paused items. A handoff moves one watcher's claims, cursor and
unacknowledged deliveries to another, so a new session can take over without replays or gaps.
"""
import contextlib
import fnmatch
import json
import os
import re
import shutil
import shlex
import sqlite3
import subprocess
import time
import uuid

RUNTIMES = ('codex', 'pull')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
SKIP = ''          # members.delivery value for an event routed to another watcher


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
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, thread TEXT NOT NULL,
                    executable TEXT NOT NULL, remote TEXT, active INTEGER NOT NULL,
                    start_after INTEGER NOT NULL, created REAL NOT NULL);
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
                CREATE TABLE IF NOT EXISTS claims (
                    project TEXT NOT NULL, pattern TEXT NOT NULL, owner TEXT NOT NULL,
                    note TEXT, claimed REAL NOT NULL, PRIMARY KEY(project,pattern));
                CREATE TABLE IF NOT EXISTS holds (
                    project TEXT NOT NULL, item TEXT NOT NULL, note TEXT NOT NULL,
                    held REAL NOT NULL, PRIMARY KEY(project,item));
            ''')
            cols = {r[1] for r in db.execute('PRAGMA table_info(subscriptions)')}
            # 0.7 stored one Codex owner per project. Upgrade in place: name old rows, allow many watchers.
            if 'name' not in cols:
                db.execute('ALTER TABLE subscriptions ADD COLUMN name TEXT')
                db.execute("UPDATE subscriptions SET name='codex-'||substr(thread,1,8) WHERE name IS NULL")
            if 'runtime' not in cols:
                db.execute("ALTER TABLE subscriptions ADD COLUMN runtime TEXT NOT NULL DEFAULT 'codex'")
            if 'last_seen' not in cols:
                db.execute('ALTER TABLE subscriptions ADD COLUMN last_seen REAL')
            if 'lease_token' not in {r[1] for r in db.execute('PRAGMA table_info(deliveries)')}:
                db.execute('ALTER TABLE deliveries ADD COLUMN lease_token TEXT')
            db.execute('DROP INDEX IF EXISTS one_owner')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_name ON subscriptions(project,name) WHERE active=1')

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

    # ---------- watchers ----------

    def watch(self, project, name, runtime='pull', thread=None, executable='codex', remote=None):
        if not (project or '').strip():
            raise ValueError('a nonempty project is required')
        if not NAME_RE.match(name or ''):
            raise ValueError('watcher name: letters, digits, dot, dash, underscore (max 64)')
        thread, executable, remote = self._target(runtime, thread, executable, remote)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._watch(db, project, name, runtime, thread, executable, remote)

    def _watch(self, db, project, name, runtime, thread, executable, remote):
        old = db.execute('SELECT * FROM subscriptions WHERE project=? AND name=? AND active=1',
                         (project, name)).fetchone()
        if old:
            if old['runtime'] != runtime or (thread and old['thread'] and old['thread'] != thread):
                raise ValueError(f'watcher {name!r} is already registered to another session; '
                                 f'use `goodeye handoff --to NEWNAME --from {name}` or pick another name')
            db.execute('UPDATE subscriptions SET last_seen=?,thread=?,executable=?,remote=? WHERE id=?',
                       (time.time(), thread or old['thread'], executable, remote, old['id']))
            return dict(db.execute('SELECT * FROM subscriptions WHERE id=?', (old['id'],)).fetchone())
        # A new watcher starts with future decisions; it never replays history.
        cutoff = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        ident = uuid.uuid4().hex
        db.execute('''INSERT INTO subscriptions(id,project,thread,executable,remote,active,start_after,created,name,runtime,last_seen)
                      VALUES(?,?,?,?,?,1,?,?,?,?,?)''',
                   (ident, project, thread, executable, remote, cutoff, time.time(), name, runtime, time.time()))
        return dict(db.execute('SELECT * FROM subscriptions WHERE id=?', (ident,)).fetchone())

    @staticmethod
    def _target(runtime, thread, executable, remote):
        if runtime not in RUNTIMES:
            raise ValueError('runtime must be one of: ' + ', '.join(RUNTIMES))
        if runtime == 'pull':
            return (thread or '')[:200], '', None
        try:
            thread = str(uuid.UUID(thread))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('use an exact Codex thread UUID')
        executable = shutil.which(executable)
        if not executable:
            raise ValueError('codex executable not found')
        if remote and not (remote == 'unix://' or remote.startswith('unix:///')):
            raise ValueError('only a local unix:// Codex endpoint is supported')
        return thread, executable, remote

    def subscribe(self, project, thread, executable='codex', remote=None, name=None):
        """0.7 interface: a Codex push watcher named after its thread."""
        try:
            short = str(uuid.UUID(thread))[:8]
        except (ValueError, TypeError, AttributeError):
            raise ValueError('use an exact Codex thread UUID')
        return self.watch(project, name or 'codex-' + short, 'codex', thread, executable, remote)

    def unwatch(self, project, name=None):
        with self.db() as db:
            if name:
                db.execute('UPDATE subscriptions SET active=0 WHERE project=? AND name=?', (project, name))
            else:
                db.execute('UPDATE subscriptions SET active=0 WHERE project=?', (project,))

    def unsubscribe(self, project):
        self.unwatch(project)

    def watcher(self, project, name):
        with self.db() as db:
            row = db.execute('SELECT * FROM subscriptions WHERE project=? AND name=? AND active=1',
                             (project, name)).fetchone()
            return dict(row) if row else None

    def touch(self, ident):
        with self.db() as db:
            db.execute('UPDATE subscriptions SET last_seen=? WHERE id=?', (time.time(), ident))

    # ---------- claims and holds ----------

    @staticmethod
    def _owner(db, project, item):
        """(owner, pattern, active) for an item: an exact claim first, then the longest matching glob."""
        rows = db.execute('SELECT pattern,owner FROM claims WHERE project=?', (project,)).fetchall()
        hits = [r for r in rows if r['pattern'] == item] or \
               sorted([r for r in rows if fnmatch.fnmatchcase(item, r['pattern'])], key=lambda r: -len(r['pattern']))
        if not hits:
            return None, None, False
        owner = hits[0]['owner']
        active = db.execute('SELECT 1 FROM subscriptions WHERE project=? AND name=? AND active=1',
                            (project, owner)).fetchone() is not None
        return owner, hits[0]['pattern'], active

    def owner(self, project, item):
        with self.db() as db:
            return self._owner(db, project, item)

    def claim(self, project, pattern, owner, note=None, force=False):
        if not pattern or len(pattern) > 120:
            raise ValueError('claim an item id or a glob such as film-*')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM subscriptions WHERE project=? AND name=? AND active=1',
                              (project, owner)).fetchone():
                raise ValueError(f'{owner!r} is not an active watcher of {project!r}; run `goodeye watch --project '
                                 f'{project} --as {owner}` first')
            cur, cur_pattern, cur_active = self._owner(db, project, pattern) if not any(c in pattern for c in '*?[') \
                else self._exact(db, project, pattern)
            if cur and cur != owner and not force:
                where = '' if cur_pattern == pattern else f' (through claim {cur_pattern!r})'
                state = '' if cur_active else '; that watcher is inactive, so pass --force to take it'
                raise ValueError(f'{pattern} is claimed by {cur}{where}{state}')
            db.execute('INSERT OR REPLACE INTO claims VALUES(?,?,?,?,?)', (project, pattern, owner, note, time.time()))

    @staticmethod
    def _exact(db, project, pattern):
        row = db.execute('SELECT owner FROM claims WHERE project=? AND pattern=?', (project, pattern)).fetchone()
        if not row:
            return None, None, False
        active = db.execute('SELECT 1 FROM subscriptions WHERE project=? AND name=? AND active=1',
                            (project, row['owner'])).fetchone() is not None
        return row['owner'], pattern, active

    def release(self, project, pattern, owner=None):
        with self.db() as db:
            row = db.execute('SELECT owner FROM claims WHERE project=? AND pattern=?', (project, pattern)).fetchone()
            if not row:
                raise ValueError(f'no claim on {pattern!r}')
            if owner and row['owner'] != owner:
                raise ValueError(f'{pattern} is claimed by {row["owner"]}, not {owner}')
            db.execute('DELETE FROM claims WHERE project=? AND pattern=?', (project, pattern))

    def claims(self, project=None):
        with self.db() as db:
            rows = db.execute('SELECT * FROM claims WHERE ? IS NULL OR project=? ORDER BY project,pattern',
                              (project, project)).fetchall()
            return [dict(r) for r in rows]

    def hold(self, project, item, note):
        if not (note or '').strip():
            raise ValueError('a hold needs a note that says why and who asked')
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?,?)', (project, item, note.strip(), time.time()))

    def unhold(self, project, item):
        with self.db() as db:
            db.execute('DELETE FROM holds WHERE project=? AND item=?', (project, item))

    def holds(self, project=None):
        with self.db() as db:
            rows = db.execute('SELECT * FROM holds WHERE ? IS NULL OR project=? ORDER BY project,item',
                              (project, project)).fetchall()
            return [dict(r) for r in rows]

    # ---------- handoff ----------

    def handoff(self, project, to_name, from_name=None, runtime='pull', thread=None, executable='codex', remote=None):
        """Move a watcher's claims, cursor and unacknowledged deliveries to a new or existing watcher."""
        if not NAME_RE.fullmatch(to_name or ''):
            raise ValueError('invalid destination watcher name')
        thread, executable, remote = self._target(runtime, thread, executable, remote)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute('SELECT * FROM subscriptions WHERE project=? AND active=1', (project,)).fetchall()
            if from_name is None:
                others = [r['name'] for r in active if r['name'] != to_name]
                if not others and any(r['name'] == to_name for r in active):
                    others = [to_name]
                if len(others) != 1:
                    raise ValueError('name the watcher to take over with --from; active watchers: ' + (', '.join(others) or 'none'))
                from_name = others[0]
            src = next((r for r in active if r['name'] == from_name), None)
            if not src:
                raise ValueError(f'no active watcher {from_name!r} in {project!r}')
            if to_name == from_name:
                db.execute('UPDATE subscriptions SET runtime=?,thread=?,executable=?,remote=?,last_seen=? WHERE id=?',
                           (runtime, thread, executable, remote, time.time(), src['id']))
                dst = src
            else:
                dst = self._watch(db, project, to_name, runtime, thread, executable, remote)
                db.execute('UPDATE claims SET owner=?,claimed=? WHERE project=? AND owner=?',
                           (to_name, time.time(), project, from_name))
                db.execute('UPDATE subscriptions SET start_after=MIN(start_after,?) WHERE id=?', (src['start_after'], dst['id']))
                db.execute('INSERT OR IGNORE INTO members SELECT ?,event,delivery FROM members WHERE subscription=?',
                           (dst['id'], src['id']))
                db.execute('UPDATE subscriptions SET active=0 WHERE id=?', (src['id'],))
            open_ = [r['id'] for r in db.execute(
                "SELECT id FROM deliveries WHERE subscription=? AND status IN ('pending','queued')", (src['id'],))]
            db.execute("UPDATE deliveries SET subscription=?,status='pending',next_attempt=0,lease_until=0,"
                       "lease_token=NULL,receipt=NULL,queued=NULL,error=NULL "
                       "WHERE subscription=? AND status IN ('pending','queued')", (dst['id'], src['id']))
            # Keep source membership as the delivery's immutable contents. The destination may
            # already have a skipped or delivered membership for the same event.
            return {'from': from_name, 'to': to_name, 'moved_deliveries': open_}

    # ---------- routing and delivery ----------

    def make_batches(self, debounce=2):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for sub in db.execute('SELECT * FROM subscriptions WHERE active=1').fetchall():
                events = db.execute('''SELECT seq,created,payload FROM events e WHERE project=? AND seq>?
                    AND NOT EXISTS(SELECT 1 FROM members m WHERE m.subscription=? AND m.event=e.seq)
                    ORDER BY seq LIMIT 200''', (sub['project'], sub['start_after'], sub['id'])).fetchall()
                if not events or time.time() - events[-1]['created'] < debounce:
                    continue
                mine, skip = [], []
                for ev in events:
                    p = json.loads(ev['payload'])
                    if p.get('kind') == 'probe':
                        ok = p.get('target') in (None, sub['name'])
                    else:
                        owner, _, active = self._owner(db, sub['project'], p.get('id', ''))
                        # Owned by an active watcher: only the owner. Otherwise everyone, and the first claim wins.
                        ok = owner == sub['name'] or not (owner and active)
                    (mine if ok else skip).append(ev['seq'])
                db.executemany('INSERT INTO members VALUES(?,?,?)', [(sub['id'], s, SKIP) for s in skip])
                if not mine:
                    continue
                ident = uuid.uuid4().hex
                db.execute('INSERT INTO deliveries(id,subscription,status,created) VALUES(?,?,?,?)',
                           (ident, sub['id'], 'pending', time.time()))
                db.executemany('INSERT INTO members VALUES(?,?,?)', [(sub['id'], s, ident) for s in mine])

    def probe(self, project, target=None):
        with self.db() as db:
            q = 'SELECT 1 FROM subscriptions WHERE project=? AND active=1' + (' AND name=?' if target else '')
            if not db.execute(q, (project, target) if target else (project,)).fetchone():
                raise ValueError('watch this project first' if not target else f'no active watcher {target!r}')
        ident = uuid.uuid4().hex
        self.ingest([{'decision_id': ident, 'project': project, 'kind': 'probe', 'target': target,
                      'feedback': 'Delivery test only. Acknowledge receipt; do not change assets.'}])
        self.make_batches(debounce=0)
        with self.db() as db:
            return [r[0] for r in db.execute('''SELECT delivery FROM members JOIN events ON events.seq=members.event
                JOIN subscriptions s ON s.id=members.subscription WHERE events.id=? AND s.active=1 AND delivery<>?''',
                                             (ident, SKIP))]

    def inbox(self, ident):
        with self.db() as db:
            row = db.execute('''SELECT d.*,s.project,s.thread,s.name AS watcher,s.runtime FROM deliveries d
                JOIN subscriptions s ON s.id=d.subscription WHERE d.id=?''', (ident,)).fetchone()
            if not row:
                raise ValueError('unknown delivery')
            out = dict(row)
            out['decisions'] = [json.loads(r['payload']) for r in db.execute('''SELECT DISTINCT seq,payload FROM events
                JOIN members ON events.seq=members.event WHERE delivery=? ORDER BY seq''', (ident,))]
            for d in out['decisions']:
                if d.get('kind') != 'probe':
                    owner, pattern, active = self._owner(db, out['project'], d.get('id', ''))
                    d['owner'] = owner if active else None
            out['watchers'] = [r[0] for r in db.execute(
                'SELECT name FROM subscriptions WHERE project=? AND active=1 ORDER BY name', (out['project'],))]
            return out

    def pull(self, sub_id):
        """Pending deliveries for a pull watcher. Handing them to the waiter's output is the queue step."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            ids = [r[0] for r in db.execute("SELECT d.id FROM deliveries d JOIN subscriptions s ON s.id=d.subscription "
                                            "WHERE d.subscription=? AND d.status='pending' AND s.active=1 AND s.runtime='pull' ORDER BY d.created",
                                            (sub_id,))]
            for ident in ids:
                db.execute("UPDATE deliveries SET status='queued',queued=?,receipt='stdout',attempts=attempts+1 WHERE id=?",
                           (time.time(), ident))
        return [self.inbox(i) for i in ids]

    def acknowledge(self, ident, who):
        with self.db() as db:
            row = db.execute('''SELECT s.thread,s.name FROM deliveries d JOIN subscriptions s
                ON s.id=d.subscription WHERE d.id=?''', (ident,)).fetchone()
            if not row or not who or who not in (row['thread'], row['name']):
                raise ValueError('ack requires the receiving watcher: --as NAME, or its thread UUID (CODEX_THREAD_ID or --thread)')
            db.execute("UPDATE deliveries SET status='acknowledged',acknowledged=?,lease_until=0 WHERE id=?",
                       (time.time(), ident))

    def retry(self, ident):
        with self.db() as db:
            row = db.execute('SELECT status FROM deliveries WHERE id=?', (ident,)).fetchone()
            if not row or row['status'] == 'acknowledged':
                raise ValueError('unknown or already acknowledged delivery')
            db.execute("UPDATE deliveries SET status='pending',next_attempt=0,lease_until=0,lease_token=NULL WHERE id=?", (ident,))

    def status(self, project=None):
        with self.db() as db:
            out = []
            for sub in db.execute('SELECT * FROM subscriptions WHERE active=1 AND (? IS NULL OR project=?) ORDER BY project,name',
                                  (project, project)).fetchall():
                counts = dict(db.execute('SELECT status,COUNT(*) FROM deliveries WHERE subscription=? GROUP BY status',
                                         (sub['id'],)))
                last = db.execute('SELECT id,status,error,queued,acknowledged FROM deliveries WHERE subscription=? ORDER BY created DESC LIMIT 1',
                                  (sub['id'],)).fetchone()
                claimed = [r[0] for r in db.execute('SELECT pattern FROM claims WHERE project=? AND owner=? ORDER BY pattern',
                                                   (sub['project'], sub['name']))]
                out.append({'project': sub['project'], 'name': sub['name'], 'runtime': sub['runtime'],
                            'thread': sub['thread'], 'subscription': sub['id'], 'last_seen': sub['last_seen'],
                            'claims': claimed, 'counts': counts, 'last': dict(last) if last else None})
            return out

    def dispatch_one(self, runner=subprocess.run):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT d.*,s.thread,s.executable,s.remote FROM deliveries d
                JOIN subscriptions s ON s.id=d.subscription WHERE s.active=1 AND s.runtime='codex' AND d.status='pending'
                AND d.next_attempt<=? AND d.lease_until<=? ORDER BY d.created LIMIT 1''',
                             (time.time(), time.time())).fetchone()
            if not row:
                return False
            row = dict(row)
            lease_token = uuid.uuid4().hex
            db.execute('UPDATE deliveries SET lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?',
                       (time.time() + 60, lease_token, row['id']))
        ident = row['id']
        # Only stable identifiers go into the wake message. Review text is loaded as data from inbox.
        message = (f'GoodEye feedback notification. Delivery {ident}. '
                   f'Run goodeye inbox --store {shlex.quote(self.home)} --delivery {ident} to read the saved review decisions, then '
                   f'goodeye ack --store {shlex.quote(self.home)} --delivery {ident} after reading them. '
                   'Follow the GoodEye skill for each verdict. Claim unowned items before working on them. '
                   'A probe only needs acknowledgment. '
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
                db.execute("UPDATE deliveries SET status='queued',queued=?,receipt=?,error=NULL,lease_until=0 WHERE id=? AND status='pending' AND lease_token=?",
                           (time.time(), match.group(1), ident, lease_token))
        except (OSError, subprocess.SubprocessError, RuntimeError):
            # Retry failed/ambiguous sends; a crash after acceptance can duplicate a wake, never a verdict.
            delay = min(300, 2 ** min(row['attempts'] + 1, 8))
            with self.db() as db:
                db.execute('UPDATE deliveries SET error=?,next_attempt=?,lease_until=0 WHERE id=? AND status=\'pending\' AND lease_token=?',
                           ('Codex queue unavailable or acceptance unconfirmed; will retry', time.time() + delay, ident, lease_token))
        return True
