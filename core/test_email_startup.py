import importlib
import os
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .apps import CoreConfig


class EmailWorkerStartupTests(SimpleTestCase):
    def setUp(self):
        self.config = CoreConfig("core", importlib.import_module("core"))

    @override_settings(EMAIL_INTAKE_AUTOSTART=True)
    def test_management_commands_do_not_start_background_polling(self):
        for command in ("check", "migrate", "test", "process_email_intake", "process_mailbox"):
            with self.subTest(command=command), patch("sys.argv", ["manage.py", command]), patch("core.email_worker.start_email_worker") as start:
                self.config.ready()
                start.assert_not_called()

    @override_settings(EMAIL_INTAKE_AUTOSTART=True)
    def test_runserver_starts_worker_in_serving_process_and_without_reloader(self):
        for arguments, run_main, expected in [
            (["manage.py", "runserver"], "false", False),
            (["manage.py", "runserver"], "true", True),
            (["manage.py", "runserver", "--noreload"], "false", True),
        ]:
            with self.subTest(arguments=arguments, run_main=run_main), patch("sys.argv", arguments), patch.dict(os.environ, {"RUN_MAIN": run_main}), patch("core.email_worker.start_email_worker") as start:
                self.config.ready()
                self.assertEqual(start.call_count, int(expected))

    @override_settings(EMAIL_INTAKE_AUTOSTART=False)
    def test_disabled_autostart_does_not_start_worker(self):
        with patch("sys.argv", ["manage.py", "runserver", "--noreload"]), patch("core.email_worker.start_email_worker") as start:
            self.config.ready()
            start.assert_not_called()
