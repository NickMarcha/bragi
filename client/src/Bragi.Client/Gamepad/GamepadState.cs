using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

namespace Bragi.Client.Gamepad;

/// <summary>
/// One controller's state as the Bragi server sends it (app/gamepad.py): an
/// Xbox layout in xpad's convention. Sticks run -32768..32767 with Y
/// pointing down, triggers 0..255, the d-pad -1/0/1.
/// </summary>
public sealed record GamepadState(
    IReadOnlySet<string> Buttons,
    int Lx, int Ly, int Rx, int Ry, int Lt, int Rt, int Dx, int Dy)
{
    public static readonly GamepadState Neutral = new(new HashSet<string>(), 0, 0, 0, 0, 0, 0, 0, 0);

    /// <summary>The state in a "gamepad" message, or null if it isn't one.</summary>
    public static GamepadState? FromMessage(JsonElement message)
    {
        try
        {
            int Axis(string name, int low, int high) =>
                Math.Clamp(message.TryGetProperty(name, out var v) && v.TryGetInt32(out var i) ? i : 0, low, high);

            var buttons = message.TryGetProperty("buttons", out var list) && list.ValueKind == JsonValueKind.Array
                ? list.EnumerateArray().Where(b => b.ValueKind == JsonValueKind.String).Select(b => b.GetString()!).ToHashSet()
                : new HashSet<string>();
            return new GamepadState(buttons,
                Axis("lx", -32768, 32767), Axis("ly", -32768, 32767), Axis("rx", -32768, 32767), Axis("ry", -32768, 32767),
                Axis("lt", 0, 255), Axis("rt", 0, 255), Axis("dx", -1, 1), Axis("dy", -1, 1));
        }
        catch (InvalidOperationException)
        {
            return null;
        }
    }
}

/// <summary>A controller this computer shows to games.</summary>
public interface IVirtualGamepad : IDisposable
{
    void Apply(GamepadState state);
}
