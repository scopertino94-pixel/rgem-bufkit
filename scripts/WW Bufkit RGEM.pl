#!/usr/local/bin/perl
## WW Bufkit RGEM.pl  -  RGEM (Canadian RDPS) BUFKIT profile downloader
##
## Pulls the pre-built RGEM .buf files from the Hugging Face dataset and drops
## them into the local BUFKIT Data folder. No Python needed on this machine; the
## files are built centrally by "RGEM Update Dataset.bat" and just downloaded
## here with curl, same as the PSU scripts.
##
## BUFKIT reserves the model name "RDPS", so it is registered as RGEM.
## One-time: run "Setup RGEM in BUFKIT.pl" first to add RGEM to the BUFKIT menu.
## Schedule this like the other WW scripts (RDPS runs 00/06/12/18Z).
##
## Set $REPO to wherever the producer publishes.

use strict;
use warnings;

my $REPO = "ORG/rgem-bufkit";
my $BASE = "https://huggingface.co/datasets/$REPO/resolve/main";
my $OUT  = "C:/Program Files (x86)/BUFKIT/data";

my @sites = (
    "3ck","2is","agr","aqq","atlh","atl4","kack","kabe","kacy","kafj","kagc","kalb",
    "kaoo","kaug","kavp","kbaf","kbfm","kbdl","bid","kbdr","kbed","b#q","b#v","b#x",
    "b#w","kbgr","kbmg","kbos","kbvi","kbvy","kbwi","kchh","c09","can","kcef","kcgx",
    "coat","kcho","kcmh","kcon","cty","kcvg","kday","kdca","kdkb","kdpa","dov","kecg",
    "kenw","keri","evr","kewb","kewr","kezf","kfdy","kfmh","kfmy","kgfl","grun","kgon",
    "kgnv","kgyy","khat","khfd","hgr","khpn","khuf","khvn","khya","kiad","ijd","kilg",
    "kind","kisp","kipt","kith","kjax","kjfk","kjvl","liso","klaf","klbe","klck","lm3",
    "klns","klga","klot","kluk","klwm","kmco","kmdt","kmdw","kmht","kmia","mie","kmiv",
    "kmke","kmlb","kmmu","kmob","kmpo","kmsv","kmtn","nhk","koqu","kord","korf",
    "korh","okx","kore","kpbi","kphl","kphf","kpie","kpit","kpne","kpns","kpou","kpvd",
    "kpwm","kpsm","kpym","rutg","krdg","krfd","kric","kroa","krnk","krpj","ksby","ksch",
    "spa","kswf","ksyr","ksfm","kteb","ktpa","kttn","tmsr","tow","kugn","kunv","kvrb",
    "w54","woo","wtby","kwwd","xmr","kpia","kgbg","kcmi","kpnt","kijx","kbmi","kdnv",
    "kpah","ktaz","kstl","kmdh","klwv","kdec","kuin","kspi","koly","kc75","txkf"
);

print "Downloading the latest RGEM (RDPS) BUFKIT profiles from server - please wait\n\n";

my ($ok, $miss) = (0, 0);
foreach my $site (@sites) {
    (my $u = $site) =~ s/#/%23/g;            # '#' is illegal in a URL path
    my $url = "$BASE/rgem_${u}.buf";
    my $dst = "$OUT/rgem_${site}.buf";
    # list-form system() => no shell quoting issues with spaces or '#'
    my $rc = system("curl", "--ssl-no-revoke", "-f", "-s", "-S", "-L", "-o", $dst, $url);
    if ($rc == 0) { print "Successfully downloaded $site\n"; $ok++; }
    else          { print "  (skipped $site - not available this cycle)\n"; $miss++; }
}

print "\nDone.  Downloaded: $ok   Skipped: $miss\n";
