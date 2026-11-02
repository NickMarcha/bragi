using Avalonia.Controls;
using Avalonia.Headless.XUnit;
using Avalonia.Interactivity;
using Avalonia.LogicalTree;
using Avalonia.Threading;
using Bragi.Client.Config;
using Bragi.Client.Setup;
using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

public class SetupWindowTests
{
    private static readonly SetupDefaults Defaults =
        new("https://bragi.example.ts.net/", "laptop", "100.64.0.30", "", ["99-roc.conf already loads Roc modules"]);

    [Fact]
    public void The_link_unit_and_its_script_ship_inside_the_app()
    {
        var names = typeof(LinkInstaller).Assembly.GetManifestResourceNames();
        Assert.Contains("bragi-roc-link-run.sh", names);
        Assert.Contains("bragi-roc-link.service", names);
    }

    [AvaloniaFact]
    public async Task Connect_sends_what_was_typed_and_closes_on_success()
    {
        (string, string, string, string?)? sent = null;
        var window = new SetupWindow(Defaults, (server, name, ip, key) => { sent = (server, name, ip, key); return Task.CompletedTask; });
        var connected = false;
        window.Connected += () => connected = true;
        var closed = false;
        window.Closed += (_, _) => closed = true;
        window.Show();

        Box(window, 3).Text = "bragi_key";
        FindButton(window, "Connect").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        await Settle();

        Assert.Equal(("https://bragi.example.ts.net/", "laptop", "100.64.0.30", "bragi_key"), sent);
        Assert.True(connected);
        Assert.True(closed);
    }

    [AvaloniaFact]
    public async Task A_failed_connect_stays_open_and_says_why()
    {
        var window = new SetupWindow(Defaults, (_, _, _, _) => throw new LinkSetupException("Could not find bragi.example.ts.net."));
        var connected = false;
        window.Connected += () => connected = true;
        window.Show();

        FindButton(window, "Connect").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        await Settle();

        Assert.False(connected);
        Assert.True(window.IsVisible);
        Assert.True(FindButton(window, "Connect").IsEnabled);
        Assert.Contains(window.GetLogicalDescendants().OfType<TextBlock>(), t => t.Text == "Could not find bragi.example.ts.net.");
    }

    [AvaloniaFact]
    public void Clashing_drop_ins_are_shown_up_front()
    {
        var window = new SetupWindow(Defaults, (_, _, _, _) => Task.CompletedTask);
        window.Show();

        Assert.Contains(window.GetLogicalDescendants().OfType<TextBlock>(), t => t.Text == "99-roc.conf already loads Roc modules");
    }

    [AvaloniaFact]
    public void A_machine_already_set_up_is_offered_a_change_not_a_first_connect()
    {
        var window = new SetupWindow(Defaults with { AlreadySetUp = true }, (_, _, _, _) => Task.CompletedTask);
        window.Show();

        Assert.Equal("Change Bragi connection", window.Title);
        FindButton(window, "Save and reconnect");
        Assert.Equal("Change Bragi connection...", TrayIconManager.SetupMenuLabel(alreadySetUp: true));
        Assert.Equal("Connect to Bragi...", TrayIconManager.SetupMenuLabel(alreadySetUp: false));
    }

    private static TextBox Box(Window window, int index) => window.GetLogicalDescendants().OfType<TextBox>().ElementAt(index);

    private static Button FindButton(Window window, string label) =>
        window.GetLogicalDescendants().OfType<Button>().Single(b => (string?)b.Content == label);

    private static async Task Settle()
    {
        for (var i = 0; i < 5; i++)
        {
            await Task.Yield();
            Dispatcher.UIThread.RunJobs();
        }
    }
}
