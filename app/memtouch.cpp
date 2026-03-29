#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cerrno>
#include <cstring>
#include <limits>
#include <vector>
#include <charconv>
#include <optional>
#include <thread>
#include <chrono>
#include <algorithm>

// Simple page toucher: allocate X MB, then every interval touch ~Y pages.
// Usage: memtouch <alloc_mb> <pages_per_interval> [interval_ms]
// Constraints: no external deps, suitable for restricted containers.

static volatile std::uint64_t sink_acc = 0; // avoid over-optimization

namespace {
constexpr std::size_t kPageSize = 4096u;      // assume 4 KiB pages (simple and stable)
constexpr unsigned kDefaultIntervalMs = 100u; // default period

std::optional<unsigned long long> parse_ull(const char* s) {
    unsigned long long v = 0;
    auto begin = s;
    auto end = s + std::strlen(s);
    auto [ptr, ec] = std::from_chars(begin, end, v, 10);
    if (ec != std::errc() || ptr == begin) return std::nullopt;
    return v;
}
} // namespace

int main(int argc, char* argv[]) {
    if (argc < 3 || argc > 4) {
        std::fprintf(stderr, "Usage: %s <alloc_mb> <pages_per_interval> [interval_ms]\n", argv[0]);
        return 1;
    }

    // Parse alloc_mb
    auto alloc_mb_opt = parse_ull(argv[1]);
    if (!alloc_mb_opt || *alloc_mb_opt == 0ull) {
        std::fprintf(stderr, "memtouch: invalid alloc_mb '%s'\n", argv[1]);
        return 1;
    }
    unsigned long long alloc_mb = *alloc_mb_opt;

    // Parse pages_per_interval
    auto pages_per_opt = parse_ull(argv[2]);
    if (!pages_per_opt || *pages_per_opt == 0ull) {
        std::fprintf(stderr, "memtouch: invalid pages_per_interval '%s'\n", argv[2]);
        return 1;
    }
    unsigned long long pages_per_in = *pages_per_opt;

    // Parse optional interval_ms
    unsigned interval_ms = kDefaultIntervalMs;
    if (argc == 4) {
        auto ms_opt = parse_ull(argv[3]);
        if (!ms_opt || *ms_opt > 600000ull) {
            std::fprintf(stderr, "memtouch: invalid interval_ms '%s'\n", argv[3]);
            return 1;
        }
        interval_ms = static_cast<unsigned>(*ms_opt);
    }

    // Compute allocation size with overflow guard
    const unsigned long long mb_to_bytes = 1024ull * 1024ull;
    if (alloc_mb > static_cast<unsigned long long>(std::numeric_limits<std::size_t>::max()) / mb_to_bytes) {
        std::fprintf(stderr, "memtouch: requested size too large for this build\n");
        return 1;
    }
    const std::size_t alloc_bytes = static_cast<std::size_t>(alloc_mb * mb_to_bytes);
    const std::size_t total_pages = alloc_bytes / kPageSize;
    if (total_pages == 0u) {
        std::fprintf(stderr, "memtouch: allocation must cover at least one page\n");
        return 1;
    }

    const std::size_t pages_per = static_cast<std::size_t>(std::min<unsigned long long>(pages_per_in, total_pages));
    if (pages_per < pages_per_in) {
        std::fprintf(stderr, "memtouch: pages_per_interval (%llu) > total_pages (%zu); clamping to total_pages.\n",
                     pages_per_in, total_pages);
    }

    // Allocate memory
    std::vector<std::uint8_t> buf;
    try {
        buf.resize(alloc_bytes);
    } catch (...) {
        std::fprintf(stderr, "memtouch: allocation of %zu bytes failed\n", alloc_bytes);
        return 1;
    }

    // Prefault/touch every page to establish RSS
    for (std::size_t off = 0; off < alloc_bytes; off += kPageSize) {
        buf[off] = static_cast<std::uint8_t>((off / kPageSize) & 0xFFu);
    }
    buf[alloc_bytes - 1] ^= 0xA5u;

    // Periodically dirty the first N pages (deterministic and simple)
    std::uint8_t toggle = 1u;
    const std::size_t pages_per_sz = pages_per;

    std::fprintf(stderr, "memtouch: ready: alloc=%llu MB (%zu bytes), total_pages=%zu, touching %zu pages every %u ms (first-N)\n",
                  alloc_mb, alloc_bytes, total_pages, pages_per_sz, interval_ms);

    for (;;) {
        // Touch/dirty the first-N pages
        for (std::size_t p = 0; p < pages_per_sz; ++p) {
            std::size_t off = p * kPageSize;
            buf[off] ^= toggle;  // write to dirty the page
            sink_acc += buf[off];
        }
        toggle ^= 0x1u; // vary pattern to avoid writing identical values repeatedly
        std::this_thread::sleep_for(std::chrono::milliseconds(interval_ms));
    }

    // unreachable
    // return 0;
}
