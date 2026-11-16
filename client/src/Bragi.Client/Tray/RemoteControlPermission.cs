using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;

namespace Bragi.Client.Tray;

/// <summary>
/// KDE asks "Allow remote control?" every time the Deskflow client starts,
/// and until someone answers at this computer the keyboard and mouse can't
/// come over. KDE's portal skips the question for an app that's
/// "mega-authorized": the permission store's kde-authorized table, entry
/// remote-desktop, app id -> "yes" (xdg-desktop-portal-kde,
/// src/remotedesktop.cpp). Deskflow runs as org.deskflow.deskflow (its
/// desktop file's id, from the autostart scope).
///
/// The tray shows it as a checkbox on KDE, so it's a choice made once per
/// computer, not something Bragi does behind your back: with it on, that
/// app id can control the keyboard and mouse without asking.
/// </summary>
public static class RemoteControlPermission
{
    public const string AppId = "org.deskflow.deskflow";
    private const string Service = "org.freedesktop.impl.portal.PermissionStore";
    private const string ObjectPath = "/org/freedesktop/impl/portal/PermissionStore";
    private const string Interface = "org.freedesktop.impl.portal.PermissionStore";

    /// <summary>A KDE session with Deskflow installed: where the checkbox makes sense.</summary>
    public static bool Applies() =>
        OperatingSystem.IsLinux()
        && (Environment.GetEnvironmentVariable("XDG_CURRENT_DESKTOP") ?? "").Split(':').Contains("KDE", StringComparer.OrdinalIgnoreCase)
        && (File.Exists("/usr/share/applications/org.deskflow.deskflow.desktop")
            || File.Exists("/usr/local/share/applications/org.deskflow.deskflow.desktop"));

    /// <summary>Whether the app id has "yes", from busctl's JSON for Lookup's a{sas}.</summary>
    public static bool GrantedIn(string lookupJson, string appId = AppId)
    {
        try
        {
            using var doc = JsonDocument.Parse(lookupJson);
            // {"type":"a{sas}v","data":[{"org.deskflow.deskflow":["yes"]},{...}]}
            var map = doc.RootElement.GetProperty("data")[0];
            return map.TryGetProperty(appId, out var permissions)
                   && permissions.EnumerateArray().Any(p => p.GetString() == "yes");
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or IndexOutOfRangeException or System.Collections.Generic.KeyNotFoundException)
        {
            return false;
        }
    }

    public static async Task<bool> IsGrantedAsync()
    {
        var result = await ProcessRunner.RunAsync("busctl",
            ["--user", "--json=short", "call", Service, ObjectPath, Interface, "Lookup", "ss", "kde-authorized", "remote-desktop"],
            TimeSpan.FromSeconds(3));
        // "No entry for remote-desktop" is an error reply: nothing granted yet.
        return result.ExitCode == 0 && GrantedIn(result.Stdout);
    }

    public static Task SetAsync(bool allowed) => allowed
        ? ProcessRunner.RunOrThrowAsync("busctl",
            ["--user", "call", Service, ObjectPath, Interface, "SetPermission", "sbssas",
             "kde-authorized", "true", "remote-desktop", AppId, "1", "yes"], TimeSpan.FromSeconds(3))
        : ProcessRunner.RunOrThrowAsync("busctl",
            ["--user", "call", Service, ObjectPath, Interface, "DeletePermission", "sss",
             "kde-authorized", "remote-desktop", AppId], TimeSpan.FromSeconds(3));
}
