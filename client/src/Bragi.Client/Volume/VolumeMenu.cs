using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Threading;
using Bragi.Client.Config;

namespace Bragi.Client.Volume;

/// <summary>Owns the volume menu and at most one window per device.</summary>
public sealed class VolumeMenu : IDisposable
{
    private readonly BragiServerAddress? _server;
    private readonly string? _peerName;
    private readonly NativeMenu _otherDevices = new();
    private readonly Dictionary<(string, string), VolumeWindow> _windows = new();
    private readonly CancellationTokenSource _stop = new();
    private bool _refreshing;
    private DateTime _lastRefresh = DateTime.MinValue;
    private string? _catalog;
    public NativeMenuItem Item { get; }
    public NativeMenuItem WebUiItem { get; }

    public VolumeMenu(RocLinkConfig? config)
    {
        _server = BragiServerAddress.FromPresenceUrl(config?.BragiWsBaseUrl);
        _peerName = config?.PeerName;
        var menu = new NativeMenu();
        Item = new NativeMenuItem { Header = "Volume", Menu = menu };
        menu.Add(new NativeMenuItem { Header = "This device", IsEnabled = false });
        menu.Add(DirectionItem("Output", "peer", _peerName, "This device", false));
        menu.Add(DirectionItem("Input", "peer", _peerName, "This device", true));
        menu.Add(new NativeMenuItemSeparator());
        menu.Add(new NativeMenuItem { Header = "Other devices", Menu = _otherDevices });
        _otherDevices.Add(new NativeMenuItem { Header = "Loading devices...", IsEnabled = false });
        menu.NeedsUpdate += (_, _) => Refresh();
        _otherDevices.NeedsUpdate += (_, _) => Refresh();
        if (_server is null)
        {
            _otherDevices.Items.Clear();
            _otherDevices.Add(new NativeMenuItem { Header = "Bragi server is not configured", IsEnabled = false });
        }
        else if (_peerName is null)
        {
            menu.Items.Insert(3, new NativeMenuItem { Header = "This device's peer name is not configured", IsEnabled = false });
        }
        WebUiItem = new NativeMenuItem { Header = "Open web UI", IsEnabled = _server is not null };
        WebUiItem.Click += (_, _) => OpenWebUi();
        Refresh();
    }

    private NativeMenuItem DirectionItem(string label, string target, string? key, string deviceLabel, bool input)
    {
        var item = new NativeMenuItem { Header = label, IsEnabled = _server is not null && key is not null };
        item.Click += (_, _) =>
        {
            if (_server is null || key is null) return;
            if (_windows.TryGetValue((target, key), out var existing))
            {
                existing.Show();
                existing.WindowState = WindowState.Normal;
                existing.Activate();
                existing.FocusDirection(input);
                return;
            }
            var window = new VolumeWindow(_server.Controls, target, key, deviceLabel, input);
            _windows[(target, key)] = window;
            window.Closed += (_, _) => _windows.Remove((target, key));
            window.Show();
        };
        return item;
    }

    // Called from the root tray menu as well: some desktop shells don't
    // emit a submenu update event until after displaying cached items.
    public void Refresh()
    {
        if (_server is null || _refreshing || _stop.IsCancellationRequested || DateTime.UtcNow - _lastRefresh < TimeSpan.FromSeconds(2)) return;
        _refreshing = true;
        _ = RefreshAsync();
    }

    private async Task RefreshAsync()
    {
        try
        {
            var state = await VolumeConnection.ReadSnapshotAsync(_server!.Controls, _stop.Token);
            var devices = state.Devices.Where(d => d.Target != "peer" || d.Key != _peerName).ToArray();
            var catalog = System.Text.Json.JsonSerializer.Serialize(devices.Select(d => new { d.Target, d.Key, d.Label }));
            await Dispatcher.UIThread.InvokeAsync(() =>
            {
                if (_stop.IsCancellationRequested || catalog == _catalog) return;
                _catalog = catalog;
                _otherDevices.Items.Clear();
                foreach (var device in devices)
                {
                    var submenu = new NativeMenu
                    {
                        DirectionItem("Output", device.Target, device.Key, device.Label, false),
                        DirectionItem("Input", device.Target, device.Key, device.Label, true),
                    };
                    _otherDevices.Add(new NativeMenuItem { Header = device.Label, Menu = submenu });
                }
                if (devices.Length == 0) _otherDevices.Add(new NativeMenuItem { Header = "No other devices", IsEnabled = false });
            });
        }
        catch (Exception)
        {
            await Dispatcher.UIThread.InvokeAsync(() =>
            {
                if (_stop.IsCancellationRequested) return;
                _catalog = null;
                _otherDevices.Items.Clear();
                _otherDevices.Add(new NativeMenuItem { Header = "Bragi unavailable. Reopen to retry.", IsEnabled = false });
            });
        }
        finally
        {
            await Dispatcher.UIThread.InvokeAsync(() => { _refreshing = false; _lastRefresh = DateTime.UtcNow; });
        }
    }

    private void OpenWebUi()
    {
        if (_server is null) return;
        try { Process.Start(new ProcessStartInfo(_server.WebUi.AbsoluteUri) { UseShellExecute = true }); }
        catch (Exception)
        {
            new Window
            {
                Title = "Open Bragi web UI", Width = 440, SizeToContent = SizeToContent.Height,
                Content = new TextBox { Text = _server.WebUi.AbsoluteUri, IsReadOnly = true, Margin = new Thickness(20) },
            }.Show();
        }
    }

    public void Dispose()
    {
        _stop.Cancel();
        foreach (var window in _windows.Values.ToArray()) window.Close();
    }
}
