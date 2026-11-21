using System;
using Bragi.Client.Tray;
using Xunit;

namespace Bragi.Client.Tests;

public class UpdateScheduleTests
{
    [Fact]
    public void Due_in_the_nightly_hour_when_not_done_yet() =>
        Assert.True(UpdateService.NightlyDue(new DateTime(2026, 11, 22, 4, 10, 0), null));

    [Fact]
    public void Not_due_outside_the_nightly_hour() =>
        Assert.False(UpdateService.NightlyDue(new DateTime(2026, 11, 22, 14, 10, 0), null));

    [Fact]
    public void Once_a_night() =>
        Assert.False(UpdateService.NightlyDue(new DateTime(2026, 11, 22, 4, 40, 0), new DateTime(2026, 11, 22, 4, 10, 0)));

    [Fact]
    public void Due_again_the_next_night() =>
        Assert.True(UpdateService.NightlyDue(new DateTime(2026, 11, 23, 4, 0, 0), new DateTime(2026, 11, 22, 4, 10, 0)));
}
