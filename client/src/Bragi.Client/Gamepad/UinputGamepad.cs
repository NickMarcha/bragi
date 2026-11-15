using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;

namespace Bragi.Client.Gamepad;

/// <summary>
/// A virtual Xbox 360 controller made with /dev/uinput. It uses the Xbox
/// 360 pad's USB ids and xpad's axes, so SDL, Steam and games treat it like
/// one plugged in. Needs write access to /dev/uinput, which Steam's udev
/// rules (60-steam-input.rules) give the logged-in user.
/// </summary>
[SupportedOSPlatform("linux")]
public sealed unsafe class UinputGamepad : IVirtualGamepad
{
    private const int O_WRONLY = 1, O_NONBLOCK = 0x800;
    private const ushort EV_SYN = 0, EV_KEY = 1, EV_ABS = 3;
    private const ushort ABS_X = 0, ABS_Y = 1, ABS_Z = 2, ABS_RX = 3, ABS_RY = 4, ABS_RZ = 5, ABS_HAT0X = 16, ABS_HAT0Y = 17;

    // _IOW('U', n, size) and _IO('U', n) from linux/uinput.h.
    private const nuint UI_SET_EVBIT = 0x40045564, UI_SET_KEYBIT = 0x40045565, UI_SET_ABSBIT = 0x40045567;
    private const nuint UI_DEV_SETUP = 0x405C5503, UI_ABS_SETUP = 0x401C5504, UI_DEV_CREATE = 0x5501, UI_DEV_DESTROY = 0x5502;

    /// <summary>The wire's button names and their Xbox 360 codes under xpad.</summary>
    public static readonly IReadOnlyDictionary<string, ushort> ButtonCodes = new Dictionary<string, ushort>
    {
        ["a"] = 304, ["b"] = 305, ["x"] = 307, ["y"] = 308, ["lb"] = 310, ["rb"] = 311,
        ["back"] = 314, ["start"] = 315, ["guide"] = 316, ["ls"] = 317, ["rs"] = 318,
    };

    [DllImport("libc", SetLastError = true)] private static extern int open(string path, int flags);
    [DllImport("libc", SetLastError = true)] private static extern int close(int fd);
    [DllImport("libc", SetLastError = true, EntryPoint = "ioctl")] private static extern int IoctlValue(int fd, nuint request, nint value);
    [DllImport("libc", SetLastError = true, EntryPoint = "ioctl")] private static extern int IoctlPointer(int fd, nuint request, byte* data);
    [DllImport("libc", SetLastError = true)] private static extern nint write(int fd, byte* buffer, nuint count);

    private readonly int _fd;
    private readonly byte[] _events = new byte[24 * 24];

    public UinputGamepad(string name = "Bragi virtual Xbox 360 controller")
    {
        _fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
        if (_fd < 0)
        {
            throw new InvalidOperationException($"Can't open /dev/uinput (errno {Marshal.GetLastPInvokeError()}). Is Steam's udev rule installed?");
        }
        try
        {
            Check(IoctlValue(_fd, UI_SET_EVBIT, EV_KEY), "EV_KEY");
            Check(IoctlValue(_fd, UI_SET_EVBIT, EV_ABS), "EV_ABS");
            foreach (var code in ButtonCodes.Values)
            {
                Check(IoctlValue(_fd, UI_SET_KEYBIT, code), "key");
            }
            foreach (var (code, low, high, fuzz, flat) in new (ushort, int, int, int, int)[]
            {
                (ABS_X, -32768, 32767, 16, 128), (ABS_Y, -32768, 32767, 16, 128),
                (ABS_RX, -32768, 32767, 16, 128), (ABS_RY, -32768, 32767, 16, 128),
                (ABS_Z, 0, 255, 0, 0), (ABS_RZ, 0, 255, 0, 0),
                (ABS_HAT0X, -1, 1, 0, 0), (ABS_HAT0Y, -1, 1, 0, 0),
            })
            {
                Check(IoctlValue(_fd, UI_SET_ABSBIT, code), "abs");
                // struct uinput_abs_setup: __u16 code, padding, struct input_absinfo (6 x __s32).
                var abs = stackalloc byte[28];
                new Span<byte>(abs, 28).Clear();
                BinaryPrimitives.WriteUInt16LittleEndian(new Span<byte>(abs, 2), code);
                BinaryPrimitives.WriteInt32LittleEndian(new Span<byte>(abs + 8, 4), low);
                BinaryPrimitives.WriteInt32LittleEndian(new Span<byte>(abs + 12, 4), high);
                BinaryPrimitives.WriteInt32LittleEndian(new Span<byte>(abs + 16, 4), fuzz);
                BinaryPrimitives.WriteInt32LittleEndian(new Span<byte>(abs + 20, 4), flat);
                Check(IoctlPointer(_fd, UI_ABS_SETUP, abs), "abs setup");
            }

            // struct uinput_setup: struct input_id (bustype, vendor, product, version), char name[80], __u32 ff_effects_max.
            var setup = stackalloc byte[92];
            var span = new Span<byte>(setup, 92);
            span.Clear();
            BinaryPrimitives.WriteUInt16LittleEndian(span[0..], 0x03);    // BUS_USB
            BinaryPrimitives.WriteUInt16LittleEndian(span[2..], 0x045e);  // Microsoft
            BinaryPrimitives.WriteUInt16LittleEndian(span[4..], 0x028e);  // Xbox 360 controller
            BinaryPrimitives.WriteUInt16LittleEndian(span[6..], 0x0114);
            Encoding.ASCII.GetBytes(name.Length > 79 ? name[..79] : name, span[8..88]);
            Check(IoctlPointer(_fd, UI_DEV_SETUP, setup), "setup");
            Check(IoctlValue(_fd, UI_DEV_CREATE, 0), "create");
        }
        catch
        {
            close(_fd);
            throw;
        }
    }

    public void Apply(GamepadState state)
    {
        var count = 0;
        void Add(ushort type, ushort code, int value)
        {
            // struct input_event on 64-bit: struct timeval (16 bytes, zero: the kernel stamps it), type, code, value.
            var e = _events.AsSpan(count++ * 24, 24);
            e[..16].Clear();
            BinaryPrimitives.WriteUInt16LittleEndian(e[16..], type);
            BinaryPrimitives.WriteUInt16LittleEndian(e[18..], code);
            BinaryPrimitives.WriteInt32LittleEndian(e[20..], value);
        }

        foreach (var (name, code) in ButtonCodes)
        {
            Add(EV_KEY, code, state.Buttons.Contains(name) ? 1 : 0);
        }
        Add(EV_ABS, ABS_X, state.Lx);
        Add(EV_ABS, ABS_Y, state.Ly);
        Add(EV_ABS, ABS_RX, state.Rx);
        Add(EV_ABS, ABS_RY, state.Ry);
        Add(EV_ABS, ABS_Z, state.Lt);
        Add(EV_ABS, ABS_RZ, state.Rt);
        Add(EV_ABS, ABS_HAT0X, state.Dx);
        Add(EV_ABS, ABS_HAT0Y, state.Dy);
        Add(EV_SYN, 0, 0);
        // The kernel drops repeats of an unchanged key or axis, so sending the whole state is cheap.
        fixed (byte* buffer = _events)
        {
            write(_fd, buffer, (nuint)(count * 24));
        }
    }

    private void Check(int result, string step)
    {
        if (result < 0)
        {
            throw new InvalidOperationException($"uinput {step} failed (errno {Marshal.GetLastPInvokeError()}).");
        }
    }

    public void Dispose()
    {
        IoctlValue(_fd, UI_DEV_DESTROY, 0);
        close(_fd);
    }
}
