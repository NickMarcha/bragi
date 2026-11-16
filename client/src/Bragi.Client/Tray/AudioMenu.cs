using System;
using System.Collections.Generic;
using System.Runtime.Versioning;
using Avalonia.Controls;
using Avalonia.Threading;
using Bragi.Client.Audio;
using NAudio.CoreAudioApi;

namespace Bragi.Client.Tray;

/// <summary>
/// The Windows tray's Audio submenu: where the headset mic plays, what goes
/// to the headset, and how the link is doing (Audio/VbanLink.cs). The device
/// lists are read again each time the menu opens.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class AudioMenu
{
    private readonly VbanLink _link;
    private readonly NativeMenuItem _status = new() { IsEnabled = false };
    private readonly NativeMenu _playback = new();
    private readonly NativeMenu _capture = new();

    public NativeMenuItem Item { get; }

    public AudioMenu(VbanLink link)
    {
        _link = link;
        var menu = new NativeMenu
        {
            _status,
            new NativeMenuItemSeparator(),
            new NativeMenuItem { Header = "Headset mic plays on", Menu = _playback },
            new NativeMenuItem { Header = "Sends to the headset", Menu = _capture },
        };
        menu.Opening += (_, _) => Refresh();
        Item = new NativeMenuItem { Header = "Audio", Menu = menu };
        _link.Changed += () => Dispatcher.UIThread.Post(RenderStatus);
        Refresh();
    }

    /// <summary>The status line for these devices and this link state.</summary>
    public static string StatusText(AudioDevices devices, bool receiving, string? problem)
    {
        if (problem is not null)
        {
            return problem;
        }
        if (devices.Playback is null && devices.Capture is null)
        {
            return "Off: choose devices below";
        }
        var parts = new List<string>();
        if (devices.Playback is not null)
        {
            parts.Add(receiving ? "hearing the headset mic" : "waiting for the headset mic");
        }
        if (devices.Capture is not null)
        {
            parts.Add("sending to the headset");
        }
        var text = string.Join(", ", parts);
        return char.ToUpperInvariant(text[0]) + text[1..];
    }

    private void RenderStatus() => _status.Header = StatusText(_link.Devices, _link.Receiving, _link.Problem);

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
        foreach (var input in SafeEndpoints(DataFlow.Capture))
        {
            _capture.Items.Add(Choice(input.Name, devices.Capture == input.Id && !devices.CaptureIsLoopback,
                () => Save(_link.Devices with { Capture = input.Id, CaptureIsLoopback = false })));
        }
        _capture.Items.Add(new NativeMenuItemSeparator());
        _capture.Items.Add(new NativeMenuItem { Header = "What's playing on:", IsEnabled = false });
        foreach (var output in SafeEndpoints(DataFlow.Render))
        {
            _capture.Items.Add(Choice(output.Name, devices.Capture == output.Id && devices.CaptureIsLoopback,
                () => Save(_link.Devices with { Capture = output.Id, CaptureIsLoopback = true })));
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
