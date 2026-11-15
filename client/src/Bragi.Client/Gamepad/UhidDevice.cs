using System;
using System.Buffers.Binary;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;
using System.Threading;

namespace Bragi.Client.Gamepad;

/// <summary>What the Bragi host sends to recreate a controller (app/gamepad.py, hid_info).</summary>
public sealed record HidDescription(
    string Name, string Uniq, ushort Bus, uint Vendor, uint Product, uint Version, uint Country, byte[] Descriptor);

/// <summary>A controller recreated here from its own HID description.</summary>
public interface IHidDevice : IDisposable
{
    /// <summary>The game or driver sent an output report (rumble, LEDs). Data, report type.</summary>
    event Action<byte[], byte>? Output;
    /// <summary>The driver asks for a report. Request id, report number, report type.</summary>
    event Action<uint, byte, byte>? GetReport;
    /// <summary>The driver sets a report. Request id, report type, data (report number first).</summary>
    event Action<uint, byte, byte[]>? SetReport;

    void Input(byte[] report);
    void ReplyGetReport(uint request, ushort error, byte[] data);
    void ReplySetReport(uint request, ushort error);
}

/// <summary>
/// A HID device made with /dev/uhid from another machine's controller: same
/// report descriptor, ids, name and bus, so the kernel binds the same driver
/// it does there. Input reports go in; output reports and the driver's
/// GET/SET_REPORT requests come out as events, answered with the Reply
/// methods. Needs write access to /dev/uhid (Steam's or Sunshine's udev
/// rules give the logged-in user that).
/// </summary>
[SupportedOSPlatform("linux")]
public sealed unsafe class UhidDevice : IHidDevice
{
    // linux/uhid.h: struct uhid_event is packed, a __u32 type then the largest member, create2.
    public const int EventSize = 4376;
    public const uint UHID_DESTROY = 1, UHID_START = 2, UHID_STOP = 3, UHID_OPEN = 4, UHID_CLOSE = 5, UHID_OUTPUT = 6;
    public const uint UHID_GET_REPORT = 9, UHID_GET_REPORT_REPLY = 10, UHID_CREATE2 = 11, UHID_INPUT2 = 12;
    public const uint UHID_SET_REPORT = 13, UHID_SET_REPORT_REPLY = 14;
    private const int O_RDWR = 2, O_NONBLOCK = 0x800, O_CLOEXEC = 0x80000;

    [DllImport("libc", SetLastError = true)] private static extern int open(string path, int flags);
    [DllImport("libc", SetLastError = true)] private static extern int close(int fd);
    [DllImport("libc", SetLastError = true)] private static extern nint read(int fd, byte* buffer, nuint count);
    [DllImport("libc", SetLastError = true)] private static extern nint write(int fd, byte* buffer, nuint count);
    [DllImport("libc", SetLastError = true)] private static extern int poll(PollFd* fds, nuint count, int timeoutMs);

    [StructLayout(LayoutKind.Sequential)]
    private struct PollFd
    {
        public int Fd;
        public short Events;
        public short Revents;
    }

    private const short POLLIN = 1;

    private readonly int _fd;
    private readonly Thread _reader;
    private readonly object _writeLock = new();
    private volatile bool _closed;

    public event Action<byte[], byte>? Output;
    public event Action<uint, byte, byte>? GetReport;
    public event Action<uint, byte, byte[]>? SetReport;

    public UhidDevice(HidDescription description)
    {
        _fd = open("/dev/uhid", O_RDWR | O_CLOEXEC | O_NONBLOCK);
        if (_fd < 0)
        {
            throw new InvalidOperationException($"Can't open /dev/uhid (errno {Marshal.GetLastPInvokeError()}).");
        }
        try
        {
            Send(Create2(description));
        }
        catch
        {
            close(_fd);
            throw;
        }
        _reader = new Thread(ReadLoop) { IsBackground = true, Name = "uhid " + description.Name };
        _reader.Start();
    }

    /// <summary>The UHID_CREATE2 event for this description.</summary>
    public static byte[] Create2(HidDescription d)
    {
        if (d.Descriptor.Length is 0 or > 4096)
        {
            throw new InvalidOperationException($"Report descriptor of {d.Descriptor.Length} bytes can't be recreated.");
        }
        var e = new byte[EventSize];
        var span = e.AsSpan();
        BinaryPrimitives.WriteUInt32LittleEndian(span, UHID_CREATE2);
        Text(span.Slice(4, 128), d.Name);
        Text(span.Slice(132, 64), "bragi");
        Text(span.Slice(196, 64), d.Uniq);
        BinaryPrimitives.WriteUInt16LittleEndian(span[260..], (ushort)d.Descriptor.Length);
        BinaryPrimitives.WriteUInt16LittleEndian(span[262..], d.Bus);
        BinaryPrimitives.WriteUInt32LittleEndian(span[264..], d.Vendor);
        BinaryPrimitives.WriteUInt32LittleEndian(span[268..], d.Product);
        BinaryPrimitives.WriteUInt32LittleEndian(span[272..], d.Version);
        BinaryPrimitives.WriteUInt32LittleEndian(span[276..], d.Country);
        d.Descriptor.CopyTo(span[280..]);
        return e;
    }

    /// <summary>UHID_INPUT2: a __u16 size, then the report.</summary>
    public static byte[] Input2(byte[] report)
    {
        var e = new byte[6 + Math.Min(report.Length, 4096)];
        BinaryPrimitives.WriteUInt32LittleEndian(e, UHID_INPUT2);
        BinaryPrimitives.WriteUInt16LittleEndian(e.AsSpan(4), (ushort)(e.Length - 6));
        report.AsSpan(0, e.Length - 6).CopyTo(e.AsSpan(6));
        return e;
    }

    /// <summary>UHID_GET_REPORT_REPLY: __u32 id, __u16 err, __u16 size, data.</summary>
    public static byte[] GetReportReply(uint request, ushort error, byte[] data)
    {
        var size = Math.Min(data.Length, 4096);
        var e = new byte[12 + size];
        BinaryPrimitives.WriteUInt32LittleEndian(e, UHID_GET_REPORT_REPLY);
        BinaryPrimitives.WriteUInt32LittleEndian(e.AsSpan(4), request);
        BinaryPrimitives.WriteUInt16LittleEndian(e.AsSpan(8), error);
        BinaryPrimitives.WriteUInt16LittleEndian(e.AsSpan(10), (ushort)size);
        data.AsSpan(0, size).CopyTo(e.AsSpan(12));
        return e;
    }

    /// <summary>UHID_SET_REPORT_REPLY: __u32 id, __u16 err.</summary>
    public static byte[] SetReportReply(uint request, ushort error)
    {
        var e = new byte[10];
        BinaryPrimitives.WriteUInt32LittleEndian(e, UHID_SET_REPORT_REPLY);
        BinaryPrimitives.WriteUInt32LittleEndian(e.AsSpan(4), request);
        BinaryPrimitives.WriteUInt16LittleEndian(e.AsSpan(8), error);
        return e;
    }

    public void Input(byte[] report) => Send(Input2(report));
    public void ReplyGetReport(uint request, ushort error, byte[] data) => Send(GetReportReply(request, error, data));
    public void ReplySetReport(uint request, ushort error) => Send(SetReportReply(request, error));

    /// <summary>Raises the event for one uhid event read from the kernel; public for tests.</summary>
    public static void Dispatch(ReadOnlySpan<byte> e, Action<byte[], byte>? output, Action<uint, byte, byte>? get,
        Action<uint, byte, byte[]>? set)
    {
        switch (BinaryPrimitives.ReadUInt32LittleEndian(e))
        {
            case UHID_OUTPUT:
                // struct uhid_output_req: data[4096], __u16 size, __u8 rtype.
                var size = Math.Min((int)BinaryPrimitives.ReadUInt16LittleEndian(e[4100..]), 4096);
                output?.Invoke(e.Slice(4, size).ToArray(), e[4102]);
                break;
            case UHID_GET_REPORT:
                // struct uhid_get_report_req: __u32 id, __u8 rnum, __u8 rtype.
                get?.Invoke(BinaryPrimitives.ReadUInt32LittleEndian(e[4..]), e[8], e[9]);
                break;
            case UHID_SET_REPORT:
                // struct uhid_set_report_req: __u32 id, __u8 rnum, __u8 rtype, __u16 size, data.
                var length = Math.Min((int)BinaryPrimitives.ReadUInt16LittleEndian(e[10..]), 4096);
                set?.Invoke(BinaryPrimitives.ReadUInt32LittleEndian(e[4..]), e[9], e.Slice(12, length).ToArray());
                break;
        }
    }

    private void ReadLoop()
    {
        var buffer = new byte[EventSize];
        try
        {
            while (!_closed)
            {
                // A short poll, so Dispose doesn't leave this thread blocked in read.
                var pfd = new PollFd { Fd = _fd, Events = POLLIN };
                if (poll(&pfd, 1, 250) <= 0 || (pfd.Revents & POLLIN) == 0)
                {
                    continue;
                }
                nint n;
                fixed (byte* p = buffer)
                {
                    n = read(_fd, p, (nuint)buffer.Length);
                }
                if (n <= 0)
                {
                    continue;
                }
                try
                {
                    Dispatch(buffer.AsSpan(0, (int)n), Output, GetReport, SetReport);
                }
                catch (Exception)
                {
                    // A handler failing must not stop the device.
                }
            }
        }
        finally
        {
            // The reader owns the fd from here, so it never reads a reused number.
            close(_fd);
        }
    }

    private void Send(byte[] e)
    {
        lock (_writeLock)
        {
            if (_closed)
            {
                return;
            }
            fixed (byte* p = e)
            {
                if (write(_fd, p, (nuint)e.Length) < 0)
                {
                    throw new InvalidOperationException($"uhid write failed (errno {Marshal.GetLastPInvokeError()}).");
                }
            }
        }
    }

    private static void Text(Span<byte> field, string value)
    {
        var bytes = Encoding.UTF8.GetBytes(value);
        bytes.AsSpan(0, Math.Min(bytes.Length, field.Length - 1)).CopyTo(field);
    }

    public void Dispose()
    {
        lock (_writeLock)
        {
            if (_closed)
            {
                return;
            }
            var e = new byte[EventSize];
            BinaryPrimitives.WriteUInt32LittleEndian(e, UHID_DESTROY);
            fixed (byte* p = e)
            {
                write(_fd, p, (nuint)e.Length);
            }
            _closed = true; // the reader closes the fd on its way out
        }
    }
}
