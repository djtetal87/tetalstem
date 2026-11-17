"""TetalStem "Ultra" separation mode.

Ultra = BS-Roformer (audio-separator) isolates the vocals first, then Demucs
(with shifts=2) splits only the instrumental into drums / bass / guitar /
piano / other. The final vocals stem is Roformer's, which is far cleaner than
Demucs' own; whatever vocal residue Demucs still finds in the instrumental is
folded into "other" so the stems keep summing to the original mix.

Best-effort by design: if audio-separator is not installed (Intel Macs), the
model cannot be fetched, or anything else fails, run_roformer() returns None
and separate.py silently continues as "best" on the original file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.models import Job, JobCancelled, _set
from app.core.registry import set_proc

logger = logging.getLogger("stemdeck.pipeline")

_PCT_RE = re.compile(r"(\d{1,3})%")
# Roformer on a 5-minute song takes ~1-4 min on M1, much longer on CPU. Stop
# only when the worker says nothing at all for this long.
_STALL_SEC = int(os.environ.get("TETALSTEM_ROFORMER_STALL_SEC", "1800"))


def available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("audio_separator") is not None
    except Exception:  # noqa: BLE001
        return False


def run_roformer(job: Job, source: Path, work: Path, device: str) -> tuple[Path, Path] | None:
    """Split `source` into (vocals.wav, instrumental.wav) inside `work`.
    Returns None on any failure (the caller falls back). Raises JobCancelled
    when the user cancelled."""
    if not available():
        logger.info("[%s] ultra: audio-separator not installed; falling back to best", job.id)
        return None
    from app.core.stem_files import decoded_wav

    work.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["STEMDECK_PARENT_PID"] = str(os.getpid())
    env["PYTHONIOENCODING"] = "utf-8:replace"
    try:
        import certifi

        env.setdefault("SSL_CERT_FILE", certifi.where())
        env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ModuleNotFoundError:
        pass

    _set(job, progress=0.0, stage="Ultra: aislando la voz con BS-Roformer...")
    try:
        with decoded_wav(source) as wav_in:
            proc = subprocess.Popen(
                [sys.executable, "-m", "app.pipeline.roformer_worker", device, str(wav_in), str(work)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
            set_proc(job.id, proc)
            ok = _pump(job, proc)
    except JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] ultra: roformer could not run: %s", job.id, exc)
        return None
    finally:
        set_proc(job.id, None)

    if job.cancel_requested:
        raise JobCancelled()
    vocals, inst = work / "vocals.wav", work / "instrumental.wav"
    if not ok or not vocals.is_file() or not inst.is_file():
        return None
    return vocals, inst


def _pump(job: Job, proc: subprocess.Popen) -> bool:
    last = [time.monotonic()]
    done = threading.Event()
    tail: list[str] = []

    def _watch() -> None:
        while not done.wait(15):
            if proc.poll() is not None:
                return
            if job.cancel_requested or time.monotonic() - last[0] > _STALL_SEC:
                proc.terminate()
                return

    threading.Thread(target=_watch, daemon=True).start()
    ok = False
    buf = ""
    try:
        assert proc.stderr is not None
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch not in "\r\n":
                buf += ch
                continue
            line, buf = buf.strip(), ""
            if not line:
                continue
            last[0] = time.monotonic()
            if line == "@@DONE@@":
                ok = True
                break
            if line.startswith("@@ERROR@@"):
                msg = line[len("@@ERROR@@") :]
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    pass
                tail.append(str(msg))
                break
            if line == "@@STAGE@@load":
                _set(job, stage="Ultra: cargando modelo BS-Roformer (la primera vez se descarga)...")
                continue
            if line == "@@STAGE@@separate":
                _set(job, stage="Ultra: aislando la voz con BS-Roformer...")
                continue
            m = _PCT_RE.search(line)
            if m:
                # Roformer is the first ~45% of an Ultra job.
                _set(job, progress=min(100, int(m.group(1))) * 0.45)
            else:
                tail.append(line)
                del tail[:-30]
    finally:
        done.set()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    if job.cancel_requested:
        raise JobCancelled()
    if not ok:
        logger.warning("[%s] ultra: roformer failed; falling back to best. tail:\n%s",
                       job.id, "\n".join(tail[-15:]) or "(nothing)")
    return ok


def merge(stems_root: Path, roformer_vocals: Path) -> None:
    """Put Roformer's vocals in place of Demucs' and fold Demucs' residual
    vocals (found in the instrumental) into other.wav."""
    import numpy as np
    import soundfile as sf

    dem_vocals = stems_root / "vocals.wav"
    other = stems_root / "other.wav"
    ref_info = sf.info(str(dem_vocals)) if dem_vocals.is_file() else None

    voc, sr = sf.read(str(roformer_vocals), dtype="float32", always_2d=True)
    if ref_info is not None:
        n = ref_info.frames
        if voc.shape[0] < n:
            voc = np.pad(voc, ((0, n - voc.shape[0]), (0, 0)))
        voc = voc[:n]
        if voc.shape[1] != ref_info.channels:
            voc = np.repeat(voc[:, :1], ref_info.channels, axis=1)
        sr = ref_info.samplerate

    if dem_vocals.is_file() and other.is_file():
        res, _ = sf.read(str(dem_vocals), dtype="float32", always_2d=True)
        oth, osr = sf.read(str(other), dtype="float32", always_2d=True)
        n = min(len(res), len(oth))
        mixed = oth.copy()
        mixed[:n] += res[:n]
        peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
        if peak > 1.0:
            mixed /= peak
        sf.write(str(other), mixed, osr, subtype="PCM_16")

    peak = float(np.max(np.abs(voc))) if voc.size else 0.0
    if peak > 1.0:
        voc /= peak
    sf.write(str(dem_vocals), voc, sr, subtype="PCM_16")


def cleanup(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)
