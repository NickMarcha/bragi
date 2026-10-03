using System.Net.WebSockets;
using System.Text.Json;
using System.Threading.Channels;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Headless.XUnit;
using Avalonia.Interactivity;
using Avalonia.LogicalTree;
using Avalonia.Threading;
using Bragi.Client.Config;
using Bragi.Client.Volume;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Xunit;

[assembly: AvaloniaTestApplication(typeof(Bragi.Client.Tests.TestApp))]

namespace Bragi.Client.Tests;

public class TestApp : Application
{
    public static AppBuilder BuildAvaloniaApp() => AppBuilder.Configure<TestApp>()
        .UseSkia().UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false });

    public override void Initialize() => Styles.Add(new Avalonia.Themes.Fluent.FluentTheme());
}

public class VolumeTests
{
    internal const string Snapshot = """
        {"type":"state","peers":[
          {"name":"laptop","incoming":{"volume":0.65,"muted":false,"connected":true},
           "outgoing":{"volume":0.3,"muted":true,"connected":true}},
          {"name":"desktop","incoming":{"volume":1,"muted":false,"connected":false},
           "outgoing":{"volume":null,"muted":false,"connected":false}}],
         "headsets":[{"key":"usb-headset","label":"USB headset",
           "playback":{"volume":0.8,"muted":false,"connected":true},
           "capture":{"volume":0.9,"muted":false,"connected":true}}]}
        """;

    [Theory]
    [InlineData("wss://bragi.example/ws/peer", "https://bragi.example/", "wss://bragi.example/ws")]
    [InlineData("ws://localhost:8000/bragi/ws/peer/", "http://localhost:8000/bragi/", "ws://localhost:8000/bragi/ws")]
    public void Server_links_preserve_host_port_and_path(string source, string web, string socket)
    {
        var result = BragiServerAddress.FromPresenceUrl(source);
        Assert.Equal(web, result?.WebUi.AbsoluteUri);
        Assert.Equal(socket, result?.Controls.AbsoluteUri);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("not a URL")]
    [InlineData("file:///ws/peer")]
    [InlineData("https://example.com/ws/peer")]
    [InlineData("wss://example.com/other")]
    public void Invalid_server_configuration_has_no_links(string? value) =>
        Assert.Null(BragiServerAddress.FromPresenceUrl(value));

    [Fact]
    public void Peer_directions_are_from_the_computers_perspective()
    {
        var state = new VolumeState();
        state.Apply(JsonSerializer.Deserialize<JsonElement>(Snapshot));
        var laptop = state.Find("peer", "laptop")!;
        Assert.Equal("incoming", laptop.OutputDirection);
        Assert.Equal(0.65, laptop.Output.Volume);
        Assert.Equal("outgoing", laptop.InputDirection);
        Assert.Equal(0.3, laptop.Input.Volume);
        Assert.True(laptop.Input.Muted);
        var headset = state.Find("headset", "usb-headset")!;
        Assert.Equal("playback", headset.OutputDirection);
        Assert.Equal(0.8, headset.Output.Volume);
        Assert.Equal("capture", headset.InputDirection);
        Assert.Equal(0.9, headset.Input.Volume);
    }

    [AvaloniaFact]
    public async Task Volume_window_receives_state_sends_controls_and_disables_on_disconnect()
    {
        await using var server = await FakeBragi.Start();
        var window = new VolumeWindow(server.Uri, "laptop");
        window.Show();
        try
        {
            await Until(() => Cards(window).Any());
            var panel = Card(window, "laptop");
            var sliders = panel.Children.OfType<Slider>().ToArray();
            var mute = panel.Children.OfType<StackPanel>().SelectMany(p => p.Children).OfType<Button>().ToArray();
            await Until(() => sliders[0].IsEnabled);
            Assert.Equal(65, sliders[0].Value);
            Assert.Equal(30, sliders[1].Value);
            Assert.Equal("Unmute", mute[1].Content);
            Assert.False(server.Actions.Reader.TryRead(out _)); // rendering isn't an action

            sliders[0].Value = 55;
            var action = await server.NextAction();
            Assert.Equal("set_volume", action.GetProperty("action").GetString());
            Assert.Equal("laptop", action.GetProperty("key").GetString());
            Assert.Equal("incoming", action.GetProperty("direction").GetString());
            Assert.Equal(0.55, action.GetProperty("value").GetDouble());
            // An update to the other direction must not restore the old output value.
            await server.Send(new { type = "control", target = "peer", key = "laptop", direction = "outgoing",
                volume = 0.35, muted = true, connected = true });
            await Until(() => sliders[1].Value == 35);
            Assert.Equal(55, sliders[0].Value);
            // Echo an older control while the final value is pending.
            await server.Send(new { type = "control", target = "peer", key = "laptop", direction = "incoming",
                volume = 0.65, muted = false, connected = true, ts = action.GetProperty("ts").GetDouble() - 1 });
            await Task.Delay(50);
            Dispatcher.UIThread.RunJobs();
            Assert.Equal(55, sliders[0].Value);
            await server.Send(new { type = "control", target = "peer", key = "laptop", direction = "incoming",
                volume = 0.55, muted = false, connected = true, ts = action.GetProperty("ts").GetDouble() });

            mute[1].RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            var toggle = await server.NextAction();
            Assert.Equal("toggle_mute", toggle.GetProperty("action").GetString());
            Assert.Equal("outgoing", toggle.GetProperty("direction").GetString());
            await server.Send(new { type = "control", target = "peer", key = "laptop", direction = "outgoing",
                volume = 0.4, muted = false, connected = true });
            await Until(() => sliders[1].Value == 40);
            Assert.Equal("Mute", mute[1].Content);
            Assert.False(server.Actions.Reader.TryRead(out _));

            await server.DropConnection();
            await Until(() => !sliders[0].IsEnabled);
            Assert.False(sliders[1].IsEnabled);
            Assert.False(mute[0].IsEnabled);
            await Until(() => sliders[0].IsEnabled);
            Assert.Equal(65, sliders[0].Value); // fresh snapshot on reconnect
        }
        finally { window.Close(); }
    }

    [AvaloniaFact]
    public async Task One_volume_window_shows_this_device_first_and_all_other_devices()
    {
        await using var server = await FakeBragi.Start();
        using var menu = new VolumeMenu(new RocLinkConfig
        {
            SagepiTailscaleIp = "127.0.0.1", LocalSinkName = "sink", LocalSourceName = "source",
            PeerName = "laptop", BragiWsBaseUrl = server.Uri.AbsoluteUri + "/peer",
        });
        Assert.Equal("Volume", menu.Item.Header);
        Assert.Null(menu.Item.Menu); // direct top-level action, no submenu
        Assert.True(menu.Item.IsEnabled);
        Assert.True(menu.WebUiItem.IsEnabled);
        var window = new VolumeWindow(server.Uri, "laptop");
        window.Show();
        try
        {
            await Until(() => Cards(window).Count() == 3);
            Assert.Equal(new[] { "laptop", "desktop", "USB headset" },
                Cards(window).Select(p => p.Children.OfType<TextBlock>().First().Text));
            Assert.Contains(Card(window, "laptop").Children.OfType<TextBlock>(), t => t.Text == "This device");
            var headset = Card(window, "USB headset").Children.OfType<Slider>().First();
            await Until(() => headset.IsEnabled);
            headset.Value = 60;
            var action = await server.NextAction();
            Assert.Equal("headset", action.GetProperty("target").GetString());
            Assert.Equal("usb-headset", action.GetProperty("key").GetString());
            Assert.Equal("playback", action.GetProperty("direction").GetString());
            Assert.Equal(0.6, action.GetProperty("value").GetDouble());
            Assert.Equal(65, Card(window, "laptop").Children.OfType<Slider>().First().Value);
        }
        finally { window.Close(); }
    }

    [AvaloniaFact]
    public void Unconfigured_client_keeps_menu_items_visible_but_disabled()
    {
        using var menu = new VolumeMenu(null);
        Assert.Null(menu.Item.Menu);
        Assert.False(menu.Item.IsEnabled);
        Assert.False(menu.WebUiItem.IsEnabled);
    }

    [AvaloniaFact]
    public async Task Offline_device_has_no_editable_controls()
    {
        await using var server = await FakeBragi.Start();
        var window = new VolumeWindow(server.Uri, "desktop");
        window.Show();
        try
        {
            await Until(() => window.GetLogicalDescendants().OfType<TextBlock>().Any(t => t.Text == "Connected"));
            var panel = Card(window, "desktop");
            Assert.All(panel.Children.OfType<Slider>(), slider => Assert.False(slider.IsEnabled));
            Assert.All(panel.Children.OfType<StackPanel>().SelectMany(p => p.Children).OfType<Button>(), button => Assert.False(button.IsEnabled));
            Assert.False(server.Actions.Reader.TryRead(out _));
        }
        finally { window.Close(); }
    }

    private static IEnumerable<StackPanel> Cards(Window window) =>
        window.GetLogicalDescendants().OfType<WrapPanel>().Single().Children.OfType<Border>()
            .Select(b => (StackPanel)b.Child!);

    private static StackPanel Card(Window window, string label) =>
        Cards(window).Single(p => p.Children.OfType<TextBlock>().First().Text == label);

    private static async Task Until(Func<bool> condition)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(8));
        while (!condition())
        {
            await Task.Delay(20, timeout.Token);
            Dispatcher.UIThread.RunJobs();
        }
    }
}

internal sealed class FakeBragi : IAsyncDisposable
{
    private readonly WebApplication _app;
    private WebSocket? _socket;
    public Uri Uri { get; private set; } = null!;
    public Channel<JsonElement> Actions { get; } = Channel.CreateUnbounded<JsonElement>();
    private FakeBragi(WebApplication app) => _app = app;

    public static async Task<FakeBragi> Start()
    {
        var builder = WebApplication.CreateBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        var app = builder.Build();
        var server = new FakeBragi(app);
        app.UseWebSockets();
        app.Map("/ws", async context =>
        {
            using var socket = await context.WebSockets.AcceptWebSocketAsync();
            server._socket = socket;
            try
            {
                // Split the snapshot to exercise WebSocket fragmentation.
                var bytes = System.Text.Encoding.UTF8.GetBytes(VolumeTests.Snapshot);
                await socket.SendAsync(bytes.AsMemory(0, 30), WebSocketMessageType.Text, false, context.RequestAborted);
                await socket.SendAsync(bytes.AsMemory(30), WebSocketMessageType.Text, true, context.RequestAborted);
                var buffer = new byte[8192];
                while (socket.State == WebSocketState.Open)
                {
                    var result = await socket.ReceiveAsync(buffer.AsMemory(), context.RequestAborted);
                    if (result.MessageType == WebSocketMessageType.Close) break;
                    using var document = JsonDocument.Parse(buffer.AsMemory(0, result.Count));
                    await server.Actions.Writer.WriteAsync(document.RootElement.Clone());
                }
            }
            catch (Exception ex) when (ex is WebSocketException or OperationCanceledException or ObjectDisposedException) { }
        });
        await app.StartAsync();
        var address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        server.Uri = new Uri(address.Replace("http:", "ws:") + "/ws");
        return server;
    }

    public async Task<JsonElement> NextAction()
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(8));
        return await Actions.Reader.ReadAsync(timeout.Token);
    }
    public async Task Send(object value) => await _socket!.SendAsync(JsonSerializer.SerializeToUtf8Bytes(value).AsMemory(), WebSocketMessageType.Text, true, CancellationToken.None);
    public async Task DropConnection() => await _socket!.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "test disconnect", CancellationToken.None);
    public async ValueTask DisposeAsync() { _socket?.Abort(); await _app.DisposeAsync(); }
}
