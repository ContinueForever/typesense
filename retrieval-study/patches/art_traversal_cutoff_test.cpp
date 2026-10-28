#include <gtest/gtest.h>
#include <art.h>
#include <posting.h>
#include <thread_local_vars.h>
#include <chrono>
#include <cstdio>
#include <limits>

class ArtTraversalCutoffTest : public ::testing::Test {
protected:
    art_tree tree{};
    const uint64_t saved_begin = search_begin_us;
    const uint64_t saved_stop = search_stop_us;
    const bool saved_cutoff = search_cutoff;

    static uint64_t now_us() {
        return std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count();
    }

    void SetUp() override {
        art_tree_init(&tree);
        // Delete the highest-scoring sibling, leaving conservative internal
        // bounds above all live leaf scores. More than 1024 internal nodes must
        // be expanded before a live candidate can be accepted.
        for (uint32_t i = 0; i < 2048; ++i) {
            char key[24];
            for (char suffix: {'a', 'b', 'c'}) {
                snprintf(key, sizeof(key), "918%09u%c", i, suffix);
                art_document doc(3 * i + suffix - 'a', suffix == 'a' ? 1000000000 + i : 1, {1});
                art_insert(&tree, reinterpret_cast<unsigned char*>(key), 14, &doc);
            }
            snprintf(key, sizeof(key), "918%09ua", i);
            auto values = art_delete(&tree, reinterpret_cast<unsigned char*>(key), 14);
            posting_t::destroy_list(values);
        }
        search_begin_us = now_us() - 1000000;
        search_stop_us = 1;
        search_cutoff = false;
    }

    void TearDown() override {
        art_tree_destroy(&tree);
        search_begin_us = saved_begin;
        search_stop_us = saved_stop;
        search_cutoff = saved_cutoff;
    }

    void search_array(std::vector<art_leaf*>& results) {
        std::set<std::string> excluded;
        art_fuzzy_search(&tree, reinterpret_cast<const unsigned char*>("918"), 3, 0, 0,
                         1, MAX_SCORE, true, true, "", nullptr, 0, results, excluded);
    }
};

TEST_F(ArtTraversalCutoffTest, ChecksDeadlineWhileExpandingInternalNodes) {
    std::vector<art_leaf*> results;
    search_array(results);
    EXPECT_TRUE(search_cutoff);
    EXPECT_TRUE(results.empty());
}

TEST_F(ArtTraversalCutoffTest, SubsequentQueryWithNewBudgetStillReturnsCandidates) {
    std::vector<art_leaf*> results;
    search_array(results);
    ASSERT_TRUE(search_cutoff);

    search_begin_us = now_us();
    search_stop_us = std::numeric_limits<uint64_t>::max();
    search_cutoff = false;
    results.clear();
    search_array(results);
    EXPECT_FALSE(search_cutoff);
    ASSERT_EQ(1, results.size());
}

#ifndef ART_PROBE_ARRAY_ONLY
TEST_F(ArtTraversalCutoffTest, IteratorEntryPointChecksInternalNodeDeadline) {
    filter_result_iterator_t filter;
    filter.validity = filter_result_iterator_t::invalid;  // No filter provided.
    std::set<std::string> excluded;
    std::vector<art_leaf*> results;
    art_fuzzy_search_i(&tree, reinterpret_cast<const unsigned char*>("918"), 3, 0, 0,
                       1, MAX_SCORE, true, true, "", &filter, results, excluded);
    EXPECT_TRUE(search_cutoff);
    EXPECT_TRUE(results.empty());
}
#endif
