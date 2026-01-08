import json
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

SHEET_URL = os.environ["SHEET_WEBAPP_URL"]
SHEET_SECRET = os.environ["SHEET_SECRET"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

client = OpenAI(api_key=OPENAI_API_KEY)

STATE_FILE = "state.json"


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"last_timestamp": ""}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def fetch_recent_raw(limit: int = 50) -> list[dict]:
    r = requests.get(
        SHEET_URL,
        params={"action": "get_raw", "secret": SHEET_SECRET, "limit": str(limit)},
        timeout=20,
    )
    print(r.status_code)
    print(r._content)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Unknown sheet error"))
    return data.get("items", [])


def append_parsed(timestamp: str, user_id: str, raw_text: str, parsed_json: str, status: str) -> None:
    payload = {
        "action": "append_parsed",
        "secret": SHEET_SECRET,
        "timestamp": timestamp,
        "user_id": user_id,
        "raw_text": raw_text,
        "parsed_json": parsed_json,
        "status": status,
    }
    r = requests.post(SHEET_URL, json=payload, timeout=20, allow_redirects=True)
    r.raise_for_status()


SYSTEM_INSTRUCTIONS = """You are a fitness log parser. Output JSON only. Do not add any entries.

Return this schema exactly:
{
  "workout": {
    "date": "YYYY-MM-DD",
    "type": "strength|rehab|board|bouldering|lead|mixed|other",
    "duration_min": number|null,
    "location": string|null,
    "session_notes": string|null
  },
  "activities": [
    {
      "exercise": string,
      "set_number": number|null,
      "weight": number|null,
      "reps": number|null,
      "rest_sec": number|null,
      "notes": string|null
    }
  ],
  "status": "ok|needs_review",
  "questions": [string]
}

Rules:
- Do not invent exercises or numbers.
- If unsure about key fields (date/type/sets), set status=needs_review and ask 1-3 questions.
- activities can be empty (e.g., climbing session description).
"""


def parse_with_llm(raw_text: str, default_date_iso: str) -> tuple[str, str]:
    # We use Responses API; output_text should be JSON text. :contentReference[oaicite:1]{index=1}
    resp = client.responses.create(
        model="gpt-5-nano",
        reasoning={"effort": "low"},
        instructions=SYSTEM_INSTRUCTIONS,
        input=f"Default date (if not specified): {default_date_iso}\n\nRaw log:\n{raw_text}",
    )
    text = resp.output_text.strip()
    # Validate JSON:
    obj = json.loads(text)
    status = obj.get("status", "needs_review")
    return json.dumps(obj, ensure_ascii=False), status


def iso_now_date_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()

#counts how many dates to account for multiple sessions in a day
def get_workouts_count_for_date(date_iso: str) -> int:
    r = requests.get(
        SHEET_URL,
        params={"action": "get_workouts_by_date", "secret": SHEET_SECRET, "date": date_iso},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Unknown sheet error"))
    return int(data.get("count", 0))

#append the workout log on a session level
def append_workout_row(workout_id: str, w: dict):
    payload = {
        "action": "append_workout",
        "secret": SHEET_SECRET,
        "workout_id": workout_id,
        "date": w.get("date"),
        "type": w.get("type"),
        "duration_min": w.get("duration_min"),
        "location": w.get("location"),
        "session_notes": w.get("session_notes"),
    }
    r = requests.post(SHEET_URL, json=payload, timeout=20, allow_redirects=True)
    r.raise_for_status()

#append individual activities on a exercise level
def append_activity_rows(workout_id: str, date_iso: str, activities: list[dict]):
    rows = []
    for a in activities:
        rows.append({
            "workout_id": workout_id,
            "date": date_iso,
            "exercise": a.get("exercise"),
            "exercise_id": None,  # keep blank for now
            "weight": a.get("weight"),
            "reps": a.get("reps"),
            "rest_sec": a.get("rest_sec"),
            "hold_sec": a.get("hold_sec"),
            "notes": a.get("notes"),
            "set_number": a.get("set_number"),
        })
    payload = {"action": "append_activities", "secret": SHEET_SECRET, "rows": rows}
    r = requests.post(SHEET_URL, json=payload, timeout=20, allow_redirects=True)
    r.raise_for_status()

#make the id
def make_workout_id(date_iso: str) -> str:
    yyyymmdd = date_iso.replace("-", "")
    count = get_workouts_count_for_date(date_iso)
    serial = count + 1
    return f"{yyyymmdd}-{serial:03d}"

def main():
    state = load_state()
    print("loading sate")
    last_ts = state.get("last_timestamp", "")
    print(last_ts)

    rows = fetch_recent_raw(limit=100)

    # Process in chronological order
    rows_sorted = sorted(rows, key=lambda r: r.get("timestamp", ""))

    processed_any = False
    for r in rows_sorted:
        print("processing row number", r)
        ts = r.get("timestamp", "")
        if not ts:
            continue
        if last_ts and ts <= last_ts:
            continue

        user_id = r.get("user_id", "")
        raw_text = r.get("raw_text", "")

        try:
            parsed_json, status = parse_with_llm(raw_text, default_date_iso=iso_now_date_utc())
            append_parsed(ts, user_id, raw_text, parsed_json, status)
        except Exception as e:
            # Log failure into parsed_logs too
            err_payload = json.dumps({"error": str(e)}, ensure_ascii=False)
            append_parsed(ts, user_id, raw_text, err_payload, "error")

        last_ts = ts
        processed_any = True

    if processed_any:
        save_state({"last_timestamp": last_ts})
        print("Done. Last processed timestamp:", last_ts)
    else:
        print("Nothing new to process.")

    #once json is parsed, post into the relevant sheets
    obj = json.loads(parsed_json)
    status = obj.get("status", "needs_review")
    if status == "ok":
        w = obj["workout"]
        date_iso = w["date"]
        workout_id = make_workout_id(date_iso)

        append_workout_row(workout_id, w)
        append_activity_rows(workout_id, date_iso, obj.get("activities", []))

        # optional: also append parsed row including workout_id (if you added the column)


if __name__ == "__main__":
    main()

# TODO: expand 3x sets deterministically (see chat)
