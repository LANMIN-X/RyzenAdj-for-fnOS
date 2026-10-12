import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_smu_repair import powerd


class BoostControlTests(unittest.TestCase):
    def test_read_write_validation_and_unsupported_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'boost'
            app = powerd.PowerApp.__new__(powerd.PowerApp)
            app.io_lock = threading.Lock()
            app._save_control_state = Mock()
            app._log_command = Mock()
            with patch.object(powerd, 'BOOST_PATH', path):
                self.assertIsNone(app.boost_status()['enabled'])
                with self.assertRaises(powerd.PowerError):
                    app.set_boost(True)
                self.assertFalse(path.exists())
                path.write_text('1\n')
                self.assertTrue(app.boost_status()['enabled'])
                self.assertFalse(app.set_boost(False))
                self.assertEqual(path.read_text(), '0')
                self.assertTrue(app.set_boost(True))
                with self.assertRaises(powerd.PowerError):
                    app.set_boost(1)
                path.write_text('invalid')
                self.assertIsNone(app.boost_status()['enabled'])
                with self.assertRaises(powerd.PowerError):
                    app.set_boost(False)


if __name__ == '__main__':
    unittest.main()
