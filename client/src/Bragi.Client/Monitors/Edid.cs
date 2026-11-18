using System;
using System.Text;

namespace Bragi.Client.Monitors;

/// <summary>
/// What a monitor says about itself in its EDID block: who made it, which
/// model, its serial and its size. The maker, product code and serial
/// together recognise the same monitor on any computer, so the TV is the
/// TV whichever computer its cable is in. Sizes are often missing or wrong
/// (TVs report an aspect ratio, some panels nonsense), so the
/// layout only takes them as a starting point.
/// </summary>
public sealed record Edid(string Maker, ushort Product, string Serial, string? Name, int? WidthMm, int? HeightMm)
{
    /// <summary>The same monitor on every computer: "GSM-5B09-123456".</summary>
    public string Id => $"{Maker}-{Product:X4}-{Serial}";

    /// <summary>The EDID's base block, or null if it isn't one (too short, bad header).</summary>
    public static Edid? Parse(ReadOnlySpan<byte> data)
    {
        ReadOnlySpan<byte> header = [0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00];
        if (data.Length < 128 || !data[..8].SequenceEqual(header))
        {
            return null;
        }
        // Three letters, 5 bits each, big-endian; 1 = 'A'.
        var packed = (data[8] << 8) | data[9];
        var maker = new string([(char)('@' + ((packed >> 10) & 0x1F)), (char)('@' + ((packed >> 5) & 0x1F)), (char)('@' + (packed & 0x1F))]);
        var product = (ushort)(data[10] | (data[11] << 8));
        var serialNumber = (uint)(data[12] | (data[13] << 8) | (data[14] << 16) | (data[15] << 24));

        string? name = null, serialText = null;
        int? widthMm = null, heightMm = null;
        for (var offset = 54; offset + 18 <= 126; offset += 18)
        {
            var block = data.Slice(offset, 18);
            if (block[0] != 0 || block[1] != 0)
            {
                // A detailed timing: its image size in mm is more precise than the cm below.
                var w = block[12] | ((block[14] & 0xF0) << 4);
                var h = block[13] | ((block[14] & 0x0F) << 8);
                if (widthMm is null && w > 0 && h > 0)
                {
                    (widthMm, heightMm) = (w, h);
                }
                continue;
            }
            switch (block[3])
            {
                case 0xFC:
                    name = Text(block[5..]);
                    break;
                case 0xFF:
                    serialText = Text(block[5..]);
                    break;
            }
        }
        // The base block's size in cm is the more trustworthy one: TVs put an
        // aspect ratio in the timing (16x9 as 160x90 mm on a 72" LG TV) and some
        // panels nonsense (698x392 mm on the Legion Go's 12x19 cm screen). The
        // timing's mm only refine it when the two agree.
        if (data[21] > 0 && data[22] > 0)
        {
            var (cmW, cmH) = (data[21] * 10, data[22] * 10);
            static bool Close(int a, int b) => Math.Abs(a - b) <= Math.Max(a, b) * 0.15;
            if (widthMm is not { } w || heightMm is not { } h || !Close(w, cmW) || !Close(h, cmH))
            {
                (widthMm, heightMm) = (cmW, cmH);
            }
        }
        var serial = !string.IsNullOrEmpty(serialText) ? serialText : serialNumber.ToString();
        return new Edid(maker, product, serial, string.IsNullOrEmpty(name) ? null : name, widthMm, heightMm);
    }

    /// <summary>A descriptor's text: up to 13 ASCII characters, ended by a newline.</summary>
    private static string Text(ReadOnlySpan<byte> bytes)
    {
        var end = bytes.IndexOf((byte)0x0A);
        var text = Encoding.ASCII.GetString(end >= 0 ? bytes[..end] : bytes);
        return text.Trim().Replace("\0", "");
    }
}
