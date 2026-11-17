using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.Versioning;
using Avalonia.Controls;
using Avalonia.Threading;
using Bragi.Client.Audio;
using NAudio.CoreAudioApi;

namespace Bragi.Client.Tray;

/// <summary>
/// The Windows tray's Audio submenu: what the link is doing, with levels
/// (Audio/VbanLink.cs), then where the headset mic plays and what goes to
/// the headset. The device lists are read again each time the menu opens.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class AudioMenu
{
    private readonly VbanLink _link;
    private readonly NativeMenuItem _mic = new() { IsEnabled = false };
    private readonly NativeMenuItem _send = new() { IsEnabled = false };
    private readonly NativeMenuItem _note = new() { IsEnabled = false, IsVisible = false };
    private readonly NativeMenu _playback = new();
    private readonly NativeMenu _capture = new();

    public NativeMenuItem Item { get; }

    public AudioMenu(VbanLink link)
    {
        _link = link;
        var menu = new NativeMenu
        {
            _mic,
            _send,
            _note,
            new NativeMenuItemSeparator(),
            new NativeMenuItem { Header = "Play the headset mic on", Menu = _playback },
            new NativeMenuItem { Header = "Send to the headset", Menu = _capture },
        };
        menu.Opening += (_, _) => Refresh();
        Item = new NativeMenuItem { Header = "Audio", Menu = menu };
        _link.Changed += () => Dispatcher.UIThread.Post(RenderStatus);
        Refresh();
    }

    /// <summary>The status lines (headset mic, sending, note) for this report.</summary>
    public static (string Mic, string Send, string? Note) StatusLines(LinkReport r, bool voicemeeterRunning)
    {
        if (r.Devices.Playback is null && r.Devices.Capture is null)
        {
            return ("Off: choose where the audio goes below", "", r.Problem);
        }

        var mic = r.Devices.Playback is null
            ? "Headset mic: off"
            : r.PlaybackName is null
                ? "Headset mic: its output device isn't plugged in"
                : r.Receiving
                    ? $"Headset mic {VbanLink.Bars(r.ReceiveLevel)} on {r.PlaybackName}"
                    : "Headset mic: nothing arriving from Bragi";

        string send;
        string? note = r.Problem;
        if (r.Devices.Capture is null)
        {
            send = "To the headset: off";
        }
        else if (r.CaptureName is null)
        {
            send = "To the headset: its device isn't plugged in";
        }
        else if (r.SendLevel >= 0.0005f)
        {
            send = $"To the headset {VbanLink.Bars(r.SendLevel)} from {r.CaptureName}";
        }
        else
        {
            send = r.Devices.CaptureIsLoopback
                ? $"To the headset: nothing is playing on {r.CaptureName}"
                : $"To the headset: {r.CaptureName} is silent";
            // Voicemeeter's WDM and KS outputs skip the Windows mixer, so a
            // loopback of the device they play to hears nothing.
            if (note is null && r.Devices.CaptureIsLoopback && voicemeeterRunning)
            {
                note = "If Voicemeeter plays to it, pick the device's MME entry there, not WDM or KS";
            }
        }
        return (mic, send, note);
    }

    private void RenderStatus()
    {
        var (mic, send, note) = StatusLines(_link.Report, VoicemeeterRunning());
        _mic.Header = mic;
        _send.Header = send;
        _send.IsVisible = send.Length > 0;
        _note.Header = note ?? "";
        _note.IsVisible = note is not null;
    }

    private static bool VoicemeeterRunning()
    {
        foreach (var name in new[] { "voicemeeter", "voicemeeterpro", "voicemeeter8", "voicemeeter8x64", "voicemeeterpro_x64", "voicemeeter_x64" })
        {
            var found = Process.GetProcessesByName(name);
            foreach (var p in found)
            {
                p.Dispose();
            }
            if (found.Length > 0)
            {
                return true;
            }
        }
        return false;
    }

    private void Refresh()
    {
        RenderStatus();
        var devices = _link.Devices;

        _playback.Items.Clear();
        _playback.Items.Add(Choice("Off", devices.Playback is null, () => Save(_link.Devices with { Playback = null })));
        foreach (var output in SafeEndpoints(DataFlow.Render))
        {
            _playback.Items.Add(Choice(output.Name, devices.Playback == output.Id,
                () => Save(_link.Devices with { Playback = output.Id })));
        }

        _capture.Items.Clear();
        _capture.Items.Add(Choice("Off", devices.Capture is null, () => Save(_link.Devices with { Capture = null })));
        _capture.Items.Add(new NativeMenuItemSeparator());
        _capture.Items.Add(new NativeMenuItem { Header = "What plays on an output:", IsEnabled = false });
        foreach (var output in SafeEndpoints(DataFlow.Render))
        {
            _capture.Items.Add(Choice(output.Name, devices.Capture == output.Id && devices.CaptureIsLoopback,
                () => Save(_link.Devices with { Capture = output.Id, CaptureIsLoopback = true })));
        }
        _capture.Items.Add(new NativeMenuItemSeparator());
        _capture.Items.Add(new NativeMenuItem { Header = "A microphone or input:", IsEnabled = false });
        foreach (var input in SafeEndpoints(DataFlow.Capture))
        {
            _capture.Items.Add(Choice(input.Name, devices.Capture == input.Id && !devices.CaptureIsLoopback,
                () => Save(_link.Devices with { Capture = input.Id, CaptureIsLoopback = false })));
        }
    }

    private void Save(AudioDevices devices)
    {
        devices.Save();
        _link.Apply(devices);
        Refresh();
    }

    private static NativeMenuItem Choice(string header, bool selected, Action choose)
    {
        var item = new NativeMenuItem { Header = header, ToggleType = MenuItemToggleType.Radio, IsChecked = selected };
        item.Click += (_, _) => choose();
        return item;
    }

    private static IReadOnlyList<AudioEndpoint> SafeEndpoints(DataFlow flow)
    {
        try
        {
            return VbanLink.Endpoints(flow);
        }
        catch (System.Runtime.InteropServices.COMException)
        {
            return [];
        }
    }
}
