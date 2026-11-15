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
/// The controllers paired with the Bragi host (app/gamepad.py). Turns the
/// server's messages on the peer socket into virtual controllers here, and
/// claims or releases them for this computer. Runs on the socket's thread;
/// StatusChanged is raised there too.
/// </summary>
public sealed class GamepadService : IDisposable
{
    private readonly string? _peerName;
    private readonly Func<IVirtualGamepad>? _create;
    private readonly Dictionary<string, IVirtualGamepad?> _pads = new();
    private readonly object _lock = new();

    public GamepadStatus Status { get; private set; } = GamepadStatus.None;
    public event Action<GamepadStatus>? StatusChanged;
    /// <summary>Sends a message on the peer socket; false if it isn't connected.</summary>
    public Func<object, Task<bool>>? Send { get; set; }
    /// <summary>Why a virtual controller couldn't be made, for the tray.</summary>
    public string? Problem { get; private set; }

    /// <param name="create">Makes a virtual controller, or null where this
    /// system has none yet (Windows, until ViGEm).</param>
    public GamepadService(string? peerName, Func<IVirtualGamepad>? create)
    {
        _peerName = peerName;
        _create = create;
    }

    public bool IsHere => Status.Target is not null && Status.Target == _peerName;

    public void OnMessage(JsonElement message)
    {
        if (!message.TryGetProperty("type", out var type))
        {
            return;
        }
        switch (type.GetString())
        {
            case "gamepads":
                var controllers = message.TryGetProperty("controllers", out var list) && list.ValueKind == JsonValueKind.Array
                    ? list.EnumerateArray().Select(c => c.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "").ToList()
                    : [];
                var target = message.TryGetProperty("target", out var t) && t.ValueKind == JsonValueKind.String ? t.GetString() : null;
                Status = new GamepadStatus(controllers, target, _create is not null);
                if (!IsHere)
                {
                    DetachAll();
                }
                StatusChanged?.Invoke(Status);
                break;
            case "gamepad":
                if (message.TryGetProperty("id", out var id) && id.GetString() is { } padId &&
                    GamepadState.FromMessage(message) is { } state)
                {
                    Pad(padId)?.Apply(state);
                }
                break;
            case "gamepad_detach":
                if (message.TryGetProperty("id", out var gone) && gone.GetString() is { } goneId)
                {
                    Detach(goneId);
                }
                break;
        }
    }

    /// <summary>The socket closed: the server can't send releases now, so let go here.</summary>
    public void OnDisconnected()
    {
        DetachAll();
        Status = GamepadStatus.None;
        StatusChanged?.Invoke(Status);
    }

    public Task<bool> ClaimAsync() => Send?.Invoke(new { type = "gamepad_claim" }) ?? Task.FromResult(false);
    public Task<bool> ReleaseAsync() => Send?.Invoke(new { type = "gamepad_release" }) ?? Task.FromResult(false);

    private IVirtualGamepad? Pad(string id)
    {
        lock (_lock)
        {
            if (_pads.TryGetValue(id, out var pad))
            {
                return pad;
            }
            if (_create is null)
            {
                return null;
            }
            try
            {
                pad = _create();
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
                pad?.Apply(GamepadState.Neutral);
                pad?.Dispose();
            }
        }
    }

    private void DetachAll()
    {
        lock (_lock)
        {
            foreach (var id in _pads.Keys.ToList())
            {
                Detach(id);
            }
        }
    }

    public void Dispose() => DetachAll();
}
