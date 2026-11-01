using System;

namespace Bragi.Client.Config;

/// <summary>Derives dashboard addresses from the existing presence endpoint configuration.</summary>
public sealed record BragiServerAddress(Uri WebUi, Uri Controls)
{
    /// <param name="apiKey">Sent as ?token= on the controls socket, which a
    /// password-protected Bragi requires (app/access.py).</param>
    public static BragiServerAddress? FromPresenceUrl(string? value, string? apiKey = null)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) ||
            (uri.Scheme != "ws" && uri.Scheme != "wss") ||
            !uri.AbsolutePath.TrimEnd('/').EndsWith("/ws/peer", StringComparison.Ordinal))
        {
            return null;
        }

        var prefix = uri.AbsolutePath.TrimEnd('/')[..^"/ws/peer".Length];
        var query = string.IsNullOrWhiteSpace(apiKey) ? "" : "token=" + Uri.EscapeDataString(apiKey.Trim());
        var builder = new UriBuilder(uri) { Path = prefix + "/ws", Query = query, Fragment = "" };
        var controls = builder.Uri;
        builder.Scheme = uri.Scheme == "wss" ? "https" : "http";
        builder.Path = prefix + "/";
        builder.Query = "";
        return new BragiServerAddress(builder.Uri, controls);
    }
}
