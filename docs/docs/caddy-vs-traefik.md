---
id: caddy-vs-traefik
title: Caddy vs Traefik
sidebar_position: 8
---

# Caddy vs Traefik

Reclio ships with [Caddy](https://caddyserver.com/) in the repo's
`docker-compose.yml`. You can swap in [Traefik](https://traefik.io/)
if it's a better fit for your environment.

## TL;DR

- **Solo Reclio deployment?** Keep **Caddy**. The `Caddyfile` is two
  lines, TLS auto-provisions, done.
- **Existing Traefik stack?** Use **Traefik** labels. No need to run
  two edge proxies on the same host.

## Side-by-side

|  | Caddy | Traefik |
| --- | --- | --- |
| Config format | `Caddyfile` (plain text, 2 lines for Reclio) | YAML or Docker labels |
| HTTPS | Automatic (ACME on first request) | Automatic (ACME resolver) |
| HTTP/2 + HTTP/3 | Built-in, default | Built-in, default |
| Dashboard | None out of the box | Yes, behind auth |
| Service discovery | Manual | Native Docker provider |
| Learning curve | Flat | Moderate |
| Best at | Single-service servers, first-timers | Multi-service clusters, many routers |

## Recommendation

| Your situation | Use |
| --- | --- |
| "I'm running Reclio on a small VPS." | **Caddy** (the default) |
| "I already have a Traefik router in front of five other services." | **Traefik** |
| "I run everything behind Cloudflare Tunnel." | Either — skip the cert dance, bind to `http://` inside the tunnel |

## Using Traefik

Remove the `caddy` service and the `Caddyfile` mount from
`docker-compose.yml`. Add Traefik labels to the `app` service, for
example:

```yaml
services:
  app:
    # ...existing config...
    labels:
      - traefik.enable=true
      - traefik.http.routers.reclio.rule=Host(`reclio.example.com`)
      - traefik.http.routers.reclio.entrypoints=websecure
      - traefik.http.routers.reclio.tls.certresolver=letsencrypt
      - traefik.http.services.reclio.loadbalancer.server.port=8000
    networks:
      - traefik_proxy

networks:
  traefik_proxy:
    external: true
```

Your Traefik instance needs an ACME resolver named `letsencrypt` and
an external network named `traefik_proxy` (or whatever you already
use). Everything else in Reclio — signed cookies, OAuth callback,
`/feeds` — works the same.
