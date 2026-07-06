package com.sgdypa.keycloak.webhook;

import org.jboss.logging.Logger;
import org.keycloak.events.Event;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.util.JsonSerialization;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * POSTs Keycloak admin events to the SGDyPA backend as raw admin-event JSON,
 * signed with HMAC-SHA256 over the exact request body.
 *
 * <p>The payload mirrors the fields Keycloak itself uses for admin events
 * ({@code resourceType}, {@code operationType}, {@code resourcePath},
 * {@code representation} as a JSON string) so the backend's
 * {@code parse_admin_event} can consume it without provider-specific mapping.
 *
 * <p>Delivery is fire-and-forget: failures are logged but never propagated, so a
 * webhook problem can never abort the Keycloak admin operation that triggered it.
 */
public class WebhookEventListenerProvider implements EventListenerProvider {

    private static final Logger LOG = Logger.getLogger(WebhookEventListenerProvider.class);

    // Shared across providers: the client owns its own connection pool + executor.
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    private final String targetUrl;
    private final String secret;
    private final String signatureHeader;

    public WebhookEventListenerProvider(String targetUrl, String secret, String signatureHeader) {
        this.targetUrl = targetUrl;
        this.secret = secret;
        this.signatureHeader = signatureHeader;
    }

    @Override
    public void onEvent(Event event) {
        // Login / user-facing events are outside the replication contract.
    }

    @Override
    public void onEvent(AdminEvent event, boolean includeRepresentation) {
        if (targetUrl == null || secret == null) {
            LOG.warn("webhook: WEBHOOK_TARGET_URL or WEBHOOK_HMAC_SECRET not set; skipping admin event");
            return;
        }

        try {
            String operationType = event.getOperationType() == null
                    ? null
                    : event.getOperationType().name();

            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("id", event.getId());
            payload.put("time", event.getTime());
            payload.put("realmId", event.getRealmId());
            payload.put("resourceType", event.getResourceTypeAsString());
            payload.put("operationType", operationType);
            payload.put("resourcePath", event.getResourcePath());
            // Keycloak carries the representation as a JSON string; forward as-is.
            payload.put("representation", event.getRepresentation());
            payload.put("error", event.getError());
            payload.put("type", "admin." + event.getResourceTypeAsString() + "-" + operationType);

            byte[] body = JsonSerialization.writeValueAsBytes(payload);
            String signature = hmacSha256Hex(secret, body);

            HttpRequest request = HttpRequest.newBuilder(URI.create(targetUrl))
                    .timeout(Duration.ofSeconds(10))
                    .header("Content-Type", "application/json")
                    .header(signatureHeader, signature)
                    .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                    .build();

            HTTP.sendAsync(request, HttpResponse.BodyHandlers.ofString())
                    .whenComplete((response, error) -> {
                        if (error != null) {
                            LOG.errorf(error, "webhook: failed to POST admin event %s", event.getId());
                        } else if (response.statusCode() >= 300) {
                            LOG.warnf("webhook: backend returned %d for admin event %s: %s",
                                    response.statusCode(), event.getId(), response.body());
                        } else {
                            LOG.debugf("webhook: delivered admin event %s (%d)",
                                    event.getId(), response.statusCode());
                        }
                    });
        } catch (Exception e) {
            LOG.errorf(e, "webhook: error building/sending admin event %s", event.getId());
        }
    }

    @Override
    public void close() {
        // no-op
    }

    private static String hmacSha256Hex(String secret, byte[] body) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        byte[] raw = mac.doFinal(body);
        StringBuilder hex = new StringBuilder(raw.length * 2);
        for (byte b : raw) {
            hex.append(Character.forDigit((b >> 4) & 0xF, 16));
            hex.append(Character.forDigit(b & 0xF, 16));
        }
        return hex.toString();
    }
}
