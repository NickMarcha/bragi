using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Bragi.Client.Config;

namespace Bragi.Client.Tray;

/// <summary>
/// The write half of "Connect to Bragi...": puts the link unit, its wrapper
/// script (both shipped inside the app, from client/systemd/) and the env
/// file in place, then enables and (re)starts the unit. client/systemd/
/// install.sh does the same by hand for anyone who prefers that.
/// </summary>
public static class LinkInstaller
{
    private const string UnitName = "bragi-roc-link.service";

    public static async Task InstallAsync(string envText, RocLinkController controller)
    {
        try
        {
            Directory.CreateDirectory(RocLinkConfig.ConfigDir);
            WriteFile(RocLinkConfig.EnvFilePath, envText,
                // It can hold an API key.
                UnixFileMode.UserRead | UnixFileMode.UserWrite);
            WriteFile(RocLinkConfig.RunScriptPath, Bundled("bragi-roc-link-run.sh"),
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute |
                UnixFileMode.GroupRead | UnixFileMode.GroupExecute | UnixFileMode.OtherRead | UnixFileMode.OtherExecute);
            WriteFile(RocLinkConfig.UnitFilePath, Bundled("bragi-roc-link.service"),
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.GroupRead | UnixFileMode.OtherRead);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            throw new LinkSetupException($"Could not write the link's files: {ex.Message}");
        }

        try
        {
            await ProcessRunner.RunOrThrowAsync("systemctl", ["--user", "daemon-reload"]);
            // Points the unit at the bundled, patched libroc. Needs the unit file to exist.
            await PatchedLibrocInstaller.EnsureInstalledAsync(controller);
            await ProcessRunner.RunOrThrowAsync("systemctl", ["--user", "enable", UnitName]);
        }
        catch (ProcessRunException ex)
        {
            throw new LinkSetupException($"systemctl --user failed: {ex.Message}");
        }

        // Restart, not start: a running link has to pick up new ports.
        if (!await controller.RestartAsync())
        {
            throw new LinkSetupException("The link didn't start. Check: journalctl --user -u bragi-roc-link");
        }
    }

    /// <summary>
    /// Which installed files (by bundled name) differ from the copies this
    /// app ships, compared without CRLF. A missing file isn't stale: that's
    /// for "Connect to Bragi..." to set up.
    /// </summary>
    public static IReadOnlyList<string> Stale(IReadOnlyDictionary<string, string?> installed, IReadOnlyDictionary<string, string> bundled) =>
        bundled
            .Where(pair => installed.TryGetValue(pair.Key, out var text) && text is not null
                && text.Replace("\r\n", "\n", StringComparison.Ordinal) != pair.Value)
            .Select(pair => pair.Key)
            .Order(StringComparer.Ordinal)
            .ToList();

    /// <summary>
    /// At startup on Linux: puts this version's script and unit in place when
    /// the installed ones differ, then points the unit at the patched libroc.
    /// Only "Connect to Bragi..." wrote them before, so a machine kept the
    /// copy from its first setup: on 2026-11-21 sagedeck's link had been
    /// failing for hours on an August script that still wanted
    /// SAGEPI_TAILSCALE_IP. The old file is kept as *.bak-yyyyMMdd. A link
    /// that was on (or failing) restarts; one turned off stays off.
    /// </summary>
    public static async Task StartupAsync(RocLinkController controller)
    {
        try
        {
            if (RocLinkConfig.IsInstalled())
            {
                await RefreshAsync(controller);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or LinkSetupException or ProcessRunException)
        {
            Console.Error.WriteLine($"Bragi: couldn't update the link's files: {ex.Message}");
        }
        await PatchedLibrocInstaller.EnsureInstalledAsync(controller);
    }

    private static async Task RefreshAsync(RocLinkController controller)
    {
        var paths = new Dictionary<string, string>
        {
            ["bragi-roc-link-run.sh"] = RocLinkConfig.RunScriptPath,
            ["bragi-roc-link.service"] = RocLinkConfig.UnitFilePath,
        };
        var installed = paths.ToDictionary(p => p.Key, p => File.Exists(p.Value) ? (string?)File.ReadAllText(p.Value) : null);
        var bundled = paths.Keys.ToDictionary(name => name, Bundled);
        var stale = Stale(installed, bundled);
        if (stale.Count == 0)
        {
            return;
        }
        var stamp = DateTime.Now.ToString("yyyyMMdd", System.Globalization.CultureInfo.InvariantCulture);
        foreach (var name in stale)
        {
            File.Copy(paths[name], $"{paths[name]}.bak-{stamp}", overwrite: true);
        }
        if (stale.Contains("bragi-roc-link-run.sh"))
        {
            WriteFile(RocLinkConfig.RunScriptPath, bundled["bragi-roc-link-run.sh"],
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute |
                UnixFileMode.GroupRead | UnixFileMode.GroupExecute | UnixFileMode.OtherRead | UnixFileMode.OtherExecute);
        }
        if (stale.Contains("bragi-roc-link.service"))
        {
            WriteFile(RocLinkConfig.UnitFilePath, bundled["bragi-roc-link.service"],
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.GroupRead | UnixFileMode.OtherRead);
        }
        await ProcessRunner.RunOrThrowAsync("systemctl", ["--user", "daemon-reload"]);
        Console.Error.WriteLine($"Bragi: updated the link's {string.Join(" and ", stale)} (old copies kept as .bak-{stamp})");
        if (await controller.GetStateAsync() != UnitState.Inactive)
        {
            await controller.RestartAsync();
        }
    }

    /// <summary>
    /// pipewire.conf.d files that already load Roc modules. Left in place,
    /// they bind the same ports as the unit; the window warns about them.
    /// </summary>
    public static IReadOnlyList<string> ConflictingDropIns(string confDir)
    {
        if (!Directory.Exists(confDir))
        {
            return [];
        }
        return Directory.EnumerateFiles(confDir, "*.conf")
            .Where(file => File.ReadAllText(file).Contains("libpipewire-module-roc-", StringComparison.Ordinal))
            .Order(StringComparer.Ordinal)
            .ToList();
    }

    public static string PipewireConfDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".config", "pipewire", "pipewire.conf.d");

    private static string Bundled(string name)
    {
        using var stream = typeof(LinkInstaller).Assembly.GetManifestResourceStream(name)
            ?? throw new LinkSetupException($"This build is missing its copy of {name}.");
        using var reader = new StreamReader(stream);
        // A checkout on Windows can turn these into CRLF, which bash rejects.
        return reader.ReadToEnd().Replace("\r\n", "\n", StringComparison.Ordinal);
    }

    private static void WriteFile(string path, string content, UnixFileMode mode)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temp = path + ".new";
        File.WriteAllText(temp, content);
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(temp, mode);
        }
        File.Move(temp, path, overwrite: true);
    }
}
