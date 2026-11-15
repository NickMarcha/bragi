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
}
