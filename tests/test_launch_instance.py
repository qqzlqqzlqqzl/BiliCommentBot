import os
import sys
import unittest
from unittest.mock import Mock, patch

import launch_instance


class LaunchInstanceTests(unittest.TestCase):
    def test_explicit_second_account_arguments_set_environment_before_import(self):
        args = Mock(
            port=5001,
            account_slot="2",
            data_dir="data-account-2",
            auto_start_monitor=False,
        )
        fake_server = Mock()

        with patch.object(launch_instance, "parse_args", return_value=args):
            with patch.dict(sys.modules, {"server": fake_server}):
                with patch.dict(os.environ, {}, clear=False):
                    launch_instance.main()

                    self.assertEqual(os.environ["BILI_PORT"], "5001")
                    self.assertEqual(os.environ["BILI_ACCOUNT_NAME"], "账号2")
                    self.assertTrue(os.environ["BILI_DATA_DIR"].endswith("data-account-2"))
                    self.assertEqual(os.environ["BILI_AUTO_START_MONITOR"], "0")
        fake_server.main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
