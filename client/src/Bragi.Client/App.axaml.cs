using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using Bragi.Client.Config;
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
            _trayIconManager = new TrayIconManager(_statusService);
            _ = UpdateService.CheckSilentlyAsync();

            var config = RocLinkConfig.TryLoad();
            _presenceClient = new PeerPresenceClient(config?.PeerName, config?.BragiWsBaseUrl);
            _statusService.StatusChanged += snapshot => _presenceClient.SetEnabled(snapshot.State == TrayState.Enabled);
            _presenceClient.SetEnabled(_statusService.Current.State == TrayState.Enabled);

            desktop.Exit += (_, _) =>
            {
                _trayIconManager.Dispose();
                _presenceClient.Dispose();
                _statusService.Dispose();
            };
        }

        base.OnFrameworkInitializationCompleted();
    }
}
