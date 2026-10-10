import asyncio
import time

import numpy as np
import pytest

from agent_phone.sessions import IdempotencyCache, PhoneError
from agent_phone.speech import Transcript, is_probable_hallucination

from .fakes import make_manager, tone


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(16000 * seconds), dtype=np.int16)


async def answered_call(dest="1001", replies=None, **kw):
    s, tel, speech, mgr, store = make_manager(replies=replies, **kw)
    token, sess = store.enable(dest, "demo", "claude", "cli-1", 600)
    snap = await mgr.place_call(sess, "test", 5)
    assert snap["state"] == "answered", snap
    return s, tel, speech, mgr, store, sess, mgr.calls[snap["call_id"]]


# ---- session isolation ---------------------------------------------------------

async def test_sessions_cannot_touch_each_others_calls():
    s, tel, speech, mgr, store, sess_a, call = await answered_call()
    _, sess_b = store.enable("1002", "other", "claude", "cli-2", 600)
    with pytest.raises(PhoneError) as e:
        mgr.get_owned(sess_b, call.id)
    assert e.value.code == "call_not_found"
    assert mgr.get_owned(sess_a, call.id) is call
    await mgr.hangup(call)


async def test_token_auth_expiry_and_disable():
    _, _, _, _, store = make_manager()
    token, sess = store.enable("1001", "demo", "claude", "cli-1", 600)
    assert store.authenticate(token).session_id == sess.session_id
    for bad in (None, "", "pt_wrong"):
        with pytest.raises(PhoneError):
            store.authenticate(bad)
    store.disable(sess.session_id)
    with pytest.raises(PhoneError) as e:
        store.authenticate(token)
    assert e.value.code == "session_disabled"
    token2, sess2 = store.enable("1001", "demo", "claude", "cli-3", 1)
    store._db.execute("UPDATE sessions SET expires_at=? WHERE session_id=?", (time.time() - 1, sess2.session_id))
    with pytest.raises(PhoneError) as e:
        store.authenticate(token2)
    assert e.value.code == "session_expired"
    assert [x.session_id for x in store.expired_unswept()] == [sess2.session_id]


async def test_reenable_same_cli_session_replaces_token():
    _, _, _, _, store = make_manager()
    t1, s1 = store.enable("1001", "demo", "claude", "cli-1", 600)
    t2, s2 = store.enable("1001", "demo", "claude", "cli-1", 600)
    with pytest.raises(PhoneError):
        store.authenticate(t1)
    assert store.find_active_for_cli("claude", "cli-1").session_id == s2.session_id
    assert store.find_active_for_cli("claude", "cli-other") is None


async def test_concurrent_sessions_serialised_per_destination():
    s, tel, speech, mgr, store, sess_a, call = await answered_call()
    _, sess_b = store.enable("1001", "other project", "claude", "cli-2", 600)
    with pytest.raises(PhoneError) as e:
        await mgr.place_call(sess_b, "x", 1)
    assert e.value.code == "destination_in_use" and e.value.retryable
    with pytest.raises(PhoneError) as e:
        await mgr.place_call(sess_a, "x", 1)
    assert e.value.code == "call_already_active"
    await mgr.hangup(call)
    snap = await mgr.place_call(sess_b, "x", 5)  # free again after hangup
    assert snap["state"] == "answered"
    await mgr.hangup(mgr.calls[snap["call_id"]])


# ---- idempotency -----------------------------------------------------------------

async def test_retried_call_with_same_key_dials_once():
    s, tel, speech, mgr, store = make_manager()
    _, sess = store.enable("1001", "demo", "claude", "cli-1", 600)
    idem = IdempotencyCache()
    run = lambda: idem.run(sess.session_id, "phone_call", "k1", {"reason": "r"},  # noqa: E731
                           lambda: mgr.place_call(sess, "r", 5))
    a, b = await asyncio.gather(run(), run())
    c = await run()
    assert len(tel.originated) == 1
    assert a["call_id"] == b["call_id"] == c["call_id"]
    assert [a["idempotent_replay"], b["idempotent_replay"], c["idempotent_replay"]].count(False) == 1
    with pytest.raises(PhoneError) as e:
        await idem.run(sess.session_id, "phone_call", "k1", {"reason": "different"}, lambda: mgr.place_call(sess, "x", 1))
    assert e.value.code == "idempotency_key_reused"
    await mgr.hangup(mgr.calls[a["call_id"]])


async def test_client_timeout_then_retry_does_not_redial():
    s, tel, speech, mgr, store = make_manager(behaviour={"1001": "noanswer"}, ring_timeout_s=2)
    _, sess = store.enable("1001", "demo", "claude", "cli-1", 600)
    idem = IdempotencyCache()
    first = await idem.run(sess.session_id, "phone_call", "k", {}, lambda: mgr.place_call(sess, "r", 0.3), wait_s=0.1)
    assert first.get("in_progress")
    second = await idem.run(sess.session_id, "phone_call", "k", {}, lambda: mgr.place_call(sess, "r", 0.3), wait_s=2)
    assert second["state"] == "ringing" and len(tel.originated) == 1
    call = mgr.calls[second["call_id"]]
    await mgr.wait_for(call, lambda c: c.state == "ended", 8)
    assert call.outcome == "no_answer" and call.snapshot()["retryable"]
    assert len(tel.originated) == 1  # never re-dialled


async def test_speak_retry_does_not_repeat_audio():
    s, tel, speech, mgr, store, sess, call = await answered_call()
    idem = IdempotencyCache()
    run = lambda: idem.run(sess.session_id, "phone_speak", "say1", {"t": "hi"},  # noqa: E731
                           lambda: mgr.speak(call, "Hello there."))
    a, b = await asyncio.gather(run(), run())
    assert speech.synth_calls == ["Hello there."]
    assert a["audio_seconds"] == b["audio_seconds"]
    await mgr.hangup(call)


# ---- outcomes & timeouts ------------------------------------------------------------

@pytest.mark.parametrize("mode,outcome", [("busy", "busy"), ("reject", "rejected"), ("offline", "unavailable")])
async def test_unanswered_outcomes(mode, outcome):
    s, tel, speech, mgr, store = make_manager(behaviour={"1001": mode})
    _, sess = store.enable("1001", "demo", "claude", "cli-1", 600)
    snap = await mgr.place_call(sess, "r", 5)
    assert snap["state"] == "ended" and snap["outcome"] == outcome
    assert "1001" not in mgr.by_destination
    if mode == "offline":
        assert "not registered" in (snap.get("hangup_cause") or {}).get("text", "")


async def test_listen_timeout_is_bounded_and_not_consent():
    s, tel, speech, mgr, store, sess, call = await answered_call()
    t0 = time.monotonic()
    res = await mgr.listen(call, 999)  # clamped to max_tool_wait_s (8 s in tests)
    assert res["status"] == "timeout" and res["transcript"] is None
    assert "not consent" in res["note"]
    assert time.monotonic() - t0 < s.max_tool_wait_s + 1
    await mgr.hangup(call)


async def test_disallowed_destination():
    s, tel, speech, mgr, store = make_manager()
    _, sess = store.enable("9999", "demo", "claude", "cli-1", 600)
    with pytest.raises(PhoneError) as e:
        await mgr.place_call(sess, "r", 1)
    assert e.value.code == "destination_not_allowed" and tel.originated == []


# ---- audio path, conversation, interruption ----------------------------------------------

async def test_speak_sends_paced_rtp_and_listen_returns_transcript():
    s, tel, speech, mgr, store, sess, call = await answered_call(replies=["Yes, and include screenshots."])
    t0 = time.monotonic()
    res = await mgr.speak(call, "The feature is done. Open a draft PR?")  # 2 sentences ~0.76 s audio
    elapsed = time.monotonic() - t0
    assert res["completed"] and not res["interrupted"] and res["sentences_total"] == 2
    assert elapsed >= res["audio_seconds"] * 0.9  # real-time pacing, not a burst
    await asyncio.sleep(0.1)
    assert tel.received_frames >= int(res["audio_seconds"] / 0.02) - 2
    listen = asyncio.create_task(mgr.listen(call, 5))
    await tel.send_audio(np.concatenate([tone(0.6), silence(0.6)]))
    out = await listen
    assert out["status"] == "speech" and out["transcript"] == "Yes, and include screenshots."
    assert out["utterances"][0]["stt"]["no_speech_prob"] == 0.01
    await mgr.hangup(call)


async def test_multiple_turns_in_one_call():
    s, tel, speech, mgr, store, sess, call = await answered_call(replies=["first answer", "second answer"])
    for expected in ("first answer", "second answer"):
        task = asyncio.create_task(mgr.speak_and_listen(call, "Question?", 5))
        await asyncio.sleep(0.4)  # let the prompt finish playing
        await tel.send_audio(np.concatenate([tone(0.5), silence(0.5)]))
        res = await task
        assert res["transcript"] == expected and res["status"] == "speech"
    assert call.active
    await mgr.hangup(call)


async def test_barge_in_stops_playback_and_reports_interruption():
    s, tel, speech, mgr, store, sess, call = await answered_call(replies=["stop, wait"])
    long_text = "This is a long status update. " * 8  # ~5 s of audio
    task = asyncio.create_task(mgr.speak_and_listen(call, long_text, 5))
    await asyncio.sleep(0.5)
    assert call.rtp.playing
    await tel.send_audio(np.concatenate([tone(0.6), silence(0.6)]))
    res = await task
    assert res["interrupted"] is True
    assert res["speak"]["interrupted"] and not res["speak"]["completed"]
    assert res["transcript"] == "stop, wait"
    assert res["listen"]["utterances"][0]["barge_in"] is True
    assert not call.rtp.playing and call.stats["barge_ins"] == 1
    await mgr.hangup(call)


async def test_short_noise_during_playback_is_not_speech():
    s, tel, speech, mgr, store, sess, call = await answered_call(replies=["should not appear"])
    task = asyncio.create_task(mgr.speak(call, "A sentence that plays for a little while. " * 3))
    await asyncio.sleep(0.3)
    await tel.send_audio(np.concatenate([tone(0.1), silence(0.5)]))  # 100 ms blip < barge-in threshold
    res = await task
    assert res["completed"] and not res["interrupted"]
    heard = await mgr.listen(call, 0.6)
    assert heard["status"] == "timeout" and speech.transcribe_calls == 0
    await mgr.hangup(call)


async def test_listen_cursor_makes_retries_lossless():
    s, tel, speech, mgr, store, sess, call = await answered_call(replies=["hello"])
    await tel.send_audio(np.concatenate([tone(0.5), silence(0.5)]))
    first = await mgr.listen(call, 3, after_seq=0)
    retry = await mgr.listen(call, 0.5, after_seq=0)  # response lost; retry with same cursor
    assert first["transcript"] == retry["transcript"] == "hello"
    assert (await mgr.listen(call, 0.5, after_seq=first["cursor"]))["status"] == "timeout"
    await mgr.hangup(call)


# ---- cleanup ------------------------------------------------------------------------------

async def test_remote_hangup_cleans_up_everything():
    s, tel, speech, mgr, store, sess, call = await answered_call()
    port = call.port
    listen = asyncio.create_task(mgr.listen(call, 5))
    await asyncio.sleep(0.1)
    tel.remote_hangup(call.id)
    res = await asyncio.wait_for(listen, 3)
    assert res["status"] == "call_ended" and res["outcome"] == "disconnected"
    await asyncio.sleep(0.05)
    assert call.state == "ended" and call.port is None and port in mgr.ports._free
    assert "1001" not in mgr.by_destination and call.bridge_id in tel.torn_down
    assert all(t.done() for t in call.tasks)
    with pytest.raises(PhoneError) as e:
        await mgr.speak(call, "hello?")
    assert e.value.code == "call_ended"


async def test_idle_timeout_hangs_up():
    s, tel, speech, mgr, store, sess, call = await answered_call(idle_timeout_s=1)
    await mgr.wait_for(call, lambda c: c.state == "ended", 5)
    assert call.outcome == "idle_timeout" and call.id in tel.hungup


async def test_session_disable_ends_calls():
    s, tel, speech, mgr, store, sess, call = await answered_call()
    ended = await mgr.end_session_calls(sess.session_id)
    assert ended == [call.id] and call.outcome == "session_ended"


# ---- transcript filtering -----------------------------------------------------------------

def test_hallucination_filter():
    t = lambda text, nsp=0.01, lp=-0.2: Transcript(text=text, stt_seconds=0.1, no_speech_prob=nsp, avg_logprob=lp)  # noqa: E731
    assert is_probable_hallucination(t(""), 800) == "empty_or_non_speech"
    assert is_probable_hallucination(t("[BLANK_AUDIO]"), 800) == "empty_or_non_speech"
    assert is_probable_hallucination(t("Thank you.", nsp=0.7, lp=-1.2), 400) is not None
    assert is_probable_hallucination(t("Thank you."), 1500) is None  # clearly spoken: keep
    assert is_probable_hallucination(t("Yes, open the PR."), 900) is None


async def test_utterance_ends_when_caller_stops_sending_packets():
    """Phones with DTX/silence suppression send nothing after speech; endpointing must still fire."""
    s, tel, speech, mgr, store, sess, call = await answered_call(replies=["dtx reply"])
    listen = asyncio.create_task(mgr.listen(call, 5))
    await tel.send_audio(tone(0.6))  # speech, then no packets at all
    out = await asyncio.wait_for(listen, 4)
    assert out["status"] == "speech" and out["transcript"] == "dtx reply"
    assert call.rtp.filled_frames > 0
    await mgr.hangup(call)


async def test_speech_waits_for_answer_settle_time():
    s, tel, speech, mgr, store, sess, call = await answered_call(answer_settle_ms=400, answer_greeting_max_ms=2000)
    t0 = time.monotonic()
    await mgr.speak(call, "Hi.")
    assert time.monotonic() - call.answered_mono >= 0.4
    await mgr.hangup(call)


async def test_hello_during_prompt_is_not_taken_as_the_answer():
    """A "hello?" said on pickup (before/while the question plays) is reported, and the real reply awaited."""
    s, tel, speech, mgr, store, sess, call = await answered_call(replies=["hello", "yes I can"],
                                                                 answer_settle_ms=900, answer_greeting_max_ms=900)
    task = asyncio.create_task(mgr.speak_and_listen(call, "Can you hear me clearly? " * 3, 6))
    await tel.send_audio(np.concatenate([tone(0.4), silence(0.4)]))   # "hello" during the pickup pause
    while not call.rtp.playing:
        await asyncio.sleep(0.02)
    while call.rtp.playing:
        await asyncio.sleep(0.05)
    await tel.send_audio(np.concatenate([tone(0.6), silence(0.5)]))   # the actual answer
    res = await task
    assert res["transcript"] == "yes I can"
    assert [h["text"] for h in res["heard_during_prompt"]] == ["hello"]
    await mgr.hangup(call)


async def test_first_prompt_waits_for_callee_hello_to_finish():
    s, tel, speech, mgr, store, sess, call = await answered_call(replies=["hello?", "yes"], answer_settle_ms=200,
                                                                 answer_greeting_max_ms=3000)
    hello = asyncio.create_task(tel.send_audio(np.concatenate([tone(0.9), silence(0.5)])))  # long "hello?"
    task = asyncio.create_task(mgr.speak_and_listen(call, "Can you hear me?", 5))
    while not call.rtp.playing or not call.speaking:
        await asyncio.sleep(0.02)
    started_speaking = time.monotonic() - call.answered_mono
    assert started_speaking > 0.9  # waited for the hello to end instead of talking over it
    await hello
    while call.rtp.playing:
        await asyncio.sleep(0.05)
    await tel.send_audio(np.concatenate([tone(0.5), silence(0.5)]))
    res = await task
    assert res["transcript"] == "yes" and res["interrupted"] is False
    assert call.stats["barge_ins"] == 0
    await mgr.hangup(call)
