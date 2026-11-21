using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Bragi.Client.Config;

namespace Bragi.Client.Tray;

/// <summary>
/// Applies the hub's "hub" and "hub_moved" messages (bragi#61, logic in
/// Config/HubSync.cs): rewrites the env file and the Deskflow client's
/// remoteHost when they point elsewhere, and restarts what read them.
/// </summary>
public sealed class HubSyncService
{
    /// <summary>Linux: restarts the Roc link so it sends to the new address.</summary>
    public Func<Task<bool>>? RestartRocLink { get; init; }
    /// <summary>Rebuilds what read the env file (the peer socket, Windows'
    /// VBAN link). Called on the UI thread by the app.</summary>
    public Action? Reconfigure { get; init; }

    private int _running;

    public void OnMessage(JsonElement message)
    {
        if (HubSync.ParseHub(message) is { } hub)
        {
            _ = RunAsync(() => ApplyHubAsync(hub));
        }
        else if (HubSync.ParseMoved(message) is { } url)
        {
            _ = RunAsync(() => MoveAsync(url));
        }
    }

    private async Task RunAsync(Func<Task> work)
    {
        // One at a time: a reconnect while this runs gets a second "hub" message.
        if (System.Threading.Interlocked.Exchange(ref _running, 1) == 1)
        {
            return;
        }
        try
        {
            await work();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            Console.Error.WriteLine($"Bragi hub sync: {ex.Message}");
        }
        finally
        {
            System.Threading.Interlocked.Exchange(ref _running, 0);
        }
    }

    private async Task ApplyHubAsync(HubInfo hub)
    {
        var values = RocLinkConfig.ReadValues();
        if (values is not null)
        {
            var changes = HubSync.EnvChanges(values, hub);
            var remember = HubSync.RememberChanges(values, hub);
            if (changes.Count + remember.Count > 0)
            {
                WriteEnv(changes.Concat(remember).ToDictionary());
            }
            if (changes.Count > 0)
            {
                if (OperatingSystem.IsLinux() && RestartRocLink is not null)
                {
                    await RestartRocLink();
                }
                else
                {
                    // Windows: the VBAN link is built from the env file.
                    Reconfigure?.Invoke();
                }
            }
        }
        DeskflowClient.PointAt(hub.DeskflowHosts);
    }

    private Task MoveAsync(string url)
    {
        var values = RocLinkConfig.ReadValues();
        if (values is null || (values.TryGetValue("BRAGI_WS_URL", out var now) && now == url))
        {
            return Task.CompletedTask;
        }
        WriteEnv(new Dictionary<string, string?> { ["BRAGI_WS_URL"] = url });
        // Connects to the new hub; its "hub" message fixes the rest.
        Reconfigure?.Invoke();
        return Task.CompletedTask;
    }

    /// <summary>Linux, LAN fallback (bragi#81): points the Roc link at this
    /// address. True when the env file changed and the link needs a restart.</summary>
    public static bool SetHostIp(string ip)
    {
        var values = RocLinkConfig.ReadValues();
        if (values is null || (values.TryGetValue("BRAGI_HOST_IP", out var now) && now == ip))
        {
            return false;
        }
        WriteEnv(new Dictionary<string, string?> { ["BRAGI_HOST_IP"] = ip });
        return true;
    }

    private static void WriteEnv(IReadOnlyDictionary<string, string?> changes)
    {
        var path = RocLinkConfig.EnvFilePath;
        var lines = HubSync.UpdateEnvLines(File.ReadAllLines(path), changes);
        var temp = path + ".new";
        File.WriteAllLines(temp, lines);
        if (!OperatingSystem.IsWindows())
        {
            // It can hold an API key.
            File.SetUnixFileMode(temp, UnixFileMode.UserRead | UnixFileMode.UserWrite);
        }
        File.Move(temp, path, overwrite: true);
    }
}

/// <summary>The Deskflow client on this computer: its config file and its app.</summary>
public static class DeskflowClient
{
    public static string ConfPath => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Deskflow", "Deskflow.conf")
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".config", "Deskflow", "Deskflow.conf");

    /// <summary>Points remoteHost at these addresses and restarts the Deskflow
    /// app so it connects there. Does nothing when Deskflow isn't set up as a
    /// client here or already points there.</summary>
    public static void PointAt(IReadOnlyList<string> hosts)
    {
        if (!File.Exists(ConfPath))
        {
            return;
        }
        var text = File.ReadAllText(ConfPath);
        var updated = HubSync.UpdateDeskflowConf(text, hosts);
        if (updated is null)
        {
            return;
        }
        var temp = ConfPath + ".new";
        File.WriteAllText(temp, updated);
        File.Move(temp, ConfPath, overwrite: true);
        RestartApp();
    }

    /// <summary>The unit systemd-xdg-autostart-generator makes from Deskflow's
    /// autostart entry on Linux.</summary>
    private const string AutostartUnit = "app-org.deskflow.deskflow@autostart.service";

    private static int _missing;

    /// <summary>
    /// Linux: starts Deskflow when it's set up as a client here but hasn't been
    /// running for two checks in a row (a minute). On 2026-11-21 sage-dev's
    /// Deskflow was gone after a tray restart and only a keyboard plugged into
    /// sage-dev could start it again; nothing at the desk could.
    /// </summary>
    public static async Task WatchAsync()
    {
        if (!OperatingSystem.IsLinux())
        {
            return;
        }
        while (true)
        {
            await Task.Delay(TimeSpan.FromSeconds(30));
            try
            {
                var configured = File.Exists(ConfPath) && File.ReadAllText(ConfPath).Contains("remoteHost=", StringComparison.Ordinal);
                if (!configured || Process.GetProcessesByName("deskflow").Length > 0)
                {
                    _missing = 0;
                    continue;
                }
                if (++_missing >= 2)
                {
                    _missing = 0;
                    Console.Error.WriteLine("Bragi: Deskflow wasn't running; starting it");
                    await StartLinuxAsync(null);
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ProcessRunException)
            {
                Console.Error.WriteLine($"Bragi: couldn't check on Deskflow: {ex.Message}");
            }
        }
    }

    /// <summary>
    /// Linux: starts Deskflow through systemd, never as this app's child. A
    /// child lands in the tray app's own unit, and the next restart of the
    /// tray (an update) stops it too; that's how sage-dev lost its Deskflow.
    /// The desktop's autostart unit is used when there is one, so Deskflow runs
    /// as it does after a login; otherwise a transient unit.
    /// </summary>
    private static async Task StartLinuxAsync(string? exe)
    {
        var load = await ProcessRunner.RunAsync("systemctl", ["--user", "show", "-p", "LoadState", "--value", AutostartUnit]);
        if (load.ExitCode == 0 && load.Stdout.Trim() == "loaded")
        {
            await ProcessRunner.RunAsync("systemctl", ["--user", "reset-failed", AutostartUnit]);
            await ProcessRunner.RunAsync("systemctl", ["--user", "restart", AutostartUnit]);
            return;
        }
        exe ??= File.Exists("/usr/bin/deskflow") ? "/usr/bin/deskflow" : null;
        if (exe is not null)
        {
            await ProcessRunner.RunAsync("systemd-run", ["--user", "--collect", "--quiet", exe]);
        }
    }

    /// <summary>Restarts the Deskflow app (not its background service on
    /// Windows): it reads remoteHost at start and hands it to the client.</summary>
    private static void RestartApp()
    {
        var running = Process.GetProcessesByName("deskflow");
        string? exe = null;
        foreach (var p in running)
        {
            try
            {
                exe ??= p.MainModule?.FileName;
                p.Kill();
                p.WaitForExit(3000);
            }
            catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception)
            {
            }
        }
        if (OperatingSystem.IsLinux())
        {
            _ = StartLinuxAsync(exe);
            return;
        }
        if (exe is null && OperatingSystem.IsWindows() && Process.GetProcessesByName("deskflow-daemon").Length > 0)
        {
            // In service mode the app may be closed; the service keeps the old
            // address until the app tells it the new one.
            exe = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Deskflow", "deskflow.exe");
        }
        if (exe is not null && File.Exists(exe))
        {
            // Deskflow keeps its working directory busy for as long as it runs:
            // its own, not ours, which an update replaces.
            Process.Start(new ProcessStartInfo(exe) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(exe)! });
        }
    }
}
