using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace Bragi.Client.Config;

/// <summary>The six Roc ports the server allocated for this peer.</summary>
public sealed record RocPorts(
    int MicSource, int MicRepair, int MicControl,
    int PlaybackSource, int PlaybackRepair, int PlaybackControl);

/// <summary>
/// The VBAN port and stream names the server gave a Windows peer. Send is
/// what the Bragi host sends to this computer (the headset mic), Receive
/// what this computer sends to the host (its audio).
/// </summary>
public sealed record VbanStreams(int Port, string Send, string Receive);

/// <summary>What the "Connect to Bragi" window collected, plus what it looked up.</summary>
public sealed record LinkSettings(
    Uri Server, string PeerName, string HostIp, string? ApiKey, string RocModuleDir);

public sealed class LinkSetupException(string message) : Exception(message);

/// <summary>
/// The decisions behind "Connect to Bragi...", kept free of UI and systemd so
/// they can be tested: reading what the user typed, finding this machine's
/// tailnet address and Roc modules, registering with the server, and turning
/// the answer into roc-link.env.
/// </summary>
public static partial class LinkSetup
{
    public const string DefaultSinkName = "bragi-headset-sink";
    public const string DefaultSourceName = "bragi-mic";

    private static readonly IPNetwork Tailnet = IPNetwork.Parse("100.64.0.0/10");

    /// <summary>The dashboard address as typed: a bare host gets https://.</summary>
    public static Uri? ServerUri(string? input)
    {
        var text = input?.Trim();
        if (string.IsNullOrEmpty(text))
        {
            return null;
        }
        if (!text.Contains("://", StringComparison.Ordinal))
        {
            text = "https://" + text;
        }
        if (!Uri.TryCreate(text, UriKind.Absolute, out var uri) || (uri.Scheme != "https" && uri.Scheme != "http") ||
            !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment) || !string.IsNullOrEmpty(uri.UserInfo))
        {
            return null;
        }
        var builder = new UriBuilder(uri) { Path = uri.AbsolutePath.TrimEnd('/') + "/" };
        return builder.Uri;
    }

    /// <summary>BRAGI_WS_URL: the presence socket's base, ws(s)://host/ws/peer.</summary>
    public static Uri PresenceUrl(Uri server)
    {
        var builder = new UriBuilder(server)
        {
            Scheme = server.Scheme == "https" ? "wss" : "ws",
            Path = server.AbsolutePath + "ws/peer",
        };
        return builder.Uri;
    }

    public static bool IsTailnet(IPAddress address) =>
        address.AddressFamily == AddressFamily.InterNetwork && Tailnet.Contains(address);

    /// <summary>
    /// This machine's Tailscale IPv4 address. Tailscale's own interface wins:
    /// a carrier or VPN can hand out 100.64.0.0/10 addresses too.
    /// </summary>
    public static string? TailnetIpv4(IEnumerable<(string Interface, IPAddress Address)> addresses)
    {
        var candidates = addresses.Where(a => IsTailnet(a.Address)).ToList();
        var best = candidates.FirstOrDefault(a => a.Interface.StartsWith("tailscale", StringComparison.Ordinal));
        if (best.Address is null)
        {
            best = candidates.FirstOrDefault();
        }
        return best.Address?.ToString();
    }

    /// <summary>A peer name the server accepts, from the machine's hostname.</summary>
    public static string SuggestedPeerName(string hostName)
    {
        var name = NotNameChars().Replace(hostName.ToLowerInvariant(), "-").Trim('-');
        if (name.Length == 0 || !char.IsAsciiLetterLower(name[0]))
        {
            name = "desktop-" + name;
        }
        return name.Length > 40 ? name[..40].TrimEnd('-') : name.TrimEnd('-');
    }

    public static bool ValidPeerName(string name) => PeerName().IsMatch(name);

    /// <summary>Where distros put PipeWire's modules, then local builds.</summary>
    public static IEnumerable<string> RocModuleDirCandidates(string home)
    {
        yield return "/usr/lib/pipewire-0.3";
        yield return "/usr/lib64/pipewire-0.3";
        yield return "/usr/lib/x86_64-linux-gnu/pipewire-0.3";
        yield return "/usr/lib/aarch64-linux-gnu/pipewire-0.3";
        var local = Path.Combine(home, ".local", "lib");
        if (Directory.Exists(local))
        {
            foreach (var dir in Directory.EnumerateDirectories(local, "pipewire-0.3*").Order(StringComparer.Ordinal))
            {
                yield return dir;
            }
        }
    }

    /// <summary>The first directory holding both Roc modules, or null.</summary>
    public static string? FindRocModuleDir(IEnumerable<string> candidates, Func<string, bool> fileExists) =>
        candidates.FirstOrDefault(dir =>
            fileExists(Path.Combine(dir, "libpipewire-module-roc-sink.so")) &&
            fileExists(Path.Combine(dir, "libpipewire-module-roc-source.so")));

    /// <summary>
    /// Registers this machine as a Roc peer (app/main.py,
    /// /api/peers/register-desktop) and returns the ports it was given.
    /// Connecting again keeps the same ports.
    /// </summary>
    public static async Task<RocPorts> RegisterAsync(HttpClient http, Uri server, string peerName, string peerIp,
        CancellationToken cancellationToken = default)
    {
        var body = await PostRegistrationAsync(http, server, new { name = peerName, tailscale_ip = peerIp }, cancellationToken);
        try
        {
            var ports = JsonDocument.Parse(body).RootElement.GetProperty("ports");
            int Port(string key) => ports.GetProperty(key).GetInt32();
            return new RocPorts(Port("mic_source"), Port("mic_repair"), Port("mic_control"),
                Port("playback_source"), Port("playback_repair"), Port("playback_control"));
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException)
        {
            throw new LinkSetupException("Bragi answered with something other than this peer's ports.");
        }
    }

    /// <summary>
    /// Registers this machine as a VBAN peer (the Windows client) and returns
    /// its port and stream names. A VBAN peer already added from the
    /// dashboard under this name keeps its stream names.
    /// </summary>
    public static async Task<VbanStreams> RegisterVbanAsync(HttpClient http, Uri server, string peerName, string peerIp,
        CancellationToken cancellationToken = default)
    {
        var body = await PostRegistrationAsync(http, server,
            new { name = peerName, tailscale_ip = peerIp, protocol = "vban" }, cancellationToken);
        try
        {
            var vban = JsonDocument.Parse(body).RootElement.GetProperty("vban");
            return new VbanStreams(vban.GetProperty("port").GetInt32(),
                vban.GetProperty("stream_send").GetString()!, vban.GetProperty("stream_receive").GetString()!);
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException)
        {
            throw new LinkSetupException("This Bragi server is too old for a Windows client. Update it first.");
        }
    }

    /// <summary>POSTs to /api/peers/register-desktop and returns the body of a successful answer.</summary>
    private static async Task<string> PostRegistrationAsync(HttpClient http, Uri server, object registration,
        CancellationToken cancellationToken)
    {
        HttpResponseMessage response;
        try
        {
            response = await http.PostAsJsonAsync(new Uri(server, "api/peers/register-desktop"),
                registration, cancellationToken);
        }
        catch (HttpRequestException ex)
        {
            throw new LinkSetupException($"Could not reach Bragi at {server}: {ex.Message}");
        }
        catch (TaskCanceledException)
        {
            throw new LinkSetupException($"Bragi at {server} did not answer.");
        }

        using (response)
        {
            var body = await response.Content.ReadAsStringAsync(cancellationToken);
            if (response.StatusCode == HttpStatusCode.NotFound)
            {
                throw new LinkSetupException("This Bragi server is too old for Connect to Bragi. Update it, or set the link up by hand (client/README.md).");
            }
            if (!response.IsSuccessStatusCode)
            {
                throw new LinkSetupException(ErrorDetail(body) ?? $"Bragi refused the registration ({(int)response.StatusCode}).");
            }
            return body;
        }
    }

    /// <summary>
    /// The whole of "Connect": check what was typed, look up the Bragi host's
    /// address and the Roc modules, register, and hand the env file to
    /// <paramref name="install"/>. Throws LinkSetupException with a message
    /// for the window on anything the user can fix.
    /// </summary>
    public static async Task<LinkSettings> ConnectAsync(
        string serverText, string peerName, string peerIp, string? apiKey,
        HttpClient http,
        Func<string, Task<IPAddress[]>> resolveHost,
        string? rocModuleDir,
        IReadOnlyDictionary<string, string>? existingEnv,
        Func<string, Task> install,
        CancellationToken cancellationToken = default)
    {
        var (server, ip) = CheckInput(serverText, ref peerName, peerIp);
        if (rocModuleDir is null)
        {
            throw new LinkSetupException("PipeWire's Roc modules aren't installed. On Arch that's the pipewire-roc package.");
        }
        var host = await ResolveHostAsync(server, resolveHost);

        var ports = await RegisterAsync(http, server, peerName, ip.ToString(), cancellationToken);
        var settings = new LinkSettings(server, peerName, host.ToString(), apiKey, rocModuleDir);
        await install(BuildEnv(settings, ports, existingEnv));
        return settings;
    }

    /// <summary>
    /// "Connect" on Windows: the same checks and lookup, but the computer
    /// registers as a VBAN peer and there's no Roc or systemd to set up.
    /// </summary>
    public static async Task<LinkSettings> ConnectVbanAsync(
        string serverText, string peerName, string peerIp, string? apiKey,
        HttpClient http,
        Func<string, Task<IPAddress[]>> resolveHost,
        Func<string, Task> install,
        CancellationToken cancellationToken = default)
    {
        var (server, ip) = CheckInput(serverText, ref peerName, peerIp);
        var host = await ResolveHostAsync(server, resolveHost);

        var streams = await RegisterVbanAsync(http, server, peerName, ip.ToString(), cancellationToken);
        var settings = new LinkSettings(server, peerName, host.ToString(), apiKey, RocModuleDir: "");
        await install(BuildVbanEnv(settings, streams));
        return settings;
    }

    private static (Uri Server, IPAddress Ip) CheckInput(string serverText, ref string peerName, string peerIp)
    {
        var server = ServerUri(serverText)
            ?? throw new LinkSetupException("Enter the Bragi dashboard's address, e.g. https://bragi.your-tailnet.ts.net/.");
        peerName = peerName.Trim();
        if (!ValidPeerName(peerName))
        {
            throw new LinkSetupException("The name must start with a letter and use lowercase letters, digits or hyphens, up to 40 characters.");
        }
        if (!IPAddress.TryParse(peerIp.Trim(), out var ip) || !IsTailnet(ip))
        {
            throw new LinkSetupException("Enter this computer's Tailscale IPv4 address (100.64.x.x to 100.127.x.x). Is Tailscale running?");
        }
        return (server, ip);
    }

    /// <summary>The Bragi host's address to send audio to: its tailnet IPv4 if it has one.</summary>
    private static async Task<IPAddress> ResolveHostAsync(Uri server, Func<string, Task<IPAddress[]>> resolveHost)
    {
        IPAddress[] hostAddresses;
        try
        {
            hostAddresses = await resolveHost(server.Host);
        }
        catch (SocketException)
        {
            throw new LinkSetupException($"Could not find {server.Host}. Is Tailscale running?");
        }
        var host = hostAddresses.FirstOrDefault(IsTailnet)
            ?? hostAddresses.FirstOrDefault(a => a.AddressFamily == AddressFamily.InterNetwork)
            ?? throw new LinkSetupException($"{server.Host} has no IPv4 address to send audio to.");
        return host;
    }

    /// <summary>FastAPI's error detail: a string, or a list of validation errors.</summary>
    private static string? ErrorDetail(string body)
    {
        try
        {
            var detail = JsonDocument.Parse(body).RootElement.GetProperty("detail");
            if (detail.ValueKind == JsonValueKind.String)
            {
                return detail.GetString();
            }
            if (detail.ValueKind == JsonValueKind.Array && detail.GetArrayLength() > 0 &&
                detail[0].TryGetProperty("msg", out var msg))
            {
                return msg.GetString()?.Replace("Value error, ", "", StringComparison.Ordinal);
            }
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException)
        {
        }
        return null;
    }

    /// <summary>
    /// roc-link.env for these settings. The server's mic ports are where this
    /// machine listens (its roc-source); its playback ports are where this
    /// machine sends (its roc-sink). Node names and latency carry over from
    /// an existing file, so WirePlumber keeps its default device choices.
    /// </summary>
    public static string BuildEnv(LinkSettings settings, RocPorts ports, IReadOnlyDictionary<string, string>? existing)
    {
        string Keep(string key, string fallback) =>
            existing is not null && existing.TryGetValue(key, out var value) && value.Length > 0 ? value : fallback;

        var env = new StringBuilder();
        env.AppendLine("# Written by Bragi Client's \"Connect to Bragi...\". Running it again");
        env.AppendLine("# rewrites this file; LOCAL_*_NAME and SESS_LATENCY_MSEC are kept.");
        env.AppendLine("# See client/systemd/roc-link.env.example for what each value means.");
        env.AppendLine($"BRAGI_HOST_IP={settings.HostIp}");
        env.AppendLine($"ROC_MODULE_DIR={settings.RocModuleDir}");
        env.AppendLine($"PEER_NAME={settings.PeerName}");
        env.AppendLine($"BRAGI_WS_URL={PresenceUrl(settings.Server)}");
        if (!string.IsNullOrWhiteSpace(settings.ApiKey))
        {
            env.AppendLine($"BRAGI_API_KEY={settings.ApiKey.Trim()}");
        }
        env.AppendLine();
        env.AppendLine("# roc-sink: what plays into it goes to the Bragi host's headset.");
        env.AppendLine($"REMOTE_SOURCE_PORT={ports.PlaybackSource}");
        env.AppendLine($"REMOTE_REPAIR_PORT={ports.PlaybackRepair}");
        env.AppendLine($"REMOTE_CONTROL_PORT={ports.PlaybackControl}");
        env.AppendLine($"LOCAL_SINK_NAME={Keep("LOCAL_SINK_NAME", DefaultSinkName)}");
        env.AppendLine();
        env.AppendLine("# roc-source: the Bragi host's headset mic, received here.");
        env.AppendLine($"LOCAL_SOURCE_PORT={ports.MicSource}");
        env.AppendLine($"LOCAL_REPAIR_PORT={ports.MicRepair}");
        env.AppendLine($"LOCAL_CONTROL_PORT={ports.MicControl}");
        env.AppendLine($"LOCAL_SOURCE_NAME={Keep("LOCAL_SOURCE_NAME", DefaultSourceName)}");
        env.AppendLine($"SESS_LATENCY_MSEC={Keep("SESS_LATENCY_MSEC", "40")}");
        return env.ToString();
    }

    /// <summary>
    /// The Windows client's link.env. The tray app carries the audio itself
    /// (Audio/VbanLink.cs) with these VBAN port and stream names.
    /// </summary>
    public static string BuildVbanEnv(LinkSettings settings, VbanStreams streams)
    {
        var env = new StringBuilder();
        env.AppendLine("# Written by Bragi Client's \"Connect to Bragi...\". Running it again rewrites this file.");
        env.AppendLine($"BRAGI_HOST_IP={settings.HostIp}");
        env.AppendLine($"PEER_NAME={settings.PeerName}");
        env.AppendLine($"BRAGI_WS_URL={PresenceUrl(settings.Server)}");
        if (!string.IsNullOrWhiteSpace(settings.ApiKey))
        {
            env.AppendLine($"BRAGI_API_KEY={settings.ApiKey.Trim()}");
        }
        env.AppendLine();
        env.AppendLine("# VBAN: the Bragi host sends its headset mic as VBAN_STREAM_SEND and");
        env.AppendLine("# listens for this computer's audio as VBAN_STREAM_RECEIVE, both on VBAN_PORT.");
        env.AppendLine($"VBAN_PORT={streams.Port}");
        env.AppendLine($"VBAN_STREAM_SEND={streams.Send}");
        env.AppendLine($"VBAN_STREAM_RECEIVE={streams.Receive}");
        return env.ToString();
    }

    [GeneratedRegex("[^a-z0-9]+")]
    private static partial Regex NotNameChars();

    [GeneratedRegex("^[a-z][a-z0-9-]{0,39}$")]
    private static partial Regex PeerName();
}
