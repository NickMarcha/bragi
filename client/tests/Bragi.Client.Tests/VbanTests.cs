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

    [Fact]
    public void The_menu_says_what_the_link_does()
    {
        Assert.Equal("Off: choose devices below", AudioMenu.StatusText(AudioDevices.Off, false, null));
        Assert.Equal("Waiting for the headset mic, sending to the headset",
            AudioMenu.StatusText(new AudioDevices("out", "in", false), false, null));
        Assert.Equal("Hearing the headset mic", AudioMenu.StatusText(new AudioDevices("out", null, false), true, null));
        Assert.Equal("port busy", AudioMenu.StatusText(new AudioDevices("out", null, false), true, "port busy"));
    }
}
