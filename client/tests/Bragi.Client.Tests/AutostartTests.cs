using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

public class AutostartTests
{
    [Fact]
    public void AppImage_path_wins_over_the_mount_dir_binary()
    {
        var exec = AutostartService.ResolveExecCommand(
            "/home/u/.local/bin/BragiClient.AppImage", "/tmp/.mount_BragiCabc123/usr/bin/Bragi.Client", null);
        Assert.Equal("/home/u/.local/bin/BragiClient.AppImage", exec);
    }

    [Fact]
    public void Published_binary_is_used_as_is_without_an_AppImage()
    {
        Assert.Equal("/opt/bragi/Bragi.Client", AutostartService.ResolveExecCommand(null, "/opt/bragi/Bragi.Client", null));
    }

    [Fact]
    public void Framework_dependent_build_runs_the_dll_through_dotnet()
    {
        var exec = AutostartService.ResolveExecCommand("", "/usr/bin/dotnet", "/home/u/My Builds/Bragi.Client.dll");
        Assert.Equal("/usr/bin/dotnet \"/home/u/My Builds/Bragi.Client.dll\"", exec);
    }

    [Theory]
    [InlineData("Exec=/tmp/.mount_BragiCabc/usr/bin/Bragi.Client\n", "/tmp/.mount_BragiCabc/usr/bin/Bragi.Client")]
    [InlineData("Exec=/usr/bin/dotnet /x/Bragi.Client.dll\n", "/usr/bin/dotnet")]
    [InlineData("Exec=\"/home/u/my apps/Bragi.AppImage\" --tray\n", "/home/u/my apps/Bragi.AppImage")]
    [InlineData("Name=Bragi Client\n", null)]
    public void Exec_program_is_read_from_the_entry(string content, string? program)
    {
        Assert.Equal(program, AutostartService.ExecProgram("[Desktop Entry]\n" + content));
    }

    [Fact]
    public void Entry_pointing_at_a_vanished_mount_dir_is_rewritten()
    {
        var path = Path.GetTempFileName();
        try
        {
            File.WriteAllText(path, AutostartService.BuildDesktopFileContent("/tmp/.mount_BragiCgone/usr/bin/Bragi.Client"));

            Assert.True(AutostartService.RepairIfStale(path, "/home/u/.local/bin/BragiClient.AppImage", _ => false));
            Assert.Equal("/home/u/.local/bin/BragiClient.AppImage", AutostartService.ExecProgram(File.ReadAllText(path)));
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Working_entry_is_left_alone()
    {
        var path = Path.GetTempFileName();
        try
        {
            var content = AutostartService.BuildDesktopFileContent("/home/u/.local/bin/BragiClient.AppImage");
            File.WriteAllText(path, content);

            Assert.False(AutostartService.RepairIfStale(path, "/usr/bin/dotnet /dev/Bragi.Client.dll", _ => true));
            Assert.Equal(content, File.ReadAllText(path));
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Missing_entry_is_not_created()
    {
        var path = Path.Combine(Path.GetTempPath(), $"bragi-autostart-{Guid.NewGuid()}.desktop");
        Assert.False(AutostartService.RepairIfStale(path, "/x/BragiClient.AppImage", _ => false));
        Assert.False(File.Exists(path));
    }
}
