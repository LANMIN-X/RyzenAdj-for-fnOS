import threading
import unittest
from unittest.mock import Mock, patch

from test_smu_repair import powerd


def info(watts):
    return '\n'.join('| %s | %s |' % (name, watts) for name in (
        'STAPM LIMIT', 'PPT LIMIT FAST', 'PPT LIMIT SLOW',
        'STAPM VALUE', 'PPT VALUE FAST', 'PPT VALUE SLOW', 'THM VALUE CORE',
    ))


class PowerReadbackTests(unittest.TestCase):
    def make_app(self):
        app = powerd.PowerApp.__new__(powerd.PowerApp)
        app.io_lock = threading.Lock()
        app._read_info = Mock(return_value={'cpu_family': 'Cezanne'})
        app._log_command = Mock()
        return app

    def test_delayed_table_update_does_not_repeat_the_write(self):
        app = self.make_app()
        app._run = Mock(side_effect=['set OK', info(15), info(20)])
        with patch.object(powerd.time, 'monotonic', side_effect=[0, 0]), patch.object(powerd.time, 'sleep') as sleep:
            self.assertEqual(app._set_limits({key: 20 for key in powerd.LIMIT_FIELDS}),
                             {key: 20 for key in powerd.LIMIT_FIELDS})
        self.assertEqual(app._run.call_count, 3)
        self.assertEqual(app._run.call_args_list[1].args[0], ['--info'])
        self.assertEqual(app._run.call_args_list[2].args[0], ['--info'])
        sleep.assert_called_once_with(0.25)

    def test_persistent_mismatch_is_still_an_error(self):
        app = self.make_app()
        app._run = Mock(side_effect=['set OK', info(15)])
        with patch.object(powerd.time, 'monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(powerd.PowerError, '实际读回 15.000W'):
                app._set_limits({key: 20 for key in powerd.LIMIT_FIELDS})
        app._log_command.assert_called_once()


if __name__ == '__main__':
    unittest.main()
