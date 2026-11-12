using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

namespace Bragi.Client.Volume;

public sealed record VolumeDirection(double? Volume, bool Muted, bool Connected)
{
    public static VolumeDirection Read(JsonElement value) => new(
        value.TryGetProperty("volume", out var volume) && volume.ValueKind == JsonValueKind.Number
            ? volume.GetDouble() : null,
        value.GetProperty("muted").GetBoolean(), value.GetProperty("connected").GetBoolean());
}

public enum DeviceStatus { Online, Partial, Offline, Disabled }

public sealed record VolumeDevice(string Target, string Key, string Label,
    VolumeDirection Output, VolumeDirection Input)
{
    // The server names peer directions from the Bragi host's perspective. The menu
    // names them from the peer's perspective: its output arrives at the host.
    public string OutputDirection => Target == "peer" ? "incoming" : "playback";
    public string InputDirection => Target == "peer" ? "outgoing" : "capture";

    /// <summary>The dashboard's line under the name, such as "Roc · 100.64.0.2".</summary>
    public string Meta { get; init; } = "";
    /// <summary>Whether the peer's tray or phone app is connected; null when Bragi doesn't track it.</summary>
    public bool? ClientConnected { get; init; }
    public bool Enabled { get; init; } = true;
    public bool Android { get; init; }
    public bool PhoneMicrophone { get; init; }

    // Strip names as the dashboard shows them.
    public string OutputLabel => Target == "headset" ? "Speakers" : PhoneMicrophone ? "Mic" : "Audio";
    public string InputLabel => Target == "peer" && Android ? "Headset" : "Mic";

    public DeviceStatus Status =>
        !Enabled ? DeviceStatus.Disabled
        : ClientConnected is { } client ? (client ? DeviceStatus.Online : DeviceStatus.Offline)
        : Output.Connected && Input.Connected ? DeviceStatus.Online
        : !Output.Connected && !Input.Connected ? DeviceStatus.Offline
        : DeviceStatus.Partial;

    /// <summary>Folded to its header at the end of the row, as on the dashboard: nothing to mix.</summary>
    public bool Parked => !Enabled || ClientConnected == false || (!Output.Connected && !Input.Connected);
}

/// <summary>The dashboard protocol as device volumes, shared by menu discovery and the volume window.</summary>
public sealed class VolumeState
{
    private readonly Dictionary<(string, string), VolumeDevice> _devices = new();
    private readonly Dictionary<string, int> _order = new();

    /// <summary>Devices in the dashboard's order, then by name.</summary>
    public IEnumerable<VolumeDevice> Devices => _devices.Values
        .OrderBy(d => _order.GetValueOrDefault($"{d.Target}:{d.Key}", int.MaxValue))
        .ThenBy(d => d.Label, StringComparer.OrdinalIgnoreCase);

    public VolumeDevice? Find(string target, string key) => _devices.GetValueOrDefault((target, key));

    public void Apply(JsonElement message)
    {
        switch (message.GetProperty("type").GetString())
        {
            case "state":
                _devices.Clear();
                _order.Clear();
                foreach (var peer in message.GetProperty("peers").EnumerateArray()) AddPeer(peer);
                foreach (var headset in message.GetProperty("headsets").EnumerateArray()) AddHeadset(headset);
                if (message.TryGetProperty("order_of", out var positions) && positions.ValueKind == JsonValueKind.Object)
                    foreach (var position in positions.EnumerateObject())
                        if (position.Value.TryGetInt32(out var index)) _order[position.Name] = index;
                break;
            case "order":
                _order.Clear();
                var i = 0;
                foreach (var id in message.GetProperty("order").EnumerateArray()) _order[id.GetString()!] = i++;
                break;
            case "headset":
                AddHeadset(message);
                break;
            case "peer_presence":
                var name = message.GetProperty("name").GetString()!;
                if (Find("peer", name) is { } present)
                    _devices[("peer", name)] = present with { ClientConnected = message.GetProperty("connected").GetBoolean() };
                break;
            case "control":
                var target = message.GetProperty("target").GetString()!;
                var key = message.GetProperty("key").GetString()!;
                if (Find(target, key) is not { } device) return;
                var direction = message.GetProperty("direction").GetString();
                var value = VolumeDirection.Read(message);
                if (direction == device.OutputDirection) device = device with { Output = value };
                else if (direction == device.InputDirection) device = device with { Input = value };
                _devices[(target, key)] = device;
                break;
        }
    }

    private void AddPeer(JsonElement peer)
    {
        var name = peer.GetProperty("name").GetString()!;
        var android = Text(peer, "client_kind") == "android";
        var protocol = Text(peer, "protocol") == "vban" ? "VBAN" : "Roc";
        var address = Text(peer, "tailscale_ip");
        _devices[("peer", name)] = new("peer", name, name,
            VolumeDirection.Read(peer.GetProperty("incoming")),
            VolumeDirection.Read(peer.GetProperty("outgoing")))
        {
            Meta = address is null ? (android ? "Android" : protocol) : $"{(android ? "Android" : protocol)} · {address}",
            ClientConnected = peer.TryGetProperty("client_connected", out var client) && client.ValueKind is JsonValueKind.True or JsonValueKind.False
                ? client.GetBoolean() : null,
            Android = android,
            PhoneMicrophone = android && Text(peer, "capture_mode") == "microphone",
        };
    }

    private void AddHeadset(JsonElement headset)
    {
        var key = headset.GetProperty("key").GetString()!;
        _devices[("headset", key)] = new("headset", key, headset.GetProperty("label").GetString()!,
            VolumeDirection.Read(headset.GetProperty("playback")),
            VolumeDirection.Read(headset.GetProperty("capture")))
        {
            Meta = "USB headset",
            Enabled = !headset.TryGetProperty("enabled", out var enabled) || enabled.ValueKind != JsonValueKind.False,
        };
    }

    private static string? Text(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
}
