// Compile-only stand-ins for the parts of the NinjaTrader 8 API that BredBot.cs uses, so CI can
// catch typos and C# features NinjaTrader's compiler rejects. They do nothing. The real check is
// compiling in NinjaTrader (NinjaScript Editor, F5); keep these in step with the NT8 help guide.
using System;
using System.Collections.Generic;

namespace NinjaTrader.Core
{
    public class GeneralOptionsStub { public TimeZoneInfo TimeZoneInfo = TimeZoneInfo.Local; }
    public static class Globals
    {
        public static string UserDataDir = "";
        public static GeneralOptionsStub GeneralOptions = new GeneralOptionsStub();
    }
}

namespace NinjaTrader.Cbi
{
    public enum OrderAction { Buy, BuyToCover, Sell, SellShort }
    public enum OrderType { Limit, Market, MIT, StopMarket, StopLimit }
    public enum OrderState { Accepted, Cancelled, CancelPending, CancelSubmitted, ChangePending, ChangeSubmitted, Filled,
                             Initialized, PartFilled, Rejected, Submitted, TriggerPending, Unknown, Working }
    public enum MarketPosition { Flat, Long, Short }
    public enum ErrorCode { NoError, Panic, OrderRejected, UnableToChangeOrder, UnableToCancelOrder }
    public class Account { public string Name = ""; }
    public class MasterInstrument { public string Name = ""; public double PointValue; public double TickSize; }
    public class Instrument { public string FullName = ""; public MasterInstrument MasterInstrument = new MasterInstrument(); }
    public class Order
    {
        public string Name = ""; public OrderState OrderState; public OrderAction OrderAction;
        public double AverageFillPrice; public int Filled; public string OrderId = "";
        // stub-only bookkeeping for the fake broker
        public OrderType OrderType; public double LimitPrice, StopPrice; public string Oco = "";
    }
    public class Execution { public Order Order; public double Price; public int Quantity; }
}

namespace NinjaTrader.Data
{
    public enum BarsPeriodType { Tick, Volume, Second, Minute, Day, Week, Month, Year }
    public class BarsPeriod { public BarsPeriodType BarsPeriodType; public int Value; }
    public class Bars
    {
        public List<DateTime> T = new List<DateTime>();
        public List<double[]> B = new List<double[]>();  // o h l c
        public int Count { get { return T.Count; } }
        public DateTime GetTime(int i) { return T[i]; }
        public double GetOpen(int i) { return B[i][0]; }
        public double GetHigh(int i) { return B[i][1]; }
        public double GetLow(int i) { return B[i][2]; }
        public double GetClose(int i) { return B[i][3]; }
    }
}

namespace NinjaTrader.NinjaScript
{
    public enum State { SetDefaults, Configure, Active, DataLoaded, Historical, Transition, Realtime, Terminated, Finalized }
    public enum Calculate { OnBarClose, OnEachTick, OnPriceChange }
    [AttributeUsage(AttributeTargets.Property)] public class NinjaScriptPropertyAttribute : Attribute { }
    public interface ISeries<T> { T this[int barsAgo] { get; } }
    public class Series<T> : ISeries<T>
    {
        public List<T> Values = new List<T>();
        public T this[int barsAgo] { get { return Values[Values.Count - 1 - barsAgo]; } }
    }
}

namespace NinjaTrader.NinjaScript.Strategies
{
    using NinjaTrader.Cbi;
    using NinjaTrader.Data;

    /// <summary>Compile stand-in, plus a tiny fake broker so tools/ninjastub/WrapperTest.cs can drive
    /// BredBot through whole sessions. Fills: market at the bar open, stops at the stop or the open if
    /// it gapped through, both with slippage; limits only when price trades a tick through them.</summary>
    public abstract class Strategy
    {
        public string Name, Description;
        public NinjaTrader.NinjaScript.Calculate Calculate;
        public bool IsUnmanaged, IsExitOnSessionCloseStrategy, TraceOrders;
        public int BarsRequiredToTrade, ExitOnSessionCloseSeconds, BarsInProgress;
        public NinjaTrader.NinjaScript.State State;
        public Account Account = new Account();
        public Instrument Instrument = new Instrument();
        public BarsPeriod BarsPeriod = new BarsPeriod();
        public Bars[] BarsArray = { new Bars(), new Bars() };
        public NinjaTrader.NinjaScript.Series<DateTime> Time = new NinjaTrader.NinjaScript.Series<DateTime>();
        public NinjaTrader.NinjaScript.Series<double> Open = new NinjaTrader.NinjaScript.Series<double>(),
            High = new NinjaTrader.NinjaScript.Series<double>(), Low = new NinjaTrader.NinjaScript.Series<double>(),
            Close = new NinjaTrader.NinjaScript.Series<double>(), Volume = new NinjaTrader.NinjaScript.Series<double>();
        protected virtual void OnStateChange() { }
        protected virtual void OnBarUpdate() { }
        protected virtual void OnOrderUpdate(Order order, double limitPrice, double stopPrice, int quantity, int filled,
                                             double averageFillPrice, OrderState orderState, DateTime time, ErrorCode error, string comment) { }
        protected virtual void OnExecutionUpdate(Execution execution, string executionId, double price, int quantity,
                                                 MarketPosition marketPosition, string orderId, DateTime time) { }
        public void AddDataSeries(BarsPeriodType t, int value) { }
        public List<string> Printed = new List<string>();
        public void Print(object o) { Printed.Add(Convert.ToString(o)); }

        // ---- fake broker
        public List<Order> Working = new List<Order>();
        public double Slip;
        public int Net;  // account position, contracts
        public bool RejectStops;
        public DateTime Now;  // the fake broker's clock, in NinjaTrader's time zone
        int ids;
        int mainThread = System.Threading.Thread.CurrentThread.ManagedThreadId;

        void Update(Order o, OrderState st)
        {
            o.OrderState = st;
            OnOrderUpdate(o, o.LimitPrice, o.StopPrice, 1, o.Filled, o.AverageFillPrice, st, DateTime.Now, ErrorCode.NoError, "");
        }

        public Order SubmitOrderUnmanaged(int barsInProgress, OrderAction action, OrderType type, int quantity,
                                          double limitPrice, double stopPrice, string oco, string signalName)
        {
            var o = new Order { Name = signalName, OrderAction = action, OrderType = type, LimitPrice = limitPrice,
                                StopPrice = stopPrice, Oco = oco ?? "", OrderId = "o" + (++ids) };
            if (quantity != 1) throw new InvalidOperationException("BredBot must trade exactly one contract");
            if (State != NinjaTrader.NinjaScript.State.Realtime) throw new InvalidOperationException("order outside realtime");
            Update(o, OrderState.Submitted);
            if (RejectStops && type == OrderType.StopMarket && (action == OrderAction.Sell || action == OrderAction.BuyToCover))
            {
                Update(o, OrderState.Rejected);
                return o;
            }
            Working.Add(o);
            Update(o, OrderState.Working);
            return o;
        }

        public void ChangeOrder(Order order, int quantity, double limitPrice, double stopPrice)
        {
            if (!Working.Contains(order)) return;
            order.LimitPrice = limitPrice; order.StopPrice = stopPrice;
            Update(order, OrderState.Working);
        }

        public void CancelOrder(Order order)
        {
            if (!Working.Remove(order)) return;
            Update(order, OrderState.Cancelled);
        }

        public void TriggerCustomEvent(Action<object> customEvent, object state)
        {
            if (System.Threading.Thread.CurrentThread.ManagedThreadId == mainThread) customEvent(state);
        }

        void Fill(Order o, double px)
        {
            Working.Remove(o);
            o.Filled = 1; o.AverageFillPrice = px;
            Net += (o.OrderAction == OrderAction.Buy || o.OrderAction == OrderAction.BuyToCover) ? 1 : -1;
            if (o.Oco.Length > 0)
                foreach (var x in Working.ToArray())
                    if (x.Oco == o.Oco) CancelOrder(x);
            Update(o, OrderState.Filled);
            OnExecutionUpdate(new Execution { Order = o, Price = px, Quantity = 1 }, "x" + o.OrderId, px, 1,
                              Net > 0 ? MarketPosition.Long : (Net < 0 ? MarketPosition.Short : MarketPosition.Flat), o.OrderId, Now);
        }

        /// <summary>Fill working orders against a new bar (before it is shown to the strategy).</summary>
        public void Match(double o, double h, double l, double tick)
        {
            for (int pass = 0; pass < 4; pass++)  // orders sent from a fill can fill on the same bar
            {
                bool any = false;
                foreach (var x in Working.ToArray())
                {
                    if (!Working.Contains(x)) continue;
                    bool buy = x.OrderAction == OrderAction.Buy || x.OrderAction == OrderAction.BuyToCover;
                    int d = buy ? 1 : -1;
                    if (x.OrderType == OrderType.Market) { Fill(x, o + d * Slip); any = true; }
                    else if (x.OrderType == OrderType.StopMarket && (buy ? h >= x.StopPrice : l <= x.StopPrice))
                    { Fill(x, (buy ? Math.Max(x.StopPrice, o) : Math.Min(x.StopPrice, o)) + d * Slip); any = true; }
                    else if (x.OrderType == OrderType.Limit && (buy ? l <= x.LimitPrice - tick : h >= x.LimitPrice + tick))
                    { Fill(x, x.LimitPrice); any = true; }
                }
                if (!any) break;
            }
        }

        public void SetState(NinjaTrader.NinjaScript.State s) { State = s; OnStateChange(); }
        public void Bar() { BarsInProgress = 0; OnBarUpdate(); }
    }
}
