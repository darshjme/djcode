"""Voice input system for DJcode.

Record from the system microphone, transcribe via local backends.
Gracefully degrades if sounddevice or transcription tools are missing.
Zero cloud dependency — everything runs locally.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Optional dependency: sounddevice
# ---------------------------------------------------------------------------
try:
    import sounddevice as sd  # type: ignore[import-untyped]

    _HAS_SOUNDDEVICE = True
except (ImportError, OSError):
    sd = None  # type: ignore[assignment]
    _HAS_SOUNDDEVICE = False


# ---------------------------------------------------------------------------
# Transcription backends
# ---------------------------------------------------------------------------

class _TranscriptionBackend:
    """Base class for transcription backends."""

    name: str = "base"

    def is_available(self) -> bool:
        return False

    async def transcribe(self, wav_path: str) -> str:
        raise NotImplementedError


class WhisperCppBackend(_TranscriptionBackend):
    """Transcribe using the whisper.cpp CLI binary."""

    name = "whisper.cpp"

    def __init__(self, model: str = "base.en") -> None:
        self.model = model
        self._binary = self._find_binary()

    def _find_binary(self) -> str | None:
        for name in ("whisper-cpp", "whisper", "main"):
            path = shutil.which(name)
            if path:
                return path
        return None

    def is_available(self) -> bool:
        return self._binary is not None

    async def transcribe(self, wav_path: str) -> str:
        if not self._binary:
            return ""
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            "-m", self.model,
            "-f", wav_path,
            "--no-timestamps",
            "-nt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace").strip()


class OllamaWhisperBackend(_TranscriptionBackend):
    """Transcribe by sending audio to an Ollama model that supports audio."""

    name = "ollama"

    def __init__(self, ollama_url: str = "http://localhost:11434", model: str = "whisper") -> None:
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import httpx

            resp = httpx.get(f"{self.ollama_url}/api/tags", timeout=3)
            if resp.status_code == 200:
                models = [m.get("name", "") for m in resp.json().get("models", [])]
                self._available = any("whisper" in m for m in models)
            else:
                self._available = False
        except Exception:
            self._available = False
        return self._available

    async def transcribe(self, wav_path: str) -> str:
        import base64

        try:
            import httpx
        except ImportError:
            return ""

        with open(wav_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()

        payload = {
            "model": self.model,
            "prompt": "Transcribe this audio exactly.",
            "images": [audio_b64],
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{self.ollama_url}/api/generate", json=payload)
            if resp.status_code == 200:
                text_parts: list[str] = []
                for line in resp.text.strip().splitlines():
                    import json

                    try:
                        chunk = json.loads(line)
                        text_parts.append(chunk.get("response", ""))
                    except json.JSONDecodeError:
                        continue
                return "".join(text_parts).strip()
        return ""


class MacOSSpeechBackend(_TranscriptionBackend):
    """Transcribe using macOS SFSpeechRecognizer via a small Swift snippet."""

    name = "macos-speech"

    def is_available(self) -> bool:
        import sys

        return sys.platform == "darwin"

    async def transcribe(self, wav_path: str) -> str:
        swift_code = f"""
import Foundation
import Speech

let semaphore = DispatchSemaphore(value: 0)
let url = URL(fileURLWithPath: "{wav_path}")
let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))!
let request = SFSpeechURLRecognitionRequest(url: url)
request.shouldReportPartialResults = false

recognizer.recognitionTask(with: request) {{ result, error in
    if let result = result, result.isFinal {{
        print(result.bestTranscription.formattedString)
    }} else if let error = error {{
        fputs("Error: \\(error.localizedDescription)\\n", stderr)
    }}
    semaphore.signal()
}}

semaphore.wait()
"""
        with tempfile.NamedTemporaryFile(suffix=".swift", mode="w", delete=False) as tmp:
            tmp.write(swift_code)
            swift_path = tmp.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "swift", swift_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode("utf-8", errors="replace").strip()
        except FileNotFoundError:
            return ""
        finally:
            Path(swift_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Voice Activity Detection (energy-based)
# ---------------------------------------------------------------------------

def _rms_energy(data: bytes) -> float:
    """Compute RMS energy of 16-bit PCM audio."""
    if len(data) < 2:
        return 0.0
    n_samples = len(data) // 2
    samples = struct.unpack(f"<{n_samples}h", data[: n_samples * 2])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / n_samples) ** 0.5


# ---------------------------------------------------------------------------
# Main VoiceInput class
# ---------------------------------------------------------------------------

class VoiceInput:
    """Record from the system microphone and transcribe locally.

    Supports multiple transcription backends with automatic fallback:
    1. whisper.cpp CLI
    2. Ollama whisper model
    3. macOS SFSpeechRecognizer
    """

    def __init__(
        self,
        provider: str | None = None,
        ollama_url: str = "http://localhost:11434",
        sample_rate: int = 16000,
        silence_threshold: float = 300.0,
        silence_duration: float = 1.5,
        max_duration: float = 30.0,
    ) -> None:
        self.recording = False
        self.sample_rate = sample_rate
        self.silence_threshold = silence_threshold
        self.silence_duration = silence_duration
        self.max_duration = max_duration

        # Build ordered backend list
        self._backends: list[_TranscriptionBackend] = []
        if provider == "whisper-cpp":
            self._backends.append(WhisperCppBackend())
        elif provider == "ollama":
            self._backends.append(OllamaWhisperBackend(ollama_url=ollama_url))
        elif provider == "macos":
            self._backends.append(MacOSSpeechBackend())
        else:
            # Auto: try all in order
            self._backends = [
                WhisperCppBackend(),
                OllamaWhisperBackend(ollama_url=ollama_url),
                MacOSSpeechBackend(),
            ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Check if audio input hardware and at least one backend exist."""
        if not _HAS_SOUNDDEVICE:
            return False
        try:
            devices = sd.query_devices()
            if not devices:
                return False
        except Exception:
            return False
        return any(b.is_available() for b in self._backends)

    def get_status(self) -> dict[str, Any]:
        """Return a status dict for diagnostics."""
        backends_status = {}
        for b in self._backends:
            backends_status[b.name] = b.is_available()
        return {
            "sounddevice_installed": _HAS_SOUNDDEVICE,
            "audio_device_found": self._has_input_device(),
            "backends": backends_status,
        }

    async def record_and_transcribe(self) -> str:
        """Record from mic until silence detected, then transcribe.

        Returns the transcribed text, or an error message string
        starting with '[voice]' if something went wrong.
        """
        if not _HAS_SOUNDDEVICE:
            return (
                "[voice] sounddevice is not installed. "
                "Install it with: pip install sounddevice\n"
                "Or: uv pip install sounddevice"
            )

        if not self._has_input_device():
            return "[voice] No audio input device found."

        backend = self._pick_backend()
        if backend is None:
            return (
                "[voice] No transcription backend available.\n"
                "Install one of:\n"
                "  - whisper.cpp: brew install whisper-cpp\n"
                "  - Ollama whisper model: ollama pull whisper\n"
                "  - macOS: available by default on macOS"
            )

        # Record audio
        wav_path = await self._record_audio()
        if wav_path is None:
            return "[voice] Recording cancelled or failed."

        try:
            text = await backend.transcribe(wav_path)
            if not text:
                return "[voice] Transcription returned empty. Try speaking louder or closer to the mic."
            return text
        except Exception as exc:
            return f"[voice] Transcription error: {exc}"
        finally:
            Path(wav_path).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _has_input_device(self) -> bool:
        if not _HAS_SOUNDDEVICE:
            return False
        try:
            info = sd.query_devices(kind="input")
            return info is not None
        except Exception:
            return False

    def _pick_backend(self) -> _TranscriptionBackend | None:
        for b in self._backends:
            if b.is_available():
                return b
        return None

    async def _record_audio(self) -> str | None:
        """Record audio from mic with silence-based VAD, return path to WAV file."""
        self.recording = True
        frames: list[bytes] = []
        chunk_size = int(self.sample_rate * 0.1)  # 100ms chunks
        silent_chunks = 0
        max_silent_chunks = int(self.silence_duration / 0.1)
        max_chunks = int(self.max_duration / 0.1)

        try:
            stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_size,
            )
            stream.start()

            for _ in range(max_chunks):
                if not self.recording:
                    break

                data, overflowed = stream.read(chunk_size)
                raw = bytes(data)
                frames.append(raw)

                energy = _rms_energy(raw)
                if energy < self.silence_threshold:
                    silent_chunks += 1
                else:
                    silent_chunks = 0

                # Only stop on silence after we have at least 0.5s of audio
                if silent_chunks >= max_silent_chunks and len(frames) > 5:
                    break

                # Yield to event loop
                await asyncio.sleep(0)

            stream.stop()
            stream.close()
        except Exception:
            self.recording = False
            return None
        finally:
            self.recording = False

        if not frames:
            return None

        # Write WAV file
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name
        tmp.close()

        audio_data = b"".join(frames)
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio_data)

        return tmp_path

    def stop_recording(self) -> None:
        """Stop an in-progress recording."""
        self.recording = False


def get_missing_deps_message() -> str:
    """Return a user-friendly message about missing voice dependencies."""
    parts: list[str] = []
    if not _HAS_SOUNDDEVICE:
        parts.append(
            "Audio capture requires 'sounddevice'. Install with:\n"
            "  pip install sounddevice   (or: uv pip install sounddevice)"
        )

    backends_available = any([
        WhisperCppBackend().is_available(),
        MacOSSpeechBackend().is_available(),
    ])
    if not backends_available:
        parts.append(
            "No transcription backend found. Install one of:\n"
            "  - whisper.cpp:  brew install whisper-cpp\n"
            "  - macOS Speech: available by default on macOS"
        )

    if not parts:
        return "Voice input is ready."
    return "\n\n".join(parts)
