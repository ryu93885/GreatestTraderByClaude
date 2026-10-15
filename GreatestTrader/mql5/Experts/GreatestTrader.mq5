//+------------------------------------------------------------------+
//|                                              GreatestTrader.mq5  |
//|   Multi-strategy FX portfolio EA (MT5 Strategy Tester edition)   |
//|                                                                  |
//|   Strategy 1  Night Reversion (NR)  - core                       |
//|   Strategy 2  Euro Session   (ES)  - satellite                   |
//|   Logic is identical to the Python production engine.            |
//+------------------------------------------------------------------+
#property copyright   "GreatestTrader"
#property version     "1.10"
#property description "Night Reversion (14 crosses, 00:00-02:00 market time) + Euro Session (EURUSD 07-13 UTC)."
#property description "Attach to ONE chart (any symbol, any timeframe). The EA trades all configured symbols."

#include <Trade/Trade.mqh>

//--- enums
enum ENUM_GT_RISK
  {
   GT_RISK_CONSERVATIVE = 0, // Conservative 0.5% per trade
   GT_RISK_STANDARD     = 1, // Standard 1.0% per trade
   GT_RISK_AGGRESSIVE   = 2, // Aggressive 1.5% per trade (5%/month target)
   GT_RISK_CUSTOM       = 3  // Custom (InpRiskNR / InpRiskES)
  };

enum ENUM_GT_SERVER_TIME
  {
   GT_SERVER_NY7   = 0, // GMT+2 winter / GMT+3 summer (US DST) - most brokers
   GT_SERVER_FIXED = 1  // Fixed GMT offset (InpServerGMTOffset)
  };

//--- inputs
input group "=== Risk management ==="
input ENUM_GT_RISK InpRiskProfile      = GT_RISK_STANDARD; // Risk profile
input double       InpRiskNR           = 1.0;   // Custom: NR risk per trade (% of equity)
input double       InpRiskES           = 1.0;   // Custom: ES risk per trade (% of equity)
input int          InpMaxOpen          = 10;    // Max open positions (total)
input int          InpMaxPerCurrency   = 4;     // Max positions per currency
input double       InpDDScaleFull      = 30.0;  // DD scaling: risk x max(floor, 1 - DD%/this)
input double       InpDDScaleFloor     = 0.30;  // DD scaling: minimum multiplier
input double       InpDailyLossLimit   = 5.0;   // Daily loss limit (% of day-start equity)
input double       InpMaxDDHalt        = 40.0;  // Halt new entries at this DD (%) from peak
input bool         InpUseMinLot        = false; // Use min lot when calculated lot is smaller
input double       InpMaxTotalRisk     = 0.0;   // Max total open risk (% of equity, 0 = off)
input int          InpMaxSameDirPerCcy = 0;     // Max same-direction positions per currency (0 = off)
input double       InpDailyHardStop    = 0.0;   // Daily hard stop: close all at this loss (%, 0 = off)
input double       InpMonthlyLossLimit = 0.0;   // Monthly loss limit: no new entries (%, 0 = off)
input double       InpMonthlyHardStop  = 0.0;   // Monthly hard stop: close all at this loss (%, 0 = off)
input string       InpSymbolRiskMult   = "";    // Per-symbol risk multipliers, e.g. EURUSD:0.5,EURJPY:0.5
input double       InpVolTargetDaily   = 0.0;   // Volatility target, daily equity vol (%, 0 = off)
input int          InpVolLookbackDays  = 20;    // Volatility target lookback (days)
input double       InpVolMultMin       = 0.25;  // Volatility target min multiplier
input double       InpVolMultMax       = 1.5;   // Volatility target max multiplier
input int          InpEqFilterDays     = 0;     // Equity-curve filter SMA (days, 0 = off)
input double       InpEqFilterMult     = 0.5;   // Equity-curve filter multiplier below SMA

input group "=== Broker ==="
input string              InpSymbolSuffix     = "";             // Symbol suffix (e.g. .raw  -STD  m)
input ENUM_GT_SERVER_TIME InpServerTime       = GT_SERVER_NY7;  // Broker server time convention
input double              InpServerGMTOffset  = 2.0;            // Fixed GMT offset (hours) if Fixed
input double              InpCommissionPerLot = 0.0;            // Round-trip commission per lot (account ccy)
input int                 InpDeviationPoints  = 20;             // Max slippage (points)

input group "=== Strategy 1: Night Reversion ==="
input bool   InpNREnabled       = true;  // Enable Night Reversion
input string InpNRSymbols       = "EURJPY,GBPJPY,USDJPY,GBPUSD,EURGBP,USDCHF,EURUSD,GBPCAD,EURCAD,AUDCAD,GBPCHF,EURAUD,CHFJPY,GBPAUD"; // Symbols (comma separated)
input int    InpBBPeriod        = 15;    // Bollinger period (M15)
input double InpBBDev           = 1.8;   // Bollinger deviation
input int    InpATRPeriod       = 14;    // ATR period (M15)
input double InpATRCostMult     = 2.0;   // Min ATR / trading cost ratio
input double InpSLATRMult       = 3.0;   // Stop loss = ATR x this
input int    InpMaxHoldBars     = 12;    // Time stop (M15 bars)
input int    InpEntryHour1      = 0;     // Entry hour 1 (market time = NY+7)
input int    InpEntryHour2      = 1;     // Entry hour 2 (-1 = off)
input bool   InpSkipMonday      = true;  // Skip Monday
input double InpNRSlippagePips  = 0.5;   // Slippage allowance in cost filter (pips)
input double InpNRMaxSpreadPips = 5.0;   // Max spread (pips)
input double InpMaxBandZ        = 0.0;   // News filter: skip if |close-mid| > this x stdev (0 = off)
input double InpMaxBarRangeATR  = 0.0;   // News filter: skip if bar range > this x ATR (0 = off)
input long   InpNRMagic         = 7101;  // Magic number NR

input group "=== Strategy 2: Euro Session ==="
input bool   InpESEnabled       = true;     // Enable Euro Session
input string InpESSymbol        = "EURUSD"; // Symbol
input int    InpESDirection     = -1;       // Direction (-1 sell, 1 buy)
input int    InpESEntryUTC      = 7;        // Entry hour (UTC)
input int    InpESExitUTC       = 13;       // Exit hour (UTC)
input int    InpESATRPeriodH1   = 24;       // ATR period (H1)
input double InpESSLATRMult     = 3.0;      // Stop loss = ATR(H1) x this
input double InpESMaxSpreadPips = 2.0;      // Max spread (pips)
input long   InpESMagic         = 7102;     // Magic number ES

input group "=== Misc ==="
input bool InpPushNotify  = false; // Push notifications (MetaQuotes ID required)
input int  InpTimerSec    = 5;     // Timer (seconds, live). Tester uses 60s
input bool InpVerbose     = false; // Verbose log

//--- globals
CTrade   g_trade;
string   g_sym[];      // broker symbol names (NR)
string   g_base[];     // base names (NR)
datetime g_lastBar[];  // last processed closed M15 bar per NR symbol
int      g_nsym = 0;
string   g_esSym = "";
bool     g_esOk = false;
double   g_peak = 0.0;
datetime g_day = 0;
double   g_dayStartEq = 0.0;
bool     g_halted = false;
datetime g_lastESDay = 0;
string   g_gvPrefix = "";
bool     g_tester = false;
datetime g_month = 0;            // market-time month start
double   g_monthStartEq = 0.0;
bool     g_monthHalted = false;
bool     g_dayHalted = false;
double   g_dailyEq[];            // closing equity of past market days (oldest first)
double   g_lastEq = 0.0;
double   g_adaptMult = 1.0;
bool     g_flatten = false;      // set when a hard stop requires closing all positions
string   g_flattenReason = "";
string   g_multSym[];
double   g_multVal[];

//+------------------------------------------------------------------+
//| Time utilities (US DST rules, identical to Python timeutil.py)    |
//+------------------------------------------------------------------+
datetime MakeDate(const int y, const int m, const int d)
  {
   MqlDateTime s;
   s.year = y; s.mon = m; s.day = d; s.hour = 0; s.min = 0; s.sec = 0;
   s.day_of_week = 0; s.day_of_year = 0;
   return StructToTime(s);
  }

datetime NthSunday(const int y, const int m, const int n)
  {
   datetime first = MakeDate(y, m, 1);
   MqlDateTime s;
   TimeToStruct(first, s);
   int add = (7 - s.day_of_week) % 7;          // day_of_week: 0 = Sunday
   return first + (add + 7 * (n - 1)) * 86400;
  }

datetime LastSunday(const int y, const int m)
  {
   int ny = (m == 12) ? y + 1 : y;
   int nm = (m == 12) ? 1 : m + 1;
   datetime last = MakeDate(ny, nm, 1) - 86400;
   MqlDateTime s;
   TimeToStruct(last, s);
   return last - s.day_of_week * 86400;
  }

bool IsUSDST(const datetime utc)
  {
   MqlDateTime s;
   TimeToStruct(utc, s);
   datetime st, en;
   if(s.year >= 2007)
     {
      st = NthSunday(s.year, 3, 2) + 7 * 3600;
      en = NthSunday(s.year, 11, 1) + 6 * 3600;
     }
   else
     {
      st = NthSunday(s.year, 4, 1) + 7 * 3600;
      en = LastSunday(s.year, 10) + 6 * 3600;
     }
   return (utc >= st && utc < en);
  }

datetime ServerToUTC(const datetime srv)
  {
   if(InpServerTime == GT_SERVER_FIXED)
      return srv - (int)MathRound(InpServerGMTOffset * 3600.0);
   datetime cand = srv - 3 * 3600;
   if(IsUSDST(cand))
      return cand;
   return srv - 2 * 3600;
  }

datetime UTCToMarket(const datetime utc)
  {
   return utc + (IsUSDST(utc) ? 3 : 2) * 3600;
  }

datetime ServerToMarket(const datetime srv)
  {
   if(InpServerTime == GT_SERVER_NY7)
      return srv;
   return UTCToMarket(ServerToUTC(srv));
  }

datetime DayStart(const datetime t)
  {
   return t - (t % 86400);
  }

datetime Floor15(const datetime t)
  {
   return t - (t % 900);
  }

//+------------------------------------------------------------------+
//| Symbol helpers                                                    |
//+------------------------------------------------------------------+
double PipSize(const string base, const int digits)
  {
   if(StringFind(base, "XAU") == 0)
      return 0.1;
   if(StringSubstr(base, 3, 3) == "JPY")
      return 0.01;
   if(digits == 2 || digits == 3)
      return 0.01;
   return 0.0001;
  }

string BaseOf(const string sym)
  {
   return StringSubstr(sym, 0, 6);
  }

double SymPip(const string sym)
  {
   return PipSize(BaseOf(sym), (int)SymbolInfoInteger(sym, SYMBOL_DIGITS));
  }

void Log(const string msg, const bool notify = false)
  {
   Print("[GT] ", msg);
   if(notify && InpPushNotify && !g_tester)
      SendNotification("[GT] " + msg);
  }

//+------------------------------------------------------------------+
//| Risk state (persisted in terminal global variables)              |
//+------------------------------------------------------------------+
void SaveState()
  {
   GlobalVariableSet(g_gvPrefix + "peak", g_peak);
   GlobalVariableSet(g_gvPrefix + "day", (double)g_day);
   GlobalVariableSet(g_gvPrefix + "dayeq", g_dayStartEq);
   GlobalVariableSet(g_gvPrefix + "halt", g_halted ? 1.0 : 0.0);
   GlobalVariableSet(g_gvPrefix + "esday", (double)g_lastESDay);
   GlobalVariableSet(g_gvPrefix + "month", (double)g_month);
   GlobalVariableSet(g_gvPrefix + "montheq", g_monthStartEq);
   GlobalVariableSet(g_gvPrefix + "monthhalt", g_monthHalted ? 1.0 : 0.0);
   GlobalVariableSet(g_gvPrefix + "dayhalt", g_dayHalted ? 1.0 : 0.0);
   GlobalVariableSet(g_gvPrefix + "lasteq", g_lastEq);
   int n = ArraySize(g_dailyEq);
   GlobalVariableSet(g_gvPrefix + "deqn", (double)n);
   for(int i = 0; i < n; i++)
      GlobalVariableSet(g_gvPrefix + "deq" + IntegerToString(i), g_dailyEq[i]);
  }

void LoadState()
  {
   if(GlobalVariableCheck(g_gvPrefix + "peak"))
      g_peak = GlobalVariableGet(g_gvPrefix + "peak");
   if(GlobalVariableCheck(g_gvPrefix + "day"))
      g_day = (datetime)GlobalVariableGet(g_gvPrefix + "day");
   if(GlobalVariableCheck(g_gvPrefix + "dayeq"))
      g_dayStartEq = GlobalVariableGet(g_gvPrefix + "dayeq");
   if(GlobalVariableCheck(g_gvPrefix + "halt"))
      g_halted = (GlobalVariableGet(g_gvPrefix + "halt") > 0.5);
   if(GlobalVariableCheck(g_gvPrefix + "esday"))
      g_lastESDay = (datetime)GlobalVariableGet(g_gvPrefix + "esday");
   if(GlobalVariableCheck(g_gvPrefix + "month"))
      g_month = (datetime)GlobalVariableGet(g_gvPrefix + "month");
   if(GlobalVariableCheck(g_gvPrefix + "montheq"))
      g_monthStartEq = GlobalVariableGet(g_gvPrefix + "montheq");
   if(GlobalVariableCheck(g_gvPrefix + "monthhalt"))
      g_monthHalted = (GlobalVariableGet(g_gvPrefix + "monthhalt") > 0.5);
   if(GlobalVariableCheck(g_gvPrefix + "dayhalt"))
      g_dayHalted = (GlobalVariableGet(g_gvPrefix + "dayhalt") > 0.5);
   if(GlobalVariableCheck(g_gvPrefix + "lasteq"))
      g_lastEq = GlobalVariableGet(g_gvPrefix + "lasteq");
   if(GlobalVariableCheck(g_gvPrefix + "deqn"))
     {
      int n = (int)GlobalVariableGet(g_gvPrefix + "deqn");
      ArrayResize(g_dailyEq, 0);
      for(int i = 0; i < n; i++)
        {
         string name = g_gvPrefix + "deq" + IntegerToString(i);
         if(!GlobalVariableCheck(name))
            break;
         int k = ArraySize(g_dailyEq);
         ArrayResize(g_dailyEq, k + 1);
         g_dailyEq[k] = GlobalVariableGet(name);
        }
     }
   g_adaptMult = AdaptiveMult();
  }

double BaseRisk(const bool isNR)
  {
   switch(InpRiskProfile)
     {
      case GT_RISK_CONSERVATIVE:
         return 0.005;
      case GT_RISK_STANDARD:
         return 0.010;
      case GT_RISK_AGGRESSIVE:
         return 0.015;
      default:
         return (isNR ? InpRiskNR : InpRiskES) / 100.0;
     }
  }

double Drawdown(const double eq)
  {
   if(g_peak <= 0.0)
      return 0.0;
   return MathMax(0.0, 1.0 - eq / g_peak);
  }

double RiskFraction(const bool isNR, const double eq)
  {
   double mult = 1.0;
   if(InpDDScaleFull > 0.0)
      mult = MathMax(InpDDScaleFloor, 1.0 - Drawdown(eq) / (InpDDScaleFull / 100.0));
   return BaseRisk(isNR) * mult * g_adaptMult;
  }

// volatility-target and equity-curve multipliers (identical to Python RiskManager)
double AdaptiveMult()
  {
   double mult = 1.0;
   int n = ArraySize(g_dailyEq);
   int minDays = (InpVolLookbackDays / 2 > 5) ? InpVolLookbackDays / 2 : 5;
   if(InpVolTargetDaily > 0.0 && n >= minDays)
     {
      int first = (int)MathMax(0, n - (InpVolLookbackDays + 1));
      double rets[];
      int m = 0;
      for(int i = first + 1; i < n; i++)
        {
         if(g_dailyEq[i - 1] <= 0.0)
            continue;
         ArrayResize(rets, m + 1);
         rets[m] = g_dailyEq[i] / g_dailyEq[i - 1] - 1.0;
         m++;
        }
      if(m >= 2)
        {
         double mu = 0.0;
         for(int i = 0; i < m; i++)
            mu += rets[i];
         mu /= m;
         double var = 0.0;
         for(int i = 0; i < m; i++)
            var += (rets[i] - mu) * (rets[i] - mu);
         double sd = MathSqrt(var / (m - 1));
         if(sd > 0.0)
            mult *= MathMin(InpVolMultMax, MathMax(InpVolMultMin, (InpVolTargetDaily / 100.0) / sd));
        }
     }
   if(InpEqFilterDays > 0 && n >= InpEqFilterDays)
     {
      double sum = 0.0;
      for(int i = n - InpEqFilterDays; i < n; i++)
         sum += g_dailyEq[i];
      if(g_dailyEq[n - 1] < sum / InpEqFilterDays)
         mult *= InpEqFilterMult;
     }
   return mult;
  }

datetime MonthStart(const datetime t)
  {
   MqlDateTime s;
   TimeToStruct(t, s);
   return MakeDate(s.year, s.mon, 1);
  }

void UpdateEquityState(const double eq, const datetime nowSrv)
  {
   g_flatten = false;
   g_flattenReason = "";
   if(eq > g_peak)
      g_peak = eq;
   datetime mkt = ServerToMarket(nowSrv);
   datetime mday = DayStart(mkt);
   if(mday != g_day)
     {
      if(g_day != 0 && g_lastEq > 0.0)
        {
         int n = ArraySize(g_dailyEq);
         ArrayResize(g_dailyEq, n + 1);
         g_dailyEq[n] = g_lastEq;
         int keep = ((InpVolLookbackDays > InpEqFilterDays) ? InpVolLookbackDays : InpEqFilterDays) + 2;
         int sz = ArraySize(g_dailyEq);
         if(sz > keep)
           {
            double tmp[];
            ArrayResize(tmp, keep);
            for(int i = 0; i < keep; i++)
               tmp[i] = g_dailyEq[sz - keep + i];
            ArrayResize(g_dailyEq, keep);
            for(int i = 0; i < keep; i++)
               g_dailyEq[i] = tmp[i];
           }
        }
      g_day = mday;
      g_dayStartEq = eq;
      g_dayHalted = false;
      g_adaptMult = AdaptiveMult();
     }
   g_lastEq = eq;
   datetime mmon = MonthStart(mkt);
   if(mmon != g_month)
     {
      g_month = mmon;
      g_monthStartEq = eq;
      g_monthHalted = false;
     }
   double dd = Drawdown(eq);
   if(!g_halted && InpMaxDDHalt > 0.0 && dd * 100.0 >= InpMaxDDHalt)
     {
      g_halted = true;
      Log(StringFormat("HALT: drawdown %.1f%% reached. New entries stopped. Delete global variables %s* to reset.",
                       dd * 100.0, g_gvPrefix), true);
     }
   if(InpMonthlyHardStop > 0.0 && !g_monthHalted && g_monthStartEq > 0.0 &&
      eq <= g_monthStartEq * (1.0 - InpMonthlyHardStop / 100.0))
     {
      g_monthHalted = true;
      g_flatten = true;
      g_flattenReason = StringFormat("monthly hard stop %.1f%%", InpMonthlyHardStop);
      Log("MONTH STOP: " + g_flattenReason, true);
     }
   if(InpDailyHardStop > 0.0 && !g_dayHalted && g_dayStartEq > 0.0 &&
      eq <= g_dayStartEq * (1.0 - InpDailyHardStop / 100.0))
     {
      g_dayHalted = true;
      g_flatten = true;
      if(g_flattenReason == "")
         g_flattenReason = StringFormat("daily hard stop %.1f%%", InpDailyHardStop);
      Log(StringFormat("DAY STOP: daily hard stop %.1f%%", InpDailyHardStop), true);
     }
  }

double SymbolMult(const string sym)
  {
   string b = BaseOf(sym);
   for(int i = 0; i < ArraySize(g_multSym); i++)
      if(g_multSym[i] == b)
         return g_multVal[i];
   return 1.0;
  }

void ParseSymbolMult()
  {
   ArrayResize(g_multSym, 0);
   ArrayResize(g_multVal, 0);
   string items[];
   int n = StringSplit(InpSymbolRiskMult, ',', items);
   for(int i = 0; i < n; i++)
     {
      string kv[];
      if(StringSplit(items[i], ':', kv) != 2)
         continue;
      string k = kv[0];
      StringTrimLeft(k);
      StringTrimRight(k);
      StringToUpper(k);
      int m = ArraySize(g_multSym);
      ArrayResize(g_multSym, m + 1);
      ArrayResize(g_multVal, m + 1);
      g_multSym[m] = k;
      g_multVal[m] = StringToDouble(kv[1]);
     }
  }

void CloseAllOurs(const string reason)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         ClosePos(tk, reason);
     }
  }

bool DailyLimitHit(const double eq)
  {
   if(InpDailyLossLimit <= 0.0 || g_dayStartEq <= 0.0)
      return false;
   return eq <= g_dayStartEq * (1.0 - InpDailyLossLimit / 100.0);
  }

//+------------------------------------------------------------------+
//| Position helpers                                                  |
//+------------------------------------------------------------------+
bool IsOurMagic(const long magic)
  {
   return (magic == InpNRMagic || magic == InpESMagic);
  }

int CountOurPositions()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         n++;
     }
   return n;
  }

bool HasPositionOnSymbol(const string sym)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(IsOurMagic(PositionGetInteger(POSITION_MAGIC)) && PositionGetString(POSITION_SYMBOL) == sym)
         return true;
     }
   return false;
  }

int CountCurrency(const string ccy)
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         continue;
      string b = BaseOf(PositionGetString(POSITION_SYMBOL));
      if(StringSubstr(b, 0, 3) == ccy || StringSubstr(b, 3, 3) == ccy)
         n++;
     }
   return n;
  }

bool HasESPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) == InpESMagic)
         return true;
     }
   return false;
  }

double OpenRiskTotal()
  {
   double total = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         continue;
      int dir = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
      double op = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      if(sl <= 0.0 || (dir > 0 && sl >= op) || (dir < 0 && sl <= op))
         continue;
      double l = LossPerLot(PositionGetString(POSITION_SYMBOL), dir, op, sl);
      if(l > 0.0)
         total += l * PositionGetDouble(POSITION_VOLUME);
     }
   return total;
  }

// number of open positions that are long (sign=+1) or short (sign=-1) the currency ccy
int CountCurrencyDir(const string ccy, const int sign)
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         continue;
      int dir = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
      string b = BaseOf(PositionGetString(POSITION_SYMBOL));
      if(StringSubstr(b, 0, 3) == ccy && dir == sign)
         n++;
      if(StringSubstr(b, 3, 3) == ccy && -dir == sign)
         n++;
     }
   return n;
  }

bool CanOpen(const string sym, const double eq, const int dir, const double newRisk, string &why)
  {
   if(g_halted)
     {
      why = "halted (max drawdown)";
      return false;
     }
   if(g_monthHalted)
     {
      why = "monthly stop";
      return false;
     }
   if(g_dayHalted)
     {
      why = "daily stop";
      return false;
     }
   if(DailyLimitHit(eq))
     {
      why = "daily loss limit";
      return false;
     }
   if(InpMonthlyLossLimit > 0.0 && g_monthStartEq > 0.0 && eq <= g_monthStartEq * (1.0 - InpMonthlyLossLimit / 100.0))
     {
      why = "monthly loss limit";
      return false;
     }
   if(CountOurPositions() >= InpMaxOpen)
     {
      why = "max open positions";
      return false;
     }
   if(HasPositionOnSymbol(sym))
     {
      why = "position already open on symbol";
      return false;
     }
   string b = BaseOf(sym);
   if(CountCurrency(StringSubstr(b, 0, 3)) >= InpMaxPerCurrency || CountCurrency(StringSubstr(b, 3, 3)) >= InpMaxPerCurrency)
     {
      why = "max positions per currency";
      return false;
     }
   if(InpMaxSameDirPerCcy > 0 && dir != 0 &&
      (CountCurrencyDir(StringSubstr(b, 0, 3), dir) >= InpMaxSameDirPerCcy ||
       CountCurrencyDir(StringSubstr(b, 3, 3), -dir) >= InpMaxSameDirPerCcy))
     {
      why = "max same-direction positions per currency";
      return false;
     }
   if(InpMaxTotalRisk > 0.0 && eq > 0.0 && (OpenRiskTotal() + newRisk) / eq > InpMaxTotalRisk / 100.0 + 1e-12)
     {
      why = "max total open risk";
      return false;
     }
   why = "";
   return true;
  }

double LossPerLot(const string sym, const int dir, const double entry, const double stop)
  {
   double profit = 0.0;
   ENUM_ORDER_TYPE t = (dir > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!OrderCalcProfit(t, sym, 1.0, entry, stop, profit))
      return -1.0;
   return MathAbs(profit);
  }

double CalcLots(const string sym, const double riskAmount, const double lossPerLot)
  {
   if(lossPerLot <= 0.0 || riskAmount <= 0.0)
      return 0.0;
   double vmin  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   if(vstep <= 0.0)
      vstep = 0.01;
   double raw = riskAmount / lossPerLot;
   double lots = MathFloor(raw / vstep + 1e-9) * vstep;
   if(lots < vmin)
     {
      if(InpUseMinLot && vmin * lossPerLot <= riskAmount * 1.5)
         lots = vmin;
      else
         return 0.0;
     }
   if(lots > vmax)
      lots = vmax;
   int stepDigits = (int)MathMax(0, MathCeil(-MathLog10(vstep) - 1e-9));
   return NormalizeDouble(lots, stepDigits);
  }

bool MarginOK(const string sym, const int dir, const double lots, const double price)
  {
   double need = 0.0;
   ENUM_ORDER_TYPE t = (dir > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!OrderCalcMargin(t, sym, lots, price, need))
      return false;
   double freeM = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   if(need > freeM * 0.8)
     {
      Log(StringFormat("skip %s: insufficient free margin (need %.2f, free %.2f)", sym, need, freeM), true);
      return false;
     }
   return true;
  }

double CommissionPips(const string sym, const double ask)
  {
   if(InpCommissionPerLot <= 0.0)
      return 0.0;
   double pip = SymPip(sym);
   double pv = LossPerLot(sym, 1, ask, ask - pip);
   if(pv <= 0.0)
      return 0.0;
   return InpCommissionPerLot / pv;
  }

bool ClosePos(const ulong ticket, const string reason)
  {
   if(!PositionSelectByTicket(ticket))
      return true;
   string sym = PositionGetString(POSITION_SYMBOL);
   g_trade.SetTypeFillingBySymbol(sym);
   bool ok = g_trade.PositionClose(ticket, (ulong)InpDeviationPoints);
   if(ok)
      Log(StringFormat("CLOSE %s #%s (%s)", sym, IntegerToString((long)ticket), reason), true);
   else
      Log(StringFormat("CLOSE FAILED %s #%s: %s", sym, IntegerToString((long)ticket), g_trade.ResultRetcodeDescription()), true);
   return ok;
  }

//+------------------------------------------------------------------+
//| Indicators (computed from closed bars, identical to Python)       |
//+------------------------------------------------------------------+
bool NRIndicators(const string sym, MqlRates &last, double &mid, double &upper, double &lower, double &atrv)
  {
   MqlRates r[];
   ArraySetAsSeries(r, true);
   int need = (int)MathMax(InpBBPeriod, InpATRPeriod + 1) + 2;
   int got = CopyRates(sym, PERIOD_M15, 1, need, r);   // shift 1 = last closed bar
   if(got < need)
      return false;
   last = r[0];
   double sum = 0.0;
   for(int i = 0; i < InpBBPeriod; i++)
      sum += r[i].close;
   mid = sum / InpBBPeriod;
   double var = 0.0;
   for(int i = 0; i < InpBBPeriod; i++)
      var += (r[i].close - mid) * (r[i].close - mid);
   double sd = MathSqrt(var / InpBBPeriod);            // population std (iBands)
   upper = mid + InpBBDev * sd;
   lower = mid - InpBBDev * sd;
   double s2 = 0.0;
   for(int i = 0; i < InpATRPeriod; i++)
     {
      double pc = r[i + 1].close;
      double tr = MathMax(r[i].high - r[i].low, MathMax(MathAbs(r[i].high - pc), MathAbs(r[i].low - pc)));
      s2 += tr;
     }
   atrv = s2 / InpATRPeriod;                            // SMA of true range (iATR)
   return true;
  }

double ATRH1(const string sym, const int period)
  {
   MqlRates r[];
   ArraySetAsSeries(r, true);
   int need = period + 1;
   int got = CopyRates(sym, PERIOD_H1, 1, need, r);
   if(got < need)
      return -1.0;
   double s = 0.0;
   for(int i = 0; i < period; i++)
     {
      double pc = r[i + 1].close;
      s += MathMax(r[i].high - r[i].low, MathMax(MathAbs(r[i].high - pc), MathAbs(r[i].low - pc)));
     }
   return s / period;
  }

//+------------------------------------------------------------------+
//| Night Reversion                                                   |
//+------------------------------------------------------------------+
bool InNRWindow(const datetime barMarket)
  {
   MqlDateTime s;
   TimeToStruct(barMarket, s);
   int dow = s.day_of_week;   // 0 Sunday ... 6 Saturday
   int hr = s.hour;
   if(dow == 0 || dow == 6)
      return false;
   if(InpSkipMonday && dow == 1 && hr < 2)
      return false;
   if(dow == 5 && hr >= 20)
      return false;
   return (hr == InpEntryHour1 || hr == InpEntryHour2);
  }

double FavorableTP(const int dir, const double openPrice, const double mid)
  {
   if(dir > 0 && mid > openPrice)
      return mid;
   if(dir < 0 && mid < openPrice)
      return mid;
   return 0.0;
  }

void NRTimeStops(const datetime nowSrv)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpNRMagic)
         continue;
      datetime ot = (datetime)PositionGetInteger(POSITION_TIME);
      if(nowSrv >= Floor15(ot) + InpMaxHoldBars * 900)
         ClosePos(tk, "time stop");
     }
  }

void NRManage(const string sym, const double mid, const datetime nowSrv)
  {
   MqlTick tick;
   if(!SymbolInfoTick(sym, tick))
      return;
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   long stops = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpNRMagic || PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      datetime ot = (datetime)PositionGetInteger(POSITION_TIME);
      if(nowSrv >= Floor15(ot) + InpMaxHoldBars * 900)
        {
         ClosePos(tk, "time stop");
         continue;
        }
      int dir = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
      double op = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      double curTP = PositionGetDouble(POSITION_TP);
      double tp = FavorableTP(dir, op, mid);
      if(tp > 0.0)
        {
         tp = NormalizeDouble(tp, digits);
         double px = (dir > 0) ? tick.bid : tick.ask;
         if((dir > 0 && px >= tp) || (dir < 0 && px <= tp) || MathAbs(tp - px) < (stops + 2) * point)
           {
            ClosePos(tk, "price at moving average");
            continue;
           }
        }
      if(MathAbs(tp - curTP) > point * 0.5)
        {
         if(g_trade.PositionModify(tk, sl, tp))
           {
            if(InpVerbose)
               Log(StringFormat("MODIFY %s TP -> %s", sym, DoubleToString(tp, digits)));
           }
         else
           {
            Log(StringFormat("modify failed %s: %s", sym, g_trade.ResultRetcodeDescription()));
            if(tp > 0.0)
               ClosePos(tk, "TP not placeable");
           }
        }
     }
  }

void NREntry(const string sym, const MqlRates &bar, const double mid, const double upper, const double lower,
             const double atrv, const double eq)
  {
   datetime barMkt = ServerToMarket(bar.time);
   if(!InNRWindow(barMkt))
      return;
   MqlTick tick;
   if(!SymbolInfoTick(sym, tick) || tick.bid <= 0.0 || tick.ask <= 0.0)
      return;
   double pip = SymPip(sym);
   double spread = (tick.ask - tick.bid) / pip;
   if(spread > InpNRMaxSpreadPips)
      return;
   double cost = spread + CommissionPips(sym, tick.ask) + InpNRSlippagePips;
   if(atrv / pip < InpATRCostMult * cost)
      return;
   double sd = (InpBBDev > 0.0) ? (upper - mid) / InpBBDev : 0.0;
   if(InpMaxBandZ > 0.0 && sd > 0.0 && MathAbs(bar.close - mid) > InpMaxBandZ * sd)
      return;
   if(InpMaxBarRangeATR > 0.0 && (bar.high - bar.low) > InpMaxBarRangeATR * atrv)
      return;
   int dir = 0;
   if(bar.close > upper)
      dir = -1;
   else
      if(bar.close < lower)
         dir = 1;
   if(dir == 0)
      return;
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   long stops = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double entry = (dir > 0) ? tick.ask : tick.bid;
   double sl = NormalizeDouble(entry - dir * InpSLATRMult * atrv, digits);
   double tp = FavorableTP(dir, entry, mid);
   if(tp <= 0.0)
      return;
   tp = NormalizeDouble(tp, digits);
   if(MathAbs(tp - entry) < (stops + 2) * point)
      return;
   double rf = RiskFraction(true, eq) * SymbolMult(sym);
   string why;
   if(!CanOpen(sym, eq, dir, eq * rf, why))
     {
      if(InpVerbose)
         Log(StringFormat("NR %s signal skipped: %s", sym, why));
      return;
     }
   double lossLot = LossPerLot(sym, dir, entry, sl);
   double lots = CalcLots(sym, eq * rf, lossLot);
   if(lots <= 0.0)
     {
      if(InpVerbose)
         Log(StringFormat("NR %s skipped: lot below minimum (risk %.2f)", sym, eq * rf));
      return;
     }
   if(!MarginOK(sym, dir, lots, entry))
      return;
   g_trade.SetExpertMagicNumber((ulong)InpNRMagic);
   g_trade.SetTypeFillingBySymbol(sym);
   bool ok = (dir > 0) ? g_trade.Buy(lots, sym, 0.0, sl, tp, "GT_NR") : g_trade.Sell(lots, sym, 0.0, sl, tp, "GT_NR");
   string desc = StringFormat("NR %s %s lots=%.2f SL=%s TP=%s risk=%.2f%% ATR=%.1fp cost=%.1fp",
                              (dir > 0 ? "BUY" : "SELL"), sym, lots, DoubleToString(sl, digits),
                              DoubleToString(tp, digits), rf * 100.0, atrv / pip, cost);
   if(ok && (g_trade.ResultRetcode() == TRADE_RETCODE_DONE || g_trade.ResultRetcode() == TRADE_RETCODE_PLACED))
      Log("OPEN " + desc, true);
   else
      Log("ORDER FAILED " + desc + ": " + g_trade.ResultRetcodeDescription(), true);
  }

void ProcessNR(const datetime nowSrv, const double eq)
  {
   NRTimeStops(nowSrv);
   for(int i = 0; i < g_nsym; i++)
     {
      string sym = g_sym[i];
      datetime t1 = iTime(sym, PERIOD_M15, 1);      // open time of last closed bar
      if(t1 <= 0 || t1 == g_lastBar[i])
         continue;
      MqlRates bar;
      double mid, upper, lower, atrv;
      if(!NRIndicators(sym, bar, mid, upper, lower, atrv))
         continue;
      bool first = (g_lastBar[i] == 0);
      g_lastBar[i] = bar.time;
      NRManage(sym, mid, nowSrv);
      // entries only on a fresh bar close (avoid acting on stale bars after restarts / data gaps)
      if(nowSrv - (bar.time + 900) > 150)
         continue;
      if(first && !g_tester && nowSrv - (bar.time + 900) > 60)
         continue;
      NREntry(sym, bar, mid, upper, lower, atrv, eq);
     }
  }

//+------------------------------------------------------------------+
//| Euro Session                                                      |
//+------------------------------------------------------------------+
void ProcessES(const datetime nowSrv, const double eq)
  {
   datetime utc = ServerToUTC(nowSrv);
   // manage: close at exit hour UTC
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpESMagic)
         continue;
      datetime openUtc = ServerToUTC((datetime)PositionGetInteger(POSITION_TIME));
      MqlDateTime os;
      TimeToStruct(openUtc, os);
      datetime exitUtc = DayStart(openUtc) + InpESExitUTC * 3600;
      if(os.hour >= InpESExitUTC)
         exitUtc += 86400;
      if(utc >= exitUtc)
         ClosePos(tk, "session exit");
     }
   // entry
   MqlDateTime u;
   TimeToStruct(utc, u);
   datetime uday = DayStart(utc);
   if(g_lastESDay == uday)
      return;
   if(u.day_of_week < 1 || u.day_of_week > 5 || u.hour != InpESEntryUTC || u.min >= 15)
      return;
   if(HasESPosition())
      return;
   MqlTick tick;
   if(!SymbolInfoTick(g_esSym, tick) || tick.bid <= 0.0)
      return;
   g_lastESDay = uday;   // one decision per day
   double pip = SymPip(g_esSym);
   double spread = (tick.ask - tick.bid) / pip;
   double a = ATRH1(g_esSym, InpESATRPeriodH1);
   if(a <= 0.0 || spread > InpESMaxSpreadPips)
     {
      Log(StringFormat("ES: no entry today (spread %.1fp, ATR %.1fp)", spread, a / pip));
      return;
     }
   int dir = (InpESDirection >= 0) ? 1 : -1;
   int digits = (int)SymbolInfoInteger(g_esSym, SYMBOL_DIGITS);
   double entry = (dir > 0) ? tick.ask : tick.bid;
   double sl = NormalizeDouble(entry - dir * InpESSLATRMult * a, digits);
   double rf = RiskFraction(false, eq) * SymbolMult(g_esSym);
   string why;
   if(!CanOpen(g_esSym, eq, dir, eq * rf, why))
     {
      Log("ES signal skipped: " + why);
      return;
     }
   double lots = CalcLots(g_esSym, eq * rf, LossPerLot(g_esSym, dir, entry, sl));
   if(lots <= 0.0)
     {
      Log("ES skipped: lot below minimum");
      return;
     }
   if(!MarginOK(g_esSym, dir, lots, entry))
      return;
   g_trade.SetExpertMagicNumber((ulong)InpESMagic);
   g_trade.SetTypeFillingBySymbol(g_esSym);
   bool ok = (dir > 0) ? g_trade.Buy(lots, g_esSym, 0.0, sl, 0.0, "GT_ES") : g_trade.Sell(lots, g_esSym, 0.0, sl, 0.0, "GT_ES");
   string desc = StringFormat("ES %s %s lots=%.2f SL=%s risk=%.2f%% ATR(H1)=%.1fp", (dir > 0 ? "BUY" : "SELL"),
                              g_esSym, lots, DoubleToString(sl, digits), rf * 100.0, a / pip);
   if(ok && (g_trade.ResultRetcode() == TRADE_RETCODE_DONE || g_trade.ResultRetcode() == TRADE_RETCODE_PLACED))
      Log("OPEN " + desc, true);
   else
      Log("ORDER FAILED " + desc + ": " + g_trade.ResultRetcodeDescription(), true);
  }

//+------------------------------------------------------------------+
//| Main cycle                                                        |
//+------------------------------------------------------------------+
void ProcessAll()
  {
   datetime nowSrv = TimeCurrent();
   if(nowSrv <= 0)
      return;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   UpdateEquityState(eq, nowSrv);
   if(g_flatten)
      CloseAllOurs(g_flattenReason);
   if(InpNREnabled && g_nsym > 0)
      ProcessNR(nowSrv, eq);
   if(InpESEnabled && g_esOk)
      ProcessES(nowSrv, eq);
   if(!g_tester)
      SaveState();
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   g_tester = (bool)MQLInfoInteger(MQL_TESTER);
   g_gvPrefix = "GT_" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "_";
   g_trade.SetDeviationInPoints((ulong)InpDeviationPoints);
   g_trade.SetMarginMode();
   g_trade.LogLevel(LOG_LEVEL_ERRORS);

   if(InpBBPeriod < 2 || InpATRPeriod < 2 || InpMaxHoldBars < 1)
     {
      Print("[GT] invalid indicator parameters");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpRiskProfile == GT_RISK_CUSTOM && (InpRiskNR < 0.0 || InpRiskNR > 5.0 || InpRiskES < 0.0 || InpRiskES > 5.0))
     {
      Print("[GT] custom risk must be between 0 and 5 percent");
      return INIT_PARAMETERS_INCORRECT;
     }

   ParseSymbolMult();

   // symbols
   string parts[];
   int n = StringSplit(InpNRSymbols, ',', parts);
   ArrayResize(g_sym, 0);
   ArrayResize(g_base, 0);
   g_nsym = 0;
   for(int i = 0; i < n; i++)
     {
      string b = parts[i];
      StringTrimLeft(b);
      StringTrimRight(b);
      StringToUpper(b);
      if(StringLen(b) < 6)
         continue;
      string s = b + InpSymbolSuffix;
      if(!SymbolSelect(s, true))
        {
         Print("[GT] WARNING symbol not available: ", s, " (check InpSymbolSuffix)");
         continue;
        }
      ArrayResize(g_sym, g_nsym + 1);
      ArrayResize(g_base, g_nsym + 1);
      g_sym[g_nsym] = s;
      g_base[g_nsym] = b;
      g_nsym++;
     }
   ArrayResize(g_lastBar, g_nsym);
   for(int i = 0; i < g_nsym; i++)
      g_lastBar[i] = 0;

   string eb = InpESSymbol;
   StringToUpper(eb);
   g_esSym = eb + InpSymbolSuffix;
   g_esOk = SymbolSelect(g_esSym, true);
   if(InpESEnabled && !g_esOk)
      Print("[GT] WARNING Euro Session symbol not available: ", g_esSym);

   // warm up history for all symbols (tester downloads data on first request)
   MqlRates tmp[];
   for(int i = 0; i < g_nsym; i++)
      CopyRates(g_sym[i], PERIOD_M15, 0, 50, tmp);
   if(g_esOk)
      CopyRates(g_esSym, PERIOD_H1, 0, 50, tmp);

   if(!g_tester)
      LoadState();
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(g_peak <= 0.0)
      g_peak = eq;
   EventSetTimer(g_tester ? 60 : (InpTimerSec < 1 ? 1 : InpTimerSec));

   string prof = EnumToString(InpRiskProfile);
   Log(StringFormat("started: %d NR symbols, ES=%s, profile=%s, NR risk=%.2f%%, ES risk=%.2f%%, server=%s",
                    g_nsym, (InpESEnabled && g_esOk) ? g_esSym : "off", prof, BaseRisk(true) * 100.0,
                    BaseRisk(false) * 100.0, EnumToString(InpServerTime)), true);
   if(!g_tester && !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      Print("[GT] WARNING: Algo Trading is disabled in the terminal.");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(!g_tester)
      SaveState();
  }

void OnTick()
  {
   ProcessAll();
  }

void OnTimer()
  {
   ProcessAll();
  }

//+------------------------------------------------------------------+
//| Optimisation criterion: net profit / equity drawdown              |
//+------------------------------------------------------------------+
double OnTester()
  {
   double profit = TesterStatistics(STAT_PROFIT);
   double dd = TesterStatistics(STAT_EQUITY_DD);
   if(dd <= 0.0)
      return profit;
   return profit / dd;
  }
//+------------------------------------------------------------------+
