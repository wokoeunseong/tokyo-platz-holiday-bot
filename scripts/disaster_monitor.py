#!/usr/bin/env python3
"""
disaster_monitor.py
일본 자연재해 모니터링 → Slack 알림

대응 재해:
  - 지진 (P2PQuake JSON API v2)
  - 쓰나미 (P2PQuake JSON API v2)
  - 태풍 (기상청 JMAXML 피드)
  - 대우 특별경보 (기상청 JMAXML 피드)

동작 모드 (환경변수 MONITOR_MODE):
  - "daily"  : 매일 정시 요약 (전24시간 지진 + 태풍 현황)
  - "urgent" : 긴급 감시 (임계값 초과 시만 전송, 이하면 무음)

필수 환경변수:
  - SLACK_WEBHOOK_URL : Slack Workflow Builder 웹훅 URL
  - MONITOR_MODE      : "daily" or "urgent"
"""

import json
import os
import sys
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── 상수 ──────────────────────────────────────────────
JST = timezone(timedelta(hours=9))

# P2PQuake API v2
QUAKE_API   = "https://api.p2pquake.net/v2/jma/quake?limit=20&order=-1"
TSUNAMI_API = "https://api.p2pquake.net/v2/jma/tsunami?limit=5&order=-1"

# 기상청 JMAXML 배신 피드
JMA_FEED_EXTRA = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"

# ── 긴급 알림 임계값 ──────────────────────────────────
# maxScale: JMA 진도 스케일
#   10=진도1, 20=진도2, 30=진도3, 40=진도4
#   45=진도5약, 50=진도5강, 55=진도6약, 60=진도6강, 70=진도7
URGENT_MAX_SCALE    = 30    # 진도3 이상에서 긴급 알림
URGENT_MAGNITUDE    = 4.5   # M4.5 이상에서 긴급 알림
DAILY_MIN_SCALE     = 20    # 진도2 이상을 일간 요약에 포함
DAILY_MIN_MAGNITUDE = 3.0   # M3.0 이상을 일간 요약에 포함

# 도쿄·아오야마 인근 광역 (지진 관측점 필터용)
TOKYO_AREA_PREFS = ["東京都", "神奈川県", "埼玉県", "千葉県"]


# ── 유틸리티 ──────────────────────────────────────────

def fetch_json(url: str) -> list | dict | None:
    """JSON 취득. 실패 시 None 반환."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "disaster-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] fetch_json 실패: {url} → {e}", file=sys.stderr)
        return None


def fetch_xml_text(url: str) -> str | None:
    """XML 텍스트 취득. 실패 시 None 반환."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "disaster-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.read().decode("utf-8")
    except Exception as e:
        print(f"[WARN] fetch_xml 실패: {url} → {e}", file=sys.stderr)
        return None


def post_to_slack(webhook_url: str, message: str) -> None:
    """Slack Workflow Builder 웹훅으로 전송.
    Workflow Builder 변수명: message (텍스트 타입)
    """
    payload = {"message": message}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        if res.status != 200:
            raise RuntimeError(f"Slack 웹훅 오류: {res.status}")
    print("[OK] Slack 전송 완료")


def scale_to_shindo(scale: int) -> str:
    """P2PQuake maxScale → 한국어 진도 표기"""
    table = {
        10: "진도1", 20: "진도2", 30: "진도3", 40: "진도4",
        45: "진도5약", 50: "진도5강", 55: "진도6약", 60: "진도6강", 70: "진도7",
        -1: "불명",
    }
    return table.get(scale, f"진도?({scale})")


def tsunami_grade_label(grade: str) -> str:
    """쓰나미 예보 등급 → 한국어 라벨"""
    labels = {
        "MajorWarning": "🚨 대쓰나미경보",
        "Warning":      "⚠️ 쓰나미경보",
        "Watch":        "🔔 쓰나미주의보",
        "Unknown":      "정보없음",
        "None":         "쓰나미 우려 없음",
    }
    return labels.get(grade, grade)


def is_within_hours(time_str: str, hours: float) -> bool:
    """P2PQuake time 필드 (JST 'YYYY/MM/DD HH:MM:SS')가
    현재 시각으로부터 지정 시간 이내인지 확인"""
    try:
        dt = datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")
        dt_jst = dt.replace(tzinfo=JST)
        now_jst = datetime.now(JST)
        return (now_jst - dt_jst) <= timedelta(hours=hours)
    except Exception:
        return False


# ── 지진 정보 처리 ────────────────────────────────────

def format_quake(q: dict, include_points: bool = False) -> str:
    """지진 1건을 Slack 표시용으로 포맷"""
    eq  = q.get("earthquake", {})
    hyp = eq.get("hypocenter", {})
    name      = hyp.get("name", "불명")
    magnitude = hyp.get("magnitude", -1)
    depth     = hyp.get("depth", -1)
    max_scale = eq.get("maxScale", -1)
    tsunami   = eq.get("domesticTsunami", "Unknown")
    time_str  = eq.get("time", "")

    mag_str  = f"M{magnitude}" if magnitude != -1 else "M불명"
    dep_str  = "극천발" if depth == 0 else f"깊이 {depth}km" if depth > 0 else "깊이 불명"
    shindo   = scale_to_shindo(max_scale)
    tsun_str = tsunami_grade_label(tsunami)

    line = f"• {time_str}  {name}  {mag_str} / {dep_str} / 최대 {shindo}  {tsun_str}"

    # 도쿄 인근 관측점이 있으면 추가
    if include_points:
        points = q.get("points", [])
        tokyo_pts = [
            p for p in points
            if any(pref in p.get("pref", "") for pref in TOKYO_AREA_PREFS)
        ]
        if tokyo_pts:
            p_strs = [
                f"{p.get('addr','?')}:{scale_to_shindo(p.get('scale',-1))}"
                for p in tokyo_pts[:3]
            ]
            line += f"\n  └ 도쿄 인근: {', '.join(p_strs)}"

    return line


# ── 쓰나미 정보 처리 ──────────────────────────────────

def get_active_tsunami() -> list[dict]:
    """현재 유효한 쓰나미 경보·주의보 취득"""
    data = fetch_json(TSUNAMI_API)
    if not data:
        return []

    active = []
    for item in data:
        if item.get("cancelled", False):
            continue
        issue_time = item.get("time", "")
        if not is_within_hours(issue_time, 24):
            continue
        areas = item.get("areas", [])
        serious = [
            a for a in areas
            if a.get("grade") in ("MajorWarning", "Warning", "Watch")
        ]
        if serious:
            active.append({"time": issue_time, "areas": serious})

    return active


# ── 태풍·대우 정보 처리 (기상청 JMAXML 피드) ──────────

def parse_jma_feed(feed_url: str, hours: int = 24) -> dict:
    """
    기상청 Atom 피드를 파싱해서
    {
      'typhoons': [제목 문자열, ...],
      'heavy_rain_warnings': [제목 문자열, ...],
    }
    반환.
    """
    xml_str = fetch_xml_text(feed_url)
    if not xml_str:
        return {"typhoons": [], "heavy_rain_warnings": []}

    entries = re.findall(r'<entry>(.*?)</entry>', xml_str, re.DOTALL)

    now_jst = datetime.now(JST)
    typhoons   = []
    heavy_rain = []

    for entry in entries:
        # 갱신 시각 확인
        m_updated = re.search(r'<updated>(.*?)</updated>', entry)
        if m_updated:
            try:
                updated_str = m_updated.group(1).strip()
                updated_str_clean = re.sub(r'([+-]\d{2}):(\d{2})$', r'\1\2', updated_str)
                dt = datetime.strptime(updated_str_clean, "%Y-%m-%dT%H:%M:%S%z")
                if (now_jst - dt) > timedelta(hours=hours):
                    continue
            except Exception:
                pass

        m_title = re.search(r'<title>(.*?)</title>', entry)
        if not m_title:
            continue
        title = m_title.group(1).strip()

        # 태풍 정보
        if re.search(r'台風|熱帯低気圧', title):
            if title not in typhoons:
                typhoons.append(title)

        # 대우·홍수 특별경보
        if re.search(r'大雨特別警報|洪水特別警報|氾濫特別警報', title):
            if title not in heavy_rain:
                heavy_rain.append(title)

    return {
        "typhoons":            typhoons[:5],
        "heavy_rain_warnings": heavy_rain[:5],
    }


# ── 모드별 메인 처리 ──────────────────────────────────

def run_daily(webhook_url: str) -> None:
    """매일 정시 요약: 전24시간 지진 + 쓰나미 + 태풍 + 대우특별경보"""
    now_jst  = datetime.now(JST)
    # 요일 한국어
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    wd = weekdays[now_jst.weekday()]
    date_str = now_jst.strftime(f"%Y년 %-m월 %-d일 ({wd})")

    sections = []

    # ── 지진 ──
    quake_data = fetch_json(QUAKE_API)
    if quake_data:
        recent = []
        for q in quake_data:
            t     = q.get("earthquake", {}).get("time", "")
            scale = q.get("earthquake", {}).get("maxScale", -1)
            mag   = q.get("earthquake", {}).get("hypocenter", {}).get("magnitude", -1)
            if not is_within_hours(t, 24):
                continue
            if scale >= DAILY_MIN_SCALE or (isinstance(mag, (int, float)) and mag >= DAILY_MIN_MAGNITUDE):
                recent.append(q)

        if recent:
            lines = [format_quake(q, include_points=True) for q in recent[:10]]
            sections.append("*🗾 지난 24시간 지진*\n" + "\n".join(lines))
        else:
            sections.append("*🗾 지난 24시간 지진*\n• 진도2 이상 · M3.0 이상 지진 없음")
    else:
        sections.append("*🗾 지난 24시간 지진*\n• 데이터 취득 실패")

    # ── 쓰나미 ──
    tsunami_list = get_active_tsunami()
    if tsunami_list:
        t_lines = []
        for t in tsunami_list:
            for area in t["areas"][:5]:
                grade = tsunami_grade_label(area.get("grade", ""))
                name  = area.get("name", "")
                t_lines.append(f"• {name}: {grade}")
        sections.append("*🌊 쓰나미 정보*\n" + "\n".join(t_lines))

    # ── 태풍 ──
    feed_info = parse_jma_feed(JMA_FEED_EXTRA, hours=24)

    if feed_info["typhoons"]:
        t_lines = [f"• {t}" for t in feed_info["typhoons"]]
        sections.append("*🌀 태풍 정보*\n" + "\n".join(t_lines))
    else:
        sections.append("*🌀 태풍 정보*\n• 현재 활동 중인 태풍 없음")

    if feed_info["heavy_rain_warnings"]:
        w_lines = [f"• {w}" for w in feed_info["heavy_rain_warnings"]]
        sections.append("*🌧️ 대우 특별경보*\n" + "\n".join(w_lines))

    # ── 조합 & 전송 ──
    body = f"*📡 일본 방재정보 일간 요약 — {date_str}*\n\n" + "\n\n".join(sections)
    post_to_slack(webhook_url, body)


def run_urgent(webhook_url: str) -> None:
    """긴급 감시: 임계값 초과 시만 전송. 이하면 무음."""
    alerts = []

    # ── 지진: 직전 30분 ──
    quake_data = fetch_json(QUAKE_API)
    if quake_data:
        for q in quake_data:
            t       = q.get("earthquake", {}).get("time", "")
            scale   = q.get("earthquake", {}).get("maxScale", -1)
            mag     = q.get("earthquake", {}).get("hypocenter", {}).get("magnitude", -1)
            tsunami = q.get("earthquake", {}).get("domesticTsunami", "None")

            if not is_within_hours(t, 0.5):
                continue

            is_large    = isinstance(mag, (int, float)) and mag >= URGENT_MAGNITUDE
            is_strong   = scale >= URGENT_MAX_SCALE
            has_tsunami = tsunami in ("MajorWarning", "Warning", "Watch")

            if is_large or is_strong or has_tsunami:
                alerts.append("🚨 *긴급 지진 정보*\n" + format_quake(q, include_points=True))

    # ── 쓰나미 경보 ──
    tsunami_list = get_active_tsunami()
    if tsunami_list:
        for t in tsunami_list:
            major = [a for a in t["areas"] if a.get("grade") in ("MajorWarning", "Warning")]
            if major:
                lines = [
                    f"• {a.get('name','?')}: {tsunami_grade_label(a.get('grade',''))}"
                    for a in major[:5]
                ]
                alerts.append("🚨 *쓰나미 경보 발령 중*\n" + "\n".join(lines))

    # ── 태풍·대우특별경보: 직전 1시간 신규 정보 ──
    feed_info = parse_jma_feed(JMA_FEED_EXTRA, hours=1)

    if feed_info["typhoons"]:
        t_lines = [f"• {t}" for t in feed_info["typhoons"]]
        alerts.append("🌀 *태풍 정보 (신규)*\n" + "\n".join(t_lines))

    if feed_info["heavy_rain_warnings"]:
        w_lines = [f"• {w}" for w in feed_info["heavy_rain_warnings"]]
        alerts.append("🚨 *대우 특별경보 발령 중*\n" + "\n".join(w_lines))

    if not alerts:
        print("[OK] 긴급 알림 없음 — 전송 스킵")
        return

    now_jst  = datetime.now(JST)
    time_str = now_jst.strftime("%m/%d %H:%M JST")
    body = f"*⚠️ 자동 재해 알림 ({time_str})*\n\n" + "\n\n".join(alerts)
    post_to_slack(webhook_url, body)


# ── 진입점 ────────────────────────────────────────────

def main():
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    mode        = os.environ.get("MONITOR_MODE", "daily").lower()

    if not webhook_url:
        print("[ERROR] SLACK_WEBHOOK_URL 이 설정되지 않았습니다", file=sys.stderr)
        sys.exit(1)

    if mode == "daily":
        print(f"[INFO] 일간 요약 모드 ({datetime.now(JST).strftime('%Y/%m/%d %H:%M JST')})")
        run_daily(webhook_url)
    elif mode == "urgent":
        print(f"[INFO] 긴급 감시 모드 ({datetime.now(JST).strftime('%Y/%m/%d %H:%M JST')})")
        run_urgent(webhook_url)
    else:
        print(f"[ERROR] 알 수 없는 MONITOR_MODE: {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
