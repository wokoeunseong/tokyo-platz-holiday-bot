import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import daily_briefing as daily

FIXTURES = Path(__file__).parent / "fixtures"


class DailyBriefingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-10-20T08:00:00+09:00")
        self.morning = (FIXTURES / "tokyo-20261020-05.xml").read_text()
        self.previous = (FIXTURES / "tokyo-20261019-17.xml").read_text()

    def test_official_six_hour_probabilities_and_correct_date_temperatures(self):
        data = daily.parse_forecasts([self.morning, self.previous], self.now)
        self.assertEqual([data["periods"][h]["probability"] for h in (6, 12, 18)], [20, 30, 20])
        self.assertEqual(data["maximum"]["value"], 28)
        self.assertEqual(data["minimum"]["value"], 20)
        self.assertEqual(data["minimum"]["issued"].isoformat(), "2026-10-19T17:00:00+09:00")
        self.assertEqual(data["periods"][6]["weather"], "☁️ 흐림")

    def test_tomorrow_minimum_is_never_used_for_today(self):
        data = daily.parse_forecasts([self.morning], self.now)
        self.assertIsNone(data["minimum"])

    def test_full_day_maximum_takes_priority_over_daytime_maximum(self):
        import xml.etree.ElementTree as ET
        root = daily.xml_root(self.morning)
        for prop in root.findall(".//Property"):
            if prop.findtext("Type") == "最高気温":
                prop.find("TemperaturePart/Temperature").text = "31"
        data = daily.parse_forecasts([ET.tostring(root)], self.now)
        self.assertEqual(data["maximum"]["value"], 31)

    def test_zero_probability_and_freezing_temperature_are_not_missing(self):
        xml = self.morning.replace('unit="%">20<', 'unit="%">0<')
        data = daily.parse_forecasts([xml], self.now)
        self.assertIn("오전　*0%*", daily.build_message(self.now, data, {}))
        self.assertEqual(daily.temperature({"value": 0}), "0℃")

    def test_forecasts_from_other_islands_cannot_fill_tokyo_missing_values(self):
        xml = self.morning.replace("<Code>130010</Code>", "<Code>999999</Code>")
        data = daily.parse_forecasts([xml], self.now)
        self.assertTrue(all(p["probability"] is None for p in data["periods"].values()))

    def test_newest_forecast_wins_independent_of_feed_order(self):
        revised = self.morning.replace("T05:00:00+09:00", "T07:00:00+09:00")
        revised = revised.replace('unit="%">30<', 'unit="%">60<')
        for docs in ([revised, self.morning], [self.morning, revised]):
            self.assertEqual(daily.parse_forecasts(docs, self.now)["periods"][12]["probability"], 60)

    def test_stale_future_and_non_operational_forecasts_are_rejected(self):
        for now in (self.now + timedelta(days=2), self.now - timedelta(days=2)):
            with self.assertRaises(ValueError):
                daily.parse_forecasts([self.morning], now)
        with self.assertRaises(ValueError):
            daily.parse_forecasts([self.morning.replace("<Status>通常</Status>", "<Status>訓練</Status>")], self.now)

    def test_missing_probability_never_becomes_zero(self):
        xml = self.morning.replace('unit="%">20<', 'unit="%">-<')
        data = daily.parse_forecasts([xml], self.now)
        self.assertIsNone(data["periods"][6]["probability"])
        msg = daily.build_message(self.now, data, {})
        self.assertIn("오전　*확인 불가*", msg)
        self.assertNotIn("*0%*", msg)

    def test_three_rows_no_tomorrow_dawn_or_irrelevant_holidays(self):
        data = daily.parse_forecasts([self.morning, self.previous], self.now)
        msg = daily.build_message(self.now, data, {"2026-11-03": "文化の日"})
        self.assertEqual(sum(line.startswith(("오전　", "오후　", "저녁　")) for line in msg.splitlines()), 3)
        for banned in ("내일", "새벽", "체감", "돌풍", "공휴일", "지진", "태풍"):
            self.assertNotIn(banned, msg)
        self.assertIn("최저 20℃", msg)
        self.assertIn("최고 28℃", msg)
        self.assertIn("최저 10/19 17:00 예보", msg)

    def test_holiday_today_and_seven_day_boundary(self):
        days = {"2026-10-20": "国民の休日", "2026-10-27": "文化の日", "2026-10-28": "元日"}
        msg = daily.build_message(self.now, None, days)
        self.assertIn("오늘은 공휴일 · 국민의 휴일", msg)
        self.assertIn("10/27", msg)
        self.assertNotIn("10/28", msg)
        self.assertNotIn("새해 첫날", msg)
        msg = daily.build_message(self.now, None, None)
        self.assertIn("공휴일 정보 확인 불가", msg)
        self.assertNotIn("공휴일 아님", msg)

    def test_precipitation_kind_and_strength_are_not_inferred_from_probability(self):
        for text, want in [
            ("雨", "☔ 비 예상"), ("弱い雨", "💧 약한 비"), ("強い雨", "🌧 강한 비"),
            ("雪", "❄️ 눈 예상"), ("弱い雪", "❄️ 약한 눈"),
            ("強い雪", "🌨❗ 강한 눈"), ("雨か雪", "🌧❄️ 비 또는 눈"),
            ("みぞれ", "🌧❄️ 진눈깨비"), ("くもり", "☁️ 흐림"),
        ]:
            self.assertEqual(daily.weather_label(text), want)

    def test_typhoon_position_or_formation_alone_never_triggers_notice(self):
        xml = (FIXTURES / "typhoon-okinawa.xml").read_text()
        now = datetime.fromisoformat("2026-10-20T18:00:00+09:00")
        notice = daily.parse_typhoon_notice(xml, now)
        self.assertIsNotNone(notice)
        self.assertEqual(notice["number"], 30)
        self.assertIn("오키나와", notice["area"])
        import xml.etree.ElementTree as ET
        root = daily.xml_root(xml)
        root.find("Head/Headline/Text").text = "台風第３０号が発生しました。"
        self.assertIsNone(daily.parse_typhoon_notice(ET.tostring(root), now))

    def test_ended_cancelled_stale_typhoon_notices_are_hidden(self):
        xml = (FIXTURES / "typhoon-okinawa.xml").read_text()
        now = datetime.fromisoformat("2026-10-20T18:00:00+09:00")
        self.assertIsNone(daily.parse_typhoon_notice(xml, now + timedelta(days=2)))
        self.assertIsNone(daily.parse_typhoon_notice(xml.replace("<InfoType>発表</InfoType>", "<InfoType>取消</InfoType>"), now))
        import xml.etree.ElementTree as ET
        root = daily.xml_root(xml)
        root.find("Head/Headline/Text").text = "台風の影響はなくなりました。高波に警戒する必要はありません。"
        self.assertIsNone(daily.parse_typhoon_notice(ET.tostring(root), now))

    def test_dry_run_never_sends_and_legacy_disaster_entry_is_silent(self):
        with patch.object(daily, "fetch_forecasts", return_value=None), \
             patch.object(daily, "fetch_holidays", return_value={}), \
             patch.object(daily, "fetch_typhoon_notices", return_value=[]), \
             patch.object(daily, "post_to_slack") as send:
            daily.main(["--dry-run"])
            send.assert_not_called()
        import disaster_monitor
        with patch("urllib.request.urlopen", side_effect=AssertionError("legacy network call")):
            disaster_monitor.main()


if __name__ == "__main__":
    unittest.main()
