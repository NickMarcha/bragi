using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using Bragi.Client.Config;
using Bragi.Client.Setup;
using Bragi.Client.Tray;

namespace Bragi.Client;

public partial class App : Application
{
    private LinkStatusService? _statusService;
    private TrayIconManager? _trayIconManager;
    private PeerPresenceClient? _presenceClient;

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
            _ = PatchedLibrocInstaller.EnsureInstalledAsync(new RocLinkController());

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
        _trayIconManager = new TrayIconManager(_statusService!);
        _trayIconManager.SetupRequested += ShowSetup;
        var config = RocLinkConfig.TryLoad();
        _presenceClient = new PeerPresenceClient(config?.PeerName, config?.BragiWsBaseUrl);
        _presenceClient.SetEnabled(_statusService!.Current.State == TrayState.Enabled);
    }

    private void StopConfigured()
    {
        _trayIconManager?.Dispose();
        _presenceClient?.Dispose();
        _trayIconManager = null;
        _presenceClient = null;
    }

    private void ShowSetup() => SetupLauncher.Show(async () =>
    {
        // roc-link.env changed: rebuild what read it, then refresh the status.
        StopConfigured();
        StartConfigured();
        await _statusService!.EvaluateAsync();
    });
}
