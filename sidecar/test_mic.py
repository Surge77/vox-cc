"""Quick mic diagnostic — run from sidecar venv to check signal levels + VAD."""
import pyaudio
import numpy as np

DIGITAL_SILENCE_RMS = 0.0005

pa = pyaudio.PyAudio()

# Show all input devices
print("\n=== Available Input Devices ===")
try:
    default_idx = pa.get_default_input_device_info()["index"]
except OSError:
    default_idx = -1

for i in range(pa.get_device_count()):
    info = pa.get_device_info_by_index(i)
    if info["maxInputChannels"] > 0:
        marker = " ← DEFAULT" if i == default_idx else ""
        print(f"  [{i}] {info['name']}  (rate={int(info['defaultSampleRate'])}Hz){marker}")

# Check WASAPI
wasapi_dev = None
for i in range(pa.get_host_api_count()):
    api = pa.get_host_api_info_by_index(i)
    if api.get("type") == pyaudio.paWASAPI:
        wasapi_dev = api.get("defaultInputDevice", -1)
        if wasapi_dev >= 0:
            winfo = pa.get_device_info_by_index(wasapi_dev)
            print(f"\n  WASAPI default input: [{wasapi_dev}] {winfo['name']}")
        else:
            print("\n  WASAPI available but no default input device!")
        break
else:
    print("\n  ⚠ No WASAPI host API found — will fall back to MME (may return zeros!)")

# Try float32 first (matches updated capture.py)
print("\n=== Recording 5 seconds — SPEAK NOW ===\n")
use_float32 = True
try:
    stream = pa.open(
        format=pyaudio.paFloat32,
        channels=1,
        rate=16000,
        input=True,
        frames_per_buffer=16000,
    )
    print("  Format: float32 ✅")
except OSError:
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=16000,
        input=True,
        frames_per_buffer=16000,
    )
    use_float32 = False
    print("  Format: int16 (float32 failed)")

# Try loading Silero VAD
silero_model = None
try:
    import torch
    silero_model, _ = torch.hub.load("snakers4/silero-vad", "silero_vad", trust_repo=True)
    silero_model.eval()
    print("  Silero VAD: loaded ✅")
except Exception as e:
    print(f"  Silero VAD: unavailable ({e})")

dropped = 0
for i in range(5):
    raw = stream.read(16000, exception_on_overflow=False)
    if use_float32:
        audio = np.frombuffer(raw, dtype=np.float32)
    else:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    rms = float(np.sqrt(np.mean(audio ** 2)))
    peak = float(np.max(np.abs(audio)))

    # Silero VAD check
    vad_result = "N/A"
    if silero_model is not None:
        try:
            import torch as _t
            tensor = _t.from_numpy(audio).float()
            # Check 512-sample frames, take max probability
            max_prob = 0.0
            for start in range(0, len(tensor) - 512 + 1, 512):
                frame = tensor[start : start + 512]
                prob = silero_model(frame.unsqueeze(0), 16000).item()
                max_prob = max(max_prob, prob)
            vad_result = f"speech_prob={max_prob:.3f} {'✅ SPEECH' if max_prob > 0.35 else '— no speech'}"
        except Exception as e:
            vad_result = f"error: {e}"

    # AGC simulation
    agc_gain = 0.08 / rms if rms > 1e-7 else 0.0
    agc_gain = min(agc_gain, 20.0)

    status = "❌ DIGITAL SILENCE" if rms < DIGITAL_SILENCE_RMS else "✅ SIGNAL"
    print(f"  Chunk {i+1}: RMS={rms:.6f}  Peak={peak:.4f}  AGC_gain={agc_gain:.1f}x  {status}")
    print(f"           Silero: {vad_result}")

    if rms < DIGITAL_SILENCE_RMS:
        dropped += 1

stream.stop_stream()
stream.close()
pa.terminate()

print(f"\n=== Result: {5-dropped}/5 chunks have signal ===")
if dropped == 5:
    print("⚠ ALL chunks are digital silence! Your mic is returning zeros.")
    print("  → Check Windows Settings > Privacy > Microphone")
    print("  → Close Discord/Teams/browser tabs that might hold exclusive mic access")
    print("  → Try a different audio device index")
elif dropped > 0:
    print("⚠ Some chunks are silent — mic signal may be intermittent.")
else:
    print("✅ Mic signal present. If dictation still fails, check Silero VAD results above.")
    print("   If Silero shows 'no speech' but you were speaking, the gain is too low.")
    print("   AGC should fix this — check audio/vad.py apply_agc().")
