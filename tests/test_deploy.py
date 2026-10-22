"""下单响应形状与幂等闸门的离线验证。

真实抢机时踩到的坑：POST /api/servers 返回 {server:{...},credentials:{...}}，
直接当裸 server 对象读 id 会得到 None，后续 GET /servers/None 全部 400。
"""
import importlib.util
import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "vyce.py"
spec = importlib.util.spec_from_file_location("vyce2", SRC)
vyce = importlib.util.module_from_spec(spec)
sys.modules["vyce2"] = vyce
spec.loader.exec_module(vyce)

OPEN_EU = {"open": True, "enabled": True,
           "regions": {"Europe": {"freeOpen": True, "open": True},
                       "Asia": {"freeOpen": False, "open": True},
                       "USA": {"freeOpen": False, "open": False}}}


class FakeApi:
    def __init__(self, servers, create_payload=None):
        self.servers = servers
        self.create_payload = create_payload or {}
        self.calls = []

    def request(self, method, path, body=None, retries=2):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/api/me":
            return {"user": {"login": "tester"}}, {}
        if method == "GET" and path == "/api/servers":
            return {"servers": self.servers}, {}
        if method == "GET" and path == "/api/keys":
            return {"keys": [{"id": "uuid-key-1", "name": "k"}]}, {}
        if method == "POST" and path == "/api/servers":
            return self.create_payload, {}
        if method == "GET" and path.startswith("/api/servers/"):
            return {"server": {"id": path.rsplit("/", 1)[-1], "status": "running",
                               "ipv6": "2a11::1", "expiresAt": int(time.time() * 1000) + 48 * 3600_000}}, {}
        raise AssertionError(f"unexpected {method} {path}")


class UnwrapTest(unittest.TestCase):
    def test_wrapped_shape(self):
        payload = {"server": {"id": "abc", "status": "creating"}, "credentials": {"username": "root"}}
        self.assertEqual(vyce.unwrap_created(payload)["id"], "abc")

    def test_bare_shape(self):
        self.assertEqual(vyce.unwrap_created({"id": "abc"})["id"], "abc")

    def test_garbage(self):
        self.assertEqual(vyce.unwrap_created({}), {})
        self.assertEqual(vyce.unwrap_created(None), {})


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vyce-deploy-"))
        self._old = (vyce.DATA_DIR, vyce.LAST_SERVER)
        vyce.DATA_DIR = self.tmp
        vyce.LAST_SERVER = self.tmp / "last-server.json"

    def tearDown(self):
        vyce.DATA_DIR, vyce.LAST_SERVER = self._old

    def _run(self, api, dry_run=False):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rec = vyce.deploy(api, {"region_preference": ["Europe"], "server_name": "vyce-mini",
                                    "os": "ubuntu-24"}, OPEN_EU, dry_run=dry_run)
        return rec, buf.getvalue()

    def test_wrapped_response_yields_record(self):
        api = FakeApi([], {"server": {"id": "uuid-123", "status": "creating"},
                           "credentials": {"username": "root", "password": "x"}})
        rec, out = self._run(api)
        self.assertIsNotNone(rec, out)
        self.assertEqual(rec["id"], "uuid-123")
        self.assertEqual(rec["ipv6"], "2a11::1")
        self.assertTrue(vyce.LAST_SERVER.exists())

    def test_has_free_server_blocks_second(self):
        api = FakeApi([{"id": "old", "source": "free", "name": "old"}])
        rec, out = self._run(api)
        self.assertIsNone(rec)
        self.assertIn("一人一台", out)
        self.assertFalse(any(m == "POST" for m, _, _ in api.calls), "已有免费机时不该发下单请求")

    def test_os_id_is_mapped_to_display_name(self):
        api = FakeApi([], {"server": {"id": "uuid-9", "status": "creating"}})
        self._run(api)
        post = [b for m, p, b in api.calls if m == "POST"][0]
        self.assertEqual(post["os"], "Ubuntu 24.04", "接口只吃展示名，id 必须映射")

    def test_key_ids_are_strings(self):
        api = FakeApi([], {"server": {"id": "uuid-9", "status": "creating"}})
        self._run(api)
        post = [b for m, p, b in api.calls if m == "POST"][0]
        self.assertEqual(post["keyIds"], ["uuid-key-1"], "key id 是 UUID，不能转 int")


if __name__ == "__main__":
    unittest.main(verbosity=2)
