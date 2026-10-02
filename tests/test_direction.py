"""Durable direction workflow, scoped delivery, and approval boundaries."""
import json
import os
import unittest
import uuid
import test_goodeye


class DirectionTest(unittest.TestCase):
    setUp = test_goodeye.GoodEyeTest.setUp
    tearDown = test_goodeye.GoodEyeTest.tearDown
    cli = test_goodeye.GoodEyeTest.cli
    file = test_goodeye.GoodEyeTest.file
    json = test_goodeye.GoodEyeTest.json
    post = test_goodeye.GoodEyeTest.post
    get = test_goodeye.GoodEyeTest.get

    def create(self, name='Mosaic'):
        self.assertEqual(self.post({'name':name, 'expected_revision':0}, path='/api/project')[0], 200)

    def state(self, name='Mosaic'):
        return json.loads(self.cli('direction', 'show', '--project', name).stdout)

    def request(self, message='Find our direction', scope='all', agent=None, ident=None):
        body = {'project':'Mosaic', 'action':'request', 'message':message, 'scope':scope,
                'agent':agent, 'request_id':ident or uuid.uuid4().hex}
        status, response = self.post(body, path='/api/direction')
        self.assertEqual(status, 200, response)
        return body, response['requests'][-1]

    def propose(self, request, phase='strategy', patch=None, **kwargs):
        proposal = {'id':uuid.uuid4().hex, 'phase':phase, 'title':'A specific direction',
                    'rationale':'Grounded in the product and its source brief.',
                    'evidence':['Founder brief: serve neighborhood shop owners.'],
                    'profile':patch or {'strategy':'Useful, calm, and grounded in real shop work.'}, **kwargs}
        file = self.json('proposal.json', proposal)
        result = self.cli('direction','propose','--project','Mosaic','--request',request['id'],'--file',file,ok=False)
        return proposal, result

    def accept(self, request, proposal):
        return self.post({'project':'Mosaic','action':'accept','request':request['id'],'proposal':proposal['id']},path='/api/direction')

    def test_waiting_request_resumes_after_join_and_does_not_change_profile(self):
        self.create()
        body, request = self.request()
        self.assertEqual(request['status'], 'waiting')
        self.assertEqual(self.post(body,path='/api/direction')[0], 200)
        self.assertEqual(len(self.state()['requests']), 1)
        changed = dict(body,message='Different request')
        self.assertEqual(self.post(changed,path='/api/direction')[0], 409)
        self.cli('direction','join','--project','Mosaic','--as','brand','--runtime','pull')
        state = self.state()
        self.assertEqual(state['requests'][-1]['status'],'queued')
        delivery = state['requests'][-1]['delivery']
        box = json.JSONDecoder().raw_decode(self.cli('inbox','--delivery',delivery).stdout)[0]
        self.assertEqual(box['decisions'][0]['kind'],'direction')
        self.assertEqual(box['decisions'][0]['request'],request['id'])
        self.assertEqual(state['revision'], 1)
        self.assertFalse(any(state['profile'].values()))
        # Both replay and join keep one stable event/delivery.
        self.cli('direction','join','--project','Mosaic','--as','brand','--runtime','pull')
        self.assertEqual(self.state()['requests'][-1]['delivery'],delivery)
        output = self.cli('wait','--project','Mosaic','--as','brand','--timeout','5').stdout
        self.assertIn('BRAND DIRECTION',output)
        self.assertNotIn('VERDICT',output)

    def test_strategy_acceptance_continues_and_retries_do_not_repeat(self):
        self.create()
        _, r = self.request()
        p, result = self.propose(r)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.state()['revision'],1)
        self.assertNotEqual(self.propose(r,'system',{'colors':'Unresearched palette'})[1].returncode,0)
        status, accepted = self.accept(r,p)
        self.assertEqual(status,200,accepted)
        self.assertEqual(accepted['revision'],2)
        self.assertEqual(len(accepted['requests']),2)
        self.assertEqual(accepted['requests'][0]['status'],'accepted')
        self.assertIn('Explore distinct visual directions',accepted['requests'][-1]['message'])
        self.assertEqual(self.accept(r,p)[0],200)
        self.assertEqual(len(self.state()['requests']),2)
        self.assertEqual(self.state()['revision'],2)
        # Proposals cannot silently change current direction or bypass scope.
        _, r2 = self.request('Only change voice','voice')
        _, bad = self.propose(r2, 'revision', {'colors':'Red'})
        self.assertNotEqual(bad.returncode,0)
        p2, good = self.propose(r2, 'revision', {'voice':'Direct, familiar words.'})
        self.assertEqual(good.returncode,0,good.stderr)
        self.assertEqual(self.accept(r2,p2)[0],200)
        self.assertEqual(self.state()['profile']['strategy'],p['profile']['strategy'])
        self.assertEqual(self.state()['profile']['voice'],'Direct, familiar words.')

    def test_newer_steering_and_profile_edit_reject_stale_work(self):
        self.create()
        _, r = self.request()
        p, result = self.propose(r)
        self.assertEqual(result.returncode,0,result.stderr)
        _, newer = self.request('Preserve the original mark')
        self.assertEqual(self.accept(r,p)[0],409)
        self.assertNotEqual(self.propose(r)[1].returncode,0)
        p2, result = self.propose(newer)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.post({'name':'Mosaic','expected_revision':1,'profile':{'voice':'An intentional manual update'}},path='/api/project')[0],200)
        self.assertEqual(self.accept(newer,p2)[0],409)

    def test_artifacts_are_real_scoped_and_carry_provenance(self):
        self.create()
        self.create('Castle Heist')
        self.cli('submit',self.png,'--project','Castle Heist','--id','study','--reasoning',self.reason)
        self.assertEqual(self.post({'name':'Mosaic','expected_revision':1,'profile':{'strategy':'Test strategy approved for this fixture'}},path='/api/project')[0],200)
        _, r = self.request()
        artifact = {'id':'study','version':'v1','origin':'generated','tool':'Test image generator','prompt':'Synthetic test prompt','provenance':'Fixture only'}
        _, result = self.propose(r,'concept',{'colors':'Proposed palette'},artifacts=[artifact])
        self.assertNotEqual(result.returncode,0)
        self.cli('submit',self.png,'--project','Mosaic','--id','study','--reasoning',self.reason)
        _, result = self.propose(r,'concept',{'colors':'Proposed palette'},artifacts=[dict(artifact,prompt='')])
        self.assertNotEqual(result.returncode,0)
        p,result = self.propose(r,'concept',{'colors':'Proposed palette'},artifacts=[artifact])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.accept(r,p)[0],200)
        self.assertIn('Build the coherent brand system',self.state()['requests'][-1]['message'])

    def test_direction_is_delivered_to_selected_agent_only(self):
        self.create()
        for name in ('brand','video'):
            self.cli('watch','--project','Mosaic','--as',name,'--runtime','pull')
        _, r = self.request(agent='brand')
        import time
        for _ in range(40):
            current = self.state()['requests'][-1]
            if current['delivery']:
                break
            time.sleep(.1)
        self.assertTrue(current['delivery'])
        state = json.loads(self.cli('watchers','--project','Mosaic').stdout)
        video = next(s for s in state if s['name']=='video')
        self.assertEqual(video['counts'],{})
        # An unavailable target cannot accept an unsaved new request.
        body={'project':'Mosaic','action':'request','request_id':uuid.uuid4().hex,'message':'New work','agent':'missing'}
        self.assertEqual(self.post(body,path='/api/direction')[0],409)
        self.assertEqual(self.post(body,path='/api/direction',headers={'Origin':'https://evil.example'})[0],403)

    def test_sources_do_not_expose_files_and_keep_conflict_protection(self):
        self.create()
        body={'project':'Mosaic','action':'sources','sources':'/private/source-bible.md','expected_source_revision':0}
        self.assertEqual(self.post(body,path='/api/direction')[0],200)
        self.assertEqual(self.post(body,path='/api/direction')[0],409)
        state=self.state()
        self.assertEqual(state['sources'],'/private/source-bible.md')
        self.assertIn('skills/goodeye-brand/SKILL.md',state['prompt'])
        self.assertNotIn('direction',json.loads(self.get('/api/items')[1])['projects'][0])
        self.assertEqual(self.get('/api/direction?project=Mosaic')[0],200)
        self.assertEqual(self.get('/api/direction?project=missing')[0],404)

    def test_same_request_id_in_two_projects_routes_independently(self):
        import time
        ident = uuid.uuid4().hex
        for project in ('Mosaic', 'Castle Heist'):
            self.create(project)
            self.cli('watch','--project',project,'--as','brand','--runtime','pull')
            self.assertEqual(self.post({'project':project,'action':'request','request_id':ident,'message':'Study this project','agent':'brand'},path='/api/direction')[0],200)
        for _ in range(40):
            a, b = self.state()['requests'][-1], self.state('Castle Heist')['requests'][-1]
            if a['delivery'] and b['delivery']:
                break
            time.sleep(.1)
        self.assertTrue(a['delivery'])
        self.assertTrue(b['delivery'])
        self.assertNotEqual(a['delivery'], b['delivery'])
        box = json.JSONDecoder().raw_decode(self.cli('inbox','--delivery',b['delivery']).stdout)[0]
        self.assertEqual(box['project'],'Castle Heist')

    def test_pending_target_follows_handoff(self):
        from goodeye_delivery import DeliveryStore
        self.create()
        store = DeliveryStore(self.env['GOODEYE_HOME'])
        store.watch('Mosaic','old','pull')
        store.claim('Mosaic','brand-direction','old')
        store.handoff('Mosaic','new','old','pull',None)
        delivery = store.direct_event('Mosaic','old',{'decision_id':uuid.uuid4().hex,'request':uuid.uuid4().hex})
        self.assertTrue(delivery)
        self.assertEqual(store.inbox(delivery)['watcher'],'new')
        # The event is targeted at the successor, so another watcher cannot receive it.
        store.watch('Mosaic','other','pull')
        store.make_batches(debounce=0)
        self.assertEqual(next(w for w in store.status('Mosaic') if w['name']=='other')['counts'],{})

    def test_background_turns_do_not_invalidate_source_editor(self):
        self.create()
        self.request()
        status, result = self.post({'project':'Mosaic','action':'sources','sources':'/repo/bible.md','expected_source_revision':0},path='/api/direction')
        self.assertEqual(status,200,result)
        self.assertEqual(result['source_revision'],1)
