"""Scripted SIP test bot (extension 1099) for automated acceptance tests. Not for humans.

Stays registered; behaviour for the NEXT incoming call is set over a tiny local HTTP API:
  POST /mode  {"mode": "answer|busy|reject|noanswer|hangup|offline", "wav": "reply-yes.wav", "hangup_after": 5}
  POST /dial  {"dest": "700", "wav": "instr-create-file.wav", "hangup_after": 90}   (place a call now)
  GET  /state -> {"mode":..., "calls": n, "last_recording": "/work/out/heard.wav", "registered": true}
A fresh player and recorder are created per call, so reply timing is relative to answer and the
recording is finalised when the call ends.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pjsua2 as pj

WORK = os.environ.get("BOT_DIR", "/work")
CTRL_PORT = int(os.environ.get("BOT_CONTROL_PORT", "8099"))
STATE = {"want_registered": True, "mode": "answer", "wav": "reply-yes.wav", "hangup_after": 5, "calls": 0,
         "last_recording": None, "registered": False, "last_event": None}
LOCK = threading.Lock()
PENDING_HANGUPS: list = []  # (deadline, call) - processed on the pjsua thread
PENDING_DIALS: list = []    # (dest, wav, hangup_after) - processed on the pjsua thread
CALLS: list = []


class BotCall(pj.Call):
    def __init__(self, acc, call_id, mode, wav):
        super().__init__(acc, call_id)
        self.mode, self.wav = mode, wav
        self.player = self.recorder = None

    def onCallMediaState(self, prm):
        ci = self.getInfo()
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                am = self.getAudioMedia(mi.index)
                if self.player is None:
                    path = f"{WORK}/out/heard.wav"
                    self.recorder = pj.AudioMediaRecorder()
                    self.recorder.createRecorder(path)
                    self.player = pj.AudioMediaPlayer()
                    self.player.createPlayer(f"{WORK}/{self.wav}", pj.PJMEDIA_FILE_NO_LOOP)
                    with LOCK:
                        STATE["last_recording"] = path
                # (Re)connect on every media update: a re-INVITE can replace the stream's conference port.
                am.startTransmit(self.recorder)
                self.player.startTransmit(am)

    def onCallState(self, prm):
        ci = self.getInfo()
        with LOCK:
            STATE["last_event"] = f"{ci.stateText} {ci.lastStatusCode}"
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.player = None
            self.recorder = None  # destroying the recorder finalises the WAV header
            if self in CALLS:
                CALLS.remove(self)


class BotAccount(pj.Account):
    def onRegState(self, prm):
        with LOCK:
            STATE["registered"] = prm.code == 200

    def onIncomingCall(self, prm):
        with LOCK:
            mode, wav, after = STATE["mode"], STATE["wav"], STATE["hangup_after"]
            STATE["calls"] += 1
        call = BotCall(self, prm.callId, mode, wav)
        CALLS.append(call)
        op = pj.CallOpParam()
        op.statusCode = {"answer": 200, "hangup": 200, "busy": 486, "reject": 603, "noanswer": 180}[mode]
        call.answer(op)
        if mode == "hangup":
            PENDING_HANGUPS.append((time.monotonic() + after, call))


class Control(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with LOCK:
            self._send(dict(STATE, active_calls=len(CALLS)))

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        if self.path == "/dial":
            if not os.path.exists(f"{WORK}/{req.get('wav', '')}"):
                return self._send({"error": "wav not found"}, 400)
            with LOCK:
                PENDING_DIALS.append((str(req.get("dest", "700")), req["wav"], float(req.get("hangup_after", 90))))
            return self.do_GET()
        if req.get("mode") not in ("answer", "busy", "reject", "noanswer", "hangup", "offline"):
            return self._send({"error": "bad mode"}, 400)
        if not os.path.exists(f"{WORK}/{req.get('wav', STATE['wav'])}"):
            return self._send({"error": "wav not found"}, 400)
        with LOCK:
            STATE["want_registered"] = req["mode"] != "offline"
            STATE.update(mode=req["mode"], wav=req.get("wav", STATE["wav"]),
                         hangup_after=float(req.get("hangup_after", 5)))
        self.do_GET()

    def log_message(self, *a):
        pass


def main():
    ep = pj.Endpoint()
    ep.libCreate()
    cfg = pj.EpConfig()
    cfg.uaConfig.threadCnt = 0
    cfg.uaConfig.mainThreadOnly = True
    cfg.logConfig.level = 2
    ep.libInit(cfg)
    tcfg = pj.TransportConfig()
    tcfg.port = int(os.environ.get("BOT_SIP_PORT", "5070"))
    ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)
    ep.libStart()
    ep.audDevManager().setNullDev()

    acfg = pj.AccountConfig()
    acfg.idUri = "sip:1099@127.0.0.1"
    acfg.regConfig.registrarUri = "sip:127.0.0.1:5060"
    acfg.regConfig.timeoutSec = 120
    acfg.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", "1099", 0, os.environ["BOT_PASSWORD"]))
    acc = BotAccount()
    acc.create(acfg)

    os.makedirs(f"{WORK}/out", exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", CTRL_PORT), Control)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"bot up; control on 127.0.0.1:{CTRL_PORT}", flush=True)
    registered_flag = True
    while True:
        ep.libHandleEvents(20)
        with LOCK:
            want = STATE["want_registered"]
        if want != registered_flag:  # (un)register on the pjsua thread
            registered_flag = want
            acc.setRegistration(want)
        now = time.monotonic()
        with LOCK:
            dials, PENDING_DIALS[:] = list(PENDING_DIALS), []
        for dest, wav, after in dials:
            call = BotCall(acc, pj.PJSUA_INVALID_ID, "dial", wav)
            CALLS.append(call)
            prm = pj.CallOpParam(True)
            call.makeCall(f"sip:{dest}@127.0.0.1:5060", prm)
            with LOCK:
                STATE["calls"] += 1
            PENDING_HANGUPS.append((now + after, call))
        for item in list(PENDING_HANGUPS):
            deadline, call = item
            if now >= deadline:
                PENDING_HANGUPS.remove(item)
                try:
                    call.hangup(pj.CallOpParam())
                except pj.Error:
                    pass


if __name__ == "__main__":
    main()
