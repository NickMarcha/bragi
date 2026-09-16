using System;
using System.Threading.Tasks;

namespace Bragi.Client.Tray;

public enum UnitState
{
    Active,
    Inactive,
    Failed,
    /// <summary>Unit file not installed, or systemctl itself errored unexpectedly.</summary>
    Unknown,
}

/// <summary>
/// The app's only write path to the Roc link: start/stop the systemd
/// --user unit. Never calls pw-cli directly to load/unload modules - that
/// logic lives in the unit's own wrapper scripts (client/systemd/), same
/// division of responsibility as the Bragi server never restarting
/// pipewire itself.
/// </summary>
public sealed class RocLinkController
{
    private const string UnitName = "bragi-roc-link.service";

    public async Task<UnitState> GetStateAsync()
    {
        try
        {
            var result = await ProcessRunner.RunAsync("systemctl", ["--user", "is-active", UnitName]);
            return result.Stdout.Trim() switch
            {
                "active" => UnitState.Active,
                "failed" => UnitState.Failed,
                "inactive" => UnitState.Inactive,
                _ => UnitState.Unknown,
            };
        }
        catch (ProcessRunException)
        {
            return UnitState.Unknown;
        }
    }

    public async Task<bool> EnableAsync()
    {
        try
        {
            await ProcessRunner.RunOrThrowAsync("systemctl", ["--user", "start", UnitName], TimeSpan.FromSeconds(15));
            return true;
        }
        catch (ProcessRunException)
        {
            return false;
        }
    }

    public async Task<bool> DisableAsync()
    {
        try
        {
            await ProcessRunner.RunOrThrowAsync("systemctl", ["--user", "stop", UnitName], TimeSpan.FromSeconds(15));
            return true;
        }
        catch (ProcessRunException)
        {
            return false;
        }
    }

    /// <summary>
    /// The recovery path for the socket-loss failure mode (bragi-assistant
    /// #081): the systemd unit and its PipeWire nodes stay up and look fine,
    /// but the Roc UDP sockets underneath silently disappear, so audio stops
    /// with nothing in the tray's own health check to show for it (see
    /// PipewireStatusChecker - it only confirms the node names exist, not
    /// that they're actually passing audio). `systemctl restart` reloads the
    /// modules fresh and has fixed every occurrence so far. Works whether
    /// the unit is currently active, failed, or inactive - restart starts an
    /// inactive unit rather than erroring.
    /// </summary>
    public async Task<bool> RestartAsync()
    {
        try
        {
            await ProcessRunner.RunOrThrowAsync("systemctl", ["--user", "restart", UnitName], TimeSpan.FromSeconds(15));
            return true;
        }
        catch (ProcessRunException)
        {
            return false;
        }
    }
}
