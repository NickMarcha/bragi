using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Bragi.Client.Tray;

/// <summary>Presses and releases keys on this computer, by Linux evdev code.</summary>
public interface IKeyboard : IDisposable
{
    void Press(IReadOnlyList<ushort> codes);
}

/// <summary>
/// "Press keys on computers" from the Bragi host (app/actions.py): the host
/// sends press_keys with evdev codes, and this presses them here, all down
/// in order and then up in reverse, like a hotkey typed by hand. The KVM can
/// do the same, but Wayland clients drop its keys while the cursor is on
/// another computer (deskflow/deskflow#10393); this doesn't go through it.
/// Says it can, with a client_hello, after each connect. Runs on the
/// socket's thread.
/// </summary>
public sealed class KeyPresser : IDisposable
{
    public const ushort MaxKey = 0x2ff;
    private readonly Lazy<IKeyboard?> _keyboard;
    private bool _helloSent;

    public Func<object, Task<bool>>? Send { get; set; }
    public string? Problem { get; private set; }

    /// <param name="createKeyboard">Makes this computer's keyboard, or null where there's none.</param>
    public KeyPresser(Func<IKeyboard>? createKeyboard)
    {
        _keyboard = new Lazy<IKeyboard?>(() =>
        {
            if (createKeyboard is null)
            {
                return null;
            }
            try
            {
                return createKeyboard();
            }
            catch (Exception ex) when (ex is InvalidOperationException or DllNotFoundException or EntryPointNotFoundException)
            {
                Problem = ex.Message;
                return null;
            }
        });
        Supported = createKeyboard is not null;
    }

    public bool Supported { get; }

    public void OnMessage(JsonElement message)
    {
        if (!message.TryGetProperty("type", out var type))
        {
            return;
        }
        switch (type.GetString())
        {
            // The host's first message on a connect: say what this computer can do.
            case "kvm" when !_helloSent && Send is not null:
                _helloSent = true;
                // Made now, so the compositor has seen the device before the first press.
                var ready = Supported && _keyboard.Value is not null;
                _ = Send(new { type = "client_hello", features = ready ? new[] { "keys" } : Array.Empty<string>() });
                break;
            case "press_keys":
                if (Codes(message) is { Count: > 0 } codes && _keyboard.Value is { } keyboard)
                {
                    try
                    {
                        keyboard.Press(codes);
                    }
                    catch (InvalidOperationException ex)
                    {
                        Problem = ex.Message;
                    }
                }
                break;
        }
    }

    public void OnDisconnected() => _helloSent = false;

    /// <summary>The codes of a press_keys message: 1 to 8 keys, each a valid key code.</summary>
    public static IReadOnlyList<ushort>? Codes(JsonElement message)
    {
        if (!message.TryGetProperty("codes", out var list) || list.ValueKind != JsonValueKind.Array)
        {
            return null;
        }
        var codes = new List<ushort>();
        foreach (var c in list.EnumerateArray())
        {
            if (c.ValueKind != JsonValueKind.Number || !c.TryGetUInt16(out var code) || code is 0 or > MaxKey)
            {
                return null;
            }
            codes.Add(code);
        }
        return codes.Count is > 0 and <= 8 ? codes : null;
    }

    public void Dispose()
    {
        if (_keyboard.IsValueCreated)
        {
            _keyboard.Value?.Dispose();
        }
    }
}

/// <summary>
/// A virtual keyboard made with /dev/uinput (the same access the controller
/// passthrough uses). The compositor treats it like any plugged-in keyboard,
/// so global shortcuts such as Discord's mute see its keys.
/// </summary>
[SupportedOSPlatform("linux")]
public sealed unsafe class UinputKeyboard : IKeyboard
{
    private const int O_WRONLY = 1, O_NONBLOCK = 0x800;
    private const ushort EV_SYN = 0, EV_KEY = 1;
    private const nuint UI_SET_EVBIT = 0x40045564, UI_SET_KEYBIT = 0x40045565;
    private const nuint UI_DEV_SETUP = 0x405C5503, UI_DEV_CREATE = 0x5501, UI_DEV_DESTROY = 0x5502;

    [DllImport("libc", SetLastError = true)] private static extern int open(string path, int flags);
    [DllImport("libc", SetLastError = true)] private static extern int close(int fd);
    [DllImport("libc", SetLastError = true, EntryPoint = "ioctl")] private static extern int IoctlValue(int fd, nuint request, nint value);
    [DllImport("libc", SetLastError = true, EntryPoint = "ioctl")] private static extern int IoctlPointer(int fd, nuint request, byte* data);
    [DllImport("libc", SetLastError = true)] private static extern nint write(int fd, byte* buffer, nuint count);

    private readonly int _fd;
    private readonly object _lock = new();

    public UinputKeyboard(string name = "Bragi virtual keyboard")
    {
        _fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
        if (_fd < 0)
        {
            throw new InvalidOperationException($"Can't open /dev/uinput (errno {Marshal.GetLastPInvokeError()}).");
        }
        try
        {
            Check(IoctlValue(_fd, UI_SET_EVBIT, EV_KEY), "EV_KEY");
            // The keyboard keys (1-248); no mouse buttons, so it isn't taken for a pointer.
            for (ushort code = 1; code <= 248; code++)
            {
                Check(IoctlValue(_fd, UI_SET_KEYBIT, code), "key");
            }
            var setup = stackalloc byte[92];
            var span = new Span<byte>(setup, 92);
            span.Clear();
            BinaryPrimitives.WriteUInt16LittleEndian(span[0..], 0x06);   // BUS_VIRTUAL
            BinaryPrimitives.WriteUInt16LittleEndian(span[2..], 0x1209); // pid.codes vendor, like other virtual devices
            BinaryPrimitives.WriteUInt16LittleEndian(span[4..], 0xb4a6);
            BinaryPrimitives.WriteUInt16LittleEndian(span[6..], 1);
            Encoding.ASCII.GetBytes(name.Length > 79 ? name[..79] : name, span[8..88]);
            Check(IoctlPointer(_fd, UI_DEV_SETUP, setup), "setup");
            Check(IoctlValue(_fd, UI_DEV_CREATE, 0), "create");
        }
        catch
        {
            close(_fd);
            throw;
        }
        // The compositor needs a moment to open a new device; a key sent
        // right away would be lost.
        Thread.Sleep(300);
    }

    public void Press(IReadOnlyList<ushort> codes)
    {
        lock (_lock)
        {
            foreach (var code in codes)
            {
                Emit(code, 1);
            }
            // Long enough for apps that poll their shortcuts to see it held.
            Thread.Sleep(30);
            foreach (var code in codes.Reverse())
            {
                Emit(code, 0);
            }
        }
    }

    private void Emit(ushort code, int value)
    {
        var events = stackalloc byte[48];
        var span = new Span<byte>(events, 48);
        span.Clear();
        // struct input_event on 64-bit: timeval (16, the kernel stamps it), type, code, value; then a SYN.
        BinaryPrimitives.WriteUInt16LittleEndian(span[16..], EV_KEY);
        BinaryPrimitives.WriteUInt16LittleEndian(span[18..], code);
        BinaryPrimitives.WriteInt32LittleEndian(span[20..], value);
        BinaryPrimitives.WriteUInt16LittleEndian(span[40..], EV_SYN);
        if (write(_fd, events, 48) < 0)
        {
            throw new InvalidOperationException($"uinput write failed (errno {Marshal.GetLastPInvokeError()}).");
        }
    }

    private static void Check(int result, string step)
    {
        if (result < 0)
        {
            throw new InvalidOperationException($"uinput {step} failed (errno {Marshal.GetLastPInvokeError()}).");
        }
    }

    public void Dispose()
    {
        lock (_lock)
        {
            IoctlValue(_fd, UI_DEV_DESTROY, 0);
            close(_fd);
        }
    }
}
