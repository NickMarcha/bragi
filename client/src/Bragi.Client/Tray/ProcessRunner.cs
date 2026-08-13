using System;
using System.Diagnostics;
using System.Threading;
using System.Threading.Tasks;

namespace Bragi.Client.Tray;

/// <summary>
/// Small subprocess wrapper mirroring app/pipewire.py's `_run()` in the
/// Bragi server: shell out, capture output, enforce a timeout, throw a
/// typed exception on nonzero exit. Deliberately dumb - let the real CLI
/// tools (systemctl, pw-dump) do the actual work.
/// </summary>
public sealed class ProcessRunResult
{
    public required int ExitCode { get; init; }
    public required string Stdout { get; init; }
    public required string Stderr { get; init; }
}

public sealed class ProcessRunException : Exception
{
    public ProcessRunException(string message) : base(message) { }
}

public static class ProcessRunner
{
    public static async Task<ProcessRunResult> RunAsync(
        string fileName,
        string[] args,
        TimeSpan? timeout = null)
    {
        var psi = new ProcessStartInfo
        {
            FileName = fileName,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
        };
        foreach (var arg in args)
        {
            psi.ArgumentList.Add(arg);
        }

        using var process = new Process { StartInfo = psi };
        using var cts = new CancellationTokenSource(timeout ?? TimeSpan.FromSeconds(10));

        try
        {
            process.Start();
            var stdoutTask = process.StandardOutput.ReadToEndAsync(cts.Token);
            var stderrTask = process.StandardError.ReadToEndAsync(cts.Token);
            await process.WaitForExitAsync(cts.Token);
            var stdout = await stdoutTask;
            var stderr = await stderrTask;
            return new ProcessRunResult { ExitCode = process.ExitCode, Stdout = stdout, Stderr = stderr };
        }
        catch (OperationCanceledException)
        {
            try { process.Kill(entireProcessTree: true); } catch { /* best-effort */ }
            throw new ProcessRunException($"{fileName} timed out");
        }
    }

    /// <summary>Runs and throws if the exit code is nonzero, same shape as _run() raising PipewireError.</summary>
    public static async Task<string> RunOrThrowAsync(string fileName, string[] args, TimeSpan? timeout = null)
    {
        var result = await RunAsync(fileName, args, timeout);
        if (result.ExitCode != 0)
        {
            var detail = string.IsNullOrWhiteSpace(result.Stderr) ? result.Stdout : result.Stderr;
            throw new ProcessRunException($"{fileName} {string.Join(' ', args)} failed: {detail.Trim()}");
        }
        return result.Stdout;
    }
}
