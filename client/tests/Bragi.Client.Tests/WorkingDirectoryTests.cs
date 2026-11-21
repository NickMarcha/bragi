using Xunit;

namespace Bragi.Client.Tests;

public class WorkingDirectoryTests
{
    [Theory]
    [InlineData("/tmp/.mount_BragiCaNLblA/usr/bin", "/home/sage")]
    [InlineData("/tmp/.mount_BragiCaNLblA", "/home/sage")]
    public void Leaves_an_appimage_mount_for_home(string current, string expected) =>
        Assert.Equal(expected, WorkingDirectory.Target(current, "/home/sage"));

    [Theory]
    [InlineData("/home/sage")]
    [InlineData("/home/sage/projects")]
    public void Stays_anywhere_else(string current) =>
        Assert.Null(WorkingDirectory.Target(current, "/home/sage"));

    [Fact]
    public void Stays_without_a_home() =>
        Assert.Null(WorkingDirectory.Target("/tmp/.mount_x/usr/bin", ""));

    private const string AppDir = @"C:\Users\u\AppData\Local\BragiClient\current\";

    [Theory]
    [InlineData(@"C:\Users\u\AppData\Local\BragiClient\current")]
    [InlineData(@"C:\Users\u\AppData\Local\BragiClient\current\")]
    [InlineData(@"c:\users\u\appdata\local\bragiclient\current\runtimes")]
    public void Leaves_the_install_dir(string current) =>
        Assert.Equal(@"C:\Users\u", WorkingDirectory.Target(current, @"C:\Users\u", AppDir));

    [Theory]
    [InlineData(@"C:\Users\u\AppData\Local\BragiClient\currently")]
    [InlineData(@"C:\Users\u")]
    public void Stays_outside_the_install_dir(string current) =>
        Assert.Null(WorkingDirectory.Target(current, @"C:\Users\u", AppDir));
}
