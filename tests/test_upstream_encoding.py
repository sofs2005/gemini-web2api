import unittest
from unittest import mock

from gemini_web2api import gemini


class UpstreamEncodingTest(unittest.TestCase):
    """Guards the Brotli regression: the buffered transport reads via urllib,
    which cannot decode Content-Encoding. Advertising br/zstd made Google answer
    with Brotli, the payload parsed to zero candidates, and every request came
    back as HTTP 200 with an empty assistant message."""

    def test_buffered_headers_request_identity(self):
        headers = gemini._build_headers()
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_identity_body_is_accepted(self):
        body = ')]}\'\n\n123\n[["wrb.fr",null,"[null,[\\"c_1\\",\\"r_1\\"]]"]]\n'
        response = mock.MagicMock()
        response.status = 200
        response.headers = {"Content-Encoding": "identity"}
        response.read.return_value = body.encode()
        with mock.patch.object(gemini.urllib.request, "urlopen", return_value=response), \
                mock.patch.object(gemini, "load_cookie", return_value=("", None)), \
                mock.patch.object(gemini, "_get_ssl_ctx", return_value=None):
            self.assertEqual(gemini.generate("hi", 1, 4), "")

    def test_compressed_body_fails_loudly(self):
        response = mock.MagicMock()
        response.status = 200
        response.headers = {"Content-Encoding": "br"}
        response.read.return_value = b"\x83\x00brotli-bytes"
        with mock.patch.object(gemini.urllib.request, "urlopen", return_value=response), \
                mock.patch.object(gemini, "load_cookie", return_value=("", None)), \
                mock.patch.object(gemini, "_get_ssl_ctx", return_value=None):
            with self.assertRaises(gemini.GeminiUpstreamError):
                gemini.generate("hi", 1, 4)


if __name__ == "__main__":
    unittest.main()
