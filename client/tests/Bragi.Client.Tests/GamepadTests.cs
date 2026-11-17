using System.Buffers.Binary;
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
        public event Action<Rumble>? Rumble;
        public void Play(Rumble rumble) => Rumble?.Invoke(rumble);
    }

    private static byte[] Effect(ushort type, short id, ushort length, ushort strong, ushort weak)
    {
        var effect = new byte[48];
        BinaryPrimitives.WriteUInt16LittleEndian(effect, type);
        BinaryPrimitives.WriteInt16LittleEndian(effect.AsSpan(2), id);
        BinaryPrimitives.WriteUInt16LittleEndian(effect.AsSpan(10), length);
        BinaryPrimitives.WriteUInt16LittleEndian(effect.AsSpan(16), strong);
        BinaryPrimitives.WriteUInt16LittleEndian(effect.AsSpan(18), weak);
        return effect;
    }

    [Fact]
    public void Rumble_effects_play_as_uploaded_scaled_by_the_gain()
    {
        var effects = new RumbleEffects();
        Assert.False(effects.Upload(Effect(0x51, 0, 100, 1, 1)));  // FF_PERIODIC: not a rumble
        Assert.True(effects.Upload(Effect(0x50, 3, 250, 0x8000, 0x4000)));

        Assert.Equal(new Rumble(0x8000, 0x4000, 500), effects.Play(3, 2));  // two repeats
        Assert.True(effects.Playing);
        Assert.Null(effects.Play(7, 0));                                    // stopping another slot
        Assert.Equal(Rumble.Stop, effects.Play(3, 0));

        Assert.Null(effects.Play(0x60, 0x7FFF));                            // FF_GAIN: half
        Assert.Equal(new Rumble(0x3FFF, 0x1FFF, 250), effects.Play(3, 1));
        Assert.True(effects.Erase(3));                                      // erasing the one playing stops it
        Assert.Null(effects.Play(3, 1));
    }

    [Fact]
    public void A_rumble_on_the_virtual_pad_goes_to_the_host()
    {
        var pad = new FakePad();
        var sent = new List<string>();
        var service = new GamepadService("deck", () => pad)
        {
            Send = m => { sent.Add(JsonSerializer.Serialize(m)); return Task.FromResult(true); },
        };
        service.OnMessage(Json("""{"type":"gamepads","target":"deck","controllers":[{"id":"p","name":"8BitDo"}]}"""));
        service.OnMessage(Json("""{"type":"gamepad","id":"p","buttons":[],"lx":0,"ly":0,"rx":0,"ry":0,"lt":0,"rt":0,"dx":0,"dy":0}"""));
        sent.Clear();

        pad.Play(new Rumble(1000, 2000, 300));

        Assert.Equal(["""{"type":"gamepad_rumble","id":"p","strong":1000,"weak":2000,"ms":300}"""], sent);
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
        Assert.Equal((true, "8BitDo: not supported here", false, false), TrayIconManager.GamepadMenu(one with { HereSupported = false }, false, null));
        Assert.Equal((true, "2 controllers: no uinput", true, true),
            TrayIconManager.GamepadMenu(new GamepadStatus(["a", "b"], "deck", true), true, "no uinput"));
    }

    [Fact]
    public void Windows_pad_reports_buttons_dpad_and_flips_stick_y()
    {
        var state = new GamepadState(new HashSet<string> { "a", "lb", "guide", "rs", "nope" },
            Lx: -32768, Ly: -32768, Rx: 1234, Ry: 32767, Lt: 255, Rt: 7, Dx: 1, Dy: -1);
        var report = XusbReport.From(state);
        Assert.Equal(XusbReport.A | XusbReport.LeftShoulder | XusbReport.Guide | XusbReport.RightThumb
            | XusbReport.Up | XusbReport.Right, report.Buttons);
        Assert.Equal((short)-32768, report.Lx);
        Assert.Equal((short)32767, report.Ly);   // pushed up: xpad -32768, XInput +32767
        Assert.Equal((short)1234, report.Rx);
        Assert.Equal((short)-32767, report.Ry);
        Assert.Equal((byte)255, report.Lt);
        Assert.Equal((byte)7, report.Rt);
        Assert.Equal(new XusbReport(0, 0, 0, 0, 0, 0, 0), XusbReport.From(GamepadState.Neutral));
    }

    [Fact]
    public void Windows_pad_rumble_scales_motors_and_lasts_until_changed()
    {
        Assert.Equal(new Rumble(65535, 257, 0), XusbReport.RumbleFrom(255, 1));
        Assert.Equal(Rumble.Stop, XusbReport.RumbleFrom(0, 0));
    }

    private sealed class FakeHid : IHidDevice
    {
        public HidDescription? Description { get; init; }
        public List<byte[]> Inputs { get; } = [];
        public List<(uint, ushort, byte[]?)> Replies { get; } = [];
        public bool Disposed { get; private set; }
        public event Action<byte[], byte>? Output;
        public event Action<uint, byte, byte>? GetReport;
        public event Action<uint, byte, byte[]>? SetReport;
        public void Input(byte[] report) => Inputs.Add(report);
        public void ReplyGetReport(uint request, ushort error, byte[] data) => Replies.Add((request, error, data));
        public void ReplySetReport(uint request, ushort error) => Replies.Add((request, error, null));
        public void RaiseOutput(byte[] data) => Output?.Invoke(data, 1);
        public void RaiseGet(uint request) => GetReport?.Invoke(request, 5, 0);
        public void RaiseSet(uint request) => SetReport?.Invoke(request, 0, [5, 1]);
        public void Dispose() => Disposed = true;
    }

    [Fact]
    public async Task A_controller_is_recreated_from_its_description_and_its_requests_go_back_to_the_host()
    {
        FakeHid? made = null;
        var sent = new List<string>();
        var service = new GamepadService("deck", () => new FakePad(), d => made = new FakeHid { Description = d })
        {
            Send = m => { sent.Add(JsonSerializer.Serialize(m)); return Task.FromResult(true); },
        };
        service.OnMessage(Json("""{"type":"gamepads","target":"deck","controllers":[{"id":"p1","name":"8BitDo"}]}"""));
        service.OnMessage(Json("""{"type":"hid_create","id":"p1","name":"8BitDo Ultimate 3mode Xbox","uniq":"e4:17","bus":5,"vendor":11720,"product":36891,"version":1,"country":0,"descriptor":"BQEJBQ=="}"""));
        service.OnMessage(Json("""{"type":"hid_input","id":"p1","data":"AwgB"}"""));
        made!.RaiseOutput([3, 0xff]);
        made.RaiseGet(7);
        made.RaiseSet(8);
        service.OnMessage(Json("""{"type":"hid_report_reply","id":"p1","kind":"hid_get_report","req":7,"err":0,"data":"BQE="}"""));
        service.OnMessage(Json("""{"type":"hid_report_reply","id":"p1","kind":"hid_set_report","req":8,"err":5,"data":""}"""));
        service.OnMessage(Json("""{"type":"gamepad_detach","id":"p1"}"""));

        // Records compare arrays by reference, so the descriptor is checked on its own.
        Assert.Equal(new HidDescription("8BitDo Ultimate 3mode Xbox", "e4:17", 5, 0x2DC8, 0x901B, 1, 0, made.Description!.Descriptor),
            made.Description);
        Assert.Equal([5, 1, 9, 5], made.Description.Descriptor);
        Assert.Equal([3, 8, 1], Assert.Single(made.Inputs));
        Assert.Equal([
            """{"type":"gamepad_hello","modes":["hid","xbox"]}""",
            """{"type":"hid_output","id":"p1","rtype":1,"data":"A/8="}""",
            """{"type":"hid_get_report","id":"p1","req":7,"rnum":5,"rtype":0}""",
            """{"type":"hid_set_report","id":"p1","req":8,"rtype":0,"data":"BQE="}""",
        ], sent);
        Assert.Equal((7u, (ushort)0), (made.Replies[0].Item1, made.Replies[0].Item2));
        Assert.Equal([5, 1], made.Replies[0].Item3);
        Assert.Equal((8u, (ushort)5, (byte[]?)null), made.Replies[1]);
        Assert.True(made.Disposed);
    }

    [Fact]
    public void Uhid_events_have_the_kernels_layout()
    {
        var create = UhidDevice.Create2(new HidDescription("Pad", "ab", 5, 0x2DC8, 0x901B, 1, 0, [5, 1]));
        Assert.Equal(UhidDevice.EventSize, create.Length);
        Assert.Equal(11u, BitConverter.ToUInt32(create, 0));                  // UHID_CREATE2
        Assert.Equal("Pad", Encoding.ASCII.GetString(create, 4, 3));
        Assert.Equal("ab", Encoding.ASCII.GetString(create, 196, 2));        // uniq
        Assert.Equal((ushort)2, BitConverter.ToUInt16(create, 260));         // rd_size
        Assert.Equal((ushort)5, BitConverter.ToUInt16(create, 262));         // bus
        Assert.Equal((0x2DC8u, 0x901Bu, 1u), (BitConverter.ToUInt32(create, 264), BitConverter.ToUInt32(create, 268), BitConverter.ToUInt32(create, 272)));
        Assert.Equal([5, 1], create[280..282]);

        Assert.Equal([12, 0, 0, 0, 2, 0, 3, 8], UhidDevice.Input2([3, 8]));
        Assert.Equal([10, 0, 0, 0, 7, 0, 0, 0, 0, 0, 1, 0, 9], UhidDevice.GetReportReply(7, 0, [9]));
        Assert.Equal([14, 0, 0, 0, 8, 0, 0, 0, 5, 0], UhidDevice.SetReportReply(8, 5));

        var output = new byte[UhidDevice.EventSize];
        BitConverter.GetBytes(6u).CopyTo(output, 0);                          // UHID_OUTPUT
        output[4] = 3; output[5] = 0xff;
        BitConverter.GetBytes((ushort)2).CopyTo(output, 4100);
        output[4102] = 1;
        byte[]? got = null; byte gotType = 9;
        UhidDevice.Dispatch(output, (d, t) => { got = d; gotType = t; }, null, null);
        Assert.Equal([3, 0xff], got);
        Assert.Equal(1, gotType);

        var get = new byte[UhidDevice.EventSize];
        BitConverter.GetBytes(9u).CopyTo(get, 0);                             // UHID_GET_REPORT
        BitConverter.GetBytes(42u).CopyTo(get, 4);
        get[8] = 5; get[9] = 0;
        (uint, byte, byte) asked = default;
        UhidDevice.Dispatch(get, null, (r, n, t) => asked = (r, n, t), null);
        Assert.Equal((42u, (byte)5, (byte)0), asked);
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
            var messages = new List<string>();
            for (var i = 0; i < 2; i++)
            {
                var result = await socket.ReceiveAsync(buffer, default);
                messages.Add(Encoding.UTF8.GetString(buffer, 0, result.Count));
            }
            received.TrySetResult(string.Join("|", messages));
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
        // The hello comes first, on the first status after connecting.
        Assert.Equal("""{"type":"gamepad_hello","modes":["xbox"]}|{"type":"gamepad_claim"}""",
            await received.Task.WaitAsync(TimeSpan.FromSeconds(10)));
    }
}
