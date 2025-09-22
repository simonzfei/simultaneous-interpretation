import os, asyncio, json, base64, signal
import numpy as np
import sounddevice as sd
import websockets
import resampy
from collections import deque

# ====== CONFIG ======
SERVER_URL = os.getenv("WS_URL", "ws://127.0.0.1:10098")  # e.g. ws://<server-ip>:10098
MODE       = os.getenv("ASR_MODE", "2pass")               # "online" or "2pass"
DEVICE_HINT = os.getenv("INPUT_NAME", "BlackHole")        # substring to find input device
TARGET_SR  = 16000
FRAME_MS   = 60  # your server uses ~0.06s chunk math; keep 60ms here
SEND_JSON_BASE64 = False  # server expects raw PCM binary; leave False

# Simple client-side VAD thresholds (very lightweight)
VAD_RMS_TH   = 0.005   # tweak if needed (smaller → more sensitive)
VAD_HANG_MS  = 400     # keep speaking true for a short time after drop
# =====================

def find_input_device(name_substring: str):
    devs = sd.query_devices()
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0 and name_substring.lower() in d["name"].lower():
            return i, d
    return None, None

class SpeakingState:
    def __init__(self, sr):
        self.is_speaking = False
        self.hang_samples = int((VAD_HANG_MS/1000.0) * sr)
        self.since_voiced = 0

    def update(self, mono: np.ndarray):
        # RMS over this frame
        rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
        voiced = rms >= VAD_RMS_TH
        if voiced:
            self.is_speaking = True
            self.since_voiced = 0
        else:
            self.since_voiced += len(mono)
            if self.is_speaking and self.since_voiced > self.hang_samples:
                self.is_speaking = False
        return self.is_speaking, rms

async def main():
    dev_idx, dev = find_input_device(DEVICE_HINT)
    if dev_idx is None:
        raise RuntimeError(f"Input device containing '{DEVICE_HINT}' not found. "
                           "Open Audio MIDI Setup and ensure 'BlackHole 2ch' exists.")

    src_sr = int(dev["default_samplerate"]) or 48000
    channels = min(2, dev["max_input_channels"]) or 1
    blocksize = int(src_sr * FRAME_MS / 1000)

    print(f"[info] Capturing from: {dev['name']}  ({src_sr} Hz, ch={channels})")
    print(f"[info] Connecting to:  {SERVER_URL}  (mode={MODE})")
    speaking = SpeakingState(TARGET_SR)

    # Graceful stop
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    async with websockets.connect(SERVER_URL, subprotocols=["binary"], ping_interval=None, max_size=None) as ws:
        # Tell server which mode to use
        await ws.send(json.dumps({"mode": MODE}))
        # Initialize as not speaking
        await ws.send(json.dumps({"is_speaking": False}))

        # Reader task: print server messages
        async def reader():
            try:
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        print("[server] (binary msg)", len(msg), "bytes")
                    else:
                        try:
                            data = json.loads(msg)
                        except Exception:
                            print("[server TEXT]", msg[:200])
                            continue
                        # Pretty-print common fields
                        if "text" in data:
                            final_tag = " [FINAL]" if data.get("is_final") else ""
                            ts = data.get("timestamp")
                            ts_str = f" {ts}" if ts else ""
                            print(f"[server] {data.get('mode')}: {data['text']}{final_tag}{ts_str}")
                        elif "speech_segment" in data:
                            print(f"[server] {data.get('mode')} (segments): {len(data['speech_segment'])}")
                        else:
                            print("[server JSON]", data)
            except websockets.ConnectionClosed:
                pass

        reader_task = asyncio.create_task(reader())

        # Audio capture callback -> async sender
        send_queue: "deque[bytes]" = deque()

        def on_audio(indata, frames, time_info, status):
            if status:
                print("[warn]", status)
            # downmix to mono float32 [-1,1]
            if indata.ndim == 2 and indata.shape[1] > 1:
                mono = indata.mean(axis=1)
            else:
                mono = indata.reshape(-1)

            # resample to 16k
            if src_sr != TARGET_SR:
                mono = resampy.resample(mono, src_sr, TARGET_SR)

            # update local VAD & enqueue PCM
            is_speaking_now, _rms = speaking.update(mono)
            pcm16 = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            send_queue.append(pcm16)

            # Opportunistically send is_speaking toggles (non-blocking)
            try:
                # only send when state flips to reduce chatter
                if hasattr(on_audio, "_prev") is False:
                    on_audio._prev = is_speaking_now
                if on_audio._prev != is_speaking_now:
                    on_audio._prev = is_speaking_now
                    asyncio.get_event_loop().create_task(ws.send(json.dumps({"is_speaking": is_speaking_now})))
            except Exception:
                pass

        # Open stream
        with sd.InputStream(
            device=dev_idx,
            channels=channels,
            samplerate=src_sr,
            dtype="float32",
            blocksize=blocksize,
            callback=on_audio,
        ):
            print("[info] Streaming… Ctrl+C to stop.")
            # Sender loop
            while not stop.is_set():
                if send_queue:
                    chunk = send_queue.popleft()
                    if SEND_JSON_BASE64:
                        payload = {
                            "type": "audio_chunk",
                            "format": "pcm_s16le",
                            "sample_rate": TARGET_SR,
                            "channels": 1,
                            "frame_ms": FRAME_MS,
                            "audio": base64.b64encode(chunk).decode("ascii"),
                        }
                        await ws.send(json.dumps(payload))
                    else:
                        await ws.send(chunk)
                else:
                    await asyncio.sleep(0.001)

        # announce end of speech before closing
        try:
            await ws.send(json.dumps({"is_speaking": False}))
        except Exception:
            pass

        await reader_task

if __name__ == "__main__":
    asyncio.run(main())