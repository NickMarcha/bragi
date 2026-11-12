using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using Avalonia;
using Avalonia.Automation;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Controls.Shapes;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform;
using Avalonia.Styling;
using Avalonia.Threading;

namespace Bragi.Client.Volume;

/// <summary>
/// Every device's volumes as the dashboard's console: a toolbar, then a wrapping row of
/// devices with two vertical faders each, and devices with nothing to mix folded into a
/// stack at the end. The look lives in VolumeStyles.axaml.
/// </summary>
public sealed class VolumeWindow : Window
{
    // A device's width comes from its two strips, as on the dashboard.
    private const double StripWidth = 56, StripGap = 6, ThumbHeight = 22;
    private const double DeviceWidth = 2 * StripWidth + StripGap + 2 * 8 + 2, DeviceMargin = 4;
    // Everything in a device but its faders: padding, name, meta, label, mute and gaps.
    private const double DeviceChrome = 165;
    // A parked device's header, with a one-line name.
    private const double ParkedHeight = 56;

    private readonly string? _peerName;
    private readonly WrapPanel _devices = new() { Orientation = Orientation.Horizontal, Margin = new Thickness(8) };
    private readonly StackPanel _parked = new() { Spacing = 8, Margin = new Thickness(DeviceMargin), Classes = { "parked" } };
    private readonly ScrollViewer _scroll;
    private readonly Dictionary<(string, string), Card> _cards = new();
    private readonly VolumeConnection _connection;
    private readonly VolumeState _state = new();
    private readonly TextBlock _status = new() { Text = "Connecting to Bragi...", Classes = { "connection" } };
    private readonly Border _statusDot = new() { Classes = { "dot", "partial" } };
    private readonly TextBlock _empty = new() { Text = "No devices in Bragi.", Classes = { "empty" }, IsVisible = false };
    private readonly Dictionary<(string Target, string Key, string Direction), Strip> _strips = new();
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(100) };
    private double _faderHeight = 300;
    private bool _connected;
    private bool _closed;
    private double _timestamp;

    private sealed class Strip
    {
        public Slider Slider { get; } = new()
        {
            Minimum = 0, Maximum = 150, TickFrequency = 1, IsSnapToTickEnabled = true, Classes = { "fader" },
        };
        public Run Name { get; } = new();
        public Run Off { get; } = new() { Foreground = new SolidColorBrush(Color.Parse("#d0525a")) };
        public Button Mute { get; } = new() { Classes = { "mute" } };
        public string MuteTip = "Mute";
        public bool Rendering;
        public double? Pending;
        public bool AwaitingVolume;
        public double LastSent;
    }

    private sealed class Card
    {
        public Border Root { get; } = new() { Width = DeviceWidth, Classes = { "device" } };
        public Border Dot { get; } = new() { Classes = { "dot" }, Margin = new Thickness(0, 5, 0, 0), VerticalAlignment = VerticalAlignment.Top };
        public TextBlock Name { get; } = new() { Classes = { "device-name" } };
        public TextBlock Meta { get; } = new() { Classes = { "device-meta" } };
        public Grid Strips { get; } = new() { ColumnDefinitions = new ColumnDefinitions($"{StripWidth},{StripGap},{StripWidth}") };
    }

    public VolumeWindow(Uri uri, string? peerName)
    {
        _peerName = peerName;
        Title = "Bragi - Volume";
        Width = 800;
        Height = 620;
        MinWidth = 420;
        MinHeight = 320;
        RequestedThemeVariant = ThemeVariant.Dark;
        Styles.Add(new VolumeStyles());
        Classes.Add("bragi");

        var bar = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,Auto,*,Auto,Auto") };
        bar.Children.Add(new Image
        {
            Source = new Bitmap(AssetLoader.Open(new Uri("avares://Bragi.Client/Assets/tray-enabled.png"))),
            Width = 26, Height = 26, Margin = new Thickness(0, 0, 8, 0),
        });
        AddAt(bar, new TextBlock { Text = "Bragi", Classes = { "brand" } }, 1);
        AddAt(bar, _statusDot, 3);
        _statusDot.Margin = new Thickness(0, 0, 6, 0);
        AddAt(bar, _status, 4);
        var top = new Border { Child = bar, Classes = { "top-bar" } };
        DockPanel.SetDock(top, Dock.Top);

        _devices.Children.Add(_parked);
        _scroll = new ScrollViewer
        {
            Content = _devices,
            HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,
        };
        _scroll.SizeChanged += (_, _) => FitFaders();
        var console = new Panel();
        console.Children.Add(_scroll);
        console.Children.Add(_empty);
        var panel = new DockPanel();
        panel.Children.Add(top);
        panel.Children.Add(console);
        Content = panel;

        _connection = new VolumeConnection(uri);
        _connection.Message += message => Dispatcher.UIThread.Post(() => OnMessage(message));
        _connection.Status += status => Dispatcher.UIThread.Post(() =>
        {
            if (_closed) return;
            _connected = status == "Connected";
            SetStatus(status, _connected ? "online" : status.StartsWith("Connecting") ? "partial" : "offline");
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
                    SetStatus("Volume was not sent. Waiting for Bragi...", "partial");
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

    private static void AddAt(Grid grid, Control control, int column)
    {
        Grid.SetColumn(control, column);
        control.VerticalAlignment = VerticalAlignment.Center;
        grid.Children.Add(control);
    }

    private void SetStatus(string text, string state)
    {
        _status.Text = text;
        _statusDot.Classes.Set("online", state == "online");
        _statusDot.Classes.Set("partial", state == "partial");
        _statusDot.Classes.Set("offline", state == "offline");
    }

    private bool IsThisDevice(VolumeDevice device) => device.Target == "peer" && device.Key == _peerName;

    private Card NewCard(VolumeDevice device)
    {
        var card = new Card();
        var head = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*") };
        head.Children.Add(card.Dot);
        card.Dot.Margin = new Thickness(0, 5, 6, 0);
        Grid.SetColumn(card.Name, 1);
        head.Children.Add(card.Name);
        card.Name.Text = device.Label;
        ToolTip.SetTip(card.Name, device.Label);

        // Strips in the dashboard's order: a peer's Mic before its Audio, a headset's
        // Speakers before its Mic.
        var directions = device.Target == "peer"
            ? new[] { device.InputDirection, device.OutputDirection }
            : new[] { device.OutputDirection, device.InputDirection };
        for (var i = 0; i < 2; i++)
        {
            var strip = AddStrip(device, directions[i]);
            Grid.SetColumn(strip, i * 2);
            card.Strips.Children.Add(strip);
        }

        var body = new StackPanel { Spacing = 6 };
        body.Children.Add(head);
        body.Children.Add(card.Meta);
        body.Children.Add(card.Strips);
        card.Root.Child = body;
        return card;
    }

    private Control AddStrip(VolumeDevice device, string direction)
    {
        var strip = new Strip();
        _strips.Add((device.Target, device.Key, direction), strip);
        var mic = direction is "outgoing" or "capture";
        strip.Mute.Content = MuteIcon(mic);
        AutomationProperties.SetAutomationId(strip.Slider, $"{direction}-volume");
        AutomationProperties.SetAutomationId(strip.Mute, $"{direction}-mute");
        strip.MuteTip = device.Target == "headset" ? (mic ? "Mute mic" : "Mute speakers") : "Mute";

        var label = new TextBlock { Classes = { "strip-label" }, Inlines = new InlineCollection { strip.Name, strip.Off } };
        var panel = new StackPanel { Spacing = 6 };
        panel.Children.Add(label);
        panel.Children.Add(strip.Slider);
        panel.Children.Add(strip.Mute);
        ToolTip.SetTip(label, device.Target == "headset"
            ? (mic ? "Headset microphone" : "Headset speakers")
            : direction == "outgoing" ? $"Headset → {device.Label}" : $"{device.Label} → headset");

        strip.Slider.ValueChanged += (_, _) =>
        {
            if (strip.Rendering || !_connected || !strip.Slider.IsEnabled) return;
            strip.Pending = strip.Slider.Value / 100;
            strip.AwaitingVolume = true;
            _timestamp = Math.Max(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), _timestamp + 0.01);
            strip.LastSent = _timestamp;
        };
        strip.Mute.Click += async (_, _) =>
        {
            if (!await _connection.SendAsync(new { action = "toggle_mute", target = device.Target, key = device.Key, direction }))
                SetStatus("Mute was not sent. Waiting for Bragi...", "partial");
        };
        return panel;
    }

    // The dashboard's mic_mute and speaker_mute icons from _icons.html.
    private static Control MuteIcon(bool mic)
    {
        var canvas = new Canvas { Width = 24, Height = 24 };
        void Add(string data, string cls) =>
            canvas.Children.Add(new Path { Data = Geometry.Parse(data), Classes = { cls } });
        if (mic)
        {
            Add("M9,5 A3,3 0 0 1 15,5 L15,9 A3,3 0 0 1 9,9 Z", "glyph");
            Add("M5,11 A7,7 0 0 0 19,11", "glyph");
            Add("M12,18 L12,21", "glyph");
            Add("M3,3 L21,21", "glyph");
        }
        else
        {
            Add("M4,9 L8,9 L12,5 L12,19 L8,15 L4,15 Z", "glyph-fill");
            Add("M16,9 L22,15 M22,9 L16,15", "glyph");
        }
        return new Viewbox { Width = 13, Height = 13, Child = canvas };
    }

    private void SyncDevices()
    {
        // This device first, then the dashboard's order.
        var devices = _state.Devices.OrderBy(d => IsThisDevice(d) ? 0 : 1).ToArray();
        var ids = devices.Select(d => (d.Target, d.Key)).ToHashSet();
        foreach (var id in _cards.Keys.Where(id => !ids.Contains(id)).ToArray())
        {
            (_cards[id].Root.Parent as Panel)?.Children.Remove(_cards[id].Root);
            _cards.Remove(id);
            foreach (var control in _strips.Keys.Where(c => (c.Target, c.Key) == id).ToArray())
                _strips.Remove(control);
        }
        foreach (var device in devices)
            if (!_cards.ContainsKey((device.Target, device.Key)))
                _cards.Add((device.Target, device.Key), NewCard(device));

        Arrange(_devices, devices.Where(d => !d.Parked).Select(d => (Control)_cards[(d.Target, d.Key)].Root).Append(_parked).ToList());
        Arrange(_parked, devices.Where(d => d.Parked).Select(d => (Control)_cards[(d.Target, d.Key)].Root).ToList());
        _empty.IsVisible = _connected && devices.Length == 0;
    }

    // Moves only what's out of place, so existing controls keep focus and drag state.
    private static void Arrange(Panel panel, List<Control> wanted)
    {
        foreach (var child in panel.Children.Where(c => !wanted.Contains(c)).ToArray())
            panel.Children.Remove(child);
        for (var i = 0; i < wanted.Count; i++)
        {
            if (panel.Children.Count > i && panel.Children[i] == wanted[i]) continue;
            (wanted[i].Parent as Panel)?.Children.Remove(wanted[i]);
            panel.Children.Insert(i, wanted[i]);
        }
    }

    // Faders take each row's share of the window's height, like the dashboard's
    // --fader-height: rows of devices wrap rather than scroll sideways.
    private void FitFaders()
    {
        var width = _scroll.Bounds.Width - 16;
        var height = _scroll.Bounds.Height - 16;
        if (width <= 0 || height <= 0) return;
        var active = _devices.Children.Count - 1;
        var perRow = Math.Max(1, (int)(width / (DeviceWidth + 2 * DeviceMargin)));
        var rows = Math.Max(1, (int)Math.Ceiling(active / (double)perRow));
        // The parked stack is only headers: when it wraps onto a line of its own,
        // that line takes the stack's height, not a full row's share.
        if (_parked.Children.Count > 0 && active % perRow == 0)
            height -= _parked.Children.Count * ParkedHeight + 8 * (_parked.Children.Count - 1) + 2 * DeviceMargin;
        var fader = Math.Clamp(Math.Floor(height / rows - DeviceChrome), 110, 420);
        if (fader == _faderHeight) return;
        _faderHeight = fader;
        foreach (var strip in _strips.Values) strip.Slider.Height = fader + ThumbHeight;
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
        if (type is not ("state" or "control" or "headset" or "order" or "peer_presence")) return;
        _state.Apply(message);
        Render();
    }

    private void Render()
    {
        SyncDevices();
        if (_connected) SetStatus("Connected", "online");
        foreach (var (id, card) in _cards)
        {
            if (_state.Find(id.Item1, id.Item2) is not { } device) continue;
            var status = device.Status.ToString().ToLowerInvariant();
            foreach (var name in new[] { "online", "partial", "offline", "disabled" })
                card.Dot.Classes.Set(name, name == status);
            card.Root.Classes.Set("disabled", device.Status == DeviceStatus.Disabled);
            card.Meta.Text = IsThisDevice(device) ? "This device" : device.Meta;
            ToolTip.SetTip(card.Meta, IsThisDevice(device) ? $"This device · {device.Meta}" : device.Meta);
            card.Strips.IsVisible = !device.Parked;
        }
        foreach (var (control, strip) in _strips)
        {
            var device = _state.Find(control.Target, control.Key);
            var output = device is not null && control.Direction == device.OutputDirection;
            var value = device is null ? null : output ? device.Output : device.Input;
            if (device is not null) strip.Name.Text = output ? device.OutputLabel : device.InputLabel;
            strip.Off.Text = value?.Connected == true ? "" : " off";
            var enabled = _connected && value is { Connected: true, Volume: not null };
            strip.Slider.IsEnabled = enabled;
            strip.Slider.Height = _faderHeight + ThumbHeight;
            strip.Mute.IsEnabled = enabled;
            strip.Mute.Classes.Set("active", value?.Muted == true);
            ToolTip.SetTip(strip.Mute, value?.Muted == true ? "Muted" : strip.MuteTip);
            if (!enabled) { strip.Pending = null; strip.AwaitingVolume = false; }
            if (strip.Pending is not null || strip.AwaitingVolume) continue;
            strip.Rendering = true;
            if (value?.Volume is { } volume) strip.Slider.Value = volume * 100;
            strip.Rendering = false;
        }
        FitFaders();
    }
}
