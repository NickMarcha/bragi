using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;

namespace Bragi.Client.Tray;

/// <summary>
/// Read-only pw-dump wrapper, checking whether the configured Roc sink and
/// source node names are present in the live PipeWire graph. Mirrors the
/// object-shape parsing in the Bragi server's app/pipewire.py::list_nodes()
/// (same "PipeWire:Interface:Node" / info.props.node.name fields), but this
/// app never calls pw-cli load-module/destroy itself - see RocLinkController.
/// </summary>
public static class PipewireStatusChecker
{
    public static async Task<HashSet<string>> GetAudioNodeNamesAsync()
    {
        var names = new HashSet<string>(StringComparer.Ordinal);
        string stdout;
        try
        {
            stdout = await ProcessRunner.RunOrThrowAsync("pw-dump", [], TimeSpan.FromSeconds(10));
        }
        catch (ProcessRunException)
        {
            return names;
        }

        using var doc = JsonDocument.Parse(stdout);
        foreach (var obj in doc.RootElement.EnumerateArray())
        {
            if (!obj.TryGetProperty("type", out var typeProp) ||
                typeProp.GetString() != "PipeWire:Interface:Node")
            {
                continue;
            }
            if (!obj.TryGetProperty("info", out var info) ||
                !info.TryGetProperty("props", out var props))
            {
                continue;
            }
            var mediaClass = props.TryGetProperty("media.class", out var mc) ? mc.GetString() : null;
            if (mediaClass is null || !mediaClass.Contains("Audio", StringComparison.Ordinal))
            {
                continue;
            }
            var nodeName = props.TryGetProperty("node.name", out var nn) ? nn.GetString() : null;
            if (nodeName is not null)
            {
                names.Add(nodeName);
            }
        }
        return names;
    }
}
