import copy
import json
import re
import unittest
from unittest.mock import patch
import jp_holiday_check as weather
import jp_ko
import disaster_monitor as disaster

JP = r'[\u3040-\u30ff\u3400-\u9fff]'

class Response:
    status = 200
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.data).encode()

def forecast():
    return {
        'current': {'temperature_2m':28, 'apparent_temperature':31, 'weather_code':2},
        'daily': {'time':['2026-12-31','2027-01-01'], 'temperature_2m_max':[30,29],
                  'temperature_2m_min':[24,23], 'weather_code':[2,61],
                  'precipitation_probability_max':[80,90], 'precipitation_sum':[5,12],
                  'wind_gusts_10m_max':[9,11]},
        'hourly': {'time':['2026-12-31T08:00','2026-12-31T09:00','2026-12-31T12:00',
                            '2026-12-31T18:00','2027-01-01T08:00'],
                   'precipitation_probability':[20,40,70,80,99],
                   'precipitation':[0,1,2,2,99]}}

class BriefingTests(unittest.TestCase):
    def test_slack_payload_has_no_raw_japanese_or_markdown_asterisks(self):
        sent=[]
        def send(req, **kwargs):
            sent.append(json.loads(req.data)['message'])
            return Response({})
        with patch('urllib.request.urlopen',side_effect=send):
            disaster.post_to_slack('https://example.invalid', '*미나토구 港区* JST')
        self.assertNotRegex(sent[0], JP)
        self.assertNotIn('*',sent[0])
        self.assertIn('일본 시간',sent[0])

    def test_no_observations_does_not_claim_no_impact(self):
        from datetime import datetime
        header=disaster.build_store_header([],24,datetime.now(disaster.JST))
        self.assertNotIn('영향 없음',header)
        self.assertIn('확인하지 못',header)

    def test_unknown_holiday_and_missing_forecast_are_explicit(self):
        msg=weather.build_message('2026-09-22','未知祝日','날씨 확인 불가',{})
        self.assertNotRegex(msg,JP)
        self.assertIn('확인 필요',msg)
        with patch('urllib.request.urlopen',return_value=Response({})):
            msg=weather.fetch_weather()
        self.assertIn('예보 확인 불가',msg)
        self.assertNotIn('0%',msg)

    def test_known_place_and_unknown_name_are_readable_without_japanese(self):
        self.assertEqual(jp_ko.ko_place('熊本県熊本地方'), '구마모토현 구마모토 지방')
        self.assertEqual(jp_ko.ko_place('日向灘'), '휴가나다 해역')
        self.assertNotRegex(jp_ko.ko_point('東京千代田区大手町'), JP)
        self.assertNotRegex(jp_ko.ko_place('未知地域'), JP)

    def test_holiday_translation_and_new_year_lookahead(self):
        msg = weather.build_message('2026-12-31', '国民の休日', '맑음',
                                    {'2027-01-01':'元日', '2026-12-30':'振替休日'})
        self.assertIn('국민의 휴일',msg)
        self.assertIn('2027-01-01',msg)
        self.assertIn('새해 첫날',msg)
        self.assertNotIn('2026-12-30',msg)
        self.assertNotRegex(msg,JP)

    def test_forecast_includes_dayparts_feels_like_gust_and_tomorrow(self):
        with patch('urllib.request.urlopen', return_value=Response(forecast())):
            msg=weather.fetch_weather()
        for value in ['40%', '70%', '80%', '31℃', '9m/s', '내일', '29℃', '12mm']:
            self.assertIn(value,msg)
        self.assertNotIn('99%',msg)
        self.assertNotIn('99mm',msg)

    def test_missing_weather_is_unknown_not_zero(self):
        data=forecast()
        data['current']['apparent_temperature']=None
        data['hourly']['precipitation_probability'][0]=None
        data['hourly']['precipitation_probability'][1]=None
        data['daily']['wind_gusts_10m_max'][0]=None
        with patch('urllib.request.urlopen', return_value=Response(data)):
            msg=weather.fetch_weather()
        self.assertIn('체감 확인 불가',msg)
        self.assertIn('최대 돌풍 확인 불가',msg)
        self.assertIn('아침',msg)
        self.assertIn('강수 확률 확인 불가',msg)

    def test_duplicate_reports_keep_complete_event_and_distinct_epicentres(self):
        self.assertTrue(callable(getattr(disaster,'unique_quakes',None)), '지진 속보 중복 제거 필요')
        a={'earthquake': {'time':'2026/09/22 01:39:00','maxScale':30,
                         'hypocenter':{'name':'熊本県熊本地方','magnitude':3.5,'depth':0}}}
        b=copy.deepcopy(a); b['earthquake']['maxScale']=-1
        c=copy.deepcopy(a); c['earthquake']['hypocenter']={'name':'','magnitude':-1}
        other=copy.deepcopy(a); other['earthquake']['hypocenter']['name']='福島県沖'
        result=disaster.unique_quakes([b,c,a,other])
        self.assertEqual(len(result),2)
        self.assertEqual(result[0]['earthquake']['maxScale'],30)
        self.assertEqual(result[0]['earthquake']['hypocenter']['magnitude'],3.5)

if __name__ == '__main__': unittest.main()
