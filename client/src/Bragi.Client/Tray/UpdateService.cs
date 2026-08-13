using System;
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

    /// <summary>Silent, best-effort check - call once at startup. Never throws.</summary>
    public static async Task CheckSilentlyAsync()
    {
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
    }

    /// <summary>Tray menu's "Check for Updates" - same flow, but reports the result visibly.</summary>
    public static void CheckForUpdatesInteractive()
    {
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
