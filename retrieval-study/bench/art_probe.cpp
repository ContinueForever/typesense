// Runs the actual upstream ART/posting implementation with a materialized ID filter.
// This deliberately does not stand in for the HTTP or lazy filter-iterator path.
#include <art.h>
#include <posting.h>
#include <thread_local_vars.h>
#include <json.hpp>
#include <chrono>
#include <iostream>
#include <limits>
#include <set>
#include <string>
#include <vector>
#include "art_metrics.h"

thread_local art_probe_stats art_probe_current;

static uint64_t now_us() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

int main(int argc, char** argv) {
    const size_t n = argc > 1 ? std::stoull(argv[1]) : 10000;
    const size_t repeats = argc > 2 ? std::stoull(argv[2]) : 5;
    const bool stale = argc > 3 && std::string(argv[3]) == "stale-expired";
    const bool expired = stale || (argc > 3 && std::string(argv[3]) == "expired");
    const bool cancelled = argc > 3 && std::string(argv[3]) == "cancelled";
    if (n < 8 || n > 10000000 || !repeats) return 2;
    art_tree tree;
    art_tree_init(&tree);
    for (uint32_t i = 0; i < n; ++i) {
        if (stale) {
            // Deleting the high-scoring sibling leaves conservative upper bounds
            // on internal nodes. All internal nodes then precede every live leaf.
            char key[24];
            for (char suffix: {'a', 'b', 'c'}) {
                snprintf(key, sizeof(key), "918%09u%c", i, suffix);
                art_document doc(3 * i + suffix - 'a', suffix == 'a' ? 1000000000 + i : 1, {1});
                art_insert(&tree, reinterpret_cast<unsigned char*>(key), 14, &doc);
            }
            snprintf(key, sizeof(key), "918%09ua", i);
            auto values = art_delete(&tree, reinterpret_cast<unsigned char*>(key), 14);
            posting_t::destroy_list(values);
            continue;
        }
        char key[20];
        const bool target = i < 8;
        const bool hot = !target || i == 0;
        const uint32_t suffix = target ? (i * 104729u + 3130u) % 100000000u
                                      : 100000000u + ((uint64_t(i - 8) * 104729 + 3130) % 100000000);
        snprintf(key, sizeof(key), "%s%09u", hot ? "918" : "625", suffix);
        art_document doc(i, target ? i + 1 : 1000000000 + i - 8, {1});
        art_insert(&tree, reinterpret_cast<unsigned char*>(key), 13, &doc);
    }
    const std::vector<uint32_t> target_ids{0,1,2,3,4,5,6,7};
    const std::vector<uint32_t> absent_ids{UINT32_MAX};
    const std::vector<uint32_t> no_filter;
    for (size_t round = 0; round < repeats; ++round) {
        for (auto order: {MAX_SCORE, FREQUENCY}) {
            for (size_t k: {1, 4, 16}) {
                for (auto filter: {&target_ids, &absent_ids, &no_filter}) {
                    search_begin_us = now_us() - (expired ? 1000000 : 0);
                    search_stop_us = expired ? 1 : std::numeric_limits<uint64_t>::max();
                    search_cutoff = cancelled;
                    art_probe_current = {};
                    std::vector<art_leaf*> results;
                    std::set<std::string> excluded;
                    auto begin = std::chrono::steady_clock::now();
                    art_fuzzy_search(&tree, reinterpret_cast<const unsigned char*>("918"), 3, 0, 0,
                                     k, order, true, true, "", filter->data(), filter->size(), results, excluded);
                    const auto elapsed = std::chrono::duration<double, std::micro>(
                        std::chrono::steady_clock::now() - begin).count();
                    const auto stats = art_probe_current;
                    std::vector<std::string> tokens;
                    for (auto leaf: results) tokens.emplace_back(reinterpret_cast<char*>(leaf->key), leaf->key_len - 1);
                    nlohmann::json row{{"n", n}, {"live_terms", art_size(&tree)}, {"stale_bounds", stale}, {"round", round}, {"k", k},
                        {"order", order == MAX_SCORE ? "max_score" : "frequency"},
                        {"filter", filter == &target_ids ? "target" : filter == &absent_ids ? "absent" : "none"},
                        {"expired", expired}, {"cancelled", cancelled}, {"cutoff", search_cutoff},
                        {"elapsed_us", elapsed}, {"tokens", tokens},
                        {"popped", stats.popped}, {"internal_nodes", stats.internal_nodes}, {"leaves", stats.leaves},
                        {"pushes", stats.pushes}, {"peak_queue", stats.peak_queue},
                        {"accepted", stats.accepted}, {"rejected", stats.rejected},
                        {"filter_checks", stats.filter_checks}, {"resets", stats.resets}, {"clock_checks", stats.clock_checks}};
                    std::cout << row.dump() << '\n';
                }
            }
        }
    }
    art_tree_destroy(&tree);
}

