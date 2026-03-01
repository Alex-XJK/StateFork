#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cerrno>
#include <cstring>
#include <limits>
#include <vector>
#include <unistd.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <time.h>

// Simple filesystem workload: create N MB of data as files in CWD.
// Usage: fsgen <total_mb>
// Strategy: write files up to 128 MiB each to better emulate typical
// chunked storage patterns and avoid single very large files.

static bool write_filled_file(const char* path, unsigned long long bytes) {
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0644);
    if (fd < 0) {
        return false;
    }

    const std::size_t bufSize = 1u << 20; // 1 MiB buffer
    std::vector<std::uint8_t> buf(bufSize, 0xAB);

    unsigned long long remaining = bytes;
    while (remaining > 0ull) {
        std::size_t chunk = static_cast<std::size_t>(remaining < bufSize ? remaining : bufSize);
        std::size_t off = 0;
        while (off < chunk) {
            ssize_t w = write(fd, buf.data() + off, chunk - off);
            if (w < 0) {
                if (errno == EINTR) continue;
                close(fd);
                return false;
            }
            off += static_cast<std::size_t>(w);
        }
        remaining -= static_cast<unsigned long long>(chunk);
    }

    // Ensure data is persisted for accurate profiling if needed
    (void)fsync(fd);
    close(fd);
    return true;
}

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::fprintf(stderr, "Usage: %s <total_mb>\n", argv[0]);
        return 1;
    }

    errno = 0;
    char* endp = nullptr;
    unsigned long long mb = std::strtoull(argv[1], &endp, 10);
    if (errno != 0 || endp == argv[1] || mb == 0ull) {
        std::fprintf(stderr, "fsgen: invalid total_mb '%s'\n", argv[1]);
        return 1;
    }

    const unsigned long long MiB = 1024ull * 1024ull;
    if (mb > (std::numeric_limits<unsigned long long>::max() / MiB)) {
        std::fprintf(stderr, "fsgen: size too large\n");
        return 1;
    }
    const unsigned long long totalBytes = mb * MiB;

    // Split into 128 MiB chunks for more realistic IO profiling
    const unsigned long long maxPerFile = 128ull * MiB;
    unsigned long long full = totalBytes / maxPerFile;
    unsigned long long rem = totalBytes % maxPerFile;
    unsigned long long files = full + (rem ? 1ull : 0ull);

    // Build a unique prefix using PID and realtime nanoseconds
    pid_t pid = getpid();
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    unsigned long long tsn = static_cast<unsigned long long>(ts.tv_sec) * 1000000000ull + static_cast<unsigned long long>(ts.tv_nsec);

    char prefix[128];
    std::snprintf(prefix, sizeof(prefix), "fsgen_%ld_%llu", static_cast<long>(pid), tsn);

    for (unsigned long long i = 0; i < files; ++i) {
        unsigned long long sz = (i < full) ? maxPerFile : (rem ? rem : maxPerFile);
        if (sz == 0ull) continue;

        for (unsigned attempt = 0; attempt < 16; ++attempt) {
            char name[256];
            if (attempt == 0) {
                std::snprintf(name, sizeof(name), "%s_%03llu.bin", prefix, i);
            } else {
                std::snprintf(name, sizeof(name), "%s_%03llu_%u.bin", prefix, i, attempt);
            }
            if (write_filled_file(name, sz)) {
                std::printf("%s\n", name);
                break;
            } else if (errno == EEXIST) {
                continue; // retry with another suffix
            } else {
                std::fprintf(stderr, "fsgen: failed to create '%s': %s\n", name, std::strerror(errno));
                return 2;
            }
        }
    }

    return 0;
}
