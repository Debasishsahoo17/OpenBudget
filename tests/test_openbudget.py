"""End-to-end tests using a real HTTP server and disposable SQLite database."""
import csv
import http.cookiejar
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from openbudget.app import OpenBudgetServer, build_csv, csv_safe
from openbudget.db import connect, initialize
from openbudget.seed import seed


class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'test.sqlite3')
        with connect(self.db_path) as db:
            initialize(db)
            self.assertTrue(seed(db))
            self.assertFalse(seed(db))  # idempotent
        self.server = OpenBudgetServer(('127.0.0.1', 0), self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.cookies = http.cookiejar.CookieJar()
        self.client = build_opener(HTTPCookieProcessor(self.cookies))
        self.csrf = self.request('GET', '/api/session')[1]['csrf']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def request(self, method, path, body=None, expect=None):
        headers = {}
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
            headers['X-CSRF-Token'] = self.csrf if hasattr(self, 'csrf') else ''
        request = Request(self.url + path,
                          data=json.dumps(body if body is not None else {}).encode() if method == 'POST' else None,
                          headers=headers, method=method)
        try:
            with self.client.open(request) as response:
                status = response.status
                content_type = response.headers.get('Content-Type','')
                raw = response.read()
        except HTTPError as err:
            status = err.code
            content_type = err.headers.get('Content-Type', '')
            raw = err.read()
        result = json.loads(raw) if 'application/json' in content_type else raw
        if expect is not None:
            self.assertEqual(status, expect, result)
        return status, result

    def login(self, email='mira@demo.local', password='DemoPass123!'):
        _, result = self.request('POST', '/api/auth/login', {'email': email, 'password': password}, 200)
        self.csrf = result['csrf']
        return result

    def test_public_pages_health_and_exports(self):
        status, html = self.request('GET', '/', expect=200)
        self.assertIn(b'OpenBudget', html)
        self.request('GET', '/styles.css', expect=200)
        self.request('GET', '/app.js', expect=200)
        self.request('GET', '/favicon.svg', expect=200)
        self.request('GET', '/api/health', expect=200)
        _, b = self.request('GET', '/api/bootstrap', expect=200)
        self.assertEqual(len(b['cycles']), 3)
        self.assertEqual(b['stats']['cycles'], 3)
        _, d = self.request('GET', '/api/cycles/impact-2025', expect=200)
        self.assertEqual(d['cycle']['allocated'], 9_200_000)
        self.assertLessEqual(d['cycle']['allocated'], d['cycle']['total_budget'])
        _, out = self.request('GET', '/api/cycles/impact-2025/export.csv', expect=200)
        self.assertNotIn(b'@demo.local', out)
        sheet = list(csv.DictReader(io.StringIO(out.decode('utf-8-sig'))))
        self.assertEqual(len(sheet), 5)
        self.assertEqual(sum(int(r['allocated_inr']) for r in sheet), 9_200_000)
        _, data = self.request('GET', '/api/cycles/impact-2025/export.json', expect=200)
        self.assertIn('privacy_note', data)
        self.assertNotIn('user_id', json.dumps(data))

    def test_csrf_auth_and_moderator_permissions(self):
        self.request('POST', '/api/cycles/neighborhood-2027/proposals', {}, expect=401)
        self.login()
        self.request('GET', '/api/admin/queue', expect=403)
        self.request('POST', '/api/admin/cycles/1/transition', {'action': 'open_voting'}, expect=403)
        # Wrong CSRF rejected even with a valid authenticated cookie.
        old = self.csrf
        self.csrf = 'malformed'
        self.request('POST', '/api/auth/logout', {}, expect=403)
        self.csrf = old
        self.request('POST', '/api/auth/logout', {}, expect=200)
        _, info = self.request('GET', '/api/session', expect=200)
        self.assertIsNone(info['user'])

    def test_register_submit_moderate_and_visibility(self):
        status, response = self.request('POST', '/api/auth/register',
            {'name': 'Jordan Citizen', 'email': 'jordan@example.test', 'password': 'A-secure-demo-password'}, expect=201)
        self.csrf = response['csrf']
        self.assertEqual(response['user']['role'], 'citizen')
        status, proposal = self.request('POST', '/api/cycles/neighborhood-2027/proposals',
            {'title': 'Inclusive community library',
             'description': 'Create a public reading space with accessible books, seating, ramps, and after-school workshops.',
             'category': 'Education', 'district': 'South Ward', 'requested_amount': 750_000}, expect=201)
        ident = proposal['id']
        self.request('GET', f'/api/proposals/{ident}', expect=200)  # owner access
        _, mine = self.request('GET', '/api/me/proposals', expect=200)
        self.assertIn(ident, [x['id'] for x in mine['proposals']])
        self.request('POST', '/api/admin/proposals/%s/moderate' % ident, {'decision':'approved'}, expect=403)
        self.login('moderator@demo.local')
        _, queue = self.request('GET', '/api/admin/queue', expect=200)
        self.assertIn(ident, [x['id'] for x in queue['items']])
        self.request('POST', f'/api/admin/proposals/{ident}/moderate',
                     {'decision':'approved','note':'Public community benefit confirmed'}, expect=200)
        self.request('POST', f'/api/admin/proposals/{ident}/moderate', {'decision':'rejected'}, expect=409)
        _, visible = self.request('GET', '/api/cycles/neighborhood-2027', expect=200)
        self.assertIn(ident, [x['id'] for x in visible['proposals']])
        self.request('GET', '/api/admin/audit', expect=200)

    def test_vote_change_and_allocation_never_overspends(self):
        self.login()
        _, c = self.request('GET', '/api/cycles/community-2026', expect=200)
        approved = [p for p in c['proposals'] if p['status'] == 'approved']
        first, second = approved[0]['id'], approved[1]['id']
        self.request('POST', f'/api/proposals/{first}/vote', {}, expect=200)
        self.request('POST', f'/api/proposals/{second}/vote', {}, expect=200)
        _, d = self.request('GET', '/api/cycles/community-2026', expect=200)
        self.assertEqual(d['my_vote'], second)
        with connect(self.db_path) as db:
            count = db.execute('SELECT COUNT(*) FROM votes WHERE cycle_id=? AND user_id=?', (d['cycle']['id'], 1)).fetchone()[0]
            self.assertEqual(count, 1)
        self.login('moderator@demo.local')
        self.request('POST', f'/api/admin/cycles/{d["cycle"]["id"]}/transition', {'action': 'close_voting'}, expect=200)
        self.request('POST', f'/api/proposals/{first}/vote', {}, expect=409)
        self.request('POST', f'/api/admin/cycles/{d["cycle"]["id"]}/transition', {'action': 'allocate'}, expect=200)
        self.request('POST', f'/api/admin/cycles/{d["cycle"]["id"]}/transition', {'action': 'allocate'}, expect=409)
        _, after = self.request('GET', '/api/cycles/community-2026', expect=200)
        self.assertEqual(after['cycle']['stage'], 'allocated')
        self.assertLessEqual(after['cycle']['allocated'], after['cycle']['total_budget'])
        self.assertEqual(after['cycle']['allocated'], sum(p['allocated_amount'] for p in after['proposals']))
        self.assertTrue(any(p['status']=='funded' for p in after['proposals']))
        self.assertTrue(all(p['status'] in ('funded','unfunded') for p in after['proposals']))
        self.request('GET', '/api/transparency?cycle=community-2026', expect=200)

    def test_cycle_transition_and_submission_validation(self):
        self.login()
        self.request('POST', '/api/cycles/community-2026/proposals',
            {'title':'Cannot submit here','description':'This cycle is voting and cannot accept a new submission.',
             'category':'Culture','district':'West Ward','requested_amount':50000}, expect=409)
        self.request('POST', '/api/cycles/neighborhood-2027/proposals',
            {'title':'Too short','description':'short','category':'Culture','district':'West Ward','requested_amount':50000}, expect=400)
        self.request('POST', '/api/cycles/neighborhood-2027/proposals',
            {'title':'Super expensive new park','description':'A large park with paths, safe playgrounds and toilets for local residents.',
             'category':'Public spaces','district':'West Ward','requested_amount':999999999}, expect=400)
        self.login('moderator@demo.local')
        self.request('POST', '/api/admin/cycles/1/transition', {'action':'allocate'}, expect=409)
        self.request('POST', '/api/admin/cycles/1/transition', {'action':'open_voting'}, expect=200)
        self.request('POST', '/api/admin/proposals/3/moderate', {'decision':'approved'}, expect=409)
        self.request('POST', '/api/admin/cycles/1/transition', {'action':'close_voting'}, expect=200)
        self.request('POST', '/api/admin/cycles/1/transition', {'action':'allocate'}, expect=200)
        _, data = self.request('GET', '/api/cycles/1', expect=200)
        self.assertEqual(data['cycle']['allocated'], 0)  # no ballots cast

    def test_moderator_can_create_a_cycle(self):
        self.login()
        payload = {"name": "Riverside Improvements", "description": "Resident-led plans for safer riverside public spaces.",
                   "total_budget": 5_000_000, "year": 2028}
        self.request('POST', '/api/admin/cycles', payload, expect=403)
        self.login('moderator@demo.local')
        self.request('POST', '/api/admin/cycles', {**payload, 'total_budget': -1}, expect=400)
        _, created = self.request('POST', '/api/admin/cycles', payload, expect=201)
        self.assertEqual(created['slug'], 'riverside-improvements-2028')
        self.assertEqual(created['stage'], 'submissions')
        self.request('GET', '/api/cycles/riverside-improvements-2028', expect=200)
        _, next_cycle = self.request('POST', '/api/admin/cycles', payload, expect=201)
        self.assertEqual(next_cycle['slug'], 'riverside-improvements-2028-2')
        _, boot = self.request('GET', '/api/bootstrap', expect=200)
        self.assertEqual(boot['stats']['cycles'], 5)

    def test_csv_injection_defense_and_no_public_pending(self):
        self.assertEqual(csv_safe('=HYPERLINK("bad")')[0], "'")
        self.assertEqual(csv_safe('+SUM(A1:A3)')[0], "'")
        self.assertEqual(csv_safe('@DDE')[0], "'")
        _, data = self.request('GET', '/api/cycles/neighborhood-2027/export.json', expect=200)
        self.assertEqual(len(data['proposals']), 2)  # private pending proposals excluded
        self.assertNotIn('Shade structures for neighborhood playgrounds', json.dumps(data))
        self.request('GET', '/api/secret.txt', expect=404)
        self.request('GET', '/../../openbudget/seed.py', expect=404)


if __name__ == '__main__':
    unittest.main()
