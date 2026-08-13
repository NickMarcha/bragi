using System;
using System.Collections.Generic;
using System.IO;

namespace Bragi.Client.Config;

/// <summary>
/// Reads the same ~/.config/bragi-client/roc-link.env file the systemd
/// unit's scripts consume via EnvironmentFile= - deliberately a trivial
/// KEY=VALUE line parser (no quoting/escaping rules) so both the shell
/// scripts and this C# reader agree on the format without sharing a schema.
/// </summary>
public sealed class RocLinkConfig
{
    public required string SagepiTailscaleIp { get; init; }
    public required string LocalSinkName { get; init; }
    public required string LocalSourceName { get; init; }

    /// <summary>
    /// This machine's peer name as known to the Bragi server (peers.yaml's
    /// Peer.name - e.g. "sage-dev", "sagedeck"), and the base wss:// URL for
    /// its presence heartbeat (app/main.py's /ws/peer/{name}). Both
    /// optional - an env file predating this feature (or without Bragi
    /// Serve/DNS set up) just means PeerPresenceClient never starts; every
    /// other feature works the same without it.
    /// </summary>
    public string? PeerName { get; init; }
    public string? BragiWsBaseUrl { get; init; }

    public static string ConfigDir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".config", "bragi-client");

    public static string EnvFilePath => Path.Combine(ConfigDir, "roc-link.env");

    public static string UnitFilePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        ".config", "systemd", "user", "bragi-roc-link.service");

    public static string RunScriptPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        ".local", "bin", "bragi-roc-link-run.sh");

    /// <summary>
    /// True if the systemd unit, the wrapper script, and the env file all
    /// exist - i.e. `client/systemd/install.sh` has been run on this
    /// machine. The app never installs these itself (see plan: no
    /// self-installing systemd units from a GUI app).
    /// </summary>
    public static bool IsInstalled() =>
        File.Exists(UnitFilePath) && File.Exists(RunScriptPath) && File.Exists(EnvFilePath);

    public static RocLinkConfig? TryLoad()
    {
        if (!File.Exists(EnvFilePath))
        {
            return null;
        }

        var values = new Dictionary<string, string>();
        foreach (var rawLine in File.ReadAllLines(EnvFilePath))
        {
            var line = rawLine.Trim();
            if (line.Length == 0 || line.StartsWith('#'))
            {
                continue;
            }
            var eq = line.IndexOf('=');
            if (eq <= 0)
            {
                continue;
            }
            values[line[..eq].Trim()] = line[(eq + 1)..].Trim();
        }

        if (!values.TryGetValue("SAGEPI_TAILSCALE_IP", out var ip) ||
            !values.TryGetValue("LOCAL_SINK_NAME", out var sink) ||
            !values.TryGetValue("LOCAL_SOURCE_NAME", out var source))
        {
            return null;
        }

        values.TryGetValue("PEER_NAME", out var peerName);
        values.TryGetValue("BRAGI_WS_URL", out var wsUrl);

        return new RocLinkConfig
        {
            SagepiTailscaleIp = ip,
            LocalSinkName = sink,
            LocalSourceName = source,
            PeerName = string.IsNullOrWhiteSpace(peerName) ? null : peerName,
            BragiWsBaseUrl = string.IsNullOrWhiteSpace(wsUrl) ? null : wsUrl,
        };
    }
}
