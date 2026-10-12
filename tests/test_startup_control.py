import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
            app.target_temperature = 65
            app.target_boost = False
            app._save_control_state()
            app._load_control_state()
            self.assertTrue(app.control_enabled)
            self.assertEqual(app.target_temperature, 65)
            self.assertFalse(app.target_boost)
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

    def test_disable_preserves_goals_and_startup_reapplies_all_settings(self):
        app = powerd.PowerApp.__new__(powerd.PowerApp)
        app.control_enabled = True
        app.target_limits = {key: 12.0 for key in powerd.LIMIT_FIELDS}
        app.target_watts = 12
        app.separate_limits = False
        app.target_temperature = 65
        app.target_boost = False
        app.baseline = {key: 45.0 for key in powerd.LIMIT_FIELDS}
        app.baseline_temperature = 92
        info = {'cpu_family': 'Cezanne', 'limits': app.baseline, 'platform_limit': 54}
        app._read_info = Mock(return_value=info)
        app._set_limits = Mock(return_value=app.baseline)
        app._save_control_state = Mock()
        app.set_temperature = Mock()
        app.set_boost = Mock()
        self.assertFalse(app.set_control(False))
        self.assertEqual(app.target_watts, 12)
        self.assertEqual(app.target_limits, {key: 12.0 for key in powerd.LIMIT_FIELDS})
        app.set_temperature.assert_called_once_with(92, persist=False)
        app._apply_limits = Mock()
        app.control_enabled = True
        app._apply_saved_settings(info)
        app._apply_limits.assert_called_once_with(app.target_limits)
        app.set_temperature.assert_called_with(65, persist=False)
        app.set_boost.assert_called_once_with(False, persist=False)


if __name__ == '__main__':
    unittest.main()
