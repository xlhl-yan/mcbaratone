import sys
from unittest import TestCase
from unittest.mock import MagicMock

from baritone_client import (
    Client,
    MovementStatus,
    ObservedChatMessage,
    PathCalculationResultType,
    PathingCommandType,
)
from baritone_client.core.facades.goals import GoalFactory
from baritone_client.models.models import Selection, BetterBlockPos
from baritone_client.core.facades.processes import BuilderProcess


class DummyTransport:
    def __init__(self):
        self.dispatched = []
        self.events = MagicMock()

    def dispatch(self, route, payload, **kwargs):
        self.dispatched.append((route, payload))
        return {"status": "ok", "data": payload, "route": route, "payload": payload}

    def subscribe(self, event, callback):
        self.events.subscribe(event, callback)

    def emit(self, event, payload):
        self.events.dispatch(event, payload)

    def shutdown(self):
        self.dispatched.append(("shutdown", {}))


class ClientFacadeTest(TestCase):
    def setUp(self):
        self.transport = DummyTransport()
        self.client = Client(self.transport)

    def test_command_dispatch(self):
        """不带 # 的命令走 bridge 的 sequence 通道。

        老行为是把它当聊天消息发出去：bridge 的 chat handler 会把它敲进游戏聊天
        然后报「成功」，实际什么也没做 —— `run_command("craft oak_planks 4")`
        静默失效就是这么来的。现在先走 sequence，失败才回退 `#` 前缀的 chat。
        """
        response = self.client.command.run("goto 1 64 1")
        expected_payload = {
            "command": "sequence",
            "params": {"sequence": "goto 1 64 1", "priority": "normal"},
        }
        self.assertIn(("command", expected_payload), self.transport.dispatched)
        self.assertEqual(expected_payload, response)

    def test_explicit_baritone_command_goes_to_chat(self):
        """带 # 前缀的仍然是原样聊天命令（bridge 的 chat handler 负责执行）。"""
        self.client.command.run("#allowBreak true")
        expected_payload = {
            "command": "chat",
            "params": {"message": "#allowBreak true", "priority": "normal"},
        }
        self.assertIn(("command", expected_payload), self.transport.dispatched)

    def test_goal_serialization_and_apply(self):
        goal = GoalFactory.goal_block(1, 2, 3)
        response = self.client.goals.apply(goal)
        self.assertEqual("goal/apply", response["route"])
        self.assertEqual(goal.to_dict(), response["payload"])

    def test_settings_validation(self):
        response = self.client.settings.set("allowSprint", True)
        self.assertEqual({"name": "allowSprint", "value": True}, response["payload"])

    def test_builder_process_selection(self):
        selection = Selection(start=BetterBlockPos(x=0, y=64, z=0), end=BetterBlockPos(x=1, y=65, z=1))
        builder = BuilderProcess(self.transport)
        result = builder.start("house", selection=selection)
        self.assertEqual("process/builder/start", result["route"])
        self.assertIn("selection", result["payload"])
        self.assertEqual({"start": {"x": 0, "y": 64, "z": 0}, "end": {"x": 1, "y": 65, "z": 1}}, result["payload"]["selection"])

    def test_process_helpers(self):
        self.client.process.movement_status(MovementStatus.RUNNING)
        self.client.process.calculation_result(PathCalculationResultType.SUCCESS)
        self.assertIn(("process/status", {"status": MovementStatus.RUNNING.value}), self.transport.dispatched)
        self.assertIn(("process/path_result", {"result": PathCalculationResultType.SUCCESS.value}), self.transport.dispatched)

    def test_create_chat_gateway_uses_this_bot_transport(self):
        source = MagicMock()
        source.poll.return_value = [ObservedChatMessage("Alice", "!crew status")]
        gateway = self.client.create_chat_gateway(
            source,
            speaker_name="Bot07",
            allowed_actors=("Alice",),
            prefix="!crew",
        )

        requests = gateway.poll()
        self.assertEqual("status", requests[0].command)
        self.assertTrue(gateway.reply("Crew ready"))
        self.assertIn(("chat", {"message": "Crew ready"}), self.transport.dispatched)

    def test_shutdown(self):
        self.client.shutdown()
        self.assertIn(("shutdown", {}), self.transport.dispatched)


class EnumSerializationTest(TestCase):
    def test_pathing_command_type_values(self):
        self.assertEqual("request_pause", PathingCommandType.REQUEST_PAUSE.value)
        self.assertEqual("move_to", PathingCommandType.MOVE_TO.value)
