import asyncio
import json
import os
import time
import uuid as _uuid_mod
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from audio.pipeline import DictationSession

router = APIRouter()


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


async def _run_final_pass(session: DictationSession, state, fallback: str) -> str:
    plan = state._load_plan.get("final_pass", "skip")

    if plan == "distil_sequential":
        from models.dual_loader import run_distil_final_pass
        audio = session.get_full_audio()
        if len(audio) == 0:
            return fallback
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
                if state._session_active:
                    await ws.send_json({"type": "error", "message": "Session already active"})
                    continue
                if state._training_active:
                    await ws.send_json({"type": "error", "message": "Training in progress"})
                    continue

                if state._load_plan.get("final_pass") == "distil_sequential":
                    from models.dual_loader import reload_turbo_if_needed
                    await asyncio.to_thread(reload_turbo_if_needed, state._turbo_model_ref)

                state._session_active = True
                device_index = msg.get("device_index")
                if isinstance(device_index, int):
                    pass  # valid
                else:
                    device_index = None
                session = DictationSession(state._turbo_model_ref, state._load_plan)
                session.open_mic(device_index=device_index)
                capture_task = asyncio.ensure_future(_capture_loop(session, ws, state))
                await ws.send_json({"type": "stream_started"})

            elif command == "terminate_stream":
                if session and session.is_active():
                    # Signal capture loop to stop — do NOT close stream yet (race with read thread)
                    session.stop_capture()
                    if capture_task is not None:
                        try:
                            await asyncio.wait_for(capture_task, timeout=1.0)
                        except (asyncio.TimeoutError, Exception):
                            pass
                        capture_task = None
                    # Now safe: capture thread has finished its last read
                    session.close_stream()
                    _maybe_save_audio(session, state)
                    fallback = session.build_turbo_fallback()
                    t0 = time.monotonic()
                    final_text = await _run_final_pass(session, state, fallback)
                    final_pass_ms = int((time.monotonic() - t0) * 1000)
                    state._pending_latencies = {
                        "capture_stop_ms": int(t0 * 1000),
                        "final_pass_ms": final_pass_ms,
                    }
                    result = final_text or fallback
                    if result:
                        await ws.send_json({
                            "type": "handoff_ready",
                            "canary_transcript": result,
                        })
                    else:
                        await ws.send_json({"type": "error", "message": "No speech detected"})
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
                    session.close_stream()
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
            session.close_stream()
        state._session_active = False


async def _capture_loop(session: DictationSession, ws: WebSocket, state) -> None:
    from audio.pipeline import CHUNK_SAMPLES
    import numpy as np
    accumulator: list = []
    accumulated = 0
    PRE_ROLL_SAMPLES = 4800  # 300 ms at 16 kHz — delay first Whisper call so first word isn't clipped
    pre_roll_done = False
    pre_roll_accumulated = 0

    try:
        while session.is_active():
            chunk = await asyncio.to_thread(session._capture.read_chunk)
            if not session.is_active():
                # stop_capture() called while reading — keep chunk so final pass gets complete audio
                accumulator.append(chunk)
                accumulated += len(chunk)
                break

            # Send audio level for reactive waveform visualization.
            # Each chunk is ~64ms at 16kHz. Scale so typical speech (RMS 0.02-0.12) maps to 0.16-0.96.
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            level = min(1.0, rms * 8.0)
            try:
                await ws.send_json({"type": "audio_level", "level": level})
            except Exception:
                pass

            accumulator.append(chunk)
            accumulated += len(chunk)

            if not pre_roll_done:
                pre_roll_accumulated += len(chunk)
                if pre_roll_accumulated < PRE_ROLL_SAMPLES:
                    continue
                pre_roll_done = True

            if accumulated < CHUNK_SAMPLES:
                continue

            full_chunk = np.concatenate(accumulator)
            accumulator = []
            accumulated = 0

            auto_terminate = await session.process_chunk(full_chunk, ws)
            if auto_terminate:
                session.stop_capture()
                session.close_stream()
                fallback = session.build_turbo_fallback()
                t0 = time.monotonic()
                final_text = await _run_final_pass(session, state, fallback)
                final_pass_ms = int((time.monotonic() - t0) * 1000)
                state._pending_latencies = {
                    "capture_stop_ms": int(t0 * 1000),
                    "final_pass_ms": final_pass_ms,
                }
                result = final_text or fallback
                try:
                    await ws.send_json({"type": "stream_stopped"})
                    if result:
                        await ws.send_json({"type": "handoff_ready", "canary_transcript": result})
                    else:
                        await ws.send_json({"type": "error", "message": "No speech detected"})
                except Exception:
                    pass
                session.reset()
                state._session_active = False
                return
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": f"Capture error: {e}"})
        except Exception:
            pass
        session.stop_capture()
        session.close_stream()
        state._session_active = False
    finally:
        # Flush any partial audio accumulated since last full chunk — distil needs it
        if accumulator:
            session.flush_partial(np.concatenate(accumulator))
