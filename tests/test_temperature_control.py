import threading
import unittest
from unittest.mock import Mock

from test_smu_repair import powerd


class TemperatureControlTests(unittest.TestCase):
    def test_input_validation_and_hardware_readback(self):
        app = powerd.PowerApp.__new__(powerd.PowerApp)
        app.control_enabled = True
        app.io_lock = threading.Lock()
        app._save_control_state = Mock()
        app._read_info = Mock(return_value={'cpu_family': 'Cezanne', 'temperature_limit': 92})
        app._run = Mock(return_value='| STAPM LIMIT | 15 |\n| PPT LIMIT FAST | 15 |\n| PPT LIMIT SLOW | 15 |\n| STAPM VALUE | 4 |\n| PPT VALUE FAST | 5 |\n| PPT VALUE SLOW | 4 |\n| THM VALUE CORE | 45 |\n| THM LIMIT CORE | 80.001 |')
        for value in (True, None, float('nan'), 19, 96):
            with self.assertRaises(powerd.PowerError):
                app.set_temperature(value)
        self.assertAlmostEqual(app.set_temperature(80), 80.001)
        self.assertEqual(app._run.call_args_list[0].args[0], ['--tctl-temp=80'])
        app._run.return_value = app._run.return_value.replace("80.001", "20.001")
        self.assertAlmostEqual(app.set_temperature(20), 20.001)
        app.control_enabled = False
        with self.assertRaises(powerd.PowerError):
            app.set_temperature(80)


if __name__ == '__main__':
    unittest.main()
