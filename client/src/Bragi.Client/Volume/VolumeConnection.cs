using System;
using System.IO;
using System.Net.WebSockets;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Bragi.Client.Volume;

/// <summary>
/// A dashboard connection while a volume window is open. Menu discovery
/// uses a single snapshot, so an idle tray doesn't keep server meters running.
/// Events run on the receive task; UI subscribers dispatch to the UI thread.
/// </summary>
public sealed class VolumeConnection : IDisposable
{
    private readonly Uri _uri;
    private readonly CancellationTokenSource _stop = new();
    private readonly SemaphoreSlim _sendLock = new(1, 1);
    private volatile ClientWebSocket? _socket;
    private volatile bool _ready;
    private Task? _run;
    public event Action<JsonElement>? Message;
    public event Action<string>? Status;

    public VolumeConnection(Uri uri) => _uri = uri;
    public void Start() => _run ??= Task.Run(RunAsync);

    public static async Task<VolumeState> ReadSnapshotAsync(Uri uri, CancellationToken token)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(TimeSpan.FromSeconds(10));
        using var socket = new ClientWebSocket();
        await socket.ConnectAsync(uri, timeout.Token);
        var message = await ReceiveAsync(socket, timeout.Token);
        if (message.GetProperty("type").GetString() != "state") throw new IOException("Expected device state from Bragi.");
        var state = new VolumeState();
        state.Apply(message);
        return state;
    }

    private async Task RunAsync()
    {
        var delay = 1;
        while (!_stop.IsCancellationRequested)
        {
            try
            {
                Status?.Invoke("Connecting to Bragi...");
                using var socket = new ClientWebSocket();
                socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(15);
                socket.Options.KeepAliveTimeout = TimeSpan.FromSeconds(10);
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
                timeout.CancelAfter(TimeSpan.FromSeconds(10));
                await socket.ConnectAsync(_uri, timeout.Token);
                _socket = socket;
                var first = await ReceiveAsync(socket, timeout.Token);
                if (first.GetProperty("type").GetString() != "state") throw new IOException("Expected device state from Bragi.");
                _ready = true;
                Message?.Invoke(first);
                Status?.Invoke("Connected");
                delay = 1;
                while (!_stop.IsCancellationRequested)
                {
                    var message = await ReceiveAsync(socket, _stop.Token);
                    if (message.GetProperty("type").GetString() is "state" or "control" or "headset")
                        Message?.Invoke(message);
                }
            }
            catch (Exception ex) when (ex is WebSocketException or IOException or JsonException or OperationCanceledException)
            {
                // No commands survive a disconnect: replaying a stale volume
                // or mute toggle after reconnect could change the wrong state.
            }
            finally
            {
                _ready = false;
                _socket = null;
            }
            if (_stop.IsCancellationRequested) break;
            Status?.Invoke("Disconnected. Reconnecting...");
            try { await Task.Delay(TimeSpan.FromSeconds(delay), _stop.Token); }
            catch (OperationCanceledException) { break; }
            delay = Math.Min(delay * 2, 30);
        }
    }

    public async Task<bool> SendAsync(object action)
    {
        var connection = _socket;
        if (!_ready || connection is null || _stop.IsCancellationRequested) return false;
        try
        {
            await _sendLock.WaitAsync(_stop.Token);
            try
            {
                if (!_ready || !ReferenceEquals(connection, _socket) || connection.State != WebSocketState.Open) return false;
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
                timeout.CancelAfter(TimeSpan.FromSeconds(5));
                await connection.SendAsync(JsonSerializer.SerializeToUtf8Bytes(action), WebSocketMessageType.Text, true, timeout.Token);
                return true;
            }
            finally { _sendLock.Release(); }
        }
        catch (Exception ex) when (ex is WebSocketException or OperationCanceledException or ObjectDisposedException)
        {
            _ready = false;
            _socket?.Abort();
            return false;
        }
    }

    private static async Task<JsonElement> ReceiveAsync(ClientWebSocket socket, CancellationToken token)
    {
        var buffer = new byte[8192];
        using var data = new MemoryStream();
        WebSocketReceiveResult result;
        do
        {
            result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), token);
            if (result.MessageType != WebSocketMessageType.Text) throw new IOException("Bragi closed the connection.");
            if (data.Length + result.Count > 1024 * 1024) throw new IOException("Bragi message is too large.");
            data.Write(buffer, 0, result.Count);
        } while (!result.EndOfMessage);
        using var document = JsonDocument.Parse(data.ToArray());
        return document.RootElement.Clone();
    }

    public void Dispose()
    {
        _ready = false;
        _stop.Cancel();
        _socket?.Abort();
    }
}
