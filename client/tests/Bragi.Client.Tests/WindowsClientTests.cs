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

/// <summary>The Windows client: a VBAN peer, with Voicemeeter carrying the audio.</summary>
public class WindowsClientTests
{
    [Fact]
    public async Task Connecting_registers_a_vban_peer_and_writes_its_streams()
    {
        await using var app = await StartServerAsync(async context =>
        {
            var request = await JsonDocument.ParseAsync(context.Request.Body);
            Assert.Equal("vban", request.RootElement.GetProperty("protocol").GetString());
            Assert.Equal("sage", request.RootElement.GetProperty("name").GetString());
            await context.Response.WriteAsJsonAsync(new
            {
                name = "sage",
                vban = new { port = 6980, stream_send = "SagepiMic", stream_receive = "SageAudio" },
            });
        });
        string? installed = null;

        var settings = await LinkSetup.ConnectVbanAsync(Url(app), "sage", "100.64.0.30", "bragi_key", new HttpClient(),
            _ => Task.FromResult(new[] { IPAddress.Parse("100.64.0.10") }),
            env => { installed = env; return Task.CompletedTask; });

        Assert.Equal("100.64.0.10", settings.HostIp);
        var config = RocLinkConfig.FromValues(Parse(installed!), requireRocNodes: false)!;
        Assert.Equal((6980, "SagepiMic", "SageAudio"), (config.VbanPort, config.VbanStreamSend, config.VbanStreamReceive));
        Assert.Equal(("sage", "bragi_key"), (config.PeerName, config.BragiApiKey));
        Assert.Equal(Url(app), BragiServerAddress.FromPresenceUrl(config.BragiWsBaseUrl)!.WebUi.ToString());
    }

    [Fact]
    public async Task A_server_without_vban_registration_is_called_too_old()
    {
        // An older server ignores "protocol" and answers with Roc ports.
        await using var app = await StartServerAsync(context => context.Response.WriteAsJsonAsync(new
        {
            name = "sage",
            ports = new { mic_source = 1, mic_repair = 2, mic_control = 3, playback_source = 4, playback_repair = 5, playback_control = 6 },
        }));

        var error = await Assert.ThrowsAsync<LinkSetupException>(() => LinkSetup.ConnectVbanAsync(Url(app), "sage", "100.64.0.30",
            null, new HttpClient(), _ => Task.FromResult(new[] { IPAddress.Parse("100.64.0.10") }), _ => Task.CompletedTask));

        Assert.Contains("too old", error.Message);
    }

    [Fact]
    public void A_windows_config_needs_no_roc_node_names_but_a_linux_one_does()
    {
        var values = new Dictionary<string, string> { ["BRAGI_HOST_IP"] = "100.64.0.10", ["PEER_NAME"] = "sage" };

        Assert.NotNull(RocLinkConfig.FromValues(values, requireRocNodes: false));
        Assert.Null(RocLinkConfig.FromValues(values, requireRocNodes: true));
    }

    [Fact]
    public void The_menu_shows_what_to_set_voicemeeter_to()
    {
        var config = RocLinkConfig.FromValues(new Dictionary<string, string>
        {
            ["BRAGI_HOST_IP"] = "100.64.0.10", ["VBAN_PORT"] = "6980",
            ["VBAN_STREAM_SEND"] = "SagepiMic", ["VBAN_STREAM_RECEIVE"] = "SageAudio",
        }, requireRocNodes: false);

        Assert.Equal(["VBAN in: SagepiMic from 100.64.0.10:6980", "VBAN out: SageAudio to 100.64.0.10:6980"],
            TrayIconManager.VbanMenuLines(config));
        Assert.Empty(TrayIconManager.VbanMenuLines(null));
    }

    [Fact]
    public void Start_at_login_points_at_velopacks_stub_not_the_version_folder()
    {
        var installed = Path.Combine("C:", "Users", "u", "AppData", "Local", "BragiClient");
        var running = Path.Combine(installed, "current", "Bragi.Client.exe");
        var stub = Path.Combine(installed, "Bragi.Client.exe");

        Assert.Equal(stub, AutostartService.WindowsLaunchPath(running, path => path == stub));
        Assert.Equal(running, AutostartService.WindowsLaunchPath(running, _ => false));
        var dev = Path.Combine("C:", "src", "bin", "Bragi.Client.exe");
        Assert.Equal(dev, AutostartService.WindowsLaunchPath(dev, _ => true));
    }

    private static string Url(WebApplication app) =>
        app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single() + "/";

    private static async Task<WebApplication> StartServerAsync(RequestDelegate register)
    {
        var builder = WebApplication.CreateBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        var app = builder.Build();
        app.MapPost("/api/peers/register-desktop", register);
        await app.StartAsync();
        return app;
    }

    private static Dictionary<string, string> Parse(string env) => env
        .Split('\n')
        .Where(line => line.Contains('=') && !line.StartsWith('#'))
        .ToDictionary(line => line[..line.IndexOf('=')], line => line[(line.IndexOf('=') + 1)..].TrimEnd('\r'));
}
