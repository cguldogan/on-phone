import numpy as np

from agent_phone.audio import (FRAME_SAMPLES, be_bytes_to_pcm, frames, le_bytes_to_pcm, pcm_to_be_bytes, resample,
                               split_sentences, wav_bytes)
from agent_phone.rtp import build_rtp, parse_rtp


def test_big_endian_roundtrip():
    pcm = np.array([0, 1, -1, 256, -32768, 32767], dtype=np.int16)
    wire = pcm_to_be_bytes(pcm)
    assert wire[:4] == b"\x00\x00\x00\x01"  # network byte order
    assert wire[6:8] == b"\x01\x00"         # 256 big-endian
    assert np.array_equal(be_bytes_to_pcm(wire), pcm)


def test_little_endian_and_odd_length():
    assert np.array_equal(le_bytes_to_pcm(b"\x01\x00\x00\x01\x05"), np.array([1, 256], dtype=np.int16))


def test_resample_24k_to_16k_length_and_content():
    t = np.arange(24000) / 24000
    pcm = (np.sin(2 * np.pi * 440 * t) * 10000).astype(np.int16)
    out = resample(pcm, 24000, 16000)
    assert out.dtype == np.int16
    assert abs(out.size - 16000) <= 2
    spectrum = np.abs(np.fft.rfft(out.astype(float)))
    assert abs(int(np.argmax(spectrum)) - 440) <= 2  # tone preserved


def test_framing_pads_last_frame():
    fr = frames(np.ones(FRAME_SAMPLES * 2 + 5, dtype=np.int16))
    assert len(fr) == 3 and all(f.size == FRAME_SAMPLES for f in fr)
    assert fr[-1][5:].sum() == 0


def test_wav_header():
    w = wav_bytes(np.zeros(160, dtype=np.int16))
    assert w[:4] == b"RIFF" and w[8:12] == b"WAVE" and len(w) == 44 + 320


def test_rtp_roundtrip_with_extension_and_padding():
    pkt = build_rtp(118, 65535, 2**32 - 1, 42, b"\x00\x01\x00\x02", marker=True)
    assert parse_rtp(pkt) == (118, 65535, 2**32 - 1, b"\x00\x01\x00\x02")
    # add a one-word header extension and 2 bytes of padding
    hdr = bytearray(pkt[:12])
    hdr[0] |= 0x30
    ext = b"\xbe\xde\x00\x01" + b"\x00" * 4
    padded = bytes(hdr) + ext + b"\x00\x01\x00\x02" + b"\x00\x02"
    assert parse_rtp(padded)[3] == b"\x00\x01\x00\x02"
    assert parse_rtp(b"\x00" * 12) is None  # wrong version


def test_sentence_split():
    parts = split_sentences("Done. Tests pass! Open a PR? " + "word, " * 80)
    assert parts[:3] == ["Done.", "Tests pass!", "Open a PR?"]
    assert all(len(p) <= 221 for p in parts)


def _snr_db(ref: np.ndarray, out: np.ndarray) -> float:
    ref, out = ref.astype(float), out.astype(float)
    # codecs add a small fixed delay; align by cross-correlation
    lag = int(np.argmax(np.correlate(out, ref[:800], mode="valid")))
    out = out[lag:lag + ref.size - 800]
    ref = ref[: out.size]
    return 10 * np.log10(np.sum(ref ** 2) / max(np.sum((ref - out) ** 2), 1e-9))


def test_g722_and_ulaw_roundtrip_preserve_speechband_audio():
    from agent_phone.rtp import make_codec
    t = np.arange(16000) / 16000
    sig = ((np.sin(2 * np.pi * 300 * t) + 0.5 * np.sin(2 * np.pi * 1200 * t)) * 6000).astype(np.int16)
    for name, min_snr in (("g722", 15), ("ulaw", 15)):
        enc, dec = make_codec(name), make_codec(name)
        out = np.concatenate([dec.decode(enc.encode(f)) for f in frames(sig)])
        assert out.size == sig.size, name
        assert _snr_db(sig, out) > min_snr, name
    assert make_codec("g722").pt == 9 and make_codec("g722").ts_per_frame == 160
