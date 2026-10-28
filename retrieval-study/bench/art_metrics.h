#pragma once
#include <cstdint>
struct art_probe_stats {
    uint64_t popped = 0, internal_nodes = 0, leaves = 0, pushes = 0, peak_queue = 0;
    uint64_t accepted = 0, rejected = 0, filter_checks = 0, resets = 0, clock_checks = 0;
};
extern thread_local art_probe_stats art_probe_current;

