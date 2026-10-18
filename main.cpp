#include <cstdlib>
#include <iostream>
#include <fmt/core.h>
#include <mpv/client.h>

void check_mpv_error(int status) {
    if (status < 0) {
        fmt::print("mpv API error: {}\n", mpv_error_string(status));
        exit(1);
    }
}

// Non-fatal option setter so optional or version-specific mpv tuning never
// aborts player startup.
void set_mpv_opt(mpv_handle *ctx, const char *name, const char *val) {
    int status = mpv_set_option_string(ctx, name, val);
    if (status < 0) {
        fmt::print("mpv warning: option {}={} failed ({})\n",
                   name, val, mpv_error_string(status));
    }
}

int main() {
    mpv_handle *ctx = mpv_create();
    if (!ctx) return 1;

    check_mpv_error(mpv_set_option_string(ctx, "force-window", "yes"));
    check_mpv_error(mpv_set_option_string(ctx, "idle", "yes"));
    set_mpv_opt(ctx, "keep-open", "yes");

    #ifdef _WIN32
        check_mpv_error(mpv_set_option_string(ctx, "vo", "gpu"));
        check_mpv_error(mpv_set_option_string(ctx, "input-ipc-server", R"(\\.\pipe\mpv-pipe)"));
    #else
        // libmpv disables config file loading by default (--config=no).
        // Enable it explicitly so /etc/mpv/mpv.conf is respected.
        set_mpv_opt(ctx, "config-dir", "/etc/mpv");
        set_mpv_opt(ctx, "config", "yes");

        // Render with the GPU straight onto the HDMI output (DRM/KMS), and
        // decode HEVC on the Pi 5's hardware decoder. With "vo=drm" every
        // frame was decoded and scaled on the CPU and 4K stuttered.
        //
        // "drm-copy" copies each decoded frame back to RAM before the GPU
        // scales it: about one core at 4K, no dropped frames. The zero-copy
        // "drm" mode does not work for 10-bit video on this stack: the
        // overlay plane shows nothing and the EGL import shows a blue screen.
        //
        // Codecs without a hardware decoder (H.264 on the Pi 5) fall back to
        // software decoding automatically and still get GPU scaling.
        const char *ipc_path = std::getenv("MPV_IPC_PATH");
        if (!ipc_path || !*ipc_path) {
            ipc_path = "/tmp/mpv-socket";
        }

        check_mpv_error(mpv_set_option_string(ctx, "vo", "gpu"));
        check_mpv_error(mpv_set_option_string(ctx, "gpu-context", "drm"));
        check_mpv_error(mpv_set_option_string(ctx, "hwdec", "drm-copy"));
        check_mpv_error(mpv_set_option_string(ctx, "input-ipc-server", ipc_path));

        // 4K UHD Blu-ray Remux tuning (60-128 Mbps peak HEVC Main 10 + lossless audio):
        // 1. Demuxer cache: absorb NVMe I/O bursts without exhausting RAM alongside 512MB CMA.
        set_mpv_opt(ctx, "cache", "yes");
        set_mpv_opt(ctx, "demuxer-max-bytes", "128MiB");
        set_mpv_opt(ctx, "demuxer-max-back-bytes", "32MiB");
        set_mpv_opt(ctx, "demuxer-readahead-secs", "8");

        // 2. HDR10 / Dolby Vision tone-mapping on VideoCore VII (v3d):
        //    Disable dynamic compute-shader peak detection (which stalls v3d on 4K frames)
        //    and use lightweight BT.2390 tone-mapping + bilinear scaling.
        set_mpv_opt(ctx, "hdr-compute-peak", "no");
        set_mpv_opt(ctx, "tone-mapping", "bt.2390");
        set_mpv_opt(ctx, "gamut-mapping-mode", "clip");
        set_mpv_opt(ctx, "scale", "bilinear");
        set_mpv_opt(ctx, "dscale", "bilinear");
        set_mpv_opt(ctx, "cscale", "bilinear");

        // 3. Cinema audio (TrueHD 7.1 Atmos / DTS-HD MA 7.1):
        //    Default to safe downmix/channel negotiation on HDMI ALSA so 7.1 tracks
        //    never fail audio init or drop the center dialogue channel on stereo TVs.
        set_mpv_opt(ctx, "audio-channels", "auto-safe");
        set_mpv_opt(ctx, "ad-lavc-downmix", "yes");
        set_mpv_opt(ctx, "volume-max", "150");

        // 4. Subtitle rendering & Croatian Central European codepage + language priority:
        set_mpv_opt(ctx, "sub-font", "/usr/share/fonts/truetype/DejaVuSans.ttf");
        set_mpv_opt(ctx, "osd-font", "/usr/share/fonts/truetype/DejaVuSans.ttf");
        set_mpv_opt(ctx, "sub-codepage", "utf-8:cp1250");
        set_mpv_opt(ctx, "sub-auto", "fuzzy");
        set_mpv_opt(ctx, "alang", "eng,en,hrv,hr");
        set_mpv_opt(ctx, "slang", "hrv,hr,eng,en");
    #endif

    check_mpv_error(mpv_initialize(ctx));

    while (true) {
        mpv_event *event = mpv_wait_event(ctx, 0.05);
        if (event->event_id == MPV_EVENT_SHUTDOWN) break;
    }

    mpv_terminate_destroy(ctx);
    return 0;
}

