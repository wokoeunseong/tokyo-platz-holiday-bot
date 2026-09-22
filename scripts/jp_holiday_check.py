#!/usr/bin/env python3
"""도쿄 날씨·공휴일 한국어 브리핑. 표준 라이브러리만 사용."""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
import jp_ko

JST = timezone(timedelta(hours=9))
HOLIDAY_API_URL = "https://holidays-jp.github.io/api/v1/date.json"
WEATHER_API_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=35.6762&longitude=139.6503"
    "&current=temperature_2m,apparent_temperature,weather_code"
    "&hourly=precipitation_probability,precipitation"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
    "precipitation_probability_max,precipitation_sum,wind_gusts_10m_max"
    "&timezone=Asia%2FTokyo&wind_speed_unit=ms&forecast_days=2"
)
WEATHER_CODES = {
    0:"맑음", 1:"대체로 맑음", 2:"구름 조금", 3:"흐림", 45:"안개", 48:"안개",
    51:"이슬비",53:"이슬비",55:"강한 이슬비",56:"어는 이슬비",57:"강한 어는 이슬비",
    61:"비",63:"비",65:"강한 비",66:"어는 비",67:"강한 어는 비",
    71:"눈",73:"눈",75:"강한 눈",77:"싸락눈",80:"소나기",81:"소나기",
    82:"강한 소나기",85:"눈 소나기",86:"강한 눈 소나기",95:"뇌우",96:"우박 동반 뇌우",99:"강한 우박 동반 뇌우",
}
HOLIDAYS = {
    "元日":"새해 첫날", "成人の日":"성년의 날", "建国記念の日":"건국기념일",
    "天皇誕生日":"천황 탄생일", "春分の日":"춘분의 날", "昭和の日":"쇼와의 날",
    "憲法記念日":"헌법기념일", "みどりの日":"녹색의 날", "こどもの日":"어린이날",
    "海の日":"바다의 날", "山の日":"산의 날", "敬老の日":"경로의 날",
    "秋分の日":"추분의 날", "スポーツの日":"스포츠의 날", "体育の日":"체육의 날",
    "文化の日":"문화의 날", "勤労感謝の日":"근로감사의 날", "振替休日":"대체휴일",
    "国民の休日":"국민의 휴일", "休日":"휴일", "即位の日":"즉위일",
    "天皇の即位の日":"천황 즉위일", "即位礼正殿の儀":"즉위식",
}

def holiday_ko(name):
    if name in HOLIDAYS:
        return HOLIDAYS[name]
    if "振替休日" in name:
        return "대체휴일"
    return jp_ko.korean_only(name, "공휴일 명칭 확인 필요")

def get_today_jst():
    return datetime.now(JST).strftime("%Y-%m-%d")

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent":"tokyoplatz-holiday-bot/2.0"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode("utf-8"))

def fetch_holidays():
    return fetch_json(HOLIDAY_API_URL)

def number(value, unit="", digits=0):
    if not isinstance(value, (int, float)):
        return "확인 불가"
    return f"{value:.{digits}f}".rstrip('0').rstrip('.') + unit if digits else f"{value:.0f}{unit}"

def fetch_weather():
    data = fetch_json(WEATHER_API_URL)
    current = data.get("current", {})
    daily = data.get("daily", {})
    hourly = data.get("hourly", {})
    dates = daily.get("time", [])
    def day_value(key, i):
        values = daily.get(key, [])
        return values[i] if i < len(values) else None
    lines = [
        "🌤 오늘의 도쿄 날씨",
        f"• {WEATHER_CODES.get(current.get('weather_code'), '날씨 상태 확인 불가')}, 현재 {number(current.get('temperature_2m'), '℃')}",
        f"• 최고 {number(day_value('temperature_2m_max',0),'℃')} / 최저 {number(day_value('temperature_2m_min',0),'℃')}",
        f"• 체감 {number(current.get('apparent_temperature'),'℃')} / 오늘 최대 돌풍 {number(day_value('wind_gusts_10m_max',0),'m/s',1)}",
        "", "☔ 오늘 시간대별 비 예보",
    ]
    for label, start, end in [("아침 06~10시",6,10),("점심 11~14시",11,14),("저녁 17~21시",17,21)]:
        indices = [i for i,t in enumerate(hourly.get('time',[]))
                   if dates and t[:10] == dates[0] and start <= int(t[11:13]) <= end]
        def values(key):
            source = hourly.get(key,[])
            return [source[i] if i < len(source) else None for i in indices]
        probs, rain = values('precipitation_probability'), values('precipitation')
        complete = lambda vs: bool(vs) and all(isinstance(v,(int,float)) for v in vs)
        prob = number(max(probs),'%') if complete(probs) else "확인 불가"
        amount = number(sum(rain),'mm',1) if complete(rain) else "확인 불가"
        lines.append(f"• {label}: 강수 확률 {prob}, 예상 강수량 {amount}")
    lines.extend(["※ 확률은 시간대 내 최댓값, 강수량은 해당 시간에 끝나는 1시간 구간의 합계입니다.", "", "📆 내일 도쿄 날씨"])
    if len(dates)>1:
        lines.extend([
            f"• {dates[1]} — {WEATHER_CODES.get(day_value('weather_code',1),'날씨 상태 확인 불가')}",
            f"• 최고 {number(day_value('temperature_2m_max',1),'℃')} / 최저 {number(day_value('temperature_2m_min',1),'℃')}",
            f"• 강수 확률 {number(day_value('precipitation_probability_max',1),'%')} / 예상 강수량 {number(day_value('precipitation_sum',1),'mm',1)}",
        ])
    else:
        lines.append("• 예보 확인 불가")
    lines.extend(["", "※ 도쿄 대표 지점 예보이며 매장별 실제 날씨와 다를 수 있습니다."])
    return "\n".join(lines)

def check_holiday(date_str, holidays):
    return holidays.get(date_str)

def build_message(date_str, holiday_name, weather, holidays=None):
    base = datetime.strptime(date_str, "%Y-%m-%d").date()
    weekday = "월화수목금토일"[base.weekday()]
    lines = ["🇯🇵 도쿄 매장 일일 브리핑",f"{date_str} ({weekday}) · 한국·일본 시간", "", weather, "", "📅 공휴일 안내"]
    if holiday_name:
        lines.append(f"• 오늘: {holiday_ko(holiday_name)} — 매장별 운영 일정을 확인해 주세요.")
    elif holidays is not None:
        lines.append("• 오늘: 일본 공휴일이 아닙니다.")
    else:
        lines.append("• 오늘 공휴일 여부: 확인 불가")
    if holidays is not None:
        upcoming = []
        for day, name in sorted(holidays.items()):
            delta = (datetime.strptime(day,'%Y-%m-%d').date()-base).days
            if 0 < delta <= 14:
                upcoming.append(f"• {day} ({delta}일 뒤): {holiday_ko(name)}")
        lines.append("다가오는 14일 공휴일")
        lines.extend(upcoming or ["• 예정된 공휴일 없음"])
    else:
        lines.append("• 다가오는 공휴일: 확인 불가")
    lines.extend(["", "출처: 오픈메테오 · 일본 공휴일 데이터"])
    return jp_ko.korean_only("\n".join(lines))

def post_to_slack(webhook_url, message):
    data=json.dumps({"message":message},ensure_ascii=False).encode('utf-8')
    req=urllib.request.Request(webhook_url,data=data,headers={"Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=15) as res:
        if res.status != 200:
            raise RuntimeError(f"웹훅 응답 오류: {res.status}")

def main():
    webhook_url=os.environ.get('SLACK_WEBHOOK_URL','').strip()
    if not webhook_url:
        print('ERROR: SLACK_WEBHOOK_URL 없음',file=sys.stderr); sys.exit(1)
    today=get_today_jst()
    try:
        holidays=fetch_holidays()
    except Exception as e:
        print(f'WARNING: 공휴일 API 실패: {type(e).__name__}',file=sys.stderr)
        holidays=None
    try:
        weather=fetch_weather()
    except Exception as e:
        print(f'WARNING: 날씨 API 실패: {type(e).__name__}',file=sys.stderr)
        weather='🌤 오늘·내일 날씨: 확인 불가 (날씨 제공처 응답 오류)'
    message=build_message(today,check_holiday(today,holidays or {}),weather,holidays)
    try:
        post_to_slack(webhook_url,message)
    except Exception as e:
        print(f'ERROR: Slack 전송 실패: {type(e).__name__}',file=sys.stderr); sys.exit(1)
    print(f'[{today}] 한국어 날씨·공휴일 브리핑 전송 완료')

if __name__=='__main__': main()
