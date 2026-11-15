using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;

namespace Bragi.Client.Tray;

/// <summary>Which computers share the desk's keyboard and mouse, and which one has them.</summary>
public sealed record KvmState(bool Available, string? Active, IReadOnlyList<string> Computers)
{
    public static readonly KvmState None = new(false, null, []);
}

/// <summary>
/// The tray's "Keyboard &amp; mouse" menu: the Bragi host sends "kvm" on the
/// peer socket at connect and whenever the active computer changes
/// (app/kvm.py); choosing a computer sends "kvm_switch". Runs on the
/// socket's thread; Changed is raised there too.
/// </summary>
public sealed class KvmService
{
    public KvmState State { get; private set; } = KvmState.None;
    /// <summary>The last switch the host refused, cleared by the next state.</summary>
    public string? Error { get; private set; }
    public event Action? Changed;
    public Func<object, Task<bool>>? Send { get; set; }

    public void OnMessage(JsonElement message)
    {
        if (!message.TryGetProperty("type", out var type))
        {
            return;
        }
        switch (type.GetString())
        {
            case "kvm":
                State = new KvmState(
                    message.TryGetProperty("available", out var a) && a.ValueKind == JsonValueKind.True,
                    message.TryGetProperty("active", out var act) && act.ValueKind == JsonValueKind.String ? act.GetString() : null,
                    message.TryGetProperty("computers", out var list) && list.ValueKind == JsonValueKind.Array
                        ? list.EnumerateArray().Where(c => c.ValueKind == JsonValueKind.String).Select(c => c.GetString()!).ToList()
                        : []);
                Error = null;
                Changed?.Invoke();
                break;
            case "kvm_error":
                Error = message.TryGetProperty("error", out var e) ? e.GetString() : "Switching failed";
                Changed?.Invoke();
                break;
        }
    }

    public void OnDisconnected()
    {
        State = KvmState.None;
        Error = null;
        Changed?.Invoke();
    }

    public Task<bool> SwitchAsync(string computer) =>
        Send?.Invoke(new { type = "kvm_switch", computer }) ?? Task.FromResult(false);

    /// <summary>The submenu's rows: (computer, ticked). Empty hides the menu.</summary>
    public static IReadOnlyList<(string Computer, bool Active)> MenuRows(KvmState state) =>
        state.Available ? state.Computers.Select(c => (c, c == state.Active)).ToList() : [];
}
