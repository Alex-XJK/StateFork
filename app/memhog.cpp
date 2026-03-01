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

// A minimal long-running memory holder for Linux.
// Usage: memhog <mem_mb> <pid_file>

static volatile std::uint64_t sink = 0; // prevent optimization

static bool write_pid_once(const char* path) {
    pid_t pid = getpid();
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        std::fprintf(stderr, "memhog: open pid file failed: %s\n", std::strerror(errno));
        return false;
    }
    char buf[32];
    int len = std::snprintf(buf, sizeof(buf), "%ld\n", static_cast<long>(pid));
    if (len < 0) {
        close(fd);
        return false;
    }
    ssize_t written = 0;
    while (written < len) {
        ssize_t w = write(fd, buf + written, static_cast<size_t>(len - written));
        if (w < 0) {
            if (errno == EINTR) continue;
            close(fd);
            return false;
        }
        written += w;
    }
    (void)fsync(fd);
    close(fd);
    return true;
}

int main(int argc, char* argv[]) {
    if (argc != 3) {
        std::fprintf(stderr, "Usage: %s <mem_mb> <pid_file>\n", argv[0]);
        return 1;
    }

    errno = 0;
    char* endp = nullptr;
    unsigned long long mb = std::strtoull(argv[1], &endp, 10);
    if (errno != 0 || endp == argv[1] || mb == 0ull) {
        std::fprintf(stderr, "memhog: invalid mem_mb '%s'\n", argv[1]);
        return 1;
    }

    unsigned long long bytes64 = mb * 1024ull * 1024ull;
    const unsigned long long maxSizeT = static_cast<unsigned long long>(std::numeric_limits<std::size_t>::max());
    if (bytes64 == 0ull || bytes64 > maxSizeT) {
        std::fprintf(stderr, "memhog: requested size too large for this build\n");
        return 1;
    }
    const std::size_t bytes = static_cast<std::size_t>(bytes64);

    // Allocate and initialize memory
    std::vector<std::uint8_t> buf;
    try {
        buf.resize(bytes);
    } catch (...) {
        std::fprintf(stderr, "memhog: allocation of %zu bytes failed\n", bytes);
        return 1;
    }

    // Touch memory to ensure it is physically backed (RSS)
    const std::size_t stride = 4096u;
    for (std::size_t i = 0; i < bytes; i += stride) {
        buf[i] = static_cast<std::uint8_t>((i / stride) & 0xFFu);
    }
    buf[bytes - 1] ^= 0x5Au;

    // Report PID once after memory is ready
    (void)write_pid_once(argv[2]);

    // Long-running loop: iterate buffer to keep memory active
    for (;;) {
        std::uint64_t acc = 0;
        for (std::size_t i = 0; i < bytes; i += stride) {
            acc += buf[i];
        }
        const std::size_t writeStride = stride * 16u;
        for (std::size_t i = 0; i < bytes; i += writeStride) {
            buf[i] ^= 0x1u;
        }
        sink = acc; // observable side-effect to avoid optimization

        usleep(100000); // 100ms
    }

    // Unreachable
    // return 0;
}
