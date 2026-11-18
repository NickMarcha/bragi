using System;
using System.IO;

namespace Bragi.Client;

/// <summary>
/// After a Linux self-update, Velopack's updater runs from inside the old
/// AppImage's mount (/tmp/.mount_BragiC...) and starts the new version with
/// that as its working directory. While anything has its working directory
/// there, the old AppImage can't unmount, so its runtime stays behind for
/// good: one leftover process and mount per update. Moving out at startup
/// lets the previous version's runtime unmount and exit.
/// </summary>
public static class WorkingDirectory
{
    /// <summary>Where to move to, or null to stay: only from inside an AppImage mount.</summary>
    public static string? Target(string current, string home) =>
        current.Contains("/.mount_", StringComparison.Ordinal) && !string.IsNullOrEmpty(home) ? home : null;

    public static void LeaveAppImageMount()
    {
        if (!OperatingSystem.IsLinux())
        {
            return;
        }
        try
        {
            var target = Target(Environment.CurrentDirectory, Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
            if (target is not null)
            {
                Directory.SetCurrentDirectory(target);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Staying put only costs the leftover mount.
        }
    }
}
