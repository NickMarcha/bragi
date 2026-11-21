using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Runtime.Versioning;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using NAudio.CoreAudioApi;
using NAudio.Wave;
using NAudio.Wave.SampleProviders;

namespace Bragi.Client.Audio;

/// <summary>Which Windows devices the link uses; null means that direction is off.</summary>
public sealed record AudioDevices(string? Playback, string? Capture, bool CaptureIsLoopback)
{
    public static readonly AudioDevices Off = new(null, null, false);

    public static string FilePath => Path.Combine(Config.RocLinkConfig.ConfigDir, "audio.json");

    public static AudioDevices Load()
    {
        try
        {
            return JsonSerializer.Deserialize<AudioDevices>(File.ReadAllText(FilePath)) ?? Off;
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
            return Off;
        }
    }

    public void Save()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        File.WriteAllText(FilePath, JsonSerializer.Serialize(this));
    }
}

/// <summary>A Windows audio endpoint, for the menu.</summary>
public sealed record AudioEndpoint(string Id, string Name);

/// <summary>What the link is doing right now, for the menu (Tray/AudioMenu.cs).</summary>
public sealed record LinkReport(
    AudioDevices Devices,
    string? PlaybackName,
    string? CaptureName,
    bool Receiving,
    float ReceiveLevel,
    float SendLevel,
    float SourceLevel,
    string? Problem);

/// <summary>
/// The Windows tray app's own audio link to the Bragi host, instead of
/// Voicemeeter's VBAN: it plays the host's headset mic (VBAN stream
/// VbanStreamSend, from the host) on a chosen output device, and sends a
/// chosen input device, or what an output device is playing (loopback), as
/// VbanStreamReceive to the host. 48 kHz, 16-bit stereo, as the host's
/// PipeWire modules expect. Voicemeeter's own VBAN must be off: both would
/// want UDP port 6980.
///
/// Devices go away and come back (unplugged, or reopened when Voicemeeter
/// changes a bus from WDM to MME), so a health check every half second
/// restarts the link when a device stops, and when the captured device is
/// playing sound but none of it reaches us.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class VbanLink : IDisposable
{
    private const int Rate = 48000, Channels = 2;
    private static readonly TimeSpan Tick = TimeSpan.FromMilliseconds(500);
    private const int StuckTicks = 6; // 3 s of sound on the device and none captured

    // Changed live by SetHost (LAN fallback, bragi#81): the send loop's
    // target and the receive loop's sender filter.
    private volatile IPAddress _host;
    private readonly int _port;
    private readonly string _receiveStream; // what the host sends us
    private readonly string _sendStream;    // what we send the host
    private readonly object _lock = new();
    private readonly Timer _health;
    private Session? _session;
    private MMDevice? _meterDevice;
    private string? _meterId;
    private DateTime _retryAt = DateTime.MaxValue;
    private int _stuck;
    private int _stuckRestarts; // in a row, without sound getting through in between
    private float _receiveLevel, _sendLevel, _sourceLevel;
    private bool _disposed;

    public string? Problem { get; private set; }
    public AudioDevices Devices { get; private set; } = AudioDevices.Off;
    public string? PlaybackName { get; private set; }
    public string? CaptureName { get; private set; }

    /// <summary>Raised from any thread when the report may have changed.</summary>
    public event Action? Changed;

    public VbanLink(string host, int port, string receiveStream, string sendStream)
    {
        _host = IPAddress.Parse(host);
        _port = port;
        _receiveStream = receiveStream;
        _sendStream = sendStream;
        _health = new Timer(_ => HealthCheck(), null, Tick, Tick);
    }

    /// <summary>Sends to, and accepts audio from, this address from now on.</summary>
    public void SetHost(string host) => _host = IPAddress.Parse(host);

    public bool Receiving => _session is { } s && DateTime.UtcNow - s.LastReceived < TimeSpan.FromSeconds(2);

    public LinkReport Report => new(Devices, PlaybackName, CaptureName, Receiving,
        _receiveLevel, _sendLevel, _sourceLevel, Problem);

    public static IReadOnlyList<AudioEndpoint> Endpoints(DataFlow flow)
    {
        using var enumerator = new MMDeviceEnumerator();
        return enumerator.EnumerateAudioEndPoints(flow, DeviceState.Active)
            .Select(d => new AudioEndpoint(d.ID, d.FriendlyName)).OrderBy(e => e.Name).ToList();
    }

    /// <summary>Starts (or restarts) with these devices.</summary>
    public void Apply(AudioDevices devices)
    {
        lock (_lock)
        {
            if (_disposed)
            {
                return;
            }
            StopSession();
            Devices = devices;
            Problem = null;
            _retryAt = DateTime.MaxValue;
            _stuck = 0;
            PlaybackName = devices.Playback is { } p ? NameOf(p) : null;
            CaptureName = devices.Capture is { } c ? NameOf(c) : null;
            if (devices.Playback is null && devices.Capture is null)
            {
                Changed?.Invoke();
                return;
            }
            var session = new Session(this);
            try
            {
                session.Start(devices);
                _session = session;
            }
            catch (SocketException ex) when (ex.SocketErrorCode == SocketError.AddressAlreadyInUse)
            {
                session.Stop();
                Problem = $"UDP port {_port} is in use. Turn Voicemeeter's VBAN off (Menu > VBAN); this retries by itself.";
                _retryAt = DateTime.UtcNow.AddSeconds(10);
            }
            catch (Exception ex) when (ex is SocketException or System.Runtime.InteropServices.COMException or InvalidOperationException or ArgumentException)
            {
                session.Stop();
                Problem = $"Audio didn't start ({ex.Message.Trim()}); trying again.";
                _retryAt = DateTime.UtcNow.AddSeconds(5);
            }
        }
        Changed?.Invoke();
    }

    private static string? NameOf(string id)
    {
        try
        {
            using var enumerator = new MMDeviceEnumerator();
            return enumerator.GetDevice(id).FriendlyName;
        }
        catch (Exception ex) when (ex is System.Runtime.InteropServices.COMException or ArgumentException)
        {
            return null; // not plugged in right now
        }
    }

    /// <summary>A device stopped under us: start over shortly (from the health check).</summary>
    private void SessionFailed(Session session, string what)
    {
        lock (_lock)
        {
            if (_session != session || _disposed)
            {
                return; // already replaced or stopped on purpose
            }
            StopSession();
            Problem = $"{what} stopped; restarting.";
            _retryAt = DateTime.UtcNow.AddSeconds(2);
        }
        Changed?.Invoke();
    }

    private void HealthCheck()
    {
        AudioDevices? restart = null;
        bool changed;
        lock (_lock)
        {
            if (_disposed)
            {
                return;
            }
            var before = (Bars(_receiveLevel), Bars(_sendLevel), Bars(_sourceLevel), Receiving);
            var session = _session;
            _receiveLevel = session?.TakeReceivePeak() ?? 0;
            _sendLevel = session?.TakeSendPeak() ?? 0;
            _sourceLevel = SourcePeak();

            // Sound on the captured device, nothing reaching us: the capture
            // is stuck on a device that was reopened. Start it again.
            if (session is not null && Devices.Capture is not null && _sourceLevel > 0.01f && _sendLevel < 0.0005f)
            {
                if (++_stuck >= StuckTicks && _stuckRestarts < 3)
                {
                    _stuckRestarts++;
                    restart = Devices;
                }
                else if (_stuck >= StuckTicks && Problem is null)
                {
                    Problem = $"Sound plays on {CaptureName ?? "the device"}, but none of it can be captured. Is something using it in exclusive mode?";
                }
            }
            else
            {
                _stuck = 0;
                if (_sendLevel >= 0.0005f)
                {
                    _stuckRestarts = 0;
                    if (Problem?.StartsWith("Sound plays on", StringComparison.Ordinal) == true)
                    {
                        Problem = null;
                    }
                }
            }
            if (DateTime.UtcNow >= _retryAt)
            {
                restart = Devices;
            }
            changed = before != (Bars(_receiveLevel), Bars(_sendLevel), Bars(_sourceLevel), Receiving);
        }
        if (restart is not null)
        {
            Apply(restart);
        }
        else if (changed)
        {
            Changed?.Invoke();
        }
    }

    /// <summary>The peak the captured device itself shows (what's playing on it), 0 when unknown.</summary>
    private float SourcePeak()
    {
        var id = Devices.Capture;
        try
        {
            if (id != _meterId)
            {
                _meterDevice?.Dispose();
                _meterDevice = null;
                _meterId = id;
                if (id is not null)
                {
                    using var enumerator = new MMDeviceEnumerator();
                    _meterDevice = enumerator.GetDevice(id);
                }
            }
            return _meterDevice?.AudioMeterInformation.MasterPeakValue ?? 0;
        }
        catch (Exception ex) when (ex is System.Runtime.InteropServices.COMException or ArgumentException or InvalidCastException)
        {
            _meterDevice?.Dispose();
            _meterDevice = null;
            _meterId = null; // look it up again next time
            return 0;
        }
    }

    private void StopSession()
    {
        _session?.Stop();
        _session = null;
    }

    public void Dispose()
    {
        lock (_lock)
        {
            _disposed = true;
            StopSession();
            _meterDevice?.Dispose();
            _meterDevice = null;
        }
        _health.Dispose();
    }

    /// <summary>Level bars for a 0..1 peak: five steps over -60..0 dBFS.</summary>
    public static string Bars(float peak)
    {
        var filled = peak <= 0 ? 0 : (int)Math.Clamp(Math.Ceiling((20 * Math.Log10(peak) + 60) / 12), 0, 5);
        return new string('■', filled) + new string('□', 5 - filled);
    }

    /// <summary>
    /// One run of the link with one set of devices. Everything the audio
    /// threads touch lives here, so stopping a session can't pull state out
    /// from under a callback that's still finishing.
    /// </summary>
    private sealed class Session
    {
        private readonly VbanLink _link;
        private readonly CancellationTokenSource _stop = new();
        private readonly List<float> _pending = new();
        private UdpClient? _udp;
        private WasapiOut? _output;
        private BufferedWaveProvider? _playBuffer;
        private IWaveIn? _capture;
        private BufferedWaveProvider? _captureBuffer;
        private ISampleProvider? _captureChain;
        private uint _counter;
        private float _receivePeak, _sendPeak;
        private volatile bool _stopping;

        public DateTime LastReceived { get; private set; } = DateTime.MinValue;

        public Session(VbanLink link) => _link = link;

        public float TakeReceivePeak() => Interlocked.Exchange(ref _receivePeak, 0);
        public float TakeSendPeak() => Interlocked.Exchange(ref _sendPeak, 0);

        public void Start(AudioDevices devices)
        {
            // Bound even when only sending: the host's packets would otherwise get ICMP replies.
            _udp = new UdpClient(new IPEndPoint(IPAddress.Any, _link._port));
            if (devices.Playback is not null)
            {
                StartPlayback(devices.Playback);
            }
            if (devices.Capture is not null)
            {
                StartCapture(devices.Capture, devices.CaptureIsLoopback);
            }
        }

        private static MMDevice Device(string id)
        {
            using var enumerator = new MMDeviceEnumerator();
            return enumerator.GetDevice(id);
        }

        private void StartPlayback(string id)
        {
            _playBuffer = new BufferedWaveProvider(new WaveFormat(Rate, 16, Channels))
            {
                BufferDuration = TimeSpan.FromMilliseconds(400),
                DiscardOnBufferOverflow = true,
                ReadFully = true, // silence while nothing arrives
            };
            _output = new WasapiOut(Device(id), AudioClientShareMode.Shared, true, 40);
            _output.Init(_playBuffer);
            _output.PlaybackStopped += (_, _) =>
            {
                if (!_stopping)
                {
                    _link.SessionFailed(this, "Playing the headset mic");
                }
            };
            _output.Play();
            var udp = _udp!;
            _ = Task.Run(() => ReceiveLoop(udp, _playBuffer, _stop.Token));
        }

        private async Task ReceiveLoop(UdpClient udp, BufferedWaveProvider buffer, CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                UdpReceiveResult result;
                try
                {
                    result = await udp.ReceiveAsync(token);
                }
                catch (Exception ex) when (ex is OperationCanceledException or ObjectDisposedException)
                {
                    return;
                }
                catch (SocketException)
                {
                    continue; // e.g. an ICMP "port unreachable" from the host while it restarts
                }
                if (!result.RemoteEndPoint.Address.Equals(_link._host))
                {
                    continue;
                }
                var packet = Vban.Decode(result.Buffer);
                if (packet is null || packet.Stream != _link._receiveStream || packet.SampleRate != Rate || packet.Channels != Channels)
                {
                    continue;
                }
                var wasReceiving = DateTime.UtcNow - LastReceived < TimeSpan.FromSeconds(2);
                LastReceived = DateTime.UtcNow;
                var pcm = packet.Pcm.ToArray();
                Peak(ref _receivePeak, Pcm16Peak(pcm));
                // Keep latency low: if the buffer has run far ahead, start over from now.
                if (buffer.BufferedDuration > TimeSpan.FromMilliseconds(200))
                {
                    buffer.ClearBuffer();
                }
                buffer.AddSamples(pcm, 0, pcm.Length);
                if (!wasReceiving)
                {
                    _link.Changed?.Invoke();
                }
            }
        }

        private void StartCapture(string id, bool loopback)
        {
            var device = Device(id);
            _capture = loopback ? new WasapiLoopbackCapture(device) : new WasapiCapture(device, true, 20);
            var format = _capture.WaveFormat;
            _captureBuffer = new BufferedWaveProvider(format) { BufferDuration = TimeSpan.FromSeconds(1), DiscardOnBufferOverflow = true, ReadFully = false };
            ISampleProvider chain = _captureBuffer.ToSampleProvider();
            chain = chain.WaveFormat.Channels switch
            {
                1 => new MonoToStereoSampleProvider(chain),
                2 => chain,
                _ => new MultiplexingSampleProvider([chain], 2), // the first two channels
            };
            if (chain.WaveFormat.SampleRate != Rate)
            {
                chain = new WdlResamplingSampleProvider(chain, Rate);
            }
            _captureChain = chain;
            _capture.DataAvailable += OnCaptured;
            _capture.RecordingStopped += (_, _) =>
            {
                if (!_stopping)
                {
                    _link.SessionFailed(this, loopback ? "Capturing what plays on the device" : "Capturing the input");
                }
            };
            _capture.StartRecording();
        }

        private void OnCaptured(object? sender, WaveInEventArgs e)
        {
            if (_stopping || _captureBuffer is not { } buffer || _captureChain is not { } chain || _udp is not { } udp)
            {
                return;
            }
            buffer.AddSamples(e.Buffer, 0, e.BytesRecorded);
            var chunk = new float[Vban.MaxFrames * Channels];
            int read;
            while (buffer.BufferedBytes > 0 && (read = chain.Read(chunk, 0, chunk.Length)) > 0)
            {
                var max = 0f;
                for (var i = 0; i < read; i++)
                {
                    max = Math.Max(max, Math.Abs(chunk[i]));
                }
                Peak(ref _sendPeak, max);
                _pending.AddRange(chunk.AsSpan(0, read).ToArray());
            }
            var pcm = new byte[Vban.MaxFrames * Channels * 2];
            while (_pending.Count >= Vban.MaxFrames * Channels)
            {
                Vban.FloatToPcm16(_pending.GetRange(0, Vban.MaxFrames * Channels).ToArray(), pcm);
                _pending.RemoveRange(0, Vban.MaxFrames * Channels);
                try
                {
                    var packet = Vban.Encode(_link._sendStream, Rate, Channels, _counter++, pcm);
                    udp.Send(packet, packet.Length, new IPEndPoint(_link._host, _link._port));
                }
                catch (Exception ex) when (ex is SocketException or ObjectDisposedException)
                {
                    return;
                }
            }
        }

        private static float Pcm16Peak(byte[] pcm)
        {
            var max = 0;
            for (var i = 0; i + 1 < pcm.Length; i += 2)
            {
                max = Math.Max(max, Math.Abs((int)BitConverter.ToInt16(pcm, i)));
            }
            return max / 32768f;
        }

        private static void Peak(ref float slot, float value)
        {
            float seen;
            do
            {
                seen = Volatile.Read(ref slot);
                if (value <= seen)
                {
                    return;
                }
            }
            while (Interlocked.CompareExchange(ref slot, value, seen) != seen);
        }

        public void Stop()
        {
            _stopping = true;
            _stop.Cancel();
            if (_capture is not null)
            {
                _capture.DataAvailable -= OnCaptured;
                try
                {
                    _capture.StopRecording();
                }
                catch (Exception)
                {
                    // already stopped or the device went away
                }
                // Disposing waits for the capture thread; do it off the caller's thread
                // (which may hold the link's lock while a callback is finishing).
                var capture = _capture;
                _ = Task.Run(capture.Dispose);
            }
            if (_output is not null)
            {
                var output = _output;
                try
                {
                    output.Stop();
                }
                catch (Exception)
                {
                    // the device went away
                }
                _ = Task.Run(output.Dispose);
            }
            _udp?.Dispose();
            _capture = null;
            _output = null;
            _udp = null;
        }
    }
}
