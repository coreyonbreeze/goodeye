"""Brand-direction conversations. Pure state transitions; the caller publishes under store_lock."""
import copy
import datetime
import hashlib
import json
import re
import uuid

FIELDS = ('strategy', 'logos', 'colors', 'typography', 'voice', 'references', 'visual_rules', 'export_rules')
PHASES = ('strategy', 'concept', 'system', 'revision', 'import')


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='microseconds')


def state(project):
    s = project.setdefault('direction', {'sources': '', 'requests': [], 'serial': 0})
    s.setdefault('source_revision', 0)
    return s


def changed(project):
    state(project)['serial'] += 1


def text(value, label, limit=20000, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f'{label} must be text, {1 if required else 0}–{limit} characters')
    return value.strip()


def request(project, body):
    ident = body.get('request_id')
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9-]{16,80}', ident):
        raise ValueError('a stable request_id is required')
    message = text(body.get('message'), 'request')
    scope = body.get('scope', 'all')
    if scope not in ('all',) + FIELDS:
        raise ValueError('unknown direction scope')
    target = body.get('agent') or None
    if target is not None:
        text(target, 'agent', 64)
    fingerprint = hashlib.sha256(json.dumps([message, scope, target]).encode()).hexdigest()
    s = state(project)
    prior = next((r for r in s['requests'] if r['id'] == ident), None)
    if prior:
        if prior['fingerprint'] != fingerprint:
            raise ValueError('request_id already used for another request')
        return prior
    for r in s['requests']:
        if r['status'] not in ('accepted', 'superseded'):
            r['status'] = 'superseded'
    r = {'id': ident, 'fingerprint': fingerprint, 'message': message, 'scope': scope, 'agent': target,
         'base_revision': project['revision'], 'baseline': copy.deepcopy(project['profile']),
         'sources': s['sources'], 'status': 'waiting', 'created': now(), 'updates': [], 'proposals': [],
         'delivery': None}
    s['requests'].append(r)
    changed(project)
    return r


def current(project, ident, editable=True):
    requests = state(project)['requests']
    r = next((r for r in requests if r['id'] == ident), None)
    if not r:
        raise ValueError('unknown direction request in this project')
    if editable and (r is not requests[-1] or r['status'] in ('accepted', 'superseded')):
        raise ValueError('a newer request supersedes this work; read the current direction conversation')
    if editable and r['base_revision'] != project['revision']:
        raise ValueError('approved direction changed; send a new request before proposing changes')
    return r


def update(project, ident, status, message):
    if status not in ('working', 'needs_input'):
        raise ValueError('status must be working or needs_input')
    r = current(project, ident)
    r['updates'].append({'message': text(message, 'agent message'), 'at': now()})
    r['status'] = status
    changed(project)
    return r


def propose(project, ident, proposal):
    r = current(project, ident)
    if not isinstance(proposal, dict):
        raise ValueError('proposal must be an object')
    phase = proposal.get('phase')
    if phase not in PHASES:
        raise ValueError('phase must be strategy, concept, system, revision, or import')
    if phase in ('concept', 'system') and not project['profile'].get('strategy', '').strip():
        raise ValueError('approve the strategy, or import the established strategy, before visual exploration and the system')
    patch = proposal.get('profile')
    if not isinstance(patch, dict) or not patch or set(patch) - set(FIELDS):
        raise ValueError('proposal needs supported profile fields')
    if r['scope'] != 'all' and set(patch) != {r['scope']}:
        raise ValueError('a scoped request can change only its named field')
    if phase == 'strategy' and set(patch) != {'strategy'}:
        raise ValueError('strategy proposals change only strategy')
    patch = {k: text(v, k, required=False) for k, v in patch.items()}
    evidence = proposal.get('evidence')
    if not isinstance(evidence, list) or not evidence or len(evidence) > 30:
        raise ValueError('cite 1–30 sources or explicit user decisions')
    evidence = [text(v, 'evidence', 3000) for v in evidence]
    artifacts = proposal.get('artifacts', [])
    if not isinstance(artifacts, list) or len(artifacts) > 12:
        raise ValueError('artifacts must be a list of at most 12 submitted asset references')
    if phase == 'concept' and not artifacts:
        raise ValueError('visual concepts need submitted artifacts to review')
    ident_p = proposal.get('id') or uuid.uuid4().hex
    if not isinstance(ident_p, str) or not re.fullmatch(r'[A-Za-z0-9-]{16,80}', ident_p):
        raise ValueError('invalid proposal id')
    p = {'id': ident_p, 'phase': phase, 'title': text(proposal.get('title'), 'title', 160),
         'rationale': text(proposal.get('rationale'), 'rationale'), 'evidence': evidence,
         'profile': patch, 'artifacts': copy.deepcopy(artifacts),
         'production_notes': text(proposal.get('production_notes', ''), 'production_notes', required=False)}
    prior = next((p0 for p0 in r['proposals'] if p0['id'] == ident_p), None)
    if prior:
        if {k: v for k, v in prior.items() if k != 'created'} != p:
            raise ValueError('proposal id already used; publish a new proposal')
        return prior
    p['created'] = now()
    r['proposals'].append(p)
    r['status'] = 'proposed'
    changed(project)
    return p


def accept(project, ident, proposal_id):
    r = current(project, ident, editable=False)
    if r.get('accepted') == proposal_id:
        return r
    r = current(project, ident)
    p = next((p for p in r['proposals'] if p['id'] == proposal_id), None)
    if not p:
        raise ValueError('unknown proposal')
    project['profile'] = {**project['profile'], **p['profile']}
    project['revision'] += 1
    project['updated'] = now()
    r.update(status='accepted', accepted=proposal_id, accepted_revision=project['revision'])
    changed(project)
    # Continue the same conversation after the strategic/visual decision; no terminal restart.
    if p['phase'] in ('strategy', 'concept') and r['scope'] == 'all':
        instruction = ('Explore distinct visual directions using the approved strategy. Inspect references and use an image-generation tool for raster studies.'
                       if p['phase'] == 'strategy' else
                       'Build the coherent brand system from the selected concept. Verify it in real applications and propose the final guidelines.')
        request(project, {'request_id': uuid.uuid4().hex, 'scope': 'all', 'agent': r['agent'],
                          'message': f'Approved “{p["title"]}” ({p["phase"]}). {instruction} Preserve the decisions already approved in this conversation.'})
    return r
