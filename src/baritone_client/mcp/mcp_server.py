"""
Expose the Baritone bridge as a Model Context Protocol (MCP) server.

The MCP server lets LLM-centric tools retrieve bridge telemetry (state, inventory,
mission status) and call high-level Baritone actions such as running commands,
setting goals, queueing macros, or starting mining jobs.

Example usage (stdio transport for tools such as Claude Desktop):

    python -m baritone_client.mcp_server --host localhost --port 5555

Example usage (streamable HTTP transport for the MCP Inspector):

    python -m baritone_client.mcp_server --transport streamable-http --http-host 127.0.0.1

The exposed tools and resources are transport-agnostic since the server wraps the
existing :mod:`baritone_client` facade and only adds an MCP-friendly surface.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, TypeVar

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from ..utils.cache_manager import CacheStats
from ..core.client import Client
from ..transport.command_dispatcher import CommandResult
from ..core.exceptions import CommandError, TransportError
from ..core.facades.goals import GoalFactory
from ..transport.transport import TcpTransport, WebSocketTransport
from ..utils.upload_manager import UploadProgress
from ..build_plans import compile_plan_preview
from ..mcp.guidance import (
    BUILD_PLAN_GUIDE_ALIAS,
    BUILD_PLAN_RESOURCE_URI,
    BUILDSITE_RESOURCE_URI,
    WORKFLOW_GUIDE_ALIAS,
    WORKFLOW_RESOURCE_URI,
    build_build_plan_prompt,
    build_buildsite_prompt,
    build_workflow_prompt,
    get_agent_guide,
    get_tool_description,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


def _normalize_guidance_topic(topic: Optional[str]) -> str:
    """Normalize guidance topic values to a simple hyphenated key."""
    if topic is None:
        return WORKFLOW_GUIDE_ALIAS

    normalized = str(topic).strip().lower().replace("_", "-").replace(" ", "-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized.strip("-")


@dataclass
class BridgeConfig:
    """Runtime configuration for the MCP server."""

    host: str = "localhost"
    port: int = 5555
    timeout: float = 15.0
    transport_type: str = "websocket"  # "tcp" or "websocket"


class BridgeSession:
    """
    Lazily connect to the Java bridge and serialize access to the shared client.

    The MCP server can receive concurrent requests when exposed over HTTP. The
    session protects the underlying :class:`Client` with a re-entrant lock and
    recreates it automatically when the socket closes or raises a
    :class:`TransportError`.
    """

    def __init__(
        self,
        config: BridgeConfig,
        client_factory: Optional[Callable[[], Client]] = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or self._default_factory
        self._client: Optional[Client] = None
        self._lock = threading.RLock()

    def _default_factory(self) -> Client:
        if self.config.transport_type == "websocket":
            url = f"ws://{self.config.host}:{self.config.port}"
            logger.info(
                "Connecting to Baritone bridge via WebSocket at %s (timeout %.1fs)",
                url,
                self.config.timeout,
            )
            transport = WebSocketTransport(url=url, timeout=self.config.timeout, enable_event_storage=False)
        else:
            logger.info(
                "Connecting to Baritone bridge via TCP at %s:%s (timeout %.1fs)",
                self.config.host,
                self.config.port,
                self.config.timeout,
            )
            transport = TcpTransport(host=self.config.host, port=self.config.port, timeout=self.config.timeout)
        return Client(transport)

    def _ensure_client(self) -> Client:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _reset_client_locked(self) -> None:
        if self._client is None:
            return
        try:
            self._client.shutdown()
        except Exception:
            logger.exception("Failed to shut down the Baritone client cleanly")
        finally:
            self._client = None

    def run(self, action: Callable[[Client], T]) -> T:
        """Execute an action while holding the session lock."""
        with self._lock:
            client = self._ensure_client()
            try:
                return action(client)
            except (OSError, TransportError):
                # Reset so the next request reconnects automatically.
                self._reset_client_locked()
                raise

    def close(self) -> None:
        """Release resources explicitly (invoked when the MCP server stops)."""
        with self._lock:
            self._reset_client_locked()


@dataclass
class BridgeLifespanContext:
    """Typed context exposed to MCP handlers."""

    session: BridgeSession


RequestContext = Context[ServerSession, BridgeLifespanContext]


def _format_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _session_from_context(ctx: RequestContext) -> BridgeSession:
    lifespan_ctx = ctx.request_context.lifespan_context
    if lifespan_ctx is None:
        raise RuntimeError("Bridge session is not initialized")
    return lifespan_ctx.session


def _call_bridge(ctx: RequestContext, label: str, fn: Callable[[Client], T], max_retries: int = 3) -> T:
    session = _session_from_context(ctx)
    last_exc = None
    for attempt in range(max_retries):
        try:
            return session.run(fn)
        except Exception as exc:
            last_exc = exc
            if "rate limit" in str(exc).lower():
                # Wait before retrying for rate limit
                wait_time = (attempt + 1) * 2  # Exponential backoff
                logger.warning(f"Rate limited on attempt {attempt + 1}, waiting {wait_time}s before retry")
                time.sleep(wait_time)
                continue
            elif attempt < max_retries - 1:
                # Retry for other errors
                logger.warning(f"Bridge operation '{label}' failed on attempt {attempt + 1}, retrying: {exc}")
                time.sleep(0.5)
                continue
            else:
                break
    raise RuntimeError(f"Bridge operation '{label}' failed after {max_retries} attempts: {last_exc}") from last_exc


def create_mcp_server(config: BridgeConfig) -> FastMCP:
    """Create a FastMCP server wired to the configured bridge."""
    session = BridgeSession(config)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[BridgeLifespanContext]:
        try:
            yield BridgeLifespanContext(session=session)
        finally:
            session.close()

    instructions = (
        "Expose key Baritone bridge controls (state, inventory, missions, goals, processes) "
        "as MCP tools and resources. Use these endpoints to orchestrate End-to-End Minecraft runs."
    )
    mcp = FastMCP(
        name="mcbaratone-bridge",
        instructions=instructions,
        lifespan=lifespan,
        json_response=True,
    )

    #
    # Resources
    #

    @mcp.resource("baritone://state")
    def runtime_state(ctx: RequestContext) -> str:
        """Return aggregated Baritone status (position, health, active goals)."""
        def _get_state(client: Client) -> Dict[str, Any]:
            result = client.command_dispatcher.dispatch("get_state", {})
            if result.is_success():
                return result.get_data()
            else:
                raise RuntimeError(f"get_state failed: {result.get_error_message()}")
        data = _call_bridge(ctx, "get_state", _get_state)
        return _format_json(data)

    @mcp.resource("baritone://inventory")
    def inventory(ctx: RequestContext) -> str:
        """Return the latest inventory snapshot."""
        def _get_inventory(client: Client) -> Dict[str, Any]:
            result = client.command_dispatcher.dispatch("get_inventory", {})
            if result.is_success():
                return result.get_data()
            else:
                raise RuntimeError(f"get_inventory failed: {result.get_error_message()}")
        data = _call_bridge(ctx, "get_inventory", _get_inventory)
        return _format_json(data)

    @mcp.resource("baritone://mission/status")
    def mission_status(ctx: RequestContext) -> str:
        """Expose bridge-side mission telemetry and queued macros."""
        data = _call_bridge(ctx, "mission_status", lambda client: client.mission.status())
        return _format_json(data)

    @mcp.resource("baritone://cache/stats")
    def cache_stats(ctx: RequestContext) -> str:
        """Return cache statistics."""
        stats = _call_bridge(ctx, "cache_stats", lambda client: client.cache.get_stats())
        return _format_json(stats.to_dict())

    @mcp.resource("baritone://upload/status")
    def upload_status(ctx: RequestContext) -> str:
        """Return upload manager status."""
        status = _call_bridge(ctx, "upload_status", lambda client: {
            "active_uploads": [p.to_dict() for p in client.upload.get_active_uploads()],
            "statistics": client.upload.get_statistics()
        })
        return _format_json(status)

    @mcp.resource("baritone://analytics/health")
    def system_health(ctx: RequestContext) -> str:
        """Return comprehensive system health metrics."""
        health = _call_bridge(ctx, "system/health", lambda client: client.get_bridge_health())
        return _format_json(health.model_dump() if hasattr(health, 'model_dump') else health.__dict__)

    @mcp.resource("baritone://analytics/performance")
    def performance_metrics(ctx: RequestContext) -> str:
        """Return performance metrics and system throughput."""
        metrics = _call_bridge(ctx, "system/metrics", lambda client: client.get_bridge_metrics())
        return _format_json(metrics)

    @mcp.resource("baritone://analytics/report")
    def analytics_report(ctx: RequestContext) -> str:
        """Return comprehensive analytics report."""
        report = _call_bridge(ctx, "analytics/report", lambda client: client.get_performance_report())
        return _format_json(report.model_dump() if hasattr(report, 'model_dump') else report.__dict__)

    @mcp.resource(WORKFLOW_RESOURCE_URI)
    def workflow_guidance_resource() -> str:
        """Return survival-first guidance for agent workflow."""
        return _format_json({
            "topic": "workflow",
            "guidance": get_agent_guide("workflow"),
        })

    @mcp.resource(BUILD_PLAN_RESOURCE_URI)
    def build_plan_guidance_resource() -> str:
        """Return survival-first guidance for build plan execution."""
        return _format_json({
            "topic": "build-plan",
            "guidance": get_agent_guide("build-plan"),
        })

    @mcp.resource(BUILDSITE_RESOURCE_URI)
    def buildsite_guidance_resource() -> str:
        """Return survival-first guidance for build site inspections."""
        return _format_json({
            "topic": "buildsite",
            "guidance": get_agent_guide("buildsite"),
        })

    if hasattr(FastMCP, "prompt"):

        @mcp.prompt(name=WORKFLOW_GUIDE_ALIAS)
        def workflow_guidance_prompt(task: Optional[str] = None) -> str:
            """Return workflow prompt for agent guidance."""
            return build_workflow_prompt(task)

        @mcp.prompt(name=BUILD_PLAN_GUIDE_ALIAS)
        def build_plan_guidance_prompt(task: Optional[str] = None) -> str:
            """Return prompt for build-plan guidance."""
            return build_build_plan_prompt(task)

    #
    # Tools
    #

    @mcp.tool()
    def run_command(command: str, ctx: RequestContext) -> Dict[str, Any]:
        """Execute a raw Baritone/mission command string."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run(command))

    @mcp.tool()
    def say(message: str, ctx: RequestContext) -> Dict[str, Any]:
        """Say something in Minecraft chat as the bot.

        Goes straight to the bridge's `chat` handler WITHOUT a "#" prefix, so it
        is spoken as a normal player message. Do not use run_command for this:
        that path treats the text as a command (or splices it into `sequence`),
        never as conversation.
        """
        return _call_bridge(
            ctx, "command/say", lambda client: client.transport.dispatch("chat", {"message": message})
        )

    @mcp.tool()
    def eat(
        ctx: RequestContext,
        food: str = "",
        hold_ms: int = 1800,
        timeout_s: float = 8.0,
    ) -> Dict[str, Any]:
        """Eat food to restore hunger (and let natural regeneration heal you).

        Picks edible food from the inventory (or the one named in `food`, e.g.
        "cooked_beef"), moves it into the selected hotbar slot if needed, then
        holds right-click until the item is actually consumed.

        Use this when health is dropping or hunger is low; do NOT use
        run_command("use_item") — that only fires a single right-click and food
        needs to be held down, and it can't find food for you.
        """
        name = (food or "").strip().lower()
        if name and not name.startswith("minecraft:"):
            name = "minecraft:" + name

        def _food_score(item_id: str) -> int:
            """Better food first (rough order; enough to pick sensibly)."""
            short = item_id.replace("minecraft:", "")
            table = {
                "golden_apple": 100,
                "enchanted_golden_apple": 110,
                "cooked_beef": 90,
                "cooked_porkchop": 90,
                "cooked_mutton": 88,
                "cooked_chicken": 86,
                "cooked_salmon": 86,
                "cooked_cod": 84,
                "cooked_rabbit": 84,
                "bread": 80,
                "baked_potato": 78,
                "apple": 70,
                "carrot": 68,
                "golden_carrot": 95,
                "melon_slice": 60,
                "sweet_berries": 58,
                "glow_berries": 58,
                "dried_kelp": 55,
                "beetroot": 50,
                "potato": 40,
                "beef": 35,
                "porkchop": 35,
                "chicken": 33,
                "mutton": 34,
                "salmon": 32,
                "cod": 30,
                "rotten_flesh": 5,
            }
            return table.get(short, 0)

        def _run(client):
            inventory = client.transport.dispatch("get_inventory", {})
            entries = (inventory or {}).get("inventory") or []
            candidates = []
            for entry in entries:
                item_id = str(entry.get("id") or "")
                count = int(entry.get("count") or 0)
                if count <= 0:
                    continue
                if name:
                    if item_id != name:
                        continue
                elif _food_score(item_id) <= 0:
                    continue
                candidates.append(
                    {
                        "slot": int(entry.get("slot", -1)),
                        "id": item_id,
                        "count": count,
                        "score": _food_score(item_id),
                    }
                )
            if not candidates:
                raise CommandError(
                    f"no edible food in inventory (looked for {name or 'any known food'})"
                )

            # 快捷栏 0-8 可以直接手持；在主背包就得先换过来
            hotbar = [c for c in candidates if 0 <= c["slot"] <= 8]
            chosen = max(hotbar or candidates, key=lambda c: c["score"])
            if chosen["slot"] > 8:
                client.transport.dispatch(
                    "inventory_click",
                    {"slot": chosen["slot"], "button": 0, "type": "SWAP"},
                )
                time.sleep(0.35)
                moved = client.transport.dispatch("get_inventory", {})
                for entry in (moved or {}).get("inventory") or []:
                    if entry.get("id") == chosen["id"] and 0 <= int(entry.get("slot", -1)) <= 8:
                        chosen = {**chosen, "slot": int(entry["slot"])}
                        break
                else:
                    raise CommandError("failed to move food into the hotbar")

            client.transport.dispatch("select_slot", {"slot": chosen["slot"]})
            time.sleep(0.15)
            client.transport.dispatch(
                "use_item", {"hand": "MAIN_HAND", "duration_ms": max(200, int(hold_ms))}
            )

            # 等它真吃完：库存里那份数量应该减少
            deadline = time.time() + max(1.0, float(timeout_s))
            before = chosen["count"]
            while time.time() < deadline:
                time.sleep(0.35)
                after = client.transport.dispatch("get_inventory", {})
                remaining = 0
                for entry in (after or {}).get("inventory") or []:
                    if entry.get("id") == chosen["id"]:
                        remaining += int(entry.get("count") or 0)
                if remaining < before:
                    state = client.transport.dispatch("get_state", {}) or {}
                    return {
                        "ate": chosen["id"].replace("minecraft:", ""),
                        "consumed": before - remaining,
                        "remaining": remaining,
                        "health": state.get("health"),
                        "food_level": state.get("food_level"),
                    }
            # 创造模式吃东西**不消耗物品**，所以"数量减少"这条验证永远不成立。
            # 那种情况下不再报错，如实说明即可（2026-10-13 在创造调试世界里踩到）。
            state = client.transport.dispatch("get_state", {}) or {}
            mode = str(state.get("game_mode") or "")
            if mode == "creative":
                return {
                    "ate": chosen["id"].replace("minecraft:", ""),
                    "consumed": 0,
                    "remaining": before,
                    "note": "creative mode: eating does not consume the item, so this is unverifiable — treated as done",
                    "health": state.get("health"),
                    "food_level": state.get("food_level"),
                }
            raise CommandError(
                f"held {chosen['id']} but nothing was consumed "
                f"(is the player actually able to eat? full hunger?)"
            )

        return _call_bridge(ctx, "command/eat", _run)

    @mcp.tool()
    def look_around(
        ctx: RequestContext,
        yaw: float = 0.0,
        pitch: float = 0.0,
    ) -> Dict[str, Any]:
        """Turn the bot's head (absolute angles in degrees)."""
        return _call_bridge(
            ctx,
            "command/look",
            lambda client: client.transport.dispatch("look", {"yaw": yaw, "pitch": pitch}),
        )

    @mcp.tool()
    def get_inventory(ctx: RequestContext) -> Dict[str, Any]:
        """List what the bot is carrying (slot / item / count), plus selected slot."""
        return _call_bridge(
            ctx, "command/get_inventory", lambda client: client.transport.dispatch("get_inventory", {})
        )

    @mcp.tool()
    def select_slot(slot: int, ctx: RequestContext) -> Dict[str, Any]:
        """Hold a hotbar slot (0-8). Slots 9+ are the main inventory and cannot be held."""
        if slot < 0 or slot > 8:
            raise ValueError("slot must be 0-8 (hotbar); use throw_item/equip to move other items")
        return _call_bridge(
            ctx,
            "command/select_slot",
            lambda client: client.transport.dispatch("select_slot", {"slot": int(slot)}),
        )

    @mcp.tool()
    def throw_item(
        ctx: RequestContext,
        slot: Optional[int] = None,
        all: bool = False,
    ) -> Dict[str, Any]:
        """Drop an item on the ground (give something to the user this way).

        `slot` selects a hotbar slot first; omit it to drop whatever is held.
        `all=True` drops the whole stack instead of one item.
        """
        def _run(client):
            if slot is not None:
                if slot < 0 or slot > 8:
                    raise CommandError("slot must be 0-8 (hotbar) — move it there first")
                client.transport.dispatch("select_slot", {"slot": int(slot)})
                time.sleep(0.15)
            return client.transport.dispatch("throw_item", {"all": bool(all)})

        return _call_bridge(ctx, "command/throw_item", _run)

    @mcp.tool()
    def attack_nearest(
        ctx: RequestContext,
        max_distance: float = 4.0,
        swings: int = 3,
        min_cooldown: float = 0.9,
        hostile_only: bool = True,
        entity_type: str = "",
    ) -> Dict[str, Any]:
        """Swing at the nearest mob.

        The bridge's attack_entity only performs ONE swing and refuses while the
        attack cooldown isn't ready, so this loops `swings` times with a pause
        between hits. Give `entity_type` (e.g. "zombie") to pick a specific kind,
        or leave it empty to hit whatever hostile mob is closest.
        """
        hostiles = {
            "zombie", "husk", "drowned", "skeleton", "stray", "spider", "cave_spider",
            "creeper", "enderman", "witch", "slime", "magma_cube", "silverfish",
            "blaze", "ghast", "piglin", "piglin_brute", "hoglin", "zoglin",
            "pillager", "vindicator", "evoker", "ravager", "phantom", "guardian",
            "elder_guardian", "shulker", "wither_skeleton", "warden", "bogged",
        }

        def _run(client):
            wanted = (entity_type or "").strip().lower().replace("minecraft:", "")
            hits = 0
            seen = ""
            for _ in range(max(1, int(swings))):
                found = client.transport.dispatch(
                    "get_entities", {"radius": max(6, int(max_distance) + 4)}
                )
                candidates = []
                for entry in (found or {}).get("entities") or []:
                    kind = str(entry.get("type") or "").replace("minecraft:", "")
                    if "player" in kind:
                        continue
                    if not entry.get("is_living"):
                        continue
                    if wanted:
                        if kind != wanted:
                            continue
                    elif hostile_only and kind not in hostiles:
                        continue
                    distance = float(entry.get("distance") or 9999)
                    if distance > float(max_distance):
                        continue
                    candidates.append({"id": entry.get("id"), "type": kind, "distance": distance})
                if not candidates:
                    break
                target = min(candidates, key=lambda c: c["distance"])
                seen = target["type"]
                result = client.transport.dispatch(
                    "attack_entity",
                    {"entity_id": target["id"], "min_cooldown": float(min_cooldown)},
                )
                if isinstance(result, dict) and result.get("attacked"):
                    hits += 1
                time.sleep(0.65)
            return {
                "swings": hits,
                "target": seen or None,
                "note": "0 swings usually means: nothing in range, or attack cooldown not ready",
            }

        return _call_bridge(ctx, "command/attack_nearest", _run)

    @mcp.tool()
    def equip(
        ctx: RequestContext,
        item: str = "",
        slot: str = "",
    ) -> Dict[str, Any]:
        """Equip armour or move an item to the off hand (by item name or slot).

        bridge 的 equip handler 要求 **slot 和 item 都给**：slot 是目标槽名
        (head/chest/legs/feet/offhand)，item 是要穿的物品 id。不传 slot 时这里
        按物品名自动推断（iron_helmet → head，iron_chestplate → chest …）。
        """
        target = (slot or "").strip().lower()
        if not target and item:
            name = item.lower().replace("minecraft:", "")
            if "helmet" in name or "cap" in name or "skull" in name or "head" in name:
                target = "head"
            elif "chestplate" in name or "tunic" in name or "elytra" in name:
                target = "chest"
            elif "leggings" in name or "pants" in name:
                target = "legs"
            elif "boots" in name:
                target = "feet"
            else:
                target = "offhand"
        if not target:
            raise ValueError("give either item or slot")
        params: Dict[str, Any] = {"slot": target}
        if item:
            params["item"] = item
        return _call_bridge(
            ctx, "command/equip", lambda client: client.transport.dispatch("equip", params)
        )

    @mcp.tool()
    def place_torches(ctx: RequestContext, x: int = 0, y: int = 0, z: int = 0) -> Dict[str, Any]:
        """Place torches around the given spot (omit coords to use the bot's own position)."""
        def _run(client):
            params: Dict[str, Any] = {}
            if x or y or z:
                params = {"x": int(x), "y": int(y), "z": int(z)}
            else:
                # handler 明确要求坐标（原话 "Coordinates required for torch placement"），
                # 不传就报错，所以这里自己取当前位置补上。
                state = client.transport.dispatch("get_state", {}) or {}
                pos = state.get("block_position") or state.get("position") or {}
                params = {
                    "x": int(pos.get("x", 0)),
                    "y": int(pos.get("y", 0)),
                    "z": int(pos.get("z", 0)),
                }
            return client.transport.dispatch("place_torches", params)

        return _call_bridge(ctx, "command/place_torches", _run)

    @mcp.tool()
    def interact_block(
        ctx: RequestContext,
        x: int,
        y: int,
        z: int,
        hand: str = "MAIN_HAND",
    ) -> Dict[str, Any]:
        """Right-click a block (open doors, press buttons, use chests).

        ⚠ 一个必须知道的限制：

        bridge 的 handler **无视玩家朝向** —— 是它自己在目标方块周围采样一批候选点
        （中心 + 六个面心 + 5×5×5 网格，共 132 个），逐个做几何射线，取第一个真正打在
        这个方块上的点，然后按那个点替玩家转头。所以这里先 `look_at` 只是让动作看起来
        合理，真正决定成败的是**站位**（别站太远，也别把自己卡进方块里）。

        薄方块（门/按钮/拉杆）以前几乎必失败（旧实现死瞄方块中心，薄板只占方块一角，
        射线从旁边擦过去），改成采样点之后实测门和按钮都能正常开关；万一仍报
        "Target is not visible on a real block ray"，**换个方向靠近**比反复重试有效。
        """
        def _run(client):
            client.transport.dispatch(
                "look_at", {"x": x + 0.5, "y": y + 0.5, "z": z + 0.5}
            )
            time.sleep(0.25)
            return client.transport.dispatch(
                "interact_block", {"x": int(x), "y": int(y), "z": int(z), "hand": hand}
            )

        return _call_bridge(ctx, "command/interact_block", _run)

    @mcp.tool()
    def entity_interact(
        ctx: RequestContext,
        action: str = "look_at",
        entity_id: int = -1,
        entity_type: str = "",
        max_distance: float = 0.0,
    ) -> Dict[str, Any]:
        """与**实体**交互：村民交易 / 驯服 / 剪羊毛 / 挤奶 / 喂食 / 单次攻击 / 看过去。

        实体 id 从 `get_entities` 拿（每条第一个字段就是 `id`），也可以直接给
        `entity_type`（"cow" 和 "minecraft:cow" 都认）。**拿不准就先调
        `action="detect"`** —— 它列出附近每一个 (entity_id, 类型, 动作, 距离)，
        省得猜。自己的 id 在 `snapshot` 的 `entity_id` 里。

        各 action 的硬性要求（bridge 侧分了两条代码路径，不满足会直接报错）：

        - `detect` / `look_at` —— 都不用给；`look_at` 是默认动作，按类型找时搜索半径 16 格。
        - `interact` / `feed` —— **必须给 `entity_id`**，这两条路不认 `entity_type`。
        - `tame` / `shear` / `milk` / `attack` / `trade` —— id 或 type 都行。
          `tame` 只能驯 wolf/cat/parrot（且不能已驯服）、`shear` 只能剪羊且背包里要有剪刀、
          `milk` 只能挤牛/蘑菇牛且背包里要有空桶、`trade` 是右键村民**打开交易界面**
          （之后用 `get_screen` 读报价、`select_trade` 选交易）。
        - `attack` 只砍**一下**；要连续砍请用 `attack_nearest`。

        一个坑：`detect` 会把**你自己**也列出来（bridge 遍历的是
        `entitiesForRendering()`，含本地玩家，距离 0.0）—— 那是噪音，交互前按自己的 id 滤掉。
        """
        act = (action or "").strip().lower()
        known = {
            "detect",
            "look_at",
            "interact",
            "feed",
            "tame",
            "shear",
            "milk",
            "attack",
            "trade",
        }
        if act not in known:
            raise ValueError(
                f"unknown action: {action!r}; expected one of " + ", ".join(sorted(known))
            )

        has_id = int(entity_id) >= 0
        etype = (entity_type or "").strip()
        if act in ("interact", "feed") and not has_id:
            raise ValueError(
                f"action={act} requires entity_id (that bridge path ignores entity_type)"
            )
        if act != "detect" and not has_id and not etype:
            raise ValueError(
                "give entity_id or entity_type (or call action=detect to see what's nearby)"
            )

        params: Dict[str, Any] = {"action": act}
        if has_id:
            params["entity_id"] = int(entity_id)
        if etype:
            params["entity_type"] = etype
        if float(max_distance) > 0:
            params["max_distance"] = float(max_distance)
        return _call_bridge(
            ctx,
            "command/entity_interact",
            lambda client: client.transport.dispatch("entity_interact", params),
        )

    @mcp.tool()
    def look_at(ctx: RequestContext, x: float, y: float, z: float) -> Dict[str, Any]:
        """Turn the bot's head toward a world position."""
        return _call_bridge(
            ctx,
            "command/look_at",
            lambda client: client.transport.dispatch(
                "look_at", {"x": float(x), "y": float(y), "z": float(z)}
            ),
        )

    @mcp.tool()
    def get_death_location(ctx: RequestContext) -> Dict[str, Any]:
        """Where the bot last died (useful for going back for its stuff)."""
        return _call_bridge(
            ctx,
            "command/get_death_location",
            lambda client: client.transport.dispatch("get_death_location", {}),
        )

    @mcp.tool()
    def respawn(ctx: RequestContext) -> Dict[str, Any]:
        """Respawn after death."""
        return _call_bridge(
            ctx, "command/respawn", lambda client: client.transport.dispatch("respawn", {})
        )

    #
    # 信息查询（只读）
    #

    @mcp.tool()
    def get_entities(radius: int = 32, ctx: RequestContext = None) -> Dict[str, Any]:
        """List nearby entities.

        Each entry has type / name (display name) / distance / position / is_living
        / health / is_aggressive. NOTE: coordinates live under `position`, and
        `name` is the DISPLAY name (Chinese), not the account name.
        """
        return _call_bridge(
            ctx,
            "command/get_entities",
            lambda client: client.transport.dispatch("get_entities", {"radius": int(radius)}),
        )

    @mcp.tool()
    def get_player_pos(ctx: RequestContext) -> Dict[str, Any]:
        """Where the bot's player is (exact + block position)."""
        return _call_bridge(
            ctx, "command/get_player_pos", lambda client: client.transport.dispatch("get_player_pos", {})
        )

    @mcp.tool()
    def get_block(x: int, y: int, z: int, ctx: RequestContext) -> Dict[str, Any]:
        """What block sits at these coordinates."""
        return _call_bridge(
            ctx,
            "command/get_block",
            lambda client: client.transport.dispatch("get_block", {"x": int(x), "y": int(y), "z": int(z)}),
        )

    @mcp.tool()
    def get_dimension(ctx: RequestContext) -> Dict[str, Any]:
        """Which dimension the bot is in (overworld / nether / end)."""
        return _call_bridge(
            ctx, "command/get_dimension", lambda client: client.transport.dispatch("get_dimension", {})
        )

    @mcp.tool()
    def get_combat_snapshot(ctx: RequestContext) -> Dict[str, Any]:
        """Combat-focused view: health, nearby hostiles, attack cooldown."""
        return _call_bridge(
            ctx,
            "command/get_combat_snapshot",
            lambda client: client.transport.dispatch("get_combat_snapshot", {}),
        )

    @mcp.tool()
    def get_look_target(ctx: RequestContext) -> Dict[str, Any]:
        """What the crosshair is pointing at right now (read-only).

        返回 type(block/entity/miss) + 命中点坐标 + 距离 + 命中方块的完整 block_state
        （"门开没开""拉杆朝哪"这类问题一眼能看出来）。纯读取，**不会触发右键** ——
        以前想看这个只能调 use_item，那会真的动一下手。
        """
        return _call_bridge(
            ctx, "command/get_look_target", lambda client: client.transport.dispatch("get_look_target", {})
        )

    @mcp.tool()
    def get_screen(ctx: RequestContext, include_empty: bool = False) -> Dict[str, Any]:
        """当前打开的容器界面里有什么（箱子 / 熔炉 / 村民交易 / 工作台…）。

        返回 `screen`（当前 GUI 类名，`"none"` = 没开界面）与 `container`（**是否真有
        容器**）：

        - **有容器**：`container: true` + `type` / `sync_id` / `slots` / `total_slots`
          （村民交易还会带 `merchant_offers` 与 `merchant`）
        - **没容器**：`container: false`，**不倒 slots** —— 玩家不开界面时
          `containerMenu` 也是有的（是他自己的 `InventoryMenu`，46 格），照搬会白刷
          一屏；那部分内容用 `get_inventory` 看。

        `slots` **默认只列非空格**（空气槽剔除，剔掉几个记在 `empty_slots_hidden`）——
        读容器绝大多数时候只关心"里面有什么"，大箱子倒 63 格纯浪费 token。要看完整
        布局（比如找某个空格放东西）时传 `include_empty=True`。

        ⚠ `screen`（GUI，比如 `ChatScreen`）和"有没有容器"是**两件事**：开着聊天窗口
        不代表有容器可读。
        """
        def _run(client):
            state = client.transport.dispatch("get_state", {})
            screen = state.get("screen") if isinstance(state, dict) else None
            screen = screen if screen else "none"

            menu = client.transport.dispatch("get_screen", {})
            if not isinstance(menu, dict):
                return {"screen": screen, "container": False, "menu_raw": menu}

            menu_type = str(menu.get("type") or "")
            if menu_type in ("", "InventoryMenu"):
                # 没有容器 —— containerMenu 退回玩家自己的背包菜单
                return {
                    "screen": screen,
                    "container": False,
                    "note": "没有容器界面；玩家自己的背包菜单用 get_inventory 看",
                }

            slots = menu.get("slots")
            if not include_empty and isinstance(slots, list):
                kept = [
                    s
                    for s in slots
                    if isinstance(s, dict)
                    and str(s.get("id") or "") not in ("", "air", "minecraft:air")
                ]
                menu["empty_slots_hidden"] = len(slots) - len(kept)
                menu["slots"] = kept

            menu["screen"] = screen
            menu["container"] = True
            return menu

        return _call_bridge(ctx, "command/get_screen", _run)

    @mcp.tool()
    def close_screen(ctx: RequestContext) -> Dict[str, Any]:
        """关闭当前打开的界面。

        **AI 打开任何界面（箱子/熔炉/交易/工作台…）之后都应该调它** —— 界面开着的时候，
        很多需要操作角色的命令会失败。

        会**回读确认**：bridge 的 close_screen 是 fire-and-forget（只往主线程排一个
        `player.closeContainer()`），立刻返回成功不代表已经关上。这里最多轮询 1.5 秒，
        只有 `screen` 变回 `"none"` 才算 `closed: true`。

        ⚠ 它调的是 `closeContainer()`，**只能关容器类界面**；PauseScreen / DeathScreen
        这类非容器界面关不掉 —— 那种情况返回 `closed: false` 并附 hint。本来就没开界面
        时视为成功。
        """
        def _run(client):
            def _current_screen():
                state = client.transport.dispatch("get_state", {})
                value = state.get("screen") if isinstance(state, dict) else None
                return value if value else "none"

            before = _current_screen()
            if before == "none":
                return {"closed": True, "screen_before": "none", "screen_after": "none"}

            client.transport.dispatch("close_screen", {})
            after = before
            for _ in range(6):
                time.sleep(0.25)
                after = _current_screen()
                if after == "none":
                    return {"closed": True, "screen_before": before, "screen_after": "none"}

            return {
                "closed": False,
                "screen_before": before,
                "screen_after": after,
                "hint": "closeContainer() 关不掉非容器界面（如 PauseScreen）；这类界面要在 bridge 侧用 setScreen(null) 才能关",
            }

        return _call_bridge(ctx, "command/close_screen", _run)

    @mcp.tool()
    def smelt_items(
        ctx: RequestContext,
        input_slot: int = -1,
        fuel_slot: int = -1,
        expected_output: str = "",
    ) -> Dict[str, Any]:
        """往熔炉里放东西：把**背包里的**物品移到熔炉的输入槽 / 燃料槽。

        ⚠ 前置条件：**熔炉界面必须是开着的**。bridge 的 handler 直接拿
        `player.containerMenu`，不是熔炉就报 `Not in a furnace screen`。所以顺序是：
        `place_block("furnace")` → `interact_block(x,y,z)` 打开它 →
        `get_screen()` 读槽位布局（`sync_id` + 每个槽的 `slot` 号）→ 再调本工具。

        `input_slot` / `fuel_slot` 是**这个界面里的槽位号**，不是背包槽号。
        熔炉段固定是 0=输入、1=燃料、2=产物，玩家背包接在后面（所以矿石通常
        在 3~38 之间，具体看 `get_screen` 的 `total_slots`）。两个至少给一个，
        且不能相同。

        `expected_output` 传产物 id（如 `minecraft:iron_ingot`）：bridge 会拿它
        核对产物槽、并顺带判断这次转移是否真的被烧掉了一个 —— 不传也能用，
        但要判断"烧成功没有"就得传。

        返回 `input_moved` / `fuel_moved`，以及传了 `expected_output` 时的
        `input_consumed`（真的消耗掉一个输入 = 烧起来了）。
        """
        # 纯参数层面的错在校验期就报，别丢给 _call_bridge 去重试三遍 ——
        # 那儿会把 ValueError 包成 RuntimeError，错误信息也没这么直白。
        if int(input_slot) < 0 and int(fuel_slot) < 0:
            raise ValueError("至少要给 input_slot 或 fuel_slot 之一")

        def _run(client):
            params: Dict[str, Any] = {}
            if int(input_slot) >= 0:
                params["input_slot"] = int(input_slot)
            if int(fuel_slot) >= 0:
                params["fuel_slot"] = int(fuel_slot)

            screen = client.transport.dispatch("get_screen", {})
            if isinstance(screen, dict):
                menu_type = str(screen.get("type") or "")
                if menu_type and "Furnace" not in menu_type:
                    raise ValueError(
                        f"当前界面不是熔炉（{menu_type}）；先用 interact_block 打开熔炉"
                    )
                sync_id = screen.get("sync_id")
                if isinstance(sync_id, int):
                    params["sync_id"] = sync_id

            if str(expected_output).strip():
                params["expected_output"] = str(expected_output).strip()

            return client.transport.dispatch("smelt_items", params)

        return _call_bridge(ctx, "command/smelt_items", _run)

    @mcp.tool()
    def inventory_click(
        ctx: RequestContext,
        slot: int,
        button: int = 0,
        click_type: str = "PICKUP",
        sync_id: int = -1,
    ) -> Dict[str, Any]:
        """在当前打开的容器界面里**点一个槽位** —— 箱子 / 熔炉 / 工作台 / 村民交易 / 自己的背包都适用。

        这是"GUI 操作"的通用原语：MC 里所有容器都是同一个东西
        (`AbstractContainerMenu`)，操作方式都是 `handleContainerInput(syncId, slot,
        button, type)`。**按左右键是操作不了它们的** —— 按住右键一万次也拿不出熔炉里的
        铁锭，这正是以前"能烧不能取"的原因。

        前置条件：**界面必须开着**。顺序是
        `interact_block(x,y,z)` 打开 → `get_screen()` 读布局 → 再调本工具。

        `slot` 是**这个界面里的槽位号**（不是背包槽号），从 `get_screen` 返回的
        `slots[].slot` 拿。箱子槽位从 0 开始，玩家背包接在后面。

        `button`：`0` = 左键，`1` = 右键（只对 PICKUP / QUICK_CRAFT 有意义）。

        `click_type` 用 MC 的 `ContainerInput` 枚举名，常用的：

        - `PICKUP`（默认）—— 左键：空手=拿起整组，手上有=放下/交换；右键=拿一半/放一个
        - `QUICK_MOVE` —— shift+左键：整组快移到对侧（箱子↔背包，方向看 `button`）
        - `SWAP` —— 数字键：把该槽物品换到快捷栏（`button` = 0~8）
        - `THROW` —— Q 键：丢一个（`button` 0）或丢一整组（`button` 1）
        - `PICKUP_ALL` —— 双击：把同种物品全收过来
        - `CLONE`（创造模式）/ `QUICK_CRAFT`（拖拽分发）

        `sync_id` 默认不用传：这里会自己 `get_screen` 读当前界面的 id。**传错 id 比不传
        更危险** —— bridge 会直接拒掉（`Stale sync_id; no click dispatched`），这是
        "菜单已经关了/换了"的保护，防止点到别的界面上去。

        返回 `before` / `after` 两份槽位快照和 `clicked`（bridge 观察到的真实变化）——
        **`clicked: false` 不代表服务端没执行**，可能只是点了个空格子；要判断结果请再
        `get_screen()` 读一次。

        ⚠ 本工具**不做重试**（`max_retries=1`）：点槽位不是幂等操作，网络抖动时重试会
        真的点第二下（拿起来又放下）。
        """
        # 纯参数层面的错在校验期就报，别丢给 _call_bridge —— 那儿会把 ValueError
        # 包成 RuntimeError，还要白等三次退避。
        if int(slot) < 0 and int(slot) != -999:
            raise ValueError("slot 必须 >= 0（-999 是 bridge 的特殊值）；界面槽号从 get_screen 读")
        if int(button) < 0:
            raise ValueError("button 必须 >= 0（0=左键，1=右键，SWAP 时是快捷栏 0~8）")
        if not str(click_type).strip():
            raise ValueError("click_type 不能为空（常用值：PICKUP / QUICK_MOVE / SWAP / THROW）")

        def _run(client):
            params: Dict[str, Any] = {
                "slot": int(slot),
                "button": int(button),
                "type": str(click_type).strip().upper(),
            }

            if int(sync_id) >= 0:
                params["sync_id"] = int(sync_id)
            else:
                # 不传就自己读当前界面的 id —— bridge 拿它挡"菜单已过期"。
                screen = client.transport.dispatch("get_screen", {})
                if isinstance(screen, dict):
                    current = screen.get("sync_id")
                    if isinstance(current, int):
                        params["sync_id"] = current

            return client.transport.dispatch("inventory_click", params)

        # 点击不是幂等操作：重试会真的点第二下（拿起来又放下），所以只许一次。
        return _call_bridge(ctx, "command/inventory_click", _run, max_retries=1)

    @mcp.tool()
    def screenshot(
        ctx: RequestContext,
        filename: str = "",
        include_metadata: bool = False,
    ) -> Dict[str, Any]:
        """Capture the game framebuffer to a PNG under <gameDir>/screenshots/.

        **这是唯一能真正"看见"游戏画面的工具** —— 其余工具只给坐标和
        block_state。返回里的 `path` 是绝对路径，直接当图片读就行；
        需要眼见为实（找路、认地形、确认建筑、看聊天栏）时调它。

        几个实测过的坑：
        - bridge 侧是 `AsyncCommandHandler`：文件由 IO 线程写完后才回调，所以
          以返回里的 `saved` 为准，别拿"命令返回了"当"文件已经在了"。
        - `filename` 会被清洗成 [A-Za-z0-9_-]，再拼时间戳 + 自增序号，所以重名
          不会覆盖；中文名会变成一串下划线，用英文短名。
        - 只支持 PNG（传别的 format 报 Unsupported format），因此这里不给该参数。
        - 默认不带 metadata：那一坨是玩家/世界快照（坐标、血量、维度、tick…），
          看图时纯噪音，而且 get_state 里本来就有；需要传 include_metadata=True。
        """
        params: Dict[str, Any] = {"include_metadata": bool(include_metadata)}
        name = (filename or "").strip()
        if name:
            params["filename"] = name
        return _call_bridge(
            ctx,
            "command/screenshot",
            lambda client: client.transport.dispatch("screenshot", params),
        )

    @mcp.tool()
    def get_events(
        ctx: RequestContext,
        event_type: str = "",
        after_seq: int = -1,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """从 bridge 的事件缓冲里读事件（只读，不消费）。

        ⚠ **此工具酌情调用** —— 不是常规手段：

        - **聊天不用调它**：`dsh-mc-chat-watch` 插件每 3 秒轮询 chat 并主动把消息推给你，
          你不需要自己来拉。
        - **一定要带 `event_type`**。不带过滤时会混进 `tick_update` / `entity_move` /
          `block_update` 这类高频噪音，一次能刷几百条，很烧 token。
        - 真正有价值的低频事件：`death` / `damage` / `entity_attack` / `weather_change` /
          `inventory_change` / `dimension_change`；而且**态势快照已经把前几种并进去了**
          （`⚠受伤-3(僵尸)` 这种），多数时候你已经在态势行里看到了。

        参数：`event_type` 是事件名，**大小写不敏感**（内部统一转小写再发给 bridge）。
        可选值：`chat` / `block_interact` / `block_break` / `block_place` / `block_update` /
        `entity_spawn` / `entity_move` / `entity_despawn` / `pathfinding_state` / `mission` /
        `tick_update` / `dimension_change` / `inventory_change` / `death` / `respawn` /
        `entity_update` / `damage` / `entity_attack` / `entity_tame` / `entity_shear` /
        `entity_milk` / `weather_change` / `time_change`。
        `after_seq` 是游标 —— 传上次返回的 `latest_seq` 就是"只看新的"；不传则默认回看最近
        `limit` 条。`limit` 上限 1000。

        （实现上**永远走游标模式**：bridge 的 get_events 不带游标时会走 `pollEvents()`，
        那里有 `iterator.remove()` 会把事件**从缓冲区抽走** —— 那会连聊天事件一起抢掉。）
        """
        def _run(client):
            params: Dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
            if event_type:
                # bridge 侧 EventManager 用大小写敏感的 equals 过滤（EventManager.java:327/397），
                # 而缓冲里存的 type 一律是小写（EventType.TICK_UPDATE → "tick_update"）。
                # 这里必须 lower()：早先写成 upper()，导致**所有带 event_type 的查询静默返回 0 条**。
                params["type"] = str(event_type).strip().lower()

            cursor = int(after_seq) if after_seq is not None else -1
            if cursor < 0:
                # 不给游标时：先探一下当前末尾，再回看 limit 条，得到一个有意义的默认
                probe = client.transport.dispatch("get_events", {"after_seq": 0, "limit": 1})
                latest = 0
                if isinstance(probe, dict):
                    latest = int(probe.get("latest_seq") or 0)
                cursor = max(0, latest - params["limit"])
            params["after_seq"] = cursor
            return client.transport.dispatch("get_events", params)

        return _call_bridge(ctx, "command/get_events", _run)

    @mcp.tool()
    def get_recipes(ctx: RequestContext) -> Dict[str, Any]:
        """List the crafting recipes the bot knows.

        ⚠ KNOWN BROKEN on MC 1.21.4: the bridge answers
        "Recipe listing not available on client in 1.21.4. Using defaults."
        （上游限制 —— 新版本客户端不再暴露完整配方表。）
        所以别指望它来查配方：`craft` 用的是 bridge 里内置的配方表，
        直接 craft("oak_planks", 4) 就行，不需要先查。
        """
        return _call_bridge(
            ctx, "command/get_recipes", lambda client: client.transport.dispatch("get_recipes", {})
        )

    #
    # 合成与方块操作
    #

    @mcp.tool()
    def craft(item: str, ctx: RequestContext, count: int = 1) -> Dict[str, Any]:
        """Craft an item, e.g. craft("oak_planks", 4).

        bridge 的 craft handler 认 item + count；3x3 配方（石镐之类）必须先打开
        工作台界面，否则报 "Recipe requires crafting table but none is open"。
        """
        return _call_bridge(
            ctx,
            "command/craft",
            lambda client: client.transport.dispatch("craft", {"item": item, "count": int(count)}),
        )

    @mcp.tool()
    def place_block(block: str, x: int, y: int, z: int, ctx: RequestContext) -> Dict[str, Any]:
        """Place a block at coordinates (it must be in the inventory).

        bridge 的 place_block 认 `block` + `position`（对象），且**要求主手拿着那个方块**
        ——否则报 "Main hand must contain a block item"。所以这里先自动把手持切到它
        （只在快捷栏 0-8 里找；在背包深处的话得先自己挪出来）。
        """
        wanted = (block or "").strip().replace("minecraft:", "")

        def _run(client):
            inventory = client.transport.dispatch("get_inventory", {}) or {}
            for entry in inventory.get("inventory") or []:
                item = str(entry.get("id") or "").replace("minecraft:", "")
                if item == wanted and int(entry.get("count") or 0) > 0:
                    slot = int(entry.get("slot", -1))
                    if 0 <= slot <= 8:
                        client.transport.dispatch("select_slot", {"slot": slot})
                        time.sleep(0.2)
                    break
            return client.transport.dispatch(
                "place_block", {"block": block, "position": {"x": int(x), "y": int(y), "z": int(z)}}
            )

        return _call_bridge(ctx, "command/place_block", _run)

    @mcp.tool()
    def dig_block(x: int, y: int, z: int, ctx: RequestContext) -> Dict[str, Any]:
        """Break a single block at coordinates."""
        return _call_bridge(
            ctx,
            "command/dig_block",
            lambda client: client.transport.dispatch("dig_block", {"x": int(x), "y": int(y), "z": int(z)}),
        )

    @mcp.tool()
    def use_bucket(
        operation: str,
        x: int,
        y: int,
        z: int,
        ctx: RequestContext,
    ) -> Dict[str, Any]:
        """Use a bucket at coordinates.

        operation is either "pickup" (fill the bucket from a water source) or
        "place" (pour water out). The main hand must already hold the matching
        item — empty bucket for pickup, water bucket for place.
        """
        op = (operation or "").strip().lower()
        if op in ("fill", "take", "scoop"):
            op = "pickup"
        if op in ("empty", "pour", "dump"):
            op = "place"
        if op not in ("pickup", "place"):
            raise ValueError('operation must be "pickup" or "place"')
        return _call_bridge(
            ctx,
            "command/use_bucket",
            lambda client: client.transport.dispatch(
                "use_bucket", {"operation": op, "x": int(x), "y": int(y), "z": int(z)}
            ),
        )

    @mcp.tool()
    def batch_command(commands: List[str], ctx: RequestContext, stop_on_error: bool = True) -> Dict[str, Any]:
        """Execute multiple Baritone commands sequentially with proper error handling."""
        results = []
        for i, command in enumerate(commands):
            try:
                result = _call_bridge(ctx, f"batch/command/{i}", lambda client: client.command.run(command))
                results.append({"command": command, "result": result, "success": True})
            except Exception as e:
                error_result = {"command": command, "error": str(e), "success": False}
                results.append(error_result)
                if stop_on_error:
                    break
        return {"batch_results": results, "total_commands": len(commands), "executed_commands": len(results)}

    @mcp.tool()
    def cancel_command(ctx: RequestContext) -> Dict[str, Any]:
        """Cancel the active Baritone task."""
        return _call_bridge(ctx, "command/cancel", lambda client: client.command.cancel())

    @mcp.tool()
    def set_goal_block(x: int, y: int, z: int, ctx: RequestContext) -> Dict[str, Any]:
        """Apply a block-level navigation goal using goto command (bridge doesn't support GoalBlock)."""
        command = f"goto {x} {y} {z}"
        return _call_bridge(ctx, "command/run", lambda client: client.command.run(command))

    @mcp.tool()
    def clear_goal(ctx: RequestContext) -> Dict[str, Any]:
        """Clear the current pathing goal."""
        return _call_bridge(ctx, "goal/clear", lambda client: client.goals.clear())

    @mcp.tool()
    def mission_macro(
        name: Optional[str],
        ctx: RequestContext,
        dequeue: bool = False,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Invoke a mission macro or consume the queued macros (dequeue=True)."""
        return _call_bridge(
            ctx,
            "mission/macro",
            lambda client: client.mission.macro(name=name, params=params, dequeue=dequeue),
        )

    @mcp.tool()
    def mission_queue(actions: list[str], ctx: RequestContext, clear: bool = False) -> Dict[str, Any]:
        """Queue mission macros that the bridge should execute sequentially."""
        return _call_bridge(ctx, "mission/queue", lambda client: client.mission.queue(actions, clear=clear))

    @mcp.tool()
    def mission_checkpoint(phase: str, ctx: RequestContext, note: Optional[str] = None) -> Dict[str, Any]:
        """Persist the active phase/checkpoint on the bridge."""
        return _call_bridge(ctx, "mission/checkpoint", lambda client: client.mission.checkpoint(phase, note=note))

    @mcp.tool()
    def start_mining(target_block: str, ctx: RequestContext, quantity: Optional[int] = None) -> Dict[str, Any]:
        """Start the mining process for a given target block."""
        return _call_bridge(
            ctx,
            "process/mine/start",
            lambda client: client.process.mine.start(target_block=target_block, quantity=quantity),
        )

    @mcp.tool()
    def stop_mining(ctx: RequestContext) -> Dict[str, Any]:
        """Stop the mining process."""
        return _call_bridge(ctx, "process/mine/stop", lambda client: client.process.mine.stop())

    @mcp.tool()
    def snapshot(
        ctx: RequestContext,
        include_state: bool = True,
        include_inventory: bool = True,
        include_mission: bool = True,
    ) -> Dict[str, Any]:
        """Return a combined snapshot of bridge telemetry."""

        def _collect(client: Client) -> Dict[str, Any]:
            data: Dict[str, Any] = {}
            if include_state:
                result = client.command_dispatcher.dispatch("get_state", {})
                data["state"] = result.get_data() if result.is_success() else {"error": result.get_error_message()}
            if include_inventory:
                result = client.command_dispatcher.dispatch("get_inventory", {})
                data["inventory"] = result.get_data() if result.is_success() else {"error": result.get_error_message()}
            if include_mission:
                data["mission"] = client.mission.status()
            return data

        return _call_bridge(ctx, "snapshot", _collect)

    @mcp.tool()
    def get_cached_block(x: int, y: int, z: int, ctx: RequestContext) -> Dict[str, Any]:
        """Get cached block information at coordinates."""
        return _call_bridge(ctx, "cache/get_block", lambda client: client.cache.get_cached_block(x, y, z))

    @mcp.tool()
    def cache_block(x: int, y: int, z: int, block_state: str, ctx: RequestContext) -> Dict[str, Any]:
        """Cache block state information."""
        success = _call_bridge(ctx, "cache/put_block", lambda client: client.cache.cache_block(x, y, z, block_state))
        return {"success": success}

    @mcp.tool()
    def clear_cache(ctx: RequestContext) -> Dict[str, Any]:
        """Clear all cached data."""
        success = _call_bridge(ctx, "cache/clear", lambda client: client.cache.clear())
        return {"success": success}

    @mcp.tool()
    def start_schematic_upload(name: str, expected_size: int, ctx: RequestContext, priority: str = "normal") -> Dict[str, Any]:
        """Start uploading a schematic file."""
        from ..utils.upload_manager import UploadPriority
        upload_priority = UploadPriority(priority.lower())
        success = _call_bridge(ctx, "upload/start", lambda client: client.upload.start_upload(name, expected_size, upload_priority))
        return {"success": success}

    @mcp.tool()
    def get_upload_progress(name: str, ctx: RequestContext) -> Dict[str, Any]:
        """Get progress of an upload."""
        progress = _call_bridge(ctx, "upload/progress", lambda client: client.upload.get_upload_progress(name))
        if progress is None:
            return {"error": "Upload not found"}
        return progress.to_dict()

    @mcp.tool()
    def cancel_upload(name: str, ctx: RequestContext) -> Dict[str, Any]:
        """Cancel an upload."""
        success = _call_bridge(ctx, "upload/cancel", lambda client: client.upload.cancel_upload(name))
        return {"success": success}

    @mcp.tool()
    def poll_events(
        ctx: RequestContext,
        event_types: Optional[List[str]] = None,
        max_events: Optional[int] = None
    ) -> Dict[str, Any]:
        """Poll events from the EventManager buffer."""
        events = _call_bridge(ctx, "events/poll", lambda client: client.poll_events(
            event_types=set(event_types) if event_types else None,
            max_events=max_events
        ))
        return {"events": [event.to_dict() for event in events]}

    @mcp.tool()
    def subscribe_events(
        ctx: RequestContext,
        event_types: List[str],
        priority_threshold: Optional[int] = None
    ) -> Dict[str, Any]:
        """Subscribe to real-time event streaming for specified event types."""
        from ..transport.enums import TransportEvent
        try:
            event_enums = {TransportEvent(et) for et in event_types}
            success = _call_bridge(ctx, "events/subscribe", lambda client: client.transport.subscribe_events(
                event_types=event_enums,
                priority_threshold=priority_threshold or 0
            ))
            return {"success": True, "subscribed_events": event_types}
        except ValueError as e:
            return {"success": False, "error": f"Invalid event type: {e}"}

    @mcp.tool()
    def unsubscribe_events(
        ctx: RequestContext,
        event_types: List[str]
    ) -> Dict[str, Any]:
        """Unsubscribe from real-time event streaming for specified event types."""
        from ..transport.enums import TransportEvent
        try:
            event_enums = {TransportEvent(et) for et in event_types}
            success = _call_bridge(ctx, "events/unsubscribe", lambda client: client.transport.unsubscribe_events(event_types=event_enums))
            return {"success": True, "unsubscribed_events": event_types}
        except ValueError as e:
            return {"success": False, "error": f"Invalid event type: {e}"}

    @mcp.tool()
    def subscribe_mission_updates(ctx: RequestContext, priority_threshold: Optional[int] = None) -> Dict[str, Any]:
        """Subscribe to real-time mission state updates and collaborative mission events."""
        return _call_bridge(ctx, "mission/subscribe", lambda client: client.transport.subscribe_events(
            event_types={"mission"}, priority_threshold=priority_threshold or 0
        ))

    @mcp.tool()
    def unsubscribe_mission_updates(ctx: RequestContext) -> Dict[str, Any]:
        """Unsubscribe from real-time mission state updates."""
        return _call_bridge(ctx, "mission/unsubscribe", lambda client: client.transport.unsubscribe_events(event_types={"mission"}))

    @mcp.tool()
    def broadcast_mission_state(ctx: RequestContext, mission_data: Dict[str, Any], priority: int = 0) -> Dict[str, Any]:
        """Broadcast mission state update to all subscribed MCP clients."""
        return _call_bridge(ctx, "mission/broadcast", lambda client: client.transport.emit(
            "mission", mission_data, priority=priority
        ))

    @mcp.tool()
    def get_rate_limit_info(ctx: RequestContext) -> Dict[str, Any]:
        """Get current rate limiting information."""
        info = _call_bridge(ctx, "rate_limit/info", lambda client: client.command_dispatcher.get_rate_limit_info())
        return info

    @mcp.tool()
    def get_command_analytics(command_name: str, ctx: RequestContext) -> Dict[str, Any]:
        """Get performance analytics for a specific command."""
        analytics = _call_bridge(ctx, "analytics/command", lambda client: client.get_command_analytics(command_name))
        if analytics is None:
            return {"error": f"No analytics available for command: {command_name}"}
        return analytics.model_dump() if hasattr(analytics, 'model_dump') else analytics.__dict__

    @mcp.tool()
    def get_system_health_alerts(ctx: RequestContext) -> Dict[str, Any]:
        """Get current system health alerts and warnings."""
        status = _call_bridge(ctx, "health/alerts", lambda client: client.get_circuit_breaker_status())
        health = _call_bridge(ctx, "health/status", lambda client: client.get_bridge_health())

        alerts = []
        if hasattr(health, 'thread_pool_utilization') and health.thread_pool_utilization > 0.9:
            alerts.append({"level": "warning", "message": "Thread pool utilization is high", "metric": "thread_pool_utilization", "value": health.thread_pool_utilization})

        if hasattr(health, 'memory_usage_mb') and health.memory_usage_mb > 1000:
            alerts.append({"level": "warning", "message": "High memory usage detected", "metric": "memory_usage_mb", "value": health.memory_usage_mb})

        return {
            "circuit_breaker_status": status,
            "health_alerts": alerts,
            "overall_status": "healthy" if not alerts else "warning"
        }

    @mcp.tool()
    def get_performance_trends(ctx: RequestContext, hours: int = 24) -> Dict[str, Any]:
        """Get performance trend analysis over the specified time period."""
        report = _call_bridge(ctx, "analytics/trends", lambda client: client.get_performance_report())
        return {
            "performance_report": report.model_dump() if hasattr(report, 'model_dump') else report.__dict__,
            "analysis_period_hours": hours,
            "trend_analysis": "Performance data collected for trend analysis"
        }

    #
    # Movement & Goals
    #

    @mcp.tool()
    def thisway(blocks: int, ctx: RequestContext) -> Dict[str, Any]:
        """Go in the direction you are facing for a specified number of blocks."""
        # thisway 是 Baritone 原生命令，bridge 没有对应的 handler。
        # 必须显式带 "#" 走 chat：否则会先进 sequence 通道，而 sequence 只认
        # craft/goto/mine，会把它当未知命令拒掉（虽然 `run` 有回退，但多一次往返）。
        return _call_bridge(
            ctx,
            "command/thisway",
            lambda client: client.transport.dispatch("chat", {"message": f"#thisway {blocks}"}),
        )

    @mcp.tool()
    def path(ctx: RequestContext) -> Dict[str, Any]:
        """Start pathing to the current goal."""
        return _call_bridge(ctx, "command/chat", lambda client: client.transport.dispatch("chat", {"message": "#path"}))

    @mcp.tool()
    def goal_coordinates(x: Optional[int], y: Optional[int], z: Optional[int], ctx: RequestContext) -> Dict[str, Any]:
        """Set a goal to the specified coordinates. Provide at least one coordinate."""
        if x is None and y is None and z is None:
            return {"error": "At least one coordinate must be specified"}
        # 注意：bridge 的 `goal` handler 要的是 type/value 两个参数，不是 x/y/z。
        # 坐标目标在 bridge 这边对应的是 `goto`（GotoCommandHandler 认 x/y/z）。
        params: Dict[str, Any] = {}
        if x is not None:
            params["x"] = int(x)
        if y is not None:
            params["y"] = int(y)
        if z is not None:
            params["z"] = int(z)
        return _call_bridge(
            ctx,
            "command/goal_coordinates",
            lambda client: client.transport.dispatch("goto", params),
        )

    @mcp.tool()
    def goto_block_type(block_type: str, ctx: RequestContext) -> Dict[str, Any]:
        """Go to a block of a specific type (e.g. 'diamond_ore', 'portal', 'ender_chest')."""
        # bridge 的 `goto` handler 只认坐标，没有「去某种方块」的能力；
        # 那是 Baritone 原生命令 `#goto <block>` 的活，所以显式走 chat。
        return _call_bridge(
            ctx,
            "command/goto_block_type",
            lambda client: client.transport.dispatch(
                "chat", {"message": f"#goto {block_type}"}
            ),
        )

    @mcp.tool()
    def follow_entity(entity_type: str, ctx: RequestContext) -> Dict[str, Any]:
        """Follow entities of a specific type (e.g. 'pig', 'cow')."""
        # ⚠ bridge 的 GotoCommandHandler 把 entity 原样拼进 "#follow <entity>"，
        # 而 Baritone 的真实语法是 `#follow player <名字>` / `#follow entity <类型>`。
        # 必须把限定词一起带上，否则发出去的是 `#follow pig` —— 无效命令，
        # 表现为「命令返回 applied:true 但角色一动不动」（实测确认）。
        return _call_bridge(
            ctx,
            "command/follow_entity",
            lambda client: client.transport.dispatch(
                "follow", {"action": "follow", "entity": f"entity {entity_type}"}
            ),
        )

    @mcp.tool()
    def follow_player(player_name: str, ctx: RequestContext) -> Dict[str, Any]:
        """Follow a specific player."""
        return _call_bridge(
            ctx,
            "command/follow_player",
            lambda client: client.transport.dispatch(
                "follow", {"action": "follow", "entity": f"player {player_name}"}
            ),
        )

    @mcp.tool()
    def come(ctx: RequestContext) -> Dict[str, Any]:
        """Tell Baritone to head towards your camera."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("come"))

    @mcp.tool()
    def invert(ctx: RequestContext) -> Dict[str, Any]:
        """Invert the current goal and path (run away from goal)."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("invert"))

    @mcp.tool()
    def blacklist(ctx: RequestContext) -> Dict[str, Any]:
        """Blacklist the closest block so Baritone won't attempt to get to it."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("blacklist"))

    @mcp.tool()
    def explore(x: Optional[int], z: Optional[int], ctx: RequestContext) -> Dict[str, Any]:
        """Explore the world from the origin of x,z (or player feet if omitted)."""
        # bridge 有 explore handler（认 x/z），直接发结构化参数，别走字符串解析。
        params: Dict[str, Any] = {}
        if x is not None:
            params["x"] = int(x)
        if z is not None:
            params["z"] = int(z)
        return _call_bridge(
            ctx, "command/explore", lambda client: client.transport.dispatch("explore", params)
        )

    @mcp.tool()
    def axis(ctx: RequestContext) -> Dict[str, Any]:
        """Go to an axis or diagonal axis."""
        return _call_bridge(
            ctx, "command/axis", lambda client: client.transport.dispatch("axis", {})
        )

    @mcp.tool()
    def surface(ctx: RequestContext) -> Dict[str, Any]:
        """Head towards the closest surface-like area."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("surface"))

    #
    # Building & Mining
    #

    @mcp.tool()
    def build_schematic(name: str, x: Optional[int], y: Optional[int], z: Optional[int], ctx: RequestContext) -> Dict[str, Any]:
        """Build a schematic. Optionally specify origin coordinates."""
        # bridge 的 build handler 认 action/schematic/x/y/z —— 走结构化参数，
        # 老实现发 "build <name> x y z"，解析层接不住。
        params: Dict[str, Any] = {"action": "schematic", "schematic": name}
        if x is not None:
            params["x"] = int(x)
        if y is not None:
            params["y"] = int(y)
        if z is not None:
            params["z"] = int(z)
        return _call_bridge(
            ctx, "command/build", lambda client: client.transport.dispatch("build", params)
        )

    @mcp.tool()
    def tunnel(height: int, width: int, depth: int, ctx: RequestContext) -> Dict[str, Any]:
        """Dig a tunnel with specified dimensions."""
        # Baritone 原生命令就是 `#tunnel <height> <width> <depth>`，而 bridge 的
        # `tunnel` handler 收的是 radius/width/坐标，两边语义对不上。
        # 这里按 API 文档的语义走 Baritone 原生命令。
        return _call_bridge(
            ctx,
            "command/tunnel",
            lambda client: client.transport.dispatch(
                "chat", {"message": f"#tunnel {int(height)} {int(width)} {int(depth)}"}
            ),
        )

    @mcp.tool()
    def farm(range: Optional[int], waypoint: Optional[str], ctx: RequestContext) -> Dict[str, Any]:
        """Automatically harvest, replant, or bone meal crops."""
        # bridge 的 farm handler 认 range（还有 crop/replant/坐标），没有 waypoint。
        params: Dict[str, Any] = {}
        if range is not None:
            params["range"] = int(range)
        return _call_bridge(
            ctx, "command/farm", lambda client: client.transport.dispatch("farm", params)
        )

    @mcp.tool()
    def mine_quantity(block_type: str, quantity: int, ctx: RequestContext) -> Dict[str, Any]:
        """Mine a block type until `quantity` NEW ones are collected (0 = until none left).

        NOTE: 走 Baritone 原生 `#mine <block>`。bridge 那个 `mine` handler 实测是坏的
        ——它返回 applied:true 但 MineProcess 起来就静止，位置和背包都不动
        （2026-10-13 排查确认）。Baritone 的 #mine 本身没有数量参数，会一直挖到
        附近没有这种方块为止；所以给了 quantity 时这里盯着背包，攒够就 cancel。
        """
        wanted = (block_type or "").strip().replace("minecraft:", "")
        if not wanted:
            raise ValueError("block_type is required")
        target = int(quantity or 0)

        def _run(client):
            def count_of(entry_list):
                total = 0
                for entry in entry_list or []:
                    item = str(entry.get("id") or "").replace("minecraft:", "")
                    if item == wanted:
                        total += int(entry.get("count") or 0)
                return total

            before = count_of((client.transport.dispatch("get_inventory", {}) or {}).get("inventory"))
            client.transport.dispatch("chat", {"message": f"#mine {wanted}"})
            if target <= 0:
                return {
                    "started": True,
                    "block": wanted,
                    "had_before": before,
                    "note": "no quantity given — will keep mining until none are left; cancel to stop",
                }

            # ⚠ 轮询上限必须**低于工具调用超时**（cordis.patch.yml 的
            # toolCallTimeoutMs，当前 120s），否则这边还在等、那边已经报
            # "Request timed out"（踩过：原来写了 150s）。
            # 数量多的时候不硬等，先回一句「在挖了」，让调用方自己查背包。
            if target > 5:
                return {
                    "started": True,
                    "block": wanted,
                    "requested": target,
                    "had_before": before,
                    "note": "large order — mining runs in background; poll get_inventory (or cancel) to track it",
                }

            deadline = time.time() + 90
            gained = 0
            while time.time() < deadline:
                time.sleep(1.0)
                inv = client.transport.dispatch("get_inventory", {}) or {}
                gained = count_of(inv.get("inventory")) - before
                if gained >= target:
                    client.transport.dispatch("cancel", {})
                    return {
                        "mined": gained,
                        "requested": target,
                        "block": wanted,
                        "note": "stopped after reaching the requested amount",
                    }
            # 超时**必须 cancel**。以前这里只是返回"还在挖"，把 Baritone 任务
            # 留在后台跑 —— 结果 AI 被它拖着到处走，之后所有操作都被干扰
            # （2026-10-13 实测：看上去像"挖两格→放不了→再挖两格"的死循环）。
            # 宁可这一单挖不完，也不要留下一个看不见的活跃任务。
            client.transport.dispatch("cancel", {})
            return {
                "mined": gained,
                "requested": target,
                "block": wanted,
                "note": "已超时并停止（90s 没凑够）。任务已取消，需要继续就再调一次。",
            }

        return _call_bridge(ctx, "command/mine_quantity", _run)

    #
    # Utility & Info
    #

    @mcp.tool()
    def eta(ctx: RequestContext) -> Dict[str, Any]:
        """Get information about the estimated time until the next segment and the goal."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("eta"))

    @mcp.tool()
    def proc(ctx: RequestContext) -> Dict[str, Any]:
        """View miscellaneous information about the process currently controlling Baritone."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("proc"))

    @mcp.tool()
    def find(block_type: str, ctx: RequestContext) -> Dict[str, Any]:
        """Search through Baritone's cache and attempt to find the location of the block."""
        # bridge 的 handler 叫 find_blocks（认 blocks/radius/limit/center），
        # 不是 find —— 之前发 "find xxx" 会被 sequence 当未知命令拒掉。
        return _call_bridge(
            ctx,
            "command/find",
            lambda client: client.transport.dispatch("find_blocks", {"blocks": [block_type]}),
        )

    @mcp.tool()
    def version(ctx: RequestContext) -> Dict[str, Any]:
        """Get the version of Baritone."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("version"))

    @mcp.tool()
    def gc(ctx: RequestContext) -> Dict[str, Any]:
        """Call System.gc() to free up memory."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("gc"))

    @mcp.tool()
    def repack(ctx: RequestContext) -> Dict[str, Any]:
        """Re-cache the chunks around you."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("repack"))

    @mcp.tool()
    def render(ctx: RequestContext) -> Dict[str, Any]:
        """Fix glitched chunk rendering."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("render"))

    @mcp.tool()
    def reload_all(ctx: RequestContext) -> Dict[str, Any]:
        """Reload Baritone's world cache."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("reloadall"))

    @mcp.tool()
    def save_all(ctx: RequestContext) -> Dict[str, Any]:
        """Save Baritone's world cache."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("saveall"))

    #
    # Waypoints
    #

    @mcp.tool()
    def wp_save(name: str, x: Optional[int], y: Optional[int], z: Optional[int], ctx: RequestContext) -> Dict[str, Any]:
        """Save a waypoint. Uses current position if coordinates are not provided."""
        cmd = f"wp save {name}"
        if x is not None and y is not None and z is not None:
             cmd += f" {x} {y} {z}"
        return _call_bridge(ctx, "command/run", lambda client: client.command.run(cmd))

    @mcp.tool()
    def wp_delete(name: str, ctx: RequestContext) -> Dict[str, Any]:
        """Delete a waypoint by name."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run(f"wp delete {name}"))

    @mcp.tool()
    def wp_list(tag: str, ctx: RequestContext) -> Dict[str, Any]:
        """List waypoints with a specific tag."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run(f"wp list {tag}"))

    @mcp.tool()
    def wp_goal(name: str, ctx: RequestContext) -> Dict[str, Any]:
        """Set a goal to a waypoint."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run(f"wp goal {name}"))

    #
    # Interaction
    #

    @mcp.tool()
    def click(ctx: RequestContext) -> Dict[str, Any]:
        """Click the destination on the screen (where player is looking)."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("click"))

    #
    # Schematica Integration
    #

    @mcp.tool()
    def build_open_schematic(ctx: RequestContext) -> Dict[str, Any]:
        """Build the schematic that is currently open in Schematica."""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("schematica"))

    #
    # Settings Management
    #

    @mcp.tool()
    def set_setting(name: str, value: Any, ctx: RequestContext) -> Dict[str, Any]:
        """Set a Baritone setting. For booleans, the value is option (toggles if omitted in chat, but here we require value)."""
        # bridge 的 settings handler 认 `set` + `value` 两个键。
        # 老实现发的是裸字符串 "allowSprint true"（连命令名都没有），必挂。
        return _call_bridge(
            ctx,
            "command/set_setting",
            lambda client: client.transport.dispatch("settings", {"set": name, "value": str(value)}),
        )

    @mcp.tool()
    def get_setting(name: str, ctx: RequestContext) -> Dict[str, Any]:
        """Read the current value of a Baritone setting."""
        return _call_bridge(
            ctx,
            "command/get_setting",
            lambda client: client.transport.dispatch("settings", {"get": name}),
        )

    @mcp.tool()
    def reset_setting(name: str, ctx: RequestContext) -> Dict[str, Any]:
        """Reset a specific setting to its default value."""
        # 这是 Baritone 原生命令（`#<name> reset`），bridge 没有对应 handler。
        return _call_bridge(
            ctx,
            "command/reset_setting",
            lambda client: client.transport.dispatch("chat", {"message": f"#{name} reset"}),
        )

    @mcp.tool()
    def reset_all_settings(ctx: RequestContext) -> Dict[str, Any]:
        """Reset all settings to their default values."""
        return _call_bridge(
            ctx,
            "command/reset_all_settings",
            lambda client: client.transport.dispatch("chat", {"message": "#reset"}),
        )

    @mcp.tool()
    def get_modified_settings(ctx: RequestContext) -> Dict[str, Any]:
        """See all settings that have been modified from their default values."""
        # Baritone 原生命令，结果会打印到游戏聊天里。
        return _call_bridge(
            ctx,
            "command/get_modified_settings",
            lambda client: client.transport.dispatch("chat", {"message": "#modified"}),
        )

    #
    # Miscellaneous
    #

    @mcp.tool()
    def help_command(query: Optional[str], ctx: RequestContext) -> Dict[str, Any]:
        """Get help for Baritone commands."""
        cmd = "help"
        if query:
            cmd += f" {query}"
        return _call_bridge(ctx, "command/run", lambda client: client.command.run(cmd))

    @mcp.tool()
    def minecraft_help(
        topic: str = "workflow",
        task: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return static, read-only guidance for MCP agents."""
        normalized = _normalize_guidance_topic(topic)
        if normalized in {"build-plan", "buildplan", "planner", "build-plan-guidance", "build_planner"}:
            return {"topic": normalized, "prompt": build_build_plan_prompt(task)}
        if normalized in {"buildsite", "build-site", "build-site-guidance", "build-site-review"}:
            return {"topic": normalized, "prompt": build_buildsite_prompt(task)}
        return {"topic": normalized, "prompt": build_workflow_prompt(task)}

    @mcp.tool()
    def minecraft_describe_tool(name: str) -> Dict[str, Any]:
        """Return stable guidance metadata for the selected MCP tool."""
        return get_tool_description(name)

    @mcp.tool()
    def inspect_build_site(radius: int, ctx: RequestContext) -> Dict[str, Any]:
        """Inspect a build site candidate by radius before any mutation."""
        if radius < 4 or radius > 24:
            raise ValueError("inspect_build_site radius must be between 4 and 24 blocks.")
        return _call_bridge(
            ctx,
            "inspect_build_site",
            lambda client: client.transport.dispatch("inspect_build_site", {"radius": int(radius)}),
        )

    @mcp.tool()
    def preview_build_plan(build_plan: Any) -> Dict[str, Any]:
        """Generate a deterministic build-plan preview without bridge connection."""
        preview = compile_plan_preview(build_plan)
        return preview.model_dump()

    @mcp.tool()
    def damn(ctx: RequestContext) -> Dict[str, Any]:
        """daniel"""
        return _call_bridge(ctx, "command/run", lambda client: client.command.run("damn"))

    @mcp.tool()
    def read_file(file_path: str, ctx: RequestContext) -> Dict[str, Any]:
        """Read the contents of a file."""
        try:
            with open(file_path, 'r') as f:
                content = f.read()
            return {"success": True, "content": content}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return mcp


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Expose the Baritone bridge as an MCP server.")
    parser.add_argument("--host", default="localhost", help="Baritone bridge host")
    parser.add_argument("--port", type=int, default=5555, help="Baritone bridge port")
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Per-request TCP timeout when talking to the bridge (seconds)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport to expose",
    )
    parser.add_argument("--http-host", default="127.0.0.1", help="Bind address for streamable HTTP transport")
    parser.add_argument("--http-port", type=int, default=8000, help="TCP port for streamable HTTP transport")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = BridgeConfig(host=args.host, port=args.port, timeout=args.timeout)
    server = create_mcp_server(config)

    logger.info(
        "Starting MCP server '%s' via %s transport",
        server.name,
        args.transport,
    )

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.settings.host = args.http_host
        server.settings.port = args.http_port
        server.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
