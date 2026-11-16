using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;

namespace Bragi.Client.Gamepad;

/// <summary>What the tray shows: the controllers on the Bragi host and who has them.</summary>
public sealed record GamepadStatus(IReadOnlyList<string> Controllers, string? Target, bool HereSupported)
{
    public static readonly GamepadStatus None = new([], null, false);
}

/// <summary>
/// The controllers paired with the Bragi host (app/gamepad.py), played here.
/// Where it can (Linux, /dev/uhid) it recreates each controller exactly from
/// its own HID description ("hid" mode): extra buttons, sensors, rumble and
/// LEDs all work as on the host. Otherwise it feeds a virtual Xbox 360
/// controller with the host's Xbox layout ("xbox" mode). It tells the host
/// which it can do in a gamepad_hello after each connect. Runs on the
/// socket's thread; StatusChanged is raised there too.
/// </summary>
public sealed class GamepadService : IDisposable
{
    private readonly string? _peerName;
    private readonly Func<IVirtualGamepad>? _createPad;
    private readonly Func<HidDescription, IHidDevice>? _createHid;
    private readonly Dictionary<string, IVirtualGamepad?> _pads = new();
    private readonly Dictionary<string, IHidDevice> _hid = new();
    private readonly object _lock = new();
    private bool _helloSent;

    public GamepadStatus Status { get; private set; } = GamepadStatus.None;
    public event Action<GamepadStatus>? StatusChanged;
    /// <summary>Sends a message on the peer socket; false if it isn't connected.</summary>
    public Func<object, Task<bool>>? Send { get; set; }
    /// <summary>Why a controller couldn't be made here, for the tray.</summary>
    public string? Problem { get; private set; }

    /// <param name="createPad">Makes a virtual Xbox 360 controller, or null where this system has none.</param>
    /// <param name="createHid">Recreates a controller from its HID description, or null where this system can't.</param>
    public GamepadService(string? peerName, Func<IVirtualGamepad>? createPad, Func<HidDescription, IHidDevice>? createHid = null)
    {
        _peerName = peerName;
        _createPad = createPad;
        _createHid = createHid;
    }

    public bool IsHere => Status.Target is not null && Status.Target == _peerName;

    /// <summary>What this computer can play, best first.</summary>
    public IReadOnlyList<string> Modes =>
        new[] { _createHid is not null ? "hid" : null, _createPad is not null ? "xbox" : null }.OfType<string>().ToList();

    public void OnMessage(JsonElement message)
    {
        if (!message.TryGetProperty("type", out var type))
        {
            return;
        }
        var id = message.TryGetProperty("id", out var idProp) && idProp.ValueKind == JsonValueKind.String ? idProp.GetString() : null;
        switch (type.GetString())
        {
            case "gamepads":
                if (!_helloSent && Send is not null)
                {
                    // The first message after a connect: say what this computer can play.
                    _helloSent = true;
                    _ = Send(new { type = "gamepad_hello", modes = Modes });
                }
                var controllers = message.TryGetProperty("controllers", out var list) && list.ValueKind == JsonValueKind.Array
                    ? list.EnumerateArray().Select(c => c.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "").ToList()
                    : [];
                var target = message.TryGetProperty("target", out var t) && t.ValueKind == JsonValueKind.String ? t.GetString() : null;
                Status = new GamepadStatus(controllers, target, Modes.Count > 0);
                if (!IsHere)
                {
                    DetachAll();
                }
                StatusChanged?.Invoke(Status);
                break;
            case "gamepad" when id is not null:
                if (GamepadState.FromMessage(message) is { } state)
                {
                    Pad(id)?.Apply(state);
                }
                break;
            case "hid_create" when id is not null:
                CreateHid(id, message);
                break;
            case "hid_input" when id is not null:
                if (Bytes(message, "data") is { } report && Hid(id) is { } device)
                {
                    Guard(() => device.Input(report));
                }
                break;
            case "hid_report_reply" when id is not null:
                if (Hid(id) is { } target2 && message.TryGetProperty("req", out var req) && req.TryGetUInt32(out var request))
                {
                    var error = message.TryGetProperty("err", out var e) && e.TryGetUInt16(out var code) ? code : (ushort)0;
                    var kind = message.TryGetProperty("kind", out var k) ? k.GetString() : null;
                    Guard(() =>
                    {
                        if (kind == "hid_get_report") target2.ReplyGetReport(request, error, Bytes(message, "data") ?? []);
                        else target2.ReplySetReport(request, error);
                    });
                }
                break;
            case "gamepad_detach" when id is not null:
                Detach(id);
                break;
        }
    }

    /// <summary>The socket closed: the server can't send releases now, so let go here.</summary>
    public void OnDisconnected()
    {
        _helloSent = false;
        DetachAll();
        Status = GamepadStatus.None;
        StatusChanged?.Invoke(Status);
    }

    public Task<bool> ClaimAsync() => Send?.Invoke(new { type = "gamepad_claim" }) ?? Task.FromResult(false);
    public Task<bool> ReleaseAsync() => Send?.Invoke(new { type = "gamepad_release" }) ?? Task.FromResult(false);

    private void CreateHid(string id, JsonElement message)
    {
        if (_createHid is null)
        {
            return;
        }
        Detach(id);
        try
        {
            uint U(string name) => message.TryGetProperty(name, out var v) && v.TryGetUInt32(out var u) ? u : 0;
            var description = new HidDescription(
                message.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "",
                message.TryGetProperty("uniq", out var q) ? q.GetString() ?? "" : "",
                (ushort)U("bus"), U("vendor"), U("product"), U("version"), U("country"),
                Bytes(message, "descriptor") ?? []);
            var device = _createHid(description);
            device.Output += (data, rtype) => _ = Send?.Invoke(new { type = "hid_output", id, rtype, data = Convert.ToBase64String(data) });
            device.GetReport += (request, rnum, rtype) => _ = Send?.Invoke(new { type = "hid_get_report", id, req = request, rnum, rtype });
            device.SetReport += (request, rtype, data) => _ = Send?.Invoke(new { type = "hid_set_report", id, req = request, rtype, data = Convert.ToBase64String(data) });
            lock (_lock)
            {
                _hid[id] = device;
            }
            Problem = null;
        }
        catch (Exception ex) when (ex is InvalidOperationException or DllNotFoundException or EntryPointNotFoundException)
        {
            Problem = ex.Message;
            StatusChanged?.Invoke(Status);
        }
    }

    private IHidDevice? Hid(string id)
    {
        lock (_lock)
        {
            return _hid.GetValueOrDefault(id);
        }
    }

    private IVirtualGamepad? Pad(string id)
    {
        lock (_lock)
        {
            if (_pads.TryGetValue(id, out var pad))
            {
                return pad;
            }
            if (_createPad is null)
            {
                return null;
            }
            try
            {
                pad = _createPad();
                // A game's rumble on the virtual pad, played on the real controller.
                pad.Rumble += r => _ = Send?.Invoke(new { type = "gamepad_rumble", id, strong = r.Strong, weak = r.Weak, ms = r.Ms });
                Problem = null;
            }
            catch (Exception ex) when (ex is InvalidOperationException or DllNotFoundException or EntryPointNotFoundException)
            {
                // Remembered as null so a missing permission isn't retried 60 times a second.
                pad = null;
                Problem = ex.Message;
                StatusChanged?.Invoke(Status);
            }
            _pads[id] = pad;
            return pad;
        }
    }

    private void Detach(string id)
    {
        lock (_lock)
        {
            if (_pads.Remove(id, out var pad))
            {
                Guard(() => pad?.Apply(GamepadState.Neutral));
                Guard(() => pad?.Dispose());
            }
            if (_hid.Remove(id, out var device))
            {
                Guard(device.Dispose);
            }
        }
    }

    private void DetachAll()
    {
        lock (_lock)
        {
            foreach (var id in _pads.Keys.Concat(_hid.Keys).Distinct().ToList())
            {
                Detach(id);
            }
        }
    }

    private static byte[]? Bytes(JsonElement message, string name)
    {
        try
        {
            return message.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? Convert.FromBase64String(v.GetString()!) : null;
        }
        catch (FormatException)
        {
            return null;
        }
    }

    /// <summary>A failing device write must not take the socket down with it.</summary>
    private static void Guard(Action action)
    {
        try
        {
            action();
        }
        catch (InvalidOperationException)
        {
        }
    }

    public void Dispose() => DetachAll();
}
