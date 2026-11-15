using System;
using System.IO;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using Bragi.Client.Config;
using Bragi.Client.Gamepad;
using Bragi.Client.Setup;
using Bragi.Client.Tray;

namespace Bragi.Client;

public partial class App : Application
{
    private LinkStatusService? _statusService;
    private TrayIconManager? _trayIconManager;
    private PeerPresenceClient? _presenceClient;
    private GamepadService? _gamepads;

    public override void Initialize()
    {
        AvaloniaXamlLoader.Load(this);
    }

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            // Tray-only app: no main window, don't exit when the (nonexistent)
            // last window closes - only Quit from the tray menu should end it.
            desktop.ShutdownMode = ShutdownMode.OnExplicitShutdown;

            _statusService = new LinkStatusService();
            _statusService.StatusChanged += snapshot => _presenceClient?.SetEnabled(snapshot.State == TrayState.Enabled);
            StartConfigured();
            _ = UpdateService.CheckSilentlyAsync();
            if (!OperatingSystem.IsWindows())
            {
                _ = PatchedLibrocInstaller.EnsureInstalledAsync(new RocLinkController());
            }

            // First run: nothing to link to yet, so ask.
            if (!RocLinkConfig.IsInstalled())
            {
                ShowSetup();
            }

            desktop.Exit += (_, _) =>
            {
                StopConfigured();
                _statusService.Dispose();
            };
        }

        base.OnFrameworkInitializationCompleted();
    }

    /// <summary>The parts that read roc-link.env once, at construction.</summary>
    private void StartConfigured()
    {
        var config = RocLinkConfig.TryLoad();
        _presenceClient = new PeerPresenceClient(config?.PeerName, config?.BragiWsBaseUrl);
        // Controllers on the Bragi host, played here. Linux recreates them
        // exactly with uhid, or falls back to a uinput Xbox 360 pad; Windows
        // can't play them yet.
        _gamepads = OperatingSystem.IsLinux()
            ? new GamepadService(config?.PeerName, () => new UinputGamepad(),
                File.Exists("/dev/uhid") ? d => new UhidDevice(d) : null)
            : new GamepadService(config?.PeerName, null);
        _presenceClient.MessageReceived += _gamepads.OnMessage;
        _presenceClient.Disconnected += _gamepads.OnDisconnected;
        _gamepads.Send = _presenceClient.SendAsync;
        _trayIconManager = new TrayIconManager(_statusService!, _gamepads);
        _trayIconManager.SetupRequested += ShowSetup;
        _presenceClient.SetEnabled(_statusService!.Current.State == TrayState.Enabled);
    }

    private void StopConfigured()
    {
        _trayIconManager?.Dispose();
        _presenceClient?.Dispose();
        _gamepads?.Dispose();
        _trayIconManager = null;
        _presenceClient = null;
        _gamepads = null;
    }

    private void ShowSetup() => SetupLauncher.Show(async () =>
    {
        // roc-link.env changed: rebuild what read it, then refresh the status.
        StopConfigured();
        StartConfigured();
        await _statusService!.EvaluateAsync();
    });
}
