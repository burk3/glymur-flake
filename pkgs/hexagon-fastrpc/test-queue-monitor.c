/* Verify method 9's wire ABI and error propagation without DSP hardware. */
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "dspqueue_rpc.h"

static int invoke_result;
static int invoke_count;
static const uint64_t queue_id = UINT64_C(0x123456780000002a);

int remote_handle64_invoke(remote_handle64 handle, uint32_t scalars, remote_arg *args) {
    assert(handle == 1234);
    assert(scalars == REMOTE_SCALARS_MAKEX(0, 9, 1, 1, 0, 0));
    assert(args[0].buf.nLen == 8 && args[1].buf.nLen == 4);
    uint64_t received;
    memcpy(&received, args[0].buf.pv, sizeof(received));
    assert(received == queue_id);
    int32_t processed = 7;
    memcpy(args[1].buf.pv, &processed, sizeof(processed));
    invoke_count++;
    return invoke_result;
}

int main(void) {
    _Static_assert(FASTRPC_MAP_FD_EXTENDED == 17, "SDK map flag ABI");
    _Static_assert(FASTRPC_MAP_FD_DELAYED_EXTENDED == 18, "SDK delayed map flag ABI");
    _Static_assert(FASTRPC_MAP_MAX == 19, "map flags accepted");
    int32_t processed = -1;
    assert(dspqueue_rpc_queue_monitor(1234, queue_id, &processed) == 0);
    assert(processed == 7 && invoke_count == 1);
    invoke_result = 0x80000414;
    processed = -1;
    assert(dspqueue_rpc_queue_monitor(1234, queue_id, &processed) == invoke_result);
    assert(processed == -1 && invoke_count == 2);
    return 0;
}
