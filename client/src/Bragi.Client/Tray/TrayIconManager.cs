using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media.Imaging;
using Avalonia.Platform;
using Avalonia.Threading;
using Bragi.Client.Config;
using Bragi.Client.Gamepad;
using Bragi.Client.Volume;

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
    private readonly NativeMenuItem _restartItem;
    private readonly NativeMenuItem _autostartItem;
    private readonly VolumeMenu _volumeMenu;
    private readonly GamepadService? _gamepads;
    private readonly NativeMenuItem _gamepadItem;
    private readonly KvmService? _kvm;
    private readonly NativeMenuItem _kvmItem;

    /// <summary>"Connect to Bragi..." / "Change Bragi connection..." was chosen; App opens the setup window.</summary>
    public event Action? SetupRequested;

    private readonly WindowIcon _disabledIcon = LoadIcon("tray-disabled.png");
    private readonly WindowIcon _enabledIcon = LoadIcon("tray-enabled.png");
    private readonly WindowIcon _errorIcon = LoadIcon("tray-error.png");

    public TrayIconManager(LinkStatusService statusService, GamepadService? gamepads = null, KvmService? kvm = null)
    {
        _statusService = statusService;
        _gamepads = gamepads;
        _kvm = kvm;
        // "Keyboard & mouse": one entry per computer, the active one ticked.
        // Hidden until the host reports its KVM.
        _kvmItem = new NativeMenuItem { Header = "Keyboard & mouse", IsVisible = false, Menu = new NativeMenu() };
        // "Use controller here": takes the Bragi host's controllers for this
        // computer, or gives them back. Hidden until the host has one.
        _gamepadItem = new NativeMenuItem { ToggleType = MenuItemToggleType.CheckBox, IsVisible = false };
        _gamepadItem.Click += async (_, _) =>
        {
            if (_gamepads is null) return;
            var ok = _gamepads.IsHere ? await _gamepads.ReleaseAsync() : await _gamepads.ClaimAsync();
            if (!ok) Dispatcher.UIThread.Post(() => _gamepadItem.Header = "Controller: Bragi isn't connected");
        };
        _volumeMenu = new VolumeMenu(RocLinkConfig.TryLoad());

        _statusItem = new NativeMenuItem { Header = "Status: checking...", IsEnabled = false };
        _toggleItem = new NativeMenuItem { Header = "Enable Link", IsEnabled = false };
        _toggleItem.Click += async (_, _) => await _statusService.ToggleAsync();

        // Manual recovery for the socket-loss failure mode: the unit and its PipeWire nodes can look fine (Enabled)
        // while the Roc UDP sockets underneath are silently gone and audio
        // has stopped. Always available (not gated on the current state)
        // since the whole point is "audio's dead, I don't trust the status
        // line, fix it anyway" - `systemctl restart` is a no-op-safe action
        // even if the link was already healthy.
        _restartItem = new NativeMenuItem { Header = "Restart Link", IsEnabled = false };
        _restartItem.Click += async (_, _) => await _statusService.RestartAsync();

        // App rebuilds this manager after a successful connect, so the label is right from then on.
        var setupItem = new NativeMenuItem { Header = SetupMenuLabel(RocLinkConfig.IsInstalled()) };
        setupItem.Click += (_, _) => SetupRequested?.Invoke();

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

        var menu = new NativeMenu { _statusItem, new NativeMenuItemSeparator() };
        if (OperatingSystem.IsWindows())
        {
            // No link of our own to switch: Voicemeeter carries the audio.
            // Show what to set it to instead.
            foreach (var line in VbanMenuLines(RocLinkConfig.TryLoad()))
            {
                menu.Add(new NativeMenuItem { Header = line, IsEnabled = false });
            }
        }
        else
        {
            menu.Add(_toggleItem);
            menu.Add(_restartItem);
        }
        foreach (var item in new NativeMenuItemBase[]
        {
            new NativeMenuItemSeparator(),
            _kvmItem,
            _gamepadItem,
            _volumeMenu.Item,
            _volumeMenu.WebUiItem,
            new NativeMenuItemSeparator(),
            setupItem,
            checkForUpdatesItem,
            _autostartItem,
            new NativeMenuItemSeparator(),
            quitItem,
        })
        {
            menu.Add(item);
        }

        _trayIcon = new TrayIcon
        {
            Icon = _disabledIcon,
            ToolTipText = "Bragi Client - checking status...",
            Menu = menu,
            IsVisible = true,
        };

        // A left click opens the volume window, like the menu's Volume item. The
        // menu stays on right click. Does nothing without a configured server.
        _trayIcon.Clicked += (_, _) => _volumeMenu.Open();

        _statusService.StatusChanged += OnStatusChanged;
        if (_gamepads is not null)
        {
            _gamepads.StatusChanged += OnGamepadsChanged;
        }
        if (_kvm is not null)
        {
            _kvm.Changed += OnKvmChanged;
        }
        // Render whatever the service's initial (pre-first-poll) state is immediately.
        Render(_statusService.Current);
    }

    /// <summary>The Voicemeeter VBAN settings this peer needs, for the Windows menu.</summary>
    public static string[] VbanMenuLines(RocLinkConfig? config) =>
        config?.VbanStreamSend is null || config.VbanStreamReceive is null
            ? []
            : [
                $"VBAN in: {config.VbanStreamSend} from {config.BragiHostIp}:{config.VbanPort}",
                $"VBAN out: {config.VbanStreamReceive} to {config.BragiHostIp}:{config.VbanPort}",
            ];

    private void OnKvmChanged() => Dispatcher.UIThread.Post(() =>
    {
        var rows = KvmService.MenuRows(_kvm!.State);
        _kvmItem.IsVisible = rows.Count > 0;
        var submenu = _kvmItem.Menu!;
        submenu.Items.Clear();
        foreach (var (computer, active) in rows)
        {
            var item = new NativeMenuItem { Header = computer, ToggleType = MenuItemToggleType.Radio, IsChecked = active };
            item.Click += async (_, _) =>
            {
                if (!await _kvm.SwitchAsync(computer))
                {
                    Dispatcher.UIThread.Post(() => _kvmItem.Header = "Keyboard & mouse (Bragi isn't connected)");
                }
            };
            submenu.Items.Add(item);
        }
        _kvmItem.Header = _kvm.Error is { } error ? $"Keyboard & mouse ({error})" : "Keyboard & mouse";
    });

    private void OnGamepadsChanged(GamepadStatus status) => Dispatcher.UIThread.Post(() =>
    {
        var (visible, header, isChecked, enabled) = GamepadMenu(status, _gamepads!.IsHere, _gamepads.Problem);
        _gamepadItem.IsVisible = visible;
        _gamepadItem.Header = header;
        _gamepadItem.IsChecked = isChecked;
        _gamepadItem.IsEnabled = enabled;
    });

    /// <summary>The "Use controller here" item for this status.</summary>
    public static (bool Visible, string Header, bool Checked, bool Enabled) GamepadMenu(
        GamepadStatus status, bool isHere, string? problem)
    {
        if (status.Controllers.Count == 0)
        {
            return (false, "", false, false);
        }
        var name = status.Controllers.Count == 1 ? status.Controllers[0] : $"{status.Controllers.Count} controllers";
        if (!status.HereSupported)
        {
            return (true, $"{name}: not on Windows yet", false, false);
        }
        if (isHere && problem is not null)
        {
            return (true, $"{name}: {problem}", true, true);
        }
        var elsewhere = !isHere && status.Target is not null ? $" (now on {status.Target})" : "";
        return (true, $"Use {name} here{elsewhere}", isHere, true);
    }

    public static string SetupMenuLabel(bool alreadySetUp) =>
        alreadySetUp ? "Change Bragi connection..." : "Connect to Bragi...";

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
                _restartItem.IsEnabled = false;
                break;
            case TrayState.Disabled:
                _trayIcon.Icon = _disabledIcon;
                _toggleItem.IsEnabled = true;
                _toggleItem.Header = "Enable Link";
                _restartItem.IsEnabled = true;
                break;
            case TrayState.Enabling:
            case TrayState.Disabling:
                _toggleItem.IsEnabled = false;
                _restartItem.IsEnabled = false;
                break;
            case TrayState.Restarting:
                _toggleItem.IsEnabled = false;
                _restartItem.IsEnabled = false;
                break;
            case TrayState.Enabled:
                _trayIcon.Icon = _enabledIcon;
                _toggleItem.IsEnabled = true;
                _toggleItem.Header = "Disable Link";
                _restartItem.IsEnabled = true;
                break;
            case TrayState.Degraded:
            case TrayState.Error:
                _trayIcon.Icon = _errorIcon;
                _toggleItem.IsEnabled = true;
                _toggleItem.Header = snapshot.State == TrayState.Degraded ? "Disable Link" : "Enable Link";
                _restartItem.IsEnabled = true;
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
        if (_gamepads is not null)
        {
            _gamepads.StatusChanged -= OnGamepadsChanged;
        }
        if (_kvm is not null)
        {
            _kvm.Changed -= OnKvmChanged;
        }
        _volumeMenu.Dispose();
        _statusService.StatusChanged -= OnStatusChanged;
        _trayIcon.IsVisible = false;
        _trayIcon.Dispose();
    }
}
