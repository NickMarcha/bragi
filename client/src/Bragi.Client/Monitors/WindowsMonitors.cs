using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Threading.Tasks;
using Microsoft.Win32;

namespace Bragi.Client.Monitors;

/// <summary>
/// Windows' monitors: each one's rectangle on the desktop in physical pixels
/// (read per-monitor DPI aware, which is how Deskflow's client sees the
/// desktop), its scaling and rotation, and its EDID from the registry
/// (HKLM\SYSTEM\CurrentControlSet\Enum\DISPLAY\...\Device Parameters\EDID,
/// readable by any user).
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class WindowsMonitors : IMonitorSource
{
    public string Kind => "windows";

    private const int MonitorInfoPrimary = 1, EnumCurrentSettings = -1, GetDeviceInterfaceName = 1;
    private static readonly IntPtr PerMonitorAwareV2 = new(-4);

    [StructLayout(LayoutKind.Sequential)]
    private struct Rect { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct MonitorInfoEx
    {
        public int Size;
        public Rect Monitor, Work;
        public int Flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string Device;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct DisplayDevice
    {
        public int Size;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public int StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceId;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct DevMode
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        public short SpecVersion, DriverVersion, Size, DriverExtra;
        public int Fields, PositionX, PositionY, DisplayOrientation, DisplayFixedOutput;
        public short Color, Duplex, YResolution, TtOption, Collate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string FormName;
        public short LogPixels;
        public int BitsPerPel, PelsWidth, PelsHeight, DisplayFlags, DisplayFrequency;
        public int IcmMethod, IcmIntent, MediaType, DitherType, Reserved1, Reserved2, PanningWidth, PanningHeight;
    }

    private delegate bool MonitorEnumProc(IntPtr monitor, IntPtr hdc, IntPtr rect, IntPtr data);

    [DllImport("user32.dll")] private static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc callback, IntPtr data);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool GetMonitorInfoW(IntPtr monitor, ref MonitorInfoEx info);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool EnumDisplayDevicesW(string? device, int index, ref DisplayDevice info, int flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool EnumDisplaySettingsW(string device, int mode, ref DevMode info);
    [DllImport("user32.dll")] private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("shcore.dll")] private static extern int GetDpiForMonitor(IntPtr monitor, int type, out uint dpiX, out uint dpiY);

    public Task<IReadOnlyList<MonitorInfo>> ReadAsync() => Task.FromResult(Read());

    private static IReadOnlyList<MonitorInfo> Read()
    {
        var handles = new List<IntPtr>();
        var old = SetThreadDpiAwarenessContext(PerMonitorAwareV2);
        try
        {
            EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, (monitor, _, _, _) => { handles.Add(monitor); return true; }, IntPtr.Zero);
            var monitors = new List<MonitorInfo>();
            foreach (var handle in handles)
            {
                var info = new MonitorInfoEx { Size = Marshal.SizeOf<MonitorInfoEx>() };
                if (!GetMonitorInfoW(handle, ref info))
                {
                    continue;
                }
                var scale = GetDpiForMonitor(handle, 0, out var dpi, out _) == 0 && dpi > 0 ? dpi / 96.0 : 1.0;
                var mode = new DevMode { Size = (short)Marshal.SizeOf<DevMode>() };
                var rotation = EnumDisplaySettingsW(info.Device, EnumCurrentSettings, ref mode) ? mode.DisplayOrientation * 90 : 0;
                var (width, height) = (info.Monitor.Right - info.Monitor.Left, info.Monitor.Bottom - info.Monitor.Top);
                var (pixelWidth, pixelHeight) = mode.PelsWidth > 0 ? (mode.PelsWidth, mode.PelsHeight) : (width, height);
                monitors.Add(MonitorInfo.From(ReadEdid(info.Device), info.Device.Replace(@"\\.\", ""),
                    info.Monitor.Left, info.Monitor.Top, width, height, pixelWidth, pixelHeight,
                    scale, rotation, (info.Flags & MonitorInfoPrimary) != 0));
            }
            return monitors.OrderBy(m => m.X).ThenBy(m => m.Y).ToList();
        }
        finally
        {
            if (old != IntPtr.Zero)
            {
                SetThreadDpiAwarenessContext(old);
            }
        }
    }

    /// <summary>
    /// The EDID of the monitor on an adapter output (\\.\DISPLAY1): its device
    /// interface, \\?\DISPLAY#GSM5B09#5&amp;...&amp;UID4353#{...}, names the
    /// registry key that holds it.
    /// </summary>
    private static Edid? ReadEdid(string device)
    {
        var display = new DisplayDevice { Size = Marshal.SizeOf<DisplayDevice>() };
        if (!EnumDisplayDevicesW(device, 0, ref display, GetDeviceInterfaceName) || string.IsNullOrEmpty(display.DeviceId))
        {
            return null;
        }
        var parts = display.DeviceId.Split('#');
        if (parts.Length < 3)
        {
            return null;
        }
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey($@"SYSTEM\CurrentControlSet\Enum\DISPLAY\{parts[1]}\{parts[2]}\Device Parameters");
            return key?.GetValue("EDID") is byte[] data ? Edid.Parse(data) : null;
        }
        catch (Exception ex) when (ex is System.Security.SecurityException or UnauthorizedAccessException or System.IO.IOException)
        {
            return null;
        }
    }
}
