using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Bragi.Client.Config;

namespace Bragi.Client.Setup;

/// <summary>What the window starts filled in with.</summary>
/// <remarks><c>AlreadySetUp</c> words the window as changing a working link rather than making one.</remarks>
public sealed record SetupDefaults(
    string Server, string PeerName, string PeerIp, string ApiKey, IReadOnlyList<string> Warnings, bool AlreadySetUp = false,
    bool VbanLink = false);

/// <summary>
/// "Connect to Bragi...": the four things only the user knows or can confirm.
/// Everything else (ports, the host's address, the Roc module directory, the
/// systemd unit) is looked up or written by <c>connect</c>.
/// </summary>
public sealed class SetupWindow : Window
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(15) };

    private readonly TextBox _link = new() { PlaceholderText = "bragi://connect?… from the dashboard" };
    private readonly TextBlock _linkNote = new() { TextWrapping = TextWrapping.Wrap, FontSize = 12 };
    private readonly TextBox _server = new() { PlaceholderText = "https://bragi.your-tailnet.ts.net/" };
    private readonly TextBox _name = new();
    private readonly TextBox _ip = new() { PlaceholderText = "100.x.x.x" };
    private readonly TextBox _apiKey = new() { PasswordChar = '•', PlaceholderText = "Only if the dashboard has a password" };
    private readonly Button _connect = new() { IsDefault = true };
    private readonly Button _cancel = new() { Content = "Cancel", IsCancel = true };
    private readonly TextBlock _status = new() { TextWrapping = TextWrapping.Wrap };
    private readonly Func<string, string, string, string?, Task> _connectAction;
    private readonly Func<Uri, string, string, Task<string>> _redeem;
    /// <summary>The pasted link's one-time code, until it's traded for a key.</summary>
    private string? _code;

    /// <summary>Raised after a successful connect, before the window closes.</summary>
    public event Action? Connected;

    /// <param name="redeem">Trades (server, code, name) for an API key; the real /api/peers/pair by default.</param>
    public SetupWindow(SetupDefaults defaults, Func<string, string, string, string?, Task> connect,
        Func<Uri, string, string, Task<string>>? redeem = null)
    {
        _connectAction = connect;
        _redeem = redeem ?? ((server, code, name) => LinkSetup.RedeemCodeAsync(Http, server, code, name));
        var heading = defaults.AlreadySetUp ? "Change Bragi connection" : "Connect to Bragi";
        Title = heading;
        _connect.Content = defaults.AlreadySetUp ? "Save and reconnect" : "Connect";
        Width = 480;
        SizeToContent = SizeToContent.Height;
        CanResize = false;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;

        _server.Text = defaults.Server;
        _name.Text = defaults.PeerName;
        _ip.Text = defaults.PeerIp;
        _apiKey.Text = defaults.ApiKey;

        var form = new StackPanel { Margin = new Thickness(20), Spacing = 6 };
        form.Children.Add(new TextBlock { Text = heading, FontSize = 22, FontWeight = FontWeight.SemiBold });
        form.Children.Add(new TextBlock
        {
            Text = (defaults.AlreadySetUp, defaults.VbanLink) switch
            {
                (true, false) => "This computer is already linked to Bragi. Change the server, name or address here; the audio link restarts with the new settings. Keeping the same name keeps the same ports.",
                (false, false) => "Registers this computer as a peer, then sets up and starts its audio link.",
                (true, true) => "This computer is already linked to Bragi. Change the server, name or address here. Keeping the same name keeps the same VBAN stream names.",
                (false, true) => "Registers this computer with Bragi. The tray app carries the audio: choose the devices in its Audio menu.",
            },
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 8),
        });
        AddField(form, "Connection link (Bragi dashboard: Add peer, Connect a device)", _link);
        form.Children.Add(_linkNote);
        _link.PropertyChanged += (_, e) =>
        {
            if (e.Property == TextBox.TextProperty)
            {
                UseLink();
            }
        };
        AddField(form, "Bragi server", _server);
        AddField(form, "This computer's name in Bragi", _name);
        AddField(form, "This computer's Tailscale address", _ip);
        AddField(form, "API key", _apiKey);

        foreach (var warning in defaults.Warnings)
        {
            form.Children.Add(new TextBlock { Text = warning, TextWrapping = TextWrapping.Wrap, Foreground = Brushes.DarkOrange });
        }
        form.Children.Add(_status);

        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 8, 0, 0) };
        buttons.Children.Add(_cancel);
        buttons.Children.Add(_connect);
        form.Children.Add(buttons);
        Content = form;

        _cancel.Click += (_, _) => Close();
        _connect.Click += async (_, _) => await ConnectAsync();
    }

    private static void AddField(Panel form, string label, TextBox box)
    {
        form.Children.Add(new TextBlock { Text = label, Margin = new Thickness(0, 4, 0, 0) });
        form.Children.Add(box);
    }

    private void UseLink()
    {
        var text = _link.Text ?? "";
        if (string.IsNullOrWhiteSpace(text))
        {
            _code = null;
            _linkNote.Text = "";
            return;
        }
        if (LinkSetup.ParseConnectLink(text) is not { } link)
        {
            _code = null;
            _linkNote.Text = "That isn't a Bragi connection link.";
            return;
        }
        _server.Text = link.Server;
        _code = link.Code;
        _linkNote.Text = link.Code is null
            ? "Server filled in."
            : "Server filled in. Connecting also gets this computer its own API key.";
    }

    private async Task ConnectAsync()
    {
        _connect.IsEnabled = false;
        _status.Foreground = null;
        _status.Text = "Connecting…";
        try
        {
            if (_code is not null && string.IsNullOrWhiteSpace(_apiKey.Text))
            {
                var server = LinkSetup.ServerUri(_server.Text)
                    ?? throw new LinkSetupException("Enter the Bragi server's address, e.g. https://bragi.your-tailnet.ts.net/.");
                // Kept in the field, so a retry after a later failure doesn't need the used code again.
                _apiKey.Text = await _redeem(server, _code, (_name.Text ?? "").Trim());
                _code = null;
            }
            await _connectAction(_server.Text ?? "", _name.Text ?? "", _ip.Text ?? "",
                string.IsNullOrWhiteSpace(_apiKey.Text) ? null : _apiKey.Text);
            _status.Text = "Connected.";
            Connected?.Invoke();
            Close();
        }
        catch (LinkSetupException ex)
        {
            _status.Foreground = Brushes.IndianRed;
            _status.Text = ex.Message;
        }
        finally
        {
            _connect.IsEnabled = true;
        }
    }
}
