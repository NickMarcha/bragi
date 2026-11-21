using System.Collections.Generic;
using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

public class LinkRefreshTests
{
    private static readonly Dictionary<string, string> Bundled = new()
    {
        ["bragi-roc-link-run.sh"] = "#!/bin/bash\necho new\n",
        ["bragi-roc-link.service"] = "[Unit]\nDescription=new\n",
    };

    [Fact]
    public void Up_to_date_files_are_left_alone() =>
        Assert.Empty(LinkInstaller.Stale(new Dictionary<string, string?>
        {
            ["bragi-roc-link-run.sh"] = "#!/bin/bash\necho new\n",
            ["bragi-roc-link.service"] = "[Unit]\nDescription=new\n",
        }, Bundled));

    [Fact]
    public void Crlf_alone_is_not_a_difference() =>
        Assert.Empty(LinkInstaller.Stale(new Dictionary<string, string?>
        {
            ["bragi-roc-link-run.sh"] = "#!/bin/bash\r\necho new\r\n",
            ["bragi-roc-link.service"] = "[Unit]\r\nDescription=new\r\n",
        }, Bundled));

    [Fact]
    public void An_old_script_is_stale() =>
        Assert.Equal(["bragi-roc-link-run.sh"], LinkInstaller.Stale(new Dictionary<string, string?>
        {
            ["bragi-roc-link-run.sh"] = "#!/bin/bash\n: ${SAGEPI_TAILSCALE_IP:?missing}\n",
            ["bragi-roc-link.service"] = "[Unit]\nDescription=new\n",
        }, Bundled));

    [Fact]
    public void Both_can_be_stale() =>
        Assert.Equal(["bragi-roc-link-run.sh", "bragi-roc-link.service"], LinkInstaller.Stale(new Dictionary<string, string?>
        {
            ["bragi-roc-link-run.sh"] = "old",
            ["bragi-roc-link.service"] = "old",
        }, Bundled));

    [Fact]
    public void A_missing_file_is_not_stale() =>
        Assert.Empty(LinkInstaller.Stale(new Dictionary<string, string?>
        {
            ["bragi-roc-link-run.sh"] = null,
            ["bragi-roc-link.service"] = "[Unit]\nDescription=new\n",
        }, Bundled));
}
