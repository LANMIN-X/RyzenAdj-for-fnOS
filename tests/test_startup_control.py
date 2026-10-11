import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_smu_repair import powerd


class StartupControlTests(unittest.TestCase):
    def test_startup_is_independent_and_tracks_system_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            app = powerd.PowerApp.__new__(powerd.PowerApp)
            app.control_path = Path(directory) / 'control.json'
            app.boot_id = 'boot-a'
            app.control_enabled = True
            app.startup_enabled = False
            app.target_watts = 15
            app.target_limits = {'stapm': 15.0, 'fast': 15.0, 'slow': 15.0}
            app.separate_limits = False
            app._save_control_state()
            app._load_control_state()
            self.assertTrue(app.control_enabled)
            app.boot_id = 'boot-b'
            app._load_control_state()
            self.assertFalse(app.control_enabled)
            app.set_startup(True)
            self.assertFalse(app.control_enabled)
            app._load_control_state()
            self.assertFalse(app.control_enabled)
            app.boot_id = 'boot-c'
            app._load_control_state()
            self.assertTrue(app.control_enabled)
            with patch.object(app, '_save_control_state', side_effect=OSError('denied')):
                with self.assertRaises(powerd.PowerError):
                    app.set_startup(False)
            self.assertTrue(app.startup_enabled)
            with self.assertRaises(powerd.PowerError):
                app.set_startup('true')
            app.control_path.write_text(json.dumps({'enabled': True, 'watts': 15}))
            app._load_control_state()
            self.assertTrue(app.startup_enabled)
            self.assertTrue(app.control_enabled)


if __name__ == '__main__':
    unittest.main()
