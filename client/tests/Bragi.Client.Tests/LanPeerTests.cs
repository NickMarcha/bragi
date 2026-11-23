using System.Collections.Generic;
using System.Linq;
using System.Security.Cryptography;
using System.Text.Json;
using Bragi.Client.Config;
using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

/// <summary>The peer socket over the LAN while Tailscale is down (bragi#90):
/// Config/HubSync.cs remembers what the hub offers, Tray/PeerPresenceClient.cs
/// pins its certificate.</summary>
public class LanPeerTests
{
    private static readonly string Cert = new('a', 64);
    private static readonly string Token = new('b', 64);

    private static JsonElement Json(string text) => JsonDocument.Parse(text).RootElement;

    private static HubInfo Hub(string lanPeer) => HubSync.ParseHub(Json(
        $$"""{"type":"hub","lan_ip":"192.168.1.32","lan_peer":{{lanPeer}}}"""))!;

    [Fact]
    public void TheHubMessageCarriesTheLanSocket()
    {
        Assert.Equal(new LanPeerInfo(8443, Cert, Token), Hub($$"""{"port":8443,"cert_sha256":"{{Cert}}","token":"{{Token}}"}""").LanPeer);
        // An older hub says nothing; anything malformed counts as nothing.
        Assert.Null(HubSync.ParseHub(Json("""{"type":"hub"}"""))!.LanPeer);
        Assert.Null(Hub($$"""{"port":"8443","cert_sha256":"{{Cert}}","token":"{{Token}}"}""").LanPeer);
        Assert.Null(Hub($$"""{"port":70000,"cert_sha256":"{{Cert}}","token":"{{Token}}"}""").LanPeer);
        Assert.Null(Hub($$"""{"port":8443,"cert_sha256":"short","token":"{{Token}}"}""").LanPeer);
        Assert.Null(Hub("""[]""").LanPeer);
    }

    [Fact]
    public void ItIsRememberedOnceAndBuildsTheEndpoint()
    {
        var hub = Hub($$"""{"port":8443,"cert_sha256":"{{Cert}}","token":"{{Token}}"}""");
        var changes = HubSync.RememberChanges(new Dictionary<string, string>(), hub);
        Assert.Equal(new[] { "BRAGI_HUB_LAN_CERT", "BRAGI_HUB_LAN_IP", "BRAGI_HUB_LAN_PORT", "BRAGI_HUB_LAN_TOKEN" },
            changes.Keys.Order());
        var stored = changes.ToDictionary(c => c.Key, c => c.Value!);
        Assert.Empty(HubSync.RememberChanges(stored, hub));

        var endpoint = HubSync.LanEndpointFrom(stored, "sage-dev")!;
        Assert.Equal("wss://192.168.1.32:8443/ws/peer/sage-dev", endpoint.Uri.ToString());
        Assert.Equal((Cert, Token), (endpoint.CertSha256, endpoint.Token));

        Assert.Null(HubSync.LanEndpointFrom(stored, null));
        Assert.Null(HubSync.LanEndpointFrom(new Dictionary<string, string> { ["BRAGI_HUB_LAN_IP"] = "192.168.1.32" }, "sage"));
    }

    [Fact]
    public void OnlyThePinnedCertificateIsAccepted()
    {
        var der = new byte[] { 1, 2, 3 };
        var pin = System.Convert.ToHexStringLower(SHA256.HashData(der));
        Assert.True(PeerPresenceClient.Pinned(der, pin));
        Assert.False(PeerPresenceClient.Pinned(new byte[] { 1, 2, 4 }, pin));
        Assert.False(PeerPresenceClient.Pinned(der, pin.ToUpperInvariant()));
    }
}
