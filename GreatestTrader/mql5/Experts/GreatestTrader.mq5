//+------------------------------------------------------------------+
//|                                              GreatestTrader.mq5  |
//|   Multi-strategy FX portfolio EA (MT5 Strategy Tester edition)   |
//|                                                                  |
//|   Strategy 1  Night Reversion (NR)  - core                       |
//|   Strategy 2  Euro Session   (ES)  - satellite                   |
//|   Logic is identical to the Python production engine.            |
//|                                                                  |
//|   v1.2 long-term operation:                                      |
//|   - lot caps (InpMaxLotsPerOrder / InpMaxRiskAmount / broker)    |
//|   - deposits / withdrawals / credit are not profit or loss       |
//|   - broker clock verified from live ticks (GT_SERVER_AUTO)       |
//|   - start-up and state safety, instance lock, hedging only       |
//|   - order retries, close back-off, monitoring alerts             |
//+------------------------------------------------------------------+
#property copyright   "GreatestTrader"
#property version     "1.20"
#property description "Night Reversion (14 crosses, 00:00-02:00 market time) + Euro Session (EURUSD 07-13 UTC). Defaults = STABLE5 preset."
#property description "Attach to ONE chart (any symbol, any timeframe) of a HEDGING account. The EA trades all configured symbols."

#include <Trade/Trade.mqh>

//--- enums
enum ENUM_GT_RISK
  {
   GT_RISK_CONSERVATIVE = 0, // Conservative 0.5% per trade
   GT_RISK_STANDARD     = 1, // Standard 1.0% per trade
   GT_RISK_AGGRESSIVE   = 2, // Aggressive 1.5% per trade (v1.0 classic; load Classic_Aggressive.set)
   GT_RISK_CUSTOM       = 3  // Custom (InpRiskNR / InpRiskES)
  };

enum ENUM_GT_SERVER_TIME
  {
   GT_SERVER_NY7   = 0, // GMT+2 winter / GMT+3 summer (US DST) - most brokers
   GT_SERVER_FIXED = 1, // Fixed GMT offset (InpServerGMTOffset)
   GT_SERVER_AUTO  = 2  // Auto: measured from live ticks (tester: NY7)
  };

//--- inputs
input group "=== Risk management ==="
input ENUM_GT_RISK InpRiskProfile      = GT_RISK_CUSTOM; // Risk profile (default = STABLE5 preset)
input double       InpRiskNR           = 3.5;   // Custom: NR risk per trade (% of equity)
input double       InpRiskES           = 1.75;  // Custom: ES risk per trade (% of equity)
input int          InpMaxOpen          = 10;    // Max open positions (total)
input int          InpMaxPerCurrency   = 4;     // Max positions per currency
input double       InpDDScaleFull      = 10.0;  // DD scaling: risk x max(floor, 1 - DD%/this)
input double       InpDDScaleFloor     = 0.10;  // DD scaling: minimum multiplier
input double       InpDailyLossLimit   = 1.0;   // Daily loss limit (% of day-start equity)
input double       InpMaxDDHalt        = 20.0;  // Halt new entries at this DD (%) from peak (InpResetHalt clears it)
input bool         InpUseMinLot        = false; // Use min lot when calculated lot is smaller
input double       InpMaxTotalRisk     = 8.75;  // Max total open risk (% of equity, 0 = off)
input double       InpMaxLotsPerOrder  = 20.0;  // Max lots per order, all strategies (0 = off; broker volume max still applies)
input double       InpMaxRiskAmount    = 0.0;   // Max risk per trade in account currency, entry to stop (0 = off)
input int          InpMaxSameDirPerCcy = 2;     // Max same-direction positions per currency (0 = off)
input double       InpDailyHardStop    = 0.0;   // Daily hard stop: close all at this loss (%, 0 = off)
input double       InpMonthlyLossLimit = 0.0;   // Monthly loss limit: no new entries (%, 0 = off)
input double       InpMonthlyHardStop  = 3.0;   // Monthly hard stop: close all at this loss (%, 0 = off)
input string       InpSymbolRiskMult   = "EURJPY:0.25,GBPJPY:0.25,USDJPY:0.25,GBPUSD:0.25,EURGBP:0.25,USDCHF:0.25,EURUSD:0.25,EURAUD:0.25,CHFJPY:0.25,GBPAUD:0.25"; // Per-symbol risk multipliers (SYM:mult,...)
input double       InpVolTargetDaily   = 0.0;   // Volatility target, daily equity vol (%, 0 = off)
input int          InpVolLookbackDays  = 20;    // Volatility target lookback (days)
input double       InpVolMultMin       = 0.25;  // Volatility target min multiplier
input double       InpVolMultMax       = 1.5;   // Volatility target max multiplier
input int          InpEqFilterDays     = 0;     // Equity-curve filter SMA (days, 0 = off)
input double       InpEqFilterMult     = 0.5;   // Equity-curve filter multiplier below SMA
input string       InpBlackoutStart    = "12-20"; // Year-end blackout start MM-DD (no new entries, empty = off)
input string       InpBlackoutEnd      = "01-05"; // Year-end blackout end MM-DD (inclusive)

input group "=== Broker ==="
input string              InpSymbolSuffix     = "";             // Symbol suffix (e.g. .raw  -STD  m)
input ENUM_GT_SERVER_TIME InpServerTime       = GT_SERVER_AUTO; // Broker server time (AUTO = measured from live ticks)
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
input bool   InpPushNotify  = false; // Push notifications (MetaQuotes ID required)
input int    InpTimerSec    = 5;     // Timer (seconds, live). Tester uses 60s
input bool   InpVerbose     = false; // Verbose log
input double InpDDAlert     = 15.0;  // Alert when drawdown from peak >= this % (notification only, 0 = off)
input bool   InpResetHalt   = false; // Clear the max-DD halt once at start (peak := equity), then set back to false

//--- constants
#define GT_FRESH_SEC        150     // act on a closed M15 bar only within this many seconds of its close (Python FRESH_SIGNAL)
#define GT_CLOCK_TOL_SEC    120     // a valid observation: server - UTC within this of a whole half-hour offset
#define GT_CLOCK_ADV_SEC    30      // clock observations only while the server time advanced within this many seconds
#define GT_CLOCK_OBS_SEC    60      // spacing of counted clock observations
#define GT_CLOCK_CONFIRM    3       // identical observations to adopt / mismatches to block / off-grid PC clock to alert
#define GT_FLOW_PENDING_SEC 60      // a balance change without a new deal in the history waits at most this long
#define GT_PRIME_STABLE_SEC 60      // first run: the cash-flow deals of the history unchanged this long = complete
#define GT_PRIME_MAX_SEC    600     // first run: start the cash-flow tracking after this long anyway (warning)
#define GT_LOCK_STALE_SEC   60      // instance lock: heartbeat older than this = holder gone
#define GT_LOCK_BEAT_SEC    10      // instance lock: heartbeat interval
#define GT_REPEAT_SEC       21600   // repeat a standing alert at most every 6 hours
#define GT_STALE_FEED_SEC   900     // no new quote for this long while the market is open = stale feed
#define GT_NOTIFY_MAX       30      // push queue length
#define GT_FAR_FUTURE       D'2100.01.01 00:00:00'

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
int      g_boStart = 0;          // blackout start as month*100+day (0 = off)
int      g_boEnd = 0;

//--- state persistence
bool     g_stateLoaded = false;  // state loaded for this login; SaveState() is a no-op before (failed OnInit safe)
bool     g_stateDirty = false;   // an important value changed: flush the global variables to disk now
datetime g_lastFlush = 0;
bool     g_gvWritten = false;    // a global variable was written since the last flush
bool     g_gvRefresh = false;    // rewrite every value this time (manual deletion / 4-week expiry)
datetime g_gvRefreshAt = 0;
string   g_gvName[];             // cache of the values last written, to write only on change
double   g_gvVal[];
bool     g_resetHaltDone = false; // InpResetHalt already applied (persisted; re-armed when InpResetHalt=false)
bool     g_resetDue = false;     // InpResetHalt is evaluated after the first cash-flow scan of the session

//--- cash flows (deposits / withdrawals / credit / bonus)
ulong    g_flowTicket = 0;       // newest cash-flow deal ticket already applied (persisted "flowtk")
datetime g_flowScan = 0;         // server time of the last history scan (persisted "flowtm")
ulong    g_allTicket = 0;        // newest deal ticket of any type seen (in memory)
double   g_lastBalCred = 0.0;    // balance + credit at the last explained change (0 = scan at the next cycle)
datetime g_pendingSince = 0;     // balance changed but no new deal in the history yet
bool     g_flowPending = false;  // this cycle: balance change not explained yet (no guard decision, no entry)
bool     g_flowPrimed = true;    // false on a first run until the existing deals are marked as seen
datetime g_primeStart = 0;       // first run: local time of the first priming attempt
datetime g_primeRefAt = 0;       // first run: since when the cash-flow deals below are unchanged
int      g_primeRefN = 0;        // ... their number, ticket sum and ticket xor (a fingerprint of the set)
ulong    g_primeRefSum = 0;
ulong    g_primeRefXor = 0;
datetime g_primeNote = 0;        // ... last 'history cannot be read' alert
datetime g_primeTry = 0;         // ... last priming attempt (the whole history is read at most every 5 s)

//--- broker clock
ENUM_GT_SERVER_TIME g_clockMode = GT_SERVER_NY7; // effective mode (AUTO -> NY7 in the tester)
long     g_clkOffset = 0;        // AUTO: adopted server - UTC offset in seconds (persisted "clkoff")
bool     g_clkHave = false;      // AUTO: an adopted offset exists
bool     g_clkVerified = false;  // a valid observation confirmed the clock since start-up
bool     g_clkBlocked = false;   // repeated mismatches: new entries blocked
int      g_clkMismatch = 0;      // consecutive observations that disagree with the clock in use
long     g_clkCand = 0;          // AUTO: candidate new offset
int      g_clkCandN = 0;         // AUTO: consecutive identical observations of the candidate
datetime g_clkLastObs = 0;       // local time of the last counted observation
datetime g_lastSrvSeen = 0;      // newest TimeCurrent() seen
datetime g_srvAdvAt = 0;         // local time when TimeCurrent() last advanced
datetime g_lastSrvSeenAt = 0;    // local time when g_lastSrvSeen was first seen (TrackServerTime)
int      g_clkOffGridN = 0;      // consecutive fresh observations off the half-hour grid (PC clock off)
long     g_clkOffGridSec = 0;    // ... seconds off the grid at the last one
datetime g_clkDriftNote = 0;     // local time of the last PC clock alert

//--- monitoring
datetime g_ddAlertDay = 0;       // market day of the last drawdown alert (persisted "ddalert")
bool     g_sumPending = false;   // a market day ended: send the daily summary
datetime g_sumDay = 0;
double   g_sumMonthRet = 0.0;
int      g_permit = -1;          // -1 unknown, 0 trading blocked, 1 trading permitted
datetime g_permitNote = 0;
datetime g_discSince = 0;
bool     g_discNotified = false;
datetime g_discNote = 0;
bool     g_staleNotified = false;
datetime g_staleNote = 0;
datetime g_startLocal = 0;
string   g_capSym[];             // lot-cap notices: once per market day per symbol
datetime g_capDay[];

//--- close back-off (bounded by the open positions, see PruneFails)
ulong    g_failTk[];
datetime g_failNext[];
datetime g_failNote[];

//--- push notifications (MT5 accepts at most 2 per second and 10 per minute)
string   g_nq[];
datetime g_lastPush = 0;

//--- single instance per account (terminal global variable GT_<login>_lock + heartbeat GT_<login>_lockhb)
double   g_lockToken = 0.0;
bool     g_lockHeld = false;
long     g_lockLogin = 0;
datetime g_lockBeat = 0;
bool     g_stopping = false;     // ExpertRemove() requested: ignore the remaining events

//+------------------------------------------------------------------+
//| Small helpers                                                     |
//+------------------------------------------------------------------+
long Secs(const datetime a, const datetime b)
  {
   return (long)a - (long)b;
  }

//+------------------------------------------------------------------+
//| Notifications and log                                             |
//+------------------------------------------------------------------+
void Notify(const string msg)
  {
   if(!InpPushNotify || g_tester)
      return;
   int n = ArraySize(g_nq);
   if(n >= GT_NOTIFY_MAX)
     {
      g_nq[n - 1] = "(more messages in the Experts log)";
      return;
     }
   ArrayResize(g_nq, n + 1);
   g_nq[n] = msg;
  }

// merges queued messages into one push of at most 255 characters, at most every 6 seconds
void FlushNotify(const bool force = false)
  {
   int n = ArraySize(g_nq);
   if(n == 0)
      return;
   if(!force && Secs(TimeLocal(), g_lastPush) < 6 && Secs(TimeLocal(), g_lastPush) >= 0)
      return;
   string m = "[GT] ";
   int used = 0;
   for(int i = 0; i < n; i++)
     {
      if(used > 0 && StringLen(m) + StringLen(g_nq[i]) + 3 > 255)
         break;
      if(used > 0)
         m += " | ";
      m += g_nq[i];
      used++;
     }
   if(!SendNotification(StringSubstr(m, 0, 255)))
      Print("[GT] push notification failed (error ", GetLastError(), ")");
   g_lastPush = TimeLocal();
   ArrayRemove(g_nq, 0, used);
  }

void Log(const string msg, const bool notify = false)
  {
   Print("[GT] ", msg);
   if(notify)
      Notify(msg);
  }

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

// server time -> UTC with the clock in use. AUTO uses the adopted (measured or persisted) offset and the NY7 rule
// until one exists; entries stay blocked until a live observation confirms the clock (CheckClock / CanOpen).
datetime ServerToUTC(const datetime srv)
  {
   if(g_clockMode == GT_SERVER_FIXED)
      return srv - (int)MathRound(InpServerGMTOffset * 3600.0);
   if(g_clockMode == GT_SERVER_AUTO && g_clkHave)
      return (datetime)((long)srv - g_clkOffset);
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
   if(g_clockMode == GT_SERVER_NY7 || (g_clockMode == GT_SERVER_AUTO && !g_clkHave))
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

//+------------------------------------------------------------------+
//| Risk state (persisted in terminal global variables GT_<login>_*)  |
//+------------------------------------------------------------------+
// writes one value only when it differs from the value last written (or on a full refresh)
void GVPut(const string key, const double v)
  {
   string name = g_gvPrefix + key;
   int n = ArraySize(g_gvName);
   int idx = -1;
   for(int i = 0; i < n; i++)
      if(g_gvName[i] == name)
        {
         idx = i;
         break;
        }
   if(idx >= 0 && g_gvVal[idx] == v && !g_gvRefresh)
      return;
   if(GlobalVariableSet(name, v) == 0)
     {
      Print("[GT] could not write global variable ", name, " (error ", GetLastError(), ")");
      return;
     }
   g_gvWritten = true;
   if(idx < 0)
     {
      idx = n;
      ArrayResize(g_gvName, n + 1);
      ArrayResize(g_gvVal, n + 1);
      g_gvName[idx] = name;
     }
   g_gvVal[idx] = v;
  }

void SaveState(const bool force = false)
  {
   if(g_tester || !g_stateLoaded)
      return;   // never overwrite the persisted state with unloaded defaults (e.g. OnDeinit after a failed OnInit)
   datetime now = TimeLocal();
   g_gvRefresh = (force || now >= g_gvRefreshAt);
   if(g_gvRefresh)
      g_gvRefreshAt = now + 3600;
   GVPut("peak", g_peak);
   GVPut("day", (double)g_day);
   GVPut("dayeq", g_dayStartEq);
   GVPut("halt", g_halted ? 1.0 : 0.0);
   GVPut("esday", (double)g_lastESDay);
   GVPut("month", (double)g_month);
   GVPut("montheq", g_monthStartEq);
   GVPut("monthhalt", g_monthHalted ? 1.0 : 0.0);
   GVPut("dayhalt", g_dayHalted ? 1.0 : 0.0);
   GVPut("lasteq", g_lastEq);
   int n = ArraySize(g_dailyEq);
   GVPut("deqn", (double)n);
   for(int i = 0; i < n; i++)
      GVPut("deq" + IntegerToString(i), g_dailyEq[i]);
   if(g_flowPrimed)
     {
      // only a primed cursor is persisted: a restart before the first-run priming primes again
      GVPut("flowtk", (double)g_flowTicket);   // deal tickets are far below 2^53: exact in a double
      GVPut("flowtm", (double)g_flowScan);
     }
   if(g_clkHave)
      GVPut("clkoff", (double)g_clkOffset);
   GVPut("ddalert", (double)g_ddAlertDay);
   GVPut("rsthalt", g_resetHaltDone ? 1.0 : 0.0);
   g_gvRefresh = false;
   // the terminal writes gvariables.dat only at a clean exit: flush so that a VPS crash cannot roll the
   // peak / halt / month stop back to old values
   if(force || g_stateDirty || (g_gvWritten && Secs(now, g_lastFlush) >= 60) || Secs(now, g_lastFlush) < 0)
     {
      GlobalVariablesFlush();
      g_lastFlush = now;
      g_stateDirty = false;
      g_gvWritten = false;
     }
  }

void ResetStateVars()
  {
   g_peak = 0.0;
   g_day = 0;
   g_dayStartEq = 0.0;
   g_halted = false;
   g_lastESDay = 0;
   g_month = 0;
   g_monthStartEq = 0.0;
   g_monthHalted = false;
   g_dayHalted = false;
   g_lastEq = 0.0;
   ArrayResize(g_dailyEq, 0);
   g_adaptMult = 1.0;
   g_flowTicket = 0;
   g_flowScan = 0;
   g_allTicket = 0;
   g_lastBalCred = 0.0;
   g_pendingSince = 0;
   g_flowPending = false;
   g_flowPrimed = true;
   g_primeStart = 0;
   g_primeRefAt = 0;
   g_primeRefN = 0;
   g_primeRefSum = 0;
   g_primeRefXor = 0;
   g_primeNote = 0;
   g_primeTry = 0;
   g_clkHave = false;
   g_clkOffset = 0;
   g_ddAlertDay = 0;
   g_resetHaltDone = false;
   g_resetDue = false;
   g_sumPending = false;
  }

// every key is optional: state written by v1.1 (no flow / clock / alert keys) loads unchanged
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
   if(GlobalVariableCheck(g_gvPrefix + "flowtk"))
      g_flowTicket = (ulong)GlobalVariableGet(g_gvPrefix + "flowtk");
   if(GlobalVariableCheck(g_gvPrefix + "flowtm"))
      g_flowScan = (datetime)GlobalVariableGet(g_gvPrefix + "flowtm");
   if(GlobalVariableCheck(g_gvPrefix + "clkoff"))
     {
      long off = (long)MathRound(GlobalVariableGet(g_gvPrefix + "clkoff"));
      if(off >= -14 * 3600 && off <= 14 * 3600)
        {
         g_clkOffset = off;
         g_clkHave = true;
        }
     }
   if(GlobalVariableCheck(g_gvPrefix + "ddalert"))
      g_ddAlertDay = (datetime)GlobalVariableGet(g_gvPrefix + "ddalert");
   if(GlobalVariableCheck(g_gvPrefix + "rsthalt"))
      g_resetHaltDone = (GlobalVariableGet(g_gvPrefix + "rsthalt") > 0.5);
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
   if(eq <= 0.0)
      return;   // an equity reading of 0 (terminal not ready) must never set a halt, a stop or a baseline
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
      if(g_day != 0)
        {
         // daily summary of the market day that just ended (sent from ProcessAll, live only)
         g_sumPending = true;
         g_sumDay = g_day;
         g_sumMonthRet = (g_monthStartEq > 0.0) ? eq / g_monthStartEq - 1.0 : 0.0;
        }
      g_day = mday;
      g_dayStartEq = eq;
      g_dayHalted = false;
      g_adaptMult = AdaptiveMult();
      g_stateDirty = true;
     }
   g_lastEq = eq;
   datetime mmon = MonthStart(mkt);
   if(mmon != g_month)
     {
      g_month = mmon;
      g_monthStartEq = eq;
      g_monthHalted = false;
      g_stateDirty = true;
     }
   double dd = Drawdown(eq);
   if(!g_halted && InpMaxDDHalt > 0.0 && dd * 100.0 >= InpMaxDDHalt)
     {
      g_halted = true;
      g_stateDirty = true;
      Log(StringFormat("HALT: drawdown %.1f%% reached (limit %.1f%%). New entries stopped; closes and stops keep working. "
                       "To resume set InpResetHalt=true once (other state is kept).", dd * 100.0, InpMaxDDHalt), true);
     }
   if(InpMonthlyHardStop > 0.0 && !g_monthHalted && g_monthStartEq > 0.0 &&
      eq <= g_monthStartEq * (1.0 - InpMonthlyHardStop / 100.0))
     {
      g_monthHalted = true;
      g_flatten = true;
      g_stateDirty = true;
      g_flattenReason = StringFormat("monthly hard stop %.1f%%", InpMonthlyHardStop);
      Log("MONTH STOP: " + g_flattenReason, true);
     }
   if(InpDailyHardStop > 0.0 && !g_dayHalted && g_dayStartEq > 0.0 &&
      eq <= g_dayStartEq * (1.0 - InpDailyHardStop / 100.0))
     {
      g_dayHalted = true;
      g_flatten = true;
      g_stateDirty = true;
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

bool IsPlainNumber(string v)
  {
   StringTrimLeft(v);
   StringTrimRight(v);
   int digits = 0, dots = 0;
   for(int i = 0; i < StringLen(v); i++)
     {
      ushort c = StringGetCharacter(v, i);
      if(c >= '0' && c <= '9')
         digits++;
      else
         if(c == '.')
            dots++;
         else
            return false;
     }
   return (digits > 0 && dots <= 1);
  }

// returns false (and prints the item) when an item is not SYMBOL:number
bool ParseSymbolMult()
  {
   ArrayResize(g_multSym, 0);
   ArrayResize(g_multVal, 0);
   string items[];
   int n = StringSplit(InpSymbolRiskMult, ',', items);
   for(int i = 0; i < n; i++)
     {
      string item = items[i];
      StringTrimLeft(item);
      StringTrimRight(item);
      if(StringLen(item) == 0)
         continue;
      string kv[];
      if(StringSplit(item, ':', kv) != 2 || !IsPlainNumber(kv[1]))
        {
         Print("[GT] InpSymbolRiskMult: invalid item '", item, "' (expected SYMBOL:number, e.g. EURJPY:0.25)");
         return false;
        }
      string k = kv[0];
      StringTrimLeft(k);
      StringTrimRight(k);
      StringToUpper(k);
      k = StringSubstr(k, 0, 6);   // base name: a key typed with the broker suffix still matches
      int m = ArraySize(g_multSym);
      ArrayResize(g_multSym, m + 1);
      ArrayResize(g_multVal, m + 1);
      g_multSym[m] = k;
      string v = kv[1];
      StringTrimLeft(v);
      StringTrimRight(v);
      g_multVal[m] = StringToDouble(v);
     }
   return true;
  }

int ParseMMDD(const string v)
  {
   string parts[];
   if(StringSplit(v, '-', parts) != 2)
      return 0;
   int m = (int)StringToInteger(parts[0]);
   int d = (int)StringToInteger(parts[1]);
   if(m < 1 || m > 12 || d < 1 || d > 31)
      return 0;
   return m * 100 + d;
  }

bool InBlackout()
  {
   if(g_boStart == 0 || g_boEnd == 0 || g_day == 0)
      return false;
   MqlDateTime s;
   TimeToStruct(g_day, s);
   int md = s.mon * 100 + s.day;
   if(g_boStart <= g_boEnd)
      return (md >= g_boStart && md <= g_boEnd);
   return (md >= g_boStart || md <= g_boEnd);
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

// a position or an in-flight order of this strategy (magic) on the symbol: used after a timeout / no reply
bool OwnTradeOnSymbol(const string sym, const long magic)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) == magic && PositionGetString(POSITION_SYMBOL) == sym)
         return true;
     }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ot = OrderGetTicket(i);
      if(ot == 0)
         continue;
      if(OrderGetInteger(ORDER_MAGIC) == magic && OrderGetString(ORDER_SYMBOL) == sym)
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
      if(sl <= 0.0)
        {
         // fail closed: an own position without a stop loss has unlimited risk and fills the total-risk cap
         Log("open position without stop loss on " + PositionGetString(POSITION_SYMBOL) + ": open risk treated as unlimited");
         return 1e300;
        }
      if((dir > 0 && sl >= op) || (dir < 0 && sl <= op))
         continue;   // stop at or beyond the entry price: no remaining risk
      double l = LossPerLot(PositionGetString(POSITION_SYMBOL), dir, op, sl);
      if(l <= 0.0)
        {
         // fail closed: a position whose risk cannot be priced fills the total-risk cap
         Log("cannot price open risk of " + PositionGetString(POSITION_SYMBOL) + ": treated as unlimited");
         return 1e300;
        }
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
   if(!EntryGateOpen(why))
      return false;
   if(g_halted)
     {
      why = "halted (max drawdown)";
      return false;
     }
   if(InBlackout())
     {
      why = "year-end holiday blackout";
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

//+------------------------------------------------------------------+
//| Sizing: risk amount, absolute caps, lots                          |
//+------------------------------------------------------------------+
double LossPerLot(const string sym, const int dir, const double entry, const double stop)
  {
   double profit = 0.0;
   ENUM_ORDER_TYPE t = (dir > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!OrderCalcProfit(t, sym, 1.0, entry, stop, profit))
      return -1.0;
   return MathAbs(profit);
  }

// per-order volume ceiling: SYMBOL_VOLUME_MAX, InpMaxLotsPerOrder and the room left under SYMBOL_VOLUME_LIMIT
// (aggregate volume of positions and pending orders in one direction on the symbol, 0 = no limit)
double VolumeCap(const string sym, const int dir, string &by)
  {
   double vmax = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   by = "broker volume max";
   if(InpMaxLotsPerOrder > 0.0 && InpMaxLotsPerOrder < vmax)
     {
      vmax = InpMaxLotsPerOrder;
      by = "InpMaxLotsPerOrder";
     }
   double lim = SymbolInfoDouble(sym, SYMBOL_VOLUME_LIMIT);
   if(lim > 0.0)
     {
      double used = 0.0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         ulong tk = PositionGetTicket(i);
         if(tk == 0 || PositionGetString(POSITION_SYMBOL) != sym)
            continue;
         int pd = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
         if(pd == dir)
            used += PositionGetDouble(POSITION_VOLUME);
        }
      for(int i = OrdersTotal() - 1; i >= 0; i--)
        {
         ulong ot = OrderGetTicket(i);
         if(ot == 0 || OrderGetString(ORDER_SYMBOL) != sym)
            continue;
         ENUM_ORDER_TYPE t = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
         int od = (t == ORDER_TYPE_BUY || t == ORDER_TYPE_BUY_LIMIT || t == ORDER_TYPE_BUY_STOP ||
                   t == ORDER_TYPE_BUY_STOP_LIMIT) ? 1 : -1;
         if(od == dir)
            used += OrderGetDouble(ORDER_VOLUME_CURRENT);
        }
      double room = lim - used;
      if(room < vmax)
        {
         vmax = MathMax(0.0, room);
         by = "broker volume limit";
        }
     }
   return vmax;
  }

// risk amount after the absolute caps, computed BEFORE CanOpen so that the total-risk cap sees the real size:
// min(equity x risk fraction x symbol mult, InpMaxRiskAmount, InpMaxLotsPerOrder x loss/lot, volume max x loss/lot)
// (shared contract with Python RiskManager.capped_risk)
double CappedRisk(const double riskAmount, const double lossPerLot, const double vcap, const string vcapBy, string &by)
  {
   double r = riskAmount;
   by = "";
   if(InpMaxRiskAmount > 0.0 && InpMaxRiskAmount < r)
     {
      r = InpMaxRiskAmount;
      by = "InpMaxRiskAmount";
     }
   if(lossPerLot > 0.0 && vcap * lossPerLot < r)
     {
      r = vcap * lossPerLot;
      by = vcapBy;
     }
   return r;
  }

double CalcLots(const string sym, const double riskAmount, const double lossPerLot, const double vmax)
  {
   if(lossPerLot <= 0.0 || riskAmount <= 0.0)
      return 0.0;
   double vmin  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
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
      lots = MathFloor(vmax / vstep + 1e-9) * vstep;
   if(lots < vmin || lots <= 0.0)
      return 0.0;   // the volume ceiling is below the broker minimum: no valid order size
   int stepDigits = (int)MathMax(0, MathCeil(-MathLog10(vstep) - 1e-9));
   return NormalizeDouble(lots, stepDigits);
  }

// lot-cap notice: notify at most once per market day per symbol (the OPEN line shows every capped trade)
bool CapNoticeDue(const string sym)
  {
   int n = ArraySize(g_capSym);
   for(int i = 0; i < n; i++)
      if(g_capSym[i] == sym)
        {
         if(g_capDay[i] == g_day)
            return false;
         g_capDay[i] = g_day;
         return true;
        }
   ArrayResize(g_capSym, n + 1);
   ArrayResize(g_capDay, n + 1);
   g_capSym[n] = sym;
   g_capDay[n] = g_day;
   return true;
  }

void ReportCap(const string strat, const string sym, const double rawRisk, const double lossPerLot, const double lots,
               const string by)
  {
   if(by == "" || lossPerLot <= 0.0)
      return;
   string msg = StringFormat("lot cap: intended %.2f lots, sending %.2f (%s %s, limited by %s)",
                             rawRisk / lossPerLot, lots, strat, sym, by);
   if(CapNoticeDue(sym))
      Log(msg, true);
   else
      if(InpVerbose)
         Log(msg);
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

//+------------------------------------------------------------------+
//| Order execution: retries, filling fallback, close back-off        |
//+------------------------------------------------------------------+
void AddFilling(ENUM_ORDER_TYPE_FILLING &arr[], const ENUM_ORDER_TYPE_FILLING f)
  {
   int n = ArraySize(arr);
   for(int i = 0; i < n; i++)
      if(arr[i] == f)
         return;
   ArrayResize(arr, n + 1);
   arr[n] = f;
  }

// filling modes to try, in the same order as Python mt5_broker._fillings: allowed FOK / IOC, RETURN, then the rest
int FillingList(const string sym, ENUM_ORDER_TYPE_FILLING &out[])
  {
   ArrayResize(out, 0);
   long fm = SymbolInfoInteger(sym, SYMBOL_FILLING_MODE);
   if((fm & SYMBOL_FILLING_FOK) != 0)
      AddFilling(out, ORDER_FILLING_FOK);
   if((fm & SYMBOL_FILLING_IOC) != 0)
      AddFilling(out, ORDER_FILLING_IOC);
   AddFilling(out, ORDER_FILLING_RETURN);
   AddFilling(out, ORDER_FILLING_FOK);
   AddFilling(out, ORDER_FILLING_IOC);
   return ArraySize(out);
  }

bool IsDone(const bool sent, const uint rc)
  {
   return sent && (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_PLACED || rc == TRADE_RETCODE_DONE_PARTIAL);
  }

bool IsPriceRetcode(const uint rc)
  {
   return (rc == TRADE_RETCODE_REQUOTE || rc == TRADE_RETCODE_PRICE_CHANGED || rc == TRADE_RETCODE_PRICE_OFF ||
           rc == TRADE_RETCODE_TOO_MANY_REQUESTS);
  }

// the request may have reached the server: never re-send before checking for the position
bool IsNoReply(const bool sent, const uint rc)
  {
   return (rc == TRADE_RETCODE_TIMEOUT || rc == TRADE_RETCODE_CONNECTION || (!sent && rc == 0));
  }

bool WaitOwnTrade(const string sym, const long magic)
  {
   for(int k = 0; k < 3; k++)
     {
      Sleep(1000);
      if(OwnTradeOnSymbol(sym, magic))
         return true;
     }
   return false;
  }

// market entry with SL/TP absolute (as in Python): up to 3 sends on requote / price changed / off quotes with
// fresh prices (price 0.0 -> CTrade reads the current bid/ask), filling-mode fallback on INVALID_FILL, and
// after a timeout / no reply a check for the new position before any re-send
bool SendEntry(const int dir, const double lots, const string sym, const double sl, const double tp,
               const long magic, const string cmt, string &err)
  {
   err = "";
   g_trade.SetExpertMagicNumber((ulong)magic);
   ENUM_ORDER_TYPE_FILLING fills[];
   int nf = FillingList(sym, fills);
   int fstep = 0;
   int priceFails = 0;
   int noReply = 0;
   for(int guard = 0; guard < 12; guard++)
     {
      g_trade.SetTypeFilling(fills[fstep]);
      bool sent = (dir > 0) ? g_trade.Buy(lots, sym, 0.0, sl, tp, cmt) : g_trade.Sell(lots, sym, 0.0, sl, tp, cmt);
      uint rc = g_trade.ResultRetcode();
      if(IsDone(sent, rc))
         return true;
      err = StringFormat("%u %s", rc, g_trade.ResultRetcodeDescription());
      if(rc == TRADE_RETCODE_INVALID_FILL && fstep + 1 < nf)
        {
         fstep++;
         continue;
        }
      if(IsNoReply(sent, rc))
        {
         if(WaitOwnTrade(sym, magic))
           {
            Log(StringFormat("%s %s: no reply to the order (%s) but the position exists - not re-sent", cmt, sym, err));
            return true;
           }
         noReply++;
         if(noReply >= 3)
            return false;
         continue;
        }
      if(IsPriceRetcode(rc))
        {
         priceFails++;
         if(priceFails >= 3)
            return false;
         Sleep(300);
         continue;
        }
      return false;
     }
   return false;
  }

int FailIdx(const ulong tk)
  {
   for(int i = 0; i < ArraySize(g_failTk); i++)
      if(g_failTk[i] == tk)
         return i;
   return -1;
  }

void DropFail(const int i)
  {
   ArrayRemove(g_failTk, i, 1);
   ArrayRemove(g_failNext, i, 1);
   ArrayRemove(g_failNote, i, 1);
  }

// once per cycle: forget back-off entries of positions that no longer exist
void PruneFails()
  {
   for(int i = ArraySize(g_failTk) - 1; i >= 0; i--)
      if(!PositionSelectByTicket(g_failTk[i]))
         DropFail(i);
  }

// returns true when the position is gone. A failed close is retried with a back-off per ticket (20 s, or 300 s
// while the market is closed / trading is disabled) and pushed at most once per hour per ticket.
bool ClosePos(const ulong ticket, const string reason)
  {
   if(!PositionSelectByTicket(ticket))
      return true;
   int fi = FailIdx(ticket);
   if(fi >= 0 && Secs(TimeLocal(), g_failNext[fi]) < 0)
      return false;
   string sym = PositionGetString(POSITION_SYMBOL);
   ENUM_ORDER_TYPE_FILLING fills[];
   int nf = FillingList(sym, fills);
   int fstep = 0;
   int priceFails = 0;
   int noReply = 0;
   uint rc = 0;
   bool done = false;
   for(int guard = 0; guard < 12; guard++)
     {
      g_trade.SetTypeFilling(fills[fstep]);
      bool sent = g_trade.PositionClose(ticket, (ulong)InpDeviationPoints);
      rc = g_trade.ResultRetcode();
      if(IsDone(sent, rc) || rc == TRADE_RETCODE_POSITION_CLOSED || !PositionSelectByTicket(ticket))
        {
         done = true;
         break;
        }
      if(rc == TRADE_RETCODE_INVALID_FILL && fstep + 1 < nf)
        {
         fstep++;
         continue;
        }
      if(IsNoReply(sent, rc))
        {
         noReply++;
         if(noReply >= 3)
            break;
         Sleep(1000);
         if(!PositionSelectByTicket(ticket))
           {
            done = true;
            break;
           }
         continue;
        }
      if(IsPriceRetcode(rc))
        {
         priceFails++;
         if(priceFails >= 3)
            break;
         Sleep(300);
         continue;
        }
      break;
     }
   if(done)
     {
      if(fi >= 0)
         DropFail(fi);
      Log(StringFormat("CLOSE %s #%s (%s)", sym, IntegerToString((long)ticket), reason), true);
      return true;
     }
   if(fi < 0)
     {
      fi = ArraySize(g_failTk);
      ArrayResize(g_failTk, fi + 1);
      ArrayResize(g_failNext, fi + 1);
      ArrayResize(g_failNote, fi + 1);
      g_failTk[fi] = ticket;
      g_failNote[fi] = 0;
     }
   bool slow = (rc == TRADE_RETCODE_MARKET_CLOSED || rc == TRADE_RETCODE_TRADE_DISABLED ||
                rc == TRADE_RETCODE_CLIENT_DISABLES_AT || rc == TRADE_RETCODE_SERVER_DISABLES_AT);
   int wait = slow ? 300 : 20;
   datetime now = TimeLocal();
   g_failNext[fi] = now + wait;
   bool notify = (g_failNote[fi] == 0 || Secs(now, g_failNote[fi]) >= 3600 || Secs(now, g_failNote[fi]) < 0);
   if(notify)
      g_failNote[fi] = now;
   Log(StringFormat("CLOSE FAILED %s #%s (%s): %u %s - next try in %d s", sym, IntegerToString((long)ticket), reason,
                    rc, g_trade.ResultRetcodeDescription(), wait), notify);
   return false;
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
            uint rc = g_trade.ResultRetcode();
            // close only when the price is already beyond the TP (fresh quote); on any other failure (no connection,
            // too many requests, ...) the position keeps its SL and the time stop, and the TP is retried next bar
            bool beyond = false;
            MqlTick t2;
            if(tp > 0.0 && SymbolInfoTick(sym, t2))
              {
               double px2 = (dir > 0) ? t2.bid : t2.ask;
               beyond = ((dir > 0 && px2 >= tp) || (dir < 0 && px2 <= tp));
              }
            if(beyond)
               ClosePos(tk, "price beyond TP (TP not placeable)");
            else
               Log(StringFormat("modify failed %s: %u %s (SL kept, TP retried next bar)", sym, rc,
                                g_trade.ResultRetcodeDescription()));
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
   double rawRisk = eq * rf;
   double lossLot = LossPerLot(sym, dir, entry, sl);
   string capBy = "";
   double vcap = VolumeCap(sym, dir, capBy);
   string by = "";
   double risk = CappedRisk(rawRisk, lossLot, vcap, capBy, by);
   string why;
   if(!CanOpen(sym, eq, dir, risk, why))
     {
      if(InpVerbose)
         Log(StringFormat("NR %s signal skipped: %s", sym, why));
      return;
     }
   double lots = CalcLots(sym, risk, lossLot, vcap);
   if(lots <= 0.0)
     {
      if(InpVerbose)
         Log(StringFormat("NR %s skipped: lot below minimum (risk %.2f)", sym, risk));
      return;
     }
   if(!MarginOK(sym, dir, lots, entry))
      return;
   ReportCap("NR", sym, rawRisk, lossLot, lots, by);
   string err = "";
   bool ok = SendEntry(dir, lots, sym, sl, tp, InpNRMagic, "GT_NR", err);
   string capTxt = "";
   if(by != "")
      capTxt = StringFormat(" [capped from %.2f%%]", rf * 100.0);
   string desc = StringFormat("NR %s %s lots=%.2f SL=%s TP=%s risk=%.2f%%%s ATR=%.1fp cost=%.1fp",
                              (dir > 0 ? "BUY" : "SELL"), sym, lots, DoubleToString(sl, digits),
                              DoubleToString(tp, digits), (eq > 0.0 ? risk / eq : rf) * 100.0, capTxt, atrv / pip, cost);
   if(ok)
      Log("OPEN " + desc, true);
   else
      Log("ORDER FAILED " + desc + ": " + err, true);
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
      g_lastBar[i] = bar.time;
      NRManage(sym, mid, nowSrv);
      // entries only on a fresh bar close (avoid acting on stale bars after restarts / data gaps);
      // the same 150 s window as Python (engine.py FRESH_SIGNAL), also for the first bar after a start
      if(nowSrv - (bar.time + 900) > GT_FRESH_SEC)
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
   string gate;
   if(!EntryGateOpen(gate))
      return;   // decide only on a verified broker clock and settled cash flows (the day stays open meanwhile)
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
   double rawRisk = eq * rf;
   double lossLot = LossPerLot(g_esSym, dir, entry, sl);
   string capBy = "";
   double vcap = VolumeCap(g_esSym, dir, capBy);
   string by = "";
   double risk = CappedRisk(rawRisk, lossLot, vcap, capBy, by);
   string why;
   if(!CanOpen(g_esSym, eq, dir, risk, why))
     {
      Log("ES signal skipped: " + why);
      return;
     }
   double lots = CalcLots(g_esSym, risk, lossLot, vcap);
   if(lots <= 0.0)
     {
      Log("ES skipped: lot below minimum");
      return;
     }
   if(!MarginOK(g_esSym, dir, lots, entry))
      return;
   ReportCap("ES", g_esSym, rawRisk, lossLot, lots, by);
   string err = "";
   bool ok = SendEntry(dir, lots, g_esSym, sl, 0.0, InpESMagic, "GT_ES", err);
   string capTxt = "";
   if(by != "")
      capTxt = StringFormat(" [capped from %.2f%%]", rf * 100.0);
   string desc = StringFormat("ES %s %s lots=%.2f SL=%s risk=%.2f%%%s ATR(H1)=%.1fp", (dir > 0 ? "BUY" : "SELL"),
                              g_esSym, lots, DoubleToString(sl, digits), (eq > 0.0 ? risk / eq : rf) * 100.0, capTxt,
                              a / pip);
   if(ok)
      Log("OPEN " + desc, true);
   else
      Log("ORDER FAILED " + desc + ": " + err, true);
  }

//+------------------------------------------------------------------+
//| Cash flows: deposits / withdrawals / credit / bonus are not P/L   |
//+------------------------------------------------------------------+
bool IsCashFlow(const long dealType)
  {
   return (dealType == DEAL_TYPE_BALANCE || dealType == DEAL_TYPE_CREDIT || dealType == DEAL_TYPE_BONUS);
  }

// first run (no persisted cursor): every deal already in the history counts as seen and is NOT applied - but
// only once the history looks complete: the deals sum to the balance (credit deals to the credit) within
// max(1, 0.1%), or the set of cash-flow deals stayed the same for GT_PRIME_STABLE_SEC. After GT_PRIME_MAX_SEC
// the cursor is primed anyway (warning). Until then new entries wait; closes, stops and the guards keep working.
// Returns true when the cursor is primed. Live only (shared contract with the Python engine).
bool TryPrimeFlowCursor()
  {
   if(g_flowPrimed)
      return true;
   datetime now = TimeLocal();
   if(g_primeStart == 0 || Secs(now, g_primeStart) < 0)
      g_primeStart = now;
   if(g_primeTry != 0 && Secs(now, g_primeTry) < 5 && Secs(now, g_primeTry) >= 0)
      return false;
   g_primeTry = now;
   bool late = (Secs(now, g_primeStart) >= GT_PRIME_MAX_SEC);
   if(!HistorySelect(0, GT_FAR_FUTURE))
     {
      if(late && (g_primeNote == 0 || Secs(now, g_primeNote) >= GT_REPEAT_SEC || Secs(now, g_primeNote) < 0))
        {
         g_primeNote = now;
         Log("the deal history cannot be read: cash-flow tracking not started, new entries wait", true);
        }
      return false;
     }
   int n = HistoryDealsTotal();
   ulong mx = 0;
   double sumBal = 0.0;
   double sumCred = 0.0;
   int nCash = 0;
   ulong cashSum = 0;
   ulong cashXor = 0;
   for(int i = 0; i < n; i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk == 0)
         continue;
      if(tk > mx)
         mx = tk;
      long type = HistoryDealGetInteger(tk, DEAL_TYPE);
      double amt = HistoryDealGetDouble(tk, DEAL_PROFIT) + HistoryDealGetDouble(tk, DEAL_COMMISSION) +
                   HistoryDealGetDouble(tk, DEAL_SWAP) + HistoryDealGetDouble(tk, DEAL_FEE);
      if(type == DEAL_TYPE_CREDIT)
         sumCred += amt;
      else
         sumBal += amt;
      if(IsCashFlow(type))
        {
         nCash++;
         cashSum += tk;
         cashXor ^= tk;
        }
     }
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double cred = AccountInfoDouble(ACCOUNT_CREDIT);
   bool sums = (MathAbs(sumBal - bal) <= MathMax(1.0, 0.001 * MathAbs(bal)) &&
                MathAbs(sumCred - cred) <= MathMax(1.0, 0.001 * MathAbs(cred)));
   bool same = (g_primeRefAt != 0 && nCash == g_primeRefN && cashSum == g_primeRefSum && cashXor == g_primeRefXor);
   if(!same || Secs(now, g_primeRefAt) < 0)
     {
      same = false;
      g_primeRefAt = now;
      g_primeRefN = nCash;
      g_primeRefSum = cashSum;
      g_primeRefXor = cashXor;
     }
   bool stable = (same && Secs(now, g_primeRefAt) >= GT_PRIME_STABLE_SEC);
   if(!sums && !stable && !late)
      return false;
   g_flowTicket = mx;
   g_allTicket = mx;
   g_flowScan = TimeCurrent();
   g_flowPrimed = true;
   g_lastBalCred = 0.0;   // the first scan (same cycle) takes balance + credit as the reference
   g_pendingSince = 0;
   g_stateDirty = true;
   string how = "NOT confirmed";
   if(sums)
      how = "deals match balance and credit";
   else
      if(stable)
         how = StringFormat("cash-flow deals unchanged for %d s", GT_PRIME_STABLE_SEC);
   Log(StringFormat("cash-flow tracking started: %d existing deals marked as seen (cursor #%s, %s)", n,
                    IntegerToString((long)mx), how));
   if(!sums && !stable)
      Log(StringFormat("WARNING: the deal history was not confirmed complete within %d min (deals sum to %.2f, "
                       "balance %.2f; credit deals %.2f, credit %.2f). Existing deals are marked as seen anyway: a "
                       "deposit or withdrawal that reaches the history later is treated as a new cash flow.",
                       GT_PRIME_MAX_SEC / 60, sumBal, bal, sumCred, cred), true);
   return true;
  }

// sums new cash-flow deals (ticket > cursor). Returns true when the history holds any deal newer than the
// newest one seen before (the balance change is explained).
bool ScanDeals(double &flow, int &count)
  {
   flow = 0.0;
   count = 0;
   datetime base = (g_flowScan > 0) ? g_flowScan : TimeCurrent();
   long fromL = (long)base - 2 * 86400;          // overlap; de-duplicated by ticket
   if(fromL < 0)
      fromL = 0;
   if(!HistorySelect((datetime)fromL, GT_FAR_FUTURE))
      return false;
   bool fresh = false;
   ulong maxFlow = g_flowTicket;
   int n = HistoryDealsTotal();
   for(int i = 0; i < n; i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk == 0)
         continue;
      if(tk > g_allTicket)
        {
         g_allTicket = tk;
         fresh = true;
        }
      if(tk <= g_flowTicket)
         continue;
      if(!IsCashFlow(HistoryDealGetInteger(tk, DEAL_TYPE)))
         continue;
      flow += HistoryDealGetDouble(tk, DEAL_PROFIT);
      count++;
      if(tk > maxFlow)
         maxFlow = tk;
     }
   g_flowTicket = maxFlow;
   g_flowScan = TimeCurrent();
   return fresh;
  }

// keeps the newest deal ticket of any type current while the balance does not change, so that "a new deal" in
// HandleCashFlows means one booked since the previous cycle (an entry deal booked an hour ago, which does not
// change the balance, must not explain a later deposit or withdrawal)
void TrackNewestDeal()
  {
   long fromL = (long)TimeCurrent() - 2 * 86400;
   if(fromL < 0)
      fromL = 0;
   if(!HistorySelect((datetime)fromL, GT_FAR_FUTURE))
      return;
   int n = HistoryDealsTotal();
   for(int i = 0; i < n; i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk > g_allTicket)
         g_allTicket = tk;
     }
  }

// rescale every equity baseline by k = E / (E - F) so that the percentage guards (DD scaling, max-DD halt,
// daily limit, monthly stop, vol target / equity filter) stay exactly where they were before the flow
void ApplyCashFlow(const double flow, const int count, const double eq)
  {
   g_stateDirty = true;
   if(flow == 0.0)
     {
      Log(StringFormat("CASH FLOW: %d deals netting to 0 detected, guards unchanged", count));
      return;
     }
   double before = eq - flow;
   double k = (before > 0.0) ? eq / before : 0.0;
   string how = "";
   if(k >= 0.02 && k <= 50.0)
     {
      g_peak *= k;
      g_dayStartEq *= k;
      g_monthStartEq *= k;
      g_lastEq *= k;
      for(int i = 0; i < ArraySize(g_dailyEq); i++)
         g_dailyEq[i] *= k;
      how = StringFormat("x%.4f", k);
     }
   else
     {
      g_peak = eq;
      g_dayStartEq = eq;
      g_monthStartEq = eq;
      g_lastEq = eq;
      for(int i = 0; i < ArraySize(g_dailyEq); i++)
         g_dailyEq[i] = eq;
      how = "rebased to current equity";
     }
   if(count > 1)
      how += StringFormat(", %d deals", count);
   Log(StringFormat("CASH FLOW %+.2f detected, guards rebased (%s)", flow, how), true);
  }

// Returns false ("pending") while a balance / credit change is not yet explained by any new deal (trade or cash,
// newer than the newest deal seen in the previous cycle) in the history: in that cycle the peak is not raised, no
// halt / month / day stop is evaluated and nothing is
// opened; closes, time stops and TP management continue. Pending lasts at most GT_FLOW_PENDING_SEC, then the
// change counts as profit / loss. eq is read again AFTER the deal scan, so that k and this cycle's guards use an
// equity that contains every flow the scan found. Live only (shared contract with the Python engine).
bool HandleCashFlows(double &eq)
  {
   if(g_tester)
      return true;
   double bc = AccountInfoDouble(ACCOUNT_BALANCE) + AccountInfoDouble(ACCOUNT_CREDIT);
   if(g_lastBalCred != 0.0 && MathAbs(bc - g_lastBalCred) < 0.005)
     {
      g_pendingSince = 0;
      ulong allTk = g_allTicket;
      TrackNewestDeal();
      if(MathAbs(AccountInfoDouble(ACCOUNT_BALANCE) + AccountInfoDouble(ACCOUNT_CREDIT) - bc) >= 0.005)
         g_allTicket = allTk;   // a deal was booked during the read: it explains the change on the next cycle
      return true;
     }
   double flow = 0.0;
   int count = 0;
   ulong flowTk0 = g_flowTicket;
   ulong allTk0 = g_allTicket;
   datetime scan0 = g_flowScan;
   bool fresh = ScanDeals(flow, count);
   double eqNow = AccountInfoDouble(ACCOUNT_EQUITY);
   double bcNow = AccountInfoDouble(ACCOUNT_BALANCE) + AccountInfoDouble(ACCOUNT_CREDIT);
   if(MathAbs(bcNow - bc) >= 0.005)
     {
      // a deal was booked while the history was scanned (eq may contain a flow the scan missed): nothing is
      // applied, the same scan is repeated on the next cycle
      g_flowTicket = flowTk0;
      g_allTicket = allTk0;
      g_flowScan = scan0;
      return false;
     }
   if(eqNow > 0.0)
      eq = eqNow;
   if(count > 0)
      ApplyCashFlow(flow, count, eq);
   if(fresh || count > 0 || g_lastBalCred == 0.0)
     {
      g_lastBalCred = bc;
      g_pendingSince = 0;
      return true;
     }
   datetime now = TimeLocal();
   if(g_pendingSince == 0)
      g_pendingSince = now;
   if(Secs(now, g_pendingSince) > GT_FLOW_PENDING_SEC || Secs(now, g_pendingSince) < 0)
     {
      Log(StringFormat("balance changed by %+.2f without a matching deal in the history: treated as profit/loss",
                       bc - g_lastBalCred), true);
      g_lastBalCred = bc;
      g_pendingSince = 0;
      return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Broker clock verification (live only; the tester uses the rule)   |
//+------------------------------------------------------------------+
// an advance of the server time counts as a new tick only when it is not larger than the local time that passed
// since the previous server time was first seen (+ GT_CLOCK_ADV_SEC): the jump from an old cached time to Friday's
// last tick after a weekend terminal start is no new tick, while the first tick after a quiet minute or the weekly
// open (the server time moved as far as the local clock) is one
void TrackServerTime(const datetime srv)
  {
   if(srv <= g_lastSrvSeen)
      return;
   datetime now = TimeLocal();
   if(g_lastSrvSeen > 0 && Secs(srv, g_lastSrvSeen) <= Secs(now, g_lastSrvSeenAt) + GT_CLOCK_ADV_SEC)
      g_srvAdvAt = now;
   g_lastSrvSeen = srv;
   g_lastSrvSeenAt = now;
  }

// clock observation from the newest tick (same rule as the Python engine):
//  1 = valid: obs = server - UTC rounded to 30 min
//  0 = no observation: the server time did not advance within GT_CLOCK_ADV_SEC (weekend, holiday, frozen feed,
//      a stale Friday tick after a restart never counts)
// -1 = fresh tick, but server - PC UTC is not within GT_CLOCK_TOL_SEC of a whole half-hour offset within +-14 h:
//      the PC clock is off (diff = server - PC UTC in seconds)
int ObserveOffset(long &obs, long &diff)
  {
   obs = 0;
   diff = 0;
   datetime srv = TimeCurrent();
   datetime gmt = TimeGMT();
   if(srv <= 0 || gmt <= 0)
      return 0;
   datetime now = TimeLocal();
   if(g_srvAdvAt == 0 || Secs(now, g_srvAdvAt) > GT_CLOCK_ADV_SEC || Secs(now, g_srvAdvAt) < 0)
      return 0;
   diff = (long)srv - (long)gmt;
   long nearest = (long)MathRound((double)diff / 1800.0) * 1800;
   if(MathAbs((double)(diff - nearest)) > GT_CLOCK_TOL_SEC || MathAbs((double)nearest) > 14.0 * 3600.0)
      return -1;
   obs = nearest;
   return 1;
  }

// AUTO waits for a verified clock after every start-up; every mode blocks after GT_CLOCK_CONFIRM mismatches
bool ClockAllowsEntries()
  {
   if(g_tester)
      return true;
   if(g_clkBlocked)
      return false;
   return (g_clockMode != GT_SERVER_AUTO || g_clkVerified);
  }

string ClockBlockReason()
  {
   if(g_clkBlocked)
      return "broker clock mismatch";
   if(g_clkOffGridN >= GT_CLOCK_CONFIRM)
      return "broker clock not verified yet (PC clock off - check the Windows time synchronisation)";
   return "broker clock not verified yet";
  }

// gates for every new entry that do not depend on the signal: the broker clock, the first-run deal history and
// an unexplained balance change in this cycle (closes, stops and TP management are never gated)
bool EntryGateOpen(string &why)
  {
   why = "";
   if(!ClockAllowsEntries())
      why = ClockBlockReason();
   else
      if(!g_flowPrimed)
         why = "deal history not confirmed complete yet (first run)";
      else
         if(g_flowPending)
            why = "balance change not in the deal history yet (cash flow pending)";
   return (why == "");
  }

void AdoptOffset(const long obs, const string how)
  {
   bool had = g_clkHave;
   long old = g_clkOffset;
   g_clkOffset = obs;
   g_clkHave = true;
   g_clkVerified = true;
   g_clkBlocked = false;
   g_clkMismatch = 0;
   g_clkCandN = 0;
   g_stateDirty = true;
   if(!had)
      Log(StringFormat("broker clock measured: server = UTC%+.1f h (AUTO, %s); new entries enabled", obs / 3600.0, how),
          true);
   else
      Log(StringFormat("broker clock offset changed: UTC%+.1f h -> UTC%+.1f h (AUTO, %s)", old / 3600.0, obs / 3600.0,
                       how), true);
  }

void ClockMatch(const long obs)
  {
   bool wasBlocked = g_clkBlocked;
   bool wasVerified = g_clkVerified;
   g_clkVerified = true;
   g_clkBlocked = false;
   g_clkMismatch = 0;
   g_clkCandN = 0;
   if(wasBlocked)
      Log(StringFormat("broker clock matches again (UTC%+.1f h): new entries re-enabled", obs / 3600.0), true);
   else
      if(!wasVerified)
         Log(StringFormat("broker clock verified: server = UTC%+.1f h (%s)", obs / 3600.0, EnumToString(g_clockMode)));
  }

// a fresh tick off the half-hour grid is no observation of the broker clock: the PC clock is off. Reported after
// GT_CLOCK_CONFIRM such observations in a row, then at most every GT_REPEAT_SEC
void ClockOffGrid(const long diff)
  {
   long nearest = (long)MathRound((double)diff / 1800.0) * 1800;
   g_clkOffGridSec = (MathAbs((double)nearest) > 14.0 * 3600.0) ? diff : diff - nearest;
   g_clkOffGridN++;
   if(g_clkOffGridN < GT_CLOCK_CONFIRM)
      return;
   datetime now = TimeLocal();
   if(g_clkDriftNote != 0 && Secs(now, g_clkDriftNote) < GT_REPEAT_SEC && Secs(now, g_clkDriftNote) >= 0)
      return;
   g_clkDriftNote = now;
   string effect = (g_clockMode == GT_SERVER_AUTO && !g_clkVerified) ?
                   "new entries wait until the broker clock is verified" : "trading continues with the clock in use";
   Log(StringFormat("PC CLOCK: server time %s vs PC clock %s UTC is %+d s off a whole half-hour offset (%d observations "
                    "in a row) - check the Windows time synchronisation; %s",
                    TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS), TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS),
                    (int)g_clkOffGridSec, g_clkOffGridN, effect), true);
  }

// one counted observation per minute (the first one at once), only from a fresh tick. Gating of new entries
// (shared rule with the Python engine, spec section 3):
//  - AUTO: new entries wait until a valid observation after start-up confirms the clock in use (the adopted /
//    persisted offset, or the NY7 rule when there is none). A different offset is adopted after GT_CLOCK_CONFIRM
//    identical observations - the first offset too, unless it matches the NY7 rule.
//  - NY7 / Fixed: new entries are allowed from the start.
//  - every mode: GT_CLOCK_CONFIRM consecutive mismatches that are not adopted block new entries until the clock
//    matches again.
// Closes, time stops, TP management and hard stops always run with the clock in use.
void CheckClock()
  {
   if(g_tester)
      return;
   datetime now = TimeLocal();
   if(g_clkLastObs != 0 && Secs(now, g_clkLastObs) < GT_CLOCK_OBS_SEC && Secs(now, g_clkLastObs) >= 0)
      return;
   long obs = 0;
   long diff = 0;
   int r = ObserveOffset(obs, diff);
   if(r == 0)
      return;   // no fresh tick: no observation, keep the last verdict
   g_clkLastObs = now;
   if(r < 0)
     {
      ClockOffGrid(diff);
      return;
     }
   g_clkOffGridN = 0;
   datetime srv = TimeCurrent();
   long expected = (long)srv - (long)ServerToUTC(srv);   // clock in use (AUTO without an offset: NY7 rule)
   bool autoMode = (g_clockMode == GT_SERVER_AUTO);
   if(obs == expected)
     {
      if(autoMode && !g_clkHave)
         AdoptOffset(obs, "matches the NY7 rule");
      else
         ClockMatch(obs);
      return;
     }
   g_clkMismatch++;
   if(autoMode)
     {
      if(g_clkCandN > 0 && obs == g_clkCand)
         g_clkCandN++;
      else
        {
         g_clkCand = obs;
         g_clkCandN = 1;
        }
      Log(StringFormat("broker clock observation UTC%+.1f h differs from UTC%+.1f h in use (%d/%d identical)",
                       obs / 3600.0, expected / 3600.0, g_clkCandN, GT_CLOCK_CONFIRM));
      if(g_clkCandN >= GT_CLOCK_CONFIRM)
        {
         AdoptOffset(obs, IntegerToString(GT_CLOCK_CONFIRM) + " identical observations");
         return;
        }
     }
   else
      if(g_clkMismatch <= GT_CLOCK_CONFIRM || InpVerbose)
         Log(StringFormat("broker clock observation %+.1f h differs from the configured %+.1f h (%d/%d)", obs / 3600.0,
                          expected / 3600.0, g_clkMismatch, GT_CLOCK_CONFIRM));
   if(g_clkMismatch >= GT_CLOCK_CONFIRM && !g_clkBlocked)
     {
      g_clkBlocked = true;
      if(autoMode)
         Log(StringFormat("broker clock unstable: observations disagree with UTC%+.1f h in use - new entries blocked; "
                          "closes and stops keep working", expected / 3600.0), true);
      else
         Log(StringFormat("broker clock mismatch: observed %+.1f h, expected %+.1f h (InpServerTime=%s). New entries "
                          "blocked; closes and stops keep working. Use GT_SERVER_AUTO or correct the setting / PC clock.",
                          obs / 3600.0, expected / 3600.0, EnumToString(InpServerTime)), true);
     }
  }

//+------------------------------------------------------------------+
//| Monitoring (notification only, never changes trading)             |
//+------------------------------------------------------------------+
bool Ready()
  {
   if(g_tester)
      return true;
   return (TerminalInfoInteger(TERMINAL_CONNECTED) != 0 && AccountInfoInteger(ACCOUNT_LOGIN) > 0 &&
           AccountInfoDouble(ACCOUNT_EQUITY) > 0.0);
  }

void MonitorConnection()
  {
   if(g_tester)
      return;
   datetime now = TimeLocal();
   if(TerminalInfoInteger(TERMINAL_CONNECTED) != 0)
     {
      if(g_discNotified)
         Log("connection to the trade server restored", true);
      g_discSince = 0;
      g_discNotified = false;
      return;
     }
   if(g_discSince == 0)
      g_discSince = now;
   if(Secs(now, g_discSince) >= 600 && (!g_discNotified || Secs(now, g_discNote) >= GT_REPEAT_SEC))
     {
      Log(StringFormat("DISCONNECTED from the trade server for %d min", (int)(Secs(now, g_discSince) / 60)), true);
      g_discNotified = true;
      g_discNote = now;
     }
  }

// run-time permission checks; while trading is blocked no close or entry is sent, state keeps updating
bool TradingPermitted(string &why)
  {
   why = "";
   if(g_tester)
      return true;
   if(TerminalInfoInteger(TERMINAL_CONNECTED) == 0)
      why = "terminal not connected to the trade server";
   else
      if(TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0)
         why = "Algo Trading is disabled in the terminal";
      else
         if(MQLInfoInteger(MQL_TRADE_ALLOWED) == 0)
            why = "'Allow Algo Trading' is unchecked in the EA properties";
         else
            if(AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0)
               why = "trading is disabled for this account (investor password?)";
            else
               if(AccountInfoInteger(ACCOUNT_TRADE_EXPERT) == 0)
                  why = "the broker does not allow expert trading on this account";
   int now = (why == "") ? 1 : 0;
   datetime t = TimeLocal();
   if(now != g_permit)
     {
      if(now == 0)
         Log("TRADING BLOCKED: " + why, true);
      else
         if(g_permit == 0)
            Log("trading permitted again", true);
      g_permit = now;
      g_permitNote = t;
     }
   else
      if(now == 0 && Secs(t, g_permitNote) >= GT_REPEAT_SEC)
        {
         Log("TRADING STILL BLOCKED: " + why, true);
         g_permitNote = t;
        }
   return (now == 1);
  }

string StatusText()
  {
   string s = "";
   if(g_halted)
      s += ", HALTED (max DD)";
   if(g_monthHalted)
      s += ", month stop";
   if(g_dayHalted)
      s += ", day stop";
   if(InBlackout())
      s += ", blackout";
   string gate;
   if(!EntryGateOpen(gate))
      s += ", entries blocked: " + gate;
   if(g_permit == 0)
      s += ", trading blocked";
   if(s == "")
      return "ok";
   return StringSubstr(s, 2);
  }

void CheckDDAlert(const double eq)
  {
   if(g_tester || InpDDAlert <= 0.0 || eq <= 0.0)
      return;
   double dd = Drawdown(eq);
   if(dd * 100.0 < InpDDAlert || g_ddAlertDay == g_day)
      return;
   g_ddAlertDay = g_day;   // at most once per market day while above the level
   Log(StringFormat("DD ALERT: drawdown %.1f%% from peak %.2f (equity %.2f; alert at %.1f%%, halt at %.1f%%)",
                    dd * 100.0, g_peak, eq, InpDDAlert, InpMaxDDHalt), true);
  }

// our deals of the market day mday (entries opened, exits closed, net P/L incl. swap and commission)
void CountDayTrades(const datetime mday, int &opened, int &closed, double &pl)
  {
   opened = 0;
   closed = 0;
   pl = 0.0;
   long fromL = (long)mday - 3 * 86400;
   if(fromL < 0)
      fromL = 0;
   if(!HistorySelect((datetime)fromL, (datetime)((long)mday + 3 * 86400)))
      return;
   int n = HistoryDealsTotal();
   long ids[];
   int nid = 0;
   for(int i = 0; i < n; i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk == 0)
         continue;
      if(IsOurMagic(HistoryDealGetInteger(tk, DEAL_MAGIC)) && HistoryDealGetInteger(tk, DEAL_ENTRY) == DEAL_ENTRY_IN)
        {
         ArrayResize(ids, nid + 1);
         ids[nid] = HistoryDealGetInteger(tk, DEAL_POSITION_ID);
         nid++;
        }
     }
   for(int i = 0; i < n; i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(tk == 0)
         continue;
      long type = HistoryDealGetInteger(tk, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
         continue;
      bool ours = IsOurMagic(HistoryDealGetInteger(tk, DEAL_MAGIC));
      long pid = HistoryDealGetInteger(tk, DEAL_POSITION_ID);
      for(int j = 0; j < nid && !ours; j++)
         if(ids[j] == pid)
            ours = true;
      if(!ours)
         continue;
      datetime dt = (datetime)HistoryDealGetInteger(tk, DEAL_TIME);
      if(DayStart(ServerToMarket(dt)) != mday)
         continue;
      if(HistoryDealGetInteger(tk, DEAL_ENTRY) == DEAL_ENTRY_IN)
         opened++;
      else
         closed++;
      pl += HistoryDealGetDouble(tk, DEAL_PROFIT) + HistoryDealGetDouble(tk, DEAL_SWAP) +
            HistoryDealGetDouble(tk, DEAL_COMMISSION);
     }
  }

// at each market-day change (live): equity, DD, month-to-date flow-adjusted return, trades of the day, state
void DailySummary(const double eq)
  {
   if(!g_sumPending)
      return;
   g_sumPending = false;
   if(g_tester)
      return;
   int opened = 0;
   int closed = 0;
   double pl = 0.0;
   CountDayTrades(g_sumDay, opened, closed, pl);
   Log(StringFormat("DAILY %s: equity %.2f, DD %.2f%%, month-to-date %+.2f%% (flow-adjusted), "
                    "trades %d opened / %d closed, P/L %.2f, state: %s",
                    TimeToString(g_sumDay, TIME_DATE), eq, Drawdown(eq) * 100.0, g_sumMonthRet * 100.0,
                    opened, closed, pl, StatusText()), true);
  }

// FX market hours in market time (NY+7): Monday 00:00 to Friday 24:00, without Dec 25 and Jan 1
bool MarketOpenNow()
  {
   datetime mkt = UTCToMarket(TimeGMT());
   MqlDateTime s;
   TimeToStruct(mkt, s);
   if(s.day_of_week == 0 || s.day_of_week == 6)
      return false;
   if((s.mon == 12 && s.day == 25) || (s.mon == 1 && s.day == 1))
      return false;
   if(s.day_of_week == 1 && s.hour == 0 && s.min < 30)
      return false;   // weekly open: some brokers start quoting late
   return true;
  }

void CheckStaleFeed()
  {
   if(g_tester || TerminalInfoInteger(TERMINAL_CONNECTED) == 0)
      return;
   datetime now = TimeLocal();
   datetime ref = (g_srvAdvAt > g_startLocal) ? g_srvAdvAt : g_startLocal;
   bool stale = (Secs(now, ref) > GT_STALE_FEED_SEC && MarketOpenNow());
   if(stale)
     {
      if(!g_staleNotified || Secs(now, g_staleNote) >= GT_REPEAT_SEC)
        {
         Log(StringFormat("STALE FEED: no new quote for %d min while the market should be open (last server time %s)",
                          (int)(Secs(now, ref) / 60), TimeToString(g_lastSrvSeen, TIME_DATE | TIME_MINUTES)), true);
         g_staleNotified = true;
         g_staleNote = now;
        }
     }
   else
      if(g_staleNotified && g_srvAdvAt > g_staleNote)
        {
         Log("quotes are coming in again", true);
         g_staleNotified = false;
        }
  }

//+------------------------------------------------------------------+
//| Single instance per account                                       |
//+------------------------------------------------------------------+
string LockName(const long login)
  {
   return "GT_" + IntegerToString(login) + "_lock";
  }

// 1 = lock held, 0 = account not known yet (retry later), -1 = held by another live instance
int AcquireLock()
  {
   if(g_tester)
      return 1;
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   if(login <= 0)
      return 0;
   if(g_lockToken <= 0.0)
     {
      long cid = ChartID();
      if(cid < 0)
         cid = -cid;
      g_lockToken = 1.0 + (double)(cid % 1000000000);   // per chart; kept across chart/parameter changes
     }
   string nm = LockName(login);
   string hb = nm + "hb";
   double holder = 0.0;
   if(GlobalVariableCheck(nm))
      holder = GlobalVariableGet(nm);
   else
     {
      GlobalVariableTemp(nm);   // temporary: never written to disk, gone when the terminal stops
      GlobalVariableSet(nm, 0.0);
     }
   double beat = 0.0;
   if(GlobalVariableCheck(hb))
      beat = GlobalVariableGet(hb);
   bool fresh = (beat > 0.0 && MathAbs((double)TimeLocal() - beat) <= GT_LOCK_STALE_SEC);
   if(holder != 0.0 && holder != g_lockToken && fresh)
      return -1;
   if(holder != g_lockToken && !GlobalVariableSetOnCondition(nm, g_lockToken, holder))
      return -1;
   if(!GlobalVariableCheck(hb))
      GlobalVariableTemp(hb);
   GlobalVariableSet(hb, (double)TimeLocal());
   g_lockHeld = true;
   g_lockLogin = login;
   g_lockBeat = TimeLocal();
   return 1;
  }

// stops this copy (and returns false) when another instance holds the lock; the losing copy does not save state
void StopThisCopy(const string msg)
  {
   g_stopping = true;
   g_lockHeld = false;
   g_stateLoaded = false;
   Log(msg, true);
   FlushNotify(true);
   ExpertRemove();
  }

bool LockHeartbeat()
  {
   if(g_tester || !g_lockHeld)
      return true;
   datetime now = TimeLocal();
   if(Secs(now, g_lockBeat) < GT_LOCK_BEAT_SEC && Secs(now, g_lockBeat) >= 0)
      return true;
   string nm = LockName(g_lockLogin);
   double holder = 0.0;
   if(GlobalVariableCheck(nm))
      holder = GlobalVariableGet(nm);
   if(holder != g_lockToken)
     {
      if(holder != 0.0)
        {
         StopThisCopy("another GreatestTrader instance took over this account - this copy stops");
         return false;
        }
      g_lockHeld = false;   // lock deleted by hand: take it again
      if(AcquireLock() < 0)
        {
         StopThisCopy("another GreatestTrader instance runs on this account - this copy stops");
         return false;
        }
      return true;
     }
   GlobalVariableSet(nm + "hb", (double)now);
   g_lockBeat = now;
   return true;
  }

void ReleaseLock()
  {
   if(g_tester || !g_lockHeld)
      return;
   string nm = LockName(g_lockLogin);
   if(GlobalVariableCheck(nm) && GlobalVariableGet(nm) == g_lockToken)
     {
      GlobalVariableSet(nm, 0.0);
      if(GlobalVariableCheck(nm + "hb"))
         GlobalVariableSet(nm + "hb", 0.0);
     }
   g_lockHeld = false;
  }

//+------------------------------------------------------------------+
//| Start-up: state is loaded on the first Ready() cycle               |
//+------------------------------------------------------------------+
// InpResetHalt: clears the max-DD halt and rebases the peak once; a persisted marker stops a forgotten
// InpResetHalt=true from clearing a later halt at every restart (re-armed by a start with InpResetHalt=false).
// Called after the first cash-flow scan of the session (ProcessAll): a deposit / withdrawal made while the EA was
// off is applied to the old peak first, then the peak is rebased to the equity that contains it.
void ApplyResetHalt(const double eq)
  {
   if(g_tester)
      return;
   if(!InpResetHalt)
     {
      g_resetHaltDone = false;
      return;
     }
   if(g_resetHaltDone)
     {
      Log("InpResetHalt=true was already applied: set it back to false (the halt is not cleared again)", true);
      return;
     }
   bool was = g_halted;
   double oldPeak = g_peak;
   g_halted = false;
   g_peak = eq;
   g_resetHaltDone = true;
   g_stateDirty = true;
   Log(StringFormat("RESET HALT (InpResetHalt): halt %s -> off, peak %.2f -> %.2f (current equity); other state kept. "
                    "Set InpResetHalt back to false.", was ? "on" : "off", oldPeak, eq), true);
  }

bool EnsureState()
  {
   if(g_stateLoaded)
      return true;
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   if(!g_tester)
     {
      // the margin mode is reliable only after authorisation (netting = 0 before it), hence checked here too
      if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
        {
         Log("NETTING / exchange account: GreatestTrader needs a HEDGING account (on netting, closing the EA position "
             "would also close manual or other-EA volume on the same symbol). The EA stops.", true);
         g_stopping = true;
         FlushNotify(true);
         ExpertRemove();
         return false;
        }
      int lk = AcquireLock();
      if(lk < 0)
        {
         StopThisCopy("another GreatestTrader instance runs on this account - this copy stops");
         return false;
        }
      if(lk == 0)
         return false;
     }
   g_gvPrefix = "GT_" + IntegerToString(login) + "_";
   ArrayResize(g_gvName, 0);
   ArrayResize(g_gvVal, 0);
   g_gvRefreshAt = 0;
   ResetStateVars();
   bool flowKnown = false;
   if(!g_tester)
     {
      flowKnown = GlobalVariableCheck(g_gvPrefix + "flowtk");
      LoadState();
     }
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(g_peak <= 0.0)
      g_peak = eq;
   g_flowPrimed = (g_tester || flowKnown);   // first run: TryPrimeFlowCursor (ProcessAll) once the history is complete
   g_resetDue = true;                        // InpResetHalt: after the first cash-flow scan (ProcessAll)
   g_stateLoaded = true;
   g_stateDirty = true;
   if(!g_tester)
     {
      string clk = "none";
      if(g_clkHave)
         clk = "UTC" + DoubleToString(g_clkOffset / 3600.0, 1) + " h";
      Log(StringFormat("state loaded (%s*): peak %.2f, DD %.1f%%, halted=%s, month stop=%s, persisted clock offset=%s",
                       g_gvPrefix, g_peak, Drawdown(eq) * 100.0, g_halted ? "yes" : "no", g_monthHalted ? "yes" : "no",
                       clk));
     }
   return true;
  }

//+------------------------------------------------------------------+
//| Input validation (parity with Python Config.validate)             |
//+------------------------------------------------------------------+
bool Bad(const string msg)
  {
   Print("[GT] invalid input: ", msg);
   return false;
  }

bool PctOK(const double v)
  {
   return (v >= 0.0 && v <= 100.0);
  }

bool ValidateInputs()
  {
   if(InpRiskProfile != GT_RISK_CONSERVATIVE && InpRiskProfile != GT_RISK_STANDARD &&
      InpRiskProfile != GT_RISK_AGGRESSIVE && InpRiskProfile != GT_RISK_CUSTOM)
      return Bad("InpRiskProfile must be 0..3");
   if(InpServerTime != GT_SERVER_NY7 && InpServerTime != GT_SERVER_FIXED && InpServerTime != GT_SERVER_AUTO)
      return Bad("InpServerTime must be 0 (NY7), 1 (Fixed) or 2 (Auto)");
   if(BaseRisk(true) < 0.0 || BaseRisk(true) > 0.05 + 1e-12 || BaseRisk(false) < 0.0 || BaseRisk(false) > 0.05 + 1e-12)
      return Bad("risk per trade (InpRiskNR / InpRiskES) must be between 0 and 5 percent");
   if(InpBBPeriod < 2 || InpATRPeriod < 2 || InpMaxHoldBars < 1 || InpESATRPeriodH1 < 1)
      return Bad("indicator periods must be >= 2 (ES ATR >= 1) and InpMaxHoldBars >= 1");
   if(InpBBDev <= 0.0 || InpSLATRMult <= 0.0 || InpESSLATRMult <= 0.0 || InpATRCostMult < 0.0)
      return Bad("InpBBDev, InpSLATRMult and InpESSLATRMult must be > 0, InpATRCostMult >= 0");
   if(InpMaxOpen < 1 || InpMaxPerCurrency < 1)
      return Bad("InpMaxOpen and InpMaxPerCurrency must be >= 1");
   if(InpMaxSameDirPerCcy < 0 || InpVolLookbackDays < 0 || InpEqFilterDays < 0)
      return Bad("InpMaxSameDirPerCcy, InpVolLookbackDays and InpEqFilterDays must be >= 0");
   if(!PctOK(InpMaxTotalRisk) || !PctOK(InpDailyLossLimit) || !PctOK(InpDailyHardStop) || !PctOK(InpMonthlyLossLimit) ||
      !PctOK(InpMonthlyHardStop) || !PctOK(InpMaxDDHalt) || !PctOK(InpDDScaleFull) || !PctOK(InpVolTargetDaily) ||
      !PctOK(InpDDAlert))
      return Bad("percentage limits (total risk, daily/monthly limits and stops, max DD halt, DD scaling, vol target, "
                 "DD alert) must be between 0 and 100");
   if(InpDDScaleFloor < 0.0 || InpDDScaleFloor > 1.0 || InpEqFilterMult < 0.0 || InpEqFilterMult > 1.0)
      return Bad("InpDDScaleFloor and InpEqFilterMult must be between 0 and 1");
   if(InpVolMultMin < 0.0 || InpVolMultMin > InpVolMultMax)
      return Bad("need 0 <= InpVolMultMin <= InpVolMultMax");
   if(InpMaxLotsPerOrder < 0.0 || InpMaxRiskAmount < 0.0)
      return Bad("InpMaxLotsPerOrder and InpMaxRiskAmount must be >= 0 (0 = off)");
   if(InpEntryHour1 < 0 || InpEntryHour1 > 23 || InpEntryHour2 < -1 || InpEntryHour2 > 23)
      return Bad("NR entry hours must be 0..23 (InpEntryHour2 may be -1)");
   if(InpMaxBandZ < 0.0 || InpMaxBarRangeATR < 0.0)
      return Bad("news filters InpMaxBandZ / InpMaxBarRangeATR must be >= 0 (0 = off)");
   if(InpNRSlippagePips < 0.0 || InpNRMaxSpreadPips < 0.0 || InpESMaxSpreadPips < 0.0)
      return Bad("slippage and max spread inputs must be >= 0");
   if(!(0 <= InpESEntryUTC && InpESEntryUTC < InpESExitUTC && InpESExitUTC <= 23))
      return Bad("need 0 <= InpESEntryUTC < InpESExitUTC <= 23");
   if(InpESDirection != -1 && InpESDirection != 1)
      return Bad("InpESDirection must be -1 (sell) or 1 (buy)");
   if(InpNRMagic <= 0 || InpESMagic <= 0 || InpNRMagic == InpESMagic)
      return Bad("InpNRMagic and InpESMagic must be > 0 and different (magic 0 = manual trades)");
   if(InpDeviationPoints < 0 || InpCommissionPerLot < 0.0)
      return Bad("InpDeviationPoints and InpCommissionPerLot must be >= 0");
   if(InpServerTime == GT_SERVER_FIXED)
     {
      double h2 = InpServerGMTOffset * 2.0;
      if(InpServerGMTOffset < -12.0 || InpServerGMTOffset > 14.0 || MathAbs(h2 - MathRound(h2)) > 1e-9)
         return Bad("InpServerGMTOffset must be within -12..+14 hours in steps of 0.5");
     }
   if(!ParseSymbolMult())
      return false;
   for(int i = 0; i < ArraySize(g_multVal); i++)
      if(g_multVal[i] < 0.0 || BaseRisk(true) * g_multVal[i] > 0.05 + 1e-12 || BaseRisk(false) * g_multVal[i] > 0.05 + 1e-12)
         return Bad("InpSymbolRiskMult: " + g_multSym[i] + " must be >= 0 and risk x multiplier must be <= 5 percent");
   g_boStart = ParseMMDD(InpBlackoutStart);
   g_boEnd = ParseMMDD(InpBlackoutEnd);
   if((StringLen(InpBlackoutStart) > 0 && g_boStart == 0) || (StringLen(InpBlackoutEnd) > 0 && g_boEnd == 0))
      return Bad("blackout start/end must be MM-DD (e.g. 12-20) or empty");
   if((g_boStart == 0) != (g_boEnd == 0))
      return Bad("blackout start/end must both be MM-DD or both empty");
   return true;
  }

//+------------------------------------------------------------------+
//| Main cycle                                                        |
//+------------------------------------------------------------------+
void ProcessAll()
  {
   if(g_stopping || !LockHeartbeat())
      return;
   if(!Ready())
     {
      MonitorConnection();
      return;   // terminal not connected / not authorised / equity 0: no state change, no save, no orders
     }
   MonitorConnection();
   if(!EnsureState())
      return;
   datetime nowSrv = TimeCurrent();
   if(nowSrv <= 0)
      return;
   TrackServerTime(nowSrv);
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(eq <= 0.0)
      return;
   // cash flows are detected BEFORE the equity guards of this cycle (eq is read again after the deal scan).
   // Pending (balance changed, no new deal in the history yet, at most GT_FLOW_PENDING_SEC): no peak raise, no
   // halt / month / day stop decision and no new entry this cycle; closes, time stops and TP management run.
   if(!g_flowPrimed)
      TryPrimeFlowCursor();
   g_flowPending = (g_flowPrimed && !HandleCashFlows(eq));
   if(g_resetDue && g_flowPrimed && !g_flowPending)
     {
      g_resetDue = false;
      ApplyResetHalt(eq);   // after the first cash-flow scan of the session
     }
   CheckClock();
   PruneFails();
   if(!g_flowPending)
     {
      UpdateEquityState(eq, nowSrv);
      CheckDDAlert(eq);
      DailySummary(eq);
     }
   string why;
   if(TradingPermitted(why))
     {
      // while a hard stop is in force our positions must stay flat (also retries failed closes)
      if(g_flatten)
         CloseAllOurs(g_flattenReason);
      else
         if(g_monthHalted || g_dayHalted)
            CloseAllOurs(g_monthHalted ? "monthly hard stop" : "daily hard stop");
      if(InpNREnabled && g_nsym > 0)
         ProcessNR(nowSrv, eq);
      if(InpESEnabled && g_esOk)
         ProcessES(nowSrv, eq);
     }
   CheckStaleFeed();
   SaveState();
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   g_tester = (bool)MQLInfoInteger(MQL_TESTER);
   g_stopping = false;
   g_stateLoaded = false;   // (re)loaded from the global variables on the first Ready() cycle
   g_startLocal = TimeLocal();
   g_clockMode = (g_tester && InpServerTime == GT_SERVER_AUTO) ? GT_SERVER_NY7 : InpServerTime;
   g_clkVerified = false;   // AUTO: the clock must be confirmed by a live observation after every start-up
   g_clkBlocked = false;
   g_clkMismatch = 0;
   g_clkCandN = 0;
   g_clkLastObs = 0;
   g_clkOffGridN = 0;
   g_flowPending = false;
   g_permit = -1;

   if(!ValidateInputs())
      return INIT_PARAMETERS_INCORRECT;

   g_trade.SetDeviationInPoints((ulong)InpDeviationPoints);
   g_trade.SetMarginMode();
   g_trade.LogLevel(LOG_LEVEL_ERRORS);

   if((g_tester || Ready()) && AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
     {
      Print("[GT] this account uses NETTING (or exchange) position accounting. GreatestTrader requires a HEDGING "
            "account: on netting, closing the EA position would also close manual / other-EA volume on the same symbol.");
      return INIT_FAILED;
     }
   if(!g_tester && AcquireLock() < 0)
     {
      Print("[GT] another GreatestTrader instance is already running on account ", AccountInfoInteger(ACCOUNT_LOGIN),
            " (lock ", LockName(AccountInfoInteger(ACCOUNT_LOGIN)), " held by another chart). Remove the other copy first.");
      return INIT_FAILED;
     }

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
         Log("WARNING symbol not available: " + s + " (check InpSymbolSuffix) - it is NOT traded", true);
         continue;
        }
      ArrayResize(g_sym, g_nsym + 1);
      ArrayResize(g_base, g_nsym + 1);
      g_sym[g_nsym] = s;
      g_base[g_nsym] = b;
      g_nsym++;
     }
   // process pairs in alphabetical order, like the Python engine and backtester: when several pairs
   // signal on the same bar and a portfolio guard admits only some of them, the same ones are taken
   for(int i = 1; i < g_nsym; i++)
      for(int j = i; j > 0 && StringCompare(g_base[j - 1], g_base[j]) > 0; j--)
        {
         string tb = g_base[j - 1];
         g_base[j - 1] = g_base[j];
         g_base[j] = tb;
         string ts = g_sym[j - 1];
         g_sym[j - 1] = g_sym[j];
         g_sym[j] = ts;
        }
   ArrayResize(g_lastBar, g_nsym);
   for(int i = 0; i < g_nsym; i++)
      g_lastBar[i] = 0;

   string eb = InpESSymbol;
   StringToUpper(eb);
   g_esSym = eb + InpSymbolSuffix;
   g_esOk = SymbolSelect(g_esSym, true);
   if(InpESEnabled && !g_esOk)
      Log("WARNING Euro Session symbol not available: " + g_esSym + " - ES is NOT traded", true);

   // warm up history for all symbols (tester downloads data on first request)
   MqlRates tmp[];
   for(int i = 0; i < g_nsym; i++)
      CopyRates(g_sym[i], PERIOD_M15, 0, 50, tmp);
   if(g_esOk)
      CopyRates(g_esSym, PERIOD_H1, 0, 50, tmp);

   EventSetTimer(g_tester ? 60 : (InpTimerSec < 1 ? 1 : InpTimerSec));

   string prof = EnumToString(InpRiskProfile);
   Log(StringFormat("started: %d NR symbols, ES=%s, profile=%s, NR risk=%.2f%% (x InpSymbolRiskMult), ES risk=%.2f%%, "
                    "max lots/order=%.2f, max risk amount=%.2f, max DD halt=%.1f%%, server=%s%s",
                    g_nsym, (InpESEnabled && g_esOk) ? g_esSym : "off", prof, BaseRisk(true) * 100.0,
                    BaseRisk(false) * SymbolMult(g_esSym) * 100.0, InpMaxLotsPerOrder, InpMaxRiskAmount, InpMaxDDHalt,
                    EnumToString(InpServerTime),
                    (!g_tester && g_clockMode == GT_SERVER_AUTO) ? " (new entries wait for a verified broker clock)" : ""),
       true);
   if(!g_tester && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0)
      Print("[GT] WARNING: Algo Trading is disabled in the terminal.");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   SaveState(true);   // no-op unless the state was loaded (REASON_INITFAILED cannot overwrite it)
   if(!g_tester && g_stateLoaded &&
      (reason == REASON_REMOVE || reason == REASON_CHARTCLOSE || reason == REASON_TEMPLATE || reason == REASON_PROGRAM ||
       reason == REASON_CLOSE))
     {
      int n = CountOurPositions();
      if(n > 0)
         Log(StringFormat("EA stopped (reason %d) with %d open position(s): time stop / session exit are no longer managed "
                          "(SL/TP stay at the broker)", reason, n), true);
     }
   FlushNotify(true);
   ReleaseLock();
   g_stateLoaded = false;
  }

void OnTick()
  {
   ProcessAll();
  }

void OnTimer()
  {
   ProcessAll();
   FlushNotify();
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
