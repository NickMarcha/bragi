using System.Collections.Generic;
using System.Text.Json;
using Bragi.Client.Monitors;
using Xunit;

namespace Bragi.Client.Tests;

/// <summary>
/// A computer's monitors for the KVM layout (Monitors/). The fixtures are
/// sagedeck docked: an MSI MAG 27CQ6F and an LG TV on, the Legion Go's own
/// panel off.
/// </summary>
public class MonitorTests
{
    private const string MsiEdid = "AP///////wA2adk8AQEBAQYjAQOAPCF4Krh6plVPnicJT1A/zwCBQIGAlQCzAAEBAQEBAQEBVl4AoKCgKVAwIDUAVVAhAAAaAAAA/QAwkB7WPP8KICAgICAgAAAA/ABNQUcgMjdDUTZGCiAgAAAA/wBDRDlNMjg1MjAwMDkxAeUCA0DxTAECAwSQERITnz9gYSMJFweDAQAAagMMACAAPEQgAABn2F3EAXiAAGgaAAABATCQ5uMFAwHmBgAAAAAAb8IAoKCgVVAwIDUAVVAhAAAeAjqAGHE4LUBYLEUAVVAhAAAeUekAoKCgVVAwIDUAVVAhAAAeAAAAAAAAAAAAzw==";
    private const string LgTvEdid = "AP///////wAebQEAAQEBAQEYAQOAoFp4Cu6Ro1RMmSYPUFShCAAxQEVAYUBxQIGAAQEBAQEBAjqAGHE4LUBYLEUAoFoAAAAeZiFQsFEAGzBAcDYAoFoAAAAeAAAA/QA6Ph5TEAAKICAgICAgAAAA/ABMRyBUVgogICAgICAgAUECAynxThCfBBMTEwMCEiAhIiIBKQkHBwkHBwkHB2cDDAAQAIAe4wUAAAI6gBhxOC1AWCxFAKBaAAAAHgEdAIBR0BogbohVAKBaAAAAGgI6gBhxOC1AWCxFAKBaAAAAHmYhULBRABswQHA2AKBaAAAAHgAAAAAAAAAAAAAAAAAAgQ==";
    private const string GoPanelEdid = "AP///////wAwrgEAiIiIAGwhAQSlDBN4CA3JoFdHmCcSSEwAAAABAQEBAQEBAQEBAQEBAQEBdYNAcGEAIqHeHuQEuoghAAAeAAAAEAAAAAAAAAAAAAAAAAAAAAAA/QAylhQ8RgAKICAgICAgAAAA/ABHbyBEaXNwbGF5CiAgAddwEisAAAMBKGITAYQ/BtsAO4AdAP8JgQAdgAMAYhMBhD8G2wA7gB0A/wk3D9aOAwDVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAkA==";
    private const string Kscreen = """
{"outputs":[{"brightness":1,"clones":[],"connected":true,"currentModeId":"1","enabled":true,"followPreferredMode":false,"id":1,"maxBpc":0,"name":"DP-2","overscan":0,"pos":{"x":0,"y":0},"preferredModes":["1"],"priority":1,"replicationSource":0,"rgbRange":0,"rotation":1,"scale":1.5,"size":{"height":1440,"width":2560},"sizeMM":{"height":336,"width":597},"type":14},{"brightness":1,"clones":[],"connected":true,"currentModeId":"40","enabled":true,"followPreferredMode":false,"id":2,"maxBpc":0,"name":"DP-3","overscan":0,"pos":{"x":1707,"y":0},"preferredModes":["40"],"priority":2,"replicationSource":1,"rgbRange":0,"rotation":1,"scale":1.35,"size":{"height":1080,"width":1920},"sizeMM":{"height":90,"width":160},"type":14},{"brightness":0.3,"clones":[],"connected":true,"currentModeId":"67","edrPolicy":1,"enabled":false,"followPreferredMode":false,"id":3,"maxBpc":0,"name":"eDP-1","overscan":0,"pos":{"x":0,"y":0},"preferredModes":["66","67","68"],"priority":1,"replicationSource":0,"rotation":2,"scale":2,"size":{"height":1600,"width":2560},"sizeMM":{"height":190,"width":120},"type":7}],"screen":{"currentSize":{"height":960,"width":3129},"id":0,"maxActiveOutputsCount":3,"maxSize":{"height":64000,"width":64000},"minSize":{"height":0,"width":0}},"tabletModeAvailable":true,"tabletModeEngaged":false}
""";

    private static byte[] Bytes(string base64) => System.Convert.FromBase64String(base64);

    [Fact]
    public void An_edid_names_the_monitor_and_recognises_it_anywhere()
    {
        var msi = Edid.Parse(Bytes(MsiEdid))!;
        Assert.Equal("MSI-3CD9-CD9M285200091", msi.Id);
        Assert.Equal("MAG 27CQ6F", msi.Name);
        Assert.Equal((597, 336), (msi.WidthMm, msi.HeightMm));  // timing mm, agrees with 60x33 cm
    }

    [Fact]
    public void Edid_sizes_trust_centimetres_over_a_timing_that_disagrees()
    {
        var tv = Edid.Parse(Bytes(LgTvEdid))!;
        Assert.Equal("LG TV", tv.Name);
        Assert.Equal((1600, 900), (tv.WidthMm, tv.HeightMm));   // not the 160x90 mm aspect ratio
        var go = Edid.Parse(Bytes(GoPanelEdid))!;
        Assert.Equal("Go Display", go.Name);
        Assert.Equal((120, 190), (go.WidthMm, go.HeightMm));    // not 698x392 mm
    }

    [Fact]
    public void Not_an_edid_is_null()
    {
        Assert.Null(Edid.Parse(new byte[128]));
        Assert.Null(Edid.Parse(Bytes(MsiEdid)[..100]));
    }

    [Fact]
    public void Kde_monitors_in_logical_pixels_with_their_edids()
    {
        var edids = new Dictionary<string, byte[]>
        {
            ["DP-2"] = Bytes(MsiEdid), ["DP-3"] = Bytes(LgTvEdid), ["eDP-1"] = Bytes(GoPanelEdid),
        };
        var monitors = KscreenMonitors.Parse(Kscreen, c => edids.GetValueOrDefault(c));

        // The Go's own panel is off, and the TV is a replica of the MSI ("replicationSource": 1):
        // one desktop area, with the TV as its mirror.
        var msi = Assert.Single(monitors);
        Assert.Equal(("MSI-3CD9-CD9M285200091", "MAG 27CQ6F", "DP-2"), (msi.Id, msi.Name, msi.Connector));
        Assert.Equal((0, 0, 1707, 960), (msi.X, msi.Y, msi.Width, msi.Height));  // 2560x1440 at 150%
        Assert.Equal((2560, 1440, 1.5, 0, true), (msi.PixelWidth, msi.PixelHeight, msi.Scale, msi.Rotation, msi.Primary));
        var tv = Assert.Single(msi.Mirrors);
        Assert.Equal(("GSM-0001-16843009", "LG TV", "DP-3", 1920, 1080, 1600), (tv.Id, tv.Name, tv.Connector, tv.PixelWidth, tv.PixelHeight, tv.WidthMm));
    }

    [Fact]
    public void A_kde_output_that_isnt_a_replica_is_a_monitor_of_its_own()
    {
        var extended = Kscreen.Replace("\"replicationSource\":1", "\"replicationSource\":0");
        Assert.NotEqual(Kscreen, extended);
        var monitors = KscreenMonitors.Parse(extended, _ => null);
        Assert.Equal(2, monitors.Count);
        Assert.Equal(("DP-3", 1707, 1422, 800), (monitors[1].Connector, monitors[1].X, monitors[1].Width, monitors[1].Height));
        Assert.Empty(monitors[0].Mirrors);
    }

    [Fact]
    public void Kde_without_an_edid_falls_back_to_the_connector_and_bad_json_to_nothing()
    {
        var monitors = KscreenMonitors.Parse(Kscreen, _ => null);
        Assert.Equal(("connector-DP-2", "DP-2", (int?)null), (monitors[0].Id, monitors[0].Name, monitors[0].WidthMm));
        Assert.Empty(KscreenMonitors.Parse("not json", _ => null));
    }

    [Fact]
    public async Task Monitors_go_to_the_host_after_a_connect_and_again_only_when_they_change()
    {
        var source = new FakeSource();
        var sent = new List<string>();
        var reporter = new MonitorReporter(source) { Send = m => { sent.Add(JsonSerializer.Serialize(m)); return Task.FromResult(true); } };

        await reporter.CheckAsync();
        Assert.Empty(sent);  // not connected yet

        reporter.OnMessage(JsonDocument.Parse("""{"type":"kvm"}""").RootElement);
        await WaitFor(() => sent.Count == 1);
        Assert.Contains("\"type\":\"monitors\"", sent[0]);
        Assert.Contains("\"source\":\"test\"", sent[0]);

        await reporter.CheckAsync();
        Assert.Single(sent);  // nothing changed

        source.Now = [source.Now[0] with { Scale = 1.25 }];
        await reporter.CheckAsync();
        Assert.Equal(2, sent.Count);
        Assert.Contains("\"scale\":1.25", sent[1]);

        source.Now = [source.Now[0] with { Mirrors = [new MirrorInfo("GSM-0001-16843009", "LG TV", "DP-3", 1920, 1080, 1600, 900)] }];
        await reporter.CheckAsync();
        Assert.Equal(3, sent.Count);  // a mirror coming or going is a change too
        Assert.Contains("\"name\":\"LG TV\"", sent[2]);

        reporter.OnDisconnected();
        reporter.OnMessage(JsonDocument.Parse("""{"type":"kvm"}""").RootElement);
        await WaitFor(() => sent.Count == 4);  // a new connection hears it again
    }

    private static async Task WaitFor(System.Func<bool> condition)
    {
        for (var i = 0; i < 100 && !condition(); i++)
        {
            await Task.Delay(10);
        }
        Assert.True(condition());
    }

    private sealed class FakeSource : IMonitorSource
    {
        public IReadOnlyList<MonitorInfo> Now { get; set; } =
            [MonitorInfo.From(null, "DISPLAY1", 0, 0, 2560, 1440, 2560, 1440, 1.0, 0, true)];
        public string Kind => "test";
        public Task<IReadOnlyList<MonitorInfo>> ReadAsync() => Task.FromResult(Now);
    }
}
