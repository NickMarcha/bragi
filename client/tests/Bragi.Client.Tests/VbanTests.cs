using Bragi.Client.Audio;
using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

/// <summary>The Windows tray's own VBAN link (Audio/Vban.cs, Tray/AudioMenu.cs).</summary>
public class VbanTests
{
    [Fact]
    public void A_packet_round_trips_with_the_header_the_host_expects()
    {
        var pcm = new byte[256 * 2 * 2];
        pcm[0] = 0x34;
        pcm[^1] = 0x12;
        var bytes = Vban.Encode("SageAudio", 48000, 2, 7, pcm);

        Assert.Equal(28 + 1024, bytes.Length);
        Assert.Equal("VBAN"u8.ToArray(), bytes[..4]);
        Assert.Equal((byte)3, bytes[4]);   // 48 kHz is rate index 3
        Assert.Equal((byte)255, bytes[5]); // 256 samples per frame - 1
        Assert.Equal((byte)1, bytes[6]);   // 2 channels - 1
        Assert.Equal((byte)1, bytes[7]);   // INT16, PCM

        var packet = Vban.Decode(bytes)!;
        Assert.Equal(("SageAudio", 48000, 2, 256, 7u), (packet.Stream, packet.SampleRate, packet.Channels, packet.Frames, packet.Counter));
        Assert.Equal(pcm, packet.Pcm.ToArray());
    }

    [Fact]
    public void Anything_but_pcm16_audio_is_not_a_packet()
    {
        var good = Vban.Encode("SagepiMic", 48000, 2, 0, new byte[64]);
        var text = (byte[])good.Clone();
        text[4] |= 0x40; // the serial/text sub-protocols
        var floats = (byte[])good.Clone();
        floats[7] = 0x04; // FLOAT32
        Assert.Null(Vban.Decode(text));
        Assert.Null(Vban.Decode(floats));
        Assert.Null(Vban.Decode(good[..40]));         // shorter than its header says
        Assert.Null(Vban.Decode("VBAX"u8.ToArray()));
        Assert.Throws<ArgumentException>(() => Vban.Encode("x", 47000, 2, 0, new byte[64]));
    }

    [Fact]
    public void Floats_become_clamped_16_bit_samples()
    {
        var pcm = new byte[8];
        Vban.FloatToPcm16([1f, -1f, 0.5f, 3f], pcm);
        Assert.Equal(new short[] { 32767, -32767, 16384, 32767 },
            Enumerable.Range(0, 4).Select(i => BitConverter.ToInt16(pcm, 2 * i)).ToArray());
    }

    private static LinkReport Report(AudioDevices devices, bool receiving = false, float received = 0, float sent = 0,
        string? playbackName = "Speakers", string? captureName = "Steam Streaming Speakers", string? problem = null) =>
        new(devices, devices.Playback is null ? null : playbackName, devices.Capture is null ? null : captureName,
            receiving, received, sent, 0, problem);

    [Fact]
    public void The_menu_says_what_the_link_does_with_levels()
    {
        Assert.Equal(("Off: choose where the audio goes below", "", null),
            AudioMenu.StatusLines(Report(AudioDevices.Off), false));

        var both = new AudioDevices("out", "in", true);
        var (mic, send, note) = AudioMenu.StatusLines(Report(both, receiving: true, received: 0.5f, sent: 0.05f), false);
        Assert.Equal("Headset mic ■■■■■ on Speakers", mic);
        Assert.Equal("To the headset ■■■□□ from Steam Streaming Speakers", send);
        Assert.Null(note);

        (mic, send, _) = AudioMenu.StatusLines(Report(both), false);
        Assert.Equal("Headset mic: nothing arriving from Bragi", mic);
        Assert.Equal("To the headset: nothing is playing on Steam Streaming Speakers", send);

        (_, send, _) = AudioMenu.StatusLines(Report(new AudioDevices(null, "in", false), captureName: "Headset Microphone"), false);
        Assert.Equal("To the headset: Headset Microphone is silent", send);

        (mic, _, _) = AudioMenu.StatusLines(Report(new AudioDevices("out", null, false), playbackName: null), false);
        Assert.Equal("Headset mic: its output device isn't plugged in", mic);
    }

    [Fact]
    public void A_silent_loopback_with_Voicemeeter_running_points_at_its_MME_entry()
    {
        var loopback = Report(new AudioDevices(null, "in", true));
        Assert.Contains("MME", AudioMenu.StatusLines(loopback, true).Note);
        Assert.Null(AudioMenu.StatusLines(loopback, false).Note);
        Assert.Equal("port busy", AudioMenu.StatusLines(loopback with { Problem = "port busy" }, true).Note);
    }

    [Fact]
    public void Level_bars_step_every_12_dB()
    {
        Assert.Equal("□□□□□", VbanLink.Bars(0));
        Assert.Equal("□□□□□", VbanLink.Bars(0.0005f));  // -66 dBFS
        Assert.Equal("■□□□□", VbanLink.Bars(0.002f));   // -54
        Assert.Equal("■■□□□", VbanLink.Bars(0.01f));    // -40
        Assert.Equal("■■■□□", VbanLink.Bars(0.05f));    // -26
        Assert.Equal("■■■■■", VbanLink.Bars(1f));
        Assert.Equal("■■■■■", VbanLink.Bars(2f));
    }
}
