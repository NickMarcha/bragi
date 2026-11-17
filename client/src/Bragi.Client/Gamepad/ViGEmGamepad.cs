using System;
using System.Runtime.Versioning;
using Nefarius.ViGEm.Client;
using Nefarius.ViGEm.Client.Exceptions;
using Nefarius.ViGEm.Client.Targets;
using Nefarius.ViGEm.Client.Targets.Xbox360;

namespace Bragi.Client.Gamepad;

/// <summary>
/// A virtual Xbox 360 controller on Windows, made with the ViGEmBus driver
/// (Nefarius). The driver is installed separately; Steam, DS4Windows and
/// Sunshine/Apollo installers often bring it along. Games see an ordinary
/// XInput pad. Their rumble comes back through the driver and is sent to the
/// host, which plays it on the real controller.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class ViGEmGamepad : IVirtualGamepad
{
    private static readonly object ClientLock = new();
    private static ViGEmClient? _client;

    private readonly IXbox360Controller _pad;
    private (byte Large, byte Small) _lastRumble;
    private bool _disposed;

    public event Action<Rumble>? Rumble;

    public ViGEmGamepad()
    {
        try
        {
            _pad = Client().CreateXbox360Controller();
            _pad.AutoSubmitReport = false;
            _pad.FeedbackReceived += OnFeedback;
            _pad.Connect();
        }
        catch (VigemBusNotFoundException)
        {
            throw new InvalidOperationException("install the ViGEmBus driver");
        }
        catch (Exception ex) when (ex is VigemBusVersionMismatchException or VigemBusAccessFailedException
                                       or VigemNoFreeSlotException or VigemTargetUninitializedException)
        {
            throw new InvalidOperationException($"ViGEmBus: {ex.Message}");
        }
    }

    /// <summary>One bus connection per process, opened on first use and retried if the driver was missing.</summary>
    private static ViGEmClient Client()
    {
        lock (ClientLock)
        {
            return _client ??= new ViGEmClient();
        }
    }

    public void Apply(GamepadState state)
    {
        if (_disposed)
        {
            return;
        }
        var report = XusbReport.From(state);
        _pad.SetButtonsFull(report.Buttons);
        _pad.SetAxisValue(Xbox360Axis.LeftThumbX, report.Lx);
        _pad.SetAxisValue(Xbox360Axis.LeftThumbY, report.Ly);
        _pad.SetAxisValue(Xbox360Axis.RightThumbX, report.Rx);
        _pad.SetAxisValue(Xbox360Axis.RightThumbY, report.Ry);
        _pad.SetSliderValue(Xbox360Slider.LeftTrigger, report.Lt);
        _pad.SetSliderValue(Xbox360Slider.RightTrigger, report.Rt);
        _pad.SubmitReport();
    }

    private void OnFeedback(object sender, Xbox360FeedbackReceivedEventArgs e)
    {
        // Games repeat XInputSetState with the same values every frame; only changes go to the host.
        if ((e.LargeMotor, e.SmallMotor) == _lastRumble)
        {
            return;
        }
        _lastRumble = (e.LargeMotor, e.SmallMotor);
        Rumble?.Invoke(XusbReport.RumbleFrom(e.LargeMotor, e.SmallMotor));
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _pad.FeedbackReceived -= OnFeedback;
        try
        {
            _pad.Disconnect();
        }
        catch (Exception ex) when (ex is VigemTargetNotPluggedInException or VigemTargetUninitializedException)
        {
        }
    }
}

/// <summary>
/// The XUSB report (what XInput reads) for a state in xpad's convention.
/// Kept apart from ViGEm so it can be tested anywhere.
/// </summary>
public readonly record struct XusbReport(ushort Buttons, short Lx, short Ly, short Rx, short Ry, byte Lt, byte Rt)
{
    // XUSB_BUTTON bits from ViGEm's / XInput's headers.
    public const ushort Up = 0x0001, Down = 0x0002, Left = 0x0004, Right = 0x0008;
    public const ushort Start = 0x0010, Back = 0x0020, LeftThumb = 0x0040, RightThumb = 0x0080;
    public const ushort LeftShoulder = 0x0100, RightShoulder = 0x0200, Guide = 0x0400;
    public const ushort A = 0x1000, B = 0x2000, X = 0x4000, Y = 0x8000;

    private static ushort Bit(string button) => button switch
    {
        "a" => A, "b" => B, "x" => X, "y" => Y, "lb" => LeftShoulder, "rb" => RightShoulder,
        "back" => Back, "start" => Start, "guide" => Guide, "ls" => LeftThumb, "rs" => RightThumb,
        _ => 0,
    };

    public static XusbReport From(GamepadState state)
    {
        ushort buttons = 0;
        foreach (var name in state.Buttons)
        {
            buttons |= Bit(name);
        }
        // The d-pad arrives hat-style, -1 being up or left.
        if (state.Dy < 0) buttons |= Up;
        if (state.Dy > 0) buttons |= Down;
        if (state.Dx < 0) buttons |= Left;
        if (state.Dx > 0) buttons |= Right;
        // xpad's Y points down, XInput's up.
        static short Flip(int y) => (short)Math.Clamp(-y, short.MinValue, short.MaxValue);
        static short Axis(int v) => (short)Math.Clamp(v, short.MinValue, short.MaxValue);
        return new XusbReport(buttons, Axis(state.Lx), Flip(state.Ly), Axis(state.Rx), Flip(state.Ry),
            (byte)Math.Clamp(state.Lt, 0, 255), (byte)Math.Clamp(state.Rt, 0, 255));
    }

    /// <summary>XInput's motors (0..255) as a rumble that lasts until the next change.</summary>
    public static Rumble RumbleFrom(byte large, byte small) =>
        large == 0 && small == 0 ? Gamepad.Rumble.Stop : new Rumble((ushort)(large * 257), (ushort)(small * 257), 0);
}
