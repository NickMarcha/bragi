using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Bragi.Client.Config;

namespace Bragi.Client.Tray;

/// <summary>
/// Sends the audio over the home network while the hub can't be reached
/// over Tailscale (bragi#81, decisions in Config/LanFallback.cs). Reports
/// this computer's LAN address to the hub after each "hub" message, so the
/// hub can do the same on its side, and remembers the hub's LAN address
/// (BRAGI_HUB_LAN_IP, written by HubSyncService). Every 5 s, once the peer
/// socket has been down for 20 s, it pings that address (ICMP: no root and
/// no Tailscale needed) and, if it answers, points the audio there; the
/// next message on the socket points it back. The configured address stays
/// the Tailscale one.
/// </summary>
public sealed class LanFallbackService : IDisposable
{
    private readonly LanFallback _state = new();
    private readonly object _lock = new();
    private readonly string _configuredHost;
    private readonly Timer _timer;
    private string? _hubLan;
    private int _probing;

    /// <summary>Sends a message on the peer socket.</summary>
    public Func<object, Task<bool>>? Send { get; init; }
    /// <summary>Whether the audio link is on; nothing switches while it's off.</summary>
    public Func<bool> LinkEnabled { get; init; } = () => true;
    /// <summary>Points the audio at this address (the hub's LAN one, or the configured one).</summary>
    public Func<string, Task>? PointAudioAt { get; init; }
    /// <summary>True when the audio went over to the LAN, false when it came back. Any thread.</summary>
    public event Action<bool>? Changed;

    public LanFallbackService(string configuredHost, string? rememberedHubLan)
    {
        _configuredHost = configuredHost;
        _hubLan = rememberedHubLan;
        _timer = new Timer(_ => _ = TickAsync(), null, TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(5));
    }

    public bool Active
    {
        get
        {
            lock (_lock)
            {
                return _state.ActiveHost is not null;
            }
        }
    }

    /// <summary>Any message from the hub: the socket is up. Runs on the socket's thread.</summary>
    public void OnMessage(JsonElement message)
    {
        bool back;
        lock (_lock)
        {
            back = _state.Connected();
        }
        if (back)
        {
            // The part before the first await runs here, so a Linux env file
            // is back to the Tailscale address before the "hub" message that
            // follows is compared with it.
            _ = PointAsync(_configuredHost, false);
        }
        if (HubSync.ParseHub(message) is { } hub)
        {
            lock (_lock)
            {
                _hubLan = hub.LanIp ?? _hubLan;
            }
            if (Send is not null)
            {
                _ = Send(new { type = "peer_lan", lan_ip = LanFallback.PickLanAddress(LocalAddresses(), hub.LanIp) });
            }
        }
    }

    public void OnDisconnected()
    {
        lock (_lock)
        {
            _state.Disconnected(DateTime.UtcNow);
        }
    }

    private async Task TickAsync()
    {
        string? hub;
        lock (_lock)
        {
            hub = _hubLan;
            if (!_state.ShouldProbe(DateTime.UtcNow, hub, LinkEnabled()))
            {
                return;
            }
        }
        if (Interlocked.Exchange(ref _probing, 1) == 1)
        {
            return;
        }
        try
        {
            var answered = await AnswersAsync(hub!);
            bool use;
            lock (_lock)
            {
                use = _state.Probed(hub!, answered);
            }
            if (use)
            {
                await PointAsync(hub!, true);
            }
        }
        finally
        {
            Interlocked.Exchange(ref _probing, 0);
        }
    }

    private async Task PointAsync(string host, bool lan)
    {
        try
        {
            if (PointAudioAt is not null)
            {
                await PointAudioAt(host);
            }
            Console.Error.WriteLine(lan
                ? $"Bragi: the hub isn't reachable over Tailscale; audio goes to its LAN address {host}"
                : $"Bragi: the hub is back; audio goes to {host}");
            Changed?.Invoke(lan);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            Console.Error.WriteLine($"Bragi LAN fallback: {ex.Message}");
        }
    }

    private static async Task<bool> AnswersAsync(string host)
    {
        try
        {
            using var ping = new Ping();
            var reply = await ping.SendPingAsync(IPAddress.Parse(host), 1000);
            return reply.Status == IPStatus.Success;
        }
        catch (Exception ex) when (ex is PingException or InvalidOperationException or FormatException)
        {
            return false;
        }
    }

    /// <summary>This computer's IPv4 addresses and masks, on interfaces that are up.</summary>
    private static List<(IPAddress Address, IPAddress Mask)> LocalAddresses()
    {
        try
        {
            return NetworkInterface.GetAllNetworkInterfaces()
                .Where(n => n.OperationalStatus == OperationalStatus.Up
                    && n.NetworkInterfaceType is not (NetworkInterfaceType.Loopback or NetworkInterfaceType.Tunnel))
                .SelectMany(n => n.GetIPProperties().UnicastAddresses)
                .Where(u => u.Address.AddressFamily == AddressFamily.InterNetwork)
                .Select(u => (u.Address, u.IPv4Mask))
                .ToList();
        }
        catch (NetworkInformationException)
        {
            return [];
        }
    }

    public void Dispose() => _timer.Dispose();
}
