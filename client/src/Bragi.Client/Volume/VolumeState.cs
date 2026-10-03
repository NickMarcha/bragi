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

public sealed record VolumeDevice(string Target, string Key, string Label,
    VolumeDirection Output, VolumeDirection Input)
{
    // The server names peer directions from sagepi's perspective. The menu
    // names them from the peer's perspective: its output arrives at sagepi.
    public string OutputDirection => Target == "peer" ? "incoming" : "playback";
    public string InputDirection => Target == "peer" ? "outgoing" : "capture";
}

/// <summary>The dashboard protocol as device volumes, shared by menu discovery and the volume window.</summary>
public sealed class VolumeState
{
    private readonly Dictionary<(string, string), VolumeDevice> _devices = new();
    public IEnumerable<VolumeDevice> Devices => _devices.Values.OrderBy(d => d.Label, StringComparer.OrdinalIgnoreCase);
    public VolumeDevice? Find(string target, string key) => _devices.GetValueOrDefault((target, key));

    public void Apply(JsonElement message)
    {
        switch (message.GetProperty("type").GetString())
        {
            case "state":
                _devices.Clear();
                foreach (var peer in message.GetProperty("peers").EnumerateArray()) AddPeer(peer);
                foreach (var headset in message.GetProperty("headsets").EnumerateArray()) AddHeadset(headset);
                break;
            case "headset":
                AddHeadset(message);
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
        _devices[("peer", name)] = new("peer", name, name,
            VolumeDirection.Read(peer.GetProperty("incoming")),
            VolumeDirection.Read(peer.GetProperty("outgoing")));
    }

    private void AddHeadset(JsonElement headset)
    {
        var key = headset.GetProperty("key").GetString()!;
        _devices[("headset", key)] = new("headset", key, headset.GetProperty("label").GetString()!,
            VolumeDirection.Read(headset.GetProperty("playback")),
            VolumeDirection.Read(headset.GetProperty("capture")));
    }
}
