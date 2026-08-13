using System;
using System.IO;
using System.Reflection;

namespace Bragi.Client.Tray;

/// <summary>
/// Manages the ~/.config/autostart/bragi-client.desktop entry (freedesktop
/// XDG autostart spec) so "Start at Login" can be a plain checkable tray
/// menu item instead of something the user has to set up by hand per
/// machine. Resolves the Exec= line from the running process itself so it
/// works whether launched via `dotnet Bragi.Client.dll` (framework-dependent
/// build, what `dotnet build` produces) or a self-contained/published binary
/// (e.g. a future Velopack install) without needing to know which in advance.
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
            File.WriteAllText(DesktopFilePath, BuildDesktopFileContent());
        }
        else if (File.Exists(DesktopFilePath))
        {
            File.Delete(DesktopFilePath);
        }
    }

    private static string BuildDesktopFileContent()
    {
        var exec = ResolveExecCommand();
        return "[Desktop Entry]\n" +
               "Type=Application\n" +
               "Name=Bragi Client\n" +
               "Comment=Tray control for the Roc audio link to sagepi\n" +
               $"Exec={exec}\n" +
               "Terminal=false\n" +
               "X-GNOME-Autostart-enabled=true\n";
    }

    /// <summary>
    /// dotnet's own process path is the "dotnet" muxer when framework-dependent
    /// (the dll path is a separate argument, not part of the process image), or
    /// the app binary itself when self-contained/published - distinguish by name
    /// rather than assuming one or the other.
    /// </summary>
    private static string ResolveExecCommand()
    {
        var processPath = Environment.ProcessPath;
        if (processPath is null)
        {
            return $"dotnet {Assembly.GetEntryAssembly()?.Location}";
        }

        var isDotnetHost = string.Equals(
            Path.GetFileNameWithoutExtension(processPath), "dotnet", StringComparison.OrdinalIgnoreCase);
        if (!isDotnetHost)
        {
            return processPath;
        }

        var dllPath = Assembly.GetEntryAssembly()?.Location;
        return $"{processPath} {dllPath}";
    }
}
