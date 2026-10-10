"""The user phones the agent: routing, instruction capture, hand-off to the session, timeouts."""
import asyncio
import time

from agent_phone.channel import ChannelHub
from agent_phone.inbound import InboundDesk, caller_extension, pick_session

from .fakes import make_manager, tone


async def until(pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def desk_setup(replies=None, **kw):
    s, tel, speech, mgr, store = make_manager(replies=replies, inbound_listen_s=3, **kw)
    hub = ChannelHub()
    desk = InboundDesk(s, store, mgr, hub)
    mgr._on_inbound = desk.handle
    return s, tel, speech, mgr, store, hub


def inbound_call(mgr):
    return next(c for c in mgr.calls.values() if c.direction == "inbound")


async def say_after_prompt(tel, speech, mgr, prompt_fragment, seconds=0.5):
    """Wait until the agent has finished saying the prompt, then 'speak' into the call."""
    await until(lambda: any(prompt_fragment in t for t in speech.synth_calls))
    await until(lambda: not inbound_call(mgr).speaking)
    await tel.send_audio(tone(seconds))


def test_caller_extension_uses_sip_endpoint():
    assert caller_extension({"name": "PJSIP/1001-0000002a"}) == "1001"
    assert caller_extension({"name": "Local/700@internal-0001;1"}) is None
    assert caller_extension({}) is None


def test_pick_session_by_label_or_ordinal():
    _, _, _, _, store = make_manager()
    _, a = store.enable("1001", "agent phone", "claude", "a", 600)
    _, b = store.enable("1001", "stash", "claude", "b", 600)
    assert pick_session("the agent phone one", [a, b]) is a
    assert pick_session("Stash.", [a, b]) is b
    assert pick_session("the second one", [a, b]) is b
    assert pick_session("banana", [a, b]) is None


async def test_no_listening_session_explains_and_hangs_up():
    s, tel, speech, mgr, store, hub = desk_setup()
    store.enable("1001", "demo", "claude", "cli-1", 600)  # phone mode on, but no channel listener
    tel.ring_in("in-1")
    await until(lambda: "in-1" in tel.hungup)
    assert tel.answered == ["in-1"]
    assert any("No coding session is listening" in t for t in speech.synth_calls)


async def test_disallowed_caller_is_refused_without_answering():
    s, tel, speech, mgr, store, hub = desk_setup()
    tel.ring_in("in-2", endpoint="1003")
    await until(lambda: "in-2" in tel.hungup)
    assert tel.answered == [] and speech.synth_calls == []


async def test_instruction_is_pushed_and_call_handed_to_session():
    s, tel, speech, mgr, store, hub = desk_setup(replies=["Please run the unit tests."], inbound_claim_s=5)
    _, sess = store.enable("1001", "demo", "claude", "cli-1", 600)
    q = hub.subscribe("cli-1")
    tel.ring_in("in-3")
    await say_after_prompt(tel, speech, mgr, "What would you like me to do?")
    ev = await asyncio.wait_for(q.get(), 8)
    call = inbound_call(mgr)
    assert "Please run the unit tests." in ev["content"]
    assert ev["meta"]["call_id"] == call.id and ev["meta"]["caller"] == "1001"
    assert call.session_id == sess.session_id and call.active
    # The agent picks the call up with its own session and keeps talking on it.
    assert mgr.get_owned(sess, call.id) is call
    await asyncio.sleep(0.3)
    assert call.active, "a claimed call must stay open for the agent"
    r = await mgr.speak(call, "Running them now.")
    assert r["ok"]
    await mgr.hangup(call)


async def test_unclaimed_handoff_tells_caller_and_ends():
    s, tel, speech, mgr, store, hub = desk_setup(replies=["deploy it"], inbound_claim_s=1)
    store.enable("1001", "demo", "claude", "cli-1", 600)
    q = hub.subscribe("cli-1")
    tel.ring_in("in-4")
    await say_after_prompt(tel, speech, mgr, "What would you like me to do?")
    await asyncio.wait_for(q.get(), 8)
    call = inbound_call(mgr)
    await until(lambda: not call.active)
    assert any("still busy" in t for t in speech.synth_calls)


async def test_two_sessions_caller_chooses_project():
    s, tel, speech, mgr, store, hub = desk_setup(replies=["stash", "add a test"], inbound_claim_s=1)
    store.enable("1001", "agent phone", "claude", "cli-a", 600)
    _, b = store.enable("1001", "stash", "claude", "cli-b", 600)
    qa, qb = hub.subscribe("cli-a"), hub.subscribe("cli-b")
    tel.ring_in("in-5")
    await say_after_prompt(tel, speech, mgr, "Which project?")
    await say_after_prompt(tel, speech, mgr, "What would you like me to do?")
    ev = await asyncio.wait_for(qb.get(), 8)
    assert "add a test" in ev["content"] and qa.empty()
    assert inbound_call(mgr).session_id == b.session_id


async def test_caller_already_on_agent_call_gets_busy():
    s, tel, speech, mgr, store, hub = desk_setup()
    _, sess = store.enable("1001", "demo", "claude", "cli-1", 600)
    snap = await mgr.place_call(sess, "outbound", 5)
    tel.ring_in("in-6")
    await until(lambda: "in-6" in tel.hungup)
    assert "in-6" not in tel.answered
    await mgr.hangup(mgr.calls[snap["call_id"]])


def test_channel_bridge_compiles_on_system_python():
    """The bridge runs on macOS's /usr/bin/python3 (3.9) with no dependencies; a syntax error kills phone-in."""
    import pathlib
    import shutil
    import subprocess
    bridge = pathlib.Path(__file__).resolve().parents[2] / "integrations/claude/bin/agent-phone-channel"
    compile(bridge.read_text(), str(bridge), "exec")
    if shutil.which("/usr/bin/python3"):
        subprocess.run(["/usr/bin/python3", "-c", f"import ast; ast.parse(open({str(bridge)!r}).read())"], check=True)
