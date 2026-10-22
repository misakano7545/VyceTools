"""离线验证：watch 的状态翻转检测、日志落盘与 stats 统计。

不联网：把 fetch_stock 换成脚本化的假数据，跑 main(['watch', ...])。
用法：python -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "vyce.py"
spec = importlib.util.spec_from_file_location("vyce", SRC)
vyce = importlib.util.module_from_spec(spec)
sys.modules["vyce"] = vyce
spec.loader.exec_module(vyce)

CLOSED = {"open": False, "enabled": True,
          "regions": {"Europe": {"freeOpen": False, "open": True},
                      "Asia": {"freeOpen": False, "open": True},
                      "USA": {"freeOpen": False, "open": False}}}
OPEN_EU = {"open": True, "enabled": True,
           "regions": {"Europe": {"freeOpen": True, "open": True},
                       "Asia": {"freeOpen": False, "open": True},
                       "USA": {"freeOpen": False, "open": False}}}
OPEN_ASIA_ONLY = {"open": True, "enabled": True,
                  "regions": {"Europe": {"freeOpen": False, "open": True},
                              "Asia": {"freeOpen": True, "open": True},
                              "USA": {"freeOpen": False, "open": False}}}


class WatchSimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vyce-test-"))
        self._old_data = vyce.DATA_DIR
        self._old_log = vyce.STOCK_LOG
        vyce.DATA_DIR = self.tmp
        vyce.STOCK_LOG = self.tmp / "stock.jsonl"
        self._old_fetch = vyce.fetch_stock
        self._old_cfg = vyce.CONFIG_PATH
        vyce.CONFIG_PATH = self.tmp / "config.json"

    def tearDown(self):
        vyce.DATA_DIR = self._old_data
        vyce.STOCK_LOG = self._old_log
        vyce.fetch_stock = self._old_fetch
        vyce.CONFIG_PATH = self._old_cfg

    def _run(self, seq, seconds=5):
        seq = list(seq)

        def fake(_api):
            return seq.pop(0) if seq else CLOSED

        vyce.fetch_stock = fake
        buf = io.StringIO()
        with redirect_stdout(buf):
            vyce.main(["watch", "--interval", "1", "--seconds", str(seconds)])
        return buf.getvalue()

    def test_transition_is_logged(self):
        out = self._run([CLOSED, CLOSED, OPEN_EU, OPEN_EU, CLOSED])
        self.assertIn("名额开放", out)
        self.assertIn("Europe", out)
        rows = [json.loads(l) for l in vyce.STOCK_LOG.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2, f"应有 open/close 两条翻转记录，实际 {rows}")
        self.assertTrue(rows[0]["open"])
        self.assertEqual(rows[0]["free_regions"], ["Europe"])
        self.assertFalse(rows[1]["open"])
        self.assertIn("latency_ms", rows[0])

    def test_region_change_is_logged(self):
        out = self._run([OPEN_EU, OPEN_ASIA_ONLY, CLOSED])
        rows = [json.loads(l) for l in vyce.STOCK_LOG.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["free_regions"], ["Asia"])
        self.assertIn("Asia", out)

    def test_stats_aggregates(self):
        self._run([CLOSED, OPEN_EU, CLOSED])
        out = io.StringIO()
        with redirect_stdout(out):
            vyce.main(["stats"])
        text = out.getvalue()
        self.assertIn("记录条数", text)
        self.assertIn("放号时段", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
