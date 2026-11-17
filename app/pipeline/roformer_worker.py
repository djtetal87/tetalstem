"""TetalStem "Ultra" quality: BS-Roformer vocal/instrumental pre-pass.

Run as its own process:
    python -m app.pipeline.roformer_worker <device> <input_wav> <out_dir>

Uses the `audio-separator` package (the engine behind Ultimate Vocal Remover)
with a BS-Roformer model to split the song into vocals.wav + instrumental.wav
in out_dir. separate.py then runs Demucs on the instrumental only and swaps in
these cleaner vocals.

Protocol (same shape as vocal_split_worker): progress/log lines on stderr,
then exactly one of "@@DONE@@" or "@@ERROR@@<json message>" (exit 1).
"""

from __future__ import annotations

import json
import os
import sys

ROFORMER_MODEL = (
    os.environ.get("TETALSTEM_ROFORMER_MODEL", "").strip()
    or "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
)


def main() -> None:
    from app.core.process import arm_parent_watchdog

    arm_parent_watchdog()
    if len(sys.argv) < 4:
        sys.stderr.write('@@ERROR@@"usage: roformer_worker <device> <input_wav> <out_dir>"\n')
        sys.stderr.flush()
        sys.exit(1)
    device, input_path, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        if device == "cpu":
            os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        from audio_separator.separator import Separator

        from app.core.config import MODELS_DIR

        sys.stderr.write("@@STAGE@@load\n")
        sys.stderr.flush()
        separator = Separator(
            log_level=40,
            model_file_dir=str(MODELS_DIR / "audio-separator"),
            output_dir=out_dir,
            output_format="WAV",
        )
        separator.load_model(model_filename=ROFORMER_MODEL)
        sys.stderr.write("@@STAGE@@separate\n")
        sys.stderr.flush()
        separator.separate(input_path, {"Vocals": "vocals", "Instrumental": "instrumental"})
    except Exception as e:  # noqa: BLE001 -- any failure means "fall back"
        sys.stderr.write("@@ERROR@@" + json.dumps(f"{type(e).__name__}: {e}") + "\n")
        sys.stderr.flush()
        sys.exit(1)
    for name in ("vocals", "instrumental"):
        if not os.path.isfile(os.path.join(out_dir, f"{name}.wav")):
            sys.stderr.write("@@ERROR@@" + json.dumps(f"{name}.wav was not produced") + "\n")
            sys.stderr.flush()
            sys.exit(1)
    sys.stderr.write("@@DONE@@\n")
    sys.stderr.flush()


if __name__ == "__main__":
    main()
