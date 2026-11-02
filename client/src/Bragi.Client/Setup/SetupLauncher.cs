using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.NetworkInformation;
using Bragi.Client.Config;
using Bragi.Client.Tray;

namespace Bragi.Client.Setup;

/// <summary>Opens "Connect to Bragi..." filled in from this machine and its current config.</summary>
public static class SetupLauncher
{
    private static SetupWindow? _open;

    public static void Show(Action onConnected)
    {
        if (_open is not null)
        {
            _open.Activate();
            return;
        }

        var existing = RocLinkConfig.ReadValues();
        var config = RocLinkConfig.TryLoad();
        var defaults = new SetupDefaults(
            Server: BragiServerAddress.FromPresenceUrl(config?.BragiWsBaseUrl)?.WebUi.ToString() ?? "",
            PeerName: config?.PeerName ?? LinkSetup.SuggestedPeerName(Environment.MachineName),
            PeerIp: LinkSetup.TailnetIpv4(LocalAddresses()) ?? "",
            ApiKey: config?.BragiApiKey ?? "",
            Warnings: LinkInstaller.ConflictingDropIns(LinkInstaller.PipewireConfDir)
                .Select(file => $"{file} already loads Roc modules and would clash with the link. " +
                                "Rename it to end in .disabled and restart pipewire.service once.")
                .ToList());

        var window = new SetupWindow(defaults, async (server, name, ip, apiKey) =>
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
            await LinkSetup.ConnectAsync(server, name, ip, apiKey, http,
                host => Dns.GetHostAddressesAsync(host),
                RocModuleDir(existing),
                existing,
                env => LinkInstaller.InstallAsync(env, new RocLinkController()));
        });
        window.Connected += onConnected;
        window.Closed += (_, _) => _open = null;
        _open = window;
        window.Show();
    }

    private static string? RocModuleDir(IReadOnlyDictionary<string, string>? existing)
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var candidates = LinkSetup.RocModuleDirCandidates(home);
        // A directory set by hand earlier, e.g. a local build, wins if it still works.
        if (existing is not null && existing.TryGetValue("ROC_MODULE_DIR", out var current))
        {
            candidates = candidates.Prepend(current);
        }
        return LinkSetup.FindRocModuleDir(candidates, File.Exists);
    }

    private static IEnumerable<(string, IPAddress)> LocalAddresses()
    {
        try
        {
            return NetworkInterface.GetAllNetworkInterfaces()
                .SelectMany(nic => nic.GetIPProperties().UnicastAddresses.Select(a => (nic.Name, a.Address)))
                .ToList();
        }
        catch (NetworkInformationException)
        {
            return [];
        }
    }
}
