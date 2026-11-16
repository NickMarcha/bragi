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

/// <summary>
/// The Windows tray app's own audio link to the Bragi host, instead of
/// Voicemeeter's VBAN: it plays the host's headset mic (VBAN stream
/// VbanStreamSend, from the host) on a chosen output device, and sends a
/// chosen input device, or what an output device is playing (loopback), as
/// VbanStreamReceive to the host. 48 kHz, 16-bit stereo, as the host's
/// PipeWire modules expect. Voicemeeter's own VBAN must be off: both would
/// want UDP port 6980.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class VbanLink : IDisposable
{
    private const int Rate = 48000, Channels = 2;
    private readonly IPAddress _host;
    private readonly int _port;
    private readonly string _receiveStream; // what the host sends us
    private readonly string _sendStream;    // what we send the host
    private readonly object _lock = new();
    private UdpClient? _udp;
    private CancellationTokenSource? _stop;
    private WasapiOut? _output;
    private BufferedWaveProvider? _playBuffer;
    private IWaveIn? _capture;
    private BufferedWaveProvider? _captureBuffer;
    private ISampleProvider? _captureChain;
    private readonly List<float> _pending = new();
    private uint _counter;
    private DateTime _lastReceived = DateTime.MinValue;

    public string? Problem { get; private set; }
    public AudioDevices Devices { get; private set; } = AudioDevices.Off;
    public event Action? Changed;

    public VbanLink(string host, int port, string receiveStream, string sendStream)
    {
        _host = IPAddress.Parse(host);
        _port = port;
        _receiveStream = receiveStream;
        _sendStream = sendStream;
    }

    public bool Receiving => DateTime.UtcNow - _lastReceived < TimeSpan.FromSeconds(2);

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
            StopAll();
            Devices = devices;
            Problem = null;
            try
            {
                if (devices.Playback is not null || devices.Capture is not null)
                {
                    // Bound even when only sending: the host's packets would otherwise get ICMP replies.
                    _udp = new UdpClient(new IPEndPoint(IPAddress.Any, _port));
                    _stop = new CancellationTokenSource();
                }
                if (devices.Playback is not null)
                {
                    StartPlayback(devices.Playback);
                }
                if (devices.Capture is not null)
                {
                    StartCapture(devices.Capture, devices.CaptureIsLoopback);
                }
            }
            catch (SocketException ex) when (ex.SocketErrorCode == SocketError.AddressAlreadyInUse)
            {
                Problem = $"UDP port {_port} is in use. Turn Voicemeeter's VBAN off (Menu > VBAN), then choose the devices again.";
                StopAll();
            }
            catch (Exception ex) when (ex is SocketException or System.Runtime.InteropServices.COMException or InvalidOperationException or ArgumentException)
            {
                Problem = $"Audio didn't start: {ex.Message}";
                StopAll();
            }
        }
        Changed?.Invoke();
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
        _output.Play();
        var udp = _udp!;
        var token = _stop!.Token;
        _ = Task.Run(() => ReceiveLoop(udp, token), token);
    }

    private async Task ReceiveLoop(UdpClient udp, CancellationToken token)
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
            if (!result.RemoteEndPoint.Address.Equals(_host))
            {
                continue;
            }
            var packet = Vban.Decode(result.Buffer);
            if (packet is null || packet.Stream != _receiveStream || packet.SampleRate != Rate || packet.Channels != Channels)
            {
                continue;
            }
            var wasReceiving = Receiving;
            _lastReceived = DateTime.UtcNow;
            // Keep latency low: if the buffer has run far ahead, start over from now.
            if (_playBuffer is { } buffer)
            {
                if (buffer.BufferedDuration > TimeSpan.FromMilliseconds(200))
                {
                    buffer.ClearBuffer();
                }
                buffer.AddSamples(packet.Pcm.ToArray(), 0, packet.Pcm.Length);
            }
            if (!wasReceiving)
            {
                Changed?.Invoke();
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
        _capture.StartRecording();
    }

    private void OnCaptured(object? sender, WaveInEventArgs e)
    {
        if (_captureBuffer is null || _captureChain is null || _udp is null)
        {
            return;
        }
        _captureBuffer.AddSamples(e.Buffer, 0, e.BytesRecorded);
        var chunk = new float[Vban.MaxFrames * Channels];
        int read;
        while (_captureBuffer.BufferedBytes > 0 && (read = _captureChain.Read(chunk, 0, chunk.Length)) > 0)
        {
            _pending.AddRange(chunk.AsSpan(0, read).ToArray());
        }
        var pcm = new byte[Vban.MaxFrames * Channels * 2];
        while (_pending.Count >= Vban.MaxFrames * Channels)
        {
            Vban.FloatToPcm16(_pending.GetRange(0, Vban.MaxFrames * Channels).ToArray(), pcm);
            _pending.RemoveRange(0, Vban.MaxFrames * Channels);
            try
            {
                var packet = Vban.Encode(_sendStream, Rate, Channels, _counter++, pcm);
                _udp.Send(packet, packet.Length, new IPEndPoint(_host, _port));
            }
            catch (Exception ex) when (ex is SocketException or ObjectDisposedException)
            {
                return;
            }
        }
    }

    private void StopAll()
    {
        _stop?.Cancel();
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
            _capture.Dispose();
        }
        _output?.Stop();
        _output?.Dispose();
        _udp?.Dispose();
        _capture = null;
        _output = null;
        _udp = null;
        _playBuffer = null;
        _captureBuffer = null;
        _captureChain = null;
        _pending.Clear();
        _lastReceived = DateTime.MinValue;
    }

    public void Dispose()
    {
        lock (_lock)
        {
            StopAll();
        }
    }
}
