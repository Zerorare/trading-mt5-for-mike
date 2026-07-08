//+------------------------------------------------------------------+
//|                                                   EmaCross34.mq5 |
//|        Pure EMA 3/4 cross strategy - cross open / cross close    |
//|        and reverse, with same-direction re-entry after a         |
//|        small-loss (whipsaw) close. No stop loss, no take profit. |
//+------------------------------------------------------------------+
#property version   "1.00"
#property strict
#property description "EMA 3/4 cross bot: opens on cross, closes and reverses on the"
#property description "opposite cross. If a trade is closed within -N pips (small loss),"
#property description "it re-enters the same direction instead of reversing (whipsaw filter)."

#include <Trade/Trade.mqh>

//--- inputs
input int                InpFastPeriod         = 3;           // Fast EMA period
input int                InpSlowPeriod         = 4;           // Slow EMA period
input ENUM_APPLIED_PRICE InpAppliedPrice       = PRICE_CLOSE; // EMA applied price
input double             InpLots               = 0.01;        // Lot size
input bool               InpEnableReentry      = true;        // Re-enter same direction after small-loss close
input double             InpReentryMaxLossPips = 10.0;        // Re-entry: max loss in pips (close within -N pips)
input int                InpMaxConsecReentries = 1;           // Re-entry: max consecutive re-entries
input bool               InpEnterOnStart       = false;       // Open in current EMA direction on the first bar
input ulong              InpMagic              = 340034;      // Magic number
input int                InpSlippagePoints     = 20;          // Max slippage (points)
input string             InpTradeComment       = "EMA 3/4 cross";

CTrade   trade;
int      fastHandle    = INVALID_HANDLE;
int      slowHandle    = INVALID_HANDLE;
datetime lastBarTime   = 0;
int      pendingSignal = 0;    // direction we still need to open: +1 long, -1 short, 0 none
int      reentryCount  = 0;    // consecutive same-direction re-entries so far
double   pipSize       = 0.0;
double   lots          = 0.0;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpFastPeriod >= InpSlowPeriod)
     {
      Print("Fast EMA period must be smaller than slow EMA period");
      return(INIT_PARAMETERS_INCORRECT);
     }

   fastHandle = iMA(_Symbol, _Period, InpFastPeriod, 0, MODE_EMA, InpAppliedPrice);
   slowHandle = iMA(_Symbol, _Period, InpSlowPeriod, 0, MODE_EMA, InpAppliedPrice);
   if(fastHandle == INVALID_HANDLE || slowHandle == INVALID_HANDLE)
     {
      Print("Failed to create EMA indicator handles");
      return(INIT_FAILED);
     }

   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   pipSize = (digits == 3 || digits == 5) ? 10.0 * _Point : _Point;

   // clamp the requested lot size to what the symbol allows
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   lots = MathMax(minLot, MathMin(maxLot, InpLots));
   if(lotStep > 0.0)
      lots = MathRound(lots / lotStep) * lotStep;
   if(MathAbs(lots - InpLots) > lotStep / 2.0)
      PrintFormat("Requested lot %.2f adjusted to %.2f for %s", InpLots, lots, _Symbol);

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpSlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);

   PrintFormat("EmaCross34 started on %s %s | EMA %d/%d | lots %.2f | re-entry %s (within -%.1f pips, max %d)",
               _Symbol, EnumToString(_Period), InpFastPeriod, InpSlowPeriod, lots,
               InpEnableReentry ? "ON" : "OFF", InpReentryMaxLossPips, InpMaxConsecReentries);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(fastHandle != INVALID_HANDLE)
      IndicatorRelease(fastHandle);
   if(slowHandle != INVALID_HANDLE)
      IndicatorRelease(slowHandle);
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   datetime barTime = iTime(_Symbol, _Period, 0);
   if(barTime != lastBarTime)
     {
      bool firstBar = (lastBarTime == 0);
      lastBarTime = barTime;
      int signal = ReadSignal(firstBar && InpEnterOnStart);
      if(!firstBar || InpEnterOnStart)
         if(signal != 0)
            pendingSignal = signal;
     }

   // pendingSignal stays set until the trade actions succeed, so failed
   // closes/opens are retried on the following ticks
   if(pendingSignal != 0)
      ProcessSignal(pendingSignal);
  }

//+------------------------------------------------------------------+
//| Cross detection on closed bars.                                  |
//| stateOnly=true returns the current EMA relation instead of a     |
//| cross (used for the optional enter-on-start).                    |
//+------------------------------------------------------------------+
int ReadSignal(const bool stateOnly)
  {
   double f[], s[];
   ArraySetAsSeries(f, true);
   ArraySetAsSeries(s, true);
   if(CopyBuffer(fastHandle, 0, 1, 2, f) != 2)   // f[0]=last closed bar, f[1]=bar before it
      return(0);
   if(CopyBuffer(slowHandle, 0, 1, 2, s) != 2)
      return(0);

   if(stateOnly)
      return(f[0] > s[0] ? 1 : (f[0] < s[0] ? -1 : 0));

   bool crossUp   = (f[1] <= s[1] && f[0] > s[0]);
   bool crossDown = (f[1] >= s[1] && f[0] < s[0]);
   return(crossUp ? 1 : (crossDown ? -1 : 0));
  }

//+------------------------------------------------------------------+
void ProcessSignal(const int dir)
  {
   long   posType   = -1;
   double openPrice = 0.0;
   ulong  ticket    = 0;

   if(!FindPosition(posType, openPrice, ticket))
     {
      // flat: just open in the pending direction
      if(OpenPosition(dir))
         pendingSignal = 0;
      return;
     }

   int posDir = (posType == POSITION_TYPE_BUY) ? 1 : -1;
   if(posDir == dir)
     {
      // signal confirms the position we already hold (e.g. after a re-entry)
      reentryCount  = 0;
      pendingSignal = 0;
      return;
     }

   double pips = FloatingPips(posDir, openPrice);
   if(!trade.PositionClose(ticket))
     {
      PrintFormat("PositionClose failed: %d %s - retrying",
                  trade.ResultRetcode(), trade.ResultRetcodeDescription());
      return;
     }

   bool reenter = InpEnableReentry
                  && pips <= 0.0
                  && pips >= -InpReentryMaxLossPips
                  && reentryCount < InpMaxConsecReentries;

   int target;
   if(reenter)
     {
      reentryCount++;
      target = posDir;
      PrintFormat("Closed %s at %.1f pips (whipsaw) -> re-entry #%d in the same direction",
                  posDir > 0 ? "LONG" : "SHORT", pips, reentryCount);
     }
   else
     {
      reentryCount = 0;
      target = dir;
      PrintFormat("Closed %s at %.1f pips -> reversing to %s",
                  posDir > 0 ? "LONG" : "SHORT", pips, dir > 0 ? "LONG" : "SHORT");
     }

   pendingSignal = target;
   if(OpenPosition(target))
      pendingSignal = 0;
  }

//+------------------------------------------------------------------+
bool FindPosition(long &type, double &openPrice, ulong &ticket)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic)
         continue;
      type      = PositionGetInteger(POSITION_TYPE);
      openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      ticket    = tk;
      return(true);
     }
   return(false);
  }

//+------------------------------------------------------------------+
//| Signed floating result of the open position in pips              |
//+------------------------------------------------------------------+
double FloatingPips(const int posDir, const double openPrice)
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return(0.0);
   double closePrice = (posDir > 0) ? tick.bid : tick.ask;
   double diff       = (posDir > 0) ? (closePrice - openPrice) : (openPrice - closePrice);
   return(diff / pipSize);
  }

//+------------------------------------------------------------------+
bool OpenPosition(const int dir)
  {
   bool ok = (dir > 0)
             ? trade.Buy(lots, _Symbol, 0.0, 0.0, 0.0, InpTradeComment)
             : trade.Sell(lots, _Symbol, 0.0, 0.0, 0.0, InpTradeComment);
   if(!ok)
      PrintFormat("Open %s failed: %d %s - retrying",
                  dir > 0 ? "LONG" : "SHORT",
                  trade.ResultRetcode(), trade.ResultRetcodeDescription());
   return(ok);
  }
//+------------------------------------------------------------------+
