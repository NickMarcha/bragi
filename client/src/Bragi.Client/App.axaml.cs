using System;
using System.IO;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using Bragi.Client.Config;
using Bragi.Client.Gamepad;
using Bragi.Client.Monitors;
using Bragi.Client.Setup;
using Bragi.Client.Tray;

namespace Bragi.Client;

public partial class App : Application
{
    private LinkStatusService? _statusService;
    private TrayIconManager? _trayIconManager;
    private PeerPresenceClient? _presenceClient;
    private GamepadService? _gamepads;
    private KvmService? _kvm;
    private KeyPresser? _keys;
    private MonitorReporter? _monitors;
    private Audio.VbanLink? _audio;

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
        // plays them as a ViGEmBus Xbox 360 pad.
        _gamepads = OperatingSystem.IsLinux()
            ? new GamepadService(config?.PeerName, () => new UinputGamepad(),
                File.Exists("/dev/uhid") ? d => new UhidDevice(d) : null)
            : OperatingSystem.IsWindows()
                ? new GamepadService(config?.PeerName, () => new ViGEmGamepad())
                : new GamepadService(config?.PeerName, null);
        _presenceClient.MessageReceived += _gamepads.OnMessage;
        _presenceClient.Disconnected += _gamepads.OnDisconnected;
        _gamepads.Send = _presenceClient.SendAsync;
        // The KVM's computers, switchable from the tray.
        _kvm = new KvmService { Send = _presenceClient.SendAsync };
        _presenceClient.MessageReceived += _kvm.OnMessage;
        _presenceClient.Disconnected += _kvm.OnDisconnected;
        // "Identify" in the dashboard's KVM setup: this computer's name on its screens.
        _presenceClient.MessageReceived += IdentifyOverlay.OnMessage;
        // "Press keys on computers" from the host, pressed here (Linux: uinput).
        _keys = new KeyPresser(OperatingSystem.IsLinux() ? () => new UinputKeyboard() : null) { Send = _presenceClient.SendAsync };
        _presenceClient.MessageReceived += _keys.OnMessage;
        _presenceClient.Disconnected += _keys.OnDisconnected;
        // This computer's monitors, for the KVM layout (app/monitors.py).
        IMonitorSource? monitorSource = OperatingSystem.IsWindows() ? new WindowsMonitors()
            : OperatingSystem.IsLinux() ? new KscreenMonitors() : null;
        _monitors = new MonitorReporter(monitorSource, TimeSpan.FromSeconds(OperatingSystem.IsWindows() ? 3 : 5))
            { Send = _presenceClient.SendAsync };
        _presenceClient.MessageReceived += _monitors.OnMessage;
        _presenceClient.Disconnected += _monitors.OnDisconnected;
        _monitors.Start();
        // Windows: the app carries the audio itself, as VBAN (instead of Voicemeeter's).
        NativeMenuItem? audioItem = null;
        if (OperatingSystem.IsWindows() && config is { VbanPort: { } port, VbanStreamSend: { } fromHost, VbanStreamReceive: { } toHost })
        {
            _audio = new Audio.VbanLink(config.BragiHostIp, port, fromHost, toHost);
            _audio.Apply(Audio.AudioDevices.Load());
            audioItem = new AudioMenu(_audio).Item;
        }
        _trayIconManager = new TrayIconManager(_statusService!, _gamepads, _kvm, audioItem);
        _trayIconManager.SetupRequested += ShowSetup;
        _presenceClient.SetEnabled(_statusService!.Current.State == TrayState.Enabled);
    }

    private void StopConfigured()
    {
        _trayIconManager?.Dispose();
        _presenceClient?.Dispose();
        _gamepads?.Dispose();
        _keys?.Dispose();
        _keys = null;
        _monitors?.Dispose();
        _monitors = null;
        _audio?.Dispose();
        _audio = null;
        _trayIconManager = null;
        _presenceClient = null;
        _gamepads = null;
        _kvm = null;
    }

    private void ShowSetup() => SetupLauncher.Show(async () =>
    {
        // roc-link.env changed: rebuild what read it, then refresh the status.
        StopConfigured();
        StartConfigured();
        await _statusService!.EvaluateAsync();
    });
}
