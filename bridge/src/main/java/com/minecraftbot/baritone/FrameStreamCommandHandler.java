package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import com.mojang.blaze3d.pipeline.RenderTarget;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.Socket;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;

/**
 * Keeps <gameDir>/screenshots/latest_frame.png refreshed on a timer.
 *
 * <p>Why this exists: {@link CaptureFrameCommandHandler} only captures when the
 * agent asks, and the agent is turn-based - it wakes, looks, thinks for seconds,
 * and disappears. So a captured frame is stale by the time the next decision
 * starts. This handler runs the capture on a timer inside the client instead, so
 * the file is never older than one interval and the agent can just read it.
 *
 * <p>Cost: a capture is 70-124ms of main-thread work (PNG encode + write). At a
 * 200ms interval that is a noticeable share of the 50ms frame budget, so the
 * default downscale is 4 (about 213x120) - enough to recognise terrain and
 * structures, cheap enough to sustain. Raise it only for a one-off close look.
 *
 * <p>The timer deliberately lives here rather than in the plugin: the framebuffer
 * only exists inside the client, and the plugin has no way to reach it.
 */
public class FrameStreamCommandHandler extends AbstractCommandHandler {

    private static final Logger LOGGER = LoggerFactory.getLogger(FrameStreamCommandHandler.class);
    private static final String FRAME_DIR = "screenshots";
    private static final String FRAME_BASENAME = "latest_frame";
    private static final int DEFAULT_INTERVAL_MS = 1000;
    private static final int MIN_INTERVAL_MS = 100;
    private static final int DEFAULT_DOWNSCALE = 2;

    /** One capture at a time across the whole client; a second start replaces the first. */
    private static ScheduledExecutorService executor;
    private static ScheduledFuture<?> task;
    private static final AtomicLong CAPTURES = new AtomicLong();
    private static final AtomicLong FAILURES = new AtomicLong();
    private static final AtomicLong SKIPPED = new AtomicLong();
    /**
     * A capture is not instant (PNG encode + write on the IO pool). At a 200ms tick a new
     * capture is requested while the previous one is still in flight, and vanilla's single
     * screenshot worker silently drops the overlap - which is exactly what happened on
     * 2026-10-21: 95 "failures", zero files, and not a single line in the log.
     * Skip the tick instead of piling up.
     */
    private static final java.util.concurrent.atomic.AtomicBoolean IN_FLIGHT =
        new java.util.concurrent.atomic.AtomicBoolean(false);
    /** When the in-flight capture started, so a never-returning callback cannot wedge the stream. */
    private static final AtomicLong IN_FLIGHT_SINCE = new AtomicLong(0);
    private static final long IN_FLIGHT_TIMEOUT_MS = 3000;
    private static volatile int activeDownscale = DEFAULT_DOWNSCALE;

    @Override
    public String getCommandName() {
        return "frame_stream";
    }

    @Override
    public CommandResult execute(
            JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
        String action = params.has("action")
            ? params.get("action").getAsString().toLowerCase()
            : "status";

        switch (action) {
            case "start" -> {
                int interval = params.has("interval_ms")
                    ? params.get("interval_ms").getAsInt()
                    : DEFAULT_INTERVAL_MS;
                if (interval < MIN_INTERVAL_MS || interval > 10_000) {
                    return CommandResult.error(
                        "interval_ms must be between " + MIN_INTERVAL_MS + " and 10000");
                }
                int downscale = params.has("downscale")
                    ? params.get("downscale").getAsInt()
                    : DEFAULT_DOWNSCALE;
                if (downscale < 1 || downscale > 8) {
                    return CommandResult.error("downscale must be between 1 and 8");
                }
                start(client, interval, downscale);
                return CommandResult.success(describe(true, interval, downscale));
            }
            case "stop" -> {
                stop();
                return CommandResult.success(describe(false, 0, activeDownscale));
            }
            case "status" -> {
                return CommandResult.success(describe(isRunning(), 0, activeDownscale));
            }
            default -> {
                return CommandResult.error("action must be start, stop or status");
            }
        }
    }

    private static synchronized boolean isRunning() {
        return task != null && !task.isDone();
    }

    private static synchronized void start(Minecraft client, int intervalMs, int downscale) {
        stop();
        activeDownscale = downscale;
        CAPTURES.set(0);
        FAILURES.set(0);
        executor = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "mc-frame-stream");
            thread.setDaemon(true);
            return thread;
        });
        task = executor.scheduleAtFixedRate(
            () -> captureOnce(client, downscale), 0, intervalMs, TimeUnit.MILLISECONDS);
        LOGGER.info("frame_stream started: every {}ms at downscale {}", intervalMs, downscale);
    }

    private static synchronized void stop() {
        if (task != null) {
            task.cancel(false);
            task = null;
        }
        if (executor != null) {
            executor.shutdownNow();
            executor = null;
        }
    }

    /**
     * One capture. Screenshot.grab must run on the client thread, but the file write
     * happens on vanilla's IO worker, so this only submits and returns.
     */
    private static void captureOnce(Minecraft client, int downscale) {
        long now = System.currentTimeMillis();
        // A callback that never fires would otherwise wedge the stream forever.
        if (IN_FLIGHT.get() && now - IN_FLIGHT_SINCE.get() > IN_FLIGHT_TIMEOUT_MS) {
            LOGGER.warn("frame_stream: previous capture stuck for {}ms, resetting",
                now - IN_FLIGHT_SINCE.get());
            IN_FLIGHT.set(false);
        }
        if (!IN_FLIGHT.compareAndSet(false, true)) {
            SKIPPED.incrementAndGet();
            return;
        }
        IN_FLIGHT_SINCE.set(now);
        try {
            client.execute(() -> {
                try {
                    if (client.player == null || client.level == null) {
                        IN_FLIGHT.set(false);
                        return;
                    }
                    RenderTarget framebuffer = client.gameRenderer.mainRenderTarget();
                    if (framebuffer == null) {
                        IN_FLIGHT.set(false);
                        return;
                    }
                    // Vanilla rejects the capture outright when the framebuffer size is not
                    // divisible by the factor ("Image size is not divisible by downscale
                    // factor"), and a 854x480 window only divides by 1 and 2. Clamp down to the
                    // largest factor that actually divides instead of failing every tick.
                    int effective = downscale;
                    while (effective > 1
                        && (framebuffer.width % effective != 0
                            || framebuffer.height % effective != 0)) {
                        effective--;
                    }
                    Path dir = client.gameDirectory.toPath().resolve(FRAME_DIR);
                    Path raw = dir.resolve(FRAME_BASENAME);
                    Path png = dir.resolve(FRAME_BASENAME + ".png");
                    Screenshot.grab(client.gameDirectory, FRAME_BASENAME, framebuffer, effective, msg -> {
                        try {
                            if (Files.exists(raw)) {
                                Files.deleteIfExists(png);
                                Files.move(raw, png);
                                CAPTURES.incrementAndGet();
                            } else {
                                FAILURES.incrementAndGet();
                                if (FAILURES.get() <= 3) {
                                    LOGGER.warn("frame_stream capture produced no file (msg={})", msg);
                                }
                            }
                        } catch (Exception e) {
                            FAILURES.incrementAndGet();
                            if (FAILURES.get() <= 3) {
                                LOGGER.warn("frame_stream could not move the frame: {}", e.getMessage());
                            }
                        } finally {
                            IN_FLIGHT.set(false);
                        }
                    });
                } catch (Exception e) {
                    FAILURES.incrementAndGet();
                    IN_FLIGHT.set(false);
                    if (FAILURES.get() <= 3) {
                        LOGGER.warn("frame_stream capture failed: {}", e.getMessage());
                    }
                }
            });
        } catch (Exception e) {
            FAILURES.incrementAndGet();
            IN_FLIGHT.set(false);
            if (FAILURES.get() <= 3) {
                LOGGER.warn("frame_stream could not submit capture: {}", e.getMessage());
            }
        }
    }

    private static JsonObject describe(boolean running, int intervalMs, int downscale) {
        JsonObject data = new JsonObject();
        data.addProperty("running", running);
        data.addProperty("interval_ms", running ? intervalMs : 0);
        data.addProperty("downscale", downscale);
        data.addProperty("captures", CAPTURES.get());
        data.addProperty("failures", FAILURES.get());
        data.addProperty("skipped", SKIPPED.get());
        data.addProperty("path", FRAME_BASENAME + ".png");
        if (running) {
            data.addProperty(
                "note",
                "latest_frame.png is refreshed on this interval; read it whenever you need to look");
        }
        return data;
    }
}
