using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using Bragi.Client.Gamepad;
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

/// <summary>The Bragi host's controllers played here (app/gamepad.py on the server).</summary>
public class GamepadTests
{
    private sealed class FakePad : IVirtualGamepad
    {
        public List<GamepadState> Applied { get; } = [];
        public bool Disposed { get; private set; }
        public void Apply(GamepadState state) => Applied.Add(state);
        public void Dispose() => Disposed = true;
    }

    private static JsonElement Json(string text) => JsonDocument.Parse(text).RootElement;

    [Fact]
    public void A_state_message_is_read_and_clamped()
    {
        var state = GamepadState.FromMessage(Json(
            """{"type":"gamepad","id":"p","buttons":["a","lb"],"lx":-40000,"ly":100,"rx":0,"ry":0,"lt":300,"rt":10,"dx":-1,"dy":0}"""))!;

        Assert.Equal(new HashSet<string> { "a", "lb" }, state.Buttons);
        Assert.Equal((-32768, 100, 255, 10, -1), (state.Lx, state.Ly, state.Lt, state.Rt, state.Dx));
    }

    [Fact]
    public void Input_makes_one_virtual_pad_per_controller_and_detach_neutralises_and_removes_it()
    {
        var pads = new List<FakePad>();
        var service = new GamepadService("deck", () => { var p = new FakePad(); pads.Add(p); return p; });
        service.OnMessage(Json("""{"type":"gamepads","target":"deck","controllers":[{"id":"p1","name":"8BitDo"}]}"""));

        service.OnMessage(Json("""{"type":"gamepad","id":"p1","buttons":["a"]}"""));
        service.OnMessage(Json("""{"type":"gamepad","id":"p1","buttons":[]}"""));
        service.OnMessage(Json("""{"type":"gamepad_detach","id":"p1"}"""));

        var pad = Assert.Single(pads);
        Assert.Equal(3, pad.Applied.Count);
        Assert.Empty(pad.Applied[^1].Buttons); // released before it goes away
        Assert.True(pad.Disposed);
        Assert.True(service.IsHere);
    }

    [Fact]
    public void Losing_the_target_or_the_connection_removes_the_pads()
    {
        var pad = new FakePad();
        var service = new GamepadService("deck", () => pad);
        service.OnMessage(Json("""{"type":"gamepads","target":"deck","controllers":[{"id":"p1","name":"8BitDo"}]}"""));
        service.OnMessage(Json("""{"type":"gamepad","id":"p1","buttons":["a"]}"""));

        service.OnMessage(Json("""{"type":"gamepads","target":"sage","controllers":[{"id":"p1","name":"8BitDo"}]}"""));
        Assert.True(pad.Disposed);
        Assert.False(service.IsHere);

        service.OnDisconnected();
        Assert.Equal(GamepadStatus.None, service.Status);
    }

    [Fact]
    public void A_pad_that_cant_be_made_is_reported_once_not_retried_per_message()
    {
        var attempts = 0;
        var service = new GamepadService("deck", () => { attempts++; throw new InvalidOperationException("Can't open /dev/uinput"); });
        service.OnMessage(Json("""{"type":"gamepads","target":"deck","controllers":[{"id":"p1","name":"8BitDo"}]}"""));

        service.OnMessage(Json("""{"type":"gamepad","id":"p1","buttons":["a"]}"""));
        service.OnMessage(Json("""{"type":"gamepad","id":"p1","buttons":[]}"""));

        Assert.Equal(1, attempts);
        Assert.Equal("Can't open /dev/uinput", service.Problem);
    }

    [Fact]
    public void The_menu_item_names_the_controller_and_where_it_is()
    {
        var one = new GamepadStatus(["8BitDo"], "sage", true);
        Assert.Equal((false, "", false, false), TrayIconManager.GamepadMenu(new GamepadStatus([], null, true), false, null));
        Assert.Equal((true, "Use 8BitDo here (now on sage)", false, true), TrayIconManager.GamepadMenu(one, false, null));
        Assert.Equal((true, "Use 8BitDo here", true, true), TrayIconManager.GamepadMenu(one with { Target = "deck" }, true, null));
        Assert.Equal((true, "8BitDo: not on Windows yet", false, false), TrayIconManager.GamepadMenu(one with { HereSupported = false }, false, null));
        Assert.Equal((true, "2 controllers: no uinput", true, true),
            TrayIconManager.GamepadMenu(new GamepadStatus(["a", "b"], "deck", true), true, "no uinput"));
    }

    [Fact]
    public async Task The_peer_socket_delivers_messages_and_sends_a_claim()
    {
        var received = new TaskCompletionSource<string>();
        var builder = WebApplication.CreateBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        await using var app = builder.Build();
        app.UseWebSockets();
        app.Map("/ws/peer/deck", async (HttpContext context) =>
        {
            using var socket = await context.WebSockets.AcceptWebSocketAsync();
            // Larger than the client's receive buffer, so it has to join the frames.
            var status = JsonSerializer.Serialize(new { type = "gamepads", target = (string?)null,
                controllers = new[] { new { id = "p1", name = new string('x', 5000) } } });
            await socket.SendAsync(Encoding.UTF8.GetBytes(status), WebSocketMessageType.Text, true, default);
            var buffer = new byte[256];
            var result = await socket.ReceiveAsync(buffer, default);
            received.TrySetResult(Encoding.UTF8.GetString(buffer, 0, result.Count));
            await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "", default);
        });
        await app.StartAsync();
        var address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();

        using var client = new PeerPresenceClient("deck", address.Replace("http://", "ws://") + "/ws/peer");
        var service = new GamepadService("deck", () => new FakePad());
        var status = new TaskCompletionSource<GamepadStatus>();
        service.StatusChanged += s => { if (s.Controllers.Count > 0) status.TrySetResult(s); };
        client.MessageReceived += service.OnMessage;
        service.Send = client.SendAsync;
        client.SetEnabled(true);

        var seen = await status.Task.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(5000, Assert.Single(seen.Controllers).Length);
        Assert.True(await service.ClaimAsync());
        Assert.Equal("""{"type":"gamepad_claim"}""", await received.Task.WaitAsync(TimeSpan.FromSeconds(10)));
    }
}
