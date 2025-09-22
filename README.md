
macOS Client for Realtime ASR

Overview

This project lets you capture system audio on macOS (e.g. Zoom/Teams/Webex meetings played in your headphones) and stream it in real time to your ASR WebSocket server (wss_en_server.py).

The workflow:
	•	BlackHole virtual audio device mirrors what you hear in your headphones.
	•	Python client (mac_blackhole_client.py) captures from BlackHole, downsamples to 16 kHz mono PCM16, and streams 60 ms frames to the server.
	•	Server performs real-time transcription (online or 2pass mode) and returns JSON results.

⸻

Requirements
	•	macOS 11+
	•	Python 3.9+
	•	BlackHole (2-channel) installed via Homebrew
	•	Your realtime server (wss_server.py) running locally or on another machine

⸻

1. Install BlackHole

brew install blackhole-2ch

Open Audio MIDI Setup:
	1.	Create a Multi-Output Device.
	2.	Check both BlackHole 2ch and your headphones.
	3.	In macOS Sound Settings (or in your meeting app), set Output Device = Multi-Output Device.

you’ll hear audio in headphones, and the client can capture the same signal from BlackHole.

⸻

2. Run the Realtime Server

Example:

python wss_server.py --host 0.0.0.0 --port 10098

	•	By default it listens on ws://0.0.0.0:10098
	•	Protocol: WebSocket, subprotocol "binary"
	•	Expects raw PCM16 mono 16 kHz binary frames
	•	Also accepts JSON control messages like:

{"mode": "2pass"}
{"is_speaking": true}



⸻

3. Install Python Dependencies

pip install sounddevice numpy websockets resampy


⸻

4. Run the macOS Client

WS_URL="ws://127.0.0.1:10098" ASR_MODE="2pass" python mac_client.py

	•	WS_URL: your server WebSocket address (e.g. ws://localhost:10098 or ws://server-ip:10098)
	•	ASR_MODE: "online" or "2pass"

By default, the client will:
	•	Find the BlackHole device automatically
	•	Capture 60 ms frames
	•	Resample to 16 kHz mono
	•	Stream raw PCM16 to the server
	•	Send is_speaking JSON messages to help with finalizing segments

⸻

5. Expected Output

On the client terminal you’ll see logs like:

[info] Capturing from: BlackHole 2ch (48000 Hz, ch=2)
[info] Connecting to: ws://127.0.0.1:10098 (mode=2pass)
[info] Streaming… Ctrl+C to stop.
[server] online-special: Hello everyone  [FINAL] [0.0, 2.4]
[server] 2pass-offline (segments): 3


