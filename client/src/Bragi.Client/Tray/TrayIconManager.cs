using System;
using System.Threading.Tasks;
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

    public TrayIconManager(LinkStatusService statusService, GamepadService? gamepads = null, KvmService? kvm = null,
                           NativeMenuItem? audioItem = null)
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

        try
        {
            AutostartService.RepairIfStale();
        }
        catch (Exception e) when (e is System.IO.IOException or UnauthorizedAccessException)
        {
            // A read-only or odd autostart dir shouldn't stop the tray from starting.
        }

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

        // KDE: let Deskflow control this computer without the "Remote control" question
        // on every start (Tray/RemoteControlPermission.cs). Off until chosen here.
        NativeMenuItem? remoteControlItem = null;
        if (RemoteControlPermission.Applies())
        {
            var item = new NativeMenuItem
            {
                Header = "Let Deskflow control this computer without asking",
                ToggleType = MenuItemToggleType.CheckBox,
            };
            async Task RefreshRemoteControl()
            {
                try
                {
                    var granted = await RemoteControlPermission.IsGrantedAsync();
                    Dispatcher.UIThread.Post(() => item.IsChecked = granted);
                }
                catch (Exception ex) when (ex is ProcessRunException or System.ComponentModel.Win32Exception or InvalidOperationException)
                {
                    Dispatcher.UIThread.Post(() => item.IsEnabled = false);
                }
            }
            item.Click += async (_, _) =>
            {
                try
                {
                    await RemoteControlPermission.SetAsync(!item.IsChecked);
                }
                catch (Exception ex) when (ex is ProcessRunException or System.ComponentModel.Win32Exception)
                {
                    // shown by the state the refresh reads back
                }
                await RefreshRemoteControl();
            };
            _ = RefreshRemoteControl();
            remoteControlItem = item;
        }

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
            new NativeMenuItem { Header = $"Bragi Client {Version}", IsEnabled = false },
            _statusItem,
            new NativeMenuItemSeparator(),
        };
        if (OperatingSystem.IsWindows())
        {
            // The app's own VBAN link (Audio/VbanLink.cs), or, without one,
            // what Voicemeeter's VBAN would have to be set to.
            if (audioItem is not null)
            {
                menu.Add(audioItem);
            }
            else
            {
                foreach (var line in VbanMenuLines(RocLinkConfig.TryLoad()))
                {
                    menu.Add(new NativeMenuItem { Header = line, IsEnabled = false });
                }
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
        })
        {
            menu.Add(item);
        }
        if (remoteControlItem is not null)
        {
            menu.Add(remoteControlItem);
        }
        menu.Add(new NativeMenuItemSeparator());
        menu.Add(quitItem);

        _trayIcon = new TrayIcon
        {
            Icon = _disabledIcon,
            ToolTipText = $"Bragi Client {Version} - checking status...",
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

    /// <summary>This app's version (the csproj's Version, e.g. 0.1.18), for the menu and tooltip.</summary>
    public static string Version { get; } = FormatVersion(typeof(TrayIconManager).Assembly.GetName().Version);

    public static string FormatVersion(System.Version? version) =>
        version is null ? "?" : $"{version.Major}.{version.Minor}.{Math.Max(version.Build, 0)}";

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
            return (true, $"{name}: not supported here", false, false);
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

    private bool _lanAudio;

    /// <summary>Says so in the status line and tooltip while the audio goes
    /// over the home network (bragi#81, LanFallbackService). Any thread.</summary>
    public void SetLanAudio(bool on) => Dispatcher.UIThread.Post(() =>
    {
        _lanAudio = on;
        Render(_statusService.Current);
    });

    /// <summary>The status line's text, with the LAN fallback note when it's on.</summary>
    public static string StatusDetail(string detail, bool lanAudio) =>
        lanAudio ? $"{detail}. Audio over the home network (Tailscale is down)" : detail;

    private void Render(StatusSnapshot snapshot)
    {
        var detail = StatusDetail(snapshot.Detail, _lanAudio);
        _statusItem.Header = $"Status: {detail}";
        _trayIcon.ToolTipText = $"Bragi Client {Version} - {detail}";

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
