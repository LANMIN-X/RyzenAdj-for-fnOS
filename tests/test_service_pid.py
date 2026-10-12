import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(Path('/proc/self/cmdline').exists(), 'requires Linux /proc')
class ServicePidTests(unittest.TestCase):
    def test_unrelated_live_pid_is_not_an_app_process(self):
        script = Path(__file__).resolve().parents[1] / 'cmd/main'
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'app.pid'
            pid_file.write_text(str(os.getpid()))
            env = {**os.environ, 'TRIM_APPDEST': directory, 'TRIM_PKGVAR': directory}
            result = subprocess.run(['bash', str(script), 'status'], env=env, capture_output=True)
            self.assertEqual(result.returncode, 3)
            self.assertFalse(pid_file.exists())


if __name__ == '__main__':
    unittest.main()
