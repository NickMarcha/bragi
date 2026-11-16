using System;
using System.Collections.Generic;
using System.Text.Json;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;

namespace Bragi.Client.Tray;

/// <summary>
/// "Identify" from the dashboard's KVM setup, like the one in Windows' display
/// settings: the Bragi host sends "kvm_identify" on the peer socket and this
/// shows the computer's KVM name, big, on each of its screens for a few
/// seconds. A click closes it early.
/// </summary>
public static class IdentifyOverlay
{
    private static readonly TimeSpan ShowFor = TimeSpan.FromSeconds(4);
    private static readonly List<Window> Open = [];
    private const double OverlayWidth = 900, OverlayHeight = 300;

    /// <summary>The text to show for a kvm_identify message, or null if it isn't one.</summary>
    public static (string Title, string Note)? Text(JsonElement message)
    {
        if (!message.TryGetProperty("type", out var type) || type.GetString() != "kvm_identify")
        {
            return null;
        }
        var computer = message.TryGetProperty("computer", out var c) && c.ValueKind == JsonValueKind.String ? c.GetString() : null;
        var peer = message.TryGetProperty("peer", out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() : null;
        return computer is not null
            ? (computer, "Keyboard and mouse: this computer")
            : (peer ?? "?", "Not in the KVM setup under this name. Add it as a computer or an alias.");
    }

    /// <summary>Peer socket handler; runs on the socket's thread.</summary>
    public static void OnMessage(JsonElement message)
    {
        if (Text(message) is { } text)
        {
            Dispatcher.UIThread.Post(() => Show(text.Title, text.Note));
        }
    }

    private static void Show(string title, string note)
    {
        Close();
        // A window is needed to ask for the screens; the first one doubles as
        // the overlay on the primary screen.
        var first = Build(title, note);
        var screens = first.Screens.All;
        if (screens.Count == 0)
        {
            Place(first, null);
        }
        for (var i = 0; i < screens.Count; i++)
        {
            Place(i == 0 ? first : Build(title, note), screens[i]);
        }
        var timer = new DispatcherTimer { Interval = ShowFor };
        timer.Tick += (_, _) =>
        {
            timer.Stop();
            Close();
        };
        timer.Start();
    }

    private static Window Build(string title, string note)
    {
        var window = new Window
        {
            WindowDecorations = WindowDecorations.None,
            Topmost = true,
            ShowInTaskbar = false,
            ShowActivated = false,
            CanResize = false,
            Width = OverlayWidth, Height = OverlayHeight,
            WindowStartupLocation = WindowStartupLocation.Manual,
            Background = new SolidColorBrush(Color.FromArgb(0xE6, 0x18, 0x1A, 0x1F)),
            Title = "Bragi: identify",
            Content = new StackPanel
            {
                VerticalAlignment = VerticalAlignment.Center,
                Spacing = 8,
                HorizontalAlignment = HorizontalAlignment.Center,
                Children =
                {
                    new TextBlock
                    {
                        Text = title, FontSize = 120, FontWeight = FontWeight.SemiBold,
                        Foreground = Brushes.White, HorizontalAlignment = HorizontalAlignment.Center,
                    },
                    new TextBlock
                    {
                        Text = note, FontSize = 20, Foreground = new SolidColorBrush(Color.Parse("#b8bcc6")),
                        HorizontalAlignment = HorizontalAlignment.Center,
                    },
                },
            },
        };
        window.PointerPressed += (_, _) => Close();
        Open.Add(window);
        return window;
    }

    private static void Place(Window window, Avalonia.Platform.Screen? screen)
    {
        if (screen is null)
        {
            window.WindowStartupLocation = WindowStartupLocation.CenterScreen;
            window.Show();
            return;
        }
        // Centred on that screen's work area: Position is in the screen's
        // pixels, the size in DIPs.
        var area = screen.WorkingArea;
        window.Position = new PixelPoint(
            area.X + (int)Math.Max(0, (area.Width - OverlayWidth * screen.Scaling) / 2),
            area.Y + (int)Math.Max(0, (area.Height - OverlayHeight * screen.Scaling) / 2));
        window.Show();
    }

    private static void Close()
    {
        foreach (var window in Open.ToArray())
        {
            window.Close();
        }
        Open.Clear();
    }
}
