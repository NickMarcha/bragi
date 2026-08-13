using System;
using System.Net.WebSockets;
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
/// Never sends anything meaningful (the server only reads to detect
/// disconnect) - this is a heartbeat purely by connection lifetime, not
/// message content.
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

    private static async Task RunLoopAsync(Uri uri, CancellationToken token)
    {
        var delay = TimeSpan.FromSeconds(2);
        var buffer = new byte[16];
        while (!token.IsCancellationRequested)
        {
            try
            {
                using var socket = new ClientWebSocket();
                await socket.ConnectAsync(uri, token);
                delay = TimeSpan.FromSeconds(2); // reset backoff after a successful connect
                while (!token.IsCancellationRequested && socket.State == WebSocketState.Open)
                {
                    // Blocks until the server closes it, the link disables
                    // (token cancelled), or the connection drops - never
                    // expects real message content.
                    await socket.ReceiveAsync(buffer, token);
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

    public void Dispose() => Stop();
}
