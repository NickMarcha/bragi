using System;
using System.IO;

namespace Bragi.Client;

/// <summary>
/// Moves the app's working directory out of where it's installed, so nothing
/// keeps that directory busy when an update replaces it.
///
/// Linux: after a self-update, Velopack's updater runs from inside the old
/// AppImage's mount (/tmp/.mount_BragiC...) and starts the new version with
/// that as its working directory. While anything has its working directory
/// there, the old AppImage can't unmount, so its runtime stays behind for
/// good: one leftover process and mount per update.
///
/// Windows: Velopack starts the app in &lt;root&gt;\current, and every program
/// the app starts inherits that (Deskflow, when the hub address changes). An
/// update moves the directory aside, which Windows refuses while any process
/// works in it. The update failed, the old version started and tried again,
/// in a loop every ten seconds or so.
/// </summary>
public static class WorkingDirectory
{
    /// <summary>Where to move to, or null to stay: from inside an AppImage
    /// mount, or inside <paramref name="appDir"/> when given.</summary>
    public static string? Target(string current, string home, string? appDir = null)
    {
        if (string.IsNullOrEmpty(home))
        {
            return null;
        }
        if (current.Contains("/.mount_", StringComparison.Ordinal))
        {
            return home;
        }
        return !string.IsNullOrEmpty(appDir) && Inside(current, appDir) ? home : null;
    }

    private static bool Inside(string path, string dir)
    {
        var p = path.TrimEnd('\\', '/');
        var d = dir.TrimEnd('\\', '/');
        return p.Equals(d, StringComparison.OrdinalIgnoreCase)
            || p.StartsWith(d + "\\", StringComparison.OrdinalIgnoreCase)
            || p.StartsWith(d + "/", StringComparison.OrdinalIgnoreCase);
    }

    public static void LeaveInstallDir()
    {
        try
        {
            var target = Target(
                Environment.CurrentDirectory,
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                OperatingSystem.IsWindows() ? AppContext.BaseDirectory : null);
            if (target is not null)
            {
                Directory.SetCurrentDirectory(target);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Staying put only costs a leftover mount, or a retried update.
        }
    }
}
