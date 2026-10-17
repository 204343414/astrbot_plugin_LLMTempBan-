import ast
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


class At:
    def __init__(self, qq):
        self.qq = qq


def _load_targets_bot():
    tree = ast.parse((ROOT / "main.py").read_text("utf-8"))
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_targets_bot"
    )
    function.decorator_list = []
    namespace = {"At": At, "AstrMessageEvent": object}
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "main.py", "exec"),
        namespace,
    )
    return namespace["_targets_bot"]


class Event:
    def __init__(self, origin, components=(), self_id="bot", wake=False):
        self.unified_msg_origin = origin
        self._components = list(components)
        self._self_id = self_id
        self.is_at_or_wake_command = wake
        self.message_obj = SimpleNamespace(message=self._components, self_id=self_id)

    def get_messages(self):
        return self._components

    def get_self_id(self):
        return self._self_id


def test_private_messages_are_always_counted():
    targets_bot = _load_targets_bot()
    assert targets_bot(Event("default:FriendMessage:1")) is True
    assert targets_bot(Event("default:PrivateMessage:1")) is True


def test_group_messages_require_an_at_to_this_bot():
    targets_bot = _load_targets_bot()
    assert targets_bot(Event("default:GroupMessage:1", [SimpleNamespace()])) is False
    assert targets_bot(Event("default:GroupMessage:1", [At("another")])) is False
    assert targets_bot(Event("default:GroupMessage:1", [At("bot")])) is True


def test_group_wake_flag_is_used_when_self_id_is_unavailable():
    targets_bot = _load_targets_bot()
    assert targets_bot(Event("default:GroupMessage:1", [At("unknown")], self_id="", wake=True)) is True
