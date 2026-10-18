"""The frozen release checks must also pass against the current source."""
import unittest

from spark_assistant.release_checks import check_v020_state


class ReleaseStateChecks(unittest.TestCase):
    def test_disposable_v2_acceptance(self):
        checks = check_v020_state()
        self.assertGreaterEqual(len(checks), 12)
        self.assertTrue(all(value == "passed" for value in checks.values()))
