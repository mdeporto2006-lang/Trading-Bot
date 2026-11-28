// Mono's minimal install has no System.ComponentModel.DataAnnotations.dll; NinjaTrader has the real one.
using System;

namespace System.ComponentModel.DataAnnotations
{
    [AttributeUsage(AttributeTargets.Property)]
    public class DisplayAttribute : Attribute
    {
        public string Name { get; set; }
        public string Description { get; set; }
        public int Order { get; set; }
        public string GroupName { get; set; }
    }
}
