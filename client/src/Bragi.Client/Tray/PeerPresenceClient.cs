using System;
using System.IO;
using System.Net.Http;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Bragi.Client.Config;

namespace Bragi.Client.Tray;

/// <summary>
/// Holds a WebSocket open to app/main.py's /ws/peer/{name} for as long as
/// the Roc link is Enabled - the server tracks "connected" purely by
/// whether this socket is open (app/peer_presence.py), which is real
/// proof this machine is up and network-reachable. A loaded local Roc
/// module can't prove that on its own since Roc runs over UDP - see
/// client/README.md and the server's peer_presence.py docstring.
///
/// The connection's lifetime is the heartbeat. Over the same socket the
/// server sends the gamepad status and, on the computer that claimed the
/// controllers, their input; the client sends gamepad_claim and
/// gamepad_release (app/peer_control.py, app/gamepad.py).
///
/// When the hub can't be reached over Tailscale (two tries in a row), it
/// connects to the hub's LAN socket instead (app/lan_peer.py, bragi#90),
/// pinning the certificate and sending the token the hub gave it earlier.
/// While on the LAN it checks the Tailscale address every 30 s and moves
/// back once that answers.
/// </summary>
public sealed class PeerPresenceClient : IDisposable
{
    private readonly Uri? _uri;
    private CancellationTokenSource? _cts;

    public PeerPresenceClient(string? peerName, string? wsBaseUrl)
    {
        if (string.IsNullOrWhiteSpace(peerName) || string.IsNullOrWhiteSpace(wsBaseUrl))
        {
            _uri = null; // not configured - SetEnabled(true) becomes a no-op
            return;
        }
        _uri = new Uri($"{wsBaseUrl.TrimEnd('/')}/{Uri.EscapeDataString(peerName)}");
    }

    public void SetEnabled(bool enabled)
    {
        if (enabled)
        {
            Start();
        }
        else
        {
            Stop();
        }
    }

    private void Start()
    {
        if (_uri is null || _cts is not null)
        {
            return;
        }
        _cts = new CancellationTokenSource();
        _ = RunLoopAsync(_uri, _cts.Token);
    }

    private void Stop()
    {
        _cts?.Cancel();
        _cts = null;
    }

    /// <summary>The hub's LAN socket, read when it's needed; null when there's none.</summary>
    public Func<LanEndpoint?>? LanEndpoint { get; set; }

    /// <summary>True while connected over the LAN rather than Tailscale.</summary>
    public bool ViaLan => _viaLan;
    private volatile bool _viaLan;

    private static readonly TimeSpan PrimaryCheckInterval = TimeSpan.FromSeconds(30);

    /// <summary>Whether a certificate is the pinned one: SHA-256 of its DER bytes, lowercase hex.</summary>
    public static bool Pinned(byte[] der, string sha256) =>
        string.Equals(Convert.ToHexStringLower(SHA256.HashData(der)), sha256, StringComparison.Ordinal);

    /// <summary>A JSON message from the server, e.g. the gamepad status (app/peer_control.py). Raised on the socket's thread.</summary>
    public event Action<JsonElement>? MessageReceived;
    /// <summary>The socket closed or dropped.</summary>
    public event Action? Disconnected;

    private ClientWebSocket? _socket;
    private readonly SemaphoreSlim _sendLock = new(1, 1);

    /// <summary>Sends a JSON message; false if the socket isn't open.</summary>
    public async Task<bool> SendAsync(object message)
    {
        var socket = _socket;
        if (socket is null || socket.State != WebSocketState.Open)
        {
            return false;
        }
        await _sendLock.WaitAsync();
        try
        {
            await socket.SendAsync(JsonSerializer.SerializeToUtf8Bytes(message), WebSocketMessageType.Text, true, CancellationToken.None);
            return true;
        }
        catch (Exception ex) when (ex is WebSocketException or ObjectDisposedException or InvalidOperationException)
        {
            return false;
        }
        finally
        {
            _sendLock.Release();
        }
    }

    private async Task RunLoopAsync(Uri uri, CancellationToken token)
    {
        var delay = TimeSpan.FromSeconds(2);
        var buffer = new byte[4096];
        var message = new MemoryStream();
        var primaryFailures = 0;
        while (!token.IsCancellationRequested)
        {
            var lan = primaryFailures >= 2 ? LanEndpoint?.Invoke() : null;
            var primaryBack = false;
            using var watch = CancellationTokenSource.CreateLinkedTokenSource(token);
            try
            {
                using var socket = new ClientWebSocket();
                if (lan is not null)
                {
                    socket.Options.SetRequestHeader("X-Bragi-Lan-Token", lan.Token);
                    var pin = lan.CertSha256;
                    socket.Options.RemoteCertificateValidationCallback = (_, cert, _, _) =>
                        cert is not null && Pinned(cert.GetRawCertData(), pin);
                }
                // Without a ping timeout a dead hub isn't noticed: on Linux
                // the kernel retries an unanswered connection for about 15
                // minutes, so the LAN fallback (LanFallbackService) never
                // started when the hub's Tailscale went away. With these
                // the socket fails within about 15 seconds.
                socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(5);
                socket.Options.KeepAliveTimeout = TimeSpan.FromSeconds(10);
                // A connect started while the hub was unreachable could hang
                // for good (TCP connected, TLS or the upgrade never answered),
                // and the loop never retried: after a LAN fallback test no
                // tray app came back until it was restarted. Give each try
                // 10 s.
                using (var connectTimeout = CancellationTokenSource.CreateLinkedTokenSource(token))
                {
                    connectTimeout.CancelAfter(TimeSpan.FromSeconds(10));
                    try
                    {
                        await socket.ConnectAsync(lan?.Uri ?? uri, connectTimeout.Token);
                    }
                    catch (OperationCanceledException) when (!token.IsCancellationRequested)
                    {
                        throw new WebSocketException("Connecting to the hub timed out");
                    }
                }
                _socket = socket;
                _viaLan = lan is not null;
                delay = TimeSpan.FromSeconds(2); // reset backoff after a successful connect
                if (lan is null)
                {
                    primaryFailures = 0;
                }
                else
                {
                    Console.Error.WriteLine($"Bragi: the hub isn't reachable over Tailscale; connected over the LAN ({lan.Uri.Host})");
                    _ = WatchPrimaryAsync(uri, () => { primaryBack = true; socket.Abort(); }, watch.Token);
                }
                while (!token.IsCancellationRequested && socket.State == WebSocketState.Open)
                {
                    // The connection's lifetime is the heartbeat; messages
                    // are the gamepad status and input (app/gamepad.py).
                    var result = await socket.ReceiveAsync(buffer, token);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        break;
                    }
                    message.Write(buffer, 0, result.Count);
                    if (!result.EndOfMessage)
                    {
                        continue;
                    }
                    Deliver(message.ToArray());
                    message.SetLength(0);
                }
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested)
            {
                break;
            }
            catch
            {
                if (lan is null)
                {
                    primaryFailures++;
                }
                else if (!primaryBack)
                {
                    // The LAN didn't work either: Tailscale gets the next try.
                    primaryFailures = 1;
                }
                // Connection failed/dropped, timed out included. An aborted
                // socket (keepalive timeout) can throw OperationCanceledException
                // without our token being cancelled; before 0.1.32 that ended
                // this loop for good, and the tray never reconnected to the hub.
                // Fall through to the retry delay below.
            }
            finally
            {
                watch.Cancel();
                if (primaryBack)
                {
                    primaryFailures = 0;
                    Console.Error.WriteLine("Bragi: the hub answers over Tailscale again; moving back");
                }
                _socket = null;
                _viaLan = false;
                message.SetLength(0);
                Disconnected?.Invoke();
            }

            try
            {
                await Task.Delay(delay, token);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            delay = TimeSpan.FromSeconds(Math.Min(delay.TotalSeconds * 2, 30));
        }
    }

    /// <summary>While on the LAN: calls back() once the hub's Tailscale
    /// address answers HTTP at all (any status), checked every 30 s.</summary>
    private static async Task WatchPrimaryAsync(Uri primary, Action back, CancellationToken token)
    {
        var probe = new UriBuilder(primary) { Scheme = primary.Scheme == "ws" ? "http" : "https", Path = "/" };
        if (primary.IsDefaultPort)
        {
            probe.Port = -1;
        }
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        while (!token.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(PrimaryCheckInterval, token);
                using var response = await http.GetAsync(probe.Uri, HttpCompletionOption.ResponseHeadersRead, token);
                back();
                return;
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested)
            {
                return;
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
            {
                // Still not reachable over Tailscale.
            }
        }
    }

    private void Deliver(byte[] json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            MessageReceived?.Invoke(doc.RootElement);
        }
        catch (JsonException)
        {
            // Not JSON: nothing this client understands.
        }
    }

    public void Dispose() => Stop();
}
