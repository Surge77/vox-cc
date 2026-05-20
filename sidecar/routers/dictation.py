import asyncio
import json
import os
import time
import uuid as _uuid_mod
import wave
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from audio.pipeline import DictationSession

router = APIRouter()


def _save_debug_wav(audio_np: np.ndarray, path: str) -> None:
    """Write float32 audio as 16-bit PCM mono WAV for human playback."""
    if len(audio_np) == 0:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pcm16 = np.clip(audio_np, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm16.tobytes())


def _dump_session_debug(session: DictationSession, state) -> None:
    """Always-on debug dump. Writes raw + AGC'd full-session audio to ~/.vox/data/debug/."""
    try:
        from audio.vad import apply_agc
        raw = session.get_full_audio()
        if len(raw) == 0:
            return
        debug_dir = os.path.join(state.DATA_DIR, "debug")
        _save_debug_wav(raw, os.path.join(debug_dir, "last_raw.wav"))
        _save_debug_wav(apply_agc(raw), os.path.join(debug_dir, "last_agc.wav"))
        rms = float(np.sqrt(np.mean(raw ** 2)))
        peak = float(np.max(np.abs(raw)))
        print(f"[sidecar] debug wav: dur={len(raw)/16000:.2f}s rms={rms:.4f} peak={peak:.4f} dir={debug_dir}", flush=True)
    except Exception as e:
        print(f"[sidecar] debug wav save failed: {e}", flush=True)


def _maybe_save_audio(session: DictationSession, state) -> None:
    """Save full session audio as .npy if passive collection is enabled. Sets state._pending_audio_uuid."""
    settings_path = os.path.join(state.DATA_DIR, "settings.json")
    try:
        with open(settings_path) as f:
            if not json.load(f).get("passive_collection_enabled", False):
                return
    except (FileNotFoundError, json.JSONDecodeError):
        return

    audio = session.get_full_audio()
    if len(audio) == 0:
        return

    clips_dir = os.path.join(state.DATA_DIR, "audio_clips")
    os.makedirs(clips_dir, exist_ok=True)
    uid = str(_uuid_mod.uuid4())
    np.save(os.path.join(clips_dir, f"{uid}.npy"), audio)
    state._pending_audio_uuid = uid


async def _run_final_pass(session: DictationSession, state, fallback: str, ws) -> str:
    plan = state._load_plan.get("final_pass", "skip")

    if plan == "distil_sequential":
        from models.dual_loader import run_distil_final_pass
        audio = session.get_full_audio()
        if len(audio) == 0:
            return fallback
        try:
            await ws.send_json({
                "type": "processing_status",
                "stage": "final_pass",
                "message": "Enhancing accuracy (~5s)…",
            })
        except Exception:
            pass
        try:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, run_distil_final_pass, audio, state._turbo_model_ref),
                timeout=30.0,
            )
            return result if result.strip() else fallback
        except (asyncio.TimeoutError, Exception):
            return fallback

    return fallback


@router.websocket("/ws/dictation")
async def dictation_ws(ws: WebSocket):
    await ws.accept()
    import main as state

    session: DictationSession | None = None
    capture_task: asyncio.Task | None = None

    try:
        while True:
            msg = await ws.receive_json()
            command = msg.get("command")

            if command == "begin_stream":
                # If a session is already active (e.g. drain timed out, or previous
                # terminate was missed), force-reset it instead of blocking the user.
                if state._session_active:
                    if capture_task is not None:
                        capture_task.cancel()
                        try:
                            await asyncio.wait_for(capture_task, timeout=0.5)
                        except Exception:
                            pass
                        capture_task = None
                    if session:
                        try:
                            session.stop_session()
                        except Exception:
                            pass
                        session = None
                    state._session_active = False
                    print("[sidecar] warn: force-reset stale session", flush=True)
                if state._training_active:
                    await ws.send_json({"type": "error", "message": "Training in progress"})
                    continue

                if state._load_plan.get("final_pass") == "distil_sequential":
                    from models.dual_loader import reload_turbo_if_needed
                    await asyncio.to_thread(reload_turbo_if_needed, state._turbo_model_ref)

                if state._mic_ref[0] is None:
                    await ws.send_json({"type": "error", "message": "Microphone not initialized"})
                    continue

                state._session_active = True
                # device_index is no longer used per-session — the persistent mic
                # was opened at sidecar startup with the configured device. To
                # change device, update settings.json + restart sidecar.
                session = DictationSession(
                    state._mic_ref[0],
                    state._turbo_model_ref,
                    state._load_plan,
                    asyncio.get_event_loop(),
                )
                session.start_session()
                print(f"[sidecar] begin_stream session={id(session)} preroll_chunks={len(session._ring)}", flush=True)
                capture_task = asyncio.ensure_future(_capture_loop(session, ws, state))
                await ws.send_json({"type": "stream_started"})

            elif command == "terminate_stream":
                if session and session.is_active():
                    print(f"[sidecar] terminate_stream session={id(session)}", flush=True)
                    # Signal capture loop to stop reading from queue. The
                    # listener stays attached so the loop's own drain phase
                    # can pull trailing chunks (mic keeps producing).
                    session.stop_capture()
                    if capture_task is not None:
                        try:
                            await asyncio.wait_for(capture_task, timeout=4.0)
                        except (asyncio.TimeoutError, Exception):
                            pass
                        capture_task = None
                    # Capture loop has unsubscribed; safe to flush state.
                    session.stop_session()
                    _maybe_save_audio(session, state)
                    _dump_session_debug(session, state)
                    fallback = session.build_turbo_fallback()
                    t0 = time.monotonic()
                    print("[sidecar] running final pass", flush=True)
                    final_text = await _run_final_pass(session, state, fallback, ws)
                    final_pass_ms = int((time.monotonic() - t0) * 1000)
                    state._pending_latencies = {
                        "capture_stop_ms": int(t0 * 1000),
                        "final_pass_ms": final_pass_ms,
                    }
                    result = final_text or fallback
                    print(f"[sidecar] final_pass result len={len(result)} preview={result[:60]!r}", flush=True)
                    print(f"[sidecar] -> handoff_ready len={len(result)}", flush=True)
                    await ws.send_json({
                        "type": "handoff_ready",
                        "canary_transcript": result,
                    })
                    session.reset()
                    session = None
                await ws.send_json({"type": "stream_stopped"})
                state._session_active = False

            elif command == "cancel_stream":
                if session and session.is_active():
                    session.stop_capture()
                    if capture_task is not None:
                        try:
                            await asyncio.wait_for(capture_task, timeout=1.0)
                        except (asyncio.TimeoutError, Exception):
                            pass
                        capture_task = None
                    session.stop_session()
                    session.reset()
                    session = None
                await ws.send_json({"type": "stream_stopped"})
                state._session_active = False

    except WebSocketDisconnect:
        pass
    finally:
        if session and session.is_active():
            session.stop_capture()
            if capture_task is not None:
                try:
                    await asyncio.wait_for(capture_task, timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            session.stop_session()
        state._session_active = False


async def _capture_loop(session: DictationSession, ws: WebSocket, state) -> None:
    """Pulls chunks from the session's queue (fed by PersistentMicCapture's
    background thread). When session goes inactive, drains for ≤2s waiting
    for 700ms silence so trailing words spoken after key-release are kept."""
    from audio.pipeline import CHUNK_SAMPLES, SAMPLE_RATE
    import numpy as np
    accumulator: list = []
    accumulated = 0

    try:
        chunk_idx = 0
        peak_rms = 0.0
        zero_streak = 0
        print(
            f"[sidecar] capture loop started (preroll={len(session._ring)} chunks already in ring)",
            flush=True,
        )

        while session.is_active():
            chunk = await session.read_chunk(timeout=0.2)
            if chunk is None:
                continue  # timeout — recheck _active

            rms = float(np.sqrt(np.mean(chunk ** 2)))
            level = min(1.0, rms * 30.0)
            try:
                await ws.send_json({"type": "audio_level", "level": level})
            except Exception:
                pass

            accumulator.append(chunk)
            accumulated += len(chunk)
            chunk_idx += 1
            peak_rms = max(peak_rms, rms)

            if rms > 0.0001:
                zero_streak = 0
            else:
                zero_streak += 1
                if zero_streak >= 150:
                    print(
                        "[sidecar] ERROR: 10s of zero-signal from persistent mic — "
                        "check Windows Sound Settings > Input.",
                        flush=True,
                    )
                    await ws.send_json({
                        "type": "error",
                        "message": "Mic not responding — wrong default device? Check Sound Settings.",
                    })
                    session.stop_capture()
                    break

            if chunk_idx % 10 == 0:
                print(f"[sidecar] chunk {chunk_idx} rms={rms:.4f} level={level:.3f}", flush=True)

            if accumulated >= CHUNK_SAMPLES:
                full_chunk = np.concatenate(accumulator)
                accumulator = []
                accumulated = 0
                auto_terminate = await session.process_chunk(full_chunk, ws)
                if auto_terminate:
                    session.stop_capture()
                    session.stop_session()
                    fallback = session.build_turbo_fallback()
                    t0 = time.monotonic()
                    print("[sidecar] auto-terminate: running final pass", flush=True)
                    final_text = await _run_final_pass(session, state, fallback, ws)
                    final_pass_ms = int((time.monotonic() - t0) * 1000)
                    state._pending_latencies = {
                        "capture_stop_ms": int(t0 * 1000),
                        "final_pass_ms": final_pass_ms,
                    }
                    result = final_text or fallback
                    print(
                        f"[sidecar] auto-terminate: final_pass len={len(result)} preview={result[:60]!r}",
                        flush=True,
                    )
                    try:
                        await ws.send_json({"type": "stream_stopped"})
                        await ws.send_json({"type": "handoff_ready", "canary_transcript": result})
                    except Exception:
                        pass
                    session.reset()
                    state._session_active = False
                    return

        # Out of main loop — _active went False. Flush any partial then drain
        # trailing speech (mic kept producing while we exited the loop).
        if accumulator:
            session.flush_partial(np.concatenate(accumulator))
            accumulator = []
            accumulated = 0

        import time as _time
        _drain_start = _time.monotonic()
        _silence_samples = 0
        _SILENCE_FLOOR = 0.0005
        _SILENCE_TARGET = int(0.70 * SAMPLE_RATE)
        _DRAIN_MAX_S = 2.0
        print("[sidecar] drain: capturing trailing speech...", flush=True)
        while (_time.monotonic() - _drain_start) < _DRAIN_MAX_S:
            _dc = await session.read_chunk(timeout=0.2)
            if _dc is None:
                continue
            session.flush_partial(_dc)
            _rms = float(np.sqrt(np.mean(_dc ** 2)))
            if _rms < _SILENCE_FLOOR:
                _silence_samples += len(_dc)
                if _silence_samples >= _SILENCE_TARGET:
                    break
            else:
                _silence_samples = 0
        _drain_ms = int((_time.monotonic() - _drain_start) * 1000)
        print(f"[sidecar] drain: done in {_drain_ms}ms", flush=True)

    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": f"Capture error: {e}"})
        except Exception:
            pass
        session.stop_capture()
        session.stop_session()
        state._session_active = False
    finally:
        if accumulator:
            session.flush_partial(np.concatenate(accumulator))
        if peak_rms < 0.015 and chunk_idx > 5:
            print(
                f"[sidecar] WARNING: mic level very low (peak_rms={peak_rms:.4f}). "
                "Increase Windows mic volume to 80-100% and disable enhancements.",
                flush=True,
            )
