"""Local adversarial regressions; no real credentials or network requests."""
import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import http.client

from test_nexus import nexus


class SecurityPathTests(unittest.TestCase):
    def test_authenticated_redirects_are_refused_without_second_request(self):
        request = urllib.request.Request(nexus.V1 + '/users/validate.json', headers={'apikey': 'TEST-SECRET'})
        for url in ('https://example.test/steal', nexus.V1 + '/another', 'http://api.nexusmods.com/test'):
            with self.assertRaises(urllib.error.HTTPError):
                nexus.SafeRedirect().redirect_request(request, None, 302, 'redirect', {}, url)

    def test_public_redirects_allow_https_but_not_downgrade_or_userinfo(self):
        request = urllib.request.Request(nexus.GQL)
        result = nexus.SafeRedirect().redirect_request(request, None, 302, 'redirect', {}, 'https://example.test/public')
        self.assertEqual(result.full_url, 'https://example.test/public')
        for url in ('http://example.test/public', 'https://user:secret@example.test/public'):
            with self.assertRaises(urllib.error.HTTPError):
                nexus.SafeRedirect().redirect_request(request, None, 302, 'redirect', {}, url)

    def test_download_rejects_unsafe_filenames_and_schemes_before_io(self):
        suffixes = ['%2e%2e%2fescape.zip', '%5cescape.zip', 'C%3aescape.zip', 'mod%00.zip', '.', '..', '', 'CON.zip', 'mod.zip.', 'mod.zip%20', 'mod%0a.zip']
        urls = ['https://example.test/' + name for name in suffixes] + ['file:///etc/passwd', 'http://example.test/mod.zip', 'https://user:secret@example.test/mod.zip']
        with TemporaryDirectory() as tmp:
            for url in urls:
                with self.subTest(url=url), patch.object(nexus, 'v1', return_value=([{'URI': url}], {})), patch.object(nexus, '_open_url') as network, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    nexus.do_download('game', 1, 2, out=tmp)
                network.assert_not_called()
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_cdn_errors_do_not_expose_signed_url_and_cleanup_partial(self):
        url = 'https://example.test/mod.zip?token=TEST-SECRET'
        errors = [urllib.error.HTTPError(url, 403, 'TEST-SECRET', {}, io.BytesIO(b'TEST-SECRET')),
                  urllib.error.URLError('TEST-SECRET'), http.client.IncompleteRead(b'test', 20)]
        with TemporaryDirectory() as tmp:
            for error in errors:
                with patch.object(nexus, 'v1', return_value=([{'URI': url}], {})), patch.object(nexus, '_open_url', side_effect=error), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                    nexus.do_download('game', 1, 2, out=tmp)
                self.assertNotIn('TEST-SECRET', out.getvalue() + err.getvalue())
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_existing_partial_file_is_not_truncated_or_removed(self):
        with TemporaryDirectory() as tmp:
            part = Path(tmp) / 'mod.zip.part'
            part.write_bytes(b'preserve')
            with patch.object(nexus, 'v1', return_value=([{'URI': 'https://example.test/mod.zip'}], {})), patch.object(nexus, '_open_url') as network, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                nexus.do_download('game', 1, 2, out=tmp)
            self.assertEqual(part.read_bytes(), b'preserve')
            network.assert_not_called()

    def test_partial_symlink_is_not_followed(self):
        with TemporaryDirectory() as tmp:
            victim = Path(tmp) / 'preserve.txt'
            victim.write_text('keep')
            part = Path(tmp) / 'mod.zip.part'
            part.symlink_to(victim)
            with patch.object(nexus, 'v1', return_value=([{'URI': 'https://example.test/mod.zip'}], {})), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                nexus.do_download('game', 1, 2, out=tmp)
            self.assertEqual(victim.read_text(), 'keep')
            self.assertTrue(part.is_symlink())

    def test_nxm_rejects_trailing_path_and_invalid_game(self):
        for url in ('nxm://game/mods/1/files/2/extra', 'nxm://user@game/mods/1/files/2', 'nxm://../mods/1/files/2'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                nexus.parse_nxm(url)
