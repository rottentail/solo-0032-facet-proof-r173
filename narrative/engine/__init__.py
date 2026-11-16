"""Offline render-progress narrative engine for Facet Proof.

The engine drives the real Facet Proof application over its documented
JSON routes and records every observation. It never reaches the network:
the HTTP entrypoint ``routing.route_request`` is called in-process, so the
recorded requests/responses are byte-for-byte the same JSON the bundled
server would exchange on 127.0.0.1.
"""
