from archolith_proxy.curator.result import CuratorToolCall


def test_curator_tool_call_to_dict_includes_proxy_note() -> None:
    tool_call = CuratorToolCall(
        tool="get_file",
        args={"path": "foo.py"},
        status="ok",
        result_preview="def example(): pass",
        raw_result="def example(): pass",
        proxy_note="Reuse the first result instead of fetching it again.",
    )

    result = tool_call.to_dict()

    assert result["tool"] == "get_file"
    assert result["proxy_note"] == "Reuse the first result instead of fetching it again."
    assert "raw_result" not in result
