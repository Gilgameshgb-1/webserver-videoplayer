#include <iostream>
#include <fmt/core.h>
#include <mpv/client.h>

// Only for debugging
void check_mpv_error(int status) {
    if (status < 0) {
        fmt::print("mpv API error: {}\n", mpv_error_string(status));
        exit(1);
    }
}

int main() {
    // engine context
    mpv_handle *ctx = mpv_create();
    if (!ctx) return 1;

    check_mpv_error(mpv_set_option_string(ctx, "force-window", "yes"));
    check_mpv_error(mpv_set_option_string(ctx, "idle", "yes"));
    check_mpv_error(mpv_set_option_string(ctx, "vo", "gpu")); 

    // enable server, this will of course be removed later for linux build
    #ifdef _WIN32
        check_mpv_error(mpv_set_option_string(ctx, "input-ipc-server", R"(\\.\pipe\mpv-pipe)"));
    #else
        check_mpv_error(mpv_set_option_string(ctx, "input-ipc-server", "/tmp/mpv-socket"));
    #endif
    
    check_mpv_error(mpv_initialize(ctx));

    while (true) {
        mpv_event *event = mpv_wait_event(ctx, -1); // -1 wait forever
        
        if (event->event_id == MPV_EVENT_SHUTDOWN) break;
    }

    mpv_terminate_destroy(ctx);
    return 0;
}