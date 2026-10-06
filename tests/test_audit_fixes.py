"""Regression coverage for credential handling and safe output publication."""
import asyncio
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import output_writer as ow
import diff_runs
import puppeteer_scraper as puppeteer
from proxy_pool import Proxy, ProxyParseError, parse_proxy_line


class AuditSecurity(unittest.TestCase):
    def test_invalid_proxy_never_echoes_credentials(self):
        for value in ('http://fake:fake:password@127.0.0.1:8080', '127.0.0.1:8080:fake:fake:password'):
            with self.assertRaises(ProxyParseError) as caught:
                parse_proxy_line(value)
            self.assertNotIn('fake', str(caught.exception))
            self.assertNotIn('password@', str(caught.exception))

    def test_auth_only_for_matching_proxy_and_only_once(self):
        class Client:
            def __init__(self): self.events, self.calls = {}, []
            def on(self, name, handler): self.events[name] = handler
            async def send(self, name, payload): self.calls.append((name, payload))
        class Page:
            def __init__(self): self._client = Client()
        async def run():
            page = Page()
            await puppeteer._authenticate_if_needed(page, Proxy('127.0.0.1', 8080, 'fake', 'fake_password'))
            challenges = [
                ('server', 'Server', 'http://127.0.0.1:8080'),
                ('other', 'Proxy', 'http://127.0.0.2:8080'),
                ('bad-port', 'Proxy', 'http://127.0.0.1:broken'),
                ('missing', None, 'http://127.0.0.1:8080'),
                ('proxy', 'Proxy', 'http://127.0.0.1:8080'),
                ('proxy', 'Proxy', 'http://127.0.0.1:8080'),
            ]
            for request, source, origin in challenges:
                page._client.events['Fetch.authRequired']({'requestId': request,
                    'authChallenge': {'source': source, 'origin': origin}})
                await asyncio.sleep(0)
            return [data['authChallengeResponse'] for name, data in page._client.calls if name == 'Fetch.continueWithAuth']
        replies = asyncio.run(run())
        self.assertEqual([r['response'] for r in replies], ['CancelAuth'] * 4 + ['ProvideCredentials', 'CancelAuth'])
        self.assertEqual(replies[4]['password'], 'fake_password')
        self.assertTrue(all('password' not in r for r in replies if r['response'] == 'CancelAuth'))


class AuditOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def row(self, title):
        return ow.Product(sku='fake', source='facebook.com', category='test', title=title,
            brand=None, price=None, currency=None, price_source=None, product_url=None,
            image_url=None, scraped_at='2026-10-06T00:00:00Z')

    def test_failed_json_write_preserves_previous_file(self):
        target = self.root / 'result.json'
        target.write_text('[{"old":true}]')
        def fail(rows, stream, **kwargs):
            stream.write('[')
            raise OSError('simulated full disk')
        with patch.object(ow.json, 'dump', fail):
            with self.assertRaises(OSError): ow.write_json([self.row('hello')], str(target))
        self.assertEqual(json.loads(target.read_text()), [{'old': True}])
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_failed_replace_preserves_previous_file(self):
        for fmt in ('json', 'csv'):
            target = self.root / ('result.' + fmt)
            target.write_text('previous')
            with patch.object(ow.os, 'replace', side_effect=OSError('simulated replace error')):
                with self.assertRaises(OSError): ow.write_output([self.row('hello')], str(target), fmt)
            self.assertEqual(target.read_text(), 'previous')
            self.assertFalse(list(self.root.glob('*.tmp')))

    def test_csv_formula_escape_and_monitoring_round_trip(self):
        titles = ['=1+1', '+123', '-123', '@SUM(1)', '  =1+1', '\t=1+1', "'literal", 'ordinary']
        rows = [self.row(title) for title in titles]
        target = self.root / 'result.csv'
        ow.finish_run(products=rows, out_path=str(target), fmt='csv', engine='test', url='test',
            pages_requested=1, pages_completed=1, failed_pages=[], blocked=False,
            remote_api_error=False, allow_empty=False, started_at=0)
        with target.open(newline='') as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual([r['title'] for r in exported], ["'" + title for title in titles[:-1]] + ['ordinary'])
        self.assertEqual([r['title'] for r in diff_runs._load_rows(str(target))], titles)

    def test_failed_metadata_write_preserves_previous_sidecar(self):
        target = self.root / 'result.json'
        sidecar = Path(str(target) + '.meta.json')
        sidecar.write_text('{"old":true}')
        with patch.object(ow.json, 'dump', side_effect=OSError('simulated full disk')):
            with self.assertRaises(OSError):
                ow.write_meta(str(target), status='complete', stop_reason='complete', engine='test',
                    url='test', pages_requested=1, pages_completed=1, product_count=1, started_at=0)
        self.assertEqual(json.loads(sidecar.read_text()), {'old': True})
        self.assertEqual(list(self.root.iterdir()), [sidecar])


class AuditPagination(unittest.TestCase):
    def test_bad_graphql_answers_keep_rows_and_report_partial(self):
        import smoke_test as smoke
        bad_answers = ['{"errors":[{"message":"session expired"}]}', '', 'not json',
            '{"data":{}}', '{"data":{"connection":{"page_info":{"has_next_page":true,"end_cursor":null}}}}',
            '{"errors":[{"message":"failed"}],"data":{"connection":{"page_info":{"has_next_page":false,"end_cursor":null}}}}']
        for answer in bad_answers:
            with self.subTest(answer=answer):
                code, meta, rows, engine = smoke._pflow(
                    {smoke._BIKE_URL: smoke._paged(answers=[(200, answer)])},
                    ['--url', smoke._BIKE_URL, '--max-results', '100'], paginate=True)
                self.assertEqual(code, ow.EXIT_PARTIAL)
                self.assertEqual(meta['status'], 'partial')
                self.assertEqual(meta['pages'][0]['pagination'], 'error')
                self.assertEqual(len(rows), 12)
