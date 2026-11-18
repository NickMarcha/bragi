using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.Versioning;
using System.Text.Json;
using System.Threading.Tasks;
using Bragi.Client.Tray;

namespace Bragi.Client.Monitors;

/// <summary>
/// KDE's monitors, from <c>kscreen-doctor -j</c>: positions in logical
/// pixels (the space KDE and Deskflow's Wayland client share), the mode, the
/// scaling and the rotation. The EDIDs come from /sys/class/drm, which any
/// user may read.
/// </summary>
[SupportedOSPlatform("linux")]
public sealed class KscreenMonitors : IMonitorSource
{
    public string Kind => "kscreen";

    public async Task<IReadOnlyList<MonitorInfo>> ReadAsync()
    {
        try
        {
            var result = await ProcessRunner.RunAsync("kscreen-doctor", ["-j"], TimeSpan.FromSeconds(5));
            return result.ExitCode == 0 ? Parse(result.Stdout, ReadEdid) : [];
        }
        catch (Exception ex) when (ex is ProcessRunException or IOException or System.ComponentModel.Win32Exception)
        {
            return [];
        }
    }

    /// <summary>The EDID behind a connector ("DP-2"): /sys/class/drm/card*-DP-2/edid.</summary>
    private static byte[]? ReadEdid(string connector)
    {
        try
        {
            foreach (var dir in Directory.EnumerateDirectories("/sys/class/drm", $"card*-{connector}"))
            {
                var data = File.ReadAllBytes(Path.Combine(dir, "edid"));
                if (data.Length >= 128)
                {
                    return data;
                }
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
        }
        return null;
    }

    /// <summary>
    /// kscreen-doctor's JSON to monitors; the EDID read per connector. An
    /// output with a replicationSource ("Replica of" in KDE's settings) shows
    /// a copy of that output: it becomes one of its mirrors, and its own
    /// position, which KDE still reports, means nothing.
    /// </summary>
    public static IReadOnlyList<MonitorInfo> Parse(string json, Func<string, byte[]?> edidFor)
    {
        var monitors = new List<MonitorInfo>();
        var byOutputId = new Dictionary<int, int>();
        var replicas = new List<(int Source, MirrorInfo Mirror)>();
        try
        {
            using var doc = JsonDocument.Parse(json);
            foreach (var output in doc.RootElement.GetProperty("outputs").EnumerateArray())
            {
                if (!Bool(output, "connected") || !Bool(output, "enabled"))
                {
                    continue;
                }
                var source = output.TryGetProperty("replicationSource", out var rs) && rs.ValueKind == JsonValueKind.Number ? rs.GetInt32() : 0;
                if (source != 0)
                {
                    var name = output.GetProperty("name").GetString() ?? "";
                    var mode = output.GetProperty("size");
                    replicas.Add((source, MirrorInfo.From(edidFor(name) is { } raw ? Edid.Parse(raw) : null, name,
                        mode.GetProperty("width").GetInt32(), mode.GetProperty("height").GetInt32())));
                    continue;
                }
                var connector = output.GetProperty("name").GetString() ?? "";
                var pos = output.GetProperty("pos");
                var size = output.GetProperty("size");
                var (pixelWidth, pixelHeight) = (size.GetProperty("width").GetInt32(), size.GetProperty("height").GetInt32());
                var scale = output.TryGetProperty("scale", out var s) && s.GetDouble() > 0 ? s.GetDouble() : 1.0;
                // KScreen's rotation flags: 1 none, 2 left, 4 inverted, 8 right.
                var rotation = output.TryGetProperty("rotation", out var r) ? r.GetInt32() switch { 2 => 90, 4 => 180, 8 => 270, _ => 0 } : 0;
                var (w, h) = rotation is 90 or 270 ? (pixelHeight, pixelWidth) : (pixelWidth, pixelHeight);
                var edid = edidFor(connector) is { } bytes ? Edid.Parse(bytes) : null;
                if (output.TryGetProperty("id", out var id) && id.ValueKind == JsonValueKind.Number)
                {
                    byOutputId[id.GetInt32()] = monitors.Count;
                }
                monitors.Add(MonitorInfo.From(edid, connector,
                    pos.GetProperty("x").GetInt32(), pos.GetProperty("y").GetInt32(),
                    (int)Math.Round(w / scale), (int)Math.Round(h / scale),
                    pixelWidth, pixelHeight, scale, rotation,
                    output.TryGetProperty("priority", out var p) && p.GetInt32() == 1));
            }
            foreach (var (source, mirror) in replicas)
            {
                if (byOutputId.TryGetValue(source, out var index))
                {
                    monitors[index] = monitors[index] with { Mirrors = [.. monitors[index].Mirrors, mirror] };
                }
            }
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException or FormatException)
        {
            return [];
        }
        return monitors.OrderBy(m => m.X).ThenBy(m => m.Y).ToList();
    }

    private static bool Bool(JsonElement element, string name) =>
        element.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;
}
