using System;
using System.Threading;
using System.Threading.Tasks;
using Bragi.Client.Config;

namespace Bragi.Client.Tray;

public enum TrayState
{
    NotInstalled,
    Disabled,
    Enabling,
    Disabling,
    Enabled,
    Degraded,
    Error,
}

public sealed record StatusSnapshot(TrayState State, string Detail);

/// <summary>
/// Owns the poll loop and the small state machine combining the systemd
/// unit's active/inactive state with local PipeWire node presence. See the
/// plan doc: this is a deliberate v1 simplification vs. the server's
/// pw-mon-based watcher (docs/realtime-control-plane.md) - a tray icon has
/// no sub-100ms latency requirement, so a 4s poll (plus an immediate
/// re-check right after the user's own toggle) is enough.
/// </summary>
public sealed class LinkStatusService : IDisposable
{
    private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(4);

    private readonly RocLinkController _controller = new();
    private readonly Timer _timer;
    private readonly SemaphoreSlim _lock = new(1, 1);

    public StatusSnapshot Current { get; private set; } = new(TrayState.NotInstalled, "Checking...");
    public event Action<StatusSnapshot>? StatusChanged;

    public LinkStatusService()
    {
        _timer = new Timer(OnTick, null, TimeSpan.Zero, PollInterval);
    }

    private async void OnTick(object? state)
    {
        await EvaluateAsync();
    }

    public async Task EvaluateAsync()
    {
        if (!await _lock.WaitAsync(0))
        {
            return; // an evaluation (or toggle) is already in flight; the poll tick can skip
        }
        try
        {
            var snapshot = await ComputeSnapshotAsync();
            Current = snapshot;
            StatusChanged?.Invoke(snapshot);
        }
        finally
        {
            _lock.Release();
        }
    }

    private async Task<StatusSnapshot> ComputeSnapshotAsync()
    {
        if (!RocLinkConfig.IsInstalled())
        {
            return new StatusSnapshot(TrayState.NotInstalled,
                "Not installed - see client/systemd/install.sh in the bragi repo");
        }

        var config = RocLinkConfig.TryLoad();
        if (config is null)
        {
            return new StatusSnapshot(TrayState.NotInstalled,
                $"{RocLinkConfig.EnvFilePath} is missing required values");
        }

        var unitState = await _controller.GetStateAsync();
        if (unitState is UnitState.Unknown)
        {
            return new StatusSnapshot(TrayState.Error, "systemctl --user is-active failed unexpectedly");
        }
        if (unitState is UnitState.Failed)
        {
            return new StatusSnapshot(TrayState.Error, $"Unit failed - check: journalctl --user -u bragi-roc-link");
        }
        if (unitState is UnitState.Inactive)
        {
            return new StatusSnapshot(TrayState.Disabled, "Disabled");
        }

        // Active - cross-check the modules actually loaded, since a
        // successful `systemctl start` only proves the oneshot script
        // exited 0, not that both modules are still present right now.
        var nodeNames = await PipewireStatusChecker.GetAudioNodeNamesAsync();
        var sinkUp = nodeNames.Contains(config.LocalSinkName);
        var sourceUp = nodeNames.Contains(config.LocalSourceName);
        if (sinkUp && sourceUp)
        {
            return new StatusSnapshot(TrayState.Enabled, $"Enabled - linked to sagepi @ {config.SagepiTailscaleIp}");
        }
        return new StatusSnapshot(TrayState.Degraded,
            $"Unit active but node(s) missing (sink={sinkUp}, source={sourceUp})");
    }

    /// <summary>Flips the link on/off and blocks (async) until the resulting state is known.</summary>
    public async Task ToggleAsync()
    {
        await _lock.WaitAsync();
        try
        {
            var wasEnabled = Current.State is TrayState.Enabled or TrayState.Degraded;
            var transientState = wasEnabled ? TrayState.Disabling : TrayState.Enabling;
            Current = new StatusSnapshot(transientState, transientState == TrayState.Enabling ? "Enabling..." : "Disabling...");
            StatusChanged?.Invoke(Current);

            var ok = wasEnabled ? await _controller.DisableAsync() : await _controller.EnableAsync();
            if (!ok)
            {
                Current = new StatusSnapshot(TrayState.Error, "systemctl start/stop failed - check journalctl --user -u bragi-roc-link");
                StatusChanged?.Invoke(Current);
                return;
            }
        }
        finally
        {
            _lock.Release();
        }

        // Re-check for real rather than assuming success, same spirit as
        // the server never trusting wpctl's own echo without a follow-up read.
        await EvaluateAsync();
    }

    public void Dispose() => _timer.Dispose();
}
