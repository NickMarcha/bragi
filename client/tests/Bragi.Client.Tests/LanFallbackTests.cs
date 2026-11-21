using System;
using System.Collections.Generic;
using System.Net;
using Bragi.Client.Config;
using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

/// <summary>Audio over the home network while the hub isn't reachable over Tailscale (bragi#81), Config/LanFallback.cs.</summary>
public class LanFallbackTests
{
    private static readonly DateTime T0 = new(2026, 11, 21, 12, 0, 0, DateTimeKind.Utc);
    private const string HubLan = "192.168.1.32";

    [Fact]
    public void WaitsTwentySecondsBeforeProbing()
    {
        var f = new LanFallback();
        Assert.False(f.ShouldProbe(T0, HubLan, linkEnabled: true));  // never disconnected
        f.Disconnected(T0);
        f.Disconnected(T0.AddSeconds(10));  // a failed retry doesn't restart the clock
        Assert.False(f.ShouldProbe(T0.AddSeconds(19), HubLan, true));
        Assert.True(f.ShouldProbe(T0.AddSeconds(20), HubLan, true));
    }

    [Fact]
    public void NeverProbesWithoutAHubLanAddressOrWithTheLinkOff()
    {
        var f = new LanFallback();
        f.Disconnected(T0);
        Assert.False(f.ShouldProbe(T0.AddMinutes(1), null, true));
        Assert.False(f.ShouldProbe(T0.AddMinutes(1), HubLan, linkEnabled: false));
    }

    [Fact]
    public void SwitchesOnlyWhenTheLanAddressAnswers()
    {
        var f = new LanFallback();
        f.Disconnected(T0);
        Assert.False(f.Probed(HubLan, answered: false));
        Assert.Null(f.ActiveHost);
        Assert.True(f.Probed(HubLan, answered: true));
        Assert.Equal(HubLan, f.ActiveHost);
        // Switched: no more probing, and a second answer changes nothing.
        Assert.False(f.ShouldProbe(T0.AddMinutes(1), HubLan, true));
        Assert.False(f.Probed(HubLan, true));
    }

    [Fact]
    public void SwitchesBackWhenTheSocketReconnects()
    {
        var f = new LanFallback();
        Assert.False(f.Connected());  // nothing to undo
        f.Disconnected(T0);
        f.Probed(HubLan, true);
        Assert.True(f.Connected());
        Assert.Null(f.ActiveHost);
        Assert.False(f.Connected());  // every later message: nothing to do
        // A probe answered after the socket came back must not switch.
        Assert.False(f.Probed(HubLan, true));
    }

    private static (IPAddress, IPAddress) If(string address, string mask) => (IPAddress.Parse(address), IPAddress.Parse(mask));

    [Fact]
    public void ReportsTheAddressOnTheHubsSubnet()
    {
        var local = new List<(IPAddress, IPAddress)>
        {
            If("100.101.102.103", "255.255.255.255"),  // Tailscale
            If("172.17.0.1", "255.255.0.0"),           // Docker
            If("192.168.1.9", "255.255.255.0"),
        };
        Assert.Equal("192.168.1.9", LanFallback.PickLanAddress(local, HubLan));
    }

    [Fact]
    public void ReportsNothingAwayFromHome()
    {
        var local = new List<(IPAddress, IPAddress)> { If("10.0.0.5", "255.255.255.0"), If("100.101.102.103", "255.192.0.0") };
        Assert.Null(LanFallback.PickLanAddress(local, HubLan));
        Assert.Null(LanFallback.PickLanAddress(local, null));
        // A hub "LAN" address that isn't private is never trusted.
        Assert.Null(LanFallback.PickLanAddress([If("100.64.0.9", "255.192.0.0")], "100.64.0.1"));
        // A zero mask would match anything.
        Assert.Null(LanFallback.PickLanAddress([If("192.168.7.2", "0.0.0.0")], HubLan));
    }

    [Theory]
    [InlineData("10.1.2.3", true)]
    [InlineData("172.16.0.1", true)]
    [InlineData("172.31.255.255", true)]
    [InlineData("172.32.0.1", false)]
    [InlineData("192.168.1.32", true)]
    [InlineData("100.64.0.1", false)]
    [InlineData("8.8.8.8", false)]
    [InlineData("::1", false)]
    public void PrivateAddresses(string address, bool lan) => Assert.Equal(lan, LanFallback.IsLanAddress(IPAddress.Parse(address)));

    [Fact]
    public void RemembersTheHubsLanAddressWithoutTouchingTheAudioAddress()
    {
        var hub = new HubInfo(null, "100.84.140.96", HubLan, []);
        var current = new Dictionary<string, string> { ["BRAGI_HOST_IP"] = "100.84.140.96" };
        Assert.Empty(HubSync.EnvChanges(current, hub));
        Assert.Equal(new Dictionary<string, string?> { ["BRAGI_HUB_LAN_IP"] = HubLan }, HubSync.RememberChanges(current, hub));
        current["BRAGI_HUB_LAN_IP"] = HubLan;
        Assert.Empty(HubSync.RememberChanges(current, hub));
        Assert.Empty(HubSync.RememberChanges(current, hub with { LanIp = null }));
    }

    [Fact]
    public void TheTrayStatusSaysWhenAudioGoesOverTheHomeNetwork()
    {
        Assert.Equal("Link enabled", TrayIconManager.StatusDetail("Link enabled", false));
        Assert.Equal("Link enabled. Audio over the home network (Tailscale is down)", TrayIconManager.StatusDetail("Link enabled", true));
    }
}
