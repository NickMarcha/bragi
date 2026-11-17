using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

/// <summary>KDE's "mega-authorization" for Deskflow (Tray/RemoteControlPermission.cs).</summary>
public class RemoteControlTests
{
    [Fact]
    public void Busctls_lookup_reply_says_whether_deskflow_may_control_without_asking()
    {
        // As busctl --json=short prints a Lookup reply (checked on sage-dev with a throwaway table).
        Assert.True(RemoteControlPermission.GrantedIn("""{"type":"a{sas}v","data":[{"org.deskflow.deskflow":["yes"]},{"type":"y","data":0}]}"""));
        Assert.False(RemoteControlPermission.GrantedIn("""{"type":"a{sas}v","data":[{"org.deskflow.deskflow":["no"]},{"type":"y","data":0}]}"""));
        Assert.False(RemoteControlPermission.GrantedIn("""{"type":"a{sas}v","data":[{"org.other.app":["yes"]},{"type":"y","data":0}]}"""));
        Assert.False(RemoteControlPermission.GrantedIn("Call failed: No entry for remote-desktop"));
    }
}
