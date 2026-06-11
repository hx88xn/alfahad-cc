"""
Gemini Live API client for real-time audio streaming.

This module provides a client wrapper for Google's Gemini Live API,
handling WebSocket connections, audio streaming, and function calling.

It is the accent-correct voice backend for the Al Fardan Exchange agent:
Gemini's native multilingual voices speak Urdu/Tamil/Tagalog in the
correct native accent, instead of bleeding the default Arabic/Najdi accent the
way OpenAI's realtime voices do. Ported from ../bankislami-callcenter.
"""

import os
import asyncio
import json
import base64
from contextlib import suppress
from typing import Dict, Any, List, Optional, Callable, AsyncIterator
from dataclasses import dataclass, field
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv(override=True)


# Gemini Live API Configuration
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
GEMINI_SEND_SAMPLE_RATE = 16000  # Input audio sample rate
GEMINI_RECEIVE_SAMPLE_RATE = 24000  # Output audio sample rate

# All available Gemini Live API voices (30 HD voices)
# Keys are Gemini API voice names, 'name' is human-friendly display name
GEMINI_VOICES = {
    # Original Live API voices
    'Puck': {'name': 'Omar', 'gender': 'Male', 'description': 'Conversational, friendly, and upbeat'},
    'Charon': {'name': 'Saad', 'gender': 'Male', 'description': 'Deep, informative, and authoritative'},
    'Kore': {'name': 'Ayesha', 'gender': 'Female', 'description': 'Energetic, youthful, and professional'},
    'Fenrir': {'name': 'Ahmed', 'gender': 'Male', 'description': 'Warm, approachable, and friendly'},
    'Aoede': {'name': 'Sara', 'gender': 'Female', 'description': 'Clear, conversational, and thoughtful'},

}

# Map OpenAI voices to Gemini voices
OPENAI_TO_GEMINI_VOICE_MAP = {
    'echo': 'Charon',      # Male, calm and informative
    'alloy': 'Puck',       # Male, upbeat and conversational
    'shimmer': 'Kore',     # Female, energetic and youthful
    'ash': 'Fenrir',       # Male, warm and friendly
    'coral': 'Aoede',      # Female, clear and thoughtful
    'sage': 'Aoede',       # Female, thoughtful
}


def get_gemini_voice(openai_voice: str) -> str:
    """Map OpenAI voice name to Gemini voice."""
    return OPENAI_TO_GEMINI_VOICE_MAP.get(openai_voice, 'Charon')


@dataclass
class GeminiLiveConfig:
    """Configuration for Gemini Live API session."""
    system_instruction: str = ""
    tools: List[Dict[str, Any]] = field(default_factory=list)
    voice: str = "Charon"
    # Kept low: language adherence (staying in the caller's language) needs to be
    # near-deterministic. Higher temperatures let the native-audio model drift to
    # English, especially after tool results / context compaction.
    temperature: float = 0.3
    response_modalities: List[str] = field(default_factory=lambda: ["AUDIO"])
    enable_input_transcription: bool = True
    enable_output_transcription: bool = True


def convert_openai_tools_to_gemini(openai_tools: List[Dict[str, Any]]) -> List[types.Tool]:
    """
    Convert OpenAI function calling tools format to Gemini format.

    OpenAI format:
    {
        "type": "function",
        "name": "function_name",
        "description": "...",
        "parameters": {...}
    }

    Gemini format uses FunctionDeclaration with same structure.
    """
    function_declarations = []

    for tool in openai_tools:
        if tool.get("type") == "function":
            func_decl = types.FunctionDeclaration(
                name=tool.get("name", ""),
                description=tool.get("description", ""),
                parameters=tool.get("parameters", {})
            )
            function_declarations.append(func_decl)

    if function_declarations:
        return [types.Tool(function_declarations=function_declarations)]
    return []


def convert_openai_tools_to_gemini_dict(openai_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert OpenAI function calling tools format to Gemini dict format for config.

    Returns a list of tool dictionaries suitable for the Live API config.
    """
    function_declarations = []

    for tool in openai_tools:
        if tool.get("type") == "function":
            func_decl = {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {})
            }
            function_declarations.append(func_decl)

    if function_declarations:
        return [{"function_declarations": function_declarations}]
    return []


@dataclass
class GeminiResponse:
    """Represents a response from Gemini Live API."""
    type: str  # 'audio', 'text', 'tool_call', 'setup_complete', 'turn_complete', 'interrupted', 'usage_metadata', 'session_resumed'
    audio_data: Optional[bytes] = None
    text: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    transcription: Optional[str] = None
    is_final: bool = False
    usage_metadata: Optional[Dict[str, Any]] = None


class GeminiLiveClient:
    """
    Client for Gemini Live API with real-time audio streaming.

    Usage:
        config = GeminiLiveConfig(
            system_instruction="You are a helpful assistant.",
            tools=converted_tools,
            voice="Charon"
        )

        async with GeminiLiveClient(config) as client:
            # Send audio
            await client.send_audio(audio_bytes)

            # Receive responses
            async for response in client.receive():
                if response.type == 'audio':
                    # Handle audio output
                    pass
                elif response.type == 'tool_call':
                    # Handle function calls
                    result = await execute_function(...)
                    await client.send_tool_response(call_id, result)
    """

    # How many times to retry the resumption handshake before giving up and
    # letting the call tear down. Gemini drops audio sessions roughly every
    # 10 minutes (close codes 1006/1011/1008), so a long call may resume
    # several times — but a tight cap avoids hammering the API on a hard outage.
    MAX_RECONNECT_ATTEMPTS = 5

    def __init__(self, config: GeminiLiveConfig):
        self.config = config
        self.client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        self.session = None
        self._session_context = None
        self._receive_task = None
        self._audio_queue = asyncio.Queue()
        self._response_queue = asyncio.Queue()
        self._is_connected = False
        self._pending_tool_calls: Dict[str, Dict] = {}

        # --- Session resumption state ---
        # The Live API periodically hands us a resumption handle. We stash the
        # latest one so that when the socket drops (it caps audio sessions at
        # ~10 min) we can reconnect and the server restores the full
        # conversation context — the caller never loses their place.
        self._resumption_handle: Optional[str] = None
        self._reconnect_lock = asyncio.Lock()
        self._go_away_received = False  # server warned it's about to disconnect
        self._resume_count = 0

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self) -> None:
        """Establish connection to Gemini Live API."""
        # Build config as a simple dictionary (per official docs)
        config = {
            "response_modalities": self.config.response_modalities,
        }

        # Add speech config for voice
        if "AUDIO" in self.config.response_modalities and self.config.voice:
            config["speech_config"] = {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": self.config.voice
                    }
                }
            }

        # Add system instruction
        if self.config.system_instruction:
            config["system_instruction"] = self.config.system_instruction

        # Add tools if defined
        if self.config.tools:
            tools = convert_openai_tools_to_gemini_dict(self.config.tools)
            # NOTE: the built-in google_search tool is intentionally NOT enabled.
            # This is a closed-domain bot — all facts come from
            # search_knowledge_base. Search grounding pulled in English web text that
            # primed the model to drift out of the caller's language, and added
            # latency, for no upside.
            config["tools"] = tools

        config["output_audio_transcription"] = {}
        config["input_audio_transcription"] = {}
        config["context_window_compression"] = {
            "sliding_window": {
                "target_tokens": 8192
            }
        }

        # Enable session resumption. With handle=None the server starts a fresh
        # resumable session and begins streaming us resumption handles; on a
        # reconnect we pass the last handle back so the server replays the
        # conversation context and the call continues mid-stream. This is what
        # keeps a >10-minute call alive across the Live API's periodic socket
        # drops (1006/1011/1008) instead of ending the call.
        config["session_resumption"] = types.SessionResumptionConfig(
            handle=self._resumption_handle
        )

        # Wire temperature + thinking level into generation config.
        # thinking_level options: "minimal" | "low" | "medium" | "high".
        # "low" gives a small reasoning budget for multi-step flows
        # without adding noticeable voice latency.
        config["generation_config"] = {
            "temperature": self.config.temperature,
            "thinking_config": {
                "thinking_level": "medium",
            },
        }

        # Start sensitivity stays LOW (less eager to trigger on background noise common
        # for callers — family nearby, TV, street). End sensitivity is HIGH
        # so the bot responds promptly once the caller actually finishes a phrase.
        # silence_duration=600ms is the balance point: long enough to ride through
        # natural Urdu inter-word pauses without cutting the caller off
        # mid-CNIC/card-number, short enough to avoid the dead-air feel that 2000ms had.
        vad_settings = {
            "disabled": False,
            "start_of_speech_sensitivity": types.StartSensitivity.START_SENSITIVITY_HIGH,
            "end_of_speech_sensitivity": types.EndSensitivity.END_SENSITIVITY_HIGH,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 600,
        }
        config["realtime_input_config"] = {
            "automatic_activity_detection": vad_settings
        }

        print(
            f"🎙️ VAD Settings: prefix_padding={vad_settings['prefix_padding_ms']}ms, "
            f"silence_duration={vad_settings['silence_duration_ms']}ms, "
            f"start_sensitivity={vad_settings['start_of_speech_sensitivity'].name}, "
            f"end_sensitivity={vad_settings['end_of_speech_sensitivity'].name}, "
            f"temp={self.config.temperature}"
        )

        # Connect to Live API - this returns an async context manager
        self._session_context = self.client.aio.live.connect(
            model=GEMINI_MODEL,
            config=config
        )
        # Enter the async context manager to get the actual session
        self.session = await self._session_context.__aenter__()
        self._is_connected = True
        mode = "resumed" if self._resumption_handle else "fresh"
        print(
            f"✅ Connected to Gemini Live API (model: {GEMINI_MODEL}, "
            f"voice: {self.config.voice}, session: {mode})"
        )

    async def close(self) -> None:
        """Close the connection."""
        self._is_connected = False
        if self._session_context:
            try:
                # Exit the async context manager properly
                await self._session_context.__aexit__(None, None, None)
            except Exception as e:
                print(f"⚠️ Error closing Gemini session: {e}")
        self.session = None
        self._session_context = None
        print("🔌 Gemini Live API connection closed")

    async def _resume(self) -> bool:
        """Reconnect to the Live API using the stored resumption handle.

        Called from the receive loop when the socket drops unexpectedly (the
        Live API ends audio sessions roughly every 10 minutes with close codes
        1006/1011/1008). The handle restores the conversation context
        server-side, so the model keeps full memory of the call and the caller
        picks up where they left off. Returns True on success.
        """
        async with self._reconnect_lock:
            if not self._resumption_handle:
                print("⚠️ [RESUME] no resumption handle yet — cannot resume session")
                return False

            # Tear down the dead session object (best effort) before reconnecting.
            old_ctx = self._session_context
            self.session = None
            self._session_context = None
            self._is_connected = False
            if old_ctx is not None:
                with suppress(Exception):
                    await old_ctx.__aexit__(None, None, None)

            for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
                try:
                    # connect() reads self._resumption_handle to request a resume.
                    await self.connect()
                    self._resume_count += 1
                    self._go_away_received = False
                    print(
                        f"🔄 [RESUME] Gemini session resumed on attempt {attempt} "
                        f"(resume #{self._resume_count}) — conversation context restored"
                    )
                    return True
                except Exception as e:
                    backoff = min(0.5 * (2 ** attempt), 5.0)
                    print(
                        f"⚠️ [RESUME] attempt {attempt}/{self.MAX_RECONNECT_ATTEMPTS} "
                        f"failed: {e} — retrying in {backoff:.1f}s"
                    )
                    await asyncio.sleep(backoff)

            print("❌ [RESUME] exhausted all resumption attempts")
            return False

    async def send_audio(self, pcm_data: bytes, mime_type: str = "audio/pcm") -> None:
        """
        Send audio data to Gemini.

        Args:
            pcm_data: 16-bit PCM audio at 16kHz sample rate
            mime_type: Audio MIME type (default: audio/pcm)
        """
        # During a transparent resume the session is briefly torn down. Dropping
        # a few hundred ms of real-time audio is fine — far better than raising
        # and tearing down the caller's WebSocket loop, which would end the call.
        if not self._is_connected or not self.session:
            return
        try:
            await self.session.send_realtime_input(
                audio=types.Blob(data=pcm_data, mime_type=mime_type)
            )
        except Exception:
            # Lost the socket mid-send; the receive loop will detect the drop
            # and resume. Swallow here so the browser loop keeps running.
            if not self._is_connected:
                return
            raise

    async def send_text(self, text: str) -> None:
        """Send text input to Gemini to trigger a response."""
        if not self._is_connected or not self.session:
            return

        # Gemini 3.1 Flash Live: send_client_content is now reserved for seeding
        # initial history; runtime text updates must go through send_realtime_input.
        try:
            await self.session.send_realtime_input(text=text)
        except Exception:
            if not self._is_connected:
                return
            raise

    async def send_tool_response(self, function_responses: List[Dict[str, Any]]) -> None:
        """
        Send function call responses back to Gemini.

        Args:
            function_responses: List of {"id": "...", "name": "...", "response": {...}}
        """
        if not self._is_connected or not self.session:
            raise RuntimeError("Not connected to Gemini Live API")

        responses = []
        for resp in function_responses:
            responses.append(types.FunctionResponse(
                id=resp.get("id"),
                name=resp.get("name"),
                response=resp.get("response", {})
            ))

        await self.session.send_tool_response(function_responses=responses)

    async def receive(self) -> AsyncIterator[GeminiResponse]:
        """
        Async iterator for receiving responses from Gemini.

        Yields:
            GeminiResponse objects with audio, text, or tool calls
        """
        if not self._is_connected or not self.session:
            raise RuntimeError("Not connected to Gemini Live API")

        # Continuously receive turns - each receive() handles one turn.
        # We keep calling receive() to handle multiple turns, and if the socket
        # drops mid-call we transparently resume with the stored handle so the
        # conversation continues instead of ending the call.
        while self._is_connected:
            try:
                turn = self.session.receive()
                async for response in turn:
                    try:
                        # --- Session resumption bookkeeping (internal) ---
                        # The server streams us resumption handles throughout the
                        # session; stash the latest so a future drop can resume.
                        sru = getattr(response, "session_resumption_update", None)
                        if sru is not None:
                            if getattr(sru, "resumable", False) and getattr(sru, "new_handle", None):
                                self._resumption_handle = sru.new_handle

                        # The server warns it's about to close the socket (e.g. the
                        # ~10-min audio cap) via a GoAway. We can't keep it open, but
                        # logging the heads-up helps explain the resume that follows.
                        go_away = getattr(response, "go_away", None)
                        if go_away is not None:
                            self._go_away_received = True
                            time_left = getattr(go_away, "time_left", None)
                            print(f"⚠️ [GO_AWAY] Gemini will disconnect soon (time_left={time_left}) — will resume on drop")

                        # Handle server content (audio/text responses)
                        if response.server_content:
                            content = response.server_content

                            # Check for model turn with parts
                            if content.model_turn:
                                for part in content.model_turn.parts:
                                    # Audio data
                                    if hasattr(part, 'inline_data') and part.inline_data:
                                        if hasattr(part.inline_data, 'data') and isinstance(part.inline_data.data, bytes):
                                            yield GeminiResponse(
                                                type='audio',
                                                audio_data=part.inline_data.data
                                            )
                                    # Text data
                                    elif hasattr(part, 'text') and part.text:
                                        yield GeminiResponse(
                                            type='text',
                                            text=part.text
                                        )

                            # Check for turn complete
                            if hasattr(content, 'turn_complete') and content.turn_complete:
                                yield GeminiResponse(type='turn_complete', is_final=True)

                            # Check for interruption
                            if hasattr(content, 'interrupted') and content.interrupted:
                                yield GeminiResponse(type='interrupted')

                            # Handle transcriptions
                            if hasattr(content, 'input_transcription') and content.input_transcription:
                                if hasattr(content.input_transcription, 'text') and content.input_transcription.text:
                                    yield GeminiResponse(
                                        type='input_transcription',
                                        transcription=content.input_transcription.text
                                    )
                            if hasattr(content, 'output_transcription') and content.output_transcription:
                                if hasattr(content.output_transcription, 'text') and content.output_transcription.text:
                                    yield GeminiResponse(
                                        type='output_transcription',
                                        transcription=content.output_transcription.text
                                    )

                        # Handle tool calls
                        if response.tool_call:
                            tool_calls = []
                            for func_call in response.tool_call.function_calls:
                                tool_calls.append({
                                    "id": func_call.id,
                                    "name": func_call.name,
                                    "arguments": dict(func_call.args) if func_call.args else {}
                                })

                            if tool_calls:
                                yield GeminiResponse(
                                    type='tool_call',
                                    tool_calls=tool_calls
                                )

                        # Handle tool call cancellation
                        if hasattr(response, 'tool_call_cancellation') and response.tool_call_cancellation:
                            cancelled_ids = response.tool_call_cancellation.ids
                            print(f"⚠️ Tool calls cancelled: {cancelled_ids}")
                            yield GeminiResponse(
                                type='tool_call_cancelled',
                                tool_calls=[{"cancelled_ids": cancelled_ids}]
                            )

                        # Handle usage metadata (real token counts from Gemini)
                        if hasattr(response, 'usage_metadata') and response.usage_metadata:
                            usage = response.usage_metadata
                            meta = {
                                "total_token_count": getattr(usage, 'total_token_count', None),
                            }
                            details = getattr(usage, 'response_tokens_details', None)
                            if details:
                                meta["response_tokens_details"] = [
                                    {"modality": str(d.modality), "token_count": d.token_count}
                                    for d in details
                                    if hasattr(d, 'modality') and hasattr(d, 'token_count')
                                ]
                            yield GeminiResponse(
                                type='usage_metadata',
                                usage_metadata=meta
                            )

                    except Exception as parse_error:
                        print(f"⚠️ Error parsing Gemini response: {parse_error}")
                        # Continue processing other responses
                        continue

            except Exception as e:
                # An intentional close() flips _is_connected first — in that
                # case just exit the loop quietly.
                if not self._is_connected:
                    break

                # Unexpected socket drop (1006/1011/1008, GoAway, network blip).
                # Try to resume with the handle the server gave us; on success the
                # outer while-loop reconnects to the new session and keeps yielding
                # so the call survives transparently.
                hint = " (after GoAway)" if self._go_away_received else ""
                print(f"❌ Gemini receive loop dropped{hint}: {e}")

                resumed = await self._resume()
                if resumed:
                    # Surface a marker so the caller can flush stale audio buffers.
                    yield GeminiResponse(type='session_resumed')
                    continue

                # No handle, or resumption exhausted — propagate so the call ends.
                import traceback
                traceback.print_exc()
                raise

    @property
    def is_connected(self) -> bool:
        return self._is_connected


async def test_gemini_connection():
    """Test basic Gemini Live API connection."""
    config = GeminiLiveConfig(
        system_instruction="You are a helpful assistant. Say hello briefly.",
        voice="Charon"
    )

    try:
        async with GeminiLiveClient(config) as client:
            print("✅ Connection test successful!")

            # Send a text message to trigger a response
            await client.send_text("Hello!")

            # Receive response
            async for response in client.receive():
                print(f"📨 Response type: {response.type}")
                if response.type == 'audio':
                    print(f"   Audio bytes: {len(response.audio_data)}")
                elif response.type == 'text':
                    print(f"   Text: {response.text}")
                elif response.type == 'turn_complete':
                    print("   Turn complete")
                    break

    except Exception as e:
        print(f"❌ Connection test failed: {e}")
        raise


if __name__ == "__main__":
    # Run connection test
    asyncio.run(test_gemini_connection())
