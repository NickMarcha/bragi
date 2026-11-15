using System;
using System.Linq;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using Microsoft.Win32;

namespace Bragi.Client.Tray;

/// <summary>
/// On Windows, a value under HKCU\...\CurrentVersion\Run. On Linux, manages the ~/.config/autostart/bragi-client.desktop entry (freedesktop
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

    // Windows: the per-user Run key, no admin rights needed.
    private const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string RunValueName = "BragiClient";

    public static bool IsEnabled()
    {
        if (OperatingSystem.IsWindows())
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKeyPath);
            return key?.GetValue(RunValueName) is string;
        }
        return File.Exists(DesktopFilePath);
    }

    public static void SetEnabled(bool enabled)
    {
        if (OperatingSystem.IsWindows())
        {
            using var key = Registry.CurrentUser.CreateSubKey(RunKeyPath);
            if (enabled)
            {
                key.SetValue(RunValueName, $"\"{WindowsLaunchPath(Environment.ProcessPath!)}\"");
            }
            else
            {
                key.DeleteValue(RunValueName, throwOnMissingValue: false);
            }
            return;
        }

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

    /// <summary>
    /// A Velopack install runs from &lt;root&gt;\current\, which an update
    /// replaces. The stub in &lt;root&gt; always starts the current version, so
    /// the Run entry points there when it exists.
    /// </summary>
    public static string WindowsLaunchPath(string processPath, Func<string, IEnumerable<string>>? exesIn = null)
    {
        exesIn ??= dir => Directory.Exists(dir) ? Directory.EnumerateFiles(dir, "*.exe") : [];
        var dir = Path.GetDirectoryName(processPath);
        if (dir is not null && string.Equals(Path.GetFileName(dir), "current", StringComparison.OrdinalIgnoreCase))
        {
            // The stub is named after the pack title ("Bragi Client.exe"), not
            // the main exe, and sits next to Velopack's Update.exe.
            var exes = exesIn(Path.GetDirectoryName(dir)!).ToList();
            var stub = exes.FirstOrDefault(e => !string.Equals(Path.GetFileName(e), "Update.exe", StringComparison.OrdinalIgnoreCase));
            if (stub is not null && exes.Count > 1)
            {
                return stub;
            }
        }
        return processPath;
    }

    private static string BuildDesktopFileContent()
    {
        var exec = ResolveExecCommand();
        return "[Desktop Entry]\n" +
               "Type=Application\n" +
               "Name=Bragi Client\n" +
               "Comment=Tray control for the Roc audio link to Bragi\n" +
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
