package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import com.mojang.blaze3d.pipeline.RenderTarget;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.Socket;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;

/**
 * Captures the current frame to a fixed path, overwriting the previous one.
 *
 * <p>This is the "eye" of the agent: the screenshot handler next to it writes a
 * unique timestamped PNG per call, which piles up files and makes the caller
 * guess which one is newest. Here the file name is constant, so whoever reads it
 * always gets the latest frame without listing a directory.
 *
 * <p>{@code downscale} maps to vanilla's own integer downscale factor: 1 keeps
 * the framebuffer size, 2 halves it, 3 thirds it. Smaller frames cost the reader
 * far fewer tokens, which matters more than the extra detail.
 *
 * <p>Deliberately returns a path rather than base64: the reader has to decode
 * the bytes into an image either way, and shipping base64 through JSON only
 * inflates it by a third on the wire and forces an extra hop to disk.
 */
public class CaptureFrameCommandHandler extends AsyncCommandHandler {

    private static final Logger LOGGER = LoggerFactory.getLogger(CaptureFrameCommandHandler.class);
    private static final String FRAME_DIR = "screenshots";
    /** Constant on purpose - every capture replaces the previous frame. */
    private static final String FRAME_BASENAME = "latest_frame";
    private static final int MIN_DOWNSCALE = 1;
    private static final int MAX_DOWNSCALE = 8;

    @Override
    public String getCommandName() {
        return "capture_frame";
    }

    @Override
    public CompletableFuture<CommandResult> execute(
            JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
        int requested = params.has("downscale") ? params.get("downscale").getAsInt() : 1;
        if (requested < MIN_DOWNSCALE || requested > MAX_DOWNSCALE) {
            return CompletableFuture.completedFuture(CommandResult.error(
                "downscale must be between " + MIN_DOWNSCALE + " and " + MAX_DOWNSCALE));
        }
        final int downscale = requested;

        CompletableFuture<CommandResult> future = new CompletableFuture<>();

        executeOnMainThread(client, () -> {
            try {
                if (client.player == null || client.level == null) {
                    future.complete(CommandResult.error("Player or world not available"));
                    return;
                }
                RenderTarget framebuffer = client.gameRenderer.mainRenderTarget();
                if (framebuffer == null) {
                    future.complete(CommandResult.error("No framebuffer available"));
                    return;
                }

                // Vanilla writes the frame at <gameDir>/<dir>/<name> with NO extension: the
                // name argument is the whole file name, not a basename it appends to. Rename it
                // to .png so image readers accept it by the extension they expect.
                Path raw = client.gameDirectory.toPath()
                    .resolve(FRAME_DIR)
                    .resolve(FRAME_BASENAME);
                Path expected = raw.resolveSibling(FRAME_BASENAME + ".png");

                Screenshot.grab(client.gameDirectory, FRAME_BASENAME, framebuffer, downscale, msg -> {
                    try {
                        if (Files.exists(raw)) {
                            Files.deleteIfExists(expected);
                            Files.move(raw, expected);
                        }
                        JsonObject data = new JsonObject();
                        boolean exists = Files.exists(expected);
                        data.addProperty("saved", exists);
                        data.addProperty("path", expected.toString());
                        data.addProperty("file_path", expected.toString());
                        data.addProperty("file_name", FRAME_BASENAME + ".png");
                        data.addProperty("format", "PNG");
                        data.addProperty("downscale", downscale);
                        data.addProperty("width", framebuffer.width / downscale);
                        data.addProperty("height", framebuffer.height / downscale);
                        if (exists) {
                            data.addProperty("bytes", Files.size(expected));
                        } else {
                            String detail = msg != null ? msg.getString() : "unknown";
                            data.addProperty("error", "Frame file was not created: " + detail);
                            LOGGER.error("capture_frame failed: {}", detail);
                        }
                        future.complete(CommandResult.success(data));
                    } catch (Exception e) {
                        future.complete(CommandResult.error("Frame completion failed: " + e.getMessage()));
                    }
                });
            } catch (Exception e) {
                LOGGER.error("capture_frame failed", e);
                future.complete(CommandResult.error("Frame capture failed: " + e.getMessage()));
            }
        });

        return future.completeOnTimeout(
            CommandResult.error("Frame capture timed out waiting for file write"),
            10, TimeUnit.SECONDS);
    }
}
