using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Bragi.Client.Monitors;

/// <summary>
/// Tells the Bragi host this computer's monitors (app/monitors.py), for the
/// KVM layout: once after each connect, and again whenever they change (a
/// monitor plugged in or out, a new resolution, scaling or arrangement).
/// Monitors are read every few seconds; a change is only sent when what's
/// read differs from what was sent last.
/// </summary>
public sealed class MonitorReporter : IDisposable
{
    private readonly IMonitorSource? _source;
    private readonly TimeSpan _interval;
    private readonly CancellationTokenSource _stop = new();
    private readonly object _lock = new();
    private IReadOnlyList<MonitorInfo>? _sent;
    private bool _connected;
    private Task? _loop;

    public Func<object, Task<bool>>? Send { get; set; }

    /// <param name="source">This computer's monitors, or null where they can't be read.</param>
    public MonitorReporter(IMonitorSource? source, TimeSpan? interval = null)
    {
        _source = source;
        _interval = interval ?? TimeSpan.FromSeconds(3);
    }

    public void Start()
    {
        if (_source is not null && _loop is null)
        {
            _loop = Task.Run(LoopAsync);
        }
    }

    /// <summary>The host's first message after a connect ("kvm") means it's listening.</summary>
    public void OnMessage(JsonElement message)
    {
        if (message.TryGetProperty("type", out var type) && type.GetString() == "kvm")
        {
            bool first;
            lock (_lock)
            {
                first = !_connected;
                _connected = true;
                if (first)
                {
                    _sent = null;  // the host may have restarted: tell it again
                }
            }
            if (first)
            {
                _ = CheckAsync();
            }
        }
    }

    public void OnDisconnected()
    {
        lock (_lock)
        {
            _connected = false;
        }
    }

    private async Task LoopAsync()
    {
        while (!_stop.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(_interval, _stop.Token);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            await CheckAsync();
        }
    }

    /// <summary>Reads the monitors and sends them if they changed since the last send.</summary>
    public async Task CheckAsync()
    {
        if (_source is null || Send is null)
        {
            return;
        }
        IReadOnlyList<MonitorInfo> now;
        try
        {
            now = await _source.ReadAsync();
        }
        catch (Exception)
        {
            return;  // a failed read keeps what the host has
        }
        lock (_lock)
        {
            if (!_connected || (_sent is not null && _sent.SequenceEqual(now)))
            {
                return;
            }
        }
        if (await Send(MonitorMessage.Build(_source.Kind, now)))
        {
            lock (_lock)
            {
                _sent = now;
            }
        }
    }

    public void Dispose()
    {
        _stop.Cancel();
        _stop.Dispose();
    }
}
