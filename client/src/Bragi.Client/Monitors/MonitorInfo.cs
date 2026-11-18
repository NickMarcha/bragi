using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;

namespace Bragi.Client.Monitors;

/// <summary>
/// One monitor as this computer lays it out. X, Y, Width and Height are in
/// the desktop's own coordinates, the space Deskflow's client works in:
/// physical pixels on Windows, logical pixels (after scaling) on KDE.
/// PixelWidth/PixelHeight are the mode, Scale the OS's scaling, Rotation in
/// degrees. Id recognises the same monitor on any computer (its EDID), or is
/// the connector when the EDID can't be read.
/// </summary>
public sealed record MonitorInfo(
    string Id, string Name, string Connector,
    int X, int Y, int Width, int Height,
    int PixelWidth, int PixelHeight, double Scale, int Rotation, bool Primary,
    int? WidthMm, int? HeightMm)
{
    /// <summary>Built from what the OS says and the monitor's EDID, if any.</summary>
    public static MonitorInfo From(Edid? edid, string connector, int x, int y, int width, int height,
        int pixelWidth, int pixelHeight, double scale, int rotation, bool primary) =>
        new(edid?.Id ?? $"connector-{connector}", edid?.Name ?? connector, connector, x, y, width, height,
            pixelWidth, pixelHeight, Math.Round(scale, 3), rotation, primary, edid?.WidthMm, edid?.HeightMm);
}

/// <summary>Where a computer's monitors come from: Windows itself, or KDE.</summary>
public interface IMonitorSource
{
    /// <summary>"windows" or "kscreen", for the host's logs and the setup page.</summary>
    string Kind { get; }

    /// <summary>The enabled monitors now; an empty list when they can't be read.</summary>
    Task<IReadOnlyList<MonitorInfo>> ReadAsync();
}

public static class MonitorMessage
{
    /// <summary>The "monitors" message for the Bragi host (app/monitors.py).</summary>
    public static object Build(string source, IReadOnlyList<MonitorInfo> monitors) => new
    {
        type = "monitors",
        source,
        monitors = monitors.Select(m => new
        {
            id = m.Id, name = m.Name, connector = m.Connector,
            x = m.X, y = m.Y, width = m.Width, height = m.Height,
            pixel_width = m.PixelWidth, pixel_height = m.PixelHeight,
            scale = m.Scale, rotation = m.Rotation, primary = m.Primary,
            width_mm = m.WidthMm, height_mm = m.HeightMm,
        }).ToList(),
    };
}
