# -*- coding: utf-8 -*-
"""
store_impact.py
===============
TPC(도쿄플라츠커피) 도심 매장 기준 방재 영향도 판정 + 슬랙 메시지 생성 모듈.

disaster_monitor.py 가 파싱한 지진/태풍 데이터를 받아서,
  1) 매장 영향 레벨(🟢/🟡/🔴)을 판정하고
  2) 슬랙에 바로 올릴 mrkdwn 메시지 문자열을 만들어 돌려준다.

────────────────────────────────────────────────────────
■ GitHub에서 수정하는 부분 (로직 안 건드려도 됨)
   - STORE_WARDS / WARNING_AREA   : 매장 소재 구, 경보 묶음
   - 임계값 상수 (EQ_*, TY_*)      : 레벨 민감도
   - EMOJI / 문구 상수            : 표현
■ 건드리지 않는 부분
   - judge_level(), build_slack_message() : 판정·조립 로직
────────────────────────────────────────────────────────

■ disaster_monitor.py 통합 방법 (3줄)
   from store_impact import Earthquake, Typhoon, build_slack_message
   msg = build_slack_message(eq_main=..., typhoon=..., base=..., minor_summary=...)
   slack_post(channel, msg)        # 기존 전송 함수에 그대로 넣기
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime

# ════════════════════════════════════════════════════════════
# 1. 매장 설정  ─ 매장이 바뀌면 여기만 수정
# ════════════════════════════════════════════════════════════

# 매장 소재 구 — 震度/警報 파싱 시 이 키워드로 매칭한다.
#   1호점 港区 南青山 / 2·3호점 千代田区 麹町
STORE_WARDS = ["港区", "千代田区"]

# 도쿄 도심 광역 키 — 위 구가 안 잡힐 때 폴백으로 도심 진도를 잡는다.
TOKYO_KEYS = ["東京都23区", "東京", "23区"]

# JMA 경보(警報) 발표 묶음 — 두 매장 다 23구라 동일 적용
WARNING_AREA = "東京都23区"

# 헤더에 박는 매장 라벨
STORE_LABEL = "TPC 도심 3점"
WARD_LABEL = "港区·千代田 공통"

# ════════════════════════════════════════════════════════════
# 2. 판정 임계값  ─ 민감도는 여기 숫자/진도만 바꾸면 된다
# ════════════════════════════════════════════════════════════

# [지진] 매장권(도쿄 도심) 최대 진도 기준
EQ_GREEN_MAX_SHINDO = "2"     # 이 진도 이하 → 🟢 영향 없음
EQ_RED_MIN_SHINDO = "5弱"     # 이 진도 이상 → 🔴 경보

# [태풍] 매장 최접근까지 남은 일수(D+N) 기준
TY_RED_MAX_DAYS = 0           # D+0(당일) 이하 또는 경보 발효 → 🔴
TY_YELLOW_MAX_DAYS = 2        # D+1~2 → 🟡   (D+3 이상이면 🟢)

# 침수 가중치 — 南青山·麹町 모두 台地(고지대) → 침수 경보 약하게 본다.
#   저지대 매장이 생기면 "high"로 바꾸고 RED 규칙에 반영.
FLOOD_WEIGHT = "low"

# ════════════════════════════════════════════════════════════
# 3. 표현(이모지·문구)  ─ 톤은 여기서 조정
# ════════════════════════════════════════════════════════════

GREEN, YELLOW, RED = 0, 1, 2          # 레벨 정수 (내부용)
EMOJI = {GREEN: "🟢", YELLOW: "🟡", RED: "🔴"}

DIVIDER = "───────────────"

# 레벨별 헤드라인 (태풍 주도 🟡는 최접근 시점이 {win}에 들어감)
HEADLINE = {
    RED: "경보 / 당일 대응 필요",
    "ty_yellow": "태풍 주의 ({win})",
    "eq_yellow": "여진 주의",
    GREEN: "영향 없음",
}

# 헤더 2번째 줄에 들어가는 도메인별 짧은 상태
EQ_SHORT = {GREEN: "영향 없음", YELLOW: "여진 주의", RED: "강진"}
TY_SHORT = {GREEN: "영향 없음", YELLOW: "{win} 경계", RED: "직격/경보"}


# ════════════════════════════════════════════════════════════
# 4. 입력 데이터 구조  ─ disaster_monitor.py 파싱 결과를 여기 담아 넘긴다
# ════════════════════════════════════════════════════════════

@dataclass
class Earthquake:
    """매장 판정 대상이 되는 '대표 지진' 1건."""
    time: str            # "07:30"
    region: str          # "도호쿠(아오모리·이와테) 앞바다"
    magnitude: str       # "M6.9"
    max_shindo: str      # "6強"  ← 전국 최대 진도 (표시용)
    tokyo_shindo: str = ""   # "2" ← 매장권 최대 진도 (판정용). 없으면 ""
    tsunami: bool = False    # 쓰나미 피해 우려 여부
    note: str = ""           # └ 보조 한 줄 (예: "진앙 북쪽 약 550km · 도심 미동")


@dataclass
class Typhoon:
    """접근 중인 태풍 정보. 없으면 build 호출 시 None을 넘긴다."""
    number: str              # "7"
    current_pos: str         # "오키나와 남쪽 북상 중"
    pressure: str = ""       # "970hPa"
    closest_label: str = ""  # "27일 밤~28일"  ← 사람이 읽는 최접근 시점
    days_to_closest: int = 99    # 오늘 기준 D+N (정수). 모르면 99
    warning_active: bool = False # 도쿄 警報(폭풍/호우/태풍) 발효 여부


# ════════════════════════════════════════════════════════════
# 5. 진도 비교 유틸
# ════════════════════════════════════════════════════════════

# JMA 진도 계급을 비교 가능한 숫자로 변환. "5-"/"5弱" 양쪽 표기 모두 허용.
_SHINDO_RANK = {
    "": 0, "0": 0,
    "1": 1, "2": 2, "3": 3, "4": 4,
    "5-": 5, "5弱": 5,
    "5+": 6, "5強": 6,
    "6-": 7, "6弱": 7,
    "6+": 8, "6強": 8,
    "7": 9,
}


def shindo_rank(s: str) -> int:
    """진도 문자열 → 정수 순위. 미등록 값은 0으로 처리(안전측)."""
    return _SHINDO_RANK.get((s or "").strip(), 0)


def max_tokyo_shindo(area_shindo: dict[str, str]) -> str:
    """
    {지역명: 진도} 사전에서 매장권 최대 진도를 뽑는다.
    먼저 STORE_WARDS(港区/千代田)로, 없으면 TOKYO_KEYS(도심 광역)로 폴백.
    disaster_monitor 파싱이 지역별 진도를 dict로 주면 이 헬퍼로 tokyo_shindo를 만들면 된다.
    """
    keys = STORE_WARDS + TOKYO_KEYS
    best, best_rank = "", -1
    for area, sd in area_shindo.items():
        if any(k in area for k in keys):
            r = shindo_rank(sd)
            if r > best_rank:
                best, best_rank = sd, r
    return best


# ════════════════════════════════════════════════════════════
# 6. 레벨 판정  ─ 로직 본체 (수정 불필요)
# ════════════════════════════════════════════════════════════

def _eq_level(eq: Earthquake | None) -> int:
    """지진 레벨: 매장권 진도 기준."""
    if eq is None:
        return GREEN
    r = shindo_rank(eq.tokyo_shindo)
    if r >= shindo_rank(EQ_RED_MIN_SHINDO):
        return RED
    if r <= shindo_rank(EQ_GREEN_MAX_SHINDO):
        return GREEN
    return YELLOW


def _ty_level(ty: Typhoon | None) -> int:
    """태풍 레벨: 경보 발효 또는 최접근 잔여일 기준."""
    if ty is None:
        return GREEN
    if ty.warning_active or ty.days_to_closest <= TY_RED_MAX_DAYS:
        return RED
    if ty.days_to_closest <= TY_YELLOW_MAX_DAYS:
        return YELLOW
    return GREEN


def judge_level(eq: Earthquake | None, ty: Typhoon | None):
    """
    종합 판정. 반환: (overall, eq_lv, ty_lv, driver)
      overall : 두 도메인 중 더 높은 레벨
      driver  : 'typhoon' / 'eq' / 'none'  (헤드라인·권장조치 분기에 사용)
    """
    eq_lv, ty_lv = _eq_level(eq), _ty_level(ty)
    overall = max(eq_lv, ty_lv)
    if overall == GREEN:
        driver = "none"
    elif ty_lv >= eq_lv:      # 동률이면 더 행동지향적인 태풍을 주도로
        driver = "typhoon"
    else:
        driver = "eq"
    return overall, eq_lv, ty_lv, driver


# ════════════════════════════════════════════════════════════
# 7. 날짜 유틸
# ════════════════════════════════════════════════════════════

_KWD = ["월", "화", "수", "목", "금", "토", "일"]  # Mon~Sun


def kdate_label(dt: datetime) -> str:
    """datetime → '06/25(목) 09:15' 형식."""
    return f"{dt:%m/%d}({_KWD[dt.weekday()]}) {dt:%H:%M}"


def _dlabel(base: date, plus: int) -> str:
    """기준일 +N일을 'D일' 또는 'M월 D일'(월 바뀌면)로."""
    from datetime import timedelta
    d = base + timedelta(days=plus)
    return f"{d.day}일" if d.month == base.month else f"{d.month}월 {d.day}일"


# ════════════════════════════════════════════════════════════
# 8. 권장 조치 생성  ─ actions 인자로 직접 넘기면 이 기본값을 덮어쓴다
# ════════════════════════════════════════════════════════════

def _default_actions(overall, driver, ty: Typhoon | None, base: date) -> list[str]:
    if overall == RED:
        return [
            "단축영업/임시휴업 판단",
            "입간판·테라스 비품 철수",
            "스태프 귀가 동선 사전 확인",
        ]
    if overall == YELLOW and driver == "typhoon" and ty is not None:
        prep = _dlabel(base, max(ty.days_to_closest, 1))  # 최접근일 = 대비 기준일
        return [
            f"{_dlabel(base, 1)} 중 {ty.number}호 진로 재확인",
            f"*{prep} 오후* 기준 단축영업·입간판 고정 판단",
        ]
    if overall == YELLOW and driver == "eq":
        return ["여진 가능성 — 집기·진열 고정 확인", "퇴근 동선 유의"]
    return []  # 🟢은 권장 조치 없음


# ════════════════════════════════════════════════════════════
# 9. 슬랙 메시지 조립  ─ 로직 본체 (수정 불필요)
# ════════════════════════════════════════════════════════════

def build_slack_message(
    eq_main: Earthquake | None,
    typhoon: Typhoon | None,
    *,
    base: datetime,
    minor_summary: str = "",
    actions: list[str] | None = None,
) -> str:
    """
    슬랙 mrkdwn 메시지 문자열을 생성한다.

    eq_main       : 대표 지진 1건 (없으면 None)
    typhoon       : 접근 중 태풍 (없으면 None)
    base          : 요약 생성 시각 (datetime)
    minor_summary : 무시 가능한 잔여 지진 한 줄 (예: "이와테 앞바다 M3.x 다수")
    actions       : 권장 조치 직접 지정. None이면 레벨 기반 기본값 사용.
    """
    overall, eq_lv, ty_lv, driver = judge_level(eq_main, typhoon)
    win = typhoon.closest_label if typhoon else ""

    # ── 헤더 ──────────────────────────────
    if overall == RED:
        head = HEADLINE[RED]
    elif overall == YELLOW:
        head = (HEADLINE["ty_yellow"].format(win=win)
                if driver == "typhoon" else HEADLINE["eq_yellow"])
    else:
        head = HEADLINE[GREEN]

    eq_short = EQ_SHORT[eq_lv]
    ty_short = TY_SHORT[ty_lv].format(win=win) if typhoon else TY_SHORT[GREEN]

    lines = [
        f"*{EMOJI[overall]} {STORE_LABEL} 영향도 — {head}*",
        f"{WARD_LABEL} · 지진 {EMOJI[eq_lv]} {eq_short} · 태풍 {EMOJI[ty_lv]} {ty_short}",
        DIVIDER,
        f"*📍 {kdate_label(base)} JST · 일본 방재 요약*",
        "",
    ]

    # ── 지진 블록 ──────────────────────────
    if eq_main is not None and shindo_rank(eq_main.tokyo_shindo) > 0:
        eq_title = {GREEN: "매장 영향 없음", YELLOW: "여진 주의",
                    RED: "강진 — 즉시 확인"}[eq_lv]
        tsu = "쓰나미 우려 없음" if not eq_main.tsunami else "*쓰나미 정보 확인 필요*"
        lines.append(f"*{EMOJI[eq_lv]} 지진 — {eq_title}*")
        lines.append(
            f"• {eq_main.time} {eq_main.region} "
            f"*{eq_main.magnitude} / 최대 진도{eq_main.max_shindo}* · {tsu}"
        )
        if eq_main.note:
            lines.append(f"  └ {eq_main.note}")
    else:
        # 매장권 유의 지진 없음 (원거리·해외만)
        lines.append("*🟢 지진 — 매장 영향 없음*")
        lines.append("• 도쿄권 유의 지진 없음 (원거리·해외 지진만 수신)")

    if minor_summary:
        lines.append(f"• {minor_summary} → 무시 가능")
    lines.append("")

    # ── 태풍 블록 ──────────────────────────
    if typhoon is not None:
        ty_title = {GREEN: "영향 적음", YELLOW: f"{win} 경계",
                    RED: "직격 — 당일 대응"}[ty_lv]
        lines.append(f"*{EMOJI[ty_lv]} 태풍 {typhoon.number}호 — {ty_title}*")
        pos = typhoon.current_pos
        if typhoon.pressure:
            pos += f" (중심기압 {typhoon.pressure})"
        lines.append(f"• 현재 {pos}")
        if win:
            lines.append(f"• 간토 최접근 *{win}* → 폭풍우·강풍·호우 가능")
        if ty_lv == YELLOW:
            lines.append("• 당일 매장 조치 불필요, 비 시작 수준")
    else:
        lines.append("*🟢 태풍 — 접근 없음*")

    # ── 권장 조치 ──────────────────────────
    acts = actions if actions is not None else _default_actions(
        overall, driver, typhoon, base.date())
    if acts:
        lines.append(DIVIDER)
        lines.append("*▶ 권장*")
        lines.extend(f"• {a}" for a in acts)

    # 완전 🟢이면 본문 최소화 문구
    if overall == GREEN and not acts:
        lines.append("_상세 없음. 평상 운영._")

    return "\n".join(lines).rstrip()


# ════════════════════════════════════════════════════════════
# 10. 단독 실행 테스트  ─ `python store_impact.py` 로 오늘(06/25) 메시지 출력
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    base = datetime(2026, 6, 25, 9, 15)

    eq = Earthquake(
        time="07:30",
        region="도호쿠(아오모리·이와테) 앞바다",
        magnitude="M6.9",
        max_shindo="6強",
        tokyo_shindo="2",          # 매장권 추정 최대 진도 → 🟢 판정
        tsunami=False,
        note="진앙 북쪽 약 550km · 도쿄 도심 미동(진도1~2 추정)",
    )

    ty = Typhoon(
        number="7",
        current_pos="오키나와 남쪽 북상 중",
        pressure="970hPa",
        closest_label="27일 밤~28일",
        days_to_closest=2,         # 6/25 기준 → 🟡 판정
        warning_active=False,
    )

    print(build_slack_message(
        eq_main=eq,
        typhoon=ty,
        base=base,
        minor_summary="이와테 앞바다 M3.x 다수 (동일 지진 여진·중복 수신)",
    ))
