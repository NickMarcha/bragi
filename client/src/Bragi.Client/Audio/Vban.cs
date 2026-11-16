using System;
using System.Buffers.Binary;
using System.Text;

namespace Bragi.Client.Audio;

/// <summary>One VBAN audio packet: 16-bit PCM, interleaved.</summary>
public sealed record VbanPacket(string Stream, int SampleRate, int Channels, int Frames, uint Counter, ReadOnlyMemory<byte> Pcm);

/// <summary>
/// The VBAN audio protocol (vb-audio.com, "VBAN Specification"), as far as
/// Bragi uses it: PCM audio, 16-bit integers. A packet is a 28-byte header
/// ("VBAN", sample rate index, samples per frame - 1, channels - 1, data
/// format, a 16-byte stream name, a frame counter) and up to 1436 bytes of
/// samples. PipeWire's vban-send/recv modules on the Bragi host speak the
/// same, as does Voicemeeter.
/// </summary>
public static class Vban
{
    public const int HeaderSize = 28;
    public const int MaxData = 1436;
    public const int MaxFrames = 256;
    private const byte DataInt16 = 0x01;
    private static readonly int[] Rates =
    [
        6000, 12000, 24000, 48000, 96000, 192000, 384000, 8000, 16000, 32000, 64000, 128000, 256000, 512000,
        11025, 22050, 44100, 88200, 176400, 352800, 705600,
    ];

    /// <summary>A packet's bytes, or null for one that can't be a VBAN PCM16 packet.</summary>
    public static byte[] Encode(string stream, int sampleRate, int channels, uint counter, ReadOnlySpan<byte> pcm)
    {
        var rateIndex = Array.IndexOf(Rates, sampleRate);
        var frames = pcm.Length / (2 * channels);
        if (rateIndex < 0 || channels is < 1 or > 256 || frames is < 1 or > MaxFrames || pcm.Length > MaxData
            || pcm.Length != frames * 2 * channels)
        {
            throw new ArgumentException("Not a VBAN PCM16 packet: rate, channels or length.");
        }
        var packet = new byte[HeaderSize + pcm.Length];
        "VBAN"u8.CopyTo(packet);
        packet[4] = (byte)rateIndex; // protocol bits 0: audio
        packet[5] = (byte)(frames - 1);
        packet[6] = (byte)(channels - 1);
        packet[7] = DataInt16; // codec bits 0: PCM
        var name = Encoding.ASCII.GetBytes(stream);
        name.AsSpan(0, Math.Min(name.Length, 16)).CopyTo(packet.AsSpan(8, 16));
        BinaryPrimitives.WriteUInt32LittleEndian(packet.AsSpan(24), counter);
        pcm.CopyTo(packet.AsSpan(HeaderSize));
        return packet;
    }

    /// <summary>The packet in these bytes, or null if it isn't VBAN PCM16 audio.</summary>
    public static VbanPacket? Decode(ReadOnlyMemory<byte> data)
    {
        var span = data.Span;
        if (span.Length < HeaderSize || !span[..4].SequenceEqual("VBAN"u8))
        {
            return null;
        }
        var protocol = span[4] & 0xE0;
        var rateIndex = span[4] & 0x1F;
        if (protocol != 0 || rateIndex >= Rates.Length || (span[7] & 0x07) != DataInt16 || (span[7] & 0xF0) != 0)
        {
            return null; // not audio, or not PCM 16-bit
        }
        var frames = span[5] + 1;
        var channels = span[6] + 1;
        var bytes = frames * channels * 2;
        if (span.Length < HeaderSize + bytes)
        {
            return null;
        }
        var nameBytes = span.Slice(8, 16);
        var end = nameBytes.IndexOf((byte)0);
        var name = Encoding.ASCII.GetString(end < 0 ? nameBytes : nameBytes[..end]);
        return new VbanPacket(name, Rates[rateIndex], channels, frames,
            BinaryPrimitives.ReadUInt32LittleEndian(span[24..]), data.Slice(HeaderSize, bytes));
    }

    /// <summary>Float samples (-1..1) as 16-bit little-endian PCM, clamped.</summary>
    public static void FloatToPcm16(ReadOnlySpan<float> samples, Span<byte> pcm)
    {
        for (var i = 0; i < samples.Length; i++)
        {
            var value = (int)MathF.Round(Math.Clamp(samples[i], -1f, 1f) * 32767f);
            BinaryPrimitives.WriteInt16LittleEndian(pcm[(2 * i)..], (short)value);
        }
    }
}
