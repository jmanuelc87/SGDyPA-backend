package com.sgdypa.keycloak.webhook;

import org.keycloak.Config;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventListenerProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;

/**
 * Registers the {@code webhook} event-listener so it can be enabled in the realm
 * via {@code "eventsListeners": [..., "webhook"]}.
 *
 * <p>Configuration is read once from the environment at init time so a running
 * provider never touches {@code System.getenv} on the hot path:
 * <ul>
 *   <li>{@code WEBHOOK_TARGET_URL} — backend endpoint that receives admin events.</li>
 *   <li>{@code WEBHOOK_HMAC_SECRET} — shared secret used to sign the raw body.</li>
 *   <li>{@code WEBHOOK_SIGNATURE_HEADER} — header carrying the hex HMAC-SHA256
 *       (defaults to {@code X-Keycloak-Signature}).</li>
 * </ul>
 * When the URL or secret is missing the listener is inert (logs and skips) rather
 * than failing Keycloak startup.
 */
public class WebhookEventListenerProviderFactory implements EventListenerProviderFactory {

    public static final String PROVIDER_ID = "webhook";

    private String targetUrl;
    private String secret;
    private String signatureHeader;

    @Override
    public EventListenerProvider create(KeycloakSession session) {
        return new WebhookEventListenerProvider(targetUrl, secret, signatureHeader);
    }

    @Override
    public void init(Config.Scope config) {
        this.targetUrl = env("WEBHOOK_TARGET_URL", null);
        this.secret = env("WEBHOOK_HMAC_SECRET", null);
        this.signatureHeader = env("WEBHOOK_SIGNATURE_HEADER", "X-Keycloak-Signature");
    }

    private static String env(String key, String defaultValue) {
        String value = System.getenv(key);
        return (value == null || value.isBlank()) ? defaultValue : value;
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        // no-op
    }

    @Override
    public void close() {
        // no-op
    }

    @Override
    public String getId() {
        return PROVIDER_ID;
    }
}
