import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.cloud import stop_child, supervise, wait_api


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.settings = MagicMock()
        self.settings.telegram_credentials.return_value = ("fake-token", 123)
        self.api = MagicMock()
        self.api.poll.return_value = None
        self.bot = MagicMock()
        self.bot.poll.return_value = None
        self.connection = MagicMock()
        self.connection.execution_options.return_value = self.connection

    def test_failed_api_never_starts_polling(self):
        with patch("app.cloud.get_settings", return_value=self.settings), patch(
            "app.cloud.subprocess.Popen", return_value=self.api) as spawn, patch(
            "app.cloud.wait_api", return_value=False), patch("app.cloud.get_engine") as engine:
            self.assertEqual(supervise(8000, self.stop), 1)
        self.assertEqual(spawn.call_count, 1)
        engine.assert_not_called()
        self.api.terminate.assert_called_once()

    def test_bot_waits_for_database_lock_and_children_stop_on_shutdown(self):
        def scalar(statement, *args):
            if "pg_try_advisory_lock" in str(statement):
                return True
            return 1
        self.connection.scalar.side_effect = scalar
        def wait(*args):
            self.stop.set()
        with patch("app.cloud.get_settings", return_value=self.settings), patch(
            "app.cloud.subprocess.Popen", side_effect=[self.api, self.bot]) as spawn, patch(
            "app.cloud.wait_api", return_value=True), patch(
            "app.cloud.get_engine") as engine, patch.object(self.stop, "wait", side_effect=wait):
            engine.return_value.connect.return_value = self.connection
            self.assertEqual(supervise(8123, self.stop), 0)
        self.assertIn("--process", spawn.call_args_list[1].args[0])
        self.assertIn("8123", spawn.call_args_list[1].args[0])
        self.api.terminate.assert_called_once()
        self.bot.terminate.assert_called_once()
        self.assertIn("pg_advisory_unlock", str(self.connection.scalar.call_args.args[0]))

    def test_unavailable_lock_does_not_start_a_second_bot(self):
        self.connection.scalar.return_value = False
        with patch("app.cloud.get_settings", return_value=self.settings), patch(
            "app.cloud.subprocess.Popen", return_value=self.api) as spawn, patch(
            "app.cloud.wait_api", return_value=True), patch("app.cloud.get_engine") as engine, patch.object(
            self.stop, "wait", side_effect=lambda *_: self.stop.set()):
            engine.return_value.connect.return_value = self.connection
            self.assertEqual(supervise(8000, self.stop), 0)
        self.assertEqual(spawn.call_count, 1)

    def test_bot_exit_stops_api_for_service_restart(self):
        self.connection.scalar.return_value = True
        self.bot.poll.return_value = 1
        with patch("app.cloud.get_settings", return_value=self.settings), patch(
            "app.cloud.subprocess.Popen", side_effect=[self.api, self.bot]), patch(
            "app.cloud.wait_api", return_value=True), patch("app.cloud.get_engine") as engine, patch.object(
            self.stop, "wait", return_value=False):
            engine.return_value.connect.return_value = self.connection
            self.assertEqual(supervise(8000, self.stop), 1)
        self.api.terminate.assert_called_once()

    def test_lost_lock_connection_stops_running_bot(self):
        self.connection.scalar.side_effect = [True, RuntimeError("fake secret"), True]
        with patch("app.cloud.get_settings", return_value=self.settings), patch(
            "app.cloud.subprocess.Popen", side_effect=[self.api, self.bot]), patch(
            "app.cloud.wait_api", return_value=True), patch("app.cloud.get_engine") as engine, patch.object(
            self.stop, "wait", return_value=False):
            engine.return_value.connect.return_value = self.connection
            self.assertEqual(supervise(8000, self.stop), 1)
        self.bot.terminate.assert_called_once()

    def test_termination_kills_unresponsive_child(self):
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("fake", 15), 0]
        stop_child(child)
        child.kill.assert_called_once()

    def test_readiness_does_not_wait_on_dead_api(self):
        child = MagicMock()
        child.poll.return_value = 1
        self.assertFalse(wait_api(8000, child, self.stop, timeout=1))

    def test_railway_config_migrates_and_supervises_without_public_domain(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "railway.json").read_text())
        self.assertEqual(config["deploy"]["startCommand"], "python -m app.cloud")
        self.assertEqual(config["deploy"]["preDeployCommand"], ["alembic upgrade head"])
        self.assertEqual(config["deploy"]["numReplicas"], 1)
        self.assertEqual(config["deploy"]["healthcheckPath"], "/health/db")
        docker = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
        self.assertNotIn("COPY . ", docker)


if __name__ == "__main__":
    unittest.main()
