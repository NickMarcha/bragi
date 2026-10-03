using System;
using System.Collections.Generic;
using System.Text.Json;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Threading;

namespace Bragi.Client.Volume;

public sealed class VolumeWindow : Window
{
    private readonly string _target;
    private readonly string _key;
    private readonly VolumeConnection _connection;
    private readonly VolumeState _state = new();
    private readonly TextBlock _status = new() { Text = "Connecting to Bragi..." };
    private readonly Dictionary<string, Strip> _strips = new();
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(100) };
    private bool _connected;
    private bool _closed;
    private bool? _initialFocus;
    private double _timestamp;

    private sealed class Strip
    {
        public Slider Slider { get; } = new() { Minimum = 0, Maximum = 150, TickFrequency = 1, IsSnapToTickEnabled = true };
        public TextBlock Readout { get; } = new();
        public Button Mute { get; } = new() { Content = "Mute" };
        public bool Rendering;
        public double? Pending;
        public bool AwaitingVolume;
        public double LastSent;
    }

    public VolumeWindow(Uri uri, string target, string key, string label, bool focusInput)
    {
        _target = target;
        _key = key;
        _initialFocus = focusInput;
        Title = $"Volume - {label}";
        Width = 380;
        SizeToContent = SizeToContent.Height;
        CanResize = false;
        var panel = new StackPanel { Margin = new Thickness(20), Spacing = 14 };
        panel.Children.Add(new TextBlock { Text = label, FontSize = 20 });
        panel.Children.Add(_status);
        var output = target == "peer" ? "incoming" : "playback";
        var input = target == "peer" ? "outgoing" : "capture";
        AddStrip(panel, "Output", output, target == "peer" ? "Audio sent to the headset" : "Headset speakers");
        AddStrip(panel, "Input", input, target == "peer" ? "Headset microphone received by this device" : "Headset microphone");
        Content = panel;

        _connection = new VolumeConnection(uri);
        _connection.Message += message => Dispatcher.UIThread.Post(() => OnMessage(message));
        _connection.Status += status => Dispatcher.UIThread.Post(() =>
        {
            if (_closed) return;
            _connected = status == "Connected";
            _status.Text = status;
            if (!_connected)
                foreach (var strip in _strips.Values) { strip.Pending = null; strip.AwaitingVolume = false; strip.LastSent = 0; }
            Render();
            if (_connected && _initialFocus is { } inputFocus)
            {
                FocusDirection(inputFocus);
                _initialFocus = null;
            }
        });
        _timer.Tick += async (_, _) =>
        {
            foreach (var (direction, strip) in _strips)
            {
                if (strip.Pending is not { } value) continue;
                strip.Pending = null;
                if (!await _connection.SendAsync(new { action = "set_volume", target, key, direction, value, ts = strip.LastSent }))
                    _status.Text = "Volume was not sent. Waiting for Bragi...";
            }
        };
        Opened += (_, _) =>
        {
            _timer.Start();
            _connection.Start();
            FocusDirection(focusInput);
        };
        Closed += (_, _) => { _closed = true; _timer.Stop(); _connection.Dispose(); };
        Render();
    }

    private void AddStrip(StackPanel panel, string label, string direction, string description)
    {
        var strip = new Strip();
        _strips.Add(direction, strip);
        panel.Children.Add(new TextBlock { Text = label, FontSize = 16 });
        panel.Children.Add(new TextBlock { Text = description, FontSize = 12 });
        panel.Children.Add(strip.Slider);
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 16 };
        row.Children.Add(strip.Readout);
        row.Children.Add(strip.Mute);
        panel.Children.Add(row);
        strip.Slider.ValueChanged += (_, _) =>
        {
            if (strip.Rendering || !_connected || !strip.Slider.IsEnabled) return;
            strip.Pending = strip.Slider.Value / 100;
            strip.AwaitingVolume = true;
            _timestamp = Math.Max(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), _timestamp + 0.01);
            strip.LastSent = _timestamp;
            strip.Readout.Text = $"{strip.Slider.Value:0}%";
        };
        strip.Mute.Click += async (_, _) =>
        {
            if (!await _connection.SendAsync(new { action = "toggle_mute", target = _target, key = _key, direction }))
                _status.Text = "Mute was not sent. Waiting for Bragi...";
        };
    }

    public void FocusDirection(bool input)
    {
        var direction = _target == "peer" ? (input ? "outgoing" : "incoming") : (input ? "capture" : "playback");
        _strips[direction].Slider.Focus();
    }

    private void OnMessage(JsonElement message)
    {
        if (_closed) return;
        if (message.GetProperty("type").GetString() == "control" &&
            message.GetProperty("target").GetString() == _target && message.GetProperty("key").GetString() == _key &&
            _strips.TryGetValue(message.GetProperty("direction").GetString()!, out var strip))
        {
            if (message.TryGetProperty("ts", out var ts) && ts.ValueKind == JsonValueKind.Number)
            {
                if (ts.GetDouble() < strip.LastSent) return;
                strip.AwaitingVolume = false;
            }
        }
        // Meter frames are frequent and don't affect this window.
        var type = message.GetProperty("type").GetString();
        if (type is not ("state" or "control" or "headset")) return;
        _state.Apply(message);
        Render();
    }

    private void Render()
    {
        var device = _state.Find(_target, _key);
        if (_connected) _status.Text = device is null ? "Device is no longer available in Bragi." : "Connected";
        foreach (var (direction, strip) in _strips)
        {
            var value = device is null ? null : direction == device.OutputDirection ? device.Output : device.Input;
            var enabled = _connected && value is { Connected: true, Volume: not null };
            strip.Slider.IsEnabled = enabled;
            strip.Mute.IsEnabled = enabled;
            if (!enabled) { strip.Pending = null; strip.AwaitingVolume = false; }
            strip.Mute.Content = value?.Muted == true ? "Unmute" : "Mute";
            if (strip.Pending is not null || strip.AwaitingVolume) continue;
            strip.Rendering = true;
            if (value?.Volume is { } volume) strip.Slider.Value = volume * 100;
            strip.Readout.Text = enabled ? $"{strip.Slider.Value:0}%" : "Unavailable";
            strip.Rendering = false;
        }
    }
}
