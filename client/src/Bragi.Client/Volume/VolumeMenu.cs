using System;
using System.Diagnostics;
using Avalonia;
using Avalonia.Controls;
using Bragi.Client.Config;

namespace Bragi.Client.Volume;

/// <summary>Two direct tray actions, with one shared volume window for every device.</summary>
public sealed class VolumeMenu : IDisposable
{
    private readonly BragiServerAddress? _server;
    private readonly string? _peerName;
    private VolumeWindow? _window;
    public NativeMenuItem Item { get; }
    public NativeMenuItem WebUiItem { get; }

    public VolumeMenu(RocLinkConfig? config)
    {
        _server = BragiServerAddress.FromPresenceUrl(config?.BragiWsBaseUrl);
        _peerName = config?.PeerName;
        Item = new NativeMenuItem { Header = "Volume", IsEnabled = _server is not null };
        Item.Click += (_, _) => Open();
        WebUiItem = new NativeMenuItem { Header = "Open web UI", IsEnabled = _server is not null };
        WebUiItem.Click += (_, _) => OpenWebUi();
    }

    public void Open()
    {
        if (_server is null) return;
        if (_window is not null)
        {
            _window.Show();
            _window.WindowState = WindowState.Normal;
            _window.Activate();
            return;
        }
        _window = new VolumeWindow(_server.Controls, _peerName);
        _window.Closed += (_, _) => _window = null;
        _window.Show();
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
        _window?.Close();
    }
}
