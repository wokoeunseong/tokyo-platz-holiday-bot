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

────────────────────────────────────────────────────────
[2026-06 추가] TPC 도심 매장(港区·千代田) 영향도 헤더
  - 기존 일간/긴급 메시지 맨 위에 🟢/🟡/🔴 2줄 헤더를 자동으로 얹는다.
  - 지진: P2PQuake 관측점에서 매장 구(港区/千代田) 진도를 추출해 판정.
  - 태풍: 기상청 bosai 트랙 JSON으로 도쿄 최접근 거리·시점을 계산해 판정.
  - 판정 로직·임계값·문구는 store_impact.py 에서 관리.
────────────────────────────────────────────────────────
"""

import json
import os
import sys
import re
import math
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# 같은 폴더(scripts/)의 store_impact 를 어떤 실행 위치(CWD)에서도 import 할 수 있게 보장.
# GitHub Actions 가 루트에서 실행하든 scripts 에서 실행하든 동일하게 동작.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# store_impact.py 의 판정 엔진·상수를 가져온다 (같은 폴더에 두 파일을 둘 것)
from store_impact import (
    Earthquake, Typhoon, judge_level,
    EMOJI, EQ_SHORT, TY_SHORT, HEADLINE, DIVIDER,
    STORE_LABEL, WARD_LABEL, STORE_WARDS,
)
import jp_ko  # 일본어 지명·제목 → 한국어 변환 (본문 표시용)

# ── 상수 ──────────────────────────────────────────────
JST = timezone(timedelta(hours=9))

# P2PQuake API v2
QUAKE_API   = "https://api.p2pquake.net/v2/jma/quake?limit=20&order=-1"
TSUNAMI_API = "https://api.p2pquake.net/v2/jma/tsunami?limit=5&order=-1"

# 기상청 JMAXML 배신 피드
JMA_FEED_EXTRA = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"

# 기상청 bosai 태풍 트랙 (위치·예보 좌표 제공 / 헤더 판정용)
JMA_TC_LIST = "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json"
JMA_TC_FCST = "https://www.jma.go.jp/bosai/typhoon/data/{tc}/forecast.json"
JMA_TC_SPEC = "https://www.jma.go.jp/bosai/typhoon/data/{tc}/specifications.json"

# 매장 기준 좌표 (港区 南青山 부근). 千代田 麹町과 동일권이라 단일 좌표로 충분.
TOKYO_LATLON = (35.658, 139.751)

# ── 태풍 헤더 판정 임계값 (여기 숫자만 바꾸면 민감도 조정) ──
TY_NEAR_KM      = 600   # 도쿄 최접근이 이 거리(km) 이내일 때만 헤더에서 태풍을 '접근'으로 본다
TY_DIRECT_KM    = 300   # 이 거리 이내 + 임박(D≤1)이면 직격(🔴)으로 본다
TY_DIRECT_DAYS  = 1

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
        "Checking":     "확인 중",
        "NonEffective": "쓰나미 우려 없음",
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


# ════════════════════════════════════════════════════════════
# [추가] 매장 영향도 헤더 — 지진/태풍 어댑터 + 헤더 조립
# ════════════════════════════════════════════════════════════

# P2PQuake scale → store_impact 랭킹용 JMA 진도 문자열 (강/약을 弱/強로)
_SCALE_TO_JMA = {
    10: "1", 20: "2", 30: "3", 40: "4",
    45: "5弱", 50: "5強", 55: "6弱", 60: "6強", 70: "7",
}
# P2PQuake scale → 표시용 한국어 진도 (store_impact 가 "최대 진도{}" 로 출력)
_SCALE_TO_KR = {
    10: "1", 20: "2", 30: "3", 40: "4",
    45: "5약", 50: "5강", 55: "6약", 60: "6강", 70: "7",
}


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """두 위경도 사이 거리(km)."""
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _store_ward_scale(points: list[dict]) -> int:
    """관측점에서 매장 구(港区/千代田)의 최대 scale을 뽑는다.
    못 찾으면 도쿄도(東京) 전체 최대 scale로 폴백. 그것도 없으면 -1."""
    ward_max, tokyo_max = -1, -1
    for p in points:
        addr = p.get("addr", "")
        pref = p.get("pref", "")
        sc = p.get("scale", -1)
        if any(w in addr for w in STORE_WARDS):      # 예: "東京千代田区大手町"
            ward_max = max(ward_max, sc)
        if "東京" in addr or "東京" in pref:
            tokyo_max = max(tokyo_max, sc)
    return ward_max if ward_max >= 0 else tokyo_max


def build_earthquake(quake_list: list, window_hours: float):
    """
    P2PQuake 목록 → (대표 Earthquake | None, 잔여요약 문자열).
    대표 = 윈도우 내 maxScale 최대 1건. 매장 진도는 관측점에서 추출.
    """
    if not quake_list:
        return None, ""

    # 윈도우 필터 + 중복(같은 time+name 속보/정정) 제거
    seen, recent = set(), []
    for q in quake_list:
        eq = q.get("earthquake", {})
        t = eq.get("time", "")
        if not is_within_hours(t, window_hours):
            continue
        key = (t, eq.get("hypocenter", {}).get("name", ""))
        if key in seen:
            continue
        seen.add(key)
        recent.append(q)

    if not recent:
        return None, ""

    # 대표 지진 = maxScale 최대 (동률이면 규모 큰 쪽)
    def _key(q):
        eq = q.get("earthquake", {})
        return (eq.get("maxScale", -1),
                eq.get("hypocenter", {}).get("magnitude", -1) or -1)
    main = max(recent, key=_key)

    eq = main["earthquake"]
    hyp = eq.get("hypocenter", {})
    scale = eq.get("maxScale", -1)
    mag = hyp.get("magnitude", -1)
    tsunami = eq.get("domesticTsunami", "None")

    ward_scale = _store_ward_scale(main.get("points", []))
    tokyo_shindo = _SCALE_TO_JMA.get(ward_scale, "")   # 판정용 (없으면 "")

    # 보조 한 줄: 진앙 거리 + 도쿄 도심 진도
    note_parts = []
    lat, lon = hyp.get("latitude"), hyp.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and lat > 0:
        dist = _haversine_km(TOKYO_LATLON, (lat, lon))
        note_parts.append(f"진앙 약 {dist:.0f}km")
    if ward_scale >= 0:
        note_parts.append(f"도쿄 도심 최대 진도{_SCALE_TO_KR.get(ward_scale, '?')}")
    else:
        note_parts.append("도쿄권 미감지")

    main_eq = Earthquake(
        time=eq.get("time", "")[11:16],                 # "HH:MM"
        region=jp_ko.ko_place(hyp.get("name", "불명")),
        magnitude=f"M{mag}" if mag != -1 else "M불명",
        max_shindo=_SCALE_TO_KR.get(scale, "불명"),       # 표시용 (6강 등)
        tokyo_shindo=tokyo_shindo,                       # 판정용 (6強 등)
        tsunami=tsunami in ("MajorWarning", "Warning", "Watch"),
        note=" · ".join(note_parts),
    )

    # 잔여 요약
    others = [q for q in recent if q is not main]
    minor_summary = ""
    if others:
        top = max(others, key=_key)["earthquake"].get("maxScale", -1)
        minor_summary = f"그 외 {len(others)}건 (최대 {scale_to_shindo(top)}) → 무시 가능"

    return main_eq, minor_summary


def assess_typhoon():
    """
    기상청 bosai 트랙으로 도쿄 최접근을 계산해 store_impact.Typhoon 생성.
    - 도쿄 최접근 거리가 TY_NEAR_KM 초과 → None (헤더상 태풍 영향 없음/🟢)
    - 네트워크/스키마 오류 → None (지진 헤더는 정상 동작)
    """
    try:
        tc_list = fetch_json(JMA_TC_LIST) or []
        if not tc_list:
            return None

        now = datetime.now(JST)
        candidates = []  # (min_dist, closest_dt, gale_covered, tc_id, typhoon_no)

        for tc in tc_list:
            tc_id = tc.get("tropicalCyclone")
            no = tc.get("typhoonNumber", "")
            fc = fetch_json(JMA_TC_FCST.format(tc=tc_id))
            if not fc:
                continue
            best = None
            for part in fc:
                c = part.get("center")
                vt = part.get("validtime", {}).get("JST")
                if not c or not vt:
                    continue
                d = _haversine_km(TOKYO_LATLON, (c[0], c[1]))
                gale = part.get("galeWarningArea", {}).get("radius")  # m
                covered = bool(gale and d * 1000 <= gale)
                if best is None or d < best[0]:
                    best = (d, datetime.fromisoformat(vt), covered)
            if best:
                candidates.append((*best, tc_id, no))

        if not candidates:
            return None

        # 가장 위협적인 태풍 = 최접근 거리 최소
        min_dist, closest_dt, gale_covered, tc_id, no = min(candidates, key=lambda x: x[0])
        if min_dist > TY_NEAR_KM:
            return None  # 멀리 있음 → 헤더 🟢 (상세는 본문 태풍 섹션에 표시됨)

        days = (closest_dt.date() - now.date()).days
        # 직격 판정: 강풍역이 도쿄를 덮거나 / 가깝고 임박
        warning = gale_covered or (min_dist <= TY_DIRECT_KM and days <= TY_DIRECT_DAYS)

        # 표시용 현재위치·기압은 specifications 에서 (실패해도 무시)
        cur_pos, pressure = "북상 중", ""
        spec = fetch_json(JMA_TC_SPEC.format(tc=tc_id)) or []
        for part in spec:
            if part.get("advancedHours") == 0:
                cur_pos = part.get("location", cur_pos)
                pressure = part.get("pressure", "")
                break

        return Typhoon(
            number=str(int(no) % 100) if no.isdigit() else no,
            current_pos=cur_pos,
            pressure=f"{pressure}hPa" if pressure else "",
            closest_label=f"{closest_dt:%m/%d %H}시경",
            days_to_closest=days,
            warning_active=warning,
        )
    except Exception as e:
        print(f"[WARN] assess_typhoon 실패 → {e}", file=sys.stderr)
        return None


def build_store_header(quake_list: list, window_hours: float, base: datetime) -> str:
    """
    매장 영향도 2줄 헤더 + 구분선 생성.
    (판정은 store_impact.judge_level 사용 — 단일 소스)
    """
    eq_main, _ = build_earthquake(quake_list, window_hours)
    typhoon = assess_typhoon()
    overall, eq_lv, ty_lv, driver = judge_level(eq_main, typhoon)
    win = typhoon.closest_label if typhoon else ""

    # 헤드라인 (store_impact.build_slack_message 와 동일 규칙)
    from store_impact import GREEN, YELLOW, RED
    if overall == RED:
        head = HEADLINE[RED]
    elif overall == YELLOW:
        head = (HEADLINE["ty_yellow"].format(win=win)
                if driver == "typhoon" else HEADLINE["eq_yellow"])
    else:
        head = HEADLINE[GREEN]

    eq_short = EQ_SHORT[eq_lv]
    ty_short = TY_SHORT[ty_lv].format(win=win) if typhoon else TY_SHORT[GREEN]

    return (
        f"*{EMOJI[overall]} {STORE_LABEL} 영향도 — {head}*\n"
        f"{WARD_LABEL} · 지진 {EMOJI[eq_lv]} {eq_short} · 태풍 {EMOJI[ty_lv]} {ty_short}\n"
        f"{DIVIDER}\n"
    )


# ── 지진 정보 처리 ────────────────────────────────────

def format_quake(q: dict, include_points: bool = False) -> str:
    """지진 1건을 Slack 표시용으로 포맷"""
    eq  = q.get("earthquake", {})
    hyp = eq.get("hypocenter", {})
    name      = jp_ko.ko_place(hyp.get("name", "불명"))
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
                f"{jp_ko.ko_point(p.get('addr','?'))}:{scale_to_shindo(p.get('scale',-1))}"
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
        t_lines = [f"• {jp_ko.ko_title(t)}" for t in feed_info["typhoons"]]
        sections.append("*🌀 태풍 정보*\n" + "\n".join(t_lines))
    else:
        sections.append("*🌀 태풍 정보*\n• 현재 활동 중인 태풍 없음")

    if feed_info["heavy_rain_warnings"]:
        w_lines = [f"• {jp_ko.ko_title(w)}" for w in feed_info["heavy_rain_warnings"]]
        sections.append("*🌧️ 대우 특별경보*\n" + "\n".join(w_lines))

    # ── 조합 & 전송 ──
    # [추가] 맨 위에 매장 영향도 헤더를 얹는다 (지진 윈도우 24h).
    # 헤더 생성이 어떤 이유로 실패해도 기존 요약은 정상 전송되도록 방어.
    try:
        header = build_store_header(quake_data or [], 24, now_jst)
    except Exception as e:
        print(f"[WARN] 매장 헤더 생성 실패 → {e}", file=sys.stderr)
        header = ""
    body = (header
            + f"*📡 일본 방재정보 일간 요약 — {date_str}*\n\n"
            + "\n\n".join(sections))
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
        t_lines = [f"• {jp_ko.ko_title(t)}" for t in feed_info["typhoons"]]
        alerts.append("🌀 *태풍 정보 (신규)*\n" + "\n".join(t_lines))

    if feed_info["heavy_rain_warnings"]:
        w_lines = [f"• {jp_ko.ko_title(w)}" for w in feed_info["heavy_rain_warnings"]]
        alerts.append("🚨 *대우 특별경보 발령 중*\n" + "\n".join(w_lines))

    if not alerts:
        print("[OK] 긴급 알림 없음 — 전송 스킵")
        return

    now_jst  = datetime.now(JST)
    time_str = now_jst.strftime("%m/%d %H:%M JST")
    # [추가] 긴급 메시지에도 매장 영향도 헤더를 얹는다 (지진 윈도우 1h).
    # 헤더 생성 실패해도 긴급 알림 본문은 정상 전송되도록 방어.
    try:
        header = build_store_header(quake_data or [], 1, now_jst)
    except Exception as e:
        print(f"[WARN] 매장 헤더 생성 실패 → {e}", file=sys.stderr)
        header = ""
    body = (header
            + f"*⚠️ 자동 재해 알림 ({time_str})*\n\n"
            + "\n\n".join(alerts))
    post_to_slack(webhook_url, body)


# ── 진입점 ────────────────────────────────────────────

def main():
    print("[INFO] ===== disaster_monitor.py 시작 =====")  # 실행 파일 식별용 마커
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
