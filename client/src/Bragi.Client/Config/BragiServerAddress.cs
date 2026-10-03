using System;

namespace Bragi.Client.Config;

/// <summary>Derives dashboard addresses from the existing presence endpoint configuration.</summary>
public sealed record BragiServerAddress(Uri WebUi, Uri Controls)
{
    public static BragiServerAddress? FromPresenceUrl(string? value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) ||
            (uri.Scheme != "ws" && uri.Scheme != "wss") ||
            !uri.AbsolutePath.TrimEnd('/').EndsWith("/ws/peer", StringComparison.Ordinal))
        {
            return null;
        }

        var prefix = uri.AbsolutePath.TrimEnd('/')[..^"/ws/peer".Length];
        var builder = new UriBuilder(uri) { Path = prefix + "/ws", Query = "", Fragment = "" };
        var controls = builder.Uri;
        builder.Scheme = uri.Scheme == "wss" ? "https" : "http";
        builder.Path = prefix + "/";
        return new BragiServerAddress(builder.Uri, controls);
    }
}
