#!/usr/bin/env python3
"""
disaster_monitor.py
日本の自然災害監視 → Slack通知

対応災害:
  - 地震 (P2PQuake JSON API v2)
  - 津波 (P2PQuake JSON API v2)
  - 台風 (気象庁 JMAXML 定期配信フィード extra.xml)
  - 大雨特別警報 (同上)

動作モード (環境変数 MONITOR_MODE で切替):
  - "daily"   : 毎日定時要約 (前24時間の地震 + 活性台風 + 大雨警報)
  - "urgent"  : 緊急監視 (閾値超えたら即送信、何もなければ無音)

必須環境変数:
  - SLACK_WEBHOOK_URL : Slack Workflow Builder の Webhook URL
  - MONITOR_MODE      : "daily" or "urgent"
"""

import json
import os
import sys
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── 定数 ──────────────────────────────────────────────
JST = timezone(timedelta(hours=9))

# P2PQuake API v2
QUAKE_API   = "https://api.p2pquake.net/v2/jma/quake?limit=20&order=-1"
TSUNAMI_API = "https://api.p2pquake.net/v2/jma/tsunami?limit=5&order=-1"

# 気象庁 JMAXML 配信フィード
# extra.xml = 高頻度(随時): 台風情報・特別警報など
# regular.xml = 定期配信: 通常警報・注意報など
JMA_FEED_EXTRA   = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
JMA_FEED_REGULAR = "https://www.data.jma.go.jp/developer/xml/feed/regular.xml"

# ── 緊急アラート閾値 ──────────────────────────────────
# maxScale: JMA震度スケール
#   10=震度1, 20=震度2, 30=震度3, 40=震度4
#   45=震度5弱, 50=震度5強, 55=震度6弱, 60=震度6強, 70=震度7
URGENT_MAX_SCALE    = 30    # 震度3以上でアラート
URGENT_MAGNITUDE    = 4.5   # M4.5以上でアラート
DAILY_MIN_SCALE     = 20    # 震度2以上を日次サマリーに含める
DAILY_MIN_MAGNITUDE = 3.0   # M3.0以上を日次サマリーに含める

# 東京・南青山周辺の都道府県 (地震点フィルタ用)
TOKYO_AREA_PREFS = ["東京都", "神奈川県", "埼玉県", "千葉県"]


# ── ユーティリティ ────────────────────────────────────

def fetch_json(url: str) -> list | dict | None:
    """JSON取得。失敗時はNoneを返す。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "disaster-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] fetch_json失敗: {url} → {e}", file=sys.stderr)
        return None


def fetch_xml_text(url: str) -> str | None:
    """XMLテキスト取得。失敗時はNoneを返す。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "disaster-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.read().decode("utf-8")
    except Exception as e:
        print(f"[WARN] fetch_xml失敗: {url} → {e}", file=sys.stderr)
        return None


def post_to_slack(webhook_url: str, message: str) -> None:
    """Slack Workflow Builder Webhookに送信。
    Workflow Builder側の変数名: message (テキスト型)
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
            raise RuntimeError(f"Slack webhook error: {res.status}")
    print("[OK] Slack送信完了")


def scale_to_shindo(scale: int) -> str:
    """P2PQuake maxScale → JMA震度表記"""
    table = {
        10: "震度1", 20: "震度2", 30: "震度3", 40: "震度4",
        45: "震度5弱", 50: "震度5強", 55: "震度6弱", 60: "震度6強", 70: "震度7",
        -1: "不明",
    }
    return table.get(scale, f"震度?({scale})")


def tsunami_grade_label(grade: str) -> str:
    """津波予報グレード → 表示ラベル"""
    labels = {
        "MajorWarning": "🚨 大津波警報",
        "Warning":      "⚠️ 津波警報",
        "Watch":        "🔔 津波注意報",
        "Unknown":      "情報なし",
        "None":         "津波の心配なし",
    }
    return labels.get(grade, grade)


def is_within_hours(time_str: str, hours: float) -> bool:
    """P2PQuake の time フィールド (JST 'YYYY/MM/DD HH:MM:SS') が
    現在時刻から指定時間以内かをチェック"""
    try:
        dt = datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")
        dt_jst = dt.replace(tzinfo=JST)
        now_jst = datetime.now(JST)
        return (now_jst - dt_jst) <= timedelta(hours=hours)
    except Exception:
        return False


# ── 地震情報処理 ──────────────────────────────────────

def format_quake(q: dict, include_points: bool = False) -> str:
    """地震1件をSlack表示用にフォーマット"""
    eq  = q.get("earthquake", {})
    hyp = eq.get("hypocenter", {})
    name      = hyp.get("name", "不明")
    magnitude = hyp.get("magnitude", -1)
    depth     = hyp.get("depth", -1)
    max_scale = eq.get("maxScale", -1)
    tsunami   = eq.get("domesticTsunami", "Unknown")
    time_str  = eq.get("time", "")

    mag_str  = f"M{magnitude}" if magnitude != -1 else "M不明"
    dep_str  = "ごく浅い" if depth == 0 else f"深さ{depth}km" if depth > 0 else "深さ不明"
    shindo   = scale_to_shindo(max_scale)
    tsun_str = tsunami_grade_label(tsunami)

    line = f"• {time_str}  {name}  {mag_str} / {dep_str} / 最大{shindo}  {tsun_str}"

    # 東京周辺の観測点があれば追記
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
            line += f"\n  └ 東京周辺: {', '.join(p_strs)}"

    return line


# ── 津波情報処理 ──────────────────────────────────────

def get_active_tsunami() -> list[dict]:
    """現在有効な津波警報・注意報を取得"""
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
        # 警報・注意報レベル以上のみ
        serious = [
            a for a in areas
            if a.get("grade") in ("MajorWarning", "Warning", "Watch")
        ]
        if serious:
            active.append({"time": issue_time, "areas": serious})

    return active


# ── 台風・大雨情報処理 (気象庁 JMAXML フィード) ─────────

def parse_jma_feed(feed_url: str, hours: int = 24) -> dict:
    """
    気象庁 Atom フィードをパースして
    {
      'typhoons': [タイトル文字列, ...],
      'heavy_rain_warnings': [タイトル文字列, ...],
    }
    を返す。
    台風情報タイトル例: '台風第6号に関する情報'
    特別警報タイトル例: '気象特別警報・警報・注意報'
    """
    xml_str = fetch_xml_text(feed_url)
    if not xml_str:
        return {"typhoons": [], "heavy_rain_warnings": []}

    # Atom エントリを正規表現で抽出 (XML名前空間の問題を避けるため)
    entries = re.findall(r'<entry>(.*?)</entry>', xml_str, re.DOTALL)

    now_jst = datetime.now(JST)
    typhoons     = []
    heavy_rain   = []

    for entry in entries:
        # 更新時刻チェック
        m_updated = re.search(r'<updated>(.*?)</updated>', entry)
        if m_updated:
            try:
                # "2026-06-03T10:00:00+09:00" 形式
                updated_str = m_updated.group(1).strip()
                # タイムゾーン付き日時パース
                updated_str_clean = re.sub(r'([+-]\d{2}):(\d{2})$', r'\1\2', updated_str)
                dt = datetime.strptime(updated_str_clean, "%Y-%m-%dT%H:%M:%S%z")
                if (now_jst - dt) > timedelta(hours=hours):
                    continue  # 古いエントリはスキップ
            except Exception:
                pass  # パース失敗時はスキップしない (安全側)

        # タイトル取得
        m_title = re.search(r'<title>(.*?)</title>', entry)
        if not m_title:
            continue
        title = m_title.group(1).strip()

        # 台風情報 (VTUP): '台風第N号に関する情報' / '台風情報'
        if re.search(r'台風|熱帯低気圧', title):
            if title not in typhoons:
                typhoons.append(title)

        # 大雨・洪水 特別警報 (level5相当)
        if re.search(r'大雨特別警報|洪水特別警報|氾濫特別警報', title):
            if title not in heavy_rain:
                heavy_rain.append(title)

    return {
        "typhoons":            typhoons[:5],
        "heavy_rain_warnings": heavy_rain[:5],
    }


# ── モード別メイン処理 ────────────────────────────────

def run_daily(webhook_url: str) -> None:
    """毎日定時サマリー: 前24時間の地震 + 津波 + 台風 + 大雨特別警報"""
    now_jst  = datetime.now(JST)
    date_str = now_jst.strftime("%Y年%-m月%-d日 (%a)")

    sections = []

    # ── 地震 ──
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
            sections.append("*🗾 過去24時間の地震*\n" + "\n".join(lines))
        else:
            sections.append("*🗾 過去24時間の地震*\n• 震度2以上・M3.0以上の地震はありませんでした")
    else:
        sections.append("*🗾 過去24時間の地震*\n• データ取得失敗")

    # ── 津波 ──
    tsunami_list = get_active_tsunami()
    if tsunami_list:
        t_lines = []
        for t in tsunami_list:
            for area in t["areas"][:5]:
                grade = tsunami_grade_label(area.get("grade", ""))
                name  = area.get("name", "")
                t_lines.append(f"• {name}: {grade}")
        sections.append("*🌊 津波情報*\n" + "\n".join(t_lines))

    # ── 台風・大雨 (extra.xml 24h) ──
    feed_info = parse_jma_feed(JMA_FEED_EXTRA, hours=24)

    if feed_info["typhoons"]:
        t_lines = [f"• {t}" for t in feed_info["typhoons"]]
        sections.append("*🌀 台風情報*\n" + "\n".join(t_lines))
    else:
        sections.append("*🌀 台風情報*\n• 現在、活動中の台風はありません")

    if feed_info["heavy_rain_warnings"]:
        w_lines = [f"• {w}" for w in feed_info["heavy_rain_warnings"]]
        sections.append("*🌧️ 大雨特別警報*\n" + "\n".join(w_lines))

    # ── 組み立て & 送信 ──
    body = f"*📡 日本防災情報 日次サマリー — {date_str}*\n\n" + "\n\n".join(sections)
    post_to_slack(webhook_url, body)


def run_urgent(webhook_url: str) -> None:
    """緊急監視: 閾値超えた場合のみ送信。何もなければ無音。"""
    alerts = []

    # ── 地震: 直近30分 ──
    quake_data = fetch_json(QUAKE_API)
    if quake_data:
        for q in quake_data:
            t       = q.get("earthquake", {}).get("time", "")
            scale   = q.get("earthquake", {}).get("maxScale", -1)
            mag     = q.get("earthquake", {}).get("hypocenter", {}).get("magnitude", -1)
            tsunami = q.get("earthquake", {}).get("domesticTsunami", "None")

            if not is_within_hours(t, 0.5):  # 30分以内
                continue

            is_large    = isinstance(mag, (int, float)) and mag >= URGENT_MAGNITUDE
            is_strong   = scale >= URGENT_MAX_SCALE
            has_tsunami = tsunami in ("MajorWarning", "Warning", "Watch")

            if is_large or is_strong or has_tsunami:
                alerts.append("🚨 *緊急地震情報*\n" + format_quake(q, include_points=True))

    # ── 津波警報 ──
    tsunami_list = get_active_tsunami()
    if tsunami_list:
        for t in tsunami_list:
            major = [a for a in t["areas"] if a.get("grade") in ("MajorWarning", "Warning")]
            if major:
                lines = [
                    f"• {a.get('name','?')}: {tsunami_grade_label(a.get('grade',''))}"
                    for a in major[:5]
                ]
                alerts.append("🚨 *津波警報発令中*\n" + "\n".join(lines))

    # ── 台風・大雨特別警報: 直近1時間の新規情報 ──
    feed_info = parse_jma_feed(JMA_FEED_EXTRA, hours=1)

    if feed_info["typhoons"]:
        t_lines = [f"• {t}" for t in feed_info["typhoons"]]
        alerts.append("🌀 *台風情報 (新着)*\n" + "\n".join(t_lines))

    if feed_info["heavy_rain_warnings"]:
        w_lines = [f"• {w}" for w in feed_info["heavy_rain_warnings"]]
        alerts.append("🚨 *大雨特別警報 発令中*\n" + "\n".join(w_lines))

    if not alerts:
        print("[OK] 緊急アラートなし — 送信スキップ")
        return

    now_jst  = datetime.now(JST)
    time_str = now_jst.strftime("%m/%d %H:%M JST")
    body = f"*⚠️ 自動災害アラート ({time_str})*\n\n" + "\n\n".join(alerts)
    post_to_slack(webhook_url, body)


# ── エントリポイント ──────────────────────────────────

def main():
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    mode        = os.environ.get("MONITOR_MODE", "daily").lower()

    if not webhook_url:
        print("[ERROR] SLACK_WEBHOOK_URL が設定されていません", file=sys.stderr)
        sys.exit(1)

    if mode == "daily":
        print(f"[INFO] 日次サマリーモード ({datetime.now(JST).strftime('%Y/%m/%d %H:%M JST')})")
        run_daily(webhook_url)
    elif mode == "urgent":
        print(f"[INFO] 緊急監視モード ({datetime.now(JST).strftime('%Y/%m/%d %H:%M JST')})")
        run_urgent(webhook_url)
    else:
        print(f"[ERROR] 不明なMONITOR_MODE: {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
