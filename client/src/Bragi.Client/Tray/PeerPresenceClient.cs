using System;
using System.IO;
using System.Net.WebSockets;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

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
        while (!token.IsCancellationRequested)
        {
            try
            {
                using var socket = new ClientWebSocket();
                // Without a ping timeout a dead hub isn't noticed: on Linux
                // the kernel retries an unanswered connection for about 15
                // minutes, so the LAN fallback (LanFallbackService) never
                // started when the hub's Tailscale went away. With these
                // the socket fails within about 15 seconds.
                socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(5);
                socket.Options.KeepAliveTimeout = TimeSpan.FromSeconds(10);
                await socket.ConnectAsync(uri, token);
                _socket = socket;
                delay = TimeSpan.FromSeconds(2); // reset backoff after a successful connect
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
            catch (OperationCanceledException)
            {
                break;
            }
            catch
            {
                // Connection failed/dropped - fall through to the retry delay below.
            }
            finally
            {
                _socket = null;
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
