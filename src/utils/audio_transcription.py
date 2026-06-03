from openai import AsyncOpenAI
from dotenv import load_dotenv
import os
import json

load_dotenv(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

async def transcribe_audio(file_path: str):
    with open(file_path, "rb") as audio_file:
        transcription = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            prompt=(
                "This is an Al Fardan Exchange customer-service conversation. The speaker may use "
                "Arabic, English, Urdu, Hindi, Tamil, or Tagalog/Filipino, and may switch language "
                "at any time. Transcribe VERBATIM in the exact language actually spoken and in its "
                "native script — do NOT translate and do NOT romanize: write Arabic in Arabic script, "
                "Urdu in Urdu script, Hindi in Devanagari, Tamil in Tamil script, English/Tagalog in "
                "Latin script. Keep proper nouns like 'Al Fardan Exchange' as-is."
            ),
        )
    return transcription.text


async def analyze_call_with_llm(call_id: str, user_transcript: str, agent_transcript: str):
    combined_transcripts = f"""
[AGENT TRANSCRIPT]
{agent_transcript.strip()}

[USER TRANSCRIPT]
{user_transcript.strip()}
"""

    system_prompt = """
You are a professional call quality analysis system for Al Fardan Exchange contact center (UAE).

The conversation is provided in two separate blocks:
- Agent transcript: utterances spoken by the exchange agent.
- User transcript: utterances spoken by the customer.

These transcripts may not be in perfect alternating order. Your first task is to:
1. Reconstruct the conversation in the correct chronological sequence of turns.
2. Clearly label each turn as either "AGENT" or "USER".
3. Make sure questions and answers are logically paired.

Once the conversation is reconstructed, analyze it and return a STRICT JSON object with the following structure:
- All fields with <score> tag should have values in percentages ranging from 0 - 100%.

{
  "core_performance": {
    "intent_recognition_accuracy": "<score>",
    "entity_extraction_accuracy": "<score>",
    "task_completion_rate": "<score>",
    "fallback_rate": "<score>",
    "branch_logic_accuracy": "<score>"
  },
  "technical_performance": {
    "response_latency": "<score>",
    "transcription_accuracy": "<score>",
    "speech_clarity": "<score>"
  },
  "conversational_quality": {
    "interrupt_handling": "<score>",
    "turn_taking_management": "<score>",
    "context_retention": "<score>",
    "tone_appropriateness": "<score>",
    "accent_understanding": "<score>",
    "disfluency_handling": "<score>"
  },
  "compliance_and_ux": {
    "ai_disclosure": "<yes/no>",
    "empathy_score": "<score>",
    "confusion_rate": "<score>"
  },
  "summary": "<3-4 line summary of the call highlighting the key points and quality>"
}

Return ONLY valid JSON. Do not include explanations or any text outside of the JSON object.
"""

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": combined_transcripts},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    if response.usage:
        print(f"📊 Call Analysis Tokens — Input: {response.usage.prompt_tokens}, Output: {response.usage.completion_tokens}, Total: {response.usage.total_tokens}")

    content = response.choices[0].message.content

    try:
        parsed_json = json.loads(content)
    except json.JSONDecodeError:
        parsed_json = {"error": "Failed to parse LLM output", "raw": content}

    os.makedirs("recordings/analysis", exist_ok=True)
    with open(f"recordings/analysis/{call_id}_analysis.json", "w", encoding="utf-8") as f:
        json.dump(parsed_json, f, ensure_ascii=False, indent=2)

    return parsed_json
