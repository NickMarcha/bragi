using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media.Imaging;
using Avalonia.Platform;
using Avalonia.Threading;

namespace Bragi.Client.Tray;

/// <summary>
/// Owns the single TrayIcon + its NativeMenu, and reflects LinkStatusService's
/// state onto it (icon, tooltip, status label, toggle item text/enabled).
/// Built entirely in code (not declared in App.axaml) so the icon/menu can
/// be updated dynamically without XAML name-scope lookups.
/// </summary>
public sealed class TrayIconManager : IDisposable
{
    private readonly LinkStatusService _statusService;
    private readonly TrayIcon _trayIcon;
    private readonly NativeMenuItem _statusItem;
    private readonly NativeMenuItem _toggleItem;
    private readonly NativeMenuItem _autostartItem;

    private readonly WindowIcon _disabledIcon = LoadIcon("tray-disabled.png");
    private readonly WindowIcon _enabledIcon = LoadIcon("tray-enabled.png");
    private readonly WindowIcon _errorIcon = LoadIcon("tray-error.png");

    public TrayIconManager(LinkStatusService statusService)
    {
        _statusService = statusService;

        _statusItem = new NativeMenuItem { Header = "Status: checking...", IsEnabled = false };
        _toggleItem = new NativeMenuItem { Header = "Enable Link", IsEnabled = false };
        _toggleItem.Click += async (_, _) => await _statusService.ToggleAsync();

        var checkForUpdatesItem = new NativeMenuItem { Header = "Check for Updates" };
        checkForUpdatesItem.Click += (_, _) => UpdateService.CheckForUpdatesInteractive();

        _autostartItem = new NativeMenuItem
        {
            Header = "Start at Login",
            ToggleType = MenuItemToggleType.CheckBox,
            IsChecked = AutostartService.IsEnabled(),
        };
        _autostartItem.Click += (_, _) =>
        {
            AutostartService.SetEnabled(!_autostartItem.IsChecked);
            _autostartItem.IsChecked = AutostartService.IsEnabled();
        };

        var quitItem = new NativeMenuItem { Header = "Quit" };
        quitItem.Click += (_, _) =>
        {
            if (Application.Current?.ApplicationLifetime is Avalonia.Controls.ApplicationLifetimes.IClassicDesktopStyleApplicationLifetime desktop)
            {
                desktop.Shutdown();
            }
        };

        var menu = new NativeMenu
        {
            _statusItem,
            new NativeMenuItemSeparator(),
            _toggleItem,
            new NativeMenuItemSeparator(),
            checkForUpdatesItem,
            _autostartItem,
            new NativeMenuItemSeparator(),
            quitItem,
        };

        _trayIcon = new TrayIcon
        {
            Icon = _disabledIcon,
            ToolTipText = "Bragi Client - checking status...",
            Menu = menu,
            IsVisible = true,
        };

        _statusService.StatusChanged += OnStatusChanged;
        // Render whatever the service's initial (pre-first-poll) state is immediately.
        Render(_statusService.Current);
    }

    private void OnStatusChanged(StatusSnapshot snapshot) => Dispatcher.UIThread.Post(() => Render(snapshot));

    private void Render(StatusSnapshot snapshot)
    {
        _statusItem.Header = $"Status: {snapshot.Detail}";
        _trayIcon.ToolTipText = $"Bragi Client - {snapshot.Detail}";

        switch (snapshot.State)
        {
            case TrayState.NotInstalled:
                _trayIcon.Icon = _disabledIcon;
                _toggleItem.IsEnabled = false;
                _toggleItem.Header = "Enable Link";
                break;
            case TrayState.Disabled:
                _trayIcon.Icon = _disabledIcon;
                _toggleItem.IsEnabled = true;
                _toggleItem.Header = "Enable Link";
                break;
            case TrayState.Enabling:
            case TrayState.Disabling:
                _toggleItem.IsEnabled = false;
                break;
            case TrayState.Enabled:
                _trayIcon.Icon = _enabledIcon;
                _toggleItem.IsEnabled = true;
                _toggleItem.Header = "Disable Link";
                break;
            case TrayState.Degraded:
            case TrayState.Error:
                _trayIcon.Icon = _errorIcon;
                _toggleItem.IsEnabled = true;
                _toggleItem.Header = snapshot.State == TrayState.Degraded ? "Disable Link" : "Enable Link";
                break;
        }
    }

    private static WindowIcon LoadIcon(string fileName)
    {
        var uri = new Uri($"avares://Bragi.Client/Assets/{fileName}");
        using var stream = AssetLoader.Open(uri);
        return new WindowIcon(new Bitmap(stream));
    }

    public void Dispose()
    {
        _statusService.StatusChanged -= OnStatusChanged;
        _trayIcon.IsVisible = false;
        _trayIcon.Dispose();
    }
}
