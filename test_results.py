"""Extraction tests built from the real wording of MoSPI CPI releases (2025 and 2026)."""
from datetime import datetime

import pytest

import pravin_results as R
from pravin_quant import IST

SEP_2026 = """PRESS RELEASE OF CONSUMER PRICE INDEX ON BASE 2024=100 FOR SEPTEMBER, 2026
I. Key Statistics A. National Level Indices
• Year-on-year inflation rate based on All India Consumer Price Index (CPI) with base year 2024 for the
month of September, 2026 over September, 2025 is 5.54% (Provisional). Corresponding inflation rates for rural
and urban are 6.08% and 4.88%, respectively.
CFPI 110.74 110.72 110.73 112.21 112.10 112.17
Highlights Retail inflation based on Consumer Price Index in September, 2026 is 5.54%
Year on year food inflation, based on Consumer Food Price Index, in September, 2026 is 7.74%
• Food Inflation: - Year-on-year inflation rate based on All India Consumer Food Price Index (CFPI) for the
month of September, 2026 over September, 2025 is 7.74% (Provisional). Corresponding inflation rates for rural
and urban are 8.10% and 7.20%, respectively."""

JUL_2026 = """• Year-on-year inflation rate based on All India Consumer Price Index (CPI) with base year 2024 for the
month of July, 2026 over July, 2025 is 4.45%(Provisional). Corresponding inflation rates for rural and urban
are 4.84% and 3.96%, respectively."""

SEP_2025_OLD_FORMAT = """1. Headline Inflation: Year-on-year inflation rate based on All India Consumer
Price Index (CPI) for the month of September, 2025 over September, 2024 is
1.54% (Provisional). There is decrease of 53 basis points in headline inflation"""

CPI_IW = """3. Year-on-year inflation for the month of September, 2026 stood at 5.57% as compared to
2.79% in September, 2025. Consumer Price Index for Industrial Workers (2016=100)"""


def test_reads_september_2026_and_not_the_food_figure():
    r = R.extract_cpi(SEP_2026, "september", 2026, previous=4.82)
    assert r.status == "READ" and r.value == 5.54 and (r.rural, r.urban) == (6.08, 4.88)
    assert "highlights line agrees" in r.checks and any("between rural" in c for c in r.checks)


def test_reads_july_2026_with_no_space_before_provisional():
    r = R.extract_cpi(JUL_2026, "july", 2026, previous=4.38)
    assert r.status == "READ" and r.value == 4.45


def test_old_format_without_confirmation_is_not_accepted():
    r = R.extract_cpi(SEP_2025_OLD_FORMAT, "september", 2025)
    assert r.status == "NEEDS_CHECK" and "no independent confirmation" in r.reason


def test_cpi_iw_and_food_only_text_are_never_read():
    assert R.extract_cpi(CPI_IW, "september", 2026).status == "NEEDS_CHECK"
    food_only = SEP_2026.split("Highlights")[1].replace("Retail inflation based on Consumer Price Index in September, 2026 is 5.54%", "")
    assert R.extract_cpi(food_only, "september", 2026).status == "NEEDS_CHECK"


def test_wrong_month_is_refused():
    r = R.extract_cpi(SEP_2026, "october", 2026)
    assert r.status == "NEEDS_CHECK" and "waiting for October 2026" in r.reason


def test_contradictions_are_refused():
    bad_hl = SEP_2026.replace("Consumer Price Index in September, 2026 is 5.54%", "Consumer Price Index in September, 2026 is 5.45%")
    assert "highlights say" in R.extract_cpi(bad_hl, "september", 2026).reason
    twice = SEP_2026 + (" Year-on-year inflation rate based on All India Consumer Price Index (CPI) for the month of "
                        "September, 2026 over September, 2025 is 5.34%")
    assert "disagree" in R.extract_cpi(twice, "september", 2026).reason
    outside = SEP_2026.replace("6.08% and 4.88%", "4.10% and 4.30%")
    assert "not between rural" in R.extract_cpi(outside, "september", 2026).reason


def test_implausible_jump_is_held_for_confirmation():
    r = R.extract_cpi(SEP_2026, "september", 2026, previous=1.20)
    assert r.status == "NEEDS_CHECK" and "confirm by hand" in r.reason


def test_per_cent_wording_and_html_are_handled():
    html = "<p>" + SEP_2026.replace("5.54%", "5.54 per cent") + "</p>"
    assert R.extract_cpi(html, "september", 2026).value == 5.54


@pytest.mark.parametrize("actual,prev,exp,bias", [(5.90, 5.54, None, "adverse"), (5.20, 5.54, None, "supportive"),
                                                  (5.60, 5.54, None, "neutral"), (5.90, 5.54, 6.10, "supportive")])
def test_judgement(actual, prev, exp, bias):
    j = R.judge_cpi(actual, prev, exp)
    assert j["bias"] == bias
    if exp is None:
        assert "compared with last month" in j["text"]


FEED = b"""<?xml version="1.0"?><rss><channel>
<item><title>Press Release of Consumer Price Index on Base 2024=100 for October, 2026</title><link>https://pib/cpi-oct</link></item>
<item><title>Consumer Price Index for Industrial Workers - October, 2026</title><link>https://pib/iw</link></item>
<item><title>Something else</title><link>https://pib/x</link></item></channel></rss>"""

OCT_TEXT = SEP_2026.replace("September", "October").replace("5.54", "5.91")


def test_end_to_end_released_event_gets_read_and_judged():
    ev = [{"date": "2026-11-12", "time_ist": "16:00", "event": "India CPI inflation for October 2026 (after the close)",
           "previous": "5.54", "expected": ""}]
    pages = {"https://pib/feed": FEED, "https://pib/cpi-oct": OCT_TEXT.encode()}
    before = R.check_released(ev, datetime(2026, 11, 12, 15, 0, tzinfo=IST), ["https://pib/feed"], pages.__getitem__, {})
    assert before == {}                                                       # not released yet: nothing happens
    after = R.check_released(ev, datetime(2026, 11, 12, 18, 0, tzinfo=IST), ["https://pib/feed"], pages.__getitem__, {})
    rec = after[R.event_key(ev[0])]
    assert rec["status"] == "READ" and rec["value"] == 5.91
    assert rec["judgement"]["bias"] == "adverse" and "hotter" in rec["judgement"]["text"]


def test_missing_release_or_broken_source_is_needs_check():
    ev = [{"date": "2026-11-12", "time_ist": "16:00", "event": "India CPI inflation for October 2026", "previous": "5.54"}]

    def broken(url):
        raise OSError("HTTP 503")
    s = R.check_released(ev, datetime(2026, 11, 12, 18, 0, tzinfo=IST), ["https://pib/feed"], broken, {})
    assert s[R.event_key(ev[0])]["status"] == "NEEDS_CHECK" and "503" in s[R.event_key(ev[0])]["reason"]


def test_rural_urban_must_be_the_very_next_sentence():
    no_ru = SEP_2026.replace("Corresponding inflation rates for rural\nand urban are 6.08% and 4.88%, respectively.", "")
    no_ru = no_ru.replace("Highlights Retail inflation based on Consumer Price Index in September, 2026 is 5.54%", "")
    r = R.extract_cpi(no_ru, "september", 2026)
    assert r.status == "NEEDS_CHECK" and r.rural is None          # food's rural/urban rates are never borrowed
