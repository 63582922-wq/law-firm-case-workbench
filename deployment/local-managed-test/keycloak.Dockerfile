ARG KEYCLOAK_IMAGE=quay.io/keycloak/keycloak:26.7.0
FROM ${KEYCLOAK_IMAGE}

# Freeze all build-time options used by the managed runtime.  `start
# --optimized` can then keep the final root filesystem read-only instead of
# attempting a Quarkus augmentation inside the running identity container.
ENV KC_DB=postgres
ENV KC_HEALTH_ENABLED=true
RUN /opt/keycloak/bin/kc.sh build
