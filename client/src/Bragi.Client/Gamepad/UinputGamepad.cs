using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;
using System.Threading;

namespace Bragi.Client.Gamepad;

/// <summary>
/// A virtual Xbox 360 controller made with /dev/uinput. It uses the Xbox
/// 360 pad's USB ids and xpad's axes, so SDL, Steam and games treat it like
/// one plugged in. Needs write access to /dev/uinput, which Steam's udev
/// rules (60-steam-input.rules) give the logged-in user.
/// It also says it can rumble, as xpad does: a reader thread answers the
/// kernel's effect uploads and turns each play into a Rumble event, which
/// the host plays on the real controller.
/// </summary>
[SupportedOSPlatform("linux")]
public sealed unsafe class UinputGamepad : IVirtualGamepad
{
    // O_CLOEXEC: the updater and the next version are started from this
    // process, and an inherited uinput fd keeps the old virtual device alive.
    private const int O_RDWR = 2, O_NONBLOCK = 0x800, O_CLOEXEC = 0x80000;
    private const ushort EV_SYN = 0, EV_KEY = 1, EV_ABS = 3, EV_FF = 0x15, EV_UINPUT = 0x0101;
    private const ushort FF_RUMBLE = 0x50, FF_GAIN = 0x60, UI_FF_UPLOAD = 1, UI_FF_ERASE = 2;
    private const int EffectsMax = 16;
    private const ushort ABS_X = 0, ABS_Y = 1, ABS_Z = 2, ABS_RX = 3, ABS_RY = 4, ABS_RZ = 5, ABS_HAT0X = 16, ABS_HAT0Y = 17;

    // _IOW('U', n, size) and _IO('U', n) from linux/uinput.h.
    private const nuint UI_SET_EVBIT = 0x40045564, UI_SET_KEYBIT = 0x40045565, UI_SET_ABSBIT = 0x40045567;
    private const nuint UI_DEV_SETUP = 0x405C5503, UI_ABS_SETUP = 0x401C5504, UI_DEV_CREATE = 0x5501, UI_DEV_DESTROY = 0x5502;
    private const nuint UI_SET_FFBIT = 0x4004556B;
    // struct uinput_ff_upload (request_id, retval, two struct ff_effect of 48 bytes) is 104 bytes,
    // struct uinput_ff_erase (request_id, retval, effect_id) 12.
    private const nuint UI_BEGIN_FF_UPLOAD = 0xC06855C8, UI_END_FF_UPLOAD = 0x406855C9;
    private const nuint UI_BEGIN_FF_ERASE = 0xC00C55CA, UI_END_FF_ERASE = 0x400C55CB;

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
    [DllImport("libc", SetLastError = true)] private static extern nint read(int fd, byte* buffer, nuint count);
    [DllImport("libc", SetLastError = true)] private static extern int poll(PollFd* fds, nuint count, int timeoutMs);

    [StructLayout(LayoutKind.Sequential)]
    private struct PollFd
    {
        public int Fd;
        public short Events;
        public short Revents;
    }

    private readonly int _fd;
    private readonly byte[] _events = new byte[24 * 24];
    private readonly RumbleEffects _effects = new();
    private readonly Thread _reader;
    private volatile bool _closed;

    public event Action<Rumble>? Rumble;

    public UinputGamepad(string name = "Bragi virtual Xbox 360 controller")
    {
        _fd = open("/dev/uinput", O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (_fd < 0)
        {
            throw new InvalidOperationException($"Can't open /dev/uinput (errno {Marshal.GetLastPInvokeError()}). Is Steam's udev rule installed?");
        }
        try
        {
            Check(IoctlValue(_fd, UI_SET_EVBIT, EV_KEY), "EV_KEY");
            Check(IoctlValue(_fd, UI_SET_EVBIT, EV_ABS), "EV_ABS");
            Check(IoctlValue(_fd, UI_SET_EVBIT, EV_FF), "EV_FF");
            Check(IoctlValue(_fd, UI_SET_FFBIT, FF_RUMBLE), "FF_RUMBLE");
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
            BinaryPrimitives.WriteUInt32LittleEndian(span[88..], EffectsMax);
            Check(IoctlPointer(_fd, UI_DEV_SETUP, setup), "setup");
            Check(IoctlValue(_fd, UI_DEV_CREATE, 0), "create");
        }
        catch
        {
            close(_fd);
            throw;
        }
        _reader = new Thread(ReadLoop) { IsBackground = true, Name = "uinput rumble" };
        _reader.Start();
    }

    /// <summary>Answers effect uploads and erases, and reports plays.</summary>
    private void ReadLoop()
    {
        var e = new byte[24];
        try
        {
            while (!_closed)
            {
                // A short poll, so Dispose doesn't leave this thread blocked.
                var pfd = new PollFd { Fd = _fd, Events = 1 };
                if (poll(&pfd, 1, 250) <= 0 || (pfd.Revents & 1) == 0)
                {
                    continue;
                }
                nint n;
                fixed (byte* p = e)
                {
                    n = read(_fd, p, 24);
                }
                if (n != 24)
                {
                    continue;
                }
                var type = BinaryPrimitives.ReadUInt16LittleEndian(e.AsSpan(16));
                var code = BinaryPrimitives.ReadUInt16LittleEndian(e.AsSpan(18));
                var value = BinaryPrimitives.ReadInt32LittleEndian(e.AsSpan(20));
                try
                {
                    if (type == EV_UINPUT && code == UI_FF_UPLOAD)
                    {
                        Upload((uint)value);
                    }
                    else if (type == EV_UINPUT && code == UI_FF_ERASE)
                    {
                        Erase((uint)value);
                    }
                    else if (type == EV_FF && _effects.Play(code, value) is { } rumble)
                    {
                        Rumble?.Invoke(rumble);
                    }
                }
                catch (Exception)
                {
                    // A handler failing must not stop the pad.
                }
            }
        }
        finally
        {
            // The reader owns the fd from here, so it never touches a reused number.
            close(_fd);
        }
    }

    private void Upload(uint request)
    {
        var upload = stackalloc byte[104];
        var span = new Span<byte>(upload, 104);
        span.Clear();
        BinaryPrimitives.WriteUInt32LittleEndian(span, request);
        if (IoctlPointer(_fd, UI_BEGIN_FF_UPLOAD, upload) < 0)
        {
            return;
        }
        // retval: 0, or -EINVAL for anything but a rumble (xpad does the same).
        var ok = _effects.Upload(span.Slice(8, 48));
        BinaryPrimitives.WriteInt32LittleEndian(span[4..], ok ? 0 : -22);
        IoctlPointer(_fd, UI_END_FF_UPLOAD, upload);
    }

    private void Erase(uint request)
    {
        var erase = stackalloc byte[12];
        var span = new Span<byte>(erase, 12);
        span.Clear();
        BinaryPrimitives.WriteUInt32LittleEndian(span, request);
        if (IoctlPointer(_fd, UI_BEGIN_FF_ERASE, erase) < 0)
        {
            return;
        }
        if (_effects.Erase((int)BinaryPrimitives.ReadUInt32LittleEndian(span[8..])))
        {
            Rumble?.Invoke(Gamepad.Rumble.Stop);
        }
        BinaryPrimitives.WriteInt32LittleEndian(span[4..], 0);
        IoctlPointer(_fd, UI_END_FF_ERASE, erase);
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
        if (_closed)
        {
            return;
        }
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
        if (_closed)
        {
            return;
        }
        IoctlValue(_fd, UI_DEV_DESTROY, 0);
        _closed = true; // the reader closes the fd on its way out
        if (_effects.Playing)
        {
            Rumble?.Invoke(Gamepad.Rumble.Stop);
        }
    }
}

/// <summary>
/// The rumble effects a game uploaded to the virtual pad, by slot, and what
/// playing one means. Kept apart from the uinput plumbing so it can be tested.
/// </summary>
public sealed class RumbleEffects
{
    private const ushort FF_RUMBLE = 0x50, FF_GAIN = 0x60;
    private readonly Dictionary<int, (ushort Strong, ushort Weak, ushort Length)> _slots = new();
    private readonly object _lock = new();
    private int _gain = 0xFFFF;
    private int? _playing;

    public bool Playing
    {
        get { lock (_lock) { return _playing is not null; } }
    }

    /// <summary>An uploaded struct ff_effect (48 bytes); false if it isn't a rumble.</summary>
    public bool Upload(ReadOnlySpan<byte> effect)
    {
        if (BinaryPrimitives.ReadUInt16LittleEndian(effect) != FF_RUMBLE)
        {
            return false;
        }
        var id = BinaryPrimitives.ReadInt16LittleEndian(effect[2..]);
        var length = BinaryPrimitives.ReadUInt16LittleEndian(effect[10..]);
        lock (_lock)
        {
            _slots[id] = (BinaryPrimitives.ReadUInt16LittleEndian(effect[16..]),
                          BinaryPrimitives.ReadUInt16LittleEndian(effect[18..]), length);
        }
        return true;
    }

    /// <summary>Forgets a slot; true if it was the one playing (so stop it).</summary>
    public bool Erase(int id)
    {
        lock (_lock)
        {
            _slots.Remove(id);
            if (_playing != id)
            {
                return false;
            }
            _playing = null;
            return true;
        }
    }

    /// <summary>An EV_FF event: code is a slot (or FF_GAIN), value the repeat count, 0 to stop.</summary>
    public Rumble? Play(ushort code, int value)
    {
        lock (_lock)
        {
            if (code == FF_GAIN)
            {
                _gain = Math.Clamp(value, 0, 0xFFFF);
                return null;
            }
            if (value <= 0)
            {
                if (_playing != code)
                {
                    return null;
                }
                _playing = null;
                return Rumble.Stop;
            }
            if (!_slots.TryGetValue(code, out var slot))
            {
                return null;
            }
            _playing = code;
            ushort Scale(ushort magnitude) => (ushort)(magnitude * _gain / 0xFFFF);
            // A length of 0 plays until stopped; repeats play it back to back.
            var ms = slot.Length == 0 ? 0 : Math.Min(slot.Length * value, 65535);
            return new Rumble(Scale(slot.Strong), Scale(slot.Weak), ms);
        }
    }
}
