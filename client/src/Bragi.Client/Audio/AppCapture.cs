using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Threading;
using System.Threading.Tasks;
using NAudio.CoreAudioApi;
using NAudio.CoreAudioApi.Interfaces;

namespace Bragi.Client.Audio;

/// <summary>
/// One app's sound as a VBAN stream of its own (bragi#93): Windows' process
/// loopback (Windows 10 2004+) captures what a process and its children
/// play, on whichever device and before that device's volume, so the app can
/// play to a device nobody listens to and still be heard through the hub.
/// Used for the "comms" group: Discord, sent in mono (voice needs no more,
/// and it halves the bandwidth).
///
/// Finds the app's root process every 2 s and starts over when it changes
/// (Discord restarted). Also notes which devices the app has sound playing
/// on, so the menu can say when it reaches the desktop stream too.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class AppCapture : IDisposable
{
    private const int Rate = 48000;
    private readonly string _exe;
    private readonly string _stream;
    private readonly int _channels;
    private readonly Func<IPAddress> _host;
    private readonly int _port;
    private readonly Timer _watch;
    private readonly object _lock = new();
    private CancellationTokenSource? _run;
    private float _peak;
    private int _ticks;
    private bool _disposed;

    /// <summary>The app's root process while it runs, else null.</summary>
    public uint? ProcessId { get; private set; }
    /// <summary>Why capturing failed, when it did.</summary>
    public string? Problem { get; private set; }
    /// <summary>Devices the app has sound playing on (from the last check, every 4 s).</summary>
    public IReadOnlyList<AudioEndpoint> PlayingOn { get; private set; } = [];
    public string Stream => _stream;
    public string AppName => _exe;

    public AppCapture(string exe, string stream, int channels, Func<IPAddress> host, int port)
    {
        _exe = exe;
        _stream = stream;
        _channels = channels;
        _host = host;
        _port = port;
        _watch = new Timer(_ => Watch(), null, TimeSpan.Zero, TimeSpan.FromSeconds(2));
    }

    /// <summary>The loudest sample since the last call, 0..1.</summary>
    public float TakePeak() => Interlocked.Exchange(ref _peak, 0);

    private void Watch()
    {
        var all = Snapshot();
        var root = RootOf(all, _exe + ".exe");
        lock (_lock)
        {
            if (_disposed)
            {
                return;
            }
            if (root != ProcessId)
            {
                _run?.Cancel();
                _run = null;
                ProcessId = root;
                Problem = null;
                if (root is { } pid)
                {
                    var run = new CancellationTokenSource();
                    _run = run;
                    new Thread(() => CaptureLoop(pid, run.Token)) { IsBackground = true, Name = $"Bragi capture {_exe}" }.Start();
                }
            }
        }
        if (_ticks++ % 2 == 0)
        {
            var tree = root is { } r ? TreeOf(all, r) : new HashSet<uint>();
            PlayingOn = tree.Count > 0 ? DevicesPlaying(tree) : [];
        }
    }

    private void CaptureLoop(uint pid, CancellationToken token)
    {
        try
        {
            var client = Loopback.ActivateAsync(pid).GetAwaiter().GetResult();
            var format = new Loopback.WaveFormatEx
            {
                wFormatTag = 1, nChannels = 2, nSamplesPerSec = Rate, wBitsPerSample = 16, nBlockAlign = 4, nAvgBytesPerSec = Rate * 4,
            };
            const int LoopbackFlag = 0x00020000, AutoConvert = unchecked((int)0x80000000), SrcDefault = 0x08000000;
            Check(client.Initialize(0, LoopbackFlag | AutoConvert | SrcDefault, 2_000_000, 0, ref format, IntPtr.Zero), "Initialize");
            var iid = typeof(Loopback.IAudioCaptureClient).GUID;
            Check(client.GetService(ref iid, out var service), "GetService");
            var capture = (Loopback.IAudioCaptureClient)service;
            Check(client.Start(), "Start");
            using var udp = new UdpClient();
            var pending = new List<byte>();
            uint counter = 0;
            var frameBytes = 2 * _channels;
            var packetBytes = Vban.MaxFrames * frameBytes;
            try
            {
                while (!token.IsCancellationRequested)
                {
                    Thread.Sleep(10);
                    while (capture.GetNextPacketSize(out var frames) == 0 && frames > 0)
                    {
                        Check(capture.GetBuffer(out var data, out var n, out var flags, out _, out _), "GetBuffer");
                        var stereo = new short[n * 2];
                        if ((flags & 2) == 0) // not AUDCLNT_BUFFERFLAGS_SILENT
                        {
                            Marshal.Copy(data, stereo, 0, stereo.Length);
                        }
                        capture.ReleaseBuffer(n);
                        Append(pending, stereo, _channels, ref _peak);
                    }
                    while (pending.Count >= packetBytes)
                    {
                        var pcm = pending.GetRange(0, packetBytes).ToArray();
                        pending.RemoveRange(0, packetBytes);
                        var packet = Vban.Encode(_stream, Rate, _channels, counter++, pcm);
                        udp.Send(packet, packet.Length, new IPEndPoint(_host(), _port));
                    }
                }
            }
            finally
            {
                client.Stop();
                Marshal.ReleaseComObject(capture);
                Marshal.ReleaseComObject(client);
            }
        }
        catch (Exception ex) when (ex is COMException or SocketException or InvalidOperationException or InvalidCastException)
        {
            lock (_lock)
            {
                if (!token.IsCancellationRequested)
                {
                    Problem = $"Couldn't capture {_exe}: {ex.Message.Trim()}";
                    ProcessId = null; // the next check tries again
                }
            }
        }
    }

    /// <summary>Adds 16-bit stereo samples to the outgoing PCM, downmixed to
    /// mono for one channel, and keeps the peak (0..1).</summary>
    public static void Append(List<byte> pcm, ReadOnlySpan<short> stereo, int channels, ref float peak)
    {
        var loudest = 0;
        for (var i = 0; i + 1 < stereo.Length; i += 2)
        {
            if (channels == 1)
            {
                var mono = (short)((stereo[i] + stereo[i + 1]) / 2);
                pcm.Add((byte)mono);
                pcm.Add((byte)(mono >> 8));
                loudest = Math.Max(loudest, Math.Abs((int)mono));
            }
            else
            {
                pcm.Add((byte)stereo[i]);
                pcm.Add((byte)(stereo[i] >> 8));
                pcm.Add((byte)stereo[i + 1]);
                pcm.Add((byte)(stereo[i + 1] >> 8));
                loudest = Math.Max(loudest, Math.Max(Math.Abs((int)stereo[i]), Math.Abs((int)stereo[i + 1])));
            }
        }
        var value = loudest / 32768f;
        float seen;
        do
        {
            seen = Volatile.Read(ref peak);
        }
        while (value > seen && Interlocked.CompareExchange(ref peak, value, seen) != seen);
    }

    /// <summary>The root of the app's process tree: a process with this exe
    /// name whose parent isn't one too (Discord runs as several). The lowest
    /// pid if there are more.</summary>
    public static uint? RootOf(IReadOnlyList<(uint Pid, uint Parent, string Exe)> processes, string exe)
    {
        var mine = processes.Where(p => string.Equals(p.Exe, exe, StringComparison.OrdinalIgnoreCase)).ToList();
        var pids = mine.Select(p => p.Pid).ToHashSet();
        return mine.Where(p => !pids.Contains(p.Parent)).Select(p => (uint?)p.Pid).OrderBy(p => p).FirstOrDefault();
    }

    /// <summary>A process and everything under it.</summary>
    public static HashSet<uint> TreeOf(IReadOnlyList<(uint Pid, uint Parent, string Exe)> processes, uint root)
    {
        var tree = new HashSet<uint> { root };
        bool grew;
        do
        {
            grew = false;
            foreach (var p in processes)
            {
                if (!tree.Contains(p.Pid) && tree.Contains(p.Parent) && p.Pid != p.Parent)
                {
                    tree.Add(p.Pid);
                    grew = true;
                }
            }
        }
        while (grew);
        return tree;
    }

    private static List<AudioEndpoint> DevicesPlaying(HashSet<uint> tree)
    {
        var found = new List<AudioEndpoint>();
        try
        {
            using var enumerator = new MMDeviceEnumerator();
            foreach (var device in enumerator.EnumerateAudioEndPoints(DataFlow.Render, DeviceState.Active))
            {
                using (device)
                {
                    var sessions = device.AudioSessionManager.Sessions;
                    for (var i = 0; i < sessions.Count; i++)
                    {
                        var session = sessions[i];
                        if (session.State == AudioSessionState.AudioSessionStateActive && tree.Contains(session.GetProcessID))
                        {
                            found.Add(new AudioEndpoint(device.ID, device.FriendlyName));
                            break;
                        }
                    }
                }
            }
        }
        catch (Exception ex) when (ex is COMException or InvalidCastException)
        {
        }
        return found;
    }

    private static List<(uint Pid, uint Parent, string Exe)> Snapshot()
    {
        var list = new List<(uint, uint, string)>();
        var snap = CreateToolhelp32Snapshot(2 /* TH32CS_SNAPPROCESS */, 0);
        if (snap == IntPtr.Zero || snap == new IntPtr(-1))
        {
            return list;
        }
        try
        {
            var entry = new ProcessEntry { dwSize = (uint)Marshal.SizeOf<ProcessEntry>() };
            for (var ok = Process32FirstW(snap, ref entry); ok; ok = Process32NextW(snap, ref entry))
            {
                list.Add((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile));
            }
        }
        finally
        {
            CloseHandle(snap);
        }
        return list;
    }

    private static void Check(int hr, string what)
    {
        if (hr != 0)
        {
            throw new COMException($"{what} failed", hr);
        }
    }

    public void Dispose()
    {
        lock (_lock)
        {
            _disposed = true;
            _run?.Cancel();
            _run = null;
        }
        _watch.Dispose();
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ProcessEntry
    {
        public uint dwSize, cntUsage, th32ProcessID;
        public IntPtr th32DefaultHeapID;
        public uint th32ModuleID, cntThreads, th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string szExeFile;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern bool Process32FirstW(IntPtr snapshot, ref ProcessEntry entry);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern bool Process32NextW(IntPtr snapshot, ref ProcessEntry entry);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    /// <summary>Process loopback activation: ActivateAudioInterfaceAsync on
    /// VAD\Process_Loopback with AUDIOCLIENT_ACTIVATION_PARAMS (include the
    /// target's process tree). NAudio 2.2.1 has the types but no public way in.</summary>
    private static class Loopback
    {
        [StructLayout(LayoutKind.Sequential)]
        public struct WaveFormatEx
        {
            public ushort wFormatTag, nChannels;
            public uint nSamplesPerSec, nAvgBytesPerSec;
            public ushort nBlockAlign, wBitsPerSample, cbSize;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ActivationParams
        {
            public int ActivationType;
            public uint TargetProcessId;
            public int ProcessLoopbackMode;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PropVariantBlob
        {
            public ushort vt;
            private ushort r1, r2, r3;
            public uint cbSize;
            public IntPtr pBlobData;
        }

        [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        public interface IAudioClient
        {
            [PreserveSig] int Initialize(int shareMode, int streamFlags, long bufferDuration, long periodicity, ref WaveFormatEx format, IntPtr sessionGuid);
            [PreserveSig] int GetBufferSize(out uint frames);
            [PreserveSig] int GetStreamLatency(out long latency);
            [PreserveSig] int GetCurrentPadding(out uint padding);
            [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, IntPtr closest);
            [PreserveSig] int GetMixFormat(out IntPtr format);
            [PreserveSig] int GetDevicePeriod(out long def, out long min);
            [PreserveSig] int Start();
            [PreserveSig] int Stop();
            [PreserveSig] int Reset();
            [PreserveSig] int SetEventHandle(IntPtr handle);
            [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
        }

        [ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        public interface IAudioCaptureClient
        {
            [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong position, out ulong qpc);
            [PreserveSig] int ReleaseBuffer(uint frames);
            [PreserveSig] int GetNextPacketSize(out uint frames);
        }

        [ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IActivateOperation
        {
            [PreserveSig] int GetActivateResult(out int hr, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        }

        [ComImport, Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface ICompletionHandler
        {
            void ActivateCompleted(IActivateOperation op);
        }

        [ComImport, Guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IAgileObject
        {
        }

        private sealed class Handler : ICompletionHandler, IAgileObject
        {
            public readonly TaskCompletionSource<IAudioClient> Done = new(TaskCreationOptions.RunContinuationsAsynchronously);

            public void ActivateCompleted(IActivateOperation op)
            {
                var hr = op.GetActivateResult(out var result, out var iface);
                if (hr != 0 || result != 0)
                {
                    Done.SetException(new COMException("Process loopback activation failed", hr != 0 ? hr : result));
                }
                else
                {
                    Done.SetResult((IAudioClient)iface);
                }
            }
        }

        [DllImport("Mmdevapi.dll", ExactSpelling = true, PreserveSig = true)]
        private static extern int ActivateAudioInterfaceAsync([MarshalAs(UnmanagedType.LPWStr)] string path, ref Guid iid,
            IntPtr activationParams, ICompletionHandler handler, out IActivateOperation op);

        public static Task<IAudioClient> ActivateAsync(uint pid)
        {
            var size = Marshal.SizeOf<ActivationParams>();
            var parameters = Marshal.AllocHGlobal(size);
            var variant = Marshal.AllocHGlobal(Marshal.SizeOf<PropVariantBlob>());
            try
            {
                Marshal.StructureToPtr(new ActivationParams { ActivationType = 1, TargetProcessId = pid, ProcessLoopbackMode = 0 }, parameters, false);
                Marshal.StructureToPtr(new PropVariantBlob { vt = 65 /* VT_BLOB */, cbSize = (uint)size, pBlobData = parameters }, variant, false);
                var handler = new Handler();
                var iid = typeof(IAudioClient).GUID;
                var hr = ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ref iid, variant, handler, out _);
                if (hr != 0)
                {
                    throw new COMException("ActivateAudioInterfaceAsync failed", hr);
                }
                // The parameters are copied during the call; the result comes later.
                return handler.Done.Task;
            }
            finally
            {
                Marshal.FreeHGlobal(variant);
                Marshal.FreeHGlobal(parameters);
            }
        }
    }
}
