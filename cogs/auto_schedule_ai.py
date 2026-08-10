"""
Gemini-backed extraction of Chief-Minister-style scheduling requests from
free-text Discord messages. This is the only place in the Auto Schedule
pipeline that uses an LLM -- the actual slot allocation
(cogs/auto_schedule.py: allocate_slots) is pure deterministic Python, so a
parsing mistake here can produce a bad *candidate* for one person but can
never cause a double-booked slot.

Uses the free tier of Google's Gemini API (aistudio.google.com) -- Claude
Pro doesn't grant API access, so this isn't a Claude integration.
"""
import asyncio
import json
import logging
import os

logger = logging.getLogger('bot')

GEMINI_MODEL = "gemini-flash-latest"

_PROMPT_TEMPLATE = """You are extracting Appointment time-slot scheduling requests for a mobile strategy game alliance, from a batch of Discord messages. Each message is one governor's request, possibly in any language, in inconsistent formats.

For each message that is a real scheduling request, extract:
- "name": the person's name, as they stated it.
- "speedup_hours": how many HOURS of speedup/boost time they said they have, as a number. If they gave it in days, multiply by 24 (e.g. "30 days" -> 720). If they gave it in hours already, use it as-is.
- "preferred_windows": a list of {{"start": "HH:MM", "end": "HH:MM"}} objects in 24-hour UTC time, one entry per distinct preferred time they mentioned:
  - If they gave a specific single time (e.g. "UTC12:00"), use it as both start and end (a narrow window).
  - If they gave a range (e.g. "6 UTC to 22 UTC"), use one window covering that whole range.
  - If they said something meaning "any time" / "fully flexible" / gave the full day (e.g. "00:00 - 23:59"), use a single window {{"start": "00:00", "end": "23:59"}}.
  - If they listed multiple preferred times/ranges (e.g. "1st preferred", "2nd preferred"), list them in that preference order.
  - If they mentioned no time preference at all, use an empty list.

Skip any message that is not actually a scheduling request (off-topic chatter, questions, etc.) -- do not include an entry for it.

Respond with ONLY a JSON array, no other text, matching this exact shape:
[{{"name": "string", "speedup_hours": number, "preferred_windows": [{{"start": "HH:MM", "end": "HH:MM"}}]}}]

Messages (one per line, prefixed with its index):
{messages}
"""


def _build_prompt(messages: list) -> str:
    numbered = "\n".join(f"[{i}] {text}" for i, text in enumerate(messages))
    return _PROMPT_TEMPLATE.format(messages=numbered)


def _parse_response_text(text: str) -> list:
    """Turns Gemini's raw response text into a cleaned list of
    {"name", "speedup_hours", "preferred_windows"} dicts. Never raises --
    logs and returns [] on anything unparseable, so one malformed API
    response degrades to "0 people parsed" rather than crashing the whole
    Generate Schedule flow."""
    if not text:
        return []
    text = text.strip()
    # Defensive: strip a ```json ... ``` fence if the model added one despite
    # response_mime_type=application/json (seen occasionally in practice).
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Auto Schedule: Gemini response was not valid JSON: {e}")
        return []

    if not isinstance(data, list):
        logger.error(f"Auto Schedule: Gemini response was valid JSON but not a list: {type(data).__name__}")
        return []

    cleaned = []
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        name = str(entry["name"]).strip()
        if not name:
            continue

        try:
            speedup_hours = float(entry.get("speedup_hours") or 0)
        except (TypeError, ValueError):
            speedup_hours = 0.0

        windows = []
        for w in entry.get("preferred_windows") or []:
            if isinstance(w, dict) and w.get("start") and w.get("end"):
                windows.append({"start": str(w["start"]), "end": str(w["end"])})

        cleaned.append({"name": name, "speedup_hours": speedup_hours, "preferred_windows": windows})

    return cleaned


def _call_gemini_sync(messages: list, api_key: str) -> list:
    """Synchronous call (the google-genai SDK is sync) -- must be run via
    asyncio.to_thread by the caller, same pattern as the portal's blocking
    DB calls in web/db.py."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=_build_prompt(messages),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
        ),
    )
    return _parse_response_text(response.text)


async def parse_schedule_requests(messages: list) -> list:
    """messages: list of raw message content strings (one per Discord
    message). Returns a cleaned list of {"name", "speedup_hours",
    "preferred_windows"} dicts -- the exact shape allocate_slots in
    cogs/auto_schedule.py expects.

    Raises RuntimeError with a clear, admin-facing message if GEMINI_API_KEY
    isn't set (checked eagerly, before spinning up a thread) or if the API
    call itself fails (network error, invalid key, etc.) -- the caller is
    expected to catch this and show it to the admin rather than assignment
    silently producing an empty schedule."""
    if not messages:
        return []

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey "
            "and set it as an environment variable for the bot."
        )

    try:
        return await asyncio.to_thread(_call_gemini_sync, messages, api_key)
    except Exception as e:
        logger.error(f"Auto Schedule: Gemini request failed: {e}")
        raise RuntimeError(f"Gemini request failed: {e}") from e
