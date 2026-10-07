import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('nexus', ROOT / 'plugins/nexus-mods/skills/nexus-mods/scripts/nexus.py')
nexus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nexus)


def response(payload, headers=None):
    value = io.BytesIO(json.dumps(payload).encode())
    value.headers = headers or {}
    return value


class NexusTests(unittest.TestCase):
    def setUp(self):
        nexus._RATE_STATE.clear()

    def failure(self, fn):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            fn()
        return output.getvalue()

    def test_graphql_identity_without_key_lookup(self):
        with patch.object(nexus, 'api_key', side_effect=AssertionError('must not read credentials')), patch.object(nexus.urllib.request, 'urlopen', return_value=response({'data': {'game': {'id': 1704}}})) as send:
            self.assertEqual(nexus.gql('query { game { id } }'), {'game': {'id': 1704}})
        req = send.call_args.args[0]
        headers = dict((k.lower(), v) for k, v in req.header_items())
        self.assertEqual(req.full_url, 'https://api.nexusmods.com/v2/graphql')
        self.assertEqual(headers['application-name'], nexus.APP_NAME)
        self.assertEqual(headers['application-version'], nexus.APP_VERSION)
        self.assertNotIn('apikey', headers)

    def test_rest_identity_and_mock_auth(self):
        with patch.object(nexus, 'api_key', return_value='TEST-ONLY-NOT-A-CREDENTIAL'), patch.object(nexus.urllib.request, 'urlopen', return_value=response({'name': 'Tester'}, {'X-RL-Daily-Remaining': '42'})) as send:
            data, limits = nexus.v1('/users/validate.json')
        headers = dict((k.lower(), v) for k, v in send.call_args.args[0].header_items())
        self.assertEqual(headers['apikey'], 'TEST-ONLY-NOT-A-CREDENTIAL')
        self.assertEqual(headers['application-name'], nexus.APP_NAME)
        self.assertEqual(headers['application-version'], nexus.APP_VERSION)
        self.assertEqual(limits['X-RL-Daily-Remaining'], '42')
        self.assertEqual(data['name'], 'Tester')

    def test_daily_exhaustion_allows_hourly_fallback(self):
        nexus.observe_limits('rest', {'X-RL-Daily-Remaining': '0', 'X-RL-Hourly-Remaining': '499'})
        self.assertNotIn('rest', nexus._RATE_STATE)

    def test_hourly_zero_with_daily_allowance(self):
        nexus.observe_limits('rest', {'X-RL-Daily-Remaining': '100', 'X-RL-Hourly-Remaining': '0'})
        self.assertNotIn('rest', nexus._RATE_STATE)

    def test_preventive_block_and_expiry(self):
        headers = {'X-RL-Daily-Remaining': '0', 'X-RL-Hourly-Remaining': '0', 'X-RL-Hourly-Reset': '1100'}
        with patch.object(nexus.time, 'time', return_value=1000), patch.object(nexus.urllib.request, 'urlopen', return_value=response({}, headers)) as send:
            nexus.http_json(nexus.V1 + '/test')
            self.assertIn('paused', self.failure(lambda: nexus.http_json(nexus.V1 + '/test')))
            self.assertEqual(send.call_count, 1)
            nexus.check_limits('graphql')  # Independent, no assumed shared quota.
        with patch.object(nexus.time, 'time', return_value=1101):
            nexus.check_limits('rest')

    def test_missing_and_invalid_headers(self):
        for headers in ({}, {'X-RL-Daily-Remaining': 'invalid'}):
            nexus.observe_limits('rest', headers)
            self.assertNotIn('rest', nexus._RATE_STATE)
        with patch.object(nexus.time, 'time', return_value=1000):
            nexus.observe_limits('rest', {'X-RL-Daily-Remaining': '0'})
        self.assertEqual(nexus._RATE_STATE['rest'], 4600)

    def test_429_retry_after_no_retry_or_secret_output(self):
        error = HTTPError(nexus.V1 + '/test?key=SECRET', 429, 'limited', {'Retry-After': '120'}, io.BytesIO(b'SECRET'))
        with patch.object(nexus.time, 'time', return_value=1000), patch.object(nexus.urllib.request, 'urlopen', side_effect=error) as send:
            text = self.failure(lambda: nexus.http_json(nexus.V1 + '/test?key=SECRET'))
            self.assertIn('No automatic retry', text)
            self.assertNotIn('SECRET', text)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(nexus._RATE_STATE['rest'], 1120)

    def test_retry_after_http_date(self):
        with patch.object(nexus.time, 'time', return_value=1000):
            nexus.observe_limits('rest', {'Retry-After': 'Thu, 01 Jan 1970 00:20:00 GMT'}, True)
        self.assertEqual(nexus._RATE_STATE['rest'], 1200)

    def test_429_malformed_headers_fallback(self):
        with patch.object(nexus.time, 'time', return_value=1000):
            nexus.observe_limits('rest', {'Retry-After': 'garbage', 'X-RL-Hourly-Reset': 'invalid'}, True)
        self.assertEqual(nexus._RATE_STATE['rest'], 4600)

    def test_iso_reset(self):
        self.assertEqual(nexus.reset_epoch('1970-01-01T00:20:00Z'), 1200)

    def test_errors_do_not_echo_body_url_or_reason(self):
        for code in (401, 403, 500):
            error = HTTPError(nexus.V1 + '/test?key=SECRET', code, 'SECRET', {}, io.BytesIO(b'SECRET'))
            with patch.object(nexus.urllib.request, 'urlopen', side_effect=error):
                self.assertNotIn('SECRET', self.failure(lambda: nexus.http_json(nexus.V1 + '/test?key=SECRET')))
        with patch.object(nexus.urllib.request, 'urlopen', side_effect=URLError('SECRET')):
            self.assertNotIn('SECRET', self.failure(lambda: nexus.http_json(nexus.V1 + '/test?key=SECRET')))

    def test_invalid_nxm_does_not_echo_token(self):
        for url in ('https://example.test/?key=SECRET', 'nxm://game/collections/1?key=SECRET'):
            self.assertNotIn('SECRET', self.failure(lambda: nexus.parse_nxm(url)))

    def test_valid_nxm(self):
        self.assertEqual(nexus.parse_nxm('nxm://game/mods/1/files/2?key=TEST&expires=42'),
                         {'game': 'game', 'mod_id': 1, 'file_id': 2, 'key': 'TEST', 'expires': '42'})

    def test_graphql_errors(self):
        with patch.object(nexus.urllib.request, 'urlopen', return_value=response({'errors': [{'message': 'Unknown field'}]})):
            self.assertIn('Unknown field', self.failure(lambda: nexus.gql('query { bad }')))

    def test_free_account_cannot_download_directly(self):
        args = SimpleNamespace(nxm=None, game='game', mod_id=1, file_id=2, out=None)
        with patch.object(nexus, 'resolve_game', return_value=(1, 'game', 'Game')), patch.object(nexus, 'v1', return_value=({'is_premium': False}, {})), patch.object(nexus, 'do_download') as download:
            self.assertIn('not Premium', self.failure(lambda: nexus.cmd_download(args)))
            download.assert_not_called()

    def test_premium_download_dispatch(self):
        args = SimpleNamespace(nxm=None, game='game', mod_id=1, file_id=2, out=None)
        with patch.object(nexus, 'resolve_game', return_value=(1, 'game', 'Game')), patch.object(nexus, 'v1', return_value=({'is_premium': True}, {})), patch.object(nexus, 'do_download') as download:
            nexus.cmd_download(args)
            download.assert_called_once_with('game', 1, 2, out=None)

    def test_simulated_download_writes_file_and_log_without_tokens(self):
        payload = io.BytesIO(b'test mod bytes')
        payload.headers = {'Content-Length': '14'}
        with TemporaryDirectory() as tmp, patch.object(nexus, 'config_dir', return_value=Path(tmp)), patch.object(nexus, 'v1', return_value=([{'URI': 'https://example.test/mod.zip?token=SECRET', 'name': 'Test'}], {})) as rest, patch.object(nexus.urllib.request, 'urlopen', return_value=payload) as cdn, contextlib.redirect_stdout(io.StringIO()) as output:
            target = nexus.do_download('game', 1, 2, 'SECRET', '42', tmp)
            self.assertEqual(target.read_bytes(), b'test mod bytes')
            self.assertNotIn('SECRET', (Path(tmp) / 'downloads.log').read_text())
            self.assertNotIn('SECRET', output.getvalue())
            self.assertNotIn('apikey', dict((k.lower(), v) for k, v in cdn.call_args.args[0].header_items()))
            rest.assert_called_once_with('/games/game/mods/1/files/2/download_link.json', {'key': 'SECRET', 'expires': '42'})

    def test_version_matches_plugin(self):
        plugin = json.loads((ROOT / 'plugins/nexus-mods/.claude-plugin/plugin.json').read_text())
        self.assertEqual(nexus.APP_VERSION, plugin['version'])


if __name__ == '__main__':
    unittest.main()
