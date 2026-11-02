using System.Net;
using System.Text.Json;
using Bragi.Client.Config;
using Bragi.Client.Tray;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Xunit;

namespace Bragi.Client.Tests;

/// <summary>"Connect to Bragi...": everything except writing files and calling systemctl.</summary>
public class LinkSetupTests
{
    private static readonly RocPorts Ports = new(10041, 10042, 10043, 10044, 10045, 10046);

    [Theory]
    [InlineData("bragi.example.ts.net", "https://bragi.example.ts.net/")]
    [InlineData(" https://bragi.example.ts.net ", "https://bragi.example.ts.net/")]
    [InlineData("http://127.0.0.1:8000", "http://127.0.0.1:8000/")]
    [InlineData("https://host/bragi", "https://host/bragi/")]
    public void Server_addresses_are_read_the_way_people_type_them(string typed, string expected) =>
        Assert.Equal(expected, LinkSetup.ServerUri(typed)!.ToString());

    [Theory]
    [InlineData("")]
    [InlineData("ftp://host/")]
    [InlineData("https://host/?x=1")]
    [InlineData("https://user:pw@host/")]
    public void Unusable_server_addresses_are_refused(string typed) => Assert.Null(LinkSetup.ServerUri(typed));

    [Fact]
    public void The_presence_url_follows_the_server_scheme_and_path()
    {
        Assert.Equal("wss://host/ws/peer", LinkSetup.PresenceUrl(new Uri("https://host/")).ToString());
        Assert.Equal("ws://host:8000/bragi/ws/peer", LinkSetup.PresenceUrl(new Uri("http://host:8000/bragi/")).ToString());
    }

    [Fact]
    public void Tailscales_own_interface_wins_over_other_cgnat_addresses()
    {
        var addresses = new[]
        {
            ("wlan0", IPAddress.Parse("192.168.1.8")),
            ("wwan0", IPAddress.Parse("100.70.1.2")),
            ("tailscale0", IPAddress.Parse("100.64.0.30")),
        };
        Assert.Equal("100.64.0.30", LinkSetup.TailnetIpv4(addresses));
        Assert.Equal("100.70.1.2", LinkSetup.TailnetIpv4(addresses.Take(2)));
        Assert.Null(LinkSetup.TailnetIpv4(addresses.Take(1)));
    }

    [Theory]
    [InlineData("Studio-PC", "studio-pc")]
    [InlineData("my_laptop.local", "my-laptop-local")]
    [InlineData("2024box", "desktop-2024box")]
    public void The_hostname_becomes_a_valid_peer_name(string host, string expected)
    {
        Assert.Equal(expected, LinkSetup.SuggestedPeerName(host));
        Assert.True(LinkSetup.ValidPeerName(expected));
    }

    [Fact]
    public void The_roc_module_directory_needs_both_modules()
    {
        var files = new HashSet<string>
        {
            Path.Combine("/a", "libpipewire-module-roc-sink.so"),
            Path.Combine("/b", "libpipewire-module-roc-sink.so"),
            Path.Combine("/b", "libpipewire-module-roc-source.so"),
        };
        Assert.Equal("/b", LinkSetup.FindRocModuleDir(["/a", "/b"], files.Contains));
        Assert.Null(LinkSetup.FindRocModuleDir(["/a"], files.Contains));
    }

    [Fact]
    public void The_env_file_maps_the_servers_ports_onto_the_link()
    {
        var settings = new LinkSettings(new Uri("https://bragi.example.ts.net/"), "laptop", "100.64.0.10", "bragi_key", "/usr/lib/pipewire-0.3");

        var env = Parse(LinkSetup.BuildEnv(settings, Ports, null));

        Assert.Equal("100.64.0.10", env["BRAGI_HOST_IP"]);
        Assert.Equal("wss://bragi.example.ts.net/ws/peer", env["BRAGI_WS_URL"]);
        Assert.Equal("laptop", env["PEER_NAME"]);
        Assert.Equal("bragi_key", env["BRAGI_API_KEY"]);
        // This machine sends to the host's playback ports and listens on its mic ports.
        Assert.Equal(("10044", "10045", "10046"), (env["REMOTE_SOURCE_PORT"], env["REMOTE_REPAIR_PORT"], env["REMOTE_CONTROL_PORT"]));
        Assert.Equal(("10041", "10042", "10043"), (env["LOCAL_SOURCE_PORT"], env["LOCAL_REPAIR_PORT"], env["LOCAL_CONTROL_PORT"]));
        Assert.Equal((LinkSetup.DefaultSinkName, LinkSetup.DefaultSourceName, "40"),
            (env["LOCAL_SINK_NAME"], env["LOCAL_SOURCE_NAME"], env["SESS_LATENCY_MSEC"]));
    }

    [Fact]
    public void Reconnecting_keeps_node_names_so_wireplumber_keeps_its_defaults()
    {
        var existing = new Dictionary<string, string>
        {
            ["LOCAL_SINK_NAME"] = "old-sink", ["LOCAL_SOURCE_NAME"] = "old-mic", ["SESS_LATENCY_MSEC"] = "60",
        };
        var settings = new LinkSettings(new Uri("https://host/"), "laptop", "100.64.0.10", null, "/usr/lib/pipewire-0.3");

        var env = Parse(LinkSetup.BuildEnv(settings, Ports, existing));

        Assert.Equal(("old-sink", "old-mic", "60"), (env["LOCAL_SINK_NAME"], env["LOCAL_SOURCE_NAME"], env["SESS_LATENCY_MSEC"]));
        Assert.False(env.ContainsKey("BRAGI_API_KEY"));
    }

    [Fact]
    public void The_env_file_is_one_the_client_can_load_again()
    {
        var settings = new LinkSettings(new Uri("https://host/"), "laptop", "100.64.0.10", null, "/usr/lib/pipewire-0.3");
        var env = Parse(LinkSetup.BuildEnv(settings, Ports, null));
        Assert.Equal("https://host/", BragiServerAddress.FromPresenceUrl(env["BRAGI_WS_URL"])!.WebUi.ToString());
    }

    [Fact]
    public async Task Connecting_registers_with_the_server_and_installs_the_env_file()
    {
        await using var server = await FakeServer.StartAsync(async context =>
        {
            var request = await JsonDocument.ParseAsync(context.Request.Body);
            Assert.Equal("laptop", request.RootElement.GetProperty("name").GetString());
            Assert.Equal("100.64.0.30", request.RootElement.GetProperty("tailscale_ip").GetString());
            await context.Response.WriteAsJsonAsync(new
            {
                name = "laptop",
                ports = new { mic_source = 10041, mic_repair = 10042, mic_control = 10043, playback_source = 10044, playback_repair = 10045, playback_control = 10046 },
            });
        });
        string? installed = null;

        var settings = await Connect(server, "laptop", env => { installed = env; return Task.CompletedTask; });

        Assert.Equal("100.64.0.10", settings.HostIp);
        Assert.Equal("10044", Parse(installed!)["REMOTE_SOURCE_PORT"]);
    }

    [Fact]
    public async Task The_servers_reason_for_refusing_is_shown()
    {
        await using var server = await FakeServer.StartAsync(async context =>
        {
            context.Response.StatusCode = 409;
            await context.Response.WriteAsJsonAsync(new { detail = "'pixel' already belongs to another client" });
        });

        var error = await Assert.ThrowsAsync<LinkSetupException>(() => Connect(server, "pixel", _ => Task.CompletedTask));

        Assert.Equal("'pixel' already belongs to another client", error.Message);
    }

    [Fact]
    public async Task An_older_server_without_the_endpoint_says_so()
    {
        await using var server = await FakeServer.StartAsync(context => { context.Response.StatusCode = 404; return Task.CompletedTask; });

        var error = await Assert.ThrowsAsync<LinkSetupException>(() => Connect(server, "laptop", _ => Task.CompletedTask));

        Assert.Contains("too old", error.Message);
    }

    [Theory]
    [InlineData("", "laptop", "100.64.0.30", "/usr/lib", "address")]
    [InlineData("https://host/", "Laptop!", "100.64.0.30", "/usr/lib", "name")]
    [InlineData("https://host/", "laptop", "192.168.1.8", "/usr/lib", "Tailscale")]
    [InlineData("https://host/", "laptop", "100.64.0.30", null, "Roc modules")]
    public async Task Problems_the_user_can_fix_are_explained_before_anything_is_sent(
        string server, string name, string ip, string? moduleDir, string mentions)
    {
        using var http = new HttpClient();
        var error = await Assert.ThrowsAsync<LinkSetupException>(() => LinkSetup.ConnectAsync(
            server, name, ip, null, http, _ => throw new InvalidOperationException("resolved"), moduleDir, null,
            _ => throw new InvalidOperationException("installed")));
        Assert.Contains(mentions, error.Message);
    }

    [Fact]
    public void Static_roc_drop_ins_are_found_so_the_window_can_warn()
    {
        var dir = Directory.CreateTempSubdirectory().FullName;
        File.WriteAllText(Path.Combine(dir, "99-roc.conf"), "context.modules = [ { name = libpipewire-module-roc-sink } ]");
        File.WriteAllText(Path.Combine(dir, "10-clock.conf"), "context.properties = { }");
        File.WriteAllText(Path.Combine(dir, "98-roc.conf.disabled"), "libpipewire-module-roc-source");

        Assert.Equal([Path.Combine(dir, "99-roc.conf")], LinkInstaller.ConflictingDropIns(dir));
        Assert.Empty(LinkInstaller.ConflictingDropIns(Path.Combine(dir, "missing")));
    }

    private static Task<LinkSettings> Connect(FakeServer server, string name, Func<string, Task> install)
    {
        var http = new HttpClient();
        return LinkSetup.ConnectAsync(server.Url, name, "100.64.0.30", null, http,
            _ => Task.FromResult(new[] { IPAddress.Parse("::1"), IPAddress.Parse("100.64.0.10") }),
            "/usr/lib/pipewire-0.3", null, install);
    }

    private static Dictionary<string, string> Parse(string env) => env
        .Split('\n')
        .Where(line => line.Contains('=') && !line.StartsWith('#'))
        .ToDictionary(line => line[..line.IndexOf('=')], line => line[(line.IndexOf('=') + 1)..].TrimEnd('\r'));

    private sealed class FakeServer : IAsyncDisposable
    {
        private readonly WebApplication _app;
        public string Url { get; }

        private FakeServer(WebApplication app, string url) { _app = app; Url = url; }

        public static async Task<FakeServer> StartAsync(RequestDelegate register)
        {
            var builder = WebApplication.CreateBuilder();
            builder.Logging.ClearProviders();
            builder.WebHost.UseUrls("http://127.0.0.1:0");
            var app = builder.Build();
            app.MapPost("/api/peers/register-desktop", register);
            await app.StartAsync();
            var address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
            return new FakeServer(app, address + "/");
        }

        public async ValueTask DisposeAsync() => await _app.DisposeAsync();
    }
}
