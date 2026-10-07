#!/usr/local/bin/perl
## Setup RGEM in BUFKIT.pl  -  run ONCE per machine.
##
## Adds a single "RGEM          eta" line to the BUFKIT model list in
## Bufkit.cfg so the RGEM (Canadian RDPS) profiles show up in the model menu.
## Safe to run more than once: if RGEM is already present it does nothing.
## It only ever inserts one line after the GFS3 line and never touches the
## profile directory settings.
##
## IMPORTANT: the model is registered as "RGEM", NOT "RDPS".  BUFKIT v19 RESERVES
## the name "RDPS" (built-in Canadian-model handling) and throws run-time error 13
## ("Type mismatch") if you try to load generic .buf files under it.  "RGEM"
## (Regional GEM) is an ordinary name BUFKIT accepts.

use strict;
use warnings;

my $cfg = "C:/Users/Public/Bufkit/Bufkit.cfg";
die "Cannot find Bufkit.cfg at $cfg\n" unless -f $cfg;

open my $in, "<", $cfg or die "Cannot read $cfg: $!\n";
binmode $in;
my @lines = <$in>;
close $in;

if (grep { /^RGEM\s/i } @lines) {
    print "RGEM is already in the BUFKIT model list - nothing to do.\n";
    exit 0;
}

my @out;
my $inserted = 0;
foreach my $l (@lines) {
    push @out, $l;
    if (!$inserted && $l =~ /^GFS3\s/i) {
        push @out, "RGEM          eta\r\n";
        $inserted = 1;
    }
}

die "Could not find the GFS3 model line - Bufkit.cfg left unchanged.\n"
    unless $inserted;

open my $w, ">", $cfg or die "Cannot write $cfg: $!\n";
binmode $w;
print $w @out;
close $w;

print "RGEM added to the BUFKIT model list.\n";
print "Close and reopen BUFKIT, then run 'WW Bufkit RGEM.pl' to fetch profiles.\n";
