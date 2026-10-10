#!/bin/zsh
# Install native speech services: whisper.cpp (Metal) + Kokoro-82M (kokoro-onnx). Idempotent.
set -euo pipefail
ROOT=${0:A:h:h}
MODELS=$ROOT/models
mkdir -p $MODELS
# Homebrew on Apple silicon must run natively even if this shell is under Rosetta.
arch -arm64 /opt/homebrew/bin/brew list whisper.cpp >/dev/null 2>&1 || arch -arm64 /opt/homebrew/bin/brew install whisper.cpp
fetch() {  # url dest expected-bytes
  if [[ -f $2 && $(stat -f%z $2) == $3 ]]; then echo "ok  ${2:t}"; return; fi
  echo "get ${2:t}"; curl -fL --retry 3 -o $2.part $1 && mv $2.part $2
  [[ $(stat -f%z $2) == $3 ]] || { echo "size mismatch for $2" >&2; exit 1; }
}
fetch https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin $MODELS/ggml-large-v3-turbo.bin 1624555275
fetch https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx $MODELS/kokoro-v1.0.onnx 325532387
fetch https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin $MODELS/voices-v1.0.bin 28214398
fetch https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx $MODELS/silero_vad.onnx $(stat -f%z $MODELS/silero_vad.onnx 2>/dev/null || echo 2327524)
(cd $ROOT/speech/kokoro && arch -arm64 uv sync -q --python 3.12)
echo "speech services installed"
