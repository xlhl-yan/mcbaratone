package com.minecraftbot.baritone;

import com.google.gson.JsonObject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.net.Socket;
import java.net.SocketTimeoutException;

/**
 * ConnectionHandler manages per-client connection lifecycle.
 * Responsible for:
 * - Reading JSON requests from client socket
 * - Writing JSON responses back to client
 * - Error handling per connection
 * - Connection cleanup on disconnect
 */
public class ConnectionHandler {
    
    private static final Logger LOGGER = LoggerFactory.getLogger("connection-handler");
    
    private final RequestProcessor requestProcessor;
    private final MissionController missionController;
    private final UploadManager uploadManager;
    
    /**
     * Listener interface for connection lifecycle events.
     */
    public interface ConnectionListener {
        /**
         * Called when a client connects.
         * @param clientSocket The client socket
         */
        void onConnect(Socket clientSocket);
        
        /**
         * Called when a client disconnects.
         * @param clientSocket The client socket
         */
        void onDisconnect(Socket clientSocket);
    }
    
    private ConnectionListener connectionListener;
    
    /**
     * Create a new ConnectionHandler.
     * 
     * @param requestProcessor The request processor for handling commands
     * @param missionController The mission controller for cleanup
     * @param uploadManager The upload manager for cleanup
     */
    public ConnectionHandler(
            RequestProcessor requestProcessor,
            MissionController missionController,
            UploadManager uploadManager) {
        this.requestProcessor = requestProcessor;
        this.missionController = missionController;
        this.uploadManager = uploadManager;
    }
    
    /**
     * Set the connection lifecycle listener.
     * 
     * @param listener The listener to receive connection events
     */
    public void setConnectionListener(ConnectionListener listener) {
        this.connectionListener = listener;
    }
    
    /**
     * Handle a client connection.
     * This method blocks until the client disconnects or an error occurs.
     * 
     * @param clientSocket The client socket to handle
     */
    public void handleClient(Socket clientSocket) {
        String clientId = clientSocket.getRemoteSocketAddress().toString();
        
        try (
            // 🔴 2026-10-22 用户报「你的游戏内回复变成乱码了，我看不了」：
            // 这两行原来**没有指定字符集**，于是走 JVM 平台默认（中文 Windows 上是 GBK），
            // 而 Python 侧 transport.py 发的是 UTF-8（`sendall(msg.encode("utf-8"))`）——
            // 中文请求体一进 Java 就被按 GBK 拆成乱码。两端必须钉死同一个编码。
            BufferedReader in = new BufferedReader(
                new InputStreamReader(clientSocket.getInputStream(), StandardCharsets.UTF_8));
            PrintWriter out = new PrintWriter(
                new OutputStreamWriter(clientSocket.getOutputStream(), StandardCharsets.UTF_8), true)
        ) {
            LOGGER.debug("Client connected: {}", clientId);
            
            if (connectionListener != null) {
                connectionListener.onConnect(clientSocket);
            }
            
            String line;
            while ((line = in.readLine()) != null) {
                try {
                    JsonObject response = requestProcessor.processRequest(line, clientSocket);
                    out.println(requestProcessor.toJson(response));
                } catch (Exception e) {
                    LOGGER.error("Error handling command from {}", clientId, e);
                    JsonObject errorResponse = createErrorResponse(line, e);
                    out.println(requestProcessor.toJson(errorResponse));
                }
            }
        } catch (SocketTimeoutException e) {
            LOGGER.warn("Client connection timeout: {}", clientId);
        } catch (IOException e) {
            LOGGER.debug("Client connection error: {}", clientId, e);
        } finally {
            cleanup(clientSocket);
            closeSocket(clientSocket);
            LOGGER.debug("Client disconnected: {}", clientId);
            
            if (connectionListener != null) {
                connectionListener.onDisconnect(clientSocket);
            }
        }
    }
    
    /**
     * Create an error response when request processing fails.
     * Attempts to extract the request ID from the raw line.
     * 
     * @param rawLine The raw request line
     * @param error The exception that occurred
     * @return A JsonObject error response
     */
    private JsonObject createErrorResponse(String rawLine, Exception error) {
        String requestId = null;
        
        // Try to extract ID from the raw line for error correlation
        try {
            JsonObject parsed = RequestProcessor.getGson().fromJson(rawLine, JsonObject.class);
            if (parsed != null && parsed.has("id")) {
                requestId = parsed.get("id").getAsString();
            }
        } catch (Exception parseEx) {
            // Ignore parse errors, ID will be null
        }
        
        return requestProcessor.createErrorResponse(error.getMessage(), requestId);
    }
    
    /**
     * Cleanup resources associated with a client connection.
     * 
     * @param clientSocket The client socket to cleanup
     */
    public void cleanup(Socket clientSocket) {
        try {
            // Release mission controller ownership
            if (missionController != null) {
                missionController.releaseOwner(clientSocket);
            }
            
            // Cleanup uploads owned by this client
            if (uploadManager != null) {
                uploadManager.cleanupClientUploads(clientSocket);
            }
        } catch (Exception e) {
            LOGGER.warn("Error during connection cleanup", e);
        }
    }
    
    /**
     * Close a client socket safely.
     * 
     * @param clientSocket The socket to close
     */
    private void closeSocket(Socket clientSocket) {
        try {
            if (!clientSocket.isClosed()) {
                clientSocket.close();
            }
        } catch (IOException e) {
            LOGGER.debug("Error closing client socket", e);
        }
    }
}
