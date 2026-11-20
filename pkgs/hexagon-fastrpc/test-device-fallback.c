/* Test the actual library's node selection without opening a real DSP. */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

extern int open_device_node(int domain_id);

static int secure_errno;
static int secure_opens;
static int nonsecure_opens;

/* ELF interposition supplies the two device outcomes seen by libcdsprpc. */
int open(const char *path, int flags, ...) {
    if (strcmp(path, "/dev/fastrpc-cdsp-secure") == 0) {
        secure_opens++;
        errno = secure_errno;
        return -1;
    }
    if (strcmp(path, "/dev/fastrpc-cdsp") == 0) {
        nonsecure_opens++;
        return 1234;
    }
    /* The library constructor also probes platform configuration. Leave
     * unrelated filesystem calls alone; no DSP exists in the build sandbox. */
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = va_arg(args, mode_t);
        va_end(args);
    }
    return syscall(SYS_openat, AT_FDCWD, path, flags, mode);
}

int main(void) {
    const int permission_errors[] = { EACCES, ENOENT, EPERM };
    for (unsigned i = 0; i < sizeof(permission_errors) / sizeof(int); ++i) {
        secure_errno = permission_errors[i];
        secure_opens = nonsecure_opens = 0;
        assert(open_device_node(3) == 1234);
        assert(secure_opens == 1 && nonsecure_opens == 1);
    }
    /* Do not hide actual remote-processor failures behind a second open. */
    secure_errno = ENODEV;
    secure_opens = nonsecure_opens = 0;
    assert(open_device_node(3) == -1);
    assert(secure_opens == 1 && nonsecure_opens == 0);
    return 0;
}
