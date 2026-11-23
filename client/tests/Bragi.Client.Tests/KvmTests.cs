using System.Text.Json;
using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

/// <summary>The tray's "Keyboard &amp; mouse" menu (app/kvm.py on the server).</summary>
public class KvmTests
{
    private static JsonElement Json(string text) => JsonDocument.Parse(text).RootElement;

    [Fact]
    public void The_menu_lists_the_computers_with_the_active_one_ticked()
    {
        var kvm = new KvmService();
        var changes = 0;
        kvm.Changed += () => changes++;

        kvm.OnMessage(Json("""{"type":"kvm","available":true,"active":"sage-dev","computers":["sage","sage-dev","sagedeck"]}"""));

        Assert.Equal([("sage", false), ("sage-dev", true), ("sagedeck", false)], KvmService.MenuRows(kvm.State));
        Assert.Equal(1, changes);
    }

    [Fact]
    public void No_kvm_on_the_host_or_no_connection_hides_the_menu()
    {
        var kvm = new KvmService();
        kvm.OnMessage(Json("""{"type":"kvm","available":false,"active":null,"computers":[]}"""));
        Assert.Empty(KvmService.MenuRows(kvm.State));

        kvm.OnMessage(Json("""{"type":"kvm","available":true,"active":"sage","computers":["sage"]}"""));
        kvm.OnDisconnected();
        Assert.Empty(KvmService.MenuRows(kvm.State));
    }

    [Fact]
    public async Task Choosing_a_computer_sends_a_switch_and_a_refusal_is_kept_until_the_next_state()
    {
        var sent = new List<string>();
        var kvm = new KvmService { Send = m => { sent.Add(JsonSerializer.Serialize(m)); return Task.FromResult(true); } };

        Assert.True(await kvm.SwitchAsync("sagedeck"));
        kvm.OnMessage(Json("""{"type":"kvm_error","error":"sagedeck isn't connected."}"""));
        Assert.Equal("sagedeck isn't connected.", kvm.Error);
        kvm.OnMessage(Json("""{"type":"kvm","available":true,"active":"sage","computers":["sage"]}"""));

        Assert.Equal(["""{"type":"kvm_switch","computer":"sagedeck"}"""], sent);
        Assert.Null(kvm.Error);
    }

    [Fact]
    public void Identify_shows_the_kvm_name_or_says_this_computer_isnt_in_the_setup()
    {
        Assert.Equal("sagedeck", IdentifyOverlay.Text(Json("""{"type":"kvm_identify","computer":"sagedeck","peer":"deck"}"""))?.Title);
        var stranger = IdentifyOverlay.Text(Json("""{"type":"kvm_identify","computer":null,"peer":"laptop"}"""));
        Assert.Equal("laptop", stranger?.Title);
        Assert.Contains("Not in the KVM setup", stranger?.Note);
        Assert.Null(IdentifyOverlay.Text(Json("""{"type":"kvm","available":true}""")));
    }

    private sealed class FakeKeyboard : IKeyboard
    {
        public List<IReadOnlyList<ushort>> Pressed { get; } = [];
        public void Press(IReadOnlyList<ushort> codes) => Pressed.Add(codes);
        public void Dispose() { }
    }

    [Fact]
    public void The_host_hears_this_computer_can_press_keys_and_its_keys_are_pressed()
    {
        var keyboard = new FakeKeyboard();
        var sent = new List<string>();
        var keys = new KeyPresser(() => keyboard) { Send = m => { sent.Add(JsonSerializer.Serialize(m)); return Task.FromResult(true); }, DeskflowSetUp = () => false };

        keys.OnMessage(Json("""{"type":"kvm","available":true,"active":"sage","computers":["sage"]}"""));
        keys.OnMessage(Json("""{"type":"kvm","available":true,"active":"sage","computers":["sage"]}"""));
        keys.OnMessage(Json("""{"type":"press_keys","codes":[29,42,56,50]}"""));
        keys.OnMessage(Json("""{"type":"press_keys","codes":[29,"x"]}"""));  // ignored

        Assert.Equal(["""{"type":"client_hello","features":["keys"]}"""], sent);
        Assert.Equal([(IReadOnlyList<ushort>)new ushort[] { 29, 42, 56, 50 }], keyboard.Pressed);
    }

    [Fact]
    public void Without_a_keyboard_here_the_host_hears_so()
    {
        var sent = new List<string>();
        var keys = new KeyPresser(null) { Send = m => { sent.Add(JsonSerializer.Serialize(m)); return Task.FromResult(true); }, DeskflowSetUp = () => false };
        keys.OnMessage(Json("""{"type":"kvm","available":true,"active":null,"computers":[]}"""));
        Assert.Equal(["""{"type":"client_hello","features":[]}"""], sent);
    }

    [Fact]
    public void With_deskflow_set_up_here_the_dashboard_may_start_it()
    {
        // bragi#92: the hub offers "start Deskflow" only to tray apps that say so.
        var sent = new List<string>();
        var keys = new KeyPresser(null) { Send = m => { sent.Add(JsonSerializer.Serialize(m)); return Task.FromResult(true); }, DeskflowSetUp = () => true };
        keys.OnMessage(Json("""{"type":"kvm","available":true,"active":null,"computers":[]}"""));
        Assert.Equal(["""{"type":"client_hello","features":["deskflow"]}"""], sent);
    }

    [Fact]
    public void The_menu_shows_the_apps_version_in_three_parts()
    {
        Assert.Equal("0.1.18", TrayIconManager.FormatVersion(new Version(0, 1, 18, 0)));
        Assert.Equal("?", TrayIconManager.FormatVersion(null));
        Assert.Matches(@"^\d+\.\d+\.\d+$", TrayIconManager.Version);
    }
}
