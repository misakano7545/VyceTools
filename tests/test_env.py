"""环境变量配置层的离线验证（GitHub Actions 里没有 config.json，全靠这一层）。"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "vyce.py"
spec = importlib.util.spec_from_file_location("vyce_env", SRC)
vyce = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules["vyce_env"] = vyce
spec.loader.exec_module(vyce)  # type: ignore[union-attr]


class EnvParseTest(unittest.TestCase):
    def test_types(self):
        got = vyce.env_overrides({
            "VYCE_POLL_INTERVAL": "3.5",
            "VYCE_MAX_WAIT": "900",
            "VYCE_CAPTCHA_SOLVER": "false",
            "VYCE_SERVER_NAME": "my-mini",
            "VYCE_REGION": "asia, eu",
        })
        self.assertEqual(got["poll_interval"], 3.5)
        self.assertEqual(got["max_deploy_wait_seconds"], 900)
        self.assertIs(got["captcha_solver"], False)
        self.assertEqual(got["server_name"], "my-mini")
        self.assertEqual(got["region_allow"], ["asia", "eu"])

    def test_bool_spellings(self):
        for raw, want in [("1", True), ("TRUE", True), ("yes", True), ("on", True),
                          ("0", False), ("false", False), ("off", False)]:
            self.assertIs(vyce.env_overrides({"VYCE_CAPTCHA_SOLVER": raw})["captcha_solver"], want, raw)

    def test_empty_means_unset(self):
        self.assertEqual(vyce.env_overrides({"VYCE_REGION": "  ", "VYCE_POLL_INTERVAL": ""}), {})

    def test_fullwidth_comma(self):
        self.assertEqual(vyce.env_overrides({"VYCE_REGION": "欧洲，亚洲"})["region_allow"],
                         ["欧洲", "亚洲"])

    def test_bad_values_are_fatal_with_var_name(self):
        import io
        from contextlib import redirect_stderr
        for env in ({"VYCE_POLL_INTERVAL": "fast"}, {"VYCE_MAX_WAIT": "很多"},
                    {"VYCE_CAPTCHA_SOLVER": "maybe"}):
            err = io.StringIO()
            name = next(iter(env))
            with redirect_stderr(err), self.assertRaises(SystemExit):
                vyce.env_overrides(env)
            self.assertIn(name, err.getvalue(), "报错必须点名是哪个变量，否则 CI 里没法排查")


class PrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vyce-env-"))
        self._old_cfg = vyce.CONFIG_PATH
        vyce.CONFIG_PATH = self.tmp / "config.json"
        self._env_backup = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("VYCE_"):
                del os.environ[k]

    def tearDown(self):
        vyce.CONFIG_PATH = self._old_cfg
        for k in list(os.environ):
            if k.startswith("VYCE_"):
                del os.environ[k]
        os.environ.update(self._env_backup)

    def test_without_any_file_uses_defaults(self):
        cfg = vyce.load_config()
        self.assertEqual(cfg["base_url"], "https://vyce.cloud")
        self.assertEqual(cfg["poll_interval"], 2.0)
        self.assertEqual(cfg["region_allow"], [])

    def test_file_then_env_env_wins(self):
        vyce.CONFIG_PATH.write_text(json.dumps(
            {"poll_interval": 9.0, "region_allow": ["USA"], "server_name": "from-file"}),
            encoding="utf-8")
        os.environ["VYCE_POLL_INTERVAL"] = "4"
        cfg = vyce.load_config()
        self.assertEqual(cfg["poll_interval"], 4.0, "环境变量应覆盖 config.json")
        self.assertEqual(cfg["region_allow"], ["USA"], "文件里有的、env 没给的要保留")
        self.assertEqual(cfg["server_name"], "from-file")

    def test_env_region_feeds_whitelist(self):
        os.environ["VYCE_REGION"] = "asia"
        cfg = vyce.load_config()
        regions = {"Europe": {"freeOpen": True, "open": True},
                   "Asia": {"freeOpen": True, "open": True},
                   "USA": {"freeOpen": False, "open": False}}
        pref, allow = vyce.resolve_region_filter(cfg, None, regions)
        self.assertEqual(allow, ["Asia"], "VYCE_REGION=asia 必须变成严格白名单")
        self.assertEqual(vyce.free_regions({"open": True, "enabled": True, "regions": regions},
                                           pref, allow), ["Asia"])

    def test_watch_seconds_from_env(self):
        os.environ["VYCE_WATCH_SECONDS"] = "270"
        cfg = vyce.load_config()
        self.assertEqual(cfg["watch_seconds"], 270)


class CookiePrecedenceTest(unittest.TestCase):
    """session.txt 优先，其次 VYCE_COOKIE（CI 里只有后者）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vyce-cookie-"))
        self._old = vyce.SESSION_PATH
        vyce.SESSION_PATH = self.tmp / "session.txt"
        self._env = os.environ.get("VYCE_COOKIE")
        os.environ.pop("VYCE_COOKIE", None)

    def tearDown(self):
        vyce.SESSION_PATH = self._old
        os.environ.pop("VYCE_COOKIE", None)
        if self._env is not None:
            os.environ["VYCE_COOKIE"] = self._env

    def test_env_only(self):
        os.environ["VYCE_COOKIE"] = "__Host-vyce_sid=abc"
        self.assertEqual(vyce.load_cookie(), "__Host-vyce_sid=abc")

    def test_file_wins_and_is_cleanable(self):
        vyce.SESSION_PATH.write_text("Cookie: __Host-vyce_sid=fromfile\r\n", encoding="utf-8")
        os.environ["VYCE_COOKIE"] = "__Host-vyce_sid=fromenv"
        self.assertEqual(vyce.load_cookie(), "__Host-vyce_sid=fromfile",
                         "本地有 session.txt 时应优先，且要去掉 Cookie: 前缀与换行")


class CheckFreeExitCodeTest(unittest.TestCase):
    def test_exit_codes(self):
        class NoServer:
            cookie = "c=1"

            def request(self, method, path, body=None, retries=2):
                return {"servers": []}, {}

        self.assertEqual(vyce.cmd_check_free(NoServer()), 1, "没有免费机 → exit 1（该去抢）")

        class HasFree:
            cookie = "c=1"

            def request(self, method, path, body=None, retries=2):
                return {"servers": [{"id": "x", "name": "vyce-mini", "source": "free",
                                     "status": "running", "expiresAt": 1792842862101}]}, {}

        self.assertEqual(vyce.cmd_check_free(HasFree()), 0, "有免费机 → exit 0（跳过抢机）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
