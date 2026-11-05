"""Offline coverage for model discovery; fixtures contain no real credentials."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from gemini_web2api import gemini, model_monitor as monitor
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG, load_env


def row(model_id, name, category="Fast", number=1):
    result = [None] * 20
    result[0], result[1] = model_id, category
    result[11], result[12], result[17] = name, "Synthetic description", number
    return result


def wire(rows=None, status=1000, tiers=None, flags=None, pretty=False):
    payload = [None] * 18
    payload[14] = status
    payload[15] = rows if rows is not None else [row("flash-id", "3.8 Flash")]
    payload[16], payload[17] = tiers or [], flags or []
    batch = [["wrb.fr", monitor.RPC_ID, json.dumps(payload), None, None, None, "generic"],
             ["wrb.fr", "unrelated", "[]"]]
    text = json.dumps(batch, indent=2 if pretty else None)
    return ")]}'\n\n" + str(len(text)) + "\n" + text + "\n"


def result(rows=None, scope="session-a"):
    parsed = monitor.parse_model_response(wire(rows))
    parsed["scope"] = scope
    return parsed


class ModelParsingTests(unittest.TestCase):
    def test_parse_framed_and_multiline_responses_with_new_names(self):
        rows = [row("argon-id", "4 Argon", "Argon", 7), row("flash-id", "3.8 Flash")]
        for pretty in (False, True):
            with self.subTest(pretty=pretty):
                parsed = monitor.parse_model_response(wire(rows, pretty=pretty))
                self.assertEqual([item["display_name"] for item in parsed["models"]], ["4 Argon", "3.8 Flash"])
                self.assertEqual(parsed["models"][0]["number"], 7)

    def test_secondary_fields_and_capacity_flags(self):
        secondary = [None] * 20
        secondary[0], secondary[10], secondary[19], secondary[9] = "id", "Pro", "3.1 Pro", 9
        secondary[2] = "Fallback description"
        for tiers, flags, capacity, field in (([], [], 1, 12), ([8], [], 2, 12),
                                             ([], [106], 3, 12), ([], [115], 4, 12),
                                             ([21], [115], 1, 13), ([22], [], 2, 13)):
            with self.subTest(tiers=tiers, flags=flags):
                model = monitor.parse_model_response(wire([secondary], tiers=tiers, flags=flags))["models"][0]
                self.assertEqual((model["capacity"], model["capacity_field"]), (capacity, field))
                self.assertEqual(model["number"], 9)
                self.assertEqual(model["display_name"], "3.1 Pro")
                self.assertEqual(model["description"], "Fallback description")

    def test_invalid_partial_and_unauthenticated_lists_are_rejected(self):
        fixtures = ["not JSON", wire([]), wire([row("id", "A"), None]),
                    wire([row("id", "A"), row("id", "B")]), wire([row("", "A")]),
                    wire(status=1016), wire(status=1060), wire(status="1000"),
                    json.dumps([["wrb.fr", monitor.RPC_ID, "invalid"]]),
                    json.dumps([["wrb.fr", monitor.RPC_ID, "[]"]]),
                    wire() + wire()]
        for fixture in fixtures:
            with self.subTest(fixture=fixture[:35]):
                with self.assertRaises(monitor.ModelResponseError):
                    monitor.parse_model_response(fixture)

    def test_order_changes_do_not_count_as_model_changes(self):
        models = result([row("a", "A"), row("b", "B")])["models"]
        self.assertFalse(any(monitor.compare_models(models, list(reversed(models))).values()))

    def test_diff_distinguishes_rename_metadata_addition_and_removal(self):
        before = result([row("rename", "Old"), row("metadata", "Stable"), row("removed", "Gone")])["models"]
        after = result([row("rename", "New"), row("metadata", "Stable", number=8), row("added", "4 Argon")])["models"]
        changes = monitor.compare_models(before, after)
        for kind, model_id in (("added", "added"), ("removed", "removed"),
                               ("renamed", "rename"), ("updated", "metadata")):
            self.assertEqual([item["id"] for item in changes[kind]], [model_id])

    def test_scope_tracks_account_and_session_but_ignores_rotation(self):
        cookie = "__Secure-1PSID=synthetic-session; SAPISID=synthetic; __Secure-1PSIDTS=old"
        baseline = monitor._scope(cookie, None)
        self.assertEqual(baseline, monitor._scope(cookie.replace("TS=old", "TS=new"), "0"))
        self.assertNotEqual(baseline, monitor._scope(cookie, "1"))
        self.assertNotEqual(baseline, monitor._scope(cookie.replace("synthetic-session", "other-session"), None))
        self.assertNotIn("synthetic", baseline)
        with self.assertRaises(monitor.ModelResponseError):
            monitor._scope("", None)


class ModelMonitorTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "models.json")
        CONFIG.update(cookie_file="synthetic-auth.json", model_check_interval_sec=3600,
                      model_snapshot_file=self.path, proxy=None, xsrf_token="synthetic-token",
                      gemini_bl="synthetic-build", auth_user=None, log_requests=False)
        self.fetch = mock.patch.object(monitor, "fetch_account_models", return_value=result()).start()
        self.scope = mock.patch.object(monitor, "_current_scope", return_value="session-a").start()
        self.logger = mock.patch.object(monitor, "log").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)
        self.directory.cleanup()

    def state(self):
        with open(self.path, encoding="utf-8") as source:
            return json.load(source)

    def test_baseline_is_private_and_attempt_persisted_before_request(self):
        def fetch():
            state = self.state()
            self.assertEqual(state["last_status"], "checking")
            self.assertEqual(state["last_attempt_at"], 10000)
            return result()
        self.fetch.side_effect = fetch
        self.assertEqual(monitor.check_models_once(now=10000), "baseline")
        state = self.state()
        self.assertEqual(state["snapshot"]["checked_at"], "1970-01-01T02:46:40Z")
        self.assertIsNone(state["last_change"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic-token", json.dumps(state))
        self.assertTrue(self.logger.call_args.kwargs["force"])

    def test_interval_survives_reload_and_short_interval_is_clamped(self):
        CONFIG["model_check_interval_sec"] = 1
        self.assertEqual(monitor.check_models_once(now=10000), "baseline")
        self.assertEqual(monitor.check_models_once(now=13599), "not_due")
        self.assertEqual(monitor.check_models_once(now=13600), "unchanged")
        self.assertEqual(self.fetch.call_count, 2)
        # Each call reloads disk state, with no process-local schedule to reset.
        self.assertEqual(monitor.check_models_once(now=13601), "not_due")

    def test_default_daily_cycle_survives_restart_without_extra_fetch(self):
        CONFIG["model_check_interval_sec"] = DEFAULT_CONFIG["model_check_interval_sec"]
        self.assertEqual(monitor.check_models_once(now=10000), "baseline")
        self.assertEqual(monitor.check_models_once(now=10060), "not_due")
        self.assertEqual(monitor.check_models_once(now=96399), "not_due")
        self.assertEqual(monitor.check_models_once(now=96400), "unchanged")
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.state()["last_attempt_at"], 96400)

    def test_configured_six_hour_interval_is_respected(self):
        CONFIG["model_check_interval_sec"] = 21600
        monitor.check_models_once(now=10000)
        self.assertEqual(monitor.check_models_once(now=31599), "not_due")
        self.assertEqual(monitor.check_models_once(now=31600), "unchanged")

    def test_change_saved_and_preserved_on_next_unchanged_check(self):
        monitor.check_models_once(now=10000)
        self.fetch.return_value = result([row("flash-id", "3.9 Flash"), row("argon-id", "4 Argon")])
        self.assertEqual(monitor.check_models_once(now=13600), "changed")
        change = self.state()["last_change"]
        self.assertEqual(change["changes"]["renamed"][0]["after"]["display_name"], "3.9 Flash")
        self.assertEqual(change["changes"]["added"][0]["display_name"], "4 Argon")
        self.assertEqual(monitor.check_models_once(now=17200), "unchanged")
        self.assertEqual(self.state()["last_change"], change)
        self.assertTrue(any("model_change" in call.args[0] for call in self.logger.call_args_list))

    def test_failures_preserve_baseline_and_do_not_retry_early_or_log_error_body(self):
        monitor.check_models_once(now=10000)
        baseline = self.state()["snapshot"]
        for index, error in enumerate((urllib.error.HTTPError("https://example.invalid", 401,
                                                               "synthetic-secret", {}, io.BytesIO(b"synthetic-secret")),
                                       monitor.ModelResponseError("synthetic-secret"),
                                       TimeoutError("synthetic-secret"))):
            now = 13600 + index * 3600
            self.fetch.side_effect = error
            status = monitor.check_models_once(now=now)
            self.assertTrue(status == "http_401" or status.startswith("error_"))
            self.assertEqual(self.state()["snapshot"], baseline)
            calls = self.fetch.call_count
            self.assertEqual(monitor.check_models_once(now=now + 60), "not_due")
            self.assertEqual(self.fetch.call_count, calls)
        self.assertNotIn("synthetic-secret", json.dumps(self.state()))
        self.assertNotIn("synthetic-secret", str(self.logger.call_args_list))

    def test_new_account_creates_baseline_instead_of_false_removals(self):
        monitor.check_models_once(now=10000)
        self.fetch.return_value = result([row("other", "Other account")], scope="session-b")
        self.scope.return_value = "session-b"
        self.assertEqual(monitor.check_models_once(now=13600), "baseline")
        self.assertIsNone(self.state()["last_change"])
        self.assertEqual(self.state()["snapshot"]["scope"], "session-b")

    def test_session_switch_during_request_preserves_old_snapshot(self):
        monitor.check_models_once(now=10000)
        baseline = self.state()["snapshot"]
        self.scope.return_value = "session-b"
        self.assertEqual(monitor.check_models_once(now=13600), "error_ModelResponseError")
        self.assertEqual(self.state()["snapshot"], baseline)

    def test_disabled_missing_cookie_and_busy_checks_do_not_fetch(self):
        CONFIG["model_check_interval_sec"] = 0
        self.assertEqual(monitor.check_models_once(), "disabled")
        CONFIG.update(model_check_interval_sec=3600, cookie_file=None)
        self.assertEqual(monitor.check_models_once(), "disabled")
        CONFIG["cookie_file"] = "synthetic-auth.json"
        with monitor._check_lock:
            self.assertEqual(monitor.check_models_once(), "busy")
        self.fetch.assert_not_called()
        self.assertFalse(os.path.exists(self.path))

    def test_process_lock_prevents_competing_checks(self):
        with monitor._state_lock(self.path) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(monitor.check_models_once(now=10000), "busy")
        self.fetch.assert_not_called()
        self.assertEqual(monitor.check_models_once(now=10000), "baseline")

    def test_corrupt_or_unwritable_state_sends_no_request(self):
        for state in ("not JSON", {"schema_version": 2},
                      {"schema_version": 1, "last_attempt_at": "invalid"},
                      {"schema_version": 1, "last_attempt_at": float("nan")},
                      {"schema_version": 1, "last_attempt_at": 1, "snapshot": {"scope": "a", "models": []}}):
            with open(self.path, "w") as output:
                output.write(state if isinstance(state, str) else json.dumps(state))
            with self.assertRaises(ValueError):
                monitor.check_models_once(now=10000)
        os.unlink(self.path)
        with mock.patch.object(monitor, "_write_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                monitor.check_models_once(now=10000)
        self.fetch.assert_not_called()

    def test_failed_atomic_write_leaves_previous_file_and_no_temporary_files(self):
        monitor.check_models_once(now=10000)
        previous = self.state()
        with mock.patch.object(monitor.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                monitor._write_state(self.path, {"schema_version": 1})
        self.assertEqual(self.state(), previous)
        self.assertFalse(any(name.startswith(".gemini-models-") for name in os.listdir(self.directory.name)))

    def test_environment_config_and_disabled_worker_start(self):
        env = os.path.join(self.directory.name, ".env")
        with open(env, "w") as output:
            output.write("MODEL_CHECK_INTERVAL_SEC=21600\nMODEL_SNAPSHOT_FILE=./custom-models.json\n")
        load_env(env)
        self.assertEqual(CONFIG["model_check_interval_sec"], 21600)
        self.assertEqual(CONFIG["model_snapshot_file"], "./custom-models.json")
        self.assertEqual(DEFAULT_CONFIG["model_check_interval_sec"], 86400)
        CONFIG["model_check_interval_sec"] = 0
        with mock.patch.object(monitor.threading, "Thread") as thread:
            monitor.start_model_monitor()
            thread.assert_not_called()

    def test_worker_checks_immediately_and_avoids_duplicate_start(self):
        with mock.patch.object(monitor, "_monitor_thread", None), \
             mock.patch.object(monitor.threading, "Thread") as thread, \
             mock.patch.object(monitor, "_stop_event") as stop:
            monitor.start_model_monitor()
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
            stop.is_set.return_value = False
            stop.wait.side_effect = [False, True]

            def fetch():
                stop.wait.assert_not_called()
                return result()

            self.fetch.side_effect = fetch
            thread.call_args.kwargs["target"]()
            stop.wait.assert_has_calls([mock.call(60), mock.call(60)])
            self.fetch.assert_called_once()
            thread.return_value.is_alive.return_value = True
            monitor.start_model_monitor()
            thread.assert_called_once()
            monitor.stop_model_monitor()
            stop.set.assert_called_once()


class ModelRequestTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)
        CONFIG.update(xsrf_token="synthetic-token", gemini_bl="synthetic-build", auth_user="1", proxy=None)
        self.cookie = "__Secure-1PSID=synthetic-session; SAPISID=synthetic"

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def test_request_uses_loaded_auth_metadata_and_makes_one_rpc_call(self):
        def load_cookie():
            CONFIG.update(xsrf_token="updated-token", gemini_bl="updated-build", auth_user="2")
            return self.cookie, "synthetic"
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(wire().encode())
        with mock.patch.object(monitor, "load_cookie", side_effect=load_cookie), \
             mock.patch("gemini_web2api.multimodal._cached_page_tokens", return_value={}), \
             mock.patch.object(monitor, "_build_headers", return_value={"Cookie": self.cookie}), \
             mock.patch.object(monitor.urllib.request, "build_opener", return_value=opener) as build:
            parsed = monitor.fetch_account_models()
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 25)
        url = urlsplit(request.full_url)
        self.assertEqual(url.path, "/u/2/_/BardChatUi/data/batchexecute")
        self.assertEqual(parse_qs(url.query)["bl"], ["updated-build"])
        self.assertEqual(parse_qs(url.query)["source-path"], ["/u/2/app"])
        body = parse_qs(request.data.decode())
        self.assertEqual(body["at"], ["updated-token"])
        self.assertEqual(json.loads(body["f.req"][0]), [[[monitor.RPC_ID, "[]", None, "generic"]]])
        self.assertEqual(parsed["scope"], monitor._scope(self.cookie, "2"))
        self.assertIsInstance(build.call_args.args[-1], monitor.NoRedirect)
        self.assertNotIn("StreamGenerate", request.full_url)

    def test_uses_fresh_page_token_and_identity_encoding(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(wire().encode())
        headers = {"Cookie": self.cookie}
        with mock.patch.object(monitor, "load_cookie"), \
             mock.patch("gemini_web2api.multimodal._cached_page_tokens",
                        return_value={"at": "page-token", "f_sid": "page-session"}), \
             mock.patch.object(monitor, "_build_headers", return_value=headers), \
             mock.patch.object(monitor.urllib.request, "build_opener", return_value=opener):
            monitor.fetch_account_models()
        request = opener.open.call_args.args[0]
        url = urlsplit(request.full_url)
        self.assertEqual(parse_qs(url.query)["f.sid"], ["page-session"])
        self.assertEqual(parse_qs(request.data.decode())["at"], ["page-token"])
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_http_failure_is_not_retried(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch.object(monitor, "load_cookie"), \
             mock.patch("gemini_web2api.multimodal._cached_page_tokens", return_value={}), \
             mock.patch.object(monitor, "_build_headers", return_value={"Cookie": self.cookie}), \
             mock.patch.object(monitor.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                monitor.fetch_account_models()
            caught.exception.close()
        opener.open.assert_called_once()

    def test_missing_cookie_or_token_sends_no_request(self):
        with mock.patch.object(monitor, "load_cookie"), \
             mock.patch("gemini_web2api.multimodal._cached_page_tokens", return_value={}), \
             mock.patch.object(monitor, "_build_headers", return_value={}), \
             mock.patch.object(monitor.urllib.request, "build_opener") as build:
            with self.assertRaises(monitor.ModelResponseError):
                monitor.fetch_account_models()
            build.assert_not_called()
        CONFIG["xsrf_token"] = None
        with mock.patch.object(monitor, "load_cookie"), \
             mock.patch("gemini_web2api.multimodal._cached_page_tokens", return_value={}), \
             mock.patch.object(monitor, "_build_headers", return_value={"Cookie": self.cookie}), \
             mock.patch.object(monitor.urllib.request, "build_opener") as build:
            with self.assertRaises(monitor.ModelResponseError):
                monitor.fetch_account_models()
            build.assert_not_called()

    def test_proxy_forwarding_response_bound_and_redirect_policy(self):
        CONFIG["proxy"] = "http://127.0.0.1:7890"
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b"x" * (monitor.MAX_RESPONSE_BYTES + 1))
        with mock.patch.object(monitor, "load_cookie"), \
             mock.patch("gemini_web2api.multimodal._cached_page_tokens", return_value={}), \
             mock.patch.object(monitor, "_build_headers", return_value={"Cookie": self.cookie}), \
             mock.patch.object(monitor.urllib.request, "build_opener", return_value=opener) as build:
            with self.assertRaises(monitor.ModelResponseError):
                monitor.fetch_account_models()
        self.assertEqual(build.call_args.args[0].proxies, {"http": CONFIG["proxy"], "https": CONFIG["proxy"]})
        opener.open.assert_called_once()
        self.assertIsNone(monitor.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.invalid"))

    def test_forced_monitor_logs_are_written_with_request_logs_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            CONFIG.update(log_requests=False, log_file=os.path.join(directory, "service.log"))
            gemini.log("request log suppressed")
            gemini.log("model_check status=unchanged", force=True)
            with open(CONFIG["log_file"]) as source:
                logged = source.read()
            self.assertIn("model_check", logged)
            self.assertNotIn("request log suppressed", logged)


if __name__ == "__main__":
    unittest.main()
