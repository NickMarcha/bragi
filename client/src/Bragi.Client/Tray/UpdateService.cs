using System;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Threading;
using Velopack;
using Velopack.Sources;

namespace Bragi.Client.Tray;

/// <summary>
/// Velopack-driven self-update against this repo's GitHub Releases. Uses
/// the "linux" channel consistently (matches the client-release.yml
/// workflow's --channel linux on pack/upload) so this stays unambiguous if
/// non-client releases ever appear in the same repo's release list.
/// </summary>
public static class UpdateService
{
    private const string RepoUrl = "https://github.com/NickMarcha/bragi";
    private const string Channel = "linux";

    private static UpdateManager BuildManager() =>
        new(new GithubSource(RepoUrl, accessToken: null, prerelease: false), new UpdateOptions { ExplicitChannel = Channel });

    // Guards against two overlapping update runs. CheckForUpdatesInteractive
    // had no such guard - spam-clicking "Check for Updates" fired a fresh
    // Task.Run per click, and multiple concurrent DownloadUpdatesAsync calls
    // raced on the same on-disk package cache
    // (/var/tmp/velopack/<app>/packages/), corrupting each other's partial
    // download. Confirmed: the GitHub release itself checks out fine
    // (manually downloaded, size and SHA256 both match the manifest), but
    // the app's own log showed three overlapping download attempts within
    // about a minute right after the observed "Size doesn't match" error.
    private static int _isChecking;

    /// <summary>Silent, best-effort check - call once at startup. Never throws.</summary>
    public static async Task CheckSilentlyAsync()
    {
        if (Interlocked.CompareExchange(ref _isChecking, 1, 0) != 0)
        {
            return; // an interactive or startup check is already running
        }
        try
        {
            var mgr = BuildManager();
            if (!mgr.IsInstalled)
            {
                return; // running from `dotnet run`/unpackaged - nothing to update
            }
            var info = await mgr.CheckForUpdatesAsync();
            if (info is null)
            {
                return;
            }
            await mgr.DownloadUpdatesAsync(info);
            mgr.ApplyUpdatesAndRestart(info);
        }
        catch
        {
            // best-effort only - a failed background check shouldn't interrupt the tray app
        }
        finally
        {
            Interlocked.Exchange(ref _isChecking, 0);
        }
    }

    /// <summary>Tray menu's "Check for Updates" - same flow, but reports the result visibly.</summary>
    public static void CheckForUpdatesInteractive()
    {
        if (Interlocked.CompareExchange(ref _isChecking, 1, 0) != 0)
        {
            // Already running (a previous click, or the startup silent check) -
            // ignore rather than starting a second overlapping download. Give
            // visible feedback here specifically because a spam-clicked no-op
            // that looks identical to a hung app is what led to this bug being
            // found in the first place.
            Dispatcher.UIThread.Post(() => new Window
            {
                Title = "Bragi Client",
                Width = 360,
                Height = 120,
                CanResize = false,
                Content = new TextBlock { Text = "Already checking for updates - please wait.", Margin = new Avalonia.Thickness(16), TextWrapping = Avalonia.Media.TextWrapping.Wrap },
            }.Show());
            return;
        }

        _ = Task.Run(async () =>
        {
            string message;
            try
            {
                var mgr = BuildManager();
                if (!mgr.IsInstalled)
                {
                    message = "Not running as an installed build (e.g. `dotnet run`) - nothing to update.";
                }
                else
                {
                    var info = await mgr.CheckForUpdatesAsync();
                    if (info is null)
                    {
                        message = "Already on the latest version.";
                    }
                    else
                    {
                        await mgr.DownloadUpdatesAsync(info);
                        Dispatcher.UIThread.Post(() => mgr.ApplyUpdatesAndRestart(info));
                        return;
                    }
                }
            }
            catch (Exception ex)
            {
                message = $"Update check failed: {ex.Message}";
            }
            finally
            {
                Interlocked.Exchange(ref _isChecking, 0);
            }

            Dispatcher.UIThread.Post(() => new Window
            {
                Title = "Bragi Client",
                Width = 360,
                Height = 120,
                CanResize = false,
                Content = new TextBlock { Text = message, Margin = new Avalonia.Thickness(16), TextWrapping = Avalonia.Media.TextWrapping.Wrap },
            }.Show());
        });
    }
}
