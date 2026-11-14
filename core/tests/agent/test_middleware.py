"""Guard and budget middleware for the LangChain agent (plan 1.9, 1.11, 1.12; brief A5).

Every test drives a real ``create_agent`` loop with a ``FakeModel`` script. A tool
body is counted through its ``ToolSpec.run``, so the counts are body executions,
not LangChain invocations.

  * a denied call never reaches the tool, and the model receives the denial text;
  * an allowed write is scanned over exactly its ``touched`` paths, and a fatal
    finding is appended to the tool message;
  * an ``ask`` verdict interrupts before the handler runs: after proceed with
    ``allow`` the body ran once in total; with ``refuse`` it ran zero times;
  * a non-idempotent tool called again with the same idempotency key returns
    the journaled result without running its body;
  * ``BudgetMiddleware`` ends the run at the cost bound and at the wall-clock bound.

Standard library, LangChain and LangGraph. Runs with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import dataclasses
import tempfile
import types
import unittest

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from core.agent.middleware import BudgetMiddleware, GuardMiddleware, approval_granted
from core.agent.models import FakeModel, ToolCall
from core.kernel.budget import UnboundedBudget
from core.kernel.guard import POLICY_SENTENCE
from core.kernel.types import Action, ActionKind, Budget
from core.tools import fs
from core.tools.registry import ToolRefused, Toolbox
from core.tools.spec import ToolContext, ToolResult, ToolSpec

SECRET = "FAKE-SECRET-MARKER"
POLICY = {"id": "fake-secret", "regex": SECRET, "severity": "fatal",
          "scope": ["any"], "summary": "fake secret marker"}


class _Journal:
    def __init__(self):
        self.store = {}

    def seen(self, key):
        return self.store.get(key)

    def record(self, key, result):
        self.store[key] = result


def make_cfg(root, ask=(), protected=(), deny=(POLICY,)):
    return types.SimpleNamespace(
        root=str(root), deny=list(deny), ask=list(ask),
        protected_paths=list(protected), blind=[])


def custom_spec(name, kind, run, *, idempotent=True, required=(), needs_approval=False):
    effects = frozenset({"read"} if kind is ActionKind.READ else {"write"})
    return ToolSpec(
        name=name, description=f"test tool {name}",
        args_schema={"type": "object",
                     "properties": {"path": {"type": "string"}},
                     "required": list(required), "additionalProperties": False},
        effects=effects,
        to_action=lambda args: Action(kind=kind, tool=name, path=args.get("path")),
        run=run, idempotent=idempotent, needs_approval=needs_approval)


def counted(spec):
    """A copy of ``spec`` whose body records each execution in the returned list."""
    runs = []
    original = spec.run

    def run(args, ctx):
        runs.append(dict(args))
        return original(args, ctx)

    return dataclasses.replace(spec, run=run), runs


def lc_tool(spec, toolbox):
    """The LangChain tool the agent sees. Its body goes through the Toolbox and returns
    (content, touched) as content_and_artifact, which is the contract GuardMiddleware reads.
    ``approved`` is what the middleware granted for this call (never True by default)."""
    def body(runtime: ToolRuntime, **kwargs):
        thread = runtime.config.get("configurable", {}).get("thread_id", "")
        result = toolbox.call(spec.name, kwargs, thread_id=thread,
                              tool_call_id=runtime.tool_call_id,
                              approved=approval_granted())
        return result.content, list(result.touched)

    return StructuredTool.from_function(
        func=body, name=spec.name, description=spec.description,
        args_schema=spec.args_schema, response_format="content_and_artifact")


def make_toolbox(root, specs):
    unit = root / "units" / "a"
    scratch = root / "scratch"
    unit.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(exist_ok=True)
    ctx = ToolContext(root=root, unit_dir=unit, scratch_dir=scratch,
                      writable=("units/a/**", "scratch/**"), blind=(),
                      budget=Budget(), journal=_Journal())
    return Toolbox(specs, ctx)


def make_agent(model, toolbox, middleware):
    tools = [lc_tool(toolbox.get(name), toolbox) for name in toolbox.names()]
    return create_agent(model.chat_model(), tools, middleware=list(middleware),
                        checkpointer=InMemorySaver())


def run(agent, thread="t1"):
    return agent.invoke({"messages": [{"role": "user", "content": "go"}]},
                        {"configurable": {"thread_id": thread}})


def tool_messages(messages):
    return [m for m in messages if isinstance(m, ToolMessage)]


def seen_by_model(model, call_index):
    return [m for m in model.calls[call_index] if isinstance(m, ToolMessage)]


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve()
        (self.root / "units" / "a").mkdir(parents=True)
        (self.root / "scratch").mkdir()
        self.fs = {s.name: s for s in fs.specs()}

    def test_denied_call_never_reaches_tool_and_model_gets_denial(self):
        spec, runs = counted(self.fs["write_file"])
        cfg = make_cfg(self.root, protected=("units/a/protected/**",))
        toolbox = make_toolbox(self.root, [spec])
        model = FakeModel([
            [ToolCall(name="write_file", args={"path": "units/a/protected/x.md",
                                               "content": "hi"}, id="c1")],
            "stopped",
        ])
        agent = make_agent(model, toolbox, [GuardMiddleware(toolbox, cfg)])
        out = run(agent)
        self.assertEqual(len(runs), 0, "the tool body must not run for a denied call")
        self.assertFalse((self.root / "units/a/protected/x.md").exists())
        denial = [m for m in seen_by_model(model, 1) if m.tool_call_id == "c1"]
        self.assertEqual(len(denial), 1)
        self.assertIn("Rule: protected-path", denial[0].content)
        self.assertIn(POLICY_SENTENCE, denial[0].content)
        self.assertEqual(out["messages"][-1].content, "stopped")

    def test_fatal_content_is_denied_before_the_write(self):
        spec, runs = counted(self.fs["write_file"])
        cfg = make_cfg(self.root)
        toolbox = make_toolbox(self.root, [spec])
        model = FakeModel([
            [ToolCall(name="write_file", args={"path": "units/a/s.md",
                                               "content": f"x {SECRET}"}, id="c1")],
            "stopped",
        ])
        run(make_agent(model, toolbox, [GuardMiddleware(toolbox, cfg)]))
        self.assertEqual(len(runs), 0, "the tool body must not run for denied content")
        self.assertFalse((self.root / "units/a/s.md").exists())
        self.assertIn("Rule: fake-secret", seen_by_model(model, 1)[0].content)

    def test_allowed_write_scans_exactly_touched(self):
        def quiet(args, ctx):
            # Writes a leak it does not report as touched: must not be scanned.
            (ctx.root / "units/a/leak.md").write_text(SECRET, encoding="utf-8")
            (ctx.root / "units/a/clean.md").write_text("fine", encoding="utf-8")
            return ToolResult(content="wrote clean", touched=("units/a/clean.md",))

        def loud(args, ctx):
            # Writes and reports the same leak: must be scanned and reported.
            (ctx.root / "units/a/leak.md").write_text(SECRET, encoding="utf-8")
            return ToolResult(content="wrote leak", touched=("units/a/leak.md",))

        quiet_spec, quiet_runs = counted(custom_spec("quiet", ActionKind.WRITE, quiet,
                                                     required=("path",)))
        loud_spec, loud_runs = counted(custom_spec("loud", ActionKind.WRITE, loud,
                                                   required=("path",)))
        cfg = make_cfg(self.root)
        toolbox = make_toolbox(self.root, [quiet_spec, loud_spec])
        model = FakeModel([
            [ToolCall(name="quiet", args={"path": "units/a/clean.md"}, id="q1")],
            [ToolCall(name="loud", args={"path": "units/a/leak.md"}, id="l1")],
            "done",
        ])
        out = run(make_agent(model, toolbox, [GuardMiddleware(toolbox, cfg)]))
        self.assertEqual(len(quiet_runs), 1)
        self.assertEqual(len(loud_runs), 1)
        by_id = {m.tool_call_id: m.content for m in tool_messages(out["messages"])}
        self.assertEqual(by_id["q1"], "wrote clean", "untouched leak must not be reported")
        self.assertIn("wrote leak", by_id["l1"])
        self.assertIn("[fake-secret]", by_id["l1"])
        self.assertIn("units/a/leak.md", by_id["l1"])

    def test_ask_interrupts_before_handler_and_allow_runs_body_once(self):
        spec, runs = counted(self.fs["write_file"])
        cfg = make_cfg(self.root, ask=[{"paths": ["units/a/ask/**"],
                                        "reason": "asked path"}])
        toolbox = make_toolbox(self.root, [spec])
        model = FakeModel([
            [ToolCall(name="write_file", args={"path": "units/a/ask/x.md",
                                               "content": "hi"}, id="c1")],
            "done",
        ])
        agent = make_agent(model, toolbox, [GuardMiddleware(toolbox, cfg)])
        first = run(agent)
        self.assertIn("__interrupt__", first)
        self.assertEqual(len(runs), 0, "nothing may run before the human answers")
        payload = first["__interrupt__"][0].value
        self.assertEqual(payload["tool"], "write_file")
        self.assertEqual(payload["reason"], "asked path")
        final = agent.invoke(Command(resume="allow"),
                             {"configurable": {"thread_id": "t1"}})
        self.assertEqual(len(runs), 1, "after allow the tool body ran exactly once")
        self.assertTrue((self.root / "units/a/ask/x.md").exists())
        self.assertEqual(final["messages"][-1].content, "done")

    def test_ask_refused_runs_body_zero_times(self):
        spec, runs = counted(self.fs["write_file"])
        cfg = make_cfg(self.root, ask=[{"paths": ["units/a/ask/**"], "reason": "asked"}])
        toolbox = make_toolbox(self.root, [spec])
        model = FakeModel([
            [ToolCall(name="write_file", args={"path": "units/a/ask/x.md",
                                               "content": "hi"}, id="c1")],
            "done",
        ])
        agent = make_agent(model, toolbox, [GuardMiddleware(toolbox, cfg)])
        self.assertIn("__interrupt__", run(agent))
        agent.invoke(Command(resume="refuse"), {"configurable": {"thread_id": "t1"}})
        self.assertEqual(len(runs), 0, "after refuse the tool body must not run")
        self.assertFalse((self.root / "units/a/ask/x.md").exists())
        self.assertIn("Rule: ask-path", seen_by_model(model, 1)[0].content)

    def test_needs_approval_tool_interrupts_even_when_guard_allows(self):
        spec, runs = counted(dataclasses.replace(self.fs["write_file"], needs_approval=True))
        cfg = make_cfg(self.root)  # the guard allows this write outright
        toolbox = make_toolbox(self.root, [spec])
        model = FakeModel([
            [ToolCall(name="write_file", args={"path": "units/a/gated.md",
                                               "content": "hi"}, id="c1")],
            "done",
        ])
        agent = make_agent(model, toolbox, [GuardMiddleware(toolbox, cfg)])
        first = run(agent)
        self.assertIn("__interrupt__", first)
        self.assertEqual(len(runs), 0, "nothing may run before the human answers")
        self.assertEqual(first["__interrupt__"][0].value["tool"], "write_file")
        final = agent.invoke(Command(resume="allow"),
                             {"configurable": {"thread_id": "t1"}})
        self.assertEqual(len(runs), 1, "after allow the tool body ran exactly once")
        self.assertTrue((self.root / "units/a/gated.md").exists())
        self.assertEqual(final["messages"][-1].content, "done")

    def test_needs_approval_tool_refused_runs_body_zero_times(self):
        spec, runs = counted(dataclasses.replace(self.fs["write_file"], needs_approval=True))
        cfg = make_cfg(self.root)
        toolbox = make_toolbox(self.root, [spec])
        model = FakeModel([
            [ToolCall(name="write_file", args={"path": "units/a/gated.md",
                                               "content": "hi"}, id="c1")],
            "done",
        ])
        agent = make_agent(model, toolbox, [GuardMiddleware(toolbox, cfg)])
        self.assertIn("__interrupt__", run(agent))
        agent.invoke(Command(resume="refuse"), {"configurable": {"thread_id": "t1"}})
        self.assertEqual(len(runs), 0, "after refuse the tool body must not run")
        self.assertFalse((self.root / "units/a/gated.md").exists())
        self.assertIn("Rule: ask-path", seen_by_model(model, 1)[0].content)

    def test_toolbox_refuses_needs_approval_tool_called_without_the_middleware(self):
        spec, runs = counted(custom_spec("gated", ActionKind.WRITE, lambda a, c: None,
                                         required=("path",), needs_approval=True))
        toolbox = make_toolbox(self.root, [spec])
        with self.assertRaises(ToolRefused):
            toolbox.call("gated", {"path": "units/a/x.md"}, thread_id="t",
                         tool_call_id="c", approved=approval_granted())
        self.assertEqual(len(runs), 0)

    def test_same_idempotency_key_returns_journaled_result(self):
        (self.root / "units/a/notes.md").write_text("alpha beta\n", encoding="utf-8")
        spec, runs = counted(self.fs["edit_file"])
        cfg = make_cfg(self.root)
        toolbox = make_toolbox(self.root, [spec])
        model = FakeModel([
            [ToolCall(name="edit_file", args={"path": "units/a/notes.md",
                                              "old": "alpha", "new": "omega"}, id="same")],
            # Same tool_call id, different arguments: a body run would now fail.
            [ToolCall(name="edit_file", args={"path": "units/a/notes.md",
                                              "old": "alpha", "new": "zeta"}, id="same")],
            "done",
        ])
        out = run(make_agent(model, toolbox, [GuardMiddleware(toolbox, cfg)]))
        self.assertEqual(len(runs), 1)
        contents = [m.content for m in tool_messages(out["messages"])]
        self.assertEqual(contents, ["edited units/a/notes.md (1 replacement(s))"] * 2)
        self.assertEqual((self.root / "units/a/notes.md").read_text(encoding="utf-8"),
                         "omega beta\n")


class BudgetTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve()

    def test_unbounded_budget_is_refused(self):
        with self.assertRaises(UnboundedBudget):
            BudgetMiddleware(Budget())
        BudgetMiddleware(Budget(allow_unbounded=True))  # explicit opt-in is accepted

    def test_cost_bound_ends_the_run(self):
        def tick(args, ctx):
            return ToolResult(content="ticked")

        spec, runs = counted(custom_spec("tick", ActionKind.READ, tick))
        toolbox = make_toolbox(self.root, [spec])
        model = FakeModel([
            [ToolCall(name="tick", args={}, id="t1")],
            [ToolCall(name="tick", args={}, id="t2")],
            [ToolCall(name="tick", args={}, id="t3")],
            "never reached",
        ])
        # Each model call costs 0.5 USD: the third model call would start at 1.0.
        budget = BudgetMiddleware(Budget(max_usd=1.0),
                                  cost=lambda: 0.5 * len(model.calls))
        run(make_agent(model, toolbox, [budget]))
        self.assertEqual(len(model.calls), 2, "the run must end before the third model call")
        self.assertEqual(len(runs), 2)
        self.assertIn("cost", budget.stop_reason)

    def test_wall_clock_bound_ends_the_run(self):
        now = [0.0]

        def tick(args, ctx):
            now[0] += 10.0
            return ToolResult(content="ticked")

        spec, runs = counted(custom_spec("tick", ActionKind.READ, tick))
        toolbox = make_toolbox(self.root, [spec])
        model = FakeModel([
            [ToolCall(name="tick", args={}, id="t1")],
            [ToolCall(name="tick", args={}, id="t2")],
            [ToolCall(name="tick", args={}, id="t3")],
            "never reached",
        ])
        budget = BudgetMiddleware(Budget(max_wall_s=15.0), clock=lambda: now[0])
        run(make_agent(model, toolbox, [budget]))
        self.assertEqual(len(model.calls), 2, "elapsed 20 s reaches the 15 s bound")
        self.assertEqual(len(runs), 2)
        self.assertIn("wall-clock", budget.stop_reason)


if __name__ == "__main__":
    unittest.main()
