import os
import json
import base64
import asyncio
import websockets
import uuid
import time
import io
import tempfile
import traceback
from fastapi import FastAPI, WebSocket, Request, HTTPException, Depends, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from openai import AsyncOpenAI
from fastapi.websockets import WebSocketDisconnect
from datetime import datetime as dt, timedelta, timezone
import jwt
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream, Parameter
from dotenv import load_dotenv
from pydub import AudioSegment
import audioop
from contextlib import suppress
from prompts import function_call_tools, build_system_message, build_chat_system_message, chat_tools, get_language_accent_result
from utils import *
import httpx
from call_log_apis import *
from customer_card_tools import (
    verify_customer_by_cnic,
    confirm_physical_custody,
    verify_tpin,
    verify_card_details,
    activate_card,
    update_customer_tpin,
    transfer_to_ivr_for_pin,
    transfer_to_agent,
    get_customer_status,
)
from rag_tools import search_knowledge_base

from src.utils.audio_transcription import transcribe_audio, analyze_call_with_llm

load_dotenv(override=True)

# --- Configuration ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = 7035

VOICE = 'echo'

# Event types to log (but not treat as errors)
LOG_EVENT_TYPES = [
    'response.content.done', 'input_audio_buffer.committed',
    'session.created', 'conversation.item.deleted', 'conversation.item.created'
]

# Event types that indicate potential issues (logged with warnings)
WARNING_EVENT_TYPES = [
    'error', 'rate_limits.updated'
]

SHOW_TIMING_MATH = False
call_recordings = {}

app = FastAPI()

# JWT Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "fortcall-ai-call-center-secret-key-2024")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# Hardcoded users for authentication
USERS_DB = {
    "admin": {
        "username": "admin",
        "password": "admin1234",
        "full_name": "Administrator"
    },
    "demo": {
        "username": "demouser",
        "password": "demouser1234",
        "full_name": "Demo User"
    },
    "fortcall": {
        "username": "fortcall",
        "password": "fortcall1234",
        "full_name": "FortCall Team"
    }
}

from fastapi.staticfiles import StaticFiles
app.mount("/client", StaticFiles(directory="static", html=True), name="client")

CHANNELS = 1
RATE = 8000

call_metadata: dict[str, dict] = {}

@app.get("/", response_class=HTMLResponse)
async def index_page():
    with open("static/voice-client.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return html_content

from fastapi import Body

AVAILABLE_VOICES = {
    'echo': {
        'name': 'Saad',
        'age': 'Young Male',
        'personality': 'Warm, Friendly and Engaging'
    }
}


@app.post("/start-browser-call")
async def start_browser_call(request: Request, payload: dict = Body(...)):
    """
    Called by the browser to create a server-side call record.
    Returns call_id to be used by the WebSocket.
    Requires valid JWT token.
    """
    # Verify JWT token
    token = get_token_from_request(request)
    user_data = verify_jwt_token(token)
    
    phone = payload.get("phone", "webclient")
    voice = payload.get("voice", "echo")
    temperature = payload.get("temperature", 0.8)
    speed = payload.get("speed", 1.05)
    
    # Validate voice
    if voice not in AVAILABLE_VOICES:
        voice = "echo"
    
    # Validate temperature (0.0 - 1.2)
    temperature = max(0.0, min(1.2, float(temperature)))
    
    # Validate speed (0.5 - 2.0)
    speed = max(0.5, min(2.0, float(speed)))
        
    print(f"🎙️ Voice selected: {voice} ({AVAILABLE_VOICES[voice]['name']})")
    print(f"🌡️ Temperature: {temperature}")
    print(f"⚡ Speed: {speed}x")
    
    call_id = await register_call(phone)
    call_id = str(call_id)
    call_recordings[call_id] = {"incoming": [], "outgoing": [], "start_time": time.time()}
    call_metadata[call_id] = {
        "phone": phone, 
        "language_id": payload.get("language_id", 1),
        "voice": voice,
        "temperature": temperature,
        "speed": speed
    }
    await update_call_status(int(call_id), "pick")
    return {
        "call_id": call_id, 
        "voice": voice,
        "temperature": temperature,
        "speed": speed
    }


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """
    When Twilio makes a call, this endpoint is invoked.
    A unique call ID is generated and stored; then a TwiML response is returned that tells Twilio to stream media to /media-stream.
    """
    form = await request.form()
    caller_number = form.get("From")
    print("Call is coming from", caller_number)  
    call_id = await register_call(caller_number)
    call_id = str(call_id)
    print("call id received is", call_id, type(call_id))

    call_recordings[call_id] = {"incoming": [], "outgoing": [], "start_time": time.time()}
    
    # Default temperature and speed for incoming calls
    call_metadata[call_id] = {
        "phone": caller_number, 
        "language_id": 1,
        "voice": "echo",
        "temperature": 0.8,
        "speed": 1.05
    }
    
    response = VoiceResponse()
    # Supported languages: Arabic (default), Urdu, Hindi, Tamil, Tagalog - the AI will detect language automatically
    response.say("This call may be recorded for quality purposes.", voice='Polly.Danielle-Generative', language='en-US')
    response.pause(length=1)
    host = request.url.hostname

    connect = Connect()
    stream = Stream(url=f"wss://{host}/media-stream")
    stream.parameter(name="call_id", value=call_id)
    connect.append(stream)
    response.append(connect)

    return HTMLResponse(content=str(response), media_type="application/xml")

    

import wave
import audioop
import io
import base64
import websockets as ws_client
from fastapi import WebSocket

USER_AUDIO_DIR = "recordings/user"
AGENT_AUDIO_DIR = "recordings/agent"
os.makedirs(USER_AUDIO_DIR, exist_ok=True)
os.makedirs(AGENT_AUDIO_DIR, exist_ok=True)
import struct
import wave
import struct


last_agent_response_time = None  # will store timestamp of the last AI audio chunk

# Helper: Generate silence PCM of given duration (in seconds)
def generate_silence(duration_sec, sample_rate=8000):
    num_samples = int(duration_sec * sample_rate)
    silence_pcm = b'\x00\x00' * num_samples  # 16-bit PCM silence
    return silence_pcm


# Helper: Execute function calls from OpenAI
async def execute_function_call(func_name: str, func_args: dict) -> dict:
    """
    Execute the appropriate function based on the function name
    
    Args:
        func_name: Name of the function to execute
        func_args: Arguments to pass to the function
    
    Returns:
        dict: Function execution result
    """
    try:
        # Per-turn language + accent re-anchor (silent). Echoes a per-language
        # accent instruction back to the model so it never carries the default
        # Najdi accent into Urdu/Hindi/Tamil/Tagalog/English.
        if func_name == "set_response_language":
            result = get_language_accent_result(func_args.get("language", ""))
            print(f"🌐 set_response_language → {result['language']} ({result['language_name']})")
            return result

        # RAG Knowledge Base Search
        if func_name == "search_knowledge_base":
            return await search_knowledge_base(query=func_args.get("query", ""))
        
        # Card Activation & Verification Functions
        elif func_name == "verify_customer_by_cnic":
            return await verify_customer_by_cnic(cnic=func_args.get("cnic", ""))
        
        elif func_name == "confirm_physical_custody":
            return await confirm_physical_custody(
                cnic=func_args.get("cnic", ""),
                has_card=func_args.get("has_card", False)
            )
        
        elif func_name == "verify_tpin":
            return await verify_tpin(
                cnic=func_args.get("cnic", ""),
                tpin=func_args.get("tpin", "")
            )
        
        elif func_name == "verify_card_details":
            return await verify_card_details(
                cnic=func_args.get("cnic", ""),
                last_four_digits=func_args.get("last_four_digits", ""),
                expiry_date=func_args.get("expiry_date", "")
            )
        
        elif func_name == "activate_card":
            return await activate_card(cnic=func_args.get("cnic", ""))
        
        elif func_name == "update_customer_tpin":
            return await update_customer_tpin(
                cnic=func_args.get("cnic", ""),
                new_tpin=func_args.get("new_tpin", "")
            )
        
        elif func_name == "transfer_to_ivr_for_pin":
            return await transfer_to_ivr_for_pin()
        
        elif func_name == "transfer_to_agent":
            return await transfer_to_agent(
                cnic=func_args.get("cnic", ""),
                reason=func_args.get("reason", "")
            )
        
        elif func_name == "get_customer_status":
            return await get_customer_status(cnic=func_args.get("cnic", ""))
        
        else:
            return {
                "success": False,
                "error": f"Unknown function: {func_name}",
                "message": "Function not found in the system."
            }
    
    except Exception as e:
        print(f"❌ Error executing function {func_name}: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "message": f"An error occurred while executing {func_name}."
        }


# ---------------------------------------------------------------------------
# TEXT CHATBOT (text + voice-message input, text-only replies, reuses RAG)
# ---------------------------------------------------------------------------

CHAT_MODEL = "gpt-5.5-2026-04-23"
chat_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
MAX_TOOL_ITERATIONS = 3


def _sanitize_history(raw) -> list:
    """Keep only valid user/assistant text turns; trim length and count."""
    history = []
    if isinstance(raw, list):
        for m in raw:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                content = m.get("content", "")
                if isinstance(content, str) and content.strip():
                    history.append({"role": m["role"], "content": content[:4000]})
    return history[-20:]


async def run_chat(history: list) -> str:
    """
    Run a tool-calling chat turn with gpt-5.5 over the provided message history.
    The model may call search_knowledge_base (RAG); results are fed back until it
    produces a final text answer. Returns the assistant's text reply.
    """
    messages = [{"role": "system", "content": build_chat_system_message()}]
    messages.extend(history)

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await chat_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            tools=chat_tools,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            return msg.content or ""

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await execute_function_call(tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    # Tool budget exhausted — force a final text answer without tools.
    final = await chat_client.chat.completions.create(model=CHAT_MODEL, messages=messages)
    return final.choices[0].message.content or ""


@app.post("/chat")
async def chat_message(request: Request, payload: dict = Body(...)):
    """Text chat turn. Body: { messages: [{role, content}, ...] }. Requires JWT."""
    token = get_token_from_request(request)
    verify_jwt_token(token)

    history = _sanitize_history(payload.get("messages", []))
    if not history:
        raise HTTPException(status_code=400, detail="No messages provided")

    reply = await run_chat(history)
    return {"reply": reply}


@app.post("/chat/voice")
async def chat_voice(
    request: Request,
    audio: UploadFile = File(...),
    history: str = Form("[]"),
):
    """
    Voice-message chat turn. Transcribes the uploaded recording (Whisper), answers
    as text. Multipart: audio file + optional `history` JSON string. Requires JWT.
    """
    token = get_token_from_request(request)
    verify_jwt_token(token)

    try:
        parsed_history = json.loads(history)
    except json.JSONDecodeError:
        parsed_history = []
    conv = _sanitize_history(parsed_history)

    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    data = await audio.read()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        transcript = await transcribe_audio(tmp_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    transcript = (transcript or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="Could not transcribe audio")

    conv.append({"role": "user", "content": transcript})
    reply = await run_chat(conv)
    return {"transcript": transcript, "reply": reply}

@app.websocket("/media-stream-browser")
async def media_stream_browser(websocket: WebSocket):
    await websocket.accept()


    openai_url = 'wss://api.openai.com/v1/realtime?model=gpt-realtime-2'
    # GA Realtime API: no "OpenAI-Beta: realtime=v1" header (the beta shape is disabled).
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
    }

  
    async with ws_client.connect(openai_url, additional_headers=headers) as openai_ws:
        session_initialized = False
        call_id = None
        stream_sid = None

        user_pcm_buffer = io.BytesIO()
        agent_pcm_buffer = io.BytesIO()
        
        # Track function call state to prevent premature response cancellation
        function_call_completed_time = None
        FUNCTION_CALL_GRACE_PERIOD = 5.0  # seconds - allow response to start after function call (increased for RAG searches)
        
        # Track whether a response is currently being generated by OpenAI
        response_active = False

        async def receive_from_browser():
            nonlocal session_initialized, call_id, stream_sid
            try:
                async for msg in websocket.iter_text():
                    try:
                        data = json.loads(msg)

                        if data.get("event") == "start":
                            # Verify JWT token from WebSocket message
                            token = data["start"]["customParameters"].get("token")
                            if not token:
                                print("❌ No token provided in WebSocket connection")
                                await websocket.close(code=1008, reason="Authentication required")
                                return
                            
                            try:
                                user_data = verify_jwt_token(token)
                                print(f"✅ WebSocket authenticated for user: {user_data['username']}")
                            except HTTPException as e:
                                print(f"❌ Invalid token in WebSocket: {e.detail}")
                                await websocket.close(code=1008, reason="Invalid or expired token")
                                return
                            
                            call_id = data["start"]["customParameters"].get("call_id")
                            stream_sid = data["start"].get("streamSid", "browser-stream")
                            meta = call_metadata.get(call_id, {})
                            await initialize_session(openai_ws, call_id)
                            await send_initial_conversation_item(openai_ws)
                            session_initialized = True
                            continue

                        if data.get("event") == "media" and session_initialized:
                            payload_b64 = data["media"]["payload"]
                            pcm_bytes = base64.b64decode(payload_b64)
                            user_pcm_buffer.write(pcm_bytes)
                            
                            # Send all audio to OpenAI - let its VAD handle filtering
                            mulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(mulaw_bytes).decode('utf-8')
                            }
                            await openai_ws.send(json.dumps(audio_append))

                        if data.get("event") == "stop":
                            print(f"🛑 Browser sent stop event for call {call_id}")
                            break
                    
                    except json.JSONDecodeError as je:
                        print(f"⚠️ Failed to parse browser message: {je}")
                        continue
                    except Exception as inner_e:
                        print(f"⚠️ Error processing browser message: {inner_e}")
                        traceback.print_exc()
                        continue
                
                print(f"🔚 Browser WebSocket stream ended normally for call {call_id}")
                        
            except WebSocketDisconnect:
                print(f"🔌 Browser WebSocket disconnected for call {call_id}")
            except Exception as e:
                print(f"❌ Unexpected error in browser receive loop: {e}")
                traceback.print_exc()

        # Track RAG function call output item IDs - only delete when NEW RAG search is made
        # This preserves context for follow-up questions until new knowledge is fetched
        current_rag_items = []
        
        async def receive_from_openai_and_forward():
            nonlocal function_call_completed_time, response_active, current_rag_items
            
            try:
                async for raw in openai_ws:
                    try:
                        response = json.loads(raw)
                        rtype = response.get("type")
                        
                        # Debug: log all event types (except high-frequency audio deltas)
                        if rtype not in ["response.output_audio.delta", "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"]:
                            print(f"📨 OpenAI event: {rtype}")

                        # Handle error events from OpenAI - log and try to recover
                        if rtype == 'error':
                            error_info = response.get('error', {})
                            error_type = error_info.get('type', 'unknown')
                            error_message = error_info.get('message', 'Unknown error')
                            error_code = error_info.get('code', '')
                            print(f"❌ OpenAI Error - Type: {error_type}, Code: {error_code}, Message: {error_message}")
                            
                            # Don't spam the browser with non-critical errors like cancel failures
                            if error_code != 'response_cancel_not_active':
                                await websocket.send_json({
                                    "event": "error",
                                    "error_type": error_type,
                                    "message": error_message
                                })
                            
                            # If it's a rate limit or server error, wait briefly before continuing
                            if error_type in ['rate_limit_exceeded', 'server_error']:
                                print(f"⏳ Waiting 2 seconds before continuing after {error_type}...")
                                await asyncio.sleep(2)
                            continue
                        
                        # Handle rate limits - log warnings
                        if rtype == 'rate_limits.updated':
                            rate_limits = response.get('rate_limits', [])
                            for limit in rate_limits:
                                remaining = limit.get('remaining', 0)
                                limit_name = limit.get('name', 'unknown')
                                if remaining < 10:
                                    print(f"⚠️ Rate limit warning: {limit_name} has {remaining} remaining")
                            continue
                        
                        # Track when a response starts being created
                        if rtype == 'response.created':
                            response_active = True
                            continue
                        
                        # Handle response failures - try to recover by requesting new response
                        if rtype == 'response.failed':
                            response_active = False
                            function_call_completed_time = None
                            
                            # Check if it's a rate limit error
                            resp_obj = response.get("response", {})
                            status_details = resp_obj.get("status_details", {})
                            error_info = status_details.get("error", {})
                            error_code = error_info.get("code", "")
                            
                            if error_code == "rate_limit_exceeded":
                                print(f"⚠️ Rate limit hit - waiting 3 seconds before retry...")
                                await asyncio.sleep(3.0)
                                print("🔄 Retrying response after rate limit wait...")
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                            else:
                                print(f"❌ Response failed: {error_info.get('message', 'Unknown error')}")
                                # Wait briefly and try to recover
                                await asyncio.sleep(0.5)
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                            continue
                        
                        # Handle response cancellation
                        if rtype == 'response.cancelled':
                            print(f"ℹ️ Response was cancelled")
                            response_active = False
                            function_call_completed_time = None
                            continue

                        if rtype == 'input_audio_buffer.speech_started':
                            # Don't cancel response if we just completed a function call
                            # This prevents silencing responses after RAG searches
                            current_time = time.time()
                            if function_call_completed_time is not None:
                                time_since_function_call = current_time - function_call_completed_time
                                if time_since_function_call < FUNCTION_CALL_GRACE_PERIOD:
                                    print(f"⚠️ Ignoring interruption {time_since_function_call:.2f}s after function call (grace period: {FUNCTION_CALL_GRACE_PERIOD}s)")
                                    continue
                            
                            # Clear audio on browser side for interruption
                            # Note: OpenAI's VAD with interrupt_response:True handles the actual cancellation
                            # We just need to clear the browser's audio buffer
                            await websocket.send_json({ "event": "clear" })
                            continue
                        
                        # Clear function call flag when response completes (successfully or not)
                        if rtype == "response.done":
                            response_active = False
                            # Log detailed response info for debugging
                            resp_obj = response.get("response", {})
                            resp_status = resp_obj.get("status", "unknown")
                            resp_status_details = resp_obj.get("status_details", {})
                            resp_output = resp_obj.get("output", [])
                            print(f"📋 Response done - Status: {resp_status}, Outputs: {len(resp_output)}, Details: {resp_status_details}")
                            
                            # Log if response failed or was incomplete
                            if resp_status != "completed":
                                print(f"⚠️ Response not completed normally: {resp_status}")
                                if resp_status_details:
                                    print(f"   Status details: {resp_status_details}")
                            
                            # Don't delete RAG items here anymore - we preserve them
                            # They'll be deleted only when a NEW RAG search is made
                            # This ensures context is available for follow-up questions
                            pass
                            
                            if function_call_completed_time is not None:
                                print(f"✅ Response completed, clearing function call flag")
                                function_call_completed_time = None
                        
                        if rtype == "response.content.done":
                            if function_call_completed_time is not None:
                                print(f"✅ Content completed, clearing function call flag")
                                function_call_completed_time = None
                        
                        # Log confirmation when RAG items are deleted (for visibility into context savings)
                        if rtype == "conversation.item.deleted":
                            deleted_item_id = response.get("item_id", "unknown")
                            if deleted_item_id.startswith("rag_output_"):
                                print(f"🗑️ Confirmed: RAG item {deleted_item_id} removed from context window")
                            continue
                        
                        if rtype in LOG_EVENT_TYPES:
                            continue

                        if rtype == "response.output_audio.delta" and "delta" in response:
                            # Clear function call flag once we start receiving audio
                            # This means the response has started successfully
                            if function_call_completed_time is not None:
                                function_call_completed_time = None
                            
                            mulaw_b64 = response["delta"]
                            mulaw_bytes = base64.b64decode(mulaw_b64)

                            try:
                                pcm = audioop.ulaw2lin(mulaw_bytes, 2)
                            except Exception:
                                pcm = mulaw_bytes
                            
                            # Append to agent buffer for recording
                            agent_pcm_buffer.write(pcm)

                            # Send raw PCM data as base64 (no WAV wrapper)
                            # This prevents discontinuities from WAV headers
                            pcm_b64 = base64.b64encode(pcm).decode('utf-8')

                            out = {
                                "event": "media",
                                "media": {
                                    "payload": pcm_b64,
                                    "format": "raw_pcm",  # Indicate raw PCM format
                                    "sampleRate": 8000,  # g711_ulaw uses 8000 Hz
                                    "channels": 1,
                                    "bitDepth": 16
                                }
                            }
                            await websocket.send_json(out)

                        elif rtype == "response.function_call_arguments.done":
                            # Execute the function call and send result back to OpenAI
                            func_name = response.get("name")
                            call_id_internal = response.get("call_id")
                            func_args_str = response.get("arguments", "{}")
                            
                            try:
                                func_args = json.loads(func_args_str)
                            except json.JSONDecodeError:
                                func_args = {}
                            
                            print(f"🔧 Function call: {func_name} with args: {func_args}")
                            
                            # Execute the appropriate function with timeout protection
                            try:
                                # For RAG searches, delete OLD RAG items BEFORE adding new ones
                                # This preserves context until new knowledge is fetched
                                if func_name == "search_knowledge_base" and current_rag_items:
                                    print(f"🗑️ New RAG search requested - deleting {len(current_rag_items)} old RAG item(s) to make room...")
                                    for old_item_id in current_rag_items:
                                        delete_event = {
                                            "type": "conversation.item.delete",
                                            "item_id": old_item_id
                                        }
                                        try:
                                            await openai_ws.send(json.dumps(delete_event))
                                            print(f"   ✅ Deleted old RAG item: {old_item_id}")
                                        except Exception as del_err:
                                            print(f"   ⚠️ Failed to delete old RAG item {old_item_id}: {del_err}")
                                    current_rag_items.clear()
                                
                                result = await asyncio.wait_for(
                                    execute_function_call(func_name, func_args),
                                    timeout=30.0  # 30 second timeout for function calls
                                )
                            except asyncio.TimeoutError:
                                print(f"⚠️ Function call {func_name} timed out after 30 seconds")
                                result = {
                                    "success": False,
                                    "error": "timeout",
                                    "message": f"The operation timed out. Please try again."
                                }
                            
                            print(f"✅ Function result: {result}")
                            
                            # Generate custom item_id for RAG searches so we can delete them later
                            rag_item_id = None
                            if func_name == "search_knowledge_base":
                                rag_item_id = f"rag_output_{call_id_internal}_{int(time.time() * 1000)}"
                            
                            # Send result back to OpenAI
                            function_output = {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id_internal,
                                    "output": json.dumps(result)
                                }
                            }
                            # Add custom item_id if this is a RAG search
                            if rag_item_id:
                                function_output["item"]["id"] = rag_item_id
                                current_rag_items.append(rag_item_id)
                                print(f"📌 Tracking RAG item in context: {rag_item_id} (will persist until next RAG search)")
                            
                            await openai_ws.send(json.dumps(function_output))
                            
                            # Mark that we just completed a function call
                            # This prevents immediate cancellation of the response we're about to request
                            function_call_completed_time = time.time()
                            print(f"⏱️ Function call completed at {function_call_completed_time}, grace period: {FUNCTION_CALL_GRACE_PERIOD}s")
                            
                            # Request a new response from OpenAI
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                            
                            # Also send to browser for UI updates
                            outgoing_func_result = {
                                "event": "function_result", 
                                "name": func_name, 
                                "arguments": func_args_str,
                                "result": result
                            }
                            await websocket.send_json(outgoing_func_result)
                    
                    except json.JSONDecodeError as je:
                        print(f"⚠️ Failed to parse OpenAI message: {je}")
                        continue
                    except Exception as inner_e:
                        print(f"⚠️ Error processing OpenAI message: {inner_e}")
                        # Don't break the loop for non-critical errors
                        continue
                        
            except websockets.exceptions.ConnectionClosed as cc:
                print(f"❌ OpenAI WebSocket connection closed: code={cc.code}, reason={cc.reason}")
                # Notify browser that connection was lost
                try:
                    await websocket.send_json({
                        "event": "connection_error",
                        "message": "Connection to AI service was lost. Please refresh and try again."
                    })
                except:
                    pass
            except Exception as e:
                print(f"❌ Unexpected error in OpenAI receive loop: {e}")
                traceback.print_exc()
                # Notify browser about the error
                try:
                    await websocket.send_json({
                        "event": "error",
                        "message": "An unexpected error occurred. Please try again."
                    })
                except:
                    pass

        recv_task = asyncio.create_task(receive_from_browser())
        send_task = asyncio.create_task(receive_from_openai_and_forward())

        try:
            # Wait for either task to complete
            # When browser disconnects OR OpenAI connection ends, we'll exit
            done, pending = await asyncio.wait(
                [recv_task, send_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Log which task completed first
            for task in done:
                if task == recv_task:
                    print(f"🔚 Browser receive task completed for call {call_id}")
                elif task == send_task:
                    print(f"🔚 OpenAI send task completed for call {call_id}")
                
                # Check if the task had an exception
                if task.exception():
                    print(f"❌ Task exception: {task.exception()}")
            
            # Cancel pending tasks
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                    
        except Exception as e:
            print(f"❌ Error in main task loop: {e}")
            traceback.print_exc()
        finally:
            if not call_id:
                print("⚠️ No call_id; skipping recording, transcription, and analysis")
                try:
                    await websocket.close()
                except Exception:
                    pass
                return

            print(f"💾 Saving recordings for call {call_id}...")

            user_file_path = f"recordings/user/{call_id}_user.wav"
            agent_file_path = f"recordings/agent/{call_id}_agent.wav"

            def save_wav_file(path: str, pcm_data: bytes):
                with wave.open(path, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)   # 16-bit PCM
                    wf.setframerate(8000)
                    wf.writeframes(pcm_data)

            save_wav_file(user_file_path, user_pcm_buffer.getvalue())
            save_wav_file(agent_file_path, agent_pcm_buffer.getvalue())

            print(f"✅ Saved user audio: {user_file_path}")
            print(f"✅ Saved agent audio: {agent_file_path}")

            try:
                user_transcript = await transcribe_audio(user_file_path)
            except Exception as e:
                print(f"⚠️ Could not transcribe user audio: {e}")
                user_transcript = ""

            try:
                agent_transcript = await transcribe_audio(agent_file_path)
            except Exception as e:
                print(f"⚠️ Could not transcribe agent audio: {e}")
                agent_transcript = ""

            transcripts_output = {
                "call_id": call_id,
                "user_transcript": user_transcript,
                "agent_transcript": agent_transcript,
            }

            with open(f"recordings/{call_id}_transcript.json", "w", encoding="utf-8") as f:
                json.dump(transcripts_output, f, ensure_ascii=False, indent=2)
            print(f"📝 Transcript file written for call {call_id}")

            try:
                analysis_result = await analyze_call_with_llm(call_id, user_transcript, agent_transcript)
                print(f"📊 Call analysis complete: {analysis_result}")
            except Exception as e:
                print(f"⚠️ Call analysis failed: {e}")
                traceback.print_exc()

            await websocket.close()


async def send_initial_conversation_item(openai_ws):
    
    await openai_ws.send(json.dumps({"type": "response.create"}))

@app.get("/call-analysis/{call_id}")
async def get_call_analysis(call_id: str, request: Request):
    token = get_token_from_request(request)
    verify_jwt_token(token)

    analysis_file_path = f"recordings/analysis/{call_id}_analysis.json"
    
    if not os.path.exists(analysis_file_path):
        raise HTTPException(status_code=404, detail=f"Analysis not found for call_id: {call_id}")
    
    try:
        with open(analysis_file_path, "r", encoding="utf-8") as f:
            analysis_data = json.load(f)
        return analysis_data
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Error reading analysis file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving analysis: {str(e)}")

async def initialize_session(openai_ws, call_id):
    meta = call_metadata.get(call_id, {})
    instructions = meta.get("instructions", "")
    caller = meta.get("phone", "")
    voice = meta.get("voice", "echo")
    temperature = meta.get("temperature", 0.8)
    speed = meta.get("speed", 1.05)

    SYSTEM_MESSAGE = build_system_message(
        instructions=instructions,
        caller=caller,
        voice=voice
    )

    print(f"🔧 Initializing session with voice: {voice}, speed: {speed}x")

    # GA Realtime API session shape (gpt-realtime-2):
    # - audio formats are objects ({"type": "audio/pcmu"}) nested under audio.input/audio.output
    # - voice/speed live under audio.output, turn_detection under audio.input
    # - "modalities" -> "output_modalities"; "temperature" is no longer a session field
    # - gpt-realtime-2 is a reasoning model: reasoning.effort tunes latency vs quality
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": SYSTEM_MESSAGE,
            "output_modalities": ["audio"],
            "reasoning": {"effort": "medium"},
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},  # g711 u-law @ 8kHz
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.7,  # Speech detection sensitivity (0.0-1.0, lower = more sensitive)
                        "prefix_padding_ms": 500,  # Audio to include before speech starts
                        "silence_duration_ms": 1000,  # Silence duration before considering turn complete
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcmu"},  # g711 u-law @ 8kHz
                    "voice": voice,
                    "speed": speed,
                },
            },
            "tool_choice": "auto",
            "tools": function_call_tools,
        }
    }
    
    print(f"📤 Sending session update to OpenAI")
    await openai_ws.send(json.dumps(session_update))


@app.get("/available-voices")
async def get_available_voices(request: Request):
    """
    Returns the list of available voices with metadata.
    Requires valid JWT token.
    """
    # Verify JWT token
    token = get_token_from_request(request)
    user_data = verify_jwt_token(token)
    
    return {
        "voices": AVAILABLE_VOICES
    }


def create_jwt_token(username: str, full_name: str) -> str:
    """Create a JWT token for the user"""
    now = dt.now(timezone.utc)
    payload = {
        "username": username,
        "full_name": full_name,
        "exp": now + timedelta(hours=JWT_EXPIRATION_HOURS),
        "iat": now
    }
    token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return token


def verify_jwt_token(token: str) -> dict:
    """Verify and decode JWT token"""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_token_from_request(request: Request) -> str:
    """Extract JWT token from Authorization header"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    return auth_header.replace("Bearer ", "")


@app.post("/auth/login")
async def login(credentials: dict = Body(...)):
    """
    Authenticate user with username and password.
    Returns a JWT token on success. Same credentials can be used for multiple logins.
    """
    username = credentials.get("username", "").strip()
    password = credentials.get("password", "")
    
    # Check if user exists and password matches
    if username in USERS_DB:
        user = USERS_DB[username]
        if user["password"] == password:
            # Generate JWT token
            token = create_jwt_token(username, user["full_name"])
            
            return {
                "success": True,
                "message": "Login successful",
                "token": token,
                "user": {
                    "username": username,
                    "full_name": user["full_name"]
                }
            }
    
    # Invalid credentials
    raise HTTPException(status_code=401, detail="Invalid username or password")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
