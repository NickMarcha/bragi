using System.Collections.Generic;
using System.Text.Json;
using Bragi.Client.Config;
using Xunit;

namespace Bragi.Client.Tests;

/// <summary>Following the hub's address (bragi#61), Config/HubSync.cs.</summary>
public class HubSyncTests
{
    private static JsonElement Json(string text) => JsonDocument.Parse(text).RootElement;

    private static readonly string HubJson = """
        {"type":"hub","ws_url":"wss://sagehub.tail08dfa.ts.net/ws/peer","tailscale_ip":"100.84.140.96",
         "lan_ip":"192.168.1.32","deskflow_hosts":["192.168.1.32","100.84.140.96","bad host!"]}
        """;

    [Fact]
    public void ParsesTheHubMessage()
    {
        var hub = HubSync.ParseHub(Json(HubJson))!;
        Assert.Equal("wss://sagehub.tail08dfa.ts.net/ws/peer", hub.WsUrl);
        Assert.Equal("100.84.140.96", hub.TailscaleIp);
        Assert.Equal("192.168.1.32", hub.LanIp);
        Assert.Equal(new[] { "192.168.1.32", "100.84.140.96" }, hub.DeskflowHosts);  // the bad one dropped
        Assert.Null(HubSync.ParseHub(Json("""{"type":"kvm"}""")));
        Assert.Null(HubSync.ParseHub(Json("""{"type":"hub","tailscale_ip":"not an ip"}"""))!.TailscaleIp);
    }

    [Fact]
    public void ParsesOnlyRealMoves()
    {
        Assert.Equal("wss://newhub.ts.net/ws/peer", HubSync.ParseMoved(Json("""{"type":"hub_moved","ws_url":"wss://newhub.ts.net/ws/peer"}""")));
        Assert.Null(HubSync.ParseMoved(Json("""{"type":"hub_moved","ws_url":"http://evil/ws/peer"}""")));
        Assert.Null(HubSync.ParseMoved(Json("""{"type":"hub_moved","ws_url":"wss://newhub.ts.net/other"}""")));
        Assert.Null(HubSync.ParseMoved(Json("""{"type":"hub_moved"}""")));
    }

    [Fact]
    public void ChangesTheAudioAddressOnlyWhenItDiffers()
    {
        var hub = HubSync.ParseHub(Json(HubJson))!;
        Assert.Empty(HubSync.EnvChanges(new Dictionary<string, string> { ["BRAGI_HOST_IP"] = "100.84.140.96" }, hub));
        Assert.Equal(new Dictionary<string, string?> { ["BRAGI_HOST_IP"] = "100.84.140.96" },
            HubSync.EnvChanges(new Dictionary<string, string> { ["BRAGI_HOST_IP"] = "100.114.201.69" }, hub));
        // The old name goes, even when its value is already right.
        Assert.Equal(new Dictionary<string, string?> { ["BRAGI_HOST_IP"] = "100.84.140.96", ["SAGEPI_TAILSCALE_IP"] = null },
            HubSync.EnvChanges(new Dictionary<string, string> { ["SAGEPI_TAILSCALE_IP"] = "100.84.140.96" }, hub));
        // A hub without Tailscale changes nothing.
        Assert.Empty(HubSync.EnvChanges(new Dictionary<string, string> { ["BRAGI_HOST_IP"] = "1.2.3.4" },
            new HubInfo(null, null, "192.168.1.32", new List<string>())));
    }

    [Fact]
    public void UpdatesEnvLinesInPlace()
    {
        var lines = new[] { "# roc link", "SAGEPI_TAILSCALE_IP=100.114.201.69", "PEER_NAME=sagedeck", "BRAGI_WS_URL=wss://old/ws/peer" };
        var output = HubSync.UpdateEnvLines(lines, new Dictionary<string, string?>
        {
            ["BRAGI_HOST_IP"] = "100.84.140.96", ["SAGEPI_TAILSCALE_IP"] = null, ["BRAGI_WS_URL"] = "wss://new/ws/peer",
        });
        Assert.Equal(new[] { "# roc link", "PEER_NAME=sagedeck", "BRAGI_WS_URL=wss://new/ws/peer", "BRAGI_HOST_IP=100.84.140.96" }, output);
    }

    [Fact]
    public void PointsDeskflowsRemoteHostAtTheHub()
    {
        var conf = "[core]\nremoteHost=\"192.168.1.13, 100.114.201.69\\n\"\nport=24800\n";
        var updated = HubSync.UpdateDeskflowConf(conf, new[] { "192.168.1.32", "100.84.140.96" });
        Assert.Equal("[core]\nremoteHost=\"192.168.1.32, 100.84.140.96\\n\"\nport=24800\n", updated);
        Assert.Null(HubSync.UpdateDeskflowConf(updated!, new[] { "192.168.1.32", "100.84.140.96" }));  // already right
        Assert.Null(HubSync.UpdateDeskflowConf("[core]\nport=24800\n", new[] { "192.168.1.32" }));      // not a client here
        Assert.Null(HubSync.UpdateDeskflowConf(conf, new List<string>()));                               // hub has no KVM
    }
}
