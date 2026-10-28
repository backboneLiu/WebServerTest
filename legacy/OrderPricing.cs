// OrderPricing.cs
// 客户遗留计价模块（C# / .NET 6）
// 背景：这套逻辑跑在客户 ERP 里已 7 年，现在要把 AI 报价助手接进同一套规则。
// 客户的原话：「结果必须一模一样，一分钱都不能差。」

using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace Legacy.Billing
{
    public enum CustomerTier { Normal, Silver, Gold, Platinum }

    public class OrderLine
    {
        public string Sku { get; set; } = "";
        public int Quantity { get; set; }
        public decimal UnitPrice { get; set; }
        public bool IsPromotional { get; set; }
    }

    public class Order
    {
        public string OrderId { get; set; } = "";
        public CustomerTier Tier { get; set; }
        public DateTime PlacedAt { get; set; }
        public List<OrderLine> Lines { get; set; } = new();
    }

    public class PricingException : Exception
    {
        public PricingException(string message) : base(message) { }
    }

    public class BatchResult
    {
        public string OrderId { get; }
        public decimal? Total { get; }
        public string? Error { get; }

        public BatchResult(string orderId, decimal? total, string? error)
        {
            OrderId = orderId;
            Total = total;
            Error = error;
        }
    }

    public static class OrderPricing
    {
        private static readonly Dictionary<CustomerTier, decimal> TierDiscount = new()
        {
            { CustomerTier.Normal,   0.00m },
            { CustomerTier.Silver,   0.03m },
            { CustomerTier.Gold,     0.07m },
            { CustomerTier.Platinum, 0.12m },
        };

        private const decimal PromotionalFactor = 0.5m;
        private const decimal MaxDiscountRate = 0.30m;
        private const decimal MinChargeable = 1.00m;

        public static decimal CalculateTotal(Order order)
        {
            if (order is null) throw new ArgumentNullException(nameof(order));
            if (order.Lines is null || order.Lines.Count == 0)
                throw new PricingException($"Order {order.OrderId} has no lines");

            decimal subtotal = 0m;
            foreach (var line in order.Lines)
            {
                if (line.Quantity <= 0)
                    throw new PricingException($"Invalid quantity {line.Quantity} for SKU {line.Sku}");
                if (line.UnitPrice < 0)
                    throw new PricingException($"Negative price for SKU {line.Sku}");

                var lineTotal = line.Quantity * line.UnitPrice;
                if (line.IsPromotional) lineTotal *= PromotionalFactor;
                subtotal += lineTotal;
            }

            var tierRate = TierDiscount[order.Tier];
            var volumeRate = VolumeDiscount(subtotal);
            var totalRate = Math.Min(tierRate + volumeRate, MaxDiscountRate);

            var total = subtotal * (1m - totalRate);
            var rounded = Math.Round(total, 2, MidpointRounding.AwayFromZero);
            return Math.Max(rounded, MinChargeable);
        }

        private static decimal VolumeDiscount(decimal subtotal)
        {
            if (subtotal >= 50000m) return 0.10m;
            if (subtotal >= 20000m) return 0.05m;
            if (subtotal >= 5000m)  return 0.02m;
            return 0m;
        }

        // 批量计价：一条失败不影响其余，PricingException 直接记录，其他异常最多重试 3 次
        public static async Task<IReadOnlyList<BatchResult>> CalculateBatchAsync(
            IEnumerable<Order> orders, Func<Order, Task> notify, int maxRetry = 3)
        {
            var results = new List<BatchResult>();
            foreach (var order in orders)
            {
                var attempt = 0;
                while (true)
                {
                    try
                    {
                        var total = CalculateTotal(order);
                        await notify(order);
                        results.Add(new BatchResult(order.OrderId, total, null));
                        break;
                    }
                    catch (PricingException ex)
                    {
                        results.Add(new BatchResult(order.OrderId, null, ex.Message));
                        break;
                    }
                    catch (Exception ex) when (attempt < maxRetry - 1)
                    {
                        attempt++;
                        await Task.Delay(200 * attempt);
                    }
                }
            }
            return results;
        }
    }
}
