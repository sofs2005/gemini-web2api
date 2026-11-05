"""Attachment protocol and network boundaries, without live Gemini calls."""
import io
import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from gemini_web2api import gemini, multimodal
from gemini_web2api.config import CONFIG
from gemini_web2api.model_monitor import _scope
from tests.test_modular_sync import _decode_payload


def raw_reply(text):
    inner = [None, None, None, None, [["candidate", [text]] + [None] * 50]]
    return json.dumps([["wrb.fr", None, json.dumps(inner)]]) + "\n"


def raw_error(code):
    return json.dumps([["wrb.fr", None, None, None, None, [13, None, [[
        "type.googleapis.com/assistant.boq.bard.application.BardErrorInfo", [code]
    ]]]]]) + "\n"


class AttachmentTransportTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(CONFIG)
        CONFIG.update(cookie_file=None, auth_user="2", proxy="http://proxy.invalid:8080",
                      retry_attempts=3, log_requests=False, request_timeout_sec=30)
        self.patches = [
            mock.patch.object(gemini, "HAS_CURL_CFFI", True),
            mock.patch.object(gemini, "load_cookie", return_value=("SAPISID=synthetic", "synthetic")),
            mock.patch.object(multimodal, "_cached_page_tokens", return_value={"at": "fresh-token", "f_sid": "page-session"}),
            mock.patch.object(gemini, "curl_requests"),
            mock.patch.object(gemini.urllib.request, "urlopen", side_effect=AssertionError("unexpected real HTTP")),
        ]
        started = [patch.start() for patch in self.patches]
        self.tokens, self.requests = started[2:4]
        self.session = self.requests.Session.return_value
        self.response = self.session.post.return_value
        self.response.status_code = 200
        self.response.iter_content.return_value = [raw_reply("red square and blue circle").encode()]

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        CONFIG.clear()
        CONFIG.update(self.original)

    def test_current_attachments_keep_model_routing_fresh_session_and_request_id(self):
        header = [None] * 17
        header[4], header[11], header[14] = "model-id", 2, 8
        selection = {"scope": _scope("SAPISID=synthetic", "2"), "header": header}
        text = gemini.generate("describe", 8, 4, [("/uploaded/ref", "photo.jpg")],
                               {"_model_selection": selection}, budget_sec=12, max_attempts=1)
        self.assertEqual(text, "red square and blue circle")
        url = self.session.post.call_args.args[0]
        args = self.session.post.call_args.kwargs
        inner = _decode_payload(args["data"])
        self.assertEqual(inner[0][3], [[["/uploaded/ref"], "photo.jpg"]])
        self.assertEqual((len(inner), inner[79], inner[80]), (81, 8, 1))
        self.assertEqual(parse_qs(args["data"])["at"], ["fresh-token"])
        self.assertEqual(parse_qs(urlsplit(url).query)["f.sid"], ["page-session"])
        self.assertIn("/u/2/", url)
        self.assertEqual(json.loads(args["headers"]["x-goog-ext-525001261-jspb"]), header)
        self.assertEqual(json.loads(args["headers"]["x-goog-ext-525005358-jspb"]), [inner[59], 1])
        self.assertEqual(args["impersonate"], "chrome")
        self.assertEqual(args["proxy"], CONFIG["proxy"])
        self.assertFalse(args["allow_redirects"])
        self.assertLessEqual(args["timeout"], 12)
        self.assertLessEqual(self.tokens.call_args.kwargs["timeout"], 12)
        self.response.close.assert_called_once()
        self.session.close.assert_called_once()

    def test_image_generation_reuses_body_uuid_in_request_headers(self):
        request_uuid = "11111111-2222-4333-8444-555555555555"
        response = mock.MagicMock()
        response.text = "image-result"
        with mock.patch.object(gemini, "HAS_CURL_CFFI", True), \
                mock.patch.object(gemini.uuid, "uuid4", return_value=request_uuid), \
                mock.patch.object(multimodal, "_cached_page_tokens", return_value={"at": "token", "f_sid": "sid"}), \
                mock.patch.object(gemini, "_build_image_payload", return_value="body") as payload, \
                mock.patch.object(gemini, "_build_headers", return_value={}) as headers, \
                mock.patch.object(gemini, "_image_model_headers", return_value={}) as model_headers, \
                mock.patch.object(gemini, "_get_url", return_value="https://gemini.test/stream"), \
                mock.patch.object(gemini, "curl_requests") as requests:
            requests.post.return_value = response
            result = gemini._generate_image_raw_with_curl("draw")
        self.assertEqual(result, "image-result")
        payload.assert_called_once_with("draw", request_uuid, xsrf_token="token")
        headers.assert_called_once_with(request_uuid=request_uuid)
        model_headers.assert_called_once_with({"at": "token", "f_sid": "sid"}, request_uuid)

    def test_image_generation_reuses_uploaded_refs_in_request_body(self):
        request_uuid = "11111111-2222-4333-8444-555555555555"
        response = mock.MagicMock()
        response.text = "image-result"
        file_refs = [("/uploaded/source", "source.png"), ("/uploaded/mask", "mask.png")]
        with mock.patch.object(gemini, "HAS_CURL_CFFI", True), \
                mock.patch.object(gemini.uuid, "uuid4", return_value=request_uuid), \
                mock.patch.object(multimodal, "_cached_page_tokens", return_value={"at": "token", "f_sid": "sid"}), \
                mock.patch.object(gemini, "_build_image_payload", return_value="body") as payload, \
                mock.patch.object(gemini, "_build_headers", return_value={}), \
                mock.patch.object(gemini, "_image_model_headers", return_value={}), \
                mock.patch.object(gemini, "_get_url", return_value="https://gemini.test/stream"), \
                mock.patch.object(gemini, "curl_requests") as requests:
            requests.post.return_value = response
            result = gemini._generate_image_raw_with_curl("edit", file_refs=file_refs)
        self.assertEqual(result, "image-result")
        payload.assert_called_once_with(
            "edit", request_uuid, xsrf_token="token", file_refs=file_refs
        )

        inner = _decode_payload(gemini._build_image_payload(
            "edit the source", "REQUEST-UUID",
            file_refs=[("/uploaded/source", "source.png"), ("/uploaded/mask", "mask.png")],
        ))
        self.assertEqual(
            inner[0][3],
            [[['/uploaded/source'], "source.png"], [['/uploaded/mask'], "mask.png"]],
        )
        self.assertEqual((len(inner), inner[79], inner[80]), (97, 6, 1))

        text = _decode_payload(gemini._build_payload("hello", 1, 4))
        self.assertEqual(len(text), 102)
        self.assertIsNone(text[80])
        file = _decode_payload(gemini._build_payload("hello", 1, 4, ["/legacy"]))
        self.assertEqual(file[0][3], [[["/legacy"], "image.png"]])
        enhanced = _decode_payload(gemini._build_payload("hello", 3, 4, ["/ref"], {31: 2, 80: 3}))
        self.assertEqual((enhanced[31], enhanced[80]), (2, 3))

    def test_file_stream_returns_one_result_and_notifies_on_first_body_data(self):
        order = []

        def chunks(**_kwargs):
            yield b""
            order.append("body")
            yield raw_reply("red square and blue circle").encode()
            self.assertEqual(order, ["body", "notified"])

        self.response.iter_content.side_effect = chunks
        result = list(gemini.generate_stream("describe", 1, 4, ["/ref"],
                      on_first_response=lambda: order.append("notified")))
        self.assertEqual(result, ["red square and blue circle"])
        self.session.post.assert_called_once()

    def test_bard_rejections_are_not_retried(self):
        self.response.iter_content.return_value = [raw_error(1003).encode()]
        with self.assertRaisesRegex(gemini.GeminiUpstreamError, "1003"):
            gemini.generate("describe", 1, 4, ["/ref"])
        self.session.post.assert_called_once()
        self.response.close.assert_called_once()
        self.session.close.assert_called_once()

    def test_auth_and_rate_limit_errors_are_not_retried_or_notified(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.session.post.reset_mock()
                self.response.status_code = status
                callback = mock.Mock()
                with self.assertRaisesRegex(gemini.GeminiUpstreamError, str(status)):
                    gemini.generate("describe", 1, 4, ["/ref"], on_first_response=callback)
                self.session.post.assert_called_once()
                callback.assert_not_called()

    def test_budget_exhaustion_prevents_page_and_generation_requests(self):
        with self.assertRaises(TimeoutError):
            gemini.generate("describe", 1, 4, ["/ref"], budget_sec=0)
        self.tokens.assert_not_called()
        self.requests.Session.assert_not_called()

    def test_cancelled_file_read_closes_session_without_retry(self):
        with self.assertRaises(ConnectionAbortedError):
            gemini.generate("describe", 1, 4, ["/ref"],
                            cancelled=mock.Mock(side_effect=[False, False, True]))
        self.session.post.assert_called_once()
        self.response.close.assert_called_once()
        self.session.close.assert_called_once()

    def test_missing_cookie_tokens_or_dependency_sends_no_generation(self):
        with mock.patch.object(gemini, "load_cookie", return_value=("", None)):
            with self.assertRaisesRegex(RuntimeError, "cookie"):
                gemini.generate("describe", 1, 4, ["/ref"])
        self.tokens.assert_not_called()
        self.tokens.return_value = {}
        with self.assertRaisesRegex(RuntimeError, "session is unavailable"):
            gemini.generate("describe", 1, 4, ["/ref"])
        with mock.patch.object(gemini, "HAS_CURL_CFFI", False):
            with self.assertRaisesRegex(RuntimeError, "curl_cffi"):
                gemini.generate("describe", 1, 4, ["/ref"])
        self.requests.Session.assert_not_called()

    def test_wrong_model_scope_fails_before_fetching_page(self):
        with self.assertRaisesRegex(RuntimeError, "different session"):
            gemini.generate("describe", 1, 4, ["/ref"],
                            {"_model_selection": {"scope": "wrong", "header": []}})
        self.tokens.assert_not_called()
        self.requests.Session.assert_not_called()

    def test_text_and_stream_structured_errors_stop_after_one_attempt(self):
        CONFIG["proxy"] = None
        with mock.patch.object(gemini.urllib.request, "urlopen", return_value=io.BytesIO(raw_error(1003).encode())) as request:
            with self.assertRaises(gemini.GeminiUpstreamError):
                gemini.generate("hello", 1, 4)
            request.assert_called_once()
        client = mock.MagicMock()
        client.stream.return_value.__enter__.return_value.iter_text.return_value = [raw_error(1003)]
        with mock.patch.object(gemini, "HAS_HTTPX", True), mock.patch.object(gemini, "_get_httpx_client", return_value=client):
            with self.assertRaises(gemini.GeminiUpstreamError):
                list(gemini.generate_stream("hello", 1, 4))
        client.stream.assert_called_once()


class PageTokenTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(CONFIG)
        CONFIG.update(cookie_file=None, auth_user="2", proxy=None, log_requests=False)
        self.cache = dict(multimodal._page_tokens_cache)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original)
        multimodal._page_tokens_cache.clear()
        multimodal._page_tokens_cache.update(self.cache)

    def test_tokens_use_account_and_decode_json_escapes_with_legacy_fallback(self):
        for token_key in ("SNlM0e", "thykhd"):
            with self.subTest(token_key=token_key):
                response = io.BytesIO(json.dumps({"FdrFJe": "sid", token_key: "token\\escaped"}).encode())
                with mock.patch.object(multimodal, "HAS_CURL_CFFI", False), mock.patch.object(multimodal.urllib.request, "urlopen", return_value=response) as request:
                    tokens = multimodal._get_page_tokens(timeout=5)
                self.assertEqual(tokens, {"f_sid": "sid", "at": "token\\escaped"})
                outgoing = request.call_args.args[0]
                self.assertEqual(outgoing.full_url, "https://gemini.google.com/u/2/app")
                self.assertEqual(outgoing.get_header("X-goog-authuser"), "2")
                self.assertEqual(outgoing.get_header("Accept-encoding"), "identity")
                self.assertEqual(request.call_args.kwargs["timeout"], 5)
                self.assertTrue(response.closed)

    def test_page_tokens_accept_current_gemini_web_build(self):
        html = b'"FdrFJe":"sid","SNlM0e":"token","boq_gemini-web-uiserver_20261014.17_p0"'
        with mock.patch.object(multimodal, "HAS_CURL_CFFI", False), mock.patch.object(multimodal.urllib.request, "urlopen", return_value=io.BytesIO(html)):
            tokens = multimodal._get_page_tokens(timeout=5)
        self.assertEqual(tokens, {"f_sid": "sid", "at": "token"})
        self.assertEqual(CONFIG["gemini_bl"], "boq_gemini-web-uiserver_20261014.17_p0")

    def test_token_cache_does_not_cross_accounts_and_force_refresh_works(self):
        multimodal._page_tokens_cache.update(tokens={}, ts=0, scope=None)
        with mock.patch.object(multimodal, "_get_page_tokens", side_effect=[{"at": "one"}, {"at": "two"}, {"at": "three"}]) as fetch:
            first = multimodal._cached_page_tokens()
            first["at"] = "mutated"
            self.assertEqual(multimodal._cached_page_tokens()["at"], "one")
            CONFIG["auth_user"] = "3"
            self.assertEqual(multimodal._cached_page_tokens()["at"], "two")
            self.assertEqual(multimodal._cached_page_tokens(max_age=0)["at"], "three")
            self.assertEqual(fetch.call_count, 3)


class RemoteImageTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(CONFIG)
        CONFIG.update(log_requests=False, proxy="http://ignored.invalid")
        self.patches = [
            mock.patch.object(multimodal, "HAS_CURL_CFFI", True),
            mock.patch.object(multimodal, "CurlOpt"),
            mock.patch.object(multimodal.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
            mock.patch.object(multimodal, "curl_requests"),
        ]
        started = [patch.start() for patch in self.patches]
        self.curl_opt, self.requests = started[1], started[3]
        self.session = self.requests.Session.return_value
        self.response = self.session.get.return_value
        self.response.status_code = 200
        self.response.headers = {"Content-Type": "image/png"}
        self.response.iter_content.return_value = [b"\x89PNG\r\n\x1a\n"]

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        CONFIG.clear()
        CONFIG.update(self.original)

    def test_download_pins_dns_bypasses_proxy_and_closes_resources(self):
        self.assertEqual(multimodal.fetch_image_bytes("https://example.com/test.png"), b"\x89PNG\r\n\x1a\n")
        args = self.requests.Session.call_args.kwargs
        self.assertFalse(args["trust_env"])
        self.assertEqual(args["curl_options"][self.curl_opt.RESOLVE], ["example.com:443:93.184.216.34"])
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])
        self.response.close.assert_called_once()
        self.session.close.assert_called_once()

    def test_rejects_private_addresses_http_and_credentials_before_network(self):
        for url in ("https://127.0.0.1/a", "https://[::1]/a", "https://169.254.169.254/a",
                    "http://example.com/a", "https://user:pass@example.com/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                multimodal.fetch_image_bytes(url)
        self.requests.Session.assert_not_called()

    def test_redirect_to_private_host_is_blocked(self):
        self.response.status_code = 302
        self.response.headers = {"Location": "https://127.0.0.1/private"}
        with self.assertRaisesRegex(ValueError, "not public"):
            multimodal.fetch_image_bytes("https://example.com/image")
        self.session.get.assert_called_once()
        self.response.close.assert_called_once()
        self.session.close.assert_called_once()

    def test_rejects_mime_mismatch_and_oversized_stream(self):
        self.response.headers = {"Content-Type": "image/jpeg"}
        with self.assertRaisesRegex(ValueError, "does not match"):
            multimodal.fetch_image_bytes("https://example.com/a")
        self.response.headers = {"Content-Type": "image/png"}
        with mock.patch.object(multimodal, "_MAX_REMOTE_IMAGE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "size limit"):
                multimodal.fetch_image_bytes("https://example.com/a")
