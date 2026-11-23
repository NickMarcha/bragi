using System.Collections.Generic;
using System.Text.Json;
using Bragi.Client.Audio;
using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

/// <summary>Discord as a stream of its own (bragi#93): Audio/AppCapture.cs,
/// Audio/VbanLink.cs and the Audio menu's lines.</summary>
public class AudioGroupTests
{
    [Fact]
    public void Discords_root_process_is_the_one_whose_parent_isnt_discord()
    {
        var processes = new List<(uint, uint, string)>
        {
            (4, 0, "System"), (16208, 900, "Discord.exe"), (3060, 16208, "Discord.exe"),
            (3388, 16208, "Discord.exe"), (7000, 3388, "crashpad.exe"), (8000, 1, "explorer.exe"),
        };
        Assert.Equal(16208u, AppCapture.RootOf(processes, "discord.exe"));
        Assert.Null(AppCapture.RootOf(processes, "Spotify.exe"));
        Assert.Equal(new HashSet<uint> { 16208, 3060, 3388, 7000 }, AppCapture.TreeOf(processes, 16208));
    }

    [Fact]
    public void Mono_is_the_average_of_both_channels_and_the_peak_is_kept()
    {
        var pcm = new List<byte>();
        var peak = 0f;
        AppCapture.Append(pcm, new short[] { 1000, 3000, -16384, -16384 }, 1, ref peak);
        Assert.Equal(new byte[] { 0xD0, 0x07, 0x00, 0xC0 }, pcm.ToArray()); // 2000, -16384
        Assert.Equal(0.5f, peak);

        pcm.Clear();
        AppCapture.Append(pcm, new short[] { 1, 2 }, 2, ref peak);
        Assert.Equal(new byte[] { 1, 0, 2, 0 }, pcm.ToArray());
        Assert.Equal(0.5f, peak); // a quieter buffer doesn't lower it
    }

    [Fact]
    public void The_comms_stream_name_fits_vbans_16_bytes()
    {
        Assert.Equal("SageAudioComms", VbanLink.CommsStreamName("SageAudio"));
        Assert.Equal(16, VbanLink.CommsStreamName("AVeryLongStreamName").Length);
    }

    [Fact]
    public void An_audio_json_from_before_groups_still_loads()
    {
        var old = JsonSerializer.Deserialize<AudioDevices>("""{"Playback":"a","Capture":"b","CaptureIsLoopback":true}""")!;
        Assert.False(old.SendCommsSeparately);
        var now = old with { SendCommsSeparately = true };
        Assert.Equal(now, JsonSerializer.Deserialize<AudioDevices>(JsonSerializer.Serialize(now)));
    }

    private static LinkReport Report(CommsReport? comms, string? capture = "steam-speakers") =>
        new(new AudioDevices("mic", capture, true, comms is not null), "Mic", "Steam", true, 0.1f, 0.1f, 0.1f, null, comms);

    [Fact]
    public void The_menu_says_what_the_discord_stream_does()
    {
        Assert.Equal((null, null), AudioMenu.CommsLines(Report(null)));
        Assert.Equal(("Discord: not running", null), AudioMenu.CommsLines(Report(new CommsReport(false, 0, [], null))));
        Assert.Equal("Discord: silent", AudioMenu.CommsLines(Report(new CommsReport(true, 0, [], null))).Line);
        Assert.Equal("Discord ■■■□□ on its own", AudioMenu.CommsLines(Report(new CommsReport(true, 0.05f, [], null))).Line);

        // Heard twice: on the device the desktop stream captures, or into Voicemeeter.
        var onDesktop = AudioMenu.CommsLines(Report(new CommsReport(true, 0.05f, [new AudioEndpoint("steam-speakers", "Speakers (Steam Streaming Speakers)")], null)));
        Assert.StartsWith("Discord also plays on Speakers (Steam Streaming Speakers)", onDesktop.Note);
        var intoVoicemeeter = AudioMenu.CommsLines(Report(new CommsReport(true, 0.05f, [new AudioEndpoint("x", "Voicemeeter AUX Input (VB-Audio Voicemeeter VAIO)")], null)));
        Assert.StartsWith("Discord also plays on Voicemeeter AUX Input", intoVoicemeeter.Note);
        Assert.Null(AudioMenu.CommsLines(Report(new CommsReport(true, 0.05f, [new AudioEndpoint("o", "Headphones (Oculus Virtual Audio Device)")], null))).Note);
    }
}
