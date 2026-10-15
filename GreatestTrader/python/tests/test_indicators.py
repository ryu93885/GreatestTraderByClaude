import numpy as np
import pandas as pd

from greatest_trader.indicators import atr, bollinger, sma


def test_sma_and_bollinger_population_std():
    c = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    mid, up, lo = bollinger(c, 5, 2.0)
    assert mid.iloc[-1] == 3.0
    sd = np.sqrt(((c - 3.0) ** 2).sum() / 5)  # population std (MT5 iBands)
    assert np.isclose(up.iloc[-1], 3.0 + 2 * sd)
    assert np.isclose(lo.iloc[-1], 3.0 - 2 * sd)
    assert sma(c, 3).iloc[-1] == 4.0
    assert np.isnan(mid.iloc[3])


def test_atr_simple_average_of_true_range():
    h = pd.Series([10.0, 11.0, 12.0, 11.5])
    l = pd.Series([9.0, 10.0, 10.5, 10.0])
    c = pd.Series([9.5, 10.5, 11.0, 10.2])
    a = atr(h, l, c, 3)
    # TR: bar1 = max(1, 1.5, 0.5)=1.5 ; bar2 = max(1.5, 1.5, 0)=1.5 ; bar3 = max(1.5, 0.5, 1.0)=1.5
    assert np.isnan(a.iloc[2])  # first bar has no TR -> needs 3 TRs
    assert np.isclose(a.iloc[3], 1.5)
