import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/serv00/install_wheels.py"
SPEC = importlib.util.spec_from_file_location("serv00_install_wheels", SCRIPT)
install_wheels = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_wheels)


class Serv00InstallWheelsTests(unittest.TestCase):
    def _wheel(self, root, name, files):
        path = root / name
        with zipfile.ZipFile(path, "w") as archive:
            for member, content in files.items():
                archive.writestr(member, content)
        return path

    def test_extracts_only_manifest_wheels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            site = root / "site"
            wanted = self._wheel(
                wheelhouse, "demo-1.0-py3-none-any.whl",
                {"demo/__init__.py": "VALUE = 1\n"})
            self._wheel(
                wheelhouse, "stray-1.0-py3-none-any.whl",
                {"stray/__init__.py": "VALUE = 2\n"})
            manifest = {wanted.name: hashlib.sha256(wanted.read_bytes()).hexdigest()}
            (wheelhouse / "SHA256SUMS.json").write_text(json.dumps(manifest))

            installed = install_wheels.install(wheelhouse, site)

            self.assertEqual([path.name for path in installed], [wanted.name])
            self.assertTrue((site / "demo/__init__.py").is_file())
            self.assertFalse((site / "stray").exists())

    def test_rejects_manifest_path_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "SHA256SUMS.json").write_text(
                json.dumps({"../evil.whl": "0" * 64}))
            with self.assertRaises(SystemExit):
                install_wheels.install(wheelhouse, root / "site")

    def test_rejects_unsafe_wheel_member(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            wheel = self._wheel(
                wheelhouse, "demo-1.0-py3-none-any.whl",
                {"../escape.py": "bad\n"})
            (wheelhouse / "SHA256SUMS.json").write_text(
                json.dumps({wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()}))
            with self.assertRaises(SystemExit):
                install_wheels.install(wheelhouse, root / "site")


if __name__ == "__main__":
    unittest.main()
