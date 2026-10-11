using System;
using System.IO;
using System.Security.Cryptography;
using System.Threading.Tasks;

namespace Bragi.Client.Tray;

/// <summary>
/// Installs the libroc.so.0.4 bundled with this release (built by
/// client/roc/build-libroc.sh, roc-toolkit 0.4.0 plus a wraparound fix) and
/// points bragi-roc-link.service at it with a systemd drop-in that sets
/// LD_LIBRARY_PATH. Only the pw-cli hosting the Roc modules picks it up; the
/// distro libroc stays untouched for everything else.
///
/// The distro 0.4.0 build hangs PipeWire's data-loop.0 after 2^32 samples
/// (~24.9h of streaming at 48kHz): LatencyTuner::report_() compares uint32
/// stream positions with a plain >=, and once report_pos_ wraps the loop can
/// never exit. That was the "Roc link dies every couple of days" bug
/// (deck-assistant #081).
///
/// Runs at startup, so a Velopack update is all a client needs. Restarts the
/// link only when the library or drop-in actually changed, which is once per
/// release that changes them.
/// </summary>
public static class PatchedLibrocInstaller
{
    private const string LibName = "libroc.so.0.4";
    private const string UnitName = "bragi-roc-link.service";

    private static readonly string Home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
    private static readonly string LibDir = Path.Combine(Home, ".local", "lib", "bragi", "roc");
    private static readonly string UnitDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "systemd", "user");
    private static readonly string DropInPath = Path.Combine(UnitDir, UnitName + ".d", "50-bragi-libroc.conf");

    private const string DropInContent =
        "# Written by Bragi Client (PatchedLibrocInstaller). Loads the bundled,\n" +
        "# patched libroc instead of the distro one. See deck-assistant #081.\n" +
        "[Service]\n" +
        "Environment=LD_LIBRARY_PATH=%h/.local/lib/bragi/roc\n";

    public static async Task EnsureInstalledAsync(RocLinkController controller)
    {
        try
        {
            var bundled = Path.Combine(AppContext.BaseDirectory, "roc", LibName);
            // Dev builds don't carry the library, and a machine without the
            // link unit (install.sh never run) has nothing to point at it.
            if (!File.Exists(bundled) || !File.Exists(Path.Combine(UnitDir, UnitName)))
            {
                return;
            }

            var libChanged = CopyIfDifferent(bundled, Path.Combine(LibDir, LibName));
            var dropInChanged = WriteIfDifferent(DropInPath, DropInContent);
            if (!libChanged && !dropInChanged)
            {
                return;
            }

            await ProcessRunner.RunOrThrowAsync("systemctl", ["--user", "daemon-reload"]);
            if (await controller.GetStateAsync() == UnitState.Active)
            {
                await controller.RestartAsync();
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ProcessRunException)
        {
            Console.Error.WriteLine($"bragi-client: patched libroc install failed: {ex.Message}");
        }
    }

    private static bool CopyIfDifferent(string source, string target)
    {
        if (File.Exists(target) && Sha256(source).AsSpan().SequenceEqual(Sha256(target)))
        {
            return false;
        }

        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        // Copy beside the target and rename over it: a running pw-cli keeps
        // its mapping of the old inode instead of seeing a half-written file.
        var temp = target + ".new";
        File.Copy(source, temp, overwrite: true);
        File.SetUnixFileMode(temp, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute |
                                   UnixFileMode.GroupRead | UnixFileMode.GroupExecute |
                                   UnixFileMode.OtherRead | UnixFileMode.OtherExecute);
        File.Move(temp, target, overwrite: true);
        return true;
    }

    private static bool WriteIfDifferent(string path, string content)
    {
        if (File.Exists(path) && File.ReadAllText(path) == content)
        {
            return false;
        }

        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, content);
        return true;
    }

    private static byte[] Sha256(string path)
    {
        using var stream = File.OpenRead(path);
        return SHA256.HashData(stream);
    }
}
