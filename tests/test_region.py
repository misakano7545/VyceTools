"""自定义区域（region_allow / --region / --any-region）的离线验证。

设计要点：
  * region_preference 只是排序，不限制范围（老行为，保持不变）；
  * region_allow / --region 是严格白名单，不在里面的区域即使开放也不下单；
  * 未知区域必须报错，不能静默忽略（否则表现是「一直监听却永不抢」）。
"""
import argparse
import importlib.util
import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "vyce.py"
spec = importlib.util.spec_from_file_location("vyce_region", SRC)
vyce = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules["vyce_region"] = vyce
spec.loader.exec_module(vyce)  # type: ignore[union-attr]

REGIONS = {"Europe": {"freeOpen": False, "open": True},
           "Asia": {"freeOpen": True, "open": True},
           "USA": {"freeOpen": False, "open": False}}


def snap(regions=None, open_=None):
    regions = regions or REGIONS
    return {"open": bool(open_) if open_ is not None else any(
        st["freeOpen"] for st in regions.values()),
        "enabled": True, "regions": regions}


def args(region=None, any_region=False):
    return argparse.Namespace(region=region, any_region=any_region)


class ParseTest(unittest.TestCase):
    def test_comma_and_repeat_and_fullwidth(self):
        self.assertEqual(vyce.parse_region_values(["Asia,USA", "eu"]), ["Asia", "USA", "eu"])
        self.assertEqual(vyce.parse_region_values(["亚洲，欧洲"]), ["亚洲", "欧洲"])
        self.assertEqual(vyce.parse_region_values(None), [])
        self.assertEqual(vyce.parse_region_values([" , "]), [])


class NormalizeTest(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertEqual(vyce.normalize_regions(["asia"], REGIONS, strict=True), ["Asia"])
        self.assertEqual(vyce.normalize_regions(["EUROPE"], REGIONS, strict=True), ["Europe"])

    def test_aliases(self):
        self.assertEqual(vyce.normalize_regions(["eu", "us", "ap"], REGIONS, strict=True),
                         ["Europe", "USA", "Asia"])
        self.assertEqual(vyce.normalize_regions(["欧洲"], REGIONS, strict=True), ["Europe"])

    def test_dedup_keeps_order(self):
        self.assertEqual(vyce.normalize_regions(["asia", "Asia", "eu"], REGIONS, strict=True),
                         ["Asia", "Europe"])

    def test_unknown_is_fatal_when_strict(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            vyce.normalize_regions(["asia-pacfic"], REGIONS, strict=True)
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("未知区域", err.getvalue())

    def test_unknown_is_dropped_when_not_strict(self):
        out = vyce.normalize_regions(["nope", "asia"], REGIONS, strict=False)
        self.assertEqual(out, ["Asia"])


class PoolTest(unittest.TestCase):
    def test_preference_only_orders(self):
        pool = vyce.region_pool(snap(), ["USA", "Europe"])
        self.assertEqual(pool, ["USA", "Europe", "Asia"])

    def test_allow_restricts(self):
        pool = vyce.region_pool(snap(), ["USA", "Europe"], ["Asia", "Europe"])
        self.assertEqual(pool, ["Europe", "Asia"], "白名单外的 USA 必须被剔除")

    def test_free_regions_respects_allow(self):
        self.assertEqual(vyce.free_regions(snap(), ["Asia"], None), ["Asia"])
        self.assertEqual(vyce.free_regions(snap(), ["Asia"], ["Europe"]), [],
                         "名额在 Asia，但白名单只有 Europe → 不该抢")

    def test_unknown_preference_ignored(self):
        self.assertEqual(vyce.region_pool(snap(), ["Mars"], None), ["Europe", "Asia", "USA"])


class FilterTest(unittest.TestCase):
    def test_cli_overrides_config(self):
        cfg = {"region_preference": ["Europe"], "region_allow": ["USA"]}
        pref, allow = vyce.resolve_region_filter(cfg, args(["asia"]), REGIONS)
        self.assertEqual(allow, ["Asia"])
        self.assertEqual(pref, ["Europe"], "preference 保持不动，只做排序")

    def test_any_region_clears_both(self):
        cfg = {"region_preference": ["Europe"], "region_allow": ["USA"]}
        pref, allow = vyce.resolve_region_filter(cfg, args(any_region=True), REGIONS)
        self.assertEqual((pref, allow), ([], []))
        self.assertEqual(vyce.free_regions(snap(), pref, allow), ["Asia"])

    def test_config_allow_used_without_cli(self):
        cfg = {"region_preference": [], "region_allow": ["eu"]}
        _, allow = vyce.resolve_region_filter(cfg, args(), REGIONS)
        self.assertEqual(allow, ["Europe"])


class FakeApi:
    """只覆盖 deploy 需要的四个接口，记录所有请求。"""

    def __init__(self, servers=None):
        self.servers = servers or []
        self.calls = []

    def request(self, method, path, body=None, retries=2):
        self.calls.append((method, path, body))
        if path == "/api/me":
            return {"user": {"login": "tester"}}, {}
        if method == "GET" and path == "/api/servers":
            return {"servers": self.servers}, {}
        if method == "GET" and path == "/api/keys":
            return {"keys": [{"id": "k-1", "name": "k"}]}, {}
        if method == "POST" and path == "/api/servers":
            return {"server": {"id": "new-id", "status": "creating"}}, {}
        if method == "GET" and path.startswith("/api/servers/"):
            return {"server": {"id": "new-id", "status": "running",
                               "ipv6": "2a11::1",
                               "expiresAt": int(time.time() * 1000) + 48 * 3600_000}}, {}
        raise AssertionError(f"unexpected {method} {path}")


class DeployRegionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vyce-region-"))
        self._old = (vyce.DATA_DIR, vyce.LAST_SERVER)
        vyce.DATA_DIR = self.tmp
        vyce.LAST_SERVER = self.tmp / "last-server.json"
        self.cfg = {"region_preference": ["Europe", "Asia", "USA"], "server_name": "vyce-mini",
                    "os": "ubuntu-24"}

    def tearDown(self):
        vyce.DATA_DIR, vyce.LAST_SERVER = self._old

    def _run(self, api, region_filter, dry_run=False):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rec = vyce.deploy(api, self.cfg, snap(), dry_run=dry_run, region_filter=region_filter)
        return rec, buf.getvalue()

    def test_refuses_when_free_region_outside_allowlist(self):
        api = FakeApi()
        rec, out = self._run(api, ([], ["Europe"]))
        self.assertIsNone(rec)
        self.assertIn("不在你指定的区域", out)
        self.assertFalse([c for c in api.calls if c[0] == "POST"], "白名单外不该下单")

    def test_deploys_inside_allowlist(self):
        api = FakeApi()
        rec, out = self._run(api, ([], ["Asia"]))
        self.assertIsNotNone(rec, out)
        post = [b for m, p, b in api.calls if m == "POST"][0]
        self.assertEqual(post["region"], "Asia")

    def test_dry_run_uses_first_allowed_region_when_nothing_free(self):
        api = FakeApi()
        closed = snap({k: dict(v, freeOpen=False) for k, v in REGIONS.items()}, open_=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            vyce.deploy(api, self.cfg, closed, dry_run=True, region_filter=(["USA"], ["USA", "Asia"]))
        self.assertIn('"region": "USA"', buf.getvalue())
        self.assertFalse([c for c in api.calls if c[0] == "POST"])

    def test_allowlist_all_bogus_dies_in_dry_run(self):
        api = FakeApi()
        closed = snap({k: dict(v, freeOpen=False) for k, v in REGIONS.items()}, open_=False)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            vyce.deploy(api, self.cfg, closed, dry_run=True, region_filter=([], ["Mars"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
