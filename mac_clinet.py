#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mac_realtime_client.py
Capture system audio on macOS via BlackHole and stream to a realtime ASR WebSocket server.

Features
- Input device by name (e.g., "BlackHole")
- 16 kHz mono PCM16 frames (binary) or JSON+base64 (optional)
- 20–80 ms frame size (default 60 ms to match typical server chunk math)
- Lightweight client-side VAD -> sends {"is_speaking": true/false}
- Heartbeat pings, graceful shutdown, simple reconnect loop
"""

import argparse
import asyncio
import base64
import json
import os
import signal
import ssl
import sys
import time
from collections import deque
from typing import Optional, Tuple

import numpy as np
import resampy
import sounddevice as sd
import websockets

###############################################################################
# Utilities
###############################################################################

def list_input_devices() -> None:
    devs = sd.query_devices()
    print("=== Input-capable devices ===")
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0:
            print(f"[{i}] {d['name']} | in_ch={d['max_input_channels']} | sr={d.get('default_samplerate')}")


def find_input_device(name_substring: str) -> Tuple[Optional[int], Optional[dict]]:
    devs = sd.query_devices()
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0 and name_substring.lower() in d["name"].lower():
            return i, d
    return None, None


class SpeakingState:
    """Tiny RMS-based VAD with hangover to avoid rapid toggling."""
    def __init__(self, target_sr: int, threshold: float = 0.005, hang_ms: int = 400):
        self.th = float(threshold)
        self.hang_samples = int(target_sr * (hang_ms / 1000.0))
        self.is_speaking = False
        self.since_voiced = 0
        self._last_state = None  # for change detection

    def update(self, mono: np.ndarray) -> bool:
        if mono.size == 0:
            rms = 0.0
        else:
            # small epsilon to avoid denormals
            rms = float(np.sqrt(np.mean(np.square(mono)) + 1e-12))
        voiced = rms >= self.th
        if voiced:
            self.is_speaking = True
            self.since_voiced = 0
        else:
            self.since_voiced += mono.size
            if self.is_speaking and self.since_voiced > self.hang_samples:
                self.is_speaking = False
        return self.is_speaking

    def changed(self) -> Optional[bool]:
        if self._last_state is None:
            self._last_state = self.is_speaking
            return None
        if self._last_state != self.is_speaking:
            self._last_state = self.is_speaking
            return self.is_speaking
        return None


###############################################################################
# Core client
###############################################################################

class RealtimeClient:
    def __init__(
        self,
        url: str,
        mode: str,
        input_name: str,
        frame_ms: int,
        target_sr: int,
        json_base64: bool,
        vad_enable: bool,
        vad_threshold: float,
        vad_hang_ms: int,
        ping_interval: float,
        reconnect_sleep: float,
        ssl_insecure: bool,
    ):
        self.url = url
        self.mode = mode
        self.input_name = input_name
        self.frame_ms = frame_ms
        self.target_sr = target_sr
        self.json_base64 = json_base64
        self.vad_enable = vad_enable
        self.vad_threshold = vad_threshold
        self.vad_hang_ms = vad_hang_ms
        self.ping_interval = ping_interval
        self.reconnect_sleep = reconnect_sleep
        self.ssl_insecure = ssl_insecure

        self.dev_idx = None
        self.dev_info = None
        self.stop_event = asyncio.Event()
        self.send_queue: "deque[bytes]" = deque()
        self.speaking: Optional[SpeakingState] = None
        self.reader_task = None
        self.pinger_task = None

    def _build_ssl_context(self) -> Optional[ssl.SSLContext]:
        if self.url.lower().startswith("wss://"):
            if self.ssl_insecure:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                return ctx
            return ssl.create_default_context()
        return None

    def select_device(self):
        if self.input_name == "":
            print("[info] Using default input device.")
            return
        idx, info = find_input_device(self.input_name)
        if idx is None:
            print(f"[error] Input device containing '{self.input_name}' not found.")
            list_input_devices()
            sys.exit(2)
        self.dev_idx, self.dev_info = idx, info
        print(f"[info] Using input device: [{idx}] {info['name']}")

    async def _reader(self, ws: websockets.WebSocketClientProtocol):
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    print(f"[server] (binary) {len(msg)} bytes")
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
                        print(f"[server] {data.get('mode')} segments: {len(data['speech_segment'])}")
                    else:
                        print("[server JSON]", data)
        except websockets.ConnectionClosed:
            pass

    async def _pinger(self, ws: websockets.WebSocketClientProtocol):
        while not self.stop_event.is_set():
            try:
                await ws.ping()
            except Exception:
                return
            await asyncio.sleep(self.ping_interval)

    async def _run_once(self):
        # Resolve device & samplerate
        if self.dev_info is None and self.input_name:
            self.select_device()
        if self.dev_info is None:
            # default device info
            idx = sd.default.device[0]
            if idx is not None and idx >= 0:
                self.dev_info = sd.query_devices(idx)
                self.dev_idx = idx

        # Fallback samplerate
        src_sr = int((self.dev_info or {}).get("default_samplerate") or 48000)
        in_channels = min(2, (self.dev_info or {}).get("max_input_channels", 1)) or 1
        blocksize = int(src_sr * self.frame_ms / 1000)

        self.speaking = SpeakingState(
            target_sr=self.target_sr,
            threshold=self.vad_threshold,
            hang_ms=self.vad_hang_ms,
        )

        print(f"[info] Capture: sr={src_sr} ch={in_channels} frame={self.frame_ms}ms  -> send 16k mono")
        print(f"[info] Connecting to: {self.url} (mode={self.mode})")
        ssl_ctx = self._build_ssl_context()

        async with websockets.connect(
            self.url,
            subprotocols=["binary"],
            max_size=None,
            ping_interval=None,  # we run our own
            ssl=ssl_ctx,
        ) as ws:
            # Initial control frames
            await ws.send(json.dumps({"mode": self.mode}))
            await ws.send(json.dumps({"is_speaking": False}))

            self.reader_task = asyncio.create_task(self._reader(ws))
            self.pinger_task = asyncio.create_task(self._pinger(ws))

            # Audio callback
            def on_audio(indata, frames, time_info, status):
                if status:
                    print("[warn]", status)
                # downmix to mono float32 [-1, 1]
                if indata.ndim == 2 and indata.shape[1] > 1:
                    mono = indata.mean(axis=1)
                else:
                    mono = indata.reshape(-1)

                # resample to 16k
                if src_sr != self.target_sr:
                    mono = resampy.resample(mono, src_sr, self.target_sr)

                # Update VAD and queue PCM
                if self.vad_enable and self.speaking is not None:
                    self.speaking.update(mono)
                    changed = self.speaking.changed()
                    if changed is not None:
                        try:
                            asyncio.get_event_loop().create_task(
                                ws.send(json.dumps({"is_speaking": bool(changed)}))
                            )
                        except Exception:
                            pass

                pcm16 = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
                self.send_queue.append(pcm16)

            # Open stream
            with sd.InputStream(
                device=self.dev_idx,
                channels=in_channels,
                samplerate=src_sr,
                dtype="float32",
                blocksize=blocksize,
                callback=on_audio,
            ):
                print("[info] Streaming… Ctrl+C to stop.")
                # Sender loop
                while not self.stop_event.is_set():
                    if self.send_queue:
                        chunk = self.send_queue.popleft()
                        if self.json_base64:
                            payload = {
                                "type": "audio_chunk",
                                "format": "pcm_s16le",
                                "sample_rate": self.target_sr,
                                "channels": 1,
                                "frame_ms": self.frame_ms,
                                "audio": base64.b64encode(chunk).decode("ascii"),
                            }
                            await ws.send(json.dumps(payload))
                        else:
                            await ws.send(chunk)
                    else:
                        await asyncio.sleep(0.001)

            # Finalize
            try:
                await ws.send(json.dumps({"is_speaking": False}))
            except Exception:
                pass

            # Drain reader/pinger
            if self.reader_task:
                self.reader_task.cancel()
            if self.pinger_task:
                self.pinger_task.cancel()

    async def run(self):
        # Signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass

        while not self.stop_event.is_set():
            try:
                await self._run_once()
                break
            except (websockets.InvalidURI, websockets.InvalidHandshake) as e:
                print(f"[fatal] WebSocket error: {e}")
                break
            except websockets.ConnectionClosed as e:
                if self.stop_event.is_set():
                    break
                print(f"[warn] Connection closed: {e}. Reconnecting in {self.reconnect_sleep:.1f}s…")
                await asyncio.sleep(self.reconnect_sleep)
            except Exception as e:
                if self.stop_event.is_set():
                    break
                print(f"[warn] Exception: {e}. Reconnecting in {self.reconnect_sleep:.1f}s…")
                await asyncio.sleep(self.reconnect_sleep)


###############################################################################
# CLI
###############################################################################

def parse_args():
    ap = argparse.ArgumentParser(description="macOS realtime client (BlackHole -> WebSocket server)")
    ap.add_argument("--url", type=str, required=False, default=os.getenv("WS_URL", "ws://127.0.0.1:10098"),
                    help="WebSocket URL, e.g., ws://host:port or wss://host:port")
    ap.add_argument("--mode", type=str, default=os.getenv("ASR_MODE", "2pass"),
                    choices=["online", "2pass"], help="Server transcription mode")
    ap.add_argument("--input-name", type=str, default=os.getenv("INPUT_NAME", "BlackHole"),
                    help="Substring of input device name to use (e.g., 'BlackHole'); empty for default device")
    ap.add_argument("--frame-ms", type=int, default=int(os.getenv("FRAME_MS", "60")),
                    help="Frame size in milliseconds (20–80 typical). Default 60ms.")
    ap.add_argument("--target-sr", type=int, default=int(os.getenv("TARGET_SR", "16000")),
                    help="Target sample rate (Hz), default 16000")
    ap.add_argument("--json-base64", action="store_true",
                    help="Send JSON+base64 frames instead of raw PCM binary")
    ap.add_argument("--no-vad", action="store_true", help="Disable client-side VAD toggles")
    ap.add_argument("--vad-threshold", type=float, default=float(os.getenv("VAD_TH", "0.005")),
                    help="RMS threshold for tiny VAD (smaller = more sensitive)")
    ap.add_argument("--vad-hang-ms", type=int, default=int(os.getenv("VAD_HANG_MS", "400")),
                    help="Keep speaking=true for HANG_MS after energy falls")
    ap.add_argument("--ping-interval", type=float, default=float(os.getenv("PING_SEC", "15")),
                    help="WebSocket ping interval seconds (client heartbeat)")
    ap.add_argument("--reconnect-sleep", type=float, default=float(os.getenv("RECONNECT_SEC", "2")),
                    help="Seconds to sleep before reconnecting")
    ap.add_argument("--ssl-insecure", action="store_true",
                    help="If wss:// and you use self-signed certs, skip verification (dev only!)")
    ap.add_argument("--list-devices", action="store_true", help="List input-capable devices and exit")
    return ap.parse_args()


def main():
    args = parse_args()
    if args.list_devices:
        list_input_devices()
        return

    if args.frame_ms < 10 or args.frame_ms > 200:
        print("[warn] frame-ms unusual; typical is 20–80 ms.")

    client = RealtimeClient(
        url=args.url,
        mode=args.mode,
        input_name=args.input-name if hasattr(args, "input-name") else args.input_name,  # hyphen guard
        frame_ms=args.frame_ms,
        target_sr=args.target_sr,
        json_base64=args.json_base64,
        vad_enable=not args.no_vad,
        vad_threshold=args.vad_threshold,
        vad_hang_ms=args.vad_hang_ms,
        ping_interval=args.ping_interval,
        reconnect_sleep=args.reconnect_sleep,
        ssl_insecure=args.ssl_insecure,
    )
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # argparse uses underscores; make sure we access the right attribute if user copies "--input-name"
    # The hasattr guard above handles it, but we also normalize here:
    if "--input-name" in sys.argv:
        # argparse will store it as input_name automatically; nothing to change
        pass
    main()