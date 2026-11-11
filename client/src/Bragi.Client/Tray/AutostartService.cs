using System;
using System.IO;
using System.Linq;
using System.Reflection;

namespace Bragi.Client.Tray;

/// <summary>
/// Manages the ~/.config/autostart/bragi-client.desktop entry (freedesktop
/// XDG autostart spec) so "Start at Login" can be a plain checkable tray
/// menu item instead of something the user has to set up by hand per
/// machine. Resolves the Exec= line from the running process itself so it
/// works whether launched via `dotnet Bragi.Client.dll` (framework-dependent
/// build, what `dotnet build` produces), a self-contained/published binary,
/// or the Velopack AppImage, without needing to know which in advance.
/// </summary>
public static class AutostartService
{
    private static readonly string AutostartDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "autostart");

    private static readonly string DesktopFilePath = Path.Combine(AutostartDir, "bragi-client.desktop");

    public static bool IsEnabled() => File.Exists(DesktopFilePath);

    public static void SetEnabled(bool enabled)
    {
        if (enabled)
        {
            Directory.CreateDirectory(AutostartDir);
            File.WriteAllText(DesktopFilePath, BuildDesktopFileContent(CurrentExecCommand()));
        }
        else if (File.Exists(DesktopFilePath))
        {
            File.Delete(DesktopFilePath);
        }
    }

    /// <summary>
    /// Rewrites an existing entry whose Exec= program no longer exists, e.g. one
    /// written by an older client from inside an AppImage's /tmp/.mount_* dir,
    /// which is gone after a reboot. Leaves working entries alone, so running a
    /// dev build doesn't steal the entry from an installed one.
    /// </summary>
    public static void RepairIfStale() => RepairIfStale(DesktopFilePath, CurrentExecCommand(), File.Exists);

    public static bool RepairIfStale(string desktopFilePath, string exec, Func<string, bool> fileExists)
    {
        if (!File.Exists(desktopFilePath))
        {
            return false;
        }

        var program = ExecProgram(File.ReadAllText(desktopFilePath));
        if (program is not null && fileExists(program))
        {
            return false;
        }

        File.WriteAllText(desktopFilePath, BuildDesktopFileContent(exec));
        return true;
    }

    public static string BuildDesktopFileContent(string exec) =>
        "[Desktop Entry]\n" +
        "Type=Application\n" +
        "Name=Bragi Client\n" +
        "Comment=Tray control for the Roc audio link to Bragi\n" +
        $"Exec={exec}\n" +
        "Terminal=false\n" +
        "X-GNOME-Autostart-enabled=true\n";

    /// <summary>The program part of the Exec= line, without quotes or arguments.</summary>
    public static string? ExecProgram(string desktopFileContent)
    {
        var line = desktopFileContent.Split('\n')
            .Select(l => l.Trim())
            .FirstOrDefault(l => l.StartsWith("Exec=", StringComparison.Ordinal));
        if (line is null)
        {
            return null;
        }

        var value = line["Exec=".Length..];
        if (value.StartsWith('"'))
        {
            var end = value.IndexOf('"', 1);
            return end > 0 ? value[1..end] : null;
        }

        var space = value.IndexOf(' ');
        return space < 0 ? value : value[..space];
    }

    private static string CurrentExecCommand() => ResolveExecCommand(
        Environment.GetEnvironmentVariable("APPIMAGE"),
        Environment.ProcessPath,
        Assembly.GetEntryAssembly()?.Location);

    /// <summary>
    /// Inside an AppImage the process path points into the AppImage's own
    /// /tmp/.mount_* dir, which only exists while it runs; the AppImage runtime
    /// sets $APPIMAGE to the .AppImage file itself, so prefer that.
    /// Otherwise dotnet's own process path is the "dotnet" muxer when
    /// framework-dependent (the dll path is a separate argument, not part of the
    /// process image), or the app binary itself when self-contained/published -
    /// distinguish by name rather than assuming one or the other.
    /// </summary>
    public static string ResolveExecCommand(string? appImage, string? processPath, string? entryAssemblyPath)
    {
        if (!string.IsNullOrEmpty(appImage))
        {
            return Quote(appImage);
        }

        if (processPath is null)
        {
            return $"dotnet {Quote(entryAssemblyPath ?? "")}";
        }

        var isDotnetHost = string.Equals(
            Path.GetFileNameWithoutExtension(processPath), "dotnet", StringComparison.OrdinalIgnoreCase);
        if (!isDotnetHost)
        {
            return Quote(processPath);
        }

        return $"{Quote(processPath)} {Quote(entryAssemblyPath ?? "")}";
    }

    // The desktop entry spec splits Exec= on spaces unless the argument is double-quoted.
    private static string Quote(string path) => path.Contains(' ') ? $"\"{path}\"" : path;
}
