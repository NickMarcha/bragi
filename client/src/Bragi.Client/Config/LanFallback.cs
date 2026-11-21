using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;

namespace Bragi.Client.Config;

/// <summary>
/// When to send the audio to the hub's LAN address instead of its Tailscale
/// one (bragi#81). The peer socket (over Tailscale) is the signal: once it
/// has been down for <see cref="After"/> and the hub's LAN address answers,
/// the audio goes there; when the socket connects again it goes back. The
/// hub makes the same switch on its side when its own Tailscale has no
/// address (app/lan_fallback.py). Pure logic, not thread-safe; the caller
/// locks. Tray/LanFallbackService.cs does the probing and switching.
/// </summary>
public sealed class LanFallback
{
    public static readonly TimeSpan After = TimeSpan.FromSeconds(20);

    private DateTime? _downSince;

    /// <summary>The LAN address the audio goes to now, or null for the configured one.</summary>
    public string? ActiveHost { get; private set; }

    /// <summary>The socket is connected (any message from the hub). True
    /// when this ends a LAN fallback: point the audio back.</summary>
    public bool Connected()
    {
        _downSince = null;
        if (ActiveHost is null)
        {
            return false;
        }
        ActiveHost = null;
        return true;
    }

    /// <summary>The socket closed or a connect attempt failed.</summary>
    public void Disconnected(DateTime now) => _downSince ??= now;

    /// <summary>Whether to check if the hub's LAN address answers now.</summary>
    public bool ShouldProbe(DateTime now, string? hubLan, bool linkEnabled) =>
        linkEnabled && ActiveHost is null && hubLan is not null && _downSince is { } since && now - since >= After;

    /// <summary>The answer to a probe. True when the audio should switch to hubLan now.</summary>
    public bool Probed(string hubLan, bool answered)
    {
        if (!answered || ActiveHost is not null || _downSince is null)
        {
            return false;
        }
        ActiveHost = hubLan;
        return true;
    }

    private static readonly (uint Network, uint Mask)[] Private =
    [
        (0x0A000000, 0xFF000000), // 10.0.0.0/8
        (0xAC100000, 0xFFF00000), // 172.16.0.0/12
        (0xC0A80000, 0xFFFF0000), // 192.168.0.0/16
    ];

    /// <summary>A private (RFC 1918) IPv4 address. Tailscale's 100.64.0.0/10 isn't one.</summary>
    public static bool IsLanAddress(IPAddress address)
    {
        if (address.AddressFamily != AddressFamily.InterNetwork)
        {
            return false;
        }
        var value = ToUInt(address);
        return Private.Any(p => (value & p.Mask) == p.Network);
    }

    /// <summary>This computer's LAN address to report to the hub: a private
    /// address on the same subnet as the hub's LAN address. That leaves out
    /// Tailscale, VPNs and virtual adapters (Docker, Hyper-V's Default
    /// Switch), and a computer on another network reports none.</summary>
    public static string? PickLanAddress(IEnumerable<(IPAddress Address, IPAddress Mask)> local, string? hubLan)
    {
        if (hubLan is null || !IPAddress.TryParse(hubLan, out var hub) || !IsLanAddress(hub))
        {
            return null;
        }
        var hubValue = ToUInt(hub);
        foreach (var (address, mask) in local)
        {
            if (!IsLanAddress(address) || mask.AddressFamily != AddressFamily.InterNetwork || address.Equals(hub))
            {
                continue;
            }
            var m = ToUInt(mask);
            if (m != 0 && (ToUInt(address) & m) == (hubValue & m))
            {
                return address.ToString();
            }
        }
        return null;
    }

    private static uint ToUInt(IPAddress address)
    {
        var b = address.GetAddressBytes();
        return (uint)(b[0] << 24 | b[1] << 16 | b[2] << 8 | b[3]);
    }
}
