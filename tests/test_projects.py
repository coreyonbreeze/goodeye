"""Project isolation, legacy compatibility, and immutable direction snapshots."""
import json
import os
import shutil
import unittest
import test_goodeye


class ProjectsTest(unittest.TestCase):
    setUp = test_goodeye.GoodEyeTest.setUp
    tearDown = test_goodeye.GoodEyeTest.tearDown
    cli = test_goodeye.GoodEyeTest.cli
    file = test_goodeye.GoodEyeTest.file
    json = test_goodeye.GoodEyeTest.json
    post = test_goodeye.GoodEyeTest.post
    get = test_goodeye.GoodEyeTest.get

    def state(self):
        return json.loads(self.get('/api/items')[1])

    def submit(self, project, asset_id='hero', revision=False, *args):
        return self.cli('submit', self.png, '--id', asset_id, '--project', project,
                        '--reasoning', self.reason2 if revision else self.reason, *args)

    def test_duplicate_ids_have_separate_files_decisions_slots_and_exports(self):
        for project in ('Mosaic', 'Castle Heist'):
            self.submit(project, 'hero', False, '--slot', 'hero', '--slot-label', project)
            self.submit(project, 'alternate', False, '--slot', 'hero')
        items = self.state()['items']
        self.assertEqual(len({i['key'] for i in items}), 4)
        self.assertEqual(len({i['versions'][0]['dir'] for i in items}), 4)
        for i in items:
            self.assertEqual(i['slot']['label'], i['project'])
            v = i['versions'][0]
            self.assertEqual(self.get('/files/' + v['dir'] + '/' + v['file'])[0], 200)
        body = dict(id='hero', version='v1', verdict='approved')
        self.assertEqual(self.post(body)[0], 409)
        ambiguous = self.cli('submit', self.png, '--id', 'hero', '--reasoning', self.reason2, ok=False)
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn('several projects', ambiguous.stderr)
        self.assertEqual(self.post(dict(body, project='Mosaic'))[0], 200)
        state = {(i['project'], i['id']): i for i in self.state()['items']}
        self.assertEqual(state['Mosaic', 'alternate']['status'], 'not_chosen')
        self.assertEqual(state['Castle Heist', 'alternate']['status'], 'pending')
        self.assertEqual(state['Castle Heist', 'hero']['status'], 'pending')
        self.assertEqual(self.post(dict(body, project='Castle Heist'))[0], 200)
        out = os.path.join(self.tmp.name, 'exports')
        self.cli('export', out)
        with open(os.path.join(out, 'manifest.json')) as f:
            manifest = json.load(f)
        self.assertEqual(len(manifest), 2)
        self.assertEqual(len({m['file'] for m in manifest}), 2)
        for m in manifest:
            self.assertTrue(os.path.isfile(os.path.join(out, m['file'])))
        self.submit('Mosaic', revision=True)
        state = {(i['project'], i['id']): i for i in self.state()['items']}
        self.assertEqual(len(state['Mosaic', 'hero']['versions']), 2)
        self.assertEqual(len(state['Castle Heist', 'hero']['versions']), 1)

    def test_profiles_are_versioned_snapshots_with_conflict_protection(self):
        profile = dict(name='Mosaic', expected_revision=0, collections=['Brand', 'Website'],
                       profile={'colors': 'Forest #326653', 'visual_rules': 'Quiet layouts'})
        status, saved = self.post(profile, path='/api/project')
        self.assertEqual(status, 200)
        self.assertEqual(saved['revision'], 1)
        self.submit('Mosaic', 'hero', False, '--collection', 'Website')
        profile['expected_revision'] = 1
        profile['profile']['colors'] = 'Ink #112233'
        self.assertEqual(self.post(profile, path='/api/project')[0], 200)
        self.assertEqual(self.post(profile, path='/api/project')[0], 409)
        self.submit('Mosaic', revision=True)
        it = self.state()['items'][0]
        self.assertEqual([v['profile_revision'] for v in it['versions']], [1, 2])
        self.assertEqual(it['versions'][0]['profile_snapshot']['colors'], 'Forest #326653')
        self.assertEqual(it['versions'][1]['profile_snapshot']['colors'], 'Ink #112233')
        self.assertEqual(it['collection'], 'Website')
        brief = self.cli('brief', '--project', 'Mosaic').stdout
        self.assertIn('Ink #112233', brief)
        self.assertIn('Website', brief)
        self.assertIn('revision 2', brief)
        for body in (dict(name='Mosaic', expected_revision=2, accent='red'),
                     dict(name='Mosaic', expected_revision=2, profile={'colors': []}),
                     dict(name='Mosaic', expected_revision=2, collections=[None])):
            self.assertEqual(self.post(body, path='/api/project')[0], 409)
        self.assertEqual(self.post(profile, path='/api/project', headers={'Origin': 'https://evil.example'})[0], 403)

    def test_collection_assignment_is_scoped_and_preserves_submission(self):
        for project in ('Mosaic', 'Castle Heist'):
            self.submit(project)
        self.cli('organize', 'hero', '--project', 'Mosaic', '--collection', 'Brand')
        state = {i['project']: i for i in self.state()['items']}
        self.assertEqual(state['Mosaic']['collection'], 'Brand')
        self.assertEqual(state['Castle Heist']['collection'], '')
        self.assertEqual(state['Mosaic']['versions'][0]['collection'], '')
        self.assertIn('Brand', next(p for p in self.state()['projects'] if p['name'] == 'Mosaic')['collections'])
        self.assertNotEqual(self.cli('slot', 'test', 'hero', ok=False).returncode, 0)
        self.cli('slot', 'test', 'hero', '--project', 'Mosaic')
        self.assertIsNone(next(i for i in self.state()['items'] if i['project'] == 'Castle Heist')['slot'])
        self.submit('Mosaic', revision=True)
        self.assertEqual(next(i for i in self.state()['items'] if i['project'] == 'Mosaic')['collection'], 'Brand')
        self.submit('Mosaic', 'hero', True, '--collection', 'Website')
        self.assertEqual(next(i for i in self.state()['items'] if i['project'] == 'Mosaic')['collection'], 'Website')

    def test_legacy_directories_and_links_survive_scoped_revision(self):
        self.submit('Mosaic')
        v = self.state()['items'][0]['versions'][0]
        base = os.path.join(self.env['GOODEYE_HOME'], 'assets')
        legacy = os.path.join(base, 'hero', 'v1')
        os.makedirs(os.path.dirname(legacy))
        shutil.move(os.path.join(base, v['dir']), legacy)
        v['dir'] = 'hero/v1'
        for key in ('profile_revision', 'profile_snapshot', 'decisions', 'status'):
            v.pop(key, None)
        with open(os.path.join(legacy, 'meta.json'), 'w') as f:
            json.dump(v, f)
        self.assertEqual(self.post(dict(id='hero', version='v1', verdict='changes', feedback='Simplify'))[0], 200)
        self.submit('Mosaic', revision=True)
        self.submit('Castle Heist')
        items = {i['project']: i for i in self.state()['items']}
        self.assertEqual(items['Mosaic']['versions'][0]['status'], 'changes')
        self.assertEqual(items['Castle Heist']['versions'][0]['status'], 'pending')
        self.assertEqual(len(items['Mosaic']['versions']), 2)
        self.assertEqual(self.get('/files/hero/v1/a.png')[0], 200)
