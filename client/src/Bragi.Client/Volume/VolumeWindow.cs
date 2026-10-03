using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;

namespace Bragi.Client.Volume;

public sealed class VolumeWindow : Window
{
    private readonly string? _peerName;
    private readonly WrapPanel _devices = new() { Orientation = Orientation.Horizontal };
    private readonly Dictionary<(string, string), Border> _cards = new();
    private readonly VolumeConnection _connection;
    private readonly VolumeState _state = new();
    private readonly TextBlock _status = new() { Text = "Connecting to Bragi..." };
    private readonly Dictionary<(string Target, string Key, string Direction), Strip> _strips = new();
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(100) };
    private bool _connected;
    private bool _closed;
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

    public VolumeWindow(Uri uri, string? peerName)
    {
        _peerName = peerName;
        Title = "Bragi - Volume";
        Width = 800;
        Height = 620;
        MinWidth = 420;
        MinHeight = 320;
        var panel = new DockPanel { Margin = new Thickness(16) };
        var header = new StackPanel { Spacing = 4, Margin = new Thickness(8, 0, 8, 16) };
        header.Children.Add(new TextBlock { Text = "Volume", FontSize = 24, FontWeight = FontWeight.SemiBold });
        header.Children.Add(_status);
        DockPanel.SetDock(header, Dock.Top);
        panel.Children.Add(header);
        panel.Children.Add(new ScrollViewer
        {
            Content = _devices,
            HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,
        });
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
        });
        _timer.Tick += async (_, _) =>
        {
            foreach (var (control, strip) in _strips.ToArray())
            {
                if (strip.Pending is not { } value) continue;
                strip.Pending = null;
                if (!await _connection.SendAsync(new { action = "set_volume", target = control.Target, key = control.Key, direction = control.Direction, value, ts = strip.LastSent }))
                    _status.Text = "Volume was not sent. Waiting for Bragi...";
            }
        };
        Opened += (_, _) =>
        {
            _timer.Start();
            _connection.Start();
        };
        Closed += (_, _) => { _closed = true; _timer.Stop(); _connection.Dispose(); };
        Render();
    }

    private void AddStrip(StackPanel panel, VolumeDevice device, string label, string direction, string description)
    {
        var strip = new Strip();
        _strips.Add((device.Target, device.Key, direction), strip);
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
            if (!await _connection.SendAsync(new { action = "toggle_mute", target = device.Target, key = device.Key, direction }))
                _status.Text = "Mute was not sent. Waiting for Bragi...";
        };
    }

    private void SyncDevices()
    {
        var devices = _state.Devices.OrderBy(d => d.Target == "peer" && d.Key == _peerName ? 0 : 1).ToArray();
        var ids = devices.Select(d => (d.Target, d.Key)).ToHashSet();
        foreach (var id in _cards.Keys.Where(id => !ids.Contains(id)).ToArray())
        {
            _devices.Children.Remove(_cards[id]);
            _cards.Remove(id);
            foreach (var control in _strips.Keys.Where(c => (c.Target, c.Key) == id).ToArray())
                _strips.Remove(control);
        }
        foreach (var device in devices)
        {
            var id = (device.Target, device.Key);
            if (_cards.ContainsKey(id)) continue;
            var panel = new StackPanel { Spacing = 8 };
            panel.Children.Add(new TextBlock
            {
                Text = device.Label, FontSize = 18, FontWeight = FontWeight.SemiBold,
                TextWrapping = TextWrapping.Wrap,
            });
            panel.Children.Add(new TextBlock
            {
                Text = device.Target == "peer" && device.Key == _peerName ? "This device" : device.Target == "headset" ? "Headset" : "Peer",
                Opacity = 0.65,
            });
            AddStrip(panel, device, "Output", device.OutputDirection, device.Target == "peer" ? "Audio sent to the headset" : "Headset speakers");
            AddStrip(panel, device, "Input", device.InputDirection, device.Target == "peer" ? "Headset microphone received by this device" : "Headset microphone");
            var card = new Border
            {
                Width = 350, Margin = new Thickness(8), Padding = new Thickness(16),
                BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8),
                BorderBrush = new SolidColorBrush(Color.FromArgb(70, 128, 128, 128)),
                Child = panel,
            };
            _cards.Add(id, card);
        }
        // Preserve existing controls and focus when the device list changes.
        for (var i = 0; i < devices.Length; i++)
        {
            var card = _cards[(devices[i].Target, devices[i].Key)];
            if (_devices.Children.Count > i && _devices.Children[i] == card) continue;
            _devices.Children.Remove(card);
            _devices.Children.Insert(i, card);
        }
    }

    private void OnMessage(JsonElement message)
    {
        if (_closed) return;
        if (message.GetProperty("type").GetString() == "control" &&
            _strips.TryGetValue((message.GetProperty("target").GetString()!, message.GetProperty("key").GetString()!,
                message.GetProperty("direction").GetString()!), out var strip))
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
        SyncDevices();
        if (_connected) _status.Text = _cards.Count == 0 ? "No devices available in Bragi." : "Connected";
        foreach (var (control, strip) in _strips)
        {
            var device = _state.Find(control.Target, control.Key);
            var value = device is null ? null : control.Direction == device.OutputDirection ? device.Output : device.Input;
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
