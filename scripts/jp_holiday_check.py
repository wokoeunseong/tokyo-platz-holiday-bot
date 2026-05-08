#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

HOLIDAY_API_URL = "https://holidays-jp.github.io/api/v1/date.json"
WEATHER_API_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=35.6762&longitude=139.6503"
    "&current=temperature_2m,weather_code"
    "&timezone=Asia%2FTokyo"
)

WEATHER_CODES = {
    0: "☀️ 맑음",
    1: "🌤 대체로 맑음", 2: "⛅ 구름 조금", 3: "☁️ 흐림",
    45: "🌫 안개", 48: "🌫 안개",
    51: "🌦 이슬비", 53: "🌦 이슬비", 55: "🌦 이슬비",
    61: "🌧 비", 63: "🌧 비", 65: "🌧 강한 비",
    71: "🌨 눈", 73: "🌨 눈", 75: "🌨 강한 눈",
    80: "🌧 소나기", 81: "🌧 소나기", 82: "⛈ 강한 소나기",
    95: "⛈ 뇌우", 96: "⛈ 뇌우", 99: "⛈ 뇌우",
}

def get_today_jst():
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%d")

def fetch_holidays():
    req = urllib.request.Request(
        HOLIDAY_API_URL,
        headers={"User-Agent": "tokyoplatz-holiday-bot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read().decode("utf-8"))

def fetch_weather():
    req = urllib.request.Request(
        WEATHER_API_URL,
        headers={"User-Agent": "tokyoplatz-holiday-bot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        data = json.loads(res.read().decode("utf-8"))
    temp = round(data["current"]["temperature_2m"])
    code = data["current"]["weather_code"]
    desc = WEATHER_CODES.get(code, "🌡 날씨 정보 없음")
    return f"{desc} {temp}°C"

def check_holiday(date_str, holidays):
    return holidays.get(date_str)

def build_message(date_str, holiday_name, weather):
    if holiday_name:
        return (
            f"🎌 오늘({date_str})은 일본 공휴일입니다 — {holiday_name} — "
            f"도쿄플라츠커피 운영 여부를 확인해주세요. | 🗼 도쿄 날씨: {weather}"
        )
    return f"✅ 오늘({date_str})은 공휴일이 아닙니다 — 정상 영업일입니다. | 🗼 도쿄 날씨: {weather}"

def post_to_slack(webhook_url, message):
    data = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        if res.status != 200:
            raise RuntimeError(f"Webhook error: {res.status}")

def main():
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("ERROR: SLACK_WEBHOOK_URL 없음", file=sys.stderr)
        sys.exit(1)

    today = get_today_jst()

    try:
        holidays = fetch_holidays()
    except Exception as e:
        print(f"ERROR: 공휴일 API 실패 → {e}", file=sys.stderr)
        sys.exit(1)

    try:
        weather = fetch_weather()
    except Exception as e:
        print(f"WARNING: 날씨 API 실패 → {e}", file=sys.stderr)
        weather = "날씨 정보 없음"

    holiday_name = check_holiday(today, holidays)
    message = build_message(today, holiday_name, weather)

    try:
        post_to_slack(webhook_url, message)
    except Exception as e:
        print(f"ERROR: Slack 전송 실패 → {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[{today}] {'🎌 ' + holiday_name if holiday_name else '✅ 평일'} | {weather}")

if __name__ == "__main__":
    main()
