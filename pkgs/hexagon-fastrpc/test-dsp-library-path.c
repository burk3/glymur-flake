/* Run the actual source's static search routines against real temporary files.
 * This catches paths that work for skels but are ignored/truncated for shells. */
#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <bsd/string.h>
#include <unistd.h>
#include "AEEStdErr.h"
#include "apps_std_internal.h"

extern const char *SUBSYSTEM_NAME[];
#define IS_VALID_DOMAIN_ID(domain) ((domain) >= 0 && (domain) < 8)
#define VERIFYC(condition, error) do { if (!(condition)) { nErr = (error); goto bail; } } while (0)
#define FARF(...) ((void)0)
#include "shell-search.inc"
#include "lib-names.inc"

static void create_file(const char *directory, const char *name) {
    char path[1024];
    assert(snprintf(path, sizeof(path), "%s/%s", directory, name) < sizeof(path));
    FILE *file = fopen(path, "w");
    assert(file != NULL);
    assert(fputs("fixture", file) >= 0);
    assert(fclose(file) == 0);
}

int main(void) {
    char directory[] = "/tmp/fastrpc-long-library-path-regression-XXXXXX";
    assert(mkdtemp(directory) != NULL);
    create_file(directory, "fastrpc_shell_unsigned_3");
    create_file(directory, "libtest.so");

    apps_std_FILE file = -1;
    assert(unsetenv(DSP_LIBRARY_PATH) == 0);
    assert(setenv(ADSP_LIBRARY_PATH, directory, 1) == 0);
    assert(try_open_shell_file(3, "fastrpc_shell_unsigned_3", &file) == 0);
    assert(apps_std_fclose(file) == 0);

    /* DSP_LIBRARY_PATH takes precedence; test multiple semicolon paths. */
    char paths[1024];
    assert(snprintf(paths, sizeof(paths), "/missing;%s", directory) < sizeof(paths));
    assert(setenv(ADSP_LIBRARY_PATH, "/missing", 1) == 0);
    assert(setenv(DSP_LIBRARY_PATH, paths, 1) == 0);
    assert(try_open_shell_file(3, "fastrpc_shell_unsigned_3", &file) == 0);
    assert(apps_std_fclose(file) == 0);

    assert(setenv(DSP_LIBRARY_PATH, directory, 1) == 0);
    char names_buffer[MAX_NON_PRELOAD_LIBS_LEN] = {0};
    char *names = names_buffer;
    size_t length = 0;
    assert(get_non_preload_lib_names(&names, &length, 3) == 0);
    assert(strcmp(names, "libtest.so;") == 0);
    assert(length == strlen(names) + 1);
    assert(strcmp(getenv(DSP_LIBRARY_PATH), directory) == 0);
    return 0;
}
