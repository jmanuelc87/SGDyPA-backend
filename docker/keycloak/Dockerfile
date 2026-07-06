# Build the webhook event-listener SPI, then bake it into the Keycloak image.
# Keep the Keycloak version in sync with keycloak.version in spi/pom.xml.
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /spi
COPY spi/pom.xml ./pom.xml
# Pre-fetch dependencies so source-only changes don't re-download the world.
RUN mvn -q -B -e dependency:go-offline
COPY spi/src ./src
RUN mvn -q -B -e package

FROM quay.io/keycloak/keycloak:26.6.0
COPY --from=build /spi/target/keycloak-webhook-spi.jar /opt/keycloak/providers/
# Augment the image with the provider so it's registered at runtime.
RUN /opt/keycloak/bin/kc.sh build
