"""No installer execution; validate literal ownership manifests on synthetic data."""
import tempfile
import unittest
from pathlib import Path

from scripts.make_uninstall_manifest import collect_payload, nsis_literal, render_manifest


class InstallerManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dist = Path(self.temp.name) / 'dist'
        self.dist.mkdir()
        (self.dist / 'SparkAssistant.exe').write_bytes(b'fake desktop')
        (self.dist / 'SparkWorker.exe').write_bytes(b'fake worker')
        (self.dist / '_internal/nested').mkdir(parents=True)
        (self.dist / '_internal/nested/literal$0.txt').write_text('owned')

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_is_literal_and_never_deletes_recursively(self):
        manifest = render_manifest(self.dist)
        self.assertIn('Delete "$INSTDIR\\_internal\\nested\\literal$$0.txt"', manifest)
        self.assertNotIn('RMDir /r', manifest)
        self.assertNotIn('Delete "$INSTDIR\\*', manifest)
        self.assertLess(manifest.index('RMDir "$INSTDIR\\_internal\\nested"'),
                        manifest.index('RMDir "$INSTDIR\\_internal"'))
        self.assertIn('Call un.SparkPathHasReparsePoint', manifest)
        self.assertNotIn('sentinel', manifest)

    def test_rejects_private_state_and_unexpected_top_level(self):
        for name in ('profiles', '.local', '.env', 'accounts'):
            private = self.dist / '_internal' / name
            private.mkdir()
            with self.assertRaises(ValueError):
                collect_payload(self.dist)
            private.rmdir()
        (self.dist / 'private.sqlite').write_bytes(b'private')
        with self.assertRaises(ValueError):
            collect_payload(self.dist)

    def test_requires_both_programs(self):
        (self.dist / 'SparkWorker.exe').unlink()
        with self.assertRaises(ValueError):
            collect_payload(self.dist)

    def test_literals_escape_variables_and_quotes(self):
        self.assertEqual('a$$0$\\"b', nsis_literal('a$0"b'))
        for invalid in ('bad\nname', '${PREPROCESSOR}', 'bad\x00name'):
            with self.assertRaises(ValueError):
                nsis_literal(invalid)

    def test_payload_source_and_runtime_dollar_paths_differ(self):
        manifest = render_manifest(self.dist)
        source = str(self.dist / '_internal/nested/literal$0.txt')
        self.assertIn(f'File "/oname=literal$$0.txt" "{source}"', manifest)


if __name__ == '__main__':
    unittest.main()
