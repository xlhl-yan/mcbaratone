from __future__ import annotations

import json
import time
import pytest
from contextlib import asynccontextmanager
from unittest.mock import Mock, MagicMock, patch

from baritone_client import TransportError, CommandResult, CacheStats
from baritone_client.mcp.mcp_server import BridgeConfig, BridgeSession, BridgeLifespanContext, create_mcp_server, _call_bridge, _format_json, _session_from_context
from baritone_client.utils.upload_manager import UploadProgress
from baritone_client.mcp.guidance import BUILD_PLAN_RESOURCE_URI, BUILDSITE_RESOURCE_URI, WORKFLOW_RESOURCE_URI, BUILD_PLAN_GUIDE_ALIAS, WORKFLOW_GUIDE_ALIAS
from typing import List
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def mock_client():
    client = Mock()
    # Set up mock facades
    client.command_dispatcher = Mock()
    client.mission = Mock()
    client.process = Mock()
    client.goals = Mock()
    client.cache = Mock()
    client.upload = Mock()
    client.poll_events = Mock(return_value=[])
    return client


@pytest.fixture
def bridge_session(mock_client):
    session = BridgeSession(BridgeConfig(), client_factory=lambda: mock_client)
    yield session
    session.close()


@pytest.fixture
def mock_request_context(bridge_session):
    ctx = Mock()
    ctx.request_context.lifespan_context = BridgeLifespanContext(session=bridge_session)
    return ctx


class DummyClient:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def test_bridge_session_reuses_single_client_instance() -> None:
    created: list[DummyClient] = []

    def factory() -> DummyClient:
        client = DummyClient()
        created.append(client)
        return client

    session = BridgeSession(BridgeConfig(), client_factory=factory)
    seen = []

    def record(client: DummyClient) -> str:
        seen.append(client)
        return "ok"

    assert session.run(record) == "ok"
    assert session.run(record) == "ok"
    assert len(created) == 1
    assert seen[0] is seen[1]

    session.close()
    assert created[0].shutdown_calls == 1


def test_bridge_session_reconnects_after_transport_error() -> None:
    created: list[DummyClient] = []

    def factory() -> DummyClient:
        client = DummyClient()
        created.append(client)
        return client

    session = BridgeSession(BridgeConfig(), client_factory=factory)

    def fail_once(client: DummyClient) -> None:
        raise TransportError("boom")

    with pytest.raises(TransportError):
        session.run(fail_once)

    assert len(created) == 1
    assert created[0].shutdown_calls == 1

    def succeed(client: DummyClient) -> str:
        return "ok"

    assert session.run(succeed) == "ok"
    assert len(created) == 2
    assert created[1].shutdown_calls == 0

    session.close()
    assert created[1].shutdown_calls == 1


def test_bridge_session_close_is_idempotent() -> None:
    session = BridgeSession(BridgeConfig(), client_factory=DummyClient)
    # No client created yet; close should be a no-op
    session.close()

    def run_once(client: DummyClient) -> str:
        return "done"

    assert session.run(run_once) == "done"
    session.close()
    # Second close should not raise or double shutdown
    session.close()


def test_create_mcp_server_initialization():
    config = BridgeConfig(host="test", port=1234, timeout=10.0)
    server = create_mcp_server(config)
    assert server.name == "mcbaratone-bridge"
    assert "Baritone bridge controls" in server.instructions


def test_mcp_server_registers_guidance_resources() -> None:
    server = create_mcp_server(BridgeConfig())
    resources = {str(resource.uri) for resource in server._resource_manager._resources.values()}
    assert WORKFLOW_RESOURCE_URI in resources
    assert BUILD_PLAN_RESOURCE_URI in resources
    assert BUILDSITE_RESOURCE_URI in resources


def test_mcp_server_registers_guidance_prompts() -> None:
    if not hasattr(FastMCP, "prompt"):
        pytest.skip("FastMCP prompt decorator is not available")

    server = create_mcp_server(BridgeConfig())
    prompt_names = {prompt.name for prompt in server._prompt_manager.list_prompts()}
    assert WORKFLOW_GUIDE_ALIAS in prompt_names
    assert BUILD_PLAN_GUIDE_ALIAS in prompt_names


def test_mcp_server_registers_read_only_tools() -> None:
    server = create_mcp_server(BridgeConfig())
    tool_names = set(server._tool_manager._tools.keys())
    assert "minecraft_help" in tool_names
    assert "minecraft_describe_tool" in tool_names
    assert "inspect_build_site" in tool_names
    assert "preview_build_plan" in tool_names


def test_get_events_normalizes_event_type_to_lowercase(mock_request_context, mock_client):
    """回归：bridge 的 EventManager 用大小写敏感的 equals 过滤，缓冲里的 type 一律小写。

    早先这里误用了 upper()，导致**所有带 event_type 的查询静默返回 0 条** ——
    调用方会以为"没有死亡事件"，实际是被过滤光了。
    """
    server = create_mcp_server(BridgeConfig())
    tool = server._tool_manager._tools["get_events"].fn

    calls = []

    def fake_dispatch(name, params=None):
        calls.append((name, dict(params or {})))
        return {"events": [], "count": 0, "latest_seq": 100}

    mock_client.transport = Mock()
    mock_client.transport.dispatch.side_effect = fake_dispatch

    tool(mock_request_context, event_type="TICK_UPDATE", after_seq=10, limit=5)

    name, params = calls[-1]
    assert name == "get_events"
    assert params["type"] == "tick_update"


def test_get_screen_hides_empty_slots_by_default(mock_request_context, mock_client):
    server = create_mcp_server(BridgeConfig())
    tool = server._tool_manager._tools["get_screen"].fn

    slots = [
        {"slot": 0, "id": "minecraft:air", "count": 0},
        {"slot": 1, "id": "minecraft:stone", "count": 3},
    ]

    def fake_dispatch(name, params=None):
        if name == "get_state":
            return {"screen": "ChestScreen"}
        if name == "get_screen":
            return {"type": "ChestMenu", "sync_id": 1, "slots": [dict(s) for s in slots], "total_slots": 2}
        return {}

    mock_client.transport = Mock()
    mock_client.transport.dispatch.side_effect = fake_dispatch

    result = tool(mock_request_context)
    assert result["container"] is True
    assert [s["slot"] for s in result["slots"]] == [1]
    assert result["empty_slots_hidden"] == 1

    full = tool(mock_request_context, include_empty=True)
    assert [s["slot"] for s in full["slots"]] == [0, 1]
    assert "empty_slots_hidden" not in full


def test_screenshot_sends_filename_only_when_given_and_skips_metadata(
    mock_request_context, mock_client
):
    """screenshot 只转发两个参数：非空的 filename + include_metadata（默认 False）。

    metadata 是玩家/世界快照，看图时是噪音且 get_state 里已有，所以默认不请求；
    filename 留空时不能往 payload 里塞空串 —— bridge 侧会当自定义名用。
    """
    server = create_mcp_server(BridgeConfig())
    tool = server._tool_manager._tools["screenshot"].fn

    calls = []

    def fake_dispatch(name, params=None):
        calls.append((name, dict(params or {})))
        return {"saved": True, "path": "/run/screenshots/x.png"}

    mock_client.transport = Mock()
    mock_client.transport.dispatch.side_effect = fake_dispatch

    tool(mock_request_context)
    assert calls[-1] == ("screenshot", {"include_metadata": False})

    tool(mock_request_context, filename="  base  ")
    assert calls[-1] == ("screenshot", {"include_metadata": False, "filename": "base"})

    tool(mock_request_context, filename="base", include_metadata=True)
    assert calls[-1] == ("screenshot", {"include_metadata": True, "filename": "base"})


def test_entity_interact_builds_structured_params(mock_request_context, mock_client):
    """entity_interact 只转发 bridge 真认的参数，并在客户端拦下必然失败的组合。

    bridge 侧 interact/feed 走 handleDirectEntityInteraction（硬性要求 entity_id），
    其余动作走 handleEntityInteraction（id 或 type 都行）。提前拦下来比让 bridge 回
    一句 "Missing required parameter: entity_id" 好读；action 大小写也不该影响结果。
    """
    server = create_mcp_server(BridgeConfig())
    tool = server._tool_manager._tools["entity_interact"].fn

    calls = []

    def fake_dispatch(name, params=None):
        calls.append((name, dict(params or {})))
        return {"success": True}

    mock_client.transport = Mock()
    mock_client.transport.dispatch.side_effect = fake_dispatch

    tool(mock_request_context, action="detect")
    assert calls[-1] == ("entity_interact", {"action": "detect"})

    tool(mock_request_context, action="ATTACK", entity_id=2749, max_distance=10)
    assert calls[-1] == (
        "entity_interact",
        {"action": "attack", "entity_id": 2749, "max_distance": 10.0},
    )

    tool(mock_request_context, action="look_at", entity_type=" minecraft:cow ")
    assert calls[-1] == (
        "entity_interact",
        {"action": "look_at", "entity_type": "minecraft:cow"},
    )

    with pytest.raises(ValueError):
        tool(mock_request_context, action="feed", entity_type="cow")
    with pytest.raises(ValueError):
        tool(mock_request_context, action="look_at")
    with pytest.raises(ValueError):
        tool(mock_request_context, action="teleport", entity_id=1)


def test_preview_build_plan_tool_is_pure_and_serializable() -> None:
    server = create_mcp_server(BridgeConfig())
    preview_tool = server._tool_manager._tools["preview_build_plan"]
    build_plan = {
        "version": 2,
            "cuboids": [
                {
                    "name": "platform",
                    "from": {"x": 0, "y": 0, "z": 0},
                    "to": {"x": 0, "y": 0, "z": 0},
                    "block": "minecraft:stone",
                }
            ],
        }
    preview = preview_tool.fn(build_plan)
    assert isinstance(preview, dict)
    assert preview["valid"] is True
    assert isinstance(preview["commands"], list)



def test_server_lifespan_context_creation():
    # Test that the lifespan context manager creates proper context
    config = BridgeConfig()
    session = BridgeSession(config)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield BridgeLifespanContext(session=session)
        finally:
            session.close()

    # Test the lifespan function creates correct context
    async def test_lifespan():
        async with lifespan(None) as ctx:
            assert isinstance(ctx, BridgeLifespanContext)
            assert ctx.session == session

    import asyncio
    asyncio.run(test_lifespan())





# Error handling and rate limiting tests
def test_call_bridge_success(mock_request_context, mock_client):
    mock_client.some_method.return_value = "success"
    session = mock_request_context.request_context.lifespan_context.session

    def action(client):
        return client.some_method()

    result = _call_bridge(mock_request_context, "test", action)
    assert result == "success"


def test_call_bridge_transport_error_reconnects(mock_request_context, mock_client):
    # First call raises TransportError, should reconnect and succeed on second
    mock_client.some_method.side_effect = [TransportError("connection lost"), "success"]
    session = mock_request_context.request_context.lifespan_context.session

    def action(client):
        return client.some_method()

    result = _call_bridge(mock_request_context, "test", action)
    assert result == "success"
    assert mock_client.some_method.call_count == 2


@patch('time.sleep')
def test_call_bridge_rate_limit_retry(mock_sleep, mock_request_context, mock_client):
    # Rate limit error should retry with backoff
    mock_client.some_method.side_effect = [Exception("rate limit exceeded"), "success"]

    def action(client):
        return client.some_method()

    result = _call_bridge(mock_request_context, "test", action)
    assert result == "success"
    mock_sleep.assert_called_with(2)  # First retry wait time


def test_call_bridge_max_retries_exceeded(mock_request_context, mock_client):
    mock_client.some_method.side_effect = TransportError("persistent error")
    session = mock_request_context.request_context.lifespan_context.session

    def action(client):
        return client.some_method()

    with pytest.raises(RuntimeError, match="persistent error"):
        _call_bridge(mock_request_context, "test", action, max_retries=2)

    assert mock_client.some_method.call_count == 2





def test_format_json_utility():
    # Test the JSON formatting utility
    data = {"test": "value", "number": 42}
    result = _format_json(data)
    assert result == '{\n  "number": 42,\n  "test": "value"\n}'


def test_bridge_config_defaults():
    config = BridgeConfig()
    assert config.host == "localhost"
    assert config.port == 5555
    assert config.timeout == 15.0


def test_bridge_session_config_passthrough():
    config = BridgeConfig(host="custom", port=9999, timeout=30.0)
    session = BridgeSession(config)
    assert session.config == config


# Performance and edge cases
def test_concurrent_access_thread_safety(bridge_session):
    import threading
    results = []
    errors = []

    def worker():
        try:
            result = bridge_session.run(lambda client: "ok")
            results.append(result)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 10
    assert len(errors) == 0
    assert all(r == "ok" for r in results)



