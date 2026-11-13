"""Verify discovered model exposure and request routing without upstream traffic."""
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs

from gemini_web2api import gemini, model_monitor as monitor, models
from gemini_web2api.config import CONFIG
from gemini_web2api.server import GeminiHandler, ThreadedServer
from tests.test_model_monitor import row, wire

ARGON_ID = "95a4c0abcba24364"
FLASH_ID = "56fdd199312815e2"
PRO_ID = "e6fa609c3fa255c0"
LITE_ID = "8c46e95b1a07cecc"


def payload(body):
    form = parse_qs(body.decode() if isinstance(body, bytes) else body)
    return json.loads(json.loads(form["f.req"][0])[1])


def promo_row(model_id, promo, category, number, label):
    """Upstream layout where `display_name` carries marketing copy, not the model name."""
    item = row(model_id, promo, category, number)
    item[2] = item[12] = label
    return item


class DynamicModelTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)
        self.original_cache = dict(gemini._cookie_cache)
        self.directory = tempfile.TemporaryDirectory()
        self.auth = os.path.join(self.directory.name, "auth.json")
        self.state = os.path.join(self.directory.name, "models.json")
        self.cookie = "__Secure-1PSID=synthetic-session; SAPISID=synthetic"
        with open(self.auth, "w") as output:
            json.dump({"cookie": self.cookie, "sapisid": "synthetic", "xsrf_token": "synthetic-token", "auth_user": "1"}, output)
        CONFIG.update(cookie_file=self.auth, model_snapshot_file=self.state, log_requests=False,
                      proxy=None, retry_attempts=1, api_keys=[], client_allowlist_file=None,
                      default_model="gemini-3.6-flash", session_hashes={}, auto_cache=False)
        self.rows = [row(FLASH_ID, "3.8 Flash", "Flash", 1), row(LITE_ID, "3.5 Flash-Lite", "Flash-Lite", 6),
                     row(ARGON_ID, "4 Argon", "Argon", 8), row(PRO_ID, "3.1 Pro", "Pro", 3)]
        self.write_snapshot()

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)
        gemini._cookie_cache.clear()
        gemini._cookie_cache.update(self.original_cache)
        self.directory.cleanup()

    def write_snapshot(self, rows=None, tiers=None, flags=None, scope=None):
        snapshot = monitor.parse_model_response(wire(self.rows if rows is None else rows, tiers=tiers or [8], flags=flags))
        snapshot.update(scope=scope or monitor._scope(self.cookie, "1"), checked_at="2026-10-14T08:18:45Z")
        monitor._write_state(self.state, {"schema_version": 1, "last_attempt_at": 1791965925,
                                        "last_status": "baseline", "snapshot": snapshot, "last_change": None})

    def test_registry_exposes_actual_models_including_argon_without_rpc(self):
        with mock.patch.object(monitor, "fetch_account_models") as fetch:
            listed = models.get_models()
        self.assertEqual(set(listed), {"gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-4-argon", "gemini-3.1-pro"})
        fetch.assert_not_called()
        selected = models.resolve_model("gemini-4-argon")
        self.assertEqual(selected[:4], ("gemini-4-argon", 8, 4, None))
        header = selected[4]["_model_selection"]["header"]
        self.assertEqual(header[4], ARGON_ID)
        self.assertEqual(header[11], 2)
        self.assertEqual(header[-3], 8)
        self.assertEqual(len(header), 17)

    def test_aliases_and_legacy_categories_select_corresponding_live_models(self):
        for alias, expected, number in (("gemini-4", "gemini-4-argon", 8), ("gemini4", "gemini-4-argon", 8),
                                        ("gemini-argon", "gemini-4-argon", 8), (ARGON_ID, "gemini-4-argon", 8),
                                        ("gemini-3.6-flash", "gemini-3.8-flash", 1),
                                        ("gemini-flash-lite", "gemini-3.5-flash-lite", 6),
                                        ("gemini-3.1-pro-enhanced", "gemini-3.1-pro", 3)):
            with self.subTest(alias=alias):
                chosen = models.resolve_model(alias)
                self.assertEqual(chosen[:2], (expected, number))
                self.assertIsNone(chosen[3])
        enhanced = models.resolve_model("gemini-3.1-pro-enhanced")[4]
        self.assertEqual((enhanced[31], enhanced[80]), (2, 3))
        self.assertEqual(models.resolve_model("gemini-4-argon@think=0")[2], 0)
        self.assertIsNotNone(models.resolve_model("gemini-4-argon@think=bad")[3])
        self.assertIsNotNone(models.resolve_model(None)[3])
        self.assertEqual(models.resolve_model("client-default")[0], "gemini-3.8-flash")

    def test_missing_removed_and_wrong_account_models_fail_without_fallback(self):
        self.assertIsNotNone(models.resolve_model("gemini-9-pro")[3])
        self.assertIsNotNone(models.resolve_model("gemini-3")[3])  # Ambiguous major alias.
        self.write_snapshot([item for item in self.rows if item[0] != ARGON_ID])
        self.assertIsNotNone(models.resolve_model("gemini-4-argon")[3])
        self.assertIsNotNone(models.resolve_model(ARGON_ID)[3])
        self.write_snapshot(scope="other-session")
        self.assertEqual(models.get_models(), models.MODELS)
        self.assertIsNotNone(models.resolve_model("gemini-4-argon")[3])
        os.unlink(self.state)
        self.assertIsNotNone(models.resolve_model("gemini-4-argon")[3])

    def test_registry_refreshes_when_snapshot_changes_without_restart(self):
        self.assertIn("gemini-4-argon", models.get_models())
        new_rows = [row(ARGON_ID, "4.1 Argon", "Argon", 8)]
        self.write_snapshot(new_rows)
        self.assertEqual(set(models.get_models()), {"gemini-4.1-argon"})
        self.assertEqual(models.resolve_model("gemini-argon")[0], "gemini-4.1-argon")

    def test_marketing_copy_is_not_used_as_the_model_name(self):
        self.write_snapshot([
            promo_row(FLASH_ID, "Everyday help", "4 Hydrogen", 1, "4 Hydrogen"),
            promo_row(LITE_ID, "Quick answers", "Flash-Lite", 6, "3.5 Flash-Lite"),
            promo_row(ARGON_ID, "Most complex work", "Carbon", 8, "4 Carbon"),
            promo_row(PRO_ID, "Detailed research", "Pro", 3, "3.1 Pro"),
        ])
        self.assertEqual(set(models.get_models()),
                         {"gemini-4-hydrogen", "gemini-3.5-flash-lite", "gemini-4-carbon", "gemini-3.1-pro"})
        # Clients already pinned to the promotional slug keep resolving.
        self.assertEqual(models.resolve_model("gemini-everyday-help")[0], "gemini-4-hydrogen")
        self.assertEqual(models.resolve_model("gemini-most-complex-work")[0], "gemini-4-carbon")
        self.assertEqual(models.resolve_model("gemini-detailed-research")[0], "gemini-3.1-pro")
        # Renamed families still honour the legacy aliases through the mode number.
        self.assertEqual(models.resolve_model("gemini-3.6-flash")[0], "gemini-4-hydrogen")
        self.assertEqual(models.resolve_model("gemini-flash-lite")[0], "gemini-3.5-flash-lite")
        self.assertEqual(models.resolve_model("gemini-3.1-pro")[0], "gemini-3.1-pro")
        self.assertEqual(models.resolve_model("gemini-4-carbon")[:2], ("gemini-4-carbon", 8))

    def test_renamed_model_id_keeps_old_labels_resolvable(self):
        self.write_snapshot([promo_row(ARGON_ID, "代码与分析", "Argon", 8, "4 Argon")])
        self.assertEqual(set(models.get_models()), {"gemini-4-argon"})
        previous = monitor._read_state(self.state)["snapshot"]
        renamed = monitor.parse_model_response(wire([promo_row(ARGON_ID, "高难任务", "Carbon", 8, "4 Carbon")]))
        renamed.update(scope=previous["scope"], checked_at="2026-11-13T00:00:00Z")
        monitor.carry_known_labels(renamed, previous)
        monitor._write_state(self.state, {"schema_version": 1, "last_attempt_at": 1, "last_status": "changed",
                                          "snapshot": renamed, "last_change": None})
        self.assertEqual(set(models.get_models()), {"gemini-4-carbon"})
        self.assertEqual(models.resolve_model("gemini-4-carbon")[0], "gemini-4-carbon")
        self.assertEqual(models.resolve_model("gemini-4-argon")[0], "gemini-4-carbon")

    def test_sparse_capacity_field_and_duplicate_names(self):
        self.write_snapshot(tiers=[22])
        header = models.resolve_model("gemini-4-argon")[4]["_model_selection"]["header"]
        self.assertIsNone(header[11])
        self.assertEqual(header[12], 2)
        self.assertEqual(header[-3], 8)
        self.assertEqual(len(header), 18)
        self.write_snapshot([row("first-id", "4 Argon", "Argon", 8), row("second-id", "4 Argon", "Argon", 9)])
        self.assertEqual(set(models.get_models()), {"gemini-4-argon-first-id", "gemini-4-argon-second-id"})
        self.assertIsNotNone(models.resolve_model("gemini-4")[3])

    def test_corrupt_snapshot_uses_legacy_list_and_private_route_is_not_in_payload(self):
        selected = models.resolve_model("gemini-4-argon")
        body = gemini._build_payload("offline", selected[1], selected[2], extra_fields=selected[4])
        self.assertEqual(payload(body)[79], 8)
        self.assertNotIn("_model_selection", body)
        self.assertNotIn(selected[4]["_model_selection"]["scope"], body)
        with open(self.state, "w") as output:
            output.write("not JSON")
        self.assertEqual(models.get_models(), models.MODELS)
        self.assertIsNotNone(models.resolve_model("gemini-4-argon")[3])

    def test_buffered_request_uses_full_header_and_number_over_legacy_hash(self):
        selected = models.resolve_model("gemini-4-argon")
        with mock.patch.object(gemini.urllib.request, "urlopen", return_value=io.BytesIO(b"")) as request, \
             mock.patch.object(gemini, "extract_response_text", return_value="offline"):
            self.assertEqual(gemini.generate("offline", selected[1], selected[2], extra_fields=selected[4],
                                             session_hash="legacy-hash"), "offline")
        outgoing = request.call_args.args[0]
        header = json.loads(outgoing.get_header("X-goog-ext-525001261-jspb"))
        self.assertEqual((header[4], header[11], header[-3]), (ARGON_ID, 2, 8))
        self.assertEqual(outgoing.get_header("X-goog-ext-73010990-jspb"), "[0,0,0]")
        self.assertEqual(payload(outgoing.data)[79], 8)
        self.assertIn("/u/1/", outgoing.full_url)
        request.assert_called_once()

    def test_streaming_request_uses_same_model_header_and_payload_number(self):
        selected = models.resolve_model("gemini-4-argon")
        client = mock.MagicMock()
        response = client.stream.return_value.__enter__.return_value
        response.iter_text.return_value = iter(())
        with mock.patch.object(gemini, "HAS_HTTPX", True), \
             mock.patch.object(gemini, "_get_httpx_client", return_value=client):
            self.assertEqual(list(gemini.generate_stream("offline", selected[1], selected[2], extra_fields=selected[4])), [])
        outgoing = client.stream.call_args.kwargs
        header = json.loads(outgoing["headers"]["x-goog-ext-525001261-jspb"])
        self.assertEqual((header[4], header[11], header[-3]), (ARGON_ID, 2, 8))
        self.assertEqual(payload(outgoing["content"])[79], 8)

    def test_account_change_between_resolving_and_generating_sends_no_request(self):
        selected = models.resolve_model("gemini-4-argon")
        with open(self.auth, "w") as output:
            json.dump({"cookie": self.cookie.replace("synthetic-session", "new-session"), "auth_user": "1"}, output)
        gemini._cookie_cache.update(mtime=-1)
        with mock.patch.object(gemini.urllib.request, "urlopen") as request:
            with self.assertRaisesRegex(RuntimeError, "different session"):
                gemini.generate("offline", selected[1], selected[2], extra_fields=selected[4])
        request.assert_not_called()

    def test_downstream_endpoints_expose_and_resolve_dynamic_model(self):
        server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            def get(path):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", path)
                response = connection.getresponse()
                body = json.loads(response.read())
                connection.close()
                return response.status, body

            with mock.patch.object(monitor, "fetch_account_models") as fetch:
                status, body = get("/v1/models")
                self.assertEqual(status, 200)
                self.assertIn("gemini-4-argon", [item["id"] for item in body["data"]])
                status, body = get("/v1beta/models")
                self.assertEqual(status, 200)
                self.assertIn("models/gemini-4-argon", [item["name"] for item in body["models"]])
                status, body = get("/")
                self.assertEqual(status, 200)
                self.assertIn("gemini-4-argon", body["models"])
                fetch.assert_not_called()
            with mock.patch("gemini_web2api.server.generate", return_value="offline") as generate:
                for path, data in (("/v1/chat/completions", {"model": "gemini-4-argon", "messages": [{"role": "user", "content": "offline"}]}),
                                   ("/v1/responses", {"model": "gemini-4-argon", "input": "offline"}),
                                   ("/v1beta/models/gemini-4-argon:generateContent", {"contents": [{"parts": [{"text": "offline"}]}]})):
                    with self.subTest(path=path):
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                        connection.request("POST", path, json.dumps(data), {"Content-Type": "application/json"})
                        response = connection.getresponse()
                        response.read()
                        connection.close()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(generate.call_args.args[1], 8)
                        self.assertEqual(generate.call_args.args[4]["_model_selection"]["header"][4], ARGON_ID)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
