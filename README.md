# Tokyo morning briefing

One daily Slack Workflow message, scheduled for 08:00 Japan/Korea time.
GitHub Actions may run later than the requested time. The existing
`SLACK_WEBHOOK_URL` secret and `{"message": ...}` Workflow payload are retained.

## Data and display

- Japan Meteorological Agency XML forecasts: Tokyo district `130010` for the
  official 06–12, 12–18, and 18–24 precipitation probabilities and 3-hour weather;
  Tokyo station `44132` for temperatures.
- Match every forecast to its **target date**, not its position in an array.
  Tomorrow's minimum is never substituted for today's.
- The latest forecast supplies each field. Today's minimum may only be available
  in the previous evening's forecast; that issue time is disclosed beneath the
  message. These are forecasts, not observed temperatures. The JMA morning
  minimum is not a guarantee of the final midnight-to-midnight minimum.
- Missing values are marked unavailable, never zero. Forecasts issued in the
  future, training reports and stale data are rejected.
- Three rows only: morning, afternoon, evening. No current/feels-like/wind values,
  dawn row or tomorrow forecast. Rain/snow intensity is displayed only when
  explicitly present in the source weather description, never inferred from
  precipitation probability. Otherwise show rain/snow expected.
- Show a holiday section only when today is a holiday; upcoming holidays only
  for the next seven days. A failed or out-of-coverage calendar is unknown.
- Weather source: https://www.data.jma.go.jp/developer/xml/feed/regular_l.xml
- Holiday source: https://holidays-jp.github.io/api/v1/date.json

## Typhoons and retired earthquake messages

No earthquake messages or routine disaster summary. Both the old disaster
workflow schedule and its script entry point are disabled.

Include a typhoon section in the morning briefing only when:

1. JMA currently lists it as a numbered typhoon.
2. A dedicated national/regional/prefectural typhoon advisory is operational,
   not cancelled, and issued within 18 hours.
3. Its headline explicitly calls for vigilance/preparation. A landfall report
   alone, including landfall outside Japan, does not qualify.

Formation notices, position bulletins, forecast tracks and storm probability
products alone never trigger a message. The Japanese regional advisory is the
Japan-impact evidence; no arbitrary distance from Tokyo is used. A forecast
warning may qualify before landfall, and the message describes an advisory,
not a claim that damage is occurring. Latest notices supersede older notices
including cancellation/end-of-impact notices. No separate urgent messages.
If the source fails, log a workflow warning; do not claim there are no typhoons.

Source: https://www.data.jma.go.jp/developer/xml/feed/extra_l.xml and
https://www.jma.go.jp/bosai/typhoon/data/targetTc.json .

## Validation

```sh
python -m unittest discover -s scripts -p 'test_*.py'
python scripts/daily_briefing.py --dry-run
```

Manual workflow runs default to preview only. A scheduled run sends the briefing.
Tests use recorded public JMA XML from 2026-10-19/20 in `scripts/fixtures`.
Legacy formatting helpers are retained for compatibility, but operational entry
points route exclusively to `daily_briefing.py`.
