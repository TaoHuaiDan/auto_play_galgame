from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


ROOT = Path(__file__).resolve().parents[1]


async def _protocol_smoke(data_dir: str) -> tuple[set[str], bool, bool, bool]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["GALGAME_MCP_DATA_DIR"] = data_dir
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "galgame_mcp.server"],
        cwd=str(ROOT),
        env=environment,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            started = await session.call_tool("start_session", {"game_name": "协议测试"})
            configured = await session.call_tool(
                "configure_game_layout",
                {
                    "profile": {
                        "speaker_markers": [{"open": "<N>", "close": "</N>"}],
                        "dialogue_markers": [{"open": "<T>", "close": "</T>"}],
                    }
                },
            )
            timing_configured = await session.call_tool(
                "configure_game_timing",
                {"profile": {"strategy": "text_hash", "stable_samples": 2}},
            )
            context = await session.call_tool("get_codex_context", {"recent_events": 5})
            captured = await session.call_tool(
                "capture_screen",
                {"include_image": True},
            )
            parsed = await session.call_tool(
                "parse_text",
                {"raw_text": "<N>小葵</N>\n<T>今日は一緒に帰らない？</T>"},
            )
            has_image = any(getattr(block, "type", None) == "image" for block in captured.content)
            return (
                names,
                not started.isError,
                not configured.isError
                and not timing_configured.isError
                and not context.isError
                and not parsed.isError
                and not captured.isError,
                has_image,
            )


class ProtocolTests(unittest.TestCase):
    def test_stdio_handshake_and_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            names, started_ok, context_ok, image_ok = asyncio.run(_protocol_smoke(temporary))
        self.assertTrue(started_ok)
        self.assertTrue(context_ok)
        self.assertTrue(image_ok)
        self.assertTrue(
            {
                "start_session",
                "record_observation",
                "parse_text",
                "record_parsed_text",
                "configure_game_layout",
                "configure_game_actions",
                "configure_game_timing",
                "perform_game_action",
                "capture_screen",
                "get_codex_context",
                "get_compaction_status",
                "get_compaction_request",
                "save_compaction",
                "dismiss_choice",
            }
            <= names
        )


if __name__ == "__main__":
    unittest.main()
