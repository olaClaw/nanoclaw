import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("privacy-image-audit.py")
SPEC = importlib.util.spec_from_file_location("privacy_image_audit", MODULE_PATH)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)
REVISION = "a" * 40


def tar_bytes(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def image_archive(destination, layers, revision=REVISION, env=None, healthcheck=None):
    layer_names = ["layer" + str(index) + "/layer.tar" for index in range(len(layers))]
    config = {
        "config": {"Env": env or ["NODE_ENV=production"], "Labels": {"org.opencontainers.image.revision": revision}, "Healthcheck": healthcheck},
        "history": [{"created_by": "fixture"}],
    }
    manifest = [{"Config": "config.json", "Layers": layer_names, "RepoTags": ["fixture:local"]}]
    files = {
        "manifest.json": json.dumps(manifest),
        "config.json": json.dumps(config),
    }
    files.update(zip(layer_names, [tar_bytes(layer) for layer in layers]))
    destination.write_bytes(tar_bytes(files))


class ImageAuditTests(unittest.TestCase):
    def test_detects_secret_in_layer_even_after_later_whiteout(self):
        address = ".".join(["10", "52", "4", "3"])
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "image.tar"
            image_archive(archive, [
                {"usr/share/base.txt": "inherited"},
                {"app/.env": "FIXTURE=example", "app/src/index.js": address},
                {"app/.wh..env": "", "app/src/index.js": "clean"},
            ])
            counts = AUDIT.check_archive(archive, 1, set(), REVISION)
            self.assertGreater(counts.get("runtime-or-secret-path", 0), 0)
            self.assertGreater(counts.get("private-ipv4", 0), 0)
            self.assertNotIn(address, json.dumps(counts))

    def test_clean_layer_and_revision_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "image.tar"
            image_archive(archive, [{"usr/share/base.txt": "inherited"}, {"app/src/index.js": "clean"}])
            self.assertEqual(AUDIT.check_archive(archive, 1, set(), REVISION), {})

    def test_requires_expected_revision_and_rejects_unsafe_layer_path(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "image.tar"
            image_archive(archive, [{"app/src/index.js": "clean"}], revision="b" * 40)
            counts = AUDIT.check_archive(archive, 0, set(), REVISION)
            self.assertGreater(counts.get("missing-or-wrong-revision", 0), 0)
            self.assertRaises(ValueError, AUDIT.image_path, "../outside")

    def test_rejects_secret_environment_assignment(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "image.tar"
            image_archive(archive, [{"app/src/index.js": "clean"}], env=["APP_TOKEN=fixture"])
            counts = AUDIT.check_archive(archive, 0, set(), REVISION)
            self.assertGreater(counts.get("secret-in-image-env", 0), 0)

    def test_scans_healthcheck_metadata(self):
        address = ".".join(["10", "52", "4", "3"])
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "image.tar"
            image_archive(archive, [{"app/src/index.js": "clean"}], healthcheck={"Test": ["CMD", "ping", address]})
            counts = AUDIT.check_archive(archive, 0, set(), REVISION)
            self.assertGreater(counts.get("private-ipv4", 0), 0)

    def test_scans_javascript_with_nul_without_treating_it_as_binary(self):
        address = ".".join(["10", "52", "4", "3"])
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "image.tar"
            image_archive(archive, [{"app/dist/driver.js": b"prefix\0" + address.encode()}])
            counts = AUDIT.check_archive(archive, 0, set(), REVISION)
            self.assertGreater(counts.get("private-ipv4", 0), 0)
            self.assertFalse(any(key.startswith("binary-or-large") for key in counts))

    def test_scans_shared_host_root_in_every_layer(self):
        address = ".".join(["10", "52", "4", "3"])
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "image.tar"
            image_archive(archive, [
                {"usr/share/base.txt": "inherited"},
                {"srv/nanoclaw/data/runtime.json": "fixture", "srv/nanoclaw/dist/index.js": address},
                {"srv/nanoclaw/data/.wh.runtime.json": "", "srv/nanoclaw/dist/index.js": "clean"},
            ])
            counts = AUDIT.check_archive(archive, 1, set(), REVISION)
            self.assertGreater(counts.get("runtime-or-secret-path", 0), 0)
            self.assertGreater(counts.get("private-ipv4", 0), 0)
            self.assertNotIn(address, json.dumps(counts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
