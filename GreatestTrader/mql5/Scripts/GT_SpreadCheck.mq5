//+------------------------------------------------------------------+
//| GT_SpreadCheck.mq5                                               |
//| Measures, from the broker's own tick history, the spread at the  |
//| Night Reversion decision times (M15 bar closes 00:15-02:00       |
//| market time = NY+7) and how often the EA's cost filter would let |
//| a signal through:  ATR(14,M15) >= mult x (spread + commission +  |
//| slippage)  and  spread <= max spread.                            |
//| Run it as a SCRIPT on a chart of a live or demo account of the   |
//| broker you want to check (not in the Strategy Tester).           |
//| Output: Experts log + MQL5\Files\GT_SpreadCheck_<server>.csv     |
//+------------------------------------------------------------------+
#property copyright "GreatestTrader"
#property version   "1.00"
#property script_show_inputs

input string InpSymbols        = "EURJPY,GBPJPY,USDJPY,GBPUSD,EURGBP,USDCHF,EURUSD,GBPCAD,EURCAD,AUDCAD,GBPCHF,EURAUD,CHFJPY,GBPAUD";
input string InpSymbolSuffix   = "";      // symbol suffix of this account (e.g. "." "#" "m")
input int    InpDays           = 180;     // calendar days to look back
input bool   InpServerIsNY7    = true;    // server time = NY+7 (GMT+2 / +3 with US DST). false: fixed offset below
input double InpServerGMTOffset = 0.0;    // used when InpServerIsNY7 = false (e.g. 0 for a GMT+0 server)
input double InpCommissionPerLot = 0.0;   // round-trip commission per 1 lot in ACCOUNT currency (as InpCommissionPerLot of the EA)
input double InpATRCostMult    = 2.0;     // EA cost filter multiple
input double InpSlippagePips   = 0.5;     // EA slippage allowance
input double InpMaxSpreadPips  = 5.0;     // EA spread cap

// cost per trade (spread + commission + 0.5 pips slippage, pips) assumed in the GreatestTrader backtest (median)
string g_refSym[] = {"AUDCAD","CHFJPY","EURAUD","EURCAD","EURGBP","EURJPY","EURUSD","GBPAUD","GBPCAD","GBPCHF","GBPJPY","GBPUSD","USDCHF","USDJPY"};
double g_refCost[] = {2.5, 2.9, 2.7, 2.7, 2.0, 2.2, 1.5, 3.5, 3.5, 3.1, 3.1, 2.0, 2.1, 1.6};

//--- US DST (2007 rule): second Sunday of March 07:00 UTC .. first Sunday of November 06:00 UTC
datetime NthSunday(const int year, const int mon, const int n)
  {
   MqlDateTime d;
   ZeroMemory(d);
   d.year = year; d.mon = mon; d.day = 1;
   datetime first = StructToTime(d);
   MqlDateTime f;
   TimeToStruct(first, f);
   int add = (7 - f.day_of_week) % 7;
   return first + (datetime)((add + 7 * (n - 1)) * 86400);
  }

bool IsUSDST(const datetime utc)
  {
   MqlDateTime u;
   TimeToStruct(utc, u);
   datetime start = NthSunday(u.year, 3, 2) + 7 * 3600;
   datetime end   = NthSunday(u.year, 11, 1) + 6 * 3600;
   return utc >= start && utc < end;
  }

// server time -> market time (NY+7)
datetime ServerToMarket(const datetime srv)
  {
   if(InpServerIsNY7)
      return srv;
   datetime utc = srv - (datetime)(InpServerGMTOffset * 3600.0);
   return utc + (datetime)((IsUSDST(utc) ? 3 : 2) * 3600);
  }

double RefCost(const string base)
  {
   for(int i = 0; i < ArraySize(g_refSym); i++)
      if(g_refSym[i] == base)
         return g_refCost[i];
   return 0.0;
  }

double Percentile(double &a[], const double p)
  {
   int n = ArraySize(a);
   if(n == 0)
      return 0.0;
   ArraySort(a);
   int k = (int)MathFloor(p * (n - 1));
   return a[k];
  }

//+------------------------------------------------------------------+
void OnStart()
  {
   string syms[];
   int ns = StringSplit(InpSymbols, ',', syms);
   string fn = "GT_SpreadCheck_" + AccountInfoString(ACCOUNT_SERVER) + ".csv";
   StringReplace(fn, " ", "_");
   int fh = FileOpen(fn, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(fh != INVALID_HANDLE)
      FileWrite(fh, "symbol", "samples", "spread_median_pips", "spread_p75", "spread_p90", "commission_pips",
                "cost_median_pips(spread+comm+slip)", "backtest_cost_pips", "atr_median_pips", "filter_pass_pct");
   PrintFormat("GT_SpreadCheck: server %s, %d days, server time %s, commission %.2f %s per lot round trip",
               AccountInfoString(ACCOUNT_SERVER), InpDays,
               InpServerIsNY7 ? "NY+7" : StringFormat("GMT%+.1f", InpServerGMTOffset), InpCommissionPerLot,
               AccountInfoString(ACCOUNT_CURRENCY));
   PrintFormat("%-8s %7s %8s %6s %6s %6s %9s %9s %7s %6s", "symbol", "samples", "spr_med", "p75", "p90", "comm",
               "cost_med", "bt_cost", "atr_med", "pass%");
   for(int s = 0; s < ns; s++)
     {
      string base = syms[s];
      StringTrimLeft(base);
      StringTrimRight(base);
      string sym = base + InpSymbolSuffix;
      if(!SymbolSelect(sym, true))
        {
         PrintFormat("%-8s not available at this broker (check InpSymbolSuffix)", sym);
         continue;
        }
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double point = SymbolInfoDouble(sym, SYMBOL_POINT);
      double pip = (digits == 3 || digits == 5) ? point * 10.0 : point;
      double tv = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
      double ts = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
      double pipValue = (ts > 0.0) ? tv * pip / ts : 0.0;            // account currency per pip per 1 lot
      double commPips = (pipValue > 0.0) ? InpCommissionPerLot / pipValue : 0.0;

      MqlRates r[];
      ArraySetAsSeries(r, false);
      int bars = InpDays * 96 + 40;
      int got = 0;
      for(int tryN = 0; tryN < 10 && got <= 0; tryN++)
        {
         got = CopyRates(sym, PERIOD_M15, 0, bars, r);
         if(got <= 0)
            Sleep(500);
        }
      if(got < 40)
        {
         PrintFormat("%-8s no M15 history (%d bars)", sym, got);
         continue;
        }
      double spreads[];
      double atrs[];
      int pass = 0, n = 0;
      for(int i = 15; i < got - 1; i++)
        {
         datetime mkt = ServerToMarket(r[i].time);
         MqlDateTime m;
         TimeToStruct(mkt, m);
         if(m.hour > 1 || m.day_of_week == 0 || m.day_of_week == 1 || m.day_of_week == 6)
            continue;   // decision bars: opens 00:00-01:45 market time, Tuesday-Friday (Monday is skipped by the EA)
         // ATR(14) over the closed bar i
         double sum = 0.0;
         for(int k = i - 13; k <= i; k++)
           {
            double tr = MathMax(r[k].high, r[k - 1].close) - MathMin(r[k].low, r[k - 1].close);
            sum += tr;
           }
         double atrPips = sum / 14.0 / pip;
         // first tick in the 60 s after the bar close = the moment the EA decides
         long from = ((long)r[i].time + 900) * 1000;
         MqlTick ticks[];
         int nt = CopyTicksRange(sym, ticks, COPY_TICKS_INFO, (ulong)from, (ulong)(from + 60000));
         if(nt <= 0)
            continue;
         double spr = (ticks[0].ask - ticks[0].bid) / pip;
         if(spr <= 0.0)
            continue;
         int k2 = ArraySize(spreads);
         ArrayResize(spreads, k2 + 1);
         ArrayResize(atrs, k2 + 1);
         spreads[k2] = spr;
         atrs[k2] = atrPips;
         n++;
         if(spr <= InpMaxSpreadPips && atrPips >= InpATRCostMult * (spr + commPips + InpSlippagePips))
            pass++;
        }
      if(n == 0)
        {
         PrintFormat("%-8s no tick history at the decision times (the broker may not provide ticks this far back)", sym);
         continue;
        }
      double med = Percentile(spreads, 0.5), p75 = Percentile(spreads, 0.75), p90 = Percentile(spreads, 0.9);
      double atrMed = Percentile(atrs, 0.5);
      double passPct = 100.0 * pass / n;
      PrintFormat("%-8s %7d %8.2f %6.2f %6.2f %6.2f %9.2f %9.2f %7.2f %5.0f%%", base, n, med, p75, p90, commPips,
                  med + commPips + InpSlippagePips, RefCost(base), atrMed, passPct);
      if(fh != INVALID_HANDLE)
         FileWrite(fh, base, n, DoubleToString(med, 2), DoubleToString(p75, 2), DoubleToString(p90, 2),
                   DoubleToString(commPips, 2), DoubleToString(med + commPips + InpSlippagePips, 2), DoubleToString(RefCost(base), 1),
                   DoubleToString(atrMed, 2), DoubleToString(passPct, 0));
     }
   if(fh != INVALID_HANDLE)
     {
      FileClose(fh);
      PrintFormat("written: MQL5\\Files\\%s", fn);
     }
   Print("Guide: cost_med (spread + commission + slippage) should be at or below bt_cost (the cost assumed in the backtest), "
         "especially for GBPCAD, EURCAD, AUDCAD and GBPCHF. A pass% far below ~50% means most signals are filtered.");
  }
//+------------------------------------------------------------------+
