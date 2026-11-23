using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Bragi.Client.Config;

/// <summary>Where the Bragi hub is, as it says in its "hub" message (app/hub_info.py).</summary>
public sealed record HubInfo(string? WsUrl, string? TailscaleIp, string? LanIp, IReadOnlyList<string> DeskflowHosts,
    LanPeerInfo? LanPeer = null);

/// <summary>The hub's peer socket on the LAN (app/lan_peer.py, bragi#90): its
/// port, the SHA-256 of its certificate to pin, and this computer's token.</summary>
public sealed record LanPeerInfo(int Port, string CertSha256, string Token);

/// <summary>Where to reach the peer socket over the LAN while Tailscale is down.</summary>
public sealed record LanEndpoint(Uri Uri, string CertSha256, string Token);

/// <summary>
/// Keeps this computer's settings pointing at the hub (bragi#61). The hub
/// sends "hub" after this app's client_hello; whatever differs here gets
/// fixed: the address the audio goes to (BRAGI_HOST_IP) and the Deskflow
/// client's server addresses. "hub_moved" switches the server URL to
/// another hub. Pure logic; Tray/HubSyncService.cs does the writing.
/// </summary>
public static partial class HubSync
{
    public static HubInfo? ParseHub(JsonElement message)
    {
        if (Type(message) != "hub")
        {
            return null;
        }
        var hosts = message.TryGetProperty("deskflow_hosts", out var list) && list.ValueKind == JsonValueKind.Array
            ? list.EnumerateArray().Select(e => e.ValueKind == JsonValueKind.String ? e.GetString() : null)
                .Where(h => h is not null && IsHost(h)).Select(h => h!).ToList()
            : new List<string>();
        return new HubInfo(String(message, "ws_url"), Ipv4(String(message, "tailscale_ip")),
            Ipv4(String(message, "lan_ip")), hosts, ParseLanPeer(message));
    }

    private static LanPeerInfo? ParseLanPeer(JsonElement message)
    {
        if (!message.TryGetProperty("lan_peer", out var lan) || lan.ValueKind != JsonValueKind.Object
            || !lan.TryGetProperty("port", out var portElement) || portElement.ValueKind != JsonValueKind.Number
            || !portElement.TryGetInt32(out var port) || port is < 1 or > 65535)
        {
            return null;
        }
        var cert = String(lan, "cert_sha256");
        var token = String(lan, "token");
        return cert is not null && token is not null && Hex64().IsMatch(cert) && Hex64().IsMatch(token)
            ? new LanPeerInfo(port, cert, token) : null;
    }

    [GeneratedRegex("^[0-9a-f]{64}$")]
    private static partial Regex Hex64();

    /// <summary>The LAN endpoint from the env file's remembered values, or null
    /// when the hub never offered one (an older hub, or this app hasn't heard
    /// its "hub" message yet).</summary>
    public static LanEndpoint? LanEndpointFrom(IReadOnlyDictionary<string, string>? values, string? peerName)
    {
        if (values is null || string.IsNullOrEmpty(peerName)
            || !values.TryGetValue("BRAGI_HUB_LAN_IP", out var ip) || Ipv4(ip) is null
            || !values.TryGetValue("BRAGI_HUB_LAN_PORT", out var portText) || !int.TryParse(portText, out var port)
            || port is < 1 or > 65535
            || !values.TryGetValue("BRAGI_HUB_LAN_CERT", out var cert) || !Hex64().IsMatch(cert)
            || !values.TryGetValue("BRAGI_HUB_LAN_TOKEN", out var token) || !Hex64().IsMatch(token))
        {
            return null;
        }
        return new LanEndpoint(new Uri($"wss://{ip}:{port}/ws/peer/{Uri.EscapeDataString(peerName)}"), cert, token);
    }

    /// <summary>The new hub's peer-socket URL from a "hub_moved" message, or null.</summary>
    public static string? ParseMoved(JsonElement message)
    {
        if (Type(message) != "hub_moved")
        {
            return null;
        }
        var url = String(message, "ws_url");
        return url is not null && Uri.TryCreate(url, UriKind.Absolute, out var uri) && uri.Scheme == "wss"
            && uri.AbsolutePath.TrimEnd('/') == "/ws/peer" ? url : null;
    }

    /// <summary>The env file keys to change so the audio goes to the hub:
    /// a value to set, or null to remove the key. Empty when nothing differs.</summary>
    public static Dictionary<string, string?> EnvChanges(IReadOnlyDictionary<string, string> current, HubInfo hub)
    {
        var changes = new Dictionary<string, string?>();
        if (hub.TailscaleIp is { } ip)
        {
            current.TryGetValue("BRAGI_HOST_IP", out var now);
            if (now is null)
            {
                current.TryGetValue("SAGEPI_TAILSCALE_IP", out now);
            }
            if (now != ip)
            {
                changes["BRAGI_HOST_IP"] = ip;
            }
            if (current.ContainsKey("SAGEPI_TAILSCALE_IP"))
            {
                // The old name (before 0.1.7): one name, so nothing reads a stale copy.
                changes["BRAGI_HOST_IP"] = ip;
                changes["SAGEPI_TAILSCALE_IP"] = null;
            }
        }
        return changes;
    }

    /// <summary>Remembers the hub's LAN address (BRAGI_HUB_LAN_IP) for when
    /// Tailscale is down (bragi#81, Tray/LanFallbackService.cs), and its LAN
    /// peer socket (BRAGI_HUB_LAN_PORT, _CERT, _TOKEN; bragi#90). Nothing
    /// reads them at start, so changing them restarts nothing.</summary>
    public static Dictionary<string, string?> RememberChanges(IReadOnlyDictionary<string, string> current, HubInfo hub)
    {
        var changes = new Dictionary<string, string?>();
        if (hub.LanIp is { } lan && (!current.TryGetValue("BRAGI_HUB_LAN_IP", out var now) || now != lan))
        {
            changes["BRAGI_HUB_LAN_IP"] = lan;
        }
        if (hub.LanPeer is { } peer)
        {
            foreach (var (key, value) in new[] { ("BRAGI_HUB_LAN_PORT", peer.Port.ToString(System.Globalization.CultureInfo.InvariantCulture)),
                         ("BRAGI_HUB_LAN_CERT", peer.CertSha256), ("BRAGI_HUB_LAN_TOKEN", peer.Token) })
            {
                if (!current.TryGetValue(key, out var stored) || stored != value)
                {
                    changes[key] = value;
                }
            }
        }
        return changes;
    }

    /// <summary>The env file's lines with these changes: keys set in place (or
    /// appended), removed ones dropped, everything else as it was.</summary>
    public static List<string> UpdateEnvLines(IEnumerable<string> lines, IReadOnlyDictionary<string, string?> changes)
    {
        var done = new HashSet<string>();
        var output = new List<string>();
        foreach (var line in lines)
        {
            var eq = line.IndexOf('=');
            var key = eq > 0 && !line.TrimStart().StartsWith('#') ? line[..eq].Trim() : null;
            if (key is not null && changes.TryGetValue(key, out var value))
            {
                if (value is not null && done.Add(key))
                {
                    output.Add($"{key}={value}");
                }
                continue;
            }
            output.Add(line);
        }
        output.AddRange(changes.Where(c => c.Value is not null && !done.Contains(c.Key)).Select(c => $"{c.Key}={c.Value}"));
        return output;
    }

    /// <summary>Deskflow's remoteHost value for these addresses. Deskflow
    /// stores the list with a literal backslash-n at the end.</summary>
    public static string DeskflowRemoteHost(IReadOnlyList<string> hosts) => string.Join(", ", hosts) + "\\n";

    /// <summary>The Deskflow config with remoteHost pointing at the hub, or
    /// null when there's nothing to change: no hosts, no remoteHost line
    /// (Deskflow isn't set up as a client here), or it already matches.</summary>
    public static string? UpdateDeskflowConf(string text, IReadOnlyList<string> hosts)
    {
        if (hosts.Count == 0)
        {
            return null;
        }
        var match = RemoteHostLine().Match(text);
        if (!match.Success)
        {
            return null;
        }
        var wanted = $"remoteHost=\"{DeskflowRemoteHost(hosts)}\"";
        return match.Value == wanted ? null : text[..match.Index] + wanted + text[(match.Index + match.Length)..];
    }

    [GeneratedRegex("^remoteHost=\"[^\"\\r\\n]*\"", RegexOptions.Multiline)]
    private static partial Regex RemoteHostLine();

    [GeneratedRegex("^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")]
    private static partial Regex HostName();

    private static bool IsHost(string? host) => host is not null && (IPAddress.TryParse(host, out _) || HostName().IsMatch(host));

    private static string? Type(JsonElement m) =>
        m.ValueKind == JsonValueKind.Object && m.TryGetProperty("type", out var t) && t.ValueKind == JsonValueKind.String
            ? t.GetString() : null;

    private static string? String(JsonElement m, string name) =>
        m.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(v.GetString())
            ? v.GetString() : null;

    private static string? Ipv4(string? text) =>
        text is not null && IPAddress.TryParse(text, out var ip) && ip.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork
            ? text : null;
}
