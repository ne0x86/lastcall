# Egress proxy: HTTPS CONNECT only, and only to the allowlist in `filter`. Runners live on an internal network with
# no way out of their own, so a manipulated agent cannot reach the LAN or send data to an arbitrary domain.
FROM alpine:3.20
RUN apk add --no-cache tinyproxy
COPY tinyproxy.conf filter /etc/tinyproxy/
USER tinyproxy
EXPOSE 8888
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
