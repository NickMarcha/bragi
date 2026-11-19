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
/// build, what `dotnet build` produces), a self-contained/published binary,
/// or the Velopack AppImage, without needing to know which in advance.
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
            File.WriteAllText(DesktopFilePath, BuildDesktopFileContent(CurrentExecCommand()));
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

    /// <summary>
    /// Rewrites an existing Linux entry whose Exec= program no longer exists,
    /// such as one written from inside an AppImage's /tmp/.mount_* dir, which
    /// is gone once the app exits. Working entries are left alone, so running
    /// a dev build doesn't take the entry over from the installed one.
    /// </summary>
    public static void RepairIfStale()
    {
        if (!OperatingSystem.IsWindows())
        {
            RepairIfStale(DesktopFilePath, CurrentExecCommand(), File.Exists);
        }
    }

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
    /// Inside an AppImage the process path points into its /tmp/.mount_* dir,
    /// which only exists while it runs. The AppImage runtime sets $APPIMAGE to
    /// the .AppImage file itself, so that wins. Otherwise the process path is
    /// the "dotnet" muxer for a framework-dependent build (the dll is a separate
    /// argument) or the app binary itself when published; tell them apart by name.
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
