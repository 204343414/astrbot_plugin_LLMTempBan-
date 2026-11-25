"""`stay_silent`（已读不回）—— 这个功能最容易做错的三件事，都钉在这里。

背景：群友让 bot「带 / 就已读不回」，bot 回了一句「我这边没有已读不回的开
关」。查下来两件事同时成立 —— 插件确实没有这个工具，但 AstrBot 支持做到，
所以那是推脱。这个工具就是补上它。

三个容易做错的地方：

1. **不能在工具里直接 `set_result(None)`**。工具调用只是 LLM 的一个中间步骤，
   之后它自己还会再产出一段话（真机上就是那句「抱歉我这边没有…」）。
   必须在装饰阶段统一压掉。

2. **不能用 `stop_event()`**。那会把整条事件掐断，连带这一轮**不写进历史**，
   模型下次完全不记得自己沉默过。

3. **不能把「不回」做成「以后都不理」**。那是 ban_sender 的事。
"""
import ast
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(cls_name, method_name):
    """从 main.py 里抠出单个方法，去掉装饰器后单独执行。

    main.py 顶部 import 了 astrbot，测试环境里没有；只测方法体就不必把
    整个插件跑起来。
    """
    tree = ast.parse((ROOT / "main.py").read_text("utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls_name)
    # async def 在 AST 里是 AsyncFunctionDef —— 只查 FunctionDef 会一个都找不到
    fn = next(
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method_name
    )
    fn.decorator_list = []
    return fn


class Event:
    """够这两个方法用的最小事件。"""

    def __init__(self):
        self.extras = {}
        self.result = "<模型本来要发的回复>"
        self.stopped = False

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extras
        return self.extras.get(key, default)

    def set_result(self, result):
        self.result = result

    def stop_event(self):
        self.stopped = True


def test_the_tool_only_marks_and_never_touches_the_result():
    """工具本身只标记 —— 绝不碰结果，也绝不 stop_event。

    如果它在这里就把结果清了，模型随后那段「补充说明」还是会照发
    （工具返回之后 LLM 还有一轮），等于白拦。
    """
    fn = _load("LLMTempBanPlugin", "stay_silent")
    namespace = {
        "logger": __import__("logging").getLogger("t"),
        # 注解里用到了 AstrMessageEvent，抠出来的方法体单独执行时需要它在场
        "AstrMessageEvent": object,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 "main.py", "exec"), namespace)
    method = namespace["stay_silent"]

    class Self:
        def _origin(self, event):
            return "umo:group:G1"

    event = Event()
    asyncio.run(method(Self(), event, reason="只是感叹一句"))

    assert event.extras.get("__llmtempban_silent__") == "只是感叹一句"
    assert event.result == "<模型本来要发的回复>", "工具里不该动结果"
    assert event.stopped is False, "更不该 stop_event"


def test_the_decorator_hook_actually_clears_the_result():
    """装饰钩子才是真正「不发」的地方。"""
    fn = _load("LLMTempBanPlugin", "suppress_silent_reply")
    namespace = {
        "logger": __import__("logging").getLogger("t"),
        # 注解里用到了 AstrMessageEvent，抠出来的方法体单独执行时需要它在场
        "AstrMessageEvent": object,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 "main.py", "exec"), namespace)
    hook = namespace["suppress_silent_reply"]

    event = Event()
    event.set_extra("__llmtempban_silent__", "不想接话")
    asyncio.run(hook(object(), event))

    assert event.result is None, "结果被清空 = 发送阶段无内容可发"
    assert event.stopped is False, "必须是 set_result(None)，不是 stop_event"


def test_the_hook_does_nothing_when_the_model_did_not_stay_silent():
    """没标记就放行 —— 绝不能顺手把所有回复都清掉。"""
    fn = _load("LLMTempBanPlugin", "suppress_silent_reply")
    namespace = {
        "logger": __import__("logging").getLogger("t"),
        # 注解里用到了 AstrMessageEvent，抠出来的方法体单独执行时需要它在场
        "AstrMessageEvent": object,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 "main.py", "exec"), namespace)
    hook = namespace["suppress_silent_reply"]

    event = Event()
    asyncio.run(hook(object(), event))
    assert event.result == "<模型本来要发的回复>"


def test_the_tool_is_registered_with_a_description_that_forbids_dodging_questions():
    """工具描述必须明确禁止「用它回避该回答的事」。

    不写这条的后果很实在：模型会把「不想答」当成「该沉默」，
    于是用户提问也得不到回答，而界面上看起来只是"它没反应"。
    """
    tree = ast.parse((ROOT / "main.py").read_text("utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "LLMTempBanPlugin")
    fn = next(n for n in cls.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "stay_silent")
    doc = ast.get_docstring(fn) or ""
    assert "不要" in doc and ("提问" in doc or "回答" in doc), doc
    decorators = [ast.unparse(d) for d in fn.decorator_list]
    assert any("stay_silent" in d for d in decorators), decorators
