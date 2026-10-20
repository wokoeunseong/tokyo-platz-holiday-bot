#!/usr/bin/env python3
"""One morning briefing, using dated JMA forecasts; no earthquake reporting."""
import argparse
import json
import math
import os
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import urlparse

from jp_holiday_check import JST, holiday_ko, fetch_holidays, post_to_slack
import jp_ko

REGULAR = "https://www.data.jma.go.jp/developer/xml/feed/regular_l.xml"
EXTRA = "https://www.data.jma.go.jp/developer/xml/feed/extra_l.xml"
ACTIVE_TC = "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json"
FORECAST_LINK = "https://www.jma.go.jp/bosai/forecast/#area_type=offices&area_code=130000"
TYPHOON_LINK = "https://www.jma.go.jp/bosai/typhoon/"
PERIODS = ((6, "오전"), (12, "오후"), (18, "저녁"))


def fetch_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": "tokyoplatz-briefing/3.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8")


def xml_root(text):
    root = ET.fromstring(text)
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    return root


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone missing")
    return dt.astimezone(JST)


def numeric(text, low, high):
    try:
        value = float(text)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) and low <= value <= high else None


def weather_label(text):
    text = unicodedata.normalize("NFKC", text or "")
    if "みぞれ" in text:
        return "🌧❄️ 진눈깨비"
    if "雨" in text and "雪" in text:
        return "🌧❄️ 비 또는 눈"
    if "雪" in text:
        if "強" in text or "大雪" in text:
            return "🌨❗ 강한 눈"
        return "❄️ 약한 눈" if "弱" in text else "❄️ 눈 예상"
    if "雨" in text:
        if "強" in text or "激" in text:
            return "🌧 강한 비"
        return "💧 약한 비" if "弱" in text else "☔ 비 예상"
    if "雷" in text:
        return "⛈ 뇌우"
    if "晴" in text:
        return "🌤 구름 조금" if "くもり" in text or "曇" in text else "☀️ 맑음"
    if "くもり" in text or "曇" in text:
        return "☁️ 흐림"
    return "날씨 확인 불가"


def parse_forecasts(documents, now):
    forecasts = []
    for document in documents:
        root = xml_root(document)
        issued = timestamp(root.findtext("Head/ReportDateTime", ""))
        if (root.findtext("Control/Status") != "通常"
                or root.findtext("Head/InfoType") == "取消"
                or not timedelta(0) <= now - issued <= timedelta(hours=36)):
            continue
        forecasts.append((issued, root))
    forecasts.sort(key=lambda pair: pair[0], reverse=True)
    if not forecasts or now - forecasts[0][0] > timedelta(hours=18):
        raise ValueError("Fresh operational forecast unavailable")
    result = {"issued": forecasts[0][0], "periods": {}, "minimum": None, "maximum": None}
    weather = {}
    for issued, root in forecasts:
        for series in root.iter("TimeSeriesInfo"):
            times = {}
            for definition in series.findall("TimeDefines/TimeDefine"):
                dt = timestamp(definition.findtext("DateTime", ""))
                if dt.date() == now.date():
                    times[definition.get("timeId")] = (dt, definition.findtext("Duration"))
            for item in series.findall("Item"):
                area = item.findtext("Area/Code")
                station = item.findtext("Station/Code")
                for prop in item.findall("Kind/Property"):
                    kind = prop.findtext("Type")
                    for value in prop.iter():
                        target = times.get(value.get("refID"))
                        if not target:
                            continue
                        dt, duration = target
                        if (area == "130010" and value.tag == "ProbabilityOfPrecipitation"
                                and duration == "PT6H" and dt.hour in (6, 12, 18)):
                            result["periods"].setdefault(dt.hour, {
                                "probability": numeric(value.text, 0, 100), "issued": issued})
                        elif (area == "130010" and kind == "３時間内卓越天気"
                              and value.tag == "Weather" and duration == "PT3H"):
                            weather.setdefault(dt.hour, weather_label(value.text))
                        elif station == "44132" and value.tag == "Temperature":
                            key = ("minimum" if kind in ("朝の最低気温", "最低気温")
                                   else "maximum" if kind == "最高気温" else None)
                            # The previous evening forecast has only daytime maximum.
                            if key is None and kind == "日中の最高気温":
                                key = "maximum"
                            if key and (result[key] is None or (
                                    key == "maximum" and kind == "最高気温"
                                    and result[key]["issued"] == issued)):
                                result[key] = {"value": numeric(value.text, -60, 60), "issued": issued}
    for hour, _ in PERIODS:
        period = result["periods"].setdefault(hour, {"probability": None, "issued": result["issued"]})
        labels = [weather.get(hour), weather.get(hour + 3)]
        period["weather"] = " → ".join(dict.fromkeys(labels)) if all(labels) else "날씨 확인 불가"
    if not any(p["probability"] is not None for p in result["periods"].values()):
        # Missing probability is still shown explicitly; dates are never substituted.
        if not weather and result["maximum"] is None and result["minimum"] is None:
            raise ValueError("No forecast for today's Tokyo area")
    return result


def feed_entries(text, now):
    entries = []
    for entry in xml_root(text).findall("entry"):
        try:
            updated = timestamp(entry.findtext("updated", ""))
            link = entry.find("link")
            url = link.get("href", "") if link is not None else ""
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.hostname != "www.data.jma.go.jp"
                    or not parsed.path.startswith("/developer/xml/data/")):
                continue
            if timedelta(0) <= now - updated <= timedelta(hours=36):
                entries.append((updated, entry.findtext("title", ""),
                                entry.findtext("content", ""), url))
        except ValueError:
            continue
    return sorted(entries, reverse=True)


def fetch_forecasts(now):
    entries = feed_entries(fetch_text(REGULAR), now)
    urls = [url for _, title, content, url in entries
            if "府県天気予報" in title and "東京都府県天気予報" in content]
    documents = []
    for url in dict.fromkeys(urls[:8]):
        try:
            documents.append(fetch_text(url))
        except Exception as exc:
            print(f"::warning::Forecast document unavailable: {type(exc).__name__}", file=sys.stderr)
    return parse_forecasts(documents, now)


def parse_typhoon_notice(document, now):
    root = xml_root(document)
    title = unicodedata.normalize("NFKC", root.findtext("Head/Title", ""))
    match = re.search(r"台風第\s*(\d+)\s*号", title)
    if not match or not re.search(r"(?:府県|地方|全般)気象(?:解説)?情報", root.findtext("Control/Title", "")):
        return None
    issued = timestamp(root.findtext("Head/ReportDateTime", ""))
    if (root.findtext("Control/Status") != "通常" or root.findtext("Head/InfoType") == "取消"
            or not timedelta(0) <= now - issued <= timedelta(hours=18)):
        return None
    headline = unicodedata.normalize("NFKC", root.findtext("Head/Headline/Text", ""))
    # Dedicated local/national typhoon advisory + an explicit call to prepare.
    # Formation, position, remote track forecasts and "no warnings needed" do not qualify.
    sentences = re.split("[。\\n]", headline)
    actionable = [s for s in sentences if
                  re.search(r"警戒してください|厳重に警戒|台風対策が必要|上陸しました", s)
                  and not re.search(r"必要[はが]?ありません|必要[はが]?ない|おそれ[はが]?ありません|影響はなく", s)]
    if not actionable:
        return None
    area = re.split("気象|に関する", title)[0].replace("・", "·")
    area_names = {
        "沖縄": "오키나와", "九州": "규슈", "奄美": "아마미", "先島諸島": "사키시마 제도",
        "八重山": "야에야마", "大東島": "다이토지마", "四国": "시코쿠", "中国": "주고쿠",
        "近畿": "긴키", "東海": "도카이", "関東甲信": "간토 고신", "北陸": "호쿠리쿠",
        "東北": "도호쿠", "東日本": "동일본", "西日本": "서일본", "北日本": "북일본",
        "本島": " 본섬",
    }
    for jp, ko in sorted(area_names.items(), key=lambda item: -len(item[0])):
        area = area.replace(jp, ko)
    area = "일본" if area in ("全般", "") else jp_ko.ko_place(area)
    hazards = []
    for jp, ko in (("暴風", "폭풍"), ("大雨", "호우"), ("高波", "높은 파도"), ("高潮", "폭풍해일")):
        if jp in " ".join(actionable):
            hazards.append(ko)
    return {"number": int(match.group(1)), "area": area, "issued": issued,
            "hazards": hazards, "title": title,
            "scope": root.findtext("Control/Title", "")}


def fetch_typhoon_notices(now):
    active = json.loads(fetch_text(ACTIVE_TC))
    if not isinstance(active, list):
        raise ValueError("Invalid active typhoon list")
    numbers = {int(tc["typhoonNumber"][-2:]) for tc in active
               if re.fullmatch(r"\d{4}", str(tc.get("typhoonNumber", "")))}
    if not numbers:
        return []
    entries = feed_entries(fetch_text(EXTRA), now)
    candidates = {}
    for _, title, content, url in entries:
        if not re.search(r"(?:府県|地方|全般)気象(?:解説)?情報", title):
            continue
        content = unicodedata.normalize("NFKC", content)
        heading = content.split("】")[0].lstrip("【")
        match = re.search(r"台風第\s*(\d+)\s*号", heading)
        if match and int(match.group(1)) in numbers:
            candidates.setdefault(heading, url)
    notices = []
    for url in candidates.values():
        notice = parse_typhoon_notice(fetch_text(url), now)
        if notice:
            notices.append(notice)
    return notices


def temperature(value):
    if not value or value["value"] is None:
        return "확인 불가"
    return f'{value["value"]:g}℃'


def build_message(now, forecast, holidays, typhoons=()):
    today = now.date()
    lines = [f'🇯🇵 *도쿄 · {today.month}/{today.day} ({"월화수목금토일"[today.weekday()]})*']
    if forecast:
        issued = forecast["issued"]
        lines.append(f'일본 기상청 · {issued:%m/%d %H:%M} 발표')
    lines.extend(["", "🌤 *오늘 날씨*", ""])
    for hour, label in PERIODS:
        period = forecast["periods"][hour] if forecast else {}
        probability = period.get("probability")
        prob = f"{probability:g}%" if probability is not None else "확인 불가"
        lines.append(f'{label}　*{prob}*　{period.get("weather", "예보 확인 불가")}')
    lines.extend(["", f'🌡 *최저 {temperature(forecast["minimum"] if forecast else None)}'
                  f'　│　최고 {temperature(forecast["maximum"] if forecast else None)}*'])
    if holidays is None:
        lines.extend(["", "공휴일 정보 확인 불가"])
    else:
        if today.isoformat() in holidays:
            lines.extend(["", f'🎌 *오늘은 공휴일 · {holiday_ko(holidays[today.isoformat()])}*'])
        upcoming = [(today + timedelta(days=n)) for n in range(1, 8)
                    if (today + timedelta(days=n)).isoformat() in holidays]
        if upcoming:
            lines.extend(["", "🗓 *다가오는 공휴일*"])
            for day in upcoming:
                lines.append(f'{day.month}/{day.day} ({"월화수목금토일"[day.weekday()]}) · {holiday_ko(holidays[day.isoformat()])}')
    if typhoons:
        lines.extend(["", "🌀 *일본 영향 태풍*"])
        groups = {}
        for notice in typhoons:
            groups.setdefault(notice["number"], []).append(notice)
        for number, notices in sorted(groups.items()):
            ordered = sorted(notices, key=lambda n: (not n["scope"].startswith("地方"), n["area"]))
            distinct = list(dict.fromkeys(n["area"] for n in ordered))
            areas = " · ".join(distinct[:2])
            if len(distinct) > 2:
                areas += f" 외 {len(distinct) - 2}개 지역"
            hazards = "·".join(dict.fromkeys(h for n in notices for h in n["hazards"]))
            lines.append(f'*태풍 {number}호* · {areas}')
            issued = max(n["issued"] for n in notices)
            lines.append(f'지역별 {hazards + " " if hazards else ""}대비 안내 · {issued:%m/%d %H:%M} 기준')
        lines.append(f'<{TYPHOON_LINK}|기상청 태풍 상세>')
    lines.extend(["", "%는 강수확률 · 오전 06–12시 / 오후 12–18시 / 저녁 18–24시"])
    if forecast:
        sources = []
        for hour, label in PERIODS:
            source = forecast["periods"][hour]["issued"]
            if source != forecast["issued"]:
                sources.append(f'{label} {source:%m/%d %H:%M} 예보')
        for key, label in (("minimum", "최저"), ("maximum", "최고")):
            item = forecast[key]
            if item and item["issued"] != forecast["issued"]:
                sources.append(f'{label} {item["issued"]:%m/%d %H:%M} 예보')
        if sources:
            lines.append(" · ".join(sources))
    else:
        lines.append("기상청 예보 조회 지연 · 확인되지 않은 수치는 표시하지 않습니다.")
    lines.append(f'<{FORECAST_LINK}|기상청 자세히 보기>')
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print only; never send to Slack")
    args = parser.parse_args(argv)
    now = datetime.now(JST)
    values = []
    for label, fetch in (("weather", lambda: fetch_forecasts(now)),
                         ("holiday", fetch_holidays),
                         ("typhoon", lambda: fetch_typhoon_notices(now))):
        try:
            value = fetch()
            if label == "holiday":
                years = {now.year, (now + timedelta(days=7)).year}
                if not isinstance(value, dict) or any(
                        not any(key.startswith(f"{year}-") for key in value) for year in years):
                    raise ValueError("Holiday calendar does not cover the date window")
            values.append(value)
        except Exception as exc:
            print(f"::warning::{label} unavailable: {type(exc).__name__}", file=sys.stderr)
            values.append(None)
    forecast, holidays, typhoons = values
    message = build_message(now, forecast, holidays, typhoons or [])
    if args.dry_run:
        print(message)
        return
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("SLACK_WEBHOOK_URL missing")
    post_to_slack(webhook, message)
    print(f"[{now.date()}] Morning briefing sent")


if __name__ == "__main__":
    main()
