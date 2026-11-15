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
    /// <summary>The Bragi host's address. Read from BRAGI_HOST_IP, or from
    /// SAGEPI_TAILSCALE_IP, its name before 0.1.7.</summary>
    public required string BragiHostIp { get; init; }
    /// <summary>The Roc link's PipeWire node names. Empty on Windows, whose
    /// link is VBAN through Voicemeeter.</summary>
    public required string LocalSinkName { get; init; }
    public required string LocalSourceName { get; init; }

    /// <summary>
    /// Windows only: the VBAN port and stream names the server handed out
    /// (app/peers.py's register_vban_desktop_peer). StreamSend is what the
    /// Bragi host sends here (its mic), StreamReceive what this computer sends
    /// back (its audio).
    /// </summary>
    public int? VbanPort { get; init; }
    public string? VbanStreamSend { get; init; }
    public string? VbanStreamReceive { get; init; }

    /// <summary>
    /// This machine's peer name as known to the Bragi server (peers.yaml's
    /// Peer.name - e.g. "laptop"), and the base wss:// URL for
    /// its presence heartbeat (app/main.py's /ws/peer/{name}). Both
    /// optional - an env file predating this feature (or without Bragi
    /// Serve/DNS set up) just means PeerPresenceClient never starts; every
    /// other feature works the same without it.
    /// </summary>
    public string? PeerName { get; init; }
    public string? BragiWsBaseUrl { get; init; }

    /// <summary>
    /// An API key made in the dashboard's Security dialog. Optional: the
    /// volume window uses the dashboard's /ws, which needs one once the
    /// web UI has a password. The presence heartbeat never does.
    /// </summary>
    public string? BragiApiKey { get; init; }

    public static string ConfigDir => OperatingSystem.IsWindows()
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "bragi-client")
        : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".config", "bragi-client");

    /// <summary>Same KEY=VALUE format on both systems; on Windows nothing but
    /// this app reads it, so it's named for what it holds.</summary>
    public static string EnvFilePath => Path.Combine(ConfigDir, OperatingSystem.IsWindows() ? "link.env" : "roc-link.env");

    public static string UnitFilePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        ".config", "systemd", "user", "bragi-roc-link.service");

    public static string RunScriptPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        ".local", "bin", "bragi-roc-link-run.sh");

    /// <summary>
    /// True if the systemd unit, the wrapper script, and the env file all
    /// exist - i.e. `client/systemd/install.sh` has been run on this
    /// machine, or "Connect to Bragi..." has set it up (Tray/LinkInstaller.cs).
    /// On Windows there's no unit: the env file alone means "Connect to Bragi" has run.
    /// </summary>
    public static bool IsInstalled() => OperatingSystem.IsWindows()
        ? File.Exists(EnvFilePath)
        : File.Exists(UnitFilePath) && File.Exists(RunScriptPath) && File.Exists(EnvFilePath);

    /// <summary>Every KEY=VALUE pair in the env file, or null if it doesn't exist.</summary>
    public static Dictionary<string, string>? ReadValues()
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
        return values;
    }

    public static RocLinkConfig? TryLoad()
    {
        var values = ReadValues();
        if (values is null)
        {
            return null;
        }

        return FromValues(values, requireRocNodes: !OperatingSystem.IsWindows());
    }

    /// <summary>The config in these values, or null if one it needs is missing.</summary>
    public static RocLinkConfig? FromValues(IReadOnlyDictionary<string, string> values, bool requireRocNodes)
    {
        if (!(values.TryGetValue("BRAGI_HOST_IP", out var ip) || values.TryGetValue("SAGEPI_TAILSCALE_IP", out ip)))
        {
            return null;
        }
        values.TryGetValue("LOCAL_SINK_NAME", out var sink);
        values.TryGetValue("LOCAL_SOURCE_NAME", out var source);
        if (requireRocNodes && (string.IsNullOrEmpty(sink) || string.IsNullOrEmpty(source)))
        {
            return null;
        }

        values.TryGetValue("PEER_NAME", out var peerName);
        values.TryGetValue("BRAGI_WS_URL", out var wsUrl);
        values.TryGetValue("BRAGI_API_KEY", out var apiKey);
        values.TryGetValue("VBAN_PORT", out var vbanPortText);
        values.TryGetValue("VBAN_STREAM_SEND", out var vbanSend);
        values.TryGetValue("VBAN_STREAM_RECEIVE", out var vbanReceive);

        return new RocLinkConfig
        {
            BragiHostIp = ip,
            LocalSinkName = sink ?? "",
            LocalSourceName = source ?? "",
            VbanPort = int.TryParse(vbanPortText, out var vbanPort) ? vbanPort : null,
            VbanStreamSend = string.IsNullOrWhiteSpace(vbanSend) ? null : vbanSend,
            VbanStreamReceive = string.IsNullOrWhiteSpace(vbanReceive) ? null : vbanReceive,
            PeerName = string.IsNullOrWhiteSpace(peerName) ? null : peerName,
            BragiWsBaseUrl = string.IsNullOrWhiteSpace(wsUrl) ? null : wsUrl,
            BragiApiKey = string.IsNullOrWhiteSpace(apiKey) ? null : apiKey,
        };
    }
}
