from datetime import datetime, timedelta

import pandas as pd
import pytest

from greatest_trader.timeutil import (ServerClock, detect_server_mode, is_us_dst_utc, market_index_to_utc,
                                      market_to_utc, us_dst_bounds_utc, utc_index_to_market, utc_to_market)


def test_dst_bounds_known_years():
    assert us_dst_bounds_utc(2021) == (datetime(2021, 3, 14, 7), datetime(2021, 11, 7, 6))
    assert us_dst_bounds_utc(2024) == (datetime(2024, 3, 10, 7), datetime(2024, 11, 3, 6))
    assert us_dst_bounds_utc(2006) == (datetime(2006, 4, 2, 7), datetime(2006, 10, 29, 6))


def test_dst_matches_zoneinfo():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        ny = zoneinfo.ZoneInfo("America/New_York")
    except Exception:  # tz database missing (e.g. Windows without tzdata)
        pytest.skip("no tz database")
    t = datetime(2001, 1, 1)
    while t < datetime(2030, 1, 1):
        aware = t.replace(tzinfo=zoneinfo.ZoneInfo("UTC")).astimezone(ny)
        assert is_us_dst_utc(t) == (aware.utcoffset() == timedelta(hours=-4)), t
        # market time = NY + 7h
        assert utc_to_market(t) == aware.replace(tzinfo=None) + timedelta(hours=7)
        t += timedelta(hours=37)


def test_market_roundtrip_scalar_and_vector():
    times = [datetime(2020, 1, 15, 3), datetime(2020, 7, 1, 23, 45), datetime(2019, 11, 4, 10)]
    for t in times:
        assert market_to_utc(utc_to_market(t)) == t
    idx = pd.DatetimeIndex(times)
    m = utc_index_to_market(idx)
    assert list(m) == [pd.Timestamp(utc_to_market(t)) for t in times]
    assert list(market_index_to_utc(m)) == list(idx)


def test_server_clock_fixed_and_ny7():
    t_server = datetime(2021, 6, 10, 9, 0)  # summer
    ny7 = ServerClock("ny7")
    assert ny7.server_to_utc(t_server) == datetime(2021, 6, 10, 6, 0)
    assert ny7.server_to_market(t_server) == t_server
    fixed = ServerClock("fixed", 9)  # e.g. a GMT+9 broker
    assert fixed.server_to_utc(t_server) == datetime(2021, 6, 10, 0, 0)
    assert fixed.server_to_market(t_server) == datetime(2021, 6, 10, 3, 0)


def test_detect_server_mode():
    winter_utc = datetime(2022, 1, 12, 10, 0)
    assert detect_server_mode(winter_utc + timedelta(hours=2), winter_utc) == ("ny7", 2.0)
    summer_utc = datetime(2022, 7, 12, 10, 0)
    assert detect_server_mode(summer_utc + timedelta(hours=3), summer_utc) == ("ny7", 3.0)
    assert detect_server_mode(summer_utc + timedelta(hours=2), summer_utc) == ("fixed", 2.0)
    assert detect_server_mode(summer_utc, summer_utc) == ("fixed", 0.0)
