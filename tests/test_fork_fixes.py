"""Regression checks for fork-specific persistence and concurrent downloads."""

import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "biliread_fork_under_test"


def load_plugin():
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PACKAGE_NAME] = package

    bili_api = types.ModuleType("bilibili_api")
    bili_api.Credential = type("Credential", (), {"__init__": lambda self, **kw: None})
    bili_api.video = types.SimpleNamespace()
    sys.modules["bilibili_api"] = bili_api
    return importlib.import_module(f"{PACKAGE_NAME}.main")


class SaveableConfig(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1


class FakeContext:
    def add_llm_tools(self, *tools):
        self.tools = tools


class FakeEvent:
    def get_sender_id(self):
        return "123"

    def plain_result(self, text):
        return text


class ForkFixesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin_module = load_plugin()

    def test_toggles_save_dict_based_config(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                config = SaveableConfig(admin_id="123", llm_provider_id="model")
                with patch.object(
                    self.plugin_module.StarTools, "get_data_dir", return_value=temp_dir
                ), patch.object(self.plugin_module, "BilibiliTool") as fake_tool:
                    fake_tool.return_value.plugin_state = {"enabled": True}
                    fake_tool.return_value.enable_audio_fallback = True
                    plugin = self.plugin_module.BiliRead(FakeContext(), config)
                event = FakeEvent()
                summary_result = [x async for x in plugin.toggle_feature(event)]
                audio_result = [x async for x in plugin.toggle_transcription(event)]
                self.assertEqual(config.saved, 2)
                self.assertFalse(config["enable_summary"])
                self.assertFalse(config["enable_audio_fallback"])
                self.assertEqual(len(summary_result), 1)
                self.assertEqual(len(audio_result), 1)

        asyncio.run(run())

    def test_concurrent_downloads_use_separate_directories(self):
        class FakeYoutubeDL:
            cookie_paths = []

            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download):
                output_dir = Path(self.options["outtmpl"]).parent
                self.cookie_paths.append(self.options["cookiefile"])
                (output_dir / "BV1GJ411x7h7.mp3").write_bytes(b"audio")
                return {"id": "BV1GJ411x7h7"}

        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                login = self.plugin_module.BilibiliLogin(temp_dir)
                login._cookies = {"SESSDATA": "dummy"}
                tool = self.plugin_module.BilibiliTool(
                    data_dir=temp_dir, bili_login=login
                )
                fake_yt_dlp = types.ModuleType("yt_dlp")
                fake_yt_dlp.YoutubeDL = FakeYoutubeDL
                with patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}):
                    paths = await asyncio.gather(
                        tool._download_audio("BV1GJ411x7h7"),
                        tool._download_audio("BV1GJ411x7h7"),
                    )
                self.assertTrue(all(paths))
                self.assertNotEqual(os.path.dirname(paths[0]), os.path.dirname(paths[1]))
                self.assertEqual(len(set(FakeYoutubeDL.cookie_paths)), 2)
                self.assertTrue(all(Path(path).exists() for path in paths))
                for path in paths:
                    tool._cleanup_file(path)
                    self.assertFalse(Path(path).parent.exists())

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
