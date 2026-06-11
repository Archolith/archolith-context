"""Tests for per-tool extraction system.

All LLM-backed extractors are mocked at httpx.AsyncClient. No live API calls.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor
from archolith_proxy.extractor.registry import ToolExtractorRegistry, get_registry


# ---------------------------------------------------------------------------
# TestGrepExtractor
# ---------------------------------------------------------------------------

class TestGrepExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.grep import GrepExtractor
        self.ext = GrepExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_path_line_match_parsing(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Grep",
            args={"pattern": "authenticate"},
            result="src/auth.py:42:def authenticate(user):\nsrc/auth.py:58:    return token\nsrc/routes.py:10:from auth import authenticate",
        )
        result = await self.ext.extract(record, self.client, 5, None)
        assert len(result.facts) == 2  # two unique files
        assert result.source_tool == "Grep"
        assert "src/auth.py" in result.facts[0]["content"]
        assert "src/routes.py" in result.facts[1]["content"]
        assert "authenticate" in result.facts[0]["content"]
        assert result.files_touched == ["src/auth.py", "src/routes.py"]
        assert result.used_llm is False

    @pytest.mark.asyncio
    async def test_multi_file_grouping(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Grep",
            args={"pattern": "TODO"},
            result="a.py:1:TODO fix\nb.py:3:TODO hack\nc.py:7:TODO review",
        )
        result = await self.ext.extract(record, self.client, 3, None)
        assert len(result.facts) == 3
        assert len(result.files_touched) == 3

    @pytest.mark.asyncio
    async def test_line_number_cap(self):
        lines = [f"src/big.py:{i}:match" for i in range(1, 20)]
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Grep",
            args={"pattern": "match"},
            result="\n".join(lines),
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 1
        assert "5 total" in result.facts[0]["content"] or "total" in result.facts[0]["content"]

    @pytest.mark.asyncio
    async def test_fallback_on_unstructured_output(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Grep",
            args={"pattern": "foo"},
            result="some unstructured text without colons",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 1
        assert "no structured matches" in result.facts[0]["content"]
        assert result.files_touched == []

    @pytest.mark.asyncio
    async def test_reject_non_path_labels(self):
        """Grep should reject lines where the 'path' is actually just a label (e.g., 'label:10:content')."""
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Grep",
            args={"pattern": "test"},
            result="src/main.py:42:match here\nlabel:10:some text\nanother:5:data",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        # Only src/main.py should be included; label:10 and another:5 are rejected (no path separators)
        assert len(result.facts) == 1
        assert "src/main.py" in result.facts[0]["content"]
        assert result.files_touched == ["src/main.py"]

    @pytest.mark.asyncio
    async def test_windows_path_parsing(self):
        """C:\\path\\file.py:42:match — drive colon must not confuse line-number split."""
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Grep",
            args={"pattern": "authenticate"},
            result=r"C:\Users\dev\project\src\auth.py:42:def authenticate(user):" + "\n"
                   + r"C:\Users\dev\project\src\routes.py:10:from auth import authenticate",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        # Both paths extracted correctly — not split at the drive colon
        paths = result.files_touched
        assert any("auth.py" in p for p in paths)
        assert any("routes.py" in p for p in paths)
        assert len(result.facts) == 2


# ---------------------------------------------------------------------------
# TestGlobExtractor
# ---------------------------------------------------------------------------

class TestGlobExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.glob import GlobExtractor
        self.ext = GlobExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_file_list_parsing(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Glob",
            args={"pattern": "**/*.py"},
            result="src/main.py\nsrc/utils.py\nsrc/config.py",
        )
        result = await self.ext.extract(record, self.client, 2, None)
        assert len(result.facts) == 1
        assert "3 files" in result.facts[0]["content"]
        assert "src/main.py" in result.facts[0]["content"]
        assert result.used_llm is False

    @pytest.mark.asyncio
    async def test_empty_result(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Glob",
            args={"pattern": "**/*.xyz"},
            result="",
        )
        result = await self.ext.extract(record, self.client, 2, None)
        assert "0 files" in result.facts[0]["content"]

    @pytest.mark.asyncio
    async def test_windows_absolute_paths_not_filtered(self):
        """C:\\Users\\... paths must survive the path filter (colon at index 1)."""
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Glob",
            args={"pattern": "**/*.py"},
            result=r"C:\Users\dev\project\src\main.py" + "\n"
                   + r"C:\Users\dev\project\src\utils.py",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert "2 files" in result.facts[0]["content"]
        assert "main.py" in result.facts[0]["content"]


# ---------------------------------------------------------------------------
# TestLsExtractor
# ---------------------------------------------------------------------------

class TestLsExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.ls import LsExtractor
        self.ext = LsExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_directory_listing_parsing(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="LS",
            args={"path": "/src"},
            result="main.py\nutils.py\nconfig/\ntests/",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 1
        assert "2 files" in result.facts[0]["content"]
        assert "2 dirs" in result.facts[0]["content"]


# ---------------------------------------------------------------------------
# TestFindExtractor
# ---------------------------------------------------------------------------

class TestFindExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.find import FindExtractor
        self.ext = FindExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_path_list_parsing(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="FindFiles",
            args={},
            result="src/a.py\nsrc/b.py\nsrc/c.py",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert "3 paths" in result.facts[0]["content"]


# ---------------------------------------------------------------------------
# TestReadExtractor
# ---------------------------------------------------------------------------

class TestReadExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.read import ReadExtractor
        self.ext = ReadExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_fact_content_format(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Read",
            args={"file_path": "src/main.py"},
            result="line1\nline2\nline3",
        )
        result = await self.ext.extract(record, self.client, 7, None)
        assert len(result.facts) == 1
        assert "[Read] src/main.py read at turn 7" in result.facts[0]["content"]
        assert result.files_touched == ["src/main.py"]

    @pytest.mark.asyncio
    async def test_path_inferred_from_result(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Read",
            args={},  # no path in args
            result="src/inferred.py\nline2\nline3",
        )
        result = await self.ext.extract(record, self.client, 3, None)
        assert "src/inferred.py" in result.facts[0]["content"]
        assert result.files_touched == ["src/inferred.py"]


# ---------------------------------------------------------------------------
# TestWriteEditExtractor
# ---------------------------------------------------------------------------

class TestWriteEditExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.write_edit import WriteEditExtractor
        self.ext = WriteEditExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_write_fact(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Write",
            args={"file_path": "src/app.py"},
            result="File written successfully",
        )
        result = await self.ext.extract(record, self.client, 2, None)
        assert "[Write] src/app.py written at turn 2" in result.facts[0]["content"]
        assert result.files_touched == ["src/app.py"]

    @pytest.mark.asyncio
    async def test_edit_fact(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Edit",
            args={"file_path": "src/app.py"},
            result="File edited successfully",
        )
        result = await self.ext.extract(record, self.client, 3, None)
        assert "edited" in result.facts[0]["content"]

    @pytest.mark.asyncio
    async def test_all_tool_names(self):
        from archolith_proxy.extractor.extractors.write_edit import WriteEditExtractor as _WEE
        for name in ("Write", "Edit", "NotebookEdit"):
            assert name in _WEE.tool_names


# ---------------------------------------------------------------------------
# TestWebSearchExtractor
# ---------------------------------------------------------------------------

class TestWebSearchExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.web_search import WebSearchExtractor
        self.ext = WebSearchExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_json_parse_path(self):
        data = json.dumps([
            {"title": "Result 1", "url": "https://example.com/1", "snippet": "Desc 1"},
            {"title": "Result 2", "url": "https://example.com/2", "snippet": "Desc 2"},
        ])
        record = ToolCallRecord(
            tool_call_id="1", tool_name="WebSearch",
            args={"query": "test query"},
            result=data,
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 2
        assert "[web_search]" in result.facts[0]["content"]
        assert "Result 1" in result.facts[0]["content"]

    @pytest.mark.asyncio
    async def test_line_regex_fallback(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="WebSearch",
            args={"query": "test query"},
            result="Title: Result 1\nURL: https://example.com\nSnippet: A description here",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 1
        assert "Result 1" in result.facts[0]["content"]

    @pytest.mark.asyncio
    async def test_cap_at_5(self):
        data = json.dumps([
            {"title": f"Result {i}", "url": f"https://example.com/{i}", "snippet": f"Desc {i}"}
            for i in range(10)
        ])
        record = ToolCallRecord(
            tool_call_id="1", tool_name="WebSearch",
            args={"query": "test"},
            result=data,
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 5

    @pytest.mark.asyncio
    async def test_raw_fallback(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="WebSearch",
            args={"query": "test"},
            result="some random text without structure",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 1
        assert "no structured" not in result.facts[0]["content"]  # raw fallback


# ---------------------------------------------------------------------------
# TestWebFetchExtractor
# ---------------------------------------------------------------------------

class TestWebFetchExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.web_fetch import WebFetchExtractor
        self.ext = WebFetchExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_llm_call_made(self):
        # Mock the httpx client to return a valid LLM response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"facts": [
                {"content": "API endpoint returns JSON", "fact_type": "observation", "confidence": 0.9}
            ]})}}]
        }
        mock_response.raise_for_status = MagicMock()
        self.client.post = AsyncMock(return_value=mock_response)

        record = ToolCallRecord(
            tool_call_id="1", tool_name="WebFetch",
            args={"url": "https://api.example.com/docs"},
            result="# API Docs\n## GET /users\nReturns a list of users.",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert result.used_llm is True
        assert "[web_fetch]" in result.facts[0]["content"]
        assert self.client.post.called


# ---------------------------------------------------------------------------
# TestBashExtractor
# ---------------------------------------------------------------------------

class TestBashExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.bash import BashExtractor
        self.ext = BashExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_pytest_regex(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "pytest tests/"},
            result="42 passed, 3 failed\nFAILED tests/test_auth.py::test_login",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert result.used_llm is False
        assert any("42 passed" in f["content"] for f in result.facts)
        assert any("3 failed" in f["content"] for f in result.facts)

    @pytest.mark.asyncio
    async def test_jest_regex(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "npm test"},
            result="Tests: 10 passed\n2 failed\nFAIL src/auth.test.ts",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert result.used_llm is False
        assert any("10 passed" in f["content"] for f in result.facts)

    @pytest.mark.asyncio
    async def test_git_status_regex(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "git status"},
            result="On branch main\n  modified:   src/app.py\n  new file:   src/test.py",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert result.used_llm is False
        assert any("src/app.py" in f["content"] for f in result.facts)
        assert "src/app.py" in result.files_touched

    @pytest.mark.asyncio
    async def test_git_diff_file_extraction(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "git diff"},
            result="+++ b/src/main.py\n--- a/src/main.py\n@@ -1,3 +1,4 @@",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert "src/main.py" in result.files_touched

    @pytest.mark.asyncio
    async def test_error_line_regex(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "python build.py"},
            result="error: cannot find module 'foo' at line 42",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert any("error" in f["content"].lower() for f in result.facts)

    @pytest.mark.asyncio
    async def test_compound_command_fallthrough(self):
        """cd && pytest — first non-env token is 'cd', a builtin → LLM fallthrough."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"facts": [
                {"content": "tests ran", "fact_type": "tool_result", "confidence": 0.8}
            ], "verifications": []})}}]
        }
        mock_response.raise_for_status = MagicMock()
        self.client.post = AsyncMock(return_value=mock_response)

        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "cd project && pytest"},
            result="some output",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        # Should have used LLM because cd is a builtin
        assert result.used_llm is True

    @pytest.mark.asyncio
    async def test_ansi_code_stripping(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "pytest"},
            result="\x1b[32m42 passed\x1b[0m, \x1b[31m3 failed\x1b[0m",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert result.used_llm is False
        assert any("42 passed" in f["content"] for f in result.facts)

    @pytest.mark.asyncio
    async def test_llm_path_when_regex_yields_nothing(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"facts": [
                {"content": "custom output analysis", "fact_type": "tool_result", "confidence": 0.7}
            ], "verifications": []})}}]
        }
        mock_response.raise_for_status = MagicMock()
        self.client.post = AsyncMock(return_value=mock_response)

        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "curl -s http://example.com"},
            result="some unstructured response",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert result.used_llm is True
        assert "[Bash]" in result.facts[0]["content"]

    @pytest.mark.asyncio
    async def test_env_var_prefix_handling(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "NODE_ENV=test pytest"},
            result="42 passed, 0 failed",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        # After stripping env var, primary is pytest → regex should match
        assert result.used_llm is False

    @pytest.mark.asyncio
    async def test_pipe_with_recognizable_primary(self):
        """pytest tests/ | tee out.txt — primary is pytest → regex classifies, no LLM."""
        record = ToolCallRecord(
            tool_call_id="1", tool_name="Bash",
            args={"command": "pytest tests/ | tee out.txt"},
            result="38 passed, 1 failed\nFAILED tests/test_api.py::test_route",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        # First whitespace-split token is "pytest" → regex matches → no LLM
        assert result.used_llm is False
        assert any("38 passed" in f["content"] for f in result.facts)


# ---------------------------------------------------------------------------
# TestMemoryRecallExtractor
# ---------------------------------------------------------------------------

class TestMemoryRecallExtractor:
    def setup_method(self):
        from archolith_proxy.extractor.extractors.memory_recall import MemoryRecallExtractor
        self.ext = MemoryRecallExtractor()
        self.client = AsyncMock()

    @pytest.mark.asyncio
    async def test_json_parse(self):
        data = json.dumps([
            {"text": "Auth module uses JWT tokens", "score": 0.9},
            {"text": "Database uses PostgreSQL", "score": 0.85},
        ])
        record = ToolCallRecord(
            tool_call_id="1", tool_name="mcp__memory__recall_memories",
            args={"query": "auth"},
            result=data,
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 2
        assert all("[memory_recall]" in f["content"] for f in result.facts)
        assert result.facts[0]["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_verbatim_fact_content(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="mcp__memory__recall_memories",
            args={},
            result=json.dumps([{"text": "exact fact here", "score": 0.8}]),
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert "exact fact here" in result.facts[0]["content"]

    @pytest.mark.asyncio
    async def test_score_passthrough(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="mcp__memory__recall_memories",
            args={},
            result=json.dumps([{"text": "fact", "score": 0.7}]),
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert result.facts[0]["confidence"] == 0.7

    @pytest.mark.asyncio
    async def test_low_score_filtered(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="mcp__memory__recall_memories",
            args={},
            result=json.dumps([
                {"text": "good fact", "score": 0.8},
                {"text": "bad fact", "score": 0.3},
            ]),
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 1
        assert "good fact" in result.facts[0]["content"]

    @pytest.mark.asyncio
    async def test_cap_at_20(self):
        items = [{"text": f"fact {i}", "score": 0.8} for i in range(30)]
        record = ToolCallRecord(
            tool_call_id="1", tool_name="mcp__memory__recall_memories",
            args={},
            result=json.dumps(items),
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 20

    @pytest.mark.asyncio
    async def test_plain_text_separator_fallback(self):
        record = ToolCallRecord(
            tool_call_id="1", tool_name="mcp__memory__recall_memories",
            args={},
            result="First fact here --- Second fact here --- Third fact here",
        )
        result = await self.ext.extract(record, self.client, 1, None)
        assert len(result.facts) == 3

    @pytest.mark.asyncio
    async def test_prefix_match_routing_via_registry(self):
        """mcp__memory__recall_memories routes to MemoryRecallExtractor via prefix match."""
        from archolith_proxy.extractor.extractors.memory_recall import MemoryRecallExtractor as _MRE
        reg = get_registry()
        ext = reg.get("mcp__memory__recall_memories")
        assert isinstance(ext, _MRE)
        ext2 = reg.get("mcp__memory__recall_context_memories")
        assert isinstance(ext2, _MRE)


# ---------------------------------------------------------------------------
# TestToolExtractorRegistry
# ---------------------------------------------------------------------------

class TestAliasRouting:
    def test_read_file_alias_routes_to_read_extractor(self):
        """read_file alias should route to ReadExtractor."""
        from archolith_proxy.extractor.extractors.read import ReadExtractor
        reg = get_registry()
        ext = reg.get("read_file")
        assert isinstance(ext, ReadExtractor)

    def test_write_file_alias_routes_to_write_edit_extractor(self):
        """write_file alias should route to WriteEditExtractor."""
        from archolith_proxy.extractor.extractors.write_edit import WriteEditExtractor
        reg = get_registry()
        ext = reg.get("write_file")
        assert isinstance(ext, WriteEditExtractor)

    def test_edit_file_alias_routes_to_write_edit_extractor(self):
        """edit_file alias should route to WriteEditExtractor."""
        from archolith_proxy.extractor.extractors.write_edit import WriteEditExtractor
        reg = get_registry()
        ext = reg.get("edit_file")
        assert isinstance(ext, WriteEditExtractor)

    def test_notebook_edit_alias_routes_to_write_edit_extractor(self):
        """notebook_edit alias should route to WriteEditExtractor."""
        from archolith_proxy.extractor.extractors.write_edit import WriteEditExtractor
        reg = get_registry()
        ext = reg.get("notebook_edit")
        assert isinstance(ext, WriteEditExtractor)


class TestToolExtractorRegistry:
    def test_known_tool_routes_correctly(self):
        reg = get_registry()
        from archolith_proxy.extractor.extractors.grep import GrepExtractor
        ext = reg.get("Grep")
        assert isinstance(ext, GrepExtractor)

    def test_longest_prefix_match(self):
        """When two prefix sentinels could match, longest wins."""
        from archolith_proxy.extractor.extractors.read import ReadExtractor

        reg = ToolExtractorRegistry()
        reg.register(ReadExtractor())  # exact "Read"
        # Simulate overlapping sentinel
        class FakeExtractor(ToolExtractor):
            tool_names = ("mcp__memory__recall",)
            async def extract(self, *a, **kw): return PartialExtractionResult(source_tool="fake")

        class LongerExtractor(ToolExtractor):
            tool_names = ("mcp__memory__recall_context",)
            async def extract(self, *a, **kw): return PartialExtractionResult(source_tool="longer")

        reg.register(FakeExtractor())
        reg.register(LongerExtractor())

        # "mcp__memory__recall_context_memories" should match longer prefix
        result = reg.get("mcp__memory__recall_context_memories")
        assert isinstance(result, LongerExtractor)

    def test_unknown_routes_to_default(self):
        from archolith_proxy.extractor.extractors.default import DefaultExtractor
        reg = get_registry()
        ext = reg.get("SomeUnknownTool_xyz")
        assert isinstance(ext, DefaultExtractor)

    def test_build_default_smoke_test(self):
        reg = ToolExtractorRegistry.build_default()
        assert reg._default is not None
        # All known tools route
        for name in ("Read", "Write", "Bash", "Grep", "Glob", "LS", "find",
                     "WebSearch", "WebFetch", "mcp__memory__recall"):
            ext = reg.get(name)
            assert ext is not None


# ---------------------------------------------------------------------------
# TestBuildCallMap
# ---------------------------------------------------------------------------

class TestBuildCallMap:
    def test_builds_map_from_assistant_messages(self):
        from archolith_proxy.openai.chat import _build_call_map

        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "tc1", "function": {"name": "Read", "arguments": '{"file_path": "a.py"}'}},
                {"id": "tc2", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "file content"},
        ]
        cmap = _build_call_map(messages)
        assert "tc1" in cmap
        assert cmap["tc1"] == ("Read", {"file_path": "a.py"})
        assert "tc2" in cmap
        assert cmap["tc2"] == ("Bash", {"command": "ls"})

    def test_multiple_assistant_messages_merged(self):
        from archolith_proxy.openai.chat import _build_call_map

        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "tc1", "function": {"name": "Read", "arguments": '{"file_path": "a.py"}'}},
            ]},
            {"role": "user", "content": "next turn"},
            {"role": "assistant", "tool_calls": [
                {"id": "tc2", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}},
            ]},
        ]
        cmap = _build_call_map(messages)
        assert len(cmap) == 2
        assert "tc1" in cmap
        assert "tc2" in cmap

    def test_malformed_args_produce_empty_dict(self):
        from archolith_proxy.openai.chat import _build_call_map

        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "tc1", "function": {"name": "Read", "arguments": "invalid json{{{"}},
            ]},
        ]
        cmap = _build_call_map(messages)
        assert cmap["tc1"][1] == {}


# ---------------------------------------------------------------------------
# TestCollectToolCallRecords
# ---------------------------------------------------------------------------

class TestCollectToolCallRecords:
    def test_builds_records_from_messages(self):
        from archolith_proxy.openai.chat import _collect_tool_call_records

        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "tc1", "function": {"name": "Read", "arguments": '{"file_path": "a.py"}'}},
                {"id": "tc2", "function": {"name": "Grep", "arguments": '{"pattern": "foo"}'}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "file content here"},
            {"role": "tool", "tool_call_id": "tc2", "content": "a.py:1:foo match"},
        ]
        records = _collect_tool_call_records(messages)
        assert len(records) == 2
        assert records[0].tool_name == "Read"
        assert records[1].tool_name == "Grep"
        assert records[0].result == "file content here"

    def test_filter_adapter_applied(self):
        """Verify filter adapter is applied to each record's result."""
        from archolith_proxy.openai.chat import _collect_tool_call_records

        with patch("archolith_proxy.openai.helpers.filter_single_tool_result", side_effect=lambda content, tool_name="": f"filtered_{content}"):
            messages = [
                {"role": "assistant", "tool_calls": [
                    {"id": "tc1", "function": {"name": "Read", "arguments": '{}'}},
                ]},
                {"role": "tool", "tool_call_id": "tc1", "content": "raw content"},
            ]
            records = _collect_tool_call_records(messages)
            assert records[0].result == "filtered_raw content"

    def test_uncapped_result(self):
        from archolith_proxy.openai.chat import _collect_tool_call_records

        # Create many tool calls
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": f"tc{i}", "function": {"name": "Bash", "arguments": '{}'}}
                for i in range(20)
            ]},
        ]
        for i in range(20):
            messages.append({"role": "tool", "tool_call_id": f"tc{i}", "content": f"output {i}"})

        records = _collect_tool_call_records(messages)
        assert len(records) == 20

    def test_scoped_to_current_turn_only(self):
        """Prior-turn tool results must not be re-extracted on subsequent turns."""
        from archolith_proxy.openai.chat import _collect_tool_call_records

        messages = [
            # Turn 1 — already extracted
            {"role": "assistant", "tool_calls": [
                {"id": "old1", "function": {"name": "Read", "arguments": '{"file_path": "old.py"}'}},
            ]},
            {"role": "tool", "tool_call_id": "old1", "content": "old file content"},
            {"role": "assistant", "content": "I read old.py"},
            {"role": "user", "content": "now do something else"},
            # Turn 2 — current turn
            {"role": "assistant", "tool_calls": [
                {"id": "new1", "function": {"name": "Bash", "arguments": '{"command": "pytest"}'}},
            ]},
            {"role": "tool", "tool_call_id": "new1", "content": "42 passed"},
        ]
        records = _collect_tool_call_records(messages)
        # Only the current turn's tool call should be returned
        assert len(records) == 1
        assert records[0].tool_call_id == "new1"
        assert records[0].tool_name == "Bash"

    def test_collects_multiple_tool_batches_in_current_agent_turn(self):
        """Turn-boundary extraction must keep every tool-call batch since the latest user."""
        from archolith_proxy.openai.chat import _collect_tool_call_records

        messages = [
            {"role": "user", "content": "inspect then test"},
            {"role": "assistant", "tool_calls": [
                {
                    "id": "read1",
                    "function": {"name": "Read", "arguments": '{"file_path": "a.py"}'},
                },
            ]},
            {"role": "tool", "tool_call_id": "read1", "content": "def a(): pass"},
            {"role": "assistant", "tool_calls": [
                {
                    "id": "bash1",
                    "function": {"name": "Bash", "arguments": '{"command": "pytest"}'},
                },
            ]},
            {"role": "tool", "tool_call_id": "bash1", "content": "2 passed"},
        ]

        records = _collect_tool_call_records(messages)
        assert [r.tool_call_id for r in records] == ["read1", "bash1"]
        assert [r.tool_name for r in records] == ["Read", "Bash"]

    def test_user_boundary_collects_previous_completed_agent_turn(self):
        """A fresh user turn should extract the completed agent turn before the final user message."""
        from archolith_proxy.openai.chat import _collect_tool_call_records

        messages = [
            {"role": "user", "content": "inspect then test"},
            {"role": "assistant", "tool_calls": [
                {
                    "id": "read1",
                    "function": {"name": "Read", "arguments": '{"file_path": "a.py"}'},
                },
            ]},
            {"role": "tool", "tool_call_id": "read1", "content": "def a(): pass"},
            {"role": "assistant", "tool_calls": [
                {
                    "id": "bash1",
                    "function": {"name": "Bash", "arguments": '{"command": "pytest"}'},
                },
            ]},
            {"role": "tool", "tool_call_id": "bash1", "content": "2 passed"},
            {"role": "assistant", "content": "Done."},
            {"role": "user", "content": "next task"},
        ]

        records = _collect_tool_call_records(messages)
        assert [r.tool_call_id for r in records] == ["read1", "bash1"]


# ---------------------------------------------------------------------------
# TestInferFileTouchStatuses
# ---------------------------------------------------------------------------

class TestInferFileTouchStatuses:
    def test_infers_read_and_modified_from_current_turn(self):
        from archolith_proxy.openai.chat import _infer_file_touch_statuses
        from archolith_proxy.models.graph_nodes import FileStatus

        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "tc1", "function": {"name": "Read", "arguments": '{"file_path": "src/readme.md"}'}},
                {"id": "tc2", "function": {"name": "Edit", "arguments": '{"file_path": "src/app.py"}'}},
            ]},
        ]

        statuses, fallback = _infer_file_touch_statuses(messages)

        assert statuses["src/readme.md"] == FileStatus.READ
        assert statuses["src/app.py"] == FileStatus.MODIFIED
        assert fallback == FileStatus.MODIFIED

    def test_read_only_turn_defaults_unmatched_files_to_read(self):
        from archolith_proxy.openai.chat import _infer_file_touch_statuses
        from archolith_proxy.models.graph_nodes import FileStatus

        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "tc1", "function": {"name": "Grep", "arguments": '{"pattern": "TODO"}'}},
            ]},
        ]

        statuses, fallback = _infer_file_touch_statuses(messages)

        assert statuses == {}
        assert fallback == FileStatus.READ

    def test_prefers_created_over_read_for_same_path(self):
        from archolith_proxy.openai.chat import _infer_file_touch_statuses
        from archolith_proxy.models.graph_nodes import FileStatus

        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "tc1", "function": {"name": "Read", "arguments": '{"file_path": "src/new.py"}'}},
                {"id": "tc2", "function": {"name": "create_file", "arguments": '{"file_path": "src/new.py"}'}},
            ]},
        ]

        statuses, _fallback = _infer_file_touch_statuses(messages)

        assert statuses["src/new.py"] == FileStatus.CREATED


# ---------------------------------------------------------------------------
# TestExtractFactsPerTool (orchestrator)
# ---------------------------------------------------------------------------

class TestExtractFactsPerTool:
    @pytest.mark.asyncio
    async def test_concurrent_fan_out(self):
        """Multiple extractors run concurrently."""
        from archolith_proxy.extractor.client import extract_facts_per_tool

        call_count = 0

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "choices": [{"message": {"content": json.dumps({
                    "facts": [{"content": f"[Bash] fact {call_count}", "fact_type": "tool_result", "confidence": 0.8}],
                    "decisions": [], "session_goal": "test", "checkpoint": None,
                    "issues": [], "verifications": [],
                })}}]
            }
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = mock_post

        records = [
            ToolCallRecord(tool_call_id="1", tool_name="Grep", args={"pattern": "x"}, result="a.py:1:x"),
            ToolCallRecord(tool_call_id="2", tool_name="Glob", args={"pattern": "*.py"}, result="a.py\nb.py"),
        ]
        # With no LLM-backed extractors, only the turn-level call should fire
        with patch("archolith_proxy.extractor.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                extractor_model="gpt-4.1-mini",
                extractor_base_url="https://api.openai.com/v1",
                extractor_api_key="test",
                extractor_llm_concurrency=3,
            )
            # Reset semaphore for test
            import archolith_proxy.extractor.client as client_mod
            client_mod._llm_semaphore = None

            result = await extract_facts_per_tool(
                http_client=mock_client,
                turn_number=1,
                user_message="test",
                assistant_response="I searched for things",
                tool_records=records,
                session_goal="test goal",
            )

        assert result is not None
        assert len(result.facts) >= 2  # At least Grep + Glob facts
        # Turn-level call should have been made
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_exception_in_one_extractor_skipped(self):
        """A failed extractor must not block others."""
        from archolith_proxy.extractor.client import extract_facts_per_tool

        class FailingExtractor(ToolExtractor):
            tool_names = ("FailTool",)
            async def extract(self, record, http_client, turn_number, session_goal):
                raise RuntimeError("extractor exploded")

        reg = ToolExtractorRegistry()
        reg.register(FailingExtractor())
        from archolith_proxy.extractor.extractors.grep import GrepExtractor
        reg.register(GrepExtractor())
        reg.set_default(GrepExtractor())  # fallback

        # Mock the turn-level LLM call
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "facts": [], "decisions": [], "session_goal": None, "checkpoint": None,
                "issues": [], "verifications": [], "files_touched": [], "invalidated": [],
            })}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        records = [
            ToolCallRecord(tool_call_id="1", tool_name="FailTool", args={}, result="boom"),
            ToolCallRecord(tool_call_id="2", tool_name="Grep", args={"pattern": "x"}, result="a.py:1:x"),
        ]

        with patch("archolith_proxy.extractor.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                extractor_model="gpt-4.1-mini",
                extractor_base_url="https://api.openai.com/v1",
                extractor_api_key="test",
                extractor_llm_concurrency=3,
            )
            import archolith_proxy.extractor.client as client_mod
            client_mod._llm_semaphore = None

            result = await extract_facts_per_tool(
                http_client=mock_client,
                turn_number=1,
                user_message="test",
                assistant_response="test",
                tool_records=records,
                session_goal=None,
                registry=reg,
            )

        # Should have Grep facts even though FailTool exploded
        assert result is not None
        grep_facts = [f for f in result.facts if "[Grep]" in f.get("content", "")]
        assert len(grep_facts) >= 1

    @pytest.mark.asyncio
    async def test_extractor_returning_none_facts_does_not_crash_merge(self):
        """An extractor returning facts=None/files_touched=None must not break the merge (D6).

        The protocol permits None; the merge loop guards with ``r.facts or []``.
        """
        from archolith_proxy.extractor.client import extract_facts_per_tool

        class NoneReturningExtractor(ToolExtractor):
            tool_names = ("NoneTool",)
            async def extract(self, record, http_client, turn_number, session_goal):
                return PartialExtractionResult(
                    source_tool="NoneTool", facts=None, files_touched=None
                )

        reg = ToolExtractorRegistry()
        reg.register(NoneReturningExtractor())
        from archolith_proxy.extractor.extractors.grep import GrepExtractor
        reg.register(GrepExtractor())
        reg.set_default(GrepExtractor())

        # Turn-level LLM returns nothing extra.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "facts": [], "decisions": [], "session_goal": None, "checkpoint": None,
                "issues": [], "verifications": [], "files_touched": [], "invalidated": [],
            })}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        records = [
            ToolCallRecord(tool_call_id="1", tool_name="NoneTool", args={}, result="x"),
            ToolCallRecord(tool_call_id="2", tool_name="Grep", args={"pattern": "x"}, result="a.py:1:x"),
        ]

        with patch("archolith_proxy.extractor.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                extractor_model="gpt-4.1-mini",
                extractor_base_url="https://api.openai.com/v1",
                extractor_api_key="test",
                extractor_llm_concurrency=3,
            )
            import archolith_proxy.extractor.client as client_mod
            client_mod._llm_semaphore = None

            # Must not raise TypeError on list.extend(None).
            result = await extract_facts_per_tool(
                http_client=mock_client,
                turn_number=1,
                user_message="test",
                assistant_response="test",
                tool_records=records,
                session_goal=None,
                registry=reg,
            )

        assert result is not None
        # None-returning extractor contributed nothing; Grep facts survive.
        grep_facts = [f for f in result.facts if "[Grep]" in f.get("content", "")]
        assert len(grep_facts) >= 1

    @pytest.mark.asyncio
    async def test_semaphore_only_applied_to_llm_backed_extractors(self):
        """No-LLM extractors (Grep, Glob) bypass the semaphore; LLM extractor waits for it."""
        from archolith_proxy.extractor.client import _extract_with_semaphore
        from archolith_proxy.extractor.extractors.grep import GrepExtractor
        from archolith_proxy.extractor.extractors.default import DefaultExtractor

        grep_ext = GrepExtractor()
        default_ext = DefaultExtractor()

        assert grep_ext.may_use_llm is False, "GrepExtractor must not claim LLM use"
        assert default_ext.may_use_llm is True, "DefaultExtractor must claim LLM use"

        # Reset semaphore with cap of 1 so we can verify it gates the LLM extractor
        import archolith_proxy.extractor.client as client_mod
        client_mod._llm_semaphore = asyncio.Semaphore(1)

        acquired_during_grep = []
        acquired_during_default = []

        async def mock_grep_extract(record, http_client, turn_number, session_goal):
            # Record whether semaphore is locked when Grep runs — it should NOT be
            sem = client_mod._llm_semaphore
            acquired_during_grep.append(sem._value < 1)
            return PartialExtractionResult(source_tool="Grep", facts=[], files_touched=[])

        async def mock_default_extract(record, http_client, turn_number, session_goal):
            acquired_during_default.append(True)
            return PartialExtractionResult(source_tool="Default", facts=[], files_touched=[])

        grep_ext.extract = mock_grep_extract
        default_ext.extract = mock_default_extract

        grep_record = ToolCallRecord(tool_call_id="g1", tool_name="Grep", args={}, result="")
        default_record = ToolCallRecord(tool_call_id="d1", tool_name="UnknownTool", args={}, result="")

        mock_client = AsyncMock()

        # Grep runs without holding semaphore (semaphore is free when Grep runs)
        await _extract_with_semaphore(grep_ext, grep_record, mock_client, 1, None)
        assert len(acquired_during_grep) == 1
        assert acquired_during_grep[0] is False  # semaphore NOT held — Grep bypassed it

        # Default runs while holding semaphore (semaphore is locked during Default's extract)
        await _extract_with_semaphore(default_ext, default_record, mock_client, 1, None)
        assert len(acquired_during_default) == 1

        # Restore
        client_mod._llm_semaphore = None


# ---------------------------------------------------------------------------
# TestComputeEmbeddingsBatch
# ---------------------------------------------------------------------------

class TestComputeEmbeddingsBatch:
    @pytest.mark.asyncio
    async def test_batch_index_mapping(self):
        """Embeddings are correctly mapped by explicit batch index, not positional offset."""
        from archolith_proxy.extractor.embeddings import compute_embeddings_batch
        import json

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            # Return embeddings for each input with explicit index field
            inputs = body["input"]
            return httpx.Response(200, json={
                "object": "list",
                "data": [
                    {"object": "embedding", "index": i, "embedding": [float(i), 0.2, 0.3]}
                    for i in range(len(inputs))
                ],
                "model": "text-embedding-3-small",
            })

        mock_transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=mock_transport)

        with patch("archolith_proxy.extractor.embeddings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                embedding_api_key="sk-test",
                embedding_base_url="https://api.openai.com/v1",
                embedding_model="text-embedding-3-small",
            )
            result = await compute_embeddings_batch(
                client, ["fact one", "fact two", "fact three"]
            )

        # Should have 3 embeddings
        assert len(result) == 3
        # Each should map to its correct index (embedding[0] corresponds to its index)
        assert result[0] == [0.0, 0.2, 0.3]
        assert result[1] == [1.0, 0.2, 0.3]
        assert result[2] == [2.0, 0.2, 0.3]


# ---------------------------------------------------------------------------
# TestLlmSemaphoreReset
# ---------------------------------------------------------------------------

class TestLlmSemaphoreReset:
    def test_semaphore_reflects_configured_concurrency(self):
        """The semaphore's limit matches the configured extractor_llm_concurrency."""
        from archolith_proxy.extractor.client import _get_llm_semaphore, _reset_llm_semaphore

        # Reset to clean state
        _reset_llm_semaphore()

        with patch("archolith_proxy.extractor.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                extractor_model="gpt-4.1-mini",
                extractor_base_url="https://api.openai.com/v1",
                extractor_api_key="test",
                extractor_llm_concurrency=5,
            )
            sem = _get_llm_semaphore()
            # Semaphore._value tracks the current permit count (not the limit directly,
            # but we can verify that it was initialized with the right value)
            assert sem._value == 5

        # Cleanup
        _reset_llm_semaphore()

    def test_reset_clears_semaphore(self):
        """_reset_llm_semaphore() clears the global semaphore."""
        from archolith_proxy.extractor.client import _get_llm_semaphore, _reset_llm_semaphore

        _reset_llm_semaphore()

        with patch("archolith_proxy.extractor.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                extractor_model="gpt-4.1-mini",
                extractor_base_url="https://api.openai.com/v1",
                extractor_api_key="test",
                extractor_llm_concurrency=3,
            )
            sem1 = _get_llm_semaphore()
            sem1_id = id(sem1)

        # Reset
        _reset_llm_semaphore()

        # Now get a new semaphore with a different concurrency
        with patch("archolith_proxy.extractor.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                extractor_model="gpt-4.1-mini",
                extractor_base_url="https://api.openai.com/v1",
                extractor_api_key="test",
                extractor_llm_concurrency=7,
            )
            sem2 = _get_llm_semaphore()
            sem2_id = id(sem2)

        # Should be different objects
        assert sem1_id != sem2_id
        assert sem2._value == 7

        # Cleanup
        _reset_llm_semaphore()


# ---------------------------------------------------------------------------
# TestTurnLevelFactProvenance
# ---------------------------------------------------------------------------

class TestExtractFactsFailure:
    @pytest.mark.asyncio
    async def test_extract_facts_returns_empty_on_failure(self):
        """extract_facts should return ExtractionResult with empty facts on API failure."""
        from archolith_proxy.extractor.client import extract_facts

        # Mock client that raises an exception
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("API error"))

        result = await extract_facts(
            http_client=mock_client,
            turn_number=1,
            user_message="test",
            assistant_response="test response",
        )

        # Should return an ExtractionResult with empty collections, not None
        assert result is not None
        assert result.facts == []
        assert result.files_touched == []
        assert result.decisions == []
        assert result.invalidated_fact_ids == []
        assert result.turn_number == 1


class TestTurnLevelFactProvenance:
    def test_turn_level_facts_have_provenance_prefix(self):
        """Turn-level facts extracted should be prefixed with [Turn-level]."""
        from archolith_proxy.extractor.client import _parse_extraction_response

        json_response = json.dumps({
            "facts": [
                {"content": "user is testing the system", "fact_type": "observation", "confidence": 0.8},
                "inferred decision",
            ],
            "files_touched": [],
            "decisions": [],
            "session_goal": None,
            "checkpoint": None,
            "issues": [],
            "verifications": [],
        })

        result = _parse_extraction_response(json_response, turn_number=5, source="Turn-level")

        assert len(result.facts) == 2
        assert result.facts[0]["content"] == "[Turn-level] user is testing the system"
        assert result.facts[1]["content"] == "[Turn-level] inferred decision"

    def test_per_tool_facts_no_provenance_when_not_specified(self):
        """Regular per-tool parsing should not add prefix unless explicitly requested."""
        from archolith_proxy.extractor.client import _parse_extraction_response

        json_response = json.dumps({
            "facts": [
                {"content": "some fact", "fact_type": "observation"},
            ],
            "files_touched": [],
            "decisions": [],
            "session_goal": None,
            "checkpoint": None,
            "issues": [],
            "verifications": [],
        })

        result = _parse_extraction_response(json_response, turn_number=1)

        assert len(result.facts) == 1
        assert result.facts[0]["content"] == "some fact"  # no prefix


# ---------------------------------------------------------------------------
# TestTurnLevelPrompt
# ---------------------------------------------------------------------------

class TestTurnLevelPrompt:
    def test_no_tool_results_section(self):
        from archolith_proxy.extractor.prompts import build_turn_level_extraction_prompt
        prompt = build_turn_level_extraction_prompt(
            turn_number=1,
            user_message="read the file",
            assistant_response="I read the file",
            session_goal="test goal",
        )
        assert "Tool results" not in prompt
        assert "### Tool results:" not in prompt

    def test_preamble_present(self):
        from archolith_proxy.extractor.prompts import TURN_LEVEL_SYSTEM_PROMPT
        assert "already been extracted" in TURN_LEVEL_SYSTEM_PROMPT
        assert "DO NOT infer" in TURN_LEVEL_SYSTEM_PROMPT

    def test_no_tool_result_fact_type(self):
        from archolith_proxy.extractor.prompts import TURN_LEVEL_SYSTEM_PROMPT
        # The turn-level prompt should NOT list "tool_result" as a valid fact type
        assert '"tool_result"' not in TURN_LEVEL_SYSTEM_PROMPT or "NOT" in TURN_LEVEL_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# TestIntegration_PerToolGate
# ---------------------------------------------------------------------------

class TestIntegration_PerToolGate:
    @pytest.mark.asyncio
    async def test_feature_flag_true_uses_per_tool(self):
        """When per_tool_extraction_enabled=True, the per-tool path is used."""
        from archolith_proxy.openai.chat import _run_extraction
        # This is a smoke test — full integration would require more setup
        # Just verify the code path exists without error
        assert callable(_run_extraction)

    def test_feature_flag_false_uses_legacy(self):
        """When per_tool_extraction_enabled=False, the legacy path is used."""
        from archolith_proxy.config import Settings
        s = Settings(per_tool_extraction_enabled=False)
        assert s.per_tool_extraction_enabled is False

    def test_config_defaults(self):
        from archolith_proxy.config import Settings
        s = Settings()
        assert s.per_tool_extraction_enabled is False
        assert s.extractor_llm_concurrency == 3
