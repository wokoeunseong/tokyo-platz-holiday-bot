#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

HOLIDAY_API_URL = "https://holidays-jp.github.io/api/v1/date.json"

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

def check_holiday(date_str, holidays):
    return holidays.get(date_str)

def build_message(date_str, holiday_name):
    if holiday_name:
        return f"🎌 오늘({date_str})은 일본 공휴일입니다 — {holiday_name} — 도쿄플라츠커피 운영 여부를 확인해주세요."
    return f"✅ 오늘({date_str})은 공휴일이 아닙니다 — 정상 영업일입니다."

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
        print(f"ERROR: API 실패 → {e}", file=sys.stderr)
        sys.exit(1)

    holiday_name = check_holiday(today, holidays)
    message = build_message(today, holiday_name)

    try:
        post_to_slack(webhook_url, message)
    except Exception as e:
        print(f"ERROR: Slack 전송 실패 → {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[{today}] {'🎌 ' + holiday_name if holiday_name else '✅ 평일'}")

if __name__ == "__main__":
    main()
