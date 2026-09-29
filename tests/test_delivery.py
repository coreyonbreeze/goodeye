"""Durable delivery tests; no real Codex calls or client assets."""
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goodeye_delivery import DeliveryStore


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeliveryStore(self.tmp.name)
        self.thread = str(uuid.uuid4())
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def subscribe(self, project='Mosaic', thread=None):
        return self.store.subscribe(project, thread or self.thread, sys.executable)

    def event(self, ident='decision1', project='Mosaic', feedback='smaller'):
        return {'decision_id': ident, 'project': project, 'id': 'film', 'version': 'v1',
                'verdict': 'changes', 'feedback': feedback}

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        thread = argv[argv.index('--thread') + 1]
        return subprocess.CompletedProcess(argv, 0, f'Queued message msg-123 for thread {thread}\n', '')

    def ready(self, events=None):
        self.store.ingest(events or [self.event()])
        self.store.make_batches(debounce=0)
        return self.store.status()[0]['last']['id']

    def test_new_subscription_skips_history_and_repeat_registration_preserves_cursor(self):
        self.store.ingest([self.event('old')])
        sub = self.subscribe()
        self.store.make_batches(0)
        self.assertIsNone(self.store.status()[0]['last'])
        self.store.ingest([self.event('new')])
        self.assertEqual(self.subscribe()['id'], sub['id'])
        self.store.make_batches(0)
        self.assertTrue(self.store.dispatch_one(self.runner))
        self.assertFalse(self.store.dispatch_one(self.runner))

    def test_projects_isolated_and_events_coalesced(self):
        self.subscribe()
        ident = self.ready([self.event('a'), self.event('b'), self.event('c', 'Other')])
        self.assertEqual([d['decision_id'] for d in self.store.inbox(ident)['decisions']], ['a', 'b'])
        self.store.ingest([self.event('a')])
        self.store.make_batches(0)
        self.store.dispatch_one(self.runner)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.status()[0]['counts'], {'queued': 1})

    def test_receipt_is_not_agent_ack_or_approval(self):
        self.subscribe()
        ident = self.ready()
        self.store.dispatch_one(self.runner)
        self.assertEqual(self.store.inbox(ident)['status'], 'queued')
        self.assertIsNone(self.store.inbox(ident)['acknowledged'])
        with self.assertRaises(ValueError):
            self.store.acknowledge(ident, str(uuid.uuid4()))
        self.store.acknowledge(ident, self.thread)
        self.store.acknowledge(ident, self.thread)
        self.assertEqual(self.store.inbox(ident)['status'], 'acknowledged')
        self.assertEqual(self.store.inbox(ident)['decisions'][0]['verdict'], 'changes')

    def test_restart_retains_queued_receipt_and_ack(self):
        self.subscribe()
        ident = self.ready()
        self.store.dispatch_one(self.runner)
        restarted = DeliveryStore(self.tmp.name)
        restarted.make_batches(0)
        self.assertFalse(restarted.dispatch_one(self.runner))
        restarted.acknowledge(ident, self.thread)
        self.assertEqual(DeliveryStore(self.tmp.name).inbox(ident)['status'], 'acknowledged')

    def test_failed_send_retries_without_marking_delivered(self):
        self.subscribe()
        ident = self.ready()
        def fail(*args, **kwargs):
            raise subprocess.TimeoutExpired('codex', 20)
        self.store.dispatch_one(fail)
        row = self.store.inbox(ident)
        self.assertEqual(row['status'], 'pending')
        self.assertGreater(row['next_attempt'], time.time())
        self.assertFalse(self.store.dispatch_one(self.runner))
        self.store.retry(ident)
        self.store.dispatch_one(self.runner)
        self.assertEqual(self.store.inbox(ident)['attempts'], 2)

    def test_zero_exit_without_exact_queue_receipt_is_failure(self):
        self.subscribe()
        ident = self.ready()
        self.store.dispatch_one(lambda *a, **k: subprocess.CompletedProcess(a, 0, 'ok', 'private diagnostic'))
        self.assertEqual(self.store.inbox(ident)['status'], 'pending')
        self.assertNotIn('private diagnostic', self.store.inbox(ident)['error'])

    def test_crashed_lease_is_recovered(self):
        self.subscribe()
        ident = self.ready()
        with self.store.db() as db:
            db.execute('UPDATE deliveries SET lease_until=? WHERE id=?', (time.time()+10, ident))
        self.assertFalse(self.store.dispatch_one(self.runner))
        with self.store.db() as db:
            db.execute('UPDATE deliveries SET lease_until=0 WHERE id=?', (ident,))
        self.assertTrue(DeliveryStore(self.tmp.name).dispatch_one(self.runner))

    def test_two_workers_cannot_send_same_lease(self):
        self.subscribe()
        self.ready()
        def slow(*a, **k):
            time.sleep(.1)
            return self.runner(*a, **k)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: DeliveryStore(self.tmp.name).dispatch_one(slow), range(2)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.calls), 1)

    def test_ack_during_queue_response_is_not_overwritten(self):
        self.subscribe()
        ident = self.ready()
        def ack(*a, **k):
            self.store.acknowledge(ident, self.thread)
            return self.runner(*a, **k)
        self.store.dispatch_one(ack)
        self.assertEqual(self.store.inbox(ident)['status'], 'acknowledged')

    def test_unsubscribe_stops_pending_and_same_name_is_explicit(self):
        self.subscribe()
        self.ready()
        # A second session is a second watcher; reusing a watcher's name from another thread is refused.
        with self.assertRaises(ValueError):
            self.store.subscribe('Mosaic', str(uuid.uuid4()), sys.executable, name='codex-' + self.thread[:8])
        self.store.unsubscribe('Mosaic')
        self.assertFalse(self.store.dispatch_one(self.runner))
        self.subscribe(thread=str(uuid.uuid4()))
        self.store.make_batches(0)
        self.assertFalse(self.store.dispatch_one(self.runner))

    def test_no_shell_feedback_or_privilege_override_in_queue_argv(self):
        self.subscribe()
        self.ready([self.event(feedback='$(touch /tmp/never); secret feedback')])
        self.store.dispatch_one(self.runner)
        argv, kwargs = self.calls[0]
        self.assertNotIn('secret feedback', ' '.join(argv))
        self.assertNotIn('shell', kwargs)
        self.assertNotIn('--model', argv)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', argv)
        self.assertIn('--store', argv[argv.index('--message')+1])

    def test_probe_is_not_a_review_decision(self):
        self.subscribe()
        [ident] = self.store.probe('Mosaic')
        payload = self.store.inbox(ident)['decisions'][0]
        self.assertEqual(payload['kind'], 'probe')
        self.assertNotIn('verdict', payload)
        self.assertFalse(Path(self.tmp.name, 'decisions.jsonl').exists())

    def test_validation_and_private_database(self):
        for thread in ('film editor', '', None):
            with self.assertRaises(ValueError):
                self.store.subscribe('Mosaic', thread, sys.executable)
        with self.assertRaises(ValueError):
            self.store.subscribe('Mosaic', self.thread, sys.executable, 'https://example.com')
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)

    def test_legacy_delivery_file_does_not_consume_subscription(self):
        self.subscribe()
        Path(self.tmp.name, 'delivered.json').write_text(json.dumps(['decision1']))
        self.ready()
        self.assertTrue(self.store.dispatch_one(self.runner))


class WatcherTest(unittest.TestCase):
    """Several sessions watch one project; claims route verdicts; handoff moves a session's work."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeliveryStore(self.tmp.name)
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, item, project='Mosaic'):
        self.n += 1
        self.store.ingest([{'decision_id': f'd{self.n}', 'project': project, 'id': item, 'version': 'v1',
                            'verdict': 'changes', 'feedback': 'x'}])
        self.store.make_batches(0)

    def got(self, name, project='Mosaic'):
        sub = self.store.watcher(project, name)
        return [d['id'] for box in self.store.pull(sub['id']) for d in box['decisions']]

    def test_unclaimed_goes_to_everyone_claimed_goes_to_owner(self):
        self.store.watch('Mosaic', 'video', 'pull', 's1')
        self.store.watch('Mosaic', 'brand', 'pull', 's2')
        self.decide('film-01')
        self.assertEqual(self.got('video'), ['film-01'])
        self.assertEqual(self.got('brand'), ['film-01'])
        self.store.claim('Mosaic', 'film-*', 'video', 'films are mine')
        self.store.claim('Mosaic', 'logo', 'brand')
        self.decide('film-02')
        self.decide('logo')
        self.assertEqual(self.got('video'), ['film-02'])
        self.assertEqual(self.got('brand'), ['logo'])

    def test_first_claim_wins_and_exact_beats_glob(self):
        self.store.watch('Mosaic', 'video', 'pull')
        self.store.watch('Mosaic', 'brand', 'pull')
        self.store.claim('Mosaic', 'film-01', 'video')
        with self.assertRaises(ValueError) as err:
            self.store.claim('Mosaic', 'film-01', 'brand')
        self.assertIn('claimed by video', str(err.exception))
        self.store.claim('Mosaic', 'film-*', 'brand')
        self.assertEqual(self.store.owner('Mosaic', 'film-01')[0], 'video')
        self.assertEqual(self.store.owner('Mosaic', 'film-02')[0], 'brand')
        with self.assertRaises(ValueError):          # a glob claim covers film-03 for brand
            self.store.claim('Mosaic', 'film-03', 'video')
        self.store.claim('Mosaic', 'film-03', 'video', force=True)
        self.assertEqual(self.store.owner('Mosaic', 'film-03')[0], 'video')
        with self.assertRaises(ValueError):          # only active watchers can claim
            self.store.claim('Mosaic', 'x', 'ghost')

    def test_owner_gone_means_broadcast(self):
        self.store.watch('Mosaic', 'video', 'pull')
        self.store.watch('Mosaic', 'brand', 'pull')
        self.store.claim('Mosaic', 'film-01', 'video')
        self.store.unwatch('Mosaic', 'video')
        self.decide('film-01')
        self.assertEqual(self.got('brand'), ['film-01'])

    def test_handoff_moves_claims_cursor_and_unacked_deliveries(self):
        old = self.store.watch('Mosaic', 'old', 'pull')
        self.store.claim('Mosaic', 'film-*', 'old')
        self.decide('film-01')
        self.store.pull(old['id'])                    # queued to the old session, never acknowledged
        out = self.store.handoff('Mosaic', 'new', runtime='pull')
        self.assertEqual(out['from'], 'old')
        self.assertEqual(len(out['moved_deliveries']), 1)
        self.assertIsNone(self.store.watcher('Mosaic', 'old'))
        self.assertEqual(self.store.owner('Mosaic', 'film-01')[0], 'new')
        self.assertEqual(self.got('new'), ['film-01'])   # redelivered once, to the new session
        self.decide('film-02')
        self.assertEqual(self.got('new'), ['film-02'])   # no replay of film-01
        with self.assertRaises(ValueError):              # several watchers: --from is required
            self.store.watch('Mosaic', 'third', 'pull')
            self.store.handoff('Mosaic', 'fourth')

    def test_probe_targets_one_watcher_and_ack_by_name(self):
        self.store.watch('Mosaic', 'video', 'pull')
        self.store.watch('Mosaic', 'brand', 'pull')
        self.assertEqual(len(self.store.probe('Mosaic')), 2)
        [ident] = self.store.probe('Mosaic', 'brand')
        with self.assertRaises(ValueError):
            self.store.acknowledge(ident, 'video')
        self.store.acknowledge(ident, 'brand')
        self.assertEqual(self.store.inbox(ident)['status'], 'acknowledged')

    def test_holds_and_names(self):
        with self.assertRaises(ValueError):
            self.store.hold('Mosaic', 'film-01', ' ')
        self.store.hold('Mosaic', 'film-01', 'paused by reviewer')
        self.assertEqual(self.store.holds('Mosaic')[0]['note'], 'paused by reviewer')
        self.store.unhold('Mosaic', 'film-01')
        self.assertEqual(self.store.holds(), [])
        for bad in ('', 'has space', 'x' * 65):
            with self.assertRaises(ValueError):
                self.store.watch('Mosaic', bad, 'pull')
        self.store.watch('Mosaic', 'video', 'pull', 's1')
        with self.assertRaises(ValueError):          # a pull watcher name cannot be taken over by a Codex push
            self.store.watch('Mosaic', 'video', 'codex', str(uuid.uuid4()), sys.executable)

    def test_upgrade_from_single_owner_schema(self):
        import sqlite3
        home = tempfile.mkdtemp(dir=self.tmp.name)
        db = sqlite3.connect(os.path.join(home, 'delivery.sqlite3'))
        db.executescript("""CREATE TABLE subscriptions (id TEXT PRIMARY KEY, project TEXT NOT NULL, thread TEXT NOT NULL,
            executable TEXT NOT NULL, remote TEXT, active INTEGER NOT NULL, start_after INTEGER NOT NULL, created REAL NOT NULL);
            CREATE UNIQUE INDEX one_owner ON subscriptions(project) WHERE active=1;""")
        thread = str(uuid.uuid4())
        db.execute("INSERT INTO subscriptions VALUES('s','Mosaic',?,?,NULL,1,0,0)", (thread, sys.executable))
        db.commit(); db.close()
        store = DeliveryStore(home)
        [row] = store.status()
        self.assertEqual((row['name'], row['runtime']), ('codex-' + thread[:8], 'codex'))
        store.watch('Mosaic', 'second', 'pull')
        self.assertEqual(len(store.status()), 2)

    def test_same_name_handoff_rebinds_role_to_new_session(self):
        old = self.store.watch('Mosaic', 'asset-agent', 'pull', 'session-1')
        self.store.claim('Mosaic', 'film-*', 'asset-agent')
        self.decide('film-01')
        self.store.pull(old['id'])
        out = self.store.handoff('Mosaic', 'asset-agent', 'asset-agent', 'pull', 'session-2')
        self.assertEqual(len(out['moved_deliveries']), 1)
        row = self.store.watcher('Mosaic', 'asset-agent')
        self.assertEqual(row['thread'], 'session-2')
        self.assertEqual(self.got('asset-agent'), ['film-01'])
        self.assertEqual(self.store.owner('Mosaic', 'film-01')[0], 'asset-agent')
        # A pull role can move to a Codex push thread, which re-validates the target.
        self.store.handoff('Mosaic', 'asset-agent', 'asset-agent', 'codex', str(uuid.uuid4()), sys.executable)
        self.assertEqual(self.store.watcher('Mosaic', 'asset-agent')['runtime'], 'codex')
