"""
Gate 3 WS dictation test.
Usage: python gate3_ws_test.py
- Connects, sends begin_stream
- Prints SPEAK NOW — user speaks for ~7 seconds
- Sends terminate_stream
- Waits up to 30s for handoff_ready (distil_sequential takes ~5s)
"""
import asyncio
import json
import sys
import websockets


PORT = 8000
URI = f"ws://127.0.0.1:{PORT}/ws/dictation"


async def run():
    print(f"Connecting to {URI} ...")
    async with websockets.connect(URI) as ws:
        await ws.send(json.dumps({"command": "begin_stream"}))
        print("\n>>> SPEAK NOW (you have 7 seconds) <<<\n")

        partial_count = 0
        deadline = asyncio.get_event_loop().time() + 10.0

        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 1.0))
                msg = json.loads(raw)
                if msg["type"] == "partial_update":
                    partial_count += 1
                    print(f"  [partial #{partial_count}] {msg['content']}")
                elif msg["type"] == "error":
                    print(f"  [ERROR] {msg['message']}")
                    return
            except asyncio.TimeoutError:
                secs_left = int(deadline - asyncio.get_event_loop().time())
                if secs_left > 0:
                    print(f"  (listening... {secs_left}s remaining)", end="\r")
                continue

        print("\n>>> Sending terminate_stream ... <<<")
        await ws.send(json.dumps({"command": "terminate_stream"}))

        print(">>> Waiting for handoff_ready (distil_sequential — can take 30-60s on first inference) ... <<<\n")
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=90.0)
                msg = json.loads(raw)
                if msg["type"] == "partial_update":
                    partial_count += 1
                    print(f"  [partial #{partial_count}] {msg['content']}")
                elif msg["type"] == "handoff_ready":
                    print(f"\n=== GATE 3 PASS ===")
                    print(f"handoff_ready received.")
                    print(f"canary_transcript: {msg['canary_transcript']}")
                    print(f"partial_updates seen: {partial_count}")
                    return
                elif msg["type"] == "error":
                    print(f"  [ERROR] {msg['message']}")
                    return
        except asyncio.TimeoutError:
            print("FAIL: no handoff_ready within 90s")
            sys.exit(1)


asyncio.run(run())
