import http.client
import base64
import io
import json
import os
import struct
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api import gemini, rotation
from gemini_web2api.cache import CACHE
from gemini_web2api.gemini import _build_payload
from gemini_web2api.server import (
    GeminiHandler, ThreadedServer, _classify_response_quality, _common_actual_size,
    _crop_edit_prompt, _edit_prompt, _image_budget_remaining,
    _image_request_deadline, _openai_error_payload,
)
from gemini_web2api.image_ops import ImageOpsUnavailable, choose_crop_box, mask_geometry
from gemini_web2api.tools import google_contents_to_prompt, messages_to_prompt, parse_tool_calls


def _decode_payload(payload):
    outer = json.loads(parse_qs(payload)["f.req"][0])
    return json.loads(outer[1])


def _decode_sse(body):
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event_type = next(
            (line[len("event: "):] for line in lines if line.startswith("event: ")),
            None,
        )
        data = next(
            (line[len("data: "):] for line in lines if line.startswith("data: ")),
            None,
        )
        if event_type and data:
            events.append((event_type, json.loads(data)))
    return events


def _png_bytes(width=2, height=2, alpha=True):
    color_type = 6 if alpha else 2
    pixel = b"\xff\x00\x00\xff" if alpha else b"\xff\x00\x00"
    raw = b"".join(b"\x00" + pixel * width for _ in range(height))

    def chunk(kind, data):
        payload = kind + data
        return (
            struct.pack(">I", len(data)) + payload
            + struct.pack(">I", zlib.crc32(payload) & 0xffffffff)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _mask_png(width=2, height=2, transparent=None):
    """Build an RGBA PNG with a fully transparent rectangle."""
    rows = []
    for y in range(height):
        pixels = bytearray()
        for x in range(width):
            editable = (
                transparent is not None
                and transparent[0] <= x < transparent[2]
                and transparent[1] <= y < transparent[3]
            )
            pixels += bytes((255, 0, 0, 0 if editable else 255))
        rows.append(b"\x00" + bytes(pixels))
    raw = b"".join(rows)

    def chunk(kind, data):
        payload = kind + data
        return (
            struct.pack(">I", len(data)) + payload
            + struct.pack(">I", zlib.crc32(payload) & 0xffffffff)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "auth.json")
        with open(self.path, "w") as out:
            json.dump({"cookie": "SAPISID=keep; __Secure-1PSIDTS=old; __Secure-1PSIDCC=old-cc",
                       "sapisid": "keep", "xsrf_token": "token"}, out)
        os.chmod(self.path, 0o600)

    def tearDown(self):
        self.directory.cleanup()

    def test_rotation_preserves_metadata_and_writes_private_json(self):
        response = mock.MagicMock()
        response.headers.get_all.return_value = [
            "__Secure-1PSIDTS=new; Secure; HttpOnly",
            "__Secure-1PSIDCC=new-cc; Secure; HttpOnly",
            "UNRELATED=ignored; Secure",
        ]
        with mock.patch.object(rotation.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            self.assertEqual(rotation.rotate_cookie_file(self.path), "updated_2")
            request = urlopen.call_args.args[0]
            self.assertEqual(request.data, rotation.ROTATE_BODY)
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 25)
        with open(self.path) as source:
            result = json.load(source)
        self.assertEqual(result["xsrf_token"], "token")
        self.assertEqual(result["sapisid"], "keep")
        self.assertIn("__Secure-1PSIDTS=new", result["cookie"])
        self.assertIn("__Secure-1PSIDCC=new-cc", result["cookie"])
        self.assertNotIn("UNRELATED", result["cookie"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_rotation_does_not_overwrite_new_export(self):
        response = mock.MagicMock()
        response.headers.get_all.return_value = ["__Secure-1PSIDTS=rotated; Secure"]

        def new_export(*_args, **_kwargs):
            with open(self.path, "w") as out:
                json.dump({"cookie": "SAPISID=fresh", "xsrf_token": "fresh"}, out)
            os.utime(self.path, ns=(os.stat(self.path).st_atime_ns,
                                    os.stat(self.path).st_mtime_ns + 1_000_000_000))
            return mock.MagicMock(__enter__=lambda _: response, __exit__=lambda *_: None)

        with mock.patch.object(rotation.urllib.request, "urlopen", side_effect=new_export):
            self.assertEqual(rotation.rotate_cookie_file(self.path), "file_changed")
        with open(self.path) as source:
            self.assertEqual(json.load(source)["xsrf_token"], "fresh")
    def test_rotation_worker_health(self):
        original_config = dict(CONFIG)
        try:
            CONFIG.update(cookie_rotation_interval_sec=300, cookie_file=self.path)
            with mock.patch.object(rotation, "_rotation_thread", None):
                self.assertFalse(rotation.rotation_worker_alive())
            with mock.patch.object(rotation, "_rotation_thread") as thread:
                thread.is_alive.return_value = True
                self.assertTrue(rotation.rotation_worker_alive())
        finally:
            CONFIG.clear()
            CONFIG.update(original_config)


class AuthSyncTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.auth_file = os.path.join(self.tmpdir.name, "auth.json")
        CONFIG.update(cookie_file=self.auth_file, auth_user=None, xsrf_token="old-token",
                      gemini_bl="old-build", proxy=None, retry_attempts=1,
                      log_requests=False)
        self.write_auth("first-token", "first-build", "1")

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)
        gemini._cookie_cache.update(str="", sapisid=None, mtime=0, path=None)
        self.tmpdir.cleanup()

    def write_auth(self, token, build, user):
        with open(self.auth_file, "w") as out:
            json.dump({"cookie": "SAPISID=test; __Secure-1PSID=session",
                       "sapisid": "test", "xsrf_token": token,
                       "gemini_bl": build, "auth_user": user}, out)
        os.utime(self.auth_file, None)

    def test_generate_uses_export_metadata_on_first_request_and_after_update(self):
        requests = []

        def urlopen(request, **_kwargs):
            requests.append(request)
            return io.BytesIO(b"")

        with mock.patch.object(gemini.urllib.request, "urlopen", side_effect=urlopen):
            gemini.generate("hello", 1, 4)
            self.write_auth("second-token", "second-build", "2")
            gemini.generate("hello", 1, 4)

        for request, token, build, user in zip(
            requests, ("first-token", "second-token"),
            ("first-build", "second-build"), ("1", "2")
        ):
            self.assertEqual(parse_qs(request.data.decode())["at"], [token])
            self.assertEqual(parse_qs(urlsplit(request.full_url).query)["bl"], [build])
            self.assertIn(f"/u/{user}/", request.full_url)
            self.assertIn(f"/u/{user}/", request.get_header("Referer"))

    def test_buffered_text_uses_identity_encoding(self):
        requests = []

        def urlopen(request, **_kwargs):
            requests.append(request)
            return io.BytesIO(b"")

        with mock.patch.object(gemini.urllib.request, "urlopen", side_effect=urlopen):
            gemini.generate("hello", 1, 4)
        self.assertEqual(requests[0].get_header("Accept-encoding"), "identity")

    def test_generate_refreshes_export_token_after_400(self):
        CONFIG.update(retry_attempts=2, retry_delay_sec=0)
        requests = []

        def urlopen(request, **_kwargs):
            requests.append(request)
            if len(requests) == 1:
                raise gemini.urllib.error.HTTPError(
                    "https://example.invalid", 400, "bad request", {}, None)
            return io.BytesIO(b"fresh response")

        with mock.patch.object(gemini.urllib.request, "urlopen", side_effect=urlopen), \
             mock.patch("gemini_web2api.multimodal._cached_page_tokens",
                        return_value={"at": "fresh-token"}), \
             mock.patch.object(gemini, "_retry_delay", return_value=0), \
             mock.patch.object(gemini, "extract_response_text", return_value="ok"):
            self.assertEqual(gemini.generate("hello", 1, 4), "ok")

        self.assertEqual(
            parse_qs(requests[0].data.decode())["at"], ["first-token"])
        self.assertEqual(
            parse_qs(requests[1].data.decode())["at"], ["fresh-token"])
        for request in requests:
            self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertEqual(CONFIG["xsrf_token"], "fresh-token")

    def test_stream_refreshes_export_token_after_400(self):
        CONFIG.update(retry_attempts=2, retry_delay_sec=0)
        client = mock.MagicMock()
        first = mock.MagicMock()
        first.raise_for_status.side_effect = type(
            "StatusError", (RuntimeError,), {})()
        first.raise_for_status.side_effect.response = mock.Mock(status_code=400)
        first.iter_text.return_value = iter(())
        second = mock.MagicMock()
        second.raise_for_status.return_value = None
        second.iter_text.return_value = iter(())
        client.stream.return_value.__enter__.side_effect = [first, second]

        with mock.patch.object(gemini, "HAS_HTTPX", True), \
             mock.patch.object(gemini, "_get_httpx_client", return_value=client), \
             mock.patch("gemini_web2api.multimodal._cached_page_tokens",
                        return_value={"at": "fresh-token"}), \
             mock.patch.object(gemini, "_retry_delay", return_value=0):
            self.assertEqual(list(gemini.generate_stream("hello", 1, 4)), [])

        first_body = client.stream.call_args_list[0].kwargs["content"]
        second_body = client.stream.call_args_list[1].kwargs["content"]
        self.assertEqual(parse_qs(first_body)["at"], ["first-token"])
        self.assertEqual(parse_qs(second_body)["at"], ["fresh-token"])
        for call in client.stream.call_args_list:
            self.assertEqual(call.kwargs["headers"]["Accept-Encoding"], "identity")

    def test_stream_uses_export_metadata_before_building_request(self):
        client = mock.MagicMock()
        response = client.stream.return_value.__enter__.return_value
        response.iter_text.return_value = iter(())
        with mock.patch.object(gemini, "HAS_HTTPX", True), \
             mock.patch.object(gemini, "_get_httpx_client", return_value=client):
            self.assertEqual(list(gemini.generate_stream("hello", 1, 4)), [])
        args, kwargs = client.stream.call_args
        self.assertEqual(parse_qs(kwargs["content"])["at"], ["first-token"])
        self.assertEqual(parse_qs(urlsplit(args[1]).query)["bl"], ["first-build"])
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertIn("/u/1/", args[1])

    def test_stream_flushes_unterminated_final_line(self):
        client = mock.MagicMock()
        response = client.stream.return_value.__enter__.return_value
        response.iter_text.return_value = iter(["tail"])
        with mock.patch.object(gemini, "HAS_HTTPX", True), \
             mock.patch.object(gemini, "_get_httpx_client", return_value=client), \
             mock.patch.object(gemini, "_extract_texts_from_line", return_value=["tail"]):
            self.assertEqual(list(gemini.generate_stream("hello", 1, 4)), ["tail"])

    def test_candidate_selection_rejects_mixed_complete_and_partial(self):
        complete = '```tool_call\n{"name":"read","arguments":{}}\n```'
        self.assertFalse(gemini._valid_tool_call_candidate(complete + '\n```tool_call\n{"name":'))
        for payload in ('[]', '{"name":3}', '{"name":"read","arguments":"bad"}'):
            self.assertFalse(gemini._valid_tool_call_candidate('```tool_call\n' + payload + '\n```'))

    def test_candidate_selection_rejects_incomplete_tool_call(self):
        with self.assertRaises(RuntimeError):
            gemini._select_response_text([
                '```tool_call\n{"name":"bash","arguments":{"command":"cat',
            ])

    def test_candidate_selection_uses_last_complete_tool_call(self):
        first = '```tool_call\n{"name":"bash","arguments":{"command":"one"}}\n```'
        second = '```tool_call\n{"name":"bash","arguments":{"command":"two"}}\n```'
        self.assertEqual(gemini._select_response_text([first, second]), second)

    def test_client_operation_selection_rejects_partial_final_snapshot(self):
        complete = '```client_operation\n{"name":"read","arguments":{"path":"a"}}\n```'
        partial = '```client_operation\n{"name":"read","arguments":{"path":"b'
        self.assertEqual(gemini._select_response_text([partial, complete]), complete)
        with self.assertRaises(RuntimeError):
            gemini._select_response_text([complete, partial])
        self.assertFalse(gemini._valid_tool_call_candidate(complete + '\n' + partial))

    def test_buffered_budget_limits_timeout_and_attempts(self):
        with mock.patch.object(gemini.urllib.request, "urlopen", side_effect=TimeoutError("slow")) as open_request:
            with self.assertRaises(TimeoutError):
                gemini.generate("hello", 1, 4, budget_sec=45, max_attempts=1)
        self.assertEqual(open_request.call_count, 1)
        self.assertLessEqual(open_request.call_args.kwargs["timeout"], 45)

    def test_buffered_cancel_closes_response(self):
        response = io.BytesIO(b"response")
        with mock.patch.object(gemini.urllib.request, "urlopen", return_value=response), \
             mock.patch.object(gemini, "extract_response_text", return_value="ok"):
            with self.assertRaises(ConnectionAbortedError):
                gemini.generate("hello", 1, 4, budget_sec=45, max_attempts=1,
                                cancelled=mock.Mock(side_effect=[False, True]))
        self.assertTrue(response.closed)

    def test_buffered_first_response_precedes_completion(self):
        for budget in (None, 45):
            with self.subTest(budget=budget):
                order = []
                chunks = iter([b"metadata without text", b"more", b""])
                response = mock.Mock()

                def read_chunk(_size):
                    order.append("read")
                    return next(chunks)

                response.read1.side_effect = read_chunk
                with mock.patch.object(gemini.urllib.request, "urlopen", return_value=response), \
                     mock.patch.object(gemini, "extract_response_text", side_effect=lambda _: order.append("parse") or "ok"):
                    result = gemini.generate(
                        "hello", 1, 4, budget_sec=budget,
                        on_first_response=lambda: order.append("first_data"),
                    )
                self.assertEqual(result, "ok")
                self.assertEqual(order, ["read", "first_data", "read", "read", "parse"])
                response.close.assert_called_once()

    def test_buffered_first_response_requires_successful_body_data(self):
        callback = mock.Mock()
        with mock.patch.object(gemini.urllib.request, "urlopen", return_value=io.BytesIO(b"")):
            gemini.generate("hello", 1, 4, on_first_response=callback)
        callback.assert_not_called()
        error = gemini.urllib.error.HTTPError("https://example.invalid", 401, "unauthorized", {}, None)
        with mock.patch.object(gemini.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gemini.urllib.error.HTTPError):
                gemini.generate("hello", 1, 4, on_first_response=callback)
        callback.assert_not_called()

    def test_stream_first_response_does_not_wait_for_text_or_newline(self):
        callback = mock.Mock()
        client = mock.MagicMock()
        response = client.stream.return_value.__enter__.return_value

        def chunks():
            callback.assert_not_called()
            yield ""
            callback.assert_not_called()
            yield ")]}'"
            callback.assert_called_once_with()
            yield "\nmetadata\n"

        response.iter_text.side_effect = chunks
        with mock.patch.object(gemini, "HAS_HTTPX", True), \
             mock.patch.object(gemini, "_get_httpx_client", return_value=client):
            self.assertEqual(list(gemini.generate_stream("hello", 1, 4, on_first_response=callback)), [])
        callback.assert_called_once_with()

    def test_stream_http_error_does_not_signal_first_response(self):
        callback = mock.Mock()
        client = mock.MagicMock()
        response = client.stream.return_value.__enter__.return_value
        response.raise_for_status.side_effect = RuntimeError("upstream HTTP error")
        with mock.patch.object(gemini, "HAS_HTTPX", True), \
             mock.patch.object(gemini, "_get_httpx_client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "upstream HTTP error"):
                list(gemini.generate_stream("hello", 1, 4, on_first_response=callback))
        callback.assert_not_called()
        response.iter_text.assert_not_called()

    def test_stream_does_not_retry_after_emitting_output(self):
        client = mock.MagicMock()
        response = client.stream.return_value.__enter__.return_value

        def interrupted():
            yield "partial\n"
            raise RuntimeError("connection interrupted")

        response.iter_text.side_effect = interrupted
        original_attempts = CONFIG["retry_attempts"]
        CONFIG["retry_attempts"] = 3
        try:
            with mock.patch.object(gemini, "HAS_HTTPX", True), \
                 mock.patch.object(gemini, "_get_httpx_client", return_value=client), \
                 mock.patch.object(gemini, "_extract_texts_from_line", return_value=["partial"]):
                with self.assertRaisesRegex(RuntimeError, "connection interrupted"):
                    list(gemini.generate_stream("hello", 1, 4))
            self.assertEqual(client.stream.call_count, 1)
        finally:
            CONFIG["retry_attempts"] = original_attempts



class PayloadPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def test_temporary_chats_default_to_disabled(self):
        self.assertIs(DEFAULT_CONFIG["temporary_chats"], False)

    def test_persistent_chat_payload(self):
        CONFIG["temporary_chats"] = False

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [2])
        self.assertIsNone(inner[45])

    def test_temporary_chat_payload(self):
        CONFIG["temporary_chats"] = True

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [1])
        self.assertEqual(inner[45], 1)

    def test_payload_includes_uploaded_image_refs(self):
        inner = _decode_payload(_build_payload("describe", 1, 4, ["/uploaded/image-ref"]))

        self.assertEqual(inner[0][0], "describe")
        self.assertEqual(inner[0][3], [[["/uploaded/image-ref"], "image.png"]])
        self.assertEqual(len(inner), 81)
        self.assertEqual(inner[80], 1)


class ContextCacheTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)
        self.tmpdir = tempfile.TemporaryDirectory()
        CONFIG["cache_db_path"] = os.path.join(self.tmpdir.name, "cache.sqlite3")

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)
        self.tmpdir.cleanup()

    def test_context_cache_create_read_and_delete(self):
        item = CACHE.create([{"role": "system", "content": "cached context"}], 60)

        cached = CACHE.get(item["id"])
        self.assertEqual(cached["messages"][0]["content"], "cached context")
        self.assertTrue(CACHE.delete(item["id"]))
        self.assertIsNone(CACHE.get(item["id"]))


class MessageParsingTests(unittest.TestCase):
    def test_code_guidance_follows_current_user_task(self):
        tools = [{"type": "function", "function": {
            "name": "read", "parameters": {"type": "object"},
        }}]
        for content in (
            "创建一个鹈鹕骑单车的svg动画",
            [{"type": "input_text", "text": "Review artifact.svg"}],
        ):
            with self.subTest(content=content):
                prompt, _ = messages_to_prompt([{"role": "user", "content": content}], tools)
                self.assertIn("# Code Artifact Task", prompt)
        prompt, _ = messages_to_prompt([
            {"role": "user", "content": "Create an SVG animation"},
            {"role": "assistant", "content": "Created artifact.svg"},
            {"role": "user", "content": "Read a.txt and b.txt"},
        ], tools)
        self.assertNotIn("# Code Artifact Task", prompt)
        self.assertIn("# Client Operation Protocol", prompt)
        self.assertNotIn("# Local Coding Agent Environment", prompt)
        self.assertNotIn("# Final Execution Rule", prompt)

    def test_operation_protocol_preserves_tool_choice_and_result_history(self):
        tools = [{"type": "function", "function": {
            "name": "read", "parameters": {"type": "object"},
        }}]
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "call_read", "function": {
                "name": "read", "arguments": '{"path":"a.txt"}',
            }}]},
            {"role": "tool", "tool_call_id": "call_read", "content": "ACTUAL_RESULT"},
            {"role": "user", "content": "Now read b.txt"},
        ]
        for choice in (None, "auto", "required", {"type": "function", "function": {"name": "read"}}):
            with self.subTest(choice=choice):
                prompt, _ = messages_to_prompt(messages, tools, choice)
                self.assertIn("[Tool result for read (call_id=call_read)]: ACTUAL_RESULT", prompt)
                self.assertIn("Now read b.txt", prompt)
                self.assertIn('```client_operation\n{"name":"tool_name","arguments":{}}', prompt)
                self.assertIn('```client_operation\n{"name": "read", "arguments": {"path":"a.txt"}}', prompt)
                self.assertNotIn("```tool_call", prompt)
                self.assertEqual("IMPORTANT:" in prompt, choice not in (None, "auto"))
                if choice not in (None, "auto"):
                    self.assertGreater(prompt.index("IMPORTANT:"), prompt.index("Now read b.txt"))
        prompt, _ = messages_to_prompt(messages, tools, "none")
        self.assertNotIn("# Client Operation Protocol", prompt)
        self.assertNotIn("Available tools:", prompt)

    def test_messages_to_prompt_extracts_openai_image_url_data_url(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(images, [(b"fake png", "image/png")])

    def test_messages_to_prompt_extracts_responses_input_image_url(self):
        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe"},
                {"type": "input_image", "image_url": "https://example.com/image.png"},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(images, [("https://example.com/image.png", "image/png")])

    def test_messages_to_prompt_ignores_malformed_image_data_url(self):
        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,%%%"}},
            ],
        }])

        self.assertEqual(prompt, "Describe")
        self.assertEqual(images, [])

    def test_response_quality_classifies_refusal_variants(self):
        self.assertEqual(
            _classify_response_quality(
                "create file", "I seem to be encountering an error. Can I try something else for you?", True
            ),
            "temporary_refusal",
        )
        self.assertEqual(
            _classify_response_quality(
                "create file", "```tool_call\n{\"name\":\"write\",\"arguments\":{}}\n```", True
            ),
            "normal",
        )
    def test_malformed_tool_call_is_not_silently_dropped(self):
        text = "before\n```tool_call\nnot valid json\n```\nafter"
        clean, calls = parse_tool_calls(text)
        self.assertEqual(calls, [])
        self.assertIn("not valid json", clean)
    def test_google_contents_to_prompt_extracts_inline_image_data(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, images = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": image_data}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe\n[Image attached]")
        self.assertEqual(images, [(b"fake png", "image/png")])

    def test_google_contents_to_prompt_ignores_malformed_inline_image_data(self):
        prompt, images = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": "%%%"}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe")
        self.assertEqual(images, [])


class StreamingEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.original_config = dict(CONFIG)
        CONFIG["api_keys"] = []
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    @mock.patch("gemini_web2api.gemini.HAS_CURL_CFFI", True)
    @mock.patch("gemini_web2api.multimodal._cached_page_tokens",
                return_value={"at": "token", "f_sid": "sid"})
    @mock.patch("gemini_web2api.gemini._build_image_payload", return_value=b"body")
    @mock.patch("gemini_web2api.gemini._build_headers", return_value={})
    @mock.patch("gemini_web2api.gemini._image_model_headers", return_value={})
    @mock.patch("gemini_web2api.gemini.curl_requests.post")
    def test_image_generation_budget_zero_skips_upstream_request(
        self, post, _model_headers, _headers, _payload, _tokens
    ):
        with self.assertRaises(TimeoutError):
            gemini._generate_image_raw_with_curl("a red fox", budget_sec=0)
        post.assert_not_called()

    def test_image_request_budget_helpers(self):
        self.assertIsNone(_image_budget_remaining(None))
        self.assertGreater(
            _image_budget_remaining(time.monotonic() + 10), 0)
        with self.assertRaises(TimeoutError):
            _image_budget_remaining(time.monotonic() - 1)
        with mock.patch.dict(CONFIG, {"image_request_budget_sec": 0}):
            self.assertIsNone(_image_request_deadline())
        with mock.patch.dict(CONFIG, {"image_request_budget_sec": 5}):
            self.assertGreater(_image_request_deadline(), time.monotonic())

        self.assertEqual(_common_actual_size([(2, 2), (2, 2)]), (2, 2))
        self.assertIsNone(_common_actual_size([(2, 2), (3, 3)]))
        self.assertIsNone(_common_actual_size([(2, 2), None]))
        self.assertIsNone(_common_actual_size([]))

    @mock.patch("gemini_web2api.server.generate_image_structured")
    def test_image_budget_exhaustion_returns_504(self, generate):
        with mock.patch(
            "gemini_web2api.server._image_request_deadline",
            return_value=time.monotonic() - 1,
        ):
            status, _, body = self.post_json("/v1/images/generations", {
                "prompt": "a red fox", "model": "gpt-image-1", "n": 2,
            })
        self.assertEqual(status, 504)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "server_error")
        self.assertEqual(error["code"], "request_timeout")
        generate.assert_not_called()

    def test_openai_error_payload_status_mapping(self):
        cases = {
            400: "invalid_request_error",
            401: "authentication_error",
            403: "permission_error",
            404: "invalid_request_error",
            422: "invalid_request_error",
            429: "rate_limit_error",
            500: "server_error",
            502: "server_error",
            503: "server_error",
        }
        for status, expected_type in cases.items():
            with self.subTest(status=status):
                error = _openai_error_payload(
                    "failure", status, param="model", code="example",
                    retryable=True)["error"]
                self.assertEqual(error, {
                    "type": expected_type,
                    "message": "failure",
                    "param": "model",
                    "code": "example",
                    "retryable": True,
                })

    def test_google_routes_keep_google_error_schema(self):
        CONFIG["api_keys"] = ["secret"]
        status, _, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:generateContent", {})
        self.assertEqual(status, 401)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], 401)
        self.assertEqual(error["message"], "invalid api key")
        self.assertEqual(error["status"], "UNAUTHENTICATED")
        self.assertNotIn("type", error)

        CONFIG["api_keys"] = []
        status, _, body = self.post_json("/v1beta/not-a-route", {})
        self.assertEqual(status, 404)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], 404)
        self.assertEqual(error["message"], "not found")
        self.assertEqual(error["status"], "NOT_FOUND")
        self.assertNotIn("type", error)

    def test_auth_and_404_use_openai_error_schema(self):
        CONFIG["api_keys"] = ["secret"]
        status, headers, body = self.post_json("/v1/chat/completions", {})
        self.assertEqual(status, 401)
        self.assertTrue(headers.get("x-request-id") or headers.get("X-Request-Id"))
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "authentication_error")
        self.assertEqual(error["message"], "invalid api key")
        self.assertIsNone(error["param"])
        self.assertEqual(error["code"], "invalid_api_key")

        CONFIG["api_keys"] = []
        status, _, body = self.post_json("/v1/not-a-route", {})
        self.assertEqual(status, 404)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["message"], "not found")
        self.assertIsNone(error["param"])
        self.assertEqual(error["code"], "not_found")

    @mock.patch("gemini_web2api.server.generate", side_effect=TimeoutError("budget exhausted"))
    def test_agent_timeout_returns_504(self, generate):
        CONFIG["agent_request_budget_sec"] = 300
        status, _, body = self.post_json("/v1/chat/completions", {
            "messages": [{"role": "user", "content": "read"}],
            "tools": [{"type": "function", "function": {"name": "read"}}],
        })
        self.assertEqual(status, 504)
        self.assertTrue(json.loads(body)["error"]["retryable"])
        self.assertEqual(generate.call_args.kwargs["max_attempts"], 1)
        self.assertEqual(generate.call_args.kwargs["budget_sec"], 300)

    @mock.patch("gemini_web2api.server.generate")
    def test_agent_busy_does_not_start_generation(self, generate):
        from gemini_web2api.server import _agent_slot
        self.assertTrue(_agent_slot.acquire(blocking=False))
        try:
            status, _, _ = self.post_json("/v1/chat/completions", {
                "messages": [{"role": "user", "content": "read"}],
                "tools": [{"type": "function", "function": {"name": "read"}}],
            })
        finally:
            _agent_slot.release()
        self.assertEqual(status, 503)
        generate.assert_not_called()

    @mock.patch("gemini_web2api.server.generate")
    def test_tool_schema_and_unknown_name_are_rejected(self, generate):
        tools = [{"type": "function", "function": {"name": "read", "parameters": {
            "type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}
        }}}]
        for name, args in (("unknown", {"path": "a"}), ("read", {}), ("read", {"path": 3})):
            generate.return_value = '```tool_call\n' + json.dumps({"name": name, "arguments": args}) + '\n```'
            for path, input_key in (("/v1/chat/completions", "messages"), ("/v1/responses", "input")):
                status, _, body = self.post_json(path, {
                    input_key: [{"role": "user", "content": "read"}], "tools": tools,
                })
                self.assertEqual(status, 502)
                error = json.loads(body)["error"]
                self.assertEqual(error["type"], "server_error")
                self.assertEqual(error["code"], "tool_call_parse_error")

    @mock.patch("gemini_web2api.server.generate")
    def test_client_operation_preserves_multiline_arguments_and_schema_validation(self, generate):
        tools = [{"type": "function", "function": {"name": "write", "parameters": {
            "type": "object", "required": ["path", "content"],
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        }}}]
        args = {"path": "test.svg", "content": '<svg>\n<circle title="中文"/>\n</svg>'}
        for path, input_key in (("/v1/chat/completions", "messages"), ("/v1/responses", "input")):
            with self.subTest(path=path):
                generate.return_value = '```client_operation\n' + json.dumps({
                    "name": "write", "arguments": args,
                }) + '\n```'
                status, _, body = self.post_json(path, {
                    input_key: [{"role": "user", "content": "write"}], "tools": tools,
                })
                self.assertEqual(status, 200)
                data = json.loads(body)
                if input_key == "messages":
                    function = data["choices"][0]["message"]["tool_calls"][0]["function"]
                else:
                    function = data["output"][0]
                self.assertEqual(function["name"], "write")
                self.assertEqual(json.loads(function["arguments"]), args)

                generate.return_value = '```client_operation\n' + json.dumps({
                    "name": "write", "arguments": {"path": "test.svg"},
                }) + '\n```'
                status, _, body = self.post_json(path, {
                    input_key: [{"role": "user", "content": "write"}], "tools": tools,
                })
                self.assertEqual(status, 502)
                error = json.loads(body)["error"]
                self.assertEqual(error["type"], "server_error")
                self.assertEqual(error["code"], "tool_call_parse_error")

    @mock.patch("gemini_web2api.server.generate", return_value="I seem to be encountering an error. Can I try something else for you?")
    def test_temporary_refusal_returns_error_on_both_endpoints(self, generate):
        for path, input_key in (("/v1/chat/completions", "messages"), ("/v1/responses", "input")):
            status, _, body = self.post_json(path, {
                input_key: [{"role": "user", "content": "read"}],
                "tools": [{"type": "function", "function": {"name": "read"}}],
            })
            self.assertEqual(status, 503)
            error = json.loads(body)["error"]
            self.assertEqual(error["type"], "server_error")
            self.assertEqual(error["code"], "upstream_temporary_refusal")
        self.assertEqual(_classify_response_quality("", 'Quoted: I seem to be encountering an error.', True), "normal")
        self.assertEqual(_classify_response_quality("", "I couldn't do that because it is unsafe.", True), "normal")

    def test_generated_image_loader_adds_d_i_variant_to_raw_rpc_url(self):
        from gemini_web2api.generated_image import _with_alr
        raw = "https://lh3.googleusercontent.com/gg-dl/signed-token"
        self.assertEqual(_with_alr(raw), raw + "=d-I?alr=yes")
        preview = "https://lh3.googleusercontent.com/gg-dl/signed-token=s1024-rj"
        self.assertEqual(_with_alr(preview), preview + "?alr=yes")
        mediator = "https://lh3.googleusercontent.com/rd-gg-dl/signed-token=s0-d-I"
        self.assertEqual(_with_alr(mediator), mediator + "?alr=yes")


        headers = gemini._build_full_size_rpc_headers({}, "A" * 36)
        header = json.loads(headers["x-goog-ext-525001261-jspb"])
        self.assertEqual(len(header), 20)
        self.assertEqual(header[8], [4, 5, 6, 8, 16, 19, 20] * 2)
        self.assertEqual(header[14:16], [6, 1])
        self.assertEqual(header[16], "A" * 36)
        self.assertEqual(header[17], [CONFIG["gemini_bl"]])
        self.assertEqual(headers["x-goog-ext-73010989-jspb"], "[0]")

    def test_gemini_browser_identity_headers_match_har(self):
        expected = {
            "x-browser-channel": "stable",
            "x-browser-copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
            "x-browser-validation": "YXjt/if5gRhK4M5qXvb22nUeBck=",
            "x-browser-year": "2026",
            "x-client-data": "CKmdygEIlaHLAQiGoM0B",
        }
        headers = gemini._build_headers()
        self.assertEqual({key: headers[key] for key in expected}, expected)
        self.assertEqual(headers["User-Agent"], CONFIG["google_user_agent"])
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded;charset=utf-8")
        self.assertEqual(headers["Referer"], "https://gemini.google.com/")
        self.assertEqual(headers["Sec-Fetch-Site"], "same-origin")
        from gemini_web2api.generated_image import _request_args
        with mock.patch("gemini_web2api.gemini.load_cookie", return_value=("SID=private", "sapisid")):
            mediator_args = _request_args(
                stream=True, url="https://lh3.google.com/rd-gg/mediator"
            )
            final_args = _request_args(
                stream=True, url="https://lh3.googleusercontent.com/rd-gg/final"
            )
        self.assertEqual(mediator_args["headers"], {"Cookie": "SID=private"})
        self.assertEqual(final_args["headers"], {})
        self.assertFalse(mediator_args["default_headers"])
        self.assertFalse(final_args["default_headers"])
        with self.assertRaises(TimeoutError):
            _request_args(stream=True, deadline=time.monotonic() - 1)

    def test_full_size_payload_reserializes_response_context_as_sparse_jspb(self):
        from gemini_web2api.generated_image import GeneratedImage
        response_context = [None] * 23
        response_context[0] = 22
        response_context[1] = "response prompt"
        response_context[16] = False
        response_context[18] = "imagen_default.ultra.complaintflow"
        response_context[20] = 3836647544
        response_context[22] = [None, True, [False]]
        image = GeneratedImage(
            "https://lh3.googleusercontent.com/generated",
            image_id="image-id", cid="c_1", rid="r_1", rcid="rc_1",
            rpc_payload={"signature": "sig", "context": response_context,
                         "placeholder_url": "http://googleusercontent.com/image_generation_content/0_1"},
        )
        payload = gemini._build_full_size_rpc_payload(image)
        context = payload[0][3]
        self.assertEqual(context[0:2], [22, "response prompt"])
        self.assertEqual(context[16], 0)
        self.assertEqual(context[18], "imagen_default.ultra.complaintflow")
        self.assertIsNone(context[20])
        self.assertEqual(context[22], [None, 1, [0]])


    def test_generated_image_id_matches_gemini_web_fallback(self):
        from gemini_web2api.generated_image import _generated_image_id
        self.assertEqual(
            _generated_image_id("rc_46d9372eef639b6b", 0, 0),
            "v5kua7v5kua7v5ku",
        )

        from gemini_web2api.generated_image import GeneratedImage
        context = [None] * 23
        context[0] = 22
        context[1] = "upstream prompt"
        context[16] = 0
        image = GeneratedImage(
            "https://lh3.googleusercontent.com/generated",
            image_id="v5kua7v5kua7v5ku",
            cid="c_1", rid="r_1", rcid="rc_1",
            rpc_payload={
                "signature": "image-signature",
                "context": context,
                "prompt": "a red circle",
                "session_id": "session-1",
                "placeholder_url": "http://googleusercontent.com/image_generation_content/0_783",
            },
        )
        payload = gemini._build_full_size_rpc_payload(image)
        self.assertEqual(payload[0][0][3][5], "image-signature")
        self.assertEqual(payload[0][1], [
            "http://googleusercontent.com/image_generation_content/0_783", 0
        ])
        self.assertEqual(payload[0][3][1], "upstream prompt")
        self.assertEqual(payload[0][3][16], 0)
        self.assertIsNone(payload[0][3][20])
        self.assertIsNone(payload[0][3][9])
        self.assertEqual(payload[0][8], "")
        self.assertEqual(payload[0][9], "v5kua7v5kua7v5ku")
        self.assertEqual(payload[1][4], "v5kua7v5kua7v5ku")


    @mock.patch(
        "gemini_web2api.server.download_generated_image",
        side_effect=[RuntimeError("generated image download failed: HTTP 403"), (b"\x89PNG\r\n\x1a\n", "image/png")],
    )
    @mock.patch("gemini_web2api.server.get_full_size_image", return_value="https://lh3.googleusercontent.com/full")
    @mock.patch("gemini_web2api.server.generate_image_structured")
    def test_image_generation_endpoint_falls_back_to_preview_on_full_size_failure(
        self, generate_image_structured, _full_size, download_generated_image
    ):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generate_image_structured.return_value = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/generated")]
        )
        status, _, body = self.post_json("/v1/images/generations", {
            "prompt": "a red fox", "model": "gpt-image-1",
            "response_format": "b64_json",
            "size": "1024x1024", "quality": "high", "background": "opaque",
        })
        self.assertEqual(status, 200)
        self.assertEqual(download_generated_image.call_args_list, [
            mock.call("https://lh3.googleusercontent.com/full", deadline=mock.ANY),
            mock.call("https://lh3.googleusercontent.com/generated", deadline=mock.ANY),
        ])
        generate_image_structured.assert_called_once()
        self.assertIn("a red fox", generate_image_structured.call_args.args[0])
        self.assertIn("1024x1024", generate_image_structured.call_args.args[0])

    @mock.patch("gemini_web2api.server.download_generated_image",
                return_value=(_png_bytes(64, 32), "image/png"))
    @mock.patch("gemini_web2api.server.get_full_size_image", return_value=None)
    @mock.patch("gemini_web2api.server.generate_image_structured")
    def test_image_generation_reports_estimated_usage(
        self, generate_image_structured, _full_size, _download
    ):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generate_image_structured.return_value = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/generated")]
        )
        status, _, body = self.post_json("/v1/images/generations", {
            "prompt": "a red fox", "model": "gpt-image-1",
        })
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["size"], "64x32")
        usage = payload["usage"]
        self.assertEqual(usage, {
            "total_tokens": 5,
            "input_tokens": 3,
            "output_tokens": 2,
            "input_tokens_details": {"text_tokens": 3, "image_tokens": 0},
            "output_tokens_details": {"text_tokens": 0, "image_tokens": 2},
        })

    @mock.patch("gemini_web2api.server.download_generated_image",
                return_value=(_png_bytes(64, 32), "image/png"))
    @mock.patch("gemini_web2api.server.get_full_size_image", return_value=None)
    @mock.patch("gemini_web2api.server.generate_image_structured")
    def test_image_edit_reports_estimated_usage(
        self, generate_image_structured, _full_size, _download
    ):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generate_image_structured.return_value = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        with mock.patch(
            "gemini_web2api.server.upload_image", return_value="/uploaded/source"
        ):
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={"prompt": "edit this", "model": "gpt-image-1"},
                files={"image": ("source.png", _png_bytes(64, 32), "image/png")},
            )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["size"], "64x32")
        usage = payload["usage"]
        self.assertEqual(usage, {
            "total_tokens": 7,
            "input_tokens": 5,
            "output_tokens": 2,
            "input_tokens_details": {"text_tokens": 3, "image_tokens": 2},
            "output_tokens_details": {"text_tokens": 0, "image_tokens": 2},
        })

    def test_image_usage_estimator_uses_utf8_text_and_pixel_patches(self):
        from gemini_web2api.server import _image_usage
        usage = _image_usage("你好", input_sizes=[(64, 32)], output_sizes=[(32, 32)])
        self.assertEqual(usage["input_tokens_details"], {
            "text_tokens": 2, "image_tokens": 2,
        })
        self.assertEqual(usage["output_tokens_details"], {
            "text_tokens": 0, "image_tokens": 1,
        })
        self.assertEqual(usage["total_tokens"], 5)

    def test_image_generation_rejects_url_response_format(self):
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, headers, body = self.post_json("/v1/images/generations", {
                "prompt": "a red fox", "model": "gpt-image-1",
                "response_format": "url",
            })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["param"], "response_format")
        self.assertEqual(error["code"], "unsupported_value")
        self.assertIn("b64_json", error["message"])
        self.assertTrue(headers.get("x-request-id", "").startswith("req_"))
        generate.assert_not_called()

    def test_image_edit_endpoint_uploads_source_and_mask(self):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult

        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(2, 2, alpha=True)
        mask = _mask_png(2, 2, (0, 0, 1, 1))
        edited = b"\x89PNG\r\n\x1a\n"
        composited = b"\x89PNG\r\n\x1a\ncomposited"
        with mock.patch.dict(CONFIG, {"image_edit_crop": False, "image_edit_locator": False,
                                 "image_edit_context_crop": False}), mock.patch(
            "gemini_web2api.server.upload_image",
            side_effect=["/uploaded/source", "/uploaded/mask"],
        ) as upload_image, mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ) as generate_image_structured, mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            return_value=(edited, "image/png"),
        ), mock.patch(
            "gemini_web2api.server.thumbnail_mask_stats", return_value=(3.0, 30.0),
        ) as thumbnail_mask_stats, mock.patch(
            "gemini_web2api.server.composite_crop_edit",
            return_value=(composited, "image/png"),
        ) as composite_crop_edit:
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={
                    "prompt": "replace the sky with a sunset",
                    "response_format": "b64_json",
                    "n": "1",
                    "model": "gpt-image-1",
                },
                files={
                    "image": ("source.png", source, "image/png"),
                    "mask": ("mask.png", mask, "image/png"),
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            base64.b64decode(json.loads(body)["data"][0]["b64_json"]),
            composited,
        )
        self.assertEqual(upload_image.call_args_list, [
            mock.call(source, "source.png", "image/png"),
            mock.call(mask, "mask.png", "image/png"),
        ])
        generate_image_structured.assert_called_once()
        prompt = generate_image_structured.call_args.args[0]
        self.assertIn("replace the sky with a sunset", prompt)
        self.assertIn("selection mask", prompt)
        self.assertIn("local retouch", prompt)
        self.assertIn("Do not fill the selection with a flat color", prompt)
        self.assertIn("Never reproduce the mask image", prompt)
        self.assertIn("failed edit", prompt)
        self.assertIn("50% of the frame width", prompt)
        self.assertIn("upper left of the frame", prompt)
        self.assertTrue(prompt.rstrip().endswith("replace the sky with a sunset"))
        self.assertEqual(
            generate_image_structured.call_args.kwargs["file_refs"],
            [("/uploaded/source", "source.png"), ("/uploaded/mask", "mask.png")],
        )
        thumbnail_mask_stats.assert_called_once_with(
            source, "image/png", edited, "image/png", mask
        )
        composite_crop_edit.assert_called_once_with(
            source, "image/png", edited, "image/png", mask,
            (0, 0, 2, 2), (2, 2),
        )


    def test_image_edit_endpoint_uses_marker_locator(self):
        from types import SimpleNamespace
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult

        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(2, 2, alpha=True)
        mask = _mask_png(2, 2, (0, 0, 1, 1))
        plan = SimpleNamespace(locator_image=b"\x89PNG\r\n\x1a\nlocator",
                               marker_alpha=b"\x89PNG\r\n\x1a\nalpha")
        raw = b"\x89PNG\r\n\x1a\nraw"
        stripped = b"\x89PNG\r\n\x1a\nstripped"
        composited = b"\x89PNG\r\n\x1a\ncomposited"
        with mock.patch.dict(CONFIG, {
            "image_edit_crop": False, "image_edit_locator": True,
            "image_edit_context_crop": False,
        }), mock.patch(
            "gemini_web2api.server.build_marker_locator", return_value=plan
        ) as build_marker_locator, mock.patch(
            "gemini_web2api.server.upload_image",
            side_effect=["/uploaded/source", "/uploaded/locator", "/uploaded/reference"],
        ) as upload_image, mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ) as generate_image_structured, mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            return_value=(raw, "image/png"),
        ), mock.patch(
            "gemini_web2api.server.strip_edit_marker",
            return_value=(stripped, "image/png"),
        ) as strip_edit_marker, mock.patch(
            "gemini_web2api.server.thumbnail_mask_stats", return_value=(3.0, 20.0),
        ) as thumbnail_mask_stats, mock.patch(
            "gemini_web2api.server.composite_crop_edit",
            return_value=(composited, "image/png"),
        ) as composite_crop_edit:
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={"prompt": "add a gull", "model": "gpt-image-1", "response_format": "b64_json"},
                files={
                    "image": ("source.png", source, "image/png"),
                    "mask": ("mask.png", mask, "image/png"),
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            base64.b64decode(json.loads(body)["data"][0]["b64_json"]), composited
        )
        build_marker_locator.assert_called_once_with(source, "image/png", mask)
        self.assertEqual(upload_image.call_args_list, [
            mock.call(source, "source.png", "image/png"),
            mock.call(plan.locator_image, "locator.png", "image/png"),
            mock.call(source, "source.png", "image/png"),
        ])
        prompt = generate_image_structured.call_args.args[0]
        self.assertIn("thin red outline", prompt)
        self.assertIn("Image 3 is the same photo again", prompt)
        self.assertIn("Do not add anything outside the red outline", prompt)
        strip_edit_marker.assert_called_once_with(
            raw, "image/png", source, "image/png", plan.marker_alpha
        )
        thumbnail_mask_stats.assert_called_once_with(
            source, "image/png", stripped, "image/png", mask
        )
        composite_crop_edit.assert_called_once_with(
            source, "image/png", stripped, "image/png", mask,
            (0, 0, 2, 2), (2, 2),
        )

    def test_image_edit_prompt_mentions_locator(self):
        from types import SimpleNamespace

        geometry = SimpleNamespace(width=100, height=100, bounds=(10, 10, 40, 40))
        prompt = _edit_prompt("add a gull", geometry, locator=True)
        self.assertIn("thin red outline", prompt)
        self.assertIn("never copy its red pixels", prompt)
        self.assertIn("Image 3 is the same photo again", prompt)
        self.assertIn("Do not add anything outside the red outline", prompt)
        legacy = _edit_prompt("add a gull", geometry)
        self.assertIn("selection mask", legacy)
        self.assertNotIn("Image 3", legacy)

    def test_context_crop_box_keeps_context_around_selection(self):
        from gemini_web2api.image_ops import context_crop_box

        # Selection sits at the top edge, so the box can only grow downward.
        box = context_crop_box((2425, 141, 2780, 513), 2816, 1536, 0.8)
        x0, y0, x1, y1 = box
        self.assertLessEqual(x0, 2425 - 100)
        self.assertLessEqual(y0, 141)
        self.assertGreaterEqual(x1, 2816)
        self.assertGreaterEqual(y1, 513 + 200)
        self.assertGreaterEqual(x1, x0)
        self.assertLessEqual(x1, 2816)
        self.assertLessEqual(y1, 1536)
        # The short side must be filled with more than just the mask.
        self.assertGreater((x1 - x0) * (y1 - y0), 400 * 400)

    def test_image_edit_endpoint_uses_context_crop_without_mask(self):
        from types import SimpleNamespace
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult

        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(2, 2, alpha=True)
        mask = _mask_png(2, 2, (0, 0, 1, 1))
        plan = SimpleNamespace(
            crop_image=b"context-crop",
            crop_mask=b"context-mask",
            crop_box=(0, 0, 2, 2),
            crop_size=(2, 2),
            geometry=SimpleNamespace(transparent_pixels=1, bounds=(0, 0, 1, 1),
                                     width=2, height=2),
        )
        edited = b"\x89PNG\r\n\x1a\nedited"
        composited = b"\x89PNG\r\n\x1a\ncomposited"
        with mock.patch.dict(CONFIG, {
            "image_edit_crop": False, "image_edit_locator": False,
            "image_edit_context_crop": True, "image_edit_attempts": 1,
            "image_edit_tone_match": False,
        }), mock.patch(
            "gemini_web2api.server.prepare_context_crop", return_value=plan
        ) as prepare_context_crop, mock.patch(
            "gemini_web2api.server.upload_image", return_value="/uploaded/crop",
        ) as upload_image, mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ) as generate_image_structured, mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            return_value=(edited, "image/png"),
        ), mock.patch(
            "gemini_web2api.server.thumbnail_mask_stats", return_value=(3.0, 30.0),
        ) as thumbnail_mask_stats, mock.patch(
            "gemini_web2api.server.composite_crop_edit",
            return_value=(composited, "image/png"),
        ) as composite_crop_edit:
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={"prompt": "add gulls", "model": "gpt-image-1", "response_format": "b64_json"},
                files={
                    "image": ("source.png", source, "image/png"),
                    "mask": ("mask.png", mask, "image/png"),
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            base64.b64decode(json.loads(body)["data"][0]["b64_json"]), composited)
        prepare_context_crop.assert_called_once_with(source, mask, "image/png")
        self.assertEqual(upload_image.call_args_list, [
            mock.call(b"context-crop", "crop.png", "image/png"),
        ])
        prompt = generate_image_structured.call_args.args[0]
        self.assertIn("add gulls", prompt)
        self.assertIn("Edit this photo directly", prompt)
        self.assertNotIn("mask", prompt.lower())
        thumbnail_mask_stats.assert_called_once_with(
            b"context-crop", "image/png", edited, "image/png", b"context-mask")
        composite_crop_edit.assert_called_once_with(
            source, "image/png", edited, "image/png", b"context-mask",
            (0, 0, 2, 2), (2, 2),
        )

    def test_frame_matches_rejects_reframed_output(self):
        from gemini_web2api.server import _frame_matches

        self.assertTrue(_frame_matches((1600, 800), (2000, 1000), 0.02))
        self.assertTrue(_frame_matches((1, 1), (2, 2), 0.02))
        self.assertFalse(_frame_matches((1024, 1024), (1600, 900), 0.02))
        self.assertFalse(_frame_matches((0, 0), (1600, 900), 0.02))

    def test_image_edit_discards_reframed_generation(self):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult

        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(8, 4, alpha=True)
        mask = _mask_png(8, 4, (0, 0, 4, 4))
        square = b"\x89PNG\r\n\x1a\nsquare"
        with mock.patch.dict(CONFIG, {
            "image_edit_crop": False, "image_edit_locator": False,
            "image_edit_context_crop": False,
            "image_edit_attempts": 2, "image_edit_frame_gate": True,
        }), mock.patch(
            "gemini_web2api.server.upload_image",
            side_effect=["/uploaded/source", "/uploaded/mask"],
        ), mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ), mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            return_value=(square, "image/png"),
        ), mock.patch(
            "gemini_web2api.server.image_dimensions", return_value=(1024, 1024),
        ), mock.patch(
            "gemini_web2api.server.thumbnail_mask_stats", return_value=(3.0, 30.0),
        ), mock.patch(
            "gemini_web2api.server.composite_crop_edit",
            return_value=(square, "image/png"),
        ) as composite_crop_edit:
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={"prompt": "add a gull", "model": "gpt-image-1", "response_format": "b64_json"},
                files={
                    "image": ("source.png", source, "image/png"),
                    "mask": ("mask.png", mask, "image/png"),
                },
            )

        self.assertEqual(status, 500)
        self.assertIn("reframed", json.loads(body)["error"]["message"])
        composite_crop_edit.assert_not_called()

    def test_image_edit_retries_when_mask_drift_is_high(self):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult

        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(2, 2, alpha=True)
        mask = _mask_png(2, 2, (0, 0, 1, 1))
        drifted = b"\x89PNG\r\n\x1a\ndrifted"
        clean = b"\x89PNG\r\n\x1a\nclean"
        composited = b"\x89PNG\r\n\x1a\ncomposited"
        with mock.patch.dict(CONFIG, {
            "image_edit_crop": False, "image_edit_context_crop": False,
            "image_edit_attempts": 2, "image_edit_drift_limit": 18.0,
        }), mock.patch(
            "gemini_web2api.server.upload_image",
            side_effect=["/uploaded/source", "/uploaded/mask"],
        ), mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ) as generate_image_structured, mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            side_effect=[(drifted, "image/png"), (clean, "image/png")],
        ), mock.patch(
            "gemini_web2api.server.thumbnail_mask_stats", side_effect=[(45.0, 30.0), (4.0, 30.0)],
        ), mock.patch(
            "gemini_web2api.server.composite_crop_edit",
            return_value=(composited, "image/png"),
        ) as composite_crop_edit:
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={"prompt": "add a gull", "model": "gpt-image-1", "response_format": "b64_json"},
                files={
                    "image": ("source.png", source, "image/png"),
                    "mask": ("mask.png", mask, "image/png"),
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            base64.b64decode(json.loads(body)["data"][0]["b64_json"]),
            composited,
        )
        self.assertEqual(generate_image_structured.call_count, 2)
        self.assertIn(
            "Make the requested change inside the selected area now",
            generate_image_structured.call_args_list[1].args[0],
        )
        composite_crop_edit.assert_called_once_with(
            source, "image/png", clean, "image/png", mask,
            (0, 0, 2, 2), (2, 2),
        )

    def test_image_edit_prefers_attempt_that_changes_selection(self):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult

        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(2, 2, alpha=True)
        mask = _mask_png(2, 2, (0, 0, 1, 1))
        first = b"\x89PNG\r\n\x1a\nfirst"
        second = b"\x89PNG\r\n\x1a\nsecond"
        composited = b"\x89PNG\r\n\x1a\ncomposited"
        with mock.patch.dict(CONFIG, {
            "image_edit_crop": False, "image_edit_context_crop": False,
            "image_edit_attempts": 2, "image_edit_drift_limit": 18.0,
        }), mock.patch(
            "gemini_web2api.server.upload_image",
            side_effect=["/uploaded/source", "/uploaded/mask"],
        ), mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ), mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            side_effect=[(first, "image/png"), (second, "image/png")],
        ), mock.patch(
            "gemini_web2api.server.thumbnail_mask_stats", side_effect=[(3.0, 2.0), (60.0, 40.0)],
        ), mock.patch(
            "gemini_web2api.server.composite_crop_edit",
            return_value=(composited, "image/png"),
        ) as composite_crop_edit:
            status, _, _body = self.post_multipart(
                "/v1/images/edits",
                fields={"prompt": "add a gull", "model": "gpt-image-1", "response_format": "b64_json"},
                files={
                    "image": ("source.png", source, "image/png"),
                    "mask": ("mask.png", mask, "image/png"),
                },
            )

        self.assertEqual(status, 200)
        composite_crop_edit.assert_called_once_with(
            source, "image/png", second, "image/png", mask,
            (0, 0, 2, 2), (2, 2),
        )

    def test_edit_prompt_is_strict_and_preserves_user_instruction(self):
        from types import SimpleNamespace

        geometry = SimpleNamespace(
            width=2816, height=1536, bounds=(2425, 142, 2779, 512),
            transparent_pixels=76715, total_pixels=2816 * 1536,
        )
        prompt = _edit_prompt("add a surfer", geometry)
        self.assertTrue(prompt.startswith("You are editing one small selected area"))
        self.assertIn("It covers about 13% of the frame width", prompt)
        self.assertIn("24% of the frame height", prompt)
        self.assertIn("upper right of the frame", prompt)
        self.assertIn("selection mask", prompt)
        self.assertIn("local retouch", prompt)
        self.assertIn("Do not fill the selection with a flat color", prompt)
        self.assertIn("Never reproduce the mask image in the output", prompt)
        self.assertIn("failed edit", prompt)
        self.assertIn("no visible lasso edge", prompt)
        self.assertTrue(prompt.rstrip().endswith("User instruction:\nadd a surfer"))
        self.assertEqual(_edit_prompt("add a surfer"), "add a surfer")

    def test_crop_edit_prompt_is_crop_aware_and_preserves_user_instruction(self):
        prompt = _crop_edit_prompt("add a surfer", (528, 226))
        self.assertTrue(prompt.startswith("You are editing a cropped region"))
        self.assertIn("528x226 crop", prompt)
        self.assertIn("Do not expand the canvas", prompt)
        self.assertIn("entirely inside the transparent region", prompt)
        self.assertTrue(prompt.endswith("User instruction:\nadd a surfer"))

    def test_mask_geometry_returns_transparent_bounds_and_crop_box(self):
        geometry = mask_geometry(_mask_png(10, 8, (2, 1, 8, 6)))
        self.assertEqual(geometry.bounds, (2, 1, 8, 6))
        self.assertEqual(geometry.transparent_pixels, 30)
        crop = choose_crop_box(geometry.bounds, 100, 100)
        self.assertLessEqual(crop[0], 2)
        self.assertLessEqual(crop[1], 1)
        self.assertGreaterEqual(crop[2], 8)
        self.assertGreaterEqual(crop[3], 6)
        with self.assertRaisesRegex(ValueError, "no fully transparent"):
            mask_geometry(_mask_png(2, 2))

    def test_image_edit_endpoint_uses_crop_and_composite_mode(self):
        from types import SimpleNamespace
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult

        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(2, 2, alpha=True)
        mask = _mask_png(2, 2, (0, 0, 1, 1))
        plan = SimpleNamespace(
            crop_image=b"cropped-source",
            crop_mask=b"cropped-mask",
            crop_box=(0, 0, 2, 2),
            crop_size=(2, 2),
            geometry=SimpleNamespace(transparent_pixels=1),
        )
        edited = b"\x89PNG\r\n\x1a\nedited"
        composited = b"\x89PNG\r\n\x1a\ncomposited"
        with mock.patch.dict(CONFIG, {
            "image_edit_crop": True, "image_edit_context_crop": False,
        }), mock.patch(
            "gemini_web2api.server.prepare_crop_edit", return_value=plan
        ), mock.patch(
            "gemini_web2api.server.upload_image",
            side_effect=["/uploaded/crop", "/uploaded/mask"],
        ) as upload_image, mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ) as generate_image_structured, mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            return_value=(edited, "image/png"),
        ), mock.patch(
            "gemini_web2api.server.composite_crop_edit",
            return_value=(composited, "image/png"),
        ) as composite_crop_edit:
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={"prompt": "add a surfer", "model": "gpt-image-1", "response_format": "b64_json"},
                files={
                    "image": ("source.png", source, "image/png"),
                    "mask": ("mask.png", mask, "image/png"),
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            base64.b64decode(json.loads(body)["data"][0]["b64_json"]),
            composited,
        )
        self.assertEqual(upload_image.call_args_list, [
            mock.call(plan.crop_image, "crop.png", "image/png"),
            mock.call(plan.crop_mask, "mask.png", "image/png"),
        ])
        prompt = generate_image_structured.call_args.args[0]
        self.assertIn("cropped region", prompt)
        self.assertIn("2x2 crop", prompt)
        self.assertTrue(prompt.endswith("User instruction:\nadd a surfer"))
        composite_crop_edit.assert_called_once_with(
            source, "image/png", edited, "image/png",
            plan.crop_mask, plan.crop_box, plan.crop_size,
        )

    def test_image_edit_endpoint_rejects_mask_without_transparent_pixels(self):
        status, _, body = self.post_multipart(
            "/v1/images/edits",
            fields={"prompt": "edit this", "model": "gpt-image-1"},
            files={
                "image": ("source.png", _png_bytes(2, 2), "image/png"),
                "mask": ("mask.png", _mask_png(2, 2), "image/png"),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("no fully transparent", json.loads(body)["error"]["message"])

    def test_image_edit_endpoint_rejects_invalid_multipart_requests(self):
        status, _, body = self.post_multipart(
            "/v1/images/edits", fields={"prompt": "edit this"}
        )
        self.assertEqual(status, 400)
        self.assertIn("image is required", json.loads(body)["error"]["message"])

        status, _, body = self.post_multipart(
            "/v1/images/edits",
            fields={"prompt": "edit this", "model": "gpt-image-1", "n": "11"},
            files={"image": ("source.png", _png_bytes(2, 2), "image/png")},
        )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "n")
        self.assertEqual(error["type"], "invalid_request_error")

    def test_image_edit_endpoint_rejects_mask_without_alpha(self):
        status, _, body = self.post_multipart(
            "/v1/images/edits",
            fields={"prompt": "edit this", "model": "gpt-image-1"},
            files={
                "image": ("source.png", _png_bytes(2, 2), "image/png"),
                "mask": ("mask.png", _png_bytes(2, 2, alpha=False), "image/png"),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("alpha channel", json.loads(body)["error"]["message"])

    def test_image_edit_endpoint_rejects_mismatched_mask_dimensions(self):
        status, _, body = self.post_multipart(
            "/v1/images/edits",
            fields={"prompt": "edit this", "model": "gpt-image-1"},
            files={
                "image": ("source.png", _png_bytes(2, 2), "image/png"),
                "mask": ("mask.png", _png_bytes(1, 1), "image/png"),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("dimensions must match", json.loads(body)["error"]["message"])

    def test_image_edit_endpoint_rejects_url_response_format(self):
        source = _png_bytes(2, 2)
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, headers, body = self.post_multipart(
                "/v1/images/edits",
                fields={
                    "prompt": "make it brighter",
                    "model": "gpt-image-1",
                    "response_format": "url",
                },
                files={"image": ("source.png", source, "image/png")},
            )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["param"], "response_format")
        self.assertEqual(error["code"], "unsupported_value")
        self.assertIn("b64_json", error["message"])
        self.assertTrue(headers.get("x-request-id", "").startswith("req_"))
        generate.assert_not_called()

    def test_image_edit_rejects_url_response_format_in_json(self):
        source = _png_bytes(2, 2)
        data_url = "data:image/png;base64," + base64.b64encode(source).decode()
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, _, body = self.post_json("/v1/images/edits", {
                "prompt": "edit this", "model": "gpt-image-1",
                "images": [{"image_url": data_url}],
                "response_format": "url",
            })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "response_format")
        self.assertEqual(error["code"], "unsupported_value")
        generate.assert_not_called()

    def test_image_generation_rejects_transparent_background(self):
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, headers, body = self.post_json("/v1/images/generations", {
                "prompt": "a red fox", "model": "gpt-image-1",
                "background": "transparent", "output_format": "png",
            })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["param"], "background")
        self.assertEqual(error["code"], "unsupported_value")
        self.assertIn("opaque", error["message"])
        self.assertTrue(headers.get("x-request-id", "").startswith("req_"))
        generate.assert_not_called()

    def test_image_edit_rejects_transparent_background(self):
        source = _png_bytes(2, 2)
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={
                    "prompt": "edit this",
                    "model": "gpt-image-1",
                    "background": "transparent",
                },
                files={"image": ("source.png", source, "image/png")},
            )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "background")
        self.assertEqual(error["code"], "unsupported_value")
        generate.assert_not_called()

    def test_image_endpoint_errors_use_openai_schema_and_request_id(self):
        status, headers, body = self.post_json("/v1/images/generations", {
            "prompt": "a red fox", "model": "gpt-image-1", "size": "not-a-size",
        })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["param"], "size")
        self.assertEqual(error["code"], "invalid_value")
        self.assertTrue(headers.get("x-request-id", "").startswith("req_"))

        status, headers, body = self.post_json("/v1/images/generations", {
            "prompt": "a red fox", "model": "gpt-image-1", "stream": True,
        })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "stream")
        self.assertEqual(error["code"], "unsupported_value")
        self.assertTrue(headers.get("x-request-id", "").startswith("req_"))

    @mock.patch("gemini_web2api.server.download_generated_image",
                return_value=(_png_bytes(2, 2), "image/png"))
    @mock.patch("gemini_web2api.server.get_full_size_image", return_value=None)
    @mock.patch("gemini_web2api.server.generate_image_structured")
    def test_image_generation_accepts_nullable_and_compat_fields(
        self, generate_image_structured, _full_size, _download
    ):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generate_image_structured.return_value = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/generated")]
        )
        with mock.patch("gemini_web2api.server.fit_image_size",
                        side_effect=lambda data, mime, width, height, fmt=None, compression=None: (data, mime)):
            status, _, body = self.post_json("/v1/images/generations", {
                "prompt": "a red fox", "model": "gpt-image-1",
                "stream": False, "n": None, "response_format": None,
                "safety_identifier": "user-1", "partial_images": 0,
            })
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["data"]), 1)
        self.assertIn("b64_json", payload["data"][0])

    @mock.patch("gemini_web2api.server._native_image_size", return_value=(1536, 1024))
    @mock.patch("gemini_web2api.server.fit_image_size",
                side_effect=lambda data, mime, width, height, fmt=None, compression=None: (data, mime))
    @mock.patch("gemini_web2api.server.download_generated_image",
                return_value=(_png_bytes(2, 2), "image/png"))
    @mock.patch("gemini_web2api.server.get_full_size_image", return_value=None)
    @mock.patch("gemini_web2api.server.generate_image_structured")
    def test_image_generation_supports_multiple_outputs_and_size(
        self, generate_image_structured, _full_size, _download, _fit, _native
    ):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generate_image_structured.return_value = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/generated")]
        )
        status, _, body = self.post_json("/v1/images/generations", {
            "prompt": "a red fox", "model": "gpt-image-1",
            "n": 2, "size": "1536x1024",
        })
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["data"]), 2)
        self.assertEqual(payload["size"], "1536x1024")
        self.assertEqual(payload["output_format"], "png")
        self.assertEqual(generate_image_structured.call_count, 2)
        prompt = generate_image_structured.call_args.args[0]
        self.assertIn("1536x1024", prompt)
        self.assertIn("3:2", prompt)

    @mock.patch("gemini_web2api.server._actual_image_size",
                side_effect=[(2, 2), (3, 3)])
    @mock.patch("gemini_web2api.server.download_generated_image",
                return_value=(_png_bytes(2, 2), "image/png"))
    @mock.patch("gemini_web2api.server.get_full_size_image", return_value=None)
    @mock.patch("gemini_web2api.server.generate_image_structured")
    def test_image_generation_omits_size_when_outputs_disagree(
        self, generate_image_structured, _full_size, _download, _actual
    ):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generate_image_structured.return_value = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/generated")]
        )
        status, _, body = self.post_json("/v1/images/generations", {
            "prompt": "a red fox", "model": "gpt-image-1", "n": 2,
        })
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["data"]), 2)
        self.assertNotIn("size", payload)
        self.assertEqual(generate_image_structured.call_count, 2)
        for call in generate_image_structured.call_args_list:
            self.assertGreater(call.kwargs["budget_sec"], 0)

    @mock.patch("gemini_web2api.server.fit_image_size",
                side_effect=ImageOpsUnavailable("no magick"))
    @mock.patch("gemini_web2api.server.download_generated_image",
                return_value=(_png_bytes(64, 32), "image/png"))
    @mock.patch("gemini_web2api.server.get_full_size_image", return_value=None)
    @mock.patch("gemini_web2api.server.generate_image_structured")
    def test_image_generation_echoes_actual_size_when_resize_is_unavailable(
        self, generate_image_structured, _full_size, _download, _fit
    ):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generate_image_structured.return_value = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/generated")]
        )
        status, _, body = self.post_json("/v1/images/generations", {
            "prompt": "a red fox", "model": "gpt-image-1",
            "size": "1536x1024",
        })
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["size"], "64x32")
        self.assertNotEqual(payload["size"], "1536x1024")

    def test_image_edit_accepts_compat_fields_with_single_image(self):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(2, 2)
        with mock.patch.dict(CONFIG, {
            "image_edit_crop": False, "image_edit_context_crop": False,
            "image_edit_locator": False,
        }), mock.patch(
            "gemini_web2api.server.upload_image", return_value="/uploaded/source"
        ) as upload_image, mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ) as generate_image_structured, mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            return_value=(source, "image/png"),
        ):
            status, headers, body = self.post_multipart(
                "/v1/images/edits",
                fields={
                    "prompt": "edit this", "model": "gpt-image-1",
                    "stream": "false", "safety_identifier": "user-1",
                    "partial_images": "1",
                },
                files={"image": ("source.png", source, "image/png")},
            )
        self.assertEqual(status, 200)
        self.assertEqual(upload_image.call_count, 1)
        file_refs = generate_image_structured.call_args.kwargs["file_refs"]
        self.assertEqual(file_refs, [("/uploaded/source", "source.png")])
        self.assertTrue(headers.get("x-request-id", "").startswith("req_"))

    def test_image_edit_rejects_multiple_multipart_images(self):
        source = _png_bytes(2, 2)
        status, _, body = self.post_multipart(
            "/v1/images/edits",
            fields={"prompt": "edit this", "model": "gpt-image-1"},
            files={"image": [
                ("one.png", source, "image/png"),
                ("two.png", source, "image/png"),
            ]},
        )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "image")
        self.assertEqual(error["code"], "unsupported_value")

    def test_image_edit_rejects_multiple_json_images(self):
        source = _png_bytes(2, 2)
        data_url = "data:image/png;base64," + base64.b64encode(source).decode()
        status, _, body = self.post_json("/v1/images/edits", {
            "prompt": "edit this", "model": "gpt-image-1",
            "images": [{"image_url": data_url}, {"image_url": data_url}],
        })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "images")
        self.assertEqual(error["code"], "unsupported_value")

    def test_image_edit_rejects_markup_in_multipart(self):
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, headers, body = self.post_multipart(
                "/v1/images/edits",
                fields={
                    "prompt": "edit this",
                    "model": "gpt-image-1",
                    "markup": "data:image/png;base64,AAAA",
                },
                files={"image": ("source.png", _png_bytes(2, 2), "image/png")},
            )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["param"], "markup")
        self.assertEqual(error["code"], "unsupported_value")
        self.assertIn("not supported", error["message"])
        self.assertTrue(headers.get("x-request-id", "").startswith("req_"))
        generate.assert_not_called()

    def test_image_edit_rejects_markup_in_json(self):
        source = _png_bytes(2, 2)
        data_url = "data:image/png;base64," + base64.b64encode(source).decode()
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, _, body = self.post_json("/v1/images/edits", {
                "prompt": "edit this",
                "model": "gpt-image-1",
                "images": [{"image_url": data_url}],
                "markup": data_url,
            })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "markup")
        self.assertEqual(error["code"], "unsupported_value")
        generate.assert_not_called()

    def test_image_edit_rejects_file_id_image_reference(self):
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, headers, body = self.post_json("/v1/images/edits", {
                "prompt": "edit this", "model": "gpt-image-1",
                "images": [{"file_id": "file-abc123"}],
            })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["param"], "images[0]")
        self.assertEqual(error["code"], "unsupported_value")
        self.assertIn("image_url", error["message"])
        self.assertTrue(headers.get("x-request-id", "").startswith("req_"))
        generate.assert_not_called()

    def test_image_edit_rejects_file_id_mask_reference(self):
        source = _png_bytes(2, 2)
        data_url = "data:image/png;base64," + base64.b64encode(source).decode()
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, _, body = self.post_json("/v1/images/edits", {
                "prompt": "edit this", "model": "gpt-image-1",
                "images": [{"image_url": data_url}],
                "mask": {"file_id": "file-mask123"},
            })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "mask")
        self.assertEqual(error["code"], "unsupported_value")
        self.assertIn("image_url", error["message"])
        generate.assert_not_called()

    def test_image_generation_requires_model(self):
        status, _, body = self.post_json("/v1/images/generations", {
            "prompt": "a red fox",
        })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "model")
        self.assertEqual(error["code"], "missing_required_parameter")

    def test_image_generation_rejects_unknown_model(self):
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, headers, body = self.post_json("/v1/images/generations", {
                "prompt": "a red fox", "model": "not-a-real-model",
            })
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "model")
        self.assertEqual(error["code"], "model_not_found")
        self.assertIn("not-a-real-model", error["message"])
        self.assertTrue(headers.get("x-request-id", "").startswith("req_"))
        generate.assert_not_called()

    def test_image_edit_rejects_unknown_model(self):
        with mock.patch("gemini_web2api.server.generate_image_structured") as generate:
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={"prompt": "edit this", "model": "not-a-real-model"},
                files={"image": ("source.png", _png_bytes(2, 2), "image/png")},
            )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "model")
        self.assertEqual(error["code"], "model_not_found")
        generate.assert_not_called()

    def test_image_model_namespace(self):
        from gemini_web2api.server import _image_model_error, _parse_json_edit_request
        self.assertIsNone(_image_model_error("gpt-image-1"))
        self.assertIsNone(_image_model_error("gpt-image-2.5-flare"))
        self.assertIsNone(_image_model_error("gemini-3.6-flash"))
        self.assertIsNone(_image_model_error("gemini-3.6-flash@think=4"))
        self.assertIsNone(_image_model_error("a" * 16))
        self.assertIsNotNone(_image_model_error("not-a-real-model"))
        source = _png_bytes(2, 2)
        data_url = "data:image/png;base64," + base64.b64encode(source).decode()
        parsed = _parse_json_edit_request(json.dumps({
            "prompt": "edit this", "images": [{"image_url": data_url}],
        }).encode())
        self.assertEqual(parsed["model"], "gpt-image-2.5-sunburst")

    def test_image_edit_requires_model_and_rejects_style(self):
        source = _png_bytes(2, 2)
        status, _, body = self.post_multipart(
            "/v1/images/edits",
            fields={"prompt": "edit this"},
            files={"image": ("source.png", source, "image/png")},
        )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "model")
        self.assertEqual(error["code"], "missing_required_parameter")

        status, _, body = self.post_multipart(
            "/v1/images/edits",
            fields={"prompt": "edit this", "model": "gpt-image-1",
                    "style": "vivid"},
            files={"image": ("source.png", source, "image/png")},
        )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "style")
        self.assertEqual(error["code"], "unknown_parameter")

    def test_image_edit_rejects_gif_and_bmp_inputs(self):
        status, _, body = self.post_multipart(
            "/v1/images/edits",
            fields={"prompt": "edit this", "model": "gpt-image-1"},
            files={"image": ("source.gif", b"GIF89a" + b"\x00" * 32, "image/gif")},
        )
        self.assertEqual(status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["param"], "image")
        self.assertIn("PNG, JPEG, or WebP", error["message"])

        status, _, body = self.post_multipart(
            "/v1/images/edits",
            fields={"prompt": "edit this", "model": "gpt-image-1"},
            files={"image": ("source.bmp", b"BM" + b"\x00" * 32, "image/bmp")},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["param"], "image")

    def test_image_edit_accepts_json_image_references(self):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(2, 2)
        data_url = "data:image/png;base64," + base64.b64encode(source).decode()
        with mock.patch.dict(CONFIG, {
            "image_edit_crop": False, "image_edit_context_crop": False,
            "image_edit_locator": False,
        }), mock.patch(
            "gemini_web2api.server.upload_image", return_value="/uploaded/source"
        ) as upload_image, mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ), mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            return_value=(source, "image/png"),
        ):
            status, _, body = self.post_json("/v1/images/edits", {
                "prompt": "edit this", "model": "gpt-image-1",
                "images": [{"image_url": data_url}],
            })
        self.assertEqual(status, 200)
        self.assertIn("b64_json", json.loads(body)["data"][0])
        self.assertEqual(upload_image.call_args.args[0], source)
        self.assertEqual(upload_image.call_args.args[1], "image-0.png")

    def test_image_edit_supports_multiple_outputs(self):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generated = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/edited")]
        )
        source = _png_bytes(2, 2)
        with mock.patch.dict(CONFIG, {
            "image_edit_crop": False, "image_edit_context_crop": False,
            "image_edit_locator": False,
        }), mock.patch(
            "gemini_web2api.server.upload_image", return_value="/uploaded/source"
        ), mock.patch(
            "gemini_web2api.server.generate_image_structured",
            return_value=generated,
        ) as generate_image_structured, mock.patch(
            "gemini_web2api.server.get_full_size_image", return_value=None
        ), mock.patch(
            "gemini_web2api.server.download_generated_image",
            return_value=(source, "image/png"),
        ):
            status, _, body = self.post_multipart(
                "/v1/images/edits",
                fields={"prompt": "edit this", "model": "gpt-image-1", "n": "2"},
                files={"image": ("source.png", source, "image/png")},
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["data"]), 2)
        self.assertEqual(generate_image_structured.call_count, 2)

    @mock.patch(
        "gemini_web2api.server.download_generated_image",
        return_value=(b"\x89PNG\r\n\x1a\n", "image/png"),
    )
    @mock.patch("gemini_web2api.server.get_full_size_image", return_value=None)
    @mock.patch("gemini_web2api.server.generate_image_structured")
    def test_responses_image_generation_returns_native_output(
        self, generate_image_structured, _full_size, _download
    ):
        from gemini_web2api.generated_image import GeneratedImage, GenerationResult
        generate_image_structured.return_value = GenerationResult(
            images=[GeneratedImage("https://lh3.googleusercontent.com/generated")]
        )
        status, _, body = self.post_json("/v1/responses", {
            "input": "make a red fox",
            "tools": [{"type": "image_generation"}],
        })
        self.assertEqual(status, 200)
        response = json.loads(body)
        self.assertEqual(response["output"][0]["type"], "image_generation_call")
        self.assertEqual(response["output"][0]["result"], "iVBORw0KGgo=")


        with mock.patch("gemini_web2api.server.rotation_worker_alive", return_value=False):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            connection.request("GET", "/")
            response = connection.getresponse()
            data = json.loads(response.read())
            connection.close()
        self.assertEqual(response.status, 503)
        self.assertEqual(data["status"], "unhealthy")

    def post_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def post_multipart(self, path, fields=None, files=None):
        boundary = "------------------------edits-test-boundary"
        body = bytearray()
        for name, value in (fields or {}).items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.extend(str(value).encode())
            body.extend(b"\r\n")
        for name, value in (files or {}).items():
            entries = value if isinstance(value, list) else [value]
            for filename, data, mime in entries:
                body.extend(f"--{boundary}\r\n".encode())
                body.extend(
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
                )
                body.extend(f"Content-Type: {mime}\r\n\r\n".encode())
                body.extend(data)
                body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        response = connection.getresponse()
        result = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, result

    def post_chunked_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            encode_chunked=True,
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    @mock.patch("gemini_web2api.server.generate_stream")
    def test_chat_stream_starts_with_assistant_role(self, generate_stream):
        generate_stream.return_value = iter(["hel", "lo"])

        status, headers, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        chunks = [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(chunks[1]["choices"][0]["delta"], {"content": "hel"})
        self.assertEqual(chunks[2]["choices"][0]["delta"], {"content": "lo"})
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    def test_chat_acknowledges_upstream_data_before_content(self):
        CONFIG["stream_heartbeat_sec"] = 0.01
        for tools_enabled in (False, True):
            with self.subTest(tools=tools_enabled):
                upstream_ready = threading.Event()
                finish = threading.Event()

                def acknowledge(kwargs):
                    if not upstream_ready.wait(5):
                        raise TimeoutError("test did not release upstream")
                    kwargs["on_first_response"]()
                    if not finish.wait(5):
                        raise TimeoutError("test did not release content")

                def buffered(*_args, **kwargs):
                    acknowledge(kwargs)
                    return '```client_operation\n{"name":"read","arguments":{}}\n```'

                def streaming(*_args, **kwargs):
                    acknowledge(kwargs)
                    yield "hello"

                def read_event(response):
                    lines = []
                    while True:
                        line = response.readline().decode()
                        if line == "\n" or not line:
                            return "".join(lines)
                        lines.append(line)

                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                target = "generate" if tools_enabled else "generate_stream"
                with mock.patch("gemini_web2api.server." + target, side_effect=buffered if tools_enabled else streaming):
                    payload = {"messages": [{"role": "user", "content": "hello"}], "stream": True}
                    if tools_enabled:
                        payload["tools"] = [{"type": "function", "function": {"name": "read"}}]
                    try:
                        connection.request("POST", "/v1/chat/completions", json.dumps(payload), {"Content-Type": "application/json"})
                        response = connection.getresponse()
                        self.assertEqual(read_event(response), ": keep-alive\n")
                        # A heartbeat before upstream data must also remain a comment.
                        self.assertEqual(read_event(response), ": keep-alive\n")
                        upstream_ready.set()
                        while True:
                            event = read_event(response)
                            if event.startswith("data: "):
                                break
                        chunk = json.loads(event[len("data: "):])
                        self.assertEqual(chunk["choices"][0]["delta"], {"role": "assistant"})
                        self.assertIsNone(chunk["choices"][0]["finish_reason"])
                        # The request is still generating, but post-ack heartbeats use data.
                        heartbeat = json.loads(read_event(response)[len("data: "):])
                        self.assertEqual(heartbeat["choices"][0]["delta"], {})
                        self.assertFalse(finish.is_set())
                        finish.set()
                        remainder = response.read().decode()
                        self.assertIn('"tool_calls"' if tools_enabled else '"hello"', remainder)
                        self.assertTrue(remainder.endswith("data: [DONE]\n\n"))
                    finally:
                        upstream_ready.set()
                        finish.set()
                        connection.close()

    @mock.patch("gemini_web2api.server.generate", side_effect=TimeoutError("no upstream body"))
    def test_chat_stream_error_before_upstream_data_has_no_role_ack(self, _generate):
        status, _, body = self.post_json("/v1/chat/completions", {
            "messages": [{"role": "user", "content": "read"}], "stream": True,
            "tools": [{"type": "function", "function": {"name": "read"}}],
        })
        self.assertEqual(status, 200)
        chunks = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
        self.assertEqual(len(chunks), 1)
        self.assertIn("error", chunks[0])
        self.assertNotIn('"role": "assistant"', body)
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    @mock.patch("gemini_web2api.server.parse_tool_calls")
    @mock.patch("gemini_web2api.server.generate", return_value="ignored")
    def test_chat_stream_emits_tool_calls_in_delta(self, _generate, parse_tool_calls):
        parse_tool_calls.return_value = (
            "",
            [{
                "id": "call_test",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": '{"path":"/tmp/fixture.txt"}',
                },
            }],
        )

        status, _, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "read the file"}],
                "tools": [{"type": "function", "function": {
                    "name": "read", "parameters": {"type": "object"}
                }}],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        chunks = [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: {")
        ]
        delta = chunks[0]["choices"][0]["delta"]
        self.assertEqual(delta["role"], "assistant")
        self.assertNotIn("tool_calls", delta)
        self.assertIsNone(chunks[0]["choices"][0]["finish_reason"])
        final = next(chunk for chunk in chunks if chunk["choices"][0]["finish_reason"] == "tool_calls")
        delta = final["choices"][0]["delta"]
        self.assertIsNone(delta["content"])
        self.assertEqual(delta["tool_calls"][0]["index"], 0)
        self.assertEqual(delta["tool_calls"][0]["function"]["name"], "read")
        self.assertEqual(final["choices"][0]["finish_reason"], "tool_calls")
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    @mock.patch("gemini_web2api.server.generate")
    def test_chat_tool_loop_continues_with_correlated_results(self, generate):
        generate.side_effect = [
            '```client_operation\n{"name":"read","arguments":{"path":"a.txt"}}\n```',
            '```tool_call\n{"name":"read","arguments":{"path":"b.txt"}}\n```',
            "Both files checked.",
        ]
        tools = [{"type": "function", "function": {
            "name": "read", "parameters": {"type": "object"}
        }}]
        messages = [{"role": "user", "content": "Check both files"}]
        for index in range(2):
            status, _, body = self.post_json("/v1/chat/completions", {
                "model": "gemini-3.6-flash", "messages": messages,
                "tools": tools, "stream": True,
            })
            self.assertEqual(status, 200)
            chunk = next(
                json.loads(line[len("data: "):])
                for line in body.splitlines()
                if line.startswith("data: {")
                and json.loads(line[len("data: "):])["choices"][0]["finish_reason"] == "tool_calls"
            )
            choice = chunk["choices"][0]
            self.assertEqual(choice["finish_reason"], "tool_calls")
            call = dict(choice["delta"]["tool_calls"][0])
            self.assertEqual(call.pop("index"), 0)
            messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": f"file-{index}-contents"})
        status, _, body = self.post_json("/v1/chat/completions", {
            "model": "gemini-3.6-flash", "messages": messages, "tools": tools,
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["finish_reason"], "stop")
        self.assertEqual(generate.call_count, 3)
        prompt = generate.call_args.args[0]
        for message in messages:
            if message["role"] == "tool":
                self.assertIn(f"[Tool result for read (call_id={message['tool_call_id']})]", prompt)
                self.assertIn(message["content"], prompt)

    @mock.patch("gemini_web2api.server.generate", return_value="Result checked.")
    def test_responses_preserves_top_level_function_call_history(self, generate):
        status, _, body = self.post_json("/v1/responses", {
            "model": "gemini-3.6-flash",
            "input": [
                {"role": "user", "content": "Check the file"},
                {"type": "function_call", "call_id": "call_read", "name": "read",
                 "arguments": '{"path":"a.txt"}'},
                {"type": "function_call_output", "call_id": "call_read", "output": "file contents"},
            ],
        })
        self.assertEqual(status, 200)
        prompt = generate.call_args.args[0]
        self.assertIn('"name": "read"', prompt)
        self.assertIn('[Tool result for read (call_id=call_read)]: file contents', prompt)
        self.assertEqual(json.loads(body)["status"], "completed")

    @mock.patch("gemini_web2api.server.generate_stream")
    def test_chat_stream_reports_upstream_error(self, generate_stream):
        def interrupted():
            yield "partial"
            raise RuntimeError("connection interrupted")

        generate_stream.return_value = interrupted()
        status, _, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertIn('"type": "server_error"', body)
        self.assertIn('"code": "upstream_stream_error"', body)
        self.assertIn('"retryable": false', body)
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    @mock.patch("gemini_web2api.server.generate", return_value="chunked ok")
    def test_chat_accepts_chunked_body(self, _generate):
        status, _, body = self.post_chunked_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "chunked ok")

    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/image-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="looks good")
    def test_chat_accepts_openai_image_url_data_url(self, generate, upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_data}"
                            },
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        upload_image.assert_called_once_with(b"fake png", "image.png", "image/png")
        self.assertEqual(generate.call_args.args[3], [("/uploaded/image-ref", "image.png")])
        self.assertIn("[Image attached]", generate.call_args.args[0])
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "looks good")

    @mock.patch("gemini_web2api.server.fetch_image_bytes", return_value=b"\xff\xd8\xffremote jpeg")
    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/remote-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="remote ok")
    def test_responses_accepts_input_image_url(self, generate, upload_image, fetch_image_bytes):
        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is shown?"},
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/image.jpg",
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        fetch_image_bytes.assert_called_once_with("https://example.com/image.jpg")
        upload_image.assert_called_once_with(b"\xff\xd8\xffremote jpeg", "image.png", "image/jpeg")
        self.assertEqual(generate.call_args.args[3], [("/uploaded/remote-ref", "image.png")])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/image-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="top-level image ok")
    def test_responses_accepts_top_level_input_image(self, generate, upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [
                    {"type": "input_text", "text": "What is shown?"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_data}",
                    },
                ],
            },
        )

        self.assertEqual(status, 200)
        upload_image.assert_called_once_with(b"fake png", "image.png", "image/png")
        self.assertEqual(generate.call_args.args[3], [("/uploaded/image-ref", "image.png")])
        self.assertIn("What is shown?", generate.call_args.args[0])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("gemini_web2api.server.upload_image", side_effect=RuntimeError("upload denied"))
    def test_google_image_upload_failure_returns_502(self, _upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:generateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": image_data,
                        },
                    }],
                }],
            },
        )

        self.assertEqual(status, 502)
        self.assertIn("image upload failed: upload denied", json.loads(body)["error"]["message"])

    @mock.patch("gemini_web2api.server.generate_stream", return_value=iter(["streamed"]))
    def test_google_stream_generate_content_uses_sse(self, _generate_stream):
        status, headers, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{"text": "Stream this"}],
                }],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn('"text": "streamed"', body)

    @mock.patch("gemini_web2api.server.generate", return_value="hello")
    def test_responses_text_stream_has_complete_event_sequence(self, _generate):
        status, headers, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "hello",
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[4][1]["delta"], "hello")
        self.assertEqual(events[-1][1]["response"]["status"], "completed")
        self.assertEqual(events[-1][1]["response"]["output"][0]["content"][0]["text"], "hello")

    @mock.patch("gemini_web2api.server.parse_tool_calls")
    @mock.patch("gemini_web2api.server.generate", return_value="tool output")
    def test_responses_function_call_stream_has_complete_event_sequence(
        self, _generate, parse_tool_calls
    ):
        parse_tool_calls.return_value = (
            "",
            [
                {
                    "id": "call_test",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Shanghai"}'},
                }
            ],
        )

        status, _, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "weather",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object"},
                    }
                ],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[2][1]["output_index"], 0)
        self.assertEqual(events[3][1]["delta"], '{"city":"Shanghai"}')
        self.assertEqual(events[4][1]["arguments"], '{"city":"Shanghai"}')
        self.assertEqual(events[-1][1]["response"]["output"][0]["name"], "get_weather")


if __name__ == "__main__":
    unittest.main()
